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

8. **REI Skill Library:** 21 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

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

**The `county` stage now runs in the container** (was workstation-only): `_stage_county` falls back to `siftmap_standalone.SiftMapClient`, which needs nothing but `REISIFT_API_KEY`, when the Deal Room `_api` checkout is absent. A `capture` stage was added alongside it to snapshot the Knox City condemnation agendas, which are **overwritten at source** and cannot be recovered later, so it is in `DEFAULT_STAGES` despite uploading nothing. Live `FTM_ARGS` is `--stages notices,capture,county --commit`.

**Also fixed here:** `python src/main.py <mode>` used to dispatch into the Apify Actor whenever `APIFY_TOKEN` was present in `.env` (it is, for `consolidate_foreclosures`), so every local CLI call died on "tn_username and tn_password are required". The Actor path now triggers only on `APIFY_IS_AT_HOME` (or an explicit `SIFTSTACK_FORCE_ACTOR=1`). `datasift_api_upload.env()` reads `os.environ` before falling back to a `.env` file, so the uploader works on a box that has no such file. State paths honor `SIFTSTACK_STATE_DIR` / `SIFTSTACK_OUTPUT_DIR` / `SIFTSTACK_LOG_DIR`. Saved-search selection and per-page postbacks wait on `domcontentloaded` rather than `networkidle`, which regularly never settles through a proxy and abandoned a working search after 30s.

## FTM Daily Health Digest + Watchdog (build 1.0.50, 2026-08-31)

`src/ftm_health.py` answers every morning whether the first-to-market data actually got pulled, and posts one short message saying so. Built because `ftm_runner._notify` fires only when a run COMPLETES: a stopped machine or a dead scheduler thread emits nothing at all, and a missing message reads exactly like a quiet morning. Measured over the nine scheduled days to 2026-08-28: five OK, three EMPTY, one egress-blocked, and nobody was told in a way that carried the trend.

```bash
python src/ftm_health.py                  # the two-line daily summary
python src/ftm_health.py --full           # detailed report: feeds, stages, inventory
python src/ftm_health.py --check          # one line, exit 1 if red (the watchdog probe)
python src/ftm_health.py --selftest       # 45 assertions, no network, no writes
python src/ftm_schedule.py --health-once  # post right now, as the scheduler would
```

**The post is deliberately two lines, three when broken** (Ty, 2026-08-31: the first version shipped at 1,490 characters and was too much to read at 8am). `WHAT GETS SAID IS NOT WHAT GETS CHECKED` — every test in `analyze()` runs on every pass; a green day just has nothing worth saying, and a red day surfaces its top finding as the single reason line, trimmed to one sentence by `_short_reason()` because the runner's empty-notices detail runs 180 chars and ends in a command to paste.

```
:white_check_mark: *FTM Aug 28* - 8 new records
Foreclosure 8 - Probate 0 - Condemned 0
```

**Three counting rules, each from what the live volume actually showed:**
- **UPLOADED IS NOT NEW.** `POST /property/` is upsert by address and the county stage re-uploads the same 10 condemnation rows every day (proven: the address column of `knox_ftm_pull_*.csv` hashes identically across 08-22, 08-25, 08-28). New is measured against `<STATE_DIR>/ftm_record_ledger.json`, seeded once from history excluding the reported day so day one does not claim 977 new records. Idempotent: re-running the same day counts zero.
- **A ZERO DAY IS NOT A DEAD FEED.** Saved searches publish in bursts (Knox probate landed 08-24 and 08-25 and nothing either side), so the metric is DAYS SINCE a feed last produced, against `FTM_FEED_STALE_DAYS` (default 7), never a single quiet day.
- **A SKIPPED STAGE IS NOT A HEALTHY STAGE.** `StageResult.ok` counts `skipped` as OK, so a county stage that loses its SiftMap client still exits 0 and Slack still shows green. The audit compares what ran against what `FTM_ARGS` asked for. Checked here rather than by changing the runner's exit contract, which the scheduler rides on.

**Per feed, not aggregate.** The runner reports notices as one number, so 2026-08-28 uploaded 19 records that were all foreclosure while both probate feeds sat silent, and it read as healthy. Counts come by county x notice type off the day's CSV.

**List names are read from the CSV's own `Lists` column, never re-derived**, because the two mappings in this repo disagree: `datasift_formatter.NOTICE_TYPE_TO_LIST` sends `code_violation` to "Code Violation" while `knox_ftm_pull.LIST_NAME` sends it to "Condemned", and knox_ftm_pull is what writes those rows.

**`_csvs_by_day` must keep EVERY CSV per day, not the last one.** A day can hold several (the backfill wrote 48), and keeping one path per day undercounted the ledger seed by an order of magnitude (105 properties instead of 977).

**Two layers, because a run cannot report on itself.** Inside the box, `ftm_schedule.py` fires the digest at `FTM_HEALTH_AT` (default 08:00 business-local, `off` disables) as a separate event kind interleaved with the run, not a second `FTM_SCHEDULE` slot, since every slot would otherwise run `FTM_ARGS`. It stays in the same process because the run history, seen-ID cache and ledger all live on one volume and Fly attaches a volume to one machine. Outside the box, `.github/workflows/ftm-heartbeat.yml` runs daily at 14:00 UTC on GitHub, SSHes in and reads `--check`; it parses the verdict off the printed line rather than trusting flyctl to propagate a remote exit code, because a watchdog that silently always passes is worse than none. **Scheduled workflows only run from the default branch, so the watchdog is inert until this is merged to `main`.** Repo secrets `FLY_API_TOKEN` (scoped `fly tokens create ssh`, one app) and `FTM_HEALTH_WEBHOOK` are already set.

**Trade-off accepted:** reporting new records only means a healthy quiet day prints all zeros and resembles a broken one, so the tick against the siren is load-bearing rather than decorative. Amber renders as the all-clear, since amber means nothing is broken and its note is exactly the noise being cut.

`FTM_HEALTH_WEBHOOK` falls back to `SLACK_WEBHOOK_URL`; register any new env var in `deploy/sync_ftm_secrets.py`'s `OPTIONAL` or it never reaches the machine.

**Also fixed here:** `main.py`'s zero-notice alert passed the webhook URL and the message in the wrong argument order, so `requests` raised on a non-URL, `_send_webhook`'s bare `except` swallowed it, and that alert had never once fired since it was written. Exactly the class of silent failure it exists to warn about.

## FTM Outage and Repair (build 1.0.63, 2026-11-11)

Audit of every first-to-market feed after Ty flagged that nothing was landing. Seven independent failures, none of them a crash. Fixed in code and tested. **Deployed to Fly 2026-11-11** (`fly deploy -c fly.ftm.toml` from the working tree, after Ty finished a browser `fly auth login`: the `.env` `FLY_API_TOKEN` is scoped to `siftstack` and cannot see `siftstack-ftm`, and `fly auth login` refuses a non-interactive shell, so it has to be launched in its own window with `Start-Process powershell`). The catch-up ran from the workstation through the same Apify proxy.

**1. The site subscription lapsed, and it lapses again 2026-12-11.** TNPN Smart Search is a PAID subscription. A 365-day term ended 2026-10-04. Login kept working, but the saved-search dropdown collapsed to one option, "Subscription or free trial expired", so every configured search was missing and each run reported EMPTY: last good run 10/02, zero notices 10/03 through 11/11. Renewed 2026-11-11 as a **30 Day Subscription** (the dashboard prints `Current Sub. Expiration` and `Current Product`); the saved searches came back intact. `scraper.read_subscription()` now reads that block on every run: an expired account raises `SubscriptionExpired` and the stage fails as `SUBSCRIPTION EXPIRED` (never retried, never EMPTY), and `ftm_runner._subscription_warning` adds a run warning from `SUBSCRIPTION_WARN_DAYS` out. Ty set the 30-day product to auto-renew (his word, 2026-11-11; the dashboard does not show it), so the window is 3 days, env-overridable: 14 days on a 30-day term warns half the time and stops carrying information.

**2. Smarty lapsed in September and silently killed probate in both counties.** It answers `Active subscription required`. TPAD and KGIS return a street line only; the city was a default guess and the ZIP came from Smarty alone, so every probate record reached validation as "missing zip" and was dropped, while foreclosures (whose notices print a ZIP) kept the run green. DataSift probate by notice month: 80, 52, 51, then 17 (September) and 0 (October). `src/address_fallback.py` fills city + ZIP from SiftMap's free address autocomplete (Step 6-fallback in `enrichment_pipeline`) under four guards: state TN, same house number, same normalized street tokens, ZIP inside the record's county. It also corrects the default city (a "Maryville" default resolved to Rockford 37853). Smarty itself is still lapsed: renew it or drop it, the pipeline no longer depends on it for a ZIP.

**3. Probate lookups were matching strangers' houses.** Blount notices are PDF text set in a narrow column, so words break mid-word with a hyphen ("de-" then "ceased" on the next line). The decedent regex ran past its `Deceased` anchor and stored "<Name>, De- Ceased". The LLM read the same notices correctly, but it is only consulted when the regex returns nothing. `property_lookup` then searched the assessor for "DE- <FIRST>" and, because it kept the first residential row with NO check on who owned it, returned a living family's home as the estate's property: 3 of the 4 addresses "found" in a live dry run were wrong houses. Fixes: `notice_parser._join_line_wraps` (and the same repair inside `_clean_name`); `property_lookup._owner_matches_decedent`, applied to every TPAD and KGIS result (the owner of record must carry the decedent's surname AND first name; a maiden name and AKA aliases count); AKA names are split and tried as their own queries instead of being sent to the assessor whole. A miss is now an honest "no property found".

**3b. Knox had a second, unguarded lookup behind the first.** `tax_enricher._probate_property_lookup` (enrichment Step 3c) runs on whatever `property_lookup` missed, so every honest miss upstream was handed to a looser matcher. A 0.4 token-overlap score is a surname-only match on a two-word name, and its executor tier accepted any parcel whose owner string carried the decedent's surname, which is the executor's own house whenever a child with the same surname is the PR: all 8 executor-tier matches in the September Knox run were that. The same function applied a county BUSINESS account (null `parcel_address`) to a notice as `address=None`, and every later `.strip()` then crashed the stage after a 103-notice scrape. Both tiers now pass through `_usable_parcels` (a street address AND `_owner_matches_decedent`), and one record that raises no longer stops the loop.

**Address audit, 2026-11-11 (read-only, all 731 Probate-list records in ty+2).** Owner of record at each address came from the Knox tax API and TPAD (TPAD answers a street query as well as a name), tested with the same guard. 464 have the decedent on the title. 92 are owned by the personal representative instead (an executor's own home, a surviving spouse's home, or a same-name stranger). 66 are owned by someone with no tie to the decedent, 19 of them already marked sold. 6 share only the surname, 24 are not on the roll at that street, 77 carry no decedent name to test. Blount before 2026-08-25: 124 confirmed of 172. Records uploaded by the 11-11 catch-up: 81 of 82 confirmed (the one miss came through Step 3c before it was guarded and carries a pinned note). Of the 164 flagged, 51 sit in prospecting and 25 in Deep Prospecting. CSV: `output/probate_address_audit_2026-11-11.csv`. **Nothing in the CRM was changed; what to do with the flagged records is Ty's call.** A flag is not proof of a wrong house: a surviving spouse on the title and an estate that has since sold both read as a mismatch.

**"Decedent not on the title" is usually TIMING, not a wrong house (deep-prospect test, 2026-11-11).** Four flagged records were run through SmartSkip plus web research (run folders under `output/deep_prospect/`, $0.60 SmartSkip). Three were the decedent's real home: on two the title had moved to the personal representative AFTER the August upload (a $0 quitclaim from the children in January 2026; a transfer to the executor in September 2026), and one was sold on the MLS in October 2026 for $392,000. Only one was a true wrong house (the executor's own residence). Rules that came out of it:
- **SmartSkip relative rows carry the RELATIVE'S OWN address.** Trace the PR at the PR's address and the decedent comes back as a parent with where they actually lived. It misses a decedent who is not blood kin (an uncle by marriage was absent) and it is stale on deaths in both directions (it listed a husband who died in 2009 as living at the house).
- **The split that needs no research:** PR's mailing street equals the property street means the PR's own home (65 of the 92 PR-owned); the PR owns it and lives elsewhere means most likely inherited and is the lead to work (27); an unrelated owner with a priced sale since mid-2025 means the estate sold it (31); an unrelated owner with no recent transfer is a wrong house (36). Columns `suggested`, `pr_lives_there`, `last_sold` in the audit CSV.
- **The CRM `last_sold` field does NOT record a $0 family transfer.** Both inherited houses read blank, so "no recent sale" never rules out a change of title. KGIS owner cards show them.
- **A "CORRECT" phone status can be the wrong person.** On one record the number marked CORRECT belonged to the PR's spouse and child per SmartSkip, the callee said he had never heard of the property, and the PR's two carrier-verified mobiles sat on the same record at UNKNOWN. Another record took 48 calls without the PR's verified cell ever being on it. Trestle Real Contact (name plus number) settles whose line it is.
- **Subagents share the session scratchpad.** A research agent cleaned up with `rm -f *.json` there and removed this session's working files. Brief every agent to write only inside its own named subfolder and to delete nothing.

**FIXED 2026-11-12 (see "Sold Guard" below; the paragraph is kept for the measurements): the sold-property cleanup kills live first-to-market leads (found 2026-11-11).** The cleanup tags any property with a sale in the last three years `recently sold` and the sequence sets status Sold. That is right for an old bulk record (the owner we pulled has since sold) and wrong for a fresh courthouse record, where the "sale" is the CURRENT owner's own purchase and the distress came afterwards. A lien debtor is by construction the current owner (matched on the tax roll), so Liens is hit hardest: 389 of 2,710 are marked Sold, at least 263 of them bought BEFORE the lien was filed (244 with a last sale in 2024 or earlier against a pull window that starts mid-2025, plus 19 of the 20 recent ones that carry a filing date), and exactly 1 is confirmed sold after. Same pattern, smaller: Foreclosure 37 of 113, Probate 20 of 62, Obituary 18 of 56, Condemned 2 of 3 (the one real post-condemnation sale was 3240 Wilson Ave, $75,000 cash to an LLC a month after the hearing). Measured read-only; nothing was changed. A fix has to stop the re-tagging as well as reset the status, and the rule it needs is "sold AFTER the record's filing date", which the SiftMap filter cannot express. Gotchas met while measuring: the records search ignores a `status` filter silently and its rows carry no `created` or `last_sold`; the record detail has `last_sold` but no created date; `Date Added` was blank on 120 of 140 lien records checked.

**4. The condemnation capture read a frozen file for its entire life.** The City moved to a new website. The old civiclive CDN URLs still return 200 and a valid PDF with a changing Last-Modified header, but the content has been the June 25 and 26, 2026 agendas throughout: "14 this cycle, 14 cumulative" every day, the same 10 rows re-uploaded each morning, and the July, August and September cycles lost (there is no online archive; the board page says to call or email the Codes office about past hearings). `knox_condemnations.py` now reads the agenda links off the live board page (`knoxvilletn.gov/173/Better-Building-Board-Public-Officer-Hea`, matched on link text, document ids 1253 and 1255 as the fallback); parses the new labelled layout (an `ADDRESS:` line opens a dossier, `CLT NUMBER` is the parcel, the boarding table is extracted column by column and paired by position only when the two columns are the same length); treats a one-paragraph cancellation notice as a valid empty cycle; and `staleness_problems()` fails the stage when the newest date in the agendas is more than `STALE_AGENDA_DAYS` (75) old. `collect()` is the one definition of a problem, shared by the CLI and `_stage_capture`. `_condemnation_money` accepts the new wording ("City taxes:", "BOARDING BILL $208.00"). Live 2026-11-11: BBB canceled, Public Officer October 30 agenda parsed as 5 properties plus 7 boardings.

**5. A dead proxy IP looked like an empty feed.** The login was retried three times on the SAME upstream address and then returned [], which the runner reported as "0 notices scraped, check the saved searches". One session timed out for 255.9 seconds while the sessions either side of it logged in within ten; that morning's scheduled run had been "empty" in 255.8 seconds, and several September EMPTY days fit the same shape. `scraper.LoginFailed` plus `scrape_all(raise_on_login_failure=True)`; `ftm_runner` rotates to the next proxy session up to `LOGIN_EGRESS_ATTEMPTS` (3) and otherwise fails the stage as `LOGIN UNREACHABLE`.

**6. The Windows task "FTM Daily Pipeline" is dead twice over** (`_api/ftm_pipeline_run.cmd`, 9:00am). 107 of 126 runs aborted at pre-flight; it now aborts because `hours_remaining` is None on the no-expiry key account and `(None or 0) < 0.5`. Its input (Apify run CSVs plus local `output/`) has also been empty since the scrape moved to Fly: every `foreclosure_master_active_*.csv` since August is header-only. Not touched.

**7. The Register of Deeds subscription expired 2026-08-26, one day after the last lien pull.** Found 2026-11-11 when Ty asked whether the recorder was coming in. It is not, for two stacked reasons. (a) The paxsub account is PAID and lapsed ("Your VOLUNTEER HOMEBUYERS Subscription Expired 8/26/2026"). Login still works and the search page renders, but the search button fires no request: the site answers with a browser ALERT, which headless Playwright dismisses silently, so `knox_rod.py` raised "no v2Search request was captured; the page layout changed" and was written up on 2026-10-27 as a broken capture. It now reads the header timer (`subscription_state`), listens for the alert, raises `RodSubscriptionExpired` and exits 4. Renewal is a button on `/views/subscription`; not clicked, it is a purchase. (b) Nothing schedules it. `knox_ftm_pull.py` is an aggregator that reads JSON files from a scratch folder, and the daily `capture` stage writes only `condemnations.json` there, so the box's county stage has only ever uploaded condemnations. Liens (general, state tax, federal tax) were one hand-run 12-month pull, last refreshed 2026-08-25. The archive holds 12 months, so the gap is recoverable once renewed: `knox_rod.py` (LEN, STL, FTL plus the release types) -> `knox_lien_resolve.py` -> `knox_ftm_pull.py` -> upload. Trustee deeds and evictions were retired on purpose 2026-08-25; tnpublicnotice public sales and orders still has no fetcher (`deploy/FTM_AUTOMATION_GAPS.md`). Blount has no recorder or condemnation source at all, only the notices. Test: `tests/test_knox_rod_subscription.py`.

**7b. Liens are now a daily stage (Ty renewed the recorder 2026-11-11, 30 days, and it lapses again around 2026-12-11).** `ftm_runner --stages notices,capture,liens,county --commit` is the live `FTM_ARGS`. `src/knox_liens_daily.py` pulls the current month (plus the previous one in the first 5 days) for LEN, STL, FTL and the four release types, rebuilds the lookups, and stages ONLY debtors holding an instrument not in `/data/knox/lien_uploaded.json`; `county` uploads them and only then calls `mark_uploaded` (same ordering rule as the notice seen-id cache). `liens` must stay BEFORE `county` in `--stages`. `src/knox_lien_index.py` is the producer that never existed for `lien_releases.json` and `lien_amounts.json` (the August copies came out of a scratchpad): a release names the instrument it clears in raw column [13], the amount is column [14]. Verified on the box: the recorder serves a Fly datacenter IP, the stage takes 38 seconds on a quiet day, and `PAXSUB_USERNAME` / `PAXSUB_PASSWORD` are Fly secrets. Unmatched debtors are marked handled as well, because the county NAME search takes about 15 seconds each (a street search takes 3) and six in ten never match. A lapse fails the stage as `REGISTER OF DEEDS SUBSCRIPTION EXPIRED` and warns 3 days out. Gap recovery, run from the workstation the same day: 1,580 lien instruments recorded 2026-08-01 to 2026-11-11, 1,768 debtor names, 721 matched (41%), 464 debtors with a lien since 2026-08-25 across 1,451 parcels. Since August, 216 liens on the original 12-month list have been released; those DataSift records still read as open. **The county name search returns one row per OPEN BILL, not per parcel**, so once the new year's bills go out (October) any parcel with last year's still unpaid comes back twice: 734 upload rows covered 396 addresses, and 16 real 8 to 14 parcel owners were pushed over the 15-parcel "institutional" line and dropped. `knox_lien_resolve.resolve` now dedupes on the account and `knox_ftm_pull.one_lien_row_per_address` keeps one row per property (largest open lien), which also collapses co-debtors on one lien. Always compare row count to unique addresses before an upload. **Gap upload result (2026-11-11): 460 lien records on the Liens list, zero left failed** (396 plus 64 from the 16 recovered owners; 231 new records, 229 already in the account and attached; 30 carry delinquent county tax, $51,471 in total; 38 upside-down rows held out). One row died on a dropped connection and landed on a single retry. Tests: `tests/test_knox_lien_index.py`, `tests/test_knox_liens_daily.py`, `tests/test_knox_ftm_tax.py`.

**7c. A released lien comes back off the record (Ty, 2026-11-11: "anytime a lien is actually released, come back in and update that to zero in Sift").** Before this a lien was a one-way write. `src/knox_lien_releases.py` keeps a LEDGER (`/data/knox/lien_records.json`): per DataSift record, every lien instrument known to sit on that property across all debtor names and all pulls, plus what the record currently SHOWS. `reconcile()` rewrites any record whose shown figures differ from the truth (instruments not yet released, and their sum). That one rule covers a release (the number goes down, to zero when all are gone) AND a record our own upload restated from a single month's filings ("1 of 1" on a debtor with five older liens). It runs inside the `liens` stage right after the release index refreshes, and again from `_mark_liens_uploaded` after `register_uploaded` adds the day's new rows (that must run BEFORE `mark_uploaded` empties the staged debtor file). A rewrite touches Outstanding Lien Amount, Liens Active vs Total, Total Delinquency (liens plus the tax already on the record), Debt vs Equity Pct, the amount band tag, and adds a dated note; a fully released record gets the tag `Liens Released` and **comes OFF the Liens list** (Ty, same day: "I want them pulled off the lead lists"). `unlist_released()` does it per record with `POST /property/{uuid}/remove-lists/ {"lists": "Liens"}` (`lists` is a STRING; an array 201s and does nothing), reads the lists back, and runs as a sweep at the end of every committed `reconcile`, so a record zeroed earlier or a failed removal is caught next run. Only the Liens list is touched: a record also on Probate or Foreclosure stays there, because a lien release does not change that reason. It requires the record ITSELF to read "0 of N" first, keeps its own done-file (`lien_unlisted.json`), and a new lien later clears that entry when the upload puts the record back on the list. Guards: the record's own notes must name one of the debtors before any dollar figure is written; a record already reading true is left alone (`already_true`); more than 3 debtor names on one record is treated as a namesake pile-up and never summed. **Tag contract, verified live: a PATCH of `tags` only ADDS. A tag left out of the list survives, so removal goes through `POST /property/{uuid}/remove-tags/` with TITLES.** A money custom field accepts `0` and reads back `0.0`. First pass 2026-11-11: ledger joined 2,476 of 2,809 Liens-list records by street; 262 were stale, 124 because of a release (105 fully released, about $804K coming off) and 138 understated (August amounts that were blank, or today's gap upload overwriting older totals). Each update is about 7 API calls, roughly 30 seconds. Results log: `lien_release_updates.jsonl` in the scratch folder. Tests: `tests/test_knox_lien_releases.py`.

**8. The tax amount was missing or wrong three ways (Ty asked for it by name, 2026-11-11).** All in `tax_enricher.py` / `knox_ftm_pull.py`, tests `tests/test_knox_tax_lookup.py` and `tests/test_knox_ftm_tax.py`.
- **The county search is literal about the street suffix.** "100 Example Street" finds nothing, "100 EXAMPLE ST" finds the parcel. On 51 Knox foreclosure addresses the lookup found 22; with `_street_queries` (trailing suffix and directions abbreviated, then no suffix, then number last) it finds 50. Every one that missed had uploaded with a blank tax field, which reads exactly like owing nothing.
- **The first search result was taken unchecked.** The search is fuzzy and relevance-ranked; 3 of those 51 would have carried another house's bill. A parcel now counts only when its own address has the same house number and street (`_roll_street_key`, which also accepts a number printed last).
- **Blount was a placeholder.** Blount's Trustee runs the SAME vendor: `blount-tn.mygovonline.com/api/v2`, same `/parcels/<q>` and `/due/PPT/<account>` routes (found through tennesseetrustee.org's county redirect). A Blount account is its own short number, not the parcel id, so Blount is looked up by street only. Both counties are enriched in Step 5 now.
- **`amount_due` is not a delinquency.** It is every unpaid bill including the current year's, which is not late until the end of February. In August that was harmless; by November it would have tagged 1,274 of 1,451 lien parcels as tax delinquent and written "unpaid 2026" on 450 of 464 debtors. `knox_ftm_pull.apply_true_delinquency` reads each kept record's own bills and counts only `delinquent and not paid`; an unreadable account gets a BLANK, never the raw figure.
- **Scope and base rate.** COUNTY tax only: City of Knoxville, Maryville and Alcoa bill separately and are not read. Delinquent county tax is rare on fresh notices: 3 of 104 Knox records uploaded 2026-11-11 owed anything ($710 to $2,000). A sparse column is correct.

**9. The first unattended run on the new code (2026-11-12) read red for three unrelated reasons, all fixed and redeployed the same morning.**
- **The lender rode along in the owner name.** A notice read "executed by <borrower> payable to the order of <lender>, LLC"; `EXECUTED_BY_RE` had no stop for it, the owner became "<Borrower> Payable To The Order Of <Lender>, Llc", and the entity-owner filter dropped a person's foreclosure as an LLC's. Stops added for "payable to", "in favor of" and "to secure" (also on `MADE_BY_RE`). Test: `tests/test_foreclosure_owner_parsing.py`.
- **A quiet day whose only notice was filtered out reported as an upload failure** ("0 records but DataSift created 0") and reverted its seen ids, so it would have re-scraped and gone red every day. `ftm_runner` now separates "nothing to upload" from "upload failed": fewer than `ALL_DROPPED_SUSPECT` (5, env `FTM_ALL_DROPPED_SUSPECT`) scraped with no survivor is OK and marked handled; that many or more with no survivor is still a failure, because that is what a broken enrichment step looks like (the Smarty lapse).
- **The City's site (Cloudflare) answers 403 to a datacenter address**, direct AND through the Apify datacenter proxy, whatever the headers; the workstation is served. So the condemnation capture worked in every test and failed on the box. `knox_condemnations.http_get` tries the free direct fetch first and falls back to Scrapfly's residential pool (plain API call, no ASP needed, a PDF comes back base64 with `format: binary`, 25 credits a fetch, 3 fetches a day). `SCRAPFLY_KEY` was already a Fly secret. A stage that was only ever verified from the workstation is not verified: run each stage once ON the box after a deploy.

**10. What actually happens to a foreclosure notice (outcome study, 2026-11-12, read-only).** All 406 courthouse foreclosure notices first published 2025-09 to 2026-11 (about 28 a month), each matched to the county owner of record today and the deed chain after the notice. Workbook: `output/ftm_sale_cycles/FTM_Sale_Cycles_2026-11-12.xlsx` (gitignored).
- **Base rates on the 288 notices whose auction date is 60+ days old:** the borrower still owns the house 58%, an investor bought it 20%, the lender took it back 9%, the owner sold at retail 6%, unclear or a no-price transfer 9%. More than half of all notices never end in a sale, so the follow-up runway is long.
- **The clock:** first notice to auction date is 34 days at the median (25 to 47 for the middle half); 8% are postponed and republished; an auction deed records about 12 days after the sale date; a flipped auction buy resells after a median 113 days; a lender resells its take-back after about 147 days, often below its own bid.
- **Investor purchases: 62 in 13 months, about 4.6 a month.** 46 at the auction, 11 direct from the owner BEFORE the auction, 5 later. 50 paid cash. Median purchase $158,000 against a $280,000 estimated value. The 20 that have already resold: bought at a median $171,000, resold at $312,500, median gross $92,400 before repairs and costs. Two buyers took 15 of the 62.
- **While the notices sat in our account (from 2026-08-14): 15 investor purchases, 11 lender take-backs and 3 owner retail sales.** On the 15 the call counter reads zero; three were texted. Across all 406, 47 were ever called, 103 texted, none mailed, 272 untouched.
- **Three measurement traps, each of which bent the first pass.** (a) **The CRM `last_sold` lags the county**: it missed 30 of the 113 transfers (14 of them lender take-backs); the county owner roll by street plus the SiftMap deed finds them. (b) **On a record the sold cleanup touched, the CRM owner is the BUYER**, because that SiftMap add replaces owners, so "does the seller match the CRM owner" reads backwards there. (c) **Classify on the FIRST deed after the notice, not the last sale.** By last sale, an investor's retail exit reads as "the owner sold at retail" (48 such rows collapsed to 17 real ones) and a lender take-back that has since resold disappears. A small-dollar deed days before the auction followed by the auction deed is a side transfer, not a purchase and a resale.
- **Probate, same method (757 records):** at least 36 estates have sold; median 4.6 months from notice to sale (middle half 2.9 to 8.7); 25 retail at 98% of value, 6 at a discount (median 69% of value), 5 family or no-price. Only about 7% of confirmed-house notices older than 9 months have sold. 12 of the 36 are still in prospecting or Deep Prospecting status.

**11. What the automated deep prospect costs, and that the research step does not need the top model (2026-11-12).** `python src/agents/cli.py run --address ...` measured at $4.17 live on one foreclosure (research $3.30, two analysis passes $0.73, SmartSkip $0.15) and about 12 minutes; a second pass on the same house cost $4.49 for research alone. **Dry mode still pays for the research** (it skips only vendors and CRM writes), research is re-bought on every re-run, and the default `AGENTS_RUN_CAP_USD=5` makes a live run skip scoring and verification. The research is expensive by configuration: Opus 5.5 at high effort, 30 searches and 30 page opens allowed with no page-size cap, only the system prompt cached, and the full heir hunt run even on a living owner (227 result links on the test, 48 of them Wikipedia pages for strangers sharing a surname). A head-to-head on 11 known-answer records (6 hard: a same-name stranger's obituary, a son with the owner's name, an owner dead since 1999, a common-name death, a wrong house, a sold house), same instructions and brief, our own search and page-open tools capped at 14 searches / 12 opens / 10,000 characters a page, graded strictly against 36 hand-verified facts: four low-cost models through OpenRouter confirmed 31 to 33 of 36 for a model cost of 1 to 3 cents a record; Claude Haiku 5.5 confirmed 26 and Claude Sonnet 5.5, run as a stronger reference, was more cautious but not more accurate on the same search budget. **The dangerous failure is a death no web search finds:** all 9 dangerous statements came from two such records, where the model treated the title holder as living or named a dead person as a required signer. The Knox County Public Library obituary index (`engagedpatrons.org/database/KnoxObits/Brief.cfm`, News Sentinel since 1991, free) lists both deaths beside their same-name strangers; with it as a lookup tool and the rule "never name a person as a required signer while their status is unverified", the four trap records produced zero dangerous statements in 24 runs. So structured lookups and guards matter more than model strength, and price no longer separates the models. One run per model per record, so a gap of a fact or two is noise. Rig, answer keys, grader and workbook: `output/research_model_bakeoff_2026-11-12/` (gitignored). Found on the way: `SERPER_API_KEY` is out of credits, `SERPAPI_KEY` is a 15,000-search monthly plan, and Firecrawl's 1,000 monthly credits will not cover a full run.

**Why nobody saw it:** the watchdog was red every day from 2026-09-03 for a series of different reasons (the 403 create route, a stale-feed finding, this), so red stopped carrying information. The top finding also hid the rest: it printed "Blount/probate has produced nothing" for a month while Knox probate was dying of the same cause.

**The trap after any long gap:** `last_run.json` advances only on a successful upload, so the first daily run after a multi-week gap pulls the whole gap in one process, trips the ~204-view per-IP block, exits 3 and reverts, every day. Close a gap with one process per (saved search x month), then set the box's `/data/last_run.json` to today:

```bash
python src/ftm_runner.py --stages notices --counties Knox --types probate \
    --backfill-months 1 --backfill-offset 1 --commit     # offset 0 = this month, 1 = last month
```

Probate has to go back to September (the Smarty lapse), foreclosure to October. A dry run posts to Slack as well; `SLACK_WEBHOOK_URL=` in the environment suppresses it. `seen_ids` are pruned after 90 days.

