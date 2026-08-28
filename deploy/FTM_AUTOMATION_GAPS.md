# FTM automation: what runs, what does not, what does not exist

Audited 2026-08-17 by reading the code rather than the docs, because the two
disagree in places. Companion to `deploy/FTM_RUNBOOK.md`.

**Re-audited 2026-08-28 against the live machine.** Sections 1 and 3 below were
written before `knox_condemnations.py`, `knox_evictions.py` and `knox_rod.py`
existed and are corrected in place. Two decisions since: **evictions and trustee
deeds were retired on 2026-08-25 (Ty)**, so their rows are answers now, not gaps.

This page is a point-in-time audit and it went stale in eleven days. The live
answer is now `python src/ftm_health.py`, which reports the same inventory every
morning from the machine's own run history. Read that first; read this for the why.

---

## 1. Automated today

Runs unattended on Fly (`siftstack-ftm`), daily 06:30 America/New_York.

| Step | Where | State |
|---|---|---|
| TN Public Notice scrape, foreclosure + probate, Knox + Blount | `ftm_runner.py` stage `notices` | live, verified 2026-08-17 |
| Probate property lookup (KGIS Knox, TPAD Blount) | `property_lookup.py` | live |
| Enrichment pipeline (Smarty, filters, validation) | `enrichment_pipeline.py` | live |
| DataSift upload over the API | `datasift_api_upload.py` | live |
| Slack summary, dead-run alerting, run ledger | `ftm_runner.py` | live |
| Condemnation agenda capture (BBB + Public Officer) | `ftm_runner.py` stage `capture` -> `knox_condemnations.py` | live since 2026-08-19 |
| Condemnations into DataSift | `ftm_runner.py` stage `county` -> `knox_ftm_pull.py` | live; the SiftMap import now falls back to `siftmap_standalone` |
| Daily coverage digest + all-clear | `ftm_health.py`, fired by `ftm_schedule.py` | added 2026-08-28 |
| Outside-the-box watchdog | `.github/workflows/ftm-heartbeat.yml` | added 2026-08-28 |

Everything below is manual.

**Measured reality of the automated part, 9 scheduled days to 2026-08-28:** five
days OK, three EMPTY (08-23, 08-26, 08-27), one egress-blocked (08-21). The
county stage reported "10 records into DataSift" on all nine, and it was the same
10 addresses every time. That pair of facts is why the health digest counts new
records against a ledger and reports per feed rather than in aggregate.

---

## 2. Built, but not automated

### 2a. The foreclosure post-processing chain

`_api/ftm_pipeline.py` chains six steps (single-family filter, wizard upsert,
Trestle phone scoring, per-phone dial tiers, optional Tracerfy re-skip, cadence
gate). Documented in `_api/FTM-PIPELINE-CRON.md`, built 2026-06-22.

Why it is not on a cron, per its own runbook:

- **The reisift admin JWT lives ~48h and cannot self-refresh.** A daily cron
  hits an expired token every other morning and aborts at pre-flight.
- Three steps drive the DataSift **browser wizard** on saved cookies.
- Step 1 depends on **Apify** run artifacts.

**The token problem is already solved elsewhere.** `datasift_api_upload.Api`
mints a fresh JWT from `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` and re-mints every
30 minutes, which is why the notice pipeline runs unattended. Porting the chain's
steps onto that auth removes the "hands-off has an asterisk" caveat entirely.

### 2b. The Knox county aggregator

`knox_ftm_pull.py` is **not a scraper**. Every collector reads a pre-existing
JSON from a scratch directory:

| Collector | Reads | Status |
|---|---|---|
| `load_condemnations` | `condemnations.json` | **hand-keyed from two agenda PDFs** |
| `load_rod` | `rod_resolved.json` | no producer in this repo |
| `load_landlords` | `landlord_parcels.json`, `evictions.json` | no producer in this repo |
| `load_liens` | `knox_lien_parcels.json` | produced by `knox_lien_resolve.py`, which itself reads lien data from scratch |
| `load_tnpn` | `tnpn_public_sales_orders.json` | no producer in this repo |
| `load_probate` | `probate_pe.json` | correctly always empty (see below) |

None of those scratch files exist in the repo, which is why the last run
(2026-08-07) produced a 1.1 KB CSV. The module is an aggregator, buy-box filter
and CSV writer. The collection layer was never committed.

It also needs the SiftMap client from the Deal Room checkout, which is why the
`county` stage skips in the container. That is the LAST blocker, not the first.

---

