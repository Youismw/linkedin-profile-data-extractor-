"""Centralized selectors, heading signatures, and DOM matching rules for parsing.

Design Philosophy:
- Zero Obfuscated CSS Classes: LinkedIn frequently regenerates hashed CSS
  classnames (e.g., `artdeco-button--tertiary-7x9z`). Relying on them creates
  fragile parsers that break weekly.
- Semantic HTML & ARIA Attributes: This module targets stable structural tokens:
  HTML tags (`h1`, `h2`, `section`), ARIA landmarks (`role="listitem"`),
  component keys (`LazyColumn`), and human-visible section headings.
- Single-File Maintenance: If LinkedIn introduces a new heading variation (e.g.
  'Career History' instead of 'Experience'), updating the regex list here adapts
  the entire pipeline without modifying parsing logic.
"""

import re
from typing import Final

# ------------------------------------------------------------------------------
# Core Section Heading Regex Patterns
# Uses \b word boundaries so "Skills (47)" matches without matching unrelated text
# ------------------------------------------------------------------------------

ABOUT_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^about\b", re.IGNORECASE),
    re.compile(r"^summary\b", re.IGNORECASE),
]

EXPERIENCE_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^experience\b", re.IGNORECASE),
    re.compile(r"^work experience\b", re.IGNORECASE),
]

EDUCATION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^education\b", re.IGNORECASE),
]

SKILLS_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^skills?\b", re.IGNORECASE),
    re.compile(r"^skills\s*&", re.IGNORECASE),
    re.compile(r"^featured skills\b", re.IGNORECASE),
]

CERTIFICATIONS_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^licenses?\s*(?:&|and)?\s*certifications?\b", re.IGNORECASE),
    re.compile(r"^certifications?\b", re.IGNORECASE),
]

PROJECTS_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^projects?\b", re.IGNORECASE),
]

LANGUAGES_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^languages?\b", re.IGNORECASE),
]

# ------------------------------------------------------------------------------
# Noise Filter Patterns
# Excludes sidebar widgets, recommendations, and notification badges
# ------------------------------------------------------------------------------

SKIP_SECTION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"^(more profiles for you|people you may know|people also viewed|"
        r"pages for you|you might like|ad options|don't want to see this)",
        re.IGNORECASE,
    ),
    re.compile(r"^\d+\s+notification", re.IGNORECASE),
]

# Top card pronoun badges (e.g., '(He/Him)') filtered so they aren't taken as headline
PRONOUN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[\(\[]?(?:he/him|she/her|they/them|he/they|she/they)[\)\]]?$",
    re.IGNORECASE,
)

# Interactive button labels filtered from experience and education raw text
NOISE_LINE_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"^(show all|see credential|show credential|edit|follow|connect|message)\b", re.IGNORECASE
    ),
    re.compile(r"^\d+\s+(?:endorsements?|followers?|connections?)$", re.IGNORECASE),
    re.compile(r"^request\s+services\b", re.IGNORECASE),
]

# UI badges and metadata that must never be treated as descriptions
UI_BADGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:LinkedIn helped me get this job|^helped me get this job$|Show credential|See credential"
    r"|Promoted|Verified certificate)",
    re.IGNORECASE,
)

# Skills chip in experience entries (e.g., "Node.js, MongoDB and +2 skills")
SKILLS_CHIP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:\band\s+\+\d+\s+skills?\b|^\s*Skills\s*:|^\s*[\u00b7·]\s*Skills\s*:|\+\d+\s+skills?$)",
    re.IGNORECASE,
)

# Academic grade / honors pattern in education (e.g., "CGPA: 7.18", "Grade: First Class")
GRADE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:CGPA|GPA|Grade|Percentage|Score)\b[:\s]*[\d.]+",
    re.IGNORECASE,
)

# Extracurricular activities pattern in education
ACTIVITIES_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*Activities and societies\s*:",
    re.IGNORECASE,
)

# Workplace arrangement types (e.g. "On-site", "Hybrid", "Remote")
WORKPLACE_TYPES: Final[re.Pattern[str]] = re.compile(
    r"\b(?:on-site|hybrid|remote)\b",
    re.IGNORECASE,
)

# Geographic location line pattern (e.g. "Munich, Bavaria, Germany", "Tokyo, Japan · Hybrid")
LOCATION_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:\w[\w\s\.\-']+,)+\s*[\w\s\.\-']+"
    r"|[A-Z][a-zA-Z\s\.\-']+(?:Area|Region|City|District|County|State)"
    r"|(?:on-site|hybrid|remote))"
    r"(?:\s*[\u00b7·]\s*(?:on-site|hybrid|remote))?$",
    re.IGNORECASE,
)

# Placeholder values that indicate absent or default content rather than usable text
PLACEHOLDER_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^[\s\-_—–\.\?\!/\\\|\*#]+$"
    r"|^(?:n/a|na|none|null|nil|undefined|unknown|not provided"
    r"|not specified|\(empty\)|no headline)$",
    re.IGNORECASE,
)

# Stable section IDs used in LinkedIn DOM
KNOWN_SECTION_IDS: Final[dict[str, str]] = {
    "about": "about",
    "experience": "experience",
    "education": "education",
    "skills": "skills",
    "certifications": "licenses_and_certifications",
    "projects": "projects",
    "languages": "languages",
}

# Non-visible and UI junk elements stripped from the DOM before text parsing
STRIP_TAGS: Final[list[str]] = [
    "script",
    "style",
    "noscript",
    "svg",
    "template",
    "aside",
    "footer",
    "nav",
    "dialog",
    "video",
    "audio",
]

# Structural noise selectors stripped from the DOM to eliminate video player modals,
# footer language lists, and navigation chrome
STRIP_SELECTORS: Final[list[str]] = [
    '[role="dialog"]',
    ".artdeco-modal",
    ".vjs-modal-dialog",
    ".video-js",
    "footer",
    "nav",
    ".global-nav",
    ".global-footer",
]

# Employment type suffixes (e.g. ' · Full-time', ' · Internship') to strip from company names
EMPLOYMENT_TYPE_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s*[\u00b7·]\s*(?:Full-time|Part-time|Internship|Contract|Self-employed|Freelance|Seasonal)\b.*$",
    re.IGNORECASE,
)

# Date range pattern matching employment timeframes (e.g. 'Oct 2026 - Present · 2 mos')
DATE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b.*\b(?:Present|\d{4})\b",
    re.IGNORECASE,
)

# Duration pattern matching tenure lengths (e.g. '22 yrs 8 mos', '5 mos', '1 yr 4 mos')
DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:\d+\s+(?:yr|yrs|mo|mos|year|years|month|months))\b",
    re.IGNORECASE,
)

# Structural selectors for Top Card fallback
NAME_TAGS: Final[list[str]] = ["h1"]
HEADLINE_SELECTORS: Final[list[str]] = [
    ".text-body-medium",
    "[data-field='headline']",
    "div.headline",
]
LOCATION_SELECTORS: Final[list[str]] = [
    ".text-body-small.inline",
    "[data-field='location']",
    "span.location",
]
