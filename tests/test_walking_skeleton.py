"""Tests for Phase 4: Walking skeleton with FakeFetcher, Orchestrator, and CLI."""

import json
from pathlib import Path

import openpyxl

from profile_extractor.cli import main
from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FakeFetcher
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.orchestrator import run_pipeline


class TestWalkingSkeleton:
    """Test suite for the end-to-end pipeline using FakeFetcher."""

    def test_end_to_end_mixed_outcomes(self, tmp_path: Path) -> None:
        """Verify pipeline with mixed outcomes, failure isolation, and polite pacing."""
        input_xlsx = tmp_path / "profiles.xlsx"
        output_dir = tmp_path / "output"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile URLs"])
        ws.append(["https://www.linkedin.com/in/user-ok/"])
        ws.append(["https://www.linkedin.com/in/user-not-found/"])
        ws.append(["https://www.linkedin.com/in/user-not-visible/"])
        ws.append(["https://www.linkedin.com/in/user-crashed/"])
        ws.append(["https://www.linkedin.com/in/user-ok-2/"])
        ws.append(["not-a-valid-profile-url"])
        wb.save(input_xlsx)

        outcomes: dict[str, FetchResult | Exception] = {
            "user-ok": FetchResult(
                url="https://www.linkedin.com/in/user-ok/",
                status=Status.OK,
                html="<html><body><h1>User OK</h1></body></html>",
            ),
            "user-not-found": FetchResult(
                url="https://www.linkedin.com/in/user-not-found/",
                status=Status.UNREACHABLE,
                reason=Reason.NOT_FOUND,
                detail="Page does not exist",
            ),
            "user-not-visible": FetchResult(
                url="https://www.linkedin.com/in/user-not-visible/",
                status=Status.UNREACHABLE,
                reason=Reason.NOT_VISIBLE,
                detail="Login wall encountered",
            ),
            "user-crashed": RuntimeError("Underlying socket connection reset"),
            "user-ok-2": FetchResult(
                url="https://www.linkedin.com/in/user-ok-2/",
                status=Status.OK,
                html="<html><body><h1>User OK 2</h1></body></html>",
            ),
        }

        fetcher = FakeFetcher(outcomes=outcomes)
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        config = ExtractorConfig(
            input_file=input_xlsx,
            output_dir=output_dir,
            delay_seconds_min=1.0,
            delay_seconds_max=2.0,
        )

        summary = run_pipeline(config, fetcher, sleep_fn=mock_sleep)

        # 1. Fetcher must be closed
        assert fetcher.is_closed is True

        # 2. Pacing check: exactly 4 sleeps for 5 valid profiles (no sleep after the last)
        assert len(sleep_calls) == 4
        for s in sleep_calls:
            assert 1.0 <= s <= 2.0

        # 3. Counts check
        assert summary.rows_read == 6
        assert summary.rows_valid == 5
        assert summary.rows_invalid == 1
        assert summary.ok_count == 2
        assert summary.unreachable_count == 4
        assert summary.not_processed_count == 0

        # 4. Check JSON files created
        profiles_dir = output_dir / "profiles"
        assert (profiles_dir / "user-ok.json").exists()
        assert (profiles_dir / "user-ok-2.json").exists()
        assert (profiles_dir / "user-not-found.json").exists()
        assert (profiles_dir / "user-not-visible.json").exists()
        assert (profiles_dir / "user-crashed.json").exists()
        assert not (profiles_dir / "not-a-valid-profile-url.json").exists()

        # Check crashed profile contract
        crashed_json = json.loads((profiles_dir / "user-crashed.json").read_text(encoding="utf-8"))
        assert crashed_json["meta"]["status"] == "unreachable"
        assert crashed_json["meta"]["reason"] == "unexpected_error"
        assert crashed_json["profile"] == {}
        assert crashed_json["other_sections"] == []

        # 5. Check unreachable Excel report
        unreachable_xlsx = output_dir / "unreachable_profiles.xlsx"
        assert unreachable_xlsx.exists()
        rep_wb = openpyxl.load_workbook(unreachable_xlsx)
        assert rep_wb["unreachable"].max_row == 5  # header + 4 unreachable rows
        rep_wb.close()

        # 6. Check summary json
        summary_path = output_dir / "run_summary.json"
        assert summary_path.exists()
        loaded_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert loaded_summary["ok_count"] == 2
        assert loaded_summary["unreachable_count"] == 4

    def test_stop_on_block_behavior(self, tmp_path: Path) -> None:
        """Verify stop_on_block halts the run and accurately counts not_processed."""
        input_xlsx = tmp_path / "profiles_block.xlsx"
        output_dir = tmp_path / "output_block"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile URLs"])
        ws.append(["https://www.linkedin.com/in/first-ok/"])
        ws.append(["https://www.linkedin.com/in/blocked-candidate/"])
        ws.append(["https://www.linkedin.com/in/third-skipped/"])
        ws.append(["https://www.linkedin.com/in/fourth-skipped/"])
        wb.save(input_xlsx)

        outcomes: dict[str, FetchResult | Exception] = {
            "first-ok": FetchResult(
                url="https://www.linkedin.com/in/first-ok/",
                status=Status.OK,
                html="<html><body><h1>First User</h1><p>Software Engineer</p></body></html>",
            ),
            "blocked-candidate": FetchResult(
                url="https://www.linkedin.com/in/blocked-candidate/",
                status=Status.UNREACHABLE,
                reason=Reason.BLOCKED,
                detail="Security challenge displayed",
            ),
        }

        fetcher = FakeFetcher(outcomes=outcomes)
        config = ExtractorConfig(
            input_file=input_xlsx,
            output_dir=output_dir,
            stop_on_block=True,
        )

        summary = run_pipeline(config, fetcher, sleep_fn=lambda _: None)

        assert summary.ok_count == 1
        assert summary.unreachable_count == 1
        assert summary.not_processed_count == 2
        assert (output_dir / "profiles" / "first-ok.json").exists()
        assert (output_dir / "profiles" / "blocked-candidate.json").exists()
        assert not (output_dir / "profiles" / "third-skipped.json").exists()
        assert not (output_dir / "profiles" / "fourth-skipped.json").exists()

        # The counts must cleanly add up to total valid rows
        assert (
            summary.ok_count + summary.unreachable_count + summary.not_processed_count
            == summary.rows_valid
        )

    def test_running_twice_overwrites_cleanly(self, tmp_path: Path) -> None:
        """Verify re-running pipeline processes everything and overwrites files cleanly."""
        input_xlsx = tmp_path / "profiles_rerun.xlsx"
        output_dir = tmp_path / "output_rerun"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["URL"])
        ws.append(["https://www.linkedin.com/in/repeat-user/"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir)

        # Run 1
        f1 = FakeFetcher()
        summary1 = run_pipeline(config, f1, sleep_fn=lambda _: None)
        assert summary1.ok_count == 1
        assert (output_dir / "profiles" / "repeat-user.json").exists()

        # Run 2
        f2 = FakeFetcher()
        summary2 = run_pipeline(config, f2, sleep_fn=lambda _: None)
        assert summary2.ok_count == 1
        assert (output_dir / "profiles" / "repeat-user.json").exists()

    def test_cli_execution(self, tmp_path: Path) -> None:
        """Verify executing pipeline via CLI main entrypoint."""
        input_xlsx = tmp_path / "cli_profiles.xlsx"
        output_dir = tmp_path / "cli_output"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/cli-user/"])
        wb.save(input_xlsx)

        ret = main(
            [
                "run",
                "--input",
                str(input_xlsx),
                "--output",
                str(output_dir),
                "--fake",
            ]
        )
        assert ret == 0
        assert (output_dir / "profiles" / "cli-user.json").exists()
        assert (output_dir / "run_summary.json").exists()
