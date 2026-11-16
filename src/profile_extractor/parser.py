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
from typing import Any
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
    CONNECTION_DEGREE_PATTERN,
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


# Regex to match standalone section titles (e.g. "Education", "Skills (19)", "Licenses & certifications")
_SECTION_HEADER_ONLY_RE = re.compile(
    r"^(?:education|experience|work experience|skills|licenses?\s*(?:&|and)?\s*certifications?|"
    r"certifications?|projects?|languages?|volunteering|honors?\s*(?:&|and)?\s*awards?|"
    r"test\s+scores?|courses?|organizations?|publications?|patents?|recommendations?)"
    r"(?:\s*\(\d+\))?$",
    re.IGNORECASE,
)
# Matches "Show all" action buttons that should not be extracted as items
_SHOW_ALL_ONLY_RE = re.compile(r"^show\s+all\b", re.IGNORECASE)
# Matches category filter tab pill strings (e.g. "All | Industry Knowledge | Tools & Technologies")
_FILTER_PILLS_RE = re.compile(
    r"^(?:all|industry knowledge|tools\s*&\s*technologies|interpersonal skills|other skills|languages)"
    r"(?:\s*\|\s*(?:all|industry knowledge|tools\s*&\s*technologies|interpersonal skills|other skills|languages))*$",
    re.IGNORECASE,
)


def _is_header_or_footer_node(node: Tag) -> bool:
    """Return True if node is an empty spacer, section heading, filter tab bar, or 'Show all' footer.
    
    This prevents UI headers, navigation buttons, and category filter bars from being treated
    as profile entries (such as preventing 'Education' from being parsed as a school name).
    """
    # 1. Structural semantic tags that are inherently headings, dividers, or navigation
    if node.name in ("h1", "h2", "h3", "header", "hr", "nav"):
        return True
    
    # 2. Empty or whitespace-only nodes
    txt = _clean_text(node.get_text(" ", strip=True))
    if not txt:
        return True
        
    # 3. Text exactly matching section headings or 'Show all' action links
    if _SECTION_HEADER_ONLY_RE.match(txt) or _SHOW_ALL_ONLY_RE.match(txt):
        return True
        
    # 4. Pipe-joined filter pills (e.g. on /details/skills/ pages)
    pipe_txt = " | ".join(_clean_text(s) for s in node.stripped_strings if _clean_text(s))
    if _FILTER_PILLS_RE.match(pipe_txt):
        return True
    return False


def _is_nested_endorsement_listitem(el: Tag, search_root: Tag) -> bool:
    """Return True if a [role='listitem'] is a nested context/endorsement row inside a skill card.
    
    On LinkedIn skill cards, the endorsement list or experience connection list often uses
    role='list' and role='listitem'. This function detects if the list item is a sub-item
    belonging to an outer skill card rather than being a top-level entity item itself.
    """
    # Check if element is enclosed by an ARIA role="list" container
    role_list = el.find_parent(attrs={"role": "list"})
    if not isinstance(role_list, Tag):
        return False
        
    # Walk up from role_list up to 3 levels within search_root to verify if the card
    # has a preceding title block (e.g. the actual skill name <p> before the endorsement list).
    curr: Tag | None = role_list
    for _ in range(3):
        if curr is None or curr is search_root:
            break
        parent = curr.parent
        if not isinstance(parent, Tag):
            break
        for sib in parent.children:
            if sib is curr:
                break
            # If a sibling before this list has non-empty text, it's the skill heading
            if isinstance(sib, Tag) and _clean_text(sib.get_text(" ", strip=True)):
                return True
        curr = parent
    return False


