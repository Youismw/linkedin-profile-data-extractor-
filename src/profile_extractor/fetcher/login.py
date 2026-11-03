"""Interactive login helper for consenting employee to save authenticated session.

Security & Architecture Principles:
- Anti-CWE-256: Passwords and credentials are NEVER handled, accepted, or stored
  by the application or its config files.
- Manual 2FA Handling: Because modern LinkedIn accounts require multi-factor
  authentication (SMS, authenticator app, or email codes), headless automated login
  invariably fails. A quick one-time manual sign-in captures genuine 2FA session
  tokens into `.auth/state.json` (gitignored).
- Proactive Cookie Health: `is_session_expired` checks the expiration timestamp
  of LinkedIn's `li_at` authentication cookie before making network requests.
"""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from profile_extractor.logging_setup import get_logger

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"


def run_interactive_login(auth_state_path: Path | str) -> None:
    """Launch a visible Chromium browser for the employee to sign in manually.

    Steps:
    1. Launches a non-headless Chromium window navigating to LinkedIn login.
    2. Consenting employee logs in manually, completing 2FA / verification prompts.
    3. Employee presses [ENTER] in the terminal once their feed has loaded.
    4. Playwright exports cookies and localStorage to auth_state_path (.auth/state.json).
    5. Headless automated extractions reuse this session file without user interaction.
    """
    logger = get_logger()
    target_path = Path(auth_state_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print("LINKEDIN INTERACTIVE LOGIN HELPER")
    print("=" * 70)
    print("A visible browser window will open at the LinkedIn login page.")
    print("Please log in manually using your consenting employee account,")
    print("including completing any 2FA or verification prompt.")
    print("When you see your LinkedIn feed or home page, return here and press [ENTER].")
    print("=" * 70 + "\n")

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

        # Pause and wait for human confirmation after 2FA sign-in
        user_input = input("\n>>> Press [ENTER] after logging in (or 'q' to cancel): ").strip()
        cancel_words = ("q", "quit", "exit", "logout")
        if user_input.lower() in cancel_words or user_input.startswith((".\\run", "run", "python")):
            try:
                browser.close()
            except Exception:
                pass
            print("\n[!] Login cancelled by user.")
            raise RuntimeError("Login cancelled by user.")

        # Capture complete session cookies and localStorage
        try:
            context.storage_state(path=str(target_path))
        except Exception as err:
            logger.error("Failed to capture session state: %s", err)
            raise RuntimeError(
                f"Could not save session. Ensure browser was not prematurely closed: {err}"
            ) from err
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # Verify that an actual authenticated session cookie ('li_at') was captured
    if is_session_expired(target_path):
        target_path.unlink(missing_ok=True)
        print("\n[!] Warning: No authenticated LinkedIn session ('li_at' cookie) was detected.")
        print("    You must finish signing in before pressing Enter.")
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
