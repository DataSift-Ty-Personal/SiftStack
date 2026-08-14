# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** Web scraping tnpublicnotice.com (foreclosures, tax sales, probates), scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, Knox County Tax API, obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

Currently focused on Knox and Blount counties, Tennessee.

8. **REI Skill Library:** 19 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months of data
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Knox          # only Knox county
python src/main.py daily --types foreclosure,probate  # only specific types
python src/main.py daily -v                       # verbose/debug logging

# NJ sheriff sales (Essex, Middlesex, Union — salesweb.civilview.com)
python src/main.py nj-sheriff                     # all 3 counties
python src/main.py nj-sheriff --counties Essex    # one county only

# NJ Lis Pendens (Essex, Middlesex, Somerset, Union — njlispendens.com, auth)
python src/main.py nj-scrape                      # all 4 counties, last 7 days
python src/main.py nj-scrape --nj-counties Essex,Union
python src/main.py nj-scrape --headed             # visible browser for debugging
python src/main.py nj-scrape --upload-datasift --notify-slack

# Middlesex surrogate probates (Bluestone portal — no auth, no captcha)
python src/main.py nj-probate                     # default 30-day lookback
python src/main.py nj-probate --days-back 60      # wider lookback
python src/main.py nj-probate --upload-datasift --notify-slack

# Comp package (boundary-filtered comps + dual-track ARV + rehab + buyers -> Excel)
python src/comp_package.py --address "158 Old State Rd" --zip 37914 \
    --beds 2 --baths 1 --sqft 1946 --year-built 1938 \
    --bbox "35.996,36.016,-83.895,-83.840" --streets "old state|nash rd|seahorn"

# Post-walkthrough package (comps + rehab matrix + walk findings + exits + dispo, Sift-linked)
python src/post_walkthrough.py --walkthrough-template     # writes walkthrough_template.json
python src/post_walkthrough.py --address "158 Old State Rd" --city Knoxville --zip 37914 \
    --bbox "35.996,36.016,-83.895,-83.840" --streets "old state|nash rd|seahorn" \
    --walkthrough walk_158.json --buyers output/buyer_sweep_37914_20260723.json \
    --outreach output/dispo_skiptrace_158.json
python src/post_walkthrough.py --address "..." --sold-json output/zillow_37914_sold.json  # free re-run

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Knox --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Architecture

**Data flows:**
- **Web scrape:** `main.py` → `scraper.py` → `captcha_solver.py` → `notice_parser.py` + `foreclosure_filter.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON → `generate_knox_report.py` → 7-sheet Excel

- **main.py** — CLI entry point. Parses args (`daily`/`historical`, `--split`, `--counties`, `--types`, `-v`). Filters saved searches by county/type, orchestrates scrape → dedup → export, logs run summary stats.
- **scraper.py** — Playwright browser automation. Reuses saved session cookies when possible, falls back to fresh login. Selects each saved search from the Smart Search dropdown (triggers ASP.NET postback), paginates results (50/page max), clicks each View button to open notice detail pages. Uses `last_run.json` for daily mode state, `cookies.json` for session persistence.
- **captcha_solver.py** — Solves reCAPTCHA v2 via **2Captcha API** on every notice detail page. Sends websiteURL + sitekey, gets back a `g-recaptcha-response` token, injects it, clicks "View Notice". Retries up to 3 times. This is the primary bottleneck (~10-30s per notice).
- **notice_parser.py** — Extracts structured fields from raw notice text using regex. There are NO structured HTML fields on the site — address, owner, dates are all embedded in free-text notice bodies. Defines the `NoticeData` dataclass used throughout.
- **foreclosure_filter.py** — Filters foreclosure search results to only keep real first-to-market trustee sales. Matches against observed title variations (substitute/successor trustee sales). Non-foreclosure notice types pass through unfiltered.
- **data_formatter.py** — Deduplicates by address (keeps most recent), then converts `NoticeData` list to Sift upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **config.py** — Credentials (from `.env`), ASP.NET element selectors, saved search definitions, rate limiting constants, paths, image processing thresholds.
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode. Handles Tesseract binary detection.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Knox/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **nj_sheriff_sales.py** — Plain-HTTP scraper for salesweb.civilview.com (Essex countyId=2, Middlesex=73, Union=15). One-page HTML table per county, 6 or 7 cells (Middlesex has extra Status column — parse cells right-to-left for structure independence). Address parser splits at last street-suffix token, peels UNIT/APT/# descriptors back into street. No Playwright, no auth, no pagination. Stale-auction filter drops any sale date older than `SHERIFF_SALE_MAX_AGE_DAYS` (env, default 90); count surfaces in Slack as `N stale auctions filtered`. When called via `scrape_civilview_notices(enrich_details=True)` it follows up with `nj_sheriff_detail.enrich_sheriff_records()` for per-record SaleDetails enrichment. Somerset uses a different site — see `nj_somerset_sheriff.py`.
- **nj_somerset_sheriff.py** — Somerset County sheriff sales (different host than CivilView). Imports + reuses `SHERIFF_SALE_MAX_AGE_DAYS` from `nj_sheriff_sales`; exports its own `LAST_STALE_DROPPED` counter. `filter_active_sales` uses the shared cutoff and reports drops the same way.
- **nj_sheriff_detail.py** — CivilView SaleDetails-page enrichment for CivilView records (Essex/Middlesex/Union). **CivilView PropertyIds are EPHEMERAL** — the backend reallocates every id when its snapshot rebuilds (every few minutes), so scrape-time PropertyIds are dead by enrichment time (this, not ELB session affinity, caused the 2026 Middlesex/Union 0% saga; a stale-id GET 302s to /Home/Index, which is also why "direct GET doesn't work" looked true). Plain HTTP (no Playwright): per county, fetch the live listing, index rows by sheriff # (always `cells[1]` — stable, unique), resolve each record's CURRENT PropertyId, GET `/SaleDetails` in the same requests.Session; a mid-batch rotation triggers one index refresh + retry. Parses `<div class="sale-detail-label">/...value` pairs (court_case_number, approx_judgment — Essex labels it "Approx. Upset", minimum_bid, plaintiff_attorney, plaintiff_attorney_phone, parcel_number, property_note) + the Status History `<table>` (current_status, status_history_json, adjournment_count, first_scheduled_date, days_since_first_scheduled). Derives `case_disposition` (Open/Sold/Redeemed/Bankruptcy/Cancelled) + `is_open`. **Auto-drops** records whose disposition ends up in `_DROP_DISPOSITIONS = {Sold, Redeemed, Cancelled}` — those auctions are over. Records missing from the live listing (retired between scrape and enrichment) pass through un-enriched. Rate-limited 1.8–2.5s with jitter.
- **nj_taxrecords.py** — HTTP wrapper around taxrecords-nj.com (Vital Communications `inf.cgi` backend). `lookup_by_block_lot(county, block, lot, qualifier="")` POSTs `select_cc/district/block/lot/qual` and returns candidate rows. Covers Middlesex / Somerset / Union — **Essex is on a different backend** and is not supported. Used by `obituary_enricher.py` (DM address waterfall) and the `deep_prospecting/` package; NOT a tax-sale scraper — name is a historical accident. The dedicated `newjerseytaxsale.com` scraper was removed in May 2026 (cloud-IP blocking + most records lacked addresses).
- **nj_newark_code_violations.py** — PARKED. Newark CKAN portal (data.ci.newark.nj.us) returns Cloudflare-passthrough 503 (origin down as of 2026-04-17). Module is complete (Playwright + stealth patches + Open Complaints schema) and ready to reactivate when the portal comes back online.
- **nj_scraper.py** — NJ Lis Pendens scraper for njlispendens.com (aMember Pro auth; primary NJ source for pre-foreclosure filings across Essex/Middlesex/Somerset/Union). Hybrid HTML+CSV approach because the results page renders street addresses as anti-scrape `<img src="/member/property/graphicaladdress?pid=...">` — only city/state/zip is plain text. Flow: (1) login with `NJLISPENDENS_EMAIL`/`PASSWORD` (cookies cached in `nj_lp_cookies.json`); (2) navigate to `/member/property` with filters as URL params (`County[]=...&date_added=7&per_page=50&cp=N`) — no form-click; (3) paginate HTML, parse each `<div class="mb_div-table">` block for Docket No, File Date, Defendant, Plaintiff, Orig Mortgage, Mortgage Date, Attorney, Attorney Phone, Lot-Block, County, city/state/zip tail, pid; (4) export CSV from same search → 5-col `Name, Address, City, State, Zip`; (5) **join CSV↔HTML by (sorted-token name key + zip)** — HTML uses `Last, First`, CSV uses `First Last`, so the normalizer strips punctuation and sorts tokens (verified 70/70 match). Rich fields packed into `NoticeData.raw_text` as `"Docket: X | Plaintiff: Y | Attorney: Z | Orig Mortgage: $N | Lot-Block: ...").
- **nj_middlesex_probate.py** — Middlesex County surrogate probate scraper (Bluestone Public Search at surrogatesearch.co.middlesex.nj.us). No auth, no captcha. Filter is per-day DOD (not a range), so the scraper loops day-by-day through `--days-back` values (default 30). Each day's search returns a grid of probate rows; cells carry `column="<name>"` attrs (full_name, instr_num, ix_date_1/2/4/5 = filed/DOD/DOB/issued) so we parse by column name, not position. Detail pages use stable GET URLs (`web_case_detail_middlesex.aspx?Q_PK_ID=N`) — no ViewState, so we fetch them concurrently with plain `requests` to grab decedent's mailing address + the `ASPxGridView2` parties grid (Name/Type/Relation/Status). `_pick_executor` prefers type=Executor + status=Accept, falls back to Administrator, then any fiduciary. NoticeData: `owner_name`=executor (the DM), `decedent_name`=deceased, address/city/zip from decedent's mailing address, `decision_maker_relationship` from parties grid, `dm_confidence="high"` (court-named). NJ municipal suffixes (Borough/Township/City) are stripped from the city field so Zillow/Smarty lookups work.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs. Reads from scraped notice CSVs in `output/`.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## Site-Specific Details

The site is **ASP.NET WebForms** — all navigation uses `__doPostBack()` with ViewState. Session IDs are embedded in URL paths (`/(S({guid}))/`). Playwright is required because direct HTTP requests would need to manage ViewState/EventValidation manually.

**reCAPTCHA v2 is required on every single notice detail page**, even when logged in. There is no CAPTCHA on login, search, or results pages. The sitekey is hardcoded in `config.py`.

## Saved Searches

