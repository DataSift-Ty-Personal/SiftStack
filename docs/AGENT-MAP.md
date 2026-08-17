# SiftStack AI Agent Org Chart

*Every agent, what triggers it, what it touches, where a human still signs off, and the specific trap each one exists to avoid*

74 agents across 9 divisions. Generated from [`docs/agents.json`](agents.json) by `tools/agent_map.py`. Build 1.0.44, 2026-08-17.

Do not hand-edit this file. Edit `docs/agents.json` and re-run the generator.

## How to read this

Every entry carries the same five things, because the fifth is the one that matters:

- **Trigger**: what starts it.
- **Does**: the steps it actually runs.
- **Human**: where a person still has to sign off. An empty list means it runs unattended.
- **Outputs**: what comes out.
- **The trap**: the specific failure this agent exists to prevent. Most were found in production, not in review, and several of them failed silently for weeks before anyone noticed. That field is the real documentation.

## Rollout phases

- **Phase 1 - Running in production**: Live today. Proven on real deals.
- **Phase 2 - Scaling / gated**: Built and validated, held behind a human go/no-go.
- **Phase 3 - Next build**: Designed, not yet wired end to end.

## Top of the org

### Operator

`CLAUDE.md` &middot; phase 1 &middot; live

Sets the buy box, approves spend, gives the go on anything that touches a homeowner.

**Trigger.** Always on. Every gated action routes here.

**Does.**

- Defines buy box: single family, AVM $1 to $700,000
- Approves outbound sends, offer numbers, and contract gates
- Re-approves the locked material list each project cycle

**Human checkpoint.**

- Every Phase 2 gate
- Any dollar figure that reaches a seller or buyer

**Outputs.** Go / no-go decisions, Buy box parameters

> **The trap this exists to avoid.** The named company is litigation bait. Outbound identity is anchored to the assigned human, never to a business name.

### Claude Orchestrator

`CLAUDE.md` &middot; phase 1 &middot; live

The session that reads the repo, picks the right agent chain, and runs it end to end.

**Trigger.** Operator prompt, scheduled task, or an inbound webhook.

**Does.**

- Reads CLAUDE.md for domain rules and hard-won gotchas
- Selects the skill or module chain for the job
- Runs the chain, degrades gracefully on any missing credential
- Reports what it did and what it could not do

**Human checkpoint.**

- Approves any irreversible or billable action

**Outputs.** Excel workbooks, CSV uploads, PDF research packs, Slack summaries

**Touches.** Claude, Cowork, Claude Code

> **The trap this exists to avoid.** A run that succeeds with no data is worse than one that fails. Every agent must state an explicit reason when it degrades.

## Divisions

### Data Acquisition

*First to market or it is not worth pulling*

15 agents.

#### Intake Orchestrator (division lead)

`src/main.py` &middot; phase 1 &middot; live

Routes every source into one NoticeData shape, dedupes by address, stamps provenance.

**Trigger.** python src/main.py daily | historical, or the Apify daily schedule.

**Does.**

- Filters saved searches by county and notice type
- Fans out to the right importer for each source
- Deduplicates by address, keeping the most recent
- Stamps date_added (our run date) separately from date_published (the legal filing date)

**Human checkpoint.**

- Reviews the run summary before upload

**Outputs.** Sift upload CSV, Run summary stats

**Touches.** main.py, data_formatter.py, Apify

> **The trap this exists to avoid.** Date semantics split two ways. date_added is when WE added it; date_published is the legal filing date. Everything downstream that reasons about timing must anchor on date_published.

#### Scheduled FTM Runner

`src/ftm_runner.py` &middot; phase 1 &middot; live

One unattended first-to-market pull of foreclosure and probate for Knox and Blount, running in the cloud instead of on a workstation.

**Trigger.** The scheduler at 06:30 America/New_York, or python src/ftm_runner.py --commit by hand.

**Does.**

- Preflights credentials, egress, state dir and saved searches
- Rotates to a fresh proxy session id per PROCESS, counter held on the volume
- Scrapes, enriches and uploads, then persists seen_ids ONLY after the upload succeeds
- Reports a 0-notice run as EMPTY and exits non-zero

**Human checkpoint.**

- Approves the first scheduled run before --commit is added to FTM_ARGS

**Outputs.** Upload CSV, ftm_runs.jsonl history, Run summary

**Touches.** Fly.io siftstack-ftm, Playwright, Apify proxy, /data volume

> **The trap this exists to avoid.** Zero notices is a FAILURE, not a quiet day. There is deliberately no inference that a missing challenge means a cleared gate; that exact reasoning reported 13 consecutive dead runs as successful over 19 days. seen_ids is persisted only AFTER a successful upload, because an aborted run that had already written it left 204 notices flagged as handled that were never sent anywhere, and a retry would have skipped every one of them permanently.

#### Egress Proxy Resolver

`src/proxy_resolver.py` &middot; phase 1 &middot; live

Decides which IP the scrape goes out from, because the site gates on egress rather than on code.

**Trigger.** Every scraper run, before the first page load.

**Does.**

- Resolution order: SIFTSTACK_PROXY_URL, then Apify groups via token lookup, then direct
- Rewrites illegal characters in the session id and logs the substitution
- Raises NoticeAccessBlocked on a refusal so the run aborts instead of grinding

**Human checkpoint.** None. Runs unattended.

**Outputs.** A proxy URL, A sanitized sticky session id

**Touches.** Apify proxy, BUYPROXIES94952

> **The trap this exists to avoid.** Apify session ids accept only letters, digits, underscore and dot. A hyphen in the name returns 407 Proxy Authentication Required, which is indistinguishable from a rejected password, and cost a full day of debugging byte-identical credentials. Separately: the site blocks an IP by VOLUME, around 204 notices per session, so the backfill runs one process per saved-search-month rather than one long job.

#### FTM Scheduler

`src/ftm_schedule.py` &middot; phase 1 &middot; live

The long-lived process that decides when the pull fires, business-local.

**Trigger.** Container CMD. Runs continuously.

**Does.**

- Computes the next fire time in the business timezone
- Launches ftm_runner as a fresh PROCESS so the proxy session rotates
- Records each run exit code

**Human checkpoint.** None. Runs unattended.

**Outputs.** Scheduled runs, Exit codes

**Touches.** Fly.io, ftm_runner.py

> **The trap this exists to avoid.** A scheduler process rather than fly machine run --schedule, because Fly schedules are coarse and pick their own minute, while a first-to-market pull wants a specific business-local hour. Being first is the entire product.

#### Notice Scraper

`src/scraper.py` &middot; phase 1 &middot; live

Drives tnpublicnotice.com through 8 saved searches and paginates every result.

**Trigger.** Daily run, or historical backfill over 12 months.

**Does.**

- Reuses saved session cookies, falls back to fresh login
- Selects each saved search from the Smart Search dropdown
- Paginates results at 50 per page
- Opens each notice detail page

**Human checkpoint.** None. Runs unattended.

**Outputs.** Raw notice HTML, Notice IDs

**Touches.** Playwright, tnpublicnotice.com, cookies.json

> **The trap this exists to avoid.** The site is ASP.NET WebForms. All navigation is __doPostBack with ViewState, so plain HTTP requests would have to hand-manage ViewState and EventValidation. Browser automation is not optional here.

#### Turnstile Gate Runner

`src/captcha_solver.py, src/scrapfly_client.py` &middot; phase 1 &middot; live

Clears the Cloudflare Turnstile challenge that sits in front of every notice detail page.

**Trigger.** Every notice detail page open.

**Does.**

- Reads the sitekey off the LIVE page, and logs SITEKEY ROTATED rather than dying quietly
- Selects the solve method and the response field from CAPTCHA_KIND
- Creates the cf-turnstile-response input when the headless widget never renders one
- Runs the blocking 2Captcha call in a thread so the browser event loop keeps servicing the page
- Raises NoticeAccessBlocked on an IP refusal and aborts the whole run