**Catch-up result, 2026-11-11 (run from the workstation):** a canary plus ten jobs, one per saved search per month, put 218 records into ty+2 with zero upload failures: foreclosure 76 (October and November), probate 142 (Knox 54, Blount 88, back to August for Blount and September for Knox). Of the 215 the ten jobs sent, 87 were new records and 128 already existed and had the list, tags and fields attached. A typical probate month: 93 notices scraped, 55 unique, 34 landed; the rest are honest "no property found" misses and vacant lots. The box was then brought level the same day: the workstation's 544 seen ids were merged into `/data/seen_ids.json` (864 -> 1,205) and `/data/last_run.json` moved from 2026-10-02 to 2026-11-11, with dated `*.pre_merge_2026-11-11.json` copies beside them. Git Bash rewrites a remote `/data/...` argument into `C:/Program Files/Git/data/...`; set `MSYS_NO_PATHCONV=1` for `fly ssh sftp put` and `fly ssh console -C`. The live dropdown also carries four saved searches the run does not use (`Tax Sale V2` and `Tax Delinquent V2`, Knox and Blount).

Tests (zero network): `tests/test_probate_parsing.py` (line wraps), `tests/test_probate_property_guard.py` (owner guard, ZIP fallback, the Knox second-pass lookup), `tests/test_knox_condemnations.py` (new layout, cancellation, staleness), `tests/test_ftm_login_rotation.py` (proxy rotation, expired subscription, expiry warning).

## Sold Guard: Sold means the house sold AFTER it became a lead (build 1.0.66, 2026-11-12)

`src/sold_guard.py` is now the only thing that hands out the `recently sold` tag in ty+2. Ty asked whether the sold sequence was firing correctly. It was, on the wrong records. Tests: `tests/test_sold_guard.py` (zero network, invented streets).

```bash
python src/sold_guard.py audit                      # read-only counts
python src/sold_guard.py presets [--commit]         # show / switch off the nightly SiftMap tagging
python src/sold_guard.py repair [--commit] [--include-distress] [--fresh]
python src/sold_guard.py mark [--commit]            # real sales the tag missed
python src/sold_guard.py daily [--commit]           # repair + mark (the `sold` stage of ftm_runner)
python src/sold_guard.py undo [--commit]            # replay output/sold_guard/changes.jsonl backwards
```

**What was wrong, all read off the live account and the records' own activity logs.**
- **The rule had a fixed date.** SiftMap presets 4076 "Knox - Recently Sold" and 4084 "Blount - Recently Sold" ran nightly (about 04:10 UTC) with `in_my_account_mode: "in"`, `type_single_family`, `extra_is_last_sale_interfamily: false` and `extra_last_sale_date_min: "2023-01-01"`, tagging every match `recently sold`. A fixed floor never moves, so "recently" had grown to 46 months: a record created one night was tagged the next, and the "sale" was the current owner's own purchase. Of 25,715 tagged records only about 640 (2.5%) had a last sale after the record was created.
- **A backlog pull planted 24,600 dead records.** On 2026-06-25 one sold-backlog pull CREATED about 24,600 records (46% of the 53,248 in the account) for houses that had already sold, born with the tag. Every later lead at one of those addresses attached to a record that was already Sold. `POST /api/internal/property/` on a duplicate address attaches to the existing record, so a courthouse notice uploaded in November landed on a record marked Sold in July.
- **The trigger is unreliable on the nightly add.** Both sequences ("Recently Sold to Sold Status", 18,691 runs; "Sold Property Removal", 11,968 runs, an exact duplicate) trigger on `property.tags.added` with the condition has-tag. On the 04:10 system add they often do not fire; the status flips hours or weeks later, when ANY other tag lands on the record (the logs show `source: sequence` at 14:30 UTC beside an unrelated tag add). About half of the newest tagged leads were tagged and never flipped. The guard therefore sets the status itself.
- **A dormant pair conflicts.** "Sold Property Cleanup" (status to Sold) and "Sold Property Cleanup V2" (strip lists and 114 tags including Courthouse Data, delete tasks, status to none) both fire on the tag `Sold`, which no record carries today. Left alone; do not start using that tag.

**The rule (`decide`).** lead day = the day the record was created, re-dated by a later join of one of OUR upload lists (our uploader corrects the owner), or, for a record born with the sold tag, the latest join of any of our lists. floor = the earliest dated distress on the record (notice month tag, `filed_YYYY-Qn`, `last_obituary_date`; a probate notice reaches back 365 days), never later than the lead day.
- last sale on or after the lead day: a real sale, stays Sold (and is set Sold if the sequence missed it).
- between the floor and the lead day: left alone ("sold between the filing and our pull"; a house can sell at its own auction before we upload the notice).
- within 120 days before the lead day AND not the sale the record carried at birth: left alone ("sold just before we pulled it"). Deeds reach the data weeks late, so the owner we hold may be the seller. `property.created` carries the birth value of `last_sold`; that one comparison separated the two cases cleanly (41 of 44 own-purchase rows in a sample had the sale on the record at birth, every left-alone row had a sale years older at birth).
- older than the floor: the owner's own purchase. The tag comes off and the status goes back to what the sequence overwrote, read from the record's own `property.status.updated` event (`[old, "sold"]`). A flip whose `source` is not `sequence` or `bulk` was a person's call and is never undone.

**Live result, 2026-11-12.** Presets 3241, 4076 and 4084: auto-add OFF (read back). Repair over 1,405 tagged leads (created after the backlog pull, or sitting on one of our lists): **855 restored** (842 to no status, 9 prospecting, 2 auction date passed, 1 follow-up, 1 dead lead), 33 untagged that had never been flipped, 71 real sales set to Sold that the sequence had missed. Left alone: about 150 real sales already Sold, 121 never leads, 114 between the filing and our pull, 59 sold just before we pulled, 13 marked Sold by a person, 3 with no sale date. The mark pass then tagged and set Sold 58 real sales the old tagging had not reached. Every change is in `output/sold_guard/changes.jsonl` with the prior status; `undo --commit` puts them back.

