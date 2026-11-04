"""Regression tests for the bounded, additive same-profile detail-page pass."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl

from profile_extractor.config import ExtractorConfig
from profile_extractor.models import (
    ExperienceItem,
    FetchResult,
    Profile,
    Status,
)
from profile_extractor.orchestrator import (
    _append_other_section_additively,
    _discover_profile_detail_pages,
    _restore_profile_snapshot_additively,
    run_pipeline,
)


def test_discovery_only_returns_exact_same_profile_detail_and_activity_routes() -> None:
    html = """
    <html><body><main>
      <a href="https://www.linkedin.com/in/jane-doe/details/test-scores/?trk=profile">Show all</a>
      <a href="/in/jane-doe/details/test-scores/">Duplicate route</a>
      <a href="/in/jane-doe/details/featured/">Featured</a>
      <a href="/in/jane-doe/details/interests/?initialTabId=interest_companies">Companies</a>
      <a href="/in/jane-doe/details/interests/?initialTabId=interest_top_voices">Top voices</a>
      <a href="/in/jane-doe/recent-activity/all/">Activity</a>
      <a href="/in/jane-doe/details/skills/urn:li:skill:123/endorsers/">Endorsers</a>
      <a href="/in/another-person/details/projects/">Other profile</a>
      <a href="https://www.linkedin.com/company/example/">Company</a>
      <a href="https://example.com/in/jane-doe/details/patents/">External</a>
      <a href="/feed/update/urn:li:activity:123/">Post</a>
    </main></body></html>
    """
    pages = _discover_profile_detail_pages(
        html,
        "https://www.linkedin.com/in/jane-doe/",
        "jane-doe",
    )
    assert pages == [
        ("test-scores", "https://www.linkedin.com/in/jane-doe/details/test-scores/"),
        ("featured", "https://www.linkedin.com/in/jane-doe/details/featured/"),
        (
            "interests",
            "https://www.linkedin.com/in/jane-doe/details/interests/?initialTabId=interest_companies",
        ),
        (
            "interests",
            "https://www.linkedin.com/in/jane-doe/details/interests/?initialTabId=interest_top_voices",
        ),
        ("recent-activity", "https://www.linkedin.com/in/jane-doe/recent-activity/all/"),
    ]


def test_discovery_has_a_hard_page_limit() -> None:
    links = "".join(
        f'<a href="/in/jane-doe/details/section-{i}/">Section</a>'
        for i in range(20)
    )
    pages = _discover_profile_detail_pages(
        f"<html><body><main>{links}</main></body></html>",
        "https://www.linkedin.com/in/jane-doe/",
        "jane-doe",
        max_pages=4,
    )
    assert len(pages) == 4


def test_additive_restore_preserves_baseline_records_without_duplicate_records() -> None:
    baseline = Profile(
        experience=[
            ExperienceItem(
                title="Software Engineer",
                company="Example Ltd",
                dates="2022 - 2024",
                raw="Software Engineer | Example Ltd | 2022 - 2024",
            ),
            ExperienceItem(
                title="Product Engineer",
                company="Example Ltd",
                dates="2024 - Present",
                raw="Product Engineer | Example Ltd | 2024 - Present",
            ),
        ],
        skills=["Python", "React"],
    )
    target = Profile(
        experience=[
            ExperienceItem(
                title="Software Engineer",
                company="Example Ltd",
                dates="2022 - 2024",
                description="Built services",
                raw="Software Engineer | Example Ltd | 2022 - 2024 | Built services",
            )
        ],
        skills=["Python", "TypeScript"],
    )

    _restore_profile_snapshot_additively(target, baseline)

    assert target.experience is not None
    assert len(target.experience) == 2
    assert target.experience[0].description == "Built services"
    assert target.skills == ["Python", "TypeScript", "React"]


def test_other_sections_merge_new_lines_without_duplicate_content() -> None:
    from profile_extractor.models import OtherSection

    sections = [OtherSection(heading="Activity", text="Post one\nPost two")]
    _append_other_section_additively(sections, "activity", "Post two\nPost three")
    assert len(sections) == 1
    assert sections[0].text == "Post one\nPost two\nPost three"


def test_unknown_detail_pages_are_added_to_raw_text_and_other_sections(tmp_path: Path) -> None:
    input_file = tmp_path / "profiles.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Profile URL"])
    sheet.append(["https://www.linkedin.com/in/jane-doe/"])
    workbook.save(input_file)

    profile_url = "https://www.linkedin.com/in/jane-doe/"
    scores_url = "https://www.linkedin.com/in/jane-doe/details/test-scores/"
    featured_url = "https://www.linkedin.com/in/jane-doe/details/featured/"
    activity_url = "https://www.linkedin.com/in/jane-doe/recent-activity/all/"

    profile_html = """
    <html><body><main>
      <section id="top-card"><h1>Jane Doe</h1><p>Software Engineer</p><p>Delhi, India</p></section>
      <section id="activity"><h2>Activity</h2><p>Post already on profile</p></section>
      <a href="/in/jane-doe/details/test-scores/?trk=profile">Show all test scores</a>
      <a href="/in/jane-doe/details/test-scores/">Duplicate test scores link</a>
      <a href="/in/jane-doe/details/featured/">Show all Featured</a>
      <a href="/in/jane-doe/recent-activity/all/">Show all Activity</a>
      <a href="/in/someone-else/details/projects/">Other person's projects</a>
    </main></body></html>
    """
    scores_html = """
    <html><body><main><h1>Test scores</h1>
      <article><h2>GRE General Test</h2><p>Verbal: 165</p><p>Quantitative: 170</p></article>
      <article><h2>TOEFL iBT</h2><p>Score: 115</p></article>
    </main></body></html>
    """
    featured_html = """
    <html><body><main><h1>Featured</h1>
      <article><h2>Research presentation</h2><p>Technical conference talk</p></article>
    </main></body></html>
    """
    activity_html = """
    <html><body><main><h1>Activity</h1>
      <article>Full timeline post</article><article>Another timeline post</article>
    </main></body></html>
    """

    class RouteFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.pages = {
                profile_url: profile_html,
                scores_url: scores_html,
                featured_url: featured_html,
                activity_url: activity_html,
            }

        def fetch(self, url: str) -> FetchResult:
            self.calls.append(url)
            if url not in self.pages:
                raise AssertionError(f"Unexpected URL fetched: {url}")
            return FetchResult(url=url, status=Status.OK, html=self.pages[url])

        def close(self) -> None:
            pass

    fetcher = RouteFetcher()
    config = ExtractorConfig(
        input_file=input_file,
        output_dir=tmp_path / "output",
        important_fields=["name"],
        deep_details=True,
        delay_seconds_min=0,
        delay_seconds_max=0,
        stop_on_block=False,
    )
    summary = run_pipeline(config, fetcher, sleep_fn=lambda _: None)
    assert summary.ok_count == 1
    assert fetcher.calls.count(scores_url) == 1
    assert fetcher.calls.count(featured_url) == 1
    assert fetcher.calls.count(activity_url) == 1
    assert not any("someone-else" in call for call in fetcher.calls)

    output_file = tmp_path / "output" / "profiles" / "jane-doe.json"
    output = json.loads(output_file.read_text(encoding="utf-8"))
    other_sections = output["other_sections"]

    scores = next(item for item in other_sections if item["heading"] == "Test scores")
    featured = next(item for item in other_sections if item["heading"] == "Featured")
    activity = next(item for item in other_sections if item["heading"] == "Activity")
    assert "GRE General Test" in scores["text"]
    assert "TOEFL iBT" in scores["text"]
    assert "Research presentation" in featured["text"]
    assert "Post already on profile" in activity["text"]
    assert "Full timeline post" in activity["text"]
    assert output["raw_text"].count(scores_url) == 1
    assert output["raw_text"].count(featured_url) == 1
    assert "GRE General Test" in output["raw_text"]
    assert "Research presentation" in output["raw_text"]
    assert "Full timeline post" in output["raw_text"]


def test_legacy_known_page_is_not_fetched_again_by_supplemental_pass(tmp_path: Path) -> None:
    input_file = tmp_path / "profiles.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Profile URL"])
    sheet.append(["https://www.linkedin.com/in/legacy-user/"])
    workbook.save(input_file)

    profile_url = "https://www.linkedin.com/in/legacy-user/"
    skills_url = "https://www.linkedin.com/in/legacy-user/details/skills/"
    profile_html = """
    <html><body><main><h1>Legacy User</h1><p>Engineer</p><p>Delhi</p>
      <a href="/in/legacy-user/details/skills/">Show all skills</a>
    </main></body></html>
    """
    skills_html = """
    <html><body><main><h1>Skills</h1><div data-component-type="LazyColumn">
      <div componentkey="entity-collection-item-1"><span>Python</span></div>
      <div componentkey="entity-collection-item-2"><span>Rust</span></div>
    </div></main></body></html>
    """

    class RouteFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, url: str) -> FetchResult:
            self.calls.append(url)
            page = profile_html if url == profile_url else skills_html if url == skills_url else None
            if page is None:
                raise AssertionError(f"Unexpected URL fetched: {url}")
            return FetchResult(url=url, status=Status.OK, html=page)

        def close(self) -> None:
            pass

    fetcher = RouteFetcher()
    config = ExtractorConfig(
        input_file=input_file,
        output_dir=tmp_path / "output",
        important_fields=["name"],
        deep_details=True,
        delay_seconds_min=0,
        delay_seconds_max=0,
        stop_on_block=False,
    )
    summary = run_pipeline(config, fetcher, sleep_fn=lambda _: None)
    assert summary.ok_count == 1
    assert fetcher.calls.count(skills_url) == 1
    output = json.loads(
        (tmp_path / "output" / "profiles" / "legacy-user.json").read_text(encoding="utf-8")
    )
    assert "Python" in output["profile"]["skills"]
    assert "Rust" in output["profile"]["skills"]