**Human checkpoint.** None. Runs unattended.

**Outputs.** Rendered notice HTML

**Touches.** Cloudflare Turnstile, 2Captcha, Playwright

> **The trap this exists to avoid.** The gate migrated from reCAPTCHA to Cloudflare Turnstile on 2026-07-13. config.py recorded the migration but the solver still called recaptcha() and injected into g-recaptcha-response, a field the page no longer reads, so EVERY solve was billed and discarded. The gate is session-level, so one solve covers the rest of the run. A blocked IP now aborts instead of grinding 50 results times 3 attempts against a wall, and the page shows no challenge at all from a blocked address, which is why this looks like a solver bug when it is an egress problem.

#### Notice Parser

`src/notice_parser.py` &middot; phase 1 &middot; live

Pulls address, owner, and dates out of free-text legal notice bodies.

**Trigger.** Every notice fetched.

**Does.**

- Regex extraction into the NoticeData dataclass
- Owner name taken from the deed-of-trust 'executed by' language
- Probate owner set to the Personal Representative, never the deceased

**Human checkpoint.** None. Runs unattended.

**Outputs.** Structured NoticeData

**Touches.** notice_parser.py

> **The trap this exists to avoid.** There are no structured HTML fields on the site. Address, owner, and dates are all embedded in free-text notice bodies, so the regex layer is load bearing.

#### Foreclosure Filter

`src/foreclosure_filter.py` &middot; phase 1 &middot; live

Keeps only real first-to-market trustee sales out of the foreclosure search results.

**Trigger.** Any record from a Foreclosure saved search.

**Does.**

- Matches full notice text against observed trustee sale title variations
- Applies INCLUDE_PHRASES and EXCLUDE_PHRASES
- Non-foreclosure notice types pass through unfiltered

**Human checkpoint.** None. Runs unattended.

**Outputs.** Filtered foreclosure set

**Touches.** foreclosure_filter.py

> **The trap this exists to avoid.** Not everything in a Foreclosure saved search is a foreclosure. Skipping this filter poisons the whole downstream list.

#### Courthouse Photo Importer

`src/photo_importer.py, src/image_utils.py` &middot; phase 1 &middot; live

Turns phone photos of courthouse terminal screens into structured records.

**Trigger.** photo-import command, or a new file landing in Dropbox.

**Does.**

- EXIF transpose, blur check, bilateral filter, perspective correction, Otsu threshold
- Tesseract OCR at PSM 4
- LLM parse into NoticeData across all 7 notice types

**Human checkpoint.**

- Runner takes the photos at the terminal

**Outputs.** NoticeData records

**Touches.** OpenCV, Tesseract, LLM parser

> **The trap this exists to avoid.** Moire from terminal screens is the number one OCR killer. Bilateral filter plus Otsu beats adaptive threshold and CLAHE, and PSM 4 beats the PSM 6 that every research guide recommends. Do not run Tesseract OSD rotation on phone photos, EXIF already handled it.

#### PDF Importer

`src/pdf_importer.py` &middot; phase 1 &middot; live

Ingests scanned notice PDFs into the same pipeline.

**Trigger.** Manual import of a scanned batch.

**Does.**

- pypdfium2 renders pages to images
- Shared OCR utilities extract text
- Records flow into the standard enrichment pipeline

**Human checkpoint.** None. Runs unattended.

**Outputs.** NoticeData records

**Touches.** pypdfium2, Tesseract

> **The trap this exists to avoid.** PDF and photo imports set date_added explicitly and it is preserved, not re-stamped by the pipeline run.

#### Dropbox Watcher

`src/dropbox_watcher.py` &middot; phase 1 &middot; live

Cursor-based polling so a runner just uploads photos and walks away.

**Trigger.** Continuous loop, default 15 minute interval.

**Does.**

- Polls the Dropbox cursor for new files
- Resolves county and notice type from the folder path
- Processes through the photo importer, then deletes on success

**Human checkpoint.**

- Runner organizes uploads as /County/notice_type/

**Outputs.** Processed records, dropbox_state.json

**Touches.** Dropbox API

> **The trap this exists to avoid.** Folder path IS the metadata. /Knox/eviction/photo.jpg is how county and type get resolved, so a misfiled photo is a mislabeled record.

#### Knox FTM Puller

`src/knox_ftm_pull.py` &middot; phase 1 &middot; live

Sweeps every Knox source that carries a property address and applies the buy box.

**Trigger.** python src/knox_ftm_pull.py

**Does.**

- Pulls liens, state and federal tax liens, and trustee deeds from the Register of Deeds
- Pulls notices from tnpublicnotice, condemnations from city agendas, evictions from the court
- Enriches against SiftMap, applies buy box, writes the upload CSV
- Routes upside-down records to a separate exclusion file

**Human checkpoint.**

- Operator sets the buy box

**Outputs.** knox_ftm_pull.csv, _upside_down.csv

**Touches.** Register of Deeds, Knox Tax API, SiftMap

> **The trap this exists to avoid.** Condemnations are one cycle only and evictions are one week only. The city overwrites its agenda PDFs and the court keeps only the current week, so roughly 86 back-dated URLs all 404. These accumulate forward or not at all.

#### Lien Resolver

`src/knox_lien_resolve.py` &middot; phase 1 &middot; live

Turns lien debtor names into parcels, then drops anyone already satisfied.

**Trigger.** python src/knox_lien_resolve.py --all

**Does.**

- Liens carry no parcel id, so the join is debtor name into the open county tax API
- Computes active liens as recorded minus released
- Drops fully cleared debtors, states '3 of 8 still active' in Notes

**Human checkpoint.** None. Runs unattended.

**Outputs.** Debtor to parcel matches

**Touches.** Knox Tax API, Register of Deeds

> **The trap this exists to avoid.** 27,493 release documents exist against 12 months of liens, and 8% of lead debtors had EVERY lien already satisfied. Release filtering is not optional. Full-run name-join hit rate is 40%, not the 64% a sorted sample suggested.

#### Foreclosure Consolidator

`src/consolidate_foreclosures.py` &middot; phase 1 &middot; live

Builds a master list of still-active foreclosures from the last N months of runs.

**Trigger.** python src/consolidate_foreclosures.py --months 3

**Does.**

- Pulls each Apify run's output.csv from its key-value store
- Merges local output CSVs
- Dedupes by property, keeping the latest sale date
- Drops anything whose auction date has already passed

**Human checkpoint.** None. Runs unattended.

**Outputs.** foreclosure_master_active_<date>.csv

**Touches.** Apify

> **The trap this exists to avoid.** Dedupe by property, not by notice. Republished and postponed notices collapse to one record only if you key on address plus city and keep the latest sale date.

#### Proof-of-Source Capture

`archive/notice_screenshots/notice_screenshot.py` &middot; phase 1 &middot; retired

RETIRED 2026-08-14. Captured a full-page screenshot of each notice as proof of source.

**Trigger.** The moment the CAPTCHA clears and the notice is visible.

**Does.**

- Full-page screenshot of the detail page
- Hosted to Apify key-value store or Google Drive
- URL rides along as a custom field plus a Notes line into DataSift

**Human checkpoint.** None. Runs unattended.

**Outputs.** notice_<ID>.png, Hosted screenshot URL

**Touches.** Scrapfly, Google Drive, Apify

> **The trap this exists to avoid.** Retired because nothing used it, and it was the slowest step in a foreclosure notice, which matters directly on a multi-thousand-notice backfill. The fields, the CSV column and the DataSift custom field are deliberately KEPT so historical records retain their URLs.

### Enrichment & Identity

*Who actually owns it and who can legally sign*

10 agents.

#### Enrichment Pipeline (division lead)

`src/enrichment_pipeline.py` &middot; phase 1 &middot; live

