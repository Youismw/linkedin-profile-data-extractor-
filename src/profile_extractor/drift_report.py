"""Offline drift reporting and parser rework kit.

Module Architecture & Inter-Module Communication:
==================================================
Produces human-readable layout drift diagnostic folders when ``health.py`` flags
parsing anomalies, and implements offline recovery CLI workflows (``reparse``,
``add-fixture``, ``reparse-run``) so engineers can iterate on parser fixes without
making live LinkedIn requests.

Module Communications:
----------------------
1. Upstream Callers:
   - ``profile_extractor.orchestrator``: Calls ``write_drift_report()`` when 3 profiles
     are flagged and again at run completion.
   - ``profile_extractor.cli``: Calls ``run_reparse_command()``,
     ``run_add_fixture_command()``, and ``run_reparse_run_command()``.

2. Downstream Dependencies:
   - ``profile_extractor.health``: Calls ``check_profile_health()``.
   - ``profile_extractor.parser`` & ``profile_extractor.parser_rules``: Re-parses
     saved HTML bundles and extracts targeted section HTML snippets.
   - ``profile_extractor.writer``: Atomically persists regenerated profile JSONs.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from profile_extractor.config import ExtractorConfig
from profile_extractor.health import SectionDiagnostic, check_profile_health
from profile_extractor.models import ContactInfo, ProfileDocument, Status
from profile_extractor.parser import (
    _extract_section_heading,
    _find_contact_info_modal,
    _is_placeholder_value,
    _matches_any_pattern,
    _merge_skill_section_items,
    _merge_structured_section_items,
    consolidate_other_sections,
    parse_certifications_details,
    parse_contact_info,
    parse_education_details,
    parse_experience_details,
    parse_languages_details,
    parse_profile,
    parse_projects_details,
    parse_skills_details,
)
from profile_extractor.parser_rules import (
    ABOUT_PATTERNS,
    CERTIFICATIONS_PATTERNS,
    EDUCATION_PATTERNS,
    EXPERIENCE_PATTERNS,
    KNOWN_SECTION_IDS,
    LANGUAGES_PATTERNS,
    PROJECTS_PATTERNS,
    SKILLS_PATTERNS,
)
from profile_extractor.writer import write_profile_json

_SUBPAGE_MARKER_RE = re.compile(
    r"<!--\s*(?:===\s*)?SUBPAGE:\s*([^\s>]+?)\s*(?:===\s*)?-->",
    re.IGNORECASE,
)

_SUBPAGE_KEY_ALIASES: dict[str, str] = {
    "contact-info": "contact_info",
    "licenses": "certifications",
    "licenses-and-certifications": "certifications",
}


def _subpage_url_to_section(url_or_key: str) -> str:
    """Map a subpage URL or key to its canonical parser section key.

    For example:
    - 'https://.../overlay/contact-info/' -> 'contact_info'
    - 'https://.../details/skills/' -> 'skills'
    - 'https://.../details/licenses-and-certifications/' -> 'certifications'
    """
    path = urlsplit(url_or_key).path.strip("/")
    segments = [seg.casefold() for seg in path.split("/") if seg]
    # Check for Contact Info modal overlay
    if "overlay" in segments and "contact-info" in segments:
        return "contact_info"
    # Check for deep detail subpages (/details/<section>/)
    if "details" in segments:
        idx = segments.index("details")
        if idx + 1 < len(segments):
            raw_sec = segments[idx + 1]
            return _SUBPAGE_KEY_ALIASES.get(raw_sec, raw_sec)
    if "recent-activity" in segments:
        return "recent-activity"
    clean = url_or_key.strip().casefold()
    return _SUBPAGE_KEY_ALIASES.get(clean, clean)


def split_saved_raw_html(raw_html_content: str) -> tuple[str, dict[str, str]]:
    """Split a saved _raw_html/<slug>.html bundle into (main_html, subpages_by_section).

    The orchestrator saves the primary profile page HTML followed by each crawled subpage
    separated by `<!-- === SUBPAGE: <url> === -->` markers. This helper parses those markers
    to isolate each subpage HTML block.
    """
    matches = list(_SUBPAGE_MARKER_RE.finditer(raw_html_content))
    # If no subpage markers exist, the entire file is the main profile HTML
    if not matches:
        return raw_html_content, {}

    # Main profile HTML is everything preceding the first subpage marker
    main_html = raw_html_content[: matches[0].start()].strip()
    subpages: dict[str, str] = {}
    for idx, match in enumerate(matches):
        url_or_key = match.group(1)
        sec_key = _subpage_url_to_section(url_or_key)
        start = match.end()
        # End is either the start of the next subpage marker or EOF
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_html_content)
        chunk = raw_html_content[start:end].strip()
        if chunk:
            subpages[sec_key] = chunk
    return main_html, subpages


def _extract_subpage_visible_text(sub_html: str, section: str) -> str:
    """Extract clean visible text from a subpage HTML block for raw_text preservation.

    Removes script, style, SVG, and template noise before extracting textual lines.
    """
    sub_soup = BeautifulSoup(sub_html, "lxml")
    # Decompose script, style, and svg elements to prevent code leakage
    for s_tag in sub_soup.find_all(["script", "style", "noscript", "svg", "template"]):
        s_tag.decompose()
    # Contact info modal requires targeted selector extraction
    if section == "contact_info":
        modal = _find_contact_info_modal(sub_soup) or sub_soup.select_one("section.pv-contact-info")
        sub_text = modal.get_text(separator="\n", strip=True) if modal else ""
        if (
            "recent entity history" in sub_text.lower()
            or "suggestions available" in sub_text.lower()
        ):
            return ""
        return sub_text
    # Standard subpages: extract text from <main> or root element
    return (sub_soup.find("main") or sub_soup).get_text(separator="\n", strip=True)


def _merge_contact(base: ContactInfo | None, incoming: ContactInfo) -> ContactInfo:
    """Merge incoming contact information into existing base contact details.

    Combines website URLs without duplication and preserves non-empty strings.
    """
    if base is None:
        return incoming
    combined_websites: list[str] = []
    seen: set[str] = set()
    for w in (base.websites or []) + (incoming.websites or []):
        w_clean = w.strip()
        if w_clean and w_clean.lower() not in seen:
            combined_websites.append(w_clean)
            seen.add(w_clean.lower())
    raw_parts = [r for r in (base.raw, incoming.raw) if r and r.strip()]
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


def _recompute_missing_fields(doc: ProfileDocument, important_fields: Sequence[str]) -> None:
    """Recompute the list of missing fields based on populated profile attributes."""
    profile_data = doc.profile.model_dump(exclude_none=True)
    missing: list[str] = []
    for field in important_fields:
        value = profile_data.get(field)
        # Empty string, None, or empty list is considered missing
        if value is None or (isinstance(value, list) and not value):
            missing.append(field)
        elif isinstance(value, str) and _is_placeholder_value(value):
            missing.append(field)
    doc.meta.missing_fields = missing


def reparse_single_html_bundle(
    slug: str,
    source_url: str,
    raw_html_content: str,
    important_fields: Sequence[str] | None = None,
) -> tuple[ProfileDocument, list[SectionDiagnostic]]:
    """Offline re-parse of a full saved HTML bundle (main + subpages) and health check."""
    fields = (
        list(important_fields)
        if important_fields
        else list(ExtractorConfig().important_fields)
    )
    main_html, subpages = split_saved_raw_html(raw_html_content)

    doc = parse_profile(
        source_url=source_url,
        slug=slug,
        html=main_html,
        status=Status.OK,
        important_fields=fields,
    )
    if doc.meta.status != Status.OK:
        return doc, []

    detail_parsers: dict[str, Any] = {
        "contact_info": parse_contact_info,
        "skills": parse_skills_details,
        "education": parse_education_details,
        "experience": parse_experience_details,
        "certifications": parse_certifications_details,
        "projects": parse_projects_details,
        "languages": parse_languages_details,
    }

    for section, sub_html in subpages.items():
        sub_text = _extract_subpage_visible_text(sub_html, section)
        if sub_text:
            doc.raw_text = (doc.raw_text or "") + f"\n\n--- Subpage: {section} ---\n" + sub_text

        parser_fn = detail_parsers.get(section)
        if parser_fn is None:
            continue

        deep_items = parser_fn(sub_html)
        if section == "contact_info":
            if isinstance(deep_items, ContactInfo):
                doc.profile.contact_info = _merge_contact(doc.profile.contact_info, deep_items)
            continue

        current_items = getattr(doc.profile, section, None)
        if isinstance(deep_items, (list, tuple)) and len(deep_items) > 0:
            if section == "skills":
                doc.profile.skills = _merge_skill_section_items(current_items, deep_items)
            else:
                merged = _merge_structured_section_items(section, current_items, deep_items)
                setattr(doc.profile, section, merged)

    doc.other_sections = consolidate_other_sections(doc.other_sections)
    _recompute_missing_fields(doc, fields)
    diagnostics = check_profile_health(doc)
    doc.meta.parse_warnings = [d.message for d in diagnostics]
    return doc, diagnostics


def _sanitize_html_snippet(html: str) -> str:
    """Strip noisy scripts, styles, and SVGs from an HTML snippet for easier inspection."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    return str(main)


