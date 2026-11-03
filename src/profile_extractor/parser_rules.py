"""Centralized selectors, heading signatures, and DOM matching rules for parsing.

All layout-specific patterns and heading variations are maintained strictly in this file,
ensuring layout changes are a single-file modification without generated CSS classes.
"""

import re
from typing import Final

# Heading keywords (case-insensitive regex patterns matching word boundaries or variations)
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

# Sidebar recommendations, ad popups, and non-profile widget patterns to exclude
SKIP_SECTION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"^(more profiles for you|people you may know|"
        r"you might like|ad options|don't want to see this)",
        re.IGNORECASE,
    ),
    re.compile(r"^\d+\s+notification", re.IGNORECASE),
]

# Pronoun patterns in top card
PRONOUN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[\(\[]?(?:he/him|she/her|they/them|he/they|she/they)[\)\]]?$",
    re.IGNORECASE,
)

# Common UI buttons and noise lines within items
NOISE_LINE_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"^(show all|see credential|show credential|edit)\b", re.IGNORECASE),
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