**Contracts verified live.**
- The records search honors `created: [from, to]` (dates or datetimes; a same-day pair `[d, d]` matches nothing, use `[d, d+1]` or datetimes) and `last_sold: [from, to]`. `created_date`, `date_added`, `last_sale_date` and `last_sold_date` are silently ignored. Search rows carry `list_count`, `tag_count`, `status` and no dates.
- `POST /api/internal/property/{uuid}/status/ {"status": null}` clears a status. A PATCH of the record refuses it ("This field may not be null").
- `GET /api/internal/property/{uuid}/logs/?any_event_type=a,b,c` filters the log. Events carry `source`: `sequence` (with `payload.extra_info.source.uuid` = the sequence), `bulk` (SiftMap adds, bulk actions), `auto-update` (DataSift's own refresh: it updates `last_sold` and `mls` by itself, about two months after a sale), `data-enriching`, `internal-api` (our key).
- `list_count` on a search row includes DataSift's classification lists, so it cannot tell a lead from a bare record.
- `PATCH https://map.reisift.io/filters/{id}/ {"auto_add_enabled": false}` works on the Open API key.

**Going forward.** `ftm_runner` has a `sold` stage (last in `ALL_STAGES`, in `FTM_ARGS` in `fly.ftm.toml`): it revives a lead that landed on an old Sold record and marks real sales. **Deployed 2026-11-12 (siftstack-ftm v27)** and run once on the box the same night, dry: 592 tagged leads, 85 re-judged with nothing to change, 8 real late-October sales queued for the first committed run. The change log and the verdict cache were copied to the box (`/data/sold_guard/`), so the box holds the complete log from here on and `undo` for anything after 2026-11-12 runs there. Verdicts are cached in `sold_guard/judged.json` (a "leave" is not read again until the row's list count, tag count or status moves; a sale is judged once per record per calendar week of the sale), so the daily pass is small after the first run. Trade-off accepted: detection now rides DataSift's own auto-update of `last_sold` (about two months behind a closing) instead of the SiftMap index (about one). Do NOT switch the presets back on: they would re-tag every restored record the same night.

**Not done, Ty to decide.** (1) About 1,000 backlog-born records sit only on DataSift's own distress lists (Tax Delinquent 341, Vacant 354, Notice of Default 95, Bankruptcy 56, Tired Landlord 48): never pulled by us, wrongly Sold by the same logic; `repair --include-distress --commit` revives them, which adds marketing volume. (2) The 24,600 backlog-born records that are on no lead list stay Sold; they are harmless now that the guard revives any that becomes a lead. (3) The duplicate sequence and the dormant `Sold` pair. (4) The community SOP (`sops/remove-sold-properties-from-marketing.sop.md` and the "Sold Property Removal" walkthrough) teaches this exact method (a fixed three-year window and a county-wide backlog pull) and needs the same correction: the window has to start the day the list was pulled and keep moving.

## Low-Cost Deep Prospect Research + Backlog Runner (build 1.0.65, 2026-11-12)

`src/agents/research/open_research.py` is a second research engine for the agents' deep prospect and `src/deep_prospect_batch.py` runs the chain over a backlog. **The Claude researcher stays the default**: nothing changes until `AGENTS_RESEARCH_ENGINE=open`. `deep_prospect.py` calls `open_research.engine()`, which hands back `researcher.research` / `researcher.analyze` untouched unless the switch is set. Tests (zero network, invented names): `tests/test_open_research.py`, `tests/test_deep_prospect_batch.py`.

```bash
python src/deep_prospect_batch.py cohort                               # read-only: who is in scope on the three lists
python src/deep_prospect_batch.py run --list Foreclosure --limit 7     # dry: research only (still paid), no vendors, no CRM writes
python src/deep_prospect_batch.py run --list Probate --limit 50 --commit --workers 2 --max-usd 60 --daily-cap 80
python src/deep_prospect_batch.py run --uuids <a,b> --commit --redo    # named records; --redo re-runs a finished one
python src/deep_prospect_batch.py estimate | status
```

**Shape.** Two inexpensive models (`AGENTS_RESEARCH_MODEL`, default DeepSeek V4.1 Flash; `AGENTS_RESEARCH_SECOND`, default Muse Ember) research the same record independently through OpenRouter, in parallel, on the production instructions and brief, with OUR tools: Google search (Serper while it has credits, else the SerpApi plan), a page opener (plain request first, then Firecrawl, then Scrapfly ASP, 8 protected pages a record) and the Knox County Public Library obituary index. Each ends its memo with a fact sheet (owner status, decedents, who signs). `compare()` splits disagreements into hard (a referee settles them: `AGENTS_RESEARCH_REFEREE`, default Claude Sonnet 5.5, 6 searches and 8 pages) and soft (carried into `unresolved` as cross-check notes). The lead memo is shaped into `RESEARCH_SCHEMA` on the low-cost model and passes the SAME `researcher.conform` and `researcher.enforce_sources` as the Claude path. A sidecar `research/<slug>_open.json` records models, hosts, fact sheets, disputes, web counts and cost; memos land beside it. `AGENTS_ANALYSIS_MODEL` runs the analysis passes on a named model by swapping `researcher._structured` for the duration of the call, so the production prompt and `enforce_analysis` are reused, never copied.

**Measured 2026-11-12.** 11 known-answer houses through the production engine: 29 of 36 facts, 19 of 23 on the hard houses, zero dangerous statements that a caller would act on, both death traps (owner dead since 1999, common-name death in 2025) correct. Analysis first pass on the same research: low-cost model 30 of 36, Opus 29, Sonnet 29 with one dangerous statement, at $0.006, $0.21 and $0.09 a pass. Seven upcoming-auction foreclosure records run live end to end and written back: $1.21 a record and about 8 minutes, against $4.17 on the Claude path the same morning. Of that $1.21, Opus analysis is $0.56 and searches at the SerpApi rate are $0.27; the research models are 3 cents. One run found a deceased co-owner and named the surviving spouse. **The final (call order) pass on the low-cost model, run on copies of those seven: same owner status and same first number on all seven, but a different signer list on three.** So the analysis stays on Claude until that gap is closed; `AGENTS_ANALYSIS_MODEL` is the switch.

**"Unverified" must not mean "prove they are alive".** Two of the seven first came back "owner status unknown, who signs: not determined" on living borrowers named in a 2026 notice: one researcher and the referee read the signer rule as needing proof of life. The tool rules now define the three statuses: Living when no death matches on name AND age AND something from the last two years names the person as a living party; Unknown only for a SPECIFIC candidate death that could not be ruled out, which must be named; Deceased only on an opened record or an index row matching name and age. Re-validated on six known-answer houses including both death traps: 16 of 19 facts, zero dangerous statements, and the 1999-death house now names the living widow. Research-only re-checks of the two live records returned Living with both owners named and no referee.

**Where the money is now.** (1) Searches: each researcher uses nearly all 14 and the two share almost no queries (4 of 419 in the test traces), so a shared cache does not halve them; 26 a record at SerpApi's effective $0.01 is the largest research line, and the plan's 15,000 a month covers about a third of the three lists. Serper's prepaid pack is about $1 per 1,000 (`SERPER_API_KEY` was at a negative balance; a refusal flips `_dead["serper"]` once per process). (2) Analysis on Opus. (3) Vendors: skip trace $0.15 a person found, scoring $0.015 a number, Real Contact about $0.03 a pair (not itemized on the run ledger). The three lists (1,494 records in scope after the full-record gates below and the sold repair: Foreclosure 20, Probate 555, Obituary 919) price four ways: about $7,200 on the default; $2,800 as the engine ran that day (SerpApi searches, Claude analysis); $2,470 with a Serper pack and Claude analysis kept, which is the setup to run now; $1,675 with the analysis on the low-cost model too, 86% of it vendor fees. Per record on that last line: Foreclosure $0.90 (measured usage), Probate $0.87 and Obituary $1.28 (usage assumed: one owner trace plus 1.0 and 2.5 relatives). Workbook with live formulas: `output/deep_prospect_batch/Deep_Prospect_Cost_Estimate_2026-11-12.xlsx` (gitignored).

**A memo proves nothing by its length.** Found live: OpenRouter routed the first researcher to a host that ignored the tools, and the model returned a confident 1,900-word memo in 12 seconds with zero searches, including a death it had no source for. `usable()` now requires at least 3 tool calls, an unusable researcher is retried once, and every tool request carries `provider.require_parameters: true` so a host that drops tools is never chosen. All calls also carry `data_collection: deny`; the hosts that served each run are in the sidecar.

**Cross-check rules, each one a false referee call found on a real record** (the referee is the expensive step, $0.22 to $0.67): Sr. and Jr. are two people; a shared middle name and surname is a parent and child, so the FIRST name must agree; an accusation ("dead in one memo, signing in the other") needs the same first name outright, because a son often carries his father's first name as his middle name; a signer line about a role or group ("the executor of the estate of X", "X's heirs", "successor trustee of the X Trust") names nobody; one date written two ways is one date; "before 2007" is not a date; a same-name stranger listed in order to be ruled out is not a finding; a date conflict goes to the referee only when it is about a title holder. Any split on the owner's status is hard, with one exception: "Living" against "could not confirm" is a note when the record is in Knox County AND the index holds no row for any owner's name (`index_covers`, `absence_vouched`). Blount has no index, so that split is refereed there. Referee rate: 1 of 11 test houses, 2 of 7 live Blount records before the status standard was tightened, 1 of 8 after.

**Guards in code.** The owner's name is looked up in the obituary index BEFORE any model runs and the rows go into the brief. `guard_signers` removes a named person the analysis marks Deceased from `must_sign`, labels an Unknown one `UNVERIFIED`, and leaves role lines and a living namesake alone. `mark_unverified` exists because `RESEARCH_SCHEMA` stores `living` as a boolean, so a person nobody verified reads exactly like a dead one; the relationship text now says which. An unsettled dispute ships the owner's status as Unknown with a `DISPUTED` red flag. No researcher producing a memo is a failure, never an empty success.

**The first count never looked at the sale date (Ty caught it, 2026-11-12).** The cohort gated on the sold tag, dead statuses and entity owners off the SEARCH rows, which carry no notes, last sale or MLS field, so 240 of the 267 foreclosures it called in scope had an auction date already passed. Every survivor's FULL record is now read (`facts_of`, cached for the day in `record_facts.json`; about 20 minutes for 1,780 records) and `detail_gate` applies three rules, each counted: the newest `Auction: M/D/YYYY` line in the notes must be after today (a sale dated today counts as passed; a courthouse foreclosure with no date is held out), `last_sold` must predate the notice month, the probate open date or `last_obituary_date`, and `mls` must not read Listed or Pending. `run_gate` repeats the check right before each record in `run`, because a cohort is a snapshot and an auction passes while a batch waits (`--force` skips it); a record run by name skips the tag and status gates but never the facts about the house. **An MLS status of Sold is not a gate:** it mirrors the last sale, which is often the current owner's own purchase (bought in January, noticed in November, sale date the next January). `mls` values seen live: Off Market, blank, Pending, Sold, Listed. Recount: Foreclosure 407 on the list to 18, Probate 756 to 540, Obituary 1,198 to 903, then 20 / 555 / 919 the same evening once the sold repair (see "Sold Guard") brought 33 wrongly-Sold records back. Two limits: `last_sold` runs weeks behind the county, so a very recent sale can still be counted (the research step is the backstop), and 71 in-scope Probate records are ones the 11-11 address audit flagged as a likely wrong house (Ty to decide).

**Backlog runner contracts.** `must_not` nests INSIDE `must` on the records search (a sibling 400s: "'must' and 'must_not' can't be specified at the same time"); verified by count delta (1,198 to 1,126 on one tag). A `status` filter is silently ignored, so status is gated in code off the rows. Search rows carry no tags. Each record runs in its own process (`deep_prospect_record` patches module state per row), with a task row marked running so no worker picks it up twice; the run prints one line a record and posts nothing to Slack. A dry run never blocks the later live one. A re-run PATCHes the existing pinned note (the marker match in `dream100_dp.phase_writeback`), so `--redo` is safe.

**Open.** A Serper pack and page-open credits before any large run (Firecrawl's free 1,000 a month and Scrapfly's roughly 6,400 protected pages a cycle will not cover 1,494 records). The low-cost analysis ties Opus on facts but not yet on signer lists (above). Two of the seven live records still carry their first-run "status unverified" files; a `--redo` on the current engine fixes them. A Blount obituary source (none found online past 1960). Probate and Obituary usage in the estimate is assumed, not measured. `deploy/sync_agents_secrets.py` lists the new settings for the Fly app. One agents selftest check (`selftest_autopilot`, the outside-vendor mail piece) hardcodes 2026-11-11 as today and fails on any other day; it is the other session's to fix.

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

**Auth: mint the JWT, never paste one.** `POST /api/token/` with `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` from `.env` returns `{access, refresh}`. The uploader mints on start and re-mints every 30 minutes so long runs cannot die on expiry. **Correction, verified live 2026-11-03: the Open API key DOES reach the `/api/internal/` write routes.** Custom fields are absent from the key's documented 93-route surface, which is where the old "every write 401s" note came from, but `authorization: Api-Key <key>` on `/api/internal/` passes the permission check for create (`POST /api/internal/property/` empty body -> 400 "address/owner required"), `add-lists` (400 "lists required") and `custom-field/update-values` (bogus field uuid -> 400 "Invalid field UUID"), against a 401 for a fake key. The SMS agent's `crm_standalone` already writes tags, status and notes this way. So customer-facing agents can run on the key alone, with no password (Ty, 2026-11-03: never collect customer passwords). Not yet done: one real create plus read-back on the key before switching the uploader off the JWT.

Four traps, each of which fails silently or cryptically:
1. **Tags must be an ARRAY.** A comma string creates one tag literally named `"Courthouse Data, code_violation, Knox"`.
2. **A select field's value must be the OPTION'S UUID, not its label.** `"LEN"` returns `{"non_field_errors": ["'LEN' is not a valid UUID."]}`. Resolve via `custom-fields/` `options[]`.
3. **Entity owners cannot have a blank `first_name`** (the API rejects it). Send the business as `company` and OMIT the person keys; omitting a key is not the same as sending `""`.
4. **`notes` on the property payload returns 200 and is discarded.** Post it separately.

`POST /property/` is **upsert by address**, so re-runs never duplicate, and **lists accumulate** rather than overwrite (verified: a record came back with both new lists plus four it already had). Custom fields go to `PATCH /api/internal/property/{uuid}/custom-field/update-values/` with `[{"field_uuid": ..., "value": ...}]`. Creating a `select` custom field REQUIRES its options in the same POST.

**Always upload one record and read it back before releasing the file.** That single habit caught the tag format, the entity-owner rejection, the option-UUID requirement and a list-name mismatch that would have silently attached nothing for 2,512 of 2,573 records.

**THE OPEN API CREATE ROUTE DIED ON 2026-09-01 (fixed 2026-09-05).** `POST /property/` began
returning HTTP 403 "You do not have permission to perform this action" for EVERY method (GET,
POST, OPTIONS) under BOTH auth types (minted ty+2 JWT and the Api-Key), with the account itself
unchanged: super-admin, active business plan, 89 of 100,000 records used. Five scheduled runs
scraped cleanly and uploaded nothing; the ledger reverted seen-IDs each time so nothing was lost,
and the watchdog is what finally said so. Records are now created through
`POST /api/internal/property/` (`datasift_api_upload.create_property`), the same surface every
other write already used. That route's contract differs from the old one in three measured ways:
- **It does NOT upsert.** A duplicate address returns 400
  `{"non_field_errors":["Property address already exists!"],"property":["<uuid>"]}`, which hands
  the uuid back. `create_property` then ATTACHES the payload to that record: lists via
  `/add-lists/` (a STRING title, one call per list), tags as a read-modify-write PATCH of the full
  set, and the owner PATCHed only when the payload's owner differs. Lists and tags accumulate
  exactly as before; the county stage's daily re-send of the same 10 rows reports
  `created=10 (of which 10 already existed)`.
- **`owner` is validated BEFORE the duplicate check**, so an address-only body 400s whether or
  not the record exists. Callers that used the upsert as a lookup (dispo_flip_buyers' tag-by-
  address, the probate one-offs) now go through `find_property` (POST-as-GET
  `search: address_prefix:<street>`, matched on normalized street + zip5) and `attach_to_record`.
- Lists by title and tags as an array are accepted as before; DataSift standardizes the street on
  ingest ("6520 FLINT GAP RD" reads back as "6520 Flint Gap Rd"), which is why the lookup match is
  case- and punctuation-insensitive.
`create_property` falls back to the legacy route only on a 403 or 404, so a reversal at DataSift
cannot break the uploader the other way. `tests/test_datasift_api_upload.py` pins all of it with
a stubbed client. Whether `/property/` was retired on purpose is a question for DataSift
engineering; the Open API key gets the same 403, so any community user on that documented route
is broken too.

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

## County List Playbook shards: what SiftStack consumes (2026-08-31)

`src/list_inventory.py`, `src/staging_build_39049.py`, `src/siftmap_pull.py` and
`src/list_inventory_report.py` all read `https://learn.datasift.ai/county-data/<st>.json`
(2-digit state shard, 5-digit FIPS keys inside). SiftStack is a READ-ONLY consumer: the
page, the shards and their builders live elsewhere (`Interactive Educational Websites`
for the page, `Deal Room Coaching Call/_api/` for the builders, runbook
`_api/RESOURCE-PAGE-RUNBOOK.md`).

**There are TWO shard families and they do not agree.** `/county-data/` scores each list
**standalone** and is what these modules read. `/capture-data/` is the market share ladder,
where every rung is **marginal** (it counts only the deals the rungs above did not already
reach) and the list-size column is cumulative. The site shows the ladder and HIDES the
standalone table wherever a county has one, so a segment's on-screen doors/deal is usually
NOT the number in `county-data` that these scripts rank on. That is expected: standalone
answers "is this list worth marketing to", marginal answers "what do I pull next without
paying twice for the same deal".

**THE CAVEAT THAT MOVES NUMBERS: `county-data` ships June 2026 as ZERO deals in every
county.** `trend[Jun] == 0` in 216/216 TX, 87/87 OH and 95/95 TN, while the shard labels
itself "2026-01 .. 2026-06 (6 complete months)". The window is a hard-coded constant and
both families run the same query with the same dedupe; they just called the endpoint on
different days, and deed recording lags 4 to 8 weeks (county sweep 2026-07-18, ladder
sweep 2026-08-11/13). So every `deals`, `dpd` and `lift` these scripts read sits on a FIVE
month denominator: Knox 745 against the ladder's 927, Harris 9,440 against 10,440, capture
higher in 187 of 216 TX counties and lower in zero. The 37 `deep_fips` counties are a third
generation, pinned to an older cached payload. **Anything that gates on lift (P1/P2
selection in `staging_build_39049.select_segments`, the `list_inventory_report` P1/P2 split)
is ranking on that short window.** The ordering is broadly preserved because the missing
month hits all segments, but absolute doors/deal reads about 20% high. Agreed fix, not yet
run: refill June against the same window plus a lag guard that refuses to ship a month
reading zero or far below trend. A plain re-run of `national-sweep.py` does nothing, it
skips any county already marked `"profile":"rich"`.

Shipped 2026-08-31 on the page side (commit 7367bc0 on `main`, the only branch Amplify
builds): the Excel export now mirrors the visible cards one tab each, and every displayed
count is the rolling six month read. Full contract: the learning-pages `CLAUDE.md`.

## ty+1 Staging Build-Out - Franklin County OH 39049 (build 1.0.45, 2026-08-19)

One-shot build of the ty+1@dataflik.com staging account from the 5-day challenge structure, driven by the County List Playbook shard (`learn.datasift.ai/county-data/39.json` key `39049` - state 2-digit shard URL, per-county keys inside). Driver: `src/staging_build_39049.py` (phases `preflight/sequences/size/pull/feeders/crm-clear/crm-mirror/qa`, DRY by default + `--commit`, state `output/staging_39049_state.json`, idempotent + read-back everywhere). Auth: `src/reisift_session.py` holds the target token IN PROCESS with a `verify_target()` hard gate before every write phase (email + account uuid + live read), so nothing can write to production ty+2.

**Auth reality:** ty+1 is reached by a PASTED JWT installed via `reisift_auth.py add datasift-admin <jwt>` (the Session auto-detects a valid one in the `_api` store). **Staff-to-staff impersonation is refused** - `POST /api/internal/impersonate/ty+1@.../` from the valid ty+2 staff token 403s empty-body, both raw and %2B-encoded. Password mint and refresh were also dead ends; a fresh cURL paste is THE unblock.

**What is live in ty+1:** 19 P1 doors-per-deal lists (`Franklin OH P1-01 - <seg>` ...) with pulls submitted for 44,398 records; 48 SiftMap presets (46 segment feeders with auto-add + buy box + `year_built_min/max` 1940/2010, a manual sold-pull preset "Franklin OH - Sold Since 2023-01", and the self-cleaning "Franklin OH - Recently Sold - Suppress" with `in_my_account_mode: "in"` tagging `recently sold`); the full 20-folder / 98-preset challenge CRM mirror cloned live from ty+2 (uuid -> title -> uuid translation, per-folder counts verified); 9 custom lead statuses; the active "Recently Sold to Sold Status" sequence (tag added -> status sold, Transactions folder); all test sequences/presets/quick-filters wiped (sequence backups in `output/backups/`). All 74 marketing presets (folders 01-13) carry `any_structure_type: ["Single Family Residential"]` + `year: [1940, 2010]` + a data-driven 75-neighborhood exclusion set (Market Finder Ohio/Franklin extraction scored by investor transactions, margin, and buy box; `output/franklin_neighborhood_suppress.csv`). FTM investigation: `deploy/FTM_FRANKLIN_39049.md` (Franklin has NO provider data for tax sale / eviction / code violation / divorce; county-direct is the only way in; no paid signups needed).

**Hard-won contracts (all verified live, most fail SILENTLY if wrong):**
- Records-page must grammar: the year-built range key is **`year`, NOT `year_built`** (year_built is accepted and ignored - probe by count delta, never trust acceptance). Structure enum is exactly **"Single Family Residential"** ("Single Family"/"SFR" match nothing). Records with no year/structure value are EXCLUDED by these filters.
- **Folder preset listing defaults to 10 rows** (`/filter-preset-folder/{uuid}/filter-preset/`): always pass `?limit=999` or exists-checks lie and re-creates 400 on the global unique-title constraint.
- Create-by-title works: `POST /api/internal/list/` and `/tag/` take `{title}`. **`POST /api/internal/status/` requires `color`** - clone the full status object from the source account.
- SiftMap sold/date filters are the **`extra_*` family**: `extra_last_sale_date_min`, `extra_is_last_sale_interfamily`, plus `in_my_account_mode: "in"` (scan only records already in the account) and `year_built_min/max`. Every guessed sold-date key name was silently ignored.
- `POST /properties/add-properties-by-query/` returns `"Activity created successfully"` and processes **async server-side**; the ty+1 staging worker queue was nearly stalled (68 of 44,398 landed in 40 min; a prior-day 145k vacancy check sat at processed=0). Zero lists after 20 min does NOT mean the add failed - check `/api/internal/activity/` and the queue. Pre-creating the lists by exact title is safe: the add attaches by NAME.
- ty+2's own presets carry **duplicated uuids** in `must_not.any_lists` and refs to deleted lists - dedupe and drop on any clone.
- Sequences API: full CRUD on `/api/internal/sequence/`; folders on `/api/internal/sequence-folder/`; the sold sequence shape is trigger `property.tags.added` + condition `has_all tags_uuid` + action `set-field-value status=sold`.

**Open items:** the P1 record landing (server queue; re-check with `python src/staging_build_39049.py --phase qa`); ty+1 has no AI addons so HOTTEST/STRONG `investor_score` presets read 0 until enabled; the 6 CALLER QUEUES presets point at ty+1's own user until staging callers exist; 4 named SiftMap presets left untouched (Okaloosa SFH, Karan Desai, Tyson Morrison, Nate Hirschberg); out-of-range-year records from the pre-filter pulls can be stripped from lists once landing completes.

## Account Blueprint: clone ty+2's structure into any account (build 1.0.52, 2026-09-10)

`src/clone_account.py` builds a DataSift account in the same structure as ty+2 over the API,
for a community member on their own account or for staff setting up a client. Two steps, one
portable file: `export` reads ty+2 LIVE into a title-keyed, uuid-free blueprint JSON; `apply`
takes that file plus the TARGET's own credential and creates every family in dependency order,
reads each object back, and writes a report. Package `src/account_blueprint/`; the apply side is
stdlib-only and imports nothing from the Deal Room checkout. Ships as the `account-blueprint`
skill with the current blueprint inside (`tools/sync_account_blueprint_skill.py` copies both;
a test pins the copy to the source).

```bash
python src/clone_account.py --phase export                                   # ty+2 -> output/blueprints/ty2_<date>.json
python src/clone_account.py --phase validate --blueprint <bp>
python src/clone_account.py --phase plan  --blueprint <bp> --target you@x.com --jwt <paste>   # dry diff + TODOs
python src/clone_account.py --phase apply --blueprint <bp> --target you@x.com --email you@x.com --password ... --commit
python src/clone_account.py --phase apply --blueprint <bp> --target client@x.com --impersonate --commit   # staff, non-staff client
python src/clone_account.py --phase apply ... --probe-only --commit          # one create + read-back per unverified route
python src/clone_account.py --phase apply ... --move-presets --commit        # re-folder presets a source folder rename left behind
python src/clone_account.py --phase verify --blueprint <bp> --target you@x.com --jwt <paste>   # read-only parity + counts
python tests/test_account_blueprint.py                                       # 35 checks, zero network
```

**What the live export holds (2026-09-10, 250 calls, 149s, every family under the Open API
key):** 20 preset folders / 98 presets (folders 01-06, 09-21 plus `default`, which is empty;
07/08 Tier 2 are gone), 27 sequences in 5 folders, 13 task presets in 3 groups, 37 custom
fields in 3 groups, 7 boards with their columns, 37 SiftMap auto-add presets, 56 lists, 30
statuses (4 custom), and 21 of 236 tags. Exit 0, one `$unresolved` reference (a deleted column
inside the already-inactive "Send to Deep Prospecting" sequence).

**The Api-Key reads EVERYTHING.** Verified against ty+2 for statuses, lists, tags, users,
custom fields and groups, task groups and presets, boards and columns, sequences and folders,
preset folders and presets, and `map.reisift.io/filters/`. The staff-JWT fallback in
`export._family` fired exactly once across two exports: `map.reisift.io/filters/` answered 401
to the Api-Key on the second run and 200 on the first, so keep the fallback. Shapes that differ from the docs:
`/api/internal/account/user/` returns a FLAT ARRAY (the old mirror's `_probe_map` did
`r.get("results")` on it, swallowed the AttributeError, and shipped an EMPTY user index, which
is why every ty+1 caller queue pointed at the wrong human); custom-field rows DO carry
`group {id,label}`; task presets carry `assigned_to_user` as an object; columns are
`{uuid,title,order}`; SiftMap rows carry `is_active:false` for soft-deleted filters.

**A saved preset and a records search speak different shapes.** The saved `must` stores relative
windows (`last_direct_mailed: ["36-months","month"]`, `last_updated_date`) that
`POST /api/internal/property/` refuses with "Use YYYY-MM-DD"; the UI resolves them at query
time. `count_shape()` drops those keys and flattens `ownerPropertiesOwned.show_properties`,
and any count without them is labelled "date window ignored". 26 of 98 presets are affected.

**Blueprint rules, each one a bug the old mirror had or would have had:**
- Every cross-reference is `{"$ref": kind, "title": ...}` (column refs carry `board`, task-preset
  refs carry `group`). The ONLY uuid in the file is `source.account_uuid`, kept so apply can
  refuse to clone an account onto itself. `validate()` regex-scans the dump for uuids, phone
  numbers and emails and REFUSES the file on any hit: ty+2's "Call New Lead" sequence carries a
  real phone, a real email and an integration uuid.
- Export is key-driven THEN sniffed: known keys (`any_lists`, `any_tags`, `any_boards`,
  `assigned_to`, `has_all tags_uuid`, `from_to column`, `task_preset`, `property-assign`) are
  translated by meaning, then every remaining uuid-shaped string becomes a `$ref` or an
  `$unresolved` marker. A field the hint table has never seen cannot smuggle a ty+2 uuid out.
  The `create-task` payload key literally named `uuid` holds a timestamp, so matching is by
  `UUID_RE`, never by key name.
- `send-sms` / `send-email` actions are replaced by a `$manual` marker; apply drops them and
  creates the sequence INACTIVE with a TODO. Absolute `end_of_day` timestamps are dropped
  (the one on ty+2 was six months stale). `remove` actions name lists/tags by TITLE, and 91 of
  the 114 tags in "Sold Property Cleanup V2" are cohort junk, so they are filtered to what the
  blueprint carries.
- Tags are pruned to those referenced by presets, sequences and SiftMap presets plus an anchor
  allowlist; 134 of ty+2's tags are cohort/import junk (`2026-W27`, `pulled_*`, `DataFlik_*`,
  the comma-string tag). A junk-shaped tag in a blueprint fails validation.
- Duplicated uuids in `must_not.any_lists` are deduped; the uuid-titled junk list is DROPPED,
  not remapped to "Auction" as the ty+1 build guessed. Users ship as first name + role only.

**Apply rules:**
- `verify_target` (exit 3, a `SystemExit` subclass no per-item except can swallow): token email
  == `--target`, account != blueprint source (ty+2 has 11 users, so a different email is NOT a
  different account), 30 min left, no `staff` flag unless `--allow-staff-target`, impersonated
  tokens must carry the flag and differ from the staff account, a live sequence read must
  return the claims account, Api-Key tokens refused (no claims). Verified live: the gate
  refused ty+2 as its own target; with `--allow-same-account` a dry run indexed all nine
  families of the live account with zero writes.
- Order: statuses, lists, tags, custom fields, task presets, boards (RESOLVE ONLY, never
  created), presets, sequences, SiftMap. Every listing passes `?limit=999`/`10000`; a 429 on a
  listing RAISES instead of reading as "nothing here" (which is what re-creates lists, tags and
  sequences, none of which have a unique-title constraint). A network error after a POST
  re-lists by title before any retry.
- Presets: exact `filters.must` equality on read-back (`_mk_preset` rule), `filters.account`
  rewritten to the target, `must_not.any_neighborhood` stripped by default (69 presets carry
  15 Knox strings; `--keep-neighborhoods`), a global title index across ALL folders because
  titles are account-unique, and **a preset with ANY dropped reference is not created**: Ready to
  Call minus its Priority 1 gate is every record with a phone, a different population wearing
  the same name. Unresolvable `assigned_to` (the 6 CALLER QUEUES presets) maps via
  `--user-map "Adriana=Jane"` or falls back to the applying user as a logged placeholder + TODO.
- Sequences: a missing TRIGGER reference (board/column) skips the sequence; a missing ACTION
  reference skips it too unless `--stub-inactive`, which creates it inactive minus that action.
  Read-back is sent-subset-of-got on trigger/conditions/actions/is_active.
- SiftMap presets are created with `auto_add_enabled` FORCED OFF unless `--siftmap-auto-add`,
  because auto-add spends the target's record allowance the moment it exists, and the 37 carry
  Knox/Blount addresses.
- Statuses: custom only, full object with `color`; system statuses missing in the target are a
  gap. Casefold matches (`Cold Lead` vs `cold lead`) map instead of creating and rewrite the
  preset strings.
- Unverified create routes (`task-group`, `task-preset`, `sequence-folder`, `custom-fields/group`)
  go through `probe.create`: the FIRST real object is POSTed and read back before the batch
  continues; 404/405 marks the family manual (every payload lands in the report), 400 stops it
  with the body verbatim. No sentinel objects, since the DELETE routes are equally unverified.
  `--probe-only --commit` does one create per route and stops.
- Exit codes: 0 verified (gaps reported), 1 invalid blueprint, 2 any read-back mismatch (the run
  continues so the report is complete), 3 refused. Report `.md` + `.json` per run, state file
  keyed by title with the target account pinned, resume adopts whatever already landed.

**LIVE ON ty+1 (2026-09-10, fresh ty+1 paste, `--allow-staff-target --move-presets
--skip siftmap`).** `plan` -> `--probe-only --commit` -> `--commit` (three passes, each resuming
what the previous one landed) -> `verify`, final exit 0. ty+1 now reads back at parity with the
blueprint on every applied family: 56 lists, 21 tags, 37 custom fields (33 created), 13 task
presets (all created), 7 boards (Deep Prospecting created plus 12 columns), 98 presets (14
created, 12 MOVED out of the August "05/06. TIER 1 - FTM" folders into "05/06. FTM"), 26 of 27
sequences (24 created; the missing one is "Send to Deep Prospecting", inactive at the source
and pointing at a deleted column). SiftMap was skipped on purpose: ty+1 is a Franklin OH
account and 37 Knox feeders there would be clutter. **Every unverified create route answered:**
`POST /custom-fields/group/`, `/task-group/`, `/task-group/{g}/task-preset/`,
`/sequence-folder/`, `/siftline/board/`, `/siftline/board/{b}/column/` all create and read back.

**Four server rules the live run taught, each now in code and in a test:**
- **A task preset takes EXACTLY ONE assignee key.** `assigned_to_user`, `assigned_to_users`,
  `assigned_to_role`: the other two must be ABSENT. An empty list 400s ("This list may not be
  empty"), two non-null keys 400 ("can't be not null together"). The GET shape shows all three,
  which is what misled the first payload.
- **`create-task-by-preset` REQUIRES `end_of_day` on create** even though the source GET carries
  it on only one sequence. It is the UI's 23:59:59.999 of today in the action's timezone, in
  UTC; `translate.end_of_day_iso()` reproduces ty+2's stored value to the millisecond, and
  apply always computes a fresh one rather than copying a stale timestamp.
- **List titles are unique per account ignoring case and surrounding whitespace.** ty+2 holds
  "Arrests " with a trailing space; the create 400'd against ty+1's "Arrests". Export strips
  titles and apply adopts the target's form (`Registry._same`). Tags keep case significant
  because ty+2 legitimately holds both "Foreclosure" and "foreclosure".
- **Board column titles are unique the same way.** "Send Back to Lead Management" collided with
  ty+1's "Send Back To Lead Management"; `Registry.column_uuid()` matches case-insensitively.

**Dry-run honesty needed its own fix.** Containers created in the dry pass (task groups,
sequence folders, custom-field groups, boards, columns, task presets) were not registered, so
the first plan reported 13 task presets and 20 sequences as gaps the real run would create. A
dry create now registers a `DRY-RUN-*` uuid so the plan is the plan.

**What the verify count probe says about ty+1, and why it is right:** 12 presets match records,
86 match zero, and 56 of those 86 are "gated on empty tag Priority 1 / Priority 2 / FTM".
ty+1 never ran `priority_tags.py` and its SiftMap feeders tag nothing, so the whole call/mail
system sits on tags no record carries. The structure is cloned; the tagging that feeds it is a
separate job, which is exactly what the probe was built to say out loud.

## Dispo Buyer Engine - Pending Flips (build 1.0.46, 2026-08-20)

`src/dispo_flip_buyers.py` turns SiftMap's investor-transaction data into a live dispo buyer machine on ty+2: properties whose last sale is an ACTIVE investor purchase are owned right now by a buyer, so pulling them into the CRM makes the sequential call/text flows dial people who provably buy in this market. Phases `infra/pull/trace/flippers`, DRY by default + `--commit`, resumable state `output/dispo_trace_state.json`.

```bash
python src/dispo_flip_buyers.py --phase infra --commit     # list + tags + presets (idempotent)
python src/dispo_flip_buyers.py --phase pull --commit      # bulk add pending flips
python src/dispo_flip_buyers.py --phase trace --limit 25 --commit   # principal skip trace
python src/dispo_flip_buyers.py --phase flippers --limit 300        # exited-flipper CSV (free)
```

**THE SEMANTIC TRAP: the filter key labels the LAST SALE, not the current owner.** The SiftMap key is `extra_last_sale_investor_transaction_type` (takes a LIST - a comma string 400s; options pending/wholesale/wholetail/flip/rental, dug out of the live SiftMapPage bundle, webpack module 47865 `FINANCIAL_DETAILS`). Verified on live Knox samples: **"pending" = investor bought, exit pending, current owner IS the investor** (Volhomes LLC, Weaver Doors LLC) - this is the bucket that becomes CRM records. **"flip" = the flip EXIT was the last sale, current owner is the RETAIL homebuyer** (Christine Korf bought from GDP Properties LLC) - texting those records reaches ordinary homeowners. The flipper there is the last-sale SELLER, so the flip bucket feeds `--phase flippers` (ranked exited-flipper CSV, recency-capped `--months 24`) and never a record pull. Every candidate key guess before the bundle dig was SILENTLY ignored (count identical to baseline) - probe by count delta, never trust acceptance.

**What is live in ty+2 (2026-08-20):** list "Dispo - Flip Buyers" (2,082 records landed: Knox 1,654 + Blount 430 pending, 335 new to account, rest were existing acquisition leads that got list-attached); SiftMap auto-add presets "Dispo - Pending Flips - Knox/Blount" (ids 9317/9318, auto_add ON, replace_owners OFF) so new pending flips keep flowing; CRM folder "21. Dispo Sequential Marketing" with 7 presets mirroring the P1 system (see below); tags "Dispo Buyer" + "Dispo Traced" only.

**PRESETS MIRROR THE PRIORITY 1 SYSTEM - COUNTER-DRIVEN, NO FLOW TAGS (Ty).** The first build used a tag-gated pendulum (`dispo_sms_sent` -> `dispo_called_dayN`); Ty rejected it: no underscore tags, mirror P1 exactly. P1's real grammar (read from the live "Hottest - *" presets): progression rides the DIALER'S OWN COUNTER - `predictivecall_attempts [0,0]` is Ready to Call, `[1,1]` is Call Attempt 1 - plus `skiptraced`/`phone` flags; the only tags are plain-name anchors ("Priority 1", "FTM") and must_not ("recently sold", "Mail Only"). The dispo mirror: `Dispo - 00 Needs Skipped / 01 Skipped No Numbers / 02 Ready to Text / 03 Ready to Call / 04-06 Call Attempt 1-3`, anchored on the VIP list, text and call as PARALLEL lanes (like P1's CALL and MAIL folders), text lane on `sms_attempts [0,0]` (a live counter: 415 account records carry sends), must_not "recently sold" tag + sold/not_interested status. Live counts at build: 01 = 24 (skiptraced, no numbers -> entity-trace candidates), 02 = 208, 03 = 208. A preset that gates calling behind a texted-tag shows 0 forever and reads as broken - that was the "Ready to Call is empty" bug. Tag-removal contract: `POST /api/internal/property/{uuid}/remove-tags/` takes TITLES (`{"tags": ["name"]}`); uuids return 200 with `removed_tags: []` (silent no-op). `DELETE /api/internal/tag/{uuid}/` 400s (empty body) while the tag INDEX still shows attached records - the index lags the detail truth by ~30s; wait and retry.

**The trace phase writes the BUYER's contact onto each record without touching owners.** Most pending records were already in the account as old acquisition leads, so their CRM owner is the PREVIOUS seller (PR/heir data included) - the pull deliberately keeps `replace_owners: False` and trace resolves the real buyer per record instead: SiftMap detail -> current deed owner -> if entity, reverse the mailing address through SiftMap owner_info (the Harper move, FREE - live hit: Bella Vista Customs LLC -> Paul S Blake) -> Enformion BusinessV2 officers on a miss -> person search phones anchored at the principal's mailing city -> `POST /api/internal/owner/{uuid}/upsert-phones/` (phones tagged `dispo`, merge semantics preserve existing numbers) + provenance note + `dispo_traced` tag via address-upsert (tags accumulate, owner key omitted = owner untouched). Verified live read-back on 1713 Rambling Rd: Pittman Properties Knoxville -> BusinessV2 -> Bradley C Sagraves -> 6 phones on the record. Person-owned records are skipped by default (`--all-owners` overrides) because DataSift's native unlimited skip trace covers humans; the entity principal is the part it cannot do.

**Contracts learned here:** map `GET /filters/` pages at 10 rows and IGNORES `?limit=` - follow `next` or the exists-check lies and re-creates (live duplicate id 9319, deleted); `DELETE /filters/{id}/` works (204). SiftMap search rows carry the full address as ONE string (`address`); the detail record has NO street field at all - join on the row. `owner_info` mailing is the single string `owner_mail_address`. The records search is POST `/api/internal/property/` with `x-http-method-override: GET` - a bare POST would try to CREATE a property. `/properties/search/` response rows live under `data`. Record allowance check: `POST /api/internal/upload/usage/`.

**VIP layer (`--phase vip`, Ty 2026-08-20): the campaign runs ONLY on never-contacted buyers.** Ty's read was correct and measured: **89% of the pending-flip owners were already in the marketing ecosystem** - of 2,082, only **234 qualified as VIP** (excl. reasons overlap: 1,618 carry a lead status, 1,294 owner-name-match a contacted record elsewhere in the account - the tired-landlord-owns-a-new-flip case, 406 sit on marketing lists, 14 have dial/SMS attempts). The gate per record detail: zero attempts on ALL four channels (`predictivecall_attempts`/`sms_attempts`/`rvm_attempts`/`directmail_attempts` all live on the detail), no status EVER (even sold), no outreach tags, no list beyond `CLASSIFICATION_LISTS` (DataSift auto-attaches classification lists at ingest - a brand-new record arrived already on "Absentee Owners", so bare list-membership is NOT outreach evidence), and the owner-name cross-check against 26,134 contacted-owner names (all marketing-list members + every record with a status or mail attempts). Output list **"Dispo - VIP Buyers" (234, membership verified exactly; the `count` field lags - trust the page sweep)**; the 5 sequential presets were re-pointed at it (read-back verified) and the folder renamed **"21. Dispo Sequential Marketing"** so it sorts last, after 16. Acquisitions / 17. Transactions. "Dispo - Flip Buyers" (2,082) stays as the raw intelligence layer. Re-run `--phase vip` after auto-add feeds new records - verdicts and the contacted-owner set checkpoint to `output/dispo_vip_state.json` (delete `contacted_owners` there to refresh the name set). Records-search gotchas hit here: empty `must` 400s ("Filter can't be empty" - query the worked populations directly), and pagination hard-refuses offset+limit > 10,000 ("Can't fetch more than 10000 items!" - cap and log the truncation).

## Dispo Buyer Registry + Deal-Blast SMS (build 1.0.49, 2026-08-28)

The dispo side gets what acquisitions already had: a deduped list of PEOPLE with scored phones, and an automated program that texts them. `src/dispo_buyers.py` builds the registry (phases `sweep / hydrate / aggregate / unmask / principals / skipinput / score / qa`, DRY by default, resumable state in `output/dispo_buyers/`). `src/sms_agent/dispo_campaign.py` stages the blast, modeled on `heir_campaign.py`: it reuses `seed.build()` for every guard, swaps in deal wording, and re-validates under a new buyer profile.

**The message shape is Ty's (2026-08-28): the ROAD NAME and the AREA, never the exact address, plus the PRICE.** Interest converts to a phone call with a human; the agent never negotiates and never sends the address. That one sentence drove the whole design, and the deal sheet enforces it structurally: `load_deal()` REFUSES a `road` starting with a house number or a zip anywhere, so the address cannot leak because it never enters the process.

**Filter contract, all verified live by count delta before any code was written.** `search` in the `addresses[]` block must be the bare county name: "Knox" returns 153,675, "Knox County, TN" returns **0**, which is indistinguishable from an empty segment. **An unknown filter key is SILENTLY IGNORED** (a deliberately bogus key returned a byte-identical count), so acceptance proves nothing. `extra_collapse_by_owner` is real and is the lever that makes the sweep cheap (Knox pending 1,633 -> 1,140 rows). Search rows carry **no owner name**, so hydration is unavoidable; they DO carry `saved_uuid`, the CRM record id. **`sale_history` is ordered NEWEST FIRST** (18 of 18 multi-sale properties strictly descending) and that ordering is load-bearing, because the flip branch reads `sale_history[0].seller_name`.

**The semantic trap, restated because it is the one that ruins the list.** `pending`/`wholesale`/`wholetail`/`rental` mean the investor still holds, so the CURRENT OWNER is the buyer. `flip` means the exit already happened, so the current owner is a RETAIL HOMEBUYER and the person we want is the last-sale SELLER. Hold types are swept collapsed; exit types must NOT collapse, because each sale is needed to read its seller.

**Dedupe is mailing-address clustering WITH a suite guard, and both halves are measured.** Clustering catches what name-matching misses (`DEVELOPERS TEAM 1 LLC` vs `DEVELOPERS TEAM I LLC`, digit vs letter; `GDP PROPERTIES LLC` vs `GDP PROPERTIES LLC PRO SOURCE HOME BUYERS`). Unguarded it over-merges: `9111 Cross Park Dr Ste D200` hosts NS Homes LLC and R D Properties Group LLC, unrelated companies sharing an agent. So an address carrying `STE|SUITE|UNIT|APT|BLDG|FL|PMB|RM` requires a fuzzy name match too, and every merge and every refusal is written to `merge_report.json` with its reason. Normalizers are IMPORTED from `enterprise_prospects` (`norm_name`, `strip_unit`, `ORG_RX`, `classify_one`) rather than rewritten; three incompatible ones already existed.

**Cost order: the free step runs first.** `unmask` reverses each LLC's mailing address through SiftMap (the Harper move) at $0. Only what that misses is worth Enformion BusinessV2 at $0.10 (`principals`, gated on `--commit`). `skipinput` then projects the registry into a SmartSkip upload, whose required columns (First Name, Last Name, Mailing Address) are the registry's natural key. **SmartSkip is free up to `pay`**, so the quote is read before spending.

**TWO SILENT TRAPS FOUND IN THE SCORE PHASE, both would have wasted money or corrupted records.**
- **The do-not-call flag lives ONLY on the search row.** A number returned as `doNotCall: true` by `/property/` search comes back from `/property/{uuid}/` with the field **absent**, not false. A DNC filter written against the full record can never fire, so it would pay Trestle to score numbers the seeder then refuses to text. The DNC set is built from the search rows; coverage is partial by construction, since the search returns one representative phone per record.
- **`crm.get_record` swallows a 429**, logs a warning and returns nothing, which silently drops that record's entire phone list while the run still reports success. `_get_record()` retries with backoff, and a gate fails the phase if too many stay unreadable. The fix recovered 23 phones on a 165-record cohort.
- Also caught by its own read-back test: adding a tier with a set union left a re-scored phone carrying **two** tier tags at once, matching two lanes. Other tiers are stripped before the new one is added.

**THE ENTERPRISE TAXONOMY EXCLUDES THE BEST DISPO BUYERS WE HAVE, so `local_tier()` wraps it.** `classify_one` was calibrated for a nationwide enterprise list where volume means 6-month purchases across the country and "construction" means D R Horton. On a county sweep it is wrong twice, both measured on the real registry: (1) below its volume floor of 10 any generic keyword is an outright EXCLUDE, and nearly every local investor is below that floor, so CREEKSTONE CONSTRUCTION LLC (9 buys), MCCARLEY CONSTRUCTION LLC (6) and HEMBREE BUILDERS LLC were all dropped as "homebuilders" when a local construction LLC buying 3 to 9 houses a year is a **self-performer**, the exact buyer this team's own dispo doctrine says takes a heavy-rehab deal when GC-model flippers cannot; (2) it matches keywords as SUBSTRINGS, so "BANK" inside "WILLBANKS" excluded a real buyer with 4 purchases. A generic match becomes REVIEW (kept, flagged) and any keyword failing a word-boundary re-check is a false positive. Exclusions fell from 177 to **25**, recovering 152 buyers; the 25 that remain are right (D R Horton, Clayton, Habitat for Humanity, City of Knoxville, Town of Farragut).

**Live results (ty+2, 2026-08-28), full run.** Sweep **6,508 unique properties** (Knox pending 1,140 / wholesale 33 / wholetail 159 / rental 1,930 / flip 2,013; Blount 352 / 4 / 41 / 512 / 324), hydrated 6,501 with an owner and 2 errors at ~2 req/s. Aggregate: **4,286 unique buyers** from 6,501 observations, 2,985 entities and 1,301 people, 3,513 with a mailing address, 135 buying in both counties, 277 merged by mailing address and 61 refused by the suite guard. Unmask resolved **1,173 principals for $0** (a 40% hit rate on entities, about $117 of Enformion calls avoided), leaving 1,250 entities for the paid pass. `skipinput` projects **1,916 people** ready for SmartSkip (1,052 owners plus 864 resolved principals). The output validates against known ground truth: it independently rediscovered **Jonathan Harper -> TN Super Props 1 LLC**, the principal the original 158 Old State buyer sweep found by hand.

Hydration hung once on compounding 429 backoff and had to be restarted; it is resumable and the checkpoint interval was cut to 100 so a stall costs less. Do not raise worker count to fix slowness, the detail cap is aggregate.

**Contact resolution, and the four bugs the spot-checks caught.** The paid Enformion pass runs behind `--min-buys` (default 2), because of the 1,257 unresolved entities that still carry a mailing address only 264 bought more than once: $26.40 instead of $125.70 for the part of the list that matters. It resolved **193 of 264**. Then the read-back found four things that would each have poisoned the output:
- **THE REGISTERED AGENT IS USUALLY THE COMPANY'S LAWYER.** 135 of 193 officers came back titled AGENT or REGISTERED AGENT, and texting a firm's attorney a deal blast is precisely the litigation bait this program avoids. But an agent title is not automatically wrong, since plenty of small operators are their own agent. `principal_confidence()` trusts an ownership title outright, trusts an agent title only when a name token also appears in the COMPANY name ("Turner Homes LLC -> Michael L Turner", "Smithbilt LLC -> Smith Kenneth" are obviously the owner; "New Season Properties LLC -> Dryer and Associates" is obviously not), and holds the other 122 back from the text list.
- **Local law firms wear person-shaped names.** The stock `AGENT_FRONTS` list only knows the national commercial agents, so `DRYER AND ASSOCIATES P C`, `CHAMBLISS BAHNER & STOPHEL PC` and `BENNETT LAW OFFICE PC` all arrived as "principals". `FIRM_RX` catches them. A first version matching a bare `&` was far too blunt: it threw away 32 real married couples ("Oneal Brian A & Ethel L") to catch 5 firms, so the rule now needs an explicit professional marker.
- **County records write LAST FIRST.** `clean_owner_name` read "KNOX JAMES" as first=Knox last=James. When the person came from an entity there is a free oracle: whichever of their tokens also appears in the company name is the surname. 45 rows corrected. Two portmanteau cases (Smithbilt) stay wrong and are left alone, because the prefix rule that would fix them also breaks "Aprilflipstn Llc -> April Marsh", where April really is her first name.
- **A legal-status token is not a surname.** "Morales Family Trust" resolved to first=Morales last=Tr. 85 such rows dropped.

**SmartSkip wants the STREET, not the whole address line.** SiftMap returns the mailing address as one string, and the first submission mapped that entire string to `mailingAddress` with no city, state or zip, which would have paid full price for a degraded match. `split_mail()` splits it and the re-submission mapped all six fields. Always check the `mapped` block the submit call echoes back.

**Skip trace results (2026-08-28).** Ty set the trace floor at 2+ purchases as well, so 294 people went instead of 1,892: **$44.10 ceiling, billed on 206 actual hits.** 203 returned results (69%), 882 phones and 2,560 relatives, and the join back to `buyer_key` matched **293 of 293** through the input CSV as the bridge. Trestle scored 853 numbers for ~$13 with zero errors: 365 Dial First, 53 Dial Second, and **181 of 204 traced buyers (89%) carry at least one Dial First or Second number**. 6 records carry SmartSkip's deceased flag, which is unreliable and is stored as something for a human to verify, never acted on.

**Scoring, and the reason the campaign was returning nothing.** On the existing `Dispo - 02 Ready to Text` cohort: 227 numbers scored for **$3.40**, 221 tier tags written, Dial First 151 / Second 14 / Third 19 / Fourth 25 / Drop 18. Before that, **exactly 1 of 165 records carried a dial tier**, because `dispo_flip_buyers --phase trace` writes phones tagged `dispo` and never scores them while `seed.from_preset` gates on the literal strings "Dial First"/"Dial Second". An unscored cohort is an empty cohort and looks exactly like a preset that matched nobody. After scoring, a dry blast staged **44 ready messages** from 63 mobile rows.

**67% OF THAT COHORT IS FLAGGED DO-NOT-CALL** (110 of 165, same in both lanes). The flag is DataSift's own registry scrub (`source: REISIFT`) on skip-traced numbers, not a user action. It is treated as a hard block, which is conservative and shrinks the reachable universe a lot. Whether B2B deal distribution to an investor is exempt is Ty's call, not a code default.

**The SMS side is a separate program, not a flag.** `respond.validate(program=)` gains a buyer profile where three rules invert: money is allowed **but only a figure on the approved deal sheet** (`allowed_prices`, so an invented price is still blocked and "92k" normalizes to 92000), distress vocabulary is permitted (it is ordinary deal description when selling TO an investor), and the zip check runs on the text with approved prices removed so a legitimate $92,000 does not read as a 5-digit zip. `knowledge.playbook(program)` reads a SUBFOLDER (`knowledge/dispo/playbook.md`) and REFUSES an empty prompt, because a model handed no identity invents one. `touches.render_deal()` fills `{road} {area} {price}`, deliberately a different function from `render()` so a caller cannot pass a full street address where a road name belongs. Selftest is **167 checks**, up from 143; the new ones assert that the approved price passes, an invented one is blocked, and the seller profile still blocks every price.

```bash
python src/dispo_buyers.py --phase sweep
python src/dispo_buyers.py --phase hydrate --workers 4      # ~2.9 req/s, resumable
python src/dispo_buyers.py --phase aggregate
python src/dispo_buyers.py --phase unmask                   # FREE principal resolution
python src/dispo_buyers.py --phase score --commit           # Trestle + dial tiers
python src/sms_agent/dispo_campaign.py --deal deals/<id>.json          # dry
python src/sms_agent/dispo_campaign.py --deal deals/<id>.json --queue  # stage HELD
```

**Buyer profiles (`--phase profiles`, 2026-08-28).** Per-buyer answer to "what does this person actually buy", for the **698 buyers with 2 or more purchases** (the other 3,588 bought once, where a buy box is a guess wearing a range). Output `buyer_profiles.json` plus a 5-sheet workbook (Overview / Buyer Profiles / Call List / By Type / Sources and Caveats). Types: 253 active holders, 219 exited flippers, 111 landlords, 34 self-performers, 20 wholesalers, 10 institutional, 10 not-a-target, and **181 reachable right now**. The one-line narrative per buyer is GENERATED, not modelled: 698 rows of arithmetic is a poor use of a model, and a model asked to summarise a buyer will eventually invent a detail a caller then repeats to that buyer on the phone.

**THE SWEEP HAS NO DATE BOUND, and the profiles are what exposed it.** `extra_last_sale_investor_transaction_type` selects properties whose LAST SALE was an investor transaction, with no time filter, so the observed purchases run **2019 to 2026**, not the 12 months the first narrative template claimed. Measured: only **41% of these buyers bought within the last year** and the **median last purchase is 456 days old**. Every profile now states its own first-to-last year span and carries `active_365d`, `days_since_last` and a `buys_per_year` computed over that buyer's real span. Half this list is dormant, and a deal blast that ignores recency is texting people who stopped buying three years ago.

Three more data traps fixed here, all found by reading the output rather than the code. **A house does not sell for $3,000:** 13 buyers had price bands anchored on $1,000 to $4,594 quitclaims, so `NOMINAL_SALE_FLOOR` (under $10K, or under 20% of the property's value) drops 89 nominal transfers from the bands while keeping them in the purchase count. **Bands are p10 to p90, never min to max**, and a buyer with fewer than 3 priced sales gets a single figure marked thin rather than a fake range. **`_is_human` now requires two tokens and rejects placeholders:** SiftMap returned the literal string `UNKNOWN` as one LLC's mailing-address owner and it passed every entity and firm check, and 301 more "principals" were bare surnames like "Martin" that would have greeted someone by their last name. 305 cleared in total. Also `phase_sweep` originally persisted 8 of ~35 fields and discarded exactly the buy-box dimensions; it now keeps beds, baths, sqft, lot, equity and the investor scores (92 to 100% populated). `yearBuilt` is NOT returned by this endpoint at all, so there is no property-age dimension, and `propertyUse` is constant because the sweep filters single-family.

**RECENCY IS NOW THE GATE (2026-08-28), because the sweep never had a date bound.** `phase_recent` re-runs the same sweep with `extra_last_sale_date_min` (verified by count delta: Knox 6,912 unbounded drops to 1,151 at 12 months while a bogus key returns the unfiltered 6,912) and becomes the qualification layer, while the unbounded sweep stays the history layer. Recency decides WHO is on the list; full history decides WHAT they buy. Cheap by construction: 1,129 properties in the 12-month window and 1,101 were already hydrated, so 28 detail calls, not another 75-minute run. It also surfaced **36 currently-active buyers who were not in the registry at all**, which no amount of re-reading cached data would have found. Cohort is now **active in the last 12 months AND 2+ lifetime purchases = 276 buyers** (154 hot within 182 days, 84 reachable), down from 698. 424 were dropped as no longer buying. Price bands prefer the last 24 months and widen to full history only when that leaves under 3 priced sales, recording `price_basis` on the row (169 recent, 101 widened, 6 with no priced sale) rather than hiding the widen.

**The blast copy, and the five bugs the first live render surfaced (2026-08-31).** Copy is Ty's own text voice: no "Ty here" opener, a bare `-Ty` sign-off, "I saw you were a potential buyer" rather than "I buy and sell a few houses", and the video offered ("if you're interested I can send over the walkthrough video") rather than demanded. Entities get Ty's transparency parenthetical, "Hi Smithbilt team (sorry, couldn't find the signing member)". **The location named is each buyer's OWN city from their deed history**, never the subject's, because we do not actually know the deal is in East Knoxville and claiming it is a lie a buyer can catch. Five bugs, none of which a spot check would have found:
- **"Real Estate" was read as a trust.** `ESTATE` sat in `_TRUST_TOKENS`, so "Affordable Houses and Real Estate" silently lost both its "team" and its transparency note. ESTATE now counts only when it ENDS the name and is not preceded by REAL, which still catches the "Willis Ailene B Life Est" case it was added for.
- **An unknown buyer rendered "Hi , I saw...".** `buyer_greeting("", "")` returned `" team"` and every variant opens `Hi {who},`. It now returns nothing and `render_deal` collapses the greeting to a plain "Hi,".
- **A mailing street carrying a zip held candidates for copy we throw away.** `seed.build` renders SELLER copy first and validates it under the seller profile, which blocks any 5-digit run; the buyer copy that actually ships is rendered afterwards. Two real buyers were held for a zip in a message that never existed.
- **429s and 502s were read as missing data.** The live client is the shared Deal Room `crm_api.CRMClient`, not `crm_standalone`, and it raises on both. 15 buyers were held as "could not read dial tier from the CRM", which is indistinguishable from an untagged record. Retry now lives at the SiftStack boundary (`crm._transient_wait`), honours the server's own "available in N seconds" hint, and covers whichever client is underneath since we do not own the shared one. A 404 or 400 is still a real answer and is not retried.
- **The selftest was calling a signature that no longer existed** and aborted the suite with a TypeError instead of failing a check. `render_deal`'s parameter list and `BUYER_POOLS`' shape are now asserted directly.

**THE PRICE BAND IS ASYMMETRIC, because the two directions are different constraints.** Above a buyer's band is AFFORDABILITY: a buyer whose ceiling is $120K cannot close $600K. Below it is only INTEREST: a buyer whose cheapest purchase was $140K can obviously afford a $75K house, they may just not want one that small. A symmetric 0.35 tolerance kept **68 of 199** at a $75,000 ask and dropped buyers purely for being too big, which is the wrong reason to skip anyone. The floor is now divided by `--band-floor-mult` (4x) while the ceiling keeps the tolerance, taking the cohort to **159**. Measured on this cohort at $75K: only 32 buyers have a band that covers the ask outright and **162 have never bought that cheap**, so this deal sits below the market these investors normally play in and the floor rule is doing real work.

**The copy audit runs over EVERY message, not a sample.** Variants are chosen by a hash of the record, so the four messages a sample prints are not the four that ship; three variants once shipped unsigned and only two surfaced in a sample. `dispo_campaign.audit()` asserts on all of them: signs off `-Ty`, never contains the contract price in any spelling ($65,000 / 65,000 / 65k), no em or en dash, no link, no zip, at most one question, under 320 characters, and **names a city that buyer has actually closed in**. A failing audit refuses to stage (exit 3) rather than printing a warning above a staged batch.

**"No institutional buyers" cannot be a keyword rule.** Scanning the 193-buyer cohort for HOMES / BUILDERS / CONSTRUCTION / DEVELOPMENT flags 24 names, and reading their data shows 23 are small local operators: portfolios of 1 to 11 doors on 2 to 18 purchases. A self-performing local builder is the single BEST buyer for a full gut at $75K, so the keyword sweep would delete the target audience in order to catch one name. Exactly the same failure as the earlier `BANK` matching inside `WILLBANKS`. `NAMED_EXCLUSIONS` is instead a list of firms verifiable as production builders, iBuyers or SFR funds (Ball Homes, Clayton, D.R. Horton, Lennar, Opendoor, Offerpad, HomeVestors, Progress, Tricon, Amherst, FirstKey, Truehold and the alias forms from the enterprise-prospects map), each drop logged with the name it matched. On this cohort it removes exactly one buyer, Ball Homes, and keeps all 21 local builders.

**HOW A BUYER IS ADDRESSED went wrong four separate ways, and the copy audit found all of them because it reads every message.** Each came from one real row: a BARE INITIAL is not a first name ("E J E Bourgeois" texted "Hi E,"); `clean_first` is a PERSON rule and on "J A Murphy Group Llc" it reads the surname slot as "Llc" and returns "Murphy", greeting a company as a man; COUNTY RECORDS WRITE LAST FIRST, so "Haddad Amer Michael" is Amer Michael Haddad and token zero is the surname; and a person we cannot name must get NO addressee rather than falling through to the entity branch, which produced "Hi Haddad Amer Michael team," and "Hi William David Faulkner Sr team,". The entity path has an oracle for the last-first problem (the company name) and a bare person name has none, so a person name of 3 or more tokens is treated as ambiguous and gets a plain "Hi,". That costs a correct "William" on one row to avoid a wrong "Haddad" on another, which is the right trade in a cold text: no name reads fine, the wrong name reads like a list. The registry's own `is_entity` also missed "Maker Building Co", so `_entity` is the OR of that flag and `touches.is_entity`, and it now decides whether a buyer can be called a team at all. Final split on 192: 188 entities, 2 first names, 2 with no addressee.

**THE SENDER IS FORCED, not defaulted.** `seed._resolve_sender` reads the record's CRM assignee FIRST and treats `SMS_AGENT_SENDER_NAME` as a mere fallback, so the single record in 156 that carried an assignee signed as the acquisitions prospector. On a dispo blast that is wrong twice: it is not Ty's deal voice, and a callback reaches someone who knows nothing about the property. `rows_from_registry(sender=)` pins `assigned` on every row, which uses the existing mechanism instead of special-casing seed.py, and `audit()` checks the sign-off against the chosen sender rather than a hardcoded "-Ty".

**THE DRY RUN WAS WRITING, AND NOTHING STOPPED A SECOND COPY.** Staging the batch twice left **312 held rows for 156 buyers**, and released that would have texted every one of them twice, which is the worst thing a cold number can do. Two independent causes, both silent: `SMS_AGENT_DRY_RUN` never reached `seed.queue` at all (it gates sends and CRM writes, so a dry run reported itself as a dry run while writing the whole batch to the local outbox), and `store.queue_message` is a plain INSERT while the duplicate check in `build()` only sees the CURRENT batch, so it cannot know about one staged an hour earlier. `queue()` now returns early under DRY_RUN and skips any phone that already has a `held` or `queued` row, returning a `duplicates` count so a caller cannot report more than it staged. Both are regression-tested. The selftest itself runs under DRY_RUN, so its staging tests opt out explicitly on their throwaway DB, which is the honest fix rather than weakening the guard.

**Copy says "we have a property on {road}" and names the redaction out loud** (Ty, 2026-08-31). "I've got one on Old State Rd" reads as though that IS the address, so a buyer either believes it or reads us as cagey. Every touch-1 and touch-2 variant now carries an explicit cue ("Holding the exact address for now", "Keeping the full address off text", "Not posting the exact address here"), which tells a real investor there is a real house and that the address comes with the conversation. `audit()` enforces the cue on every rendered message; touch 3 is the breakup and pivots to what else they want, so it carries the road but not the cue.

**Slack routing is per program.** `SMS_AGENT_DISPO_SLACK_WEBHOOK` carries buyer traffic and falls back to the seller webhook with a warning rather than going silent. A single webhook would have put "a price just went out to 156 buyers" in the seller text-leads channel. `escalate._post(program=)` does the routing and the selftest stub records the program, so posting dispo traffic to the seller channel is now a visible regression.

**THE SENDING NUMBERS WERE NOT REAL, AND THE REFRESH THAT WOULD HAVE CAUGHT IT WAS EXCLUDING THEM.** Every dry run and staging pass in this session used five numbers carried forward in a shell variable that **do not exist in the smrtPhone account**; a live pull of the number grid shows 25 numbers and none of them are among the five. The sends would simply have failed. The check that should have caught this had been disabled by accident: `numbers_sync.EXCLUDE_PATTERNS` carried a `dispo` rule from when the only "Dispo" line was Ty's personal test number, so once real dispo numbers were bought the refresh silently dropped the entire pool and reported a healthy "19 numbers", which looks exactly like not having bought them. Dispo is now a pool keyed on the NUMBER'S NAME and checked before the exclusions, because all five sit on a routing flow named "Ty Test" and the generic test rule would drop them. Live refresh: Adriana 19 + Dispo 5. **The live pool lives in `config/sms_numbers.json`, which is gitignored; this file is public, so the DIDs are deliberately not written here.** Re-derive them any time with `cli.py numbers --refresh`, which reads smrtPhone directly and is the only source that cannot go stale.

**THE POOL FELL BACK TO THE SELLER NUMBERS, which is the expensive version of the same class of bug.** `sender_pool.pool()` returns EVERY number in the account when the owner has no pool of their own, and the dispo sender is "Ty", who has none. A staged batch duly spread 156 messages across all 24 numbers including Adriana's 19 acquisitions lines. Three things break at once: the 10DLC per-number cap is counted per database so the seller program's budget is spent invisibly, one number carries two programs, and a buyer who calls back rings the acquisitions flow. `set_forced_pool()` pins a program to one pool and a pinned pool that is missing returns NOTHING rather than falling back, because no numbers is a loud failure and the seller pool is a silent one. Stickiness is also pinned: 89% of these buyers were already in the seller marketing ecosystem, so their sticky number is often Adriana's, and a dispo text signed `-Ty` arriving from the acquisitions number is worse than a number change.

**The dispo numbers ring the "Ty Test" flow BY DESIGN** (Ty, 2026-08-31). It looked like a misconfiguration and was raised as blocking; it is not. Dispo callbacks are meant to reach Ty exclusively, while the prospecting side stays under Adriana. The number pool is grouped by flow for everyone else because the flow is what a callback rings, so the dispo lane is keyed on the number's NAME instead.

**FLY SECRETS OVERRIDE `fly.toml` `[env]`, and reading the file told the opposite of the truth.** `fly.toml` says `SMS_AGENT_DRY_RUN = "1"` and `SMS_AGENT_PHASE = "1"`, and both are also set as SECRETS with the opposite values, so the live box runs `phase=2, dry_run=false` and has been sending for real (1,619 sent, 92 failed, 73 cancelled). `fly config env` shows only the `[env]` block, so it confirms the wrong answer. **Ask the running system: `curl https://siftstack.fly.dev/health` reports the effective phase and dry-run, and `fly ssh console -C "printenv"` shows what the process actually has.** On that box a release SENDS; there is no dry-run gate standing between a staged batch and 156 real texts.

**`SMRTPHONE_NUMBERS` (a secret) also outranks `/data/sms_numbers.json`**, so uploading the file with the new Dispo pool changed nothing and `sender_pool` correctly refused to fall back, reporting "pinned pool 'Dispo' does not exist". The supported path is `cli.py numbers --push | fly secrets import`. Live pools are now Dispo 5 and Adriana 19.

**Caps: dispo runs at 60 per number, acquisitions stays at 25** (Ty: these go to buyers, not sellers, and the whole blast has to land in one day). That is 300/day of dispo capacity against a 156-message blast. `SMS_AGENT_POOL_CAPS` carries it per pool so the two programs never share a carrier-risk setting.

**THE REPLY PATH COULD NOT TELL A BUYER THREAD FROM A SELLER THREAD, so every piece of buyer machinery was unreachable.** `respond.validate(program=)`, the dispo playbook subfolder and `_deal_facts_block` all existed and were tested; NOTHING in the live path passed `program`, and `engine.py` and `worker.py` contained not one reference to it. A buyer answering "how much?" would have been handled by the SELLER playbook ("never name a dollar amount"), validated under the SELLER profile which blocks every dollar figure so the approved asking price could never be quoted, and a hot BUYER lead would have paged the seller Slack channel rather than the dispo one. The signal is **the number the reply arrived ON**: the pools are disjoint by construction, so `sender_pool.program_for(to_number)` decides it with no schema change and nothing to keep in sync, and an unknown number falls back to seller, never buyer. It is threaded to `respond.draft`, the playbook, and `escalate.hot_lead`, which the WORKER posts after a debounce, so the worker re-derives it from the conversation's own `from_number`.

**The responder also had no deal.** `seed.queue` records owner facts only, so the dispo agent's facts block carried no price, road or specs and the price whitelist had nothing to authorize. `dispo_campaign` now writes the deal onto each thread's phone-map context after queueing.

**A KILLED OR DROPPED RUN LEAVES A PARTIAL BATCH.** Stopping the broken band-less run left **34 rows already queued**, precisely the out-of-band buyers it should never have staged, and the Fly outbox read 190 held instead of 156. Separately, `fly ssh console` dropped a 20-minute staging run mid-flight ("remote command exited without exit status"). Stage detached on the box (`nohup ... > /data/stage.log`) and poll the log, and ALWAYS read the held count back before trusting a batch.

**`SMS_AGENT_DRY_RUN` and `SMS_AGENT_PHASE` are APP-WIDE, not per program.** Flipping either to take dispo live also takes the seller agent out of dry run and up a phase in the same step, which is almost certainly not intended and is an argument for a separate Fly app for dispo.

**PRODUCTION IS THE FLY BOX, NOT THIS WORKSTATION, and the blast was staged in the wrong database.** `siftstack` on Fly holds `/data/sms_agent.db` with 1,784 outbox rows, 775 conversations and 3,667 phone maps; the local `output/sms_agent/sms_agent.db` has 609 / 763 / 3,332. Fly is where smrtPhone posts inbound texts. Releasing a locally staged batch would therefore send the texts and then lose every reply: the Fly receiver has no phone map for those buyers, so answers arrive as unknown numbers and never join a thread. Money spent, interest dropped, silently. The campaign inputs (`registry.json`, `buyer_profiles.json`, `phones.json`, `phone_scores.json`, about 5MB) have to live on the volume and the staging has to happen there, so one system owns both sending and replies. Two further gates are deliberate and are Ty's to flip, not code defaults: Fly runs `SMS_AGENT_DRY_RUN=1` (fly.toml says "flip to 0 only after a watched live send") and `SMS_AGENT_PHASE=1`, which classifies replies but never escalates, while the buyer handoff needs phase 2 or higher.

**The launch notice said "is sending" when nothing had been sent.** It fires at STAGE time, so a channel of humans read that 156 texts had gone out. It now reads "STAGED (nothing sent yet)" and names the release step as the only thing that sends. A safety notice that misstates what happened is worse than no notice, because it spends the credibility that makes people read the next one.


**STAGED AND WAITING ON A HUMAN (158 Old State Road, 2026-08-31).** 192 buyers to 158 in band to **156 held in the outbox**, 156 distinct phones so nobody is double-texted, spread 29 to 32 across the five dispo numbers (cap 60). 155 send today between 12:20pm and 5:59pm Eastern; the 156th tipped past the 6pm business cutoff and lands 9:03am tomorrow, which is the window working, not a fault. Verified by reading the DATABASE rather than the render path: every body signs `-Ty`, says "we have a property on Old State Road", carries the address-held-back cue, and quotes $75,000; none contains the $65,000 contract price in any spelling; none carries a link, zip or dash; longest is 308 characters. Nothing has been sent: `release` is the go/no-go and it is Ty's call.

```bash
python src/sms_agent/cli.py release --touch 1     # the irreversible step
```

**The launch notice is not wired yet and that is a real gap, not a cosmetic one.** `notify_staged` posts "a price just went out to 156 people", which is how a wrong road or a wrong figure gets caught in minutes rather than from a reply. The only configured webhook belongs to the seller text-leads channel, so it was suppressed for staging rather than fired at the wrong audience. The dispo channel id alone cannot be used: verified live, the Slack connector available here is installed on a different workspace and returns `channel_not_found` for it, so an incoming webhook URL for that channel is the unblock.





**THE `rental` LABEL LAGS BY OVER A YEAR, and it nearly deleted every landlord.** Measured: `rental` returns 1,930 properties in Knox unbounded, 17 at 18 months and **ZERO at 12 months** (Blount 511 / 4 / 0). SiftMap cannot classify a purchase as a rental until it observes the property being rented, so a recent buy-and-hold sits under `pending`. The old buyer-type rule required `txn_types == {"rental"}` exactly, so the moment recency entered the picture **111 landlords became 0** and reappeared mislabelled as active holders. The rule is now "any rental history AND holds at least as many as it exits", which restored 115. Related ordering fix: `wholesale`/`wholetail` describe how a buyer ACQUIRED, not their model, so that label now only fires when nothing about holding or exiting did.

**A BUSINESSV2 PRINCIPAL CANNOT BE SKIP TRACED AT THE COMPANY'S ADDRESS.** SmartSkip matches First + Last + Mailing Address, and a `siftmap-reverse-address` principal satisfies that BY CONSTRUCTION, since we found that person living at that address. An officer lifted from a corporate filing has no established link to it. Measured across two real batches: reverse-address principals hit **163 of 172 (95%)**, BusinessV2 principals hit **1 of 26 (4%)**. They are now held out of the SmartSkip batch by default (`--trace-businessv2` overrides) and need a name plus city/state person search instead. Cost of learning this was about $0.30 because the batch was small, which is the argument for small batches. Two more guards added at the same time: `skipinput` refuses to re-buy a number we already own (92 of 130 rows on the first pruned run were already-traced people, a silent double charge), and `phase_phones` now builds its join bridge from the REGISTRY and merges every downloaded batch, because the input CSV is regenerated on each run and an older download loses its bridge the moment the cohort changes.

**BUSINESSV2 CANNOT BE CONSTRAINED GEOGRAPHICALLY, AND MOST OF WHAT IT RETURNS IS THE WRONG COMPANY.** Verified live 2026-08-28: searching `SMITHBILT LLC` with no anchor, with `Addresses[{AddressLine2: "Knoxville, TN"}]`, and with `Addresses[{State: "TN"}]` returns byte-identical results. It fuzzy-matches company names nationally, and `extract_officers` accepts any filing containing the entity's first token, so a Knoxville LLC resolves to officers of a same-named company in another state: GDP Properties to West Point IA, Smithbilt to Hagerstown MD, Knox Development to Montrose SD, Vanguard Investments to Saint Albans VT. Measured on 12 cohort entities, only **2 had any Tennessee officer**. The first pass therefore put roughly 25 out-of-state strangers into the workbook as buyer names. The officer's own `address.fullAddress` is the ONLY geographic control available, and it is only available because `extract_officers` returns it: `rank_officers()` now drops any officer outside the buyer's own state, which resolved **29 of 153 (19%)** and cleared **62 unverifiable names**. An out-of-state officer is not a weaker answer, it is a different person.

**The same pass fixed the 4% skip-trace rate, and the cause was an address FORMAT.** SmartSkip matches First + Last + Mailing Address, and a BusinessV2 principal was being matched against the COMPANY's address, which is why those rows hit 1 of 26 while reverse-address principals hit 163 of 172. Tracing the officer's own address instead lifted it to **17 of 22 (77%)**. It took two attempts because Enformion writes `"4300 Hiawatha; Knoxville, TN 37919"` with a **SEMICOLON** before the city while SiftMap uses a comma, so `split_mail` glued the city onto the street and left Mailing City empty; the first paid batch came back with every result column blank. The semicolon now takes precedence over commas, since the street itself can contain a comma (`"2099 Thunderhead, Ste 204; Knoxville"`).

**Two of my own bugs in this pass are worth remembering, because both reported as findings about the data.** A `` word boundary written through a shell heredoc reached the file as a literal **backspace byte** (``), so the state filter matched nothing and the run announced "0 of 153 verified" as though BusinessV2 had failed; the check is now backslash-free tokenization (`"".join(c if c.isalnum() else " " for c in addr).split()`) because there is no escaping to get wrong. And `phase_phones` built its join bridge only from the company mailing address, so 17 newly bought hits silently failed to attach; it now indexes BOTH candidate addresses plus an unambiguous name-only fallback, which recovered 68 records.

**Coverage after the pass: 84 to 99 reachable of 268 targets**, with 222 buyers now carrying phones and 200 of those having a Dial First or Second number. Well short of the ~170 projected, because that projection assumed BusinessV2 would verify at close to 100% rather than 19%. The residue is honest and on the Overview sheet: 44 entities where BusinessV2 found no human officer at all, 71 where it found officers but none in the buyer's state, 29 where a principal resolved but the trace missed, 11 untraced people. Agent-titled principals are traced as a FALLBACK per Ty but carry a `VERIFY` flag on both the Buyer Profiles and Call List sheets (25 rows), because a caller who assumes they have the owner will open wrong on a lawyer's line. Also added: `write_profiles_xlsx` writes to a `_PENDING_` name and swaps, since Excel holds an exclusive lock and saving over an open workbook otherwise loses the whole run.

**THE NUMBERS WERE ALREADY IN THE ACCOUNT, AND A COMMENT STOPPED US LOOKING.** `dispo_flip_buyers.py` carries the line "person-owned records are skipped by default because DataSift's native unlimited skip trace covers humans; the entity principal is the part it cannot do". That was inherited as established and never measured. It is wrong: DataSift skip traces ENTITY-owned records too. Measured 2026-08-28 across the 128 unreachable target buyers holding a CRM record, **107 already carried phones** (514 rows, 316 unique mobiles) on records whose owner is the LLC itself, with GDP Properties on 9 numbers, Vanguard 7, Smithbilt 6. Only **2** had ever been Trestle scored. `--phase crm-phones` reads `owner.phones` off each record and merges it, and the whole harvest costs nothing because the account already paid for it under the unlimited plan.

**Coverage went 99 to 205 of 268 targets for $12.34 of Trestle scoring**, against roughly $105 already spent on Enformion and SmartSkip chasing identities. That vendor spend is not wasted, it answers a different question: the CRM yields NUMBERS with no person attached (the record owner is the company), while Enformion yields a NAME. Texting works from numbers alone because the buyer copy has no-name variants; calling needs the name so the caller knows who to ask for. Both columns are on the sheet now (`Phone source`, `Name known?`, and `ASK WHO` on the Call List). The residue is 33 buyers with no CRM record to harvest, 21 whose record held no usable number, and 15 whose numbers all scored too low.

Three rails on the harvest: an existing phone **disposition outranks it** (DNC, CORRECT_DNC, WRONG_DNC, WRONG and DEAD are skipped exactly as `seed.SKIP_PHONE_STATUSES` does, so nothing set by hand is overwritten); numbers **merge rather than overwrite**, so a number SmartSkip already returned keeps its place and gains a second source; and a live read-back asserted that **every harvested number is present on its source CRM record**, zero invented.

**The rule this build keeps re-learning: an assertion in a comment is not a measurement.** The same session also shipped a `` word boundary through a shell heredoc that reached the file as a literal backspace byte and made a state filter match nothing, which the run then reported as "0 of 153 verified" as though it were a finding about the data. Check what the account already holds before buying, and probe a filter with a count delta before trusting what it says.

**Open:** dedicated dispo DIDs are still unbought, so a blast shares the 18-number seller pool (450/day) and the pools must be disjoint before going live, since per-number caps are counted per database. Threads currently sign as **Adriana** because the dispo records' `assigned_to` resolves to her; the dispo program wants its own sender identity. The cross-program collision check (a buyer who is also an open seller lead) is designed but not yet built, and it matters because 89% of pending-flip owners were already in the marketing ecosystem.

## Dispo Blast, Live: two deals, two shapes (build 1.0.51, 2026-09-03)

The dispo program went from staged to sold. **158 Old State Rd**: 156 of 156
delivered, **25 replies (16.0%)**, one offer, **full ask $75,000** from Chabella
LLC against a $65,000 contract, so a $10,000 assignment. **3014 Sanland Ave**
at $104,000 (contract $92,000): 159 sent in one day, and the first response was
a **phone call from Litton Properties 12 minutes after their text**.

**The two blasts deliberately have opposite copy, and that is the finding.**
Blast 1 WITHHELD the address and let the offer do the redacting, so "send me
the address" became the call to action: 14 of the 25 repliers asked for exactly
that. Blast 2 leads WITH the full address, price and specs and offers the
lockbox. Reply rate fell and the first contact arrived as a CALL instead of a
text. Withholding buys conversation; disclosing buys showings. Watch lockbox
requests, not raw replies, when the address is public.

`disclose_address` is a per-deal opt-in and the four guards INVERT rather than
disappear: a redacted deal still refuses a house number and a zip, an
address-forward deal REQUIRES both, `audit()` asserts the exact address is
present, and `_deal_facts_block` includes it only when disclosing. **The lockbox
code is never in the deal sheet at all**, so the agent can offer it and cannot
give it; that code is physical access to a house.

**The money check is a WHITELIST, not a blocklist.** Every money-looking token
in a message must equal the approved asking price. That catches an invented
number AND the contract price without the audit ever being told what the
contract price is, which is why `deals/3014-sanland.json` carries no
`contract_price` field. Written without a regex on purpose, after a `` escape
reached a file through a shell heredoc as a literal backspace byte for the third
time in one session.

**Never imply a price relationship that is not true** (Ty, 2026-09-02). "Another
one at this price" on a $104,000 deal, to people who saw $75,000, is a factual
claim a buyer can catch. The warm variant references the RELATIONSHIP instead
("since you looked at the last one").

**A NEW DEAL may reopen a thread a human paused, but never an opt-out.** The
takeover pause is per conversation and meant the last property; on blast 2 it
held all 16 buyers who had engaged on blast 1, excluding the most valuable
audience precisely because they had been good leads. `seed.build(new_deal=True)`
allows `paused` and never `opted_out`, and `queue` reactivates the thread, since
otherwise the text goes out and the reply lands on a paused conversation where
it is never classified.

**`dispo_phonebook` turns a finished blast into an asset.** The engine already
sets phone status CORRECT with a Dial First tag when someone engages, so
verified-number capture is free; what was missing is grouping and a summary a
caller can read. Classification comes from what people actually typed: 16
verified, 131 silent, 9 suppressed (3 opt-out, 3 wrong number, 3 hard no).
**Scope it to the sending pool** or an unscoped pass classifies 947 recipients
across both programs and suppresses seller numbers as a side effect of a dispo
cleanup.

**Operational lessons that cost real time:**
- **Fly SECRETS override `fly.toml` `[env]`.** `fly config env` shows the file
  and confirms the wrong answer. Ask the running system: `/health` reports the
  effective phase and dry-run. The box was live at phase 2 the whole time.
- **`nohup` does not survive `fly ssh console`.** Use `setsid`; Fly kills the
  process group on hangup. A long run also drops mid-flight, so stage detached
  and poll the DATABASE, never the command's exit.
- **A probe whose pattern appears in its own command line always matches
  itself.** Searching `/proc` for `*dispo_campaign*` from a shell whose own
  cmdline contained that string reported a dead job as alive for twenty
  minutes. List the processes and read them instead.
- **A killed run leaves a partial batch.** Stopping the band-less run left 34
  already-queued rows, exactly the out-of-band buyers. Always read the held
  count back.
- **Carrier registration is invisible to tests.** 262 passing checks cannot know
  four of five numbers were unregistered for A2P 10DLC. Canary one message per
  number and read the result.

**Shipped public (2026-09-03).** Four agents added to the org chart under Dispo
& Buyers (`dispobuyers`, `dispoblast`, `dispobook`, `dispoflow`) plus updates to
`sender`, `classifier` and `responder`; `docs/AGENT-MAP.md` and
`docs/agent-system.html` regenerate from `docs/agents.json` and publish at
learn.datasift.ai/agent-org-chart. New community skill **`dispo-deal-blast`**
(tier none, 27 packages in the manifest) carrying the method and every guard.

**Two things the CI gates caught that a review would not.** A `.skill` archive
has no diff, so drift between what people download and what they can read is
invisible: `dist/playbook-creator.skill` was stale, and separately its
description was 1,154 characters against a hard 1,024 limit that makes Claude
Code reject the WHOLE package at install time, after the download. Both were
inherited, both are fixed. The drift itself was **line endings**: the archive is
zipped from the WORKING TREE, `.gitattributes` normalises on checkout and add
but never rewrites a file already sitting there with CRLF, so
`git add --renormalize .` is the command that actually makes them match.

**Never commit this repo blind; it is PUBLIC.** The sweep for this session found
a SmartSkip session file holding live access and refresh tokens, county eviction
records naming tenants and their court dates, vendor webpack bundles saved while
reverse-engineering filter keys, and a 39MB screen recording carrying seller
data. All now gitignored. The sending DIDs were also removed from this file and
replaced with a pointer to the gitignored pool.

## D4D Top 3,000 Call List (build 1.0.53, 2026-09-11)

`src/d4d_call_list.py` turns the Driving for Dollars visual distress ranking
(`output/Knox_Blount_Master_<date>.xlsx`, sheet "All properties", sorted by the vendor's
`visual_distress_score`) into a dialer-ready list: top N rows -> every one a DataSift record ->
DataSift skip trace -> Trestle tier on every unscored number -> tiers written back onto the CRM
phones -> two CSVs. Phases `cohort/pull/skiptrace/harvest/score/writeback/export/qa`, DRY by
default + `--commit`, resumable state in `output/d4d_call_list/`. Tests:
`python tests/test_d4d_call_list.py` (zero network).

**Live run 2026-09-11, top 3,000 (score 100 down to 69, Knox 2,997 / Blount 3):** 1,286 were
already records, **1,714 pulled in**, 3,000 of 3,000 harvested with **20,501 phones**, 7,411 already
tiered, **10,849 numbers Trestle-scored for $162.73 with zero errors** (Dial First 4,749 / Second 720
/ Third 588 / Fourth 3,648 / Drop 1,110 / litigator 24), tiers written to **2,246 owners / 11,036
phones** with 0 mismatches on an 80-owner read-back. Export: 2,997 properties, **2,916 with a
callable number**, 19,282 phone rows, 81 with nothing callable.

**Contracts learned, each verified live:**
- **Pull by id works through `add-properties-by-query`** with `query.addresses =
  [{"search": "<dataflik_id>", "type": "ids"}, ...]`, `filters: {}`, `polygon: WORLD`, chunks of
  250. Probed on one id with a read-back (`dataflik_id` matched, list attached) before the batch.
  It is async: 1,713 ids landed in about 6 minutes, polled through the same search-by-ids call,
  which also returns `saved` + `saved_uuid` live. The documented-only `/properties/add-properties/`
  route was never needed.
- **The SiftMap add skip traces on its own** on the unlimited plan: 37 of 40 sampled new records
  carried phones minutes after landing, account traced count 56,207 -> 57,303, `value_spent`
  still $0. So the wizard was unnecessary. It was also BROKEN: `_filter_by_list` no longer finds
  `#Records__Filters_Trigger` and `skip_trace_records` then "continues anyway", which means
  SELECT ALL on the whole account. The run was killed at the Send To menu before it submitted.
  Do not run that wizard unattended until the list filter is fixed.
- **SiftMap holds two ids for one address.** Three top-3,000 rows were duplicates of another row
  at the same address (ranks 12/234, 973/974, 2308/2309); DataSift dropped the second pull as an
  address duplicate (the uuid 404s minutes after landing) and attached the list to its existing
  record. The export sends the second occurrence to the no-phone file as "duplicate of rank N".
- **A record's `dataflik_id` equals the workbook `PropertyID`** on 3,000 of 3,000, so the join
  needs no address matching.
- `upsert-phones` is sent once per OWNER with only the changed phones (2,246 calls instead of
  11,036); untouched phones are never re-sent. Two 504s on the first pass were retried by the
  resumable state. The read-back must sample owners still in the plan: a refreshed record no
  longer has eligible phones, so it drops out of `planned_writes` and a naive sample KeyErrors.
- Trestle's free key was already exhausted for September (1,000 used on 09-09), so the router
  billed everything to the paid key. Check `trestle_router.py usage` before quoting a cost.

## Dream 100 (build 1.0.55, 2026-09-22)

`src/dream100.py` + `src/dream100_build.py` rank the D4D top 3,000 by OPPORTUNITY SIZE and write
the hundred largest to a workbook and to a CRM list. Three sources joined on the record uuid: the
D4D visual distress rank (`output/d4d_call_list/cohort.json`), the FULL CRM record (investor,
realtor and on-market scores plus every deal field: value, equity, mortgage, taxes, last sale,
rental value, vacancy, owner age, deceased, dates), and the lists the account itself attached.
Tests: `python tests/test_dream100.py` (19 checks, zero network).

```bash
python src/dream100.py --phase pull             # full records, ~2.2/s, resumable (23 min for 3,000)
python src/dream100.py --phase build --top 100  # workbook + CSV + dream100_ranked.json
python src/dream100.py --phase attach --commit  # list + tag "Dream 100" on the top rows, read-back first
```

**The harvest that `d4d_call_list` keeps is a projection and has NO investor score**, which is why
a second pull of the full record was needed. `/api/internal/property/{uuid}/` carries
`investor_score`, `realtor_score`, `on_market_score`, `rental_value`, `loan_to_value`,
`open_mortgages`, `tax_delinquent_value`, `owned_since`, `secondary_owners`, and the owner's
`age`/`deceased`/`dnc`/attempt counters. Custom fields are NOT on it (separate route, not needed).

**Opportunity score, 100 points, every component a column:** deal size 35 (discount room =
min(equity $, 30% of value), ramp $15K to $120K: a seller cannot sell below the payoff and the
70% rule gap is the most a wholesale buyer pays for), condition 20 (D4D score ramp 69 to 100),
investor 20 (ramp 20 to 90, up to 5 off when realtor_score >= 70), evidence 15 (list points:
Foreclosure/Trustee 5, Tax Delinquent/Vacant/Probate/Obituary/Condemned 4, Liens/Bankruptcy/
Divorce 3, Tired Landlord/Out-of-State 2, Senior/Absentee 1, plus record fields, capped at 14
raw), reach 10 (best Trestle tier; DNC = mail only). Gates, each counted on the Excluded sheet:
dead status, recently sold / Do Not Market tags, Dispo buyer lists (the owner is a buyer), no
value, over $700K, not single family (the structure string `Residential (General) (Single)` IS
single family; match on multi/duplex/condo/town/mobile, never on the absence of "single family"),
upside down, listed on the MLS. In-progress records are KEPT and flagged, because a Dream 100 that
hides the team's own hottest files is wrong.

**Live result (ty+2, 2026-09-22):** 2,856 qualified of 2,997, 141 excluded (36 not interested,
29 listed or pending on the MLS, 19 sold, 12 on a Dispo buyer list, 9 Do Not Market or recently
sold, 7 upside down). Dream 100 combined value $32.9M, combined discount room $9.9M, median value
$309K, 98 of 100 reachable on a Dial First or Second number, 4 entity owners, 36 vacant, 26 already
in the pipeline. 99 Knox / 1 Blount (the D4D cohort itself is 2,997 Knox), and 37917 alone holds 38
rows. List + tag `Dream 100` attached to all 100 with a read-back on rank 1.

**The biggest finding is the Portfolio owners sheet, not the ranking.** 194 owners hold two or
more properties in the D4D 3,000; the top one holds 22 ($4.9M value, $1.46M discount room) and the
next 13. One conversation there is worth more than a dozen single rows, and the per-property score
cannot see it. Every Dream row carries "Owner's other D4D records" for the same reason.

## Dream 100 deep prospecting (build 1.0.56, 2026-09-23)

`src/dream100_dp.py` runs deep-prospecting v5 on the top N of `dream100_ranked.json`. Phases
`records / smartskip / merge / gapfill / score / build / writeback / qa`, DRY by default, `--commit`
to spend or write, `--only <slugs>` for writeback. Two layers are written by a researcher, never by
the module: `research/<slug>.json` (agents; every claim carries a URL and a verbatim quote) and
`analysis/<slug>.json` (headline, who must sign, outreach). Output: a pack PDF per property, a
workbook plus a redacted demo twin, and the CRM write-back. Tests: `python tests/test_dream100_dp.py`.

**Top 5 run (2026-09-23): every record changed once researched.** Three owners of record were
dead (in 2024, 1999 and 2025, the last with a son as executor and a probate case open), and
the team had been calling and texting all three by name. The two living owners carried wrong CRM
facts: one is 85, not 95, and his mailing address is a stranger's house; the other is a
33-year-old portfolio holder winding down two LLCs. Written
back and read back clean: 20 new numbers tagged `[relationship, tier, source]`, 5 pinned notes, tag
`Deep Prospected`, decision-maker fields on 5, death fields plus `owner.deceased` on 3. Cost $1.79.

**Lessons, each measured on this run:**
- **Audio models invent speech on short clips.** 9 of 37 call recordings came back as whole
  invented conversations (660 words from 11 seconds). A transcript counts only at 3 words a second
  or less of real audio (ffprobe), plus a strict second pass that forbids guessing.
- **The activity log is per OWNER.** Touches on the owner's other properties show up on this record
  (every call to one owner asked about his other house). Call direction comes from the payload, never
  the author: a Kixie dial is logged by "system" and reads like an inbound call.
- **SmartSkip is stale on deaths in both directions.** It listed three relatives who died in 2000,
  2017 and 2007 as living. Its Deceased flag was right on
  all 3 owners here, and it is still only advisory.
- **A mailing address on the same parcel is a second home, not an absentee owner** (two of the five). Selling the parcel sells the widow's own home.
- **AVMs overstate these deals:** one read $659,000 against a $153,900 county appraisal, another
  $402,000 against $87,100. Name-based tax joins also mix parcels: the "unpaid 2015 to 2021" note on
  one record belonged to the owner's lot on another street.
- **Maiden-name emails confirm daughters** (a gmail address built on the family's surname, found on a married daughter's record).
- **The `\b` heredoc trap hit again** and the name harvester matched nothing. Regex patches go
  through files; a test asserts the module holds no backspace byte.
- `GET/PATCH /api/internal/owner/{uuid}/` carries `deceased`, verified by read-back.

## Vacant Land: buildable lots, Union County OH (build 1.0.60, 2026-11-05)

`src/vacant_land.py` finds every vacant parcel of 0.20 to 2.00 acres in a county, screens out the ones that cannot carry a house, ranks the owners by motivation, prices an offer band off real lot sales and writes the marketing plan. County GIS is the truth for the PARCEL, SiftMap for the OWNER. Read-only everywhere; `pull` is dry unless `--commit --key-env <VAR>` names the TARGET account's Open API key. Output `output/vacant_land/union_oh/`: `Union_OH_Buildable_Lots_<date>.xlsx` (Overview, Mail list, Marketing plan, Not marketable, Builder inventory, Lot comps, Lot buyers, Sources and caveats) + `mail_list.csv`. Tests: `python tests/test_vacant_land.py` (95 checks, zero network); the LIVE test is `--phase probe` (exit 3 if the contract moved).

```bash
python src/vacant_land.py --phase probe     # live: proves the land filter by count delta
python src/vacant_land.py --phase all       # county sweep topo screen hydrate score export (~20 min cold)
python src/vacant_land.py --phase pull      # dry tier plan; --commit --key-env TARGET_KEY sends it
```

**Live result 2026-11-05:** 36,768 parcels -> 1,927 vacant-class in the window -> 496 BUILT SINCE REAPPRAISAL, 276 NOT BUILDABLE (144 landlocked, 88 floodplain, 70 strips, 14 steep, 14 non-residential zoning), 213 public or common area, 129 builder inventory -> **813 marketable** (162 buildable, 490 likely, 161 verify) -> **625 mailable** (tier A 48, B 168, C 409; excluded 131 bought in the last 2 years, 5 on MLS, 53 with no DataSift owner). Biggest segment is 200 neighbor side lots (owner holds the house next door). Lot comps: 148 valid lot sales in 36 months, county median $165,000.

**SiftMap contracts (count-delta proven, all silent if wrong):** `type_land: true` is the ONLY land filter (40,017 -> 4,379); `type_land_residential`, `type_vacant_land`, `type_lot`, `type_vacant`, `type_commercial`, `type_farm` all return the unfiltered count, and there is still no lot-size filter. **type_land is RESIDENTIAL vacant only:** SiftMap files small ag lots as "Rural/Agricultural-Vacant Land", which it never returns. hydrate reaches them by searching with the parcel's own bounds as the `polygon` viewport (`filters: {}`), point-in-polygon, then requires detail `apn` digits == county CAMA number (170 of 193 found, 8 ids rejected on apn). Row `distressors` is `{key: {is_active}}` with every key on every row. The AVM is junk on land ($1,249,000 on a 0.022 ac sliver).

**County traps (Union GIS, `www7.co.union.oh.us/unioncountyohio/rest/services/`):**
- **The class lags construction.** 2,185 "vacant" (500) sales in 36 months had a median of $590K at 7.5x land value: new houses. Built-on = dwelling year, sale at 3x+ land value and $150K+, or a permit in 3 years.
- **The dwelling layer has a row for ~every parcel;** `age` is the year built and `'0'` means no house. Membership alone marked 1,035 of 1,046 lots built.
- Acreage is BLANK on platted lots (measure the polygon, 875 of 1,927); `ZipCode` holds "MARYSVILLE OH 43040"; ParcelMailingAddress refuses queries (mailing comes from SiftMap).
- **No sewer layer exists, and soil cannot decide it:** USDA rates nearly every Union soil "Very limited" for septic fields. Sewer proxy = median lot size of houses within 1,000 ft (under 0.50 ac sewer, 1.0+ septic), else platted and under 0.50 ac = sewer, unplatted with few houses = rural septic.

**Topography (Ty's ask) is USGS 3DEP, free and keyless, 1 m lidar.** `computeStatisticsHistograms` costs 7-8 s a call whatever the area (3 hours for the county); `getSamples` reads 1,000 points in ~11 s, so `topo` samples a ~60-point grid per lot plus a 150 ft ring and batches ~10 lots per request (192 requests, ~8 min). Measures: share of the lot over a 15% grade (fail at 50%), relief, and how far the lot's median sits under its ring. Union is flat: 107 lots are steep, 165 sit 2+ ft low, and sitting low (drainage, fill) is the signal that matters here.

**Pricing: never one county figure.** Jerome subdivision lots are 98 of 148 comps (median $186,800) and would triple a rural lot's value. Order: same township and size band (5+ sales) -> the lot's auditor land value (detail `assessed_land_value / 0.35`) x its township's sale-to-land ratio, or the rural 1.95 (Jerome and Washington/Dublin run 1.20) -> rural lot sales in the size band. **Class 110 is CAUV farm valuation ($29-$400 land values on house-size lots) and is never priced off land value.** Offer band default 50-65% of the reference (`--offer-low/--offer-high`).

**Tiers are rules, not a blended cutoff:** buildability topped out near 82 and motivation near 35, so a blended 70 put nobody in A. A = build 65+ with a strong signal (tax, estate, out of state, liens); B = build 55+ with motivation 12+.

## Builder Land Desk: buy boxes from deeds, lots matched to builders (build 1.0.61, 2026-11-06)

`src/builder_land.py` runs a business model Ty took from a wholesaler friend: find the buyers who target one niche, learn exactly what they buy, and build the seller list to match. For land, those buyers are builders, and all of their purchases are on the deeds. For any county, the module reads each builder's buy box off those purchases: where they buy, lot size, price paid, what they build and sell. It then matches the lots on the ground today to the builders who would buy them. It works as a county run and as a national screen. It is read-only on the opensearch MCP and never writes to an account (a test asserts it never calls `add-properties-by-query`). Output: `output/builder_land/<market>/<Market>_Builder_Land_Desk_<date>.xlsx`, which carries Overview with a verdict line, Builder buy boxes with a call opening line per builder, the Lot list mail list, Lots by builder (filter to one builder to get their tailored list), Teardowns, Development tracts, Competitors, Economics (live formulas), Builder intake, Niches, and Sources and caveats. Tests: `python tests/test_builder_land.py` (72 checks, zero network).

```bash
python src/builder_land.py --market knox_blount --fips 47093,47009 --phase all --streams 6
python src/builder_land.py --market union_oh --fips 39159 --phase all --lots-csv output/vacant_land/union_oh/mail_list.csv
python src/builder_land.py --phase national            # every county, cached; --refresh re-pulls
```

**Results (2026-11-06):**
- **Knox and Blount: yes.** 110 buyers qualify: 3 or more purchases since 2021, on 2 or more separate deeds, with a purchase in the last 24 months. Of those, 81 buy lots. 2,915 lots owned by individuals fit at least one builder and 1,182 fit 3 or more. Builders paid a median $40K. The measured quick-flip spread is a $15K median over 47 days. These builders bought 134 lots in the last 12 months, and 66% of their purchases came from individuals. Only 22 of the 110 have phones in the dispo registry, which is the gap to close before outreach.
- **Union OH: no.** Repeat buyers there are production builders (Maronda, Pulte, Rockford, Jerome Village) and tract buyers. Only 3 of the 610 screened lots fit a repeat builder, and 66% of rural homesites are still owned by the person who bought the lot, so those lots sell retail.
- **Franklin OH (Columbus): yes.** It is the Knox shape at a bigger scale: about 86 urban infill homes a year, 1,988 infill lot purchases since 2021, and buyers with 3 or more purchases took 44% of them. Median lot $35K, median new home $300K. 55 active builders (51 of them buy lots), 1,306 fitting lots, 505 that fit 3 or more builders, 3,704 teardowns. The largest builders are Walnut Homes For You, Monticello Homes and ARV Investors. None are in the dispo registry, which only covers Knox and Blount. 871 of the fitting lots have no postal address; for these the place fields are blanked by `place()`, because the district-as-city artifact otherwise reads "Columbus Grove 45830". The full live run (370,613 parcels, 8 balanced streams, 57 history batches) took 25 minutes.
- **National ranking:** Harris, Dallas and Tarrant TX lead, then Hidalgo, Cook, Jackson MO, Johnson KS and Wake. Knox ranks 96th of 2,183 counties, Franklin OH 29th, Union OH 729th.

**The buy box and the fit:**
- **Purchases are DEED DATES, not parcels.** A 13-lot deed is one purchase, and a takedown is priced at the deed price divided by its lot count. A deed priced at 3x or more of the parcel's value also carried parcels outside the list, so it gets no lot price (Ball Homes and Mesana had read $1.37M and $1.5M "lots").
- **Box contents:** lot size is p10-p90 widened 20-25%; price is p25-p75; areas and ZIPs are by count; finished size and resale come from the houses the builder built.
- **A lot fits a builder** when one of that builder's own lots is within 600 m (acreage 3 km, tracts 5 km) and the lot size is inside their range. A class with only one purchase is an event, not a box.
- **Offer** = what the fitting builders paid nearby, minus the fee (`--fee`, default $12K). There is no offer when the fee would exceed 60% of the lot price.
- **Slivers** under 0.06 acres are not lots.

**Deed-chain fix with reach beyond this module:** the jump's exit must be priced like a finished house (at least 40% of today's value deflated to the sale year). Without that, a lot wholesaled at 1.5x before the build is read as the builder's purchase plus the new-home sale, which misattributes the builder and hides the middleman. The Knox niche scripts in `output/niche_buyers/method/` predate this fix.

**National screen, three traps in buyer-stats:**
- **A purchase is counted by the parcel's CURRENT class.** A builder's lot that became a house therefore leaves `land_residential`, which makes land `FLIP` mean land bought and resold as land. The first ranking built on it put the Florida and Arizona platted-lot sellers on top (Western Land and Ranches, Parcels A Plenty, Lots of Land LLC). The builder signal is now small buyers of single-family parcels with a builder name, matched by wildcards on the keyword `buyer_full_name` in one aggregation (about 8 s). It misses unnamed builders such as Braden, and it counts some house flippers with "Homes" in the name, so it ranks markets and does not size them.
- **Land `WHOLESALE` is the lot wholesalers** (on Knox: SMP Holdings, the same name the deeds found).
- **HOAs, POAs, utilities and fire districts** top the raw land counts. They are filtered by name client side (`JUNK`).

**Pull mechanics:**
- **Equal dataflik_id ranges are lopsided** (one Union stream held most of the rows). Even a 400-bucket histogram is not enough: Franklin OH put 276,000 of its 370,000 parcels in 2 buckets. `id_buckets` re-histograms any bucket holding more than half of one stream's share, up to 3 levels, and `balanced_edges` cuts the streams from the result (8 streams came out at 32,000-50,000 rows each). The edges are saved so a resume keeps them.
- `--from-dir` imports an earlier pull.
- Patch scripts containing triple quotes break bash heredocs; write them with the file tool.

## Knoxville Region Land Market: TAM, verification, end buyers, plan (build 1.0.62, 2026-11-09)

`src/land_market.py` sizes the land market across 11 counties (Knox, Blount, Sevier, Anderson, Loudon, Roane, Jefferson, Campbell, Grainger, Union, Morgan), verifies every land parcel Union-style, finds the end buyers per county and writes the marketing plan. The model is Ty's: **buy at farm price, entitle, sell the approved land (no site work)**, plus the builder lot desk. It is read-only everywhere and pulls nothing into an account. Output: `output/land_market/Knoxville_Region_Land_Market_<date>.xlsx` (18 sheets) plus mail CSVs per lane. Tests: `python tests/test_land_market.py` (122 checks, zero network).

```bash
python src/land_market.py --phase all       # gis tam screen topo buyers score export (~45 min cold, topo is 23 of it)
python src/land_market.py --phase export    # re-render from scored.jsonl
```

**Sources:**
- **The opensearch MCP pulls** (builder_land markets `knox_blount` and `knox_ring`) are the owner and deed truth. `knoxville_region/sources.json` makes builder_land read both pulls in place, so the region builder analysis needs no copy.
- **The TN Comptroller OLG land use layer** (`tnmap.tn.gov/.../ENVIRONMENTAL/COMPTROLLER_OLG_LANDUSE/MapServer/2`, public) is the parcel truth in 10 counties: polygon, `WATERSEWER` ("03 Public/Individual" = public water, septic), building count, improvement value, and `PEROFPARCELINFLOOD`.
- **It does not cover Knox** (nor Davidson, Shelby or Hamilton). Knox uses:
  - KGIS public layers on `services1.arcgis.com/QWaOgwdmpqI9HUzf`: growth policy plan, future land use, dissolved county zoning.
  - Statewide DFIRM flood polygons.
  - TIGER roads.
  - A circle footprint of the cluster acreage.
- **Built-on checks in all counties** come from USA Structures footprints (FEMA/ORNL, public, queried by `FIPS`).
- **Slope** comes from USGS 3DEP.

**Traps, each silently wrong:**
- **Parcel ids.** GISLINK = TN county code + the cluster parcel number with spaces removed. Anderson, Jefferson, Loudon and Sevier ship it dashed ('019-028.00' becomes '001' + '019' + '02800'). Without that rebuild those 4 counties joined 0%; with it, 97-99.6%. A parcel can appear twice with different LANDVALUE; keep the higher. LU_ACRES has outliers, so acres come from the polygon.
- **Footprints.** They doubled the built-on catch (Knox 984 to 2,117, Sevier 1,549 to 3,037). The rolls lag construction here exactly as in Union.
- **Slope.** Union's slope rule (fail at half the lot over a 15% grade) rejected 70% of East TN builder-fit lots. Sevier cabin lots run a 41% median average grade and get built every week, and Sevier allows 30-50% slopes on 2 acre lots. Lots now fail only at a 45% AVERAGE grade, and 15-45% is priced. Tracts fail at 35%, and developable acres take half the steep share.
- **Big-tract prices.** County bands are timber land: the region's 150+ acre band is about $665/ac, and that priced a 176 acre Knox farm at $117K. Tracts are priced from tract sales within 5-10 miles at a similar size (`TractComps`), with the county band as the last fallback.
- **No-margin tracts.** A tract whose nearby land already sells above its approved value (Hardin Valley, about $86K/ac) has no entitlement margin at farm price. It drops to tier C with a flag.
- **Economics formulas.** The first Economics sheet hard-coded B-row references, and every formula pointed one row high ("lots closed" multiplied the builders' yearly demand). Formulas now resolve by label (`econ_resolve`) and the test recalculates the sheet.
- **Knox pipeline layer.** KGIS DevelopmentProjects is commercial and apartments from 2011-18, not builders. DensityViewerDevelopments stops in 2017 (approved PR density 2.7-4.8 du/ac); it is used only as evidence for the 2.0/acre net density assumption.

**Results (2026-11-09):**
- **Supply:** 572,294 parcels, of which 141,543 are in a land lane. Privately held: 47,098 builder lots, 25,993 homesites, 35,820 small tracts, and 26,048 entitlement tracts (15-150 ac).
- **Trading:** about 4,100 vacant sales and $685M a year (2022-24).
- **Mailable after verification:**

  | List | Count |
  |---|---|
  | Builder-fit lots and homesites, tier A+B | 5,562 |
  | Entitlement tracts, A+B | 2,595 |
  | Homesite-split tracts, tier B | 6,642 |
  | Buildable lots with no builder fit (tier R, not the lot desk) | about 40,000 |

- **County order:** Knox ranks 1 in both lanes. Production-builder demand outside Knox is in Loudon (171 lot buys, 74 builders) and Blount, which are the two with approval pauses (Blount: 12-month moratorium on 11+ lot subdivisions recommended and referred to the planning commission, Oct 2026; Loudon: road pause reported). Sevier and Jefferson are open, but their builders are cabin and small builders.
- **Economics at placeholder rates:**
  - The lot desk makes about $249K a year.
  - Entitlement makes about $48K a year at the median spread and $384K at the 75th percentile. Engineering is spent only on tracts a builder has pre-committed to (LOI), which is load-bearing.

**Land texting through Smarter Contact (LIVE 2026-11-10, touch 1 started 1:15pm ET):**
- **What went out.** 8 campaigns (lots and tracts x named/no-name x East/Other TZ), 1,860 contacts from the ty+2 land pilot lists (`Land - Builder Lots`, `Land - Entitlement Tracts`, tag `Land Pilot 1`), signed Adriana, 2 to 6 texts a minute each. Copy rule (Ty): never claim we work with builders; say we're looking to buy.
- **The APN field is taken.** Smarter Contact has land merge tags (`<APN>`, `<Parcel acres>`, `<Parcel county>`), but DataSift's integration writes its RECORD UUID into APN. Land contacts carry the parcel in APN and the uuid in a `Sift Record` custom field.
- **DataSift's push delivered nothing.** `POST /api/internal/property/smarter_contact/` (the Send To button) answers `{"count": N}` and creates the group, but no contact ever arrived (two probes, 30 minutes). So `src/smarter_contact_land.py --phase create` makes the contacts directly over GraphQL (`createContact` with every attribute set at creation), applying the Priority 1 exclusions first: SC suppressions, a number already in SC, DNC/wrong/dead phone status, landlines, the registry DNC flag off the search row, Do Not Market, deceased or opted-out owners, dead statuses. Max 3 numbers per owner, best Trestle tier first. `--phase group` rebuilds every group from SC itself. **Never wrap create in `timeout`**: the first run was killed at 1,248 before grouping.
- **SC refuses near-duplicate templates** (TEMPLATE_DUPLICATE_ERROR: refused at 0.95 similarity, accepted at 0.88). `land_audit()` guards at `SC_SIMILAR = 0.90` across all land variants.
- **The browser session lapses.** `--phase templates` and `smarter_contact_launch.py --http` run on the agent's own Firebase login, so no browser is needed to launch.
- **Reply routing (deployed to Fly).** `sc_client.contact_record_uuid` reads `Sift Record` first, then APN.
- **Templates.** 94 land variants (`LAND_LOT`, `LAND_TRACT`) checked on a land profile where "vacant" is allowed; tracts never say vacant. `--land lot|tract` on `smarter_contact_launch.py` picks them.
- **Day 1 result.** 15 campaigns ended (8 plus 7 "Late 1110" make-up groups, via `create --group-suffix`, so a sending campaign never gains a contact), **1,999 texts, 0 refunded**, 14 interested owners by 6pm, all Knox lots but one Sevier tract and one Jefferson tract.
- **Deal cards (`--phase deals --commit`).** A pinned Sift note `Land deal card` plus SC fields Land Offer / Land Value / Land Notes on every land record (1,293 records, 1,984 contacts): lane, how we make money, offer range, could-sell-for, spread, and `BuyerIndex` (builders who fit from builder_land `analysis.json`, land buyers within the lane radius in 3 years, new homes within 2 mi). **Buyer sales must exclude BUILT parcels and houses:** with them, a 0.14 acre lot read a $237,000 median.
- **The Slack hot lead attaches the card** (deployed 2026-11-10 6pm): `worker` reads `crm.land_deal_card(record)` for non-dispo escalations and `escalate.hot_lead(land_card=)` posts it as its own block under "internal numbers, never quote them by text". The agent never sees it. Selftest 383; two checks that read the wall clock (eastern layout, Sift counter) failed every evening after 6pm ET and now pin `sender_pool.within_quiet_hours`.
- **Touch 2 needs `--group "<group>"`** on `smarter_contact_launch.py --phase followup|touch`; without it the P1 group names are built and nothing is found.
- **Runbook.** `docs/smarter-contact-setup.md`, Land campaigns section. The older `patch`/`push` phases remain for a contact that arrives through DataSift's push.

**County rules** (Rules sheet, `COUNTY_RULES`) were researched 2026-11-09 from county regulations and local news. Blount and the Knox road-cost law were checked against the source; the Loudon article is offline. From Jan 1, 2027 (Public Chapter 1044), a preliminary plat not acted on in 60 business days is deemed approved.

## Agents Brain: status change fires the Research agent (build 1.0.59, 2026-11-03)

`src/agents/` is the infrastructure for four autonomous agents (Analysis, Research, Sales Assistant, Operations; see the `project-four-agent-consolidation` memory). One always-on brain per install: FastAPI receives DataSift sequence webhooks, writes a task into a SQLite queue on its own volume, one worker thread runs the owning agent, writes back over the Open API key, posts to Slack. Nothing shared between customers, no Supabase, no password anywhere. Fly app `siftstack-agents` (`fly.agents.toml`, `deploy/Dockerfile.agents`, secrets via `deploy/sync_agents_secrets.py`, which never sends DATASIFT_EMAIL/PASSWORD).

```bash
python src/agents/cli.py selftest                     # zero network (45 checks; needs the repo .venv for fastapi)
python src/agents/cli.py run --address "123 Example St"   # run the deep prospect in-process
python src/agents/cli.py setup-datasift              # app steps + whether DataSift's Test delivery arrived (exit 2 = waiting)
python src/agents/cli.py setup-datasift --staff-api [--commit]   # OUR accounts only: webhook + sequence by unlisted routes
python src/agents/cli.py status
```

**First workflow: status changed to "Deep Prospecting" -> Research agent deep prospect.** ty+2 already had the sequence "Send to Deep Prospecting" (trigger `property.status.updated` to `deep prospecting`), inactive, creating a card in a DELETED column. `setup-datasift` repairs it in place (plans cap sequence counts): card into "Check Relatives" on the Deep Prospecting board plus a `send-webhook` action, active. The chain is `deep_prospect_record` with Claude as the researcher: records -> smartskip -> merge -> RESEARCH (Claude Opus 5.5 + web search/fetch) -> ANALYSIS -> heirs -> score -> gapfill -> verify -> FINAL ANALYSIS -> build -> writeback. `AGENTS_MODE=dry` (default) pays no vendor and writes nothing to the CRM; live spends inside `AGENTS_RUN_CAP_USD` (5) / `AGENTS_DAILY_CAP_USD` (25), skipping (not failing) any paid phase that would cross a cap. Run folders sit on the volume keyed by address, so a re-run reuses paid SmartSkip orders. Dedupe: one record is not re-run inside `AGENTS_DEDUPE_HOURS` (24).

**Contracts learned building it (all verified live 2026-11-03):**
- **DataSift webhook action:** `{"action": "send-webhook", "payload": {"webhook_uuid": <uuid>, "label": "send.webhook"}}`, read out of the app bundle (`main.min.js`, unminified, `ACTION_VALIDATORS['send-webhook']` requires `webhook_uuid`; feature flag `isWebhooksUser`). Webhook registry: `POST /api/internal/integration/webhook/` takes only `name` + `url`, so the secret rides in the URL path. **Superseded 2026-11-11 by the published docs (see "DataSift Open API" below):** creating a webhook is APP-ONLY, so `setup-datasift` now prints the app steps and confirms through DataSift's own `webhook.test` delivery (kv `datasift:webhook_test:<slug>`, shown in `/health` as `datasift_webhook_tests`; the CLI asks the RUNNING brain's /health, since the receipt lives on the Fly volume). The body is documented (`property.uuid` on `sequence.action.triggered`); `brain.datasift_hook` verifies the Ed25519 signature (`webhook_sig.py`, `AGENTS_WEBHOOK_SIGNATURES` require (default) / log / off), records the test event, dedupes on DataSift's `event_id`, then routes. `cryptography>=42` is in `deploy/requirements-agents.txt` for it.
- **The Open API key reaches every write the chain makes:** property PATCH/message/pin, custom-field update-values, add-lists, owner PATCH (`deceased`), owner upsert-phones, sequence/board/column/webhook create. `datasift_api_upload.Api` uses the key whenever DATASIFT_EMAIL/PASSWORD are absent; `dream100_dp._crm_clients` falls back to `crm_key_client` (key-only ports of the Deal Room CRMClient reads and the dossier helpers) when the Desktop `_api/clients` folder is missing.
- **Structured outputs reject schemas this size** ("The compiled grammar is too large"). The research and analysis schemas stay the contract, but Claude returns JSON in text and `researcher.conform()` forces the shape (fills empties, drops extras, coerces enums), so every reader can index blindly.
- **Guards in code, not just the prompt:** a claim whose URL Claude never opened or searched in this run moves to `unresolved`; the owner is marked deceased only when an obituary opened this run backs it AND the decedent shares two name tokens with the owner of record (the spouse-obituary trap); an empty `trace_address` is dropped because `heir_rows` would otherwise override the real street with it and skip the heir; empty CRM fields never write.
- **`relative_to(ROOT)` crashed every run off the repo** (the Fly volume is `/data`). `dream100_dp.rel()` returns the absolute path there; a stored absolute path still resolves because `ROOT / "/data/x"` is `/data/x`. Writing that helper with a regex also rewrote the helper's own body into infinite recursion: the dry run caught it, a reason to always dry-run before deploying.
- `load_env()` now returns the process environment too: Fly secrets arrive as env vars and there is no `.env`, and phases read keys off that dict.
- **No `.dockerignore` existed**, so every Fly build uploaded `output/` (seller data, gigabytes) and `.env` to the builder. Added; every Dockerfile only COPYs `src/`, `deploy/`, `skills/`, `.actor/`, `requirements.txt`.
- `anthropic==0.86.0` is pinned in the image: it is what the run was verified on. It warns that it does not model the newest web-fetch result blocks; a 2.x upgrade could not be tested locally because the scratchpad path breaks Windows' 260-character limit inside a venv.
- The `.env` FLY_API_TOKEN is scoped to `siftstack` only and cannot create apps; standing up `siftstack-agents` needs `fly auth login`.

## The AI Team: Atlas, Diggs, Roma, Quinn (build 1.0.63, 2026-11-11)

Tyler's AI Operator Bootcamp scaffold (four named employees on Slack, handoffs on one thread, ask-back) merged onto the agents brain above. `team/roster.yaml` is the single source of truth: employees, channels, models, skills, and the 31-workflow catalog (owner, `live`/`planned`, spend class, triggers, and `covers`: the org-chart agent ids from `docs/agents.json` each workflow runs; autopilot stages carry `covers` too). `team/TEAM.md` and `team/COVERAGE.md` are GENERATED (`cli.py team`; the selftest fails when either is stale, and `cli.py team` plus the selftest fail when a live org-chart agent has no workflow, a covers id is off the chart, or a retired agent is listed). 2026-11-11: 90 agents = 81 run by a workflow + 3 platform (operator, orchestrator, core) + 6 retired, 0 unassigned; `sms_replies` (Roma), `sms_campaign` and `obituary_mail` (Quinn) were added so nothing sat outside the team. The Fly image ships no `docs/`, so the coverage check runs where the file exists and never breaks a roster load; each employee's brief is `team/<role>/CLAUDE.md`. Ids are roles (analysis, research, sales, ops), display names are personas. Plan doc for Ty and Tyler: https://claude.ai/artifact/9WFWp5o8jcfJqXbZuFtyqH (six decisions open).

```bash
python src/agents/cli.py team        # validate the roster, regenerate TEAM.md
python src/agents/cli.py hello       # meet-the-team, free, no model
python src/agents/cli.py ask "Roma, quick screen 123 Main St"
python deploy/agents_up.py [--commit]  # one-click: app, volume, secrets, deploy, Slack link, DataSift, hello
```

- **Agents decide, workflows do.** An employee's tools are handoff / ask_operator / thread_history, record_lookup, and ONLY the workflows it owns (a teammate's work is a handoff). A long job is queued as a direct task on the same thread, never run inside the turn. A `planned` workflow answers with how it runs by hand, so nobody pretends.
- **Claude Agent SDK lockdown (0.2.180, verified live): `allowed_tools` only PRE-APPROVES, it does not restrict.** Restriction is `tools=[built-ins]` (Read, Glob, Grep, Skill; plus WebSearch and WebFetch for Diggs) + `disallowed_tools` + `permission_mode="dontAsk"` + a PreToolUse hook that re-checks the allowlist and keeps reads inside the workspace (Read could otherwise open the box's environment). `strict_mcp_config=True`.
- **Workspaces live OUTSIDE the repo** (temp dir locally, `/data/workspaces` on Fly): a cwd inside the repo loads this file into every employee. The brief goes in `system_prompt` and is written to the workspace as EMPLOYEE.md, never CLAUDE.md (it would load twice). `CLAUDE_CONFIG_DIR=/data/claude` keeps sessions on the volume, so an ask-back resumes after a redeploy.
- **Reply length is set in the prompt.** The first live run posted multi-paragraph Markdown; "three short lines" in TEAM.md and in every turn's prompt fixed it. `slack_app.clean_text` strips em and en dashes and turns `**bold**` into Slack `*bold*`.
- **Slack is a real app per install** from `team.slack_manifest` (`chat:write.customize` lets each employee post under its own name). Events carry channel IDS, resolved through `conversations.info`; a mention arrives twice (app_mention + message), so intake dedupes on channel + ts; plain chatter in the team channel is ignored; `/slack/events` 404s until SLACK_SIGNING_SECRET is set.
- Three bugs in Tyler's scaffold are fixed in the port: channel routing compared IDs to names (never matched), the double-queued mention, and "good morning" anywhere in a message firing the hello chain.
- Verified: selftest 141 checks with zero network (a fake SDK drives the real guard and handlers); a live SDK smoke from the `C:\pp\_work\agentsdk` venv (a short path: Windows long paths are off), 3 turns, $0.42 over two runs. The scheduler now exists (next section). NOT built yet: the browser job machine, the Fly app (`fly auth login`).
- Keep out of this public repo until reviewed: the Florida underwriting baseline (a third party's method) and the internal API reference in Tyler's zip. His `crm_api.upload_document` (presigned upload to a record's Documents tab) is worth porting after one live test.

## The Autopilot: the FTM lifecycle run by the team (build 1.0.64, 2026-11-11)

`src/agents/autopilot/` + `team/autopilot.yaml` (the playbook) + `src/agents/scheduler.py` (the clock). ty+2 already describes the whole prospecting lifecycle as presets (05 FTM - CALL, 06 FTM - MAIL, 11 DEEP PROSPECTING, 12 REACTIVATION, 13 HOT AUCTION, 20 CALLER QUEUES); nothing moved records between them except the dialer. The autopilot makes the moves, one stage per move, each stage a live roster workflow owned by an employee (Atlas assigns and watches auctions, Diggs traces and scores and takes deep prospects, Quinn mails and reports).

```bash
python src/agents/cli.py autopilot check [--live]       # validate the playbook (--live: every preset title exists)
python src/agents/cli.py autopilot plan [stage ...]     # read-only against the live account
python src/agents/cli.py autopilot run <stage> [--plan]
python src/agents/cli.py autopilot mail-designs         # SiftMail designs + price tiers, read-only
python src/agents/cli.py autopilot batches | release <id> [--commit] [--uploaded]
python src/agents/cli.py schedule                       # next fire times, business-local
```

**Design rules.** The presets ARE the state: a stage reads its presets live by title (10-minute cache, `AUTOPILOT_PRESET_TTL_S`), so an edit in DataSift changes what it works on. Progression rides DataSift's own counters (dialer attempts, mail attempts); no flow tags (Ty). A stage acts only when its playbook `mode` is `live` AND the box runs `AGENTS_MODE=live`; every stage ships in `plan`, which works out exactly what it would do and cost, posts that, and writes nothing (every write goes through `Ctx.write`). Caps, checked before every paid call: stage `daily_usd`, autopilot `caps.daily_usd` (data spend), the brain's `AGENTS_DAILY_CAP_USD`, and a per-record lifetime `caps.record_usd`. **Mail has its own `caps.outreach_daily_usd` and never counts toward the data or brain caps**: the first live plan put both on one $25 cap and the mail lane could send 13 handwritten letters a day against a 589-record backlog. The ledger (agents.db: `ap_events`, `ap_phones`, `ap_facts`, `ap_batches`) records only LIVE actions, so a planned trace never reads later as a trace that happened; every scored number is cached so it is never bought twice. A quiet scheduled run posts nothing; a problem always posts. `/health` carries each stage's last run.

**YAML 1.1 reads a bare `off` as the boolean False.** The loader used to fall through to "plan", so a stage the owner switched off would have started running. `policy.mode_of` maps False to off, True is refused, and the playbook quotes every mode.

**Stage kinds:** `assign` (unassigned records to the active caller with the fewest waiting, 30-minute cadence; the FIRST run draws a baseline and leaves records already waiting alone unless `backlog: true`), `native_trace` (DataSift's own trace, scoped to exactly the waiting records with a dry run first, 20 or more per job), `retrace` (the waterfall: the next source this record has not had, Tracerfy then Enformion; a dead owner or probate record goes to Diggs; every source missed means `after_sources`: mail_only tag, deep prospect or nothing), `score` (Trestle on every unscored number, one tier per phone, mirrors `d4d_call_list.payload_for_tier`), `auction` (Auction Soon true inside `soon_days`, removed once the date passes), `mail`, `reactivate`, `handoff` (a preset's records to a teammate's workflow, a few a day, each once), `digest`.

**The waterfall follows the lessons (5-01): DataSift's free trace first, then at most two paid passes on first-to-market records, SmartSkip ($0.15 a hit, `sources.SmartSkip`, the same client the deep prospect chain uses) then Tracerfy; one extra pass (SmartSkip) on Priority 1 leftovers.** A record counts as REACHED only when a Dial First or Dial Second number lands (`callable_tiers`, lessons 4-05, 14-13, 5-03: only those are dialed); unscored counts, since it cannot be judged and the waterfall must stop paying. A Drop-only find is still written, tagged with its source, and the record keeps going. On a living owner only SmartSkip's SUBJECT numbers land; relatives' numbers belong to the deep prospect. At the day cap a record waits for tomorrow; it is never written off as a miss. Research behind every number: 68 course lessons (751 rules) and the five challenge days plus the four-week workshop that followed (665 rules), every quote checked against its source, saved in `output/autopilot/research/` (gitignored, local only); the playbook cites lesson stems with timestamps and challenge rule ids (C2.2 trace every 30 minutes, C3.1 dial First and Second only, C6.7-C6.9 mail monthly for 12 months on FTM, C11.2 exhausted, C12.1 obituary hold). A scoped DataSift trace runs every slot; a record sent in the last day is not sent again (the 12-hour gate applies only to the account-wide route).

**THE API UPLOADER NEVER SENT THE SALE DATE (found 2026-11-11 by the autopilot's first plan, fixed in code).** `datasift_api_upload.FIELD_MAP` had no "Foreclosure Date", so every foreclosure uploaded from the move to Fly (2026-08-14) on landed with no sale date anywhere: the newest courthouse foreclosures carry only Notice Type, County and Source URL. Hot Auction read 0 while this week's catch-up CSVs named 53 sales still ahead, 9 of them the next day, none assigned, none dialed. Fix: the field is mapped, date fields are converted to YYYY-MM-DD (`iso_date`; the formatter writes M/D/YYYY), an unreadable date is skipped, and `write_fields` retries a refused batch field by field so one bad value never costs a record its Notice Type and County (tests in `tests/test_datasift_api_upload.py`). The sale date is recoverable from the run CSVs: `python src/agents/cli.py autopilot backfill-sale-dates --csv "output/ftm_notices_*.csv" [--commit]` (dry by default; matched 59 of 63 catch-up addresses, 58 to write). **Address matching:** `find_property`'s raw-prefix search misses whenever DataSift standardized the street on ingest ("Street" stored as "St"); `backfill.find_record` searches on the house number and first word, then requires the same house number, street name after abbreviations, and ZIP, and two candidates is no match.

**Contracts verified live 2026-11-11 (read-only count deltas on ty+2):**
- `assigned_to: null` (JSON null) in a search means unassigned (721 of 1,154 FTM records); any string 400s "Must be a valid UUID".
- Date filters take a LIST `[from, to]` (`foreclosure_date`, `last_direct_mailed`, ...); a dict 400s "Expected a list".
- **The courthouse sale date lives in the CUSTOM field "Foreclosure Date"** (`GET /property/<uuid>/custom-field/`, items carry `custom_field.label` + `value`). The built-in `foreclosure_date` on bulk records is a stale artifact (an FTM record in Ready to Call carried 2025-10-21), and `auction_date` / `tax_auction_date` are empty on every FTM record. So nothing ever set Auction Soon: Hot Auction read 0.
- Relative windows: the second token is a POINT (today, yesterday, week, month, quarter, year), the first counts back FROM it. `["36-months","month"]` = 37 months ago to 1 month ago (the app's calculateDateRange). `windows.resolve` converts; an unreadable token is dropped and reported, never guessed.
- The Open API key reads `mail.reisift.io` (SiftMail designs) and `/accounting/direct-mail-pricing/`.

**Moved onto the published API 2026-11-11 (`autopilot/crm.py`, all bodies from developers.datasift.ai):** assign `{"user_uuid"}`, status `{"status"}`, add-tags / remove-tags `{"tags": "<one title>"}` per title, add-notes `{"notes"}`, custom fields via `update-values`, and the scoped skip trace `POST /property/skip-trace/` `{"query": {"must": {"properties": [...], "property_type": "clean"}}, "dry_run": true}` first, then `expected_count` + `tags` (the stage refuses unless the dry run covers exactly the records asked for). Pacing 80 a minute (`AUTOPILOT_API_GAP_S` 0.75); a bulk job needs 20 or more records (`min_batch`), fewer wait. Callers may be named `{name, uuid}`, which skips the unlisted users list.

**Read from the public app bundle, NOT in the published API, so behind opt-ins (off by default):**
- **SiftMail (DataSift's own direct mail, printed by Open Letter Connect)**: `POST /property/direct-mail-campaign/` (`estimate: 1` quotes, `0` + `confirmation_date` launches; touches carry `template`, `external_product_id`, `envelope`, return address, `tracking_phone`). Runs only with `internal_routes: true` on the stage (set for ty+2's playbook, needs one-record staging verification); without it `vendor_for` refuses and the plan says why. Business tier: 4x6 $0.67, 6x9 $0.74, Personal Letter $1.23, Real Penned $1.89. Open Letter product ids: 13 4x6, 14 6x9, 5 Personal Letter, 16 Real Penned. A SiftMail step under 20 pieces waits (bulk floor).
- Mail counter: there is NO documented direct-mail attempt route (sms, rvm, predictivecall and owner skiptrace have one). By default each piece is logged as the tag `Mailed {yyyy}-{mm}` plus the custom field "Last Mailed"; `undocumented_counter: true` adds `POST /property/<uuid>/directmail-attempts/` `{"directmail_attempts": TOTAL}` + `PATCH last_direct_mailed`, first piece read back, a write that did not stick stops counter writes for the rest.
- Bulk actions scope by `query.must.properties` (exclusions at `query.must.must_not.properties`). A top-level `properties` is ignored: that is the 2026-08-19 whole-account trace. The published reference only says the skip-trace `query` matches "the list endpoint's query parameter"; the shape is on the ask list for DataSift.
- Smarter Contact push (`POST /property/smarter_contact/`): `list_id` is the SC list NAME and a `columns.phones` block filters which numbers go. Worth checking against the land push that returned a count and delivered nothing.

**First read-only plan on ty+2, 2026-11-11 (658 reads):** 457 unassigned in the FTM call lane (all backlog by the baseline rule); retrace 50 with no number, of which 40 are dead-owner or probate records for Diggs (3 a day) and 8 are traced via SmartSkip then Tracerfy (up to $1.36); score 501 numbers on 159 records (about $7.51); 86 FTM records on the Obituary list (2 a day); 589 never mailed (about 31 handwritten letters a day at $60); 8 back from Not Interested, all already with the active caller. The deep prospect queue (about $2.60 a record) is the bottleneck.

**Windows are recognised by VALUE, not by a key list**: ty+2's obituary preset carries `last_obituary_date ["24-months","quarter"]`, which no key list had, and the search 400s on the raw tokens. A two-item list converts only when both ends read as time tokens, so statuses and number ranges are never touched.

Selftest: `python src/agents/cli.py selftest` (388 checks as of 11-13 with the autopilot, phase 0, webhook signatures and org-chart coverage, zero network; needs the repo `.venv` for fastapi). Plan doc for Ty and Tyler, tab "FTM autopilot": https://claude.ai/artifact/9WFWp5o8jcfJqXbZuFtyqH#a53d3fd6-2791. Done since this was written: the brain was deployed in plan mode on 11-12 (see "Phase 0 Built") and the sale-date backfill wrote 63 records the same day (60 by the script, 3 by hand). Still open before anything goes live: the three FTM mail designs in Mail Design (ty+2 holds only "Test" and "Ramis Mail List"), the assignment pool (Tinaa is inactive, Adriana is now lead manager) and the backlog of unassigned records (457 on 11-11, 450 on 11-13), the soft-offer amount rule, when the mail lane starts (the presets mail every FTM record from day one for six pieces, the February teaching; the newest teaching mails only records the call and text lane did not reach, for twelve months), how many dials before a record leaves the FTM call lane (presets stop at 3; the newest teaching is 5 full attempts on consecutive weekdays, exhausted at 6), one FTM deploy for the uploader's owner guard (the sale-date fix went out in v27), and one-record verification on staging of the scoped trace, a SiftMail launch and the counter routes (each writes, so each waits on Ty's go).

## The Build Plan: every teaching gap mapped to an automation (2026-11-12)

A plan, not a build: 93 packages in 8 phases close all 335 items of the gap register (what the Challenge and the lessons teach that the system does not do or the map does not show). Data, checker and renderer live in `output/autopilot/plan/` (gitignored, like the research): `plan_a.py` / `plan_b.py` (packages), `plan_d.py` (42 decisions, 26 the owner's), `build_plan.py check | render`. Readable copies: the plan doc's "Build plan" tab and frame 9 of the Figma map. Phase 0 (23 safety fixes) gates everything; nothing was changed in `src/`, DataSift or Fly by the plan.

- **An independent review the same evening found 25 defects in the first draft (84 packages), 6 serious; all are folded in.** Facts it surfaced that bind any build, plan or not: `datasift_api_upload.attach_to_record` PATCHes the owner whenever the payload's name differs, so a re-uploaded notice overwrites a researched owner swap (package 0.16); the SMS reply agent's `_wants_no_contact` already ends mail as well as texts on any texted demand to stop, which is Ty's rule, so the teaching's "never call me still gets mail" is a decision (D40, default: keep his rule); a suppression check at batch LOAD is not enough, it must run again at RELEASE; a lane is per OWNER, because the activity log and the homeowner both are; an attempt keeper must count dials since the record entered its lane or it re-counts history; nothing screened for a death before text 1 (0.21); the brain has to be up in plan mode in phase 0, since phases 1 and 2 depend on it.
- **Quote the research backlog from `output/deep_prospect_batch/cohort.json`, never from an earlier message:** it read 1,461 before the sold repair and 1,494 after (Foreclosure 20, Probate 555, Obituary 919), with 71 of the probates flagged wrong-house by the 11-11 audit.

- **Division of labor the plan rests on:** DataSift sequences react to events and have NO time trigger; the autopilot owns the clock and the counting; both write through documented routes. A clock move is "move the card", which fires the sequences the account already has.
- **Published routes found while planning (all in `output/datasift_api/specs/datasift.yaml`):** `GET /siftline/board/column/{column}/card/{card}/timeline/` returns seconds spent per column keyed by column uuid (time in phase, and the board's column list, with no unlisted route); card PATCH takes `column`; tasks carry `event_type: appointment`, start, end and `set-outcome`; `POST /property/{uuid}/predictivecall-attempts/`, `/sms-attempts/`, `/owner/{uuid}/skiptrace-attempts/` take the TOTAL as an integer; `GET /property/logs/` is the account-wide activity log with a cursor; `POST /filter-preset/{uuid}/scheduled-export/` (weekly, bi-weekly, monthly); `/do-not-mail-ever/`. Unproven until one staging record each: whether an API card move fires `card.moved` sequences.
- **THE CALL COUNTER DOES NOT FOLLOW THE CALLS (measured 2026-11-12, read-only).** Of 130 records sampled from "FTM - 02 Ready to Call" (attempts 0), 18 already had `owner.call.made` events, 10 of 11 in the second sample with `source: kixie`. The call lane advances only on `predictivecall_attempts`, so dialed records never leave Ready to Call. Call events are in the record log (`owner.call.made / answered / noanswer`, payload `call.source`, `destination_number`, `duration`); the log is per OWNER. The fix is plan package 0.5 (an attempt keeper that writes the counter from the log).
- **Census the same evening** (`census_2026-11-12.json`, every preset counted): Ready to Call 515 FTM / 100 Priority 1 / 9,277 Strong / 10,114 Bulk; never mailed 610 / 2,962 / 10,903 / 12,082; Not Interested due a second call 540; rehash 588; obituary call and mail presets 918 and 962 with no stage working them; leads with no task 44; one caller holds 2,977 open records against the taught ceiling of about 2,000.
- **A `\n` inside a Python patch run through a bash heredoc reached the file as a real newline** (same family as the `\b` trap). Patch plan files with the Edit tool.
- **Since 11-13 the plan data has two overlays**, both imported by `build_plan.py`: `plan_x.py` (20 rules from the 21 lessons the first pass skipped, items X01 to X20, decisions D43 and D44, so 355 items, 44 decisions, 27 the owner's) and `plan_p.py` (`PROGRESS`: state, done, found, left per package built; `DECISION_FACTS`). `render` also writes the Figma card data (`doc_tabs/figma_cards.json`, `figma_decisions.json`); `gen_doc_ops.py <phase> <table1> <table2> <rev>` prints compact edits for one phase tab of the plan doc.

## Phase 0 Built: one gate, owner lanes, the attempt keeper and the daily checks (build 1.0.67, 2026-11-13)

Built overnight 11-12 into 11-13 on Ty's go, on top of 15c2225, and committed on `agents-infra` the next morning (PR #26). **The overnight build wrote nothing to DataSift, sent, mailed or spent: every new stage is `mode: "plan"`. What changed the account afterwards, on Ty's answers, is under "Applied on Ty's go" below.** 34 of the plan's 93 packages moved, 22 of phase 0's 23 (0.4, the texting app's disposition table, is the one left). `siftstack-agents` is LIVE on Fly in plan mode (app created 11-12, volume `agents_data`, one machine in iad, version 5 = this working tree; three stages were run ON the box and its log copy is complete) with NO Slack destination: `deploy/sync_agents_secrets.py --exclude NAME` kept every Slack name off the box, because `agents_up.py` would have pushed `SLACK_WEBHOOK_URL`, which is the FTM channel. `siftstack-ftm` (v27) and `siftstack` were not touched overnight; the uploader guard went live with FTM v28 on 11-13. Selftest 435 checks as of 11-13 (158 in `selftest_phase0.py`), run with `.venv/Scripts/python.exe` (system python has no fastapi).

```bash
python src/agents/cli.py autopilot plan <stage> [--json f]   # read-only run of one stage against the live account
python src/agents/cli.py autopilot last [stage]              # each stage's last run in its own words (operator view)
python src/agents/cli.py autopilot runs | undo <run_id> [--commit] | budget | lanes | log | calibrate [days]
python src/agents/cli.py autopilot release <batch> [--commit]     # mail batches AND the attempt keeper's first staged list
python src/agents/autopilot/proofs.py list | run --key-env STAGING_KEY --record <uuid> [--commit]
python src/agents/autopilot/preset_patch.py plan | apply --plan <file> [--commit]   # apply also needs its env switch
python deploy/agents_heartbeat.py https://siftstack-agents.fly.dev                  # the outside watchdog's verdict
fly ssh console -a siftstack-agents -C "python /app/src/agents/cli.py autopilot last"   # what the box's stages said
```

**What exists (`src/agents/autopilot/`), 23 stages in `team/autopilot.yaml` (21 plan, 2 off), 54 roster workflows:**
- `gate.py`: ONE check, 18 rules, eight channels (assign, call, text, mail, trace, score, research, task). Run when a record is picked up AND again when a staged batch is released. A stage lifts a rule only by naming it in `gate_allow`. Deceased is lifted only by `deceased`, or by `heir` on a record carrying the heir tag. A lead is never touched by a prospecting stage.
- `lanes.py` + `lanecheck.py` (stage `owner_lanes`, 02:30): a lane is per OWNER. Order lead > heirs > hot_auction > ftm > obituary > priority1 > strong > bulk; the gate holds an owner's lower-lane records. The FTM timeline is one table the stages read: texts days 0 to 2, the call round ends at counter 5, mail when the round ends or on day 8, at once for a foreclosure or a record with no number, 12 monthly pieces.
- `activity.py`: one shared copy of the account log in `ap_log`, synced oldest day first with a 6-hour overlap and a 45-day floor per group (calls, state, texts, board). Every stage that needs history reads this copy, never the API.
- `attempts.py` (stage `ftm_attempts`): an attempt is a DAY with an outbound dial, counted since the record entered its lane; a person lowering the counter resets the count; the counter never goes down; the first live run is staged as a batch for a person.
- `screen.py` (`ftm_death_screen`): the record, the notice words and the Knox obituary index, before assignment and text 1. A hit tags for research and never marks an owner dead. Probate records are skipped by design, Blount has no index.
- `revoke.py` (`revocation`, hourly), `watchdog.py` (`sequence_watchdog`), `audit.py` (`touch_audit`), `capture.py` (`daily_capture`), `leadcheck.py` (`lead_check`), `fullattempt.py` (`full_attempt`), `calibration.py`, `undo.py` + `ledger.change` (every live write records the value it replaced), `proofs.py` (six one-record write proofs; refuses the production key), `budget.py`.
- Operator tools, read-only by default: `preset_patch.py` (the phase 0 preset exclusions as a proven change list) and `feeder_audit.py` (every SiftMap feeder with its change list). Output in `output/autopilot/preset_patch/` and `feeder_audit/`.

**Rules that are load-bearing:**
- **Plan-mode knowledge is written, plan-mode actions are not.** The owner-lane map, death-screen non-hits, capture rows and lane-entry facts are the autopilot's own records and are kept in plan mode; anything that changes the account goes through `ctx.write`. Revocation keeps separate plan and live marks, or a live run would skip what a plan run only listed.
- **The request budget is shared per app in SQLite (`ap_rate`)**: 45 a minute and 2,800 an hour by day, 70 and 3,800 from 20:00 to 06:00 ET, 20% of each minute reserved for priority work, and a quiet window (`DATASIFT_RATE_QUIET`, default 06:25-07:15 ET, 12 a minute) while the first-to-market box makes its run. A full minute waits; a full hour raises `BudgetExhausted` and the stage says so.
- **The public `/health` page carries counts only.** A run's headline and problem lines name streets, so `brain.public_last` strips them; the sentences are `cli.py autopilot last`. Found before any stage had run on the box.
- **A stage that reads per record must stop reading when it can no longer act.** The mail stage read all 610 never-mailed records (about 1,800 requests, 28 minutes) to tell each one "tomorrow"; it now counts the rest once the day's mail budget refuses a piece (67 requests, one minute). The scoring stage took 754 requests for 568 records and now reads at most 250 a run, never-read records first; the auction stage does the same at 400.
- **The uploader keeps a researched or deceased owner** (`datasift_api_upload.owner_guard`): `attach_to_record` no longer PATCHes the owner on a record tagged as researched or whose owner is marked deceased, and posts a note with the notice's name. A probate notice still writes the court's personal representative. Keeping the owner of record at FIRST upload sits behind `UPLOAD_KEEP_OWNER_OF_RECORD=1`, off.
- **`dispo_buyers` prices an exited flipper at the purchase deed** (`price`, `buy_date`, `exit_price`); the registry files have not been re-run.

**Contracts verified read-only on ty+2 (11-12):**
- Account log: `GET /api/internal/property/logs/?query={"must":{"timestamp":[from,to],"any_event_type":[...]}}&limit<=100&cursor=`, newest first, timestamps `YYYY-MM-DD HH:MM:SS` UTC. **`any_event_type` is the only event filter honored**; `event_type` and `event_types` are silently ignored.
- Calls are OWNER events: `owner.call.made` (a dial, no caller name), `owner.call.answered` (duration and the caller's name), `owner.call.missed` / `received` (inbound). An "answer" is not a conversation: a voicemail greeting answers.
- `owner.phone.status.updated` is written by Kixie itself (1,657 in 45 days), so a disposition import may not be needed. Smarter Contact and smrtPhone texts are NOT in DataSift's log (only `owner.sms.failed`).
- Records search: `{"owners": [uuid]}` returns exactly that owner's records; `any_property_status`, `any_lists`, `any_tags` work as positive filters; `created: [from, to]` slices a scan past the 10,000-row wall. A search row carries one representative phone, and that row is the ONLY place the do-not-call registry flag shows.
- **No published route lists every status.** `/api/internal/global-status/` answers 7 rows, one per lead pipeline heading, naming 8 of ty+2's 30 statuses (follow_up, sold, prospecting, Exhausted and Auction Date Passed are not among them); the unlisted `/api/internal/status/` returns all 30. The preset tool therefore keeps a table of the stored spellings and uses global-status as a spelling check. The audit's note calling global-status a replacement was wrong and is corrected. Run `python tools/datasift_api_audit.py --check` after adding any route.
- **`CRM()` uses the Open API key first** (changed 11-13). `datasift_api_upload.Api()` logs in with a password whenever the box carries one, and this workstation's `.env` does, so every local autopilot run before the change was on a login token while the Fly box was on the key. The log, users, preset, tag, list and status routes were then re-checked on the key.

**Read-only findings on the live account (11-12 and 11-13), each from a plan run in `output/autopilot/night_2026-11-13/`:**
- 36 call counters in the FTM call lane are behind the call log (27 dialed once and still reading 0).
- 535 owners sit in two lanes once Priority 2 is counted (the box's own 02:30 run on 11-13 read lead 68, ftm 946, obituary 1,178, priority1 4,344, strong 17,206); 1,293 of their records would be held. Without Priority 2 it is 41 owners and 62 records.
- 152 records became untouchable in one day (138 sold); 175 of those owners' numbers are still contacts in Smarter Contact.
- 257 of 568 numbers shown on FTM call-lane rows carry the do-not-call registry flag (45%); 38 of 100 on Priority 1 Ready to Call.
- One caller's day: 234 dials to 196 owners, 185 "answered", 6 conversations of a minute or more, 63 minutes of talk.
- The touch audit for 11-11: 156 dials, 12 that should not have happened (3 to owners whose house had sold, 1 with an obituary on the record, 3 to numbers already marked wrong or dead, 3 numbers dialed three or more times, 2 outside calling hours). For 11-12, run on the box: 234 dials, 22 flagged (8 repeat numbers, 6 to owners whose house had sold, 2 outside hours) and 3 caller ID lines over 25 dials in the day (32, 29, 28): 30 origin numbers appear, ten of them carrying 10 or more dials.
- 68 leads: 18 with no confirmed number, 13 still New Lead after a day, 9 with no open task; median 18.8 hours from New Lead to the first call.
- 66 of 66 sequence triggers produced their result. 6 foreclosure sales sit inside 45 days, 4 inside 14.
- Tier calibration over 45 days (`autopilot calibrate`): 1,556 numbers marked on 1,283 owners. 875 carry no tier today (56%). Dial First was the owner's number 46% of the time (255 of 552 settled) against 37% for no-tier numbers; Dial Second to Drop have 78 marks between them, too few to judge.
- Dry runs with the gate in front: assign 22 new records (450 older ones left, 8 held); mail 31 letters for about $59 of 610 never mailed (7 held, no SiftMail design exists yet); score 562 numbers on 172 records for about $8.43 (21 held); retrace 9 of 51 no-number records for up to $1.53, 40 to research.
- Presets: 98 read, 68 would change, 5 to create, 1,986 preset memberships leave (883 Obituary list, 824 Do Not Market, 658 DNC list, 212 Probate list). Two rules wait on a decision: "deceased: no" is the only way a preset can skip a dead owner and it also drops 208 records whose mark is blank, and the Obituary rule is left off the FTM lane because 137 FTM records sit on both the Obituary and Probate lists by nature. Do Not Market records sit in live presets today: 168 in Hottest Mail 01, 152 in the caller's Assigned (All), 105 in her To Start and 47 in her Actively Prospecting queue. Feeders: 49 filters, 35 adding daily, 33 changes proposed, 23 adding daily afterwards.

**Traps met while building:**
- **A function-local `from agents import store` makes `store` local for the WHOLE function**, so an earlier `store.init()` in the same function died with UnboundLocalError and every `autopilot` command broke until it was removed.
- A bash heredoc whose body holds triple quotes fails ("unexpected EOF"): write the patch script to a file, then run it.
- A log row older than the sync mark is never read, hence the 6-hour overlap.
- **A day with more log events than one run may read was read from the top on every run, and the mark never moved past it.** 2026-10-19 is such a day (a 14,998-record pull writes tens of thousands of events). It showed up only when two stages were run ON the box, whose empty database syncs 45 days back; the workstation's copy reached back three days and never met that day. The reader now keeps the oldest row it saw (`autopilot:log:<group>:partial`) and the next run reads only what is older. Same lesson as the City's 403: a stage verified only from the workstation is not verified.
- A watchdog verdict written in jq could not be tested on this machine, so the logic lives in `deploy/agents_heartbeat.py` (standard library, driven by the selftest); the workflow is inert until it is on `main`.
- The API audit follows every module in the autopilot package: one unlisted route in an operator tool downgrades all 23 stages.

**First unattended morning (11-13, read-only check at 07:36 ET):** the box ran owner_lanes at 02:30, the death screen at 07:00 and 07:30 (60 records a run; 3 possible deaths in the second), the sequence watchdog at 07:20 (66 of 66) and the native trace check on its own; 346 requests in the 07:00 hour, no refusals; `deploy/agents_heartbeat.py` read ok. The FTM pull the same morning (v27, not touched by this build) ran 06:39 to 06:56 ET inside the brain's quiet window: notices EMPTY with a healthy scrape behind it (login fine, all 20 result pages of each saved search read, nothing unseen), liens 4 records updated for released liens, county 18 uploaded with 0 failed, and the sold stage's first committed run revived 3 leads and marked 9 real sales.

**Applied on Ty's go, morning of 11-13.** His six answers: commit; apply both change lists to ty+2 ("I want to get it right in there first"); run the proofs on our own account; make the webhook through a sequence and use the new Slack channel; yes to the recommendations on D18, D32 and D40; deploy the first-to-market box. Every autopilot stage is still `mode: "plan"` and the brain is still dry.
- **Feeders** (`src/agents/autopilot/feeder_apply.py`, `tests/test_feeder_apply.py`): 33 changes applied, each read back, with an undo log (`output/autopilot/feeder_audit/applied_2026-11-13.jsonl`; `feeder_apply.py undo --log <file> --commit` with `FEEDER_APPLY_ALLOW=1`). 23 feeders add daily, down from 35; the three that must stay off are off. **SiftMap filter contract:** an object is `{name, is_active, auto_add_enabled, replace_owners_enabled, tags, lists, filter_data: {filters, addresses}, limit, scope, ...}`; `tags` is a list of TITLES; a PATCH of `auto_add_enabled`, `tags` or `filter_data` works on the Open API key and reads back; the listing pages at 10 rows (follow `next`). The tool never switches auto-add ON and refuses a field it does not know.
- **Presets** (`preset_patch.py apply`, `--commit` AND `PRESET_PATCH_ALLOW=1`): 68 changed and 5 created (a Call Attempt 4 preset in the Hottest, Strong, first-to-market and obituary call lanes, and "03b No Live Number -> DP"), after the tags Litigator and Heir Identified were created. A fresh plan over all 103 presets proposes nothing. Preset memberships 58,800 to 56,784. **A preset title is unique per ACCOUNT** (a second create answers 400 "The fields title must make a unique set."), so apply is resumable: a same-title preset whose filter already matches counts as done. Left out on purpose: the deceased-owner rule (it also drops 208 records whose mark is blank) and the Obituary rule in the first-to-market lane; the returned-mail filter could not be proven. Backups in `output/backups/` (`presets_ty2_2026-11-13_before.json`, `siftmap_filters_ty2_2026-11-13_before.json`).
- **Write proofs:** all six passed on ty+2 on the key against one test record. The call, text and skip trace counters take the TOTAL; a status clears with `{"status": null}`; the scoped skip trace dry run covered exactly one record. The record reads as before; an empty tag "Proof Test" remains.
- **FTM v28** (11-13, from 0d9dbf1): the owner guard is live, with one fix found on the re-read: the "owner was kept" note is posted once, not on every daily re-send of the same notice.
- **Webhook:** `setup-datasift --staff-api --commit` created the webhook and repaired "Send to Deep Prospecting" (card into Check Relatives, `send-webhook`, active). The command now proves itself: it calls `POST /api/internal/integration/webhook/{uuid}/test/` (the app's Test button, `{"success": true, "status_code": 200}`) and waits for the brain's own receipt. One real status change on the test record reached the brain in about 3 seconds, signed and verified. The delivery body carries the WHOLE property object (about 80 fields, owner included) plus `account_id`, `webhook_id`, `sequence`, `event_id`, `event_datetime`. A webhook object is `{uuid, name, url, enabled, created, created_by, auth}`. A SiftLine card names its record in `prop`, and card DELETE is a published route. In 45 days only two records were moved to Deep Prospecting, both our own tests. **To test a trigger without paying for research, seed a finished `deep_prospect` task for that record on the box first: the 24-hour dedupe then queues nothing.**
- **Slack is one channel** (`AGENTS_SLACK_CHANNEL`, a channel ID kept in `.env` and as a Fly secret, never in this repo). `slack_app.home_channel()` sends every post that belongs to no thread there, still under the employee's own name. The app joins a public channel by itself on `not_in_channel` (`channels:join` is in the manifest; a private channel needs an invite). `brain.first_hello()` has the team introduce itself once on the first boot with working keys, marked only when the opening message landed. `/health` carries `slack_one_channel` and `slack_first_hello`. **A stage in plan mode says its plan once a business day** (`sweep.plan_repeat`): it writes nothing, so every run finds the same work, and the death screen and the scorer would have posted the same plan every 30 minutes. A live run, a Slack ask and a new problem are never held back. Ty created the app from the one-click link (`team.manifest_url`) the same morning; its bot token carries exactly the manifest's ten scopes.
- **Socket Mode is the second way in** (`src/agents/slack_socket.py`, `SLACK_APP_TOKEN`, an app-level token `xapp-...` with `connections:write`). Asked for the bot token and the signing secret, Ty pasted the bot token and an APP-LEVEL token: Slack's own post-create screen pushes the Bolt starter template, whose two values are those. So the brain now listens both ways: the Request URL when `SLACK_SIGNING_SECRET` is set, a connection it opens itself when `SLACK_APP_TOKEN` is set (no URL check, no signing secret); one handler (`intake.handle_body`), duplicates dropped by event id. Each envelope is acknowledged BEFORE its event is handled (Slack allows three seconds). `websockets>=15.0` is in `deploy/requirements-agents.txt` and imported late, so the brain starts without it. **An open socket proves nothing:** with Socket Mode switched off for the app, `apps.connections.open` still answers and the connection just stays silent. Hence the hearing check (`brain.hearing_notice`): the team's own posts come back as events, so if nothing has arrived three minutes after the hello lands, the app says once, in the channel, that it can post but cannot hear, with the setting that fixes it. `/health`: `slack_socket`, `slack_heard`, `slack_hearing`.
- **Two-way Slack went live 11-13 at 09:50 ET, and the hearing check earned its keep the same hour.** The hello landed a minute after the invite, each employee under its own name. Three minutes later the brain had heard nothing and said so in the channel: the app was created with a Request URL and Socket Mode OFF, so Slack was POSTing every event to `/slack/events` (visible in the Fly log as a run of 404s, since that route stays shut without a signing secret) while the open socket sat silent. Ty switched Socket Mode on; the brain then needed one more event before it could confirm, and nothing arrives until somebody writes, so both sides waited. One reply posted with the bot token ended that, and the note now asks for a message. While it hears nothing on an open connection the watch also asks for a fresh connection every two minutes (`slack_socket.refresh`), and the moment an event arrives it says "I can hear this channel now" once.
- **A private channel is invisible to the app until it is invited** (`conversations.info` and `chat.postMessage` both answer `channel_not_found`, and `conversations.join` cannot help). The SiftStack channel is private, so the first hello waits on `/invite @SiftStack Team`; the brain retries every minute for half an hour, then every five minutes for a day.
- **The selftest is sealed off from Slack.** With a real bot token in this machine's `.env`, a selftest run would have called Slack for real. `selftest.main` blanks every Slack setting and puts a `NoNetwork` guard where `requests` stands until a section installs its fake. Any new setting that can reach a live service gets the same treatment.
- **Research on the box runs on the low-cost engine** (`AGENTS_RESEARCH_ENGINE = "open"` in `fly.agents.toml`, decision D18). Probed ON the box: search through the SerpApi plan (Serper still answers "Not enough credits"), the obituary index, and a tool call from both models. A record moved to Deep Prospecting while the brain is dry is researched and nothing is written back.
- **D18, D32 and D40 are decided as recommended** (`plan_p.DECIDED`; 24 of the owner's 27 decisions stay open). Under D18 the first tranche, the 20 foreclosures with a sale still ahead, was started the same morning from the workstation (`deep_prospect_batch.py run --list Foreclosure --commit --workers 2 --max-usd 30 --daily-cap 40`; log `output/deep_prospect_batch/run_foreclosure_2026-11-13.log`). The cohort's tag gate already leaves out the 7 records researched on 11-12, so all 20 are new. The two re-runs of the 11-12 "status unknown" files were not part of that yes.
- **Traps met:** a background shell task given an explicit timeout can be killed at that timeout, so a long paid job is launched detached (PowerShell `Start-Process` with redirected output) and read from its log. A batch stopped seconds after it starts leaves `running` rows in the local agents.db (the one-record runner uses `force=True`, so they do not block a relaunch; close them anyway). A selftest must reset every setting this machine's `.env` can carry (`AGENTS_SLACK_CHANNEL` made a "without the setting" check fail). A patch script whose prose holds an apostrophe breaks a bash heredoc: write it with the file tool.

**Waiting on Ty:** the search pack and page-open credits, which the probate and obituary research tranches wait on; the two re-runs of the 11-12 files; the two preset rules left out (deceased owner, Obituary in the first-to-market lane); the first-upload switch on the uploader; and the go to take any stage from plan to live.

## The Build Wave of November 13: phases 1 to 6 in plan mode (build 1.0.68, 2026-11-13)

Ty, 11-13: "move forward on everything, and keep building out all of the workflows in depth." Ten tracks built in parallel (one agent each, each owning its own files, briefs in `output/autopilot/build/BRIEF_COMMON.md` and `BRIEF_WORKFLOWS.md`), integrated and committed the same afternoon (39df225 to 232638a, pushed to `agents-infra`, PR #26). **Every new stage ships `mode: "plan"` and every new behavior in the texting app and the first-to-market box is behind a switch that defaults to today.** The brain (`siftstack-agents`) is deployed with 57 stages in plan mode and 2 off; the texting app and the first-to-market box were NOT redeployed (nothing there changes until a switch is set, and a Friday deploy of an unattended Monday pipeline buys nothing). Selftests: agents 1,831, texting 642 (383 before), first-to-market 613 tests (252 before), all zero network. Plan data: 21 built, 58 built and off, 10 partial, 3 new, 1 in flight.

**Extension points (use these, never an if-chain):** a stage kind is one module plus one line in `src/agents/autopilot/registry.py` (`kind: (module, run fn, spend class, needs preset, always reports)`); a workflow the team runs on request is one module in `src/agents/workflows/` with `run(task)` and `render(task, result)` plus one line in its `REGISTRY`; a selftest section is one line in `selftest.EXTRA_SECTIONS`; a batch kind is one line in `agents/release.py` `RELEASE`. A workflow's declared roster `inputs` arrive in `store.payload_of(task)["inputs"]`; its files go in `RUNS_DIR/<workflow>-<task id>/files/` and the brain serves them signed (`slack.file_url`). `policy.parse` runs each registered kind's `block_problems`, so a mistyped block is REFUSED when the playbook loads (a test that needs a bad block builds it on a parsed policy after the load: `selftest_textlane._unchecked`).

**What was built, by area (each a stage kind in `src/agents/autopilot/`):** lead management `leadclock` (lead clock and speed to lead), `drips`, `leadtasks` (missing task, weekly pass, nurture), `appointments` (plus the `book_appointment` workflow), `exits`; text lane `textlane` (opening texts 1-3, follow-up series ending in the breakup), `limits` (the house limits, asked before anything is staged and checked nightly), `scrub` (D32: a registry-flagged number is remembered for good, never texted, held from calls); other lanes `secondchance`, `callerbook`, `dialexport`, `auctiontasks`, plus tranches for Strong and Bulk in `lanes.py` and the second trace waterfall in `stages.py`; KPIs `alarms`, `goals`, `dealsource`, `cadence`, `coachpick`, `researchstats` over one catalog `team/kpi.yaml` (loader `kpi.py`; `autopilot check` fails on a catalog problem); research `researchqueue`, `heirlane`, `obituary`, `probate`, `paidlookup`; data `laddercheck`, `conventions`. The research write-back's rules are pure functions in `src/agents/research/writeback.py`.

**Releasing a staged batch:** `agents/release.py` routes every kind (mail, attempts, text, leadclock, drip, heir_mail, heir_text, obituary_mail, probate) for the command line and Slack. In Slack: `release batch N` says what it holds, `confirm N` (same person, 15 minutes) queues it; only Slack ids in `AGENTS_RELEASERS` (empty today: command line only), never on a dry brain (checked at confirm and again in the worker), and the command is read by a pattern before any employee sees it, so a model never releases. A file kind (heir and obituary letters) takes `confirm N uploaded` after the upload.

**Slack noise rule (`AGENTS_PLAN_POSTS`, default `digest`):** a plan-mode stage posts on its own only when its SET of problems changes (counts inside a problem are ignored), so a pending decision is said once, not every morning. What each plan-mode stage would do is in the morning digest (`stages.plan_lines`, up to 16 lines for the digest only). `daily` restores one plan post per stage per day; `off` posts nothing but the digest.

**Defaults that changed live behavior (all safety-positive):** the research write-back writes only Dial First and Second numbers (Third only when nothing better; replay of 28 runs: 113 numbers instead of 127) and holds back a decision maker field naming a dead or unverified person and a deceased mark on a living current owner; the gate lifts `return_mail` for the `heir` allow word (a heir letter goes to the heir's own address) and treats the tag `Estate Sold` as sold; `knox_ftm_pull` rejects a record with no list instead of uploading it onto none.

**Account changes made on 11-13 afternoon (ty+2, each read back):** decision D29 applied: sequences "Sold Property Removal" (an exact duplicate) and "Sold Property Cleanup V2" (strips lists and blanks the status) switched OFF, snapshot `output/backups/sold_sequences_ty2_2026-11-13_before.json`; tags `Verify Deceased` and `Auction Critical` created; preset "Obituary - All" (the Obituary list, no date window, the exclusions of "02 Obituary / Deceased", 966 records) created. `autopilot check --live`: 104 presets, none the playbook names missing. The 11-11 probate address audit is on the brain at `/data/probate/` (the probate stage reads it).

**Go-live checklist (nothing below is done; each is a deliberate act):**
- Texting app switches, in this order (`src/sms_agent/README.md` "Build plan switches"): `SMS_AGENT_RATE_BUDGET=count`, `SMS_AGENT_GATE=report`, `SMS_AGENT_OWNER_REVOKE=1` with `SMS_AGENT_SEND_RECHECK=local`, then `GATE=enforce`, `DISPOSITION_TABLE=1` (after Ty reads `cli.py dispositions`), the extra Smarter Contact labels, speed to lead `held` (copy unreviewed), hand raise to New Lead (after 2.1). FLY SECRETS OVERRIDE `[env]`.
- First-to-market: deploy on a weekday after the 06:30 run, then on the box `ftm_runner.py --doctor` ("stages beyond today's five: none"), `ftm_health.py --selftest` (55), `ftm_budget.py --selftest`. Feeds one at a time, two mornings each: tax_sale, tax_delinquent, appointments (dry run first to confirm the APPT and TD labels), tax_sale_report, code_violations, deeds, sold_reentry/sold_audit, public_notices (saved searches created first). Each new list and field (Notice Text, Mortgage Debt, Listing Status, Tax Auction Date) created and one record read back before its switch.
- Research write-back switches (`AGENTS_DP_*`), each after one staging proof: level, heir tag (only once an heir-lane preset exists), skip trace counter, litigator, document, owner change.
- Stages: one at a time, two mornings in plan, then live with a named releaser (D37). Tags each needs created when it goes live: Research Queued, Two Sources, Needs Fix-Up, Return Mail, Still Owned After Sale, "Obituary, No Probate", Probate Filed, Paid Lookup, Paid Lookup Miss, Wrong House, Estate Sold, Verify Number; seven deal-source custom fields; the research column ids in `research_queue`. When `research_queue` goes live, switch `ftm_deep_prospect`, `ftm_return_mail` and `ftm_no_numbers` off.
- Decisions that gate stages: D2, D5, D10, D11 (SmartSkip on every FTM record is commented out in `ftm_retrace`), D13, D19, D20, D23, D24, D27, D30, D33, D34, D36, D37, D38, D39, D41.

**Traps met this wave:**
- **A usage-limit cutoff stops every running agent mid-edit.** Each Edit is atomic, so files are whole but work is partial; `SendMessage` to the agent id resumes it with its context (far cheaper than a new agent). Resume four at a time, not ten.
- **The texting selftest reached the live DataSift account** (about a dozen record reads per run on invented ids). It now has an offline client and a socket guard, and the agents selftest blanks every Slack setting and puts a `NoNetwork` guard where `requests` stands.
- **`tests/test_e2e_obituary.py` ran a live web search and model call when pytest collected it,** and carried a real owner. It now runs only by hand with the record on the command line. Never run `pytest tests` over the whole folder without checking for import-time work.
- **Test phone numbers on a real exchange** (865-880, 865-881) could be someone's line: tests use the never-assigned 865-000 and 865-001 exchanges or 555.
- **One worker thread runs every task,** so five 8-minute research runs would hold every scheduled stage for 40 minutes: `research_queue.max_open` is 2.
- `tools/datasift_api_audit.py` now skips `selftest*` modules, whose routes are fakes.
- An agent told a file is "read only" can still need a change in it: the brief's report section ("Problems I saw outside my files", "Integration (e)") is where those come back, and the orchestrator makes them.

## Wave 4: the hand-run workflows go live, and files in Slack (build 1.0.69, 2026-11-13)

Ty, 11-13: "in the way that we use the comping workflow to paste in a boundary and some quick notes, or the way that we use rehab with the photos... do we have the functionality to do that?" Answer, built the same day: yes, in Slack. Commits ccd0481 and e5df8ab (PR #26), deployed to `siftstack-agents` from a clean detached checkout (`C:\pp\_work\agents_deploy`) so nothing an agent was still editing shipped. Selftest 2,328, zero network.

**19 workflows went from planned to live** (one track per employee, brief `output/autopilot/build/BRIEF_WORKFLOWS.md`): Atlas `market_report`, `idle_records`, `list_pull`, `land_desk`, `account_setup`; Diggs `skip_trace`, `phone_score` (the autopilot's own waterfall and scorer, scoped to one ask), `entity_principals`, `probate_property`, `buyer_registry`; Roma `quick_screen`, `underwrite`, `offer_sheet`, `deal_buyers`, `lender_package`; Quinn `call_coaching`, `kpi_report`, `qa_batch` (never releases), `outreach_health`. Retired: `daily_digest` (autopilot_digest is it), `sms_campaign` and `obituary_mail` (their org-chart agents moved to `ftm_texts`, `ftm_followup_texts`, `obituary_lane`, `sms_replies`). Org chart still 90 agents, 81 run by a workflow, 0 unassigned. The four first-to-market box entries stay planned with manual lines that say what really runs there.

**Files in Slack (`src/agents/attachments.py`).** Intake records a message's files on the thread (it has three seconds to answer Slack); the worker saves them into the working employee's own workspace (`inbox/<channel>-<thread>/NN-name`) when its turn starts, where the Read guard lets the employee open them. A photo is saved as Slack's 1024px preview (also turns an iPhone HEIC into a JPEG); a KMZ is unzipped to its KML. A message with files starts its turn `AGENTS_FILE_SETTLE_S` (40) later and every further message on the thread JOINS that waiting turn (`store.queued_ask` / `join_ask`, tasks gained `not_before`), so a walkthrough posted ten photos at a time is one turn. Files posted with no ask are held 30 minutes for the same person's next ask in that channel. Limits: 60 files a thread, 20 MB a file, kept 14 days, a turn carrying files gets `AGENTS_FILE_TURN_USD` (4). **The app needs `files:read`.** It is in the manifest now; the app created 11-13 does not have it (read off auth.test's X-OAuth-Scopes), so Ty adds it under OAuth & Permissions and reinstalls once. Without it the thread is told once what to change and `/health` `slack_files` reads `blocked`. Intake passes only the `file_share` and `thread_broadcast` subtypes; edits, joins and the bot's own posts are still dropped.

**Boundary and notes (`comp_package.parse_boundary`, `parse_card_notes`).** A box `lat_min,lat_max,lon_min,lon_max`, two corners, lat,lon points in either order, KML `<coordinates>`, GeoJSON, or `radius 0.75`; anything else gets one sentence naming the shapes. Notes: county card facts beat the record and the photos; a bare year counts only beside another card fact ("roof replaced in 2022" is not the year built). Comps come from SiftMap deeds first (documented search plus capped detail reads: 15 a screen, 25 an underwrite, 150 deal buyers, 300 a day), and a Slack ask may also pull Zillow sold data (`OPENWEBNINJA_API_KEY`, a Fly secret since 11-13, `AGENTS_COMPS_DAILY_USD` 5; the per-call price is a guess, `AGENTS_ZILLOW_CALL_USD`). A screen started by a webhook or a stage never pulls Zillow. `underwrite` with a walk and no boundary builds the rehab on its own: photo findings (`photo_notes`), one number, repair detail.

**Roster input `address`, not `record`, on quick_screen, underwrite and deal_buyers.** `tools.py` resolves a `record` input and then refuses the same workflow on the same record for 24 hours (the webhook dedupe), so a re-ask with a new boundary or new notes would never run.

**Market Finder API contract (verified live on the key, 11-13).** A county is filtered by `fips` and a page is `offset` + `limit`, the names DataSift's own Market Finder page sends (its code is a lazy-loaded chunk; `main.min.js` holds only the API client, chunk file names come from `__webpack_require__.u`). `county=` and `page=` are SILENTLY ignored: `county=` returned all 33,784 ZIPs in the country starting in Alaska, and the pull's own "ZIP outside the county" check refused it rather than score Knox off them. Knox now reads 31 ZIPs (all in the county) and all 121 neighborhoods. The command line still defaults to the browser (`--source api` works).

**Measured costs (11-13, real turns through the brain's own runner, Opus 5.5 at default effort, $4 in / $20 out per MTok, cache reads $0.20, 5-minute cache writes $5).** The ledger held no employee turn before this, so these are the first real numbers: a comp ask with a pasted boundary and notes $0.12 (4 round trips, 10 s; $0.10 of it is writing her ~20,000 tokens of standing context to the cache, her own instructions being 8,372); a rehab ask with 20 walkthrough photos $0.31 (24 round trips, 50 s, about a penny a photo). The jobs run no model (the rehab workbook 1.6 s, $0). A Slack 1024px preview is 1,039 tokens on the token counter, a full-size phone photo 4,743. The Zillow per-call price is not recorded anywhere (`AGENTS_ZILLOW_CALL_USD` 0.01 is a guess). `spend_report` (every employee) reads the real figures from the ledger by thread, day or week. The quick_screen roster line `model: claude-sonnet-5-5` is parsed and never used.

**The same measured turn found that a model writes the walk in its own shapes** (single_scenario true, target_config a sentence, work_done as sentences, flags keyed "flag" with no cost) and underwrite crashed; `quick_screen.normalize_walk` reads those, says each change, and moves an unpriced flag to the open items. Always run a model-fed workflow on a real model's output once, not only on fixtures.

**Query tools (`src/agents/query.py`, every employee, `AGENTS_QUERY_TOOLS=0` removes them):** `records_query` (count plus up to 25 rows by preset, list, tag, status, dates, assignee or counters; only filter keys proven to change the count; never a phone or owner name), `siftmap_count` (an area, count only, no detail reads), `activity_query` (the log copy), `team_query` (the brain's own tables). At most 8 requests a call. Counts are the clean tab by default; `include_incomplete` adds the app's second tab. Verified live: Probate list 756, Obituary - All 908 clean + 58 incomplete = 966, both matching the counts on record. Not offered because unproven on the search: ZIP, city, county, value, equity, deceased.

**Claude Code parity (Ty, 11-13: "for literally every single thing that we would type out in the Claude code, we need to be able to query inside of SiftStack in these agents").** Three pieces, all live and verified on the box the same day:
- **Query tools** (above).
- **Scripts from Slack** (`src/agents/commands.py`, `workflows/run_script.py`, tools `script_catalog` and `run_script`). The catalog `team/commands.json` is generated by AST and never imports a script; `python src/agents/commands.py --check|--write`; the selftest fails on a stale catalog, and email addresses in script defaults are masked because the file is public. 138 scripts: 53 runnable, 28 wired, 32 retired, 24 browser, 1 missing package. A read or dry run is queued and runs with the paid-vendor and outreach keys removed and `AGENTS_MODE=dry`; a write flag, a paid vendor or SiftMap detail reads stage a `command` batch for a named releaser. One script at a time (one worker), 20-minute limit. Box test: shard_guard ran and linked its log, main was refused (browser) with the workstation command, `sold_guard daily --commit` was staged. **Adding a package to `deploy/requirements-agents.txt` changes the catalog** (pandas moved two demo exports to runnable): rerun `--write`.
- **Code sandbox** (`src/agents/sandbox.py`; tools `run_python` and `export_data`; Ty approved it 11-13). How it isolates: `unshare --net` plus `setpriv` to the image's `sandbox` user; six fixed environment variables; no_new_privs; CPU, memory, file and process limits; a 180 s wall clock; a write watch; a root-owned bootstrap that re-checks every step from inside; `/data` made 0700 at startup. It fails closed. `export_data` writes CSV inputs: records without phones or emails, the log copy, and the team's tables. **Run `python -m agents.sandbox --check` on the box after any deploy that changes the image.** 11-13 result: 26 of 26 PASS, after one false positive (`FLY_PRIVATE_IP`, named like a key, is the machine's own address in /etc/hosts). The brain moved to shared-cpu-1x with 2 GB (`AGENTS_SANDBOX_MEM_MB` 1280) so pandas fits beside a turn.
- **Not built:** a worker on Ty's PC for browser and local-file jobs (offered, no answer yet). Run folders on the 1 GB volume are not pruned.

**Open, Ty's call:** (1) `buyer_registry`'s nightly slice reads SiftMap detail without adding the records, which DataSift's acceptable use calls scraping even inside the limits: it is wired on an ask only, schedule off, `/data/dispo_buyers` not seeded. (2) Its CRM write-back puts buyer phones on the record's owner without checking the owner is the buyer (a pending flip's CRM owner is often the previous seller); not in the nightly job. **Open, ours:** `land_desk` renders only from an `analysis.json` copied to `/data/builder_land/<market>/`; the first live SiftMap address search and record-note read-back of the deal workflows are unverified; `post_walkthrough` run with no walk still prices mid and gut as an added 3/2 (the workflows pin the existing layout; changing the CLI changes its numbers).

**Traps met:** the clock fires only LIVE roster workflows, so a schedule on a planned entry is description; a helper named `test` in a test file is collected by pytest as a test (`fixture 'fn' not found`); a check that uses a workflow as its "planned" example breaks the day that workflow goes live (`selftest_team` now flips the status in the test instead).

## DataSift Open API: the published contract (2026-11-11)

DataSift publishes the API SiftStack had been reverse-engineering: https://developers.datasift.ai (guides + reference; OpenAPI bundles at `/_bundle/apis/datasift.yaml`, DataSift API 2.0 on apiv2.reisift.io, 287 operations, and `/_bundle/apis/siftmap.yaml`, SiftMap 1.0, 15). Most routes we found in the app bundle are now documented under `/api/internal/`. Rules that bind every integration, skill and agent (Acceptable use is part of the Terms):

- **Auth is the Open API key only** (`Authorization: Api-Key`, no expiry, inherits its user's permissions). A bearer token copied from a browser is "being restricted" and throttled lower. JWT, password mint and impersonation stay on OUR accounts; a user's install never has them.
- **Rate limits per ACCOUNT, shared by every key-based tool on it:** 120/min and 5,000/hour for account and record work, a separate SiftMap counter of the same size; a 429 says "Expected available in X seconds" or sends Retry-After. Sustain about 80/min.
- **Bulk jobs:** 20+ records, about 1,000 per job, 2-3 jobs a minute; under 20 use per-record routes; many small jobs read as abuse.
- **Prohibited:** SiftMap detail pulls in volume WITHOUT adding the records (scraping, even inside the limits), volume through SiftMap autocomplete (use `POST /properties/search/`), partner namespaces (calltools, launch-control, reirail, smarter-contact, smrtphone, smrtdialer, xencall, kixie, aircall, twilio, plivo, dataflik), `GET /properties/images/`, and user/billing/account administration (reading users is allowed).
- **App-only (removed from the reference 2026-11-01 with the duplicate mirror routes):** webhook and integration setup, billable addon settings, owner merge/swap, deleting SiftLine boards, deleting custom fields. Sequences are not in the API at all.
- **Webhooks:** outbound, `sequence.action.triggered` + `webhook.test`; body `{event_type, event_id, event_datetime, sequence{uuid,name}, property{uuid,address,owner,tags,lists}}`; Ed25519 signature `X-Sift-Webhook-Signature: v1,<b64>` over `f"{X-Sift-Webhook-Timestamp}."` + RAW body, key by `X-Sift-Webhook-Key-Id` from `GET https://apiv2.reisift.io/webhook/signing-keys/` (no auth); up to 3 retries, dedupe on `event_id`. Created in the app (Settings > Integrations > Webhooks, Super Admin or Sensei), used from Sequence Builder.
- **Versioning:** 90 days' notice, Deprecation + Sunset headers; beta can change without notice.
- **Documented replacements for routes we still call:** property message board -> `/api/internal/owner/{owner_uuid}/message/` (+ `/pin/`); `/status/` -> `/global-status/` (no create); per-record `add-tags`/`remove-tags`/`add-lists`/`add-notes`/`assign`/`status`; SiftLine card create on `/siftline/board/column/{column_uuid}/card/`; Market Finder is fully documented (`/api/internal/market-finder/...`, replaces the Playwright scrape); record documents (`/property/{uuid}/document/` + presigned url: the deep-prospect PDF can go on the record); `/property/exists/`; `/properties/add-properties/quote/` before spending the allowance. NOT in the API: the account users LIST (`GET /api/internal/user/` is the key's own user), direct-mail attempts, SiftMail, sequences, board columns, upload usage, skip trace stats, custom status create, bulk tags by query.

**Audit + coverage (run after touching any DataSift call):** `python tools/datasift_api_audit.py [--refresh] [--check]` scans every route string against the cached spec (`output/datasift_api/specs/`, gitignored), follows each roster workflow's modules (sources, covered agents' files, module-scope imports; function-scope imports are cautions), and grades it. `KNOWN` reviews every unlisted route (replace / app / absent / staff), `GATED` lists opt-in-only routes, `REVIEWED` holds hand-checked verdicts; `--check` exits 1 on an unreviewed route. 2026-11-11: 64 routes (38 documented, 12 replaceable, 9 absent, 2 app, 2 staff, 1 dynamic); 45 workflows = 15 ready on the key (all 14 autopilot stages + land desk), 6 no DataSift API, 2 swap routes (ftm_pull, quick_screen), 22 need work. The five shared fixes that move most of the 22: message board to owner routes, autocomplete to search, the Deal Room preference and hardcoded Desktop paths out, teammates by uuid, the uploader's password path out. The browser write paths: courthouse import (dropbox_watcher -> datasift_uploader), the Tracerfy merge (tracerfy_ftm -> sift_upload_wizard), d4d_call_list's skip trace, sold cleanup, Market Finder. **The community kpi-engine skill tells users to paste a browser JWT, which the Terms now say not to build on: move it to the key first.** Plan doc tab: https://claude.ai/artifact/9WFWp5o8jcfJqXbZuFtyqH#2af33d63-8323.

## Single-Record Deep Prospecting (build 1.0.58, 2026-10-27)

`src/deep_prospect_record.py` runs deep-prospecting v5 on ANY one ty+2 record by street line. It reuses every `dream100_dp` phase and adds what an heir case needs. Run folder `output/deep_prospect/<address slug>/`; `research/` and `analysis/` JSON are researcher-written exactly as in the Dream 100 run. First run: an Obituary list record (owner died early 2026, house in his trust, four children traced, 20 numbers written, about $0.80).

```bash
python src/deep_prospect_record.py --address "123 Example St" --phase records --context "Obituary list record in ty+2"
# then: smartskip [--subject-address property] > merge > write research + analysis > heirs > merge
#       > score > gapfill > verify > build > writeback   (--commit to spend or write)
```

- **`heirs` = a second SmartSkip BULK pass with each decision maker as a subject**, at the street SmartSkip or a county roll gave (never name plus city). Orders accumulate; anyone already matched is not re-billed. A married daughter hides under her maiden-name record at the parents' house; `smartskip_heirs_keys.json` folds her married name back onto the maiden-name record.
- **Drop obituary middle names on heir traces.** 3 of 4 children came back EMPTY with their obituary middle names and all three matched without. The owner matched WITH his, because the county title carries it.
- **Query the Knox tax API by street, never the CRM parcel id.** The search is fuzzy: `107gb031` returned 22 unrelated parcels, and `dream100_dp._tax` would have reported the first ("GBC51") as the title owner.
- **Trestle Real Contact 403s AUTHENTICATION_FAILED on a browser User-Agent**, which reads exactly like a bad key. Send only `x-api-key` and `Accept`.
- `verify` (Real Contact) says whose number each is: the record's two 626 mobiles belonged to a stranger, and a wife's cell also matched her husband (`also_reaches`). `wrong_party` keeps a stranger's activity-100 number out of the tiers and off the dial sheet. `gapfill` targets a decision maker with no LIVE number, not only none at all (Tracerfy found a daughter's cell when her one landline scored 30).
- Street View at the record's coordinates can be the neighbor: read the house number before a photo goes on a pack.

## Deal Buyer Match + Buyer Deep Prospect (build 1.0.57, 2026-10-27)

Top N buyers for ONE contracted house, then who signs and which number reaches them. `src/deal_buyer_match.py` (phases `sweep/hydrate/rank`, all free) and `src/deal_buyer_dp.py` (phases `crm/dossier/trace/verify/export`, DRY by default, `--commit` to spend). Output `output/deal_buyers/<slug>/`: `ranked.json`, `dossier.json`, `dial_sheet.csv`, `Buyers_<slug>.xlsx` (Top 10 / Dial Sheet / Local Buys / Comps / Sources). First run: 5123 Odell Rd 37920, contract $90,000, working ask $135,000; total spend $1.05 SmartSkip plus 124 Real Contact queries.

```bash
python src/deal_buyer_match.py --slug 5123-odell --lat 35.914504 --lon -83.941647 --beds 3 --baths 1 \
    --sqft 1284 --year 1952 --ask 135000 --contract 90000 --phase all
python src/deal_buyer_dp.py --slug 5123-odell --phase crm        # then dossier, trace --commit, verify --commit, export
```

- **The sweep is a SiftMap polygon search around the subject, verified by count delta** (county 10,611 -> 2mi box 383 -> 1mi box 97). Comps = every arm's length SFR sale in `--comp-mi` (default 1.0, 18 months); buyers = every investor transaction type in `--buyer-mi` (3.0, 36 months). Deeds hydrate through the dispo registry's `details.json` cache, re-read when the cached deed predates the window.
- **Unlabeled flips are found from the deed chain.** SiftMap never called 2416 Alberta Dr a flip, yet Unique Construct LLC bought it at $155,000 and sold it at $362,000, 0.28 mi from the subject. An entity seller who bought within 24 months counts; a person seller also needs a 1.35x resale.
- **THE REGISTRY PRICES EXITED FLIPPERS AT THEIR EXIT.** `dispo_buyers._observations` records a `flip` row at `last.get("price")`, the deed to the retail buyer, so every flipper's price band is a resale band: GDP Properties read a $307,500 median on a book of real $100K to $200K buys ($189,000 after the fix). `deal_buyer_match._acquisition_lookup` recovers the buy from the deed before the exit. **Not yet fixed in `dispo_buyers.py`**, so `buyer_profiles.json` `box_price` and the dispo blast's band filter still sit on resale prices for every exited flipper.
- **One slot per OPERATOR.** `groups.json` (research layer, every grouping cited) ties LLCs one family runs; South Knox 12 and 2614 Dexter both file from one Mt Juliet house and took two of ten slots unchecked. The top N also requires `--min-buys` 3 lifetime purchases, because a two-purchase LLC outscores a 23-flip operator on recency alone. Named exclusions plus `NOT_BUYERS` drop lenders' REO vehicles (Groundfloor) and foundations (Aslan) that score high on local volume.
- **Trestle REAL CONTACT is licensed on this account; Reverse Phone is not (403).** `GET api.trestleiq.com/1.1/real_contact?name=&phone=&add_ons=litigator_checks` returns `phone.name_match`, `phone.contact_grade`, `phone.activity_score`, line type and litigator risk in one call. It turns "a number on the company record" into "the decision maker's number": 121 pairs on the first run, zero errors. A company name never name-matches, so every unattributed company line needs a named person tested against it.
- **Registry phones on an entity buyer are not the buyer's until a name matches.** All 10 GDP Properties record numbers failed to match its principal. Gateway Properties of TN's only CRM record is a house it SOLD to a homebuyer, and both its numbers name-matched that homebuyer (the record owner is tested as a `FOIL:`). The live CRM status outranks the August snapshot: one Tipton-group number reads WRONG, and 2614 Dexter's number reads CORRECT from the blast.
- **No name-plus-city person search, ever.** Knox holds several people sharing one principal's common name; a city-anchored search returns whichever ranks first and Real Contact then confirms the number belongs to A person of that name, a false positive verify cannot see. `trace` runs SmartSkip only at a verified HOME street (office, suite and PO box refused; `trace.json` supplies one where research established it). A decision maker without one is verified against company numbers only and falls back to the published company line.
- **Decision makers come from `research/<slug>.json`, never the module.** The TN SOS search (tncab.tnsos.gov) is behind a Cloudflare check that the permission system will not let an agent get past; the working sources were the Knox tax API (`knox-tn.mygovonline.com/api/v2/parcels/<q>`, which also carries county BUSINESS accounts by address: that is how one principal's consulting LLC tied to his home), KGIS owner cards, TPAD, Bizapedia's embedded SOS JSON, company sites and the BBB. The registry's reverse-address principal was right on all four it was checked on, but spelled one of them wrong (one letter off in the first name), which would have failed every name match.

## Contractor Research Workflow (build 1.0.47, 2026-08-20)

The team's contractor/sub sourcing method, imported from the Desktop Contractor-Research-Toolkit and registered as two community-safe skills: `skills/vendor-directory-builder/` (research engine: community mining -> public-record verification -> geo sweep + gap analysis + niche gatekeeper layer -> Excel via `scripts/build_directory.py`) and `skills/contractor-call-sheet/` (action layer: printable call sheet via `scripts/build_call_sheet.py` + personalized outreach drafts, never sends). Both tier none (pure openpyxl), category Operations, on the agent map under Deal Analysis (`vendordir`, `callsheet`). Internal SOP with the real Knox+Blount worked example (68 providers): `docs/contractor-research-workflow.md`.

**Rules that make it work:** never fabricate a field (unverifiable = "not found" / UNVERIFIED, someone will dial these numbers); cross-validation (2+ independent recommenders) is the call-first signal; service area is the most common silent failure (the geo sweep removed 5 of the first Knox list); ratings always carry the review COUNT; found/AI-generated lists are claims to verify, and catching their wrong numbers IS the deliverable. The distributed bundles carry fictional example data and [Your Company]/[Your Name] placeholders; the real provider data stays in the internal doc and the Desktop toolkit. Community page (learn.datasift.ai/claude-skills-rei) listing is still a follow-up.

## Playbook Creator v2 + Agent SOP Infrastructure (build 1.0.48, 2026-08-21)

The playbook-creator skill was overhauled into a three-format documentation engine modeled on two open-source systems researched for the build: **strands-agents/agent-sop** (the Agent SOPs standard: markdown SOPs an agent executes directly) and **westpoint-io/mimik** (an MIT Scribe clone; its export format defines our step-guide shape). Formats: **SOP** (human Word doc + agent-executable `.sop.md` twin), **Playbook** (strategy, unchanged), **Step Guide** (Scribe style: title under 60 chars, `N steps · Created date · Source` metadata line, zero-padded `## Step 01:` action-verb headings, one screenshot per step).

**The `.sop.md` twin contract (Agent SOPs standard):** four structural invariants: kebab-case filename ending `.sop.md`; `## Overview` (2-4 self-contained sentences, doubles as the description in every distribution channel); `## Parameters` (snake_case, required first, mandatory `**Constraints for parameter acquisition:**` block); `## Steps` as `### N. Name` headings each carrying `**Constraints:**` in RFC 2119 (You MUST / SHOULD / MAY). Every negative constraint carries a because; every producing step names its artifact path (the resumability story). Validator: `skills/playbook-creator/scripts/validate_sop.py` (stdlib port of the strands checks), run after EVERY edit. The human SOP's Inputs table and the twin's Parameters share names so the pair cannot drift.

**Team SOP library + MCP serving:** twins live in `sops/` (see its README for house rules). The project `.mcp.json` runs `python -m strands_agents_sops mcp --sop-paths sops` (pip `strands-agents-sops`, in the repo venv), so every file there is a Claude Code slash prompt (`/agent-sops:<name>`) that asks for its parameters and runs the steps. Smoke-tested over real MCP JSON-RPC. Windows note: `--sop-paths` separates with `;` on Windows, `:` on Unix.

**The video pipeline is the flagship input path (proven on the real "Remove Sold Properties" walkthrough, 8.3 min, 2026-08-21).** A narrated screen recording replaces both the transcript AND the screenshots: `scripts/transcribe_video.py` (bundled, stdlib) extracts audio via ffmpeg, transcribes through OpenRouter Gemini 2.5 Flash (`OPENROUTER_API_KEY`, ~$0.002/audio-min, the whole video cost ~2 cents), and returns three sections: TRANSCRIPT (timestamped), ACTIONS (every UI action with the exact label spoken), JUDGMENT (every decision rule stated aloud, which become the SOP's Rules and the twin's MUST NOTs). `--frames-dir` then pulls a frame per action timestamp: **the frames ARE the screenshots**. Gotchas learned live: transcription clocks drift (first pass read 9:27 on an 8:15 video; the script scales stated time onto ffprobe-real time), frames can catch a page mid-load, so every frame is VERIFIED visually before embedding and near-duplicates dropped; `--crop 1920:910:0:125` removes 1080p Chrome chrome+taskbar. No-key fallback: paste the recorder's own free transcript (Loom/Zoom/Fireflies). The key is OPTIONAL and the skill stays tier none.

**`build_docx.js` now embeds real screenshots:** a standalone markdown image line (local path or base64 data URI) lands in the Word doc at content width, alt text as caption, missing file degrades to a visible placeholder instead of breaking the build. That closes the loop: capture-tool exports and video frames flow straight to a finished doc. Screenshot placeholders were upgraded to Scribe-style capture specs (Capture / Highlight / Crop, exactly one highlighted element per shot).

**First production run:** `output/sops/sold-property-removal/` (13-step Word doc, 13 real frames + process map) and `sops/remove-sold-properties-from-marketing.sop.md` (validated twin, parameterized county/price_floor/years_back). The process itself: sequence `recently sold` tag -> status Sold (Transactions folder), SiftMap county filter at $1,000+ / 3-year window, backlog pulled 10K at a time WITH the tag and auto-add OFF, auto-add ON only after the backlog lands, verify next-day auto-upload arrives at status Sold. Frames carry real seller data + Ty's webcam bubble: fine internally, blur before community distribution.

## Enterprise Prospect Pipeline (build 1.0.46, 2026-08-19)

`src/enterprise_prospects.py` builds the DataSift SALES team's enterprise outbound list: the top property BUYERS nationwide, researched and tiered as prospects to sell DataSift enterprise contracts to. Phases `audit/classify/probe/sweep/rank/verify/queue/merge/export/qa`, state in `output/enterprise_prospects/state.json`, every phase gates on nonzero data and is resumable. Read-only against SiftMap; it must NEVER call `add-properties-by-query` (spends record allowance).

```bash
python src/enterprise_prospects.py --phase audit          # seed CSV -> 52,634 unique buyers
python src/enterprise_prospects.py --phase probe          # verify the SiftMap search contract live
python src/enterprise_prospects.py --phase sweep --counties 60 --hydrate-cap 150 --workers 4 --resume
python src/enterprise_prospects.py --phase qa             # full gate table
```

**Deliverable (2026-08-19): `output/enterprise_prospects/DataSift_Enterprise_Prospects_20260819.xlsx`.** 9 sheets: exec summary, Top 250 Master, Tier 1 Core ICP (156 independent wholesalers/flippers/landlords, 10-99 buys/6mo), Tier 2 Institutional D2S (31: Opendoor, Offerpad, HomeGo, Truehold, JWB, HomeVestors franchisees), Tier 3 Wholesale-Channel SFR funds (63, kept with a dispo-angle note), 1,000-buyer scored second-tier pool, Knox depth layer (398 buyers from the cached zip sweeps), exclusions log, methodology. 71 targets carry a confirmed direct-to-seller verdict, 138 a named principal.

**Seed = `nationwide_buyers.csv`** (84,048 buyer x county rows; canonical copy under `Skills for REI/extracted/.../buyer-prospector/data/`, the `skills/` copy differs only in line endings). Two data traps, both handled in `audit`: BuyerCity/BuyerState VALUES are swapped vs their headers (detected empirically, 100% two-letter codes in the city column), and `BuyerPurchases6MSum` is a REGIONAL total repeated on every county row, so volume = max-per-name + county-row-count, never a sum (summing gave Opendoor 442K "purchases").

**The SiftMap /properties/search/ contract (all verified live 2026-08-19, most fail SILENTLY if wrong):**
- The **Open API key works on the bulk search endpoint** (`authorization: Api-Key`), not just detail/autocomplete. No Bearer JWT needed.
- **`result_index` is a ROW OFFSET, not a page number.** Pages of 250 are result_index 1, 251, 501, ...; passing 1,2,3 returns 99.6% duplicate rows and reads exactly like broken pagination. Ordering is stable.
- `extra_last_sale_date_min/max` filter for real (Knox 188,010 -> 3,943 sold in 183 days). **`extra_last_sale_price_min` is silently IGNORED** (a $10M floor changed nothing), as are guessed keys like `corporate_owned`. Count-delta is the only proof.
- Search rows carry **NO owner name** but DO carry `id` (dataflik_id), `corporateOwned` (~15.6% of recent solds), `absenteeOwner`, value/equity/scores/distressors. So the sweep paginates each county's solds, keeps corporate-owned rows, and hydrates a capped random sample per county via `get_detail` for `sale_history[0].buyer_name` - the current owner of a recently sold property IS the buyer.
- **Detail-endpoint throughput caps near 2-4 req/s AGGREGATE regardless of worker count**, with escalating Retry-After under sustained load (1s -> 16s -> 22s). The 60-county sweep (2,317 search + 7,789 detail calls, 7,069 fresh corporate buyers, 0 empty counties) took ~3 hours; budget accordingly and use `--resume`.

**Classification polarity is INVERTED from `export_buyer_list.py`** (there institutions are excluded from a wholesale buyer list; here high volume is the signal and there is no volume auto-exclude). New taxonomy in-script: EXCLUDE groups (government, GSE/bank/servicer, homebuilder, relocation, title, estate/nonprofit, land dev; HIGH keywords drop outright, MED generics go to REVIEW near the cutline), Tier 1 core ICP, Tier 2 institutional direct-to-seller, Tier 3 SFR funds (structural patterns: `BORROWER .* LP`, `OWNER [IVX]+ LLC`, `\bSFR\b`, acquisition trusts).

**Research fan-out: rules guess, research decides.** All 500 top-ranked targets were researched by 21 parallel web agents in 2 waves (spec at `output/enterprise_prospects/research/RESEARCH_SPEC.md`, schema-fixed JSON per batch). **Measured exclusion rate in the top 200 by raw volume: 44%** (builders, lender/servicer REO takebacks, land bankers, relocation nominees, MHC operators, one university), so always over-select ~2x for attrition. Research overrode the rule tier on 286 of 500. Alias map proven by research (SEC filings, SOS records, shared-suite address matches): OP SPE * = **Offerpad** (NOT Opendoor), ARMM/MSR = Amherst/Main Street Renewal, FKH SFR = FirstKey (Cerberus), SFR JV-* = Tricon, DFH * = Dream Finders, CPT-ASL = Truehold, Hoose 18 = JWB, FREO = Progress Residential, CMH = Clayton.

**`verify` = the Harper move at list scale:** autocomplete the buyer's mailing address (strip `STE/UNIT/#...` suffixes first - suites return zero hits), house-number match, `get_detail` -> `owner_info` portfolio + reverse-address principal. A reverse-address owner only counts as a human principal if it matches no ORG word (`ORG_RX`); "State Bank Of Geneva" and "Us Postal Svc" both slipped the narrow entity regex before that guard. 373/500 resolved, 90 principals unmasked, every miss carries an explicit reason.

**Open item (deferred by Ty): the paid contact-resolution pass** - Enformion BusinessV2 principals + skip trace on the final 250. The workbook's Phone/Email columns are headered "(Enformion pass pending)" and the merge/export phases re-run cleanly once contacts exist.

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

**SELLER AUTO-CAMPAIGN DISABLED 2026-09-11.** `SMS_AGENT_CAMPAIGN=0` is set as a Fly SECRET on `siftstack` (secrets override `fly.toml [env]`), because the daily scheduler was texting sellers on four consecutive days (touch gap 1) and re-sending touch 1 to 144 phones in 14 days. Inbound stays live at phase 2. Re-enable via the secret only after `SMS_AGENT_TOUCH_GAP_DAYS` is raised and the touch-1 repeat in `campaign.py` is fixed.

`src/sms_agent/` sends outreach, reads replies in real time, classifies them, writes the result back to DataSift (phone status, opt-outs, lead status), and hands positive responses to a prospector in Slack. Full runbook: `src/sms_agent/README.md`.

**The constraint that shapes the whole build: DataSift webhooks CANNOT see an inbound text.** DataSift released webhooks as a **sequence ACTION**, so the trigger surface is exactly the ten sequence triggers, all of them CRM state changes (`Property Status Change`, `Property Assignee Change`, `Property Tags Added/Removed`, `Property Lists Added/Removed`, `Task Created/Completed`, `SiftLine Card Created/Moved`). There is no SMS-received trigger, no conversation event, and DataSift does not send SMS itself (it hands off to smrtPhone/Twilio/Plivo; drip campaigns have no documented reply-exit either). **smrtPhone's webhooks are the inbound leg**: `smsIncoming` (`smsId, from, to, message, date, callerIdName, userName, contactName, source`), `smsOutgoing`, `smsDeliveryCallback` (`status`, `failure_reason`), `addNumberToDNT`, `addNumberToDNC`. Both vendors post to the same receiver. Conversation replies go out over the smrtPhone API (`POST phone.smrt.studio/sms/send`, header `X-Auth-smrtPhone`), which is TEXT-ONLY, so only the original auction-screenshot MMS still needs the browser path in `mms_sender.py`.

**Voice comes from the `text-touch-builder` skill's message recipe**, not from defaults: warm, positive, properly capitalized, one easy question per message, under 160 chars, street line ONLY (never the full address with zip), first-real-name-token hygiene (initials-only / companies / trusts get owner-of-the-address wording), and the rule the whole program rests on, **never name the list** (foreclosure, auction, probate, inherited, tax, lien, code violation, eviction, divorce, bankruptcy, "behind on") because **the seller should feel found, not targeted**. Soft no vs hard no is noted on every NOT_INTERESTED since soft nos become follow-ups. STOP or hostility gets NO reply at all, not even an apology. `knowledge/playbook.md` is the editable system prompt.

**Identity is anchored to the ASSIGNEE, and we never say a company name** (Ty: a named company is litigation bait). The record's `assigned_to` uuid resolves through `config/sms_senders.json` to a first name, so an Adriana-assigned record signs as Adriana and Adriana is who calls; an unmapped uuid means the thread goes out UNSIGNED, never a guessed name. The agent describes itself by locality built from the record's own county ("a local buyer here in Blount County"). `cli.py senders [--record <uuid>]` shows what resolves. **The responder is given almost nothing** on purpose: owner first name, street line, city, county. Valuation, equity, distress flags, vacancy, beds/baths/sqft and every list tag are withheld from the prompt entirely, so there is nothing to leak. The validator hard-blocks any draft naming a dollar amount, naming the list, carrying a link or a zip code, over 320 chars, asking two questions, or self-identifying as automated.

**Two send transports (`SMS_AGENT_TRANSPORT`), because Ty believed smrtPhone had no API.** It does, for TEXT: `POST phone.smrt.studio/sms/send` with header `X-Auth-smrtPhone` (key from Admin > API Tokens). What has no API is **MMS**, which is what forced the browser route on the original auction-screenshot send; replies carry no image, so the API is the right transport here. `session_sender.py` is the fallback, driving the web app's Compose Message modal via `smrtphone_state.json` (reusing the mms_sender mechanics: context-level microphone permission or the dialer's Allow-microphone modal covers the compose UI and silently times out every send; the compose button is icon-only and targeted by POSITION at ~[80,63]). `auto` prefers API and falls back only on a transport failure, never a 4xx. **The session path sends from the account default caller ID only**, so sticky senders and per-number caps do not apply there and `doctor` says so.

**smrtPhone API key is VERIFIED LIVE (2026-08-10).** Auth probe: `POST /sms/send` with NO params returns 400 "Missing required parameter(s): from, to, message" on a valid key and 403 on a bogus one, so that is the clean credential check and it cannot send anything. Do NOT probe `GET /dialerConfigs`: it 405s on GET and serves the web app HTML on POST regardless of key, proving nothing. Prod transport is `api`; the browser session stays a local-only fallback.

**Cloud deployment (Fly.io, `fly.toml` + `deploy/Dockerfile`).** Four deliberate choices: ONE machine with the worker as a thread inside the receiver (`SMS_AGENT_INLINE_WORKER=1`), because SQLite is single-writer and two machines would fight over one volume; `auto_stop_machines = false`, because a stopped machine drops a webhook and there is no replay; a persistent volume at `/data` holding the event log, `sms_numbers.json` and `sms_senders.json` (editable without a redeploy via `fly ssh console`); and NO Playwright in the image since the API transport works. **`src/sms_agent/crm_standalone.py` is what makes this possible**: a self-contained reisift client (`authorization: Api-Key <key>`, `REISIFT_API_KEY`, base `apiv2.reisift.io`, 429 backoff that parses the server's "available in N seconds" hint) so the cloud box needs no Deal Room checkout on disk. `crm.py` prefers the shared CRMClient and falls back to it.

**Number pool is OWNER-BOUND, 18 numbers at 25/day = 450/day.** Pulled 2026-08-11 from smrtPhone Admin > Phone Numbers, which has NO public API: `/phoneNumbers` and `/callerIds` return the SPA shell to an API key. The route is the web session plus the FOSJsRouting trick (`GET /js/routing?callback=fos.Router.setData` dumps ~1,187 routes) which finds `POST /phoneNumbers/filtered`, a DataTables endpoint like `/logs/calls/filtered`; fields come back as HTML fragments and need parsing. 21 numbers total, 3 excluded (Website 865-324-1736 on the Inbound Calls flow, Ty - Dispo 865-338-9203 on the Ty Test flow, and Adriana Test Flow 865-273-0739), leaving Adriana 9 and Tinaa 9. **`config/sms_numbers.json` is keyed by the caller who owns each number** and `sender_pool.assign(phone, owner)` prefers that caller's numbers, because the thread is signed by the assigned person and a homeowner who calls the number back must reach the same person the text claimed to be from. A sticky number wins over owner preference: changing numbers mid-thread is the worse problem.

**Autonomy ladder (`SMS_AGENT_PHASE`), because the phone number is the asset:** 1 classify + write phone status/opt-outs, 2 + escalate and flip CRM status, 3 + draft replies held in Slack for approval, 4 + auto-send a narrow gated intent set. **Phase 2 already delivers the prospector handoff with zero AI-authored text sent.** `SMS_AGENT_DRY_RUN=1` independently blocks every CRM write and every send.

**Guardrails, each from a specific failure mode:** human takeover wins instantly (an `smsOutgoing` we did not author means a person typed it -> pause the thread, cancel every queued AND held message); opt-outs are decided by regex and never by a model, and cover natural language ("stop texting me", "take me off your list") not just the STOP keyword; 6-turn cap; **two send windows that must both agree (Ty, 2026-08-28): our own 9am-6pm Eastern in one fixed timezone (`SMS_AGENT_BUSINESS_*`, so no reply lands when nobody here can take the callback) AND 9am-6pm recipient-local from the area code (`SMS_AGENT_QUIET_*`, the compliance half, which a fixed zone cannot express since 9am Eastern is 6am in California)**, with up to 30 min of wake jitter so a night's backlog is not one 09:00:00 burst; sticky sender number per conversation (switching mid-thread reads as a spam farm); per-number daily cap + pacing; a hard output validator that blocks any draft naming a dollar amount, carrying a link, over 320 chars, asking two questions, or self-identifying as automated; a 0.80 confidence floor; and `sys_`-prefixed system tags so our own writes never re-trigger the sequences that called us.

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

## The Offer Sheet: the default post-walkthrough deliverable (build 1.0.49, 2026-08-28)

`src/offer_sheet.py` is THE deliverable after a walkthrough. It replaces the 8-tab post-walkthrough workbook: **one file, five tabs, Offer in front**, wholesale by default, answering exactly one question: **OFFER TO SELLER**. `--offer-only` renders just the front page.

Tabs: **Offer** (the answer) | **The House** (walk condition, seller and probate intel, priced flags, gates) | **Repair Detail** | **Comps** | **Buyers**. Repair Detail, Comps and Buyers are the post_walkthrough builders REUSED as-is; The House condenses the old Overview and Repair Logic into one. Exit Strats is gone (the Offer page supersedes it and four lanes was the confusion Ty rejected), Active-Pending and Outreach are folded away.

**`_hydrate()` is load-bearing.** The reused builders read `pack["sold"]` as live `MarketListing` objects, plus `finished` and an `exits["inputs"]` band; a saved pack carries comps SERIALIZED as `sold_comps`. Without the revive the Comps tab renders empty and reads like a thin market rather than a wiring bug. The buyers band is synthesized from the offer math, which is a truer target than the old exit spread.

```bash
python src/offer_sheet.py --pack output/<deal>_pack.json --walk walk_<deal>.json                           --out "<Address>_Offer.xlsx"
```

**Layout is the Fortune Builders "Deal Analyzer for Flips"** (the real file is `output/Copy of The Repair Estimator.xlsx`): paired left/right blocks under banded headers, a percent column beside every dollar column, and inputs INLINE on the same page. The three mortgage tranches are dropped, because financing is a lender-package concern and not an offer concern. Formatting helpers (`_band`, `_row`, `_kv`, `_para`, `_polish`, the INPUT/CALC/TOTAL palette) are IMPORTED from `lender_package.py`, never copied.

**The math is live Excel formulas off defined names**, so one blue cell moves the whole page: `BuyerMax = RulePct * ARV - Rehab`, `OfferToSeller = BuyerMax - Fee`. Rule defaults 70%, fee $15,000 (Ty runs a flat $10-15K assignment fee, per the exit-analysis rule). The "if we buy and rehab it ourselves" block carries net profit, ROI on total cost, purchase-plus-rehab ROI and annualized cash on cash, and it is deliberately subordinate to the offer, not a competing option. Comps (5 lines) and gates (4) are condensed onto the page rather than dropped: an ARV with no visible support is unauditable.

**THE STALE PACK TRAP, hit twice on one deal.** A `--save-pack` JSON is a snapshot and does NOT track later edits. On 1342 Grainger the pack still carried the original four-scenario rehab totals AND the original $481,000 engine ARV after both had been corrected, so a naive re-render produced a workbook three revisions out of date. Rules: recompute rehab via `build_rehab_matrix(subject, walk)` and NEVER read `pack["rehab"]["totals"]`; an explicit CLI `--arv` / `--as-is` WINS over the pack; and write corrections back into the pack so it stops being a landmine.

**Verify by recalculating, not by reading.** The `formulas` package loads the saved xlsx and computes it the way Excel would; assert the offer figure and assert zero formula errors, then flip `RulePct` to 0.75 and confirm the offer, buyer profit and every ROI move together. **Column widths must be set AFTER `_polish`**: `_autofit` sizes off the longest string per column and the full-width paragraphs live in column A, which blew the page to 221 width units and scrolled sideways. Fixed widths total 135 and paragraph rows are re-heighted against the width the text actually gets.

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

## Plugin Marketplace: the install and update channel (build 1.0.54, 2026-09-21)

The skill library installs from THIS repo as a Claude Code / Cowork plugin marketplace. Source of truth is still `skills/` + `plugins/`; everything below is GENERATED by `python tools/build_skills.py --marketplace` (run it BEFORE `--build`, it writes into a tree that gets zipped). `install.py` and `dist/*.skill` remain the fallback for claude.ai chat uploads and plugin-less environments.

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-all@siftstack        # 27 current packages, one command
python install.py --plugins                    # same result on a CLI too old for bundles
```

- **`skills/<name>/` is published AS a plugin with zero restructuring:** `strict: false` + `skills: ["./"]` makes the marketplace entry the whole definition, and SKILL.md at the plugin root is a valid single-skill plugin. Proven by install, not by reading docs.
- **Bundles live in `marketplace/bundles/`, NOT `plugins/`,** because `build()` / `manifest()` treat every directory under `plugins/` as a package. One bundle per category plus `siftstack-all`; each is a `plugin.json` holding only `dependencies`. `siftstack-core` (doctor + setup skills) rides in every bundle.
- **NO `version` field anywhere, and the builder refuses one.** A declared version pins the plugin and becomes the only update signal, so one forgotten bump strands every user. Without it the commit SHA is the signal (the cache dir is literally named after the SHA). This is why `version` was stripped from `deal-analyzer` and `sift-operations`.
- **`dependencies` needs a current CLI.** Ty's terminal CLI was 2.1.38 (the VS Code extension bundles its own, newer one) and rejected the bundle with `Unrecognized key: "dependencies"` AFTER `plugin validate` had passed. Validation is not installation: CI now does the real install on `@latest` via `npx` into a throwaway `CLAUDE_CONFIG_DIR` and asserts every current package landed. `install.py --plugins` installs plugin by plugin, which works on any version.
- **Auto-update is OFF by default for third-party marketplaces.** Users flip it once (`/plugin` -> Marketplaces -> siftstack -> Enable auto-update). The `setup` skill and the README both say so.
- **`siftstack-core` gets its own `data/requires.json`, not a manifest copy:** an installed plugin is copied to a cache so `../skills/manifest.json` does not exist, and the manifest carries the sha of the very archive that would contain it.
- **Secrets stay on the `.env` / env var contract.** Plugin `userConfig` substitutes into skill text (the key lands in the prompt) and is not exposed to Bash-run scripts.
- **Never `rmtree` a generated dir inside this OneDrive checkout.** It died with WinError 5 mid-delete and left the repo with no bundles. The builder overwrites in place and prunes only stale bundle dirs.
- **No top-level `bin/` in any plugin** (Cowork refuses it); `check_docs.check_marketplace` gates that, plus every package in `siftstack-all` and in exactly one division bundle. `check_counts` gates the hand-written totals in README / GETTING-STARTED / `.env.skills.example`, which had drifted to four different wrong numbers (24, 22, nine, 21).
- `.claude/` is gitignored, so `extraKnownMarketplaces` in project settings cannot reach a cloner. Not used.

**Scope honesty:** the marketplace ships the ~22 skill-backed agents of the 73 on the org chart. The `src/` engine (~44 agents, 12 of them hardcoding the Desktop Deal Room path) and the two Fly apps are Phases 3 and 4 of `C:\Users\Tyrus\.claude\plans\i-m-trying-to-find-soft-unicorn.md`; Phase 2 adds install badges to `docs/agents.json`. Unverified until tested live: Cowork VM network egress for API-tier skills, and the GitHub-URL add path (only the local-path add has been exercised, since the branch is unpushed).

## REI Skill Library (21 Skills)

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
| 13 | `playbook-creator.skill` | Operations | 2026-08 | Three-format doc engine: Playbook, SOP (human Word doc + agent-executable `.sop.md` twin per the open Agent SOPs standard, RFC 2119 constraints, `scripts/validate_sop.py`), and Scribe-style Step Guide (zero-padded `Step 01:` action-verb headings, one capture/highlight/crop screenshot spec per step, title under 60 chars). 7-node chart limit, 5th grade reading level, Word doc output |
| 14 | `text-touch-builder.skill` | Operations | 2026-08 | Four-text-touch pre-call SMS sequence per ready-to-call record (identity check, drip, soft ask, breakup) with cold-email style copy rotation; CSV export -> stdlib script -> Add-Data re-import into Text Touch 1-4 custom fields. **Human-voice gate added 2026-08:** `AI_TELLS` refuses (not warns) any message or pool variant containing an em/en dash, a semicolon, a link, emoji, ALL CAPS, stacked exclamations, form-letter openers, or AI vocabulary; `--check-pools` audits the variants and runs on every invocation. Same list mirrored in `src/sms_agent/respond.py` so outbound touches and inbound replies sound like one person. Community-safe (no internal API) |
| 15 | `cold-call-coach.skill` | Operations | new | Pull SmrtPhone cold-call recordings, audio-model transcription with real tonality notes, grade vs the cold-calling rubric (measured reliability +/-3 pts, calibration examples, short calls on their own scale, JSON score footers), Excel workbook export. Self-contained scripts, config-driven roster |
| 16 | `lead-manager-coach.skill` | Operations | new | Same engine, lead-management rubric: 4 pillars qualification, roadblocks, no-ladder, next-action discipline. Call quality only (no CRM hygiene scoring) |
| 17 | `closer-coach.skill` | Operations | new | Same engine, closer rubric: money conversation, three-option offer stack, objection frameworks, commitment locking, negotiation timeline reports |
| 18 | `kpi-engine.skill` | Operations | new | Universal DataSift KPI reporting from the user's own account: activity-log pull (self-contained stdlib script, own JWT, no internal API), three distinct rates, lead counting incl new_lead statuses, funnel pacing (dials->correct->leads->appts->contracts), record-level detail mode, md/CSV/Excel/Slack outputs. Benchmarks shipped as tune-per-operation baselines; internal production version lives in Deal Room `_api/kpi-engine/` |
| 19 | `comp-package.skill` | Deal Analysis | new | Boundary-filtered comp package: /search API pull with 41-row-cap band partitioning, condition bucketing by price/Zestimate ratio, dual-track ARV (same-bed base + labeled reconfig upside), 3-scenario rehab, MAO math, buyer targeting, Excel deliverable spec. Community-safe (own OPENWEBNINJA_API_KEY, requests-only script) |
| 20 | `vendor-directory-builder.skill` | Operations | new | Vetted contractor/vendor directory for any market: community mining (FB in-group search, self-promoters + recommendation-thread comments), public-record verification (phone provenance, service area, rating with count, license board, BBB), geo sweep + gap analysis + niche gatekeeper layer (utility districts), Excel via bundled build_directory.py. Never-fabricate rule; also THE tool for vetting a found/AI-generated list. Community-safe (openpyxl only, fictional example data) |
| 21 | `contractor-call-sheet.skill` | Operations | new | Action layer on a finished directory: printable one-page call sheet (build_call_sheet.py, fuzzy column detection, call-first banner for cross-validated providers) + personalized first-contact texts/voicemails + the 6 vetting-call questions. Drafts only, never sends. Community-safe (openpyxl only) |

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
