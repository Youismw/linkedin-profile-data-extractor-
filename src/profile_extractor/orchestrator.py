"""Pipeline orchestrator for LinkedIn profile extraction.

Module Architecture & Inter-Module Communication:
==================================================
This module serves as the central conductor for the entire extraction pipeline.
It coordinates candidate spreadsheet ingestion, polite request pacing, headless
browser navigation, two-pass deep detail discovery, HTML parsing, failure auditing,
atomic file serialization, and statistical reconciliation.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.cli`: Calls `run_pipeline(config, fetcher)` to launch the job.
   - Test suites: `tests/test_walking_skeleton.py`, `tests/test_integration_hardening.py`,
     `tests/test_additive_detail_crawl.py`.

2. Downstream Modules Called:
   - `profile_extractor.reader`:
     * Calls `read_profile_urls(config.input_file)` to parse candidate spreadsheet links.
   - `profile_extractor.fetcher` (`PlaywrightFetcher` or `FakeFetcher`):
     * Calls `fetcher.fetch(url)` to retrieve profile and sub-page HTML over authenticated Chromium.
     * Calls `fetcher.close()` in a `finally` block for deterministic browser teardown.
   - `profile_extractor.parser`:
     * Calls `parse_profile()` to parse top card, experience, education, skills, about, and metadata.
     * Calls `parse_skills_details()`, `parse_experience_details()`, `parse_education_details()`,
       `parse_certifications_details()`, `parse_projects_details()`, `parse_languages_details()`.
   - `profile_extractor.writer`:
     * Calls `write_profile_json()` to store individual candidate JSON documents in `output/profiles/`.
     * Calls `write_unreachable_report()` to generate audit spreadsheets (`unreachable_profiles.xlsx`).
     * Calls `write_run_summary()` to write reconciliation totals (`run_summary.json`).
   - `profile_extractor.models`:
     * Constructs `RunSummary`, `UnreachableRecord`, `Status`, `Reason`, `OtherSection`.
"""

import concurrent.futures
import hashlib
import random
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FakeFetcher, Fetcher
from profile_extractor.fetcher.login import ensure_worker_logins, get_worker_auth_path
from profile_extractor.fetcher.playwright_fetcher import PlaywrightFetcher
from profile_extractor.logging_setup import get_logger
from profile_extractor.models import (
    ContactInfo,
    OtherSection,
    Reason,
    RunSummary,
    Status,
    UnreachableRecord,
)
from profile_extractor.parser import (
    _is_placeholder_value,
    _merge_complementary_texts,
    _section_text_subsumed,
    consolidate_other_sections,
    normalize_section_heading_key,
    parse_certifications_details,
    parse_contact_info,
    parse_education_details,
    parse_experience_details,
    parse_languages_details,
    parse_profile,
    parse_projects_details,
    parse_skills_details,
)
from profile_extractor.reader import InputRow, read_profile_urls
from profile_extractor.writer import (
    profile_filename_stem,
    write_profile_json,
    write_run_summary,
    write_unreachable_report,
)


T = TypeVar("T")


def partition_workload(items: Sequence[T], num_workers: int) -> list[list[T]]:
    """Partition items across num_workers using fair base + remainder division.

    The first remainder items are distributed to earlier workers so no items are dropped.
    Example: 13 items across 3 workers -> [5, 4, 4]
    """
    if not items:
        return []
    if num_workers <= 1:
        return [list(items)]
    k = min(num_workers, len(items))
    base = len(items) // k
    rem = len(items) % k
    partitions: list[list[T]] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < rem else 0)
        partitions.append(list(items[start : start + size]))
        start += size
    return partitions


@dataclass
class WorkerResult:
    """Outcome aggregated from a single worker partition execution."""

    ok_count: int = 0
    unreachable_count: int = 0
    unreachable_by_reason: dict[str, int] = field(default_factory=dict)
    unreachable_records: list[UnreachableRecord] = field(default_factory=list)
    completed_rows: int = 0
    not_processed_count: int = 0
    stop_triggered: bool = False
    interrupted: bool = False


def _count_field_richness(items: Any) -> int:
    """Calculate total non-empty information score across a collection of items."""
    if not isinstance(items, (list, tuple)):
        return 0
    score = 0
    for it in items:
        if isinstance(it, str):
            if it.strip():
                score += 1
        elif hasattr(it, "model_dump"):
            dump = it.model_dump(exclude_none=True)
            score += len(dump)
    return score




def _merge_skill_items(current: Any, detailed: Any) -> list[str]:
    """Merge main-profile and deep-page skills without discarding either source.

    The profile page and /details/skills/ page can expose different subsets while
    LinkedIn is still hydrating or virtualizing its list. Keep every existing
    profile-page skill in its original order, then append unseen detail-page
    skills. Comparison is case-insensitive, but the first-seen spelling wins.
    """
    merged = [item for item in current if isinstance(item, str)] if isinstance(
        current, (list, tuple)
    ) else []
    seen = {" ".join(item.split()).casefold() for item in merged if item.strip()}

    if isinstance(detailed, (list, tuple)):
        for item in detailed:
            if not isinstance(item, str):
                continue
            clean = " ".join(item.split())
            if not clean:
                continue
            key = clean.casefold()
            if key not in seen:
                merged.append(clean)
                seen.add(key)
    return merged


