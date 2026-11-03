"""Excel reader for extracting LinkedIn profile URLs."""

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from profile_extractor.urls import extract_slug, normalize_profile_url, sanitize_slug


@dataclass
class InputRow:
    """A single row processed from the input spreadsheet."""

    row_number: int
    raw: str
    normalized: str | None
    slug: str | None = None
    sanitized_slug: str | None = None
    error: str | None = None


@dataclass
class ReadResult:
    """Aggregated output from reading the Excel file."""

    rows: list[InputRow]
    rows_read: int
    rows_valid: int
    rows_invalid: int
    duplicates_skipped: int


def is_link_like(val: str) -> bool:
    """Determine if a cell string looks like a LinkedIn link or URL."""
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
    """Read LinkedIn profile URLs from the first worksheet of an Excel file.

    Parameters
    ----------
    file_path : Path | str
        Path to the .xlsx workbook.
    column_index : int
        1-based column index to read (default 1).

    Returns
    -------
    ReadResult
        Structured rows, counts for valid, invalid, duplicates, and total read.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Input Excel file does not exist: {path}")

    # load_workbook data_only=False preserves hyperlinks
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

        # Check if first row is a header
        first_cell = sheet.cell(row=1, column=column_index)
        first_val = str(first_cell.value or "").strip()
        first_hyperlink = (
            first_cell.hyperlink.target
            if (first_cell.hyperlink and first_cell.hyperlink.target)
            else None
        )

        # If row 1 has content and is not link-like in text or hyperlink, treat as header
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

        for r in range(start_row, max_row + 1):
            cell = sheet.cell(row=r, column=column_index)
            cell_text = str(cell.value or "").strip()
            hyperlink = (
                cell.hyperlink.target
                if (cell.hyperlink and cell.hyperlink.target)
                else None
            )

            # Skip completely blank rows
            if not cell_text and not hyperlink:
                continue

            rows_read += 1
            # Prefer cell text if it looks like a URL; otherwise fallback to hyperlink target
            raw_target = cell_text if is_link_like(cell_text) else (hyperlink or cell_text)

            normalized = normalize_profile_url(raw_target)
            if normalized:
                slug = extract_slug(normalized)
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
