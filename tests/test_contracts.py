"""Tests for Phase 1 data contracts, models, configuration, and logging."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from profile_extractor.config import ExtractorConfig, load_config
from profile_extractor.logging_setup import setup_logging
from profile_extractor.models import (
    ContactInfo,
    EducationItem,
    ExperienceItem,
    FetchResult,
    Meta,
    OtherSection,
    Profile,
    ProfileDocument,
    Reason,
    RunSummary,
    Status,
)


class TestModels:
    """Test suite for data models and contract serialization."""

    def test_model_round_trip_success(self) -> None:
        """Verify full ProfileDocument serializes and deserializes accurately."""
        meta = Meta(
            source_url="https://www.linkedin.com/in/jane-doe-12ab/",
            slug="jane-doe-12ab",
            status=Status.OK,
            reason=None,
            fetched_at="2026-11-04T09:30:00Z",
            missing_fields=["skills"],
        )
        profile = Profile(
            name="Jane Doe",
            headline="Staff Software Engineer",
            location="San Francisco Bay Area",
            about="Passionate distributed systems engineer.",
            experience=[
                ExperienceItem(
                    title="Senior Engineer",
                    company="Acme Corp",
                    dates="2020 - Present",
                    location="Remote",
                    description="Building scalable services.",
                    raw="Senior Engineer at Acme Corp (2020 - Present)",
                )
            ],
            education=[
                EducationItem(
                    school="UC Berkeley",
                    degree="B.S. Computer Science",
                    dates="2016 - 2020",
                    raw="UC Berkeley, B.S. CS 2016 - 2020",
                )
            ],
            skills=None,  # Missing field
        )
        other_sections = [OtherSection(heading="Certifications", text="AWS Solutions Architect")]

        doc = ProfileDocument(meta=meta, profile=profile, other_sections=other_sections)
        serialized = doc.to_serializable_dict()

        # Contract assertion: missing field 'skills' must be omitted from profile dict
        assert "skills" not in serialized["profile"]
        assert serialized["meta"]["missing_fields"] == ["skills"]
        assert serialized["profile"]["name"] == "Jane Doe"
        assert len(serialized["profile"]["experience"]) == 1
        assert serialized["profile"]["experience"][0]["raw"] != ""
        assert serialized["other_sections"][0]["heading"] == "Certifications"

        # JSON round trip
        json_str = json.dumps(serialized)
        deserialized = json.loads(json_str)
        assert deserialized["meta"]["slug"] == "jane-doe-12ab"
        assert deserialized["meta"]["status"] == "ok"
        assert deserialized["profile"]["headline"] == "Staff Software Engineer"

    def test_contact_info_contract(self) -> None:
        """Verify ContactInfo serialization inside ProfileDocument."""
        meta = Meta(
            source_url="https://www.linkedin.com/in/jane-doe/",
            slug="jane-doe",
            status=Status.OK,
        )
        ci = ContactInfo(
            profile_url="https://www.linkedin.com/in/jane-doe",
            email="jane@example.com",
            phone="+1 555 1234",
            websites=["https://github.com/janedoe"],
            raw="Contact info text",
        )
        doc = ProfileDocument(meta=meta, profile=Profile(name="Jane Doe", contact_info=ci))
        serialized = doc.to_serializable_dict()
        assert "contact_info" in serialized["profile"]
        assert serialized["profile"]["contact_info"]["email"] == "jane@example.com"
        assert serialized["profile"]["contact_info"]["phone"] == "+1 555 1234"
        assert serialized["profile"]["contact_info"]["websites"] == ["https://github.com/janedoe"]

        # If contact_info is None, it should not appear in serialized profile dict
        doc_no_ci = ProfileDocument(meta=meta, profile=Profile(name="Jane Doe", contact_info=None))
        serialized_no_ci = doc_no_ci.to_serializable_dict()
        assert "contact_info" not in serialized_no_ci["profile"]

    def test_unreachable_document_contract(self) -> None:
        """Verify unreachable doc has empty profile dict and empty other_sections."""
        meta = Meta(
            source_url="https://www.linkedin.com/in/unknown-user/",
            slug="unknown-user",
            status=Status.UNREACHABLE,
            reason=Reason.NOT_FOUND,
            fetched_at="2026-11-04T09:30:00Z",
            missing_fields=[],
        )
        doc = ProfileDocument(
            meta=meta,
            profile=Profile(name="Should not appear"),
            other_sections=[OtherSection(heading="Extra", text="Data")],
        )

        serialized = doc.to_serializable_dict()
        assert serialized["meta"]["status"] == "unreachable"
        assert serialized["meta"]["reason"] == "not_found"
        assert serialized["profile"] == {}
        assert serialized["other_sections"] == []

    def test_fetch_result_and_run_summary_serialization(self) -> None:
        """Verify FetchResult and RunSummary serialize without errors."""
        fetch_res = FetchResult(
            url="https://www.linkedin.com/in/jane-doe-12ab/",
            status=Status.OK,
            html="<html><body>Jane Doe</body></html>",
        )
        assert fetch_res.status == Status.OK
        assert fetch_res.reason is None

        summary = RunSummary(
            started_at="2026-11-04T09:00:00Z",
            finished_at="2026-11-04T09:05:00Z",
            duration_seconds=300.0,
            rows_read=10,
            rows_valid=8,
            rows_invalid=2,
            duplicates_skipped=1,
            ok_count=7,
            unreachable_count=1,
            unreachable_by_reason={"not_found": 1},
            not_processed_count=0,
            output_dir="output",
        )
        dumped = summary.model_dump()
        assert dumped["rows_read"] == 10
        assert dumped["ok_count"] == 7
        assert dumped["unreachable_by_reason"]["not_found"] == 1


class TestConfig:
    """Test suite for configuration loading, defaults, and validation."""

    def test_config_defaults(self) -> None:
        """Verify default configuration values per section 7.5."""
        cfg = ExtractorConfig()
        assert cfg.input_file == Path("input/profiles.xlsx")
        assert cfg.output_dir == Path("output")
        assert cfg.auth_state_path == Path(".auth/state.json")
        assert cfg.delay_seconds_min == 1.0
        assert cfg.delay_seconds_max == 2.0
        assert cfg.page_timeout_seconds == 30.0
        assert cfg.max_profiles is None
        assert cfg.headless is True
        assert cfg.stop_on_block is True
        assert cfg.log_level == "INFO"
        assert cfg.disextra is False
        assert cfg.scroll_steps == 25
        assert cfg.scroll_step_delay_ms == 250
        assert "name" in cfg.important_fields
        assert "skills" in cfg.important_fields

    def test_load_config_from_file_and_overrides(self, tmp_path: Path) -> None:
        """Verify loading config from a TOML file with CLI overrides."""
        custom_toml = tmp_path / "custom.toml"
        custom_toml.write_text(
            """
            delay_seconds_min = 2.0
            delay_seconds_max = 6.0
            headless = false
            log_level = "DEBUG"
            """,
            encoding="utf-8",
        )

        cfg = load_config(custom_toml, overrides={"delay_seconds_max": 8.0, "max_profiles": 5})
        assert cfg.delay_seconds_min == 2.0
        assert cfg.delay_seconds_max == 8.0
        assert cfg.headless is False
        assert cfg.log_level == "DEBUG"
        assert cfg.max_profiles == 5

    def test_invalid_config_raises_readable_errors(self) -> None:
        """Verify invalid values raise clear ValidationError exceptions."""
        # delay min > max
        with pytest.raises(ValidationError, match="delay_seconds_min"):
            ExtractorConfig(delay_seconds_min=15.0, delay_seconds_max=10.0)

        # negative delay
        with pytest.raises(ValidationError):
            ExtractorConfig(delay_seconds_min=-1.0)

        # invalid log level
        with pytest.raises(ValidationError, match="Invalid log_level"):
            ExtractorConfig(log_level="SUPER_VERBOSE")

        # empty important fields
        with pytest.raises(ValidationError, match="cannot be empty"):
            ExtractorConfig(important_fields=[])

        # scroll_steps out of bounds
        with pytest.raises(ValidationError):
            ExtractorConfig(scroll_steps=-1)
        with pytest.raises(ValidationError):
            ExtractorConfig(scroll_steps=101)

        # scroll_step_delay_ms out of bounds
        with pytest.raises(ValidationError):
            ExtractorConfig(scroll_step_delay_ms=10)
        with pytest.raises(ValidationError):
            ExtractorConfig(scroll_step_delay_ms=10000)


class TestLoggingSetup:
    """Test suite for logging configuration."""

    def test_setup_logging(self, tmp_path: Path) -> None:
        """Ensure logging initializes properly and creates log file."""
        log_dir = tmp_path / "logs"
        logger = setup_logging(level="DEBUG", log_dir=log_dir)
        assert logger.name == "profile_extractor"
        assert (log_dir / "extractor.log").exists()

        logger.info("Test row 1 status: ok")
        content = (log_dir / "extractor.log").read_text(encoding="utf-8")
        assert "Test row 1 status: ok" in content
