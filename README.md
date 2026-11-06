# LinkedIn Profile Extractor

> [!WARNING]
> **PROTOTYPE / PROOF-OF-CONCEPT NOTICE**
> This tool's browser automation via Playwright using an employee session is **strictly a temporary proof-of-concept (PoC)** built for an internship demonstration on 5–6 consenting employee profiles. It is **not** approved or architected as-is for production deployment. See [Production Deployment Roadmap](#production-deployment-roadmap--permanent-architecture) below for what is required before any production rollout.

> [!CAUTION]
> ### 🛑 Anti-Bot Rate Limits & Speed Boundaries (Must-Read)
> **There is a strict operational limit on how fast extraction can run before LinkedIn flags the session as a bot.**
>
> LinkedIn's anti-bot infrastructure (Akamai / PerimeterX) dynamically monitors client interaction speeds. While LinkedIn does not publish static rate limits, the following speed thresholds apply:
> 1. **Burst Navigation Limit**: No more than **6–8 full-page requests within a 90–120 second window** from the same account session.
> 2. **Human Interaction Floor**: Scroll step pauses under **`150ms`** (and especially **`50ms`**) trigger client telemetry alarms because no human scrolls down an entire 15,000px page in under a second.
>
> #### ⚠️ Real Test Incident: Flagged with HTTP 999
> During testing, we attempted to run at maximum speed with these exact terminal flags:
> ```powershell
> .\run "C:\Users\ROHIT CHAUHAN\Desktop\outputfast\output" --scrollsteps 15 --steptime 50 --disextra
> ```
> **What Happened**:
> * Across the first 3 profiles, the script made **9 rapid page loads in ~100 seconds** (main profile, skills, education, certifications), each scrolled at 50ms per step.
> * On the 9th page request (`Row 4: certifications subpage`), LinkedIn intercepted the session and returned **`HTTP 999 (Request Denied / Rate Throttled)`**.
> * The tool's safety guard (`stop_on_block=True`) immediately halted the run, safely saving the 3 already-extracted profiles and preventing the account from escalating to an Arkose CAPTCHA challenge or suspension.
>
> #### ✅ Safe Operational Guidelines
> * **Never set `--steptime` below `150ms`**: We recommend **`200ms`–`250ms`** (default: `250ms`).
> * **Maintain human polite delays**: Keep inter-profile delays at least **`1.0s`–`2.0s`** (or `0.5s`–`1.0s` in `--fast`).
> * **For maximum safe speed, skip subpages**: Use **`--no-deep`** to visit only the main profile (1 request per person, ~5–8s per profile). This eliminates 75% of page loads and avoids triggering subpage burst rate limits.

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
| `.\run_gui.bat` / `.\gui.bat` | `.\run gui` | **Desktop GUI Assistant**: Launches the native Tkinter graphical interface with file/folder pickers, interactive speed/depth sliders, toggle flares, inline button explanations, and live streaming console logs. |
| `.\run.bat "<input>" "<output>"` | `.\run "in.xlsx" "out"` | **Custom Paths Runner**: Runs on an input file anywhere on the machine and writes to any output folder. **Persists these paths** in `.run_state.json` so future bare `.\run` runs remember them without retyping. |
| `.\run.bat "output\<out_dir>"` | `.\run "output\C:\path"` / `.\run "C:\path\output"` | **Output-Only Override**: Changes the output folder without re-specifying the input file. Recognizes `output\` prefix or `\output` suffix, keeping the default/remembered spreadsheet. |
| `.\run.bat --prune-stale` | `.\run --prune-stale` | **Safe Stale File Cleanup**: Opt-in flag to delete JSON files from previous batches that are no longer in the input spreadsheet. Disabled by default to prevent accidental data loss. |
| `.\run.bat --fake` | `.\run --fake` | **Offline / Demo Mode**: Executes the entire pipeline against simulated synthetic profile fixtures with zero network calls. Perfect for safe presentations and testing. |
| `.\run.bat --fast` | `.\run --fast` | **High-Speed Mode**: Reduces polite delays between profiles to 0.5s–1.0s (instead of 1.0s–2.0s) for rapid testing or swift demos. |
| `.\run.bat --disextra` | `.\run --disextra` | **Fast Mode (Skip Extras)**: Skips supplemental discovery crawls (`/recent-activity/` and `/details/interests/`) to cut extraction time from ~2 minutes per profile down to ~15-20 seconds while retaining full profile, skills, experience, and education. |
| `.\run.bat --scrollsteps 25` | `.\run --scrollsteps <x>` | **Scroll Depth Limit**: Sets maximum progressive scroll steps (0 to 100, default: `25`). **Trade-off**: More steps = more time, fuller data; fewer steps = faster but emptier. (~25 scrolls captures ~80% of data, ~40 scrolls captures ~90%). |
| `.\run.bat --steptime 250` | `.\run --steptime <ms>` | **Scroll Step Pause**: Delay between scroll increments in milliseconds (default: `250`). **Pros**: Lower time (e.g. 150ms) runs significantly faster. **Cons**: If set too low, dynamic lazy-loaded sections won't load in time before scrolling past. |
| `.\run.bat --aboutwait 1000` | `.\run --aboutwait <ms>` | **About Section Ready Wait**: Maximum wait time in milliseconds for the candidate About card and shell to hydrate before progressive scrolling begins (default: `1000`). Stops waiting immediately once loaded. |
| `.\run.bat --polite 1.5` | `.\run --polite <sec>` | **Polite Delay Between Profiles**: Target delay between visiting consecutive candidate profiles in seconds (default: `1.5s`). Paces requests to mimic natural human browsing and avoid bot detection. |
| `.\run.bat --workers 3` | `.\run -w <N>` | **Parallel Multi-Worker Mode**: Divides candidates across `N` concurrent Playwright browser sessions (1 to 10, default: `1`). Prompts sequential 2FA login for each worker account (`.auth/state_worker_N.json`) with remainder fairly distributed to earlier workers (e.g. 13 IDs / 3 workers -> [5, 4, 4]). |
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
disextra = false           # When true, disables activity and interests discovery for much faster runs
scroll_steps = 25         # Scroll steps per page (0 to 100, default: 25)
                          # Trade-off: More steps = more time, fuller data; fewer steps = less time, emptier data.
                          # Rule of thumb: ~25 scrolls captures ~80% of data, ~40 scrolls captures ~90% of data.
scroll_step_delay_ms = 250 # Pause between scroll increments in ms (default: 250)
                          # Pros: Lower value (e.g. 150ms) yields significantly faster runtime per profile.
                          # Cons: If set too low, dynamic lazy-loaded sections won't load in time before scrolling past.
prune_stale = false       # Disabled by default to protect existing output
stop_on_block = true      # Halts immediately upon CAPTCHA/challenge
log_level = "INFO"
```

