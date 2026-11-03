"""Tests for Phase 6: HTML profile parsing, rules, coverage, and fixtures."""

from pathlib import Path

from profile_extractor.models import Reason, Status
from profile_extractor.parser import parse_profile

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_IMPORTANT_FIELDS = [
    "name",
    "headline",
    "location",
    "about",
    "experience",
    "education",
    "skills",
]


class TestParser:
    """Test suite for DOM parsing, section routing, and missing fields detection."""

    def test_full_profile_fixture(self) -> None:
        """Verify complete extraction from full_profile.html including Unicode characters."""
        html = (FIXTURES_DIR / "full_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.meta.reason is None
        assert doc.meta.missing_fields == []

        # Name with Unicode
        assert doc.profile.name == "Renée Björk-Müller"
        assert "Principal Software Architect" in (doc.profile.headline or "")
        assert "Munich" in (doc.profile.location or "")

        # About
        assert doc.profile.about is not None
        assert "distributed systems" in doc.profile.about.lower()

        # Experience
        assert doc.profile.experience is not None
        assert len(doc.profile.experience) == 2
        exp1 = doc.profile.experience[0]
        assert exp1.title == "Principal Architect"
        assert exp1.company == "Global Tech AG"
        assert exp1.raw != ""
        assert "Global Tech AG" in exp1.raw

        # Education
        assert doc.profile.education is not None
        assert len(doc.profile.education) == 1
        edu = doc.profile.education[0]
        assert edu.school == "Technical University of Munich"
        assert edu.degree == "M.S. in Computer Science"
        assert edu.raw != ""

        # Skills
        assert doc.profile.skills is not None
        assert "Distributed Systems" in doc.profile.skills
        assert "Python" in doc.profile.skills

    def test_sparse_profile_fixture(self) -> None:
        """Verify sparse profile accurately computes missing_fields and contract serialization."""
        html = (FIXTURES_DIR / "sparse_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/john-minimal/",
            slug="john-minimal",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.profile.name == "John Minimal"
        assert doc.profile.headline == "Independent IT Consultant"

        # Missing fields check
        missing = set(doc.meta.missing_fields)
        expected_missing = {"location", "about", "experience", "education", "skills"}
        assert expected_missing.issubset(missing)

        # Contract assertion: missing fields are omitted from serialized profile dict
        serialized = doc.to_serializable_dict()
        for field in expected_missing:
            assert field not in serialized["profile"]

    def test_unusual_sections_land_in_other_sections(self) -> None:
        """Verify unmapped sections land cleanly in other_sections without data loss."""
        html = (FIXTURES_DIR / "unusual_section_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/alice-wonderland/",
            slug="alice-wonderland",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.profile.name == "Alice Wonderland"
        assert len(doc.other_sections) >= 3

        headings = [sec.heading.lower() for sec in doc.other_sections]
        assert any("volunteer" in h for h in headings)
        assert any("patent" in h or "publication" in h for h in headings)
        assert any("languages" in h for h in headings)

        # Check content preserved
        volunteer_sec = next(s for s in doc.other_sections if "volunteer" in s.heading.lower())
        assert "Girls Who Code" in volunteer_sec.text

    def test_idempotent_parsing(self) -> None:
        """Verify identical HTML produces identical output apart from fetched_at timestamp."""
        html = (FIXTURES_DIR / "full_profile.html").read_text(encoding="utf-8")

        doc1 = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        doc2 = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        dict1 = doc1.to_serializable_dict()
        dict2 = doc2.to_serializable_dict()

        # Apart from fetched_at, all keys and values must match exactly
        dict1["meta"]["fetched_at"] = "FIXED"
        dict2["meta"]["fetched_at"] = "FIXED"
        assert dict1 == dict2

    def test_parse_failed_when_no_name_and_headline(self) -> None:
        """Verify parse_failed is returned when neither name nor headline can be found."""
        blank_html = "<html><body><div>Just some random text without headers</div></body></html>"
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/empty-user/",
            slug="empty-user",
            html=blank_html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.UNREACHABLE
        assert doc.meta.reason == Reason.PARSE_FAILED
        assert doc.profile.name is None
        assert doc.to_serializable_dict()["profile"] == {}