def _unwrap_sdui_container_items(root: Tag) -> list[Tag]:
    """Unwrap SDUI section or LazyColumn wrappers to locate individual item tags.
    
    Modern LinkedIn SDUI pages nest collection items deeply inside multiple wrapper divs.
    This helper unrolls wrapper layers while filtering out headers and footers, exposing
    the true individual cards for parsing.
    """
    curr = root
    # Traverse down through wrapper div hierarchies up to 8 levels deep
    for _ in range(8):
        raw_children = [c for c in curr.children if isinstance(c, Tag)]
        
        # Check if any child tag is a section header (e.g. <h2>Education</h2>)
        had_hdr = any(
            _is_header_or_footer_node(c) and bool(_clean_text(c.get_text(" ", strip=True)))
            for c in raw_children
        )
        # Exclude header and footer nodes to focus on content nodes
        children = [c for c in raw_children if not _is_header_or_footer_node(c)]
        
        if had_hdr:
            # If all non-header children are inline/leaf elements (<p>, <span>) rather than card containers,
            # keep curr intact so multi-item line splitters can process its full lines.
            if children and all(
                c.name in ("p", "span", "strong", "b", "em", "i", "a") for c in children
            ):
                return [curr]
                
            # If exactly one list container remains below the header, inspect inside it
            if len(children) == 1:
                list_box = children[0]
                raw_leaves = [
                    c
                    for c in list_box.children
                    if isinstance(c, Tag) and not _is_header_or_footer_node(c)
                ]
                # If list_box contains direct inline leaves, preserve list_box for line parsing
                if raw_leaves and all(
                    c.name in ("p", "span", "strong", "b", "em", "i", "a") for c in raw_leaves
                ):
                    return [list_box]

                items = [
                    c
                    for c in list_box.children
                    if isinstance(c, Tag) and not _is_header_or_footer_node(c)
                ]
                # If nested list_box still has a single wrapper containing items, inspect one level deeper
                if len(items) == 1:
                    sub_items = [
                        c
                        for c in items[0].children
                        if isinstance(c, Tag) and not _is_header_or_footer_node(c)
                    ]
                    non_trivial = [
                        s
                        for s in sub_items
                        if len([x for x in s.stripped_strings if _clean_text(x)]) >= 2
                    ]
                    # Verify multiple distinct items are present
                    if len(sub_items) >= 2 and len(non_trivial) >= 2:
                        return sub_items
                return items if items else [list_box]
            return children
            
        if len(children) == 1:
            # Check if this single child is a list wrapper around multiple cards separated by <hr>
            child_items = [
                c
                for c in children[0].children
                if isinstance(c, Tag) and not _is_header_or_footer_node(c)
            ]
            has_hr = any(
                isinstance(c, Tag)
                and (c.name == "hr" or c.find("hr", recursive=False) is not None)
                for c in children[0].children
            )
            if has_hr and len(child_items) >= 2:
                return child_items
            # Continue unwrapping the single child container
            curr = children[0]
            continue
            
        if len(children) >= 2:
            # If multiple child containers exist and any is a batch wrapper, recursively unwrap them
            expanded: list[Tag] = []
            for ch in children:
                ch_raw = [gc for gc in ch.children if isinstance(gc, Tag)]
                ch_had_hdr = any(
                    _is_header_or_footer_node(gc)
                    and bool(_clean_text(gc.get_text(" ", strip=True)))
                    for gc in ch_raw
                )
                if ch_had_hdr:
                    expanded.extend(_unwrap_sdui_container_items(ch))
                else:
                    expanded.append(ch)
            return expanded
        break
        
    # Return the leaf node as a single item if non-empty and not a header
    clean_txt = _clean_text(curr.get_text(" ", strip=True))
    return [curr] if clean_txt and not _is_header_or_footer_node(curr) else []


