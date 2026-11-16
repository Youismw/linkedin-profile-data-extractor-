"""Tests for layout drift detection, 3-profile alarm pause, and offline rework kit."""

import json
from pathlib import Path

import openpyxl
import pytest

from profile_extractor.cli import main
from profile_extractor.config import ExtractorConfig
from profile_extractor.drift_report import (
    reparse_single_html_bundle,
    split_saved_raw_html,
)
from profile_extractor.fetcher import FakeFetcher
from profile_extractor.health import check_profile_health
from profile_extractor.models import (
    CertificationItem,
    EducationItem,
    FetchResult,
    LanguageItem,
    Meta,
    OtherSection,
    Profile,
    ProfileDocument,
    Status,
)
from profile_extractor.orchestrator import run_pipeline


def _make_drifted_html(name: str) -> str:
    return f"""
    <html><body><main>
      <section>
        <h1>{name}</h1>
        <div>· 3rd</div>
        <div>Delhi, India</div>
        <div>Contact info</div>
      </section>
      <section>
        <h2>Skills (12)</h2>
        <div data-component-type="LazyColumn">
          <div componentkey="entity-collection-item-1">
            <div>2 experiences at Acme</div>
          </div>
        </div>
      </section>
    </main></body></html>
    """


def _make_clean_html(name: str) -> str:
    return f"""
    <html><body><main>
      <section>
        <h1>{name}</h1>
        <div>Senior Software Engineer at Acme</div>
        <div>Delhi, India</div>
        <div>Contact info</div>
      </section>
      <section>
        <h2>About</h2>
        <p>Distributed systems engineer building reliable data pipelines.</p>
      </section>
      <section>
        <h2>Experience</h2>
        <ul><li>Senior Software Engineer | Acme Corp | Jan 2022 - Present</li></ul>
      </section>
      <section>
        <h2>Education</h2>
        <ul><li>BITS Pilani | B.E. Computer Science | 2017 - 2021</li></ul>
      </section>
      <section>
        <h2>Skills</h2>
        <ul><li>Python</li><li>SQL</li></ul>
      </section>
    </main></body></html>
    """


def test_clean_profile_omits_parse_warnings() -> None:
    """Clean ProfileDocument produces 0 health warnings and omits parse_warnings in JSON."""
    doc = ProfileDocument(
        meta=Meta(
            source_url="https://www.linkedin.com/in/clean-user/",
            slug="clean-user",
            status=Status.OK,
        ),
        profile=Profile(
            name="Clean User",
            headline="Senior Software Engineer",
            location="Bengaluru, India",
            skills=["Python", "SQL"],
            education=[EducationItem(school="BITS Pilani", degree="B.E.", dates="2017 - 2021")],
        ),
    )

    assert check_profile_health(doc) == []
    serialized = doc.to_serializable_dict()
    assert "parse_warnings" not in serialized["meta"]


def test_health_checks_detect_all_drift_modes() -> None:
    """Verify check_profile_health detects headline, header, skill, squashed, and count drift."""
    secret_name = "ConfidentialCandidateName"
    doc = ProfileDocument(
        meta=Meta(
            source_url="https://www.linkedin.com/in/drifted-user/",
            slug="drifted-user",
            status=Status.OK,
        ),
        profile=Profile(
            name=secret_name,
            headline="· 3rd",
            education=[
                EducationItem(school="Education", raw="Education"),
                EducationItem(
                    school="Example Institute of Technology",
                    degree="B.Tech",
                    raw=(
                        "Example Institute of Technology | B.Tech | Aug 2021 – Jul 2025 | "
                        "High School | 12th | Jul 2019 – Apr 2021"
                    ),
                ),
            ],
            certifications=[
                CertificationItem(
                    name="Licenses & certifications",
                    issuer="SQL Course",
                    raw="SQL Course | Issued Jul 2025 | Python Course | Issued Jun 2025",
                )
            ],
            languages=[
                LanguageItem(name="Languages", raw="Languages"),
                LanguageItem(
                    name="English",
                    proficiency="Full professional proficiency",
                    raw=(
                        "English | Full professional proficiency | "
                        "Hindi | Native or bilingual proficiency"
                    ),
                ),
            ],
            skills=[
                "2 experiences at SAP",
                "Database Intern at Dixon",
                "Passed LinkedIn Skill Assessment",
            ],
        ),
        other_sections=[
            OtherSection(heading="Licenses & certifications (9)", text="SQL Course")
        ],
        raw_text=(
            "Show all 4 educations\n"
            "Licenses & certifications (9)\n"
            "Projects (5)\n"
            "Skills (31)\n"
            "Languages (3)\n\n"
            "--- Subpage: certifications ---\nSQL Course"
        ),
    )

    diagnostics = check_profile_health(doc)
    pairs = {(d.section, d.code) for d in diagnostics}

    assert ("headline", "junk_headline") in pairs
    assert ("education", "junk_header_item") in pairs
    assert ("education", "squashed_items") in pairs
    assert ("certifications", "junk_header_item") in pairs
    assert ("certifications", "squashed_items") in pairs
    assert ("certifications", "section_routed_to_other") in pairs
    assert ("certifications", "count_mismatch") in pairs
    assert ("languages", "junk_header_item") in pairs
    assert ("languages", "squashed_items") in pairs
    assert ("skills", "junk_skill_subheading") in pairs
    assert ("projects", "count_mismatch") in pairs

    for diag in diagnostics:
        assert secret_name not in diag.message


