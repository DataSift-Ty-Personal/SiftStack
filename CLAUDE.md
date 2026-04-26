# CLAUDE.md — SiftStack (Ohio Playbook)

Guidance for Claude Code when working with this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Scrapes distress-property notices from Ohio public portals, runs a 10-step enrichment pipeline, and hands clean records to the DataSift CRM every morning — ready for niche sequential marketing. The operator's data manager opens DataSift at 9am and finds new Ohio leads already tagged, listed, skip-traced, and phone-tier-scored.

### Scope

**Markets:** Ohio — Franklin (Columbus), Montgomery (Dayton), Greene (Xenia).
**Notice types (Phase 1 scope):** foreclosure, probate. Future phases: tax sale, tax delinquency, eviction, code violation, divorce.
**Owner:** Aaron Leddy (Wright Home Offer). Lives and operates in Ohio.

### Lifecycle covered

1. **Data Acquisition** — Per-county portal scrapers (6 sources total; see OH Scraper Registry below)
2. **Enrichment Pipeline** — 10+ steps: Smarty address standardization, Zillow property data, obituary/heir research, Ancestry SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis** — Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing)
4. **Market Intelligence** — Zip-code scoring, Market Finder reports, cash-buyer list building
5. **CRM Automation** — DataSift upload, 26 TCA sequence templates, niche sequential presets, SiftMap sold tagging
6. **Lead Management** — 4 Pillars auto-qualification, pipeline reporting, deep prospecting (4-level framework)
7. **Operations** — Acquisition playbook generator, Slack/Discord notifications, Google Drive upload, Apify Actor deployment

### Current status

- **Phase 1** ✅ Purged TN-specific pipeline (commit `df5b0df`) — 6 files deleted, 15+ cleaned
- **Phase 2** ✅ OH scraper foundation (commit `4a2f05c`) — `src/scrapers/` package + base class
- **Phase 3** ✅ Montgomery probate scraper (commit `c761786`) — 49 real records in 9 min
- **Phase 4** 🚧 Remaining 5 scrapers (parallel build in progress)
- **Phase 5** ✅ This OH playbook
- **Phase 6** ⏳ Wire `main.py daily` to registry + Apify cron + 8:30am Slack delivery
- **Phase 7** ⏳ End-to-end test (scrape → enrich → DataSift → Slack)

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Test individual scrapers (Phase 3-4)
PYTHONPATH=src python -m scrapers.oh_montgomery_probate --days 7
PYTHONPATH=src python -m scrapers.oh_montgomery_foreclosure --days 7
PYTHONPATH=src python -m scrapers.oh_greene_probate --days 30
PYTHONPATH=src python -m scrapers.oh_greene_foreclosure --days 30
PYTHONPATH=src python -m scrapers.oh_franklin_probate --days 7
PYTHONPATH=src python -m scrapers.oh_franklin_foreclosure --days 30

# Daily run (Phase 6+ — wires to SCRAPER_SOURCES registry)
python src/main.py daily                                        # all OH sources
python src/main.py daily --counties Franklin                    # only Franklin
python src/main.py daily --types probate                        # only probate
python src/main.py daily --upload-datasift --notify-slack       # full white-glove flow

# DataSift preset/sequence management (state-agnostic)
python src/main.py manage-presets --discover                    # list all presets + sequences
python src/main.py manage-presets --add-sold-exclusion          # add Sold exclusion
python src/main.py manage-presets --create-sold-sequence        # create Sold cleanup sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12 --counties Franklin,Montgomery,Greene

# Deal analysis (state-agnostic)
python src/main.py comp --address "123 Main St, Columbus, OH 43201"
python src/main.py rehab --address "123 Main St" --tier 2 --region columbus
python src/main.py analyze-deal --address "123 Main St" --purchase-price 150000

# Market intelligence (state-agnostic)
python src/main.py market-analysis --counties Franklin,Montgomery,Greene
python src/main.py buyer-prospect --counties Franklin
python src/main.py deep-prospect --csv-path output/records.csv --depth 3

