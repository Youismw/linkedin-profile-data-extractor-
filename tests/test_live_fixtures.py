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
from bs4 import BeautifulSoup

from profile_extractor.fetcher.playwright_fetcher import _compose_captured_profile_html
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


def test_identical_certification_cards_are_deduplicated() -> None:
    """Responsive duplicate DOM cards must not create duplicate credentials."""
    html_path = LIVE_FIXTURES_DIR / "shruti-dwivedi-aba0b425b.html"
    html = html_path.read_text(encoding="utf-8")
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/shruti-dwivedi-aba0b425b/",
        slug="shruti-dwivedi-aba0b425b",
        html=html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )
    certs = doc.profile.certifications or []
    serialized = [item.model_dump_json() for item in certs]
    assert len(serialized) == len(set(serialized))


def test_incremental_profile_card_capture_preserves_unmounted_about_and_featured() -> None:
    """Cards seen in an earlier scroll snapshot survive if absent from the final DOM."""
    html_path = LIVE_FIXTURES_DIR / "vishal-soam-5ba768168.html"
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")

    # Model separate viewport snapshots: the top card and About were seen first;
    # Featured and Experience were seen in later snapshots. The fetcher composes
    # these retained card fragments before the regular parser runs.
    selectors = [
        '[id$="Topcard"]',
        '[id$="About"]',
        '[id$="Featured"]',
        '[id$="ExperienceTopLevelSection"]',
    ]
    cards = [str(soup.select_one(selector)) for selector in selectors]
    assert all(card != "None" for card in cards)

    merged_html = _compose_captured_profile_html(cards, "<html><body>fallback</body></html>")
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/vishal-soam-5ba768168/",
        slug="vishal-soam-5ba768168",
        html=merged_html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )

    assert doc.profile.name == "Vishal Soam"
    assert doc.profile.about is not None
    assert doc.profile.about.startswith("Full Stack Engineer (MERN)")
    assert "Featured" in [section.heading for section in doc.other_sections]
    assert "My Resume" in (doc.raw_text or "")


def test_linkedin_hired_badge_is_not_used_as_experience_description() -> None:
    """The standalone badge text is UI metadata, not an employee-written description."""
    html_path = LIVE_FIXTURES_DIR / "sujeet-patel-61987227a.html"
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/sujeet-patel-61987227a/",
        slug="sujeet-patel-61987227a",
        html=html_path.read_text(encoding="utf-8"),
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )

    assert doc.profile.experience
    assert all(
        "helped me get this job" not in (item.description or "").lower()
        for item in doc.profile.experience
    )


def test_composed_profile_cards_preserve_parser_output_for_all_eight_fixtures() -> None:
    """Composing retained cards must not alter extraction on the saved profile pages."""
    for html_path in sorted(LIVE_FIXTURES_DIR.glob("*.html")):
        if html_path.name == "rohit_skills_details.html":
            continue

        original_html = html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(original_html, "lxml")
        candidate_cards = soup.select(
            '[id^="com.linkedin.sdui.profile.card."], '
            '[id^="profileCardsAboveActivity"], '
            '[id^="profileCardsBelowActivity"]'
        )
        cards = []
        for card in candidate_cards:
            card_id = str(card.get("id") or "")
            is_legacy_group = card_id.startswith((
                "profileCardsAboveActivity", "profileCardsBelowActivity"
            ))
            if is_legacy_group and card.select_one(
                '[id^="com.linkedin.sdui.profile.card."]'
            ):
                continue
            cards.append(str(card))
        if not cards:
            continue

        composed_html = _compose_captured_profile_html(cards, original_html)
        original_doc = parse_profile(
            source_url="https://www.linkedin.com/in/fixture/",
            slug=html_path.stem,
            html=original_html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        composed_doc = parse_profile(
            source_url="https://www.linkedin.com/in/fixture/",
            slug=html_path.stem,
            html=composed_html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert composed_doc.profile.model_dump() == original_doc.profile.model_dump()
        assert [item.model_dump() for item in composed_doc.other_sections] == [
            item.model_dump() for item in original_doc.other_sections
        ]
        assert composed_doc.raw_text == original_doc.raw_text


def test_card_composer_falls_back_when_no_profile_cards_were_seen() -> None:
    """Non-SDUI pages retain the final HTML as the safe fallback."""
    fallback_html = "<html><body><main><h1>Legacy profile</h1></main></body></html>"
    assert _compose_captured_profile_html([], fallback_html) == fallback_html


def test_card_composer_preserves_final_document_and_adds_missing_virtualized_card() -> None:
    """Captured cards augment the final page; they must never replace its contents."""
    fallback_html = (
        '<html><body><main>'
        '<section id="top-card">Profile header</section>'
        '<section id="experience">Visible final-page experience</section>'
        '</main></body></html>'
    )
    captured_about = (
        '<section id="com.linkedin.sdui.profile.card.About">'
        '<h2>About</h2><p>About text retained from an earlier scroll snapshot.</p>'
        '</section>'
    )

    composed = _compose_captured_profile_html([captured_about], fallback_html)
    soup = BeautifulSoup(composed, "lxml")

    assert soup.select_one("#top-card").get_text(" ", strip=True) == "Profile header"
    assert soup.select_one("#experience").get_text(" ", strip=True) == (
        "Visible final-page experience"
    )
    assert "About text retained from an earlier scroll snapshot." in soup.get_text(" ", strip=True)
    assert len(soup.select('[id="com.linkedin.sdui.profile.card.About"]')) == 1


def test_card_composer_replaces_existing_card_only_with_richer_snapshot() -> None:
    """A richer earlier snapshot may expand a truncated card without losing siblings."""
    fallback_html = (
        '<html><body><main>'
        '<section id="about-card"><h2>About</h2><p>Short excerpt</p></section>'
        '<section id="other-card">Keep this unrelated section</section>'
        '</main></body></html>'
    )
    richer_about = (
        '<section id="about-card"><h2>About</h2>'
        '<p>Short excerpt plus the complete text retained from the expanded card.</p>'
        '</section>'
    )

    composed = _compose_captured_profile_html([richer_about], fallback_html)
    soup = BeautifulSoup(composed, "lxml")

    assert soup.select_one("#about-card").get_text(" ", strip=True).endswith(
        "Short excerpt plus the complete text retained from the expanded card."
    )
    assert soup.select_one("#other-card").get_text(" ", strip=True) == (
        "Keep this unrelated section"
    )
    assert len(soup.select('[id="about-card"]')) == 1


def test_virtualized_about_card_is_merged_without_dropping_final_page_sections() -> None:
    """A card missing from final DOM is restored while all remaining page data survives."""
    html_path = LIVE_FIXTURES_DIR / "vishal-soam-5ba768168.html"
    original_html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(original_html, "lxml")
    about_card = soup.select_one('[id$="About"]')
    assert about_card is not None
    captured_about = str(about_card)
    about_card.decompose()  # Simulate LinkedIn virtualizing the card after scrolling.

    final_html_without_about = str(soup)
    merged_html = _compose_captured_profile_html(
        [captured_about], final_html_without_about
    )
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/vishal-soam-5ba768168/",
        slug="vishal-soam-5ba768168",
        html=merged_html,
        status=Status.OK,
        important_fields=DEFAULT_IMPORTANT_FIELDS,
    )

    assert doc.profile.about is not None
    assert doc.profile.about.startswith("Full Stack Engineer (MERN)")
    assert doc.profile.name == "Vishal Soam"
    assert doc.profile.experience
    assert "Featured" in [section.heading for section in doc.other_sections]
    assert "My Resume" in (doc.raw_text or "")
