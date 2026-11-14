"""Interactive login helper for consenting employee to save authenticated session.

Module Architecture & Inter-Module Communication:
==================================================
This module manages human-in-the-loop authentication for LinkedIn. It launches
a visible browser window for the employee to sign in, complete any 2FA or CAPTCHA,
and saves the authenticated storage state (`.auth/state.json`) for subsequent headless runs.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.cli`:
     * Calls `is_session_expired(auth_state_path)` during pre-flight health checks.
     * Calls `run_interactive_login(auth_state_path)` when the user runs `.\\run login`
       or when an active session has expired.
   - `profile_extractor.gui`:
     * Calls `run_interactive_login()` via subprocess when "LinkedIn Login" button is clicked.
   - `profile_extractor.fetcher.playwright_fetcher`:
     * Calls `run_interactive_login()` as an in-flight recovery mechanism if an authwall
       is encountered mid-scrape.

2. Downstream Dependencies:
   - Playwright: Launches Chromium in headed mode (`headless=False`) with human-like viewport.
   - Filesystem: Saves cookie and localStorage JSON snapshot to `.auth/state.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from playwright.sync_api import sync_playwright

from profile_extractor.logging_setup import get_logger

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"


def get_worker_auth_path(base_auth_path: Path | str, worker_index: int, total_workers: int) -> Path:
    """Return the designated authentication state file path for a specific worker.

    If total_workers is 1, uses base_auth_path directly (e.g. .auth/state.json).
    If total_workers > 1, generates separate paths: .auth/state_worker_1.json, etc.
    Automatically seeds worker 1 from base_auth_path if worker 1 file is missing.
    """
    base = Path(base_auth_path)
    if total_workers <= 1:
        return base

    worker_path = base.parent / f"state_worker_{worker_index + 1}.json"
    # Seed worker 1 from existing base session if available
    if worker_index == 0 and not worker_path.exists() and base.exists() and not is_session_expired(base):
        try:
            worker_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base, worker_path)
        except Exception:
            pass

    return worker_path


def ensure_worker_logins(
    auth_paths: Sequence[Path | str],
) -> None:
    """Sequentially verify and prompt interactive login for all worker accounts.

    Ensures that login windows never pop open simultaneously on top of each other.
    """
    total = len(auth_paths)
    for idx, p in enumerate(auth_paths):
        target = Path(p)
        if is_session_expired(target):
            label = f"Worker {idx + 1} of {total}" if total > 1 else "Primary Account"
            print(f"\n[!] Authentication required for {label} -> {target.name}")
            run_interactive_login(target, worker_label=label)


def run_interactive_login(auth_state_path: Path | str, worker_label: str = "") -> None:
    """Launch a visible Chromium browser for the employee to sign in manually.

    Steps:
    1. Launches a non-headless Chromium window navigating to LinkedIn login.
    2. Consenting employee logs in manually, completing 2FA / verification prompts.
    3. Auto-detects login when candidate reaches feed / profile, or user presses [ENTER].
    4. Playwright exports cookies and localStorage to auth_state_path (.auth/state.json).
    5. Headless automated extractions reuse this session file without user interaction.
    """
    logger = get_logger()
    target_path = Path(auth_state_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    banner_suffix = f" - {worker_label.upper()}" if worker_label else ""
    print("\n" + "=" * 70)
    print(f"LINKEDIN INTERACTIVE LOGIN HELPER{banner_suffix}")
    print("=" * 70)
    print("A visible browser window will open at the LinkedIn login page.")
    if worker_label:
        print(f"Please log in using the dedicated account for: {worker_label}")
    else:
        print("Please log in manually using your consenting employee account,")
    print("including completing any 2FA or verification prompt.")
    print("The helper will automatically detect when you reach your feed,")
    print("or you can press [ENTER] in the terminal once signed in.")
    print("=" * 70 + "\n")

    is_interactive = sys.stdin is not None and sys.stdin.isatty()
    user_pressed_enter = threading.Event()
    user_cancelled = threading.Event()

    def _listen_stdin() -> None:
        try:
            val = input("\n>>> Press [ENTER] after logging in (or 'q' to cancel): ").strip()
            if val.lower() in ("q", "quit", "exit", "logout"):
                user_cancelled.set()
            else:
                user_pressed_enter.set()
        except (EOFError, Exception):
            pass

    if is_interactive:
        threading.Thread(target=_listen_stdin, daemon=True).start()

    with sync_playwright() as p:
        # Launch visible browser for human interaction
        browser = p.chromium.launch(headless=False)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        logger.info("Opening visible browser to %s", LINKEDIN_LOGIN_URL)
        page.goto(LINKEDIN_LOGIN_URL)

        start_time = time.time()
        timeout_seconds = 600  # 10 minutes maximum for manual 2FA
        captured_live_session = False

        while time.time() - start_time < timeout_seconds:
            if user_cancelled.is_set():
                print("\n[!] Login cancelled by user.")
                try:
                    browser.close()
                except Exception:
                    pass
                raise RuntimeError("Login cancelled by user.")

            # Check if browser was closed by user
            if page.is_closed():
                break

            # Check if user pressed Enter in terminal
            if user_pressed_enter.is_set():
                captured_live_session = True
                break

            # Auto-detect login success via cookies and URL
            try:
                cookie_names = {c.get("name") for c in context.cookies() if c.get("name")}
                current_url = page.url.lower()
                has_auth = "li_at" in cookie_names
                on_app_page = any(
                    path in current_url
                    for path in ("/feed", "/in/", "/mynetwork", "/messaging", "/notifications")
                )
                not_on_auth_gate = (
                    "login" not in current_url
                    and "checkpoint" not in current_url
                    and "uas" not in current_url
                )

                if has_auth and (on_app_page or not_on_auth_gate):
                    print("\n[OK] Detected active LinkedIn login session in browser!")
                    captured_live_session = True
                    page.wait_for_timeout(3000)  # Settle companion cookies
                    try:
                        context.storage_state(path=str(target_path))
                    except Exception as state_err:
                        logger.debug("Intermediate state capture: %s", state_err)
                    break
            except Exception:
                pass

            page.wait_for_timeout(1000)

        # Capture complete session cookies and localStorage before closing browser
        try:
            if not captured_live_session:
                cookie_names = {c.get("name") for c in context.cookies() if c.get("name")}
                if "li_at" in cookie_names:
                    captured_live_session = True
            if captured_live_session:
                context.storage_state(path=str(target_path))
        except Exception as err:
            if not target_path.is_file():
                logger.error("Failed to capture session state: %s", err)
                raise RuntimeError(
                    f"Could not save session. Ensure browser was not prematurely closed: {err}"
                ) from err
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # Verify that an actual authenticated session cookie ('li_at') was captured in this login run
    if not captured_live_session or is_session_expired(target_path):
        target_path.unlink(missing_ok=True)
        print("\n[!] Warning: No authenticated LinkedIn session ('li_at' cookie) was detected.")
        print("    You must finish signing in before the browser is closed.")
        raise RuntimeError("No authenticated session captured.")

    # Apply restrictive file permissions where supported (POSIX 0o600)
    try:
        os.chmod(target_path, 0o600)
    except Exception:
        pass

    print(f"\n[OK] Authenticated session saved successfully to: {target_path}\n")
    logger.info("Session state saved to %s", target_path)


def is_session_expired(auth_state_path: Path | str | None) -> bool:
    """Check if the session state file is missing, unreadable, or has expired cookies.

    Inspects the `li_at` cookie:
    - If file doesn't exist -> Expired (True).
    - If `li_at` cookie is missing -> Expired (True).
    - If `li_at` expiration timestamp is in the past -> Expired (True).
    - If `li_at` expiration is in the future -> Active (False).
    """
    if not auth_state_path:
        return True
    path = Path(auth_state_path)
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        # Locate the core authentication cookie
        li_at = next((c for c in cookies if c.get("name") == "li_at"), None)
        if not li_at:
            return True
        expires = li_at.get("expires", -1)
        # Check against current epoch timestamp
        if expires != -1 and expires < time.time():
            return True
        return False
    except Exception:
        return True
