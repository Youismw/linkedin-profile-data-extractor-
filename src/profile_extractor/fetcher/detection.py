"""Outcome detection logic based on URL and page HTML inspection.

Module Architecture & Inter-Module Communication:
==================================================
This module serves as the centralized classifier for HTTP status codes, URL redirects,
and DOM response signatures. It categorizes every page navigation into standardized
Status (OK, UNREACHABLE) and Reason (BLOCKED, NOT_FOUND, NOT_VISIBLE, OK).

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.fetcher.playwright_fetcher`:
     * Calls `detect_outcome(status_code, final_url, html)` immediately after each
       page navigation to determine whether content is viewable or blocked.
   - Unit tests in `tests/test_detection.py`.

2. Downstream Dependencies:
   - `profile_extractor.models`:
     * Returns `(Status, Reason | None)` enum instances.
"""

import re

from profile_extractor.models import Reason, Status

# ==============================================================================
# Regex Signatures for Page State Detection
# ==============================================================================

# URL patterns indicating an authwall or login redirect (profile not visible)
LOGIN_WALL_URL_PATTERNS = [
    re.compile(r"/authwall", re.IGNORECASE),
    re.compile(r"/login", re.IGNORECASE),
    re.compile(r"/checkpoint/lg/", re.IGNORECASE),
    re.compile(r"/uas/login", re.IGNORECASE),
]

# DOM text patterns indicating the page requires signing in
LOGIN_WALL_CONTENT_PATTERNS = [
    re.compile(r"Sign in to view", re.IGNORECASE),
    re.compile(r"Join LinkedIn", re.IGNORECASE),
    re.compile(r"class=[\"'].*authwall.*[\"']", re.IGNORECASE),
    re.compile(r"id=[\"']authwall.*[\"']", re.IGNORECASE),
    re.compile(r"Please sign in", re.IGNORECASE),
]

# DOM patterns indicating non-existent, deleted, or merged profiles
NOT_FOUND_CONTENT_PATTERNS = [
    re.compile(r"Page not found", re.IGNORECASE),
    re.compile(r"This profile is not available", re.IGNORECASE),
    re.compile(r"This page doesn't exist", re.IGNORECASE),
    re.compile(r"This page does not exist", re.IGNORECASE),
    re.compile(r"An exact match for this profile wasn't found", re.IGNORECASE),
]

# URL patterns indicating anti-bot security checkpoints
BLOCKED_URL_PATTERNS = [
    re.compile(r"/checkpoint/challenge/", re.IGNORECASE),
    re.compile(r"/checkpoint/rp/", re.IGNORECASE),
    re.compile(r"/security-check", re.IGNORECASE),
]

# DOM patterns indicating Arkose puzzles, CAPTCHAs, or account rate restrictions
BLOCKED_CONTENT_PATTERNS = [
    re.compile(r"Security Verification", re.IGNORECASE),
    re.compile(r"Quick verification", re.IGNORECASE),
    re.compile(r"Let's do a quick security check", re.IGNORECASE),
    re.compile(r"We've detected unusual activity", re.IGNORECASE),
    re.compile(r"unusual traffic from your computer network", re.IGNORECASE),
    re.compile(r"account.*temporarily restricted", re.IGNORECASE),
    re.compile(r"Please solve this puzzle", re.IGNORECASE),
    re.compile(r"\bcaptcha\b", re.IGNORECASE),
]


def detect_outcome(
    current_url: str,
    html: str,
    status_code: int | None = None,
) -> tuple[Status, Reason | None, str | None]:
    """Inspect current URL, HTTP status code, and HTML body to detect page state.

    Evaluation Order:
    1. Check for HTTP status codes:
       - 429 / 999 -> BLOCKED
       - 404 -> NOT_FOUND
    2. Check for Security Challenges / Restrictions -> BLOCKED.
    3. Check for login / authwall redirection -> NOT_VISIBLE.
    4. Check for Profile Not Found text -> NOT_FOUND.
    5. Otherwise -> Status.OK.

    Returns:
        tuple of (Status, Reason, diagnostic_detail_string).
    """
    # 1. Check HTTP Status Codes
    if status_code is not None:
        if status_code in (429, 999):
            return (
                Status.UNREACHABLE,
                Reason.BLOCKED,
                f"Rate limited or request denied by LinkedIn (HTTP {status_code})",
            )
        if status_code == 401:
            return (
                Status.UNREACHABLE,
                Reason.NOT_VISIBLE,
                "Authentication required / session expired (HTTP 401)",
            )
        if status_code == 403:
            return (
                Status.UNREACHABLE,
                Reason.BLOCKED,
                "Forbidden / Access restricted by LinkedIn (HTTP 403)",
            )
        if status_code in (404, 410):
            return Status.UNREACHABLE, Reason.NOT_FOUND, f"Profile not found (HTTP {status_code})"
        if 500 <= status_code < 600:
            return (
                Status.UNREACHABLE,
                Reason.UNEXPECTED_ERROR,
                f"LinkedIn server error (HTTP {status_code})",
            )
        if 400 <= status_code < 500:
            return (
                Status.UNREACHABLE,
                Reason.UNEXPECTED_ERROR,
                f"Client error response (HTTP {status_code})",
            )

    # 2. Check for Security Challenges / Bot Blocks (URL redirects first)
    for pattern in BLOCKED_URL_PATTERNS:
        if pattern.search(current_url):
            return Status.UNREACHABLE, Reason.BLOCKED, f"Blocked URL detected: {current_url}"

    # 3. Check for Login Wall / Authwall (URL redirects first)
    for pattern in LOGIN_WALL_URL_PATTERNS:
        if pattern.search(current_url):
            return Status.UNREACHABLE, Reason.NOT_VISIBLE, f"Redirected to authwall: {current_url}"

    # 4. Check for Security Challenge / Blocked content
    for pattern in BLOCKED_CONTENT_PATTERNS:
        if pattern.search(html):
            match_str = pattern.pattern
            return (
                Status.UNREACHABLE,
                Reason.BLOCKED,
                f"Security challenge or restriction detected: {match_str}",
            )

    # 5. Check for Profile Not Found content
    for pattern in NOT_FOUND_CONTENT_PATTERNS:
        if pattern.search(html):
            return Status.UNREACHABLE, Reason.NOT_FOUND, "Profile not found page detected"

    # 6. Check for Authwall content
    for pattern in LOGIN_WALL_CONTENT_PATTERNS:
        if pattern.search(html):
            return Status.UNREACHABLE, Reason.NOT_VISIBLE, "Login wall content detected"

    # 6. Standard Visible Page
    return Status.OK, None, None