def _merge_contact_info(base: ContactInfo, incoming: ContactInfo) -> ContactInfo:
    """Merge two ContactInfo instances, preferring non-empty incoming values."""
    combined_websites: list[str] = []
    seen_websites: set[str] = set()
    for w in (base.websites or []) + (incoming.websites or []):
        w_clean = w.strip()
        if w_clean and w_clean.lower() not in seen_websites:
            combined_websites.append(w_clean)
            seen_websites.add(w_clean.lower())

    raw_parts = [r for r in [base.raw, incoming.raw] if r and r.strip()]
    return ContactInfo(
        profile_url=incoming.profile_url or base.profile_url,
        email=incoming.email or base.email,
        phone=incoming.phone or base.phone,
        address=incoming.address or base.address,
        websites=combined_websites or None,
        twitter=incoming.twitter or base.twitter,
        birthday=incoming.birthday or base.birthday,
        connected=incoming.connected or base.connected,
        raw="\n\n".join(raw_parts),
    )



# The supplemental pass is intentionally bounded. The original six-page path
# remains unchanged; this cap only applies to previously-unhandled same-profile
# section links discovered after that pass.
MAX_SUPPLEMENTAL_DETAIL_PAGES = 12
_DETAIL_ROUTE_SEGMENT_RE = re.compile(r"^[a-z0-9_-]{1,80}$", re.IGNORECASE)


def _normalised_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _discover_profile_detail_pages(
    html: str,
    profile_url: str,
    slug: str,
    max_pages: int = MAX_SUPPLEMENTAL_DETAIL_PAGES,
) -> list[tuple[str, str]]:
    """Discover bounded, same-profile LinkedIn detail/activity routes only.

    This deliberately does not crawl links found on discovered pages. It only
    considers direct links from the initial profile HTML, and only accepts the
    exact current profile's /details/<section>/ or /recent-activity/{all,comments}/
    routes. Query strings and fragments are removed for URL deduplication.
    """
    if not html or max_pages <= 0:
        return []
    try:
        source = urlsplit(profile_url)
        source_host = (source.hostname or "").lower().removeprefix("www.")
        if source_host != "linkedin.com":
            return []
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("main") or soup
    except (TypeError, ValueError):
        return []

    canonical_slug = quote(unquote(slug), safe="-._")
    results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for anchor in root.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            joined = urljoin(profile_url, href)
            parts = urlsplit(joined)
            host = (parts.hostname or "").lower().removeprefix("www.")
            if parts.scheme.lower() not in {"http", "https"} or host != "linkedin.com":
                continue
            if parts.username or parts.password or parts.port not in (None, 80, 443):
                continue
            segments = [unquote(segment) for segment in parts.path.split("/") if segment]
        except (TypeError, ValueError):
            continue

        section: str | None = None
        canonical_path: str | None = None
        if (
            len(segments) == 4
            and segments[0].casefold() == "in"
            and segments[1].casefold() == unquote(slug).casefold()
            and segments[2].casefold() == "details"
            and _DETAIL_ROUTE_SEGMENT_RE.fullmatch(segments[3])
        ):
            section = segments[3].casefold()
            canonical_path = f"/in/{canonical_slug}/details/{quote(section, safe='-_')}/"
        elif (
            len(segments) in {3, 4}
            and segments[0].casefold() == "in"
            and segments[1].casefold() == unquote(slug).casefold()
            and segments[2].casefold() == "recent-activity"
            and (len(segments) == 3 or segments[3].casefold() in {"all", "comments"})
        ):
            section = "recent-activity"
            suffix = f"{segments[3].casefold()}/" if len(segments) == 4 else ""
            canonical_path = f"/in/{canonical_slug}/recent-activity/{suffix}"
        elif (
            len(segments) == 4
            and segments[0].casefold() == "in"
            and segments[1].casefold() == unquote(slug).casefold()
            and segments[2].casefold() == "overlay"
            and segments[3].casefold() == "contact-info"
        ):
            section = "contact_info"
            canonical_path = f"/in/{canonical_slug}/overlay/contact-info/"

        if not section or not canonical_path:
            continue
        # Keep query parameters that can select meaningful LinkedIn content
        # (e.g. initialTabId, vieweeProfileId), but drop known tracking noise.
        meaningful_query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"trk", "lipi", "licu", "original_referer"}
        ]
        query = urlencode(sorted(meaningful_query))
        canonical_url = f"https://www.linkedin.com{canonical_path}"
        if query:
            canonical_url = f"{canonical_url}?{query}"
        if canonical_url in seen_urls:
            continue
        seen_urls.add(canonical_url)
        results.append((section, canonical_url))
        if len(results) >= max_pages:
            break

    return results