Ten-plus step chain that takes a bare address to a dialable decision maker.

**Trigger.** Every record after intake.

**Does.**

- Address standardization, property data, tax data
- Obituary and heir research where the owner is deceased
- Skip trace waterfall, then phone scoring
- Stamps date_added at run time

**Human checkpoint.**

- Reviews decision-maker confidence before calling

**Outputs.** Enriched NoticeData

**Touches.** enrichment_pipeline.py

> **The trap this exists to avoid.** Every step is optional and states its own failure. One dead API must never take the record with it.

#### Address Standardizer

`src/address_standardizer.py` &middot; phase 1 &middot; live

Normalizes addresses so dedupe and CRM matching actually work.

**Trigger.** Step 1 of enrichment.

**Does.**

- Smarty API standardization
- Normalized form used as the dedupe key

**Human checkpoint.** None. Runs unattended.

**Outputs.** Standardized address

**Touches.** Smarty

> **The trap this exists to avoid.** Address is the upsert key for DataSift. If standardization drifts, re-runs create duplicates instead of updating.

#### Property Enricher

`src/property_enricher.py` &middot; phase 1 &middot; live

Pulls beds, baths, sqft, year built, and valuation for the subject.

**Trigger.** Step 2 of enrichment.

**Does.**

- Zillow property-details-address endpoint
- Merged into NoticeData

**Human checkpoint.**

- County card overrides beat the aggregator

**Outputs.** Property facts

**Touches.** OpenWeb Ninja, Zillow

> **The trap this exists to avoid.** Aggregators get bedroom counts wrong. Explicit CLI county-card values always win over Zillow, and the whole dual-track ARV rests on the bed count being right.

#### Tax Enricher

`src/tax_enricher.py` &middot; phase 1 &middot; live

Pulls per-parcel delinquency, assessed value, and owner of record.

**Trigger.** Step 3 of enrichment.

**Does.**

- Knox County Tax API by parcel
- Delinquent year gated on a positive per-parcel amount

**Human checkpoint.** None. Runs unattended.

**Outputs.** Tax delinquency, Parcel data

**Touches.** Knox County Tax API

> **The trap this exists to avoid.** The county API returns bills per OWNER. Without the per-parcel gate, a multi-parcel owner stamps one property's debt onto another. Only about 12% of parcels owe anything, so a sparse column is correct.

#### Obituary Enricher

`src/obituary_enricher.py` &middot; phase 1 &middot; live

Finds the date of death and the named representative for deceased owners.

**Trigger.** Owner flagged deceased, or a probate record.

**Does.**

- Probate preset sets DM to the court-named PR and skips obituary search entirely
- Otherwise searches obituary sources for the decedent
- Runs the DOD sanity check against the publication date

**Human checkpoint.**

- Must confirm the decedent IS the owner of record

**Outputs.** Date of death, Decision maker, Obituary URL

**Touches.** Obituary sources, Knox Tax API, Scrapfly

> **The trap this exists to avoid.** THE SPOUSE-OBITUARY TRAP. An obituary on the record does not mean the OWNER died. A live Blount case had the husband's obituary on a living widow's record. An unresearched caller would have asked a recent widow for her dead husband. Always match decedent against owner of record first.

#### Deep Prospecting v5

`skills/deep-prospecting-v5` &middot; phase 1 &middot; live

Resolves heirs and required signers via SmartSkip, then confirms against published obituaries.

**Trigger.** Confirmed deceased owner that no cheaper path resolved.

**Does.**

- SmartSkip batch returns relatives AND their phones in one row
- Tracerfy gap-fills the roughly 7% of relatives left phoneless
- Mandatory free obituary and web research supplies DOD and true relationships
- Trestle scores every number into dial tiers

**Human checkpoint.**

- Research pass is mandatory, never skipped

**Outputs.** Heir map, Master dial sheet, Branded PDF pack

**Touches.** SmartSkip, Tracerfy, Trestle IQ, Scrapfly

> **The trap this exists to avoid.** SmartSkip is WRONG about death. It returned Deceased=false for a man who died with a published funeral-home obituary, and it has no DOD column at all. 63% of relationship labels come back generic. The obituary layer is what makes it trustworthy. Cost is $0.24 per record, 4.9x cheaper than the retired Enformion path.

#### Entity Researcher

`src/enformion_business.py, src/entity_researcher.py` &middot; phase 1 &middot; live

Unmasks the human behind an LLC, trust, or corporation.

**Trigger.** Owner is an entity, not a person.

**Does.**

- Enformion BusinessV2 returns officers from corp filings
- Filters out entity self-references and registered-agent fronts
- Reverse-lookups an LLC's residential mailing address to find the principal

**Human checkpoint.** None. Runs unattended.

**Outputs.** Named principals

**Touches.** Enformion BusinessV2, SiftMap

> **The trap this exists to avoid.** Entities cannot be name-traced. 35 of 321 vacant owners were LLCs or trusts, so filter them out of the consumer skip-trace batch up front or you burn the spend. The v1 BusinessSearch type is access-denied; only BusinessV2 works.

#### Tracerfy Skip Trace

`src/tracerfy_skip_tracer.py, src/tracerfy_ftm.py` &middot; phase 1 &middot; live

Cheap batch phone and email fill at about two cents a record.

**Trigger.** Any record or relative missing contact data.

**Does.**

- Batch submission
- Merge found phones back by address upsert

**Human checkpoint.** None. Runs unattended.

**Outputs.** Phone numbers, Emails

**Touches.** Tracerfy

> **The trap this exists to avoid.** DataSift MERGES phones rather than replacing them, so Tracerfy and Enformion accumulate. Run them sequentially, then re-score and re-tag. One live run went from 109 to 302 phones across 33 records.

#### Phone Validator

`src/phone_validator.py` &middot; phase 1 &middot; live

Scores every number and sorts the call list into dial tiers.

**Trigger.** After every skip-trace pass.

**Does.**

- Trestle phone_intel scores activity and line type
- Tiers: 81-100 dial first, 61-80 second, 41-60 third, 21-40 fourth, 0-20 drop
- Litigator risk check
- Writes phone tags back to DataSift

**Human checkpoint.** None. Runs unattended.

**Outputs.** Tiered dial list, DataSift phone tags

**Touches.** Trestle IQ

> **The trap this exists to avoid.** On a shared household line the owner rule wins: the number carries source and tier only, never a relationship tag, or the dial sheet labels the owner's own landline 'Husband'.

#### Probate Property Finder

`src/property_lookup.py` &middot; phase 1 &middot; live

Finds the property when the court record has names but no address.

**Trigger.** Probate record with a PR and decedent but no property.

**Does.**

- Tier 1: Knox Tax API name search, scored by token overlap, accept at 0.4 or better
- Tier 2: executor family search for transferred family property
- Tier 3: people search for the decedent's last known address

**Human checkpoint.**

- Confidence score reviewed before the record ships

**Outputs.** Property address, Confidence score

**Touches.** Knox Tax API, People search, Scrapfly

> **The trap this exists to avoid.** Courthouse probate records have decedent and executor names but no property address. Without this three-tier lookup the entire probate niche is unusable.

### Deal Analysis

*What it is worth, what it costs, what you can pay*

11 agents.

#### Post-Walkthrough Package (division lead)

`src/post_walkthrough.py` &middot; phase 1 &middot; live

The one workbook you build the hour after walking a house. Nine sheets, Sift-linked.

**Trigger.** python src/post_walkthrough.py after a walk.

**Does.**

- Loads the LIVE Sift lead as the anchor, not just the address
- Spins comps, rehab, walk findings, exits, and the dispo stack together
- Renders Overview, Exit Strats, Comps, Active-Pending, Repair Logic, Repair Numbers, Buyer Targets, Outreach, Lender Analysis

**Human checkpoint.**

- Walkthrough JSON is the human layer and overrides the live record

