"""Pipeline orchestrator for LinkedIn profile extraction.

This module coordinates the entire workflow:
1. Input Ingestion: Invokes reader.py to parse the Excel spreadsheet.
2. Error Isolation: Wraps each candidate visit in a try/except block so a failure
   on one candidate never crashes the pipeline for remaining candidates.
3. Polite Pacing: Injects random jitter delays between profile visits to mimic
   human browsing and protect employee accounts from rate limits.
4. Circuit Breaker: Halts immediately if bot challenge/CAPTCHA is detected
   (when stop_on_block=True), marking remaining rows as 'not_processed'.
5. Clean Output: Purges stale JSON files from prior runs so only fresh data exists.
6. Audit Generation: Writes individual JSON files, unreachable_profiles.xlsx,
   and mathematical run_summary.json.
7. Safe Teardown: Always releases browser resources via `fetcher.close()` in a `finally` block.
"""

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

from profile_extractor.config import ExtractorConfig
from profile_extractor.fetcher import Fetcher
from profile_extractor.logging_setup import get_logger
from profile_extractor.models import (
    Reason,
    RunSummary,
    Status,
    UnreachableRecord,
)
from profile_extractor.parser import parse_profile
from profile_extractor.reader import read_profile_urls
from profile_extractor.writer import (
    write_profile_json,
    write_run_summary,
    write_unreachable_report,
)


