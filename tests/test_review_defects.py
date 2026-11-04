"""Comprehensive tests verifying fixes for the 10 defects identified in static review."""

from pathlib import Path

import openpyxl
import pytest

from profile_extractor.cli import main
from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FakeFetcher
from profile_extractor.fetcher.detection import detect_outcome
from profile_extractor.models import FetchResult, Reason, Status
from profile_extractor.orchestrator import run_pipeline
from profile_extractor.parser import parse_profile
from profile_extractor.reader import read_profile_urls
from profile_extractor.urls import extract_slug, normalize_profile_url


class TestReviewDefectFixes:
    """Tests covering all 10 defect items from code review."""

    # Defect 1: Output cleanup can delete every previously extracted profile on empty/failed batch
    def test_prune_stale_never_deletes_when_zero_valid_profiles(self, tmp_path: Path) -> None:
        """Verify that prune_stale=True never wipes files when workbook has 0 valid profiles."""
        input_xlsx = tmp_path / "empty_input.xlsx"
        output_dir = tmp_path / "out"
        profiles_dir = output_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        existing_file = profiles_dir / "existing-user.json"
        existing_file.write_text('{"meta": {"slug": "existing-user"}}', encoding="utf-8")

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Invalid", "Data"])
        ws.append(["not-a-url", "12345"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir, prune_stale=True)
        summary = run_pipeline(config, FakeFetcher(), sleep_fn=lambda _: None)

        assert summary.rows_valid == 0
        assert existing_file.exists(), "Existing profile must NOT be deleted when 0 valid profiles"

    def test_prune_stale_never_deletes_on_partial_or_capped_run(self, tmp_path: Path) -> None:
        """Verify that prune_stale=True does not prune if run did not process all valid profiles."""
        input_xlsx = tmp_path / "partial_input.xlsx"
        output_dir = tmp_path / "out"
        profiles_dir = output_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        existing_file = profiles_dir / "existing-user.json"
        existing_file.write_text('{"meta": {"slug": "existing-user"}}', encoding="utf-8")

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/user-a/"])
        ws.append(["https://www.linkedin.com/in/user-b/"])
        wb.save(input_xlsx)

        # Capped to 1 profile out of 2 valid
        config = ExtractorConfig(
            input_file=input_xlsx, output_dir=output_dir, max_profiles=1, prune_stale=True
        )
        summary = run_pipeline(config, FakeFetcher(), sleep_fn=lambda _: None)

        assert summary.rows_valid == 2
        assert summary.ok_count == 1
        assert existing_file.exists(), "Existing profile must NOT be deleted during capped run"

    # Defect 2: Sanitized filenames deleted immediately & collision risk
    def test_sanitized_slug_preserved_and_collision_disambiguated(self, tmp_path: Path) -> None:
        """Verify sanitized slugs survive cleanup and collisions receive unique stems."""
        input_xlsx = tmp_path / "collision_input.xlsx"
        output_dir = tmp_path / "out"
        profiles_dir = output_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        # Two slugs that would sanitize to the same base stem "test-user"
        ws.append(["https://www.linkedin.com/in/test-user/"])
        ws.append(["https://www.linkedin.com/in/test%20user/"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir, prune_stale=True)
        summary = run_pipeline(config, FakeFetcher(), sleep_fn=lambda _: None)

        assert summary.ok_count == 2
        files = list(profiles_dir.glob("*.json"))
        # Both profiles must have been written and neither deleted
        assert len(files) == 2
        stems = {f.stem for f in files}
        assert "test-user" in stems
        # Second file should have collision hash
        colliding_stem = [s for s in stems if s != "test-user"][0]
        assert colliding_stem.startswith("test-user-")

    # Defect 3: Configuration file paths and precedence
    def test_cli_config_file_and_overrides_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify deterministic precedence: CLI args > run_state > config.toml > default."""
        state_file = tmp_path / ".run_state.json"
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("profile_extractor.cli.STATE_FILE", state_file)
        monkeypatch.chdir(tmp_path)

        config_input = tmp_path / "config_input.xlsx"
        config_output = tmp_path / "config_output"
        cli_input = tmp_path / "cli_input.xlsx"
        cli_output = tmp_path / "cli_output"

        for p in (config_input, cli_input):
            wb = openpyxl.Workbook()
            ws = wb.active
            assert ws is not None
            ws.append(["Links"])
            ws.append(["https://www.linkedin.com/in/precedence-user/"])
            wb.save(p)

        # 1. config.toml specifies config_input and config_output
        config_file.write_text(
            f'input_file = "{config_input.as_posix()}"\n'
            f'output_dir = "{config_output.as_posix()}"\n',
            encoding="utf-8",
        )

        # Run with no arguments - should automatically pick up config.toml from cwd
        ret1 = main(["run", "--fake"])
        assert ret1 == 0
        assert (config_output / "profiles" / "precedence-user.json").exists()

        # 2. CLI arguments take precedence over config.toml
        ret2 = main(["run", str(cli_input), str(cli_output), "--fake"])
        assert ret2 == 0
        assert (cli_output / "profiles" / "precedence-user.json").exists()

    # Defect 4: Deep-page blocks trigger circuit breaker
    def test_deep_page_blocks_handled_and_trigger_circuit_breaker(self, tmp_path: Path) -> None:
        """Verify deep-page blocks pass through block handling and circuit breaker."""
        input_xlsx = tmp_path / "deep_block.xlsx"
        output_dir = tmp_path / "out"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/user-1/"])
        ws.append(["https://www.linkedin.com/in/user-2/"])
        wb.save(input_xlsx)

        # Custom fetcher: root profile has link to details/skills, but deep page returns BLOCKED
        class DeepBlockedFetcher(FakeFetcher):
            def fetch(self, url: str) -> FetchResult:
                if "details" in url:
                    return FetchResult(
                        url=url,
                        status=Status.UNREACHABLE,
                        reason=Reason.BLOCKED,
                        detail="Deep-page rate limited",
                    )
                return FetchResult(
                    url=url,
                    status=Status.OK,
                    html=(
                        "<html><body>"
                        "<h1>User One</h1>"
                        '<p class="headline">Software Engineer</p>'
                        '<a href="/details/skills/">Show all 20 skills</a>'
                        "</body></html>"
                    ),
                )

        # Circuit breaker trips on block when stop_on_block=True
        config = ExtractorConfig(
            input_file=input_xlsx,
            output_dir=output_dir,
            deep_details=True,
            stop_on_block=True,
        )
        summary = run_pipeline(config, DeepBlockedFetcher(), sleep_fn=lambda _: None)

        # user-1 had deep block; circuit breaker halts so user-2 is skipped
        assert summary.ok_count == 1
        assert summary.not_processed_count == 1

    # Defect 5: max_profiles summary reconciliation
    def test_max_profiles_summary_reconciles(self, tmp_path: Path) -> None:
        """Verify RunSummary correctly reconciles rows_valid with not_processed_count."""
        input_xlsx = tmp_path / "reconcile.xlsx"
        output_dir = tmp_path / "out"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        for i in range(5):
            ws.append([f"https://www.linkedin.com/in/user-{i}/"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir, max_profiles=2)
        summary = run_pipeline(config, FakeFetcher(), sleep_fn=lambda _: None)

        assert summary.rows_valid == 5
        assert summary.ok_count == 2
        assert summary.unreachable_count == 0
        assert summary.not_processed_count == 3
        assert summary.rows_valid == (
            summary.ok_count + summary.unreachable_count + summary.not_processed_count
        )

    # Defect 6: HTTP error status code classification
    def test_detect_outcome_http_status_codes(self) -> None:
        """Verify HTTP 401, 403, 404, 410, 429, 999, 500, 503, 418 classification."""
        url = "https://www.linkedin.com/in/sample/"
        html = "<html><body>Error</body></html>"

        # 401 -> NOT_VISIBLE
        st, re, _ = detect_outcome(url, html, status_code=401)
        assert st == Status.UNREACHABLE and re == Reason.NOT_VISIBLE

        # 403 -> BLOCKED
        st, re, _ = detect_outcome(url, html, status_code=403)
        assert st == Status.UNREACHABLE and re == Reason.BLOCKED

        # 404 -> NOT_FOUND
        st, re, _ = detect_outcome(url, html, status_code=404)
        assert st == Status.UNREACHABLE and re == Reason.NOT_FOUND

        # 410 -> NOT_FOUND
        st, re, _ = detect_outcome(url, html, status_code=410)
        assert st == Status.UNREACHABLE and re == Reason.NOT_FOUND

        # 429 & 999 -> BLOCKED
        for code in (429, 999):
            st, re, _ = detect_outcome(url, html, status_code=code)
            assert st == Status.UNREACHABLE and re == Reason.BLOCKED

        # 500 & 503 -> UNEXPECTED_ERROR
        for code in (500, 503):
            st, re, _ = detect_outcome(url, html, status_code=code)
            assert st == Status.UNREACHABLE and re == Reason.UNEXPECTED_ERROR

        # 418 / other 4xx -> UNEXPECTED_ERROR
        st, re, _ = detect_outcome(url, html, status_code=418)
        assert st == Status.UNREACHABLE and re == Reason.UNEXPECTED_ERROR

    # Defect 7: Parser rejects error page headings
    def test_parser_rejects_generic_error_page_as_unreachable(self) -> None:
        """Verify error pages like 500 Internal Server Error are not accepted as valid profiles."""
        error_html = """
        <html>
        <head><title>500 Internal Server Error</title></head>
        <body>
            <h1>Internal Server Error</h1>
            <p>An unexpected error occurred. Please try again later.</p>
        </body>
        </html>
        """
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/error-page/",
            slug="error-page",
            html=error_html,
            status=Status.OK,
            important_fields=["name", "headline"],
        )
        assert doc.meta.status == Status.UNREACHABLE
        assert doc.meta.reason == Reason.PARSE_FAILED
        assert doc.profile.name is None

    # Defect 8: Worksheet selection scores valid link count over title match
    def test_worksheet_ranking_prefers_link_count_over_metadata_title(
        self, tmp_path: Path
    ) -> None:
        """Verify a sheet with candidate links is chosen over a sheet named 'Metadata'."""
        wb = openpyxl.Workbook()

        # First sheet named "Metadata" (contains "data" substring)
        ws_meta = wb.active
        assert ws_meta is not None
        ws_meta.title = "Metadata"
        ws_meta.append(["Generated At", "Version", "User"])
        ws_meta.append(["2026-11-04", "1.0", "Admin"])

        # Second sheet named "Candidates"
        ws_cand = wb.create_sheet(title="Candidates")
        ws_cand.append(["Profile URLs"])
        ws_cand.append(["https://www.linkedin.com/in/cand-1/"])
        ws_cand.append(["https://www.linkedin.com/in/cand-2/"])

        file_path = tmp_path / "metadata_first.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_valid == 2
        assert res.rows[0].slug == "cand-1"
        assert res.rows[1].slug == "cand-2"

    # Defect 10: URL normalization uppercase scheme and reserved character rejection
    def test_url_normalization_uppercase_and_percent_decoding(self) -> None:
        """Verify uppercase scheme HTTPS:// and rejection of reserved delimiter characters."""
        # Uppercase scheme
        url_upper = "HTTPS://www.linkedin.com/in/example-dev/"
        norm_upper = normalize_profile_url(url_upper)
        assert norm_upper == "https://www.linkedin.com/in/example-dev/"
        assert extract_slug(norm_upper) == "example-dev"

        # Valid percent-encoded Unicode letters decode safely
        url_unicode = "https://www.linkedin.com/in/ren%C3%A9e-dev/"
        norm_unicode = normalize_profile_url(url_unicode)
        assert norm_unicode == "https://www.linkedin.com/in/renée-dev/"
        assert extract_slug(norm_unicode) == "renée-dev"

        # Reserved URL delimiter %2F (slash) is rejected to prevent identifier mutation
        url_encoded_slash = "https://www.linkedin.com/in/path%2Fuser/"
        assert normalize_profile_url(url_encoded_slash) is None

        # Reserved URL delimiters %3F (query ?) and %23 (fragment #) are rejected
        assert normalize_profile_url("https://www.linkedin.com/in/john%3Fdoe/") is None
        assert normalize_profile_url("https://www.linkedin.com/in/john%23doe/") is None

    # New Edge-case 1: URL validation rejects userinfo / external hostname tricks
    def test_url_validation_rejects_userinfo_and_external_hosts(self) -> None:
        """Verify URLs with embedded credentials or spoofed hostnames are strictly rejected."""
        # Hostname spoofing with userinfo
        evil_spoof = "https://linkedin.com:443@evil.com/in/john-doe"
        assert normalize_profile_url(evil_spoof) is None

        # Userinfo on linkedin.com
        userinfo_url = "https://user:pass@www.linkedin.com/in/john-doe"
        assert normalize_profile_url(userinfo_url) is None

        # Non-standard port
        bad_port_url = "https://www.linkedin.com:8080/in/john-doe"
        assert normalize_profile_url(bad_port_url) is None

    # New Edge-case 2: CSV headerless detection preserves first candidate with 'profile' in title
    def test_csv_headerless_detection_preserves_first_candidate(
        self, tmp_path: Path
    ) -> None:
        """Verify headerless CSV does not discard first candidate with 'profile' in title."""
        csv_file = tmp_path / "candidates.csv"
        csv_file.write_text(
            "Profile Engineer,Jane Doe,https://www.linkedin.com/in/jane-doe/\n"
            "Sales Manager,John Smith,https://www.linkedin.com/in/john-smith/\n",
            encoding="utf-8",
        )

        res = read_profile_urls(csv_file)
        assert res.rows_valid == 2
        assert res.rows[0].slug == "jane-doe"
        assert res.rows[1].slug == "john-smith"

    # New Edge-case 4: Writer strictly prevents directory traversal
    def test_writer_prevents_directory_traversal(self, tmp_path: Path) -> None:
        """Verify write_profile_json sanitizes explicit stems and prevents directory traversal."""
        from profile_extractor.models import Meta, Profile, ProfileDocument, Status
        from profile_extractor.writer import write_profile_json

        doc = ProfileDocument(
            meta=Meta(
                source_url="https://www.linkedin.com/in/traversal-test/",
                slug="traversal-test",
                status=Status.OK,
            ),
            profile=Profile(name="Traversal Tester"),
        )
        out_dir = tmp_path / "safe_output"
        out_dir.mkdir()

        # Pass a traversal attempt: ../../escape
        written_path = write_profile_json(doc, out_dir, filename_stem="../../escape")

        # Must be contained inside out_dir (sanitized to 'escape.json' inside out_dir)
        assert written_path.parent == out_dir.resolve()
        assert written_path.exists()
        assert not (tmp_path / "escape.json").exists()

    # New Edge-case 6: Normal INFO logs do not contain candidate slugs
    def test_normal_logs_do_not_contain_candidate_slugs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify orchestrator INFO logs omit personally identifiable candidate slugs."""
        import logging

        input_xlsx = tmp_path / "privacy_test.xlsx"
        output_dir = tmp_path / "out"

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])
        ws.append(["https://www.linkedin.com/in/secret-person-12345/"])
        wb.save(input_xlsx)

        config = ExtractorConfig(input_file=input_xlsx, output_dir=output_dir)

        with caplog.at_level(logging.INFO, logger="profile_extractor"):
            run_pipeline(config, FakeFetcher(), sleep_fn=lambda _: None)

        # Check all logged INFO messages
        info_messages = [
            r.message for r in caplog.records if r.levelno == logging.INFO
        ]
        for msg in info_messages:
            assert "secret-person-12345" not in msg