### Configuration Precedence Order
When settings are defined across multiple sources, parameters are evaluated with deterministic precedence:
1. **Explicit CLI Arguments**: Options passed on the command line (e.g. `--input`, `--output`, `--scrollsteps <0-100>`, `--steptime <ms>`, `--disextra`, `--prune-stale`, `--fast`, `--max-profiles`).
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

## Debugging Directive: Look at DOM Issues from the SDUI Angle First

> **CRITICAL LESSON LEARNED**: Whenever diagnosing missing extraction data, failing locators, or modals that visibly render in the browser but fail to extract into JSON, **always inspect the DOM from the Server-Driven UI (SDUI) angle first**.
>
> Modern LinkedIn desktop has systematically migrated from legacy Artdeco containers (`#artdeco-modal-outlet`, `.artdeco-modal`, `[role="dialog"]`, `section.pv-contact-info`) to SDUI components:
> - `data-sdui-screen` (e.g., `com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay`)
> - `id="com.linkedin.sdui.profile.card.*"`
> - `componentkey` and `data-testid` (e.g., `data-testid="dialog-content"`, `header#dialog-header`)
>
> Days of debugging have been lost attempting to tweak click timings or scroll limits when the actual issue was querying deprecated Artdeco classes that LinkedIn no longer renders. Always prioritize SDUI attributes first, using legacy selectors only as fallback.

