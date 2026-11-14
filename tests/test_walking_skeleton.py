"""Tests for Phase 4: Walking skeleton with FakeFetcher, Orchestrator, and CLI."""

import json
from pathlib import Path

import openpyxl
import pytest

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

        # 4. Check JSON files created (only OK profiles are written to profiles/)
        profiles_dir = output_dir / "profiles"
        assert (profiles_dir / "user-ok.json").exists()
        assert (profiles_dir / "user-ok-2.json").exists()
        assert not (profiles_dir / "user-not-found.json").exists()
        assert not (profiles_dir / "user-not-visible.json").exists()
        assert not (profiles_dir / "user-crashed.json").exists()
        assert not (profiles_dir / "not-a-valid-profile-url.json").exists()

        # 5. Check unreachable Excel report
        unreachable_xlsx = output_dir / "unreachable_profiles.xlsx"
        assert unreachable_xlsx.exists()
        rep_wb = openpyxl.load_workbook(unreachable_xlsx)
        ws_rep = rep_wb["unreachable"]
        assert ws_rep.max_row == 5  # header + 4 unreachable rows
        status_by_slug = {
            ws_rep.cell(row=r, column=3).value: ws_rep.cell(row=r, column=7).value
            for r in range(2, 6)
        }
        assert status_by_slug["user-not-found"] == "genuinely_missing"
        assert status_by_slug["user-not-visible"] == "unreachable"
        assert status_by_slug["user-crashed"] == "unreachable"
        rep_wb.close()

        # 6. Check summary json
        summary_path = output_dir / "run_summary.json"
        assert summary_path.exists()
        loaded_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert loaded_summary["ok_count"] == 2
        assert loaded_summary["unreachable_count"] == 4
        assert loaded_summary["genuinely_missing_count"] == 1
        assert loaded_summary["unreachable_by_status"] == {
            "genuinely_missing": 1,
            "unreachable": 3,
        }

    def test_stop_on_block_behavior(self, tmp_path: Path) -> None:
        """Verify stop_on_block halts the run and accurately counts not_processed."""
        input_xlsx = tmp_path / "profiles_block.xlsx"
        output_dir = tmp_path / "output_block"
        profiles_dir = output_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        # Seed a stale unreachable stub from a prior failed run to verify it is cleaned up
        stale_stub = profiles_dir / "blocked-candidate.json"
        stale_stub.write_text(
            json.dumps({"meta": {"status": "unreachable", "reason": "blocked"}, "profile": {}}),
            encoding="utf-8",
        )

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
        assert not (output_dir / "profiles" / "blocked-candidate.json").exists()
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

    def test_cli_execution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify executing pipeline via CLI main entrypoint."""
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", tmp_path / ".run_state.json")
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

    def test_cli_state_persistence_and_stale_cleaning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify CLI remembers custom paths for subsequent runs and cleans stale files."""
        # Isolate state file to tmp_path
        state_file = tmp_path / ".run_state.json"
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", state_file)

        input_xlsx = tmp_path / "custom_input.xlsx"
        output_dir = tmp_path / "custom_output"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/persisted-user/"])
        wb.save(input_xlsx)

        # 1. Run with explicit positional input and output
        ret1 = main(["run", str(input_xlsx), str(output_dir), "--fake"])
        assert ret1 == 0
        assert (output_dir / "profiles" / "persisted-user.json").exists()
        assert state_file.is_file()

        # Place a stale profile from an old run in output
        stale_file = output_dir / "profiles" / "stale-old-user.json"
        stale_file.write_text("{}", encoding="utf-8")
        assert stale_file.is_file()

        # 2. Run with NO positional input or output arguments
        # (should remember paths and preserve existing profiles by default)
        ret2 = main(["run", "--fake"])
        assert ret2 == 0
        assert (output_dir / "profiles" / "persisted-user.json").exists()
        assert stale_file.exists(), (
            "By default, stale profile must NOT be deleted without --prune-stale"
        )

        # 3. Run with --prune-stale flag to explicitly opt into pruning
        ret3 = main(["run", "--fake", "--prune-stale"])
        assert ret3 == 0
        assert (output_dir / "profiles" / "persisted-user.json").exists()
        assert not stale_file.exists(), (
            "Stale profile must be cleaned when --prune-stale is specified"
        )

    def test_cli_output_only_intent_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify CLI correctly recognizes output-only paths via prefix, suffix, or keyword."""
        state_file = tmp_path / ".run_state.json"
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", state_file)

        input_xlsx = tmp_path / "default_input.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/output-intent-user/"])
        wb.save(input_xlsx)

        # Pre-seed default input in state
        state_file.write_text(
            json.dumps({"last_input": str(input_xlsx.resolve())}), encoding="utf-8"
        )

        # 1. Output specified via prefix: "output\<path>"
        out1 = tmp_path / "custom_out1"
        ret1 = main(["run", f"output\\{out1}", "--fake"])
        assert ret1 == 0
        assert (out1 / "profiles" / "output-intent-user.json").exists()

        # 2. Output specified via suffix: "<path>\output"
        out2 = tmp_path / "custom_out2" / "output"
        ret2 = main(["run", str(out2), "--fake"])
        assert ret2 == 0
        assert (out2 / "profiles" / "output-intent-user.json").exists()

        # 3. Output specified via keyword: output "<path>"
        out3 = tmp_path / "custom_out3"
        ret3 = main(["run", "output", str(out3), "--fake"])
        assert ret3 == 0
        assert (out3 / "profiles" / "output-intent-user.json").exists()

    def test_cli_logout_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify logout/clean-session command cleanly removes session and path memory."""
        state_file = tmp_path / ".run_state.json"
        state_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", state_file)

        auth_file = tmp_path / "test_state.json"
        auth_file.write_text("{}", encoding="utf-8")
        assert auth_file.is_file()

        ret = main(["logout", "--auth-path", str(auth_file)])
        assert ret == 0
        assert not auth_file.exists()
        assert not state_file.exists()

    def test_cli_fails_honestly_when_no_input_provided_after_reset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Verify CLI refuses to run and does NOT silently guess input files when none provided."""
        state_file = tmp_path / ".run_state.json"
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", state_file)

        # Ensure no state file exists (like after logout)
        if state_file.exists():
            state_file.unlink()

        ret = main(["run", "--fake"])
        assert ret == 1
        captured = capsys.readouterr()
        assert "[ERROR] No input spreadsheet provided" in captured.err

    def test_pipeline_preserves_existing_output_on_mid_run_interruption(
        self, tmp_path: Path
    ) -> None:
        """Verify mid-run interruption (Ctrl+C) never wipes prior profiles or empties folder."""
        input_xlsx = tmp_path / "interrupt_profiles.xlsx"
        output_dir = tmp_path / "interrupt_output"
        profiles_dir = output_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        # Pre-seed an existing profile from a prior successful run
        prior_file = profiles_dir / "prior-user.json"
        prior_file.write_text('{"meta": {"slug": "prior-user"}}', encoding="utf-8")
        assert prior_file.is_file()

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/user-1/"])
        ws.append(["https://www.linkedin.com/in/user-2/"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir)

        # Mock fetcher that simulates user interrupting (Ctrl+C) during row 2
        call_count = 0

        class InterruptingFetcher(FakeFetcher):
            def fetch(self, url: str) -> FetchResult:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise KeyboardInterrupt("Simulated Ctrl+C mid-run")
                return super().fetch(url)

        summary = run_pipeline(config, InterruptingFetcher(), sleep_fn=lambda _: None)

        # Prior file MUST still exist! (Never wiped at startup)
        assert prior_file.exists(), "Prior profile must NOT be wiped when interrupted mid-run"
        # User 1 (completed before interrupt) MUST exist!
        assert (profiles_dir / "user-1.json").exists()
        # Summary reflects accurate partial state
        assert summary.ok_count == 1
        assert summary.not_processed_count == 1
