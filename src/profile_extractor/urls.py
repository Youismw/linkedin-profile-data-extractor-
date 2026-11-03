"""URL normalization and slug extraction/sanitization for LinkedIn profiles."""

import hashlib
import re
from urllib.parse import unquote, urlsplit

from profile_extractor.logging_setup import get_logger

# Allowed characters in slug filenames: letters, digits, dot, underscore, hyphen
ALLOWED_SLUG_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
CANONICAL_DOMAIN = "www.linkedin.com"


def is_linkedin_host(netloc: str) -> bool:
    """Check whether a host belongs to LinkedIn (including country subdomains)."""
    clean_host = netloc.lower().split(":")[0]  # strip port if present
    return clean_host == "linkedin.com" or clean_host.endswith(".linkedin.com")


def normalize_profile_url(raw_url: str) -> str | None:
    """Normalize a LinkedIn profile link to canonical format.

    Canonical format: https://www.linkedin.com/in/<slug>/

    Accepts links with or without scheme, www, or country subdomains.
    Strips queries, fragments, and trailing slashes.
    Percent-decodes the slug.
    Accepts only /in/<slug> paths.

    Returns the canonical URL string if valid, or None if invalid.
    """
    if not raw_url:
        return None

    cleaned = raw_url.strip()
    if not cleaned:
        return None

    # Prepend scheme if missing so urlsplit parses host correctly
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parts = urlsplit(cleaned)
    except Exception:
        return None

    if not parts.netloc or not is_linkedin_host(parts.netloc):
        return None

    # Path must start with /in/
    path = parts.path
    if not path:
        return None

    # Split path segments: e.g. ['', 'in', 'slug']
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) != 2 or segments[0].lower() != "in":
        return None

    raw_slug = segments[1]
    decoded_slug = unquote(raw_slug).strip()
    if not decoded_slug:
        return None

    return f"https://{CANONICAL_DOMAIN}/in/{decoded_slug}/"


def extract_slug(canonical_url: str) -> str:
    """Extract the decoded slug from a canonical LinkedIn URL."""
    parts = urlsplit(canonical_url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 2 and segments[0].lower() == "in":
        return unquote(segments[1])
    raise ValueError(f"URL is not a valid canonical LinkedIn profile URL: {canonical_url}")


def sanitize_slug(slug: str, fallback_seed: str = "") -> str:
    """Sanitize slug for safe use as a file name.

    Allowed characters: letters, digits, dot (.), underscore (_), hyphen (-).
    If nothing usable remains, generates a deterministic 8-character hex hash.
    """
    sanitized = ALLOWED_SLUG_CHARS_PATTERN.sub("-", slug)
    # Collapse multiple hyphens
    sanitized = re.sub(r"-+", "-", sanitized).strip("-._")

    if not sanitized:
        logger = get_logger()
        seed = fallback_seed or slug or "unknown"
        hashed = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
        logger.warning(
            "Slug '%s' sanitized to empty string; using fallback hash '%s'", slug, hashed
        )
        return f"profile-{hashed}"

    return sanitized
