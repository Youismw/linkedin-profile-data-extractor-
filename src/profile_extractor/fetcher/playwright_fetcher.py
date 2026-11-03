"""Playwright-based fetcher utilizing stored session state and incremental scrolling.

Technical Highlights:
1. Reusable Browser Lifecycle: Launches Chromium once per pipeline run and
   shares the authenticated context across profiles for high throughput.
2. Container-Targeted Lazy Scrolling: LinkedIn's SPA layout nests the scrollable
   viewport inside `<main id="workspace">` rather than `window`. This fetcher
   progressively scrolls both `main` and `window`, triggering intersection
   observers and GraphQL queries for deep sections (Experience, Education, Skills).
3. In-Flight Authwall Recovery: If a session expires mid-run, automatically
   prompts the user, opens a login window, captures fresh cookies, and retries.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from profile_extractor.fetcher.detection import detect_outcome
from profile_extractor.fetcher.login import run_interactive_login
from profile_extractor.logging_setup import get_logger
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.urls import extract_slug, sanitize_slug


class PlaywrightFetcher:
    """Fetcher implementation using Chromium via Playwright sync API."""

    def __init__(
        self,
        auth_state_path: Path | str | None = None,
        headless: bool = True,
        timeout_seconds: float = 30.0,
        save_raw_html_dir: Path | str | None = None,
        max_scroll_steps: int = 15,
        scroll_step_delay_ms: int = 400,
        interactive_relogin: bool = True,
    ) -> None:
        self.auth_state_path = Path(auth_state_path) if auth_state_path else None
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self.save_raw_html_dir = Path(save_raw_html_dir) if save_raw_html_dir else None
        self.max_scroll_steps = max_scroll_steps
        self.scroll_step_delay_ms = scroll_step_delay_ms
        self.interactive_relogin = interactive_relogin
        self._allow_relogin = True

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    def _ensure_browser(self) -> BrowserContext:
        """Initialize playwright browser and context if not already active.

        Injects saved storage state (cookies) and desktop user agent to bypass
        mobile web views and maintain session continuity.
        """
        # Reconnect if browser crashed or disconnected
        if self._browser is not None and not self._browser.is_connected():
            self.close()

        if self._context is not None:
            return self._context

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)

        has_auth = self.auth_state_path and self.auth_state_path.is_file()
        if has_auth:
            # Load stored session cookies from .auth/state.json
            self._context = self._browser.new_context(
                storage_state=str(self.auth_state_path),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
        else:
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 900},
            )

        return self._context

    def fetch(self, url: str) -> FetchResult:
        """Navigate to the profile URL, scroll for lazy loaded sections, and detect outcome.

        Execution stages:
        1. Opens a new tab with hard timeout.
        2. Navigates to target URL with 'domcontentloaded' wait.
        3. Executes progressive multi-step scrolling on `<main>` and `window`.
        4. Detects page status (OK, NOT_FOUND, NOT_VISIBLE, BLOCKED).
        5. If session expired, triggers interactive relogin and retries.
        """
        logger = get_logger()
        logger.debug("Fetching profile via Playwright: %s", url)
        now_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        try:
            context = self._ensure_browser()
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                response = page.goto(url, wait_until="domcontentloaded")
                status_code = response.status if response else None

                # --------------------------------------------------------------
                # Progressive Container Scrolling:
                # Scrolls document.querySelector('main') and window by 600px
                # increments to trigger GraphQL lazy loading of lower sections.
                # --------------------------------------------------------------
                for _ in range(self.max_scroll_steps):
                    scroll_info = page.evaluate("""() => {
                        const m = document.querySelector('main')
                            || document.scrollingElement
                            || document.documentElement;
                        const prevScrollTop = m.scrollTop;
                        m.scrollTop += 600;
                        window.scrollBy(0, 600);
                        return {
                            moved: m.scrollTop > prevScrollTop,
                            atBottom: (m.scrollTop + m.clientHeight + 50) >= m.scrollHeight
                        };
                    }""")
                    page.wait_for_timeout(self.scroll_step_delay_ms)
                    if isinstance(scroll_info, dict):
                        # Stop scrolling early if container reached the bottom
                        if not scroll_info.get("moved") and scroll_info.get("atBottom"):
                            break

                # Settle delay for DOM mutations to finish rendering
                page.wait_for_timeout(1000)

                current_url = page.url
                html = page.content()

                # Detect outcome (ok, not_found, not_visible, blocked)
                status, reason, detail = detect_outcome(
                    current_url=current_url,
                    html=html,
                    status_code=status_code,
                )

                # --------------------------------------------------------------
                # In-Flight Authwall Recovery:
                # If session expired mid-batch, prompt user, pop browser login,
                # save updated .auth/state.json, and retry fetching this profile.
                # --------------------------------------------------------------
                if (
                    status == Status.UNREACHABLE
                    and reason == Reason.NOT_VISIBLE
                    and self._allow_relogin
                    and self.interactive_relogin
                    and self.auth_state_path
                    and sys.stdin
                    and sys.stdin.isatty()
                ):
                    print("\n" + "=" * 70)
                    print("[!] LinkedIn session has expired or requires re-authentication.")
                    print("    Opening LinkedIn login window now. Please sign in to resume...")
                    print("=" * 70 + "\n")

                    run_interactive_login(self.auth_state_path)
                    self.close()
                    self._allow_relogin = False
                    return self.fetch(url)

                # Optional debug dumping of raw HTML fixtures
                if self.save_raw_html_dir:
                    try:
                        slug = extract_slug(url)
                        clean_slug = sanitize_slug(slug, fallback_seed=url)
                        self.save_raw_html_dir.mkdir(parents=True, exist_ok=True)
                        dump_path = self.save_raw_html_dir / f"{clean_slug}.html"
                        dump_path.write_text(html, encoding="utf-8")
                    except Exception as dump_err:
                        logger.debug("Failed saving raw HTML dump: %s", dump_err)

                return FetchResult(
                    url=url,
                    status=status,
                    reason=reason,
                    html=html if status == Status.OK else None,
                    fetched_at=now_utc,
                    detail=detail,
                )

            except PlaywrightTimeoutError:
                return FetchResult(
                    url=url,
                    status=Status.UNREACHABLE,
                    reason=Reason.TIMEOUT,
                    fetched_at=now_utc,
                    detail="Page navigation timed out",
                )
            finally:
                # Always close tab to prevent memory leaks
                page.close()

        except Exception as e:
            return FetchResult(
                url=url,
                status=Status.UNREACHABLE,
                reason=Reason.UNEXPECTED_ERROR,
                fetched_at=now_utc,
                detail=str(e),
            )

    def close(self) -> None:
        """Release browser context and shut down Playwright processes cleanly."""
        try:
            if self._context:
                try:
                    self._context.close()
                except Exception:
                    pass
                self._context = None

            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None

            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        except (Exception, KeyboardInterrupt):
            pass