**Outputs.** 9-sheet branded Excel workbook

**Touches.** Sift Deal Room API, SiftMap, Zillow, openpyxl

> **The trap this exists to avoid.** Exact numbers, not ranges, in the cells. A wide band is not an answer you can take to a seller or a buyer. The lo/hi still drives the math and surfaces as one 'If it moves' sensitivity line.

#### Comp Package Engine

`src/comp_package.py, src/zillow_market_api.py` &middot; phase 1 &middot; live

Boundary-filtered comps and dual-track ARV for a subject property.

**Trigger.** python src/comp_package.py with a subject address.

**Does.**

- Band-partitioned Zillow /search pull to beat the 41-row cap
- Boundary clip by bbox AND street regex, both applied
- Condition bucketing by sold price over Zestimate ratio
- Dual-track ARV, then MAO math and buyer matching

**Human checkpoint.**

- County card overrides for beds, baths, sqft, year built

**Outputs.** Branded comp workbook

**Touches.** OpenWeb Ninja /search, Zillow

> **The trap this exists to avoid.** The API contract moved. similar-sale-homes is retired and 404s. Every /search caps at 41 rows with totalPages=1, so you partition by price band and recursively split saturated bands, 50 to 80 calls to recover 2 to 3 years. price_min and price_max are SILENTLY ignored, so always check the echoed parameters object.

#### Dual-Track ARV Engine

`src/post_walkthrough.py, src/comp_package.py` &middot; phase 1 &middot; live

Prices the subject inside its own bedroom band and labels reconfig upside separately.

**Trigger.** Called by the comp package and post-walkthrough.

**Does.**

- Base ARV from same-bed renovated comps only, clamped to that band's median
- Reconfig-to-more-beds rides as a labeled UPSIDE track capped at band p75
- tight_arv applies recency, size window, and same-bed rules for underwriting
- refine_bucket demotes investor buys that Zillow re-anchored to look renovated

**Human checkpoint.**

- Reconfig upside credited only after a walkthrough verifies the layout converts

**Outputs.** Base ARV, Upside ARV, Basis string

**Touches.** comp_package.py

> **The trap this exists to avoid.** A subject below the comp set's bed count lives in a LOWER value band than per-bedroom adjustments imply. In 37914, renovated 2-beds capped at $215-280K while same-size 3/2s ran $285-385K. Extra square footage cannot escape the band.

#### Rehab Estimator

`src/rehab_estimator.py, src/sku_pricing.py` &middot; phase 1 &middot; live

Room-by-room, four-tier scope priced off the locked Knox material list.

**Trigger.** Deal analysis or post-walkthrough.

**Does.**

- SKU baskets per category from the locked master material list
- Engine labor per category, regional multiplier on LABOR ONLY
- Any missing SKU drops the whole category back to the legacy table with a loud log
- Self-perform estimate alongside the GC grand total

**Human checkpoint.**

- Signed bids replace estimates and collapse the range

**Outputs.** Itemized scope, 4-scenario matrix, Self-perform estimate

**Touches.** Locked material list, Home Depot SKU pricing

> **The trap this exists to avoid.** THE DOUBLE-DISCOUNT TRAP. Locked prices are already Knox-local, so applying the 0.88 regional multiplier to materials under-prices by about 12%. The multiplier is labor only in SKU mode, and a test asserts the exact basket math to catch a leak.

#### Master Material List

`src/material_list.py` &middot; phase 1 &middot; live

The frozen, git-tracked price source every Knox estimate reads from.

**Trigger.** Re-locked only when the PM re-approves the list.

**Does.**

- Pulls fresh Knox pricing by zip
- --lock writes the git-tracked JSON and CSV twins
- An ordinary re-pull refreshes cache but can never drift prices into estimates

**Human checkpoint.**

- PM re-approval required to re-lock

**Outputs.** master_materials_locked_37914.json, CSV twin

**Touches.** Home Depot, SERP pull

> **The trap this exists to avoid.** Only --lock writes to data/. That single rule is what stops a casual re-pull from silently changing every estimate in flight. Current lock: 94 of 94 search keys priced, 88 SKU rows plus 12 allowances.

#### Exit Strategy Engine

`src/post_walkthrough.py` &middot; phase 1 &middot; live

Scores each lane off the conservative ARV and says why each is in or out.

**Trigger.** Post-walkthrough render.

**Does.**

- Scores wholesale assignment, wholetail, flip same-config, flip reconfig, BRRRR
- Only lanes that clear their gate get a suggestion block
- Everything ruled out is named in the headline and explained
- Names the BUYER's exit on the outreach sheet, never our hold

**Human checkpoint.**

- Reconfig lane gated on walkthrough verification

**Outputs.** Ranked exit lanes with rationale

**Touches.** post_walkthrough.py

> **The trap this exists to avoid.** The template's six exit slots were placeholders, not a quota. If nothing clears its gate, the two closest misses render under an explicit banner rather than fabricating a recommendation. Novation is not modelled at all, it was removed rather than hidden.

#### Deal Analyzer

`src/deal_analyzer.py` &middot; phase 1 &middot; live

MAO, ROI, and financing scenarios across multiple loan structures.

**Trigger.** analyze-deal command or the deal-analyzer plugin.

**Does.**

- 75% and 70% MAO rules
- Hard money at 12%, conventional at 7%, 2 points, 2.5% closing
- Exit strategy comparison

**Human checkpoint.**

- Operator sets target margin

**Outputs.** MAO, ROI, Financing comparison

**Touches.** deal_analyzer.py

> **The trap this exists to avoid.** Transfer tax defaults are Tennessee-specific. The skill ships a state reference table because shipping one state's number as universal is how community users get burned.

#### Lender Analysis

`src/post_walkthrough.py` &middot; phase 1 &middot; live

Bakes private money into every resale lane and renders the lender's own view.

**Trigger.** A financing block present in the walkthrough JSON.

**Does.**

- Profit goes net of debt: points plus interest over the lane's hold
- Sources and uses, draw schedule, loan-to-ARV, equity cushion
- Band-floor stress case and payoff waterfall at the point ARV
- Lender's annualized yield

**Human checkpoint.**

- Assumed terms paint a red placeholder banner until real terms land

**Outputs.** Lender Analysis sheet

**Touches.** post_walkthrough.py

> **The trap this exists to avoid.** ROI reads as cash-on-cash when financed, and a financed flip must ALSO clear the $10K wholesale floor to stay suggested. No financing block means the old cash-basis math, byte identical.

#### Lot Split Underwriter

`src/lot_split_underwrite.py` &middot; phase 2 &middot; live

Underwrites splitting a parcel as its own exit lane.

**Trigger.** python src/lot_split_underwrite.py

**Does.**

- Parcel and zoning review
- Split scenario economics
- Comparison against the standard lanes

**Human checkpoint.**

- Zoning confirmation before relying on it

**Outputs.** Lot split underwrite workbook

**Touches.** Knox Tax API, SiftMap

> **The trap this exists to avoid.** Run on 306 N Morgan and 2810 Barton. Treat the split as an exit lane that competes with the others, not as free upside stacked on top.

#### Lender Package Builder

`src/lender_package.py` &middot; phase 1 &middot; live

The 8-piece set handed to a private money lender to fund one named property.

**Trigger.** python src/lender_package.py --spec deals/<deal>.json

**Does.**

- Mirrors The Repair Estimator block for block, inputs inline beside the answers
- Writes 240+ live Excel formulas off workbook defined names, including the prose
- Rolls the 65-line repair grid up into the front page
- Sizes every column from what a cell will DISPLAY, not what it holds

**Human checkpoint.**

- Reviews and edits the blue input cells directly in Excel
- Negotiates the loan amount, the one input that sets the deal

**Outputs.** The Private Lender Package.xlsx, 8 tabs

**Touches.** openpyxl, deal spec JSON

