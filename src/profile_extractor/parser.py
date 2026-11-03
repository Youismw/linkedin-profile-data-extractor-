"""DOM parsing and structured profile extraction using BeautifulSoup.

Architecture & Parsing Strategy:
1. Two-Tier Extraction:
   - Structured Core Fields: name, headline, location, about, experience, education, skills.
   - Complete Raw Text Dump: Appends unedited full text under 'raw_text' in each document.
   - Preservation of Unmapped Sections: Unmapped sections (Licenses, Patents, Activity)
     land cleanly in 'other_sections' so zero data is lost.
2. Resilience against LinkedIn SPA Updates:
   - Does NOT rely on fragile minified CSS classes (e.g. '.artdeco-card--compact-7d8').
   - Targets semantic elements (`h1`, `h2`, `<section>`), component attributes
     (`data-component-type="LazyColumn"`), and ARIA listitems (`role="listitem"`).
3. Text Coverage Verification:
   - Measures captured text against the entire page body text to ensure high fidelity.
"""

import re
from collections.abc import Sequence

from bs4 import BeautifulSoup, Tag

from profile_extractor.logging_setup import get_logger
from profile_extractor.models import (
    EducationItem,
    ExperienceItem,
    Meta,
    OtherSection,
    Profile,
    ProfileDocument,
    Reason,
    Status,
)
from profile_extractor.parser_rules import (
    ABOUT_PATTERNS,
    EDUCATION_PATTERNS,
    EXPERIENCE_PATTERNS,
    KNOWN_SECTION_IDS,
    NOISE_LINE_PATTERNS,
    PRONOUN_PATTERN,
    SKILLS_PATTERNS,
    SKIP_SECTION_PATTERNS,
    STRIP_TAGS,
)


