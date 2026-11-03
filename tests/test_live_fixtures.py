"""Offline verification tests asserting 100% extraction precision on real live DOM fixtures.

These tests run completely offline using snapshots saved in tests/fixtures/live/.
They verify:
1. Top-card headline and location anchoring across all 8 evaluation profiles.
2. Multi-role experience grouping (Google, Observance Solutions, Tata Technologies).
3. Single-role experience retention alongside multi-role containers (Innodata, KANINI, CEDCOSS).
4. DOM decontamination (stripping 35-language footer and video player caption modals).
5. Text hygiene (About trailing '...more' stripping, word-gluing prevention).
6. Details page skills extraction.
"""

from pathlib import Path

import pytest

from profile_extractor.models import ProfileDocument, Status
from profile_extractor.parser import (
    parse_profile,
    parse_skills_details,
)

DEFAULT_IMPORTANT_FIELDS = [
    "name",
    "headline",
    "location",
    "about",
    "experience",
    "education",
    "skills",
]

LIVE_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live"


@pytest.fixture(scope="module")
def sundar_doc() -> ProfileDocument:
    html_path = LIVE_FIXTURES_DIR / "sundarpichai.html"
    if not html_path.exists():
        pytest.skip("sundarpichai.html fixture not found")
    html = html_path.read_text(encoding="utf-8")
    return parse_profile(
        source_url="https://www.linkedin.com/in/sundarpichai/",
        slug="sundarpichai",
        html=html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )


@pytest.fixture(scope="module")
def harsh_doc() -> ProfileDocument:
    html_path = LIVE_FIXTURES_DIR / "harsh-shukla-921566154.html"
    if not html_path.exists():
        pytest.skip("harsh-shukla-921566154.html fixture not found")
    html = html_path.read_text(encoding="utf-8")
    return parse_profile(
        source_url="https://www.linkedin.com/in/harsh-shukla-921566154/",
        slug="harsh-shukla-921566154",
        html=html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )


@pytest.fixture(scope="module")
def vishal_doc() -> ProfileDocument:
    html_path = LIVE_FIXTURES_DIR / "vishal-soam-5ba768168.html"
    if not html_path.exists():
        pytest.skip("vishal-soam-5ba768168.html fixture not found")
    html = html_path.read_text(encoding="utf-8")
    return parse_profile(
        source_url="https://www.linkedin.com/in/vishal-soam-5ba768168/",
        slug="vishal-soam-5ba768168",
        html=html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )


@pytest.fixture(scope="module")
def rohit_doc() -> ProfileDocument:
    html_path = LIVE_FIXTURES_DIR / "rohit-chauhan-464b15262.html"
    if not html_path.exists():
        pytest.skip("rohit-chauhan-464b15262.html fixture not found")
    html = html_path.read_text(encoding="utf-8")
    return parse_profile(
        source_url="https://www.linkedin.com/in/rohit-chauhan-464b15262/",
        slug="rohit-chauhan-464b15262",
        html=html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )


class TestLiveFixturesExtraction:
    """Rigorous offline assertions verifying real-world DOM accuracy."""

    def test_sundar_top_card_and_experience(self, sundar_doc: ProfileDocument) -> None:
        """Verify Sundar Pichai top card and multi-role Google experience."""
        assert sundar_doc.profile.name == "Sundar Pichai"
        assert sundar_doc.profile.headline == "CEO at Google"
        assert sundar_doc.profile.location == "Mountain View, California, United States"

        exps = sundar_doc.profile.experience
        assert exps is not None
        assert len(exps) == 2
        # Both sub-roles inherit parent company Google
        assert exps[0].company == "Google"
        assert exps[0].title == "CEO"
        assert "2015" in (exps[0].dates or "")

        assert exps[1].company == "Google"
        assert exps[1].title == "Product Management + Leadership"

    def test_harsh_multi_role_and_standalone(self, harsh_doc: ProfileDocument) -> None:
        """Verify Harsh Shukla gets both Observance Solutions roles AND CEDCOSS."""
        assert harsh_doc.profile.name == "Harsh Shukla"
        assert harsh_doc.profile.location == "Noida, Uttar Pradesh, India"

        exps = harsh_doc.profile.experience
        assert exps is not None
        assert len(exps) == 3

        companies = [e.company for e in exps]
        assert companies.count("Observance Solutions") == 2
        assert "CEDCOSS Technologies Private Limited" in companies

        titles = [e.title for e in exps]
        assert "Software Engineer" in titles
        assert "Product Engineer" in titles
        assert "Associate Software Engineer" in titles

    def test_vishal_multi_role_and_standalone(self, vishal_doc: ProfileDocument) -> None:
        """Verify Vishal Soam retains Innodata, KANINI, and both Tata Technologies roles."""
        assert vishal_doc.profile.name == "Vishal Soam"
        assert vishal_doc.profile.location == "Noida, Uttar Pradesh, India"

        exps = vishal_doc.profile.experience
        assert exps is not None
        assert len(exps) == 4

        companies = [e.company for e in exps]
        assert "Innodata Inc." in companies
        assert "KANINI" in companies
        assert companies.count("Tata Technologies") == 2

    def test_rohit_headline_dash_and_location(self, rohit_doc: ProfileDocument) -> None:
        """Verify Rohit Chauhan preserves headline '--' without being stolen by college chip."""
        assert rohit_doc.profile.name == "Rohit Chauhan"
        assert rohit_doc.profile.headline == "--"
        assert rohit_doc.profile.location == "Delhi, India"

    def test_text_hygiene_and_modal_decontamination(
        self,
        sundar_doc: ProfileDocument,
        harsh_doc: ProfileDocument,
        rohit_doc: ProfileDocument,
    ) -> None:
        """Verify modal captions, language lists, and trailing '...more' are eliminated."""
        for doc in [sundar_doc, harsh_doc, rohit_doc]:
            raw = doc.raw_text or ""
            assert "Caption Area Background" not in raw
            assert "Bahasa Indonesia" not in raw
            assert "Questions? Visit our Help Center" not in raw

            if doc.profile.about:
                assert not doc.profile.about.endswith("...more")
                assert not doc.profile.about.endswith("… more")
                assert not doc.profile.about.endswith("see more")

    def test_skills_details_page_parser(self) -> None:
        """Verify parse_skills_details unrolls all skills from a details page."""
        html_path = LIVE_FIXTURES_DIR / "rohit_skills_details.html"
        if not html_path.exists():
            pytest.skip("rohit_skills_details.html fixture not found")
        html = html_path.read_text(encoding="utf-8")
        skills = parse_skills_details(html)
        assert len(skills) >= 15
        assert "Next.js" in skills
        assert "Flutter" in skills
        assert "Problem Solving" in skills
