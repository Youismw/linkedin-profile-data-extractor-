# LinkedIn Profile Extractor

> [!WARNING]
> **PROTOTYPE / PROOF-OF-CONCEPT NOTICE**
> This tool's browser automation via Playwright using an employee session is **strictly a temporary proof-of-concept (PoC)** built for an internship demonstration on 5–6 consenting employee profiles. It is **not** approved or architected as-is for production deployment. See [Production Deployment Roadmap](#production-deployment-roadmap--permanent-architecture) below for what is required before any production rollout.

---

## Overview

The **LinkedIn Profile Extractor** is a command-line tool designed to process a list of LinkedIn profile URLs from an Excel spreadsheet (`.xlsx`), fetch and parse profile contents into structured and contract-compliant JSON documents, and generate comprehensive run reports.

### Key Capabilities
- **Tolerant Excel Reading**: Automatically recognizes headers, handles blank rows, resolves cell hyperlinks, and deduplicates repeated profile URLs.
- **Data Contracts**: Writes one atomic, indented `<slug>.json` document per profile. Missing important fields are explicitly cataloged in `meta.missing_fields`.
- **Fault-Tolerant Batching**: Failure on an individual profile never halts the entire batch.
- **Unreachable Reporting**: Profiles that are inaccessible (`invalid_url`, `not_found`, `not_visible`, `blocked`, `timeout`, `parse_failed`, `unexpected_error`) generate an explicit record in `unreachable_profiles.xlsx`.
- **Polite Execution**: Configurable random delays between requests (default 1.0–2.0s; fast mode 0.5–1.0s) and automatic cessation (`stop_on_block = true`) upon encountering challenges or warnings to protect accounts.
- **Data Privacy & Operational Logging**: Candidate personal profiles (names, bios, contact details, work history, skills) are strictly confined to the output directory and never printed in logs or committed to version control. Operational logs record only row numbers, URLs, and slugs for diagnostic traceability.

---

## 🛠️ Technology Stack & Versions

| Category | Tool / Library | Version | Core Purpose & Architecture Role |
|---|---|---|---|
| **Core Runtime** | **Python** | `>=3.13` (Tested on 3.13 / 3.14) | Core language runtime with modern union syntax (`X \| None`), pattern matching, and dataclasses. |
| **Browser Automation** | **Playwright** | `1.57.0` | Chromium automation engine for rendering LinkedIn client-side SPAs and capturing authentic 2FA sessions. |
| **Data Contracts** | **Pydantic** | `2.12.5` | Strict schema validation, missing field detection, and immutable serializable data transfer objects. |
| **HTML Parsing** | **BeautifulSoup4** | `4.15.0` | Semantic DOM traversal, noise tag stripping, and heuristic attribute inspection. |
| **HTML Parsing Engine** | **lxml** | `6.1.3` | High-performance C-accelerated tree builder for BeautifulSoup. |
| **Spreadsheet I/O** | **openpyxl** | `3.1.5` | Ingests candidate `.xlsx` workbooks, resolves formulas (`=HYPERLINK`), and generates audit sheets. |
| **Test Automation** | **pytest** | `8.4.2` | Automated unit, contract, regression, and integration test suite (98 passing tests; one auth-dependent browser test is skipped when no saved session exists). |
| **Code Quality / Linter** | **Ruff** | `0.15.0` | High-speed Rust-based code formatter and linter enforcing PEP 8 compliance. |
| **Static Type Checking** | **Pyright & Mypy** | `1.1.408` / `2.4.1` | Static typing enforcement across all 28 source files (0 errors). |

---

## ⚡ Execution Commands Reference (What Each Does)

