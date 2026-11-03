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

    Handles hostname normalization and strips non-standard port numbers.
    Accepts: 'linkedin.com', 'www.linkedin.com', 'in.linkedin.com', etc.
    Rejects: 'notlinkedin.com', 'phishing-linkedin.com'.
    """
    clean_host = netloc.lower().split(":")[0]  # strip port if present
    return clean_host == "linkedin.com" or clean_host.endswith(".linkedin.com")


def normalize_profile_url(raw_url: str) -> str | None:
    """Normalize a LinkedIn profile link to canonical format.

    Canonical format: https://www.linkedin.com/in/<slug>/

    Transformations performed:
    - Strips leading and trailing whitespace.
    - Prepends 'https://' if scheme is omitted by user.
    - Validates host belongs to LinkedIn.
    - Ensures path follows standard candidate profile format (/in/<slug>).
    - Percent-decodes URL-encoded slugs (e.g., %20 -> space or Unicode characters).
    - Strips query parameters (?miniProfileUrn=...) and URL hashes (#experience).

    Returns:
        The canonical URL string if valid, or None if invalid/unsupported.
    """
    # Defensive guard for None or blank strings
    if not raw_url:
        return None

    cleaned = raw_url.strip()
    if not cleaned:
        return None

    # Prepend scheme if missing so urlsplit parses network location correctly
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parts = urlsplit(cleaned)
    except Exception:
        # Invalid URI formatting
        return None

    # Reject non-LinkedIn domains (e.g. twitter.com, github.com)
    if not parts.netloc or not is_linkedin_host(parts.netloc):
        return None

    # Path must start with /in/
    path = parts.path
    if not path:
        return None

    # Split path segments into clean list: e.g. ['', 'in', 'slug'] -> ['in', 'slug']
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) != 2 or segments[0].lower() != "in":
        return None

    # Decode percent-encoded Unicode characters in the slug
    raw_slug = segments[1]
    decoded_slug = unquote(raw_slug).strip()
    if not decoded_slug:
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
