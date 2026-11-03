"""Centralized selectors, heading signatures, and DOM matching rules for parsing.

All layout-specific patterns and heading variations are maintained strictly in this file,
ensuring layout changes are a single-file modification without generated CSS classes.
"""

import re
from typing import Final

# Heading keywords (case-insensitive regex patterns)
ABOUT_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^about$", re.IGNORECASE),
    re.compile(r"^summary$", re.IGNORECASE),
]

EXPERIENCE_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^experience$", re.IGNORECASE),
    re.compile(r"^work experience$", re.IGNORECASE),
]

EDUCATION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^education$", re.IGNORECASE),
]

SKILLS_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^skills$", re.IGNORECASE),
    re.compile(r"^skills & endorsements$", re.IGNORECASE),
]

# Section IDs or data attributes that LinkedIn uses stably
KNOWN_SECTION_IDS: Final[dict[str, str]] = {
    "about": "about",
    "experience": "experience",
    "education": "education",
    "skills": "skills",
}

# Elements to strip before text extraction (scripts, styles, SVGs, accessibility hidden text)
STRIP_TAGS: Final[list[str]] = [
    "script",
    "style",
    "noscript",
    "svg",
    "template",
]

# Attribute selectors for name and headline fallback
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

