"""Fetcher package: protocols, implementations, detection, and login helper."""

from collections.abc import Mapping
from typing import Protocol

from profile_extractor.fetcher.detection import detect_outcome
from profile_extractor.fetcher.login import is_session_expired, run_interactive_login
from profile_extractor.fetcher.playwright_fetcher import PlaywrightFetcher
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.urls import extract_slug


class Fetcher(Protocol):
    """Protocol defining the interface for fetching LinkedIn profiles."""

    def fetch(self, url: str) -> FetchResult:
        """Fetch the page at the given canonical profile URL."""
        ...

    def close(self) -> None:
        """Release underlying resources (e.g. browser context, session)."""
        ...


class FakeFetcher:
    """Configurable test fetcher that returns pre-arranged outcomes or raises exceptions."""

    def __init__(
        self,
        outcomes: Mapping[str, FetchResult | Exception] | None = None,
        default_status: Status = Status.OK,
    ) -> None:
        self.outcomes: Mapping[str, FetchResult | Exception] = outcomes or {}
        self.default_status = default_status
        self.fetched_urls: list[str] = []
        self.is_closed = False

    def fetch(self, url: str) -> FetchResult:
        """Simulate fetching by looking up configured outcome for slug."""
        self.fetched_urls.append(url)
        slug = extract_slug(url)

        if slug in self.outcomes:
            outcome = self.outcomes[slug]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        if self.default_status == Status.OK:
            return FetchResult(
                url=url,
                status=Status.OK,
                html=f"<html><head><title>{slug}</title></head><body><h1>{slug}</h1></body></html>",
            )

        return FetchResult(
            url=url,
            status=Status.UNREACHABLE,
            reason=Reason.NOT_FOUND,
            detail=f"Fake not found for {slug}",
        )

    def close(self) -> None:
        """Mark fetcher as closed."""
        self.is_closed = True


__all__ = [
    "Fetcher",
    "FakeFetcher",
    "PlaywrightFetcher",
    "FetchResult",
    "detect_outcome",
    "is_session_expired",
    "run_interactive_login",
]