> **The trap this exists to avoid.** Read the workbook back before regenerating over it. Ty reviews in Excel and edits inputs directly; on one review he made three changes and mentioned one. Also: DayOne = Loan minus RehabTotal is derived, never typed, because typing the closing advance let financed closing costs land in the draw tranche and the holdback disagreed with the repair budget by $1,608.

#### Lender Document Set

`src/lender_docs.py` &middot; phase 1 &middot; live

Cover letter, promissory note, personal guarantee, closing instructions, insurance request, investor sheet, satisfaction and release.

**Trigger.** python src/lender_docs.py --spec deals/<deal>.json

**Does.**

- Picks exactly one side of every either/or branch from the spec
- Renders one signature block per company member on the guarantee
- Re-reads every rendered document and raises if the internal notes block survived

**Human checkpoint.**

- Signs. A closing attorney draws the deed of trust on their own form.

**Outputs.** 7 .docx documents, numbered 1 and 3 through 8

**Touches.** python-docx, CMO Stack voice guide

> **The trap this exists to avoid.** The templates carry a NOTES FOR CLAUDE block that must never reach a lender, so build_all re-reads each rendered file and raises rather than trusting the code path. main exits 1 if fewer than 7 documents write, and stale files from a previous numbering are deleted, so a folder cannot grow a second copy of everything.

### Dispo & Buyers

*Deed-verified buyers before you need them*

4 agents.

#### Deal Package Generator (division lead)

`src/deal_package.py` &middot; phase 1 &middot; live

Six-sheet workbook that turns analysis into a dial sheet somebody can work today.

**Trigger.** python src/deal_package.py --spec

**Does.**

- Deal Summary, Dial Sheet, Deal Math, Comps, Pitch and Sequence, Sources and Audit
- Per-buyer open and target prices, tuned to each buyer's model
- 30-second script, objection answers, day-by-day plan

**Human checkpoint.**

- Operator approves the ask prices

**Outputs.** 6-sheet branded workbook

**Touches.** openpyxl, buyer_sweep output

> **The trap this exists to avoid.** Per-buyer ask prices are tuned to each buyer's model, self-performer above landlord above out-of-state. One blast number leaves money on the table and burns credibility.

#### Buyer Sweep

`src/buyer_sweep.py` &middot; phase 1 &middot; live

Deed-level sweep of who actually buys in a zip, ranked by fit.

**Trigger.** python src/buyer_sweep.py --zip

**Does.**

- Pulls the sold universe, filters to the investor band
- Per sale runs SiftMap autocomplete then get_detail for the DEED sale history
- Aggregates buyer name, cash flag, portfolio size, equity, mailing
- Ranks by purchase count, band fit, and portfolio

**Human checkpoint.** None. Runs unattended.

**Outputs.** Ranked buyer list JSON and CSV

**Touches.** SiftMap, Zillow /search, Enformion BusinessV2

> **The trap this exists to avoid.** THE HARPER MOVE. When an LLC's mailing address is a residence, reverse-look up that address and take the human owner as the principal. Live run resolved 175 of 193 sales and unmasked TN Super Props to Jonathan Harper, Braden Family to Joshua Braden.

#### Dispo Skiptrace

`src/dispo_skiptrace.py` &middot; phase 1 &middot; live

Three-source waterfall with a built-in audit matrix showing which source missed.

**Trigger.** After the buyer sweep ranks a list.

**Does.**

- Source 1 Enformion person search, address-anchored
- Source 2 Tracerfy batch at two cents a record
- Source 3 web people-search cross-check, merged in manually
- Dedupes the union, Trestle-scores every unique number

**Human checkpoint.**

- Web source is manual because aggregators bot-block

**Outputs.** Cross-confirmed contact list, Per-contact source audit

**Touches.** Enformion, Tracerfy, Trestle IQ

> **The trap this exists to avoid.** The audit matrix answers the question everyone forgets to ask: did we skip-trace this landline at BOTH Tracerfy and Enformion? Single-source numbers get flagged so you know what you are actually dialing.

#### Buyer Prospector

`skills/buyer-prospector` &middot; phase 1 &middot; live

Builds a county buyers list from a nationwide database of active buyers.

**Trigger.** Any new market.

**Does.**

- Pulls buyers for the county from 84K-plus records
- Categorizes LLCs, trusts, and corporations
- Researches decision makers for skip tracing across 50-state SOS lookups

**Human checkpoint.**

- Reviews entity research before spending on skip trace

**Outputs.** County buyers workbook

**Touches.** Nationwide buyer database, State SOS sites

> **The trap this exists to avoid.** This is the cold-start answer for a new market. The buyer sweep needs deed history you do not have yet; the prospector gives you a list on day one.

### Outreach & Marketing

*The seller should feel found, not targeted*

9 agents.

#### Two-Way SMS Agent (division lead)

`src/sms_agent/` &middot; phase 2 &middot; gated

Sends outreach, reads replies live, classifies them, writes back to the CRM, hands positives to a prospector.

**Trigger.** smrtPhone smsIncoming webhook, or a seeded outreach touch.

**Does.**

- Receiver ingests the webhook into a SQLite event log
- Classifier decides intent, opt-outs decided by regex and never by a model
- Responder drafts inside a hard output validator
- CRM writes phone status, opt-outs, and lead status; Slack gets the handoff

**Human checkpoint.**

- Autonomy ladder. Phase 2 delivers the handoff with zero AI-authored text sent.

**Outputs.** Classified replies, CRM writes, Slack handoffs

**Touches.** smrtPhone API, DataSift API, Slack, Fly.io, SQLite

> **The trap this exists to avoid.** DataSift webhooks CANNOT see an inbound text. Webhooks shipped as a sequence ACTION, so all ten triggers are CRM state changes. smrtPhone's smsIncoming is the only inbound leg. That single constraint shapes the entire architecture.

#### Reply Classifier

`src/sms_agent/respond.py` &middot; phase 2 &middot; gated

Reads an inbound text and decides what it means, with a confidence floor.

**Trigger.** Every inbound SMS.

**Does.**

- Regex rules handle opt-outs and obvious cases
- Model handles the rest at a 0.80 confidence floor
- Below the floor it drafts for a human instead of paging a prospector

**Human checkpoint.**

- Anything under the confidence floor

**Outputs.** Intent label, Confidence, Soft vs hard no

**Touches.** Claude, smrtPhone

> **The trap this exists to avoid.** Backfill proved it on 9 real replies before anything was wired, and it was correct on all 9. It also caught that 'I'd like to get the house in auction if it's cheap enough' is a BUYER, not a seller, and correctly refused to escalate at 0.55.

#### Reply Drafter

`src/sms_agent/respond.py` &middot; phase 3 &middot; planned

Writes the reply in a human voice, inside a validator that hard-blocks anything risky.

**Trigger.** A classified reply that warrants a response.

**Does.**

- Given almost nothing on purpose: first name, street line, city, county
- Signs as the record's assigned human, resolved from the assignee uuid
- Validator blocks dollar amounts, list names, links, zips, two questions, or self-identifying as automated

**Human checkpoint.**

- Phase 3 holds every draft in Slack for approval

**Outputs.** Draft reply

**Touches.** Claude, playbook.md

> **The trap this exists to avoid.** The model invents an identity. With no name configured it introduced itself as 'Alex'. Unresolved identity now means the agent is explicitly told it has NO name and NO company name rather than being left to fill the gap.

#### Text Touch Builder

`skills/text-touch-builder` &middot; phase 1 &middot; live

Four pre-call SMS touches per hot record, varied like cold email so nothing looks blasted.

**Trigger.** A ready-to-call list export.

**Does.**

- Renders identity check, drip, soft ask, and breakup touches
- Rotates copy variants across the pools
- Writes back into Text Touch 1-4 custom fields via CSV re-import

**Human checkpoint.**

- Caller copies the next touch into the dialer before calling

**Outputs.** Four touches per record in DataSift

