"""Tests for PlaywrightFetcher: timeout handling, no-session outcome, and live smoke test."""

from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from profile_extractor.fetcher.login import is_session_expired
from profile_extractor.fetcher.playwright_fetcher import (
    PlaywrightFetcher,
    _ensure_about_section,
)
from profile_extractor.models import Reason, Status


class TestPlaywrightFetcher:
    """Test suite for PlaywrightFetcher functionality and error handling."""

    def test_timeout_handling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify Playwright timeout maps to Status.UNREACHABLE and Reason.TIMEOUT."""
        fetcher = PlaywrightFetcher(headless=True, timeout_seconds=1.0)

        # Mock page.goto to raise PlaywrightTimeoutError
        class MockPage:
            def set_default_timeout(self, timeout: int) -> None:
                pass

            def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
                raise PlaywrightTimeoutError("Navigation timed out")

            def close(self) -> None:
                pass

        class MockContext:
            def new_page(self) -> MockPage:
                return MockPage()

            def close(self) -> None:
                pass

        monkeypatch.setattr(fetcher, "_ensure_browser", MockContext)

        try:
            res = fetcher.fetch("https://www.linkedin.com/in/timeout-profile/")
            assert res.status == Status.UNREACHABLE
            assert res.reason == Reason.TIMEOUT
            assert "timed out" in (res.detail or "").lower()
        finally:
            fetcher.close()

    def test_no_session_returns_not_visible(self, tmp_path: Path) -> None:
        """Verify fetching with no session encounters authwall (Reason.NOT_VISIBLE)."""
        nonexistent_auth = tmp_path / "nonexistent_state.json"
        fetcher = PlaywrightFetcher(auth_state_path=nonexistent_auth, headless=True)

        # Using a simulated local/authwall response or real navigation check
        # Mock ensure_browser to simulate hitting an authwall
        class MockPage:
            url = "https://www.linkedin.com/authwall?trk=bf"

            def set_default_timeout(self, timeout: int) -> None:
                pass

            def goto(self, url: str, wait_until: str = "domcontentloaded") -> object:
                return type("Resp", (), {"status": 200})()

            def evaluate(self, script: str) -> int:
                return 1000

            def wait_for_timeout(self, ms: int) -> None:
                pass

            def content(self) -> str:
                return "<html><body class='authwall'>Sign in to view full profile</body></html>"

            def close(self) -> None:
                pass

        class MockContext:
            def new_page(self) -> MockPage:
                return MockPage()

            def close(self) -> None:
                pass

        fetcher._context = MockContext()  # type: ignore[assignment]

        try:
            res = fetcher.fetch("https://www.linkedin.com/in/sample-profile/")
            assert res.status == Status.UNREACHABLE
            assert res.reason == Reason.NOT_VISIBLE
        finally:
            fetcher.close()

    def test_is_session_expired(self, tmp_path: Path) -> None:
        """Verify is_session_expired accurately detects missing, expired, and valid states."""
        # 1. Nonexistent file
        assert is_session_expired(tmp_path / "missing.json") is True
        assert is_session_expired(None) is True

        # 2. File with no cookies
        no_cookies = tmp_path / "empty.json"
        no_cookies.write_text('{"cookies": []}', encoding="utf-8")
        assert is_session_expired(no_cookies) is True

        # 3. File with expired li_at cookie
        expired_file = tmp_path / "expired.json"
        expired_file.write_text(
            '{"cookies": [{"name": "li_at", "expires": 100000}]}',
            encoding="utf-8",
        )
        assert is_session_expired(expired_file) is True

        # 4. File with valid future li_at cookie
        valid_file = tmp_path / "valid.json"
        valid_file.write_text(
            '{"cookies": [{"name": "li_at", "expires": 9999999999}]}',
            encoding="utf-8",
        )
        assert is_session_expired(valid_file) is False

    def test_ensure_about_section_adds_about_when_missing(self) -> None:
        """Additive safety net: appends About section from final page if missing in composed."""
        composed = (
            "<html><body><main><section id='experience'>"
            "<h2>Experience</h2></section></main></body></html>"
        )
        final = (
            "<html><body><main><section id='about'>"
            "<h2>About</h2><p>Experienced engineer</p></section></main></body></html>"
        )
        result = _ensure_about_section(composed, final)
        assert "Experienced engineer" in result
        assert "Experience" in result

    def test_ensure_about_section_noop_when_about_already_present(self) -> None:
        """Additive safety net: does nothing if composed HTML already contains About section."""
        composed = (
            "<html><body><main><section id='about'>"
            "<h2>About</h2><p>Original</p></section></main></body></html>"
        )
        final = (
            "<html><body><main><section id='about'>"
            "<h2>About</h2><p>Replacement</p></section></main></body></html>"
        )
        result = _ensure_about_section(composed, final)
        assert result == composed

    def test_ensure_about_section_noop_when_neither_has_about(self) -> None:
        """Additive safety net: gracefully returns composed HTML when no About section exists."""
        composed = (
            "<html><body><main><section id='experience'>"
            "<h2>Experience</h2></section></main></body></html>"
        )
        final = (
            "<html><body><main><section id='experience'>"
            "<h2>Experience</h2></section></main></body></html>"
        )
        result = _ensure_about_section(composed, final)
        assert result == composed

    def test_save_raw_html_dir_appends_subpages_into_single_dump(self, tmp_path: Path) -> None:
        """Main profile resets <slug>.html and subsequent subpages append with markers."""
        pages = {
            "https://www.linkedin.com/in/jane-doe/": (
                "<html><body><main><h1>Jane Doe</h1><p>Main v1</p></main></body></html>"
            ),
            "https://www.linkedin.com/in/jane-doe/details/skills/": (
                "<html><body><main><h2>Skills</h2><p>Python</p></main></body></html>"
            ),
            "https://www.linkedin.com/in/jane-doe/overlay/contact-info/": (
                "<html><body><div data-sdui-screen='ProfileContactDetailsOverlay'>"
                "Contact</div></body></html>"
            ),
        }

        class MockPage:
            def __init__(self) -> None:
                self.url = ""

            def set_default_timeout(self, timeout: int) -> None:
                pass

            def goto(self, url: str, wait_until: str = "domcontentloaded") -> object:
                self.url = url
                return type("Resp", (), {"status": 200})()

            def evaluate(self, script: str) -> object:
                if "is_already_open" in script or "popover-floating" in script:
                    return True
                if "__profileExtractorCapturedCardsV1" in script and "order.map" in script:
                    return []
                if "totalTextLength" in script:
                    return {"count": 1, "totalTextLength": 100}
                if "atBottom" in script:
                    return {"moved": False, "atBottom": True}
                return 0

            def wait_for_timeout(self, ms: int) -> None:
                pass

            def content(self) -> str:
                return pages[self.url]

            def close(self) -> None:
                pass

        class MockContext:
            def new_page(self) -> MockPage:
                return MockPage()

            def close(self) -> None:
                pass

        fetcher = PlaywrightFetcher(save_raw_html_dir=tmp_path, headless=True)
        fetcher._context = MockContext()  # type: ignore[assignment]

        try:
            fetcher.fetch("https://www.linkedin.com/in/jane-doe/")
            fetcher.fetch("https://www.linkedin.com/in/jane-doe/details/skills/")
            fetcher.fetch("https://www.linkedin.com/in/jane-doe/overlay/contact-info/")

            dump_files = list(tmp_path.glob("*.html"))
            assert [p.name for p in dump_files] == ["jane-doe.html"]

            combined = (tmp_path / "jane-doe.html").read_text(encoding="utf-8")
            assert "Main v1" in combined
            skills_marker = (
                "<!-- === SUBPAGE: https://www.linkedin.com/in/jane-doe/details/skills/ === -->"
            )
            contact_marker = (
                "<!-- === SUBPAGE: "
                "https://www.linkedin.com/in/jane-doe/overlay/contact-info/ === -->"
            )
            assert skills_marker in combined
            assert "<p>Python</p>" in combined
            assert contact_marker in combined
            assert "ProfileContactDetailsOverlay" in combined

            # Re-fetching the main profile starts a fresh dump file rather than appending.
            pages["https://www.linkedin.com/in/jane-doe/"] = (
                "<html><body><main><h1>Jane Doe</h1><p>Main v2</p></main></body></html>"
            )
            fetcher.fetch("https://www.linkedin.com/in/jane-doe/")
            refreshed = (tmp_path / "jane-doe.html").read_text(encoding="utf-8")
            assert "Main v2" in refreshed
            assert "Main v1" not in refreshed
            assert "SUBPAGE:" not in refreshed
        finally:
            fetcher.close()

    def test_http_999_login_wall_triggers_relogin_before_scrolling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 999 login wall triggers interactive login before scrolling and retries."""
        auth_file = tmp_path / "state.json"
        auth_file.write_text('{"cookies": [{"name": "li_at", "expires": 9999999999}]}')

        login_calls: list[Path] = []

        def fake_relogin(path: Path | str) -> None:
            login_calls.append(Path(path))

        monkeypatch.setattr(
            "profile_extractor.fetcher.playwright_fetcher.run_interactive_login",
            fake_relogin,
        )

        attempt = 0
        scroll_calls_by_attempt = {1: 0, 2: 0}

        class MockPage:
            def __init__(self, current_attempt: int) -> None:
                self.current_attempt = current_attempt
                self.url = (
                    "https://www.linkedin.com/authwall?trk=bf"
                    if current_attempt == 1
                    else "https://www.linkedin.com/in/gaurav-akoliya-aa8a3636a/"
                )

            def set_default_timeout(self, timeout: int) -> None:
                pass

            def goto(self, url: str, wait_until: str = "domcontentloaded") -> object:
                code = 999 if self.current_attempt == 1 else 200
                return type("Resp", (), {"status": code})()

            def evaluate(self, script: str) -> object:
                if "scrollBy(0, 600)" in script:
                    scroll_calls_by_attempt[self.current_attempt] += 1
                    return {"moved": True, "atBottom": False}
                if "window.__peCapturedCards" in script and "cards:" in script:
                    return []
                return {"count": 1, "totalTextLength": 1500}

            def wait_for_timeout(self, ms: int) -> None:
                pass

            def content(self) -> str:
                if self.current_attempt == 1:
                    return "<html><body><p>Sign in to view full profile</p></body></html>"
                return "<html><body><main><h1>Gaurav Akoliya</h1></main></body></html>"

            def close(self) -> None:
                pass

        class MockContext:
            def new_page(self) -> MockPage:
                nonlocal attempt
                attempt += 1
                return MockPage(attempt)

            def close(self) -> None:
                pass

        fetcher = PlaywrightFetcher(
            auth_state_path=auth_file,
            headless=True,
            max_scroll_steps=5,
            interactive_relogin=True,
        )
        monkeypatch.setattr(fetcher, "_ensure_browser", lambda: MockContext())

        try:
            res = fetcher.fetch("https://www.linkedin.com/in/gaurav-akoliya-aa8a3636a/")
            assert res.status == Status.OK
            assert login_calls == [auth_file]
            assert scroll_calls_by_attempt[1] == 0
            assert scroll_calls_by_attempt[2] == 5
        finally:
            fetcher.close()

    @pytest.mark.live
    def test_live_smoke_test(self) -> None:
        """Live smoke test returning HTML containing the consenting employee's name.

        Skipped by default unless running with `pytest -m live`.
        Requires a valid .auth/state.json session created via `python -m profile_extractor login`.
        """
        auth_file = Path(".auth/state.json")
        if not auth_file.is_file():
            pytest.skip("Auth state not found. Run manual login helper first.")

        fetcher = PlaywrightFetcher(auth_state_path=auth_file, headless=True)
        try:
            # Replaced with consenting employee profile URL
            res = fetcher.fetch("https://www.linkedin.com/in/me/")
            assert res.status == Status.OK
            assert res.html is not None
            assert len(res.html) > 0
        finally:
            fetcher.close()
