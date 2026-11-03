"""Excel reader for extracting LinkedIn profile URLs.

Provides tolerant extraction of URLs from Excel spreadsheets:
- Automatically detects and skips header rows (e.g., 'Profile URL', 'Candidate Links').
- Resolves raw text URLs and cell hyperlink targets (e.g. friendly display text
  pointing to a hyperlink).
- Tolerates empty cells and trailing blank rows.
- Deduplicates repeated profile links while preserving accurate row accounting.
"""

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from profile_extractor.urls import extract_slug, normalize_profile_url, sanitize_slug


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


def read_profile_urls(
    file_path: Path | str,
    column_index: int = 1,
) -> ReadResult:
    """Read LinkedIn profile URLs from the primary worksheet of an Excel file.

    Parameters:
        file_path: Absolute or relative path to the .xlsx workbook.
        column_index: 1-based column index to inspect (defaults to Column 1 / 'A').

    Returns:
        ReadResult containing parsed rows, counts, and deduplication stats.

    Raises:
        FileNotFoundError: If the input Excel path does not exist.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Input Excel file does not exist: {path}")

    # load_workbook with data_only=False preserves formulas and hyperlinks
    wb = load_workbook(filename=path, data_only=False, read_only=False)
    try:
        sheet = wb.worksheets[0]
        max_row = sheet.max_row
        if max_row is None or max_row < 1:
            return ReadResult(
                rows=[],
                rows_read=0,
                rows_valid=0,
                rows_invalid=0,
                duplicates_skipped=0,
            )

        start_row = 1

        # ----------------------------------------------------------------------
        # Header Detection Heuristic:
        # Check row 1. If it has text but is not link-like, treat as header and
        # start data processing at row 2.
        # ----------------------------------------------------------------------
        first_cell = sheet.cell(row=1, column=column_index)
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

        # ----------------------------------------------------------------------
        # Row Iteration and Normalization Loop
        # ----------------------------------------------------------------------
        for r in range(start_row, max_row + 1):
            cell = sheet.cell(row=r, column=column_index)
            cell_text = str(cell.value or "").strip()
            hyperlink = (
                cell.hyperlink.target
                if (cell.hyperlink and cell.hyperlink.target)
                else None
            )

            # Skip completely blank rows gracefully
            if not cell_text and not hyperlink:
                continue

            rows_read += 1
            # Prefer visible cell text if URL; otherwise fall back to underlying hyperlink
            raw_target = cell_text if is_link_like(cell_text) else (hyperlink or cell_text)

            # Normalize to canonical format: https://www.linkedin.com/in/<slug>/
            normalized = normalize_profile_url(raw_target)
            if normalized:
                slug = extract_slug(normalized)
                # Deduplicate by slug handle
                if slug in seen_slugs:
                    duplicates_skipped += 1
                    continue

                seen_slugs.add(slug)
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