---

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

---

## Desktop GUI Assistant

The tool includes a lightweight, native graphical user interface (`gui.bat` / `run_gui.bat` or `.\run gui`) built with Python's standard `tkinter` library. It makes configuring crawler flags ("flares") effortless, allowing you to select spreadsheets and destination folders via mouse pickers and tune speed settings without memorizing command-line syntax.

### Key GUI Features & Inline Explanations
* **File & Folder Pickers**: Select input candidate spreadsheets (`.xlsx`, `.xlsm`, `.csv`) and output directories with native Windows dialogs. Synchronizes automatically with `.run_state.json` so CLI and GUI share remembered paths.
* **Inline Explanations Below Every Control**:
  * **Disable Supplemental Feeds (`--disextra`)**: *"Skips /recent-activity/ and /details/interests/ for 3x–4x faster run. Recommended for resume extraction."*
  * **Scroll Depth Steps (`--scrollsteps`)**: Slider (0 to 100, default `25`). *"25 steps gets ~80% of lazy DOM; 40 steps gets ~90%+. Fewer steps = faster, more steps = fuller data."*
  * **Scroll Step Delay (`--steptime`)**: Slider (50 to 1000 ms, default `250`). *"Delay between scrolls. Faster vs safer. Keep >= 150ms to avoid bot rate limits."*
  * **About Section Ready Wait (`--aboutwait`)**: Slider (0 to 5000 ms, default `1000`). *"Max wait for About card to hydrate. Stops immediately once loaded."*
  * **Polite Delay Between Profiles (`--polite`)**: Slider (0.0 to 10.0 s, default `1.5`). *"Pause between visiting candidates. Lower = faster batch, higher = safer against bot tracking."*
  * **Single-Page Fast Mode (`--fast` / `--no-deep`)**: *"Extracts top card only without visiting deep subpages (~5s/profile)."*
  * **Show Chromium Browser (`--visible`)**: *"Opens browser visibly so you can watch live page navigation."*
  * **Prune Old Profiles (`--prune`)**: *"Clears old JSON profiles from output directory before starting."*
* **Process Controls**:
  * **[ ▶ Start Extraction ]**: Spawns the pipeline with real-time stdout streaming into a dark-themed log console.
  * **[ ⏹ Stop ]**: Terminate button that cleanly kills the process tree (including any spawned Chromium browser processes).
  * **[ 🔑 LinkedIn Login ]**: Triggers interactive login browser helper if session cookies expire.
  * **[ 🗑 Purge Presets & Login ]**: One-click reset that safely deletes `.auth/state.json`, clears `.run_state.json`, and resets spreadsheet and output folder paths back to defaults.
  * **[ 📂 Open Output Folder ]**: Directly reveals the output directory in Windows Explorer.
* **Launching the GUI**:
  ```powershell
  # Method 1: Double-click either batch file from Windows Explorer or run in terminal
  .\run_gui.bat
  .\gui.bat

  # Method 2: Use the standard runner
  .\run gui
  ```

---

## 📊 Performance Benchmarks: Fast Mode vs. Max Depth Comparison

The table below documents a live benchmark conducted across 7 real candidate profiles comparing **Fast Mode** (`output` folder, ~4.5 minutes) against **Maximum Depth Mode** (`outputslow` folder, ~32.5 minutes).

### High-Level Benchmark Summary