## 3. Fetchers: built since this page was written, and the one still missing

Three of the four rows below were built between 2026-08-17 and 2026-08-25. Only
the last has no fetch code anywhere.

| Source | Fetcher | Time sensitivity | State |
|---|---|---|---|
| Knox Register of Deeds (liens, state tax, federal tax) | **`knox_rod.py`** exists. `paxsub /api/v2Search`, per the `reference_knox_rod_api` memory: replay the page's own request, bare int doc ids, 5000-result cap needs monthly chunking | 12-month archive, recoverable | built, **run by hand** |
| Trustee deeds (TRS) | same fetcher | 12 months | **RETIRED 2026-08-25**, duplicates the foreclosure scrape |
| Eviction docket | **`knox_evictions.py`** exists | **ONE WEEK ONLY.** The court keeps only the current week; ~86 back-dated URLs all 404 | **RETIRED 2026-08-25.** Module kept; re-wiring is restoring one block in `_stage_capture` |
| Condemnation agendas (BBB + Public Officer) | **`knox_condemnations.py`** exists, prose PDFs, `_condemnation_money()` parses the dollar figures | **ONE CYCLE ONLY.** The city overwrites its agenda PDFs | **automated daily** in the `capture` stage |
| tnpublicnotice public sales + orders | none. `knox_ftm_pull.load_tnpn` consumes a JSON that nothing writes | 12-month archive | **still the real gap** |

**Knox TN probate is correctly absent.** Knox County TN probate runs through
Chancery Court under the Clerk and Master, which publishes no online case index.
The only online route is the notice to creditors on tnpublicnotice, which the
automated scrape already collects. Do not "fix" `load_probate`.

---

## 4. The urgent part

**Resolved 2026-08-28.** Condemnations **accumulate forward or not at all**: the
city overwrites its agenda PDFs, so every cycle nobody pulls is gone for good.
They are now captured daily by the `capture` stage, which is why that stage is in
`DEFAULT_STAGES` despite uploading nothing. Evictions had the same property and
were retired instead, deliberately: the docket keeps one week, the list existed
only as our own accumulation, and each landlord still needed a name-to-parcel join
to become a lead.

The residual risk is no longer that nobody runs the capture. It is that the
capture runs and quietly parses nothing, which is why `_stage_capture` fails on
any problem rather than only on a total miss, and why the daily digest reports the
cumulative count.

---

## 5. Missing glue

| Gap | Why it matters |
|---|---|
| **Skip trace is not automated** | The FTM presets require `phone: 1` and `skiptraced: 1`. Records enter no cadence without it, and probate notices carry no phone numbers at all |
| **Enrichment cannot be scoped** | The only confirmed payload for `POST /api/internal/property/enrich/` is an empty body, which enriches all 50,769 records. Unsafe to put on a schedule until a filter shape is found |
| **Enrichment overwrites the PR** | It takes the owner of title, which on a probate record is the decedent. Any automated enrich needs a PR re-assert chained after it |
| **16 PR contacts cannot hold** | Probate notices list the attorney's office as the mailing address; DataSift dedupes owner entities by mailing address, so PRs sharing one collapse. Deep prospecting (SmartSkip) supplies the PR's own address and fixes this properly |

---

## 6. Suggested order

Items 1, 3 and 4 are done. What is left, in order:

1. ~~Eviction + condemnation fetchers~~ **done.** Condemnations automated,
   evictions built then retired by choice.
2. **Skip trace automation.** Unblocks every preset; free on the plan. Now the
   top of the list: records land daily and enter no cadence without it.
3. ~~Standalone SiftMap client~~ **done.** `siftmap_standalone.py`; the `county`
   stage runs in the container.
4. ~~Knox ROD fetcher~~ **built** (`knox_rod.py`), but still run by hand. Wiring
   it into a stage is the next automation win, and the archive is recoverable so
   it is not urgent.
5. **A producer for tnpn public sales + orders.** The only source whose consumer
   is live with nothing feeding it.
6. **Port the `_api` foreclosure chain onto minted-JWT auth**, retiring the 48h
   token dependency and the browser wizard steps.
7. **Enrichment scoping**, then chain enrich plus PR re-assert into the daily run.

Two things to verify before building 1 and 4, both learned the hard way on the
notice scrape: whether those county sites serve a **datacenter IP** (tnpublicnotice
does not, which is what `proxy_resolver.py` exists for), and whether the
condemnation PDFs need OCR, since Tesseract and OpenCV were deliberately left out
of the Fly image.
