"""Tests for Phase 7: Integration hardening, privacy audit, and error isolation."""

from pathlib import Path

import openpyxl

from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FakeFetcher
from profile_extractor.logging_setup import setup_logging
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.orchestrator import run_pipeline


class TestIntegrationHardening:
    """Test suite verifying pipeline robustness, privacy guarantees, and error isolation."""

    def test_unreachable_path_malformed_and_nonexistent(self, tmp_path: Path) -> None:
        """Verify unreachable path handles one malformed link and one nonexistent profile."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile Links"])
        ws.append(["this_is_a_malformed_url_without_domain"])
        ws.append(["https://www.linkedin.com/in/nonexistent-profile-xyz/"])
        ws.append(["https://www.linkedin.com/in/valid-candidate/"])

        xlsx_path = tmp_path / "hardening_input.xlsx"
        output_dir = tmp_path / "hardening_output"
        wb.save(xlsx_path)

        outcomes = {
            "nonexistent-profile-xyz": FetchResult(
                url="https://www.linkedin.com/in/nonexistent-profile-xyz/",
                status=Status.UNREACHABLE,
                reason=Reason.NOT_FOUND,
                detail="HTTP 404 / Profile not found",
            ),
            "valid-candidate": FetchResult(
                url="https://www.linkedin.com/in/valid-candidate/",
                status=Status.OK,
                html="<html><body><h1>Valid Candidate</h1><p>Software Engineer</p></body></html>",
            ),
        }

        fetcher = FakeFetcher(outcomes=outcomes)
        config = ExtractorConfig(
            input_file=xlsx_path,
            output_dir=output_dir,
            delay_seconds_min=0.1,
            delay_seconds_max=0.2,
        )

        summary = run_pipeline(config, fetcher, sleep_fn=lambda _: None)

        assert summary.ok_count == 1
        assert summary.unreachable_count == 2
        assert summary.unreachable_by_reason.get("invalid_url") == 1
        assert summary.unreachable_by_reason.get("not_found") == 1

        # Check unreachable Excel report
        rep_file = output_dir / "unreachable_profiles.xlsx"
        assert rep_file.exists()
        rep_wb = openpyxl.load_workbook(rep_file)
        try:
            ws_rep = rep_wb["unreachable"]
            assert ws_rep.max_row == 3  # header + 2 unreachable records
        finally:
            rep_wb.close()

    def test_privacy_audit_logs_contain_no_profile_content(self, tmp_path: Path) -> None:
        """Verify application logs never receive or print candidate personal data."""
        log_dir = tmp_path / "logs"
        setup_logging(level="INFO", log_dir=log_dir)

        candidate_name = "SecretCandidateFirstName SuperUniqueLastName"
        candidate_headline = "SecretCompanyExecutive"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile URLs"])
        ws.append(["https://www.linkedin.com/in/secret-candidate/"])

        xlsx_path = tmp_path / "privacy_input.xlsx"
        output_dir = tmp_path / "privacy_output"
        wb.save(xlsx_path)

        outcomes = {
            "secret-candidate": FetchResult(
                url="https://www.linkedin.com/in/secret-candidate/",
                status=Status.OK,
                html=(
                    f"<html><body><h1>{candidate_name}</h1>"
                    f"<div>{candidate_headline}</div></body></html>"
                ),
            )
        }

        fetcher = FakeFetcher(outcomes=outcomes)
        config = ExtractorConfig(input_file=xlsx_path, output_dir=output_dir)

        run_pipeline(config, fetcher, sleep_fn=lambda _: None)

        # Inspect log file
        log_file = log_dir / "extractor.log"
        assert log_file.exists()
        log_text = log_file.read_text(encoding="utf-8")

        # Name and headline must NEVER appear in logs
        assert candidate_name not in log_text
        assert candidate_headline not in log_text

    def test_gitignore_protects_sensitive_directories(self) -> None:
        """Verify .gitignore includes input, output, .auth, logs, and private fixtures."""
        gitignore_path = Path(".gitignore")
        assert gitignore_path.exists()
        content = gitignore_path.read_text(encoding="utf-8")

        required_patterns = ["input/", "output/", ".auth/", "logs/", "tests/fixtures/private/"]
        for pat in required_patterns:
            assert pat in content, f"Missing {pat} in .gitignore"