**Touches.** DataSift Add-Data, text-touch-builder skill

> **The trap this exists to avoid.** clean_first('E A Henry') took the first token of length 2 or more, walked past the initials, and greeted people by their SURNAME. The fix is positional: only tokens BEFORE the surname can supply a first name. This bug shipped in two places and was fixed in both.

#### MMS Screenshot Sender

`src/mms_sender.py` &middot; phase 2 &middot; gated

Texts each foreclosure homeowner the actual auction notice image.

**Trigger.** Operator go, per campaign.

**Does.**

- Text goes via the Compose Message modal
- Image goes via the conversation reply box inside main-iframe
- File set on a hidden input, send arrow clicked by bounding box position

**Human checkpoint.**

- PAUSED pre-send. Requires explicit operator GO.

**Outputs.** Sent MMS with proof-of-source image

**Touches.** smrtPhone web app, Playwright, Dropbox

> **The trap this exists to avoid.** smrtPhone has an API for TEXT but not for MMS, which is what forced the browser route. Replies carry no image, so the API is the right transport for conversation and the browser is only for the original screenshot send.

#### Obituary Mail Campaign

`src/obituary_campaign.py, src/obituary_mail_export.py` &middot; phase 2 &middot; live

Builds and validates the direct mail drop for the obituary list.

**Trigger.** After the obituary opportunity ranking.

**Does.**

- Export, validate addresses, produce the mail file

**Human checkpoint.**

- Operator approves the drop

**Outputs.** Validated mail export

**Touches.** DataSift API, Address validation

> **The trap this exists to avoid.** Never name the list. Foreclosure, probate, inherited, tax, lien: none of those words appear in outreach copy, because the seller should feel found, not targeted.

#### Caller Reputation Monitor

`skills/caller-reputation-monitor` &middot; phase 1 &middot; live

Keeps outbound numbers out of Spam Likely by watching your own call outcomes.

**Trigger.** Daily.

**Does.**

- Monitors every caller ID by answer rate, call length, and short calls
- Runs a warm-up, active, watch, rest, retire lifecycle with dial caps
- Writes an HTML health dashboard and a recommended dial pool

**Human checkpoint.**

- Walks the operator through free carrier registration

**Outputs.** Number health dashboard, Recommended dial pool

**Touches.** smrtPhone, Carrier registries

> **The trap this exists to avoid.** The phone number is the asset. Every autonomy decision in the SMS agent exists to protect it.

#### Sender Pool Manager

`src/sms_agent/sender_pool.py` &middot; phase 2 &middot; gated

Owner-bound number pool so a callback reaches the person who claimed to text.

**Trigger.** Every outbound send.

**Does.**

- 18 numbers at 25 a day, 450 a day total
- Prefers the assigned caller's own numbers
- Sticky number per conversation wins over owner preference
- Quiet hours 8am-9pm recipient-local with up to 30 minutes of wake jitter

**Human checkpoint.** None. Runs unattended.

**Outputs.** Assigned sender number

**Touches.** smrtPhone, sms_numbers.json

> **The trap this exists to avoid.** This module was originally named numbers.py, which SHADOWED the stdlib numbers module and broke pydantic inside the Anthropic SDK. The exception was swallowed and every classification silently degraded to the weak keyword fallback while still returning a plausible answer.

#### Obituary Mail Export

`src/obituary_mail_export.py` &middot; phase 2 &middot; gated

Turns the ranked obituary opportunity list into a validated mail file.

**Trigger.** After obituary_opportunity ranks the list.

**Does.**

- Pulls the ranked records
- Validates deliverable mailing addresses
- Writes the mail-house export

**Human checkpoint.**

- Approves the drop and the spend

**Outputs.** Mail export CSV

**Touches.** DataSift API, Smarty

> **The trap this exists to avoid.** Every row ships a must-verify note. Whether the person who died is the owner of record is NOT verifiable from CRM data, and the spouse-obituary trap is live on every single row: an obituary on the record does not mean the OWNER died.

### CRM Operations

*Get it into DataSift without silently losing half of it*

7 agents.

#### Upload Orchestrator (division lead)

`src/datasift_api_upload.py` &middot; phase 1 &middot; live

Owns the path from finished CSV to live, tagged, enriched CRM records.

**Trigger.** --upload-datasift on any run.

**Does.**

- Mints a JWT and re-mints every 30 minutes so long runs cannot die on expiry
- Upserts by address so re-runs never duplicate
- Fires enrich and skip trace after upload
- Verifies one record read-back before releasing the file

**Human checkpoint.**

- Always upload one record and read it back first

**Outputs.** Live CRM records

**Touches.** DataSift API, Playwright

> **The trap this exists to avoid.** Four traps that each fail silently. Tags must be an ARRAY or you create one tag literally named the whole comma string. A select field needs the OPTION'S UUID, not its label. Entity owners cannot have a blank first_name, and omitting a key is not the same as sending empty. notes on the property payload returns 200 and is discarded.

#### API Uploader

`src/datasift_api_upload.py` &middot; phase 1 &middot; live

Pushes records entirely over the API, no browser.

**Trigger.** python src/datasift_api_upload.py --commit

**Does.**

- POST /property/ upserts by address
- Custom fields PATCH separately by field uuid
- Notes posted separately from the property payload
- Lists accumulate rather than overwrite

**Human checkpoint.**

- --limit 1 verification run is mandatory

**Outputs.** Created and updated records

**Touches.** DataSift internal API

> **The trap this exists to avoid.** The Open API key CANNOT do this job. Custom fields do not exist anywhere in its 93-route surface and every write 401s. Only the minted user JWT reaches /api/internal/ where they do.

#### Schema Setup

`src/datasift_schema_setup.py` &middot; phase 1 &middot; live

Creates custom fields, select options, and lists. Idempotent, dry-run by default.

**Trigger.** python src/datasift_schema_setup.py --commit

**Does.**

- Creates the field structure
- Select fields require options in the same POST
- Safe to re-run

**Human checkpoint.** None. Runs unattended.

**Outputs.** CRM schema

**Touches.** DataSift internal API

> **The trap this exists to avoid.** Dry run by default is the right posture for anything that mutates an account's schema. Creating a select custom field REQUIRES its options in the same POST.

#### Upload Wizard Driver

`src/datasift_uploader.py` &middot; phase 1 &middot; live

Drives the 5-step browser upload wizard when the API path will not do.

**Trigger.** Legacy or wizard-only flows.

**Does.**

- Setup, tags, upload file, map columns, review and finish
- Core address fields auto-map; tags, lists, and custom fields need manual mapping

**Human checkpoint.**

- Watches the column mapping step

**Outputs.** Uploaded list

**Touches.** Playwright, DataSift web app

> **The trap this exists to avoid.** Only core address fields reliably auto-map. Tags, Lists, and every enrichment column routinely stay unmapped in step 4, which is exactly how you upload 2,500 records that attach to nothing.

#### Preset Manager

`src/datasift_uploader.py, src/niche_sequential.py` &middot; phase 1 &middot; live

Discovers and updates the 21 filter presets that drive sequential marketing.

**Trigger.** manage-presets --all

**Does.**

- Opens the filter panel, scrolls to the bottom, expands Filter Presets
- Applies the Sold exclusion across all presets
- Saves with overwrite confirmation

**Human checkpoint.** None. Runs unattended.

**Outputs.** Updated presets

**Touches.** Playwright, DataSift web app

> **The trap this exists to avoid.** The filter panel is a scrollable div, not the viewport, so Playwright's scroll_into_view_if_needed does nothing. You need JS scrollIntoView. And the Beamer NPS iframe blocks ALL pointer events globally until you remove it from the DOM.

#### Sequence Builder

`src/sequence_templates.py` &middot; phase 1 &middot; live

Builds the 26 TCA sequence templates via drag and drop.

