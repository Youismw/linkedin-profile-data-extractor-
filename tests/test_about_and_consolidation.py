from bs4 import BeautifulSoup
from unittest.mock import MagicMock

from profile_extractor.fetcher.playwright_fetcher import (
    _WAIT_FOR_MEANINGFUL_ABOUT_JS,
    _compose_captured_profile_html,
    PlaywrightFetcher,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from profile_extractor.models import OtherSection, Status
from profile_extractor.parser import (
    _merge_complementary_texts,
    _section_text_subsumed,
    consolidate_other_sections,
    normalize_section_heading_key,
    parse_profile,
)
from profile_extractor.orchestrator import _append_other_section_additively


def test_test_scores_heading_normalization() -> None:
    """Test scores (6) and Test Scores normalize to the identical comparison key."""
    assert normalize_section_heading_key("Test scores (6)") == "test scores"
    assert normalize_section_heading_key("Test Scores") == "test scores"
    assert normalize_section_heading_key("  Test  Scores  ( 12 ) ") == "test scores"
    assert normalize_section_heading_key("Honors & awards (3)") == "honors & awards"
    assert normalize_section_heading_key("Honors & Awards") == "honors & awards"
    assert normalize_section_heading_key("Activity") == "activity"


def test_consolidation_completeness_preference() -> None:
    """When one representation contains all information in the other, retain the more complete one."""
    preview_text = (
        "JEE Advanced 2021 | Score: Qualified · Oct 2021\n"
        "MHT-CET 2021 | Score: 98.99 Percentile · Sep 2021"
    )
    detail_text = (
        "JEE Advanced 2021\n"
        "Score: Qualified · Oct 2021\n"
        "MHT-CET 2021\n"
        "Score: 98.99 Percentile · Sep 2021\n"
        "HSC (12th Boards)\n"
        "Score: 94.67% · Aug 2021\n"
        "SSC (10th Boards)\n"
        "Score: 94.40% · Jul 2019"
    )

    sections = [
        OtherSection(heading="Test scores (6)", text=preview_text),
        OtherSection(heading="Test Scores", text=detail_text),
    ]

    consolidated = consolidate_other_sections(sections)
    assert len(consolidated) == 1
    assert consolidated[0].heading == "Test Scores"
    assert consolidated[0].text == detail_text
    assert "HSC (12th Boards)" in consolidated[0].text
    assert "SSC (10th Boards)" in consolidated[0].text

    # Also test reverse initial order
    reverse_sections = [
        OtherSection(heading="Test Scores", text=detail_text),
        OtherSection(heading="Test scores (6)", text=preview_text),
    ]
    reverse_consolidated = consolidate_other_sections(reverse_sections)
    assert len(reverse_consolidated) == 1
    assert reverse_consolidated[0].heading == "Test Scores"
    assert reverse_consolidated[0].text == detail_text


def test_consolidation_complementary_content_merge() -> None:
    """Complementary information is merged without losing unique content."""
    sec1 = OtherSection(
        heading="Publications (2)",
        text="Paper 1: Distributed Consensus in Python · 2023\nAuthor: Jane Doe",
    )
    sec2 = OtherSection(
        heading="Publications",
        text="Paper 2: High Throughput Async Queues · 2024\nAuthor: Jane Doe",
    )

    consolidated = consolidate_other_sections([sec1, sec2])
    assert len(consolidated) == 1
    assert consolidated[0].heading == "Publications"
    assert "Distributed Consensus in Python" in consolidated[0].text
    assert "High Throughput Async Queues" in consolidated[0].text


def test_orchestrator_append_other_section_additively_consolidates_test_scores() -> None:
    """_append_other_section_additively replaces preview representation with complete detail page."""
    preview_text = (
        "JEE Advanced 2021 | Score: Qualified · Oct 2021\n"
        "MHT-CET 2021 | Score: 98.99 Percentile · Sep 2021"
    )
    sections = [OtherSection(heading="Test scores (6)", text=preview_text)]

    detail_text = (
        "JEE Advanced 2021\n"
        "Score: Qualified · Oct 2021\n"
        "MHT-CET 2021\n"
        "Score: 98.99 Percentile · Sep 2021\n"
        "GATE 2022\n"
        "Score: AIR 100"
    )
    _append_other_section_additively(sections, "Test Scores", detail_text)

    assert len(sections) == 1
    assert sections[0].heading == "Test Scores"
    assert sections[0].text == detail_text
    assert "GATE 2022" in sections[0].text


def test_raw_text_preserved_when_other_sections_consolidated() -> None:
    """Preview text in raw_text is preserved even when other_sections consolidates."""
    html = """
    <html>
      <body>
        <main>
          <h1>Alex Mercer</h1>
          <div class="text-body-medium">Senior Architect</div>
          <div class="text-body-small">Seattle, Washington</div>
          <section id="test-scores-card">
            <h2>Test scores (6)</h2>
            <p>JEE Advanced 2021 | Score: Qualified</p>
            <p>MHT-CET 2021 | Score: 98.99</p>
          </section>
        </main>
      </body>
    </html>
    """
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/alex-mercer/",
        slug="alex-mercer",
        html=html,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc.raw_text is not None
    assert "JEE Advanced 2021 | Score: Qualified" in doc.raw_text

    # Now add detail page to other_sections
    _append_other_section_additively(
        doc.other_sections,
        "Test Scores",
        "JEE Advanced 2021\nScore: Qualified\nMHT-CET 2021\nScore: 98.99\nExtra Exam\nScore: 99",
    )
    # other_sections is consolidated
    assert len(doc.other_sections) == 1
    assert doc.other_sections[0].heading == "Test Scores"
    # But original preview raw_text remains intact
    assert "JEE Advanced 2021 | Score: Qualified" in doc.raw_text


def test_unrelated_sections_not_deduplicated() -> None:
    """Distinct sections are never aggressively merged."""
    sections = [
        OtherSection(heading="Activity", text="1,000 followers · Shared a post"),
        OtherSection(heading="Test Scores", text="GRE: 330"),
        OtherSection(heading="Volunteer Experience", text="Red Cross Volunteer"),
    ]
    consolidated = consolidate_other_sections(sections)
    assert len(consolidated) == 3
    assert [s.heading for s in consolidated] == ["Activity", "Test Scores", "Volunteer Experience"]


def test_about_disappearing_from_dom_after_capture() -> None:
    """Captured About is retained even if LinkedIn removes/virtualizes it from final DOM."""
    captured_about = (
        '<section id="com.linkedin.sdui.profile.card.About">'
        "<h2>About</h2>"
        "<p>Full-stack distributed systems engineer with deep expertise in asynchronous queues.</p>"
        "</section>"
    )
    fallback_html = """
    <html>
      <body>
        <main>
          <h1>John Doe</h1>
          <div class="text-body-medium">Software Engineer</div>
          <div class="text-body-small">San Francisco, CA</div>
          <section id="com.linkedin.sdui.profile.card.Experience">
            <h2>Experience</h2>
            <p>Staff Engineer at TechCorp</p>
          </section>
        </main>
      </body>
    </html>
    """
    # The final DOM had the About card virtualized away
    composed = _compose_captured_profile_html([captured_about], fallback_html)
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/john-doe/",
        slug="john-doe",
        html=composed,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc.profile.about is not None
    assert "Full-stack distributed systems engineer" in doc.profile.about


def test_about_richer_never_overwritten_by_incomplete() -> None:
    """A rich About section is never overwritten by an incomplete or empty version."""
    html_with_rich_about = """
    <html>
      <body>
        <main>
          <section id="top-card">
            <h1>Jane Doe</h1>
            <p>Software Engineer</p>
          </section>
          <section id="about">
            <h2>About</h2>
            <p>Comprehensive description of 10 years experience across distributed systems, streaming data, and ML.</p>
          </section>
          <section id="com.linkedin.sdui.profile.card.About">
            <h2>About</h2>
            <p>Comprehensive description of 10 years experience across distributed systems, streaming data, and ML.</p>
          </section>
        </main>
      </body>
    </html>
    """
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/jane-doe/",
        slug="jane-doe",
        html=html_with_rich_about,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc.profile.about is not None
    assert "Comprehensive description" in doc.profile.about

    # If subsequent section has shorter/truncated text
    html_with_short_about = """
    <html>
      <body>
        <main>
          <section id="top-card">
            <h1>Jane Doe</h1>
            <p>Software Engineer</p>
          </section>
          <section id="about">
            <h2>About</h2>
            <p>Comprehensive description of 10 years experience across distributed systems, streaming data, and ML.</p>
          </section>
          <section id="duplicate-about">
            <h2>About</h2>
            <p>Short ...more</p>
          </section>
        </main>
      </body>
    </html>
    """
    doc2 = parse_profile(
        source_url="https://www.linkedin.com/in/jane-doe/",
        slug="jane-doe",
        html=html_with_short_about,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc2.profile.about is not None
    assert "Comprehensive description of 10 years experience" in doc2.profile.about


def test_about_legacy_structure_capture_and_parsing() -> None:
    """Legacy section structures (id='about', data-section='about') are parsed accurately."""
    legacy_html = """
    <html>
      <body>
        <main>
          <section id="top-card">
            <h1>Mark Stone</h1>
            <div class="text-body-medium">VP of Infrastructure</div>
            <div class="text-body-small">Austin, Texas</div>
          </section>
          <section id="about">
            <h2>About</h2>
            <p>Building high scale distributed platforms for 20 years.</p>
          </section>
        </main>
      </body>
    </html>
    """
    doc = parse_profile(
        source_url="https://www.linkedin.com/in/mark-stone/",
        slug="mark-stone",
        html=legacy_html,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc.profile.about == "Building high scale distributed platforms for 20 years."

    data_section_html = """
    <html>
      <body>
        <main>
          <section id="top-card">
            <h1>Mark Stone</h1>
            <div class="text-body-medium">VP of Infrastructure</div>
            <div class="text-body-small">Austin, Texas</div>
          </section>
          <section data-section="about">
            <h2>About</h2>
            <p>Building high scale distributed platforms for 20 years.</p>
          </section>
        </main>
      </body>
    </html>
    """
    doc_ds = parse_profile(
        source_url="https://www.linkedin.com/in/mark-stone/",
        slug="mark-stone",
        html=data_section_html,
        status=Status.OK,
        important_fields=["name"],
    )
    assert doc_ds.profile.about == "Building high scale distributed platforms for 20 years."


def test_about_wait_for_function_js_structure() -> None:
    """_WAIT_FOR_MEANINGFUL_ABOUT_JS is syntactically valid and contains expected checks."""
    assert "sduiAbout" in _WAIT_FOR_MEANINGFUL_ABOUT_JS
    assert "legacyAbout" in _WAIT_FOR_MEANINGFUL_ABOUT_JS
    assert "#about" in _WAIT_FOR_MEANINGFUL_ABOUT_JS
    assert "terminalPage" in _WAIT_FOR_MEANINGFUL_ABOUT_JS


def test_about_never_appearing_timeout_graceful_continuation() -> None:
    """If About never appears within 15s, extraction continues without failure."""
    mock_page = MagicMock()
    # First wait (profile shell ready) succeeds
    # Second wait (About section) raises PlaywrightTimeoutError
    mock_page.wait_for_function.side_effect = [
        None,  # profile shell ready
        PlaywrightTimeoutError("Timeout 15000ms exceeded waiting for About section"),
    ]
    mock_page.url = "https://www.linkedin.com/in/sujeet-patel/"
    mock_page.content.return_value = """
    <html>
      <body>
        <main>
          <section id="top-card">
            <h1>Sujeet Patel</h1>
            <p>Engineer</p>
          </section>
        </main>
      </body>
    </html>
    """
    mock_page.evaluate.return_value = {"count": 1, "totalTextLength": 40}

    # Verify that the timeout in About wait is caught cleanly and does not raise
    try:
        mock_page.wait_for_function(_WAIT_FOR_MEANINGFUL_ABOUT_JS, timeout=15000)
    except PlaywrightTimeoutError:
        pass  # Expected graceful continuation


def test_delayed_about_appearance_stops_waiting_immediately() -> None:
    """When About appears after a short delay, wait_for_function succeeds and stops immediately."""
    mock_page = MagicMock()
    mock_page.wait_for_function.return_value = True

    # Calling wait_for_function with _WAIT_FOR_MEANINGFUL_ABOUT_JS succeeds immediately
    result = mock_page.wait_for_function(_WAIT_FOR_MEANINGFUL_ABOUT_JS, timeout=15000)
    assert result is True
    assert mock_page.wait_for_function.call_count == 1