| Command | Shorthand | What It Does & When to Use |
|---|---|---|
| `.\run.bat` | `.\run` | **Primary 1-Click Runner**: Checks LinkedIn session health. If missing/expired, opens a visible browser for 2FA login. Automatically picks up input from `input/*.xlsx`, processes profiles with polite human delays (1.0–2.0s), preserves prior outputs by default, saves JSON files to `output/profiles/`, and **automatically opens the output folder in Windows Explorer**. |
| `.\run.bat "<input>" "<output>"` | `.\run "in.xlsx" "out"` | **Custom Paths Runner**: Runs on an input file anywhere on the machine and writes to any output folder. **Persists these paths** in `.run_state.json` so future bare `.\run` runs remember them without retyping. |
| `.\run.bat "output\<out_dir>"` | `.\run "output\C:\path"` / `.\run "C:\path\output"` | **Output-Only Override**: Changes the output folder without re-specifying the input file. Recognizes `output\` prefix or `\output` suffix, keeping the default/remembered spreadsheet. |
| `.\run.bat --prune-stale` | `.\run --prune-stale` | **Safe Stale File Cleanup**: Opt-in flag to delete JSON files from previous batches that are no longer in the input spreadsheet. Disabled by default to prevent accidental data loss. |
| `.\run.bat --fake` | `.\run --fake` | **Offline / Demo Mode**: Executes the entire pipeline against simulated synthetic profile fixtures with zero network calls. Perfect for safe presentations and testing. |
| `.\run.bat --fast` | `.\run --fast` | **High-Speed Mode**: Reduces polite delays between profiles to 0.5s–1.0s (instead of 1.0s–2.0s) for rapid testing or swift demos. |
| `.\run.bat --max-profiles 3` | `.\run --max-profiles 3` | **Profile Cap**: Stops processing after processing the specified number of valid profiles; unvisited valid profiles are reconciled in `not_processed_count`. |
| `.\run.bat login` | `python -m profile_extractor login` | **Stand-alone Login Helper**: Explicitly opens a visible Chromium browser to `https://www.linkedin.com/login` and exports active cookies to `.auth/state.json`. |
| `.\run.bat logout` | `.\run logout` / `.\run clean-session` | **Clean Session**: Manually clears saved `.auth/state.json` so future runs prompt for login (pass `--all` to also reset remembered paths). |
| `.\run.bat --help` | `.\run -h` | **Help & Manual**: Prints all available arguments, options, and flags. |
| `.\extract.bat` | — | **Legacy Runner**: Retained for backward-compatibility; delegates directly to the extraction engine. |
| `pytest -m "not live"` | — | **Automated Test Suite**: Executes the offline unit, contract, regression, and integration suite (98 passed; one browser-session test skipped when no saved session exists). |
| `ruff check src tests` | — | **Lint Check**: Validates PEP 8 style, import ordering, and code cleanliness (0 errors). |
| `pyright src tests` | — | **Type Engine Verification**: Verifies static typing across the entire codebase (0 errors). |
| `mypy src tests` | — | **Strict Static Typing**: Runs Mypy static analysis across all 28 source files (0 errors). |

---

---

## Architecture: Current PoC vs. Production Target

```
[Current Temporary PoC]
Excel (.xlsx) ──> Reader ──> Orchestrator ──> Playwright (Chromium + Employee Session)
                                   │
                                   ├──> Atomic JSON Writer (<slug>.json)
                                   ├──> Unreachable Report (unreachable_profiles.xlsx)
                                   └──> Run Summary (run_summary.json)

[Production Target Architecture]
ATS / Database ──> Message Queue ──> Worker ──> Licensed API / Official Partner Provider
                                       │         (Zero Scraping / Fully Compliant)
                                       ├──> Encrypted Database (TTL / Right to Erasure)
                                       └──> Observability & Audit Trail
```

---

## Production Deployment Roadmap & Permanent Architecture

Before this tool can be deployed or used in production workflows, the following architectural and governance requirements must be fulfilled:

### 1. Replacing Browser Scraping with a Compliant Data Provider
- **The Problem**: Web scraping LinkedIn with an authenticated user account violates LinkedIn's Terms of Service (Section 8.2) and introduces severe operational fragility (layout changes, CAPTCHAs, account bans).
- **The Solution**: The codebase decouples fetching through the `Fetcher` protocol (`src/profile_extractor/fetcher/__init__.py`). In production, `PlaywrightFetcher` must be swapped for:
  - **Official LinkedIn APIs** (e.g., LinkedIn Talent Solutions / Enterprise Partner API with client OAuth 2.0).
  - **Licensed B2B Data Providers** (e.g., People Data Labs, Proxycurl, Bright Data) that provide compliant API access and legal indemnification.
  - *No core pipeline or parsing code needs to change—only the Fetcher implementation.*

### 2. Legal & Regulatory Compliance
- **Applicant Consent & Notice**: Under GDPR (Articles 6 & 13) and CCPA/CPRA, automated collection of candidate profile data requires an explicit lawful basis (e.g., legitimate interest with documented balancing test, or candidate consent during job application).
- **Terms of Service Review**: Corporate legal counsel must review the chosen data ingestion pathway.