**Trigger.** create-sold-sequence, or sequence template deployment.

**Does.**

- Trigger, condition, then actions
- Slow mouse drag in 20 incremental steps because cards are draggable=false
- Duplicate name detection retries with a V2 suffix

**Human checkpoint.** None. Runs unattended.

**Outputs.** Live sequences

**Touches.** Playwright, React DnD

> **The trap this exists to avoid.** Cards carry draggable=false so Playwright's native drag will not work. Mouse down, 20 incremental moves at 50ms, mouse up, with 500ms pauses between phases.

#### SiftMap Sold Tagger

`src/datasift_uploader.py` &middot; phase 1 &middot; live

Tags sold properties so they drop out of active marketing.

**Trigger.** manage-sold --months-back 12

**Does.**

- Searches by city, not county
- Adds records with tags
- Sold Property Cleanup sequence fires on the tag

**Human checkpoint.** None. Runs unattended.

**Outputs.** Tagged sold records

**Touches.** Playwright, SiftMap

> **The trap this exists to avoid.** Known limitation, stated out loud: SiftMap filters set values visually but do not trigger a React re-query, so only the 3 to 5 sidebar-visible properties get added per run.

### Market Intelligence

*Where to spend the next marketing dollar*

4 agents.

#### Market Analyzer (division lead)

`src/market_analyzer.py` &middot; phase 1 &middot; live

Six-factor weighted zip scoring with letter grades and budget allocation.

**Trigger.** After a market finder extraction.

**Does.**

- Distress 30, Value 20, Equity 15, Tax Delinquency 15, Competition 10, DOM 10
- Grades A through D
- Allocates budget across the top zips

**Human checkpoint.**

- Operator sets the budget

**Outputs.** Scored zip list, Budget allocation

**Touches.** market_analyzer.py

> **The trap this exists to avoid.** Weights are set from measured distribution, not intuition. Weighting a near-constant variable produces a ranking with no spread, which looks rigorous and tells you nothing.

#### Market Finder Extractor

`src/extract_market_finder.py` &middot; phase 1 &middot; live

Pulls every zip and neighborhood row out of DataSift Market Finder.

**Trigger.** python src/extract_market_finder.py

**Does.**

- Selects state and county via InputMultiSearch
- Paginates all pages at 20 rows each
- Extracts the county summary panel via regex

**Human checkpoint.** None. Runs unattended.

**Outputs.** Market finder JSON

**Touches.** Playwright, DataSift Market Finder

> **The trap this exists to avoid.** There is no HTML table element. The data table is entirely div-based styled-components, so searching for tr or td finds nothing. It is pagination, not infinite scroll, and pagination controls sit below the viewport until you scroll the body container.

#### County Market Report Generator

`src/county_market_report.py` &middot; phase 1 &middot; live

Merges a Market Finder extraction with a public-data bundle into a 7-sheet county research workbook. County-agnostic.

**Trigger.** After extraction and scoring.

**Does.**

- Blends Sift data with BLS, Census, Zillow, Redfin, FBI crime data
- Renders 7 branded sheets

**Human checkpoint.** None. Runs unattended.

**Outputs.** 7-sheet market report

**Touches.** openpyxl, Public data sources

> **The trap this exists to avoid.** Sheets 4, 5 and 6 (Economic, Crime, Recommendations) are FULLY populated rather than placeholders, which is the difference between a report someone acts on and a template someone files. Lived in a gitignored output/ directory until 2026-08-17, where it was one cleanup away from being lost.

#### Obituary Opportunity Ranker

`src/obituary_opportunity.py` &middot; phase 1 &middot; live

Turns the obituary list into a lean-budget call order.

**Trigger.** python src/obituary_opportunity.py

**Does.**

- Pulls detail and custom fields, applies gates, scores six weighted components
- Gates: under 3 months since death, already sold, dead statuses, upside down, over buy box
- Renders a branded 6-sheet Excel

**Human checkpoint.**

- Every row ships a Must Verify note

**Outputs.** Ranked call order workbook

**Touches.** DataSift API, openpyxl

> **The trap this exists to avoid.** Total Delinquency is liens PLUS taxes. Reading it as the tax figure double counts the lien and inflates exactly the records the model exists to surface. It put a lien-only record at rank 1 before the fix. Also: gate on lead status, not just on sold, or a not_interested record tops your ranking.

### Coaching & Performance

*Grade the humans and the agents on the same rubric*

8 agents.

#### Call Coaching Engine (division lead)

`src/call_coaching/` &middot; phase 1 &middot; live

Pulls real recordings, transcribes with tonality, routes to three grading rubrics.

**Trigger.** Weekly, or on demand per caller.

**Does.**

- Pull calls over the minimum duration that have a recording
- Transcribe with delivery notes, then triage into a pipeline
- Route to cold call, lead manager, or closer rubric
- Write per-call reports and per-caller scorecards

**Human checkpoint.**

- Coach reviews scorecards with the caller

**Outputs.** Coaching reports, Scorecards, Excel workbook

**Touches.** smrtPhone, OpenRouter Gemini, Claude

> **The trap this exists to avoid.** Voicemails and wrong numbers are never scored. Short calls get their own scale, because grading a 40-second wrong number against a full discovery rubric produces a meaningless zero.

#### Call Puller

`src/call_coaching/pull_calls.py` &middot; phase 1 &middot; live

Fetches the call log and downloads recordings.

**Trigger.** Start of a coaching run.

**Does.**

- POST /logs/calls/filtered as a DataTables form with a cookie session
- Filters by minimum seconds and presence of a recording
- Downloads MP3s from the direct recording URL

**Human checkpoint.**

- Session expiry means re-running the login script

**Outputs.** Call metadata, MP3 recordings

**Touches.** smrtPhone web session

> **The trap this exists to avoid.** Recording URLs on rec.smrtphone.io are public once known, no auth. The call log itself needs the cookie session, and an expired session exits 2 rather than returning an empty list.

#### Transcription & Triage

`src/call_coaching/transcribe.py` &middot; phase 1 &middot; live

Two passes: diarized transcript with delivery notes, then strict-JSON triage.

**Trigger.** Each downloaded recording.

**Does.**

- Pass 1: audio to diarized transcript with bracketed delivery notes and a pace, tone, talk-balance summary
- Pass 2: text to strict JSON, call type, pipeline, worth_grading
- Groups the review queue by pipeline

**Human checkpoint.** None. Runs unattended.

**Outputs.** Transcripts, review_queue.json

**Touches.** OpenRouter, Gemini 2.5 Flash

> **The trap this exists to avoid.** AGENT and SELLER labels are decided by CONTENT with the caller name as anchor. On a callback the labels otherwise swap, and every downstream grade is then assigned to the wrong person.

#### Cold Call Coach

`skills/cold-call-coach` &middot; phase 1 &middot; live

Grades opener, motivation probing, objection handling, tonality, close.

**Trigger.** Transcripts triaged to the cold_call pipeline.

**Does.**

- Scores against the cold-calling rubric
- Calibration examples keep reliability inside 3 points
- JSON score footer per call

**Human checkpoint.**

- Coach delivers the feedback

**Outputs.** Per-call report, Caller scorecard

**Touches.** Claude, DataSift Call Playbook

> **The trap this exists to avoid.** Measured reliability is stated as plus or minus 3 points. A grading rubric that will not state its own error bar is not a measurement, it is an opinion.

#### Lead Manager Coach

`skills/lead-manager-coach` &middot; phase 1 &middot; live

Grades the four pillars, roadblocks, no-ladder, and next-action discipline.

**Trigger.** Transcripts triaged to lead_management.

**Does.**

- Scores qualification: condition, timeline, motivation, price
- Call quality only, no CRM hygiene scoring

**Human checkpoint.**

- Coach delivers the feedback

**Outputs.** Per-call report, Scorecard

**Touches.** Claude, LEAD-M_1.MD

