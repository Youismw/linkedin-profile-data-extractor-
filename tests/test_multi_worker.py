"""Unit and integration tests for multi-worker parallel extraction architecture."""

from pathlib import Path
import tempfile
import openpyxl
import pytest

from profile_extractor.cli import build_parser
from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import FakeFetcher, get_worker_auth_path
from profile_extractor.models import FetchResult, Status
from profile_extractor.reader import InputRow
from profile_extractor.orchestrator import partition_workload, run_pipeline


def _make_dummy_row(idx: int) -> InputRow:
    return InputRow(
        row_number=idx,
        raw=f"https://www.linkedin.com/in/candidate-{idx}",
        normalized=f"https://www.linkedin.com/in/candidate-{idx}",
        slug=f"candidate-{idx}",
    )


def test_partition_workload_13_items_3_workers():
    """Verify 13 items across 3 workers partitions into [5, 4, 4]."""
    items = [_make_dummy_row(i) for i in range(1, 14)]
    partitions = partition_workload(items, 3)

    assert len(partitions) == 3
    assert [len(p) for p in partitions] == [5, 4, 4]
    
    # Check that all items are preserved in original order
    flattened = [row for p in partitions for row in p]
    assert [r.row_number for r in flattened] == list(range(1, 14))


def test_partition_workload_edge_cases():
    """Verify partition_workload handles small counts, 1 worker, and empty lists."""
    assert partition_workload([], 3) == []

    items = [_make_dummy_row(1)]
    p1 = partition_workload(items, 3)
    assert len(p1) == 1
    assert len(p1[0]) == 1

    items5 = [_make_dummy_row(i) for i in range(1, 6)]
    # 5 items across 2 workers -> [3, 2]
    p2 = partition_workload(items5, 2)
    assert [len(p) for p in p2] == [3, 2]

    # 1 worker
    p_single = partition_workload(items5, 1)
    assert len(p_single) == 1
    assert len(p_single[0]) == 5


def test_get_worker_auth_path(tmp_path: Path):
    """Verify distinct auth files are generated for workers > 1."""
    base = tmp_path / "state.json"
    base.write_text("{}", encoding="utf-8")

    # When total_workers == 1, returns base
    assert get_worker_auth_path(base, 0, 1) == base

    # When total_workers == 3
    w1 = get_worker_auth_path(base, 0, 3)
    w2 = get_worker_auth_path(base, 1, 3)
    w3 = get_worker_auth_path(base, 2, 3)

    assert w1.name == "state_worker_1.json"
    assert w2.name == "state_worker_2.json"
    assert w3.name == "state_worker_3.json"


def test_cli_parser_workers_argument():
    """Verify CLI accepts --workers and -w flags."""
    parser = build_parser()
    args1 = parser.parse_args(["run", "--workers", "3"])
    assert args1.workers == 3

    args2 = parser.parse_args(["run", "-w", "2"])
    assert args2.workers == 2


def test_multi_worker_pipeline_execution(tmp_path: Path):
    """Verify pipeline runs concurrently with workers=2 and produces reconciled outputs."""
    # Create input spreadsheet with 5 candidates
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Profile URL"])
    for i in range(1, 6):
        ws.append([f"https://www.linkedin.com/in/worker-test-{i}"])

    input_file = tmp_path / "workers_test.xlsx"
    wb.save(input_file)

    output_dir = tmp_path / "output"

    minimal_html = """
    <html>
      <head><title>Worker Candidate</title></head>
      <body>
        <main>
          <h1>Candidate Name</h1>
          <div>Location: San Francisco</div>
        </main>
      </body>
    </html>
    """

    outcomes = {
        f"https://www.linkedin.com/in/worker-test-{i}": FetchResult(
            url=f"https://www.linkedin.com/in/worker-test-{i}",
            status=Status.OK,
            html=minimal_html,
        )
        for i in range(1, 6)
    }
    fake_fetcher = FakeFetcher(outcomes=outcomes)

    cfg = ExtractorConfig(
        input_file=input_file,
        output_dir=output_dir,
        workers=2,
        delay_seconds_min=0.0,
        delay_seconds_max=0.0,
        deep_details=False,
    )

    summary = run_pipeline(cfg, fake_fetcher, sleep_fn=lambda _: None)

    assert summary.rows_read == 5
    assert summary.rows_valid == 5
    assert summary.ok_count == 5
    assert summary.unreachable_count == 0
    assert summary.not_processed_count == 0

    profiles_dir = output_dir / "profiles"
    json_files = list(profiles_dir.glob("*.json"))
    assert len(json_files) == 5