### 3. Data Retention, Security, and Governance
- **Data Retention Policy**: Candidate personal data must have an automated Time-To-Live (TTL) or retention schedule (e.g., purging records after 90 days unless active application is ongoing).
- **Data Subject Rights**: Support for GDPR Right to Erasure ("Right to be Forgotten") and Right of Access.
- **Secret Management**: Replace local `.auth/state.json` with enterprise secrets management (e.g., AWS Secrets Manager, HashiCorp Vault) and OAuth 2.0 tokens.
- **Storage Encryption**: Output files or records must reside on encrypted volumes (AES-256) with strict role-based access control (RBAC).

### 4. Infrastructure & Scalability
- **Asynchronous Task Queue**: Transition from single-machine synchronous CLI to distributed workers (Celery / Redis / AWS SQS) with dynamic rate-limiting.
- **Persistent Storage**: Replace local JSON files with a centralized database (PostgreSQL / MongoDB).
- **Observability**: Metrics and alerts via Prometheus, OpenTelemetry, and Grafana.

---

## Installation & Setup

### Requirements
- Python 3.13 or newer
- Virtual environment (`.venv`)

### Setup Commands
```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
.\.venv\Scripts\python.exe -m pip install -r requirements.lock

# 3. Install Playwright Chromium browser binary
.\.venv\Scripts\python.exe -m playwright install chromium
```

---

## Usage

### The Two Shorthand Commands
Two root commands are provided for quick and simple execution:
- **`.\run`**: Primary command launcher.
- **`.\extract`**: Identical alias command.

Both work directly from PowerShell or Command Prompt:

```powershell
# 1. Default Run (uses last-remembered paths or input/profiles.xlsx -> output/)
.\run

# 2. Or using the alias command
.\extract

# 3. Specify an input file (remembers path for future runs automatically)
.\run "C:\path\to\candidates.xlsx"

# 4. Specify an input folder (automatically picks the most recently modified .xlsx)
.\run "C:\path\to\folder_with_spreadsheets"

# 5. Specify input AND custom output folder anywhere on the machine
.\run "C:\path\to\candidates.xlsx" "D:\MyData\ExtractedProfiles"

# 6. Fast mode for quick testing
.\run --fast
```

> [!TIP]
> **Automatic Path Memory & Safe Data Preservation**:
> - **Path Memory**: Whenever you specify a custom input file or output directory, the tool saves them in a local `.run_state.json` file. Future runs (`.\run` or `.\extract`) automatically remember and reuse your chosen paths without needing to re-type them!
> - **Safe Data Preservation by Default**: Old `.json` profile files in the output directory are preserved by default. To clean up stale profiles from older runs that are no longer in the current input spreadsheet, pass `--prune-stale` (pruning only executes after an uninterrupted run with valid profiles).
> - **Single Window Pop-Up**: When extraction completes, a single Windows File Explorer window pops open directly to the extracted `profiles/` directory.

---

### Profile Container Cleaned Text Dump (`raw_text`)
Each extracted JSON profile in `output/profiles/<slug>.json` contains both structured parsed fields and a clean visible text dump of the candidate profile container at the end. Non-content noise elements (scripts, stylesheets, templates, modals, dialogs, navigation bars, and language selector footers) are stripped before capturing the visible text from the profile's `<main>` container:

```json
{
  "meta": {
    "source_url": "https://www.linkedin.com/in/rohit-chauhan-464b15262/",
    "slug": "rohit-chauhan-464b15262",
    "status": "ok",
    "missing_fields": ["experience"]
  },
  "profile": {
    "name": "Rohit Chauhan",
    "headline": "Dr. Akhilesh Das Gupta Institute of Professional Studies",
    "location": "Delhi, India",
    "about": "René Descartes famously said...",
    "education": [...],
    "skills": [...]
  },
  "other_sections": [...],
  "raw_text": "Rohit Chauhan\nDr. Akhilesh Das Gupta Institute of Professional Studies\nDelhi, India\nAbout\nRené Descartes famously said...\nActivity..."
}
```

---

### Automatic Session Expiry & Relogin
You do not need to run a separate login command. 

