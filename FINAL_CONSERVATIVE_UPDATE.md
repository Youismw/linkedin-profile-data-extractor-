# Final conservative update

## Scope

This update changes only `src/profile_extractor/fetcher/playwright_fetcher.py` and adds regression coverage in `tests/test_live_fixtures.py`.

## Change

The previous `_compose_captured_profile_html()` discarded the complete final-page HTML and rebuilt a synthetic document from a whitelist of captured profile-card fragments. Any visible final-page content outside those selected card IDs could therefore be lost from the parser input and `raw_text`.

The function now:

1. Keeps the final page HTML as the source document.
2. Adds a captured card only when its ID is absent from the final DOM.
3. Replaces an existing card only when the captured snapshot contains more text.
4. Returns the original final HTML unchanged when no merge is needed or when composition fails.

No parser extraction rules, scroll strategy, card selectors, models, or deep-detail fetch logic were changed.

## Verification

- `python -m pytest -o addopts='' -q`: **101 passed, 1 skipped**.
- `python -m compileall -q src tests`: passed.
- Replayed the eight saved profile HTML fixtures and confirmed the structured profile, `other_sections`, and `raw_text` outputs are unchanged when the same cards already exist in the final HTML.
- Added a regression test simulating an About card present in an earlier snapshot but absent from the final DOM. The About card is restored while existing Experience, Featured, and raw text remain available.

## Limitation

No authenticated live LinkedIn browser session was available for this verification. This patch protects final-page content from being discarded and restores cards that the existing capture selector actually captured earlier. It cannot recover a card that never matches the existing selectors or never appears in any captured snapshot.