> **The trap this exists to avoid.** Deliberately excludes CRM hygiene. Mixing 'did you update the record' into a call-quality score hides whether the person can actually run a conversation.

#### Closer Coach

`skills/closer-coach` &middot; phase 1 &middot; live

Grades the money conversation, the three-option offer stack, and commitment locking.

**Trigger.** Transcripts triaged to closing.

**Does.**

- Discovery deepening
- Offer presentation
- Objection frameworks
- Negotiation timeline reports

**Human checkpoint.**

- Coach delivers the feedback

**Outputs.** Per-call report, Scorecard

**Touches.** Claude, DataSift Call Playbook

> **The trap this exists to avoid.** The flywheel worth building next: point these same three rubrics at the SMS agent's own threads, so the texter is graded exactly like the humans.

#### KPI Engine

`skills/kpi-engine` &middot; phase 1 &middot; live

Activity-log pull and funnel pacing straight from the account.

**Trigger.** Daily or weekly.

**Does.**

- Pulls the activity log with its own JWT
- Three distinct rates, lead counting including new_lead statuses
- Funnel pacing from dials to correct to leads to appointments to contracts
- Outputs markdown, CSV, Excel, or Slack

**Human checkpoint.**

- Benchmarks are baselines to tune per operation, not targets

**Outputs.** KPI report, Slack digest

**Touches.** DataSift API, Slack

> **The trap this exists to avoid.** Benchmarks ship as tune-per-operation baselines. Publishing someone else's conversion rate as a target is how you get a team optimizing for a number that never applied to them.

#### Playbook Creator

`skills/playbook-creator` &middot; phase 1 &middot; live

Turns a transcript or a recorded process into an SOP with process maps.

**Trigger.** Any repeatable process worth documenting.

**Does.**

- Transcript to structured workflow
- Mermaid flowcharts, decision trees, screenshot placeholders
- Seven-node chart limit, 5th grade reading level

**Human checkpoint.**

- Operator supplies screenshots

**Outputs.** Word doc SOP, Process maps

**Touches.** Mermaid, docx

> **The trap this exists to avoid.** Seven nodes per chart and a 5th grade reading level are hard limits, not style preferences. An SOP nobody can follow at 7am is not an SOP.

### Sphere of Influence

*Reverse-search a realtor's own contact export into a scored homeowner call list.*

6 agents.

#### SOI Pipeline (division lead)

`src/soi_intake.py` &middot; phase 2 &middot; live

Turns name-and-email-only contact exports into confirmed metro homeowners ranked by Realtor AI score.

**Trigger.** A realtor hands over a Facebook or LinkedIn contact export.

**Does.**

- Normalize and dedupe the roster
- Join against free county owner rolls
- Pay to resolve only the misses
- Score the confirmed homeowners

**Human checkpoint.**

- Decides the call order and makes the calls

**Outputs.** soi_priority_list.csv

**Touches.** SQLite, Enformion, SiftMap

> **The trap this exists to avoid.** First live run: 848 raw rows to 728 unique people to 222 confirmed metro homeowners to 191 scored. The distribution is steep, one at 95+, six in the 80s, so the 95+ bar isolates a real call list rather than a third of the sphere.

#### Contact Intake

`src/soi_intake.py` &middot; phase 2 &middot; live

Normalizes and dedupes the raw social exports.

**Trigger.** python src/soi_intake.py

**Does.**

- Normalizes names and emails
- Dedupes across sources
- Flags real-estate-industry domains as referral partners, not sphere

**Human checkpoint.** None. Runs unattended.

**Outputs.** soi_contacts_normalized.csv

**Touches.** stdlib

> **The trap this exists to avoid.** 27 contacts on the live run were kw.com, mortgage or title domains. Those are referral partners, not homeowner sphere, and scoring them as leads pollutes the call list.

#### County Owner Pull

`src/soi_county_pull.py, src/soi_owner_db.py` &middot; phase 2 &middot; live

Pulls the owner rolls for the whole metro, free.

**Trigger.** python src/soi_county_pull.py then soi_owner_db.py

**Does.**

- Franklin from the open file server
- Fairfield from the nightly CAMA dump
- Four more counties from open ArcGIS layers
- Loads 811,146 rows into SQLite

**Human checkpoint.** None. Runs unattended.

**Outputs.** output/soi/owners.db

**Touches.** ArcGIS, SQLite

> **The trap this exists to avoid.** The whole Columbus metro is free data and costs $0. Two traps: Franklin's newer-looking tab-delimited appraisal extract has NO owner fields at all, and the Parcel_CSV folder path is stale on purpose, so check the Last-Modified header rather than the path. The vendor search UIs are bot-walled and never needed.

#### Owner Name Join

`src/soi_owner_match.py` &middot; phase 2 &middot; live

Matches roster people to deeds.

**Trigger.** python src/soi_owner_match.py

**Does.**

- Handles LAST FIRST M deed order and co-owner ampersands
- Strips credentials from LinkedIn surnames
- Searches the Facebook middle token as a maiden name
- Boosts a household pair when two contacts hit the same deed

**Human checkpoint.**

- Reviews ambiguous same-name groups

**Outputs.** soi_owner_matches.csv

**Touches.** owners.db

> **The trap this exists to avoid.** owner_occupied compares mailing address key to situs address key. Do NOT fall back to Franklin's OWNER_ADD1: it sometimes echoes the situs and false-flagged a Texas absentee as owner-occupied.

#### Enformion Resolver

`src/soi_enformion.py` &middot; phase 2 &middot; live

Pays to resolve only the roster misses.

**Trigger.** python src/soi_enformion.py, about $0.10 per match

**Does.**

- Anchors Person Search on name plus the metro city and state
- Matches the response emails against the contact's exported email
- Cross-checks each resolved address back against owners.db

**Human checkpoint.**

- Approves the spend

**Outputs.** soi_enformion_resolved.json

**Touches.** Enformion Person Search

> **The trap this exists to avoid.** EMAIL is the verifier. An exact email hit grounds identity with no address needed; name-only matches are kept but flagged. emailAddresses is a list of DICTS with .emailAddress inside, not a list of strings. Misses are free.

#### Realtor Score Enrichment

`src/soi_enrich.py` &middot; phase 2 &middot; live

Attaches the Realtor AI score to each confirmed homeowner.

**Trigger.** python src/soi_enrich.py

**Does.**

- Autocomplete then get_detail per matched address
- Requires a token overlap between the deed owner and SiftMap owner_info before trusting the row
- Carries equity, mortgage, portfolio count and both investor scores

**Human checkpoint.**

- Calls the 95+ list

**Outputs.** soi_priority_list.csv

**Touches.** SiftMap

> **The trap this exists to avoid.** realtor_score off SiftMap get_detail IS the Realtor AI score, and 95+ is a priority call. 8 rows were flagged rather than trusted because the deed owner and SiftMap owner did not overlap. Renters are kept on their own track as future first-time buyers.

## What the system depends on

**County & Public Record**

- tnpublicnotice.com
- Knox County Tax API
- Register of Deeds
- City condemnation agendas
- Eviction court docket

**Property & Market Data**

- SiftMap
- Zillow /search (OpenWeb Ninja)
- Smarty
- Redfin
- BLS / Census / FBI

**Identity & Contact**

- SmartSkip
- Tracerfy
- Enformion BusinessV2
- Trestle IQ
- Obituary sources

**CRM & Comms**

- DataSift (apiv2.reisift.io)
- smrtPhone
- Slack
- Dropbox
- Google Drive

**Runtime & Access**

- Apify Actor
- Fly.io
- Scrapfly
- 2Captcha
- Playwright
- SQLite event log

**Models & Knowledge**

- Claude
- OpenRouter / Gemini
- Locked material list
- DataSift Call Playbook
- playbook.md

---

Install the skills that drive these agents: see the [README](../README.md#install-the-skill-library).