def _extract_section_snippet(raw_html_content: str, section: str) -> str | None:
    """Extract a concise HTML snippet for a single flagged section."""
    main_html, subpages = split_saved_raw_html(raw_html_content)
    if section in subpages:
        return _sanitize_html_snippet(subpages[section])

    if not main_html:
        return None
    soup = BeautifulSoup(main_html, "lxml")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    main = soup.find("main") or soup
    if section == "headline":
        h1 = main.find("h1")
        if h1:
            sec = h1.find_parent("section")
            if isinstance(sec, Tag):
                return str(sec)
        first_sec = main.find("section")
        return str(first_sec) if isinstance(first_sec, Tag) else None

    pattern_map = {
        "about": ABOUT_PATTERNS,
        "experience": EXPERIENCE_PATTERNS,
        "education": EDUCATION_PATTERNS,
        "skills": SKILLS_PATTERNS,
        "certifications": CERTIFICATIONS_PATTERNS,
        "projects": PROJECTS_PATTERNS,
        "languages": LANGUAGES_PATTERNS,
    }
    patterns = pattern_map.get(section, [])
    target_id = KNOWN_SECTION_IDS.get(section, "")

    for sec in main.find_all("section"):
        if sec.get("aria-label") == "Primary content":
            continue
        heading = _extract_section_heading(sec)
        sec_id = str(sec.get("id") or "").lower()
        if (patterns and _matches_any_pattern(heading, patterns)) or (
            target_id and sec_id == target_id
        ):
            return str(sec)

    return None