def _get_container_entity_items(container: Tag) -> list[Tag]:
    """Extract top-level entity item tags from any DOM container (section or subpage main).

    Inspection cascade:
    1. Modern LinkedIn LazyColumn component (<div data-component-type="LazyColumn">).
       Grabs entity-collection-item elements or SDUI skill components.
    2. Direct [role="listitem"] elements (excluding nested skill endorsement sub-items).
    3. Direct <li> elements (ul > li or ol > li).
    4. Unwrapped SDUI container item divs.
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
        # Strategy 1b: SDUI skill items (com.linkedin.sdui.profile.skill)
        sdui_skill_items = [
            el
            for el in search_root.select(
                'div[componentkey^="com.linkedin.sdui.profile.skill"], '
                'div[id^="com.linkedin.sdui.profile.skill"]'
            )
            if isinstance(el, Tag)
        ]
        if sdui_skill_items:
            candidates = sdui_skill_items
        else:
            # Strategy 2: [role="listitem"] (excluding nested endorsement sub-items inside skills)
            role_items = [
                el
                for el in search_root.select('[role="listitem"]')
                if isinstance(el, Tag) and not _is_nested_endorsement_listitem(el, search_root)
            ]
            if role_items:
                candidates = role_items
            else:
                # Strategy 3: <li> elements
                lis = [li for li in search_root.select("ul > li, ol > li") if isinstance(li, Tag)]
                if lis:
                    candidates = lis
                else:
                    # Strategy 4: Unwrap SDUI container / LazyColumn children
                    candidates = _unwrap_sdui_container_items(search_root)

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
    # Never match employment-type lines (e.g. 'Club, College · Full-time'), dates, durations, or noise as location
    if EMPLOYMENT_TYPE_SUFFIX_PATTERN.search(line):
        return False
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
                if line in ("…", "...", "more", "see more", "Present"):
                    continue
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
                if not any(p.search(line) for p in NOISE_LINE_PATTERNS):
                    sub_desc_parts.append(line)

            raw_text = _clean_text(s_tag.get_text(separator=" | "))
            desc_str = " ".join(sub_desc_parts) if sub_desc_parts else None

            # Guard against narrative sentences accidentally captured as title
            if sub_title and (len(sub_title) > 140 or (len(sub_title) > 60 and "." in sub_title)):
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
        if line in ("…", "...", "more", "see more", "Present"):
            continue

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
        desc_parts.append(line)

    description: str | None = " ".join(desc_parts) if desc_parts else None
    raw_text = _clean_text(tag.get_text(separator=" | "))

    # Invert-proof title guard against narrative sentences
    if title and (len(title) > 140 or (len(title) > 60 and "." in title)):
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


# Common academic degree and diploma keywords used to delimit education records
_DEGREE_LINE_RE = re.compile(
    r"\b(?:bachelor|master|b\.?\s*tech|m\.?\s*tech|b\.?\s*e\b|m\.?\s*e\b|b\.?\s*sc|m\.?\s*sc|"
    r"mba|ph\.?d|doctor|diploma|high\s+school|senior\s+secondary|secondary|intermediate|"
    r"class\s*(?:10|12|x|xii)|cbse|icse|isc|aissce|aissee|hsc|ssc|postgraduate|undergraduate|"
    r"associate\s+of|certificate\s+in|b\.?\s*com|m\.?\s*com|b\.?\s*a\b|m\.?\s*a\b|bca|mca|bba)\b",
    re.IGNORECASE,
)
# Academic institution indicators
_SCHOOL_NAME_RE = re.compile(
    r"\b(?:university|institute|college|school|vidyalaya|academy|polytechnic|iit|nit|iiit|"
    r"bits\s+pilani|xlri|wharton|stanford)\b",
    re.IGNORECASE,
)
# Date range or single year patterns in education
_EDU_DATE_RE = re.compile(
    r"(?:\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[-–—]\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4}|Present)\b|"
    r"\b\d{4}\s*[-–—]\s*(?:\d{4}|Present)\b)",
    re.IGNORECASE,
)
_EDU_HEADER_RE = re.compile(r"^education(?:\s*\(\d+\))?$", re.IGNORECASE)


def _is_edu_start_line(lines: list[str], i: int) -> bool:
    """Return True if lines[i] represents the start of a distinct school/institution entry."""
    if i == 0:
        return True
    ln = lines[i]
    prev = lines[i - 1]
    # If previous line ends with a colon (e.g. 'Skills:', 'Coursework:'), it is a subfield, not a new school
    if prev.rstrip().endswith(":"):
        return False
    # Numbers and percentages are grades or stats, not school names
    if re.match(r"^\d", ln) or "%" in ln:
        return False
    # Non-school metadata indicators (grade, activities, skills, bullet points)
    if re.match(
        r"^(?:grade|cgpa|gpa|percentage|score|activities|skills|coursework|focus areas|-|•|\*)\b",
        ln,
        re.IGNORECASE,
    ):
        return False
    # Date lines are not school names
    if _EDU_DATE_RE.search(ln):
        return False
    next1 = lines[i + 1] if i + 1 < len(lines) else ""
    next2 = lines[i + 2] if i + 2 < len(lines) else ""
    # If line contains an institution keyword and is followed by degree/dates, it is a new school
    if _SCHOOL_NAME_RE.search(ln) and (
        _DEGREE_LINE_RE.search(next1)
        or _EDU_DATE_RE.search(next1)
        or _DEGREE_LINE_RE.search(next2)
        or _EDU_DATE_RE.search(next2)
    ):
        return True
    if not _DEGREE_LINE_RE.search(ln) and (
        _DEGREE_LINE_RE.search(next1) or _EDU_DATE_RE.search(next1)
    ):
        return True
    return False


def _split_edu_lines(lines: list[str]) -> list[list[str]]:
    """Split a combined list of education text lines into individual school card chunks.
    
    Prevents multiple degrees or institutions from being squashed into a single item's raw text.
    """
    # Strip any leading section heading (e.g. "Education")
    while lines and _EDU_HEADER_RE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return []
    chunks: list[list[str]] = []
    curr: list[str] = [lines[0]]
    has_meta = False
    
    # Iterate through lines and split whenever a new school boundary is detected after previous metadata
    for i in range(1, len(lines)):
        ln = lines[i]
        if (
            _EDU_DATE_RE.search(ln)
            or _DEGREE_LINE_RE.search(ln)
            or GRADE_PATTERN.search(ln)
        ):
            if len(curr) >= 1 and not _is_edu_start_line(lines, i):
                has_meta = True
        if has_meta and _is_edu_start_line(lines, i):
            chunks.append(curr)
            curr = [ln]
            has_meta = False
        else:
            curr.append(ln)
    if curr:
        chunks.append(curr)
    return chunks


def _build_education_item_from_lines(lines: list[str], fallback_raw: str = "") -> EducationItem:
    raw_text = " | ".join(lines) if lines else fallback_raw
    school: str | None = lines[0] if lines else None
    degree: str | None = None
    dates: str | None = None
    grade: str | None = None
    activities: str | None = None

    for line in lines[1:]:
        if not grade and GRADE_PATTERN.search(line):
            grade = line
            continue
        if not activities and ACTIVITIES_PATTERN.search(line):
            activities = line
            continue
        if not dates and (
            DATE_RANGE_PATTERN.search(line)
            or re.search(r"\b\d{4}\s*[-–—]\s*(?:\d{4}|Present)\b", line)
        ):
            dates = line
            continue
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


def _parse_education_items(tag: Tag) -> list[EducationItem]:
    """Extract one or more structured EducationItem entries from a DOM tag."""
    raw_lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line)
        and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
        and not UI_BADGE_PATTERN.search(line)
    ]
    lines = _dedupe_consecutive(raw_lines)
    chunks = _split_edu_lines(lines)
    return [_build_education_item_from_lines(chunk) for chunk in chunks if chunk]


def _parse_education_item(tag: Tag) -> EducationItem:
    """Extract structured fields from an education entry using semantic line classification."""
    items = _parse_education_items(tag)
    if items:
        return items[0]
    return EducationItem(raw="")


# Header pattern for Licenses & Certifications
_CERT_HEADER_RE = re.compile(
    r"^(?:licenses?\s*(?:&|and)?\s*certifications?|certifications?)(?:\s*\(\d+\))?$",
    re.IGNORECASE,
)
# Anchors identifying date lines or credential IDs for certificate separation
_ISSUED_LINE_RE = re.compile(r"^(?:issued|expires|expired)\b", re.IGNORECASE)
_CRED_ID_LINE_RE = re.compile(r"^credential\s+id\b", re.IGNORECASE)


def _split_cert_lines(lines: list[str]) -> list[list[str]]:
    """Split a squashed list of certificate lines into distinct credential chunks.
    
    Identifies 'Issued <date>' and 'Credential ID' anchors to separate multiple credentials
    that were grouped into one raw DOM container.
    """
    # Remove any leading section title
    while lines and _CERT_HEADER_RE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return []
    anchors: list[int] = []
    # Find all date/credential ID anchor line indices
    for i, ln in enumerate(lines):
        if _ISSUED_LINE_RE.match(ln):
            anchors.append(i)
        elif _CRED_ID_LINE_RE.match(ln):
            if i == 0 or not _ISSUED_LINE_RE.match(lines[i - 1]):
                anchors.append(i)
    # If 0 or 1 anchor, treat as a single certification entry
    if len(anchors) <= 1:
        return [lines]
    # Each certificate starts ~2 lines before its 'Issued' date (Certificate Name, Issuer)
    starts = [max(0, a - 2) for a in anchors]
    starts[0] = 0
    chunks: list[list[str]] = []
    for idx, st in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        if st < end:
            chunks.append(lines[st:end])
    return chunks


def _build_certification_item_from_lines(
    lines: list[str], credential_url: str | None = None
) -> CertificationItem:
    """Build a structured CertificationItem from parsed semantic lines.

    Lines layout:
    - Line 0: Certificate or credential name (e.g. 'AWS Certified Solutions Architect')
    - Subsequent lines: Issuing authority, issuance date ('Issued May 2024'), expiration date,
      and credential identifier.
    """
    raw_text = " | ".join(lines)
    # The first line of a credential block is always the credential title
    name: str | None = lines[0] if lines else None
    issuer: str | None = None
    issue_date: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None

    for line in lines[1:]:
        line_lower = line.lower()
        # Parse 'Issued <date>' metadata
        if "issued" in line_lower and not issue_date:
            issue_date = line
            continue
        # Parse 'Expires <date>' or expiration metadata
        if "expires" in line_lower and not expiration_date:
            expiration_date = line
            continue
        # Parse credential ID code string
        if "credential id" in line_lower and not credential_id:
            credential_id = line
            continue
        # The organization/issuer line is the non-date, non-action line
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


def _parse_certification_items(tag: Tag) -> list[CertificationItem]:
    """Extract one or more structured CertificationItem entries from a DOM tag."""
    lines = _clean_role_lines(tag)
    chunks = _split_cert_lines(lines)
    if not chunks:
        return []
    credential_url: str | None = None
    if len(chunks) == 1:
        cred_a = tag.find("a", href=True)
        if cred_a and isinstance(cred_a, Tag):
            href = str(cred_a.get("href", ""))
            if "http" in href:
                credential_url = href
    return [_build_certification_item_from_lines(ch, credential_url) for ch in chunks if ch]


def _parse_certification_item(tag: Tag) -> CertificationItem:
    """Extract structured fields from a license / certification entry."""
    items = _parse_certification_items(tag)
    if items:
        return items[0]
    return CertificationItem(raw="")


# Regex to detect project dates (e.g. "Jan 2026 – Present", "2024 – 2025")
_PROJ_HEADER_RE = re.compile(r"^projects?(?:\s*\(\d+\))?$", re.IGNORECASE)
_PROJ_DATE_RE = re.compile(
    r"^(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[-–—]\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|Present)|"
    r"\d{4}\s*[-–—]\s*(?:\d{4}|Present))$",
    re.IGNORECASE,
)
_ASSOC_RE = re.compile(r"^Associated with\b", re.IGNORECASE)


def _is_proj_desc_line(ln: str) -> bool:
    """Return True if ln represents project narrative description or noise rather than a title."""
    if ln.startswith(("-", "•", "●", "*", "…", "...")):
        return True
    if ln.lower() in ("more", "see more", "show project", "project link", "see project"):
        return True
    if SKILLS_CHIP_PATTERN.search(ln) or _ASSOC_RE.match(ln):
        return True
    if len(ln) > 95 or ln.endswith("."):
        return True
    return False


def _split_proj_lines(lines: list[str]) -> list[list[str]]:
    """Split a combined sequence of project lines into individual project records.
    
    Identifies new project titles based on boundary lines (e.g. lines preceding dates or bullet descriptions).
    """
    while lines and _PROJ_HEADER_RE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return []
    chunks: list[list[str]] = []
    curr: list[str] = [lines[0]]
    saw_body = False
    for i in range(1, len(lines)):
        ln = lines[i]
        next_ln = lines[i + 1] if i + 1 < len(lines) else ""
        prev_ln = lines[i - 1]
        is_new_title = False
        if not _is_proj_desc_line(ln) and not _PROJ_DATE_RE.match(ln):
            if _PROJ_DATE_RE.match(next_ln):
                is_new_title = True
            elif saw_body and (
                SKILLS_CHIP_PATTERN.search(prev_ln)
                or _ASSOC_RE.match(prev_ln)
                or next_ln.startswith(("-", "•", "●", "*"))
            ):
                is_new_title = True
        if is_new_title and curr:
            chunks.append(curr)
            curr = [ln]
            saw_body = False
        else:
            curr.append(ln)
            if _is_proj_desc_line(ln) or _PROJ_DATE_RE.match(ln):
                saw_body = True
    if curr:
        chunks.append(curr)
    return chunks


def _build_project_item_from_lines(lines: list[str], url: str | None = None) -> ProjectItem:
    """Build a structured ProjectItem from parsed semantic lines.

    Lines layout:
    - Line 0: Project title (e.g. 'Automated Resume Matcher')
    - Subsequent lines: Date periods, associated skills chips, and multi-line narrative descriptions.
    """
    raw_text = " | ".join(lines)
    # The first line is always the project name
    title: str | None = lines[0] if lines else None
    dates: str | None = None
    description_parts: list[str] = []
    project_skills: list[str] | None = None

    for line in lines[1:]:
        # Detect short project timeline or standalone year/month-year line (never full descriptions)
        stripped = line.strip()
        is_proj_date = len(stripped) <= 60 and (
            bool(DATE_RANGE_PATTERN.search(stripped))
            or bool(_PROJ_DATE_RE.fullmatch(stripped))
            or bool(
                re.fullmatch(
                    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+)?\d{4}",
                    stripped,
                    re.IGNORECASE,
                )
            )
        )
        if not dates and is_proj_date:
            dates = line
            continue
        # Detect associated skills chip list
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
        # Accumulate genuine project description lines (ignoring 'see more' truncators and UI badges)
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


def _parse_project_items(tag: Tag) -> list[ProjectItem]:
    """Extract one or more structured ProjectItem entries from a DOM tag."""
    lines = _clean_role_lines(tag)
    chunks = _split_proj_lines(lines)
    if not chunks:
        return []
    url: str | None = None
    if len(chunks) == 1:
        a_tag = tag.find("a", href=True)
        if a_tag and isinstance(a_tag, Tag):
            href = str(a_tag.get("href", ""))
            if "http" in href and "linkedin.com" not in href:
                url = href
    return [_build_project_item_from_lines(ch, url) for ch in chunks if ch]


def _parse_project_item(tag: Tag) -> ProjectItem:
    """Extract structured fields from an academic, personal, or professional project entry."""
    items = _parse_project_items(tag)
    if items:
        return items[0]
    return ProjectItem(raw="")


_LANG_HEADER_RE = re.compile(r"^languages?(?:\s*\(\d+\))?$", re.IGNORECASE)
# Standard language proficiency descriptors
_PROFICIENCY_RE = re.compile(
    r"\b(?:proficiency|native|bilingual|elementary|limited working|"
    r"professional working|full professional)\b",
    re.IGNORECASE,
)


def _parse_language_items(tag: Tag) -> list[LanguageItem]:
    """Extract one or more structured LanguageItem entries from a DOM tag.
    
    Pairs language names with their corresponding proficiency lines and handles multi-language
    lists squashed into a single DOM element.
    """
    lines = _clean_role_lines(tag)
    while lines and _LANG_HEADER_RE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return []

    prof_indices = [i for i, ln in enumerate(lines) if _PROFICIENCY_RE.search(ln)]
    if len(prof_indices) <= 1:
        return [
            LanguageItem(
                name=lines[0],
                proficiency=lines[1] if len(lines) > 1 else None,
                raw=" | ".join(lines),
            )
        ]

    results: list[LanguageItem] = []
    first_start = max(0, prof_indices[0] - 1)
    for idx in range(0, first_start):
        results.append(LanguageItem(name=lines[idx], proficiency=None, raw=lines[idx]))

    # Split lines based on detected proficiency anchors
    starts = [max(0, p_idx - 1) for p_idx in prof_indices]
    for k, st in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        sub = lines[st:end]
        if sub:
            results.append(
                LanguageItem(
                    name=sub[0],
                    proficiency=sub[1] if len(sub) > 1 else None,
                    raw=" | ".join(sub),
                )
            )
    return results


def _parse_language_item(tag: Tag) -> LanguageItem:
    """Extract structured fields from a language proficiency entry."""
    items = _parse_language_items(tag)
    if items:
        return items[0]
    return LanguageItem(raw="")


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
    if cand in [
        "Skills",
        "Show all",
        "Endorsements",
        "More profiles for you",
        "All",
        "Industry Knowledge",
        "Tools & Technologies",
        "Interpersonal Skills",
        "Other Skills",
    ]:
        return False
    if re.search(r"^skills(?:\s*\(\d+\))?$", cand, re.IGNORECASE):
        return False
    if re.search(r"^\d+\s+endorsements?$", cand, re.IGNORECASE):
        return False
    if re.search(r"^show all\b", cand, re.IGNORECASE):
        return False
    if re.search(r"^passed\s+linkedin\s+skill\s+assessment$", cand, re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:\d+\s+(?:educational\s+)?experiences?\s+(?:at|across)|experiences? across|endorsed by)\b",
        cand,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"\b(?:engineer|developer|analyst|manager|consultant|architect|intern|"
        r"trainee|fellow|fellowship|coordinator|director|president|lead|leader|"
        r"editor|writer|designer|assistant|associate|specialist|member|server|"
        r"teacher|professor|founder)\b.*\bat\s+\S+",
        cand,
        re.IGNORECASE,
    ):
        return False
    return True


def _sanitize_details_soup(soup: BeautifulSoup) -> Tag:
    """Strip nav, footer, aside, modals, and sidebar recommendation sections from a details page."""
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for selector in STRIP_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
    main = soup.find("main") or soup
    for sec in main.find_all("section"):
        heading = _extract_section_heading(sec)
        if heading and _matches_any_pattern(heading, SKIP_SECTION_PATTERNS):
            sec.decompose()
    return main


def parse_skills_details(html: str) -> list[str]:
    """Parse all skills from a LinkedIn /details/skills/ page."""
    soup = BeautifulSoup(html, "lxml")
    main = _sanitize_details_soup(soup)
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
    main = _sanitize_details_soup(soup)
    top_items = _get_container_entity_items(main)
    results: list[EducationItem] = []
    for it in top_items:
        for edu in _parse_education_items(it):
            if edu.school or edu.raw:
                results.append(edu)
    return results


def parse_experience_details(html: str) -> list[ExperienceItem]:
    """Parse all experience items from a LinkedIn /details/experience/ page."""
    soup = BeautifulSoup(html, "lxml")
    main = _sanitize_details_soup(soup)
    top_items = _get_container_entity_items(main)
    results: list[ExperienceItem] = []
    for it in top_items:
        exps = _parse_experience_items(it)
        for exp in exps:
            if exp.title and re.match(r"^(?:experience|work experience)$", exp.title, re.I) and not exp.company:
                continue
            results.append(exp)
    return results


def parse_certifications_details(html: str) -> list[CertificationItem]:
    """Parse all certifications from a LinkedIn /details/certifications/ page."""
    soup = BeautifulSoup(html, "lxml")
    main = _sanitize_details_soup(soup)
    top_items = _get_container_entity_items(main)
    results: list[CertificationItem] = []
    for it in top_items:
        for cert in _parse_certification_items(it):
            if cert.name or cert.raw:
                results.append(cert)
    return _dedupe_certifications(results)


def parse_projects_details(html: str) -> list[ProjectItem]:
    """Parse all projects from a LinkedIn /details/projects/ page."""
    soup = BeautifulSoup(html, "lxml")
    main = _sanitize_details_soup(soup)
    top_items = _get_container_entity_items(main)
    results: list[ProjectItem] = []
    for it in top_items:
        for proj in _parse_project_items(it):
            if proj.title or proj.raw:
                results.append(proj)
    return results


def parse_languages_details(html: str) -> list[LanguageItem]:
    """Parse all languages from a LinkedIn /details/languages/ page."""
    soup = BeautifulSoup(html, "lxml")
    main = _sanitize_details_soup(soup)
    top_items = _get_container_entity_items(main)
    results: list[LanguageItem] = []
    for it in top_items:
        for lang in _parse_language_items(it):
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
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            return None
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        return href
    except Exception:
        return None


def _find_contact_info_modal(soup: BeautifulSoup) -> Tag | None:
    """Locate the genuine LinkedIn contact info modal dialog.

    Specifically excludes top-nav search autocomplete popups, global headers, and sidebars.
    Supports modern SDUI overlay containers, legacy #artdeco-modal-outlet, and #interop-outlet.
    """
    # 1. Modern SDUI Contact Overlay container check
    sdui_candidate = soup.select_one(
        '[data-sdui-screen*="ContactDetailsOverlay"], '
        '[id*="ContactInfoDetailSection"]'
    )
    if sdui_candidate is not None:
        outer_wrapper = sdui_candidate.find_parent(
            lambda t: isinstance(t, Tag) and t.name == "div" and t.find("header", id="dialog-header") is not None
        )
        target = outer_wrapper if outer_wrapper is not None else sdui_candidate
        txt = target.get_text().lower()
        if "recent entity history" not in txt and "suggestions available" not in txt:
            return target

    # 2. Check for dialog wrapper containing header#dialog-header with "contact info"
    for hdr in soup.select("header#dialog-header, header:has(h2)"):
        if "contact info" in hdr.get_text().lower():
            wrapper = hdr.find_parent(["div", "section"])
            if wrapper is not None:
                txt = wrapper.get_text().lower()
                if "recent entity history" not in txt and "suggestions available" not in txt:
                    return wrapper

    # 3. Known modal outlets
    for outlet_sel in ("#artdeco-modal-outlet", "#interop-outlet"):
        outlet = soup.select_one(outlet_sel)
        if outlet:
            for dlg in outlet.select('[role="dialog"], .artdeco-modal, [aria-modal="true"]'):
                txt = dlg.get_text().lower()
                if "recent entity history" in txt or "suggestions available" in txt:
                    continue
                if (
                    "contact info" in txt
                    or "email" in txt
                    or "phone" in txt
                    or "website" in txt
                    or "profile" in txt
                    or "connected" in txt
                    or dlg.select_one(
                        "#pv-contact-info, .pv-contact-info, .ci-vanity-url, "
                        ".ci-email, .ci-phone, a[href^='mailto:'], a[href*='linkedin.com/in/']"
                    )
                ):
                    return dlg

    # 4. Global dialogs
    for dlg in soup.select(
        '[role="dialog"], [aria-modal="true"], .artdeco-modal, section.pv-contact-info, #pv-contact-info'
    ):
        if (
            dlg.find_parent(["nav", "header"])
            or dlg.get("id") == "global-nav-typeahead"
            or dlg.find_parent(id="global-nav-typeahead")
        ):
            continue
        txt = dlg.get_text().lower()
        if "recent entity history" in txt or "suggestions available" in txt:
            continue
        if (
            "contact info" in txt
            or "email" in txt
            or "phone" in txt
            or "website" in txt
            or "profile" in txt
            or "connected" in txt
            or dlg.select_one(
                "#pv-contact-info, .pv-contact-info, .ci-vanity-url, "
                ".ci-email, .ci-phone, a[href^='mailto:'], a[href*='linkedin.com/in/']"
            )
        ):
            return dlg

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

    # Decompose global header, nav, search typeahead, and footer to prevent noise capture
    for unwanted in soup.select("nav, header, #global-nav, #global-nav-typeahead, footer, .global-footer"):
        unwanted.decompose()

    modal = _find_contact_info_modal(soup) or soup.select_one("section.pv-contact-info")
    if modal is None:
        return None

    raw_text = modal.get_text(separator="\n", strip=True)
    if "recent entity history" in raw_text.lower() or "suggestions available" in raw_text.lower():
        return None

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
        else:
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            for p_idx, line in enumerate(lines[:-1]):
                if line.lower() in ("phone", "phone number", "mobile", "mobile number"):
                    cand = lines[p_idx + 1]
                    if re.search(r"^[+]?[\d\s().-]{7,25}$", cand):
                        phone = cand
                        break

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


def _merge_skill_section_items(
    existing: list[str] | None, incoming: Sequence[str]
) -> list[str]:
    """Merge skill strings preserving first-seen casing/order without duplicates."""
    merged = list(existing) if existing else []
    seen = {" ".join(item.split()).casefold() for item in merged if item.strip()}
    for skill in incoming:
        clean = " ".join(skill.split())
        if not clean:
            continue
        key = clean.casefold()
        if key not in seen:
            merged.append(clean)
            seen.add(key)
    return merged


def _section_record_matches(section: str, left: Any, right: Any) -> bool:
    """Return True if two parsed section records represent the same entry."""
    a = left.model_dump(exclude_none=True)
    b = right.model_dump(exclude_none=True)

    def val(source: dict[str, Any], field: str) -> str:
        raw_val = source.get(field)
        return _clean_text(raw_val).casefold() if isinstance(raw_val, str) else ""

    def compatible(field: str) -> bool:
        av, bv = val(a, field), val(b, field)
        return not (av and bv and av != bv)

    if section == "experience":
        return (
            bool(val(a, "title"))
            and val(a, "title") == val(b, "title")
            and compatible("company")
            and compatible("dates")
            and bool(
                (val(a, "company") and val(b, "company"))
                or (val(a, "dates") and val(b, "dates"))
            )
        )
    if section == "education":
        return (
            bool(val(a, "school"))
            and val(a, "school") == val(b, "school")
            and compatible("degree")
            and compatible("dates")
            and bool(
                (val(a, "degree") and val(b, "degree"))
                or (val(a, "dates") and val(b, "dates"))
            )
        )
    if section == "certifications":
        left_id, right_id = val(a, "credential_id"), val(b, "credential_id")
        if left_id and right_id:
            return left_id == right_id
        return bool(val(a, "name")) and val(a, "name") == val(b, "name") and compatible("issuer")
    if section == "projects":
        left_url, right_url = val(a, "url"), val(b, "url")
        if left_url and right_url:
            return left_url == right_url
        return bool(val(a, "title")) and val(a, "title") == val(b, "title") and compatible("dates")
    if section == "languages":
        return bool(val(a, "name")) and val(a, "name") == val(b, "name")
    return bool(val(a, "raw")) and val(a, "raw") == val(b, "raw")


def _merge_section_record(existing: Any, incoming: Any) -> None:
    """Fill missing scalar fields and retain richer descriptions/skills/raw text on match."""
    for field, value in incoming.model_dump(exclude_none=True).items():
        if value is None or value == "" or value == []:
            continue
        current = getattr(existing, field, None)
        if current is None or current == "" or current == []:
            setattr(existing, field, value)
        elif (
            field in ("description", "raw")
            and isinstance(current, str)
            and isinstance(value, str)
        ):
            if len(value) > len(current):
                setattr(existing, field, value)
        elif field == "skills" and isinstance(current, list) and isinstance(value, list):
            seen = {
                " ".join(str(item).split()).casefold()
                for item in current
                if str(item).strip()
            }
            for item in value:
                item_str = str(item)
                key = " ".join(item_str.split()).casefold()
                if key and key not in seen:
                    current.append(item_str)
                    seen.add(key)


def _merge_structured_section_items(
    section: str, existing: list[Any] | None, incoming: Sequence[Any]
) -> list[Any]:
    """Merge matching structured section records additively while preserving order."""
    merged = [item.model_copy(deep=True) for item in existing] if existing else []
    for candidate in incoming:
        match = next(
            (item for item in merged if _section_record_matches(section, item, candidate)),
            None,
        )
        if match is None:
            merged.append(candidate.model_copy(deep=True))
        else:
            _merge_section_record(match, candidate)
    return merged


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
    all_main_sections = main.find_all("section")
    candidate_sections = [
        sec
        for sec in all_main_sections
        if sec.get("aria-label") != "Primary content"
        and sec.get("aria-roledescription") != "carousel"
        and sec.get("data-testid") != "carousel"
        and not re.search(
            r"^\d+\s+notification", sec.get_text(separator=" ", strip=True), re.IGNORECASE
        )
        and len(
            [
                child_sec
                for child_sec in sec.find_all("section")
                if child_sec is not sec and _extract_section_heading(child_sec)
            ]
        )
        < 2
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

            # Headline: scan immediate lines after name, skipping pronouns, connection badges, and buttons
            for cand in filtered[name_idx + 1 : name_idx + 6]:
                if PRONOUN_PATTERN.match(cand):
                    continue
                if CONNECTION_DEGREE_PATTERN.match(cand):
                    continue
                if cand in ("·", "\u00b7", "•"):
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
                experience = _merge_structured_section_items("experience", experience, exp_items)
            continue

        # 4c. Check Education
        if (
            _matches_any_pattern(heading, EDUCATION_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["education"]
        ):
            edu_items: list[EducationItem] = []
            for it in items:
                for edu_item in _parse_education_items(it):
                    if edu_item.raw:
                        edu_items.append(edu_item)
            if edu_items:
                education = _merge_structured_section_items("education", education, edu_items)
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
                skills = _merge_skill_section_items(skills, skill_list)
            continue

        # 4e. Check Certifications
        if (
            _matches_any_pattern(heading, CERTIFICATIONS_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["certifications"]
        ):
            valid_certs: list[CertificationItem] = []
            for it in items:
                for c in _parse_certification_items(it):
                    if c.name or c.raw:
                        valid_certs.append(c)
            if valid_certs:
                certifications = _merge_structured_section_items(
                    "certifications", certifications, valid_certs
                )
                continue

        # 4f. Check Projects
        if (
            _matches_any_pattern(heading, PROJECTS_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["projects"]
        ):
            valid_projs: list[ProjectItem] = []
            for it in items:
                for p in _parse_project_items(it):
                    if p.title or p.raw:
                        valid_projs.append(p)
            if valid_projs:
                projects = _merge_structured_section_items("projects", projects, valid_projs)
                continue

        # 4g. Check Languages
        if (
            _matches_any_pattern(heading, LANGUAGES_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["languages"]
        ):
            valid_langs: list[LanguageItem] = []
            for it in items:
                for lang_item in _parse_language_items(it):
                    if lang_item.name or lang_item.raw:
                        valid_langs.append(lang_item)
            if valid_langs:
                languages = _merge_structured_section_items("languages", languages, valid_langs)
                # Keep unstructured/narrative language list in other_sections if no valid proficiency lines were matched
                if not any(item.proficiency for item in valid_langs):
                    parsed_other = _parse_other_section(sec, heading or "Languages")
                    if parsed_other:
                        other_sections.append(parsed_other)
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
