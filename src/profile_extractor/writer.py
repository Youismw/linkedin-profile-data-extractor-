"""Atomic and compliant file writer for profile documents, summaries, and reports.

Module Architecture & Inter-Module Communication:
==================================================
This module is responsible for durable, crash-resilient persistence of pipeline outputs.
It performs atomic writes via temporary files and `os.replace` to prevent corrupted or
half-written files, creating individual candidate JSON files, summary reports, and Excel audits.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.orchestrator`:
     * Calls `write_profile_json(doc, output_dir)` after each successfully parsed candidate.
     * Calls `write_unreachable_report(unreachable, output_dir)` to write unreachable_profiles.xlsx.
     * Calls `write_run_summary(summary, output_dir)` to write run_summary.json.
     * Calls `profile_filename_stem(doc)` for filesystem path determination.
   - Unit tests in `tests/test_writer.py`.

2. Downstream Dependencies:
   - `profile_extractor.models`: Consumes `ProfileDocument`, `RunSummary`, `UnreachableRecord`.
   - `profile_extractor.urls`: Calls `sanitize_slug()` for filename generation.
   - `openpyxl`: Generates the formatted Excel audit report for unreachable rows.
"""

import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import openpyxl

from profile_extractor.logging_setup import get_logger
from profile_extractor.models import ProfileDocument, RunSummary, UnreachableRecord
from profile_extractor.urls import sanitize_slug

logger = get_logger()


def _set_restrictive_permissions(file_path: Path) -> None:
    """Attempt to set user-only read/write permissions (0o600).

    Guarantees privacy on multi-user systems. Gracefully ignored on Windows
    filesystems that do not implement POSIX permission mode bits.
    """
    try:
        os.chmod(file_path, 0o600)
    except Exception:
        pass


def _atomic_write_text(target_path: Path, content: str) -> None:
    """Write string content atomically using a temporary file in the target directory.

    Mechanism:
    1. Writes to a hidden temp file located in the exact same directory/filesystem.
    2. Flushes buffers and invokes os.fsync to force hardware write.
    3. Uses os.replace() which is atomic on modern POSIX and Windows filesystems.
    4. Cleans up temp file if any error occurs.
    """
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Place temp file in same directory so os.replace is an atomic inode/MFT move
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{target_path.stem}_",
        suffix=".tmp",
    ) as tmp_file:
        tmp_name = tmp_file.name
        tmp_file.write(content)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())

    tmp_path = Path(tmp_name)
    try:
        _set_restrictive_permissions(tmp_path)
        try:
            os.replace(tmp_path, target_path)
        except PermissionError:
            timestamp = int(datetime.now(UTC).timestamp())
            fallback_path = parent / f"{target_path.stem}_{timestamp}{target_path.suffix}"
            logger.warning(
                "Could not overwrite %s (file locked by another process). Writing to %s instead.",
                target_path.name,
                fallback_path.name,
            )
            os.replace(tmp_path, fallback_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def profile_filename_stem(slug: str, source_url: str = "") -> str:
    """Canonical filename stem for a profile slug (single source of truth)."""
    return sanitize_slug(slug, fallback_seed=source_url)


def write_profile_json(
    document: ProfileDocument,
    output_dir: Path | str,
    filename_stem: str | None = None,
) -> Path:
    """Write a profile document to JSON atomically.

    File is saved as <filename_stem>.json (default: canonical sanitized slug).
    Uses UTF-8 encoding with ensure_ascii=False to preserve international names.
    Enforces path-traversal safety checks to guarantee output stays within output_dir.

    Returns:
        The Path to the written JSON file.
    """
    out_dir = Path(output_dir).resolve()
    raw_stem = (
        filename_stem
        if filename_stem is not None
        else profile_filename_stem(document.meta.slug, document.meta.source_url)
    )

    # Strip directory parts and sanitize to protect against path traversal (e.g. '../../')
    clean_stem = sanitize_slug(Path(raw_stem).name, fallback_seed=document.meta.source_url)
    target_path = (out_dir / f"{clean_stem}.json").resolve()

    # Defensive boundary verification
    try:
        target_path.relative_to(out_dir)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {filename_stem!r} escapes {out_dir}"
        ) from None

    # Serialize using contract model method
    data = document.to_serializable_dict()
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    _atomic_write_text(target_path, content)
    return target_path


def write_unreachable_report(
    records: Sequence[UnreachableRecord],
    output_dir: Path | str,
) -> Path | None:
    """Generate unreachable_profiles.xlsx if there are unreachable records.

    Columns: input_row, url_as_given, slug, reason, detail, timestamp_utc.
    Sheet name: unreachable.

    Returns:
        The Path to the written Excel report, or None if records is empty.
    """
    if not records:
        return None

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / "unreachable_profiles.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "unreachable"

    # Write standardized audit headers
    headers = [
        "input_row",
        "url_as_given",
        "slug",
        "reason",
        "detail",
        "timestamp_utc",
        "status",
    ]
    ws.append(headers)

    # Write each unreachable diagnostic entry
    for rec in records:
        ws.append(
            [
                rec.input_row,
                rec.url_as_given,
                rec.slug or "",
                rec.reason.value if hasattr(rec.reason, "value") else str(rec.reason),
                rec.detail or "",
                rec.timestamp_utc,
                rec.status,
            ]
        )

    # Save via temporary file for atomic write safety
    temp_target = out_dir / f".{target_path.stem}_{os.getpid()}.tmp"
    final_path = target_path
    try:
        wb.save(temp_target)
        _set_restrictive_permissions(temp_target)
        try:
            os.replace(temp_target, target_path)
        except PermissionError:
            timestamp = int(datetime.now(UTC).timestamp())
            final_path = out_dir / f"unreachable_profiles_{timestamp}.xlsx"
            logger.warning(
                "Could not overwrite %s (file locked by Excel). Writing report to %s instead.",
                target_path.name,
                final_path.name,
            )
            os.replace(temp_target, final_path)
    finally:
        wb.close()
        if temp_target.exists():
            temp_target.unlink(missing_ok=True)

    return final_path


def write_run_summary(summary: RunSummary, output_dir: Path | str) -> Path:
    """Write run summary statistics to run_summary.json atomically.

    Guarantees that recruitment leaders and QA have complete accounting of
    processing times, counts, and category breakdowns.
    """
    out_dir = Path(output_dir)
    target_path = out_dir / "run_summary.json"
    content = json.dumps(summary.model_dump(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(target_path, content)
    return target_path