Whenever you run `.\run` or `.\extract`, the tool automatically checks if your LinkedIn session is active:
- **If your session is active**: Extraction starts immediately.
- **If your session has expired or is missing**: The tool automatically displays:
  ```text
  ======================================================================
  [!] LinkedIn session not found or has expired.
      Opening LinkedIn login window now. Please sign in to continue...
  ======================================================================
  ```
  It opens a visible browser window directly to `https://www.linkedin.com/login`. You sign in (including 2FA), press **[ENTER]** in your terminal, and the tool immediately saves the fresh session and begins extracting your profiles automatically.

---

## Configuration (`config.toml`)

```toml
input_file = "input/profiles.xlsx"
output_dir = "output"
auth_state_path = ".auth/state.json"

important_fields = [
  "name", "headline", "location", "about",
  "experience", "education", "skills"
]

delay_seconds_min = 1.0
delay_seconds_max = 2.0
page_timeout_seconds = 30.0
max_profiles = 0          # 0 or null for unlimited
headless = true
deep_details = true       # Unrolls full skills, experience, and education
prune_stale = false       # Disabled by default to protect existing output
stop_on_block = true      # Halts immediately upon CAPTCHA/challenge
log_level = "INFO"
```

### Configuration Precedence Order
When settings are defined across multiple sources, parameters are evaluated with deterministic precedence:
1. **Explicit CLI Arguments**: Options passed on the command line (e.g. `--input`, `--output`, `--prune-stale`, `--fast`, `--max-profiles`).
2. **Remembered Run State**: Saved paths in `.run_state.json` from previous executions.
3. **Configuration File**: Values specified in `config.toml` (or custom TOML passed with `-c` / `--config`).
4. **Application Defaults**: Built-in defaults in `ExtractorConfig`.

---

## Output Contract & Formats

### Profile JSON (`<output_dir>/<slug>.json`)
```json
{
  "meta": {
    "schema_version": "1.0",
    "source_url": "https://www.linkedin.com/in/jane-doe-12ab/",
    "slug": "jane-doe-12ab",
    "status": "ok",
    "reason": null,
    "fetched_at": "2026-11-04T09:30:00Z",
    "missing_fields": ["skills"]
  },
  "profile": {
    "name": "Jane Doe",
    "headline": "Staff Software Engineer",
    "location": "San Francisco Bay Area",
    "about": "Distributed systems...",
    "experience": [
      {
        "title": "Senior Engineer",
        "company": "Acme",
        "dates": "2020 - Present",
        "raw": "Senior Engineer at Acme..."
      }
    ]
  },
  "other_sections": [
    {
      "heading": "Certifications",
      "text": "AWS Solutions Architect"
    }
  ]
}
```

### Unreachable Profiles (`<output_dir>/unreachable_profiles.xlsx`)
Created only when at least one profile cannot be retrieved. Columns:
- `input_row`: Original Excel row number.
- `url_as_given`: Raw input cell contents.
- `slug`: Extracted slug (or empty if URL was invalid).
- `reason`: Enum (`invalid_url`, `not_found`, `not_visible`, `blocked`, `timeout`, `parse_failed`, `unexpected_error`).
- `detail`: Diagnostic explanation.
- `timestamp_utc`: ISO timestamp.

### Failure Reasons & Diagnostic Meanings

| Reason Enum | Meaning & Typical Cause | Output Produced |
| :--- | :--- | :--- |
| `invalid_url` | Malformed URL or non-LinkedIn link (cannot determine `/in/<slug>`). | Excel report only (no slug for JSON filename). |
| `not_found` | Profile does not exist, was deleted, or changed username (HTTP 404). | `<slug>.json` (empty profile) + Excel report row. |
| `not_visible` | Profile is restricted to 1st-degree connections or redirected to authwall. | `<slug>.json` (empty profile) + Excel report row. |
| `blocked` | Encountered CAPTCHA, Arkose challenge, rate limit, or account warning. | `<slug>.json` + Excel row; triggers run halt if `stop_on_block=true`. |
| `timeout` | Page navigation or DOM content loading exceeded `page_timeout_seconds`. | `<slug>.json` (empty profile) + Excel report row. |
| `parse_failed` | Page loaded but neither candidate name (`h1`) nor headline was found. | `<slug>.json` (empty profile) + Excel report row. |
| `unexpected_error`| Unhandled network drop or OS exception during row processing. | `<slug>.json` (empty profile) + Excel report row. |

---

## How to Swap the Fetcher (Extensibility)

