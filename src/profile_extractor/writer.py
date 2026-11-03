"""Atomic and compliant file writer for profile documents, summaries, and reports."""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import openpyxl

from profile_extractor.models import ProfileDocument, RunSummary, UnreachableRecord
from profile_extractor.urls import sanitize_slug


def _set_restrictive_permissions(file_path: Path) -> None:
    """Attempt to set user-only read/write permissions where supported by OS."""
    try:
        os.chmod(file_path, 0o600)
    except Exception:
        # Gracefully proceed on filesystems/OS that do not support POSIX permission bits
        pass


def _atomic_write_text(target_path: Path, content: str) -> None:
    """Write string content atomically using a temporary file in the target directory."""
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Create temporary file in the exact same directory for atomic rename
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

    File name is <slug>.json.
    Formatted with 2-space indentation, UTF-8 encoding, and ensure_ascii=False.
    """
    out_dir = Path(output_dir)
    clean_slug = sanitize_slug(document.meta.slug, fallback_seed=document.meta.source_url)
    target_path = out_dir / f"{clean_slug}.json"

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
    Returns None if records is empty.
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

    headers = ["input_row", "url_as_given", "slug", "reason", "detail", "timestamp_utc"]
    ws.append(headers)

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

    # Save via temporary file for atomic write
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
    """Write run_summary.json atomically with 2-space indentation and UTF-8 encoding."""
    out_dir = Path(output_dir)
    target_path = out_dir / "run_summary.json"

    content = json.dumps(summary.model_dump(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(target_path, content)
    return target_path