| Metric | Fast Run (`output`) | Max Slow Run (`outputslow`) | Variance / Difference |
| :--- | :--- | :--- | :--- |
| **Total Duration** | **4 min 30 sec** (270.61s) | **32 min 27 sec** (1,947.44s) | **7.2× slower** in Max Mode |
| **Success Rate** | 7 of 7 OK (100%) | 7 of 7 OK (100%) | Identical reliability |
| **Core Identifiers** | Name, Headline, Location | Name, Headline, Location | **100% byte-for-byte identical** |
| **Work Experience** | Complete titles, dates, descriptions | Complete titles, dates, descriptions | **100% byte-for-byte identical** |
| **Education History** | Degrees, institutions, fields | Degrees, institutions, fields | **100% byte-for-byte identical** |
| **Certifications** | Licenses & certifications | Licenses & certifications | **100% byte-for-byte identical** |
| **About Section** | Complete summary text | Complete summary text | **100% byte-for-byte identical** |
| **Total Skills Extracted** | **140 skills** | **288 skills** | **+148 additional skills** |
| **Activity Feed Dump** | Skipped (`--disextra`) | Dumped into `raw_text` | All public posts & shares captured |
| **Total Output Size** | **205 KB** across 7 files | **678 KB** across 7 files | **+473 KB (3.3× larger)** |

---

### Candidate-by-Candidate Breakdown Table

| Candidate Profile | Fast Size | Max Slow Size | Work Experience | Education | Certifications | Skills (Fast vs Slow) | Activity Feed in `raw_text` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Avnish Kotambkar** | 45.4 KB | 94.0 KB | 4 vs 4 | 4 vs 4 | 9 vs 9 | 20 vs **31** (+11) | +20k characters |
| **Gaurav Akoliya** | 12.2 KB | 18.6 KB | 2 vs 2 | 2 vs 2 | 3 vs 3 | 24 vs 24 (=) | +4k characters |
| **Harsh Shukla** | 61.0 KB | 92.1 KB | 3 vs 3 | 5 vs 5 | 8 vs 8 | 20 vs **92** (+72) | +7k characters |
| **Shruti Dwivedi** | 15.0 KB | 172.0 KB | 2 vs 2 | 2 vs 2 | 1 vs 1 | 27 vs 27 (=) | +57k characters (Full Posts) |
| **Sujeet Patel** | 9.7 KB | 17.4 KB | 5 vs 5 | 1 vs 1 | 0 vs 0 | 19 vs 19 (=) | +4k characters |
| **Sundar Pichai** | 30.4 KB | 205.7 KB | 2 vs 2 | 3 vs 3 | 0 vs 0 | 0 vs 0 (=) | +76k characters (Full Posts) |
| **Vishal Soam** | 16.9 KB | 47.7 KB | 4 vs 4 | 1 vs 1 | 0 vs 0 | 30 vs **95** (+65) | +15k characters |

---

### Key Analytical Takeaways

1. **What is 100% Identical:**
   - Candidate personal details (Name, Headline, Location, Profile URLs).
   - Complete Work Experience history (Company names, job titles, start/end dates, role summaries).
   - Complete Education history (Degree names, universities, graduation years).
   - Licenses and certifications.
   - Complete About summary text.
   - **Verdict**: For recruitment matching, resume parsing, and applicant screening, Fast Mode provides 100% of the requisite employment and education data.

2. **What Causes the Difference in Max Mode:**
   - **Virtualized Skills Unrolling**: LinkedIn virtualizes its `/details/skills/` page to load only ~20–30 skills in memory at a time. Fast Mode captures the top 20–30 skills. In Max Mode (100 scroll steps + higher step pause), the crawler unrolls all 90+ skills (e.g. Harsh Shukla gained 72 skills including *LangChain, LangGraph, React.js, MySQL, NLP, Agents, Chart.js*).
   - **Supplemental Activity Feeds**: When `--disextra` is disabled, the crawler visits `/recent-activity/all/` and records pages of public posts, reactions, and shares into `raw_text` (accounting for the 170KB–200KB file sizes on high-activity profiles).

3. **Recommended "Sweet Spot" Configuration:**
   To capture deep 90+ skills without waiting 32 minutes or scraping noisy social activity feeds:
   - **Disable Supplemental Feeds (`--disextra`)**: Checked (saves ~15–20 minutes by skipping social feeds).
   - **Scroll Steps (`--scrollsteps`)**: `40`–`50` steps.
   - **Scroll Step Time (`--steptime`)**: `200ms`–`250ms`.
   - **Polite Delay (`--polite`)**: `1.5s`.
   - **Expected Batch Runtime**: **6 to 8 minutes** for 7 profiles (captures 100% of skills and 100% of experience safely).