The codebase strictly decouples fetching through the `Fetcher` protocol in [`src/profile_extractor/fetcher/__init__.py`](file:///src/profile_extractor/fetcher/__init__.py).

To plug in an official API or licensed data provider in production, implement the two-method protocol:

```python
from profile_extractor.models import FetchResult, Status

class LicensedProviderFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def fetch(self, url: str) -> FetchResult:
        # 1. Query licensed B2B provider API
        # response = requests.get(f"https://api.provider.com/v1/profile?url={url}", headers={"Authorization": self.api_key})
        # 2. Return FetchResult with html or mapped data
        return FetchResult(
            url=url,
            status=Status.OK,
            html="<html>...rendered payload...</html>",
        )

    def close(self) -> None:
        pass  # cleanup connection pool
```

Pass your custom fetcher directly to `run_pipeline(config, fetcher)` in [`cli.py`](file:///src/profile_extractor/cli.py). Zero parsing, validation, or Excel logic needs to change.

---

## Known Limitations (v1 Prototype)

1. **Volume Restriction**: Intended strictly for demo scale (5–6 profiles). Not designed for bulk extraction without official provider APIs.
2. **Date Parsing**: Dates (e.g., "Jan 2020 - Present") are preserved as displayed text strings without datetime serialization in v1.
3. **No Dynamic Resume / Caching**: Re-running the pipeline processes all rows from the beginning and atomically replaces existing output files.
4. **Single-threaded Sequential Execution**: Profiles are fetched sequentially with deliberate delays to maintain polite pacing and avoid automated rate limiting.

---

## Testing & Quality Assurance
Accounts for every single row processed:
```json
{
  "started_at": "2026-11-04T09:00:00Z",
  "finished_at": "2026-11-04T09:05:00Z",
  "duration_seconds": 300.0,
  "rows_read": 10,
  "rows_valid": 8,
  "rows_invalid": 2,
  "duplicates_skipped": 0,
  "ok_count": 7,
  "unreachable_count": 3,
  "unreachable_by_reason": {
    "invalid_url": 2,
    "not_found": 1
  },
  "not_processed_count": 0,
  "output_dir": "output",
  "tool_version": "0.1.0"
}
```

---

## Testing & Quality Assurance
```powershell
# Run full unit and integration test suite
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe -m pytest

# Run lint and formatting check
.\.venv\Scripts\python.exe -m ruff check .

# Run static type analysis
.\.venv\Scripts\python.exe -m pyright src tests
```

## Recording-driven capture hardening (current working version)

The Playwright fetcher now snapshots profile-card containers before scrolling and
after each scroll/settle step. It retains the richest observed version of each card
and composes those cards into the HTML passed to the existing parser. This is
intended to prevent cards that disappear from the final DOM during LinkedIn's
client-side page updates from being lost. Both the newer SDUI card IDs and the
older `profileCardsAboveActivity*` / `profileCardsBelowActivity*` wrappers are
handled. If no cards are detected, the fetcher falls back to the original final
page HTML.

This change preserves the existing parser's structured-field, `other_sections`,
and `raw_text` behaviour; it does not force every visible LinkedIn card into a
named field. Unclassified sections remain available through `other_sections` and
the raw text dump. An additional narrow cleanup prevents LinkedIn's “helped me get
this job” UI badge from being treated as a work-experience description.

### Additive same-profile detail discovery

After the original profile parse and the existing six known detail-page fetches,
the orchestrator performs a bounded supplemental pass over direct links found on
the original profile page. It accepts only same-profile LinkedIn `/details/<section>/`
routes and `/recent-activity/{all,comments}/` routes. It does not recursively crawl
links found on detail pages, and it caps supplemental requests at 12 per profile.

Already-attempted legacy detail sections are not fetched a second time. Known
sections are parsed and merged additively; matching records fill empty fields,
while distinct records are appended. Unknown sections are retained in
`other_sections` and appended to `raw_text` with their source URL. The original
profile-page data is restored additively after the legacy pass, so a partial deep
page response cannot delete data captured on the profile page.

Offline verification: **113 passed, 1 skipped**. The skipped test needs an
authenticated Playwright browser session. Dynamic discovery has been tested with
mocked pages and the saved fixtures, but has not been exercised against a live
authenticated LinkedIn session in this environment. See
[`RECORDING_REVIEW.md`](RECORDING_REVIEW.md) for recording-derived capture findings
and limits.
