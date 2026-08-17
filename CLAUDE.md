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

Currently focused on Knox and Blount counties, Tennessee. A realtor sphere-of-influence beta runs on the Columbus OH metro (the `soi_*` modules; see "Sphere of Influence Pipeline").

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

## Scheduled First-to-Market Pull (build 1.0.42, 2026-08-14)

The TN Public Notice scrape now runs unattended in the cloud instead of on a workstation, and covers **probate as well as foreclosure** for Knox and Blount. Entry points: `src/ftm_runner.py` (one run) and `src/ftm_schedule.py` (the long-lived scheduler, the container's CMD). Full runbook: `deploy/FTM_RUNBOOK.md`.

```bash
python src/ftm_runner.py --doctor              # credentials, egress, state dir, searches
python src/main.py list-searches               # dump the LIVE saved-search dropdown labels
python src/ftm_runner.py --max-notices 2       # bounded dry run, writes nothing
python src/ftm_runner.py --commit              # the real thing
python src/ftm_schedule.py --next              # next 5 fire times, business-local
```

**THE SCRAPE IS GATED ON EGRESS, NOT ON CODE. This is the finding that reframes everything else.** tnpublicnotice.com decides per-IP whether it will serve notice detail pages at all. Verified live 2026-08-14 against the same logged-in account: from the office IP the page carries **no CAPTCHA whatsoever**, just "You are not permitted to view public notices from this computer at this time"; through an Apify datacenter proxy the same notice serves the normal Turnstile gate and the text; through Scrapfly residential it serves with no gate at all. A Fly machine is a datacenter IP by definition, so `proxy_resolver.py` is mandatory infrastructure, not an optimization. Resolution order: `SIFTSTACK_PROXY_URL` -> `APIFY_PROXY_GROUPS` + `APIFY_TOKEN` (the API token is NOT the proxy password; it is used to look the password up) -> direct. Apify's RESIDENTIAL group is **not on the current plan** (`availableCount: 0`); `BUYPROXIES94952` (27 US datacenter IPs) clears the block today. A run blocked this way exits **3**, distinct from a normal failure, because no retry fixes it. The CLI path previously had no proxy support at all while the Apify Actor did, which is exactly why the scrape worked in the cloud and died on a workstation.

**The gate is Cloudflare TURNSTILE, and the old solver never solved it.** `config.py` had recorded the 2026-07-13 migration but `captcha_solver.py` still called `solver.recaptcha()` and injected into `g-recaptcha-response`, a field the page no longer reads: every solve was billed and discarded. It now selects method and response field off `CAPTCHA_KIND`, reads the sitekey off the **live page** (a rotation logs `SITEKEY ROTATED` rather than silently killing the scrape), creates the `cf-turnstile-response` input when the headless widget never renders one, and runs the blocking 2Captcha call in a thread so the browser event loop keeps servicing the page. Verified live: gate cleared, notice text visible. **The gate is session-level, so one solve covers the rest of the run.** A blocked IP now raises `NoticeAccessBlocked` and aborts the whole run instead of grinding 50 results x 3 attempts against a wall.

**Zero notices is a FAILURE.** Success requires positively seeing the notice body; there is deliberately no "the challenge markup is gone, so we must have passed" inference, which is precisely the reasoning that reported 13 consecutive dead runs as successful over 19 days. `ftm_runner` reports a 0-notice run as EMPTY and exits non-zero.

**Probate (`Probate V2 Knox` / `Probate V2 Blount`, names verified live).** `main.py list-searches` dumps the real dropdown labels and flags configured-but-missing entries, because a mistyped saved search scrapes nothing and looks exactly like a quiet day. Three parsing bugs found by running real notices through the pipeline, all now regression-tested:
- **The PR was the court.** "Notice to Creditors" names the role in prose ("issued to the referenced Personal Representative by the Chancery Court") before naming the human under a standalone `PERSONAL REPRESENTATIVE(S)` heading, so a same-line pattern set `owner_name` to "By The Chancery Court". Patterns are now tried block-form first, a rejected candidate falls through instead of ending the search, and court/clerk prose is in `_INVALID_NAMES`.
- **The courthouse became the subject property.** A probate body's only street addresses belong to the court, the attorney, or the PR, so `_parse_address` now returns immediately for probate (it was uploading "400 W. Main Street", the Knox County courthouse). The real property is resolved downstream by `property_lookup` (Knox Tax API by decedent name -> executor family search -> people search), which works: a live run resolved 4100 Landon Dr from decedent "Doris O. Young".
- **The vacant-land filter deleted the entire type.** It judges by house number and probate has no address yet, so on a mixed run every probate record vanished while the foreclosures came through and the run looked healthy. `NO_ADDRESS_TYPES = {"probate", "divorce"}` is exempt per-record, so the filter keeps doing its real job on types that do carry an address.

**Two more fixes with reach beyond probate:** `max_notices` is now enforced **within** a results page (a cap of 1 still ground through all 50 results, paying a gate solve and screenshot for each, which matters because it is the cloud run's cost ceiling); and `_clean_and_split_name` no longer folds a spelled-out middle name into the surname ("Eric Lee Sharp" uploaded as last name "Lee Sharp", breaking record matching and skip trace), with a surname-particle list so "Van Buren" and "De La Cruz" stay whole.

**Deployment: LIVE on Fly as `siftstack-ftm` (deployed 2026-08-14).** A separate app from the SMS agent's `siftstack` because the shapes are opposite: the SMS agent is a web service that must never stop, this is a 10-40 minute batch job idle the rest of the day, and sharing a machine would put a long scrape in contention with webhook handling. Four deliberate choices: the **Playwright base image is pinned to the client version** (the site is ASP.NET postbacks behind a JS gate, so there is no HTTP-only path); a **volume at `/data`** holds `seen_ids.json`, `last_run.json`, `cookies.json` and `ftm_runs.jsonl`, because losing the seen-ID cache means re-scraping and re-paying for months of notices; a **scheduler process rather than `fly machine run --schedule`**, since Fly's schedules are coarse and pick their own minute while a first-to-market pull wants a specific business-local hour; and **`FTM_ARGS` ships without `--commit`** so the first scheduled run does everything except write. `deploy/sync_ftm_secrets.py` pushes credentials from `.env` in one staged call, masked by default.

**THE 407 THAT LOOKED LIKE BAD CREDENTIALS.** The first Fly deploy could not reach the site at all: 60s timeouts, then `407 Proxy Authentication Required` from Apify on a token and password that were byte-identical to the working local ones. The cause is that **Apify session ids accept only letters, digits, `_` and `.`** and `fly.ftm.toml` set `APIFY_PROXY_SESSION = 'tnpn-fly'`. Proven live from the machine on one credential set: `session-tnpn-fly` 407s while `tnpn_fly`, `tnpnfly`, `tnpn.fly` and `tnpn` all return 200. A hyphen in a session name is indistinguishable from a rejected password in the error, so `proxy_resolver._safe_session()` now rewrites illegal characters and logs the substitution. **The VM is `shared-cpu-2x` / 2GB, not 1x/1gb**: on one shared core Chromium took 60 seconds just to launch and the backfill would have run roughly twice as long as it needs to.

**Backfill: the 1000-row cap is why 12 months was never reachable.** The site truncates EVERY result set at 20 pages / 1000 rows, newest first, so a plain 12-month search silently loses its tail: Knox foreclosure and Knox probate both sat on that ceiling showing only their most recent weeks. The site itself retains 12 months and no more ("Notices for the past 12 months are available in the current search"), so 12 is both the target and the maximum. `--backfill-months N` re-submits each saved search once per calendar month over an explicit date range (`rbRange` + `txtDateFrom`/`txtDateTo`, set after selecting the saved search so its keywords and county checkboxes stay intact), and `--backfill-offset M` shifts that window so one 19-hour job becomes twelve resumable ones. Measured monthly volume: Knox probate ~150, Knox foreclosure ~100, Blount probate ~80, Blount foreclosure ~33, about **4,350 raw notices over 12 months**. Resuming is cheap because the seen-ID check now reads the notice id out of the RESULTS GRID and skips before opening the page (was ~5s per already-seen notice, now zero).

**Blount probate was returning nothing, for two stacked silent reasons.** `property_lookup._tpad_lookup` hit TPAD with bare `requests`: TPAD **403s anything without a browser User-Agent**, so every Blount lookup failed outright. Even fixed, the HTML page only ships an EMPTY table shell whose id is `searchResultsTable`, not the `resultsTable` the parser searched for; the rows come from `POST /TPAD/Search/GetSearchResults`, which returns clean JSON. Both failures were quiet (lookup returns [], address stays empty, validation later drops the record as "missing address"), so a Blount probate backfill would have produced ~950 records and zero usable ones. Also: **TPAD prints the house number LAST** ("LAKESHORE DR  5705"), which fails validation and Smarty unless normalized to "5705 Lakeshore Dr". A live test slice went from 0 usable to 3 of 5.

**Trustee sales leak into the probate saved search.** Its keyword is "probate", which also appears in foreclosure notices, so a successor-trustee sale surfaces in probate results and uploads to the Probate list with the trustee's law firm as the personal representative (seen live: a Marinosci Law Group notice produced "PR = From Felicia F. Coalson"). `foreclosure_filter.looks_like_trustee_sale()` drops those, and requires the ABSENCE of a genuine probate anchor (notice to creditors, letters testamentary, personal representative) so a real estate filing that merely mentions a trustee still passes.

**Probate notices never carry a phone number.** Measured on 10 Knox notices: 8 had no phone at all and the 2 that did carried the LAW FIRM's ("The Ebbert Law Firm ... Telephone (865) 234-2488", a successor trustee's office line). The PR is published with a mailing address only, so every probate phone must come from skip trace against the PR name and address; a number lifted from the notice body dials the estate's attorney.

**Notice screenshots retired 2026-08-14** (Ty: not used for anything from TN Public Notice any more). Capture, Drive/Dropbox/KVS hosting and the CSV re-write are removed from the live path and the tooling is in `archive/notice_screenshots/`. It was also the slowest step in a foreclosure notice, which matters directly on a multi-thousand-notice backfill. The `notice_screenshot_path` / `notice_screenshot_url` fields, the CSV column and the DataSift custom-field mapping are deliberately KEPT so historical records retain their URLs.

**BACKFILL COMPLETE 2026-08-16: 1,226 records, 936 distinct properties, all 12 months, 48 of 48 jobs.** Probate 786 / Foreclosure 476; Knox 910 / Blount 352. The daily schedule went live with `--commit` the same day (06:30 America/New_York).

**The constraint that dictated the whole backfill shape: the site blocks an egress IP by VOLUME.** One month running all four searches through a single sticky Apify session viewed about **204 notices** before that IP began refusing, and every later month then failed in ~60s against the same dead IP. Two mechanisms fix it, and both are load-bearing: `ftm_runner` rotates to a fresh Apify session id per PROCESS (counter on the volume), and the backfill runs **one process per (saved search x month)**, 48 jobs, keeping the worst case (Knox probate ~150/month) under the threshold. Even so ~7% of jobs hit a burned IP; a retry pass that re-runs any non-zero exit with a fresh IP converged both stragglers within three passes. Do not "fix" a run of exit-3s by retrying immediately in a tight loop; the pool is 27 addresses and they need to cool.

**seen_ids is persisted ONLY after a successful upload.** The scrape no longer writes it incrementally. Ordering is the whole point: the upload happens after the entire scrape, so persisting mid-scrape meant an aborted run left notices flagged as handled that were never sent anywhere. The first egress block did exactly that to **204 notices**, and a retry would have skipped every one of them permanently. `_revert_seen()` rolls back on failure, on `--no-upload`, and on any dry run.

**Verified in production, not just in tests:** across the 675 post-fix rows checked mid-run there were 0 junk owner names, 0 courthouse addresses, 0 trustee sales on the Probate list, and 0 duplicate rows within a job, while legitimate multi-token surnames survived intact (St. John, St. Leger, Van Zandt, Van Gentry, VAN DAVIS). Repeats ACROSS months are expected and harmless: a foreclosure republished over a month boundary appears in both files and `POST /property/` upserts by address (confirmed by re-POSTing and getting the same uuid back).

**Still workstation-only:** the `county` stage (`knox_ftm_pull.py`) skips in the container with the reason stated in the run summary, because its buy-box enrichment needs the SiftMap client from the Deal Room `_api` checkout. Vendoring it the way `sms_agent/crm_standalone.py` vendored the reisift client is the next step to get liens, condemnations, trustee deeds and evictions on the same schedule.

**Also fixed here:** `python src/main.py <mode>` used to dispatch into the Apify Actor whenever `APIFY_TOKEN` was present in `.env` (it is, for `consolidate_foreclosures`), so every local CLI call died on "tn_username and tn_password are required". The Actor path now triggers only on `APIFY_IS_AT_HOME` (or an explicit `SIFTSTACK_FORCE_ACTOR=1`). `datasift_api_upload.env()` reads `os.environ` before falling back to a `.env` file, so the uploader works on a box that has no such file. State paths honor `SIFTSTACK_STATE_DIR` / `SIFTSTACK_OUTPUT_DIR` / `SIFTSTACK_LOG_DIR`. Saved-search selection and per-page postbacks wait on `domcontentloaded` rather than `networkidle`, which regularly never settles through a proxy and abandoned a working search after 30s.

## Scraping Backend: Scrapfly (build 1.0.31+)

The gated notice detail fetch (the "caps structure": residential proxy, anti-bot, reCAPTCHA, and the proof-of-source screenshot) can run through the **Scrapfly API** instead of the in-house Playwright + 2Captcha path. Selected by `SCRAPE_BACKEND` (defaults to `scrapfly` when `SCRAPFLY_KEY` is set, otherwise `playwright`).

- **`scrapfly_client.py`** provides `ScrapflyNoticeClient`. `login(session)` logs into Smart Search inside a Scrapfly session (forms-auth cookie + sticky residential IP), then `fetch_notice(id, session)` opens the detail page with `asp=True` + `render_js=True`, a JS scenario clicks "View Notice" (ASP solves the reCAPTCHA), and it returns rendered HTML + a full-page screenshot in one call. `fetch_notices(ids)` logs in once and yields a result per ID. Best-effort with retries; every call returns a `NoticeFetchResult`.
- **Scraper integration** (`scraper.py`): when `SCRAPE_BACKEND == "scrapfly"`, Playwright still drives login + saved-search navigation and supplies each notice ID, but the per-notice content + screenshot come from Scrapfly via `_scrapfly_notice()`. Any Scrapfly failure falls back to the 2Captcha path, so the swap is safe. Returned HTML is parsed by `notice_parser.parse_notice_html()` (shares field extraction with `parse_notice_page`).
- **Screenshots** come natively from Scrapfly (`screenshots={'notice': 'fullpage'}`), saved to `output/notices/` and hosted/linked exactly like the Playwright path.
- **Tooling:** `scrapfly_spike.py --id <id>` validates one notice (gate clears + screenshot) before relying on it. `backfill_screenshots.py [--csv ...]` logs in once and backfills screenshots for a master list (e.g. the output of `consolidate_foreclosures.py`), writing `notice_screenshot_path` / `notice_screenshot_url` back to the CSV.
- **Env:** `SCRAPFLY_KEY` (required), `SCRAPE_BACKEND`, `SCRAPFLY_COUNTRY` (default `us`), `SCRAPFLY_RENDER_WAIT_MS`, `SCRAPFLY_TIMEOUT_MS`, `SCRAPFLY_MAX_RETRIES`. Needs `scrapfly-sdk` (in requirements.txt).
- **Open validation:** whether Scrapfly's ASP clears this site's in-page reCAPTCHA "View Notice" gate is confirmed per-notice by the spike. A `gate_not_cleared` result means the JS scenario action schema or an explicit CAPTCHA step needs a tweak.
- **STATUS 2026-08-14: the route wired into `scraper.py` is the broken one.** `_scrapfly_notice()` calls `fetch_notice()` (a direct `Details.aspx?ID=` fetch), and the client's own `fetch_notice_via_search` docstring explains why that cannot work: every Scrapfly scrape gets a fresh ASP.NET cookieless session, so a detail fetch lands in a session that never ran a search and the server returns an unpopulated shell. Live result is `gate_not_cleared` on every notice, ~3 minutes each, before falling back to Playwright. `fetch_notice_via_search` (search + walk inside ONE call) **does** work: verified live returning real notice content with no gate at all from Scrapfly's residential IP. Until the saved-search equivalent of that in-session walk is built, **leave `SCRAPE_BACKEND=playwright`** (`.env` currently overrides it to `scrapfly`, which is what makes runs slow rather than wrong).

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

## Sphere of Influence Pipeline (Columbus OH beta, build 1.0.43, 2026-08-14)

Reverse-searches a realtor's exported Facebook/LinkedIn contacts (name + email ONLY, no addresses) into a Realtor-AI-scored priority list. Built for a Columbus OH realtor partner; the architecture is metro-agnostic. Chain: `soi_intake` (normalize/dedupe) -> `soi_county_pull` + `soi_owner_db` (free county owner rolls -> SQLite) -> `soi_owner_match` (name join) -> `soi_enformion` (paid resolve for misses) -> `soi_enrich` (SiftMap detail + `realtor_score`). First live run: 848 raw rows -> 728 unique people -> 222 confirmed metro homeowners -> 191 scored.

```bash
python src/soi_intake.py                                   # exports -> output/soi_contacts_normalized.csv/.json
python src/soi_county_pull.py                              # 4 ArcGIS counties -> output/soi/raw/*.jsonl
python src/soi_owner_db.py                                 # all 6 counties -> output/soi/owners.db (811K rows)
python src/soi_owner_match.py                              # name join -> output/soi_owner_matches.csv/.json
python src/soi_enformion.py                                # PAID (~$0.10/match) resolve of status=none
python src/soi_enrich.py                                   # SiftMap realtor_score on unique matches
python src/soi_enrich.py --matches output/soi_recovered_matches.json --out output/soi_enriched_recovered
```

**The whole Columbus metro is FREE data: 811,146 owner rows across six counties, $0.** Franklin (484K) from the open file server `apps.franklincountyauditor.com` - use `/Parcel_CSV/{yyyy}/{mm}/Parcel.csv` which carries NAME1/2/3 + MAILAD1-4 + values + TRANDT/PRICE; **the newer-looking `Outside_User_Files` Tab-Delimited appraisal extract has NO owner fields at all** (its Parcel.txt is values/situs only), and **the Parcel_CSV folder path is stale on purpose** (latest folder said 2025/07 but the file's Last-Modified was 3 days old - check the header, not the path). Fairfield (76K) from the nightly full CAMA dump `share.pivotpoint.us/oh/fairfield/cama/fairfieldaa407.zip` (iasWorld OWNDAT/PARDAT/APRVAL/DWELL, join on PARID, filter DEACTIVAT). Delaware/Licking/Pickaway/Union (250K) from open ArcGIS layers (endpoints + field maps in `soi_county_pull.py`; Licking serves 100K rows per call and inlines the last 3 transfers). The vendor SEARCH UIs (Schneider Beacon, DEVNET Pivot) are bot-walled and never needed. Ohio's statewide OGRIP parcel layer strips owner fields from the public view - counts and geometry only.

**Name-join mechanics that decide the hit rate** (`soi_owner_match.py`): deeds store "LAST FIRST M" with co-owners as "... & FIRST [LAST]"; LinkedIn last names carry credentials ("Weatherford, CRS"); Facebook's middle token is usually a MAIDEN name and is searched as an alternate surname; the nickname map is multi-target (Nikki -> Nicole/Nichole, Kathy -> Katherine/Kathleen); and a **household-pair boost** rescues spouses - if two roster contacts hit the same deed, both are lifted (Charlie Wlodyka scored 2.0 alone, confirmed by Jamie Wlodyka on the same Dublin parcel). Same-name collisions group by DISTINCT owner-name string: one person on 5 parcels is a portfolio signal, five different "JOHN SMITH" strings is ambiguity. `owner_occupied` = mailing addr-key == situs addr-key; **do not fall back to Franklin's OWNER_ADD1, it sometimes echoes the situs** and false-flagged a Texas absentee as owner-occupied.

**Enformion closes the gap, and EMAIL is the verifier.** Person Search accepts name + "Columbus, OH" city/state anchor (the name-alone 400 does not apply once a metro anchor is attached). The response's `emailAddresses` (a list of DICTS, `.emailAddress` inside) is matched against the contact's exported email - an exact hit grounds identity with no address needed; name-only OH matches are kept but flagged `name_metro`. Current address = `addresses[]` with `addressOrder == 1`, read `fullAddress` + `county`. Each resolved address is cross-checked back against owners.db: surname on the deed = `owns_here` (the roll join missed a name variation or trust - 37 of 349 on the live run), someone else = renter/other-titled (74), county outside the six = `out_of_metro` (106, cleans the sphere honestly). ~$24 total, misses free.

**`realtor_score` off SiftMap `get_detail` IS the Realtor AI score. Ty's rule: 95+ is a priority call.** Enrichment runs autocomplete -> get_detail per matched address and REQUIRES a token overlap between the county deed owner and SiftMap's `owner_info` before trusting the row (8 mismatches flagged, not trusted). The live distribution is steep - 191 scored: one 95+ (97), six 80s, seventeen 60-79 - so the 95+ bar isolates a real call list rather than a third of the sphere. Rows also carry equity, mortgage, portfolio count and both investor scores for an investor-referral cut. Renters are kept on their own track (future first-time buyers), RE-industry contacts (kw.com/mortgage/title domains, 27 flagged at intake) are referral partners, not homeowner sphere.

**Outputs:** `soi_contacts_normalized` -> `soi_owner_matches` -> `soi_enformion_resolved` -> `soi_enriched` / `soi_enriched_recovered` -> **`soi_priority_list.csv`** (merged, ranked by realtor_score). Enformion/SiftMap stages checkpoint to `soi_enrich_state.json` / `soi_enformion_state.json` and are resumable.

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

## The Lender Package (build 1.0.44, 2026-08)

An 8-piece set handed to a private money lender to fund ONE named property. The team hand-edited every template on 2026-08-16 and those edits are the spec; the originals live in `Lender Docs Templates-*.zip`.

```
1. Cover Letter                     lender_docs.py
2. The Private Lender Package.xlsx  lender_package.py
3. Promissory Note                  lender_docs.py
4. Personal Guarantee               lender_docs.py
5. Closing Instructions Letter      lender_docs.py
6. Insurance Request Letter         lender_docs.py
7. Investor Information Sheet       lender_docs.py
8. Satisfaction and Release Request lender_docs.py
```

```bash
python src/lender_package.py --spec deals/3014_sanland_lender.json
python src/lender_docs.py    --spec deals/3014_sanland_lender.json
```
Both write to `output/lender/<Deal_Name>/`, one folder per deal, numbered 1 through 8.

**THE FRONT END IS A BLOCK-FOR-BLOCK MIRROR OF THE REPAIR ESTIMATOR** (`Copy of The Repair Estimator`, Ty's Drive). Not "inspired by", mirrored: Property header, then `Property Values & Pricing | Holding Costs (Monthly)` with Annually and Monthly columns, then `Financing Costs | Buying Transaction Costs` and `Selling Transaction Costs` with Perc. Of Purch and Perc. Of ARV columns, then the `Estimated Net Profit and ROI Snapshot` band, then `Purchase and Deal Analysis | Lender Coverage and Return`. Six columns: label, percent, dollars on each side. Bold `Total X:` rows close every block. The single adaptation is the last right-hand block, which is the lender's coverage instead of our cash on cash, because this is their document. **Inputs live inline on that page**, never on a separate tab, for the same reason the Estimator does it: a blue cell next to the answer gets changed, a blue cell on another tab does not.

**Repair Costs is the Estimator's detail grid on its own tab** (Category / Include Y-N / Repair Type / Qty / Unit / Unit Cost / Total / Notes, banded EXTERIOR / INTERIOR / MECHANICALS / OTHER, 65 lines) and it rolls up into Estimated Repair Costs on the front page. **On a straight relist every line is switched to N and the total is zero, which is the answer rather than a missing tab.** Switch a line to Y and the budget, the loan, the LTV and every coverage ratio move with it.

**Selling costs are itemized, not a flat percentage.** Escrow, recording, realtor %, transfer %, warranty, staging, marketing, misc, exactly like the Estimator. That matters beyond cosmetics: `SellFixed` and `SellVarPct` are separate names so the band-floor case reprices commission against the LOWER sale price instead of carrying the ask's dollar figure down with it.

**THE COVER LETTER IS THE SPEC FOR THE WORKBOOK.** It tells the lender the package contains an overview of the deal, an overview of their contribution, a term sheet, the numbers on repairs and re-sell value, the comps, backups and risk, and next steps to fill out. So the tabs ARE that list, in that order, and nothing else: **Deal Overview, Your Investment, Term Sheet, Repair Costs, Resale Value, Comps, Backups and Risk, Next Steps** (repairs and resale being the two halves of one bullet). Do not add a tab without adding it to the cover letter first.

**Structure is lifted from The Repair Estimator** (`Copy of The Repair Estimator`, Ty's Drive), because that is the sheet the team actually trusts:
- **Inputs live INLINE on the summary page, not on their own tab.** The Estimator puts its blue cells right next to the results, which is why people actually change them. Build 1.0.41 had a separate Inputs tab and it was the thing Ty disliked.
- Banded full-width section headers, paired left and right blocks, dense rows.
- **A percent column beside every dollar column.** "$16,272" means nothing until it reads as 7% of ARV.
- Bold `TOTAL X` rows closing each block, and the detail page rolls UP into the summary.
- **The repair grid carries a Y/N per line.** Switch a category to N and `RehabTotal`, the loan, and every coverage ratio drop with it.

**Everything is a live Excel formula** off workbook defined names, including the sentences (`_say()` + `_t()` build `="..."&TEXT(Loan,"$#,##0")&"..."`, and the LTV paragraph is a live `IF(LTV>0.75,...)`). 240 formulas across the two live deals, verified by actually recalculating with the `formulas` pip package.

**Derived, never typed:** `DayOne = Loan - RehabTotal`. Typing the closing advance let financed closing costs land in the draw tranche, so the holdback disagreed with the repair budget ($88,800 against an $87,192 scope). Anything definitionally equal to other cells is a formula. The one deliberate exception is **`Loan`, which is a single blue input**: it is a negotiated number, not a derived one, and making it the only lever that sets the deal is what keeps the front page simple. `Borrower Cash` then falls out as `Purchase + Repairs + BuyCosts + HoldTotal - Loan`, deliberately excluding interest because that is paid from sale proceeds rather than at closing.

**Read the workbook back before regenerating over it.** Ty reviews in Excel and edits input cells directly. On the 158 review he made three changes and only mentioned one: realtor fees 6% to 5%, as-is raised to match the ask, and he deleted a comp. Diff the blue cells against the spec before overwriting, and rewrite any prose that cites a number or a comp he moved.

**Contract changes from the team's edits, all of which move numbers:**
- **The LOAN covers closing costs, document prep, recording and the lender's title policy**, repaid with interest and backed by the guarantee. It used to be borrower cash.
- **Every member of the company personally guarantees the note**, so `borrower.members` is a list and the guarantee plus closing letter render one signature block each.
- **Default is not a penalty rate and not a foreclosure lecture.** On default we liquidate immediately and the guarantee covers any shortfall including interest still owed. That framing replaced the old 15% default-rate language everywhere.
- **Minimum interest is quoted as a percent as well as months** ("3% guaranteed" at 12% over 3 months).
- `deal_type` picks the wholetail or flip branch in the cover letter; a non-zero repair budget picks builder's risk over vacant dwelling on the insurance letter. **Exactly one side of every OR gets written.**

**The templates carry a NOTES FOR CLAUDE block that must never reach a lender.** `build_all()` re-reads each rendered document and raises if the string survives, rather than trusting the code path. Same reflex as the partial-set guard: `main()` exits 1 if fewer than 7 documents write, and stale files from a previous numbering are deleted so a folder cannot grow a second copy of everything.

**No deed of trust template on purpose:** in TN the closing attorney draws it on their own form for the Register of Deeds and the title underwriter, so a downloaded form is a recording problem rather than a shortcut. Document 5 tells them exactly what to prepare instead.

**Voice** comes from `CMO Stack/context/voice-guide.md`. The note and the guarantee stay in formal legal register; the letters are in Ty's voice. Audit scans rendered formula output as well as static cells for em/en dashes, ~30 AI tell words, leaked notes and unresolved `XXXXXX` placeholders. Current state on both deals: zero.

**FORMATTING IS PART OF THE DELIVERABLE.** Nobody should drag a column or a row to read this workbook. `_polish()` runs over every sheet after the content is written: column widths come from the longest thing actually in each column, then wrapped rows get a height computed from the width they ended up with, plus landscape fit-to-width print setup. The trick that makes it work is **`_rendered_len()`, which measures what a cell will SHOW rather than what it holds.** A formula cell stores `="..."&TEXT(Loan,"$#,##0")&"..."` but displays a sentence, so sizing off the raw formula blows every column out; the function sums the quoted literals, adds 12 per `TEXT()`, and takes 62% when there is an `IF()` because only one branch ever renders. **Verify formatting against DISPLAYED text, not raw values:** a coverage cell holds 1.1176756139 and shows "1.12x", so a naive width check reports false overflows. Apply the number format first. Both live deals currently pass at zero fit problems.

**Gotcha:** Excel holds an exclusive lock, so a workbook open on the desktop makes `wb.save()` raise `PermissionError` and `formulas` cannot even read it. Write to a `_PENDING_` name and swap.

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
