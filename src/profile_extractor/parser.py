"""DOM parsing and structured profile extraction using BeautifulSoup.

Module Architecture & Inter-Module Communication:
==================================================
This module is responsible for analyzing raw LinkedIn HTML, sanitizing noise elements,
and extracting structured candidate fields into strongly-typed Pydantic profile models.
It handles both primary profile layouts and specialized detail-page collections (skills,
certifications, education, experience, languages, projects).

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.orchestrator`:
     * Calls `parse_profile()` to parse top card, experience, education, skills, and metadata.
     * Calls `parse_skills_details()`, `parse_experience_details()`, `parse_education_details()`,
       `parse_certifications_details()`, `parse_projects_details()`, `parse_languages_details()`.
   - Unit and fixture tests in `tests/test_parser.py`, `tests/test_live_fixtures.py`,
     `tests/test_advanced_parser_defects.py`.

2. Downstream Dependencies:
   - `profile_extractor.parser_rules`:
     * Imports date parsing rules (`DATE_RANGE_REGEX`), UI badge filters (`UI_BADGE_PATTERN`),
       noise filters (`NOISE_LINE_PATTERNS`), and placeholder detection.
   - `profile_extractor.models`:
     * Constructs and returns `ProfileDocument`, `Profile`, `Meta`, `ExperienceItem`,
       `EducationItem`, `CertificationItem`, `ProjectItem`, `LanguageItem`, `OtherSection`.
   - `bs4 (BeautifulSoup)`: Performs high-speed DOM traversal, node filtering,
     and identity comparisons.
"""

import re
from collections.abc import Sequence
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from profile_extractor.logging_setup import get_logger
from profile_extractor.models import (
    CertificationItem,
    ContactInfo,
    EducationItem,
    ExperienceItem,
    LanguageItem,
    Meta,
    OtherSection,
    Profile,
    ProfileDocument,
    ProjectItem,
    Reason,
    Status,
)
from profile_extractor.parser_rules import (
    ABOUT_PATTERNS,
    ACTIVITIES_PATTERN,
    CERTIFICATIONS_PATTERNS,
    DATE_RANGE_PATTERN,
    DURATION_PATTERN,
    EDUCATION_PATTERNS,
    EMPLOYMENT_TYPE_SUFFIX_PATTERN,
    EXPERIENCE_PATTERNS,
    GRADE_PATTERN,
    KNOWN_SECTION_IDS,
    LANGUAGES_PATTERNS,
    NOISE_LINE_PATTERNS,
    PLACEHOLDER_REGEX,
    PROJECTS_PATTERNS,
    PRONOUN_PATTERN,
    SKILLS_CHIP_PATTERN,
    SKILLS_PATTERNS,
    SKIP_SECTION_PATTERNS,
    STRIP_SELECTORS,
    STRIP_TAGS,
    UI_BADGE_PATTERN,
    WORKPLACE_TYPES,
)

GENERIC_ERROR_HEADING_PATTERN = re.compile(
    r"^(internal server error|server error|error|404|500|502|503|"
    r"access denied|forbidden|page not found|service unavailable)\b",
    re.IGNORECASE,
)


def _clean_text(text: str | None) -> str:
    """Normalize whitespace in extracted strings (collapsing multiple spaces and newlines)."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_consecutive(strings: Sequence[str]) -> list[str]:
    """Remove consecutive duplicate strings caused by LinkedIn's mirrored aria-hidden spans."""
    result: list[str] = []
    for s in strings:
        if not result or result[-1] != s:
            result.append(s)
    return result


def _matches_any_pattern(text: str, patterns: list[re.Pattern[str]]) -> bool:
    """Check if normalized text matches any of the compiled regex patterns."""
    clean = _clean_text(text)
    return any(p.search(clean) for p in patterns)


def _clean_company_name(raw_company: str | None) -> str | None:
    """Strip employment type suffix (e.g. ' · Full-time', ' · Internship') from company name."""
    if not raw_company:
        return None
    cleaned = EMPLOYMENT_TYPE_SUFFIX_PATTERN.sub("", raw_company).strip()
    return cleaned or raw_company


def _clean_role_lines(tag: Tag) -> list[str]:
    """Extract and deduplicate non-noise text lines from a tag."""
    raw_lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line) and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
    ]
    return _dedupe_consecutive(raw_lines)


def _extract_section_heading(section: Tag) -> str:
    """Find and return the heading text of a section.

    Inspection cascade:
    1. Direct <h2>, <h3>, or <header> tags belonging directly to this section.
       Ignores headings belonging to nested child <section> tags.
    2. 'aria-label' attribute on the section container.
    3. 'id' attribute on the section container.
    """
    # 1. Check for standard heading tags directly belonging to this section
    for heading_tag in section.find_all(["h2", "h3", "header"], recursive=True):
        # Ignore headings belonging to a child <section>
        parent_sec = heading_tag.find_parent("section")
        if parent_sec is not None and parent_sec is not section:
            continue
        txt = _clean_text(heading_tag.get_text(separator=" "))
        if txt:
            return txt

    # 2. Check aria-label attribute
    aria = section.get("aria-label")
    if aria and isinstance(aria, str):
        return _clean_text(aria)

    # 3. Check section id attribute
    sec_id = section.get("id")
    if sec_id and isinstance(sec_id, str):
        return _clean_text(sec_id)

    return ""


def _dedupe_certifications(items: Sequence[CertificationItem]) -> list[CertificationItem]:
    """Remove exact duplicate certification cards while preserving DOM order.

    LinkedIn can render the same credential more than once in its DOM (for
    example, responsive/duplicated card markup). Only byte-for-field identical
    parsed records are removed; records with different dates, IDs, URLs, or raw
    text are retained as potentially distinct credentials.
    """
    seen: set[str] = set()
    result: list[CertificationItem] = []
    for item in items:
        key = item.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _filter_top_level_tags(tags: Sequence[Tag]) -> list[Tag]:
    """Filter out descendant tags so only top-level container tags are retained.

    Guards against descendant duplication where an entity card contains nested
    collection items or listitems.
    """
    # BeautifulSoup Tag hashing/equality traverses serialized descendant HTML.
    # These candidates are DOM nodes, so identity is the intended comparison.
    tag_ids = {id(tag) for tag in tags}
    return [
        tag for tag in tags if not any(id(parent) in tag_ids for parent in tag.parents)
    ]


