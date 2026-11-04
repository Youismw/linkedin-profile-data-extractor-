"""Tests verifying architectural fixes for advanced parser, detail page, and data integrity defects.

Covers:
1. Top-level entity selection without descendant duplication.
2. Bullet lists in experience descriptions not becoming fake jobs.
3. Nested section isolation and heading extraction.
4. Detail page parsers with clean DOM contracts.
5. Data quality and completeness comparison in detail crawler.
6. Subpage raw text capture and recovery audit.
7. Placeholder value detection in missing_fields.
8. Semantic location detection across diverse global locations.
"""

from typing import Any
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FetchResult
from profile_extractor.models import (
    ExperienceItem,
    Status,
)
from profile_extractor.orchestrator import (
    _count_field_richness,
    _merge_skill_items,
    run_pipeline,
)
from profile_extractor.parser import (
    _extract_section_heading,
    _get_container_entity_items,
    _is_grouped_experience,
    _is_location_line,
    _is_placeholder_value,
    _parse_experience_items,
    parse_certifications_details,
    parse_education_details,
    parse_experience_details,
    parse_languages_details,
    parse_profile,
    parse_projects_details,
    parse_skills_details,
)


class TestDescendantSelection:
    """Verify parser selects only top-level entity items and ignores matching descendants."""

    def test_filter_nested_entity_collection_items(self) -> None:
        html = """
        <div data-component-type="LazyColumn">
            <div componentkey="entity-collection-item-parent">
                <span>Parent Item</span>
                <div componentkey="entity-collection-item-child">
                    <span>Child Item (e.g. nested skill tag)</span>
                </div>
            </div>
            <div componentkey="entity-collection-item-2">
                <span>Second Top Item</span>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        items = _get_container_entity_items(soup)
        assert len(items) == 2
        texts = [it.get_text() for it in items]
        assert any("Parent Item" in t for t in texts)
        assert any("Second Top Item" in t for t in texts)


class TestBulletListVsMultiRole:
    """Verify experience descriptions with HTML bullet lists never create fake jobs."""

    def test_bullet_list_in_description_remains_single_job(self) -> None:
        html = """
        <li class="artdeco-list__item">
            <div>Software Engineer</div>
            <div>Example Technologies</div>
            <div>Jan 2023 - Present</div>
            <div>London, UK · Hybrid</div>
            <ul>
                <li>Developed backend RESTful APIs in Python and FastAPI.</li>
                <li>Optimized database queries decreasing latency by 40%.</li>
                <li>Implemented secure OAuth2 authentication flow.</li>
            </ul>
        </li>
        """
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("li")
        assert tag is not None

        is_grouped, sub_items = _is_grouped_experience(tag)
        assert is_grouped is False
        assert len(sub_items) == 0

        exps = _parse_experience_items(tag)
        assert len(exps) == 1
        exp = exps[0]
        assert exp.title == "Software Engineer"
        assert exp.company == "Example Technologies"
        assert exp.dates == "Jan 2023 - Present"
        assert exp.location == "London, UK · Hybrid"
        assert exp.description is not None
        assert "Developed backend RESTful APIs" in exp.description
        assert "Optimized database queries" in exp.description
        assert "Implemented secure OAuth2" in exp.description

    def test_genuine_multi_role_company_is_grouped(self) -> None:
        html = """
        <li class="artdeco-list__item">
            <div>Google</div>
            <div>4 yrs 2 mos</div>
            <ul>
                <li>
                    <div>Staff Software Engineer</div>
                    <div>Jan 2023 - Present · 1 yr 2 mos</div>
                    <div>London, UK · Hybrid</div>
                </li>
                <li>
                    <div>Senior Software Engineer</div>
                    <div>Jan 2020 - Dec 2022 · 3 yrs</div>
                    <div>London, UK · On-site</div>
                </li>
            </ul>
        </li>
        """
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("li")
        assert tag is not None

        is_grouped, sub_items = _is_grouped_experience(tag)
        assert is_grouped is True
        assert len(sub_items) == 2

        exps = _parse_experience_items(tag)
        assert len(exps) == 2
        assert exps[0].title == "Staff Software Engineer"
        assert exps[0].company == "Google"
        assert exps[0].location == "London, UK · Hybrid"
        assert exps[1].title == "Senior Software Engineer"
        assert exps[1].company == "Google"
        assert exps[1].location == "London, UK · On-site"


class TestNestedSectionIsolation:
    """Verify parent sections do not steal headings from child sections or duplicate."""

    def test_parent_heading_not_hijacked_by_nested_child_section(self) -> None:
        html = """
        <section id="experience">
            <h2>Experience</h2>
            <section id="sub-eng">
                <h3>Software Engineering Sub-Division</h3>
                <div>Content</div>
            </section>
        </section>
        """
        soup = BeautifulSoup(html, "lxml")
        parent_sec = soup.find("section", id="experience")
        assert parent_sec is not None
        heading = _extract_section_heading(parent_sec)
        assert heading == "Experience"


class TestDetailParsersContracts:
    """Verify detail page parsers enforce clean DOM contracts."""

    def test_parse_skills_details_filters_noise_and_duplicates(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>Python</div>
                    <div>12 endorsements</div>
                </div>
                <div componentkey="entity-collection-item-2">
                    <div>Python</div>
                </div>
                <div componentkey="entity-collection-item-3">
                    <div>Staff Engineer at Google</div>
                </div>
                <div componentkey="entity-collection-item-4">
                    <div>FastAPI</div>
                </div>
                <div componentkey="entity-collection-item-5">
                    <div>Show all 25 skills</div>
                </div>
            </div>
        </main>
        """
        skills = parse_skills_details(html)
        assert skills == ["Python", "FastAPI"]

    def test_parse_education_details(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>Stanford University</div>
                    <div>Master of Science - MS, Computer Science</div>
                    <div>2020 - 2022</div>
                    <div>CGPA: 3.95</div>
                    <div>Activities and societies: Stanford AI Club</div>
                </div>
            </div>
        </main>
        """
        edus = parse_education_details(html)
        assert len(edus) == 1
        assert edus[0].school == "Stanford University"
        assert edus[0].degree == "Master of Science - MS, Computer Science"
        assert edus[0].dates == "2020 - 2022"
        assert edus[0].grade == "CGPA: 3.95"
        assert edus[0].activities == "Activities and societies: Stanford AI Club"

    def test_parse_experience_details(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>Lead Architect</div>
                    <div>DeepMind Technologies</div>
                    <div>Jan 2021 - Present · 3 yrs</div>
                    <div>London, UK · Hybrid</div>
                    <div>Leading next-generation agent architecture research.</div>
                </div>
            </div>
        </main>
        """
        exps = parse_experience_details(html)
        assert len(exps) == 1
        assert exps[0].title == "Lead Architect"
        assert exps[0].company == "DeepMind Technologies"
        assert exps[0].dates == "Jan 2021 - Present · 3 yrs"
        assert exps[0].location == "London, UK · Hybrid"
        assert "agent architecture" in (exps[0].description or "")

    def test_parse_certifications_details(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>AWS Certified Solutions Architect</div>
                    <div>Amazon Web Services (AWS)</div>
                    <div>Issued Sep 2023 · Expires Sep 2026</div>
                    <div>Credential ID AWS-12345</div>
                    <a href="https://aws.amazon.com/verify/12345">Show credential</a>
                </div>
            </div>
        </main>
        """
        certs = parse_certifications_details(html)
        assert len(certs) == 1
        assert certs[0].name == "AWS Certified Solutions Architect"
        assert certs[0].issuer == "Amazon Web Services (AWS)"
        assert certs[0].issue_date == "Issued Sep 2023 · Expires Sep 2026"
        assert certs[0].credential_id == "Credential ID AWS-12345"
        assert certs[0].credential_url == "https://aws.amazon.com/verify/12345"

    def test_parse_projects_details(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>Autonomous Agent Framework</div>
                    <div>Jan 2024 - Present</div>
                    <div>High-performance multi-agent runtime for distributed systems.</div>
                    <div>Skills: Python, Asyncio, gRPC</div>
                    <a href="https://github.com/example/agent">Project Link</a>
                </div>
            </div>
        </main>
        """
        projs = parse_projects_details(html)
        assert len(projs) == 1
        assert projs[0].title == "Autonomous Agent Framework"
        assert projs[0].dates == "Jan 2024 - Present"
        assert "High-performance multi-agent" in (projs[0].description or "")
        assert projs[0].skills == ["Python", "Asyncio", "gRPC"]
        assert projs[0].url == "https://github.com/example/agent"

    def test_parse_languages_details(self) -> None:
        html = """
        <main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>English</div>
                    <div>Native or bilingual proficiency</div>
                </div>
                <div componentkey="entity-collection-item-2">
                    <div>German</div>
                    <div>Professional working proficiency</div>
                </div>
            </div>
        </main>
        """
        langs = parse_languages_details(html)
        assert len(langs) == 2
        assert langs[0].name == "English"
        assert langs[0].proficiency == "Native or bilingual proficiency"
        assert langs[1].name == "German"


class TestPlaceholderValueDetection:
    """Verify placeholder values like '--' or 'N/A' are reported in missing_fields."""

    def test_placeholder_headline_is_flagged_in_missing_fields(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <main>
                <section>
                    <h1>Rohit Chauhan</h1>
                    <div>--</div>
                    <div>Delhi, India</div>
                </section>
            </main>
        </body>
        </html>
        """
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/rohit-test/",
            slug="rohit-test",
            html=html,
            status=Status.OK,
            important_fields=["name", "headline", "location"],
        )
        assert doc.profile.name == "Rohit Chauhan"
        assert doc.profile.headline == "--"
        assert "headline" in doc.meta.missing_fields
        assert "name" not in doc.meta.missing_fields
        assert "location" not in doc.meta.missing_fields

    def test_is_placeholder_value_helper(self) -> None:
        assert _is_placeholder_value("--") is True
        assert _is_placeholder_value("-") is True
        assert _is_placeholder_value("N/A") is True
        assert _is_placeholder_value("none") is True
        assert _is_placeholder_value("") is True
        assert _is_placeholder_value(None) is True
        assert _is_placeholder_value("Senior Engineer") is False


class TestSemanticLocationDetection:
    """Verify global locations are accurately parsed and not confused with sentences."""

    def test_global_locations_recognized(self) -> None:
        assert _is_location_line("London, Greater London, United Kingdom · Hybrid") is True
        assert _is_location_line("Tokyo, Japan · On-site") is True
        assert _is_location_line("Bengaluru, Karnataka, India") is True
        assert _is_location_line("San Francisco Bay Area") is True
        assert _is_location_line("Remote") is True
        assert _is_location_line("Hybrid") is True

    def test_narrative_sentences_rejected_as_location(self) -> None:
        assert _is_location_line("Developed scalable microservices in Go and Python.") is False
        assert _is_location_line("Node.js, MongoDB and +2 skills") is False
        assert _is_location_line("Jan 2020 - Present · 3 yrs") is False


class TestDetailCrawlerQualityReconciliation:
    """Verify subpage crawler reconciles based on quality score and preserves raw text."""

    def test_count_field_richness(self) -> None:
        sparse_item = ExperienceItem(title="Engineer", company="Tech Corp")
        rich_item = ExperienceItem(
            title="Engineer",
            company="Tech Corp",
            dates="2020 - 2022",
            location="Remote",
            description="Built distributed infrastructure.",
        )
        assert _count_field_richness([rich_item]) > _count_field_richness([sparse_item])

    def test_subpage_raw_text_preserved(self, tmp_path: Any) -> None:
        input_file = tmp_path / "candidates.xlsx"
        # Create minimal excel
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile URL"])
        ws.append(["https://www.linkedin.com/in/jane-doe/"])
        wb.save(input_file)

        main_html = """
        <html><body><main>
            <section>
                <h1>Jane Doe</h1>
                <div>Software Engineer</div>
                <div>London, UK</div>
            </section>
            <section id="skills">
                <h2>Skills</h2>
                <a href="/details/skills/">Show all 10 skills</a>
            </section>
        </main></body></html>
        """
        skills_subpage_html = """
        <html><body><main>
            <div data-component-type="LazyColumn">
                <div componentkey="entity-collection-item-1">
                    <div>Python</div>
                </div>
                <div componentkey="entity-collection-item-2">
                    <div>Kubernetes</div>
                </div>
            </div>
        </main></body></html>
        """

        mock_fetcher = MagicMock()
        mock_fetcher.fetch.side_effect = [
            FetchResult(
                url="https://www.linkedin.com/in/jane-doe/",
                status=Status.OK,
                html=main_html,
            ),
            FetchResult(
                url="https://www.linkedin.com/in/jane-doe/details/skills/",
                status=Status.OK,
                html=skills_subpage_html,
            ),
        ]

        cfg = ExtractorConfig(
            input_file=input_file,
            output_dir=tmp_path / "out",
            delay_seconds_min=0,
            delay_seconds_max=0,
        )

        summary = run_pipeline(config=cfg, fetcher=mock_fetcher, sleep_fn=lambda _: None)
        assert summary.rows_valid == 1
        assert summary.ok_count == 1

        json_file = tmp_path / "out" / "profiles" / "jane-doe.json"
        assert json_file.exists()
        import json

        data = json.loads(json_file.read_text(encoding="utf-8"))
        assert "raw_text" in data
        assert "--- Subpage: skills ---" in data["raw_text"]
        assert "Kubernetes" in data["raw_text"]
        assert data["profile"]["skills"] == ["Python", "Kubernetes"]


class TestSkillMergeSafety:
    """Deep skill results must supplement, never replace, main-page skills."""

    def test_merge_preserves_both_sources_and_original_order(self) -> None:
        current = ["Python", "React", "C++"]
        detailed = ["react", "Go", "FastAPI", "PYTHON"]

        assert _merge_skill_items(current, detailed) == [
            "Python", "React", "C++", "Go", "FastAPI"
        ]

    def test_incomplete_details_page_cannot_remove_profile_page_skills(self) -> None:
        current = ["Python", "Rust", "TypeScript"]
        detailed = ["Python"]

        assert _merge_skill_items(current, detailed) == current

    def test_empty_profile_skills_uses_detail_page(self) -> None:
        assert _merge_skill_items(None, ["Python", "Go"]) == ["Python", "Go"]
