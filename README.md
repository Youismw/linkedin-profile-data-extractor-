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
- **Polite Execution**: Configurable random delays between requests and automatic cessation (`stop_on_block = true`) upon encountering challenges or warnings to protect accounts.
- **Data Privacy by Design**: Personal profile data is strictly confined to the output directory—it is never printed to logs or committed to version control.

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

### 1. One-Time Manual Login (Temporary PoC Setup)
To authenticate the temporary Chromium session for the demo:
```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe -m profile_extractor login
```
1. A visible Chromium window will open at `https://www.linkedin.com/login`.
2. The consenting employee signs in manually (including completing 2FA).
3. Once the LinkedIn feed loads, return to the terminal and press **[ENTER]**.
4. The authenticated session is saved to `.auth/state.json` (gitignored).

### 2. Running Extraction
```powershell
$env:PYTHONPATH="src"
# Standard execution (reads input_file from config.toml or default input/profiles.xlsx)
.\.venv\Scripts\python.exe -m profile_extractor run --input input/profiles.xlsx --output output

# Testing / Demo fallback without live browser (uses FakeFetcher)
.\.venv\Scripts\python.exe -m profile_extractor run --input input/profiles.xlsx --output output --fake
```

### CLI Arguments
- `--input`, `-i`: Path to the input Excel spreadsheet (`.xlsx`).
- `--output`, `-o`: Output folder for profile JSON files, reports, and summary.
- `--config`, `-c`: Path to custom `config.toml`.
- `--max-profiles`: Limit number of valid profiles processed in this run.
- `--fake`: Run using `FakeFetcher` (useful for offline demonstration and CI testing).

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

delay_seconds_min = 5.0
delay_seconds_max = 12.0
page_timeout_seconds = 30.0
max_profiles = 0          # 0 or null for unlimited
headless = true
stop_on_block = true      # Halts immediately upon CAPTCHA/challenge
log_level = "INFO"
```

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

### Run Summary (`<output_dir>/run_summary.json`)
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