def _get_container_entity_items(container: Tag) -> list[Tag]:
    """Extract top-level entity item tags from any DOM container (section or subpage main).

    Inspection cascade:
    1. Modern LinkedIn LazyColumn component (<div data-component-type="LazyColumn">).
       Grabs entity-collection-item elements.
    2. Direct [role="listitem"] elements.
    3. Direct <li> elements (ul > li or ol > li).
    4. Top-level direct children.
    Always applies top-level filtering to prevent parent-child duplication.
    """
    candidates: list[Tag] = []
    lazy_col = container.select_one('[data-component-type="LazyColumn"]')
    search_root = lazy_col if lazy_col is not None else container

    # Strategy 1: entity-collection-item
    items = [
        el
        for el in search_root.select("div[componentkey]")
        if "entity-collection-item" in str(el.get("componentkey", "")) and isinstance(el, Tag)
    ]
    if items:
        candidates = items
    else:
        # Strategy 2: [role="listitem"]
        role_items = [el for el in search_root.select('[role="listitem"]') if isinstance(el, Tag)]
        if role_items:
            candidates = role_items
        else:
            # Strategy 3: <li> elements
            lis = [li for li in search_root.select("ul > li, ol > li") if isinstance(li, Tag)]
            if lis:
                candidates = lis
            elif lazy_col is not None:
                candidates = [c for c in lazy_col.children if isinstance(c, Tag)]

    return _filter_top_level_tags(candidates)


def _get_section_items(sec: Tag) -> list[Tag]:
    """Extract individual top-level item tags from a profile section."""
    return _get_container_entity_items(sec)