def _extract_supplemental_page_text(html: str, section: str) -> tuple[str, str]:
    """Extract visible main text and a useful heading from a detail page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    for tag in soup.select("nav, footer, aside, [role='dialog'], .artdeco-modal"):
        tag.decompose()
    main = soup.find("main") or soup.body or soup

    expected = "activity" if section == "recent-activity" else section.replace("-", " ").replace("_", " ")
    heading = ""
    candidates = [
        " ".join(tag.get_text(" ", strip=True).split())
        for tag in main.find_all(["h1", "h2", "h3"])
    ]
    for candidate in candidates:
        if candidate and _normalised_text(candidate) == expected:
            heading = candidate
            break
    if not heading:
        for tag in main.find_all(["h2", "h3"]):
            candidate = " ".join(tag.get_text(" ", strip=True).split())
            if candidate:
                heading = candidate
                break
    if not heading:
        for candidate in candidates:
            if candidate:
                heading = candidate
                break
    if not heading:
        heading = "Activity" if section == "recent-activity" else " ".join(
            part.capitalize() for part in section.replace("_", "-").split("-")
        )

    text = main.get_text(separator="\n", strip=True)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _normalised_text(line) == _normalised_text(heading):
            del lines[index]
            break
    return "\n".join(lines).strip(), heading


def _unique_lines(existing: str, incoming: str) -> str:
    """Append only genuinely new normalized lines, retaining original wording."""
    old = existing or ""
    seen = {_normalised_text(line) for line in old.splitlines() if _normalised_text(line)}
    additions: list[str] = []
    for line in (incoming or "").splitlines():
        key = _normalised_text(line)
        if key and key not in seen:
            additions.append(line)
            seen.add(key)
    if not additions:
        return old
    return f"{old.rstrip()}\n{chr(10).join(additions)}".strip()


def _append_supplemental_raw(
    raw_text: str | None, heading: str, url: str, text: str
) -> str:
    """Add one detail-page text dump, deduplicated by canonical URL and lines."""
    current = raw_text or ""
    marker = f"--- Supplemental subpage: {heading} | {url} ---"
    if marker in current:
        return current
    # Recompute only lines not already present in the existing raw dump.
    old_keys = {_normalised_text(line) for line in current.splitlines() if _normalised_text(line)}
    additions: list[str] = []
    for line in (text or "").splitlines():
        key = _normalised_text(line)
        if key and key not in old_keys:
            additions.append(line)
            old_keys.add(key)
    payload = "\n".join(additions)
    return f"{current.rstrip()}\n\n{marker}\n{payload}".strip()


def _record_matches(section: str, left: Any, right: Any) -> bool:
    """Conservatively identify duplicate structured records across two sources."""
    if isinstance(left, str) or isinstance(right, str):
        return _normalised_text(left) == _normalised_text(right)
    if not hasattr(left, "model_dump") or not hasattr(right, "model_dump"):
        return _normalised_text(left) == _normalised_text(right)

    a = left.model_dump(exclude_none=True)
    b = right.model_dump(exclude_none=True)

    def value(source: dict[str, Any], field: str) -> str:
        return _normalised_text(source.get(field))

    def compatible(field: str) -> bool:
        av, bv = value(a, field), value(b, field)
        return not (av and bv and av != bv)

    if section == "experience":
        return (
            bool(value(a, "title"))
            and value(a, "title") == value(b, "title")
            and compatible("company")
            and compatible("dates")
            and bool(
                (value(a, "company") and value(b, "company"))
                or (value(a, "dates") and value(b, "dates"))
            )
        )
    if section == "education":
        return (
            bool(value(a, "school"))
            and value(a, "school") == value(b, "school")
            and compatible("degree")
            and compatible("dates")
            and bool(
                (value(a, "degree") and value(b, "degree"))
                or (value(a, "dates") and value(b, "dates"))
            )
        )
    if section == "certifications":
        left_id, right_id = value(a, "credential_id"), value(b, "credential_id")
        if left_id and right_id:
            return left_id == right_id
        return (
            bool(value(a, "name"))
            and value(a, "name") == value(b, "name")
            and compatible("issuer")
        )
    if section == "projects":
        left_url, right_url = value(a, "url"), value(b, "url")
        if left_url and right_url:
            return left_url == right_url
        return (
            bool(value(a, "title"))
            and value(a, "title") == value(b, "title")
            and compatible("dates")
        )
    if section == "languages":
        return bool(value(a, "name")) and value(a, "name") == value(b, "name")
    return bool(value(a, "raw")) and value(a, "raw") == value(b, "raw")


def _merge_matching_record(existing: Any, incoming: Any) -> None:
    """Fill empty fields on a match; never replace populated scalar content."""
    if not hasattr(existing, "model_dump") or not hasattr(incoming, "model_dump"):
        return
    for field, value in incoming.model_dump(exclude_none=True).items():
        current = getattr(existing, field, None)
        if value is None or value == "" or value == []:
            continue
        if current is None or current == "" or current == []:
            setattr(existing, field, value)
        elif isinstance(current, list) and isinstance(value, list):
            existing_keys = {_normalised_text(item) for item in current}
            current.extend(
                item for item in value if _normalised_text(item) and _normalised_text(item) not in existing_keys
            )
        elif field == "raw" and isinstance(current, str) and isinstance(value, str):
            setattr(existing, field, _unique_lines(current, value))


def _merge_collection_additively(section: str, current: Any, additions: Any) -> list[Any]:
    """Union structured records while preserving current order and values."""
    merged = [
        item.model_copy(deep=True) if hasattr(item, "model_copy") else item
        for item in (current or [])
    ]
    for candidate in additions or []:
        match = next((item for item in merged if _record_matches(section, item, candidate)), None)
        if match is None:
            merged.append(
                candidate.model_copy(deep=True)
                if hasattr(candidate, "model_copy")
                else candidate
            )
        else:
            _merge_matching_record(match, candidate)
    return merged


def _restore_profile_snapshot_additively(profile: Any, baseline: Any) -> None:
    """Restore any original profile-page records removed by legacy deep replacement."""
    for field in (
        "experience", "education", "skills", "certifications", "projects", "languages"
    ):
        current = getattr(profile, field, None)
        original = getattr(baseline, field, None)
        if current is not None or original is not None:
            if field == "skills":
                setattr(profile, field, _merge_skill_items(current, original))
            else:
                setattr(profile, field, _merge_collection_additively(field, current, original))
    for field in ("name", "headline", "location", "about"):
        if not getattr(profile, field, None) and getattr(baseline, field, None):
            setattr(profile, field, getattr(baseline, field))


def _append_other_section_additively(
    sections: list[OtherSection], heading: str, text: str
) -> None:
    """Append a new section or add only unseen lines to an existing one."""
    clean_heading = " ".join((heading or "").split()).strip() or "Additional Information"
    clean_text = (text or "").strip()
    if not clean_text:
        return
    norm_key = normalize_section_heading_key(clean_heading)
    for i, existing in enumerate(sections):
        if normalize_section_heading_key(existing.heading) == norm_key:
            existing_text = existing.text or ""
            # If incoming text subsumes existing, update with richer content
            if _section_text_subsumed(existing_text, clean_text):
                best_heading = (
                    clean_heading
                    if not re.search(r"\(\s*\d+\s*\)$", clean_heading)
                    else existing.heading
                )
                sections[i] = OtherSection(heading=best_heading, text=clean_text)
                return
            # If existing text already subsumes incoming, keep existing
            elif _section_text_subsumed(clean_text, existing_text):
                if re.search(r"\(\s*\d+\s*\)$", existing.heading) and not re.search(
                    r"\(\s*\d+\s*\)$", clean_heading
                ):
                    existing.heading = clean_heading
                return
            # Complementary content: merge unique lines
            else:
                existing.text = _merge_complementary_texts(existing_text, clean_text)
                if re.search(r"\(\s*\d+\s*\)$", existing.heading) and not re.search(
                    r"\(\s*\d+\s*\)$", clean_heading
                ):
                    existing.heading = clean_heading
                return
    sections.append(OtherSection(heading=clean_heading, text=clean_text))


def _recalculate_missing_fields(doc: Any, important_fields: list[str]) -> None:
    profile_data = doc.profile.model_dump(exclude_none=True)
    missing: list[str] = []
    for field in important_fields:
        value = profile_data.get(field)
        if value is None or (isinstance(value, list) and not value):
            missing.append(field)
        elif isinstance(value, str) and _is_placeholder_value(value):
            missing.append(field)
    doc.meta.missing_fields = missing


def _process_candidate_batch(
    rows: Sequence[InputRow],
    config: ExtractorConfig,
    fetcher: Fetcher,
    filename_stems: dict[int, str],
    profiles_dir: Path,
    sleep_fn: Callable[[float], None],
    worker_idx: int = 0,
    total_workers: int = 1,
    stop_event: threading.Event | None = None,
) -> WorkerResult:
    """Execute sequential extraction loop for one worker partition of candidate rows."""
    logger = get_logger()
    res = WorkerResult()
    num_rows = len(rows)
    prefix = f"[Worker {worker_idx + 1}] " if total_workers > 1 else ""

    try:
        for idx, row in enumerate(rows):
            if (stop_event and stop_event.is_set()) or res.stop_triggered:
                res.not_processed_count += 1
                logger.info(
                    "%sRow %d: skipped (pipeline stopped after block)", prefix, row.row_number
                )
                continue

            slug = row.slug or ""
            url = row.normalized or ""
            logger.info(
                "%sRow %d: visiting candidate profile (%d of %d)...",
                prefix,
                row.row_number,
                idx + 1,
                num_rows,
            )
            logger.debug("%sRow %d: candidate slug '%s'", prefix, row.row_number, slug)

            try:
                fetch_res = fetcher.fetch(url)

                if fetch_res.status == Status.OK:
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.OK,
                        important_fields=config.important_fields,
                    )
                    baseline_profile = doc.profile.model_copy(deep=True)

                    if getattr(config, "deep_details", True) and fetch_res.html:
                        main_html = fetch_res.html
                        legacy_attempted_sections: set[str] = set()
                        detail_specs = [
                            (
                                "contact_info",
                                [
                                    r"/overlay/contact-info/",
                                    r"\bcontact\s+info\b",
                                ],
                                parse_contact_info,
                            ),
                            (
                                "skills",
                                [
                                    r"/details/skills/",
                                    r"show all\s*(?:\d+)?\s*skills",
                                    r"skills\s*\(\d+\)",
                                ],
                                parse_skills_details,
                            ),
                            (
                                "education",
                                [r"/details/education/", r"show all\s*(?:\d+)?\s*education"],
                                parse_education_details,
                            ),
                            (
                                "experience",
                                [r"/details/experience/", r"show all\s*(?:\d+)?\s*experience"],
                                parse_experience_details,
                            ),
                            (
                                "certifications",
                                [
                                    r"/details/certifications/",
                                    r"show all\s*(?:\d+)?\s*certifications",
                                    r"licenses\s*(?:&|and)\s*certifications",
                                ],
                                parse_certifications_details,
                            ),
                            (
                                "projects",
                                [
                                    r"/details/projects/",
                                    r"show all\s*(?:\d+)?\s*projects",
                                    r"projects\s*\(\d+\)",
                                ],
                                parse_projects_details,
                            ),
                            (
                                "languages",
                                [
                                    r"/details/languages/",
                                    r"show all\s*(?:\d+)?\s*languages",
                                    r"languages\s*\(\d+\)",
                                ],
                                parse_languages_details,
                            ),
                        ]
                        for section, patterns, parse_fn in detail_specs:
                            if (stop_event and stop_event.is_set()) or res.stop_triggered:
                                break
                            if not any(re.search(pat, main_html, re.I) for pat in patterns):
                                continue
                            legacy_attempted_sections.add(section)
                            if section == "contact_info":
                                detail_url = f"https://www.linkedin.com/in/{slug}/overlay/contact-info/"
                            else:
                                detail_url = f"https://www.linkedin.com/in/{slug}/details/{section}/"
                            sleep_fn(
                                random.uniform(config.delay_seconds_min, config.delay_seconds_max)
                            )
                            logger.info(
                                "%sRow %d: unrolling full %s list...",
                                prefix,
                                row.row_number,
                                section,
                            )
                            logger.debug(
                                "%sRow %d: fetching subpage %s",
                                prefix,
                                row.row_number,
                                detail_url,
                            )
                            try:
                                detail_res = fetcher.fetch(detail_url)
                            except KeyboardInterrupt:
                                raise
                            except Exception as detail_err:
                                logger.warning(
                                    "%sRow %d: could not fetch %s subpage: %s",
                                    prefix,
                                    row.row_number,
                                    section,
                                    detail_err,
                                )
                                continue
                            if detail_res.status != Status.OK:
                                logger.warning(
                                    "%sRow %d: %s subpage unreachable (%s)",
                                    prefix,
                                    row.row_number,
                                    section,
                                    detail_res.reason.value if detail_res.reason else "unknown",
                                )
                                if config.stop_on_block and detail_res.reason == Reason.BLOCKED:
                                    logger.warning(
                                        "%sRow %d: Block detected on %s subpage! Halting run "
                                        "as stop_on_block=True",
                                        prefix,
                                        row.row_number,
                                        section,
                                    )
                                    res.stop_triggered = True
                                    if stop_event is not None:
                                        stop_event.set()
                                continue
                            if not detail_res.html:
                                continue

                            # Preserve subpage raw text in doc.raw_text for recovery audit
                            sub_soup = BeautifulSoup(detail_res.html, "lxml")
                            for s_tag in sub_soup.find_all(
                                ["script", "style", "noscript", "svg", "template"]
                            ):
                                s_tag.decompose()
                            if section == "contact_info":
                                modal = (
                                    sub_soup.select_one(
                                        '[role="dialog"], .artdeco-modal, #artdeco-modal-outlet, '
                                        'section.pv-contact-info, .pv-profile-section__section-info'
                                    )
                                    or sub_soup.find("main")
                                    or sub_soup
                                )
                                sub_text = modal.get_text(separator="\n", strip=True)
                            else:
                                sub_text = (sub_soup.find("main") or sub_soup).get_text(
                                    separator="\n", strip=True
                                )
                            if sub_text:
                                doc.raw_text = (
                                    (doc.raw_text or "")
                                    + f"\n\n--- Subpage: {section} ---\n"
                                    + sub_text
                                )

                            deep_items = parse_fn(detail_res.html)
                            if section == "contact_info":
                                if isinstance(deep_items, ContactInfo):
                                    existing_ci = doc.profile.contact_info
                                    if existing_ci is None:
                                        doc.profile.contact_info = deep_items
                                    else:
                                        doc.profile.contact_info = _merge_contact_info(
                                            existing_ci, deep_items
                                        )
                                    logger.info(
                                        "%sRow %d: unrolled %s from overlay modal",
                                        prefix,
                                        row.row_number,
                                        section,
                                    )
                                else:
                                    logger.warning(
                                        "%sRow %d: could not extract contact_info from overlay modal HTML",
                                        prefix,
                                        row.row_number,
                                    )
                                continue

                            current_items = getattr(doc.profile, section)
                            current_count = (
                                len(current_items)
                                if isinstance(current_items, (list, tuple))
                                else 0
                            )
                            if (
                                isinstance(deep_items, (list, tuple))
                                and len(deep_items) > 0
                            ):
                                if section == "skills":
                                    merged_skills = _merge_skill_items(current_items, deep_items)
                                    if len(merged_skills) > current_count:
                                        logger.info(
                                            "%sRow %d: merged %d skills (was %d; added %d from details page)",
                                            prefix,
                                            row.row_number,
                                            len(merged_skills),
                                            current_count,
                                            len(merged_skills) - current_count,
                                        )
                                        doc.profile.skills = merged_skills
                                else:
                                    deep_score = _count_field_richness(deep_items)
                                    current_score = _count_field_richness(current_items)
                                    if (
                                        len(deep_items) >= current_count
                                        or deep_score >= current_score
                                    ):
                                        logger.info(
                                            "%sRow %d: unrolled %d total %s (was %d, score %d vs %d)",
                                            prefix,
                                            row.row_number,
                                            len(deep_items),
                                            section,
                                            current_count,
                                            deep_score,
                                            current_score,
                                        )
                                        setattr(doc.profile, section, deep_items)

                        _restore_profile_snapshot_additively(doc.profile, baseline_profile)

                        if not (stop_event and stop_event.is_set()) and not res.stop_triggered:
                            parser_by_section = {
                                "contact_info": ("contact_info", parse_contact_info),
                                "contact-info": ("contact_info", parse_contact_info),
                                "skills": ("skills", parse_skills_details),
                                "education": ("education", parse_education_details),
                                "experience": ("experience", parse_experience_details),
                                "certifications": ("certifications", parse_certifications_details),
                                "licenses": ("certifications", parse_certifications_details),
                                "licenses-and-certifications": (
                                    "certifications", parse_certifications_details
                                ),
                                "projects": ("projects", parse_projects_details),
                                "languages": ("languages", parse_languages_details),
                            }
                            legacy_aliases = {
                                "contact-info": "contact_info",
                                "licenses": "certifications",
                                "licenses-and-certifications": "certifications",
                            }
                            legacy_attempted_normalized = {
                                legacy_aliases.get(item, item)
                                for item in legacy_attempted_sections
                            }
                            discovered_pages = (
                                []
                                if getattr(config, "disextra", False)
                                else _discover_profile_detail_pages(main_html, url, slug)
                            )
                            profile_text, _ = _extract_supplemental_page_text(main_html, "profile")
                            profile_text_norm = _normalised_text(profile_text)
                            supplemental_attempts = 0

                            for section, detail_url in discovered_pages:
                                target_field = legacy_aliases.get(section, section)
                                if target_field in legacy_attempted_normalized:
                                    continue
                                if supplemental_attempts >= MAX_SUPPLEMENTAL_DETAIL_PAGES:
                                    logger.warning(
                                        "%sRow %d: supplemental detail-page limit (%d) reached",
                                        prefix,
                                        row.row_number,
                                        MAX_SUPPLEMENTAL_DETAIL_PAGES,
                                    )
                                    break

                                supplemental_attempts += 1
                                sleep_fn(
                                    random.uniform(
                                        config.delay_seconds_min,
                                        config.delay_seconds_max,
                                    )
                                )
                                logger.info(
                                    "%sRow %d: additive discovery fetching '%s'...",
                                    prefix,
                                    row.row_number,
                                    section,
                                )
                                try:
                                    supplemental_res = fetcher.fetch(detail_url)
                                except KeyboardInterrupt:
                                    raise
                                except Exception as supplemental_error:
                                    logger.warning(
                                        "%sRow %d: supplemental page '%s' failed: %s",
                                        prefix,
                                        row.row_number,
                                        section,
                                        supplemental_error,
                                    )
                                    continue

                                if supplemental_res.status != Status.OK:
                                    logger.warning(
                                        "%sRow %d: supplemental page '%s' unreachable (%s)",
                                        prefix,
                                        row.row_number,
                                        section,
                                        supplemental_res.reason.value
                                        if supplemental_res.reason else "unknown",
                                    )
                                    if (
                                        config.stop_on_block
                                        and supplemental_res.reason == Reason.BLOCKED
                                    ):
                                        logger.warning(
                                            "%sRow %d: block detected on supplemental page '%s'; halting run",
                                            prefix,
                                            row.row_number,
                                            section,
                                        )
                                        res.stop_triggered = True
                                        if stop_event is not None:
                                            stop_event.set()
                                        break
                                    continue
                                if not supplemental_res.html:
                                    continue

                                page_text, page_heading = _extract_supplemental_page_text(
                                    supplemental_res.html, section
                                )
                                if not page_text:
                                    continue
                                page_norm = _normalised_text(page_text)
                                if page_norm and profile_text_norm and (
                                    page_norm == profile_text_norm
                                    or page_norm in profile_text_norm
                                ):
                                    continue

                                heading = page_heading or (
                                    "Activity" if section == "recent-activity"
                                    else " ".join(
                                        part.capitalize()
                                        for part in section.replace("_", "-").split("-")
                                    )
                                )
                                doc.raw_text = _append_supplemental_raw(
                                    doc.raw_text, heading, detail_url, page_text
                                )

                                if section == "about":
                                    if not doc.profile.about:
                                        doc.profile.about = page_text
                                    continue

                                parser_spec = parser_by_section.get(section)
                                if parser_spec is not None:
                                    field_name, parser_fn = parser_spec
                                    supplemental_items = parser_fn(supplemental_res.html)
                                    if supplemental_items:
                                        if field_name == "contact_info" and isinstance(
                                            supplemental_items, ContactInfo
                                        ):
                                            existing_ci = doc.profile.contact_info
                                            doc.profile.contact_info = (
                                                _merge_contact_info(
                                                    existing_ci, supplemental_items
                                                )
                                                if existing_ci is not None
                                                else supplemental_items
                                            )
                                        else:
                                            current_items = getattr(doc.profile, field_name, None)
                                            merged_items = _merge_collection_additively(
                                                field_name, current_items, supplemental_items
                                            )
                                            setattr(doc.profile, field_name, merged_items)
                                    continue

                                _append_other_section_additively(
                                    doc.other_sections, heading, page_text
                                )

                        doc.other_sections = consolidate_other_sections(doc.other_sections)
                        _recalculate_missing_fields(doc, config.important_fields)

                    write_profile_json(doc, profiles_dir, filename_stems.get(row.row_number))

                    if doc.meta.status == Status.OK:
                        res.ok_count += 1
                        logger.info("%sRow %d: ok", prefix, row.row_number)
                    else:
                        reason = doc.meta.reason or Reason.PARSE_FAILED
                        res.unreachable_count += 1
                        res.unreachable_by_reason[reason.value] = (
                            res.unreachable_by_reason.get(reason.value, 0) + 1
                        )
                        res.unreachable_records.append(
                            UnreachableRecord(
                                input_row=row.row_number,
                                url_as_given=row.raw,
                                slug=slug,
                                reason=reason,
                                detail="Parse completed without extracting candidate name/headline",
                            )
                        )
                        logger.info("%sRow %d: unreachable (%s)", prefix, row.row_number, reason.value)

                else:
                    reason = fetch_res.reason or Reason.UNEXPECTED_ERROR
                    res.unreachable_count += 1
                    res.unreachable_by_reason[reason.value] = (
                        res.unreachable_by_reason.get(reason.value, 0) + 1
                    )
                    res.unreachable_records.append(
                        UnreachableRecord(
                            input_row=row.row_number,
                            url_as_given=row.raw,
                            slug=slug,
                            reason=reason,
                            detail=fetch_res.detail,
                        )
                    )
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.UNREACHABLE,
                        important_fields=config.important_fields,
                        reason=reason.value,
                    )
                    write_profile_json(doc, profiles_dir, filename_stems.get(row.row_number))
                    logger.info("%sRow %d: unreachable (%s)", prefix, row.row_number, reason.value)

                    if config.stop_on_block and reason == Reason.BLOCKED:
                        logger.warning(
                            "%sRow %d: Block detected! Halting run as stop_on_block=True",
                            prefix,
                            row.row_number,
                        )
                        res.stop_triggered = True
                        if stop_event is not None:
                            stop_event.set()

            except Exception as row_exc:
                logger.error(
                    "%sRow %d: unexpected error during processing: %s",
                    prefix,
                    row.row_number,
                    row_exc,
                )
                reason = Reason.UNEXPECTED_ERROR
                res.unreachable_count += 1
                res.unreachable_by_reason[reason.value] = (
                    res.unreachable_by_reason.get(reason.value, 0) + 1
                )
                res.unreachable_records.append(
                    UnreachableRecord(
                        input_row=row.row_number,
                        url_as_given=row.raw,
                        slug=slug,
                        reason=reason,
                        detail=str(row_exc),
                    )
                )
                doc = parse_profile(
                    source_url=url,
                    slug=slug,
                    html=None,
                    status=Status.UNREACHABLE,
                    important_fields=config.important_fields,
                    reason=reason.value,
                )
                write_profile_json(doc, profiles_dir, filename_stems.get(row.row_number))

            res.completed_rows += 1

            if (
                idx < num_rows - 1
                and not res.stop_triggered
                and not (stop_event and stop_event.is_set())
            ):
                delay = random.uniform(config.delay_seconds_min, config.delay_seconds_max)
                logger.info("%sApplying polite delay of %.1fs before next profile...", prefix, delay)
                sleep_fn(delay)
    except KeyboardInterrupt:
        res.interrupted = True
        logger.warning(
            "\n[!] Execution interrupted (Ctrl+C). Preserving extracted profiles & reports..."
        )

    return res


def run_pipeline(
    config: ExtractorConfig,
    fetcher: Fetcher,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """Execute the end-to-end extraction pipeline.

    Parameters:
        config: ExtractorConfig containing paths, timeouts, and delay bounds.
        fetcher: Concrete Fetcher implementation (PlaywrightFetcher or FakeFetcher).
        sleep_fn: Injected sleep function (time.sleep in prod, mock in tests).

    Returns:
        RunSummary object with fully reconciled audit statistics.
    """
    logger = get_logger()
    start_time = datetime.now(UTC)
    started_at_str = start_time.isoformat().replace("+00:00", "Z")
    logger.info("Starting pipeline run on %s", config.input_file)

    # --------------------------------------------------------------------------
    # 1. Read Input Spreadsheet
    # --------------------------------------------------------------------------
    try:
        read_result = read_profile_urls(config.input_file)
    except Exception as e:
        logger.error("Failed to read input spreadsheet: %s", e)
        fetcher.close()
        raise

    all_rows = read_result.rows

    # Apply max_profiles limit if configured; rows beyond the cap are counted
    # explicitly as not processed so the summary always reconciles.
    skipped_by_limit = 0
    if config.max_profiles and config.max_profiles > 0:
        valid_count = 0
        capped_rows = []
        for r in all_rows:
            if r.normalized:
                if valid_count < config.max_profiles:
                    capped_rows.append(r)
                    valid_count += 1
                else:
                    skipped_by_limit += 1
            else:
                capped_rows.append(r)
        candidate_rows = capped_rows
    else:
        candidate_rows = all_rows

    valid_rows = [r for r in candidate_rows if r.normalized and r.slug]
    invalid_rows = [r for r in candidate_rows if not r.normalized]

    ok_count = 0
    unreachable_count = 0
    unreachable_by_reason: dict[str, int] = {}
    not_processed_count = skipped_by_limit
    if skipped_by_limit:
        logger.info(
            "%d valid row(s) not processed due to max_profiles=%d",
            skipped_by_limit,
            config.max_profiles,
        )
    unreachable_records: list[UnreachableRecord] = []

    # Assign one canonical, collision-free filename per valid row. The same stem is
    # used for writing and for stale-file reconciliation.
    filename_stems: dict[int, str] = {}
    used_stems: set[str] = set()
    for r in valid_rows:
        stem = profile_filename_stem(r.slug or "", r.normalized or "")
        if stem.lower() in used_stems:
            digest = hashlib.sha256((r.slug or "").encode("utf-8")).hexdigest()[:8]
            stem = f"{stem}-{digest}"
            logger.warning(
                "Row %d: filename collision after sanitizing; saving as %s.json",
                r.row_number,
                stem,
            )
        used_stems.add(stem.lower())
        filename_stems[r.row_number] = stem

    # --------------------------------------------------------------------------
    # 2. Record Malformed / Invalid URLs Immediately
    # Invalid URLs appear in unreachable_profiles.xlsx without creating JSON files
    # --------------------------------------------------------------------------
    for inv in invalid_rows:
        unreachable_count += 1
        unreachable_by_reason[Reason.INVALID_URL.value] = (
            unreachable_by_reason.get(Reason.INVALID_URL.value, 0) + 1
        )
        unreachable_records.append(
            UnreachableRecord(
                input_row=inv.row_number,
                url_as_given=inv.raw,
                slug=None,
                reason=Reason.INVALID_URL,
                detail="Malformed or unsupported URL",
            )
        )
        logger.info("Row %d: unreachable (invalid_url)", inv.row_number)

    num_valid = len(valid_rows)
    stop_triggered = False

    # --------------------------------------------------------------------------
    # 3. Prepare Clean Output Directory
    # Ensure output profiles directory exists without prematurely deleting prior data
    # --------------------------------------------------------------------------
    profiles_dir = config.output_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------------------
    # 4. Processing Across Valid Candidate Rows (Single or Multi-Worker)
    # --------------------------------------------------------------------------
    interrupted = False
    completed_rows = 0
    try:
        if config.workers <= 1 or num_valid <= 1:
            worker_res = _process_candidate_batch(
                rows=valid_rows,
                config=config,
                fetcher=fetcher,
                filename_stems=filename_stems,
                profiles_dir=profiles_dir,
                sleep_fn=sleep_fn,
                worker_idx=0,
                total_workers=1,
            )
            ok_count += worker_res.ok_count
            unreachable_count += worker_res.unreachable_count
            for r_k, r_c in worker_res.unreachable_by_reason.items():
                unreachable_by_reason[r_k] = unreachable_by_reason.get(r_k, 0) + r_c
            unreachable_records.extend(worker_res.unreachable_records)
            completed_rows += worker_res.completed_rows
            not_processed_count += worker_res.not_processed_count
            if worker_res.stop_triggered:
                stop_triggered = True
            if worker_res.interrupted:
                interrupted = True
                not_processed_count = skipped_by_limit + (num_valid - completed_rows)
        else:
            partitions = partition_workload(valid_rows, config.workers)
            actual_workers = len(partitions)
            logger.info(
                "Spawning %d parallel workers across %d candidate rows (%s)...",
                actual_workers,
                num_valid,
                ", ".join(f"Worker {i+1}: {len(p)} rows" for i, p in enumerate(partitions)),
            )
            stop_event = threading.Event()

            auth_paths = [
                get_worker_auth_path(config.auth_state_path, i, actual_workers)
                for i in range(actual_workers)
            ]
            if isinstance(fetcher, PlaywrightFetcher):
                ensure_worker_logins(auth_paths)

            def _worker_task(w_idx: int, p_rows: list[InputRow]) -> WorkerResult:
                if isinstance(fetcher, PlaywrightFetcher):
                    w_auth = auth_paths[w_idx]
                    w_fetcher: Fetcher = PlaywrightFetcher(
                        auth_state_path=w_auth,
                        headless=fetcher.headless,
                        timeout_seconds=fetcher.timeout_ms / 1000.0,
                        save_raw_html_dir=fetcher.save_raw_html_dir,
                        max_scroll_steps=fetcher.max_scroll_steps,
                        scroll_step_delay_ms=fetcher.scroll_step_delay_ms,
                        about_wait_ms=fetcher.about_wait_ms,
                        interactive_relogin=fetcher.interactive_relogin,
                        block_images=fetcher.block_images,
                    )
                elif isinstance(fetcher, FakeFetcher):
                    w_fetcher = FakeFetcher(
                        outcomes=dict(fetcher.outcomes),
                        default_status=fetcher.default_status,
                    )
                else:
                    w_fetcher = fetcher

                try:
                    return _process_candidate_batch(
                        rows=p_rows,
                        config=config,
                        fetcher=w_fetcher,
                        filename_stems=filename_stems,
                        profiles_dir=profiles_dir,
                        sleep_fn=sleep_fn,
                        worker_idx=w_idx,
                        total_workers=actual_workers,
                        stop_event=stop_event,
                    )
                finally:
                    if w_fetcher is not fetcher:
                        try:
                            w_fetcher.close()
                        except Exception:
                            pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = [
                    executor.submit(_worker_task, idx, part)
                    for idx, part in enumerate(partitions)
                ]
                results = [f.result() for f in futures]

            for res in results:
                ok_count += res.ok_count
                unreachable_count += res.unreachable_count
                for r_k, r_c in res.unreachable_by_reason.items():
                    unreachable_by_reason[r_k] = unreachable_by_reason.get(r_k, 0) + r_c
                unreachable_records.extend(res.unreachable_records)
                completed_rows += res.completed_rows
                not_processed_count += res.not_processed_count
                if res.stop_triggered:
                    stop_triggered = True
                if res.interrupted:
                    interrupted = True

            if interrupted:
                not_processed_count = skipped_by_limit + (num_valid - completed_rows)

            unreachable_records.sort(key=lambda rec: rec.input_row)

    except KeyboardInterrupt:
        interrupted = True
        # Rows never completed (including the one in progress) are not processed;
        # rows already counted as skipped after a block are not double-counted.
        not_processed_count = skipped_by_limit + (num_valid - completed_rows)
        logger.warning(
            "\n[!] Execution interrupted (Ctrl+C). Preserving extracted profiles and reports..."
        )
    finally:
        # Guarantee browser shutdown and process cleanup
        fetcher.close()

    # --------------------------------------------------------------------------
    # 5. Stale Profile Reconciliation (opt-in only) & Audit Reports
    # --------------------------------------------------------------------------
    # Destructive cleanup never runs by default. With prune_stale enabled it runs only
    # after an uninterrupted, unblocked run that processed at least one valid profile.
    run_complete = (
        not interrupted
        and not stop_triggered
        and num_valid > 0
        and completed_rows == num_valid
        and skipped_by_limit == 0
    )
    if getattr(config, "prune_stale", False):
        if run_complete:
            keep_stems = {stem.lower() for stem in filename_stems.values()}
            for existing_json in profiles_dir.glob("*.json"):
                if existing_json.stem.lower() not in keep_stems:
                    try:
                        existing_json.unlink()
                        logger.info("Pruned stale profile file: %s", existing_json.name)
                    except OSError:
                        pass
        else:
            logger.warning(
                "Skipping stale-file pruning: run was empty, interrupted, or stopped early."
            )

    if unreachable_records:
        write_unreachable_report(unreachable_records, config.output_dir)
    else:
        # Delete stale unreachable report from previous runs so successful runs show clean state
        stale_report = Path(config.output_dir) / "unreachable_profiles.xlsx"
        try:
            stale_report.unlink(missing_ok=True)
        except PermissionError:
            logger.warning(
                "Stale %s is open in another program and could not be removed.",
                stale_report.name,
            )

    end_time = datetime.now(UTC)
    finished_at_str = end_time.isoformat().replace("+00:00", "Z")
    duration = (end_time - start_time).total_seconds()

    summary = RunSummary(
        started_at=started_at_str,
        finished_at=finished_at_str,
        duration_seconds=round(duration, 2),
        rows_read=read_result.rows_read,
        rows_valid=read_result.rows_valid,
        rows_invalid=read_result.rows_invalid,
        duplicates_skipped=read_result.duplicates_skipped,
        ok_count=ok_count,
        unreachable_count=unreachable_count,
        unreachable_by_reason=unreachable_by_reason,
        not_processed_count=not_processed_count,
        output_dir=str(config.output_dir),
    )

    write_run_summary(summary, config.output_dir)
    logger.info(
        "Run completed in %.2fs: %d ok, %d unreachable, %d not processed",
        duration,
        ok_count,
        unreachable_count,
        not_processed_count,
    )
    return summary
