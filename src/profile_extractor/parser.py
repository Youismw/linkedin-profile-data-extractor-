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
3. Text Hygiene & DOM Decontamination:
   - Decomposes dialog modals, video player settings, footer language lists, and navigation chrome.
   - Uses proper text separators to prevent word gluing across adjacent elements.
   - Anchors location reliably on 'Contact info' / middle-dot separators.
   - Correctly groups multi-role company containers (e.g. Google, Tata Technologies).
4. Sub-page details extraction:
   - Functions for parsing full skills, education, and experience details pages.
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
    DATE_RANGE_PATTERN,
    DURATION_PATTERN,
    EDUCATION_PATTERNS,
    EMPLOYMENT_TYPE_SUFFIX_PATTERN,
    EXPERIENCE_PATTERNS,
    KNOWN_SECTION_IDS,
    NOISE_LINE_PATTERNS,
    PRONOUN_PATTERN,
    SKILLS_PATTERNS,
    SKIP_SECTION_PATTERNS,
    STRIP_SELECTORS,
    STRIP_TAGS,
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
    1. Direct <h2>, <h3>, or <header> tags.
    2. 'aria-label' attribute on the section container.
    3. 'id' attribute on the section container.
    """
    # 1. Check for standard heading tags inside section
    for heading_tag in section.find_all(["h2", "h3", "header"], recursive=True):
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


def _get_section_items(sec: Tag) -> list[Tag]:
    """Extract individual item tags from a profile section.

    Inspection cascade:
    1. Modern LinkedIn LazyColumn component (<div data-component-type="LazyColumn">).
       Grabs top-level entity collection items.
    2. Direct [role="listitem"] elements.
    3. Direct <li> elements (ul > li or ol > li).
    """
    # Strategy 1: Modern LinkedIn LazyColumn component
    lazy_col = sec.select_one('[data-component-type="LazyColumn"]')
    if lazy_col:
        items = [
            el
            for el in lazy_col.select("div[componentkey]")
            if "entity-collection-item" in str(el.get("componentkey", ""))
        ]
        if items:
            return items
        role_items = [el for el in lazy_col.select('[role="listitem"]') if isinstance(el, Tag)]
        if role_items:
            return role_items
        direct_children = [c for c in lazy_col.children if isinstance(c, Tag)]
        if direct_children:
            return direct_children

    # Strategy 2: Direct ARIA listitems within section
    role_items = [el for el in sec.select('[role="listitem"]') if isinstance(el, Tag)]
    if role_items:
        return role_items

    # Strategy 3: Direct <li> elements
    direct_lis = [li for li in sec.find_all("li", recursive=False) if isinstance(li, Tag)]
    if direct_lis:
        return direct_lis
    ul = sec.find(["ul", "ol"])
    if ul and isinstance(ul, Tag):
        nested_lis = [c for c in ul.children if isinstance(c, Tag) and c.name == "li"]
        if nested_lis:
            return nested_lis

    return []


def _parse_experience_items(tag: Tag) -> list[ExperienceItem]:
    """Extract structured fields from an experience item tag.

    Handles:
    - Multi-role company containers (e.g. Google with CEO and Product Management sub-roles).
    - Single-role entries (Title, Company, Dates, Location, Description).
    - Field inversion protection (long text routed to description instead of title).
    """
    nested_lis = tag.find_all("li")
    # Check if this item is a multi-role company
    if nested_lis:
        top_lines = _clean_role_lines(tag)
        parent_company = _clean_company_name(top_lines[0] if top_lines else "Unknown")

        results: list[ExperienceItem] = []
        for n_li in nested_lis:
            li_lines = _clean_role_lines(n_li)
            if not li_lines:
                continue
            sub_title = li_lines[0] if li_lines else None
            sub_dates: str | None = None
            sub_loc: str | None = None
            desc_parts: list[str] = []

            for line in li_lines[1:]:
                if not sub_dates and (
                    DATE_RANGE_PATTERN.search(line) or DURATION_PATTERN.search(line)
                ):
                    sub_dates = line
                elif not sub_loc and any(
                    kw in line.lower()
                    for kw in [
                        "on-site",
                        "hybrid",
                        "remote",
                        "india",
                        "united states",
                        "california",
                    ]
                ):
                    sub_loc = line
                elif not any(p.search(line) for p in NOISE_LINE_PATTERNS) and line not in [
                    "…",
                    "...",
                    "more",
                    "see more",
                ]:
                    desc_parts.append(line)

            raw_text = " | ".join(li_lines)
            desc_str = " ".join(desc_parts) if desc_parts else None

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
                    raw=raw_text,
                )
            )
        if results:
            return results

    # Standard single-role layout
    lines = _clean_role_lines(tag)
    if not lines:
        return []

    title: str | None = lines[0]
    company: str | None = _clean_company_name(lines[1]) if len(lines) > 1 else None
    dates: str | None = lines[2] if len(lines) > 2 else None
    location: str | None = lines[3] if len(lines) > 3 else None
    description: str | None = " ".join(lines[4:]) if len(lines) > 4 else None
    raw_text = " | ".join(lines)

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
            raw=raw_text,
        )
    ]


def _parse_education_item(tag: Tag) -> EducationItem:
    """Extract structured fields and complete raw text from an education entry.

    Extracts: School / Institution, Degree / Major, Dates / Honors, and full raw string.
    """
    raw_lines = [
        _clean_text(line)
        for line in tag.stripped_strings
        if _clean_text(line) and not _matches_any_pattern(line, NOISE_LINE_PATTERNS)
    ]
    lines = _dedupe_consecutive(raw_lines)
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


def _parse_other_section(sec: Tag, heading: str) -> OtherSection | None:
    """Format an unmapped section cleanly into an OtherSection object."""
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


def parse_skills_details(html: str) -> list[str]:
    """Parse all skills from a LinkedIn /details/skills/ page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "nav, footer, .global-nav, .global-footer, [role='dialog'], .artdeco-modal"
    ):
        tag.decompose()

    main = soup.find("main") or soup
    items = main.select('div[componentkey^="entity-collection-item"], [role="listitem"], li')
    skills: list[str] = []
    for it in items:
        lines = [_clean_text(s) for s in it.stripped_strings if _clean_text(s)]
        if not lines:
            continue
        cand = lines[0]
        if cand in ["Skills", "Show all", "Endorsements", "More profiles for you"]:
            continue
        if re.search(r"^\d+\s+endorsements?$", cand, re.IGNORECASE):
            continue
        if cand not in skills:
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
    lazy = main.select_one('[data-component-type="LazyColumn"]')
    items = (
        lazy.select('div[componentkey^="entity-collection-item"]')
        if lazy
        else main.select('[role="listitem"], li')
    )
    results: list[EducationItem] = []
    for it in items:
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
    lazy = main.select_one('[data-component-type="LazyColumn"]')
    items = (
        lazy.select('div[componentkey^="entity-collection-item"]')
        if lazy
        else main.select('[role="listitem"], li')
    )
    results: list[ExperienceItem] = []
    for it in items:
        exps = _parse_experience_items(it)
        results.extend(exps)
    return results


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

    # Filter out outer layout containers, nested carousels, and dialogs
    valid_sections: list[Tag] = []
    for sec in main.find_all("section"):
        if sec.get("aria-label") == "Primary content":
            continue
        if sec.get("aria-roledescription") == "carousel" or sec.get("data-testid") == "carousel":
            continue
        sec_text = sec.get_text(separator=" ", strip=True)
        if re.search(r"^\d+\s+notification", sec_text, re.IGNORECASE):
            continue
        valid_sections.append(sec)

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
                    if prev in ["·", "\u00b7", "-", "—"]:
                        continue
                    location = prev
                    break

            # Location Strategy B: Fallback for synthetic/mock fixtures lacking 'contact info'
            if not location:
                for cand in filtered[name_idx + 1 : name_idx + 7]:
                    if cand == headline:
                        continue
                    geo_match = re.search(
                        r"\b(area|united states|india|germany|uk|canada|california"
                        r"|delhi|london|greater|san francisco|munich|bavaria)\b",
                        cand,
                        re.IGNORECASE,
                    )
                    comma_geo = "," in cand and not re.search(
                        r"\b(engineer|developer|manager|lead|director|student|founder"
                        r"|intern|designer|consultant|analyst|specialist)\b",
                        cand,
                        re.IGNORECASE,
                    )
                    if geo_match or comma_geo:
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
                    if not re.search(
                        r"^\d+\s+endorsements?$", skill_name, re.IGNORECASE
                    ) and not re.search(r"^show all\b", skill_name, re.IGNORECASE):
                        skill_list.append(skill_name)
            if skill_list:
                skills = skill_list
            continue

        # 4e. Route any other visible section into other_sections
        parsed_other = _parse_other_section(sec, heading or "Additional Information")
        if parsed_other:
            other_sections.append(parsed_other)

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

    has_profile_signals = bool(
        name
        or headline
        or location
        or about
        or experience
        or education
        or skills
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
