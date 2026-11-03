"""Atomic and compliant file writer for profile documents, summaries, and reports.

Core responsibilities:
1. Writes individual profile documents as indented UTF-8 JSON files (<slug>.json).
2. Generates unreachable_profiles.xlsx auditing any failed profiles.
3. Generates run_summary.json reconciling processing statistics.
4. Uses atomic filesystem operations (writing to temporary files and renaming)
   to ensure zero half-written or corrupted files during crashes or interrupts.
5. Applies restrictive permission bits (0o600) where supported by the OS.
"""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import openpyxl

from profile_extractor.models import ProfileDocument, RunSummary, UnreachableRecord
from profile_extractor.urls import sanitize_slug


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
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def write_profile_json(document: ProfileDocument, output_dir: Path | str) -> Path:
    """Write a profile document to JSON atomically.

    File is saved as <slug>.json in the given output directory.
    Uses UTF-8 encoding with ensure_ascii=False to preserve international names.

    Returns:
        The Path to the written JSON file.
    """
    out_dir = Path(output_dir)
    # Sanitize slug so special characters never cause directory traversal or illegal paths
    clean_slug = sanitize_slug(document.meta.slug, fallback_seed=document.meta.source_url)
    target_path = out_dir / f"{clean_slug}.json"

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
    headers = ["input_row", "url_as_given", "slug", "reason", "detail", "timestamp_utc"]
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
            ]
        )

    # Save via temporary file for atomic write safety
    temp_target = out_dir / f".{target_path.stem}_{os.getpid()}.tmp"
    try:
        wb.save(temp_target)
        _set_restrictive_permissions(temp_target)
        os.replace(temp_target, target_path)
    finally:
        wb.close()
        if temp_target.exists():
            temp_target.unlink(missing_ok=True)

    return target_path


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
