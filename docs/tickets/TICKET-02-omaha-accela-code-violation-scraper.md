# TICKET-02 — Omaha Accela code-violation scraper

**Priority:** High — introduces a brand-new, high-value notice type (`code_violation`) with a
real queryable online source. This is the ONLY genuine searchable code-enforcement DB in either county.

## Goal
Ingest City of Omaha (Douglas County) **code violations / enforcement cases** from the Accela
Citizen Access portal as `list[NoticeData]` with `notice_type="code_violation"`, feeding the
existing enrichment + DataSift pipeline.

## Source facts (from research)
- **Jurisdiction:** City of Omaha Planning Dept — Permits & Inspections / Housing Enforcement.
  (Omaha is ~all of Douglas County's developed area; unincorporated Douglas is negligible and
  records-request-only — out of scope.)
- **Primary URL (KEY):** Accela Citizen Access — Enforcement module:
  `https://aca-prod.accela.com/OMAHA/Cap/CapHome.aspx?module=Enforcement&TabName=Home`
- **Scrapability: YES.** Searchable **without login, no CAPTCHA**. Search by record number
  (format `CASE-15-00420`), address components, parcel, and **date range**. Results are paginated
  ASP.NET tables. Record types exposed: **Abandoned & Vacant Property Registration, Citation
  Record, Housing Inspection Case, Property Owner Registration**, plus a linked "properties with
  demolition orders" report. This is an ASP.NET/JS app → drive with **Playwright** (same pattern
  as `scraper.py`) or Firecrawl. No 2Captcha needed.
- **Bulk fallback:** DOGIS Open Data Hub dataset "Omaha Planning Building Code Violations"
  (`data-dogis.opendata.arcgis.com`, item `30cac7cf734341a9ac71bef6d25a1d55`) — CSV/GeoJSON +
  ArcGIS REST. ⚠️ **May be stale** (a snapshot suggested last update ~Jan 2023) and the raw REST
  `Planning/` folder was not anonymously enumerable (500s) — use the Hub download endpoint, not raw
  REST, and **verify freshness**. Treat Accela as the authoritative live source; DOGIS as an
  optional bulk backfill only if current.

## Integration points (existing code)
- **Output type:** `NoticeData` (`src/notice_parser.py:29`). Populate `address, city="Omaha",
  state="NE", zip, owner_name` (owner of record / Property Owner Registration), `notice_type=
  "code_violation", county="Douglas", parcel_id, date_added` (case open date), `source_url`
  (Accela record URL). Consider stashing violation type + compliance deadline in `raw_text` or a
  `notes`-equivalent — CLAUDE.md's photo pipeline already defines `code_violation` semantics
  (owner of record, violation type, compliance deadline).
- **Contact logic:** code_violation → owner of record is the target contact (living-owner path in
  `datasift_formatter.py`); no PR/decedent handling.
- **Photo pipeline parity:** `code_violation` already exists as a photo-import notice type. This
  ticket adds the *online* acquisition path for the same type — reuse the downstream mapping in
  `datasift_formatter.py` (Lists column → "Code Violation") and niche_sequential.
- **State:** persist a cursor by max case date / seen case numbers via `save_state`/`load_state`
  + `accela_state.json`; daily incremental by date-range search (yesterday → today).

## New files / changes
- `src/accela_scraper.py` — new. `fetch_violations(since_date, record_types=[...]) -> list[NoticeData]`.
  Drive the Enforcement search by date range, paginate result tables, open each record for
  owner/address/parcel, map to `NoticeData`.
- `src/config.py` — add `ACCELA_OMAHA_URL`, record-type list, `accela_state.json` path; add
  `"code_violation"` to `NOTICE_TYPES` (currently `["foreclosure","probate"]`).
- `src/main.py` — add mode `code-violations` (or `accela-scrape`), add to `enrichment_modes`
  (main.py:63) so it flows through enrichment → dedup → export/upload.
- `src/datasift_formatter.py` — confirm `code_violation` → "Code Violation" list mapping + tags
  exist (per CLAUDE.md they should); add if missing.

## CLI (proposed)
```bash
python src/main.py code-violations --since 2026-06-01            # date-range pull
python src/main.py code-violations                               # daily incremental (since last run)
python src/main.py code-violations --record-types citation,housing_inspection
```

## Acceptance criteria
- Date-range search against the live Accela Enforcement portal returns paginated records with no
  login/CAPTCHA.
- Each record maps to a valid `NoticeData` (`notice_type="code_violation", county="Douglas",
  city="Omaha", state="NE"`, owner + address + parcel populated, Accela URL in `source_url`).
- Incremental daily run only emits new/updated cases since the last cursor.
- Records enrich, dedup, and upload through the unchanged downstream pipeline; DataSift shows them
  on the "Code Violation" list with correct tags.

## Open questions / risks
- Accela anti-automation behavior **at scale** (rate limits, session expiry) — unverified; add
  rate limiting like `config.REQUEST_DELAY_MIN/MAX` and retries.
- Whether the Accela result/detail view reliably exposes **owner mailing address** vs. only
  property address — verify; may need county assessor (Beacon) join for mailing address.
- DOGIS dataset currency — do not rely on it until freshness confirmed.
- Which record types count as actionable "distress" (Citation + Housing Inspection + Vacant
  Property Registration are the strong signals; Property Owner Registration alone may be noise).

## Effort estimate
Medium (2–3 days). Accela ACA is a well-known, stable ASP.NET target; main work is field mapping
and picking the right record types.
