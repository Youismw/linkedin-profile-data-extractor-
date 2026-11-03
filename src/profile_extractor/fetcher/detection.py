"""Outcome detection logic based on URL and page HTML inspection."""

import re

from profile_extractor.models import Reason, Status

# Patterns indicating an auth / login wall (profile not visible)
LOGIN_WALL_URL_PATTERNS = [
    re.compile(r"/authwall", re.IGNORECASE),
    re.compile(r"/login", re.IGNORECASE),
    re.compile(r"/checkpoint/lg/", re.IGNORECASE),
    re.compile(r"/uas/login", re.IGNORECASE),
]

LOGIN_WALL_CONTENT_PATTERNS = [
    re.compile(r"Sign in to view", re.IGNORECASE),
    re.compile(r"Join LinkedIn", re.IGNORECASE),
    re.compile(r"class=[\"'].*authwall.*[\"']", re.IGNORECASE),
    re.compile(r"id=[\"']authwall.*[\"']", re.IGNORECASE),
    re.compile(r"Please sign in", re.IGNORECASE),
]

# Patterns indicating profile not found / deleted / nonexistent
NOT_FOUND_CONTENT_PATTERNS = [
    re.compile(r"Page not found", re.IGNORECASE),
    re.compile(r"This profile is not available", re.IGNORECASE),
    re.compile(r"This page doesn't exist", re.IGNORECASE),
    re.compile(r"This page does not exist", re.IGNORECASE),
    re.compile(r"An exact match for this profile wasn't found", re.IGNORECASE),
]

# Patterns indicating security challenge, CAPTCHA, or account restriction
BLOCKED_URL_PATTERNS = [
    re.compile(r"/checkpoint/challenge/", re.IGNORECASE),
    re.compile(r"/checkpoint/rp/", re.IGNORECASE),
    re.compile(r"/security-check", re.IGNORECASE),
]

BLOCKED_CONTENT_PATTERNS = [
    re.compile(r"Security Verification", re.IGNORECASE),
    re.compile(r"Quick verification", re.IGNORECASE),
    re.compile(r"Let's do a quick security check", re.IGNORECASE),
    re.compile(r"We've detected unusual activity", re.IGNORECASE),
    re.compile(r"unusual traffic from your computer network", re.IGNORECASE),
    re.compile(r"account.*temporarily restricted", re.IGNORECASE),
    re.compile(r"Please solve this puzzle", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
]


def detect_outcome(
    current_url: str,
    html: str,
    status_code: int | None = None,
) -> tuple[Status, Reason | None, str | None]:
    """Inspect current URL, HTTP status, and HTML body to detect page state.

    Returns (Status, Reason, Detail).
    All detection rules and signatures are maintained solely in this module.
    """
    # 1. Check for Security Challenges / Restrictions (Blocked)
    for pattern in BLOCKED_URL_PATTERNS:
        if pattern.search(current_url):
            return Status.UNREACHABLE, Reason.BLOCKED, f"Blocked URL detected: {current_url}"

    for pattern in BLOCKED_CONTENT_PATTERNS:
        if pattern.search(html):
            match_str = pattern.pattern
            return (
                Status.UNREACHABLE,
                Reason.BLOCKED,
                f"Security challenge or restriction detected: {match_str}",
            )

    # 2. Check for Not Found
    if status_code == 404:
        return Status.UNREACHABLE, Reason.NOT_FOUND, "HTTP status 404 Not Found"

    for pattern in NOT_FOUND_CONTENT_PATTERNS:
        if pattern.search(html):
            return Status.UNREACHABLE, Reason.NOT_FOUND, "Profile not found page detected"

    # 3. Check for Login Wall / Authwall (Not Visible)
    for pattern in LOGIN_WALL_URL_PATTERNS:
        if pattern.search(current_url):
            return Status.UNREACHABLE, Reason.NOT_VISIBLE, f"Redirected to authwall: {current_url}"

    for pattern in LOGIN_WALL_CONTENT_PATTERNS:
        if pattern.search(html):
            return Status.UNREACHABLE, Reason.NOT_VISIBLE, "Login wall content detected"

    # 4. Standard Visible Page
    return Status.OK, None, None