8 searches defined in `config.py` as `SAVED_SEARCHES`. Each maps to an exact dropdown option name on the Smart Search dashboard:
- Knox & Blount × (Foreclosure V2, Tax Sale V2, Tax Delinquent V2, Probate V2)

Filterable via `--counties` and `--types` CLI args (comma-separated, or omit for all).

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures. The scraper parses each notice's full text and only includes ones with trustee sale language. See `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Owner names** in foreclosure notices typically appear after "executed by" in the deed of trust language.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

**Date Semantics (build 1.0.30+):** `date_added` = the date WE added the record (the pipeline run date, stamped in `run_enrichment_pipeline`), so a daily run shows today. The legal notice's publication date lives in its own field/column, `date_published` / "Notice Publish Date" (parsed by `notice_parser` / the scraper results grid). PDF/photo imports set `date_added` explicitly (preserved, not re-stamped); CSV re-import preserves both columns. Downstream that needs the filing date (DOD sanity check, DataSift Probate Open Date, the month tag, dedup tie-break) uses `date_published` (fallback `date_added`).

## Notice Screenshots (proof-of-source)

Each scraped notice gets a full-page screenshot of its detail page on tnpublicnotice.com, captured the moment the reCAPTCHA is solved and the legal notice is visible (`notice_screenshot.py::capture_notice_screenshot`, called from `scraper.py` in the kept-notice branch). The image is the actual published notice, used to add legitimacy to outreach.

- **Scope:** foreclosures only by default (`config.NOTICE_SCREENSHOT_TYPES`, comma-separated env override). Toggle the whole feature with `CAPTURE_NOTICE_SCREENSHOTS` (default on). Capture is best-effort: a screenshot failure never drops the record. PNGs land in `output/notices/` (gitignored), named `notice_{ID}.png` by the numeric notice ID.
- **Carried on `NoticeData`:** `notice_screenshot_path` (local PNG, set at scrape) → `notice_screenshot_url` (hosted link, set at output time).
- **Hosting:** Apify run pushes each PNG to the key-value store and sets a shareable URL (mirrors the deep-prospecting PDF pattern). CLI run uploads to Google Drive when `GOOGLE_DRIVE_FOLDER_ID` + `GOOGLE_SERVICE_ACCOUNT_KEY` are set, else falls back to the local path. Helpers: `host_screenshots_via_drive()`, `set_local_screenshot_urls()`.
- **Delivery to DataSift:** the URL rides along as the `Notice Screenshot` custom field plus a "Notice Screenshot:" line in record Notes (`datasift_formatter`). DataSift's CSV upload cannot push an image into the REISift Gallery panel, so the link is the supported route.

## Scraping Backend: Scrapfly (build 1.0.31+)

The gated notice detail fetch (the "caps structure": residential proxy, anti-bot, reCAPTCHA, and the proof-of-source screenshot) can run through the **Scrapfly API** instead of the in-house Playwright + 2Captcha path. Selected by `SCRAPE_BACKEND` (defaults to `scrapfly` when `SCRAPFLY_KEY` is set, otherwise `playwright`).

- **`scrapfly_client.py`** provides `ScrapflyNoticeClient`. `login(session)` logs into Smart Search inside a Scrapfly session (forms-auth cookie + sticky residential IP), then `fetch_notice(id, session)` opens the detail page with `asp=True` + `render_js=True`, a JS scenario clicks "View Notice" (ASP solves the reCAPTCHA), and it returns rendered HTML + a full-page screenshot in one call. `fetch_notices(ids)` logs in once and yields a result per ID. Best-effort with retries; every call returns a `NoticeFetchResult`.
- **Scraper integration** (`scraper.py`): when `SCRAPE_BACKEND == "scrapfly"`, Playwright still drives login + saved-search navigation and supplies each notice ID, but the per-notice content + screenshot come from Scrapfly via `_scrapfly_notice()`. Any Scrapfly failure falls back to the 2Captcha path, so the swap is safe. Returned HTML is parsed by `notice_parser.parse_notice_html()` (shares field extraction with `parse_notice_page`).
- **Screenshots** come natively from Scrapfly (`screenshots={'notice': 'fullpage'}`), saved to `output/notices/` and hosted/linked exactly like the Playwright path.
- **Tooling:** `scrapfly_spike.py --id <id>` validates one notice (gate clears + screenshot) before relying on it. `backfill_screenshots.py [--csv ...]` logs in once and backfills screenshots for a master list (e.g. the output of `consolidate_foreclosures.py`), writing `notice_screenshot_path` / `notice_screenshot_url` back to the CSV.
- **Env:** `SCRAPFLY_KEY` (required), `SCRAPE_BACKEND`, `SCRAPFLY_COUNTRY` (default `us`), `SCRAPFLY_RENDER_WAIT_MS`, `SCRAPFLY_TIMEOUT_MS`, `SCRAPFLY_MAX_RETRIES`. Needs `scrapfly-sdk` (in requirements.txt).
- **Open validation:** whether Scrapfly's ASP clears this site's in-page reCAPTCHA "View Notice" gate is confirmed per-notice by the spike. A `gate_not_cleared` result means the JS scenario action schema or an explicit CAPTCHA step needs a tweak.

## Foreclosure Master List Consolidation (build 1.0.31+)

`consolidate_foreclosures.py` builds a master list of still-active foreclosures from the last N months of runs. It pulls each Apify run's `output.csv` from the run's key-value store (the default dataset is unused), merges local `output/` CSVs, dedupes by **property** (address + city, keeping the latest sale date so republished/postponed notices collapse to one), and removes any whose `auction_date` ("option date") has already passed. Needs `APIFY_TOKEN`. Output: `output/foreclosure_master_active_<date>.csv`.

```bash
python src/consolidate_foreclosures.py --months 3                  # Apify + local
python src/consolidate_foreclosures.py --months 3 --require-sale-date  # drop no-date junk
python src/consolidate_foreclosures.py --county Knox --no-apify     # local only, one county
```

## Comp Package Engine (build 1.0.33, 2026-07)

One-command, boundary-filtered comp package for a subject property (the "158 Old State Rd" deliverable, generalized). Pipeline: subject facts -> API sold/active pull -> boundary clip -> condition bucketing -> dual-track ARV -> rehab scenarios -> MAO math -> buyer matching -> branded Excel workbook.

- **`src/zillow_market_api.py`** — reusable OpenWeb Ninja `/search` client. THE API CONTRACT MOVED: `similar-sale-homes` (and every other comps-style endpoint) is retired and 404s; `/search` is the workhorse. Hard-won contract (verified 2026-07-21): `home_status` must be exactly `RECENTLY_SOLD`/`FOR_SALE` (else 400); every search caps at 41 rows with `totalPages=1` (~5 weeks of sales in an active zip), so `pull_sold()` partitions by `min_price`/`max_price` bands and recursively splits saturated bands (recovers 2-3 years per zip, ~50-80 calls); `price_min`/`price_max` are SILENTLY ignored — always check the echoed `parameters` object to confirm a filter applied; `dateSold` is epoch ms; `soldPrice` is a display string (use `unformattedPrice`); `homeType: LOT` can be a house sold at land value or a new build with missing sqft (verify against the county card). MLS-only: auction/wholesale/off-market transfers never appear — county records are truth for those.
- **`src/comp_package.py`** — CLI orchestrator (see Commands). Boundary = bbox AND street-regex (apply both: bbox catches street misses, streets catch bleed across I-40/highway edges). Condition bucketing by sold-price/Zestimate ratio (>=0.90 renovated/retail, <=0.70 distressed). Buyer sheet auto-matches the latest `output/buyers_datasift_*.csv` by zip. County card overrides (`--beds/--baths/--sqft/--year-built`) beat Zillow — aggregators get bedroom counts wrong.
- **`comp_analyzer.py`** `fetch_comparable_sales()` now routes through `zillow_market_api` (old endpoint dead); the ARV/adjustment/report engine on top is unchanged.

