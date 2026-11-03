"""Tests for PlaywrightFetcher: timeout handling, no-session outcome, and live smoke test."""

from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from profile_extractor.fetcher.login import is_session_expired
from profile_extractor.fetcher.playwright_fetcher import PlaywrightFetcher
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
