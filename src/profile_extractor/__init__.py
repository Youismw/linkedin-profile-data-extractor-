"""LinkedIn Profile Extractor package.

Architecture Overview:
======================
A modular, resilient, and privacy-focused pipeline for extracting structured
profile data from LinkedIn profile URLs listed in Excel spreadsheets.

Key Subpackages & Modules:
- `cli`: Command-line interface, argument routing, and Windows Explorer presentation.
- `config`: Configuration loading, schema validation, and runtime bounds enforcement.
- `fetcher`: Headless Playwright Chromium automation, lazy scrolling, and session management.
- `parser`: Semantic HTML traversal, noise stripping, and Pydantic profile construction.
- `orchestrator`: Pipeline coordinator, polite pacing, circuit breakers, and two-pass discovery.
- `reader`: Fault-tolerant spreadsheet reader (.xlsx, .xlsm, .csv) with formula resolution.
- `writer`: Atomic filesystem persistence for JSON documents, Excel audits, and run summaries.
- `models`: Strongly-typed data schemas and binary outcome classifications.
- `urls`: Canonical URL validation, slug extraction, and filename sanitization.
- `logging_setup`: Privacy-safe logging with sensitive candidate data redaction.
"""

__version__ = "0.1.0"
