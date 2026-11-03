"""Smoke test for profile_extractor package."""

import profile_extractor


def test_package_version() -> None:
    """Ensure package version is defined."""
    assert profile_extractor.__version__ == "0.1.0"