def run_pipeline(
    config: ExtractorConfig,
    fetcher: Fetcher,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """Execute the end-to-end extraction pipeline.

    Parameters:
        config: ExtractorConfig containing paths, timeouts, and delay bounds.
        fetcher: Concrete Fetcher implementation (PlaywrightFetcher or FakeFetcher).
        sleep_fn: Injected sleep function (time.sleep in prod, mock in tests).

    Returns:
        RunSummary object with fully reconciled audit statistics.
    """
    logger = get_logger()
    start_time = datetime.now(UTC)
    started_at_str = start_time.isoformat().replace("+00:00", "Z")
    logger.info("Starting pipeline run on %s", config.input_file)

    # --------------------------------------------------------------------------
    # 1. Read Input Spreadsheet
    # --------------------------------------------------------------------------
    try:
        read_result = read_profile_urls(config.input_file)
    except Exception as e:
        logger.error("Failed to read input spreadsheet: %s", e)
        fetcher.close()
        raise

    all_rows = read_result.rows

    # Apply max_profiles limit if configured
    if config.max_profiles and config.max_profiles > 0:
        valid_count = 0
        capped_rows = []
        for r in all_rows:
            if r.normalized:
                if valid_count < config.max_profiles:
                    capped_rows.append(r)
                    valid_count += 1
            else:
                capped_rows.append(r)
        candidate_rows = capped_rows
    else:
        candidate_rows = all_rows

    valid_rows = [r for r in candidate_rows if r.normalized and r.slug]
    invalid_rows = [r for r in candidate_rows if not r.normalized]

    ok_count = 0
    unreachable_count = 0
    unreachable_by_reason: dict[str, int] = {}
    not_processed_count = 0
    unreachable_records: list[UnreachableRecord] = []

    # --------------------------------------------------------------------------
    # 2. Record Malformed / Invalid URLs Immediately
    # Invalid URLs appear in unreachable_profiles.xlsx without creating JSON files
    # --------------------------------------------------------------------------
    for inv in invalid_rows:
        unreachable_count += 1
        unreachable_by_reason[Reason.INVALID_URL.value] = (
            unreachable_by_reason.get(Reason.INVALID_URL.value, 0) + 1
        )
        unreachable_records.append(
            UnreachableRecord(
                input_row=inv.row_number,
                url_as_given=inv.raw,
                slug=None,
                reason=Reason.INVALID_URL,
                detail="Malformed or unsupported URL",
            )
        )
        logger.info("Row %d: unreachable (invalid_url)", inv.row_number)

    num_valid = len(valid_rows)
    stop_triggered = False

    # --------------------------------------------------------------------------
    # 3. Prepare Clean Output Directory
    # Purges leftover .json files from previous runs to guarantee fresh data
    # --------------------------------------------------------------------------
    profiles_dir = config.output_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    for stale_json in profiles_dir.glob("*.json"):
        try:
            stale_json.unlink()
        except OSError:
            pass

    # --------------------------------------------------------------------------
    # 4. Processing Loop Across Valid Candidate Rows
    # --------------------------------------------------------------------------
    try:
        for idx, row in enumerate(valid_rows):
            # If circuit breaker was triggered by a bot challenge, skip remaining rows
            if stop_triggered:
                not_processed_count += 1
                logger.info("Row %d: skipped (pipeline stopped after block)", row.row_number)
                continue

            slug = row.slug or ""
            url = row.normalized or ""
            logger.info(
                "Row %d: visiting profile '%s' (%d of %d)...",
                row.row_number,
                slug,
                idx + 1,
                num_valid,
            )

            try:
                # Retrieve profile HTML via Playwright
                fetch_res = fetcher.fetch(url)

                if fetch_res.status == Status.OK:
                    # Parse DOM into structured ProfileDocument
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.OK,
                        important_fields=config.important_fields,
                    )
                    write_profile_json(doc, profiles_dir)

                    if doc.meta.status == Status.OK:
                        ok_count += 1
                        logger.info("Row %d: ok", row.row_number)
                    else:
                        # Fallback if parser could not identify candidate name/headline
                        reason = doc.meta.reason or Reason.PARSE_FAILED
                        unreachable_count += 1
                        unreachable_by_reason[reason.value] = (
                            unreachable_by_reason.get(reason.value, 0) + 1
                        )
                        unreachable_records.append(
                            UnreachableRecord(
                                input_row=row.row_number,
                                url_as_given=row.raw,
                                slug=slug,
                                reason=reason,
                                detail="Parser could not extract candidate name/headline from DOM",
                            )
                        )
                        logger.info("Row %d: unreachable (%s)", row.row_number, reason.value)

                else:
                    # Retrieval failed (NOT_FOUND, NOT_VISIBLE, BLOCKED, TIMEOUT)
                    reason = fetch_res.reason or Reason.UNEXPECTED_ERROR
                    unreachable_count += 1
                    unreachable_by_reason[reason.value] = (
                        unreachable_by_reason.get(reason.value, 0) + 1
                    )
                    unreachable_records.append(
                        UnreachableRecord(
                            input_row=row.row_number,
                            url_as_given=row.raw,
                            slug=slug,
                            reason=reason,
                            detail=fetch_res.detail,
                        )
                    )
                    # Persist unreachable stub document adhering to schema contract
                    doc = parse_profile(
                        source_url=url,
                        slug=slug,
                        html=fetch_res.html,
                        status=Status.UNREACHABLE,
                        important_fields=config.important_fields,
                        reason=reason.value,
                    )
                    write_profile_json(doc, profiles_dir)
                    logger.info("Row %d: unreachable (%s)", row.row_number, reason.value)

                    # Circuit breaker: halt immediately upon security checkpoint
                    if config.stop_on_block and reason == Reason.BLOCKED:
                        logger.warning(
                            "Row %d: Block detected! Halting run as stop_on_block=True",
                            row.row_number,
                        )
                        stop_triggered = True

            except Exception as row_exc:
                # Isolate unexpected runtime exceptions so one candidate cannot kill the batch
                logger.error(
                    "Row %d: unexpected error during processing: %s",
                    row.row_number,
                    row_exc,
                )
                reason = Reason.UNEXPECTED_ERROR
                unreachable_count += 1
                unreachable_by_reason[reason.value] = (
                    unreachable_by_reason.get(reason.value, 0) + 1
                )
                unreachable_records.append(
                    UnreachableRecord(
                        input_row=row.row_number,
                        url_as_given=row.raw,
                        slug=slug,
                        reason=reason,
                        detail=str(row_exc),
                    )
                )
                doc = parse_profile(
                    source_url=url,
                    slug=slug,
                    html=None,
                    status=Status.UNREACHABLE,
                    important_fields=config.important_fields,
                    reason=reason.value,
                )
                write_profile_json(doc, profiles_dir)

            # Polite pacing: random delay between min and max bounds (skip after last profile)
            if idx < num_valid - 1 and not stop_triggered:
                delay = random.uniform(config.delay_seconds_min, config.delay_seconds_max)
                logger.info("Applying polite delay of %.1fs before next profile...", delay)
                sleep_fn(delay)

    finally:
        # Guarantee browser shutdown and process cleanup
        fetcher.close()

    # --------------------------------------------------------------------------
    # 5. Generate Audit Reports and Mathematical Summary
    # --------------------------------------------------------------------------
    write_unreachable_report(unreachable_records, config.output_dir)

    end_time = datetime.now(UTC)
    finished_at_str = end_time.isoformat().replace("+00:00", "Z")
    duration = (end_time - start_time).total_seconds()

    summary = RunSummary(
        started_at=started_at_str,
        finished_at=finished_at_str,
        duration_seconds=round(duration, 2),
        rows_read=read_result.rows_read,
        rows_valid=read_result.rows_valid,
        rows_invalid=read_result.rows_invalid,
        duplicates_skipped=read_result.duplicates_skipped,
        ok_count=ok_count,
        unreachable_count=unreachable_count,
        unreachable_by_reason=unreachable_by_reason,
        not_processed_count=not_processed_count,
        output_dir=str(config.output_dir),
    )

    write_run_summary(summary, config.output_dir)
    logger.info(
        "Run completed in %.2fs: %d ok, %d unreachable, %d not processed",
        duration,
        ok_count,
        unreachable_count,
        not_processed_count,
    )
    return summary
