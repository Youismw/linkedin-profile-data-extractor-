"""Profile health checks and LinkedIn SDUI layout drift diagnostics.

Module Architecture & Inter-Module Communication:
==================================================
Inspects parsed ``ProfileDocument`` instances after extraction to detect signs that
LinkedIn changed its Server-Driven UI (SDUI) structure or that a parser rule broke.
This module is strictly read-only on parsed outputs and never stores or emits
candidate personal data (PII) in diagnostic messages.

Module Communications:
----------------------
1. Upstream Callers:
   - ``profile_extractor.orchestrator``: Calls ``check_profile_health()`` after parsing
     each profile and attaches warning messages to ``meta.parse_warnings``.
   - ``profile_extractor.drift_report``: Calls ``check_profile_health()`` during
     offline ``reparse``, ``add-fixture``, and ``reparse-run`` workflows.

2. Downstream Dependencies:
   - ``profile_extractor.models``: Consumes ``ProfileDocument`` and ``Status``.
   - ``pydantic``: Defines ``SectionDiagnostic`` contract.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from profile_extractor.models import ProfileDocument, Status

_JUNK_HEADLINE_RE = re.compile(r"^[·\u00b7\s]*(?:1st|2nd|3rd\+?)\b", re.IGNORECASE)
_SKILL_EXPERIENCE_SUBHEADING_RE = re.compile(r"\b\d+\s+experiences?\s+at\b", re.IGNORECASE)
_SKILL_INTERN_SUBHEADING_RE = re.compile(r"\bintern\s+at\b", re.IGNORECASE)
_SKILL_ASSESSMENT_RE = re.compile(r"^passed\s+linkedin\s+skill\s+assessment$", re.IGNORECASE)

_CERT_ISSUED_RE = re.compile(
    r"\bIssued\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b",
    re.IGNORECASE,
)
_LANG_PROFICIENCY_RE = re.compile(
    r"\b(?:Native or bilingual|Full professional|Professional working|"
    r"Limited working|Elementary)\s+proficiency\b",
    re.IGNORECASE,
)
_EDU_DATE_RANGE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b\s*[–-]\s*"
    r"(?:Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4})\b",
    re.IGNORECASE,
)

_OTHER_CERTS_HEADING_RE = re.compile(
    r"^licenses?\s*(?:&|and)?\s*certifications?\b",
    re.IGNORECASE,
)
_OTHER_LANGS_HEADING_RE = re.compile(r"^languages?\b", re.IGNORECASE)
_RAW_EDUCATION_HEADING_RE = re.compile(r"(?:^|\r?\n)Education(?:\r?\n|$)")

_STATED_COUNT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "education": (
        re.compile(r"show all\s+(\d+)\s+educations?", re.IGNORECASE),
        re.compile(r"(?:^|\r?\n)Education\s*\((\d+)\)", re.IGNORECASE),
    ),
    "certifications": (
        re.compile(r"licenses\s*(?:&|and)\s*certifications\s*\((\d+)\)", re.IGNORECASE),
        re.compile(r"show all\s+(\d+)\s+licenses", re.IGNORECASE),
    ),
    "projects": (
        re.compile(r"projects\s*\((\d+)\)", re.IGNORECASE),
        re.compile(r"show all\s+(\d+)\s+projects", re.IGNORECASE),
    ),
    "skills": (
        re.compile(r"skills\s*\((\d+)\)", re.IGNORECASE),
        re.compile(r"show all\s+(\d+)\s+skills", re.IGNORECASE),
    ),
    "languages": (
        re.compile(r"languages\s*\((\d+)\)", re.IGNORECASE),
        re.compile(r"show all\s+(\d+)\s+languages", re.IGNORECASE),
    ),
}

_HEADER_ONLY_CERT_NAMES = frozenset(
    {"licenses & certifications", "licenses and certifications", "certifications"}
)
_HEADER_ONLY_LANG_NAMES = frozenset({"languages", "language"})


class SectionDiagnostic(BaseModel):
    """Single layout/parse drift finding for a parsed profile section."""

    section: str
    code: str
    expected_count: int | None = None
    actual_count: int | None = None
    message: str


def _normalize_text(value: str) -> str:
    """Normalize whitespace and case-fold a string for fuzzy equality matching.

    Replaces multiple consecutive whitespace characters with a single space
    and converts to lowercase for case-insensitive comparison.
    """
    return " ".join(value.split()).casefold()


def _extract_stated_count(raw_text: str, section: str) -> int | None:
    """Extract LinkedIn's declared item count from section headers or subpage links.

    Looks for patterns like 'Education (3)' or 'Show all 5 licenses' in raw page text.
    If multiple counts match, returns the maximum detected count to avoid under-counting.
    """
    patterns = _STATED_COUNT_PATTERNS.get(section, ())
    counts: list[int] = []
    # Test each pre-compiled regex for this section against raw text
    for pat in patterns:
        for match in pat.finditer(raw_text):
            try:
                # Capture the first group representing the numeric quantity
                val = int(match.group(1))
                if val > 0:
                    counts.append(val)
            except (ValueError, IndexError):
                continue
    # Pick the highest stated number as the expected count
    return max(counts) if counts else None


def check_profile_health(doc: ProfileDocument) -> list[SectionDiagnostic]:
    """Run non-destructive health checks on a parsed ProfileDocument.

    Returns a list of ``SectionDiagnostic`` entries when count mismatches,
    placeholder/header items, skill subheadings, squashed items, or missing
    sections are detected. Never raises or includes candidate PII in messages.
    """
    # Only evaluate successfully extracted profiles; unreachable profiles have no extracted entities
    if doc.meta.status != Status.OK:
        return []

    diagnostics: list[SectionDiagnostic] = []
    profile = doc.profile

    # --------------------------------------------------------------------------
    # Check A: Junk headline (connection degree badge captured as headline)
    # Detects when LinkedIn's SDUI layout changes placed a '· 3rd' badge where the
    # parser expected the candidate's professional headline.
    # --------------------------------------------------------------------------
    if profile.headline and _JUNK_HEADLINE_RE.search(profile.headline.strip()):
        diagnostics.append(
            SectionDiagnostic(
                section="headline",
                code="junk_headline",
                message=(
                    "headline: captured connection badge (e.g. '· 3rd') instead of "
                    "professional headline."
                ),
            )
        )

    # --------------------------------------------------------------------------
    # Check B: Junk section-header items parsed as real entries
    # Detects when the section title itself (e.g. 'Education', 'Languages') leaked
    # into the item list as if it were a school or language name.
    # --------------------------------------------------------------------------
    edu_items = profile.education or []
    # Flag if any education entry's school name is literally just 'Education'
    if any((item.school or "").strip().casefold() == "education" for item in edu_items):
        diagnostics.append(
            SectionDiagnostic(
                section="education",
                code="junk_header_item",
                message="education: section header 'Education' was parsed as an education entry.",
            )
        )

    cert_items = profile.certifications or []
    # Flag if any certification entry's name is just the header 'Licenses & certifications'
    if any((item.name or "").strip().casefold() in _HEADER_ONLY_CERT_NAMES for item in cert_items):
        diagnostics.append(
            SectionDiagnostic(
                section="certifications",
                code="junk_header_item",
                message=(
                    "certifications: section header 'Licenses & certifications' was parsed "
                    "as a certificate name."
                ),
            )
        )

    lang_items = profile.languages or []
    # Flag if any language entry's name is just the header 'Languages'
    if any((item.name or "").strip().casefold() in _HEADER_ONLY_LANG_NAMES for item in lang_items):
        diagnostics.append(
            SectionDiagnostic(
                section="languages",
                code="junk_header_item",
                message="languages: section header 'Languages' was parsed as a language entry.",
            )
        )

    # --------------------------------------------------------------------------
    # Check C: Skill subheadings / school names / assessment badges captured as skills
    # In newer SDUI layouts, endorsements, sub-roles, and context lines (e.g. '2 experiences at...')
    # are rendered in nested divs. This checks if noise text leaked into the skills list.
    # --------------------------------------------------------------------------
    real_schools = {
        _normalize_text(item.school)
        for item in edu_items
        if item.school
        and item.school.strip().casefold() != "education"
        and len(item.school.strip()) > 10
    }
    skills_list = profile.skills or []
    junk_skill_found = False
    for skill in skills_list:
        # Check against context subheadings, assessment text, or duplicated university names
        if (
            _SKILL_EXPERIENCE_SUBHEADING_RE.search(skill)
            or _SKILL_INTERN_SUBHEADING_RE.search(skill)
            or _SKILL_ASSESSMENT_RE.match(skill.strip())
            or _normalize_text(skill) in real_schools
        ):
            junk_skill_found = True
            break
    if junk_skill_found:
        diagnostics.append(
            SectionDiagnostic(
                section="skills",
                code="junk_skill_subheading",
                message=(
                    "skills: captured context subheadings (e.g. experience/school lines) "
                    "instead of skill headings."
                ),
            )
        )

    # --------------------------------------------------------------------------
    # Check D: Multiple records squashed into a single item's raw string
    # When LinkedIn wraps all items in a single container without individual <li> nodes,
    # multiple dates or issuance strings end up in one item instead of separate records.
    # --------------------------------------------------------------------------
    # Check for multiple 'Issued <Date>' occurrences inside a single certificate item
    if any(len(_CERT_ISSUED_RE.findall(item.raw or "")) >= 2 for item in cert_items):
        diagnostics.append(
            SectionDiagnostic(
                section="certifications",
                code="squashed_items",
                message=(
                    "certifications: multiple certificates were squashed into a single "
                    "entry's raw text."
                ),
            )
        )

    # Check for multiple language proficiency phrases in one language entry
    if any(len(_LANG_PROFICIENCY_RE.findall(item.raw or "")) >= 2 for item in lang_items):
        diagnostics.append(
            SectionDiagnostic(
                section="languages",
                code="squashed_items",
                message=(
                    "languages: multiple languages were squashed into a single entry's raw text."
                ),
            )
        )

    # Check for multiple graduation/attendance date ranges in one education entry
    if any(len(_EDU_DATE_RANGE_RE.findall(item.raw or "")) >= 2 for item in edu_items):
        diagnostics.append(
            SectionDiagnostic(
                section="education",
                code="squashed_items",
                message=(
                    "education: multiple education entries were squashed into a single "
                    "entry's raw text."
                ),
            )
        )

    # --------------------------------------------------------------------------
    # Check E: Sections wrongly routed to other_sections, missing sections, or count mismatches
    # Detects when parser rules failed to recognize a standard section heading, causing it
    # to fall through to `other_sections`.
    # --------------------------------------------------------------------------
    routed_sections_seen: set[str] = set()
    for other in doc.other_sections:
        heading = (other.heading or "").strip()
        # If 'Licenses & certifications' ended up in other_sections
        if _OTHER_CERTS_HEADING_RE.match(heading) and "certifications" not in routed_sections_seen:
            routed_sections_seen.add("certifications")
            diagnostics.append(
                SectionDiagnostic(
                    section="certifications",
                    code="section_routed_to_other",
                    message=(
                        "certifications: section was routed into 'other_sections' instead of "
                        "'profile.certifications'."
                    ),
                )
            )
        # If 'Languages' ended up in other_sections
        elif _OTHER_LANGS_HEADING_RE.match(heading) and "languages" not in routed_sections_seen:
            routed_sections_seen.add("languages")
            diagnostics.append(
                SectionDiagnostic(
                    section="languages",
                    code="section_routed_to_other",
                    message=(
                        "languages: section was routed into 'other_sections' instead of "
                        "'profile.languages'."
                    ),
                )
            )

    # Calculate valid item counts (filtering out known junk header items)
    raw_text = doc.raw_text or ""
    real_counts: dict[str, int] = {
        "education": sum(
            1 for item in edu_items if (item.school or "").strip().casefold() != "education"
        ),
        "certifications": sum(
            1
            for item in cert_items
            if (item.name or "").strip().casefold() not in _HEADER_ONLY_CERT_NAMES
        ),
        "projects": len(profile.projects or []),
        "skills": len(skills_list),
        "languages": sum(
            1
            for item in lang_items
            if (item.name or "").strip().casefold() not in _HEADER_ONLY_LANG_NAMES
        ),
    }

    # If raw page text clearly contained an 'Education' heading, but 0 items were parsed
    if _RAW_EDUCATION_HEADING_RE.search(raw_text) and real_counts["education"] == 0:
        diagnostics.append(
            SectionDiagnostic(
                section="education",
                code="missing_section",
                expected_count=None,
                actual_count=0,
                message=(
                    "education: 'Education' heading is present in raw text, but 0 "
                    "education items were parsed."
                ),
            )
        )

    # --------------------------------------------------------------------------
    # Check F: Stated count vs parsed count mismatch
    # If LinkedIn stated e.g. 'Show all 5 educations' or 'Skills (12)' on a subpage
    # but the parser retrieved fewer, flag a count mismatch.
    # --------------------------------------------------------------------------
    for section in ("education", "certifications", "projects", "skills", "languages"):
        stated = _extract_stated_count(raw_text, section)
        if stated is None or stated <= 0:
            continue
        actual = real_counts[section]
        subpage_present = f"--- Subpage: {section} ---" in raw_text
        if subpage_present and actual < stated:
            # Subpage was visited but some virtualized items were missed
            diagnostics.append(
                SectionDiagnostic(
                    section=section,
                    code="count_mismatch",
                    expected_count=stated,
                    actual_count=actual,
                    message=(
                        f"{section}: LinkedIn shows {stated} item(s) on subpage, "
                        f"but parsed {actual}."
                    ),
                )
            )
        elif not subpage_present and actual == 0:
            # Main profile mentions items exist, but 0 were parsed and subpage wasn't fetched
            diagnostics.append(
                SectionDiagnostic(
                    section=section,
                    code="count_mismatch",
                    expected_count=stated,
                    actual_count=0,
                    message=(
                        f"{section}: LinkedIn shows {stated} item(s), but parsed 0."
                    ),
                )
            )

    return diagnostics
