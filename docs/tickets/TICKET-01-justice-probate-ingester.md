# TICKET-01 — Nebraska JUSTICE probate ingester (Douglas + Sarpy)

> **Supersedes** the original "Sarpy County Times (Column) scraper" ticket. Per the user, probate
> is sourced from **Nebraska JUSTICE** (the statewide court case system), not newspaper legal
> notices. Sarpy foreclosure moved to the emailed-list importer (TICKET-03), so the Column scraper
> is no longer needed.

**Priority:** High — probate is a core notice type and JUSTICE is the user's chosen source.
**⚠️ BLOCKED on access tier — see Open Questions.** Confirm before building.

## Goal
Ingest **probate** cases for Douglas County (and Sarpy — JUSTICE is one system covering all 93
county courts) from Nebraska JUSTICE into `list[NoticeData]`, feeding the existing enrichment
pipeline (including the probate property-address lookup that fills the missing address).

## Source facts (from research)
- **Source:** Nebraska **JUSTICE** statewide trial-court case management (Tyler Technologies,
  operated via Nebraska.gov). One system for all counties — filter by county; probate is handled
  by the **County Court**. Douglas and Sarpy both covered.
- **URLs:** name search `https://www.nebraska.gov/justicecc/ccname.cgi`; subscriber portal
  `https://www.nebraska.gov/subscriber/`; case-number search `https://www.nebraska.gov/justice/case.cgi`.
- **Access / cost:** paywalled two ways — one-time search (~$15–17, party-name based, up to 30
  records) OR subscriber account ($100/yr, name searches free, **$2 per case-detail view**). A paid
  **bulk Trial Court Record Index feed** ($1,200–2,000/mo) also exists.
- **⚠️ Core obstacle — search is PARTY-NAME-CENTRIC.** Documented entry points are Name, Case
  Number, and Judgment Date; county / case-type / year act as *filters layered on a name search*.
  Sources conflict on whether a free "all probate cases filed in county X in date range Y, no party
  name" list query exists. **This is the make-or-break unknown** — without a no-name date-range
  case-type query, you cannot enumerate new probate filings systematically from the UI, and the
  bulk feed becomes the only real programmatic route.
- **Tech:** CGI/`.cgi` forms behind a Tyler/NIC subscriber login (server-rendered, not a modern
  SPA). **No CAPTCHA** observed (unlike the legacy TN source's per-notice reCAPTCHA).
- **Cadence:** filings continuous (daily); JUSTICE index updates nightly, ~24h lag.
- **Address gap:** court probate records give **decedent + PR/executor but NO property address** —
  must be filled downstream (see integration).

## Integration points (existing code)
- **Output type:** `NoticeData` (`src/notice_parser.py:29`). Populate `owner_name` = **PR /
  executor / administrator** (NOT the decedent — per CLAUDE.md domain rule), `decedent_name` =
  deceased, `notice_type="probate"`, `county`, `state="NE"`, `date_added` (filing date),
  `source_url` (JUSTICE case URL). Address is filled later.
- **Property-address lookup:** the pipeline already has the 3-tier probate lookup (CLAUDE.md
  "Probate Deep Prospecting": Assessor/Beacon name search → executor family search → people
  search) in `property_lookup.py`. JUSTICE records feed directly into this — this is exactly the
  no-address probate case the tier system was built for.
- **Obituary/probate preset:** `obituary_enricher.py` triggers on PR-name + decedent-name (no
  address required) and sets DM = named PR, skipping obituary search. JUSTICE gives both names → this preset fires cleanly.
- **Config/state:** add JUSTICE URLs + credentials (`JUSTICE_SUBSCRIBER_USER/PASS` in `config.py`
  via `os.getenv`), county filter, and `justice_state.json` cursor via `save_state`/`load_state`.
- **Fallback path already exists:** `photo_importer.py` has a `probate` type — if online access is
  unworkable, the courthouse-terminal → photo → OCR route already covers this. Do not duplicate it.

## New files / changes
- `src/justice_scraper.py` — new. `fetch_probate(county, since_date) -> list[NoticeData]`.
  Approach depends on access tier (see Open Questions).
- `src/config.py` — JUSTICE URLs, subscriber creds, state path; `probate` already in `NOTICE_TYPES`.
- `src/main.py` — add mode `justice-probate`, wire into `enrichment_modes` (main.py:63).

## CLI (proposed)
```bash
python src/main.py justice-probate --counties Douglas,Sarpy --since 2026-06-01
python src/main.py justice-probate --counties Douglas          # daily incremental
```

## Acceptance criteria
- Pulls new probate cases for Douglas (and Sarpy) since the last cursor.
- Emits valid `NoticeData`: `notice_type="probate"`, PR in `owner_name`, decedent in
  `decedent_name`, `county`/`state="NE"` correct, JUSTICE URL in `source_url`.
- Records flow into the probate property-address lookup and PR-based DM preset unchanged.
- Incremental run avoids re-emitting already-seen cases.

## Open Questions (RESOLVE FIRST — ticket is blocked on these)
1. **What access tier does the user have?** (marked "not sure yet"). Determines the whole build:
   - *Subscriber account* → Playwright/requests login + query, pay $2/case-detail.
   - *Bulk feed* → parse the Trial Court Record Index feed (cleanest; no UI scraping).
   - *Kiosk only* → use the existing courthouse-photo pipeline; no new scraper.
   **Action:** confirm the user's Nebraska.gov subscriber status/plan before coding.
2. **Does a no-party-name, date-range + case-type (probate) list query exist for a subscriber?**
   If NO, UI scraping can't enumerate new filings → bulk feed required. Verify against a live
   subscriber session.
3. Whether JUSTICE case detail returns the **PR mailing address** (helps contact mapping) —
   unverified; may still need the assessor/people-search join regardless.

## Effort estimate
Unknown until access tier is confirmed. Bulk feed = Low–Med (parse a structured feed). Subscriber
UI scraping = Med–High (CGI forms + the party-name-query obstacle). Kiosk = zero new code.
