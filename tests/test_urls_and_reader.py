"""Tests for Phase 2: URL normalization, slug sanitization, and Excel reading."""

import time
from pathlib import Path

import openpyxl

from profile_extractor.reader import read_profile_urls
from profile_extractor.urls import extract_slug, normalize_profile_url, sanitize_slug


class TestURLNormalization:
    """Test suite covering URL normalization and slug sanitization."""

    def test_valid_urls(self) -> None:
        """Test at least 10 valid URL permutations."""
        valid_cases = [
            ("https://www.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
            ("http://www.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
            ("https://linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
            ("linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
            ("www.linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
            ("https://uk.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
            ("https://in.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
            (
                "https://www.linkedin.com/in/jane-doe?trk=prof",
                "https://www.linkedin.com/in/jane-doe/",
            ),
            (
                "https://www.linkedin.com/in/jane-doe#details",
                "https://www.linkedin.com/in/jane-doe/",
            ),
            ("https://www.linkedin.com/in/jane%20doe/", "https://www.linkedin.com/in/jane doe/"),
            (
                "https://www.linkedin.com/in/alex.smith_99/",
                "https://www.linkedin.com/in/alex.smith_99/",
            ),
        ]

        for raw, expected in valid_cases:
            normalized = normalize_profile_url(raw)
            assert normalized == expected, f"Failed for {raw}"
            assert normalized is not None
            slug = extract_slug(normalized)
            assert slug in ("jane-doe", "jane doe", "alex.smith_99")

    def test_invalid_urls(self) -> None:
        """Test at least 9 invalid URL permutations."""
        invalid_cases = [
            "",
            "   ",
            "not a url at all",
            "https://www.linkedin.com/company/google/",
            "https://www.linkedin.com/feed/",
            "https://www.linkedin.com/in/",
            "https://www.google.com/search?q=linkedin",
            "https://malicious-domain.com/in/fake",
            "mailto:user@linkedin.com",
            "https://www.linkedin.com/in/foo/bar/baz",
        ]

        for raw in invalid_cases:
            assert normalize_profile_url(raw) is None, f"Should be invalid: {raw}"

    def test_slug_sanitization(self) -> None:
        """Test that slug sanitization produces safe filenames and handles fallback."""
        assert sanitize_slug("jane-doe") == "jane-doe"
        assert sanitize_slug("jane.doe_99") == "jane.doe_99"
        assert sanitize_slug("jane doe") == "jane-doe"
        assert sanitize_slug("jane/doe?test*") == "jane-doe-test"

        # Special characters only -> fallback hash
        fallback = sanitize_slug("$$$###@@@")
        assert fallback.startswith("profile-")
        assert len(fallback) == 16  # "profile-" (8) + 8 hex chars


class TestExcelReader:
    """Test suite covering Excel input reading across diverse file variations."""

    def test_file_with_header(self, tmp_path: Path) -> None:
        """Verify workbook with a header row is read properly without treating header as profile."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile Link"])
        ws.append(["https://www.linkedin.com/in/jane-doe/"])
        ws.append(["https://www.linkedin.com/in/john-smith/"])

        file_path = tmp_path / "with_header.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_read == 2
        assert res.rows_valid == 2
        assert res.rows_invalid == 0
        assert res.duplicates_skipped == 0
        assert len(res.rows) == 2
        assert res.rows[0].slug == "jane-doe"
        assert res.rows[1].slug == "john-smith"

    def test_file_without_header(self, tmp_path: Path) -> None:
        """Verify workbook starting directly with a profile link is not skipped."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["https://www.linkedin.com/in/first-profile/"])
        ws.append(["https://www.linkedin.com/in/second-profile/"])

        file_path = tmp_path / "no_header.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_read == 2
        assert res.rows_valid == 2
        assert res.rows[0].slug == "first-profile"

    def test_file_with_blanks_and_duplicates(self, tmp_path: Path) -> None:
        """Verify blanks are skipped and duplicates are counted and deduped."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Candidate Links"])
        ws.append(["https://www.linkedin.com/in/profile-a/"])
        ws.append([""])  # Blank row
        ws.append(["https://www.linkedin.com/in/profile-a/"])  # Duplicate
        ws.append(["not_a_valid_link"])  # Invalid row
        ws.append([None])  # None blank row
        ws.append(["https://www.linkedin.com/in/profile-b/"])

        file_path = tmp_path / "blanks_dupes.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_read == 4  # profile-a, duplicate, invalid, profile-b
        assert res.rows_valid == 2
        assert res.duplicates_skipped == 1
        assert res.rows_invalid == 1
        assert len(res.rows) == 3  # 2 valid + 1 invalid

    def test_file_with_hyperlinks(self, tmp_path: Path) -> None:
        """Verify cell with display text 'View Profile' and hyperlink target is processed."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Links"])

        cell = ws.cell(row=2, column=1, value="View Profile")
        cell.hyperlink = "https://www.linkedin.com/in/hyperlink-user/"

        file_path = tmp_path / "hyperlink.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_valid == 1
        assert res.rows[0].slug == "hyperlink-user"
        assert res.rows[0].normalized == "https://www.linkedin.com/in/hyperlink-user/"

    def test_single_row_file(self, tmp_path: Path) -> None:
        """Verify a single row file works without errors."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["https://www.linkedin.com/in/solo-user/"])

        file_path = tmp_path / "single_row.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_valid == 1
        assert res.rows[0].slug == "solo-user"

    def test_1000_rows_benchmark(self, tmp_path: Path) -> None:
        """Verify 1000 rows file is read and validated in under 2 seconds."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["LinkedIn Profile URLs"])

        for i in range(1, 1001):
            ws.append([f"https://www.linkedin.com/in/benchmark-user-{i}/"])

        file_path = tmp_path / "perf_1000.xlsx"
        wb.save(file_path)

        start = time.perf_counter()
        res = read_profile_urls(file_path)
        elapsed = time.perf_counter() - start

        assert res.rows_valid == 1000
        assert res.duplicates_skipped == 0
        assert elapsed < 2.0, f"Reading 1000 rows took {elapsed:.2f}s, expected < 2.0s"

    def test_subpage_profile_urls(self) -> None:
        """Verify LinkedIn sub-pages (e.g. details/experience) normalize to candidate root."""
        subpage_url = "https://www.linkedin.com/in/satya-nadella/details/experience/"
        normalized = normalize_profile_url(subpage_url)
        assert normalized == "https://www.linkedin.com/in/satya-nadella/"
        assert extract_slug(normalized) == "satya-nadella"

        overlay_url = "https://www.linkedin.com/in/john-smith/overlay/contact-info/"
        normalized_overlay = normalize_profile_url(overlay_url)
        assert normalized_overlay == "https://www.linkedin.com/in/john-smith/"

    def test_hyperlink_formula_reading(self, tmp_path: Path) -> None:
        """Verify Excel =HYPERLINK('url', 'text') formulas are resolved to the URL."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile"])
        ws.append(['=HYPERLINK("https://www.linkedin.com/in/formula-candidate/", "View Profile")'])

        file_path = tmp_path / "formula_test.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_valid == 1
        assert res.rows[0].slug == "formula-candidate"
        assert res.rows[0].normalized == "https://www.linkedin.com/in/formula-candidate/"

    def test_case_insensitive_deduplication(self, tmp_path: Path) -> None:
        """Verify duplicate URLs with differing slug casing are deduplicated cleanly."""
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["Profile"])
        ws.append(["https://www.linkedin.com/in/satya-nadella/"])
        ws.append(["https://www.linkedin.com/in/SATYA-NADELLA/"])

        file_path = tmp_path / "casing_dedupe.xlsx"
        wb.save(file_path)

        res = read_profile_urls(file_path)
        assert res.rows_valid == 1
        assert res.duplicates_skipped == 1