def test_run_pipeline_pauses_at_3_flagged_profiles_and_writes_drift_report(
    tmp_path: Path,
) -> None:
    """Pipeline pauses after 3 flagged profiles, saves report, and stops if user declines."""
    input_xlsx = tmp_path / "candidates.xlsx"
    output_dir = tmp_path / "output"

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["URL"])
    for i in range(1, 5):
        ws.append([f"https://www.linkedin.com/in/user-{i}/"])
    wb.save(input_xlsx)

    outcomes = {
        f"user-{i}": FetchResult(
            url=f"https://www.linkedin.com/in/user-{i}/",
            status=Status.OK,
            html=_make_drifted_html(f"User {i}"),
        )
        for i in range(1, 5)
    }

    prompt_calls: list[tuple[int, Path]] = []

    def stop_on_drift(flagged: list[dict[str, object]], report_dir: Path) -> bool:
        prompt_calls.append((len(flagged), report_dir))
        return False

    config = ExtractorConfig(
        input_file=input_xlsx,
        output_dir=output_dir,
        delay_seconds_min=0.0,
        delay_seconds_max=0.0,
    )
    summary = run_pipeline(
        config,
        FakeFetcher(outcomes=outcomes),
        sleep_fn=lambda _: None,
        drift_prompt_fn=stop_on_drift,
    )

    assert len(prompt_calls) == 1
    assert prompt_calls[0][0] == 3
    assert summary.ok_count == 3
    assert summary.not_processed_count == 1
    assert summary.parse_warnings_count == 3
    assert summary.drift_report_dir is not None

    report_dir = Path(summary.drift_report_dir)
    assert (report_dir / "WHAT_HAPPENED.txt").is_file()
    assert (report_dir / "profiles.json").is_file()
    assert list((report_dir / "failed_sections").glob("*.html"))

    raw_html_files = list((output_dir / "_raw_html").glob("*.html"))
    assert len(raw_html_files) == 3

    saved_profile = json.loads(
        (output_dir / "profiles" / "user-1.json").read_text(encoding="utf-8")
    )
    assert saved_profile["meta"]["parse_warnings"]


def test_run_pipeline_continues_when_prompt_returns_true(tmp_path: Path) -> None:
    """Pipeline finishes remaining rows when user confirms continue on drift alarm."""
    input_xlsx = tmp_path / "candidates.xlsx"
    output_dir = tmp_path / "output"

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["URL"])
    for i in range(1, 5):
        ws.append([f"https://www.linkedin.com/in/cont-{i}/"])
    wb.save(input_xlsx)

    outcomes = {
        f"cont-{i}": FetchResult(
            url=f"https://www.linkedin.com/in/cont-{i}/",
            status=Status.OK,
            html=_make_drifted_html(f"User {i}"),
        )
        for i in range(1, 5)
    }

    config = ExtractorConfig(
        input_file=input_xlsx,
        output_dir=output_dir,
        delay_seconds_min=0.0,
        delay_seconds_max=0.0,
    )
    summary = run_pipeline(
        config,
        FakeFetcher(outcomes=outcomes),
        sleep_fn=lambda _: None,
        drift_prompt_fn=lambda _flagged, _dir: True,
    )

    assert summary.ok_count == 4
    assert summary.not_processed_count == 0
    assert summary.parse_warnings_count == 4


