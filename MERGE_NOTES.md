# Merged submission candidate

Prepared from `backup-morning-work` at `e56fd22093d6c0f6d24d3736406033b8ffc886b6`. The `main` head (`61e98d269c95286f2f2c930200bb0595d2b4d8c6`) is an ancestor of that backup branch, so the backup branch already contains the main-branch work. This candidate therefore keeps the backup implementation and applies only verified fixes; it does not replace backup modules wholesale with older main versions.

## Retained backup work

- Expanded Playwright scroll and “see more” handling.
- Typed profile fields for certifications, projects, and languages.
- Detail-page parsers and deep-detail fetching/selection logic.
- Multi-role experience parsing and the live HTML regression fixtures.

## Changes applied

1. Replaced BeautifulSoup `Tag` hashing and structural equality checks with object-identity checks in `parser.py`. BeautifulSoup hashes/equality serialize DOM subtrees; using them in ancestor filtering caused severe performance regression on large profile pages. The new checks preserve DOM-ancestor semantics and avoid serializing the subtree for membership tests.
2. Added exact-record deduplication for certifications. It removes only records identical across all parsed fields and preserves original order. This eliminates the duplicate Shruti certification in the supplied fixture without collapsing credentials that differ in dates, IDs, URLs, or raw text.
3. Added a regression test for duplicate certification cards.
4. Updated current README and handover test counts. Historical progress notes were left unchanged.

## Verification performed

- Test suite: **94 passed, 1 skipped**. The skipped test requires a saved authenticated browser session (`.auth/state.json`); no live LinkedIn session was available in this test environment.
- `compileall` completed successfully. The CLI `--help` command completed successfully.
- Offline full-pipeline dry run: **8/8 profiles processed**, 8 JSON files written, 24 fetch attempts, about 1.54 seconds. Generated JSONs passed `ProfileDocument` validation.
- Output totals from the supplied fixtures: 22 experience entries, 13 education entries, 31 skills, 7 unique certifications, 2 projects, and 2 languages.
- Parser regression comparison: before certification deduplication, the optimized parser's output was identical to the unmodified backup parser for all eight profile fixtures and the supplied Rohit skills-details fixture. The only intentional data change is removal of the exact duplicate certification record.
- Parsing time across the eight profile fixtures fell from about **32.85 seconds** on the unmodified backup parser to about **1.02 seconds** after the identity-comparison fixes. The older `main` parser took about 0.57 seconds, but it did not extract the additional roles/fields delivered by the backup parser.

## Limits of this verification

Only one deep-detail HTML fixture (`rohit_skills_details.html`) was supplied. Other missing detail-page fixtures were treated as `NOT_FOUND`; no skills or other details were invented. This run does not establish live LinkedIn behaviour, authentication success, or whether every real profile will return all detail pages.

## Submission handling

Use this tree as-is. Do not perform another automatic merge or broad refactor. Preserve `.git` from the working repository when copying these files over it; this ZIP itself does not contain Git history. Do not commit `.auth/`, `input/`, `output/`, `logs/`, `.run_state.json`, or other session/output data.


## Follow-up: recording-driven capture hardening

This working tree adds incremental profile-card snapshots in
`src/profile_extractor/fetcher/playwright_fetcher.py`, preserving both SDUI and
legacy LinkedIn card-wrapper formats. It composes the richest observed version
of each card into the parser input and falls back to final page HTML when no
recognized cards are captured. The parser continues to leave unclassified
sections in `other_sections` / `raw_text`. A narrow UI-badge rule prevents
“helped me get this job” from being misattributed as experience description.

Latest verification: 98 passed, 1 skipped; compileall and capture-script Node
syntax check passed. The eight-fixture dry run preserved exact `profile` and
`other_sections` output from the original fixture HTML. Live authenticated
LinkedIn execution was not available, so this must not be represented as live
verified. See `RECORDING_REVIEW.md`.