def _is_placeholder_value(text: str | None) -> bool:
    """Return True if the text represents a placeholder rather than usable content."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(PLACEHOLDER_REGEX.match(stripped))


def _clean_field_value(text: str | None) -> str | None:
    """Return None if text is empty or a placeholder value, else cleaned text."""
    if not text or _is_placeholder_value(text):
        return None
    return _clean_text(text)


def _is_location_line(line: str) -> bool:
    """Return True if the text line semantically represents a location or workplace mode."""
    if not line or len(line) > 85:
        return False
    # Narrative sentences ending in period are not locations (except D.C. / U.S.)
    if line.endswith(".") and not line.endswith(" D.C.") and not line.endswith(" U.S."):
        return False
    # Never match dates, durations, skills chips, or UI noise as location
    if DATE_RANGE_PATTERN.search(line) or DURATION_PATTERN.search(line):
        return False
    if SKILLS_CHIP_PATTERN.search(line) or UI_BADGE_PATTERN.search(line):
        return False
    if any(p.search(line) for p in NOISE_LINE_PATTERNS):
        return False
    if WORKPLACE_TYPES.search(line):
        return True
    if "," in line and not re.search(r"\b(?:and|\&|with|inc|llc|ltd|co)\b", line, re.IGNORECASE):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 2 and all(len(p) <= 40 for p in parts):
            return True
    if re.search(
        r"\b(?:Area|Metro(?:politan)?|Region|City|District|County)\b",
        line,
        re.IGNORECASE,
    ):
        return True
    return False


def _is_grouped_experience(tag: Tag) -> tuple[bool, list[Tag]]:
    """Determine if an experience card represents a genuine multi-role company.

    Returns:
        (True, sub_role_tags) if this card contains 2 or more distinct positions
        with their own date ranges or job metadata under a single employer.
        (False, []) if this is a single position (including when the job description
        contains HTML bullet lists <ul><li>...</li></ul>).
    """
    nested_lis = [el for el in tag.find_all("li") if isinstance(el, Tag)]
    if not nested_lis:
        return False, []

    cand_items = _filter_top_level_tags(nested_lis)
    if len(cand_items) < 2:
        return False, []

    # Exclude list items that are inside an obvious narrative/description container
    filtered_items: list[Tag] = []
    for cand in cand_items:
        inside_desc = False
        for parent in cand.parents:
            if parent is tag or not isinstance(parent, Tag):
                continue
            raw_cls = parent.get("class")
            cls_list = [raw_cls] if isinstance(raw_cls, str) else (raw_cls or [])
            if (
                "inline-show-more-text" in cls_list
                or "show-more-less-html" in cls_list
                or parent.name == "p"
                or parent.get("data-field") == "description"
            ):
                inside_desc = True
                break
        if not inside_desc:
            filtered_items.append(cand)

    if len(filtered_items) < 2:
        return False, []

    # In a genuine multi-position company, each position item has its own distinct date range.
    # Bullet lists in descriptions describe duties without individual date ranges.
    roles_with_dates = [
        it
        for it in filtered_items
        if any(
            DATE_RANGE_PATTERN.search(s) or DURATION_PATTERN.search(s)
            for s in it.stripped_strings
        )
    ]

    if len(roles_with_dates) >= 2:
        return True, filtered_items

    return False, []


def _parse_experience_items(tag: Tag) -> list[ExperienceItem]:
    """Extract structured fields from an experience item tag using semantic classification.

    Handles:
    - Multi-role company containers (e.g. Google with CEO and Product Management sub-roles).
    - Single-role entries (Title, Company, Dates, Location, Description, Skills Preview).
    - Distinguishes description bullet lists (<ul><li>...</li></ul>) from fake jobs.
    - Rejection of UI badges ('LinkedIn helped me get this job') from descriptions.
    - Identification of skills chips ('Node.js, MongoDB and +2 skills') without
      polluting the description field.
    """
    is_grouped, sub_role_tags = _is_grouped_experience(tag)
    if is_grouped:
        top_lines = _clean_role_lines(tag)
        parent_company = _clean_company_name(top_lines[0] if top_lines else "Unknown")

        results: list[ExperienceItem] = []
        for s_tag in sub_role_tags:
            s_lines = _clean_role_lines(s_tag)
            if not s_lines:
                continue
            sub_title = s_lines[0] if s_lines else None
            sub_dates: str | None = None
            sub_loc: str | None = None
            sub_skills: str | None = None
            sub_desc_parts: list[str] = []

            for line in s_lines[1:]:
                if UI_BADGE_PATTERN.search(line):
                    continue
                if SKILLS_CHIP_PATTERN.search(line):
                    sub_skills = line
                    continue
                if not sub_dates and (
                    DATE_RANGE_PATTERN.search(line) or DURATION_PATTERN.search(line)
                ):
                    sub_dates = line
                    continue
                if not sub_loc and _is_location_line(line):
                    sub_loc = line
                    continue
                if not any(p.search(line) for p in NOISE_LINE_PATTERNS) and line not in (
                    "…",
                    "...",
                    "more",
                    "see more",
                ):
                    sub_desc_parts.append(line)

            raw_text = _clean_text(s_tag.get_text(separator=" | "))
            desc_str = " ".join(sub_desc_parts) if sub_desc_parts else None

            # Guard against description in title
            if sub_title and (len(sub_title) > 90 or (len(sub_title) > 60 and "." in sub_title)):
                if desc_str:
                    desc_str = f"{sub_title} {desc_str}"
                else:
                    desc_str = sub_title
                sub_title = None

            results.append(
                ExperienceItem(
                    title=sub_title,
                    company=parent_company,
                    dates=sub_dates,
                    location=sub_loc,
                    description=desc_str,
                    skills_preview=sub_skills,
                    raw=raw_text,
                )
            )
        if results:
            return results

    # Standard single-role layout with semantic line interpretation
    raw_lines = _clean_role_lines(tag)
    if not raw_lines:
        return []

    lines = [
        line
        for line in raw_lines
        if not UI_BADGE_PATTERN.search(line) and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
    ]
    if not lines:
        return []

    title: str | None = lines[0]
    company: str | None = None
    dates: str | None = None
    location: str | None = None
    skills_preview: str | None = None
    desc_parts: list[str] = []

    for line in lines[1:]:
        # Skills chip (e.g. "Node.js, MongoDB and +2 skills")
        if SKILLS_CHIP_PATTERN.search(line):
            skills_preview = line
            continue

        # Dates / duration
        if not dates and (
            DATE_RANGE_PATTERN.search(line) or DURATION_PATTERN.search(line)
        ):
            dates = line
            continue

        # Location / modality
        if not location and _is_location_line(line):
            location = line
            continue

        # Company name
        if not company:
            company = _clean_company_name(line)
            continue

        # Narrative description (including bullet list texts!)
        if line not in ("…", "...", "more", "see more"):
            desc_parts.append(line)

    description: str | None = " ".join(desc_parts) if desc_parts else None
    raw_text = _clean_text(tag.get_text(separator=" | "))

    # Invert-proof title guard
    if title and (len(title) > 90 or (len(title) > 60 and "." in title)):
        if description:
            description = f"{title} {description}"
        else:
            description = title
        title = None

    return [
        ExperienceItem(
            title=title,
            company=company,
            dates=dates,
            location=location,
            description=description,
            skills_preview=skills_preview,
            raw=raw_text,
        )
    ]


def _parse_education_item(tag: Tag) -> EducationItem:
    """Extract structured fields from an education entry using semantic line classification.

    Extracts: School / Institution, Degree / Major, Dates, Grade / Honors, and full raw string.
    Distinguishes grades/CGPA from dates to prevent field pollution.
    """
    raw_lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line)
        and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
        and not UI_BADGE_PATTERN.search(line)
    ]
    lines = _dedupe_consecutive(raw_lines)
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    school: str | None = lines[0] if lines else None
    degree: str | None = None
    dates: str | None = None
    grade: str | None = None
    activities: str | None = None

    for line in lines[1:]:
        # Grade / CGPA pattern
        if not grade and GRADE_PATTERN.search(line):
            grade = line
            continue

        # Activities pattern
        if not activities and ACTIVITIES_PATTERN.search(line):
            activities = line
            continue

        # Dates / Year range pattern (e.g. "2018 - 2022", "May 2020")
        if not dates and (
            DATE_RANGE_PATTERN.search(line)
            or re.search(r"\b\d{4}\s*[-–]\s*(?:\d{4}|Present)\b", line)
        ):
            dates = line
            continue

        # Degree / Field of study
        if not degree:
            degree = line
            continue

    return EducationItem(
        school=school,
        degree=degree,
        dates=dates,
        grade=grade,
        activities=activities,
        raw=raw_text,
    )


def _parse_certification_item(tag: Tag) -> CertificationItem:
    """Extract structured fields from a license / certification entry."""
    lines = _clean_role_lines(tag)
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    name: str | None = lines[0] if lines else None
    issuer: str | None = None
    issue_date: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None

    cred_a = tag.find("a", href=True)
    if cred_a and isinstance(cred_a, Tag):
        href = str(cred_a.get("href", ""))
        if "http" in href:
            credential_url = href

    for line in lines[1:]:
        line_lower = line.lower()
        if "issued" in line_lower and not issue_date:
            issue_date = line
            continue
        if "expires" in line_lower and not expiration_date:
            expiration_date = line
            continue
        if "credential id" in line_lower and not credential_id:
            credential_id = line
            continue
        if not issuer and not any(
            kw in line_lower for kw in ("show credential", "see credential", "skills:")
        ):
            issuer = line
            continue

    return CertificationItem(
        name=name,
        issuer=issuer,
        issue_date=issue_date,
        expiration_date=expiration_date,
        credential_id=credential_id,
        credential_url=credential_url,
        raw=raw_text,
    )


def _parse_project_item(tag: Tag) -> ProjectItem:
    """Extract structured fields from an academic, personal, or professional project entry."""
    lines = _clean_role_lines(tag)
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    title: str | None = lines[0] if lines else None
    dates: str | None = None
    description_parts: list[str] = []
    project_skills: list[str] | None = None
    url: str | None = None

    a_tag = tag.find("a", href=True)
    if a_tag and isinstance(a_tag, Tag):
        href = str(a_tag.get("href", ""))
        if "http" in href and "linkedin.com" not in href:
            url = href

    for line in lines[1:]:
        if not dates and (DATE_RANGE_PATTERN.search(line) or re.search(r"\b\d{4}\b", line)):
            dates = line
            continue
        if SKILLS_CHIP_PATTERN.search(line) or line.lower().startswith("skills:"):
            clean_sk = re.sub(
                r"^(?:skills|associated skills)\s*:\s*", "", line, flags=re.IGNORECASE
            )
            project_skills = [
                s.strip()
                for s in re.split(r"[,·|]", clean_sk)
                if s.strip() and not re.search(r"\+\d+\s+skills?", s, re.IGNORECASE)
            ]
            continue
        if line not in ("…", "...", "more", "see more") and not UI_BADGE_PATTERN.search(line):
            description_parts.append(line)

    description = " ".join(description_parts) if description_parts else None
    return ProjectItem(
        title=title,
        dates=dates,
        description=description,
        url=url,
        skills=project_skills,
        raw=raw_text,
    )


def _parse_language_item(tag: Tag) -> LanguageItem:
    """Extract structured fields from a language proficiency entry."""
    lines = _clean_role_lines(tag)
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    name: str | None = lines[0] if lines else None
    proficiency: str | None = lines[1] if len(lines) > 1 else None

    return LanguageItem(
        name=name,
        proficiency=proficiency,
        raw=raw_text,
    )


def _parse_other_section(sec: Tag, heading: str) -> OtherSection | None:
    """Format an unmapped section cleanly into an OtherSection object.

    Activity has a particularly deep/nested list structure. Walking every nested
    ``li`` repeats parent and child post content and can pull in unrelated nested
    video-player menu entries. For Activity only, use the same top-level entity
    selection as the structured parsers. Other section handling is unchanged.
    """
    is_activity = heading.strip().casefold().startswith("activity")
    if is_activity:
        items = _get_container_entity_items(sec)
    else:
        lazy = sec.select_one('[data-component-type="LazyColumn"]')
        if lazy:
            items = lazy.select('div[componentkey^="entity-collection-item"]') or [
                c for c in lazy.children if isinstance(c, Tag)
            ]
        else:
            items = sec.find_all("li")

    if items:
        item_texts: list[str] = []
        for it in items:
            raw_lines = [
                _clean_text(s)
                for s in it.stripped_strings
                if _clean_text(s) and not _matches_any_pattern(s, NOISE_LINE_PATTERNS)
            ]
            lines = _dedupe_consecutive(raw_lines)
            if lines:
                item_texts.append(" | ".join(lines))
        if item_texts:
            return OtherSection(heading=heading, text="\n".join(item_texts))

    # Fallback to general section text
    sec_content = _clean_text(sec.get_text(separator=" "))
    if heading and sec_content.startswith(heading):
        sec_content = _clean_text(sec_content[len(heading) :])
    if sec_content and len(sec_content) > 5:
        # Strip trailing noise like 'Show all'
        sec_content = re.sub(
            r"[\s\.\u2026]*(?:see\s+more|more|show\s+all.*)\s*$",
            "",
            sec_content,
            flags=re.IGNORECASE,
        ).strip()
        if sec_content:
            return OtherSection(heading=heading, text=sec_content)
    return None


def normalize_section_heading_key(heading: str | None) -> str:
    """Normalize heading for comparison: ignore case, whitespace, and trailing count."""
    if not heading:
        return ""
    norm = " ".join(heading.split()).casefold()
    return re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", norm).strip()


def _section_text_subsumed(smaller_text: str, larger_text: str) -> bool:
    """Check if all information in smaller_text is present in larger_text."""
    if not smaller_text or not smaller_text.strip():
        return True
    if not larger_text or not larger_text.strip():
        return False
    norm_larger = " ".join(larger_text.casefold().split())
    lines = [ln.strip() for ln in smaller_text.splitlines() if ln.strip()]
    for line in lines:
        segments = [s.strip() for s in line.split("|") if s.strip()]
        for seg in segments:
            norm_seg = " ".join(seg.casefold().split())
            if norm_seg and norm_seg not in norm_larger:
                return False
    return True


def _merge_complementary_texts(text1: str, text2: str) -> str:
    """Merge complementary section texts preserving all unique lines without loss."""
    seen: set[str] = set()
    merged: list[str] = []
    for text in (text1, text2):
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            norm = " ".join(line_str.casefold().split())
            if norm not in seen:
                seen.add(norm)
                merged.append(line_str)
    return "\n".join(merged)


def consolidate_other_sections(sections: list[OtherSection]) -> list[OtherSection]:
    """Consolidate duplicate section representations in other_sections.

    - Normalizes section headings ignoring case, whitespace, and trailing count in parentheses.
    - When one representation contains all information in the other, retains the more complete one.
    - If complementary, merges unique content.
    - If cannot be safely reconciled, preserves both.
    """
    if not sections or len(sections) <= 1:
        return list(sections)

    consolidated: list[OtherSection] = []
    for sec in sections:
        sec_key = normalize_section_heading_key(sec.heading)
        if not sec_key:
            consolidated.append(sec)
            continue

        matched_index = -1
        for i, existing in enumerate(consolidated):
            if normalize_section_heading_key(existing.heading) == sec_key:
                matched_index = i
                break

        if matched_index == -1:
            consolidated.append(sec)
            continue

        existing = consolidated[matched_index]
        existing_text = existing.text or ""
        sec_text = sec.text or ""

        # Case 1: sec subsumes existing -> retain sec (the more complete representation)
        if _section_text_subsumed(existing_text, sec_text):
            best_heading = (
                sec.heading
                if not re.search(r"\(\s*\d+\s*\)$", sec.heading)
                else existing.heading
            )
            consolidated[matched_index] = OtherSection(heading=best_heading, text=sec_text)
        # Case 2: existing subsumes sec -> retain existing
        elif _section_text_subsumed(sec_text, existing_text):
            if re.search(r"\(\s*\d+\s*\)$", existing.heading) and not re.search(
                r"\(\s*\d+\s*\)$", sec.heading
            ):
                consolidated[matched_index] = OtherSection(
                    heading=sec.heading, text=existing.text
                )
        # Case 3: complementary information -> merge unique lines
        else:
            merged_text = _merge_complementary_texts(existing_text, sec_text)
            best_heading = (
                sec.heading
                if not re.search(r"\(\s*\d+\s*\)$", sec.heading)
                else existing.heading
            )
            consolidated[matched_index] = OtherSection(heading=best_heading, text=merged_text)

    return consolidated


def _is_valid_skill(cand: str) -> bool:
    """Validate whether an extracted line is genuinely a skill name."""
    if not cand or len(cand) > 60:
        return False
    if cand in ["Skills", "Show all", "Endorsements", "More profiles for you"]:
        return False
    if re.search(r"^\d+\s+endorsements?$", cand, re.IGNORECASE):
        return False
    if re.search(r"^show all\b", cand, re.IGNORECASE):
        return False
    if re.search(r"\b(?:experiences? across|endorsed by)\b", cand, re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:engineer|developer|analyst|manager|consultant|architect)\s+at\b",
        cand,
        re.IGNORECASE,
    ):
        return False
    return True


def parse_skills_details(html: str) -> list[str]:
    """Parse all skills from a LinkedIn /details/skills/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)

    skills: list[str] = []
    for it in top_items:
        lines = [_clean_text(s) for s in it.stripped_strings if _clean_text(s)]
        if not lines:
            continue
        cand = lines[0]
        if _is_valid_skill(cand) and cand not in skills:
            skills.append(cand)
    return skills