def write_drift_report(
    output_dir: Path,
    flagged_records: Sequence[dict[str, Any]],
    report_dir: Path | None = None,
) -> Path:
    """Write WHAT_HAPPENED.txt, profiles.json, and failed_sections/ HTML snippets."""
    target_dir = Path(
        report_dir
        or (Path(output_dir) / "drift_reports" / datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S"))
    )
    snippets_dir = target_dir / "failed_sections"
    snippets_dir.mkdir(parents=True, exist_ok=True)

    # Extract concise section HTML snippets for flagged sections
    for rec in flagged_records:
        slug = str(rec.get("slug") or "unknown")
        raw_path_str = rec.get("raw_html_path")
        if not raw_path_str:
            continue
        raw_path = Path(str(raw_path_str))
        if not raw_path.is_file():
            continue
        raw_html = raw_path.read_text(encoding="utf-8")
        seen_sections: set[str] = set()
        for diag in rec.get("diagnostics", []):
            sec = str(diag.get("section") or "")
            if not sec or sec in seen_sections:
                continue
            seen_sections.add(sec)
            snippet = _extract_section_snippet(raw_html, sec)
            if snippet:
                (snippets_dir / f"{slug}_{sec}.html").write_text(snippet, encoding="utf-8")

    # Machine-readable report
    profiles_json_path = target_dir / "profiles.json"
    profiles_json_path.write_text(
        json.dumps(list(flagged_records), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Plain-English report
    section_code_counts: Counter[tuple[str, str]] = Counter()
    sample_messages: dict[tuple[str, str], str] = {}
    for rec in flagged_records:
        seen_in_profile: set[tuple[str, str]] = set()
        for diag in rec.get("diagnostics", []):
            key = (str(diag.get("section", "unknown")), str(diag.get("code", "warning")))
            if key not in seen_in_profile:
                section_code_counts[key] += 1
                seen_in_profile.add(key)
            if key not in sample_messages:
                sample_messages[key] = str(diag.get("message", ""))

    total_flagged = len(flagged_records)
    lines: list[str] = [
        "========================================================================",
        "LINKEDIN PROFILE EXTRACTOR - LAYOUT DRIFT REPORT",
        "========================================================================",
        f"Generated (UTC) : {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}",
        f"Flagged Profiles: {total_flagged}",
        f"Report Folder   : {target_dir}",
        "",
        "WHAT HAPPENED:",
        "--------------",
        f"Health checks flagged {total_flagged} profile(s) with unexpected layout or",
        "parsing issues. LinkedIn likely updated part of its page structure or CSS/SDUI",
        "attributes, causing the parser to miss or mis-split items.",
        "",
        "ISSUES DETECTED:",
        "----------------",
    ]
    for (sec, code), count in section_code_counts.most_common():
        lines.append(f"  - [{sec} / {code}] on {count}/{total_flagged} flagged profile(s)")
        lines.append(f"      Detail: {sample_messages[(sec, code)]}")

    lines.extend(["", "FLAGGED PROFILES:", "-----------------"])
    for rec in flagged_records:
        slug = rec.get("slug", "unknown")
        raw_p = rec.get("raw_html_path", "")
        diags = rec.get("diagnostics", [])
        lines.append(f"  * {slug} ({len(diags)} warning(s))")
        if raw_p:
            lines.append(f"      Raw HTML: {raw_p}")
        for d in diags:
            lines.append(f"      - {d.get('message', '')}")

    abs_report = target_dir.resolve()
    abs_output = Path(output_dir).resolve()
    lines.extend(
        [
            "",
            "HOW TO FIX THIS OFFLINE (NO LINKEDIN REQUESTS NEEDED):",
            "------------------------------------------------------",
            f"1. Inspect the small HTML snippets inside:\n   {snippets_dir.resolve()}",
            f"2. Update parser rules in src/profile_extractor/parser.py (or parser_rules.py),\n"
            f"   then test your fix offline on these saved profiles:\n"
            f'   python -m profile_extractor reparse "{abs_report}"',
            f"3. Save these broken HTML pages as a local regression test (gitignored):\n"
            f'   python -m profile_extractor add-fixture "{abs_report}"',
            f"4. Once 'reparse' reports 0 warnings, regenerate all JSONs from saved HTML:\n"
            f'   python -m profile_extractor reparse-run "{abs_output}"',
            "========================================================================",
        ]
    )

    (target_dir / "WHAT_HAPPENED.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_dir


def _resolve_report_dir(path: Path) -> Path:
    """Resolve either a report directory or an output directory to its drift report."""
    if (path / "profiles.json").is_file():
        return path
    drift_parent = path / "drift_reports"
    if drift_parent.is_dir():
        candidates = sorted(
            [p for p in drift_parent.iterdir() if p.is_dir() and (p / "profiles.json").is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    raise FileNotFoundError(f"No drift report (profiles.json) found at: {path}")


def run_reparse_command(report_path: Path) -> int:
    """Re-run the parser on saved HTML inside a drift report and print [PASS]/[FAIL]."""
    report_dir = _resolve_report_dir(Path(report_path))
    records = json.loads((report_dir / "profiles.json").read_text(encoding="utf-8"))
    if not records:
        print(f"No flagged profiles recorded in {report_dir}")
        return 0

    total_warnings = 0
    print(f"Reparsing {len(records)} profile(s) from drift report: {report_dir}")
    print("-" * 72)
    for rec in records:
        slug = str(rec.get("slug") or "unknown")
        source_url = str(rec.get("source_url") or f"https://www.linkedin.com/in/{slug}/")
        raw_path = Path(str(rec.get("raw_html_path") or ""))
        if not raw_path.is_file():
            fallback_html = report_dir.parent.parent / "_raw_html" / f"{slug}.html"
            if fallback_html.is_file():
                raw_path = fallback_html
            else:
                local_html = report_dir / "html" / f"{slug}.html"
                if local_html.is_file():
                    raw_path = local_html
        if not raw_path.is_file():
            print(f"[FAIL] {slug}: raw HTML file not found ({raw_path})")
            total_warnings += 1
            continue

        raw_html = raw_path.read_text(encoding="utf-8")
        _doc, diagnostics = reparse_single_html_bundle(slug, source_url, raw_html)
        if not diagnostics:
            print(f"[PASS] {slug}: 0 warnings")
        else:
            total_warnings += len(diagnostics)
            print(f"[FAIL] {slug}: {len(diagnostics)} warning(s)")
            for d in diagnostics:
                print(f"       - [{d.section}:{d.code}] {d.message}")

    print("-" * 72)
    if total_warnings == 0:
        print("Result: ALL PASS (0 warnings remaining).")
        return 0
    print(f"Result: {total_warnings} total warning(s) remaining.")
    return 1


def run_add_fixture_command(
    report_path: Path,
    fixture_root: Path | None = None,
) -> Path:
    """Create a local gitignored regression test fixture from a drift report."""
    report_dir = _resolve_report_dir(Path(report_path))
    records = json.loads((report_dir / "profiles.json").read_text(encoding="utf-8"))

    target_root = Path(fixture_root) if fixture_root else (
        Path("tests/fixtures/private/drift") / report_dir.name
    )
    html_dir = target_root / "html"
    html_dir.mkdir(parents=True, exist_ok=True)

    bundled_records: list[dict[str, Any]] = []
    for rec in records:
        slug = str(rec.get("slug") or "unknown")
        src_html = Path(str(rec.get("raw_html_path") or ""))
        copied_name = f"{slug}.html"
        if src_html.is_file():
            shutil.copy2(src_html, html_dir / copied_name)
        bundled_records.append(
            {
                "slug": slug,
                "source_url": rec.get("source_url", f"https://www.linkedin.com/in/{slug}/"),
                "html_file": copied_name,
                "original_diagnostics": rec.get("diagnostics", []),
            }
        )

    (target_root / "profiles.json").write_text(
        json.dumps(bundled_records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    test_code = '''"""Auto-generated local drift regression test (kept out of git)."""

import json
from pathlib import Path

from profile_extractor.drift_report import reparse_single_html_bundle


def test_drift_fixture_profiles_pass_health_checks() -> None:
    """Verify parser fixes resolve all layout drift warnings on captured HTML."""
    fixture_dir = Path(__file__).parent
    records = json.loads((fixture_dir / "profiles.json").read_text(encoding="utf-8"))
    assert records, "Expected at least one captured fixture record"

    for rec in records:
        html_path = fixture_dir / "html" / rec["html_file"]
        assert html_path.is_file(), f"Missing fixture HTML: {html_path}"
        _doc, diagnostics = reparse_single_html_bundle(
            slug=rec["slug"],
            source_url=rec["source_url"],
            raw_html_content=html_path.read_text(encoding="utf-8"),
        )
        assert diagnostics == [], (
            f"Drift warnings remain for {rec['slug']}: "
            + "; ".join(d.message for d in diagnostics)
        )
'''
    test_file = target_root / "test_drift_fixture.py"
    test_file.write_text(test_code, encoding="utf-8")

    print(f"[OK] Saved local gitignored fixture to: {target_root}")
    print(f"Run regression test with:\n  pytest \"{test_file}\"")
    return target_root


def run_reparse_run_command(output_dir: Path) -> int:
    """Regenerate all profile JSONs in a past run directory from saved _raw_html files."""
    out_path = Path(output_dir)
    raw_dir = out_path / "_raw_html"
    profiles_dir = out_path / "profiles"

    if not raw_dir.is_dir():
        print(f"[ERROR] No _raw_html directory found in: {out_path}")
        return 1

    html_files = sorted(raw_dir.glob("*.html"))
    if not html_files:
        print(f"[ERROR] No saved HTML files found in: {raw_dir}")
        return 1

    profiles_dir.mkdir(parents=True, exist_ok=True)
    regenerated = 0
    profiles_with_warnings = 0

    for html_file in html_files:
        slug = html_file.stem
        source_url = f"https://www.linkedin.com/in/{slug}/"
        existing_json = profiles_dir / f"{slug}.json"
        if existing_json.is_file():
            try:
                existing_payload = json.loads(existing_json.read_text(encoding="utf-8"))
                meta_url = existing_payload.get("meta", {}).get("source_url")
                if isinstance(meta_url, str) and meta_url.strip():
                    source_url = meta_url.strip()
            except Exception:
                pass

        doc, diagnostics = reparse_single_html_bundle(
            slug=slug,
            source_url=source_url,
            raw_html_content=html_file.read_text(encoding="utf-8"),
        )
        write_profile_json(doc, profiles_dir, slug)
        regenerated += 1
        if diagnostics:
            profiles_with_warnings += 1

    print(
        f"[OK] Regenerated {regenerated} profile JSON(s) in {profiles_dir}; "
        f"{profiles_with_warnings} profile(s) still have parse warnings."
    )
    return 0
