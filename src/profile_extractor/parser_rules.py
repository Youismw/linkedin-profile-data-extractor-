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

# ------------------------------------------------------------------------------
# Noise Filter Patterns
# Excludes sidebar widgets, recommendations, and notification badges
# ------------------------------------------------------------------------------

SKIP_SECTION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"^(more profiles for you|people you may know|"
        r"you might like|ad options|don't want to see this)",
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
    re.compile(r"^(show all|see credential|show credential|edit)\b", re.IGNORECASE),
]

# Stable section IDs used in LinkedIn DOM
KNOWN_SECTION_IDS: Final[dict[str, str]] = {
    "about": "about",
    "experience": "experience",
    "education": "education",
    "skills": "skills",
}

# Non-visible elements stripped from the DOM before text parsing
STRIP_TAGS: Final[list[str]] = [
    "script",
    "style",
    "noscript",
    "svg",
    "template",
]

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