def parse_education_details(html: str) -> list[EducationItem]:
    """Parse all education items from a LinkedIn /details/education/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)
    results: list[EducationItem] = []
    for it in top_items:
        edu = _parse_education_item(it)
        if edu.school or edu.raw:
            results.append(edu)
    return results


def parse_experience_details(html: str) -> list[ExperienceItem]:
    """Parse all experience items from a LinkedIn /details/experience/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)
    results: list[ExperienceItem] = []
    for it in top_items:
        exps = _parse_experience_items(it)
        results.extend(exps)
    return results


def parse_certifications_details(html: str) -> list[CertificationItem]:
    """Parse all certifications from a LinkedIn /details/certifications/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)
    results: list[CertificationItem] = []
    for it in top_items:
        cert = _parse_certification_item(it)
        if cert.name or cert.raw:
            results.append(cert)
    return _dedupe_certifications(results)


def parse_projects_details(html: str) -> list[ProjectItem]:
    """Parse all projects from a LinkedIn /details/projects/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)
    results: list[ProjectItem] = []
    for it in top_items:
        proj = _parse_project_item(it)
        if proj.title or proj.raw:
            results.append(proj)
    return results


def parse_languages_details(html: str) -> list[LanguageItem]:
    """Parse all languages from a LinkedIn /details/languages/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    top_items = _get_container_entity_items(main)
    results: list[LanguageItem] = []
    for it in top_items:
        lang = _parse_language_item(it)
        if lang.name or lang.raw:
            results.append(lang)
    return results


def _clean_external_website_url(raw_href: str) -> str | None:
    """Extract clean target URL, unwrapping LinkedIn redirect parameters if present."""
    href = raw_href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    try:
        parsed = urlsplit(href)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        # If wrapped in linkedin redirect: https://www.linkedin.com/redir/redirect?url=...
        if host == "linkedin.com" and "redirect" in parsed.path:
            query = parse_qs(parsed.query)
            target = query.get("url", [None])[0]
            if target:
                return _clean_external_website_url(target)
        if (host in {"linkedin.com", "maps.google.com", "google.com"}) and "maps" in href:
            return None
        if host == "linkedin.com":
            return None
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        return href
    except Exception:
        return None


def parse_contact_info(html: str) -> ContactInfo | None:
    """Parse contact info from LinkedIn contact overlay modal or profile DOM.

    Extracts vanity/profile URL, email address, phone numbers, websites, address,
    birthday, connection date, and twitter handles.
    """
    if not html or not html.strip():
        return None

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    # Locate modal container if present; otherwise fallback to whole document
    modal = (
        soup.select_one(
            '[role="dialog"], .artdeco-modal[role="dialog"], .artdeco-modal, '
            'section.pv-contact-info, .pv-profile-section__section-info'
        )
        or soup.select_one(
            '#artdeco-modal-outlet:has([role="dialog"]), '
            '#artdeco-modal-outlet:has(.artdeco-modal)'
        )
        or soup.find("main")
        or soup
    )
    if len(modal.get_text(strip=True)) < 5:
        modal = soup.find("main") or soup

    raw_text = modal.get_text(separator="\n", strip=True)

    # Initialize extracted fields
    profile_url: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    websites: list[str] = []
    twitter: str | None = None
    birthday: str | None = None
    connected: str | None = None

    def _text_content(node: Tag | None) -> str:
        if node is None:
            return ""
        return " ".join(node.get_text(" ", strip=True).split())

    section_nodes: list[Tag] = list(
        modal.select(
            "section.pv-contact-info__contact-type, div.pv-contact-info__contact-type, "
            "div.pv-profile-section__section-info > section"
        )
    )
    if not section_nodes:
        section_nodes = [
            sec
            for sec in modal.find_all(["section", "div"])
            if isinstance(sec, Tag) and sec.find(["h1", "h2", "h3", "h4", "header"])
        ]

    for sec in section_nodes:
        header_tag = sec.find(["h1", "h2", "h3", "h4", "header"])
        header_text = _text_content(header_tag).lower() if isinstance(header_tag, Tag) else ""
        cls_val = sec.get("class")
        if isinstance(cls_val, list):
            classes_lower = " ".join(c for c in cls_val if isinstance(c, str)).lower()
        elif isinstance(cls_val, str):
            classes_lower = cls_val.lower()
        else:
            classes_lower = ""

        # Vanity URL / Profile link
        if "ci-vanity-url" in classes_lower or any(
            k in header_text for k in ("your profile", "profile")
        ):
            link = sec.find("a", href=True)
            if isinstance(link, Tag) and not profile_url:
                href_attr = link.get("href")
                if isinstance(href_attr, str) and "linkedin.com/in/" in href_attr:
                    profile_url = href_attr.strip()

        # Email
        if "ci-email" in classes_lower or "email" in header_text:
            mail_link = sec.select_one('a[href^="mailto:"]')
            if isinstance(mail_link, Tag) and not email:
                raw_href = mail_link.get("href")
                if isinstance(raw_href, str):
                    email = raw_href.removeprefix("mailto:").split("?")[0].strip()
            if not email:
                text = _text_content(sec)
                match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
                if match:
                    email = match.group(0).strip()

        # Phone
        if "ci-phone" in classes_lower or "phone" in header_text or "mobile" in header_text:
            tel_link = sec.select_one('a[href^="tel:"]')
            if isinstance(tel_link, Tag) and not phone:
                raw_href = tel_link.get("href")
                if isinstance(raw_href, str):
                    phone = raw_href.removeprefix("tel:").strip()
            if not phone:
                ci_box = sec.select_one(".pv-contact-info__ci-container") or sec
                lines = [
                    line.strip()
                    for line in ci_box.get_text("\n", strip=True).splitlines()
                    if line.strip() and line.lower() != header_text
                ]
                if lines:
                    phone = " ".join(lines)

        # Websites
        if "ci-websites" in classes_lower or "website" in header_text:
            for a in sec.find_all("a", href=True):
                if isinstance(a, Tag):
                    raw_href = a.get("href")
                    if isinstance(raw_href, str):
                        clean_url = _clean_external_website_url(raw_href)
                        if clean_url and clean_url not in websites:
                            websites.append(clean_url)

        # Address
        if "ci-address" in classes_lower or "address" in header_text:
            ci_box = sec.select_one(".pv-contact-info__ci-container") or sec
            lines = [
                line.strip()
                for line in ci_box.get_text("\n", strip=True).splitlines()
                if line.strip() and line.lower() != header_text
            ]
            if lines and not address:
                address = ", ".join(lines)

        # Birthday
        if "ci-birthday" in classes_lower or "birthday" in header_text:
            ci_box = sec.select_one(".pv-contact-info__ci-container") or sec
            lines = [
                line.strip()
                for line in ci_box.get_text("\n", strip=True).splitlines()
                if line.strip() and line.lower() != header_text
            ]
            if lines and not birthday:
                birthday = " ".join(lines)

        # Connected
        if "ci-connected" in classes_lower or "connected" in header_text:
            ci_box = sec.select_one(".pv-contact-info__ci-container") or sec
            lines = [
                line.strip()
                for line in ci_box.get_text("\n", strip=True).splitlines()
                if line.strip() and line.lower() != header_text
            ]
            if lines and not connected:
                connected = " ".join(lines)

        # Twitter
        if "ci-twitter" in classes_lower or "twitter" in header_text:
            t_link = sec.find("a", href=True)
            if isinstance(t_link, Tag) and not twitter:
                raw_href = t_link.get("href")
                if isinstance(raw_href, str):
                    twitter = raw_href.strip()
            elif not twitter:
                t_text = _text_content(sec)
                for part in t_text.split():
                    if part.startswith("@"):
                        twitter = part
                        break

    # Strategy 2: Global heuristics across modal for anything missed
    if not email:
        mail_link = modal.select_one('a[href^="mailto:"]')
        if isinstance(mail_link, Tag):
            raw_href = mail_link.get("href")
            if isinstance(raw_href, str):
                email = raw_href.removeprefix("mailto:").split("?")[0].strip()
        else:
            match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", raw_text)
            if match:
                email = match.group(0).strip()

    if not phone:
        tel_link = modal.select_one('a[href^="tel:"]')
        if isinstance(tel_link, Tag):
            raw_href = tel_link.get("href")
            if isinstance(raw_href, str):
                phone = raw_href.removeprefix("tel:").strip()

    if not profile_url:
        for a_tag in modal.find_all("a", href=True):
            if isinstance(a_tag, Tag):
                raw_href = a_tag.get("href")
                if (
                    isinstance(raw_href, str)
                    and "linkedin.com/in/" in raw_href
                    and "/overlay/" not in raw_href
                ):
                    profile_url = raw_href.strip()
                    break

    if not twitter:
        tw_link = modal.select_one('a[href*="twitter.com/"], a[href*="x.com/"]')
        if isinstance(tw_link, Tag):
            raw_href = tw_link.get("href")
            if isinstance(raw_href, str):
                twitter = raw_href.strip()

    if not websites:
        for a in modal.find_all("a", href=True):
            if isinstance(a, Tag):
                raw_href = a.get("href")
                if isinstance(raw_href, str):
                    clean_url = _clean_external_website_url(raw_href)
                    if clean_url and clean_url not in websites:
                        websites.append(clean_url)

    has_contact_data = any([
        profile_url,
        email,
        phone,
        address,
        websites,
        twitter,
        birthday,
        connected,
    ])
    if not has_contact_data:
        if not re.search(r"\b(contact|email|phone|website|address)\b", raw_text, re.I):
            return None

    return ContactInfo(
        profile_url=profile_url,
        email=email,
        phone=phone,
        address=address,
        websites=websites or None,
        twitter=twitter,
        birthday=birthday,
        connected=connected,
        raw=raw_text,
    )


def parse_profile(
    source_url: str,
    slug: str,
    html: str | None,
    status: Status,
    important_fields: Sequence[str],
    reason: str | None = None,
) -> ProfileDocument:
    """Parse raw profile HTML into a validated contract-compliant ProfileDocument.

    Pipeline stages:
    1. Early return if profile was unreachable.
    2. DOM cleanup (stripping scripts, styles, templates, dialogs, footer languages).
    3. Raw text extraction (dump of all unedited visible text).
    4. Top card extraction (Candidate Name, Headline, Location).
    5. Section parsing (About, Experience, Education, Skills).
    6. Unmapped section routing (Licenses, Activity, Patents -> other_sections).
    7. Missing fields calculation against important_fields configuration.
    8. Text coverage sanity verification.
    """
    logger = get_logger()

    # --------------------------------------------------------------------------
    # 1. Handle Unreachable Status Directly
    # --------------------------------------------------------------------------
    if status == Status.UNREACHABLE:
        reason_enum = Reason(reason) if reason else None
        return ProfileDocument(
            meta=Meta(
                source_url=source_url,
                slug=slug,
                status=Status.UNREACHABLE,
                reason=reason_enum,
                missing_fields=[],
            ),
            profile=Profile(),
            other_sections=[],
        )

    # If HTML is empty or blank, mark as PARSE_FAILED
    if not html or not html.strip():
        return ProfileDocument(
            meta=Meta(
                source_url=source_url,
                slug=slug,
                status=Status.UNREACHABLE,
                reason=Reason.PARSE_FAILED,
                missing_fields=[],
            ),
            profile=Profile(),
            other_sections=[],
            raw_text=None,
        )

    # --------------------------------------------------------------------------
    # 2. Parse HTML and Strip Noise Elements
    # --------------------------------------------------------------------------
    soup = BeautifulSoup(html, "lxml")

    # Strip scripts, stylesheets, templates, SVGs, modals, dialogs, nav, and footers
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for selector in STRIP_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()

    body = soup.find("body") or soup
    total_visible_text = _clean_text(body.get_text(separator=" "))

    # Raw text dump: unedited visible text from the profile container
    profile_container = soup.find("main") or body
    raw_text = profile_container.get_text(separator="\n", strip=True)

    # --------------------------------------------------------------------------
    # 3. Top Card: Name, Headline, Location
    # --------------------------------------------------------------------------
    name: str | None = None
    headline: str | None = None
    location: str | None = None

    main = soup.find("main") or soup
    top_card = None

    # Filter out outer layout containers, nested sections, and carousels
    candidate_sections = [
        sec
        for sec in main.find_all("section")
        if sec.get("aria-label") != "Primary content"
        and sec.get("aria-roledescription") != "carousel"
        and sec.get("data-testid") != "carousel"
        and not re.search(
            r"^\d+\s+notification", sec.get_text(separator=" ", strip=True), re.IGNORECASE
        )
    ]
    # BeautifulSoup Tag.__hash__ serializes the entire subtree. Using Tags as
    # set members here makes nested-section filtering quadratic (and very slow
    # on large LinkedIn pages). Section membership is identity-based, so use
    # object identities instead; this preserves the exact ancestor test while
    # making each membership lookup O(1) without hashing HTML.
    candidate_section_ids = {id(sec) for sec in candidate_sections}
    valid_sections: list[Tag] = [
        sec
        for sec in candidate_sections
        if not any(id(parent) in candidate_section_ids for parent in sec.parents)
    ]

    # Strategy A: Check <h1> tags (rejecting generic error page headings)
    h1 = soup.find("h1")
    if h1 and _clean_text(h1.get_text(separator=" ")):
        cand_name = _clean_text(h1.get_text(separator=" "))
        if not GENERIC_ERROR_HEADING_PATTERN.search(cand_name):
            name = cand_name
            top_card = h1.find_parent("section")

    # Strategy B: First valid section in <main> (LinkedIn logged-in profile top card)
    if not top_card and valid_sections:
        top_card = valid_sections[0]

    if top_card:
        top_lines = [_clean_text(s) for s in top_card.stripped_strings if _clean_text(s)]
        filtered = [
            line
            for line in top_lines
            if not re.search(r"^\d+\s+notification", line, re.IGNORECASE)
            and not re.search(r"^ad\s+options", line, re.IGNORECASE)
        ]

        if not name and filtered:
            cand_name = filtered[0]
            if not GENERIC_ERROR_HEADING_PATTERN.search(cand_name):
                name = cand_name

        if name and filtered:
            try:
                name_idx = filtered.index(name)
            except ValueError:
                name_idx = 0

            # Headline: scan immediate lines after name, skipping pronouns and buttons
            for cand in filtered[name_idx + 1 : name_idx + 4]:
                if PRONOUN_PATTERN.match(cand):
                    continue
                if re.search(
                    r"\b(contact info|connections|connect|message|more|followers)\b",
                    cand,
                    re.IGNORECASE,
                ):
                    continue
                headline = cand
                break

            # Location Strategy A: anchor on 'contact info' or middle dot '·' / '\u00b7'
            contact_idx = -1
            for idx, line in enumerate(filtered):
                if re.search(r"\bcontact\s+info\b", line, re.IGNORECASE):
                    contact_idx = idx
                    break

            if contact_idx != -1:
                for idx in range(contact_idx - 1, max(0, contact_idx - 4), -1):
                    prev = filtered[idx]
                    if prev in ["·", "\u00b7", "-", "—"] or _is_placeholder_value(prev):
                        continue
                    location = prev
                    break

            # Location Strategy B: Fallback for synthetic/mock fixtures lacking 'contact info'
            if not location:
                for cand in filtered[name_idx + 1 : name_idx + 7]:
                    if cand == headline:
                        continue
                    if _is_placeholder_value(cand):
                        continue
                    if _is_location_line(cand):
                        if not re.search(
                            r"\b(contact info|connections|connect|message|followers)\b",
                            cand,
                            re.IGNORECASE,
                        ):
                            location = cand
                            break

    # Strategy C: Title tag fallback ("Name | LinkedIn")
    if not name and soup.title and soup.title.string:
        raw_title = soup.title.string.strip()
        title_cand_name: str | None = None
        if " | " in raw_title:
            title_cand_name = _clean_text(raw_title.split(" | ")[0])
        elif " - " in raw_title:
            title_cand_name = _clean_text(raw_title.split(" - ")[0])

        generic_titles = {
            "sign up",
            "log in",
            "login",
            "join linkedin",
            "linkedin",
            "page not found",
            "404",
            "feed",
            "authwall",
            "security verification",
        }
        utility_kws = ["sign up", "log in", "page not found", "security verification"]
        if (
            title_cand_name
            and title_cand_name.lower() not in generic_titles
            and not any(kw in title_cand_name.lower() for kw in utility_kws)
        ):
            name = title_cand_name

    # Gate requirement: return parse_failed if no name and headline can be found
    if not name and not headline:
        logger.warning("Parse failed for slug '%s': neither name nor headline found", slug)
        return ProfileDocument(
            meta=Meta(
                source_url=source_url,
                slug=slug,
                status=Status.UNREACHABLE,
                reason=Reason.PARSE_FAILED,
                missing_fields=[],
            ),
            profile=Profile(),
            other_sections=[],
            raw_text=raw_text,
        )

    # --------------------------------------------------------------------------
    # 4. Section Parsing: About, Experience, Education, Skills, Others
    # --------------------------------------------------------------------------
    about: str | None = None
    experience: list[ExperienceItem] | None = None
    education: list[EducationItem] | None = None
    skills: list[str] | None = None
    certifications: list[CertificationItem] | None = None
    projects: list[ProjectItem] | None = None
    languages: list[LanguageItem] | None = None
    other_sections: list[OtherSection] = []

    seen_sections: set[int] = set()

    for sec in valid_sections:
        if id(sec) in seen_sections:
            continue
        seen_sections.add(id(sec))

        # Skip top card section
        if top_card is not None and sec is top_card:
            continue

        heading = _extract_section_heading(sec)
        sec_id = str(sec.get("id") or "").lower()

        # Skip sidebar recommendations, ads, and notification popups
        if _matches_any_pattern(heading, SKIP_SECTION_PATTERNS):
            continue

        # Skip redundant heading matching candidate name
        if name and heading.lower() == name.lower():
            continue

        items = _get_section_items(sec)

        # 4a. Check About
        if _matches_any_pattern(heading, ABOUT_PATTERNS) or sec_id == KNOWN_SECTION_IDS["about"]:
            body_text = _clean_text(sec.get_text(separator=" "))
            if heading and body_text.startswith(heading):
                body_text = _clean_text(body_text[len(heading) :])
            # Strip trailing "Top skills ..." block first (appears after '...more' in DOM)
            body_text = re.sub(r"\s*Top skills.*$", "", body_text, flags=re.IGNORECASE).strip()
            # Then strip trailing ...more, … more, see more
            body_text = re.sub(
                r"[\s\.\u2026]*(?:see\s+more|more|\.\.\.|…)\s*$",
                "",
                body_text,
                flags=re.IGNORECASE,
            ).strip()
            if body_text:
                if not about or len(body_text) > len(about):
                    about = body_text
            continue

        # 4b. Check Experience
        if (
            _matches_any_pattern(heading, EXPERIENCE_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["experience"]
        ):
            exp_items: list[ExperienceItem] = []
            for it in items:
                exps = _parse_experience_items(it)
                for exp in exps:
                    if exp.raw:
                        exp_items.append(exp)
            if exp_items:
                experience = exp_items
            continue

        # 4c. Check Education
        if (
            _matches_any_pattern(heading, EDUCATION_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["education"]
        ):
            edu_items: list[EducationItem] = []
            for it in items:
                edu_item = _parse_education_item(it)
                if edu_item.raw:
                    edu_items.append(edu_item)
            if edu_items:
                education = edu_items
            continue

        # 4d. Check Skills
        if _matches_any_pattern(heading, SKILLS_PATTERNS) or sec_id == KNOWN_SECTION_IDS["skills"]:
            skill_list: list[str] = []
            for it in items:
                lines = [_clean_text(s) for s in it.stripped_strings if _clean_text(s)]
                if lines:
                    skill_name = lines[0]
                    if _is_valid_skill(skill_name) and skill_name not in skill_list:
                        skill_list.append(skill_name)
            if skill_list:
                skills = skill_list
            continue

        # 4e. Check Certifications
        if (
            _matches_any_pattern(heading, CERTIFICATIONS_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["certifications"]
        ):
            cert_items = [_parse_certification_item(it) for it in items]
            valid_certs = [c for c in cert_items if c.name or c.raw]
            if valid_certs:
                certifications = valid_certs
                continue

        # 4f. Check Projects
        if (
            _matches_any_pattern(heading, PROJECTS_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["projects"]
        ):
            proj_items = [_parse_project_item(it) for it in items]
            valid_projs = [p for p in proj_items if p.title or p.raw]
            if valid_projs:
                projects = valid_projs
                continue

        # 4g. Check Languages
        if (
            _matches_any_pattern(heading, LANGUAGES_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["languages"]
        ):
            lang_items = [_parse_language_item(it) for it in items]
            valid_langs = [item for item in lang_items if item.name or item.raw]
            if valid_langs:
                languages = valid_langs
                continue

        # 4h. Route any other visible section into other_sections
        parsed_other = _parse_other_section(sec, heading or "Additional Information")
        if parsed_other:
            other_sections.append(parsed_other)

    other_sections = consolidate_other_sections(other_sections)

    # --------------------------------------------------------------------------
    # 5. Construct Profile Model
    # --------------------------------------------------------------------------
    name = _clean_text(name) if name else None
    headline = _clean_text(headline) if headline else None
    location = _clean_text(location) if location else None
    about = _clean_text(about) if about else None
    initial_contact_info = parse_contact_info(html) if html else None

    profile = Profile(
        name=name,
        headline=headline,
        location=location,
        about=about,
        experience=experience,
        education=education,
        skills=skills,
        certifications=_dedupe_certifications(certifications or []),
        projects=projects,
        languages=languages,
        contact_info=initial_contact_info,
    )

    # --------------------------------------------------------------------------
    # 6. Determine Missing Fields from Important Fields Configuration
    # --------------------------------------------------------------------------
    missing_fields: list[str] = []
    profile_data = profile.model_dump(exclude_none=True)
    for field in important_fields:
        val = profile_data.get(field)
        if val is None or (isinstance(val, list) and len(val) == 0):
            missing_fields.append(field)
        elif isinstance(val, str) and _is_placeholder_value(val):
            missing_fields.append(field)

    # --------------------------------------------------------------------------
    # 7. Text Coverage Sanity Check
    # --------------------------------------------------------------------------
    captured_pieces = [name or "", headline or "", location or "", about or ""]
    if experience:
        captured_pieces.extend(item.raw for item in experience)
    if education:
        captured_pieces.extend(item.raw for item in education)
    if skills:
        captured_pieces.extend(skills)
    if certifications:
        captured_pieces.extend(item.raw for item in certifications if item.raw)
    if projects:
        captured_pieces.extend(item.raw for item in projects if item.raw)
    if languages:
        captured_pieces.extend(item.raw for item in languages if item.raw)
    captured_pieces.extend(sec.text for sec in other_sections)

    total_captured_len = sum(len(p) for p in captured_pieces if p)
    total_page_len = len(total_visible_text)

    if total_page_len > 200:
        coverage = total_captured_len / total_page_len
        if coverage < 0.35:
            logger.warning(
                "Coverage check: low captured text ratio (%.1f%%) for slug '%s'",
                coverage * 100,
                slug,
            )

    has_profile_signals = bool(
        name
        or headline
        or location
        or about
        or experience
        or education
        or skills
        or certifications
        or projects
        or languages
    )
    final_status: Status = status
    final_reason: Reason | None = Reason(reason) if reason else None
    if status == Status.OK and not has_profile_signals:
        final_status = Status.UNREACHABLE
        final_reason = Reason.PARSE_FAILED

    return ProfileDocument(
        meta=Meta(
            source_url=source_url,
            slug=slug,
            status=final_status,
            reason=final_reason,
            missing_fields=missing_fields,
        ),
        profile=profile,
        other_sections=other_sections,
        raw_text=raw_text,
    )
