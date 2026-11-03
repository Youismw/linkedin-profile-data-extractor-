"""URL normalization and slug extraction/sanitization for LinkedIn profiles.

This module provides defensive parsing for messy real-world LinkedIn URLs:
1. Normalizes varied schemes (http, https, missing schemes), country subdomains
   (e.g., in.linkedin.com, uk.linkedin.com), query strings (?trk=...), and
   anchors (#...) into a clean canonical format: https://www.linkedin.com/in/<slug>/
2. Extracts and percent-decodes candidate slugs.
3. Sanitizes slugs to ensure safe, cross-platform filesystem filenames.
"""

import hashlib
import re
from urllib.parse import unquote, urlsplit

from profile_extractor.logging_setup import get_logger

# Allowed characters in slug filenames across Windows, macOS, and Linux:
# letters, digits, dot, underscore, hyphen
ALLOWED_SLUG_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
CANONICAL_DOMAIN = "www.linkedin.com"


def is_linkedin_host(netloc: str) -> bool:
    """Check whether a host belongs to LinkedIn (including country subdomains).

    Handles hostname normalization and rejects credentials or external hosts.
    Accepts: 'linkedin.com', 'www.linkedin.com', 'in.linkedin.com', etc.
    Rejects: 'evil.com', 'linkedin.com:443@evil.com', 'notlinkedin.com'.
    """
    if not netloc:
        return False
    try:
        parts = urlsplit(f"//{netloc}")
    except Exception:
        return False

    if parts.username or parts.password:
        return False

    try:
        if parts.port is not None and parts.port not in (80, 443):
            return False
    except ValueError:
        return False

    hostname = parts.hostname
    if not hostname:
        return False
    clean = hostname.lower()
    return clean == "linkedin.com" or clean.endswith(".linkedin.com")


def normalize_profile_url(raw_url: str) -> str | None:
    """Normalize a LinkedIn profile link to canonical format.

    Canonical format: https://www.linkedin.com/in/<slug>/

    Transformations performed:
    - Strips leading and trailing whitespace.
    - Prepends 'https://' if scheme is omitted by user.
    - Validates host belongs to LinkedIn without embedded credentials or non-standard ports.
    - Ensures path follows standard candidate profile format (/in/<slug>).
    - Percent-decodes valid Unicode slug characters while rejecting URL delimiters.
    - Strips query parameters (?miniProfileUrn=...) and URL hashes (#experience).

    Returns:
        The canonical URL string if valid, or None if invalid/unsupported.
    """
    # Defensive guard for None or blank strings
    if not raw_url:
        return None

    cleaned = raw_url.strip()
    # Strip common copy-paste wrappers (quotes, angle brackets, markdown wrappers, punctuation)
    cleaned = cleaned.strip(" \t\r\n<>\"'`").rstrip(",;.").strip()
    if not cleaned:
        return None

    # Case-insensitive scheme check to prevent 'https://HTTPS://...' duplication
    if not cleaned.lower().startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parts = urlsplit(cleaned)
    except Exception:
        # Invalid URI formatting
        return None

    # Disallow embedded username/password in profile URLs (e.g. https://linkedin.com@evil.com)
    if parts.username or parts.password:
        return None

    # Validate port: only standard 80, 443, or omitted allowed
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and port not in (80, 443):
        return None

    # Validate hostname belongs genuinely to LinkedIn
    hostname = parts.hostname
    if not hostname:
        return None
    clean_host = hostname.lower()
    if not (clean_host == "linkedin.com" or clean_host.endswith(".linkedin.com")):
        return None

    # Path must start with /in/
    path = parts.path
    if not path:
        return None

    # Split path segments into clean list: e.g. ['', 'in', 'slug'] -> ['in', 'slug']
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None

    # For sub-page URLs (e.g. /in/slug/details/experience/), only allow
    # standard LinkedIn sub-sections
    if len(segments) > 2 and segments[2].lower() not in ("details", "overlay", "recent-activity"):
        return None

    raw_slug = segments[1]
    # Reject raw path/query/fragment delimiters inside the slug
    if any(c in raw_slug for c in ("?", "#", "/", "\\", "@")):
        return None

    # Decode percent-encoded characters (e.g. Unicode names or spaces)
    decoded_slug = unquote(raw_slug).strip()
    if not decoded_slug:
        return None

    # Reject reserved URL delimiters that would alter URL structure or mutate identifiers
    # (e.g. %2F for '/', %3F for '?', %23 for '#')
    if any(c in decoded_slug for c in ("?", "#", "/", "\\", "@", ":", "\x00", "\r", "\n")):
        return None

    # Return standard canonical format
    return f"https://{CANONICAL_DOMAIN}/in/{decoded_slug}/"


def extract_slug(canonical_url: str) -> str:
    """Extract the decoded slug identifier from a canonical LinkedIn URL.

    Example:
        'https://www.linkedin.com/in/rohit-chauhan/' -> 'rohit-chauhan'

    Raises:
        ValueError: If URL does not conform to the /in/<slug> structure.
    """
    parts = urlsplit(canonical_url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 2 and segments[0].lower() == "in":
        return unquote(segments[1])
    raise ValueError(f"URL is not a valid canonical LinkedIn profile URL: {canonical_url}")


def sanitize_slug(slug: str, fallback_seed: str = "") -> str:
    """Sanitize slug for safe use as a file name across operating systems.

    Replaces illegal filesystem characters (slashes, colons, spaces, quotes)
    with safe hyphens. If a slug contains only special characters and collapses
    to empty, generates a deterministic 8-character hex hash from a seed string.
    """
    # Replace any non-alphanumeric character (except . _ -) with hyphen
    sanitized = ALLOWED_SLUG_CHARS_PATTERN.sub("-", slug)
    # Collapse multiple consecutive hyphens into one and trim outer punctuation
    sanitized = re.sub(r"-+", "-", sanitized).strip("-._")

    # If completely empty after stripping (e.g. slug was "???"), create fallback hash
    if not sanitized:
        logger = get_logger()
        seed = fallback_seed or slug or "unknown"
        hashed = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
        logger.warning(
            "Slug '%s' sanitized to empty string; using fallback hash '%s'", slug, hashed
        )
        return f"profile-{hashed}"

    return sanitized
