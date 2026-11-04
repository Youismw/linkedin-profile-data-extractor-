"""Playwright-based fetcher utilizing stored session state and incremental scrolling.

Module Architecture & Inter-Module Communication:
==================================================
This module is responsible for authenticated headless Chromium browser lifecycle
management, page navigation, lazy-container progressive viewport scrolling, in-flight
DOM card snapshotting, in-flight authwall recovery, and deterministic resource teardown.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.cli`: Instantiates `PlaywrightFetcher(auth_state_path, headless, timeout_seconds)`.
   - `profile_extractor.orchestrator`:
     * Calls `fetcher.fetch(url)` for candidate profiles and deep detail subpages.
     * Calls `fetcher.close()` to cleanly shut down Chromium contexts and browser processes.
   - Unit tests in `tests/test_playwright_fetcher.py`.

2. Downstream Modules Called:
   - `profile_extractor.fetcher.detection`:
     * Calls `detect_outcome(status_code, final_url, html)` to classify page responses
       into Status (OK, UNREACHABLE) and Reason (BLOCKED, NOT_FOUND, NOT_VISIBLE, OK).
   - `profile_extractor.fetcher.login`:
     * Calls `run_interactive_login(auth_state_path)` for automatic in-flight re-login
       if an active session cookie expires mid-run.
   - `profile_extractor.urls`:
     * Calls `extract_slug()` and `sanitize_slug()` for logging context.
   - `profile_extractor.models`:
     * Returns `FetchResult(url, status, reason, html, status_code, elapsed_seconds)`.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from profile_extractor.fetcher.detection import detect_outcome
from profile_extractor.fetcher.login import run_interactive_login
from profile_extractor.logging_setup import get_logger
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.urls import extract_slug, sanitize_slug


_CAPTURE_PROFILE_CARDS_JS = r"""() => {
    const root = document.querySelector('main') || document.body || document.documentElement;
    if (!root) return {count: 0, totalTextLength: 0};

    const stateKey = '__profileExtractorCapturedCardsV1';
    const state = window[stateKey] || (window[stateKey] = {order: [], cards: {}});
    const isSkillsDetails = /\/details\/skills\/?$/.test(window.location.pathname);
    const profileCardSelectors =
        '[id^="com.linkedin.sdui.profile.card."], ' +
        '[id^="profileCardsAboveActivity"], ' +
        '[id^="profileCardsBelowActivity"]';
    // Skills detail pages can virtualize entity cards as the list scrolls. Capture
    // those cards too, but only on /details/skills/ to avoid broad DOM snapshots
    // or changing capture behaviour on ordinary profile pages.
    const selectors = isSkillsDetails
        ? profileCardSelectors + ', div[componentkey*="entity-collection-item"]'
        : profileCardSelectors;
    const cards = root.querySelectorAll(selectors);

    for (const card of cards) {
        const id = card.getAttribute('id');
        const componentKey = card.getAttribute('componentkey');
        const isSkillEntityItem = isSkillsDetails
            && componentKey
            && componentKey.includes('entity-collection-item');
        const key = isSkillEntityItem
            ? `componentkey:${componentKey}`
            : (id ? `id:${id}` : '');
        if (!key) continue;

        // The older LinkedIn layout uses profileCardsAbove/BelowActivity wrappers.
        // Skip a wrapper when its child SDUI cards are captured individually; keep
        // it when it is the only container for a section such as Interests.
        const isLegacyGroup = typeof id === 'string' && (
            id.startsWith('profileCardsAboveActivity')
            || id.startsWith('profileCardsBelowActivity')
        );
        if (isLegacyGroup && card.querySelector(
            '[id^="com.linkedin.sdui.profile.card."]'
        )) continue;
        const text = (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim();
        if (!text) continue;

        const previous = state.cards[key];
        if (!previous) state.order.push(key);
        // Avoid serializing unchanged cards on every scroll step. Keep the richest
        // version seen, including same-length text changes after hydration.
        if (!previous || text !== previous.text) {
            if (!previous || text.length >= previous.textLength) {
                state.cards[key] = {html: card.outerHTML, text, textLength: text.length};
            }
        }
    }

    let totalTextLength = 0;
    for (const id of state.order) {
        totalTextLength += state.cards[id]?.textLength || 0;
    }
    return {count: state.order.length, totalTextLength};
}"""

_GET_CAPTURED_PROFILE_CARDS_JS = r"""() => {
    const state = window.__profileExtractorCapturedCardsV1;
    if (!state) return [];
    return state.order.map(id => state.cards[id]?.html).filter(Boolean);
}"""


def _compose_captured_profile_html(cards: object, fallback_html: str) -> str:
    """Preserve the final page and merge in cards lost to LinkedIn virtualization.

    The final DOM can contain useful content that is not covered by the known
    profile-card ID prefixes (including unclassified sections and raw text).
    On skills detail pages it can also merge captured entity-collection items.
    Never replace that document with a whitelist of captured cards. Instead,
    retain it as the source of truth, add missing captured nodes in snapshot order,
    and replace an existing node only when the captured snapshot has more text.
    """
    if not isinstance(cards, list):
        return fallback_html

    valid_cards = [card for card in cards if isinstance(card, str) and card.strip()]
    if not valid_cards:
        return fallback_html

    try:
        soup = BeautifulSoup(fallback_html, "lxml")
        main = soup.find("main")
        if main is None:
            container = soup.body if soup.body is not None else soup
            main = soup.new_tag("main", id="workspace")
            container.append(main)

        changed = False
        for card_index, markup in enumerate(valid_cards):
            fragment = BeautifulSoup(markup, "lxml")
            fragment_body = fragment.body
            captured = (
                fragment_body.find(True, recursive=False)
                if fragment_body is not None
                else fragment.find(True)
            )
            if captured is None:
                continue

            card_id = captured.get("id")
            component_key = captured.get("componentkey")
            if isinstance(component_key, str) and component_key:
                existing = soup.find(None, attrs={"componentkey": component_key})
                # Keep detail-page entity items inside LazyColumn so the existing
                # skills parser discovers them through its normal selection path.
                destination = soup.select_one('[data-component-type="LazyColumn"]') or main
            elif isinstance(card_id, str) and card_id:
                existing = soup.find(id=card_id)
                destination = main
            else:
                continue

            if existing is None:
                inserted = False
                if isinstance(component_key, str) and component_key:
                    # Retain the original detail-list order when an earlier card
                    # was virtualized away but a later card remains in the final DOM.
                    for later_markup in valid_cards[card_index + 1 :]:
                        later_fragment = BeautifulSoup(later_markup, "lxml")
                        later_node = later_fragment.find(None, attrs={"componentkey": True})
                        later_key = later_node.get("componentkey") if later_node else None
                        if isinstance(later_key, str) and later_key:
                            later_existing = soup.find(None, attrs={"componentkey": later_key})
                            if later_existing is not None:
                                later_existing.insert_before(captured)
                                inserted = True
                                break
                if not inserted:
                    destination.append(captured)
                changed = True
                continue

            # Use text length as a conservative richness check. Equal/shorter
            # snapshots never replace content from the final DOM.
            captured_text = captured.get_text(" ", strip=True)
            existing_text = existing.get_text(" ", strip=True)
            if len(captured_text) > len(existing_text):
                existing.replace_with(captured)
                changed = True

        # Returning the original bytes when no merge was needed avoids needless
        # serialization differences and preserves the exact final-page capture.
        return str(soup) if changed else fallback_html
    except Exception:
        # A malformed fragment must not make the primary page capture unusable.
        return fallback_html


def _about_section_node(soup: BeautifulSoup) -> Tag | None:
    """Return the <section> the parser treats as About (same test as parse_profile)."""
    from profile_extractor.parser import _extract_section_heading, _matches_any_pattern
    from profile_extractor.parser_rules import ABOUT_PATTERNS

    for sec in soup.find_all("section"):
        if not isinstance(sec, Tag):
            continue
        heading = _extract_section_heading(sec)
        if _matches_any_pattern(heading, ABOUT_PATTERNS) or str(sec.get("id") or "").lower() == "about":
            return sec
    return None


def _ensure_about_section(composed_html: str, final_html: str) -> str:
    """Additive safety net for the About section.

    Acts only when the composed document has no About section but the final page
    does. It then appends a copy of the final page's About section to the composed
    <main>. It never removes or edits existing content, so fields already captured
    are unaffected.
    """
    try:
        composed = BeautifulSoup(composed_html, "lxml")
        if _about_section_node(composed) is not None:
            return composed_html
        about_node = _about_section_node(BeautifulSoup(final_html, "lxml"))
        if about_node is None:
            return composed_html
        main = composed.find("main")
        if main is None:
            return composed_html
        fragment = BeautifulSoup(str(about_node), "lxml")
        node = fragment.body.find(True, recursive=False) if fragment.body else None
        if node is None:
            return composed_html
        main.append(node)
        return str(composed)
    except Exception:
        return composed_html


class PlaywrightFetcher:
    """Fetcher implementation using Chromium via Playwright sync API."""

    def __init__(
        self,
        auth_state_path: Path | str | None = None,
        headless: bool = True,
        timeout_seconds: float = 30.0,
        save_raw_html_dir: Path | str | None = None,
        max_scroll_steps: int = 70,
        scroll_step_delay_ms: int = 350,
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
        logger.debug("Fetching profile via Playwright")
        now_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        try:
            context = self._ensure_browser()
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                response = page.goto(url, wait_until="domcontentloaded")
                status_code = response.status if response else None

                # Wait for the profile shell to hydrate before moving the viewport.
                # LinkedIn often returns DOMContentLoaded while the profile cards are
                # still skeletons; immediate scrolling can skip the initial card set.
                wait_for_function = getattr(page, "wait_for_function", None)
                if callable(wait_for_function):
                    try:
                        wait_for_function(
                            """() => {
                                const main = document.querySelector('main');
                                const card = main?.querySelector(
                                    '[id^="com.linkedin.sdui.profile.card."]'
                                );
                                const cardText = (card?.innerText || '').trim();
                                const heading = main?.querySelector('h1');
                                const mainText = (main?.innerText || '').trim();
                                const bodyText = (document.body?.innerText || '').toLowerCase();
                                const terminalPage = [
                                    'sign in to view',
                                    'security verification',
                                    'page not found',
                                    'profile unavailable'
                                ].some(token => bodyText.includes(token));
                                return terminalPage || (card && cardText.length > 40)
                                    || (heading && mainText.length > 40);
                            }""",
                            timeout=min(self.timeout_ms, 15000),
                        )
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Profile shell did not reach the expected ready state within 15s; "
                            "continuing with incremental capture."
                        )

                # Capture profile cards before the first scroll. We also capture after
                # each scroll/settle step because LinkedIn may virtualize cards that
                # have moved out of the viewport. Only reading page.content() at the
                # bottom can therefore lose About, Featured, or other early cards.
                page.evaluate(_CAPTURE_PROFILE_CARDS_JS)

                # --------------------------------------------------------------
                # Progressive Container Scrolling:
                # Scrolls document.querySelector('main') and window by 600px
                # increments to trigger GraphQL lazy loading of lower sections.
                # --------------------------------------------------------------
                reached_bottom = False
                consecutive_at_bottom = 0
                previous_card_text_length: int | None = None
                stable_bottom_samples = 0
                for _ in range(self.max_scroll_steps):
                    scroll_info = page.evaluate("""() => {
                        const m = document.querySelector('main')
                            || document.scrollingElement
                            || document.documentElement;
                        const prevM = m.scrollTop;
                        const prevWin = window.scrollY || window.pageYOffset || 0;

                        m.scrollTop += 600;
                        window.scrollBy(0, 600);

                        const currWin = window.scrollY || window.pageYOffset || 0;
                        const winScrollHeight = Math.max(
                            document.documentElement.scrollHeight,
                            document.body ? document.body.scrollHeight : 0
                        );
                        const winBottom = (window.innerHeight + currWin + 50) >= winScrollHeight;
                        const mBottom = (m.scrollTop + m.clientHeight + 50) >= m.scrollHeight;

                        const moved = (m.scrollTop > prevM) || (currWin > prevWin);
                        const atBottom = mBottom && winBottom;
                        return { moved, atBottom };
                    }""")
                    page.wait_for_timeout(self.scroll_step_delay_ms)
                    capture_info = page.evaluate(_CAPTURE_PROFILE_CARDS_JS)

                    if isinstance(scroll_info, dict):
                        at_bottom_and_still = (
                            scroll_info.get("atBottom") and not scroll_info.get("moved")
                        )
                        if at_bottom_and_still:
                            consecutive_at_bottom += 1
                            card_text_length = (
                                capture_info.get("totalTextLength")
                                if isinstance(capture_info, dict)
                                else None
                            )
                            if (
                                card_text_length is not None
                                and card_text_length == previous_card_text_length
                            ):
                                stable_bottom_samples += 1
                            else:
                                stable_bottom_samples = 0
                            previous_card_text_length = card_text_length

                            # Require both scroll position and captured card text to
                            # settle, rather than stopping after two empty bottom polls.
                            if consecutive_at_bottom >= 3 and stable_bottom_samples >= 2:
                                reached_bottom = True
                                break
                        else:
                            consecutive_at_bottom = 0
                            stable_bottom_samples = 0
                            previous_card_text_length = None

                if not reached_bottom and self.max_scroll_steps > 0:
                    logger.warning(
                        "Page scrolling reached safety limit (%d steps) before confirming bottom; "
                        "some dynamic lazy-loaded sections may be incomplete.",
                        self.max_scroll_steps,
                    )

                # --------------------------------------------------------------
                # Systematic Content Expansion:
                # Clicks all inline '...see more' / 'show more' controls to un-truncate
                # role descriptions, About text, and project details in place.
                # --------------------------------------------------------------
                try:
                    expanded_count = page.evaluate("""() => {
                        const buttons = Array.from(document.querySelectorAll(
                            'button.inline-show-more-text__button, ' +
                            'button.show-more-less-html__button, ' +
                            'button[aria-expanded="false"], ' +
                            'button, [role="button"]'
                        ));
                        let clicked = 0;
                        for (const b of buttons) {
                            const text = (b.innerText || b.textContent || "").trim().toLowerCase();
                            const aria = (b.getAttribute("aria-label") || "").toLowerCase();
                            const cls = (b.className || "").toString().toLowerCase();

                            const isInlineExpand =
                                cls.includes("show-more") ||
                                cls.includes("see-more") ||
                                text === "...more" ||
                                text === "... more" ||
                                text === "see more" ||
                                text === "show more" ||
                                text === "more" ||
                                aria.includes("see more") ||
                                aria.includes("show more");

                            const isNavigation = b.closest('a') !== null
                                || text.includes("show all")
                                || (b.getAttribute('href')
                                    && b.getAttribute('href').startsWith('/'));

                            if (isInlineExpand && !isNavigation && b.offsetParent !== null) {
                                try {
                                    b.click();
                                    clicked++;
                                } catch (e) {}
                            }
                        }
                        return clicked;
                    }""")
                    if expanded_count:
                        logger.debug("Expanded %d inline text controls", expanded_count)
                        page.wait_for_timeout(400)
                except Exception as exp_err:
                    logger.debug("Could not expand inline controls: %s", exp_err)

                # Settle delay for DOM mutations to finish rendering and take one
                # final card snapshot after expansion.
                page.wait_for_timeout(600)
                page.evaluate(_CAPTURE_PROFILE_CARDS_JS)

                current_url = page.url
                final_html = page.content()

                # Detect outcome using the actual final response DOM (not the
                # synthetic merged document, which intentionally contains only
                # retained profile cards).
                status, reason, detail = detect_outcome(
                    current_url=current_url,
                    html=final_html,
                    status_code=status_code,
                )

                captured_cards = page.evaluate(_GET_CAPTURED_PROFILE_CARDS_JS)
                html = _compose_captured_profile_html(captured_cards, final_html)
                html = _ensure_about_section(html, final_html)

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
