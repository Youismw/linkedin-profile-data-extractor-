# Latest update: bounded additive detail-page discovery

## What changed

The previous conservative capture hardening remains intact. The only additional
production change is in `src/profile_extractor/orchestrator.py`.

After the original profile parse and the existing six known deep-detail attempts,
the orchestrator discovers direct same-profile LinkedIn detail/activity links from
the original profile HTML and fetches eligible pages in a second pass.

- Accepted routes: `/in/<same-slug>/details/<section>/` and
  `/in/<same-slug>/recent-activity/{all,comments}/`.
- Supplemental fetch cap: 12 pages per profile.
- No recursive link-following from supplemental pages.
- Existing legacy-known pages already attempted are skipped by the new pass.
- Existing profile data is restored additively after the legacy path.
- Known section parsers merge records using conservative section-specific keys.
- Unknown section pages are retained in `other_sections` and `raw_text`.
- Raw supplemental text includes a heading and source URL marker.

## Duplicate controls

- Canonical URLs are deduplicated after dropping tracking parameters.
- Different meaningful query parameters are retained (for example, separate
  Interests tabs).
- Structured records are matched using section-specific identifiers and
  compatible metadata rather than company or school alone.
- Skills are merged by normalized, case-insensitive name.
- Unknown sections merge by normalized heading and append only unseen text lines.
- Supplemental raw text is marked by canonical source URL to avoid appending the
  same subpage twice.

## Verification

- `python -m pytest -o addopts='' -q`: **113 passed, 1 skipped**.
- `python -m compileall -q src tests`: passed.
- The single skipped test requires an authenticated Playwright browser session.
- Mocked integration tests verify discovery, same-profile restrictions, request
  caps, additive raw/Other sections, and that legacy detail pages are not fetched
  a second time.
- The saved live-profile fixtures expose 2–8 eligible direct routes per profile.

## Limitations

No authenticated live LinkedIn session was available in this environment. The
supplemental pass is verified using mocked page responses and saved HTML fixtures,
but actual availability, redirect behaviour, and live LinkedIn DOM changes remain
to be validated by the user's next run. About content that has no detail link and
is absent from every captured profile-card snapshot cannot be recovered by this
link-discovery pass alone.
