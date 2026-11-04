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

import hashlib
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import Fetcher
from profile_extractor.logging_setup import get_logger
from profile_extractor.models import (
    OtherSection,
    Reason,
    RunSummary,
    Status,
    UnreachableRecord,
)
from profile_extractor.parser import (
    _is_placeholder_value,
    parse_certifications_details,
    parse_education_details,
    parse_experience_details,
    parse_languages_details,
    parse_profile,
    parse_projects_details,
    parse_skills_details,
)
from profile_extractor.reader import read_profile_urls
from profile_extractor.writer import (
    profile_filename_stem,
    write_profile_json,
    write_run_summary,
    write_unreachable_report,
)


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
    for existing in sections:
        if _normalised_text(existing.heading) == _normalised_text(clean_heading):
            existing.text = _unique_lines(existing.text, clean_text)
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
    # 4. Processing Loop Across Valid Candidate Rows
    # --------------------------------------------------------------------------
    interrupted = False
    completed_rows = 0
    try:
        for idx, row in enumerate(valid_rows):
            # If circuit breaker was triggered by a bot challenge, skip remaining rows
            if stop_triggered:
                not_processed_count += 1
                logger.info("Row %d: skipped (pipeline stopped after block)", row.row_number)
                continue

            slug = row.slug or ""
            url = row.normalized or ""
            logger.info(
                "Row %d: visiting candidate profile (%d of %d)...",
                row.row_number,
                idx + 1,
                num_valid,
            )
            logger.debug("Row %d: candidate slug '%s'", row.row_number, slug)

            try:
                # Retrieve profile HTML via Playwright
                fetch_res = fetcher.fetch(url)

                if fetch_res.status == Status.OK:
                    # Parse DOM into structured ProfileDocument
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.OK,
                        important_fields=config.important_fields,
                    )
                    # Preserve the first-pass output as immutable input to a later
                    # additive reconciliation. The legacy path below is retained.
                    baseline_profile = doc.profile.model_copy(deep=True)

                    # Deep crawling of sub-pages (Show all skills, education, experience).
                    # Every sub-page fetch goes through the same block policy as the main
                    # profile fetch and is paced with the same polite delay.
                    if getattr(config, "deep_details", True) and fetch_res.html:
                        main_html = fetch_res.html
                        legacy_attempted_sections: set[str] = set()
                        detail_specs = [
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
                            if stop_triggered:
                                break
                            if not any(re.search(pat, main_html, re.I) for pat in patterns):
                                continue
                            legacy_attempted_sections.add(section)
                            detail_url = f"https://www.linkedin.com/in/{slug}/details/{section}/"
                            sleep_fn(
                                random.uniform(config.delay_seconds_min, config.delay_seconds_max)
                            )
                            logger.info(
                                "Row %d: unrolling full %s list...",
                                row.row_number,
                                section,
                            )
                            logger.debug(
                                "Row %d: fetching subpage %s",
                                row.row_number,
                                detail_url,
                            )
                            try:
                                detail_res = fetcher.fetch(detail_url)
                            except KeyboardInterrupt:
                                raise
                            except Exception as detail_err:
                                logger.warning(
                                    "Row %d: could not fetch %s subpage: %s",
                                    row.row_number,
                                    section,
                                    detail_err,
                                )
                                continue
                            if detail_res.status != Status.OK:
                                logger.warning(
                                    "Row %d: %s subpage unreachable (%s)",
                                    row.row_number,
                                    section,
                                    detail_res.reason.value if detail_res.reason else "unknown",
                                )
                                if config.stop_on_block and detail_res.reason == Reason.BLOCKED:
                                    logger.warning(
                                        "Row %d: Block detected on %s subpage! Halting run "
                                        "as stop_on_block=True",
                                        row.row_number,
                                        section,
                                    )
                                    stop_triggered = True
                                continue
                            if not detail_res.html:
                                continue

                            # Preserve subpage raw text in doc.raw_text for recovery audit
                            sub_soup = BeautifulSoup(detail_res.html, "lxml")
                            for s_tag in sub_soup.find_all(
                                ["script", "style", "noscript", "svg", "template"]
                            ):
                                s_tag.decompose()
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
                                    # Skills are scalar labels, not rich records. Merge
                                    # both sources instead of replacing the profile-page
                                    # subset with a potentially incomplete details page.
                                    merged_skills = _merge_skill_items(current_items, deep_items)
                                    if len(merged_skills) > current_count:
                                        logger.info(
                                            "Row %d: merged %d skills (was %d; added %d from details page)",
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
                                            "Row %d: unrolled %d total %s (was %d, score %d vs %d)",
                                            row.row_number,
                                            len(deep_items),
                                            section,
                                            current_count,
                                            deep_score,
                                            current_score,
                                        )
                                        setattr(doc.profile, section, deep_items)
                        # ------------------------------------------------------------------
                        # Additive recovery pass. The original profile parse and the
                        # existing six known deep-page fetches have already run. This pass
                        # only discovers additional same-profile detail/activity routes,
                        # preserves unknown pages in raw_text + other_sections, and unions
                        # new structured records. It never recursively follows subpage links.
                        # ------------------------------------------------------------------
                        _restore_profile_snapshot_additively(doc.profile, baseline_profile)

                        if not stop_triggered:
                            parser_by_section = {
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
                                "licenses": "certifications",
                                "licenses-and-certifications": "certifications",
                            }
                            legacy_attempted_normalized = {
                                legacy_aliases.get(item, item)
                                for item in legacy_attempted_sections
                            }
                            discovered_pages = _discover_profile_detail_pages(main_html, url, slug)
                            profile_text, _ = _extract_supplemental_page_text(main_html, "profile")
                            profile_text_norm = _normalised_text(profile_text)
                            supplemental_attempts = 0

                            for section, detail_url in discovered_pages:
                                target_field = legacy_aliases.get(section, section)
                                if target_field in legacy_attempted_normalized:
                                    continue
                                if supplemental_attempts >= MAX_SUPPLEMENTAL_DETAIL_PAGES:
                                    logger.warning(
                                        "Row %d: supplemental detail-page limit (%d) reached",
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
                                    "Row %d: additive discovery fetching '%s'...",
                                    row.row_number,
                                    section,
                                )
                                try:
                                    supplemental_res = fetcher.fetch(detail_url)
                                except KeyboardInterrupt:
                                    raise
                                except Exception as supplemental_error:
                                    logger.warning(
                                        "Row %d: supplemental page '%s' failed: %s",
                                        row.row_number,
                                        section,
                                        supplemental_error,
                                    )
                                    continue

                                if supplemental_res.status != Status.OK:
                                    logger.warning(
                                        "Row %d: supplemental page '%s' unreachable (%s)",
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
                                            "Row %d: block detected on supplemental page '%s'; halting run",
                                            row.row_number,
                                            section,
                                        )
                                        stop_triggered = True
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
                                # Fake fetchers, redirects, and stale LinkedIn links can
                                # return the original profile page. Do not misclassify it
                                # as a new section or append the whole profile a second time.
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
                                        current_items = getattr(doc.profile, field_name, None)
                                        merged_items = _merge_collection_additively(
                                            field_name, current_items, supplemental_items
                                        )
                                        setattr(doc.profile, field_name, merged_items)
                                    continue

                                # No dedicated parser? Preserve the page under Others.
                                _append_other_section_additively(
                                    doc.other_sections, heading, page_text
                                )

                        _recalculate_missing_fields(doc, config.important_fields)

                    write_profile_json(doc, profiles_dir, filename_stems.get(row.row_number))

                    if doc.meta.status == Status.OK:
                        ok_count += 1
                        logger.info("Row %d: ok", row.row_number)
                    else:
                        # Fallback if parser could not identify candidate name/headline
                        reason = doc.meta.reason or Reason.PARSE_FAILED
                        unreachable_count += 1
                        unreachable_by_reason[reason.value] = (
                            unreachable_by_reason.get(reason.value, 0) + 1
                        )
                        unreachable_records.append(
                            UnreachableRecord(
                                input_row=row.row_number,
                                url_as_given=row.raw,
                                slug=slug,
                                reason=reason,
                                detail="Parser could not extract candidate name/headline from DOM",
                            )
                        )
                        logger.info("Row %d: unreachable (%s)", row.row_number, reason.value)

                else:
                    # Retrieval failed (NOT_FOUND, NOT_VISIBLE, BLOCKED, TIMEOUT)
                    reason = fetch_res.reason or Reason.UNEXPECTED_ERROR
                    unreachable_count += 1
                    unreachable_by_reason[reason.value] = (
                        unreachable_by_reason.get(reason.value, 0) + 1
                    )
                    unreachable_records.append(
                        UnreachableRecord(
                            input_row=row.row_number,
                            url_as_given=row.raw,
                            slug=slug,
                            reason=reason,
                            detail=fetch_res.detail,
                        )
                    )
                    # Persist unreachable stub document adhering to schema contract
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.UNREACHABLE,
                        important_fields=config.important_fields,
                        reason=reason.value,
                    )
                    write_profile_json(doc, profiles_dir, filename_stems.get(row.row_number))
                    logger.info("Row %d: unreachable (%s)", row.row_number, reason.value)

                    # Circuit breaker: halt immediately upon security checkpoint
                    if config.stop_on_block and reason == Reason.BLOCKED:
                        logger.warning(
                            "Row %d: Block detected! Halting run as stop_on_block=True",
                            row.row_number,
                        )
                        stop_triggered = True

            except Exception as row_exc:
                # Isolate unexpected runtime exceptions so one candidate cannot kill the batch
                logger.error(
                    "Row %d: unexpected error during processing: %s",
                    row.row_number,
                    row_exc,
                )
                reason = Reason.UNEXPECTED_ERROR
                unreachable_count += 1
                unreachable_by_reason[reason.value] = unreachable_by_reason.get(reason.value, 0) + 1
                unreachable_records.append(
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

            completed_rows += 1

            # Polite pacing: random delay between min and max bounds (skip after last profile)
            if idx < num_valid - 1 and not stop_triggered:
                delay = random.uniform(config.delay_seconds_min, config.delay_seconds_max)
                logger.info("Applying polite delay of %.1fs before next profile...", delay)
                sleep_fn(delay)

    except KeyboardInterrupt:
        interrupted = True
        # Rows never completed (including the one in progress) are not processed;
        # rows already counted as skipped after a block are not double-counted.
        not_processed_count = skipped_by_limit + (num_valid - completed_rows)
        logger.warning(
            "\n[!] Execution interrupted (Ctrl+C). Preserving extracted profiles & reports..."
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