**Rollout (2026-07-21):** the API pull is the CORE comp-acquisition path across the deal-analysis stack. `deal_analyzer.py` and `main.py comps` already route through the fixed `fetch_comparable_sales`; `real-estate-comping.skill` and `deal-analyzer.plugin` now teach the API-first path (comp-package contract) with manual Zillow/Redfin browsing preserved as the no-key fallback for community users who skip the API. `property_enricher.py` is unaffected (uses the still-live `property-details-address`). Deep-prospecting v4 has no comp surface (heir resolution only). No SiftStack module calls apiv2.reisift.io directly (all CRM writes are Playwright browser automation or Deal Room `_api` scripts, which carry their own Api-Key auth per Ty's directive).

**Dual-track ARV (bedroom-band rule, Ty 2026-07-21):** a subject whose bed count is below the comp set lives in a LOWER value band than per-bedroom adjustments imply (37914 proof: renovated 2-beds capped $215-280K while same-size 3/2s ran $285-385K; a NEW 688sf 3/2 beat the whole 2-bed band at $265K). Base ARV = same-bed renovated comps only, clamped to that band's MEDIAN price (extra sqft cannot escape the band); reconfig-to-more-beds is a labeled UPSIDE track (capped at band p75) credited only after a walkthrough verifies the layout converts. Underwriting (MAO, contract targets) always uses the base track; future-value projections ride the same-bed curve. For stalled/partial renovations, underwrite full gut until walked.

## Dispo Stack (build 1.0.34, 2026-07)

Reusable buyer-finding + dispo-outreach pipeline, generalized from the 158 Old State Rd deal so ANY future property starts with deed-verified buyers and 3-source contact data instead of backfilling. Chain: `buyer_sweep` (who buys here) -> `dispo_skiptrace` (how to reach them) -> `deal_package` (one clean workbook). Runs against the shared Deal Room `_api` SiftMap client + reisift Open API key.

- **`src/buyer_sweep.py`** — SiftMap deed-level buyer sweep for a zip. Pulls the sold universe (Zillow `/search` band pull or a saved `--sold-json`), filters to the investor band (`--min-price/--max-price`, default $25K-$170K, `--months` default 18), then per sale runs SiftMap `autocomplete -> get_detail` for the DEED `sale_history` (buyer_name, is_cash_sale) + `owner_info` (portfolio size/value/equity, mailing). Aggregates + ranks buyers by purchase count, band-fit, portfolio. **Unmasks hidden principals:** when an LLC's mailing address is a residence, it reverse-lookups that address through SiftMap `owner_info` and takes the human owner as the principal (the "Harper move"), falling back to Enformion BusinessV2 officers. Live 2026-07: resolved 175/193 37914 sales -> ranked buyer list; found TN Super Props -> Jonathan Harper, Braden Family -> Joshua Braden by reverse-address. Output `output/buyer_sweep_<zip>_<date>.json|.csv`.
- **`src/dispo_skiptrace.py`** — three-source skip-trace waterfall with a built-in AUDIT MATRIX. Per contact: Source 1 Enformion Person Search (address-anchored via `_best_person` to beat common-name collisions), Source 2 Tracerfy batch ($0.02/rec), Source 3 web people-search cross-check (MANUAL: aggregators bot-block, so it merges a `--web` JSON dropped in by an agent/browser). Dedupes the union, Trestle-scores every unique number, and emits per-number `sources` + `confirm_count` (x2/x3 = cross-confirmed) plus a per-contact audit showing which source MISSED (answers "did we skip-trace this landline at both Tracerfy AND Enformion?"). Dial tiers = phone_validator standard (81-100 first, 61-80 second, 41-60 third, <=40 drop). Input = contacts JSON; output `.json|.csv` with `single_source_flag` + source-gap list.
- **`src/enformion_business.py`** — Enformion **BusinessV2** client (`galaxy-search-type: BusinessV2` on `devapi.enformion.com/BusinessV2Search`, verified live). The v1 `BusinessSearch` type is access-denied and `AddressSearch` is unlicensed on this account. `find_principals(entity, city_state)` returns human officers from `usCorpFilings`/`newBusinessFilings`, filtering out entity self-refs and commercial registered-agent fronts (Northwest Registered Agent, US Corp Agents, etc.).
- **`src/deal_package.py`** — spec-driven 6-sheet workbook generator (the consolidated 158 deliverable, generalized): 1 Deal Summary (numbers to use, value anchors, done-work story, contract gates), 2 Dial Sheet (ranked buyers with PER-BUYER open/target prices), 3 Deal Math (buyer-side + your-side, rehab detail, dual-track ARV), 4 Comps (each with its ROLE in the pitch), 5 Pitch + Sequence (30-sec script, objection answers, day-by-day plan), 6 Sources + Audit. Every section optional on its spec key. DataSift brand styling, zero em/en dashes. `--template` writes `deal_spec_template.json`; `--spec x.json --out "Addr_Deal_Package.xlsx"` renders.

**Feasibility framing (Ty, 158 run):** contract price at/above the as-is band converts a discount-wholesale into a dispo-EXECUTION play: the fee is won on the buyer side, not the buy. GC-model flippers drop out once rehab is heavy (their MAO collapses); the buyer pool becomes SELF-PERFORMERS and landlords, whose MAO/1%-rule math tolerates a higher price. Always verify the seller's real payoff at the Register of Deeds before trusting a stated "what he owes" number, and hold a novation/MLS listing as the backstop (an MLS shell sale is the true market ceiling). Per-buyer ask prices are tuned to each buyer's model (self-performer > landlord > out-of-state), not one blast number.

## Post-Walkthrough Package (build 1.0.35, 2026-07)

`src/post_walkthrough.py` is the JOIN POINT of the deal-analysis stack: the one workbook you build the hour after walking a house. It spins the comp engine, the rehab engine, the walkthrough findings, the exit engine, and the dispo stack into the exact 8 sheets of `Post Walkthrough Template.xlsx` (Overview | Exit Strats | Comps | Active-Pending | Repair Logic | Repair Numbers | Buyer Targets | Outreach Sheet), contextualized by the LIVE Sift lead. Where `comp_package.py` answers "what is it worth", this answers "we walked it, now what do we do with it and who do we call".

- **Sift lead is the anchor, not the address.** `load_lead()` runs the Deal Room `dossier.build_dossier(flow="A")` (CRM record + custom fields + message board + SIFTline cards + activity summary + SiftMap detail). Auth defaults to the **no-expiry Api-Key account** (`DEFAULT_SIFT_ACCOUNT = "datasift-apikey"`) by setting `REISIFT_ACCOUNT`, which `reisift_auth._resolve_account_name` honors ahead of `active_account`; `--sift-account` switches to a JWT account when the lead lives elsewhere. Live 2026-07-23 on 158 Old State: owner Maron Brown, status Warm Lead, SIFTline Acquisitions/Offer Accepted, 11 board messages (surfaced the real blocker: title not cleared).
- **Record field names (verified live, they are NOT the CSV upload names):** `estimate_value`, `equity_percent`, `last_sold`, `last_sale_price`, `rental_value`, `sqft`, `bedrooms`, `bathrooms`, `year`, `lot_size`, `parcel_id`/`apn`, `investor_score`, `structure_type`, `assigned_to` (bare uuid, not a dict), `address{street,city,state,postal_code,county,latitude,longitude,vacant}`, `owner{first_name,last_name,company,address{...}}` (mailing lives here). County and lat/lon come off `address`, so the comp Dist column works with **no Zillow call**; `rental_value` auto-feeds the BRRRR line.
- **Subject-fact precedence:** explicit CLI (county card) > Sift record > Zillow. Aggregators get bed counts wrong, so the human override always wins.
- **Repair Numbers = the rehab engine expanded to a 4-scenario matrix** (Cosmetic at existing config / Mid Reno / Full Gut T2 / Full Gut T3 at the reconfig target), left block category x scenario, right block itemized line items per category. Line-item labels must NOT carry tier-dependent unit rates or the same line splits into one row per tier. Walkthrough **credits** (work the seller already paid for) and **team-walk flags** are their own rows, never smeared into categories, so every dollar traces back to Repair Logic. Bottom block: materials, labor, subtotal, soft costs, GC grand total, and a **self-perform estimate** (`SELF_PERFORM_LABOR_FACTOR = 0.55`, the lane that actually buys heavy-rehab shells).
- **Exit engine scores up to six exits off the CONSERVATIVE ARV track** and prints why each is recommended AND why not: wholesale assignment, wholetail, flip same-config T2, flip reconfig T3 (gated on `reconfig_verified`, else labeled upside only), BRRRR, novation/listing. **Only the exits that clear their gate get a suggestion block** (Rami, 158 review: the template's six slots were placeholders, not a quota); everything ruled out is named in the headline and explained in the logic block. If nothing clears, the two closest misses render under an explicit "nothing cleared its gate" banner. Each exit carries `kind` (assign/resale/hold): the Outreach sheet names the **buyer's** exit (best viable `resale`), never our hold. BRRRR profit is cash out at refi, a different unit from sale profit, and the logic block says so out loud.
- **EXACT numbers, not ranges (Ty, 112 Milligan review).** `EXACT_NUMBERS = True` makes every cell print one figure; the lo/hi still drives the math and surfaces as a single "If it moves" sensitivity line under each block. A wide band is not an answer you can take to a seller or a buyer. The range machinery below still computes the downside, it just does not render in the cells.
- **Lanes we actually run: wholesale, wholetail, fix and flip, rental (dispo angle). Novation is NOT modelled** and was removed from the exit engine, not just hidden.
- **`tight_arv()` is the underwriting ARV.** The dual track gives the wide market picture; underwriting uses the tight set and overwrites `arv["base"]` with it. Three hard rules: RECENT (prefer 12 months, widen to `--months` only if that leaves under 3 comps, and say so), SIZE (`ARV_SIZE_LO/HI` 0.70-1.35x subject sqft, because $/sf does not carry across a 2x size gap: a 2,392 sqft sale cannot price a 1,400 sqft house), SAME BED (clamped to the same-bed median sale so extra sqft cannot escape the bedroom band; a +/-1 bed widen costs an 8% discount). Emits a `basis` string (comp count, bed, sqft window, date span, median $/sf) that renders on Exit Strats. **Pass a `--months` POOL wider than the preference** (24 works) or the upstream recency cut starves the widener: 0.5mi + `--months 12` left only 2 comps.
- **`--months` is enforced on the cached-pull path too.** A saved `--sold-json` spans years; without the filter, stale sales quietly set the ARV.
- **Prices ship as RANGES unless we are confident** (Marwan, 158 review: "anytime it gives us a price, can it give us a range if it's not confident"). `rng()/fmt_rng()` carry lo/hi/point/confident; a confident figure writes a numeric cell, an unconfident one writes `"$X - $Y"` (plain hyphen). Rules: rehab is always a range (-10%/+15%, overruns skew high) until a signed bid lands in `walk["bids"]`; ARV is the comp band itself and tightens to one number only at n>=5 with band width <=30%; a signed `contract_price`/`assignment_price` is a fact, a derived MAO is a range; profit pairs the low sale with the high rehab.
- **Comps sheet restructured.** The template's second date column ("Date") had drifted from "Sold" in the sample data, so both ambiguous columns are replaced by the two facts that get argued about in a dispo call: **vs Zest** (sold over Zestimate, the signal the Bucket is derived from, so the call is auditable) and **Buyer (deed)** (who actually bought it, `CASH:` prefixed, joined from the buyer sweep's `records` block via `_norm_addr`). That wires the comp table into the dispo list: the buyer of the distressed comp two streets over is the person to call.
- **Bucket refinement (`refine_bucket`), a real accuracy fix the deed join exposed.** Zillow re-anchors the Zestimate to a recent sale, so an investor buy shows a ~1.00 ratio and `comp_package.classify` reads it RENOVATED, dragging the same-bed retail median down and understating ARV. When the ratio sits in the absorbed band (0.97-1.03) OR the deed shows an entity/cash buyer, AND the $/sf is well under the retail median, the comp is rebucketed. **Size guard:** $/sf falls as houses get bigger, so a sale priced at or above the retail band's lower quartile is never demoted (this is what keeps a large renovated comp from being thrown out). Refinement runs BEFORE the ARV: demoted comps are withheld from the list passed to `dual_track_arv` (which calls `classify` internally) but still render on the Comps sheet, correctly labeled, with a footnote counting the corrections. Live 158: 4 investor buys ($105K, $105K, $132K, $163K) pulled out of the 2-bed retail set, base ARV $265K -> $280K and tight enough to publish as a single number.
- **Walkthrough JSON is the human layer** (`--walkthrough-template`). Anything filled in OVERRIDES the live record, so fields stay empty unless the walk proved the record wrong. `work_done[].credit`, `flags[].cost`, and per-item `scenarios` flow straight into the matrix; `gates` render as pre-contract verify items.
- **`single_scenario` (walk key, 2026-08-10):** once the menu phase is over (contract signed, comps dictate the finish), the walk JSON collapses the 4-column matrix to ONE plan: `{"key","label","tier","scope","gut","beds","baths","drop"}`. Exits then price every lane off that one work number (`work_rng`/self-perform/pitch all fall back when the scenario list has a single entry). Comp-driven finish upgrades ride as named `flags` rows (auditable deltas), with the evidence recorded in a `comp_finish_basis` walk field. Built for the 3014 Sanland comp-match consolidation.
- **Placeholders, never blanks (Ty, 2026-08-10):** a pending sub quote never renders as $0 or an empty cell. Walk flag `"placeholder": true` gives the line a realistic assumed cost painted RED (C00000) with a legend row, so the grand total is always a true number; replace with the signed bid and re-render. Pair with `"drop"` on the scenario when the placeholder REPLACES an engine category (Sanland: engine Roof line dropped, the red $8,500 roofer line IS the roof budget). Labor model: `"self_perform_factor"` + `"labor_model_label"` walk keys override the 0.55 own-crew factor (Sanland runs 0.75 "PM + subs (owner-managed)"), renaming the second budget line and the flip lane to the model the operator actually runs.
- **Financing in the profit + Lender Analysis sheet (2026-08-11):** a walk `"financing"` block (`kind/rate/points/term_months/ltc/draws/lender/assumed`) bakes private money into every resale lane: profit goes NET OF DEBT (points + interest on the full balance over the lane's hold, conservative vs a draw schedule) plus buy-side closing (`BUY_CLOSE_FLAT` $900 + `BUY_TITLE_PCT` 0.77% title). ROI reads as cash-on-cash when financed, and a financed flip must ALSO clear the $10K wholesale floor to stay suggested. A 9th sheet, Lender Analysis, renders sources and uses, the draw schedule, loan-to-ARV, equity cushion, day-one as-is coverage, a band-floor stress case, the payoff waterfall at the POINT ARV over the FULL term (the conservative case; Exit Strats mids the band on a faster hold, the truth lives between), and the lender's annualized yield. `"assumed": true` paints the red placeholder-terms banner. No financing block = the old cash-basis math, byte-identical.
- **Free re-runs:** `--sold-json` reuses a saved band pull (`output/zillow_37914_sold.json`) instead of paying for the 50-80 call partition again. `--save-pack` writes the assembled pack; `--spec` re-renders it. Buyers come from `buyer_sweep`'s `ranked` list (`buyer`, `n_buys`, `cash_n`, `avg_price`, `portfolio_n`, `principal`, `buys[[addr,date,price]]`), ranked by fit against THIS deal's band and capped at `--max-buyers` (default 25) with the drop count stated on the sheet.
- **As-is band is the number that decides the deal, and it is the easiest one to corrupt.** Three guards, learned on the 112 Milligan run: (1) `NON_ARMS_LENGTH_RATIO = 0.30` drops family deeds/quitclaims (a $12,000 sale on a $247,700 house); they still render on Comps with a NOT ARM'S LENGTH role note. (2) Size band 0.65-1.40x subject sqft, because $/sf does not transfer across a 2x size gap. (3) Priced BELOW the retail band floor, because "sold under Zestimate" catches ordinary $240K-$300K trades that are not as-is investor buys. Best source when available is deed-verified cash/entity purchases from the buyer sweep (needs 3+ in the size band); the distressed bucket is the fallback. When the pocket is too thin for any of it, set `as_is_value` in the walkthrough JSON with a written basis: that override exists for exactly this.
- **Boundary discipline is not optional on the ARV.** 112 Milligan at 1.0mi bled into the Chaucer/Milton/Bobwhite subdivisions and pushed base ARV to $325K; held to 0.75mi the pocket reads $300K-$395K around the next-door twin at $305K. Always test 2-3 radii and look at WHICH streets enter before accepting an ARV.
- **Degrades, never fails:** no CRM auth, no API key, no buyer sweep, no skip trace each render a stated reason in place of the section and the workbook still builds.

**`buyer_sweep.py` auth (fixed 2026-07-23):** the sweep took `reisift_auth`'s `active_account`, normally the ~48h admin JWT. With that token expired every `get_detail` threw, the per-property `except` counted it a miss, and the run exited 0 with "resolved 0/133 sales" as if the market were empty. It now pins `--account` (default `datasift-apikey`, no expiry) into `REISIFT_ACCOUNT` and logs an explicit AUTH-or-COVERAGE error when it resolves zero of a non-empty target list. Same class of failure as any other silent-degradation path: a run that "succeeds" with no data is worse than one that fails.

## Knox First-to-Market Pull + DataSift API Upload (build 1.0.36, 2026-08)

`src/knox_ftm_pull.py` collects every Knox FTM source that carries a property address, enriches against SiftMap, applies the buy box, and writes an upload CSV. `src/datasift_api_upload.py` pushes it into DataSift entirely over the API. `src/knox_lien_resolve.py` turns lien debtors into parcels. `src/datasift_schema_setup.py` creates the custom fields, select options and lists (idempotent, dry-run by default).

```bash
python src/knox_lien_resolve.py --all --workers 6         # debtors -> parcels
python src/knox_ftm_pull.py --out output/knox_ftm_pull.csv
python src/datasift_schema_setup.py --commit              # schema, safe to re-run
python src/datasift_api_upload.py --limit 1 --commit      # ALWAYS verify one first
python src/datasift_api_upload.py --commit
```

**Buy box (Ty):** single family only, AVM **$1 to $700,000**. The $1 floor is deliberate and wider than the $100K floor in `_api/build-ty2-priority-siftmap.py`, because condemned and tax-distressed stock routinely falls under $100K.

**Sources and their real depth.** Liens/state tax/federal tax liens and trustee deeds come from the Register of Deeds (12 months). Notices come from tnpublicnotice (12 months). **Condemnations are one cycle only** and **evictions are one week only**: the city overwrites its agenda PDFs and the court keeps only the current week on the server (~86 back-dated URLs all 404). Both accumulate forward or not at all.

**Liens carry no parcel id** (0% of rows) because they are indexed against the person. The join is debtor name -> the open county tax API. See [[reference_knox_lien_join]] for the guards; full-run hit rate is **40%** (a 500-name sample read 64% only because it was sorted highest-lien-count first).

**Release filtering is not optional.** 27,493 release documents exist against 12 months of liens. **8% of lead debtors had EVERY lien already satisfied** and were dead leads. `load_liens` computes active = recorded minus released, drops fully-cleared debtors, and states `3 of 8 still active` in Notes. Instrument-level matching is the trustworthy signal; a name match only means that person had *something* released.

**Numbers that decide a deal, and where they come from:**
- Lien amounts live in the recorder's **Consideration** column (11,511 of 12,867 general liens, 373 of 377 federal; state tax liens carry none). Only ACTIVE liens are summed.
- **Condemnation dollar figures are PROSE in the agenda**, not API data (`"1 bill $254.00, county tax $271.36 (2025)"`). `_condemnation_money()` parses them; without it those records upload completely blank (caught on 3240 Wilson Ave).
- Tax delinquency is per-parcel and **only ~12% of parcels owe anything** — a sparse column is correct, not a fill failure. The delinquent YEAR is gated on a positive per-parcel amount, because the county API returns bills per OWNER and a multi-parcel owner would otherwise stamp one property's debt onto another.
- **No mortgage of record = free and clear = 100% equity** (Ty). Leaving equity blank made those records unjudgeable for the upside-down test.
- Upside-down records are written to `_upside_down.csv` and EXCLUDED from the upload: debt swallowing the equity is not workable.

**Date semantics here differ from the scraper.** `Date Added` holds the **county filing date** (recording date / hearing date / publication date / docket date), not the pull date, per Ty. Provenance survives as a `pulled_<date>` tag alongside `filed_<YYYY-Qn>`.

### DataSift API upload contract (hard-won, 2026-08)

**Auth: mint the JWT, never paste one.** `POST /api/token/` with `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` from `.env` returns `{access, refresh}`. The uploader mints on start and re-mints every 30 minutes so long runs cannot die on expiry. **The Open API key cannot do this job** — custom fields do not exist anywhere in its 93-route surface and every write 401s. The minted user JWT reaches `/api/internal/` where they do.

Four traps, each of which fails silently or cryptically:
1. **Tags must be an ARRAY.** A comma string creates one tag literally named `"Courthouse Data, code_violation, Knox"`.
2. **A select field's value must be the OPTION'S UUID, not its label.** `"LEN"` returns `{"non_field_errors": ["'LEN' is not a valid UUID."]}`. Resolve via `custom-fields/` `options[]`.
3. **Entity owners cannot have a blank `first_name`** (the API rejects it). Send the business as `company` and OMIT the person keys; omitting a key is not the same as sending `""`.
4. **`notes` on the property payload returns 200 and is discarded.** Post it separately.

`POST /property/` is **upsert by address**, so re-runs never duplicate, and **lists accumulate** rather than overwrite (verified: a record came back with both new lists plus four it already had). Custom fields go to `PATCH /api/internal/property/{uuid}/custom-field/update-values/` with `[{"field_uuid": ..., "value": ...}]`. Creating a `select` custom field REQUIRES its options in the same POST.

**Always upload one record and read it back before releasing the file.** That single habit caught the tag format, the entity-owner rejection, the option-UUID requirement and a list-name mismatch that would have silently attached nothing for 2,512 of 2,573 records.

## Obituary Opportunity Ranking (build 1.0.37, 2026-08)

`src/obituary_opportunity.py` turns a reisift account's **Obituary list** into a lean-budget call order. The premise: a notice-of-default owner is on every wholesaler's mail drop because the filing is public and machine readable, but a decedent home is only reachable after somebody researches who died, who inherited and who signs. That research is the moat. Chain: pull (detail + custom fields) -> gate -> six weighted components -> branded 6-sheet Excel. Read-only, runs on the no-expiry Api-Key account (`datasift-apikey` = ty+2).

```bash
python src/obituary_opportunity.py --pull                          # refresh output/obituary_raw.json
python src/obituary_opportunity.py --out output/Obituary_Opportunities.xlsx --top 60
python src/obituary_opportunity.py --min-months 6 --mail-cost 0.75 --touches 6
```

**What the ty+2 obituary universe actually is (measured live, 740 records, 424 qualified):** NOT a distressed-debt list. 63% of qualified records are free and clear, **99% carry no auction-track flag at all**, and only ~3% carry any tax delinquency, lien, vacancy or code action. It is paid-off senior homes whose owner died. The motivation is the estate itself, so the pitch is speed, certainty and as-is, not rescue.

**Weights are set from that measured distribution, not intuition** (`W_DISTRESS 28, W_FIT 22, W_EQUITY 20, W_TIMING 12, W_SATURATION 10, W_CONTACT 8`). A first pass at equity 30 / saturation 20 produced only **11 distinct scores across the top 40** because on this list equity and quietness are near-constants. **Saturation is a LIST-level advantage, not a within-list ranking variable**: it is the reason to work obituary over foreclosure, and it is already banked the moment you pick the list. It is weighted 10 and the finding is stated on the Overview sheet rather than buried in a weight. The variables with real spread are dataflik `investor_score` (p10 18 to p100 100), `realtor_score` (inverted: a high one means an agent wins it, not you) and `year` built (older stock means rehab, which means retail hesitates).

**Gates (each counted on sheet 6, nothing silently dropped):** no obituary/death date; **under 3 months since death** (Ty's rule, give probate time to open, drops 237 of 740); already sold or an MLS sale after the death; `DEAD_STATUSES`; upside down; do-not-mail; no value; over the $700K buy box (drops 57); not single family.

**Two traps this build exists to avoid, both caught live:**
- **`Total Delinquency` is liens PLUS taxes.** 6031 Ridgeview reads 13,766.72 = 12,908.72 lien + 858.00 tax. Reading it as the tax figure double counts the lien and inflates exactly the records the model is built to surface (it put a lien-only record at rank 1). Tax amount comes from `Tax delinquency amount` or native `tax_delinquent_value`, never from Total Delinquency. Unpaid tax YEARS are often only in the `notes` prose, so `flatten()` parses "Unpaid county tax years: 2025" as a fallback.
- **Gate on lead status, not just on sold.** 205 Shasta Dr topped the ranking on perfect fundamentals (vacant, absentee, free and clear, investor score 91) while sitting at `not_interested`. `DEAD_STATUSES` drops those 22 records; `IN_PROGRESS_STATUSES` flags rather than drops the ones already being worked.

**Every row ships a "Must verify" note**, because whether the person who died is the owner of record is NOT verifiable from CRM data. Zero ty+2 obituary records carry a probate open date, decedent name or resolved heir, so the whole research layer is still ahead and the spouse-obituary trap is live on every single row. Sheet 3 isolates the ~12 records that actually carry hard distress, since that is where the lien and tax-delinquency numbers exist at all.

**Rate limit:** `/api/internal/` throttles hard. Six threads at ~7 req/s 429'd 529 of 740; single-threaded at ~2 req/s with backoff on the server's "available in N seconds" hint completed cleanly. `pull()` is resumable and checkpoints every 25 records.

## Call Coaching Engine (2026-07)

Pulls real call recordings from the SmrtPhone web session, transcribes them with tonality notes, and routes them to three grading skills (`~/.claude/skills/`): **cold-call-coach**, **lead-manager-coach**, **closer-coach**. Each skill grades transcripts against a rubric built from the DataSift Call Playbook KB.

- **`src/call_coaching/pull_calls.py`** - SmrtPhone call log via `POST /logs/calls/filtered` (DataTables form, cookie session from `smrtphone_state.json`). Returns duration, disposition, caller, reisift record link, and a DIRECT recording URL on `rec.smrtphone.io` (public once known, no auth). Filters >= `--min-seconds` (default 60) + has recording; downloads MP3s to `output/call_coaching/recordings/`. Session expired -> exit 2; re-run `_api/smrtphone_login.py` (Deal Room Coaching Call project).
- **`src/call_coaching/transcribe.py`** - two passes per call via OpenRouter Gemini 2.5 Flash (~$0.002/audio-min): (1) audio -> diarized transcript with bracketed delivery notes + DELIVERY SUMMARY (pace/tone/talk balance; the model hears the audio), (2) text -> strict-JSON triage (call_type, pipeline cold_call|lead_management|closing, worth_grading). AGENT/SELLER labels are decided by content with the caller name as anchor (callbacks otherwise swap the labels). Outputs `transcripts/{id}.md|.json` + `review_queue.json` grouped by pipeline.
- **Grading:** Claude (in-session or via Workflow fan-out) scores each `worth_grading` transcript against the skill's `references/rubric.md`, writes per-call reports + per-caller scorecards to `output/call_coaching/reports/{pipeline}/`. Voicemails and wrong numbers are never scored.
- **Rubric sources:** DataSift Call Playbook (Cold Caller / Lead Manager / Closer scripts + trainings), LEAD-M_1.MD, playbook research corpus + elite-call transcripts.

## Two-Way SMS Agent (build 1.0.38, 2026-08)

`src/sms_agent/` sends outreach, reads replies in real time, classifies them, writes the result back to DataSift (phone status, opt-outs, lead status), and hands positive responses to a prospector in Slack. Full runbook: `src/sms_agent/README.md`.

**The constraint that shapes the whole build: DataSift webhooks CANNOT see an inbound text.** DataSift released webhooks as a **sequence ACTION**, so the trigger surface is exactly the ten sequence triggers, all of them CRM state changes (`Property Status Change`, `Property Assignee Change`, `Property Tags Added/Removed`, `Property Lists Added/Removed`, `Task Created/Completed`, `SiftLine Card Created/Moved`). There is no SMS-received trigger, no conversation event, and DataSift does not send SMS itself (it hands off to smrtPhone/Twilio/Plivo; drip campaigns have no documented reply-exit either). **smrtPhone's webhooks are the inbound leg**: `smsIncoming` (`smsId, from, to, message, date, callerIdName, userName, contactName, source`), `smsOutgoing`, `smsDeliveryCallback` (`status`, `failure_reason`), `addNumberToDNT`, `addNumberToDNC`. Both vendors post to the same receiver. Conversation replies go out over the smrtPhone API (`POST phone.smrt.studio/sms/send`, header `X-Auth-smrtPhone`), which is TEXT-ONLY, so only the original auction-screenshot MMS still needs the browser path in `mms_sender.py`.

**Voice comes from the `text-touch-builder` skill's message recipe**, not from defaults: warm, positive, properly capitalized, one easy question per message, under 160 chars, street line ONLY (never the full address with zip), first-real-name-token hygiene (initials-only / companies / trusts get owner-of-the-address wording), and the rule the whole program rests on, **never name the list** (foreclosure, auction, probate, inherited, tax, lien, code violation, eviction, divorce, bankruptcy, "behind on") because **the seller should feel found, not targeted**. Soft no vs hard no is noted on every NOT_INTERESTED since soft nos become follow-ups. STOP or hostility gets NO reply at all, not even an apology. `knowledge/playbook.md` is the editable system prompt.

**Identity is anchored to the ASSIGNEE, and we never say a company name** (Ty: a named company is litigation bait). The record's `assigned_to` uuid resolves through `config/sms_senders.json` to a first name, so an Adriana-assigned record signs as Adriana and Adriana is who calls; an unmapped uuid means the thread goes out UNSIGNED, never a guessed name. The agent describes itself by locality built from the record's own county ("a local buyer here in Blount County"). `cli.py senders [--record <uuid>]` shows what resolves. **The responder is given almost nothing** on purpose: owner first name, street line, city, county. Valuation, equity, distress flags, vacancy, beds/baths/sqft and every list tag are withheld from the prompt entirely, so there is nothing to leak. The validator hard-blocks any draft naming a dollar amount, naming the list, carrying a link or a zip code, over 320 chars, asking two questions, or self-identifying as automated.

**Two send transports (`SMS_AGENT_TRANSPORT`), because Ty believed smrtPhone had no API.** It does, for TEXT: `POST phone.smrt.studio/sms/send` with header `X-Auth-smrtPhone` (key from Admin > API Tokens). What has no API is **MMS**, which is what forced the browser route on the original auction-screenshot send; replies carry no image, so the API is the right transport here. `session_sender.py` is the fallback, driving the web app's Compose Message modal via `smrtphone_state.json` (reusing the mms_sender mechanics: context-level microphone permission or the dialer's Allow-microphone modal covers the compose UI and silently times out every send; the compose button is icon-only and targeted by POSITION at ~[80,63]). `auto` prefers API and falls back only on a transport failure, never a 4xx. **The session path sends from the account default caller ID only**, so sticky senders and per-number caps do not apply there and `doctor` says so.

**smrtPhone API key is VERIFIED LIVE (2026-08-10).** Auth probe: `POST /sms/send` with NO params returns 400 "Missing required parameter(s): from, to, message" on a valid key and 403 on a bogus one, so that is the clean credential check and it cannot send anything. Do NOT probe `GET /dialerConfigs`: it 405s on GET and serves the web app HTML on POST regardless of key, proving nothing. Prod transport is `api`; the browser session stays a local-only fallback.

**Cloud deployment (Fly.io, `fly.toml` + `deploy/Dockerfile`).** Four deliberate choices: ONE machine with the worker as a thread inside the receiver (`SMS_AGENT_INLINE_WORKER=1`), because SQLite is single-writer and two machines would fight over one volume; `auto_stop_machines = false`, because a stopped machine drops a webhook and there is no replay; a persistent volume at `/data` holding the event log, `sms_numbers.json` and `sms_senders.json` (editable without a redeploy via `fly ssh console`); and NO Playwright in the image since the API transport works. **`src/sms_agent/crm_standalone.py` is what makes this possible**: a self-contained reisift client (`authorization: Api-Key <key>`, `REISIFT_API_KEY`, base `apiv2.reisift.io`, 429 backoff that parses the server's "available in N seconds" hint) so the cloud box needs no Deal Room checkout on disk. `crm.py` prefers the shared CRMClient and falls back to it.

**Number pool is OWNER-BOUND, 18 numbers at 25/day = 450/day.** Pulled 2026-08-11 from smrtPhone Admin > Phone Numbers, which has NO public API: `/phoneNumbers` and `/callerIds` return the SPA shell to an API key. The route is the web session plus the FOSJsRouting trick (`GET /js/routing?callback=fos.Router.setData` dumps ~1,187 routes) which finds `POST /phoneNumbers/filtered`, a DataTables endpoint like `/logs/calls/filtered`; fields come back as HTML fragments and need parsing. 21 numbers total, 3 excluded (Website 865-324-1736 on the Inbound Calls flow, Ty - Dispo 865-338-9203 on the Ty Test flow, and Adriana Test Flow 865-273-0739), leaving Adriana 9 and Tinaa 9. **`config/sms_numbers.json` is keyed by the caller who owns each number** and `sender_pool.assign(phone, owner)` prefers that caller's numbers, because the thread is signed by the assigned person and a homeowner who calls the number back must reach the same person the text claimed to be from. A sticky number wins over owner preference: changing numbers mid-thread is the worse problem.

**Autonomy ladder (`SMS_AGENT_PHASE`), because the phone number is the asset:** 1 classify + write phone status/opt-outs, 2 + escalate and flip CRM status, 3 + draft replies held in Slack for approval, 4 + auto-send a narrow gated intent set. **Phase 2 already delivers the prospector handoff with zero AI-authored text sent.** `SMS_AGENT_DRY_RUN=1` independently blocks every CRM write and every send.

**Guardrails, each from a specific failure mode:** human takeover wins instantly (an `smsOutgoing` we did not author means a person typed it -> pause the thread, cancel every queued AND held message); opt-outs are decided by regex and never by a model, and cover natural language ("stop texting me", "take me off your list") not just the STOP keyword; 6-turn cap; recipient-local 8am-9pm quiet hours from the area code, with up to 30 min of wake jitter so a night's backlog is not one 08:00:00 burst; sticky sender number per conversation (switching mid-thread reads as a spam farm); per-number daily cap + pacing; a hard output validator that blocks any draft naming a dollar amount, carrying a link, over 320 chars, asking two questions, or self-identifying as automated; a 0.80 confidence floor; and `sys_`-prefixed system tags so our own writes never re-trigger the sequences that called us.

**The loop is complete both directions.** `seed.py` renders outreach touches from `knowledge/touches.py` (the text-touch-builder pools, kept in sync with the skill) and queues them through **the same outbox as every AI reply**, so outreach gets no private send path and inherits suppression, quiet hours, per-number caps, pacing and the sticky sender. Seeding also registers `phone_map`, which is how a reply later finds its record. Staged as HELD; `release --touch N` is the deliberate go/no-go. `digest.py` is the daily readout: funnel on top, work queue underneath (drafts awaiting approval, threads a human took, soft nos old enough to rework, send failures, and a warning when webhook events sit unprocessed for a day, meaning the worker is down). Soft nos close separately from hard nos because the playbook works them again later.

**Backfill proved the classifier on REAL replies before anything was wired.** smrtPhone already syncs inbound SMS into the CRM as `owner.sms.received` activity events, so `backfill.py` replays them through the live classifier read-only. First run (24 records from the June MMS send, 9 real replies): classifier correct on all 9 (5 on rules, 4 on the model). **The finding that changed the code: 5 of the 9 replies came from a DIFFERENT number than the one we texted.** People answer from whichever line is in their hand, so mapping only the target number leaves most replies unroutable. `crm.map_all_phones()` now maps every phone on a record, called from `seed.queue` and `map --all-phones` (219 extra numbers across those 24 records). Also caught: *"I'd like it get the house tho in auction if it's cheap enough"* is a BUYER, not a seller; the model read it OTHER at 0.55, under the floor, so it drafts for a human instead of paging a prospector.

**`selftest.py` is the test harness: 69 assertions, zero network, throwaway DB, every outbound edge stubbed. Covers the engine AND the FastAPI surface (wrong secret, empty secret, IP allowlist, retry dedupe, malformed body, non-object payload, health).** Safe to run any time with production credentials loaded. It ASSERTS rather than prints, because the failure mode this codebase keeps rediscovering is a run that reports success while doing nothing. It has already caught two real bugs (both below).

**Two traps caught during the build, both silent:**
- **`numbers.py` shadowed the stdlib `numbers` module** when the CLI ran as a script (its own directory lands on `sys.path` first). That broke pydantic inside the Anthropic SDK, the exception was swallowed, and EVERY classification silently degraded to the weak keyword fallback while still returning a plausible answer. Renamed to `sender_pool.py`.
- **The model invents an identity.** With no name configured it introduced itself as "Alex". Unresolved identity now means the agent is explicitly told it has NO name and NO company name, rather than being left to fill the gap.
- **Name hygiene greeted people by their surname.** `clean_first("E A Henry")` took "the first token of length 2 or more", which walks past the initials and lands on the SURNAME, so an initials-only owner got "Hi Henry!". The fix is positional: on a multi-token name only the tokens BEFORE the surname can supply a first name, and if they are all initials there is none. **This bug was shipped in the text-touch-builder skill too** and is fixed in both.

**Knowledge base = `src/sms_agent/knowledge/playbook.md`** (the system prompt): DataSift Call Playbook, 4 Pillars of Motivation, handoff triggers, hard rules, adapted to SMS. Edit the file, not the code. The flywheel worth building next is pointing the three coach skills' grading engine at the agent's own threads, so the texter is graded by the same rubric as the humans.

**Open items:** the DataSift webhook payload shape is unverified (`handle_datasift` logs and resolves defensively, writes nothing); smrtPhone's DNT *write* route is undocumented (only the webhook is), so `add_to_dnt` tries plausible paths, always suppresses locally, and Slack-alerts on failure; smrtPhone webhooks are unsigned and its logs purge after 30 days, hence the secret URL path, optional IP allowlist, and the local SQLite event log; Slack is post-only until a real Slack app replaces the incoming webhook.

```bash
python src/sms_agent/cli.py selftest                  # 69 assertions, zero network, safe any time
python src/sms_agent/cli.py backfill --queue output/mms_send_queue.csv   # classify REAL past replies, read-only
python src/sms_agent/cli.py doctor                    # wiring check, live transports, the webhook URLs to paste
python src/sms_agent/cli.py seed --csv export.csv --touch 1   # outreach preview (--queue stages, release sends)
python src/sms_agent/cli.py digest                    # daily funnel + work queue
python src/sms_agent/cli.py senders --record <uuid>   # which caller name a record signs as
python src/sms_agent/cli.py map --csv output/mms_send_queue.csv   # phone -> record backfill
python src/sms_agent/cli.py simulate 8652548712 "how much are you offering"
python src/sms_agent/cli.py serve                     # receiver
python src/sms_agent/cli.py work --loop               # worker (separate process)
```

## Locked Master Material List + SKU-Grounded Rehab Engine (build 1.0.39, 2026-08)

The team committed to the Master Material List as THE material source. Knox pricing is pulled fresh and FROZEN: `python src/material_list.py --master --zip 37914 --cached --lock` writes the git-tracked lock artifacts `data/master_materials_locked_37914.json` (engine-canonical) + `.csv` (skill/human twin). **Only `--lock` writes `data/`**: an ordinary re-pull refreshes `output/` cache + xlsx but can never drift prices into estimates. Current lock: 94/94 search keys priced, 88 SKU rows + 12 allowances, pulled 2026-08-10.

- **`src/sku_pricing.py`** loads the lock and prices per-category material BASKETS (quantity drivers from the MASTER catalog / `build_lines`). Grade map: tier 1/2/3 -> Budget/Standard/Upgrade; tier 4 (Premium/Custom) is off-list by definition. `estimate_rehab` (knoxville/blount, tier <= 3) takes SKU materials + engine labor per category; any missing SKU drops the WHOLE category back to the legacy table with a loud log (the "outstanding random issue" clause), and a missing/invalid lock file means full engine fallback, so nothing hard-fails.
- **THE DOUBLE-DISCOUNT TRAP: locked prices are already Knox-local. The 0.88/0.86 regional multiplier applies to LABOR ONLY in SKU mode.** Multiplying locked materials by 0.88 under-prices ~12%; `tests/test_sku_pricing.py` asserts the exact basket math to catch a leak.
- Demo reclassifies to the labor side in SKU mode (it is a service); exterior siding + driveway and Foundation/Structural stay on engine lines (no HD-SKU basket). `line_items` key contract is preserved so `post_walkthrough._line_rows` renders unchanged; `RoomEstimate`/`RehabEstimate` gained a trailing `materials_source` field and estimates stamp `locked_sku 37914 pulled <date>`.
- **Consumers needed zero changes** (post_walkthrough Repair Numbers, comp_package scenarios, deal_analyzer, main.py rehab). Knox totals SHIFTED on purpose: real SKUs raise the too-cheap tier 1 (~+17% grand) and trim the padded tier 3 (~-18%); non-Knox and tier-4 outputs are regression-tested byte-identical. `use_locked_materials=False` opts out.
- **rehab-estimator.skill + deal-analyzer.plugin** now ship `data/master_material_list_37914.csv` with the doctrine: locked list is the material source for the vast majority of items on Knox deals (off-list only for an outstanding random issue, flagged), cheat sheet keeps labor + non-Knox markets, never multiply locked material prices. The skill's `material_specs` JSON contract is now wired (the Material Specs sheet renderer always existed but was never fed). deal-analyzer's bundled `skills/rehab-estimator/` was EMPTY despite instructing Claude to read 5 files from it; it now carries the full 8-file skill. Both zips rebuilt with forward-slash entry names (Compress-Archive backslash paths are non-portable).
- Re-lock cadence: re-pull before each project cycle if desired, but re-lock (an explicit, dated, git-diffable act) only when the PM re-approves the list.

## FTM Foreclosure: multi-pass skip-trace + screenshot-MMS (2026-06; orchestrated from `_api`)

The FTM foreclosure pipeline (consolidate -> single-family filter -> wizard upload -> phone scoring -> cadence) is orchestrated by `_api/ftm_pipeline.py`; these SiftStack scripts are its skip-trace + texting building blocks. Deep detail: the `_api` CLAUDE.md + the `reisift-tagging-and-phone-scoring` / `smrtphone-mms-screenshot-texting` memories.

- **`src/tracerfy_ftm.py`** — Tracerfy re-skip for FTM records (2nd phone source after the free DataSift enrichment). `--all` traces EVERY record (not just no-phone); `--finish` merges found phones into reisift via Add-Data upsert by ADDRESS into the existing "Foreclosure" list. ~$0.02/record.
- **`src/enformion_ftm.py`** — Enformion/Endato 3rd skip-trace pass. Reuses `enformion_heir.person_search` but for the LIVING OWNER (name + property-address anchor; name alone is HTTP-400'd) -> `enf_phones` -> populate `NoticeData.PHONE_FIELDS` -> same merge path. **`clean_owner_name(raw)`** cuts messy co-owner notice strings (AND/&/AKA/C-O markers, Jr/Sr/II-IV suffixes, middle initials, punctuation) to ONE clean (First,Last) so they don't 400. `--addr "<substr,...>"` re-runs specific records; `--finish` merges. **reisift MERGES phones, so Tracerfy + Enformion ACCUMULATE** — run sequentially, then re-score (`_api/score_ftm_phones.py --commit`) + re-tag (`src/run_phone_tag_upload.py --finish`). Live 2026-06-25: 109 -> 302 phones across 33 records, 32/33 with a Dial 1/2. CWD: run `run_phone_tag_upload.py` from the SiftStack root (relative `output/` path).
- **`src/mms_sender.py`** — GATED browser sender for the foreclosure screenshot-MMS (texts each homeowner the auction-notice Dropbox image + a personal message). Built + validated, **PAUSED pre-send (needs Ty's explicit GO).** Drives the **SmrtPhone web app** (SmrtPhone's API can't do MMS): a 2-step send — the TEXT via the new-message "Compose Message" modal, then the IMAGE via the conversation reply box, which lives in the **`main-iframe`** (`page.frame(name="main-iframe")` -> set the screenshot on its hidden `input[type=file]` -> click the send arrow by `bounding_box()` screen position). Reuses `datasift_core` Playwright primitives. Session captured to `smrtphone_state.json` by `_api/smrtphone_login.py`. Recipients/compose/schedule live in `_api` (`build_mms_recipients.py` pulls from the "FTM - 02 Ready to Call" preset). Full mechanism: the `smrtphone-mms-screenshot-texting` memory.

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode`: "daily" or "historical"
- `counties` / `types`: arrays to filter saved searches (empty = all)
- `tn_username`, `tn_password`, `captcha_api_key`: secrets (required)
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## NJ Modal Cloud Pipeline (build 1.0.30+)

The NJ stack runs as a Modal app (`modal_app.py`). Local-CLI commands above (`nj-scrape`, `nj-sheriff`, `nj-probate`) are dev/manual paths; production is Modal.

**Schedule**: `nj_weekly_all` cron = `0 10 * * 3` (Wednesdays 10:00 UTC ≈ 5/6am ET). Slack summary fires at end of each run.

**Parallel scraper isolation (`_safe` wrapper)**: All scrapers run via `asyncio.gather` and wrapped in `_safe(coro, label)` that returns `(label, notices, error)`. `CloudflareBlockError` (raised by Bluestone-based scrapers when IPs get challenged) is caught and surfaced as `error="cloudflare_block"` — one scraper failing does not abort the others. Records from failed scrapers are NOT marked as seen in the dedup tracker, so they re-enter on the next successful run.

**RAW CSV persistence**: The combined pre-enrichment scrape is written to `/tracking/raw/{date_folder}/raw_combined_{ts}.csv` on the `siftstack-tracking` Modal volume **before** enrichment runs. If enrichment crashes, the raw rows are recoverable. `ts`/`date_folder`/`volume_out_dir` are hoisted to the top of the post-scrape block specifically so the RAW write happens before any enrichment can fail.

**Sheriff stale auctions**: `SHERIFF_SALE_MAX_AGE_DAYS` (default 90) drops sales whose date is older than today − N days. Both `nj_sheriff_sales.py` and `nj_somerset_sheriff.py` honor it and export a `LAST_STALE_DROPPED` count surfaced in Slack as `N stale auctions filtered`.

**Sheriff disposition auto-drop**: After CivilView detail enrichment, records whose `case_disposition` ∈ {Sold, Redeemed, Cancelled} are dropped from the export (`_DROP_DISPOSITIONS` in `nj_sheriff_detail.py`). "Open" / "Bankruptcy" / "" pass through. Disposition is derived from lowercased `current_status` via `_CASE_DISPOSITION_RULES` (`scheduled→Open`, `purchased/sold→Sold`, `redeemed→Redeemed`, `bankruptcy→Bankruptcy`, `cancelled/canceled→Cancelled`).

**Block/lot address flag**: Records arriving with a block/lot description instead of a street address are flagged `needs_manual_address="yes"` at Step 2b of enrichment (`enrichment_pipeline._flag_block_lot_addresses`). The vacant-land filter keeps them; Smarty skips them. This step was primarily exercised by the (now-removed) NJ tax-sale scraper; it remains in the pipeline as a no-op for current sources and a safety net for any future block/lot-only intake.

**Dedup tracker** (`tracking/processed_ids_modal.json` on the Modal volume): stores `{record_id: ISO-timestamp}` per source in `_SOURCES = ("njlp", "probate", "somerset_probate", "somerset", "civilview_sheriff", "probate_runner")`. Only ID + timestamp is stored — **lost records cannot be reconstructed from the tracker**; recovery requires a re-scrape with dedup bypassed.

**SIFT_COLUMNS = 91** (in `data_formatter.py`). Sheriff detail fields are appended at the end after `run_id` — existing column order is preserved. Sheriff-sale priority tiers (adjournments_remaining, days_until_auction, priority_tier) come last, stamped by `nj_sheriff_sales.apply_priority_tiers()` after detail enrichment based purely on adjournments + auction proximity (judgment amount alone isn't a real equity signal, so it stays as a raw column only).

### Recovery Scripts

When a scraper fails or its IP is blocked, run these locally on a residential connection:

```bash
python scripts/nj_probate_local_backfill.py             # mirrors Bluestone probate when Cloudflare challenges
```

Triggers a fresh scrape, bypasses the dedup tracker, writes the rich-schema CSV. Upload that CSV through the normal DataSift pipeline.

### Modal-CLI Gotcha

Modal's argparse-based CLI does NOT accept PEP-604 union types on entrypoint signatures (`list[str] | None` → ValidationError). Use comma-separated `str = "Middlesex,Essex,Somerset,Union"` and split internally.

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at Knox/Blount county terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes.

### Notice Types (7 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — existing from web scraper
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals)

Courthouse probate records have decedent name + PR/executor name but NO property address. Multi-tier lookup fills the gap:

**Property Address Lookup** (Step 3c in enrichment pipeline):
1. **Tier 1: Knox Tax API name search** — search `/parcels/{decedent_name}`, score by token overlap (FIRST MIDDLE LAST → LAST FIRST MIDDLE), accept >= 0.4 match. Tries multiple name variations (with/without suffix, LAST FIRST format, first+last only).
2. **Tier 2: Executor family search** — search Knox Tax API by executor name, look for properties where decedent's last name appears in owner field (family property transferred to executor).
3. **Tier 3: People search** — search TruePeopleSearch/FastPeopleSearch for decedent's last known Knox County address.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (Knox Tax API → People Search → Tracerfy)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice **publication** date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches
- Anchors on `date_published` (the legal publication date), falling back to `date_added` — NOT `date_added` alone, which is now the run date (see "Date Semantics" under Output)

### Deep Prospecting v5 — SmartSkip heir engine (build 1.0.36, 2026-07-29)

**The heir engine is now SmartSkip, not Enformion.** v4 resolved relatives through the Enformion/Endato Person Search; v5 retires it after a live head-to-head on Knox/Blount records. Enformion **BusinessV2 is retained for entity owners only** (see `src/enformion_business.py`) because nothing else can resolve an LLC/trust.

**The measured case for the swap (12 owners, same records, both sources):**
- **Coverage:** Enformion returned ZERO relatives on **6 of 12** owners; SmartSkip returned relatives on 12/12.
- **Phones:** Enformion's `relativesSummary` carries names but **no phone numbers** — every relative you want to call is another $0.10 search. SmartSkip returns relatives AND their phones in one batch row.
- **Cost:** 100 owners / 682 relatives = **$15.90** (SmartSkip $15.00 + Tracerfy $0.90) vs **$78.20** the Enformion way. **4.9x.**
- **Precision:** on the validation record SmartSkip returned exactly 3 relatives and **all 3 appeared in the published obituary**; Enformion returned a capped 50-name blob plus out-of-state numbers that looked like wrong-person bleed.

**The v5 stack:** SmartSkip ($0.15/hit, relatives + phones) -> Tracerfy ($0.02, gap-fill only for relatives SmartSkip named but left phoneless, ~7%) -> **obituary/web research (mandatory, free)** for date of death + true relationships -> TrestleIQ ($0.015/number) for dial tiers. One record end to end is **~$0.24**. Skill: `Skills for REI/improved/deep-prospecting-v5.skill`; runner `scripts/smartskip_trace.py`; API contract `references/smartskip-api.md` + the `reference_smartskip_api` memory.

**v5 gotchas (all verified live, they are why the research layer stayed):**
- **SmartSkip is WRONG about death.** It returned `Deceased=false` for a man who died 12/06/2025 with a published funeral-home obituary, and it has **no DOD column at all**. Death data comes from the obituary/web pass, always.
- **THE SPOUSE-OBITUARY TRAP (highest-value check in the skill).** An obituary on the record does NOT mean the OWNER died. Live case (2026-07-29, details in the private `project_smartskip_spouse_obituary_trap` memory): a Blount County record sat on the Obituary list in Deep Prospecting status. The obituary was the **owner's husband's**, not hers; the owner was alive and owned the property. It was never an heir case, it was a living senior widow to call gently. An un-researched caller would have asked a recent widow for her dead husband. **Always match the decedent name against the owner of record before treating a record as an heir case.**
- **Relationship labels are coarse.** The column is literally "Possible Type"; **63% came back generic** ("Relative"/"In-Law") on a 100-record batch, and it labeled a 62-year husband a plain "Relative." The obituary overwrites it.
- **The wallet does NOT pay for bulk skip** — it bills the saved Stripe card via `payment-intent`. $25 sat untouched in the wallet while a batch charged the card.
- **Unpaid orders are invisible** in `GET /bulk-skip`; persist the `bulkSkipId` before paying.
- **Entities can't be name-traced** (SmartSkip needs First+Last, Tracerfy is consumer-only). **35 of 321** vacant owners were LLCs/trusts -> route to BusinessV2, filter them out of the batch up front.
- **The owner rule wins on a shared line:** a household number the owner also holds carries source + tier only, never a relationship tag, or the dial sheet labels the owner's own landline "Husband."
- **The 3-year DOD sanity check still anchors on `date_published`.** A stale 2004 index date surfaced during validation.

**Retired (kept only as a v4 reference):** `src/enformion_heir.py` / `scripts/enformion_person_search.py`. Failure modes for the record: zero relatives half the time, no phones on the graph, ~50-relative cap that silently truncates, a surname gate that drops married-out daughters, `isDeceased` flags that lag reality, and wrong-person matches when anchored on city/ZIP instead of the full street line.

---

### Legacy: Deceased-Owner Heir Resolution — Enformion (v4, superseded by v5 above)

The default obituary path extracts survivors/heirs from obituary text with an LLM, which can hallucinate an entire heir map (see `project_obituary_heir_hallucination` memory). The **v4 Primary Path** of the `deep-prospecting` skill replaced this with the Enformion/Endato relatives graph — grounded, nothing inferred. **v5 supersedes this**: SmartSkip now supplies the grounded relative list, so the LLM never invents an heir set, and the obituary layer only confirms relationships and supplies the DOD.

- **Module:** `src/enformion_heir.py` — reusable client: `person_search()`, `relatives_to_survivors()`, `required_signers()` (cost gate: living closest-kin `relativeLevel == "ab"` + decedent surname + DOB), `dedupe_phones()`, and `resolve_heirs_enformion(notice, parsed)` which returns `(ranked_dms, error_info)` shaped exactly like `build_heir_map()` so the rest of the pipeline is unchanged. Heir signing authority reuses `obituary_enricher.rank_decision_makers` (TN intestacy).
- **Pipeline (Step A only, 1 call/record):** `python src/main.py daily --deep-heirs`. In `obituary_enricher` Phase B, a new **Path E** runs Enformion FIRST for confirmed-deceased owners that no cheaper high-confidence path resolved (surviving co-owner on title, court-named executor). Falls through to the obituary-survivor waterfall on a miss or when creds are absent. Default (no flag, and the Apify daily Actor) keeps the old behavior — Enformion is never auto-billed.
- **Full waterfall (one record):** `python src/run_deep_prospect.py --first X --last Y --street "..." --city Knoxville --state TN --zip 37917` runs Steps A-E (decedent → required signers → per-signer search → phone dedupe → Trestle scoring) and prints a master dial sheet. Consolidates the one-off `run_brice_*` scripts.
- **Creds:** `ENFORMION_AP_NAME` / `ENFORMION_AP_PASSWORD` in `.env` + `config.py`. Billed per match ($0.10/search on the DataSift/affiliate rate the community gets; ~$0.35 public rack); misses are free. Detect API failure by HTTP status, NOT the always-present `error` object.
- **DOD conflict:** Enformion's death-index DOD can disagree with the obituary DOD (often a second household death). Surfaced via a `dod_conflict` flag in `missing_data_flags`; never silently resolved.
- **Live-run gotchas (build 1.0.32, from the 7619 Trey Oaks / James G. Key run):**
  - **Anchor with the full street line on common names.** A name + city/ZIP search returned the WRONG person as `persons[0]` (an Alabama "James B Key"); only `Addresses:[{"AddressLine1":"7619 Trey Oaks Ln","AddressLine2":"Knoxville, TN 37918"}]` pinned the exact record. `enformion_heir.person_search()` currently sends only `AddressLine2` (city/ST/ZIP), so on a common name pass the street line and confirm the match via address history + a cross-referenced relative before trusting `first_match`.
  - **`relativesSummary[].isDeceased` lags and is unreliable** — it showed the decedent, his late wife, and both long-deceased sons as "living." Trust the obituary + the person-level `dod` (the person index had a son's 2014 DOD even though the relatives-summary flag said living).
  - **The relatives graph is capped (~50) and misses married-out daughters** (different surname). Worse, `enformion_heir.required_signers()` gates on a surname match, so it DROPS married-out daughters who are required signers; the skill's shipped `scripts/enformion_person_search.py` correctly gates on `relativeType` (Son/Daughter/Child) and catches them. Always reconcile the signer set against the published obituary's survivor list, not the graph alone.
- **L3 fallback fetcher (Scrapfly ASP, build 1.0.32+):** `src/scrapfly_browser.py` (`ScrapflyBrowserClient.fetch(url)`, plus a `python src/scrapfly_browser.py <url>` CLI) clears Cloudflare/JS walls on county-record + genealogy pages (assessor & deed datalets, FindAGrave, Legacy, court info pages) that plain fetches and sandboxed agent WebFetch fail on. Reuses the `asp=True, render_js=True` core of `scrapfly_client.py` but is URL-generic. `run_deep_prospect.py --fallback-urls "<deed>,<obit>,<docket>"` pulls them inline in the same heir waterfall. **Sweet spot = county/records/genealogy portals** (e.g. recovered deed instrument + joint-owner names when the assessor datalet was blocking plain fetch). **Limits:** hardened people-search aggregators (TruePeopleSearch/FastPeopleSearch) frequently IP-ban ASP (`SHIELD_PROTECTION_FAILED`), and records a county doesn't publish online (Knox TN estate/probate cases, ROD deed images behind a paid subscription) can't be fetched at all (phone/in-person). Residential proxy via `SCRAPFLY_PROXY_POOL` (default `public_residential_pool`). The distributed skill ships a self-contained `scripts/scrapfly_fetch.py` (requests-only, no repo/SDK) for community users.
- **Deliverable = PDF (build 1.0.32+):** deep-prospecting research packs render to a branded PDF via `python src/deep_prospect_pdf.py <pack>.md` (reportlab; no new deps) so they upload cleanly into DataSift/Sift as a record attachment. The renderer keeps the heir map + master dial sheet monospaced and strips em/en dashes + non-WinAnsi glyphs to ASCII.

### Dropbox Folder Structure
```
{DROPBOX_ROOT_FOLDER}/
├── Knox/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
└── Blount/
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "TN Public Notice")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is **no REST API** — upload is via Playwright browser automation of the web UI.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

**apiv2 JWT (shared with the Deal Room project):** any script hitting `apiv2.reisift.io` reads the shared auth store at `Deal Room Coaching Call/_api/clients/config/reisift_auth.json` (`datasift-admin` = staff ty+1, ~48h access token; NEVER hardcode a Bearer in SiftStack). Refresh: app.reisift.io DevTools -> Copy as cURL -> `python _api/clients/reisift_auth.py add datasift-admin <jwt>` (run with `PYTHONIOENCODING=utf-8`; the checkmark-glyph crash after "saved account" is cosmetic, the save succeeded). Then re-impersonate before client-account calls. Last refresh 2026-07-21, exp 2026-07-23 19:41 UTC.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (42 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (42 columns)
- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (for niche sequential), Notes (contextual per notice type)
- **Built-in fields (13):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (16):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL, Notice Screenshot

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name (`foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Violation", `divorce` → "Divorce"). DataSift auto-creates lists from CSV.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

### Upload Wizard (5 Steps)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 4
- Notes and MSL Status sometimes auto-map
- Custom fields (TN Public Notice group) require drag-and-drop mapping

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phone numbers (up to 5 per owner) + emails via unlimited plan ($97/mo). Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both are ON by default when `--upload-datasift` is set.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
python src/main.py daily --deep-heirs               # resolve deceased-owner heirs via Enformion ($0.10/match DataSift rate, ~$0.35 rack)
```

### Environment Variables
- `DATASIFT_EMAIL` — DataSift login email
- `DATASIFT_PASSWORD` — DataSift login password
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 21 presets have Property Status "Do not include" → "Sold" (build 1.0.23)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by city (NOT county): Knox → "Knoxville, TN", Blount → "Maryville, TN"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data. Knox County has 48 ZIPs (3 pages) and 120+ neighborhoods (7 pages).
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

```bash
# Extract all Market Finder data for a county
python src/extract_market_finder.py --state "Tennessee" --county "Knox" -v
python src/extract_market_finder.py --state "Tennessee" --county "Knox,Blount" --headless

# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

## REI Skill Library (18 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py`. API-first comp acquisition (Zillow /search per comp-package) with manual browsing fallback + bedroom-band dual-track rule (2026-07) |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison. Phase 3 now routes comp acquisition API-first (comp-package contract) with the bedroom-band rule (2026-07) |
| 7 | `deep-prospecting-v5.skill` | Deal Analysis | v5 | **SmartSkip heir engine** (relatives + phones in one batch call) + mandatory obituary/web research for DOD and true relationships + Tracerfy gap-fill + Trestle tiers. ~$0.24/record, 4.9x cheaper than the retired Enformion person path. Ships the spouse-obituary trap, the unreliable-deceased-flag gotcha, and the owner-rule-on-shared-lines rule. Enformion BusinessV2 kept for entity owners only |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (Tax API→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |
| 14 | `text-touch-builder.skill` | Operations | 2026-08 | Four-text-touch pre-call SMS sequence per ready-to-call record (identity check, drip, soft ask, breakup) with cold-email style copy rotation; CSV export -> stdlib script -> Add-Data re-import into Text Touch 1-4 custom fields. **Human-voice gate added 2026-08:** `AI_TELLS` refuses (not warns) any message or pool variant containing an em/en dash, a semicolon, a link, emoji, ALL CAPS, stacked exclamations, form-letter openers, or AI vocabulary; `--check-pools` audits the variants and runs on every invocation. Same list mirrored in `src/sms_agent/respond.py` so outbound touches and inbound replies sound like one person. Community-safe (no internal API) |
| 15 | `cold-call-coach.skill` | Operations | new | Pull SmrtPhone cold-call recordings, audio-model transcription with real tonality notes, grade vs the cold-calling rubric (measured reliability +/-3 pts, calibration examples, short calls on their own scale, JSON score footers), Excel workbook export. Self-contained scripts, config-driven roster |
| 16 | `lead-manager-coach.skill` | Operations | new | Same engine, lead-management rubric: 4 pillars qualification, roadblocks, no-ladder, next-action discipline. Call quality only (no CRM hygiene scoring) |
| 17 | `closer-coach.skill` | Operations | new | Same engine, closer rubric: money conversation, three-option offer stack, objection frameworks, commitment locking, negotiation timeline reports |
| 18 | `kpi-engine.skill` | Operations | new | Universal DataSift KPI reporting from the user's own account: activity-log pull (self-contained stdlib script, own JWT, no internal API), three distinct rates, lead counting incl new_lead statuses, funnel pacing (dials->correct->leads->appts->contracts), record-level detail mode, md/CSV/Excel/Slack outputs. Benchmarks shipped as tune-per-operation baselines; internal production version lives in Deal Room `_api/kpi-engine/` |
| 19 | `comp-package.skill` | Deal Analysis | new | Boundary-filtered comp package: /search API pull with 41-row-cap band partitioning, condition bucketing by price/Zestimate ratio, dual-track ARV (same-bed base + labeled reconfig upside), 3-scenario rehab, MAO math, buyer targeting, Excel deliverable spec. Community-safe (own OPENWEBNINJA_API_KEY, requests-only script) |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```

## My Defaults

- **Primary counties:** Essex, Middlesex, Somerset, and Union (New Jersey)
- **Daily summaries:** Send to Slack via `SLACK_WEBHOOK_URL`
- **Data source:** NJLisPendens — weekly CSV/XLSX file drops, not a scrapable website. Use `csv-import` as the primary data path (no Playwright scraping for this source)
- **CRM:** DataSift (same upload/enrich/skip-trace pipeline as TN data)
- **Production run:** Modal `nj_weekly_all`, Wednesdays 10:00 UTC. Slack summary auto-fires on completion. Manual re-trigger: `modal run modal_app.py::nj_weekly_all`.
- **When a scraper fails on Modal:** Don't refactor. Run the matching local recovery script (`scripts/nj_probate_local_backfill.py`) from a residential connection, then upload the rich-schema CSV through the normal DataSift pipeline.