def test_offline_rework_kit_reparse_add_fixture_and_reparse_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify split_saved_raw_html, reparse, add-fixture, and reparse-run CLI workflows."""
    bundle = (
        _make_clean_html("Sample User")
        + "\n\n<!-- === SUBPAGE: https://www.linkedin.com/in/sample-user/details/skills/ === -->\n"
        "<html><body><main><ul><li>Python</li><li>SQL</li></ul></main></body></html>"
    )
    main_html, subpages = split_saved_raw_html(bundle)
    assert "Sample User" in main_html
    assert "skills" in subpages

    doc, diags = reparse_single_html_bundle(
        "sample-user", "https://www.linkedin.com/in/sample-user/", bundle
    )
    assert diags == []
    assert doc.profile.skills == ["Python", "SQL"]

    # Stage an output run with a drifted profile
    input_xlsx = tmp_path / "in.xlsx"
    output_dir = tmp_path / "run_out"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["URL"])
    ws.append(["https://www.linkedin.com/in/sample-user/"])
    wb.save(input_xlsx)

    config = ExtractorConfig(
        input_file=input_xlsx,
        output_dir=output_dir,
        delay_seconds_min=0.0,
        delay_seconds_max=0.0,
    )
    summary = run_pipeline(
        config,
        FakeFetcher(
            outcomes={
                "sample-user": FetchResult(
                    url="https://www.linkedin.com/in/sample-user/",
                    status=Status.OK,
                    html=_make_drifted_html("Sample User"),
                )
            }
        ),
        sleep_fn=lambda _: None,
    )
    assert summary.parse_warnings_count == 1
    report_dir = Path(str(summary.drift_report_dir))

    # 1. reparse fails while drifted HTML triggers warnings
    assert main(["reparse", str(report_dir)]) == 1

    # 2. add-fixture creates local test inside tmp_path (zero repo mutation)
    fixture_dir = tmp_path / "private_fixture"
    assert main(["add-fixture", str(report_dir), "--fixture-dir", str(fixture_dir)]) == 0
    assert (fixture_dir / "test_drift_fixture.py").is_file()
    assert (fixture_dir / "profiles.json").is_file()
    assert (fixture_dir / "html" / "sample-user.html").is_file()

    # 3. Replace saved raw HTML with clean HTML to simulate parser fix, then reparse & reparse-run
    (output_dir / "_raw_html" / "sample-user.html").write_text(
        _make_clean_html("Sample User"), encoding="utf-8"
    )
    assert main(["reparse", str(report_dir)]) == 0
    assert main(["reparse-run", str(output_dir)]) == 0

    regenerated = json.loads(
        (output_dir / "profiles" / "sample-user.json").read_text(encoding="utf-8")
    )
    assert regenerated["profile"]["headline"] == "Senior Software Engineer at Acme"
    assert "parse_warnings" not in regenerated["meta"]


def test_post_sdui_update_dom_structures_extract_cleanly() -> None:
    """Verify post-SDUI DOM layouts (no entity-collection-item, nested skill listitems, squashed subpages)."""
    main_html = """
    <html><body><main>
      <section>
        <h1>Avnish Kotambkar</h1>
        <span>He/Him</span>
        <span>· 3rd</span>
        <div>Software Engineer at Innodata Inc.</div>
        <div>Noida, Uttar Pradesh, India</div>
        <div>Contact info</div>
      </section>
      <section>
        <div>
          <div><h2>Education</h2></div>
          <div>
            <div>
              <p>Birla Institute of Technology and Science, Pilani</p>
              <p>Bachelor of Engineering - B.E, Electrical and Electronics Engineering</p>
              <p>Aug 2021 – Jul 2026</p>
            </div>
          </div>
        </div>
      </section>
      <section>
        <div>
          <div><h2>Skills (3)</h2></div>
          <div>
            <div>
              <p>Node.js</p>
              <div role="list"><div role="listitem">Software Engineer at Innodata Inc.</div></div>
            </div>
            <hr role="presentation"/>
            <div>
              <p>MongoDB</p>
              <div role="list"><div role="listitem">2 experiences at MSD and 1 other company</div></div>
            </div>
            <hr role="presentation"/>
            <div>
              <p>PySpark</p>
              <div role="list"><div role="listitem">Data Engineering &amp; Analytics Intern at MSD</div></div>
            </div>
          </div>
        </div>
      </section>
    </main></body></html>
    """
    edu_subpage = """
    <html><body><main>
      <div data-component-type="LazyColumn">
        <div><p>Education</p></div>
        <div>
          <div>
            <p>Birla Institute of Technology and Science, Pilani</p>
            <p>Bachelor of Engineering - B.E, Electrical and Electronics Engineering</p>
            <p>Aug 2021 – Jul 2026</p>
            <p>Master of Science - M.Sc, Physics</p>
            <p>Birla Institute of Technology and Science, Pilani</p>
            <p>Master of Science - M.Sc, Physics</p>
            <p>Aug 2021 – Jul 2026</p>
            <p>Dr. Ambedkar College, Nagpur</p>
            <p>Passed 12th Standard State Board Exam (MSBSHSE)</p>
            <p>Jul 2019 – Apr 2021</p>
            <p>97.5% in 12th Std State Board Exam</p>
            <p>Montfort Secondary School</p>
            <p>Passed 10th Standard Board Exam (CBSE)</p>
            <p>Jun 2008 – Jun 2019</p>
          </div>
        </div>
      </div>
    </main></body></html>
    """
    cert_subpage = """
    <html><body><main>
      <div data-component-type="LazyColumn">
        <div>
          <p>Licenses &amp; certifications</p>
          <p>The Structured Query Language (SQL)</p>
          <p>Coursera</p>
          <p>Issued Jul 2025</p>
          <p>Skills:</p>
          <p>PostgreSQL, SQL</p>
          <p>Cash Flow Management</p>
          <p>Coursera</p>
          <p>Issued Jun 2025</p>
          <p>Microsoft Excel</p>
          <p>Udemy</p>
          <p>Issued Mar 2025</p>
        </div>
      </div>
    </main></body></html>
    """
    lang_subpage = """
    <html><body><main>
      <div data-component-type="LazyColumn">
        <div><p>Languages</p></div>
        <div>
          <p>English</p>
          <p>Full professional proficiency</p>
          <p>Hindi</p>
          <p>Native or bilingual proficiency</p>
          <p>Marathi</p>
          <p>Native or bilingual proficiency</p>
        </div>
      </div>
    </main></body></html>
    """
    bundle = (
        main_html
        + "\n\n<!-- === SUBPAGE: https://www.linkedin.com/in/avnish-kotambkar/details/education/ === -->\n"
        + edu_subpage
        + "\n\n<!-- === SUBPAGE: https://www.linkedin.com/in/avnish-kotambkar/details/certifications/ === -->\n"
        + cert_subpage
        + "\n\n<!-- === SUBPAGE: https://www.linkedin.com/in/avnish-kotambkar/details/languages/ === -->\n"
        + lang_subpage
    )
    doc, diags = reparse_single_html_bundle(
        "avnish-kotambkar", "https://www.linkedin.com/in/avnish-kotambkar/", bundle
    )
    assert diags == []
    assert doc.profile.headline == "Software Engineer at Innodata Inc."
    assert doc.profile.skills == ["Node.js", "MongoDB", "PySpark"]
    assert len(doc.profile.education or []) == 4
    assert len(doc.profile.certifications or []) == 3
    assert len(doc.profile.languages or []) == 3


def test_merged_raw_variants_do_not_trigger_squashed_items() -> None:
    """Newline-joined main+subpage raw variants of the same entry must not trigger squashed_items."""
    doc = ProfileDocument(
        meta=Meta(
            source_url="https://www.linkedin.com/in/merged-user/",
            slug="merged-user",
            status=Status.OK,
        ),
        profile=Profile(
            name="Merged User",
            headline="Software Engineer",
            education=[
                EducationItem(
                    school="Example University",
                    degree="B.E.",
                    dates="Aug 2021 – Jul 2026",
                    raw=(
                        "Example University | B.E. | Aug 2021 – Jul 2026 | Skills: | MATLAB, +1 skill\n"
                        "Example University | B.E. | Aug 2021 – Jul 2026 | MATLAB and +1 skill"
                    ),
                )
            ],
            certifications=[
                CertificationItem(
                    name="SQL Course",
                    issuer="Coursera",
                    issue_date="Issued Jul 2025",
                    raw=(
                        "SQL Course | Coursera | Issued Jul 2025 | Skills: | PostgreSQL\n"
                        "SQL Course | Coursera | Issued Jul 2025 | PostgreSQL"
                    ),
                )
            ],
            languages=[
                LanguageItem(
                    name="English",
                    proficiency="Full professional proficiency",
                    raw=(
                        "English | Full professional proficiency\n"
                        "English | Full professional proficiency | Verified"
                    ),
                )
            ],
        ),
    )

    assert check_profile_health(doc) == []
