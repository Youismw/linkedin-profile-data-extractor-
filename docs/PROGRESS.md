# Implementation Progress

## Phase Summary
- [x] Phase 0: Scaffold
- [x] Phase 1: Contracts
- [x] Phase 2: URLs and Input Reader
- [x] Phase 3: Writer
- [x] Phase 4: Walking skeleton (Fetcher interface, Orchestrator, CLI)
- [x] Phase 5: Playwright Fetcher and login helper (Live / H1)
- [x] Phase 6: Parser (H3)
- [x] Phase 7: Integration and hardening (H4)
- [x] Phase 8: Documentation and handover

---

## Phase 0: Scaffold
- **Status**: Completed
- **Done**:
  - Initialized Git repository and environment (`.venv` with Python 3.14).
  - Pinned all dependencies in `requirements.lock`.
  - Installed Chromium browser binaries for Playwright (`playwright install chromium`).
  - Configured `pyproject.toml` with `ruff`, `pytest`, and `pyright` settings.
  - Created `config.example.toml`, `.gitignore`, `README.md`, `docs/ARCHITECTURE.md`, `docs/HANDOVER.md`, and `docs/PROGRESS.md`.
  - Scaffolded source directory `src/profile_extractor` and test suite `tests/`.
  - Phase 0 Acceptance Gate passed (`pytest`, `ruff check`, `pyright`, `python -m profile_extractor --help`).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 1: Contracts
