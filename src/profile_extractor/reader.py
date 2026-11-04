"""Excel reader for extracting LinkedIn profile URLs.

Module Architecture & Inter-Module Communication:
==================================================
This module is responsible for reading candidate spreadsheets (.xlsx, .xlsm, .csv),
handling malformed cell structures, resolving =HYPERLINK formulas, skipping header
lines, deduplicating candidate URLs, and returning typed ReadResult structures.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.orchestrator`:
     * Calls `read_profile_urls(input_file)` at the beginning of the pipeline run.
   - Unit tests in `tests/test_urls_and_reader.py`.

2. Downstream Dependencies:
   - `profile_extractor.urls`:
     * Calls `normalize_profile_url()` to validate and canonicalize profile URLs.
     * Calls `extract_slug()` to extract the raw candidate slug handle.
     * Calls `sanitize_slug()` to generate safe filename stems.
   - `openpyxl`: Reads Excel .xlsx and .xlsm workbooks.
   - `csv`: Reads fallback comma-separated files.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from profile_extractor.urls import extract_slug, normalize_profile_url, sanitize_slug

# Regex to extract URL from Excel =HYPERLINK("url", "text") formulas
HYPERLINK_FORMULA_RE = re.compile(r'=HYPERLINK\(\s*["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class InputRow:
    """A single candidate row processed from the input spreadsheet."""

    row_number: int  # 1-based original Excel row index for auditing
    raw: str  # Raw string as extracted from cell value or hyperlink
    normalized: str | None  # Normalized canonical URL, or None if malformed
    slug: str | None = None  # Decoded slug handle
    sanitized_slug: str | None = None  # Safe filename slug
    error: str | None = None  # Error identifier (e.g., 'invalid_url')


@dataclass
class ReadResult:
    """Aggregated outcome of reading the input Excel workbook."""

    rows: list[InputRow]  # All non-empty rows parsed
    rows_read: int  # Total non-empty data rows encountered
    rows_valid: int  # Valid canonical candidate links
    rows_invalid: int  # Malformed links requiring failure logging
    duplicates_skipped: int  # Repeated entries skipped to prevent duplicate work


def is_link_like(val: str) -> bool:
    """Determine if a cell string looks like a LinkedIn link or URL.

    Distinguishes genuine links from text headers (e.g., 'Candidates', 'LinkedIn URLs').
    """
    val_lower = val.lower().strip()
    if val_lower.startswith(("http://", "https://", "www.linkedin.", "linkedin.")):
        return True
    if "/in/" in val_lower:
        return True
    return False


def _find_best_worksheet(wb: Any) -> Any:
    """Select the worksheet containing candidate links by scoring actual contents."""
    if len(wb.worksheets) == 1:
        return wb.worksheets[0]

    best_sheet = wb.worksheets[0]
    best_score = -1

    for sheet in wb.worksheets:
        score = 0
        title = sheet.title.lower()
        # Signal bonus if title specifically targets profiles or candidates
        if any(k in title for k in ("candidate", "profile", "linkedin", "links")):
            score += 5

        # Score by counting actual link-like cells in the sheet
        max_r = min(sheet.max_row or 0, 30)
        max_c = min(sheet.max_column or 0, 15)
        link_count = 0
        for r in range(1, max_r + 1):
            for c in range(1, max_c + 1):
                cell = sheet.cell(row=r, column=c)
                val = str(cell.value or "")
                hl = cell.hyperlink.target if (cell.hyperlink and cell.hyperlink.target) else ""
                if is_link_like(val) or is_link_like(hl):
                    link_count += 1

        score += link_count * 10
        if score > best_score:
            best_score = score
            best_sheet = sheet

    return best_sheet


def _find_best_column(sheet: Any, max_row: int) -> int:
    """Detect which column contains LinkedIn profile URLs by scoring actual link distribution."""
    max_col = sheet.max_column or 1
    if max_col <= 1:
        return 1

    scan_limit = min(max_row, 30)
    col_scores = [0] * (max_col + 1)

    for c in range(1, max_col + 1):
        for r in range(1, scan_limit + 1):
            cell = sheet.cell(row=r, column=c)
            val = str(cell.value or "")
            hl = cell.hyperlink.target if (cell.hyperlink and cell.hyperlink.target) else ""
            if is_link_like(val) or is_link_like(hl):
                col_scores[c] += 10

        # Header keyword bonus
        header_val = str(sheet.cell(row=1, column=c).value or "").strip().lower()
        if any(k in header_val for k in ("linkedin", "profile link", "profile url", "url")):
            col_scores[c] += 2

    best_col = 1
    best_score = -1
    for c in range(1, max_col + 1):
        if col_scores[c] > best_score:
            best_score = col_scores[c]
            best_col = c

    return best_col if best_score > 0 else 1


def _read_csv_urls(path: Path) -> ReadResult:
    """Read LinkedIn profile URLs from a CSV file."""
    rows: list[InputRow] = []
    seen_slugs: set[str] = set()
    rows_read = 0
    rows_valid = 0
    rows_invalid = 0
    duplicates_skipped = 0

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        reader = list(csv.reader(f))

    if not reader:
        return ReadResult(
            rows=[], rows_read=0, rows_valid=0, rows_invalid=0, duplicates_skipped=0
        )

    # 1. Evaluate link distribution across all columns to pick the true URL column
    num_cols = max(len(r) for r in reader) if reader else 0
    col_link_counts = [0] * num_cols
    col_header_scores = [0] * num_cols

    for r_idx, row in enumerate(reader[:30]):
        for c_idx, cell in enumerate(row):
            if is_link_like(cell):
                col_link_counts[c_idx] += 10
            if r_idx == 0:
                cell_lower = cell.strip().lower()
                if any(
                    k in cell_lower
                    for k in ("linkedin", "profile url", "profile link", "url")
                ):
                    col_header_scores[c_idx] += 2

    url_col = 0
    combined_scores = [
        col_link_counts[i] + col_header_scores[i] for i in range(num_cols)
    ]
    if combined_scores and max(combined_scores) > 0:
        url_col = combined_scores.index(max(combined_scores))

    # 2. Header Detection: If ANY cell in row 0 contains a valid LinkedIn profile URL,
    # row 0 is definitely candidate data, NOT a header!
    first_row = reader[0]
    row0_has_valid_url = any(
        is_link_like(c) and normalize_profile_url(c) is not None for c in first_row
    )

    start_idx = 0
    if not row0_has_valid_url:
        # Check if the designated URL column cell in row 0 looks like a text header
        if url_col < len(first_row) and not is_link_like(first_row[url_col]):
            start_idx = 1

    for row_num, row_data in enumerate(reader[start_idx:], start=start_idx + 1):
        if not row_data or not any(c.strip() for c in row_data):
            continue

        raw_target = ""
        if url_col < len(row_data) and is_link_like(row_data[url_col]):
            raw_target = row_data[url_col].strip()
        else:
            for cell in row_data:
                if is_link_like(cell):
                    raw_target = cell.strip()
                    break

        if not raw_target:
            continue

        rows_read += 1
        normalized = normalize_profile_url(raw_target)
        if normalized:
            slug = extract_slug(normalized)
            slug_key = slug.lower()
            if slug_key in seen_slugs:
                duplicates_skipped += 1
                continue
            seen_slugs.add(slug_key)
            sanitized = sanitize_slug(slug, fallback_seed=normalized)
            rows_valid += 1
            rows.append(
                InputRow(
                    row_number=row_num,
                    raw=raw_target,
                    normalized=normalized,
                    slug=slug,
                    sanitized_slug=sanitized,
                    error=None,
                )
            )
        else:
            rows_invalid += 1
            rows.append(
                InputRow(
                    row_number=row_num,
                    raw=raw_target,
                    normalized=None,
                    slug=None,
                    sanitized_slug=None,
                    error="invalid_url",
                )
            )

    return ReadResult(
        rows=rows,
        rows_read=rows_read,
        rows_valid=rows_valid,
        rows_invalid=rows_invalid,
        duplicates_skipped=duplicates_skipped,
    )


def read_profile_urls(
    file_path: Path | str,
    column_index: int = 1,
) -> ReadResult:
    """Read LinkedIn profile URLs from an Excel (.xlsx/.xlsm) or CSV file.

    Parameters:
        file_path: Absolute or relative path to the workbook or CSV.
        column_index: 1-based column index to inspect (defaults to auto-detecting).

    Returns:
        ReadResult containing parsed rows, counts, and deduplication stats.

    Raises:
        FileNotFoundError: If the input file does not exist.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {path}")

    # Handle CSV files seamlessly
    if path.suffix.lower() == ".csv":
        return _read_csv_urls(path)

    # load_workbook with data_only=False preserves formulas and hyperlinks
    wb = load_workbook(filename=path, data_only=False, read_only=False)
    try:
        sheet = _find_best_worksheet(wb)
        max_row = sheet.max_row
        if max_row is None or max_row < 1:
            return ReadResult(
                rows=[],
                rows_read=0,
                rows_valid=0,
                rows_invalid=0,
                duplicates_skipped=0,
            )

        # Detect best column if using default column 1
        effective_col = (
            _find_best_column(sheet, max_row) if column_index == 1 else column_index
        )

        start_row = 1

        # Header Detection Heuristic:
        # If ANY cell in row 1 contains a valid LinkedIn profile URL, row 1 is data, NOT a header!
        row1_has_valid_url = False
        max_c = sheet.max_column or 1
        for c in range(1, max_c + 1):
            c_cell = sheet.cell(row=1, column=c)
            c_val = str(c_cell.value or "").strip()
            c_hl = (
                c_cell.hyperlink.target
                if (c_cell.hyperlink and c_cell.hyperlink.target)
                else None
            )
            candidate = c_hl or c_val
            if candidate and is_link_like(candidate) and normalize_profile_url(candidate):
                row1_has_valid_url = True
                break

        start_row = 1
        if not row1_has_valid_url:
            first_cell = sheet.cell(row=1, column=effective_col)
            first_val = str(first_cell.value or "").strip()
            first_hyperlink = (
                first_cell.hyperlink.target
                if (first_cell.hyperlink and first_cell.hyperlink.target)
                else None
            )
            if first_val or first_hyperlink:
                candidate_link = first_hyperlink or first_val
                if not is_link_like(candidate_link):
                    start_row = 2

        seen_slugs: set[str] = set()
        rows: list[InputRow] = []
        rows_read = 0
        rows_valid = 0
        rows_invalid = 0
        duplicates_skipped = 0

        # Row Iteration and Normalization Loop
        for r in range(start_row, max_row + 1):
            cell = sheet.cell(row=r, column=effective_col)
            cell_text = str(cell.value or "").strip()
            hyperlink = (
                cell.hyperlink.target if (cell.hyperlink and cell.hyperlink.target) else None
            )

            # If designated cell is empty or not link-like, scan other columns in the same row
            if not is_link_like(cell_text) and not (hyperlink and is_link_like(hyperlink)):
                max_c = sheet.max_column or 1
                for alt_c in range(1, max_c + 1):
                    if alt_c == effective_col:
                        continue
                    alt_cell = sheet.cell(row=r, column=alt_c)
                    alt_val = str(alt_cell.value or "").strip()
                    alt_hl = (
                        alt_cell.hyperlink.target
                        if (alt_cell.hyperlink and alt_cell.hyperlink.target)
                        else None
                    )
                    if is_link_like(alt_val) or (alt_hl and is_link_like(alt_hl)):
                        cell_text = alt_val
                        hyperlink = alt_hl
                        break

            # Skip completely blank rows gracefully
            if not cell_text and not hyperlink:
                continue

            rows_read += 1

            # Extract target URL from Excel =HYPERLINK("...", ...) formulas if present
            if cell_text.startswith("="):
                hl_match = HYPERLINK_FORMULA_RE.search(cell_text)
                if hl_match:
                    cell_text = hl_match.group(1).strip()

            # Prefer visible cell text if URL; otherwise fall back to underlying hyperlink
            raw_target = cell_text if is_link_like(cell_text) else (hyperlink or cell_text)

            # Normalize to canonical format: https://www.linkedin.com/in/<slug>/
            normalized = normalize_profile_url(raw_target)
            if normalized:
                slug = extract_slug(normalized)
                # Deduplicate by slug handle (case-insensitive)
                slug_key = slug.lower()
                if slug_key in seen_slugs:
                    duplicates_skipped += 1
                    continue

                seen_slugs.add(slug_key)
                sanitized = sanitize_slug(slug, fallback_seed=normalized)
                rows_valid += 1
                rows.append(
                    InputRow(
                        row_number=r,
                        raw=raw_target,
                        normalized=normalized,
                        slug=slug,
                        sanitized_slug=sanitized,
                        error=None,
                    )
                )
            else:
                # Malformed or unsupported URL
                rows_invalid += 1
                rows.append(
                    InputRow(
                        row_number=r,
                        raw=raw_target,
                        normalized=None,
                        slug=None,
                        sanitized_slug=None,
                        error="invalid_url",
                    )
                )

        return ReadResult(
            rows=rows,
            rows_read=rows_read,
            rows_valid=rows_valid,
            rows_invalid=rows_invalid,
            duplicates_skipped=duplicates_skipped,
        )
    finally:
        wb.close()
