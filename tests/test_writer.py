"""Tests for Phase 3: Atomic writers, Unicode support, reports, and summaries."""

import json
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from profile_extractor.models import (
    Meta,
    Profile,
    ProfileDocument,
    Reason,
    RunSummary,
    Status,
    UnreachableRecord,
)
from profile_extractor.writer import (
    write_profile_json,
    write_run_summary,
    write_unreachable_report,
)


def _make_sample_doc(slug: str, name: str) -> ProfileDocument:
    return ProfileDocument(
        meta=Meta(
            source_url=f"https://www.linkedin.com/in/{slug}/",
            slug=slug,
            status=Status.OK,
            missing_fields=[],
        ),
        profile=Profile(name=name, headline="Software Engineer", location="München, Deutschland"),
        other_sections=[],
    )


class TestWriter:
    """Test suite covering atomic file writers and output generation."""

    def test_unicode_round_trip(self, tmp_path: Path) -> None:
        """Verify non-ASCII / Unicode characters are preserved natively without ASCII escaping."""
        doc = _make_sample_doc("münchen-dev", "René François Müller — 東京")
        written_path = write_profile_json(doc, tmp_path)

        assert written_path.exists()
        raw_text = written_path.read_text(encoding="utf-8")
        assert "René François Müller — 東京" in raw_text
        assert "\\u" not in raw_text  # ensure_ascii=False

        parsed = json.loads(raw_text)
        assert parsed["profile"]["name"] == "René François Müller — 東京"
        assert parsed["profile"]["location"] == "München, Deutschland"

    def test_atomic_overwrite(self, tmp_path: Path) -> None:
        """Verify repeated writes cleanly overwrite target file."""
        doc1 = _make_sample_doc("alex-smith", "Version 1 Name")
        written_path1 = write_profile_json(doc1, tmp_path)
        assert "Version 1 Name" in written_path1.read_text(encoding="utf-8")

        doc2 = _make_sample_doc("alex-smith", "Version 2 Name")
        written_path2 = write_profile_json(doc2, tmp_path)
        assert written_path1 == written_path2
        assert "Version 2 Name" in written_path2.read_text(encoding="utf-8")
        assert "Version 1 Name" not in written_path2.read_text(encoding="utf-8")

    def test_simulated_failure_leaves_old_file_intact(self, tmp_path: Path) -> None:
        """Verify a failure during write leaves the previous file untouched with no corruption."""
        doc = _make_sample_doc("fail-test", "Original Safe Content")
        target_path = write_profile_json(doc, tmp_path)
        original_content = target_path.read_text(encoding="utf-8")

        doc_new = _make_sample_doc("fail-test", "Corrupted Partial Content")

        # Simulate exception during os.replace
        with patch("os.replace", side_effect=OSError("Disk write simulated failure")):
            with pytest.raises(OSError, match="Disk write simulated failure"):
                write_profile_json(doc_new, tmp_path)

        # The original target file must be completely unharmed
        assert target_path.read_text(encoding="utf-8") == original_content

        # No partial or temporary files left behind
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_unreachable_report_columns_and_rows(self, tmp_path: Path) -> None:
        """Verify unreachable_profiles.xlsx has the correct sheet name, columns, and data."""
        records = [
            UnreachableRecord(
                input_row=2,
                url_as_given="https://www.linkedin.com/in/missing-user/",
                slug="missing-user",
                reason=Reason.NOT_FOUND,
                detail="Profile not found",
            ),
            UnreachableRecord(
                input_row=3,
                url_as_given="not-a-valid-url",
                slug=None,
                reason=Reason.INVALID_URL,
                detail="Cannot parse slug",
            ),
        ]

        report_path = write_unreachable_report(records, tmp_path)
        assert report_path is not None
        assert report_path.exists()

        wb = openpyxl.load_workbook(report_path)
        try:
            assert "unreachable" in wb.sheetnames
            ws = wb["unreachable"]
            headers = [ws.cell(row=1, column=col).value for col in range(1, 7)]
            assert headers == [
                "input_row",
                "url_as_given",
                "slug",
                "reason",
                "detail",
                "timestamp_utc",
            ]

            assert ws.cell(row=2, column=1).value == 2
            assert ws.cell(row=2, column=3).value == "missing-user"
            assert ws.cell(row=2, column=4).value == "not_found"

            assert ws.cell(row=3, column=1).value == 3
            assert ws.cell(row=3, column=3).value in ("", None)
            assert ws.cell(row=3, column=4).value == "invalid_url"
        finally:
            wb.close()

    def test_no_report_created_when_nothing_unreachable(self, tmp_path: Path) -> None:
        """Verify unreachable report is skipped when all profiles are reachable."""
        res = write_unreachable_report([], tmp_path)
        assert res is None
        assert not (tmp_path / "unreachable_profiles.xlsx").exists()

    def test_run_summary_writer(self, tmp_path: Path) -> None:
        """Verify run_summary.json is written properly."""
        summary = RunSummary(
            started_at="2026-11-04T10:00:00Z",
            finished_at="2026-11-04T10:05:00Z",
            duration_seconds=300.0,
            rows_read=5,
            rows_valid=4,
            rows_invalid=1,
            duplicates_skipped=0,
            ok_count=3,
            unreachable_count=2,
            unreachable_by_reason={"not_found": 1, "invalid_url": 1},
            not_processed_count=0,
            output_dir=str(tmp_path),
        )

        sum_path = write_run_summary(summary, tmp_path)
        assert sum_path.exists()
        parsed = json.loads(sum_path.read_text(encoding="utf-8"))
        assert parsed["ok_count"] == 3
        assert parsed["unreachable_count"] == 2
        assert parsed["rows_read"] == 5
