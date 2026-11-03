"""Interactive login helper for consenting employee to save authenticated session."""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from profile_extractor.logging_setup import get_logger

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"


def run_interactive_login(auth_state_path: Path | str) -> None:
    """Launch a visible Chromium browser for the employee to sign in manually.

    Once logged in (including 2FA), the employee presses Enter in the console,
    and storage state (cookies & localStorage) is saved to auth_state_path.
    Credentials are never handled, viewed, or stored by the application.
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

        # Wait for user confirmation in terminal
        input("\n>>> Press [ENTER] in this terminal AFTER you have successfully logged in: ")

        # Save session storage state
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

    # Apply restrictive permissions where supported
    try:
        os.chmod(target_path, 0o600)
    except Exception:
        pass

    print(f"\n[OK] Authenticated session saved successfully to: {target_path}\n")
    logger.info("Session state saved to %s", target_path)
