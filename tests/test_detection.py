"""Tests for Phase 5: URL and content detection patterns."""

from profile_extractor.fetcher.detection import detect_outcome
from profile_extractor.models import Reason, Status


class TestDetection:
    """Test suite covering detection rules for visible, blocked, not found, and authwall pages."""

    def test_detect_ok_page(self) -> None:
        """Verify normal profile page is detected as Status.OK."""
        url = "https://www.linkedin.com/in/jane-doe/"
        html = (
            "<html><head><title>Jane Doe | LinkedIn</title></head>"
            "<body><h1>Jane Doe</h1></body></html>"
        )
        status, reason, detail = detect_outcome(url, html, status_code=200)
        assert status == Status.OK
        assert reason is None
        assert detail is None

    def test_detect_login_wall_by_url(self) -> None:
        """Verify authwall redirect is identified as Reason.NOT_VISIBLE."""
        url = "https://www.linkedin.com/authwall?trk=bf&trkInfo=AQ..."
        html = "<html><body>Authwall redirecting...</body></html>"
        status, reason, detail = detect_outcome(url, html)
        assert status == Status.UNREACHABLE
        assert reason == Reason.NOT_VISIBLE
        assert "authwall" in (detail or "")

    def test_detect_login_wall_by_content(self) -> None:
        """Verify login wall content is identified as Reason.NOT_VISIBLE."""
        url = "https://www.linkedin.com/in/private-profile/"
        html = (
            "<html><body><div>Sign in to view full profile</div>"
            "<button>Join LinkedIn</button></body></html>"
        )
        status, reason, detail = detect_outcome(url, html)
        assert status == Status.UNREACHABLE
        assert reason == Reason.NOT_VISIBLE

    def test_detect_not_found(self) -> None:
        """Verify 404 status code or not found text is identified as Reason.NOT_FOUND."""
        # By status code
        status, reason, _ = detect_outcome(
            "https://www.linkedin.com/in/deleted/",
            "<html><body>Not found</body></html>",
            status_code=404,
        )
        assert status == Status.UNREACHABLE
        assert reason == Reason.NOT_FOUND

        # By content
        status2, reason2, _ = detect_outcome(
            "https://www.linkedin.com/in/missing/",
            "<html><body><h1>This profile is not available</h1></body></html>",
        )
        assert status2 == Status.UNREACHABLE
        assert reason2 == Reason.NOT_FOUND

    def test_detect_blocked_security_challenge(self) -> None:
        """Verify security challenge or CAPTCHA is identified as Reason.BLOCKED."""
        # By URL
        status, reason, _ = detect_outcome(
            "https://www.linkedin.com/checkpoint/challenge/AgFk8...",
            "<html><body>Challenge</body></html>",
        )
        assert status == Status.UNREACHABLE
        assert reason == Reason.BLOCKED

        # By content
        status2, reason2, detail2 = detect_outcome(
            "https://www.linkedin.com/in/some-user/",
            (
                "<html><body><h1>Security Verification</h1>"
                "<p>Please solve this puzzle</p></body></html>"
            ),
        )
        assert status2 == Status.UNREACHABLE
        assert reason2 == Reason.BLOCKED
        assert "Security Verification" in (detail2 or "")