- **Status**: Completed
- **Done**:
  - Created `src/profile_extractor/models.py` with Pydantic v2 models: `Status`, `Reason`, `ExperienceItem`, `EducationItem`, `OtherSection`, `Profile`, `Meta`, `ProfileDocument`, `FetchResult`, `UnreachableRecord`, and `RunSummary`.
  - Created `src/profile_extractor/config.py` with TOML parser, defaults, bounds checking, and validation.
  - Created `src/profile_extractor/logging_setup.py` ensuring personal data is never printed or sent to logs.
  - Added comprehensive test suite `tests/test_contracts.py` covering model round-tripping, unreachable contracts (empty profile dict and list), and config validation errors.
  - Phase 1 Acceptance Gate passed (8 tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 2: URLs and Input Reader
- **Status**: Completed
- **Done**:
  - Created `src/profile_extractor/urls.py`:
    - Normalizes URLs across country subdomains, schemes, query parameters, fragments, and percent-encoding to canonical `https://www.linkedin.com/in/<slug>/`.
    - Extracts slug and sanitizes safe file names (`[A-Za-z0-9._-]`), with deterministic fallback hash if empty.
  - Created `src/profile_extractor/reader.py`:
    - Tolerant reading of Excel worksheets, detecting headers automatically.
    - Resolves raw text URLs and cell hyperlink targets.
    - Handles blank rows and dedupes repeated slugs while retaining accurate counts.
  - Created `tests/test_urls_and_reader.py` with over 20 URL valid/invalid variations, header/no-header/blank/hyperlink/duplicate/single-row cases, and 1000-row benchmark (passed in ~0.3s vs <2.0s limit).
  - Phase 2 Acceptance Gate passed (17 total tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 3: Writer
- **Status**: Completed
- **Done**:
  - Created `src/profile_extractor/writer.py`:
    - `write_profile_json`: atomic file replacement via temporary file, 2-space indentation, UTF-8, `ensure_ascii=False`, restrictive file permissions where supported. Clean rollback on error.
    - `write_unreachable_report`: writes `unreachable_profiles.xlsx` with exact columns and sheet name `unreachable` only when unreachable records exist (skipped if none).
    - `write_run_summary`: atomic write of `run_summary.json`.
  - Created `tests/test_writer.py` verifying atomic replacement, corruption prevention upon simulated write failure, native Unicode retention, and correct Excel generation.
  - Phase 3 Acceptance Gate passed (23 total tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 4: Walking Skeleton
- **Status**: Completed
- **Done**:
  - Created `src/profile_extractor/fetcher/__init__.py` with `Fetcher` protocol and configurable `FakeFetcher`.
  - Created `src/profile_extractor/parser.py` contract stub fulfilling schema requirements and calculating missing fields.
  - Created `src/profile_extractor/orchestrator.py` with row-level failure isolation, polite pacing (injected sleep without delay after the final row), and `stop_on_block` handling.
  - Created `src/profile_extractor/cli.py` with `run` and `login` subcommands.
  - Created `tests/test_walking_skeleton.py` covering mixed outcomes (ok, not_found, not_visible, blocked, crashed exceptions), verifying `stop_on_block` behavior, no post-last delay, atomic overwrite on rerun, and CLI invocation.
  - Phase 4 Acceptance Gate passed (27 total tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 5: Playwright Fetcher and login helper
- **Status**: Completed
- **Done**:
  - Checkpoint H1 fulfilled: Consenting employee executed interactive manual login; authenticated session saved to `.auth/state.json`.
  - Created `src/profile_extractor/fetcher/detection.py` with patterns for `blocked`, `not_found`, and `not_visible` (authwall).
  - Created `src/profile_extractor/fetcher/login.py` with realistic desktop viewport and standard user-agent.
  - Created `src/profile_extractor/fetcher/playwright_fetcher.py` with session loading, incremental scrolling for lazy elements, timeout handling, and automatic browser reconnection.
  - Live session verified: authenticated navigation returns `Status.OK` (HTML length > 190KB) without challenges.
  - Phase 5 Acceptance Gate passed (34 tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 6: Parser
- **Status**: Completed
- **Done**:
  - Checkpoint H3 confirmed: default important fields (`name`, `headline`, `location`, `about`, `experience`, `education`, `skills`).
  - Created `src/profile_extractor/parser_rules.py` isolating heading matching regex and stable attribute patterns.
  - Created `src/profile_extractor/parser.py`:
    - Robust extraction of structured fields (`name`, `headline`, `location`, `about`, `experience`, `education`, `skills`).
    - Raw text copies retained for each experience and education item.
    - Unmapped sections safely routed to `other_sections`.
    - Automated missing_fields computation against configured list.
    - Low text coverage verification warning.
    - `parse_failed` triggered when no name/headline can be detected.
  - Created synthetic test fixtures: `full_profile.html`, `sparse_profile.html`, `unusual_section_profile.html`.
  - Created `tests/test_parser.py`: 5 comprehensive tests validating Unicode, missing fields, unmapped sections, idempotence, and parse failure handling.
  - Phase 6 Acceptance Gate passed (39 total tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 7: Integration and Hardening
- **Status**: Completed
- **Done**:
  - Configured `PlaywrightFetcher` as default in CLI with helpful message if `.auth/state.json` is missing.
  - Verified error isolation with mixed test workbook containing malformed link and nonexistent profile.
  - Verified privacy audit: candidate personal data is never printed to logs or output to stdout.
  - Verified `.gitignore` protects input, output, `.auth/state.json`, logs, and private fixtures.
  - Verified atomic overwriting on repeated runs.
  - Created `tests/test_integration_hardening.py` (3 new tests).
  - Phase 7 Acceptance Gate passed (42 total tests passing, 0 ruff errors, 0 pyright errors).
- **Skipped**: None.
- **Open Issues**: None.

---

## Phase 8: Documentation and Handover
- **Status**: Completed
- **Done**:
  - Created and polished `README.md`: project purpose, installation, configuration, CLI usage, failure reasons, known limitations, and how to swap the Fetcher with code examples.
  - Created and polished `docs/ARCHITECTURE.md`: comprehensive Mermaid component diagram, Does/Does-Not matrix, risk register with mitigations, detailed technical explanation of why automated headless login is disallowed (CWE-256, 2FA, anti-bot), and production transition sequence diagram.
  - Created and polished `docs/HANDOVER.md`: executive summary, management decisions required (applicant consent, Terms of Service, data retention schedule, production API selection), senior reviewer Q&A cheat sheet, and 3-minute demo script with offline fallback.
  - Tested running in clean environment with `FakeFetcher` on `input/demo_profiles.xlsx` (completed in 6.91s vs 10m gate limit).
  - Phase 8 Acceptance Gate passed.
- **Skipped**: None.
- **Open Issues**: None.

---

## Post-Phase 8 Enhancements & Bugfixes
- **Status**: Completed
- **Root Cause & Resolution**:
  - **Scroll Container**: LinkedIn redesigned single-page application profiles place the scroll container on `<main id="workspace">` rather than `window` (leaving `document.body.scrollHeight` static at 900px). The fetcher now scrolls both `<main>` and `window` progressively, triggering all intersection observers and GraphQL lazy queries.
  - **Component Structure**: Redesigned LinkedIn sections utilize `data-component-type="LazyColumn"` and ARIA listitems (`role="listitem"`) rather than traditional `<ul><li>`. The parser now recursively inspects `li`, `LazyColumn`, and ARIA item elements.
  - **Layout Isolation**: Filtered out container layout wrappers (`aria-label="Primary content"`) and nested carousels (`aria-roledescription="carousel"`) from swallowing child sections or stealing top card metadata.
  - **Top Card Refinement**: Filtered out candidate pronouns (`He/Him`, `She/Her`, etc.) and `--` empty placeholders from being misidentified as professional headlines.
  - **Single Windows Explorer Popup**: Guarded folder pop-up with an `opened` state flag so `os.startfile` and `explorer.exe` fallback never spawn duplicate folder instances.
  - **Path Memory & Persistence**: Implemented `.run_state.json` storage in `cli.py` to remember custom input spreadsheets and output directories across runs, eliminating the need to re-type paths.
  - **Safe Stale File Cleanup**: Replaced automatic startup file deletion with opt-in `--prune-stale` running only after a full, successful batch to guarantee prior extractions are never wiped.
  - **Raw Text Dump**: Added visible text dump under `"raw_text"` at the end of each JSON profile document, capturing candidate content while stripping non-content noise (scripts, stylesheets, modals, footers).
  - **Auto-Relogin Flow**: Integrated proactive cookie expiration checks and reactive authwall recovery directly into `PlaywrightFetcher` and `cli.py`.
  - **Directory Isolation**: Stored all generated profile JSONs inside `output/profiles/<slug>.json`, keeping root `output/` clean for `run_summary.json` and reports.
- **Verification**: Verified on real profiles (`rohit-chauhan-464b15262`, `gaurav-akoliya-aa8a3636a`, `sundarpichai`, and batch of 8 candidates).

---

## Phase 9: Static Review Remediation & Pipeline Hardening
- **Status**: Completed
- **Done**:
  - **Item 1 (Destructive Cleanup)**: Made stale profile pruning strictly opt-in via `--prune-stale` (default `False`). Guaranteed it never executes if valid profiles count is zero or if a run was interrupted/capped.
  - **Item 2 (Slug Sanitization & Filename Collision)**: Implemented `profile_filename_stem()` as single canonical truth. Added deterministic SHA256 collision hashing for colliding sanitized stems.
  - **Item 3 (CLI Config Precedence)**: Loaded `config.toml` before path resolution with deterministic 4-tier precedence: CLI args > `.run_state.json` > `config.toml` > defaults.
  - **Item 4 (Deep-Page Circuit Breaker)**: Subpage fetches (`/details/skills/`, etc.) now pass through status check and trigger circuit breaker on `BLOCKED` outcomes.
  - **Item 5 (Summary Accounting Reconciliation)**: Capped rows from `max_profiles` are counted under `skipped_by_limit` and added to `not_processed_count`.
  - **Item 6 (HTTP Status Code Classification)**: Explicit classification in `detect_outcome()` for 401, 403, 404, 410, 429, 999, 5xx, and other 4xx codes.
  - **Item 7 (Error Page Rejection)**: Added rejection pattern in `parser.py` for generic error headings (`500 Internal Server Error`, etc.) and mandated profile signals before marking `Status.OK`.
  - **Item 8 (Dual-Container Scroll Tracking)**: Evaluated scroll movements across both `window` and `main` elements to prevent premature exit on lazy-loaded layouts.
  - **Item 9 (Worksheet Ranking Heuristic)**: Worksheet selector scores actual valid LinkedIn URL counts instead of naive `"data"` substring match.
  - **Item 10 (URL Normalization Case & Encoding)**: Handled uppercase schemes (`HTTPS://`) and encoded slashes (`%2F` replaced with `-`).
  - Added dedicated test suite `tests/test_review_defects.py` covering all 10 items.
- **Verification**: All 73 tests passing (`pytest -m "not live"`), `mypy src tests` passing (0 errors across 28 files), `ruff check .` passing (0 errors).

---

---

## Project Definition of Done Checklist
- [x] One JSON per reachable profile, valid against the contract, in one folder
- [x] Unreachable profiles marked in JSON and listed in `unreachable_profiles.xlsx`
- [x] `run_summary.json` accounts for every row (the counts add up mathematically)
- [x] No hardcoded profile count; works for 1 row and 1000 rows
- [x] Tests, lint, and type checks pass (77 tests, 0 ruff errors, 0 pyright errors, 0 mypy errors)
- [x] No personal profile data in logs or in the repository; operational logs record only URLs and slugs
- [x] `README.md`, `docs/ARCHITECTURE.md`, and `docs/HANDOVER.md` complete, with limitations stated honestly
- [x] Demo rehearsed once, including the fallback mode