def _clean_text(text: str | None) -> str:
    """Normalize whitespace in extracted strings (collapsing multiple spaces and newlines)."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _matches_any_pattern(text: str, patterns: list[re.Pattern[str]]) -> bool:
    """Check if normalized text matches any of the compiled regex patterns."""
    clean = _clean_text(text)
    return any(p.search(clean) for p in patterns)


def _extract_section_heading(section: Tag) -> str:
    """Find and return the heading text of a section.

    Inspection cascade:
    1. Direct <h2>, <h3>, or <header> tags.
    2. 'aria-label' attribute on the section container.
    3. 'id' attribute on the section container.
    """
    # 1. Check for standard heading tags inside section
    for heading_tag in section.find_all(["h2", "h3", "header"], recursive=True):
        txt = _clean_text(heading_tag.get_text())
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


def _get_section_items(sec: Tag) -> list[Tag]:
    """Extract individual item tags (li, role=listitem, or LazyColumn elements).

    Handles both legacy HTML (<ul><li>) and LinkedIn's redesign
    (<div data-component-type="LazyColumn"><div role="listitem">).
    """
    # Strategy 1: Standard <li> list items
    lis = sec.find_all("li", recursive=True)
    if lis:
        return [li for li in lis if isinstance(li, Tag)]

    # Strategy 2: Modern LinkedIn LazyColumn component
    lazy_col = sec.select_one('[data-component-type="LazyColumn"]')
    if lazy_col:
        items = [
            el
            for el in lazy_col.select("div[componentkey]")
            if str(el.get("componentkey", "")).startswith("entity-collection-item")
        ]
        if items:
            return items
        role_items = [el for el in lazy_col.select('[role="listitem"]') if isinstance(el, Tag)]
        if role_items:
            return role_items
        direct_children = [c for c in lazy_col.children if isinstance(c, Tag)]
        if direct_children:
            return direct_children

    # Strategy 3: Direct ARIA listitems within section
    role_items = [el for el in sec.select('[role="listitem"]') if isinstance(el, Tag)]
    if role_items:
        return role_items

    return []


def _parse_experience_item(tag: Tag) -> ExperienceItem:
    """Extract structured fields and complete raw text from a work experience entry.

    Heuristic Breakdown:
    - Extracts stripped strings, ignoring UI button noise ('show all', 'edit').
    - Handles standard single-role entries (Title, Company, Dates, Location, Description).
    - Handles multi-role entries at the same company (where line 1 indicates total duration).
    """
    lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line) and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
    ]
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    title: str | None = None
    company: str | None = None
    dates: str | None = None
    location: str | None = None
    description: str | None = None

    if lines:
        # Multi-role heuristic: line 1 has duration words ("2 yrs 8 mos")
        has_duration = re.search(
            r"\b(?:yr|yrs|mo|mos|year|years|month|months)\b",
            lines[1] if len(lines) > 1 else "",
            re.IGNORECASE,
        )
        if len(lines) > 1 and has_duration:
            company = lines[0]
            title = lines[2] if len(lines) > 2 else lines[0]
            dates = lines[3] if len(lines) > 3 else lines[1]
            if len(lines) > 4:
                description = " ".join(lines[4:])
        else:
            # Standard single-role layout
            title = lines[0]
            if len(lines) > 1:
                company = lines[1]
            if len(lines) > 2:
                dates = lines[2]
            if len(lines) > 3:
                location = lines[3]
            if len(lines) > 4:
                description = " ".join(lines[4:])

    return ExperienceItem(
        title=title,
        company=company,
        dates=dates,
        location=location,
        description=description,
        raw=raw_text,
    )


def _parse_education_item(tag: Tag) -> EducationItem:
    """Extract structured fields and complete raw text from an education entry.

    Extracts: School / Institution, Degree / Major, Dates / Honors, and full raw string.
    """
    lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line) and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
    ]
    raw_text = " | ".join(lines) if lines else _clean_text(tag.get_text(separator=" | "))

    school: str | None = None
    degree: str | None = None
    dates: str | None = None

    if lines:
        school = lines[0]
    if len(lines) > 1:
        degree = lines[1]
    if len(lines) > 2:
        dates = lines[2]

    return EducationItem(
        school=school,
        degree=degree,
        dates=dates,
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
    2. DOM cleanup (stripping scripts, styles, templates).
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

    # Strip scripts, stylesheets, templates, and SVGs
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.find("body") or soup
    total_visible_text = _clean_text(body.get_text())

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

    # Filter out outer layout containers, nested carousels, and dialogs
    valid_sections: list[Tag] = []
    for sec in main.find_all("section"):
        if sec.get("aria-label") == "Primary content":
            continue
        if sec.get("aria-roledescription") == "carousel" or sec.get("data-testid") == "carousel":
            continue
        sec_text = sec.get_text(strip=True)
        if re.search(r"^\d+\s+notification", sec_text, re.IGNORECASE):
            continue
        valid_sections.append(sec)

    # Strategy A: Check <h1> tags
    h1 = soup.find("h1")
    if h1 and _clean_text(h1.get_text()):
        name = _clean_text(h1.get_text())
        top_card = h1.find_parent("section")

    # Strategy B: First valid section in <main> (LinkedIn logged-in profile top card)
    if not top_card and valid_sections:
        top_card = valid_sections[0]

    if top_card:
        top_lines = [_clean_text(s) for s in top_card.stripped_strings if _clean_text(s)]
        # Filter out UI noise badges like "0 notifications" or "Ad Options"
        filtered = [
            line
            for line in top_lines
            if not re.search(r"^\d+\s+notification", line, re.IGNORECASE)
            and not re.search(r"^ad\s+options", line, re.IGNORECASE)
        ]

        if not name and filtered:
            name = filtered[0]

        if name and filtered:
            try:
                name_idx = filtered.index(name)
                # Headline: scan immediate lines after name, skipping pronouns and buttons
                for cand in filtered[name_idx + 1 : name_idx + 4]:
                    if PRONOUN_PATTERN.match(cand):
                        continue
                    if cand in ["--", "-", "—"]:
                        continue
                    if any(
                        kw in cand.lower()
                        for kw in [
                            "contact info",
                            "connections",
                            "connect",
                            "message",
                            "more",
                            "followers",
                        ]
                    ):
                        continue
                    headline = cand
                    break

                # Location: scan lines for geographic indicators
                for cand in filtered[name_idx + 1 : name_idx + 7]:
                    if "," in cand or any(
                        kw in cand.lower()
                        for kw in [
                            "area",
                            "united",
                            "india",
                            "states",
                            "germany",
                            "uk",
                            "canada",
                            "california",
                            "delhi",
                            "london",
                        ]
                    ):
                        noise_kws = [
                            "contact info",
                            "connections",
                            "connect",
                            "message",
                            "followers",
                        ]
                        if not any(kw in cand.lower() for kw in noise_kws):
                            location = cand
                            break
            except ValueError:
                pass

    # Strategy C: Title tag fallback ("Name | LinkedIn")
    if not name and soup.title and soup.title.string:
        raw_title = soup.title.string.strip()
        if " | " in raw_title:
            name = _clean_text(raw_title.split(" | ")[0])
        elif " - " in raw_title:
            name = _clean_text(raw_title.split(" - ")[0])

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
    other_sections: list[OtherSection] = []

    seen_sections: set[int] = set()

    for sec in valid_sections:
        if id(sec) in seen_sections:
            continue
        seen_sections.add(id(sec))

        # Skip top card section
        if top_card and sec == top_card:
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
            body_text = _clean_text(sec.get_text())
            # Strip heading if present at start
            if heading and body_text.startswith(heading):
                body_text = _clean_text(body_text[len(heading) :])
            body_text = re.sub(r"\s*\.\.\.more$", "", body_text)
            body_text = re.sub(r"\s*\bsee more$", "", body_text, flags=re.IGNORECASE)
            if body_text:
                about = body_text
            continue

        # 4b. Check Experience
        if (
            _matches_any_pattern(heading, EXPERIENCE_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["experience"]
        ):
            exp_items: list[ExperienceItem] = []
            for it in items:
                item = _parse_experience_item(it)
                if item.raw:
                    exp_items.append(item)
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
                item = _parse_education_item(it)
                if item.raw:
                    edu_items.append(item)
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
                    if (
                        not re.search(r"^\d+\s+endorsements?$", skill_name, re.IGNORECASE)
                        and not re.search(r"^show all\b", skill_name, re.IGNORECASE)
                    ):
                        skill_list.append(skill_name)
            if skill_list:
                skills = skill_list
            continue

        # 4e. Route any other visible section into other_sections
        sec_content = _clean_text(sec.get_text())
        if sec_content:
            if heading and sec_content.startswith(heading):
                sec_content = _clean_text(sec_content[len(heading) :])
            if sec_content and len(sec_content) > 5:
                display_heading = heading or "Additional Information"
                other_sections.append(OtherSection(heading=display_heading, text=sec_content))

    # --------------------------------------------------------------------------
    # 5. Construct Profile Model
    # --------------------------------------------------------------------------
    profile = Profile(
        name=name,
        headline=headline,
        location=location,
        about=about,
        experience=experience,
        education=education,
        skills=skills,
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

    return ProfileDocument(
        meta=Meta(
            source_url=source_url,
            slug=slug,
            status=Status.OK,
            reason=None,
            missing_fields=missing_fields,
        ),
        profile=profile,
        other_sections=other_sections,
        raw_text=raw_text,
    )
