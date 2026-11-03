"""DOM parsing and structured profile extraction using BeautifulSoup."""

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
    SKILLS_PATTERNS,
    STRIP_TAGS,
)


def _clean_text(text: str | None) -> str:
    """Normalize whitespace in extracted string."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _matches_any_pattern(text: str, patterns: list[re.Pattern[str]]) -> bool:
    """Check if normalized text matches any of the compiled regex patterns."""
    clean = _clean_text(text)
    return any(p.search(clean) for p in patterns)


def _extract_section_heading(section: Tag) -> str:
    """Find and return the heading text of a section."""
    # Check for h2, h3, header, or aria-label
    for heading_tag in section.find_all(["h2", "h3", "header"], recursive=True):
        txt = _clean_text(heading_tag.get_text())
        if txt:
            return txt
    # Check aria-label or id attribute
    aria = section.get("aria-label")
    if aria and isinstance(aria, str):
        return _clean_text(aria)
    sec_id = section.get("id")
    if sec_id and isinstance(sec_id, str):
        return _clean_text(sec_id)
    return ""


def _parse_experience_item(li: Tag) -> ExperienceItem:
    """Extract structured fields and complete raw text from an experience list item."""
    raw_text = _clean_text(li.get_text(separator=" | "))
    lines = [_clean_text(line) for line in li.stripped_strings if _clean_text(line)]

    title: str | None = None
    company: str | None = None
    dates: str | None = None
    location: str | None = None
    description: str | None = None

    if lines:
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


def _parse_education_item(li: Tag) -> EducationItem:
    """Extract structured fields and complete raw text from an education list item."""
    raw_text = _clean_text(li.get_text(separator=" | "))
    lines = [_clean_text(line) for line in li.stripped_strings if _clean_text(line)]

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

    Handles Status.UNREACHABLE, extracts structured profile fields and raw text copies,
    routes unmapped content into other_sections, calculates missing_fields, and verifies
    text coverage against the raw page body.
    """
    logger = get_logger()

    # 1. Handle Unreachable Status directly
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
        )

    soup = BeautifulSoup(html, "lxml")

    # Strip noisy elements (scripts, styles, templates)
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.find("body") or soup
    total_visible_text = _clean_text(body.get_text())

    # 2. Top Card: Name, Headline, Location
    name: str | None = None
    headline: str | None = None
    location: str | None = None

    main = soup.find("main") or soup
    top_card = None

    # Strategy A: Check <h1> tags
    h1 = soup.find("h1")
    if h1 and _clean_text(h1.get_text()):
        name = _clean_text(h1.get_text())
        top_card = h1.find_parent("section")

    # Strategy B: First section in <main> (LinkedIn logged-in profile top card)
    main_sections = main.find_all("section")
    if not top_card and main_sections:
        top_card = main_sections[0]

    if top_card:
        top_lines = [_clean_text(s) for s in top_card.stripped_strings if _clean_text(s)]
        # Filter out UI noise badges like "0 notifications" or "Ad Options"
        filtered = [
            line
            for line in top_lines
            if not re.search(r"^\d+\s+notification", line, re.I)
            and not re.search(r"^ad\s+options", line, re.I)
        ]

        if not name and filtered:
            name = filtered[0]

        if name and filtered:
            try:
                name_idx = filtered.index(name)
                if name_idx + 1 < len(filtered):
                    headline = filtered[name_idx + 1]
                for cand in filtered[name_idx + 2 : name_idx + 6]:
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
                        ]
                    ):
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
        )

    # 3. Section Extraction
    about: str | None = None
    experience: list[ExperienceItem] | None = None
    education: list[EducationItem] | None = None
    skills: list[str] | None = None
    other_sections: list[OtherSection] = []

    sections = soup.find_all("section")
    seen_sections: set[int] = set()

    for sec in sections:
        if id(sec) in seen_sections:
            continue
        seen_sections.add(id(sec))

        # Skip top card if it contains h1
        if top_card and sec == top_card:
            continue

        heading = _extract_section_heading(sec)
        sec_id = str(sec.get("id") or "").lower()

        # Check About
        if _matches_any_pattern(heading, ABOUT_PATTERNS) or sec_id == KNOWN_SECTION_IDS["about"]:
            body_text = _clean_text(sec.get_text())
            # Strip heading if present at start
            if heading and body_text.startswith(heading):
                body_text = _clean_text(body_text[len(heading) :])
            if body_text:
                about = body_text
            continue

        # Check Experience
        if (
            _matches_any_pattern(heading, EXPERIENCE_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["experience"]
        ):
            items = []
            for li in sec.find_all("li", recursive=True):
                item = _parse_experience_item(li)
                if item.raw:
                    items.append(item)
            if items:
                experience = items
            continue

        # Check Education
        if (
            _matches_any_pattern(heading, EDUCATION_PATTERNS)
            or sec_id == KNOWN_SECTION_IDS["education"]
        ):
            items = []
            for li in sec.find_all("li", recursive=True):
                item = _parse_education_item(li)
                if item.raw:
                    items.append(item)
            if items:
                education = items
            continue

        # Check Skills
        if _matches_any_pattern(heading, SKILLS_PATTERNS) or sec_id == KNOWN_SECTION_IDS["skills"]:
            skill_list: list[str] = []
            for li in sec.find_all("li", recursive=True):
                txt = _clean_text(li.get_text())
                if txt:
                    skill_list.append(txt)
            if skill_list:
                skills = skill_list
            continue

        # Route any other visible section into other_sections
        sec_content = _clean_text(sec.get_text())
        if sec_content:
            if heading and sec_content.startswith(heading):
                sec_content = _clean_text(sec_content[len(heading) :])
            if sec_content:
                display_heading = heading or "Additional Information"
                other_sections.append(OtherSection(heading=display_heading, text=sec_content))

    # 4. Construct Profile Model
    profile = Profile(
        name=name,
        headline=headline,
        location=location,
        about=about,
        experience=experience,
        education=education,
        skills=skills,
    )

    # 5. Determine Missing Fields from Important Fields
    missing_fields: list[str] = []
    profile_data = profile.model_dump(exclude_none=True)
    for field in important_fields:
        val = profile_data.get(field)
        if val is None or (isinstance(val, list) and len(val) == 0):
            missing_fields.append(field)

    # 6. Text Coverage Check
    # Compare visible text captured in output with visible text on the page
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
    )