# Acquisition playbook (state-agnostic)
python src/main.py playbook --blueprint wholesale --market columbus --team-size 1

# Dormant paths (kept for future use, no active maintenance)
python src/main.py pdf-import --pdf-path ./tax_sale.pdf --pdf-county Franklin
python src/main.py photo-import --folder ./photos --photo-county Franklin --photo-type probate
python src/main.py dropbox-watch
```

All source files live in `src/`. Run from project root with `PYTHONPATH=src python ...` or use `python src/main.py`.

## Architecture

### Data flow

```
  Per-county scrapers   ──→  NoticeData   ──→  Enrichment Pipeline  ──→  DataSift CSV  ──→  Upload
  (src/scrapers/*.py)        (models.py)       (enrichment_pipeline.py)   (datasift_formatter.py)   (datasift_uploader.py)
                                                                                                           │
                                                                                                           ↓
                                                                                                   Slack/Discord summary
                                                                                                   Google Drive backup
```

Each scraper module exposes a `Scraper` class subclassing `NoticeScraper` (`src/scrapers/base.py`) with one async method: `scrape(since_date) -> list[NoticeData]`. Main dispatches to the registry in `config.SCRAPER_SOURCES`.

### Core modules (state-agnostic)

- `src/models.py` — `NoticeData` dataclass (the universal record shape) — 100+ fields covering raw scrape + every enrichment step
- `src/enrichment_pipeline.py` — canonical 10-step enrichment orchestration
- `src/datasift_formatter.py` — `NoticeData` → 41-column DataSift CSV
- `src/datasift_uploader.py` — Playwright DataSift UI automation (login + upload + enrich + skip trace + presets + SiftMap)
- `src/address_standardizer.py` — Smarty USPS + ZIP+4 + geocode + vacancy
- `src/property_enricher.py` — Zillow (OpenWebNinja) — Zestimate, MLS, equity, beds/baths
- `src/obituary_enricher.py` — Serper + Firecrawl + LLM for DOD, heirs, DM ranking
- `src/entity_researcher.py` — LLC/Corp/Trust research via SOS search
- `src/tracerfy_skip_tracer.py` — batch skip trace ($0.02/record)
- `src/phone_validator.py` — Trestle 5-tier phone scoring
- `src/ancestry_enricher.py` — SSDI + obituary collection
- `src/slack_notifier.py` — Slack/Discord run summaries
- `src/drive_uploader.py` — Google Drive CSV backup
- `src/llm_client.py` / `src/llm_parser.py` — Claude Haiku (default) / Ollama / OpenRouter backends
- `src/deep_prospector.py` / `src/case_summary.py` / `src/report_generator.py` — 4-level research + PDF reports
- `src/comp_analyzer.py` / `src/rehab_estimator.py` / `src/deal_analyzer.py` — Two-Bucket ARV, 4-tier rehab, MAO/ROI
- `src/market_analyzer.py` — Zip-code scoring (6 weighted factors)
- `src/buyer_prospector.py` — Cash buyer identification
- `src/lead_manager.py` / `src/niche_sequential.py` / `src/sequence_templates.py` — CRM orchestration
- `src/playbook_generator.py` — SOP/script/checklist generator
- `src/extract_market_finder.py` — DataSift Market Finder extractor

### Dormant modules (kept for future, not actively used)

- `src/photo_importer.py`, `src/image_utils.py`, `src/pdf_importer.py` — courthouse photo + PDF OCR pipelines (built for TN terminals, retained in case needed)
- `src/dropbox_watcher.py` — Dropbox auto-polling for courthouse photos

## OH Scraper Registry

All active sources live in `config.SCRAPER_SOURCES` (a list of `ScraperSource(county, notice_type, module_path, ...)`). Main iterates this list, loads each module via `scrapers.base.load_scraper()`, calls `scrape()`, collects `NoticeData`.

| County | Notice Type | Source | Tech | Account? |
|---|---|---|---|---|
| Montgomery | probate | [go.mcohio.org](https://go.mcohio.org/applications/probate/prodcfm/casesearchall.cfm) | ColdFusion | No |
| Montgomery | foreclosure | [go.mcohio.org](https://go.mcohio.org/applications/sheriffauction/sflistauction.cfm) | ColdFusion | No |
| Greene | probate | [courts.greenecountyohio.gov/probatejw](https://courts.greenecountyohio.gov/probatejw) | JWorks | No |
| Greene | foreclosure | [apps.greenecountyohio.gov/sheriff/sheriffsales.aspx](https://apps.greenecountyohio.gov/sheriff/sheriffsales.aspx) | ASP.NET ViewState | No |
| Franklin | probate | [probate.franklincountyohio.gov](https://probate.franklincountyohio.gov/record-search/general-case-index) | Custom .NET | No |
| Franklin | foreclosure | [franklin.sheriffsaleauction.ohio.gov](https://franklin.sheriffsaleauction.ohio.gov) | RealAuction | Free account (`REALAUCTION_EMAIL/PASSWORD`) |

## Hard-Won OH Lessons

### Montgomery Probate (Phase 3)

- **NO date filter exists in the form.** The Montgomery probate search has only case-number lookup and name-prefix search. There is no "filter by filing date range" — strategy: probe case numbers directly for the current year, walk backward from max until past `since_date`.
- **Name-prefix queries are silently capped at ~500 rows**, and older cases dominate. 1-letter prefix sweeps MISS recent cases. Don't use name sweeps for recent-window scraping.
- **Detail URLs use obfuscated TOKENs** (hex-encoded session-scoped ids) — can't construct from case number alone. Must POST search, parse list, follow link.
- **Case Status date ≠ filing date.** For closed/amended cases, the status line shows the last-status-change date. Fall back to the docket page's earliest entry for the true filing date.
- **Case Types:** Type `1` = FULL ADMIN;PROBATE WILL, Type `6` = RELEASE OF ADMIN;PROBATE WILL. Keep ALL estate types — every estate filing has a fiduciary worth contacting.
- **Fiduciary addresses** are free-text — handle unit tokens (#N / SUITE N / STE N / APT N / UNIT N) and missing-ZIP cases in the parser.
- **Throughput:** 2-3s rate limit × ~75 calls = ~9 min for 7-day backfill. For historical runs (`--days 365`), add resume state.

### Pattern for OH scrapers (generalized from Phase 3)

1. **Check for a date-range filter first** — if present (e.g., Montgomery foreclosure has SaleDate / Thru Sale), use it.
2. **If no date filter** — probe by case number if the portal exposes one (year-based, sequential).
3. **Always use sessions** (`requests.Session()`) to hold cookies across paginated requests.
4. **Always rate-limit** 2-3 seconds between HTTP calls. ColdFusion + ASP.NET portals throttle aggressively.
5. **Set realistic User-Agent** — not Python-requests default. Some gov portals block obvious bots.
6. **Handle cp1252/windows-1252 encoding** — ColdFusion often serves in these, not UTF-8.
7. **Status ≠ filing date** — always verify which date field represents actual filing vs status change.
8. **Obfuscated detail URLs** — many OH portals use session-scoped hex tokens. Always follow links from list pages; don't try to construct URLs.

## Key OH Domain Rules

### Foreclosure (ORC §2329.26)

- Ohio is a **judicial foreclosure state** — sheriff runs the sale.
- Statutory requirement: sale must be advertised in a newspaper of general circulation **once a week for 3 consecutive weeks** before the sale date. This means newspaper publication is the earliest public signal, often 1-3 weeks before the RealAuction/sheriff-site listing.
- **Confirmation hearing** 7-30 days after auction; deed records afterward.
- **Lis pendens / initial complaint** filed in Court of Common Pleas is the earliest signal of all (weeks/months before sheriff sale publication) — Phase 4+ scraping may target this.
- **Online auctions** (HB 138, 2016) — Franklin, Montgomery, Greene all use RealAuction (`{county}.sheriffsaleauction.ohio.gov`). Sales typically Friday mornings.
- Target = **defendant** (homeowner being foreclosed on), not plaintiff (bank).

### Probate (ORC §2117.06)

- **6-month creditor window** from decedent's death (NOT from fiduciary appointment) — claims barred after.
- Key filings for lead-gen: Form 2.0 (Application to Probate Will), Form 4.0 (Application for Authority to Administer Estate), Form 4.3 (Fiduciary Acceptance).
- **The Fiduciary = Executor/Administrator/PR = the decision-maker we contact.** The decedent is deceased; the fiduciary inherits decision authority over the estate and the property.
- Ohio probate courts are a **separate division of Common Pleas Court**. Each county has one.
- **Property addresses are NOT in Franklin/Montgomery/Greene probate records online.** The decedent's property needs a separate lookup (County Auditor) — handled in enrichment pipeline Step 4.

### Owner Name Conventions

- **Probate `owner_name`** = the Personal Representative / Executor / Administrator (NOT the deceased). This is our contact. `decedent_name` holds the deceased's name separately.
- **Foreclosure `owner_name`** = the defendant / homeowner being foreclosed on (NOT the plaintiff bank).

### Dedup + Rate Limiting

- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page (see `config.REQUEST_DELAY_MIN/MAX`, `MAX_RETRIES`).

## Enrichment Pipeline (10 steps)

Every scraped `NoticeData` record flows through the same pipeline (`src/enrichment_pipeline.py`). Each step skippable via `PipelineOptions` flags.

1. **Deduplicate** by address (keep most recent)
2. **Vacant Land Filter** — remove parcels with no house number
3. **Entity Filter** — flag LLC/Corp owners, research the person behind them (see `entity_researcher.py`)
4. **Probate Property Lookup** — County Auditor parcel search for decedent (Phase 4+ for OH; uses placeholder skip today)
5. **Tax Delinquency** — parcel lookup (Phase 4+ via OH Auditors; skipped today)
6. **Address Standardization** — Smarty USPS validation, ZIP+4, geocoding, vacancy detection
7. **Commercial Filter** — Smarty RDI check; remove commercial unless `--include-commercial`
8. **Zillow Enrichment** — OpenWebNinja API; Zestimate, MLS status, equity, beds/baths/sqft/year built
9. **Obituary Search** — deceased owner detection, heir identification, DM ranking, DOD sanity check (`MAX_DOD_GAP_YEARS = 3`)
10. **Data Validation** — catch garbage OCR/parse errors, verify required fields, compute `mailable` flag

### DOD Sanity Check

Rejects obituary matches where DOD is > 3 years before the notice filing date. Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name). Applied to both full-page and snippet matches.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames.

**Sift upload CSV columns:** date_added, address, city, state, zip, owner_name, notice_type, county, source_url + enrichment columns (see DataSift section).

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where enriched records land for niche sequential marketing. There is **no REST API** — upload is via Playwright browser automation.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### Key Files

- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (41 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (41 columns)

- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (niche sequential), Notes (contextual per notice type)
- **Built-in fields (13):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Delinquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (15):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL

### Niche Sequential Marketing

DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status. A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag.

- **"Courthouse Data" tag:** Every record gets this — signals first-to-market county data (prioritized in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name (`foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Violation", `divorce` → "Divorce"). DataSift auto-creates lists from CSV.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence, has_auction, tax_delinquent

### Upload Wizard (5 Steps)

1. **Setup:** Click "Upload File" sidebar → "Add Data" → "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, enrichment columns often need manual drag-and-drop
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Contact Logic

- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phones (up to 5/owner) + emails via unlimited plan. Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both ON by default when `--upload-datasift` is set.

### Login Selectors (SPA quirks)

- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (NOT `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won. Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value; `[class*="SelectOptionContainer"]` = options
- Multiple Select dropdowns per panel — target the **LAST visible one**
- Use `x > 450` bounds check to avoid sidebar elements (sidebar is 0-400px)
- React state requires native setter + event dispatch:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})`
- Filter Presets section is at the BOTTOM — must scroll container to reveal

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up
- "Add new Action +" button required for 2nd+ actions

**Pointer Interception (common blockers)**
- Beamer NPS iframe (`#npsIframeContainer`) blocks ALL pointer events — remove from DOM
- Beamer push modal (`#beamerPushModal`) — same issue, different element
- `RecordsFiltersstyles__RecordsFiltersSection` intercepts clicks — use `page.evaluate()` JS click or `force=True`
- SiftMap PropertyDetails panel blocks sidebar — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name: detect error toast "different sequence title", retry with " V2" suffix
- Autocomplete: after each selection, `fill("")` + Escape to dismiss dropdown before next entry

**SiftMap Automation**
- Search by city (NOT county): Franklin → "Columbus, OH"; Montgomery → "Dayton, OH"; Greene → "Xenia, OH"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters set values visually but don't trigger React re-query. Only ~3-5 sidebar-visible properties get added per run

### Market Finder Extraction Patterns

- **NO HTML `<table>`** — table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell`
- **PAGINATION, not infinite scroll** — 20 rows/page. Must click through ALL pages
- **State/County selection uses `InputMultiSearch`** — placeholders: "Select States", "Select Counties", "Select ZIP Codes"
- **ZIP/Neighborhood toggle** — styled Select at top bar. Check displayed text BEFORE clicking (clicking when already on target view toggles AWAY)
- **Beamer push modal** (`#beamerPushModal`) — blocks ALL pointer events. Different from NPS iframe. Remove from DOM
- **Page body scrolling required** — pagination controls below viewport. Scroll `AdminPage__AdminPageBody` container down before pagination

```bash
# Extract all Market Finder data for OH counties
python src/extract_market_finder.py --state "Ohio" --county "Franklin" -v
python src/extract_market_finder.py --state "Ohio" --county "Franklin,Montgomery,Greene" --headless
# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

### Statewide County Ranking (niche-sequential expansion)

For deciding which OH counties to expand scrapers into beyond Franklin/Montgomery/Greene, two scripts compose the workflow:

```bash
# 1. Resilient batch extraction — top 20 OH counties by population, skips fresh extracts (<7d old)
PYTHONPATH=src python -m run_oh_extraction --headless
# Logs to output/oh_extraction_run_<ts>.log; one bad county doesn't kill the run.

# 2. Apply 3-axis scoring rubric, output ranked CSV + console table
PYTHONPATH=src python -m score_oh_counties
# Tunables: --inv-threshold 5  --ab-min 150000  --ab-max 600000
```

**Rubric:** `score = inv_density × ab_class_pct × portal_reuse`
- `inv_density` — sum of `total_inv_trans_6mo` across zips clearing per-zip threshold (default ≥5)
- `ab_class_pct` — share of qualifying zips with `median_home_value` in A/B band (default $150k–$600k)
- `portal_reuse` — split foreclosure vs probate; 1.0 = template exists (RealAuction), 0.7 = same platform family, 0.3 = unknown/custom. Edit `PORTAL_REUSE` dict in `score_oh_counties.py` as recon completes for each new county.

Output: `output/oh_county_scores_<ts>.csv` ranked by `combined_score`. Use `foreclosure_score` to pick RealAuction expansion targets (cheap to scale via subdomain template); use `probate_score` to pick which probate portals justify a custom build.

## Apify Deployment & Daily Cadence

SiftStack runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: configure daily schedule + secrets in Actor input
```

### Actor Input (configured in Apify Console)

- `mode`: "daily" or "historical"
- `counties` / `types`: arrays to filter OH sources (empty = all)
- `realauction_email`, `realauction_password`: optional (only for Franklin foreclosure)
- `datasift_email`, `datasift_password`: required for CRM upload
- `anthropic_api_key`, `smarty_auth_id/token`, `openwebninja_api_key`, `serper_api_key`, `firecrawl_api_key`, `tracerfy_api_key`, `trestle_api_key`: enrichment
- `slack_webhook_url`: daily summary + error alerts
- `google_drive_folder_id`, `google_service_account_key`: optional backup

### Actor Output

- **Dataset**: structured records via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file

### White-Glove Daily Flow (8:30am delivery)

Target: Mike (data manager) starts at 9am and finds new OH records already in DataSift, tagged, listed, skip-traced, phone-scored.

```
06:00 AM ET  Apify Actor wakes (cloud — your laptop can be off)
06:00-06:30  Scrape 6 OH portals since yesterday
06:30-07:00  Dedup + enrichment pipeline (Smarty → Zillow → obituary → Tracerfy → Trestle)
07:00-07:30  Format + upload CSV to DataSift
07:30-08:15  DataSift runs Enrich Property Info + Skip Trace in background
08:15-08:30  Final phone-tier scoring + list assignment
08:30 AM     Slack: "Ohio daily — X new records (Y probate, Z foreclosure). Top 5 by phone score: ..."
09:00 AM     Mike opens DataSift → records in Probate/Foreclosure lists, phone tiers scored
```

### Key Files

- `.actor/actor.json` — Actor manifest
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — `apify/actor-python-playwright:3.12` base
- `input.json` — Local test input (gitignored)

## Environment Variables

All configured in `.env` (gitignored) or Apify Console secrets:

### Credentials
- `REALAUCTION_EMAIL` / `REALAUCTION_PASSWORD` — Franklin foreclosure (free account at franklin.sheriffsaleauction.ohio.gov)
- `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` — REISift/DataSift login

### Enrichment APIs
- `SMARTY_AUTH_ID` / `SMARTY_AUTH_TOKEN` — address standardization ($)
- `OPENWEBNINJA_API_KEY` — Zillow data ($)
- `ANTHROPIC_API_KEY` — Claude Haiku for LLM parsing
- `SERPER_API_KEY` — Google Search for DM lookup
- `FIRECRAWL_API_KEY` — JS-rendered page scraping
- `TRACERFY_API_KEY` — batch skip trace ($0.02/record)
- `TRESTLE_API_KEY` — phone validation ($0.015/phone)

### Optional
- `ANCESTRY_EMAIL` / `ANCESTRY_PASSWORD` — SSDI + obituary collection ($29/mo)
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook (push notifications to phone when app installed)
- `DROPBOX_APP_KEY` / `DROPBOX_APP_SECRET` / `DROPBOX_REFRESH_TOKEN` — dormant courthouse photo pipeline
- `GOOGLE_DRIVE_FOLDER_ID` / `GOOGLE_SERVICE_ACCOUNT_KEY` — CSV backup

### Framework
- `LLM_BACKEND` — `anthropic` (default) | `ollama` | `openrouter`
- `DROPBOX_POLL_INTERVAL` / `DROPBOX_ROOT_FOLDER` — dormant

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/`. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing
- **DOD sanity:** `MAX_DOD_GAP_YEARS = 3`
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Skills

| # | File | Division | What It Does |
|---|------|----------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | Market Finder reports, zip scoring (6 weighted factors), 7-sheet Excel |
| 2 | `first-market-county-data.skill` | Market Intel | County clerk data extraction for all 7 notice types, FOIA templates |
| 3 | `buyer-prospector.skill` | Market Intel | Cash buyer list, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | Two-Bucket ARV, disclosure/non-disclosure routing (12 states) |
| 5 | `rehab-estimator.skill` | Deal Analysis | 912-line skill, Repair Cheat Sheet, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | Combined comp+rehab, MAO (75%/70%), multi-loan financing |
| 7 | `deep-prospecting.skill` | Deal Analysis | 4-level research (L1-L4), heir verification, DOD sanity (3yr) |
| 8 | `probate-property-finder.skill` | Deal Analysis | Property lookup for probate decedents, 3-tier search |
| 9 | `phone-validator.skill` | Operations | Trestle API scoring, 5-tier dial priority, 4.75× connect rate |
| 10 | `sequential-presets.skill` | Operations | 12 niche + 9 bulk filter presets, Pendulum Theory |
| 11 | `sift-sequences.skill` | CRM | 26 TCA sequence templates, UI walkthrough |
| 12 | `sift-operations.plugin` | CRM | CRM operations encyclopedia, STABM routine, pipeline (9 statuses) |
| 13 | `playbook-creator.skill` | Operations | Playbook/SOP generator from transcripts, Word doc output |
