# CLAUDE.md: SiftStack developer guide

This file guides Claude Code and developers working on SiftStack. It covers what the system is, how the repo is laid out, how to run, test and deploy each part, and the API contracts and traps that cost real time to find. The skill library's install guide is `README.md`; vendor contracts in depth are in `docs/api/`.

## What SiftStack is

A real estate investing operations platform built around the DataSift.ai CRM (formerly REISift; app at `app.reisift.io`, API at `apiv2.reisift.io`). It covers the lead lifecycle end to end:

- **Acquisition:** county public-notice scraping (foreclosure, probate, tax sale, tax delinquent), scanned PDF and courthouse-photo import, county recorder and condemnation sources.
- **Enrichment:** address standardization, property data, county tax, obituary and heir research, skip trace, phone scoring.
- **CRM automation:** API upload into DataSift, presets and sequences, an account blueprint cloner, a sold-property guard.
- **Deal analysis:** comps, rehab, offer sheet, post-walkthrough package, dispo buyers, lender package.
- **The agents brain:** four Claude "employees" in Slack plus an autopilot that runs the prospecting lifecycle stage by stage.
- **Outreach:** a two-way SMS agent, Smarter Contact campaigns, cold email, direct mail.
- **The skill library:** Claude skills shipped as a Claude Code plugin marketplace.

Python 3.12, flat modules under `src/` (the import root, not a package), three Fly.io apps with SQLite on volumes, GitHub Actions for CI and outside watchdogs.

## Ground rules

1. **The repo is public.** Never commit keys, tokens, session or cookie files, county records naming people, recordings, vendor JS bundles or seller data. `output/`, `logs/`, `.env`, `*_state.json`, `cookies.json`, `config/sms_*.json` are gitignored for this reason. Read `git status` before every commit.
2. **Dry by default.** Anything that writes to DataSift, pays a vendor or contacts a person is a plan unless given `--commit`; account-wide tools also need an env switch (`*_ALLOW=1`). New code keeps that contract.
3. **One record first.** Before a batch write, write one record, read it back, compare. Most DataSift and vendor failures return a success status.
4. **A filter is proven by a count delta.** DataSift and SiftMap silently ignore unknown filter keys. Count with and without the key before trusting it; acceptance proves nothing.
5. **Zero is a failure.** A run with zero notices, zero records or zero matches exits non-zero or says so. Never infer success from the absence of an error or of challenge markup.
6. **Persist "handled" only after the downstream write succeeded** (seen ids, ledgers, done-files), so an aborted run retries instead of skipping work forever.
7. **No silent degradation.** A bare `except` that turns a 401 or 429 into "no data" is a bug. Count errors, retry transient ones, fail the phase past a threshold.
8. **Never fabricate.** A research claim carries a URL it actually opened and a verbatim quote; a value with no source is Unknown. A wrong name or number reaches a real person.
9. **No em dash or en dash characters** anywhere: code, docs, copy, commit messages. CI rejects them in `docs/**/*.md` and `README.md`. Number ranges use a plain hyphen.
10. **Read large files by range.** Several modules exceed 50 KB (`datasift_uploader.py`, `post_walkthrough.py`, `land_market.py`, `agents/autopilot/kpitracker.py`, `agents/autopilot/stages.py`, `sms_agent/selftest.py`). Grep for the symbol, then read the lines around it.
11. **A stage verified only on a workstation is not verified.** After a deploy, run each changed stage once on the box: datacenter egress, missing files and missing secrets only show up there.

## Repository layout

| Path | What lives there |
|---|---|
| `src/` | Every Python module. Run as `python src/<module>.py` or set `PYTHONPATH=src`. |
| `src/agents/` | The agents brain: FastAPI app (`brain.py`), worker, scheduler, team runtime, tools, Slack, relay, code sandbox, script catalog |
| `src/agents/autopilot/` | The autopilot engine: one module per stage kind, plus `gate.py`, `lanes.py`, `budget.py`, `ledger.py`, `crm.py`, `policy.py`, `registry.py` |
| `src/agents/workflows/` | Workflows the team runs on request, one module each, registered in `__init__.py` |
| `src/agents/research/` | Deep-prospect research engines (`researcher.py`, `open_research.py`) and the write-back rules |
| `src/agents/kpi_sources/` | KPI source adapters (DataSift log, spend ledger, file import, webhooks) |
| `src/sms_agent/` | Two-way SMS agent: receiver, worker, engine, classifier, responder, seeding, transports |
| `src/cold_email/`, `src/call_coaching/`, `src/account_blueprint/` | Instantly cold email; call recording pull, transcription and grading exports; account export/apply |
| `team/` | `roster.yaml` (the team), `autopilot.yaml` (the playbook), `kpi.yaml` (KPI catalog), `commands.json` (script catalog), generated `TEAM.md` and `COVERAGE.md`, one brief per role in `<role>/CLAUDE.md` |
| `deploy/` | Dockerfiles, per-app requirements, secret sync scripts, `agents_up.py`, `agents_heartbeat.py`, `FTM_RUNBOOK.md` |
| `fly.toml`, `fly.agents.toml`, `fly.ftm.toml` | The three Fly apps: texting agent, agents brain, first-to-market runner |
| `skills/`, `plugins/` | Skill and plugin SOURCE trees, plus `skills/manifest.json` |
| `dist/` | Built `.skill` / `.plugin` archives (build products, never edit) |
| `marketplace/bundles/`, `.claude-plugin/marketplace.json` | Generated plugin marketplace |
| `tests/` | pytest files; most also run standalone (`python tests/<file>.py`) |
| `tools/` | `build_skills.py`, `check_docs.py`, `datasift_api_audit.py`, `agent_map.py`, `build_agent_page.py`, `sync_account_blueprint_skill.py` |
| `docs/` | `agents.json` (org chart source), generated `AGENT-MAP.md` and `agent-system.html`, `api/` vendor contracts, `setup/` guides, runbooks (`smarter-contact-setup.md`, `sms-deliverability-routes.md`, `walkthrough-to-offer.md`, `direct-mail-swipe-file.md`) |
| `sops/` | Agent-executable SOPs (`.sop.md`), served as slash prompts through `.mcp.json` |
| `data/` | Locked material price list, the deal-odds model (both copied into the agents image) |
| `.actor/`, `Dockerfile`, `src/__main__.py` | The Apify Actor (legacy cloud path for `main.py`) |
| `install.py` | Stdlib skill installer and readiness doctor |

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows; .venv/bin/python elsewhere
playwright install chromium                                       # scraping and browser automation only
cp .env.example .env                                              # fill in only what you use
```

- `requirements.txt` is the workstation set. Each Fly app has its own narrower set: `deploy/requirements-agents.txt`, `deploy/requirements-ftm.txt`, `deploy/requirements-sms-agent.txt`.
- The agents brain also needs `deploy/requirements-agents.txt` (pyyaml, cryptography, the pinned `anthropic` and `claude-agent-sdk`). Run its selftest from the repo venv, never a bare system Python.
- Config loads `.env` from the repo root, and the process environment always wins over the file (`datasift_api_upload.env()`, `agents/config.py`). On Fly there is no `.env`: every key is a secret.
- State paths honor `SIFTSTACK_STATE_DIR`, `SIFTSTACK_OUTPUT_DIR`, `SIFTSTACK_LOG_DIR` (scraper and runner) and `AGENTS_DATA_DIR` (brain; default `output/agents/`).
- `.env.skills.example` documents every variable the skill library reads and what each costs.

## Commands that matter

```bash
# Notice pipeline (workstation)
python src/main.py daily [--counties Knox] [--types foreclosure,probate] [--split] [-v]
python src/main.py daily --upload-datasift [--no-enrich] [--no-skip-trace] [--notify-slack] [--deep-heirs]
python src/main.py list-searches                          # live saved-search labels; flags missing ones
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type probate
python src/main.py dropbox-watch [--poll-interval 300] [--max-polls 5] [--no-delete]
python src/main.py manage-presets --discover | --add-sold-exclusion | --create-sold-sequence

# First-to-market runner
python src/ftm_runner.py --doctor                         # credentials, egress, state dir, searches
python src/ftm_runner.py --max-notices 2                  # bounded dry run, writes nothing
python src/ftm_runner.py --stages notices,capture --commit
python src/ftm_schedule.py --next                         # next fire times, business-local
python src/ftm_health.py [--full | --check | --selftest]

# DataSift API upload
python src/datasift_schema_setup.py [--commit]            # custom fields, options, lists (idempotent)
python src/datasift_api_upload.py --limit 1 [--commit]    # always one record first

# Deal analysis
python src/comp_package.py --address "<street>" --zip <zip> --bbox "lat_min,lat_max,lon_min,lon_max" --streets "<regex>"
python src/post_walkthrough.py --walkthrough-template
python src/post_walkthrough.py --address "<street>" --zip <zip> --walkthrough walk.json --save-pack pack.json
python src/offer_sheet.py --pack pack.json --walk walk.json --out Offer.xlsx
python src/lender_package.py --spec deals/EXAMPLE_lender_spec.json
python src/lender_docs.py --spec deals/EXAMPLE_lender_spec.json

# Agents brain
python src/agents/cli.py selftest | team | hello | status | schedule
python src/agents/cli.py ask "Roma, quick screen 123 Example St"
python src/agents/cli.py autopilot check [--live] | plan [stage ...] | run <stage> [--plan] | last [stage]
python src/agents/cli.py autopilot batches | release <id> [--commit] [--uploaded] | runs | undo <run id> [--commit]
python src/agents/cli.py autopilot lanes | budget | log | calibrate [days]
python deploy/agents_up.py [--commit]                     # stand up the brain on Fly

# Texting agent
python src/sms_agent/cli.py doctor | selftest | switches | status | digest

# Skill library and audits
python tools/build_skills.py --marketplace --build --manifest
python tools/build_skills.py --verify
python install.py --doctor
python tools/datasift_api_audit.py [--refresh] [--check]
python tools/agent_map.py [--check | --audit]
python tools/check_docs.py
```

## Architecture: five deployables

| Deployable | Entry point | Runs on | State |
|---|---|---|---|
| Notice pipeline CLI | `src/main.py` | a workstation (or the Apify Actor) | `output/`, `logs/`, `last_run.json`, `seen_ids.json` |
| First-to-market runner | `src/ftm_schedule.py` -> `src/ftm_runner.py` | Fly app from `fly.ftm.toml` (default `siftstack-ftm`), Playwright image | volume `/data` |
| Texting agent | `uvicorn sms_agent.receiver:app` | Fly app from `fly.toml` (default `siftstack`) | volume `/data` (`sms_agent.db`) |
| Agents brain | `uvicorn agents.brain:app` | Fly app from `fly.agents.toml` (default `siftstack-agents`) | volume `/data` (`agents.db`, workspaces, Claude sessions) |
| Skill library | `skills/`, `plugins/` | Claude Code and Cowork installs | none |

Why three apps: a 10-40 minute browser batch must never sit in front of a webhook, each app keeps its own volume, and each runs ONE machine because SQLite is single-writer and Fly attaches a volume to one machine. Web services run their worker as a thread inside the web process.

All three apps share ONE DataSift request budget per account (120 a minute, 5,000 an hour). Each counts its own share in SQLite: the brain 45 a minute and 2,800 an hour by day (more from 20:00 to 06:00); the first-to-market box 20 and the texting app 10 once their switches are on (`FTM_RATE_BUDGET`, `SMS_AGENT_RATE_BUDGET`, both off by default); the rest stays in reserve. The brain drops to a trickle during the first-to-market run's window (`DATASIFT_RATE_QUIET`, default `06:25-07:15`, `off` for an install with no such run).

## The notice pipeline

```
web scrape:  main.py -> scraper.py -> captcha_solver.py -> notice_parser.py + foreclosure_filter.py
PDF import:  pdf_importer.py (pypdfium2 -> image_utils OCR)
photos:      photo_importer.py (OpenCV -> OCR -> llm_parser.py);  dropbox_watcher.py feeds it
             -> enrichment_pipeline.py -> datasift_formatter.py / data_formatter.py -> CSV -> DataSift
```

- **The notice site is ASP.NET WebForms** (`__doPostBack`, ViewState, session id in the URL path), so Playwright drives it. Detail pages sit behind Cloudflare Turnstile; `CAPTCHA_KIND` picks the solver method and response field, the sitekey is read off the live page (a rotation logs `SITEKEY ROTATED`), and the gate is session-level, so one solve covers the run.
- **Clearing the gate means positively seeing the notice body.** Never infer a pass from missing challenge markup.
- **Saved searches** live in `config.SAVED_SEARCHES` and must match the dropdown labels exactly; a mistyped one scrapes nothing and looks like a quiet day. `main.py list-searches` checks them.
- **Notice types:** foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce.
- **Enrichment steps** (`enrichment_pipeline.py`): filter sold, dedupe, vacant-land filter, entity research and filter, probate property lookup, parcel lookup, tax delinquency, Smarty, city and ZIP fallback (`address_fallback.py`), commercial filter, reverse geocode, Zillow, obituary, validation, mailable flag. Every API is optional; a missing key skips its step and the run says so.
- **Dates:** `date_added` is the day the pipeline added the record; `date_published` is the notice's publication date. Anything that needs the filing date uses `date_published`, falling back to `date_added`.

**Parsing rules, each from a real failure (all regression-tested):**

- Foreclosure results include non-foreclosures: keep only trustee-sale language (`INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`). A probate search's keyword also matches trustee sales; `looks_like_trustee_sale()` drops those unless a genuine probate anchor is present.
- The foreclosure owner follows "executed by"; stop at "payable to", "in favor of", "to secure", or the lender rides along in the owner name.
- The probate `owner_name` is the personal representative, never the deceased and never court prose. Try block-form patterns first and let a rejected candidate fall through.
- A probate notice carries no property address: its only streets belong to the court, the attorney or the representative. `_parse_address` returns nothing for probate, `property_lookup` resolves the house, and every assessor result must pass `_owner_matches_decedent` (surname AND first name; maiden name and AKA count). A miss is an honest "no property found".
- A phone number inside a probate notice is the law firm's. Representative phones come from skip trace.
- PDF text in narrow columns breaks words with hyphens across lines: `_join_line_wraps` repairs them.
- The vacant-land filter judges by house number, so types with no address yet are exempt (`NO_ADDRESS_TYPES = {"probate", "divorce"}`).
- Keep a spelled-out middle name out of the surname; keep particles whole (Van, De La, St.).
- An obituary match whose date of death is more than `MAX_DOD_GAP_YEARS` (3) before `date_published` is a different person.

**Courthouse photo OCR:** terminal screens produce moire that defeats adaptive threshold and CLAHE. Use `cv2.bilateralFilter(gray, 15, 75, 75)`, then Otsu, then Tesseract PSM 4. Use EXIF transpose for rotation, never Tesseract OSD on phone photos. Dropbox folders resolve county and type from the path (`<root>/<County>/<notice_type>/photo.jpg`).

**Adapting to another market:** edit `SAVED_SEARCHES` in `src/config.py`, add the county tax source in `src/tax_enricher.py`, and add assessor lookups in `src/property_lookup.py`. The county-specific sources shipped (recorder liens, condemnation agendas and tax sale results in the `knox_*` modules; assessor and trustee lookups in `property_lookup.py` and `tax_enricher.py`) are for Knox and Blount counties, Tennessee.

## Uploading to DataSift

**The API path** (`src/datasift_api_upload.py`, used by the runner and everything unattended):

- `create_property` posts to `/api/internal/property/`. The legacy Open API create route `POST /property/` answers 403; the uploader falls back to it only on a 403 or 404.
- The internal create does NOT upsert: a duplicate address returns 400 with the existing uuid, and `attach_to_record` adds the payload to that record (lists one title per call, tags as a read-modify-write, owner PATCHed only when it differs and `owner_guard` allows it).
- `owner_guard` keeps a researched or deceased owner from being overwritten by a re-sent notice. `UPLOAD_KEEP_OWNER_OF_RECORD=1` (in `datasift_formatter.py`) keeps the owner of record at first upload.
- `find_property` looks a record up by a POST-as-GET `address_prefix:` search, matched on normalized street and ZIP5. DataSift standardizes the street on ingest, so matching is case- and punctuation-insensitive.
- Auth: with no `DATASIFT_EMAIL`/`DATASIFT_PASSWORD` the client uses `REISIFT_API_KEY` (the Open API key), which reaches every `/api/internal/` route the uploader writes. A login, where present, mints a JWT re-minted every 30 minutes. Hosted installs never take a password.
- Custom fields go through `FIELD_MAP` / `field_map()`; dates are converted with `iso_date`; `write_fields` retries a refused batch field by field so one bad value never costs the rest.

**The browser path** (`src/datasift_uploader.py`, `main.py --upload-datasift`) drives the app's upload wizard, enrich and skip trace with Playwright. Workstation only, needs a login, and fails by hanging rather than erroring. Prefer the API. UI automation patterns are under "DataSift app automation" below.

`datasift_formatter.py` writes the CSV: core address and owner columns, Tags (always including `Courthouse Data`), Lists from the notice type, custom fields. For a deceased owner the contact is the decision maker.

## Deal analysis

| Module | What it produces |
|---|---|
| `zillow_market_api.py` | OpenWeb Ninja `/search` client with price-band partitioning |
| `comp_analyzer.py` | Comp adjustments: $85/sqft, $5,000/bed, $7,500/bath, $500/yr of age, lot $2/sqft capped at $15,000, $8,000/garage stall |
| `comp_package.py` | Boundary-filtered comps, condition buckets, dual-track ARV, rehab scenarios, buyers, one workbook |
| `post_walkthrough.py` | The post-walkthrough package anchored on the live CRM record and a walkthrough JSON |
| `offer_sheet.py` | The one-page wholesale offer from a saved pack and a walk (the default deliverable) |
| `rehab_estimator.py`, `sku_pricing.py`, `material_list.py` | Four-tier rehab; SKU-priced material baskets from the locked list in `data/` |
| `deal_analyzer.py` | MAO, ROI and financing scenarios (defaults: hard money 12%, 2 points; conventional 7%; closing 2.5%) |
| `lender_package.py`, `lender_docs.py` | The private lender workbook and letters from one spec (`deals/EXAMPLE_lender_spec.json`) |
| `buyer_sweep.py`, `dispo_skiptrace.py`, `deal_package.py`, `deal_buyer_match.py`, `deal_buyer_dp.py`, `dispo_buyers.py` | Deed-verified buyers, three-source contact resolution, buyer workbooks, the dispo buyer registry |
| `mls_offers.py`, `mls_offers_api.py`, `mortgage_model.py` | A cash and a creative offer on every active listing in a market, on the install's own Open API key ([docs/mls-offers.md](docs/mls-offers.md)); the skill `mls-offer-engine` carries copies (`tools/sync_mls_offers_skill.py`) |
| `curative_title.py`, `curative_engine/` | Curative title end to end on the install's own key: rank every house tied to a death, SmartSkip family trees, the heir research (each house in its own Claude Code session, `heir_method.md`), Trestle phone checks, the workbook; `agents/autopilot/curative_setup.py` then loads the work-now houses onto the Curative Title board ([docs/curative-title.md](docs/curative-title.md)) |

Rules the engines enforce:

- **Boundary = bbox AND street regex.** The box catches street misses; the regex catches bleed across a highway. Test two or three radii and read which streets enter before accepting an ARV.
- **Condition buckets** by sold price over Zestimate (0.90 and up renovated, 0.70 and under distressed). Zillow re-anchors a Zestimate to a recent sale, so an investor buy can read 1.00: `refine_bucket` demotes deed-verified cash or entity buys priced well under the retail $/sf.
- **Same-bed ARV.** A subject with fewer bedrooms than its comps lives in a lower band: the base ARV uses same-bed renovated comps clamped to that band's median; a reconfiguration is a labeled upside credited only after a walkthrough. `tight_arv` prefers 12 months, sizes 0.70-1.35x the subject, and clamps to the same-bed median.
- **As-is band guards:** drop non-arm's-length deeds (`NON_ARMS_LENGTH_RATIO = 0.30` of value), keep a size band, cap at the retail floor, prefer deed-verified investor buys.
- **Precedence of subject facts:** CLI (county card) > CRM record > Zillow. Aggregators get bedroom counts wrong.
- **Pass a `--months` pool wider than the preference**, and reuse a saved pull with `--sold-json` (`--months` is enforced on cached pulls too).
- **Locked material prices are already local.** In SKU mode the regional multiplier applies to LABOR only; multiplying locked materials double-discounts. Only `material_list.py --lock` writes `data/`.
- **Never a blank cost.** A pending quote renders as a red placeholder with a realistic figure.
- **A saved pack is a snapshot.** Recompute rehab from the walk (`build_rehab_matrix`) and let explicit CLI values (`--arv`, `--as-is`) win; `offer_sheet._hydrate` revives serialized comps.
- **Workbooks are live formulas off defined names.** Verify by recalculating with the `formulas` package (assert the answer and zero formula errors), set column widths after `_polish`, and write to a `_PENDING_` name then swap, because Excel holds an exclusive lock on an open file.
- **Degrade, never fail:** no CRM, no key, no buyer sweep each render a stated reason in place of the section.
- **Ship the producer with the consumer.** Shipped code never reads a gitignored research folder (`tests/test_shipped_inputs.py`): a workflow ships with every step that makes its input. The curative board's loader once shipped without the ranking and heir research that feed it.
- **Curative title: an obituary is not the owner's death.** The obituary found can be the co-owner's (the spouse trap), and a married couple's house passes to the survivor, so it is stuck only after the second death. A death counts when the age at death fits the owner's age; a name alone never does. Heirs are tied by a record that names the parent: a stepchild inherits nothing.
- **MLS offers read details only for records in the account.** SiftMap search runs in volume (the screen, investor sales by price band, rent by band); `GET /properties/detail/` runs only on listings already in the account, because acceptable use allows volume details on nothing else. Adding the rest is quoted first (`/properties/add-properties-by-query/quote/`) and happens only with `--add --commit`. Paced at 80 calls a minute.
- **A modeled loan says where its rate came from.** "HMDA match" is the actual rate (one HMDA loan on year, tract, type, $10K band and term); otherwise Freddie Mac in the lock week plus the spread the market's own HMDA shows ("local spread"), or "Freddie Mac week" alone when no HMDA was loaded. Credit lines and construction loans stay out of the payment and the loan-to-list ratio. The HMDA host refuses Python's HTTP client, so `curl` fetches it.
- **After an engine edit, run `python tools/sync_mls_offers_skill.py`**, then `python tools/build_skills.py --marketplace --build --manifest`; `tests/test_mls_offers_skill_sync.py` fails on any drift.

## Research: deep prospecting

The heir engine: SmartSkip bulk skip (relatives and their phones in one call) -> obituary and web research for the date of death and true relationships (mandatory) -> Tracerfy only for relatives left without a phone -> Trestle tiers. Enformion BusinessV2 resolves the person behind an entity.

- `src/deep_prospect_record.py --address "<street>" --phase records|smartskip|merge|heirs|gapfill|score|verify|build|writeback [--commit]` runs the chain on one CRM record; research and analysis JSON are written by a researcher, never invented by the module.
- `src/deep_prospect_batch.py cohort | estimate | status | run --list <list> [--commit] [--max-usd N]` runs it over a backlog, one process per record, gating each record on its full CRM facts (a sale date already passed, a sale after the filing, an MLS listing) right before it runs.
- The brain's research engine: Claude with web search by default; `AGENTS_RESEARCH_ENGINE=open` runs two low-cost models through OpenRouter, compares their fact sheets, and sends hard disagreements to a referee model (`src/agents/research/open_research.py`).

Research rules:

- **An obituary on a record does not mean the owner died.** It is often the spouse's. Match the decedent against the owner of record before treating a record as an heir case.
- **Vendor deceased flags are wrong in both directions** (SmartSkip, Enformion, Tracerfy). Death comes from an opened obituary or an index row matching name and age.
- **"Unknown" status names a specific candidate death that could not be ruled out.** Living does not require proof of life; never list a person whose status is unverified as a required signer.
- **The owner rule on shared lines:** a number the owner also holds carries no relationship tag.
- **A mailing address on the same parcel is a second home, not an absentee owner.**
- **AVMs overstate distressed houses;** read the county appraisal. Name-based tax joins mix parcels.
- **LLMs can invent a whole heir map** from an obituary. Ground every survivor in the raw text.

## The first-to-market runner

`src/ftm_runner.py` is one unattended run: scrape, enrich, CSV, upload, report. `src/ftm_schedule.py` is the container's long-lived process that fires it at business-local times and posts the daily health digest. Runbook: `deploy/FTM_RUNBOOK.md`.

- **Stages** (`--stages`, order matters): `notices`, `tax_sale`, `tax_delinquent`, `public_notices`, `capture`, `liens`, `appointments`, `tax_sale_report`, `code_violations`, `county`, `deeds`, `sold_reentry`, `sold_audit`, `sold`. A stage that only prepares rows goes before `county`, which uploads them and only then marks them handled. Default without `--stages`: `notices,capture`.
- **`FTM_ARGS` is the safety switch** in `fly.ftm.toml`. A fresh app ships without `--commit`; add it after one scheduled run has been read.
- **Exit codes:** 0 ok; 1 a stage failed; 2 misconfiguration; 3 blocked by egress (no retry fixes it).
- **Egress decides everything.** The notice site serves or refuses notice pages per IP: an office IP can get a flat refusal, a datacenter proxy gets the solvable gate, a residential proxy may get no gate. `proxy_resolver.py` resolves `SIFTSTACK_PROXY_URL`, then `APIFY_PROXY_GROUPS` + `APIFY_TOKEN` (the token looks the proxy password up; it is not the password), then direct. `python src/proxy_resolver.py` prints the live egress IP.
- **The site blocks an IP by volume** (a couple of hundred notice views). The runner rotates to a fresh proxy session per process; a backfill runs one process per (saved search x month) (`--backfill-months 1 --backfill-offset M`). Do not retry exit-3s in a tight loop.
- **Every result set is capped at 20 pages / 1,000 rows,** newest first, so long windows silently lose their tail. Slice by month. The seen-id check reads the id off the results grid before opening a page.
- **`seen_ids` is written only after a successful upload** and rolled back on failure, `--no-upload` and dry runs. `last_run.json` advances only on success, so the first run after a long gap pulls the whole gap: close gaps with sliced backfill jobs, then move `last_run.json` forward.
- **Subscriptions lapse silently.** `scraper.read_subscription()` reads the site's subscription block on every run, and an expired account fails the stage as `SubscriptionExpired` instead of reading as an empty day; a dead proxy IP raises `LoginFailed` and the runner rotates proxy sessions before failing as `LOGIN UNREACHABLE`. A quiet day whose few notices were all filtered out is OK; many scraped and none surviving is a failure (`FTM_ALL_DROPPED_SUSPECT`).
- **A paid public-record login can answer a lapsed subscription with a browser `alert()`,** which headless Playwright dismisses silently. Listen for dialogs and treat them as answers.
- **Overwritten sources are captured daily** (the `capture` stage), because an agenda replaced at the source cannot be recovered later. Staleness checks fail a source whose newest date is too old.
- **Wait on `domcontentloaded`, not `networkidle`,** which often never settles through a proxy.
- **Health has two layers.** `ftm_health.py` posts a two-line digest every morning: per feed, new records against a persisted ledger (uploaded is not new), days since a feed last produced (a zero day is not a dead feed), and requested stages that were skipped (skipped is not healthy). Outside the box, `.github/workflows/ftm-heartbeat.yml` SSHes in and reads `--check`; it parses the printed verdict rather than trusting flyctl to pass a remote exit code.

## The agents brain

A FastAPI app (`src/agents/brain.py`) with an inline worker thread (`worker.py`), a clock thread (`scheduler.py`) and one SQLite queue on its volume. DataSift sequence webhooks, Slack and schedules enqueue tasks; the worker runs them and posts results in Slack threads under the owning employee's name. Modes: `AGENTS_MODE=dry` (default) pays no vendor and writes nothing to DataSift; `live` spends inside `AGENTS_RUN_CAP_USD` and `AGENTS_DAILY_CAP_USD` and writes back, skipping (not failing) any paid phase that would cross a cap. The reference workflow: a DataSift sequence on the status change to `AGENTS_DP_STATUS` creates a card in the board column `AGENTS_DP_COLUMN` and sends the webhook, and the research employee runs the deep prospect; one record is not re-run inside `AGENTS_DEDUPE_HOURS`. `python src/agents/cli.py run --uuid <uuid>` (or `--address`) runs it in-process.

### The four employees

| Role id | Default persona | Owns |
|---|---|---|
| `analysis` | Atlas | data and market: notice pulls, county pulls, list pulls, idle records, account structure |
| `research` | Diggs | people: owners, heirs, entity principals, skip trace, phone scoring, probate property |
| `sales` | Roma | numbers and deals: quick screens, underwrites, offer sheets, buyers, lender packages |
| `ops` | Quinn | the machine: digest, QA before outreach, KPIs, call coaching, SOPs, outreach health |

- `team/roster.yaml` is the single source of truth: employees (channel, model, skills, built-in tools), and the workflow catalog (owner, `status: live|planned`, spend class, triggers, inputs, `covers`). Ids are roles and never change; display names are personas and can be renamed per install.
- `python src/agents/cli.py team` validates the roster, regenerates `team/TEAM.md` and `team/COVERAGE.md`, and fails when a live agent on the org chart (`docs/agents.json`) has no workflow. The selftest fails when either file is stale.
- Spend classes: `free` runs without asking; `paid` runs inside caps; `records` spends the account's record allowance and needs the operator's yes; `outreach` is only staged, a human releases it.
- Triggers: `slack`, `datasift:<slug>`, `schedule:<grammar>`, `chain:<workflow>`. Only `live` workflows are fired by the clock; a planned workflow answers with how it runs by hand.
- Schedule grammar (`scheduler.py`): `07:15`, `07:15 Mon-Fri`, `Mon 07:00 America/New_York`, `1st 07:00`, `every 30 min 08:00-18:00 Mon-Fri`, `every 15 min`. Fired slots persist; a missed slot older than the grace window is logged as skipped, never run late.

### How an employee turn runs

`runner.py` runs each turn on the Claude Agent SDK in a workspace OUTSIDE the repo (a temp dir locally, `/data/workspaces` on Fly), so the repo's CLAUDE.md never loads into an employee. The brief goes into the system prompt and is written to the workspace as `EMPLOYEE.md`. `CLAUDE_CONFIG_DIR` on the volume keeps sessions, so an ask-back resumes after a redeploy.

The SDK's `allowed_tools` only pre-approves; it does not restrict. Four layers lock a turn down: `tools=[Read, Glob, Grep, Skill]` (plus WebSearch and WebFetch where granted), `allowed_tools` for the employee's own MCP tools, `permission_mode="dontAsk"`, and a PreToolUse hook that re-checks the allowlist and keeps reads inside the workspace. `strict_mcp_config=True`. Every turn runs under a dollar budget and lands in the spend ledger.

Tools (`tools.py`, `query.py`, `commands.py`, `sandbox.py`):

- team tools: `handoff`, `ask_operator`, `thread_history`;
- shared: `record_lookup`, `spend_report`, the read-only query tools `records_query`, `siftmap_count`, `activity_query`, `team_query` (`AGENTS_QUERY_TOOLS=0` removes them), `script_catalog` and `run_script` (`AGENTS_SCRIPT_TOOLS=0`), `run_python` and `export_data` (`AGENTS_SANDBOX=0`);
- one tool per workflow the employee OWNS. A teammate's workflow is a handoff, which is how ownership is enforced. A long job is queued on the same thread, never run inside the turn.

**Scripts from Slack.** `team/commands.json` is a catalog of every `src/*.py` command line, generated by AST without importing anything (`python src/agents/commands.py --write`; `--check` fails when stale, and so does the selftest). A read or dry run runs on the box with vendor and outreach keys removed; a write flag, a paid vendor or SiftMap detail reads stage a `command` batch for a named releaser. Adding a package to `deploy/requirements-agents.txt` changes the catalog: rerun `--write`.

**The code sandbox** (`sandbox.py`) runs Python an employee writes as an unprivileged user in a fresh network namespace (`unshare --net`, `setpriv`), with a replaced environment, `no_new_privs`, resource limits, a wall clock and a write watch, and a bootstrap that re-checks every guarantee from inside. It fails closed off Linux or when any step does not take. After any deploy that changes the image, run `python -m agents.sandbox --check` on the box from `/app/src`. `export_data` writes the CSV inputs (records without phones or emails, the log copy, the brain's tables).

### Slack

- A real Slack app per install, created from a manifest (`cli.py slack-manifest`, or the one-click link `deploy/agents_up.py` prints). Scopes are in `team.BOT_SCOPES`; `chat:write.customize` lets each employee post under its own name and portrait (roster `icon`, a .png under `team/assets/`; the app's own posts use `app_icon`; `slack_app.icon_url` serves them from the brain and versions each URL by content, because Slack caches an icon by its URL, and falls back to `icon_emoji` when the brain has no public address; Slack's own app icon is uploaded by hand in the app's settings), `files:read` lets employees open dropped files, `channels:join` lets the app join public channels by itself.
- Two ways in, both deduped by event id: the Request URL `/slack/events` (needs `SLACK_SIGNING_SECRET`; 404s without it) and Socket Mode (`SLACK_APP_TOKEN`, `xapp-...` with `connections:write`).
- Channels by purpose: `AGENTS_SLACK_CHANNEL` (talk to the team), `AGENTS_SLACK_PIPELINE_CHANNEL` (digest, live results, leads to call now), `AGENTS_SLACK_ISSUES_CHANNEL` (anything broken, once a day per problem). An unset one falls back to the team channel.
- `AGENTS_PLAN_POSTS` (`digest` default, `daily`, `off`) keeps plan-mode stages quiet: a plan-mode stage posts only when its set of problems changes; what each would do goes in the morning digest.
- `voice.py` cleans every post (no emoji, no programmer text) and, unless `AGENTS_SLACK_VOICE=plain`, has a small model rewrite it. A rewrite ships only if every number is in the source, it adds no dash or emoji, and a second model pass answers PASS; otherwise the cleaned post goes out.
- Files dropped in a thread are saved into the working employee's workspace at the start of its turn (`attachments.py`); messages with files join one waiting turn (`AGENTS_FILE_SETTLE_S`), so ten photos posted in batches are one turn. Limits: `AGENTS_MAX_FILES`, `AGENTS_MAX_FILE_MB`, `AGENTS_FILE_TURN_USD`.
- The brain says once when it can post but cannot hear (no events after its own hello), with the setting that fixes it.

### HTTP routes

| Route | Purpose |
|---|---|
| `GET /health` | mode, worker and clock liveness, per-stage last run (counts only: never a street or name) |
| `POST /hooks/datasift/{secret}/{workflow}` | DataSift sequence webhook: Ed25519-verified, deduped on `event_id` |
| `POST /slack/events` | Slack Events API (signed) |
| `POST /hooks/slack/{secret}/{source}` | Slack relay: other jobs post here instead of to a channel webhook; routed by the message's first line; `?dry=1` shows where it would go |
| `POST /hooks/kpi/{secret}/{source}` | KPI source webhooks (`AGENTS_KPI_HOOK_SECRET`) |
| `GET /kpi/{sig}/{name}` | KPI tracker pages and CSVs at signed links (a Google Sheet mirrors one with `=IMPORTDATA(<link>)`) |
| `GET /files/{task_id}/{sig}/{name}` | signed links to files a workflow produced |
| `GET /assets/{name}` | the team's portraits and the app icon (`team/assets/*.png` only), public so Slack can show them |
| `POST /team/hello/{secret}` | the team introduces itself |

`webhook_sig.py` verifies `X-Sift-Webhook-Signature: v1,<b64>` over `f"{timestamp}."` + the RAW body with the key named by `X-Sift-Webhook-Key-Id` (fetched from `GET https://apiv2.reisift.io/webhook/signing-keys/`, no auth; a rotation lists both keys). `AGENTS_WEBHOOK_SIGNATURES` = `require` (default) | `log` | `off`.

### Releasing a batch

Anything that contacts a person is staged as a batch (`ledger.batch_create`). `agents/release.py` routes every batch kind (`RELEASE` table) for the CLI and Slack. In Slack, `release batch N` describes it and `confirm N` by the same person within 15 minutes queues it; only Slack ids in `AGENTS_RELEASERS` may release, never on a dry brain (checked at confirm and again in the worker), and the command is matched by a pattern before any model sees it. File kinds (a list a person uploads elsewhere) take `confirm N uploaded` after the upload. Every release re-runs the gate on every item.

## The autopilot

The prospecting lifecycle run as stages (`src/agents/autopilot/`, playbook `team/autopilot.yaml`).

- **The account's saved presets are the state.** A stage reads its presets live by exact title every run (cached `AUTOPILOT_PRESET_TTL_S`), so editing a preset in DataSift changes what the stage works on. Progression rides DataSift's own counters (dial attempts, mail attempts), never private flow tags.
- **Modes:** `off`, `plan` (runs on schedule, works out exactly what it would do and cost, writes nothing), `live` (acts inside its caps, and only on a box with `AGENTS_MODE=live`). Two switches on purpose: the playbook says what the owner wants, the environment says whether this box may write. Every stage ships in `plan` and goes live one at a time after its plan reads right.
- **YAML 1.1 reads a bare `off` as boolean false.** Quote every mode (`mode: "plan"`). `policy.mode_of` maps false to off and refuses true.
- **Plan-mode knowledge is kept, plan-mode actions are not.** A stage may write its own bookkeeping (lane map, screen verdicts) in plan mode; every account change goes through `Ctx.write`.
- **Caps** are checked before every paid call: the stage's `daily_usd`, the playbook's `caps.daily_usd`, the brain's daily cap, a per-record lifetime `caps.record_usd`, and a separate `caps.outreach_daily_usd` for mail so outreach never starves data spend.
- **The ledger** (`ledger.py`, tables in `agents.db`) records only LIVE actions, caches every scored number so none is bought twice, and every live write records the value it replaced: `cli.py autopilot runs` lists runs and `undo <run id> --commit` puts one back.
- **A stage that reads per record stops reading once it can no longer act** (a spent budget, a full day) and counts the rest instead.

**The suppression gate** (`gate.py`) is the one check before any assign, call, text, mail, trace, score, research or task, run when a record is picked up AND again at release. Rules include do-not-market, litigator, opted out, sold, deceased (held for research), in research, already a lead, finished status, listed, mail only, exhausted, returned mail, do not mail, owner lane, no number. A stage lifts a rule only by naming it in `gate_allow`. Nothing lifts do-not-market, a litigator, an opt-out or a sold house. The `gate:` block of the playbook names the tags, lists and fields each rule reads in that account.

**Lanes** (`lanes.py`): an owner sits in ONE lane across all of their records; the highest lane wins and the owner's other records are held. Each lane with a timeline (texts, call round, mail start, pieces) is worked on that timeline only, so two stages can never disagree about what comes next.

**The activity log copy** (`activity.py`): one shared copy of the account log in SQLite, synced oldest day first with a 6-hour overlap. Every stage reads history from the copy, never the API. A day with more events than one run may read keeps a partial mark so the next run continues instead of rereading from the top. Authors are stored as `self` (the key's own user) or `person`; use `activity.by_person` to ask whether a person did something.

**Adding a stage kind:** one module in `src/agents/autopilot/` plus one line in `registry.py` (`kind: (module, run function, spend class, needs a preset, always reports)`). `policy.parse` runs the kind's `block_problems`, so a mistyped block is refused when the playbook loads.

**Account tools** that change presets, filters or structure (`preset_patch.py`, `feeder_audit.py` / `feeder_apply.py`, `restructure.py`, `pipelines.py`, `flows.py`, `top1000.py`) follow one pattern: `plan` reads, proves each filter by count and writes a change list; `apply` is dry unless BOTH `--commit` and the tool's env switch (`PRESET_PATCH_ALLOW`, `FEEDER_APPLY_ALLOW`, `RESTRUCTURE_ALLOW`, `PIPELINES_ALLOW`, `FLOWS_ALLOW`, `TOP1000_ALLOW`) are set, sends exactly what the plan recorded, and reads each object back. `feeder_apply.py`, `restructure.py`, `pipelines.py` and `flows.py` keep an apply log that `undo --log <file>` replays backwards. Back up the presets before applying.

`flows.py` lays the presets out as three marketing flows, in folder order: foreclosures (first-to-market records on the Foreclosure list: texts from day 0, the call round, a mail piece every week until the sale), estates (courthouse probates and obituary records, each worked to the person who can sign), then the Top 1,000 and the agency. The first-to-market call and mail presets keep their titles, because every stage reads presets by title; they are narrowed with `all_lists` (which ANDs, so a filter's own `any_lists` either-or keeps its meaning) and new presets are built from their filters. A record on two flows' lists goes to the estate flow. The estate call presets read the probate notice tag OR a tag ("Obituary 30+ Days") that goes on an obituary record once its death is 30 days old: a preset's keys AND, and the app reads a date window only on the include side (a window under `must_not` works for the API and silently vanishes in the app), so the hold is a tag that `flows.py ready` and the obituary lane's `ready_tag` keep current.

**Proofs** (`autopilot/proofs.py list | run --key-env STAGING_DATASIFT_KEY --record <uuid> [--commit]`) try each unproven write on ONE record of a staging account, read it back and restore it. The account is named by the env var holding its key; the production key is refused.

## The texting agent

`src/sms_agent/`, full runbook in `src/sms_agent/README.md`.

- **DataSift webhooks cannot see an inbound text.** Its triggers are CRM state changes only, and DataSift hands SMS to providers. The inbound leg is smrtPhone's webhooks (`smsIncoming`, `smsOutgoing`, `smsDeliveryCallback`, `addNumberToDNT`, `addNumberToDNC`) to `POST /hooks/{secret}/smrtphone`; DataSift posts to `/hooks/{secret}/datasift`.
- Smarter Contact replies arrive at `POST /hooks/smartercontact`, accepted only with a bearer token (`SMARTERCONTACT_WEBHOOK_TOKEN`) or a valid ConnectSI signature (`SMARTERCONTACT_SIGNING_SECRET`); with neither configured every delivery is refused (401, so the sender retries).
- Flow: receiver verifies, persists, returns 200 -> worker drains events then the outbox -> engine resolves the record via `phone_map`, classifies, acts (phone status, opt-out, draft, escalate).
- Probes: `/health` (phase and dry-run, never touches the database), `/alive` (503 once the worker's last pass is stale; what the outside watchdog reads), `/stats` (counters).
- **Autonomy ladder** `SMS_AGENT_PHASE`: 1 classify and write phone status / opt-outs; 2 + escalate and flip CRM status; 3 + draft replies held for approval; 4 + auto-send a narrow intent set. `SMS_AGENT_DRY_RUN=1` independently blocks every send and CRM write.
- **Guardrails:** a human-typed outgoing message pauses the thread and cancels queued and held texts; opt-outs are decided by regex, never a model, and include natural language; a turn cap; a sticky sending number per conversation; per-number caps and pacing; two send windows that must both agree (the business window `SMS_AGENT_BUSINESS_*` and the recipient-local window `SMS_AGENT_QUIET_*` by area code); a confidence floor.
- **The output validator** blocks a draft that names a dollar amount (except a whitelisted price on the buyer program), names the list (foreclosure, probate, tax, lien, eviction, "behind on"), carries a link or a ZIP, exceeds 320 characters, asks two questions, or self-identifies as automated. Never name a company: identity is the assigned person's first name or none (`config/sms_senders.json`). The responder gets only first name, street line, city and county.
- **Outreach shares the reply outbox:** `seed` renders touches and stages them HELD; `release --touch N` is the go/no-go. A dry run must not write to the outbox, and `queue` skips any phone already held or queued, so a batch can never be staged twice.
- **Replies come from other lines.** Map every phone on a record (`map --all-phones`), not just the one texted.
- **Programs are told apart by the receiving number** (`sender_pool.program_for`): seller and buyer pools are disjoint, and a pinned pool that is missing returns no numbers rather than falling back.
- **Every new behavior is a switch that defaults to the previous behavior** (`cli.py switches`; the table and the order to turn them on are in the README). Fly secrets override `fly.toml [env]`: ask `/health`, not the file.
- **Run exactly one worker per database;** a second worker breaks human-takeover detection. Replay the stored events before changing the engine.
- A module named after a stdlib module shadows it when run as a script: the pool module was renamed from `numbers.py` to `sender_pool.py` after it silently broke pydantic inside the SDK.

## Other modules

| Module | What it does |
|---|---|
| `sold_guard.py` | Decides `recently sold` from the deed: a sale counts only after the record became a lead; a buyer who is the owner we hold is that owner's own purchase. Subcommands `audit`, `repair`, `mark`, `detect`, `recheck`, `daily`, `undo` |
| `clone_account.py` (`account_blueprint/`) | Exports an account's structure to a title-keyed, uuid-free blueprint and applies it to another account with read-back; `--phase` is `export`, `validate`, `plan`, `apply` or `verify` |
| `knox_ftm_pull.py`, `knox_liens_daily.py`, `knox_lien_releases.py`, `knox_condemnations.py`, `knox_rod.py` | County first-to-market sources: recorder liens and releases, condemnation agendas, staged and uploaded by the runner's `liens`, `capture`, `county` stages |
| `smarter_contact_*.py`, `sms_agent/sc_*.py` | Smarter Contact contacts, templates, groups, launches and inbound replies (runbook `docs/smarter-contact-setup.md`) |
| `cold_email/` | Instantly: list build, ZeroBounce verification, campaign, reply classification (README in folder) |
| `call_coaching/` | Pull smrtPhone recordings (`pull_calls.py`), transcribe with delivery notes (`transcribe.py`), export grades |
| `mls_offers.py`, `mortgage_model.py` | A cash and a creative offer on every active listing, with the seller's mortgage modeled (sends nothing) |
| `deal_odds.py`, `deal_odds_train.py` | The odds an investor buys a house within six months; `data/deal_odds_model.json` holds the weights the brain ranks on |
| `opensearch_mcp.py` | Read-only client for an `opensearch` MCP server configured in `~/.claude.json` with writes disabled; the property-index tools (`deal_odds_train.py`, `builder_land.py`, `mls_offers.py`) need it, and a regular DataSift account does not have it |
| `builder_land.py`, `land_market.py` | Builder buy boxes from deeds and land market sizing (they need the opensearch MCP; the team's `land_desk` renders a finished analysis) |
| `vacant_land.py` | Buildable-lot screening for a county set up in `COUNTIES`: county GIS for the parcel, SiftMap search for the land rows, USGS 3DEP for slope, owners from records already in the account (no SiftMap detail pull: DataSift's acceptable use forbids them in volume), offers off real lot sales, a mail list and a pull list. In the team it is Atlas's `buildable_lots`: it runs in the background and posts the files; the pull list goes through `list_pull`, which quotes before adding anything |
| `extract_market_finder.py`, `market_analyzer.py`, `county_market_report.py` | Market Finder pull and ZIP scoring workbook |
| `phone_validator.py` | Trestle scoring into Dial First (81-100), Dial Second (61-80), Dial Third (41-60), Dial Fourth (21-40), Drop (0-20) |

## Skills and the plugin marketplace

- **Source is the folder, not the ZIP.** Edit `skills/<name>/` or `plugins/<name>/`; `dist/*.skill` and `dist/*.plugin` are build products.
- **Build order:** `python tools/build_skills.py --marketplace` (writes `.claude-plugin/marketplace.json`, `marketplace/bundles/`, `plugins/siftstack-core/data/`), then `--build`, then `--manifest`. `--marketplace` must run before `--build` because core's archive is zipped from the tree it writes.
- **No `version` field anywhere;** the builder refuses one. A declared version pins a plugin and becomes its only update signal; without it every push to main is an update.
- `skills/<name>/` is published as a plugin with no restructuring (`strict: false`, `skills: ["./"]`). Bundles (one per category plus `siftstack-all`) live in `marketplace/bundles/` and hold only `dependencies`; `siftstack-core` (doctor and setup) rides in every bundle. Every current package must have a category or the builder stops.
- `dependencies` in a bundle needs a current Claude Code; `install.py --plugins` installs plugin by plugin and works on old versions. Auto-update is off by default for third-party marketplaces.
- Every `SKILL.md` needs `name` and `description` frontmatter (a skill without it never fires and no error is raised); a description over 1,024 characters makes Claude Code reject the whole package at install time. `tools/check_docs.py` checks these limits, routes, the marketplace and the hand-written counts in the docs.
- No top-level `bin/` in a plugin (Cowork refuses it). Secrets stay on the `.env` / env var contract: plugin `userConfig` substitutes into skill text and is not visible to scripts.
- A `.skill` archive has no diff: drift between source and archive is invisible except to `--verify`. Line endings count; `git add --renormalize .` fixes CRLF drift in zipped trees.
- The repo slug used by the installer, manifest and marketplace is set once as `REPO` in `tools/build_skills.py` (README has the install commands).
- **SOPs:** `sops/*.sop.md` follow the Agent SOPs format (Overview, Parameters with acquisition constraints, numbered Steps with RFC 2119 constraints). Validate after every edit with `python skills/playbook-creator/scripts/validate_sop.py sops/<name>.sop.md`. `.mcp.json` serves the folder as slash prompts (`python -m strands_agents_sops mcp --sop-paths sops`; on Windows the path list separator is `;`).
- **The org chart:** `docs/agents.json` is the source; `python tools/agent_map.py` writes `docs/AGENT-MAP.md` and `python tools/build_agent_page.py` writes `docs/agent-system.html`.

## Configuration

**Environment.** Every module reads the environment first and `.env` as a fallback. `.env.example` lists the pipeline keys; `.env.skills.example` the skill keys; `deploy/sync_*_secrets.py` list what each Fly app needs. Main groups:

| Group | Variables |
|---|---|
| DataSift | `REISIFT_API_KEY` (the Open API key, preferred everywhere); `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` (a login: needed only by the browser wizard and the few routes the key cannot reach, such as board columns and SiftMail designs; the uploader mints a JWT from it when present, the first-to-market secret sync lists it as required, and the brain never takes one) |
| Notice site | `TNPN_EMAIL`, `TNPN_PASSWORD`, `CAPTCHA_API_KEY`, `CAPTCHA_KIND`, `TURNSTILE_SITEKEY`, `SCRAPE_BACKEND` |
| Egress | `SIFTSTACK_PROXY_URL`, `APIFY_PROXY_GROUPS`, `APIFY_PROXY_SESSION`, `APIFY_TOKEN`, `SCRAPFLY_KEY` |
| Enrichment | `SMARTY_AUTH_ID`/`_TOKEN`, `OPENWEBNINJA_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `SERPER_API_KEY`, `SERPAPI_KEY`, `FIRECRAWL_API_KEY`, `LLM_BACKEND` |
| Skip trace and phones | `SMARTSKIP_EMAIL`/`_PASSWORD`, `TRACERFY_API_KEY`, `ENFORMION_AP_NAME`/`_PASSWORD`, `TRESTLE_API_KEY`, `TRESTLE_FREE_API_KEY`, `TRESTLE_PAID_API_KEY` |
| Runner | `FTM_ARGS`, `FTM_SCHEDULE`, `FTM_SCHEDULE_DAYS`, `FTM_JITTER_SECONDS`, `FTM_HEALTH_AT`, `FTM_HEALTH_WEBHOOK`, `FTM_FEED_STALE_DAYS`, `FTM_RATE_BUDGET`, `BUSINESS_TIMEZONE` |
| Brain | `AGENTS_MODE`, `AGENTS_RUN_CAP_USD`, `AGENTS_DAILY_CAP_USD`, `AGENTS_PUBLIC_URL`, `AGENTS_WEBHOOK_SECRET`, `AGENTS_RELAY_SECRET`, `AGENTS_RELEASERS`, `AGENTS_RESEARCH_ENGINE`, `AGENTS_DP_*` (research write-back switches), `DATASIFT_RATE_QUIET` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`, `AGENTS_SLACK_*`, `SLACK_WEBHOOK_URL` (post-only fallback) |
| Texting | `SMS_AGENT_*`, `SMRTPHONE_API_KEY`, `SMRTPHONE_NUMBERS` |

A new env var a Fly app needs must be added to that app's `deploy/sync_*_secrets.py` list (`REQUIRED` or `OPTIONAL`), or it never reaches the machine.

**The playbook** (`team/autopilot.yaml`): `business` (zone, hours, days), `caps`, `people` (who records go to), `gate` (the tags, lists and fields the gate reads), `lanes` (how each lane is recognized and its timeline), and `stages` (`<id>: kind, owner, mode, schedule, presets, summary, gate_allow` plus kind-specific settings). Check it with `cli.py autopilot check` (`--live` confirms every preset title exists). Ids an account owns (board columns, SiftMail designs) are per account.

**The roster** (`team/roster.yaml`) and the briefs (`team/<role>/CLAUDE.md`): rename a persona with `display_name`; teach a method by adding a skill folder to `skills:`; wire a workflow by registering its handler and setting `status: live`. Run `cli.py team` after any change.

**The KPI catalog** (`team/kpi.yaml`) holds every figure the reports judge against; the reports carry no numbers of their own.

**The KPI sheet** (`kpi_tracker` stage, `autopilot/kpisheet.py` layout, `autopilot/kpisinks.py` writer): a Google Sheet the brain rewrites three times a weekday in the DataSift KPI tracker's layout (title, one Refreshed line, metrics down the side, the newest month first, notes at the bottom). Its Goals tab is where the owner types the goal, a calling day's targets, the monthly costs and each deal's fee, each goal shown beside what it needs a month, the month so far and the pace; every run reads it first and `kpi.load()` lays it over `team/kpi.yaml`, so every report uses those numbers. A Texts tab carries the text funnel and every new lead is credited to a source. Connect it once with `python deploy/kpi_sheet_connect.py --sheet <link> --client-env <a .env holding a Google OAuth desktop client>`; without it the brain's own page and CSVs (`GET /kpi/...`) carry the same tabs.

## Deploying

Prerequisites: `flyctl` installed and signed in with `fly auth login` (it needs an interactive terminal; from an agent session, launch it in its own window). An app-scoped `FLY_API_TOKEN` in the environment makes flyctl fail on every other app and cannot create apps: blank it for those calls.

### Agents brain (one command)

```bash
python deploy/agents_up.py                                # preflight + plan, changes nothing
python deploy/agents_up.py --commit                       # create, sync, deploy, wire, verify
python deploy/agents_up.py --app <name> --region iad --commit
```

Steps, each skipped when already done, so a rerun picks up where the last stopped: preflight (roster valid and `TEAM.md` current, selftest green, `REISIFT_API_KEY` and `ANTHROPIC_API_KEY` present, flyctl signed in), app, volume `agents_data`, secrets (`deploy/sync_agents_secrets.py --commit`, which generates `AGENTS_WEBHOOK_SECRET` once and appends it to `.env`), `fly deploy -c fly.agents.toml`, `/health` until the worker is alive, the one-click Slack app link (install the app, paste `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` into `.env`, rerun), DataSift wiring, and the team's hello. Socket Mode (`SLACK_APP_TOKEN`) can replace the signing secret for listening, but the manifest ships with Socket Mode off: switch it on in the app's settings or the connection opens and hears nothing.

Creating a DataSift webhook is an app-only operation (Settings, Integrations, Webhooks): `cli.py setup-datasift` prints the URL and steps and waits for DataSift's own Test delivery to reach the brain (exit 2 while waiting). Then build a sequence whose action is that webhook. `--staff-api` wires accounts you operate yourself through an unlisted route.

By hand: `fly apps create <app>`, `fly volumes create agents_data --size 1 --region iad -a <app>`, `python deploy/sync_agents_secrets.py --app <app> --commit`, `fly deploy -c fly.agents.toml -a <app>`. `sync_agents_secrets.py --exclude NAME` keeps an optional secret off the box (for example the Slack names on a first deploy, so a plan-mode brain posts nowhere).

### First-to-market runner

```bash
fly launch --no-deploy --copy-config --config fly.ftm.toml --name <app>
fly volumes create ftm_data --size 3 --region iad -a <app>
python deploy/sync_ftm_secrets.py [--app <app>]           # masked plan
python deploy/sync_ftm_secrets.py [--app <app>] --commit
fly deploy --config fly.ftm.toml
```

Then on the box, in order: `--doctor`, `main.py list-searches`, `ftm_runner.py --max-notices 2`, `ftm_schedule.py --next` (`fly ssh console -a <app> -C "python /app/src/ftm_runner.py --doctor"`). Ship `FTM_ARGS` without `--commit` until one scheduled run has been read. The Playwright base image tag in `deploy/Dockerfile.ftm` and the `playwright` pin in `deploy/requirements-ftm.txt` move together, or the run dies at launch. The VM is `shared-cpu-2x` / 2 GB: on one shared core Chromium takes about a minute just to launch. Deploy on a weekday after the morning run, never right before it.

### Texting agent

```bash
fly launch --no-deploy --copy-config
fly volumes create sms_agent_data --size 1 --region iad
fly secrets set SMS_AGENT_WEBHOOK_SECRET=... SMRTPHONE_API_KEY=... REISIFT_API_KEY=... ANTHROPIC_API_KEY=... SMS_AGENT_SLACK_WEBHOOK=...
fly deploy
```

Paste the two webhook URLs `doctor` prints into smrtPhone and DataSift. `SMRTPHONE_NUMBERS` (a secret) outranks `/data/sms_numbers.json`; refresh the pool with `cli.py numbers --refresh` and push it with `cli.py numbers --push | fly secrets import`. Keep `SMS_AGENT_DRY_RUN=1` and phase 1 until a live send has been watched end to end.

### Skill library

Push to the default branch. CI (`.github/workflows/skills.yml`) verifies archives against sources, regenerates and diffs the manifest and marketplace, installs `siftstack-all` into a throwaway config on the latest Claude Code, runs the installer twice (the second must be a no-op), runs the doctor with no credentials, and scans packages for credentials.

### Apify Actor (legacy)

`apify run --purge` locally (set `SIFTSTACK_FORCE_ACTOR=1`), `apify push` to deploy. `main.py` enters the Actor path only when `APIFY_IS_AT_HOME` (set by the platform) or `SIFTSTACK_FORCE_ACTOR` is set; an `APIFY_TOKEN` in `.env` alone must not switch a local run into the Actor.

### After every deploy

- Read the effective configuration from the running box: `GET /health` (brain and texting agent), `fly ssh console -a <app> -C "printenv"`. `fly config env` shows only the file's `[env]` block.
- Run each changed stage once on the box (`fly ssh console -a <app> -C "python /app/src/agents/cli.py autopilot run <stage> --plan"`).
- Brain: `python -m agents.sandbox --check` if the image changed; `python deploy/agents_heartbeat.py https://<app>.fly.dev`.
- Runner: `ftm_runner.py --doctor`, `ftm_health.py --selftest`, `ftm_budget.py --selftest`.
- Deploy from a clean checkout when other work is in progress in the tree; `fly deploy` ships the working directory, and `.dockerignore` is all that keeps `output/` and `.env` out of the build context.

## Testing

**The zero-network rule.** Selftests and unit tests make no network call and write only to a temp folder. A test that needs a vendor gets a fake; a test that can reach a live service is a bug. The agents selftest blanks every Slack setting and installs `NoNetwork` where `requests` stands; the texting selftest uses an offline client and a socket guard. Test data is invented: streets with Example, Sample, Test or Demo in the name, phones in 555-01XX or a never-assigned exchange, never a real person.

```bash
python src/agents/cli.py selftest            # thousands of checks; run with the repo venv
python src/sms_agent/cli.py selftest         # offline, sends nothing ([--live-model] spends cents)
python src/ftm_health.py --selftest
python src/ftm_budget.py --selftest
python -m pytest tests/test_ftm_runner_stages.py      # one file at a time
python tests/test_account_blueprint.py                # many test files also run standalone
```

- Each test file inserts `src/` on `sys.path` itself. There is no `conftest.py`.
- **Do not run `pytest tests` over the whole folder** without checking for import-time work: a module that does live work when collected will spend money. Run files by name.
- A selftest section is one line in `agents/selftest.EXTRA_SECTIONS` and one `selftest_<name>.py` module. A section run on its own must set its own `config.DB_PATH`, which is fixed when config loads.
- Pin the clock in any check that reads it (business hours, quiet hours, "today"); a wall-clock check passes in the morning and fails in the evening.
- A helper named `test` in a test file is collected by pytest as a test.
- Assert, do not print. The failure this codebase keeps rediscovering is a run that reports success while doing nothing; a check that only prints cannot catch it.
- Run a model-fed workflow on a real model's output once, not only on fixtures: models write inputs in their own shapes, and the normalizer has to meet them.

CI also runs `.github/workflows/docs.yml` (`tools/agent_map.py --audit` and `--check`, and the dash check), and three outside watchdogs: `agents-heartbeat.yml`, `ftm-heartbeat.yml`, `sms-agent-heartbeat.yml`. **Scheduled workflows run only from the default branch.**

## API contracts and traps

### DataSift Open API rules (developers.datasift.ai)

- **Auth is the Open API key:** `Authorization: Api-Key <key>`, no expiry, inherits its user's permissions. It reaches the documented `/api/internal/` read and write routes. A bearer token copied from a browser is restricted and throttled. Never collect a customer's password.
- **Rate limits are per ACCOUNT, shared by every key-based tool on it:** 120 a minute and 5,000 an hour, with a separate SiftMap counter of the same size. A 429 carries `Retry-After` or "available in N seconds": honor it. Sustain about 80 a minute.
- **Bulk jobs** take 20 or more records, about 1,000 per job, two or three jobs a minute. Under 20 use the per-record routes; many tiny jobs read as abuse.
- **Prohibited:** SiftMap detail pulls in volume without adding the records (scraping, even inside the limits); volume through SiftMap autocomplete (use `POST /properties/search/`); partner namespaces (calltools, launch-control, reirail, smarter-contact, smrtphone, smrtdialer, xencall, kixie, aircall, twilio, plivo, dataflik); `GET /properties/images/`; user, billing and account administration.
- **App-only:** webhook and integration setup, billable add-on settings, owner merge and swap, deleting SiftLine boards and custom fields. Sequences are not in the API.
- **Not in the published API:** listing an account's users (`GET /api/internal/user/` is the key's own user), listing every status (`/api/internal/global-status/` names only the lead pipeline), direct-mail attempts, SiftMail, board columns, upload usage, custom status create.
- Changes get 90 days' notice with `Deprecation` and `Sunset` headers; beta routes can change without notice. `python tools/datasift_api_audit.py --check` fails on any called route that is neither in the published spec nor reviewed in its `KNOWN` table: run it after touching any DataSift call.

### Records search

- `POST /api/internal/property/` with header `x-http-method-override: GET` and `{"query": {"must": ...}}`. A bare POST CREATES a record. Rows are under `results` (or `data`).
- **offset + limit may not exceed 10,000** ("Can't fetch more than 10000 items!"); slice by `created: [from, to]`. An empty `must` 400s ("Filter can't be empty").
- `must_not` nests INSIDE `must`; as a sibling it 400s.
- **Honored:** `created`, `last_sold` (lists `[from, to]`; dates or datetimes; a same-day pair matches nothing, use the next day), `any_property_status`, `any_lists`, `any_tags`, `owners: [uuid]`, `properties: [uuid]`, `assigned_to: null` (unassigned; a non-uuid string 400s), `year` (year built), `property_type` (`clean` or `incomplete`).
- **Silently ignored:** `status`, `year_built`, `created_date`, `date_added`, `last_sale_date`, `last_sold_date`, and any key it does not know. Two titles in `any_tags` mean OR.
- Structure must be exactly `"Single Family Residential"`. Records with no value for a filtered field are excluded by it.
- A date filter takes a LIST; a dict 400s. A saved preset's relative windows (`["36-months","month"]`) are refused ("Use YYYY-MM-DD"): convert with `autopilot/windows.resolve`. A preset nests `ownerPropertiesOwned.show_properties`, which the search wants flat (`autopilot/crm.search_shape`).
- A custom field cannot be a search filter: a list under `custom_fields` 400s and a bare field name is ignored.
- **Search rows** carry `list_count` (including DataSift's own classification lists), `tag_count`, `status`, one representative phone and NO dates or tags. **The do-not-call registry flag exists only on the search row**; the full record omits it.
- **The full record** (`GET /api/internal/property/{uuid}/`) carries scores, deal fields, owner flags, every phone and no created date. Field names differ from CSV upload names (`estimate_value`, `equity_percent`, `last_sold`, `year`, `assigned_to` as a bare uuid). `Total Delinquency` is liens plus taxes, never the tax figure. DataSift's own refresh can move `last_sale_price` without moving `last_sold`; a price change with no date change means `last_sold` is stale.

### Writes

- Per-record actions under `POST /api/internal/property/{uuid}/`: `assign/ {"user_uuid"}`, `status/ {"status"}`, `add-tags/` and `remove-tags/` `{"tags": "<one title>"}` one call per title, `add-notes/ {"notes"}`, `add-lists/` with a STRING title.
- **Tags must be an array** on a create; a comma string creates one tag named with the commas.
- **A PATCH of `tags` only ADDS;** removal is `remove-tags` with TITLES (uuids return 200 and remove nothing). `remove-lists` takes `{"lists": "<title>"}` as a string; an array returns 201 and does nothing.
- **A select custom field's value is the option's UUID,** not its label. A `select` field must be created with its options in the same POST. Custom field values go to `PATCH /api/internal/property/{uuid}/custom-field/update-values/` `[{"field_uuid", "value"}]`.
- **An entity owner:** send `company` and OMIT the person keys; a blank `first_name` 400s.
- **`notes` on a property payload returns 200 and is discarded.** Post notes separately.
- **Clear a status** with `POST .../status/ {"status": null}`; a PATCH refuses null.
- Owner fields (`first_name`, `last_name`, `company`, `type_override`, `primary_phone`, `deceased`) go to `PATCH /api/internal/owner/{uuid}/`. Phones merge through `POST /api/internal/owner/{uuid}/upsert-phones/`.
- Attempt counters (`predictivecall-attempts`, `sms-attempts`, owner `skiptrace-attempts`) take the TOTAL as an integer.
- **Scoped skip trace:** `POST /api/internal/property/skip-trace/` with `{"query": {"must": {"properties": [...], "property_type": "clean"}}, "dry_run": true}` first, then `expected_count`. Bulk actions scope by `query.must.properties`; a top-level `properties` is ignored and the action runs on the WHOLE account.
- The message board is per owner (`/api/internal/owner/{owner_uuid}/message/`, `/pin/`).
- Titles are unique per account: presets exactly, lists and board columns ignoring case and surrounding whitespace. Tags keep case. A custom status needs `color` to be created.
- Folder preset listings default to 10 rows: always `?limit=999`.
- Index counts lag writes by about 30 seconds; verify a write on the record, not on a count.

### The activity log

- Account log: `GET /api/internal/property/logs/?query={"must":{"timestamp":[from,to],"any_event_type":[...]}}&limit=100&cursor=`, newest first, UTC timestamps. `any_event_type` is the only event filter it honors.
- Per-record log: `GET /api/internal/property/{uuid}/logs/` pages by `offset` with `limit` at most 100 (more answers 400) and IGNORES `any_event_type`: filter by `event_type` in code.
- Calls are OWNER events (`owner.call.made`, `answered`, `missed`, `received`), logged per owner, so activity on an owner's other properties shows on every one of them. "Answered" includes voicemail greetings: a conversation is a duration threshold, never the event alone.
- People acting in the app log as `internal-api` under their own email, exactly like an API key: the author, not the source, says who acted. Dialers log as their own source (for example `kixie`).
- **The dialer's attempt counter does not follow the calls;** rebuild it from the log.
- Texts sent through Smarter Contact or smrtPhone do not appear in the log (only `owner.sms.failed`).
- Sequences triggered on `property.tags.added` may not fire on a SiftMap system add; they fire later when another tag lands. Set the outcome directly if it matters.

### SiftMap

- `POST /properties/search/` with the Open API key; rows under `data`. **`result_index` is a ROW OFFSET** (1, 251, 501), not a page number: page numbers return near-total duplicates.
- Rows carry `id` (the dataflik id, equal to a CRM record's `dataflik_id`), `saved` / `saved_uuid`, `corporateOwned`, `absenteeOwner`, scores and distressors, the address as ONE string, and NO owner name. The detail record has no street field (join on the row) and `owner_info.owner_mail_address` as one string. `sale_history` is ordered newest first.
- In the `addresses[]` block, `search` is the bare county name ("Knox"); "Knox County, TN" returns 0.
- Proven filters: `extra_last_sale_date_min/max`, `extra_is_last_sale_interfamily`, `extra_collapse_by_owner`, `extra_last_sale_investor_transaction_type` (a LIST of pending, wholesale, wholetail, flip, rental), `in_my_account_mode: "in"` (records already in the account), `year_built_min/max`, `type_land: true` (residential vacant only). Ignored: `extra_last_sale_price_min` and every guessed key.
- **`flip` means the exit already happened:** the current owner is a retail buyer and the investor is the last sale's SELLER. `pending`, `wholesale`, `wholetail`, `rental` mean the investor still owns it. `rental` lags by over a year, so a recent buy-and-hold shows as `pending`.
- Detail reads cap near 2-4 a second in aggregate, with escalating backoff; more workers do not help.
- Saved filters live at `map.reisift.io/filters/`: the listing pages at 10 rows and ignores `?limit=` (follow `next`); `PATCH {"auto_add_enabled": false}` works on the key; `tags` are titles; auto-add spends the record allowance. A filter with a fixed date floor drifts until "recently sold" means years.
- `add-properties-by-query` is asynchronous ("Activity created successfully"); lists attach by NAME; pulling by id uses `addresses: [{"search": "<id>", "type": "ids"}]`. On an unlimited plan the add may skip trace on its own.

### Market Finder

The documented API filters a county by `fips` and pages by `offset` + `limit`; `county=` and `page=` are silently ignored (and return the whole country). The browser path (`extract_market_finder.py`) reads a div-based table (no `<table>`), paginated 20 rows at a time, with `InputMultiSearch` state and county inputs and a styled Select for the ZIP or neighborhood view.

### DataSift app automation (Playwright)

- Wait on `domcontentloaded`; the SPA keeps sockets open. Hidden checkboxes: click the `<label>`.
- Dropdowns are styled components, not `<select>`; target the last visible one and bound queries to the main panel. Set React inputs through the native value setter plus `input` and `change` events.
- The filter panel is its own scroll container: scroll with `el.scrollIntoView` in JS.
- Sequence builder cards use React DnD: drag with slow stepped mouse moves.
- Remove the Beamer NPS iframe and push modal from the DOM before clicking anything; use a JS click when an overlay intercepts pointer events.
- Never run a bulk action whose list filter failed to apply: "continue anyway" means SELECT ALL on the whole account.

### Webhooks

Outbound only, created in the app (Super Admin), used as a sequence action (`{"action": "send-webhook", "payload": {"webhook_uuid": ..., "label": "send.webhook"}}`). Events: `sequence.action.triggered` and `webhook.test`. Body: `event_type`, `event_id`, `event_datetime`, `sequence {uuid, name}`, `property {uuid, address, owner, tags, lists, ...}`. Up to three retries: dedupe on `event_id`. Verify the Ed25519 signature over the raw body, never re-serialized JSON.

### SiftMail (DataSift direct mail)

Designs live at `mail.reisift.io` and answer a login token, not the Open API key; they are made only in the app's Mail Design page. Campaigns go through `POST /api/internal/property/direct-mail-campaign/` (`estimate: 1` quotes, `0` plus a confirmation date launches), which is not in the published API: the mail stage refuses it unless the stage sets `internal_routes: true`. Campaigns print on a weekday at least two business days out. Designs merge the owner's raw first name (a company name when blank), so route company owners to a no-name twin (`mail.is_named`). Never edit a design a live campaign uses. A check-style piece must state on the check that it is not a negotiable instrument and has no cash value, and mail copy never offers to stop or delay a foreclosure.

### Smarter Contact

- Read the vendor's terms before automating: they ban unofficial interfaces, bots and RPA, and the official integration route is ConnectSI. `sms_agent/sc_client.py` uses the app's internal GraphQL route, so treat it as a decision to revisit, not a pattern to copy.
- DataSift's "Send To" push answered a count and delivered no contacts: verify contacts arrive, or create them directly with every attribute set at creation.
- DataSift writes its record uuid into the contact's APN field; keep a separate field for the record id.
- Near-duplicate templates are refused (`TEMPLATE_DUPLICATE_ERROR`); keep variants under 0.90 similarity.
- Never wrap a long create run in `timeout`; a killed run leaves contacts made and no groups.

### smrtPhone

- Public API for TEXT only: `POST phone.smrt.studio/sms/send`, header `X-Auth-smrtPhone`. No API for MMS. Probe a key with an empty send: 400 "Missing required parameter(s)" means valid, 403 invalid. Do not probe `GET /dialerConfigs` (it serves the web app regardless of key).
- Webhooks are unsigned (use a secret URL path and an optional IP allowlist), logs purge after 30 days, and there is no replay: keep the receiver always on and the event log on a volume.
- The phone number list and the call log have no public API; the web session's DataTables routes (`POST /phoneNumbers/filtered`, `POST /logs/calls/filtered`) work and recordings sit at public `rec.smrtphone.io` URLs. Check the provider's acceptable use policy before automating a signed-in session.
- The browser session transport sends from the account's default caller id only.
- A2P 10DLC registration is invisible to tests: send one canary per number and read the result.

### Trestle

- Phone scoring tiers: 81-100 Dial First, 61-80 Dial Second, 41-60 Dial Third, 21-40 Dial Fourth, 0-20 Drop. Add the litigator check. There is no usage API: track spend locally.
- Real Contact (`GET api.trestleiq.com/1.1/real_contact?name=&phone=&add_ons=litigator_checks`) says whose number it is (`name_match`, `contact_grade`, `activity_score`). Send only `x-api-key` and `Accept`: a browser User-Agent gets `AUTHENTICATION_FAILED`, which reads like a bad key. Reverse Phone may be unlicensed (403).
- A company name never name-matches; test each named person against the company's numbers.

### SmartSkip, Tracerfy, Enformion

- **SmartSkip** bulk skip returns relatives and their phones in one row; required columns are First Name, Last Name and Mailing Address, so split one-string addresses into street, city, state and ZIP and read the `mapped` block echoed on submit. The wallet does not pay for bulk: `payment-intent` bills the saved card. Unpaid orders are invisible in `GET /bulk-skip`: persist the `bulkSkipId` before paying. Relationship labels are coarse, relative rows carry the relative's own address, entities cannot be traced, and middle names from an obituary make heir traces miss.
- **Tracerfy** batch: multipart CSV with a Bearer token, `mail_*` columns required, flat phone fields, consumer only. Its deceased flag exists only on the instant lookup, never with a date of death.
- **Enformion** person search 400s on a name alone; anchor it with the full street line, or a common name returns the wrong person first. Detect failure by HTTP status (the response always carries an `error` object). The relatives graph is capped and lags deaths. BusinessV2 (`galaxy-search-type: BusinessV2`) resolves company officers but ignores geographic anchors: keep only officers whose own address is in the buyer's state, and treat a registered agent as a likely lawyer unless their name appears in the company name. Enformion addresses put a semicolon before the city.
- **Never search a person by name plus city:** a same-name stranger ranks first and a later name match confirms the wrong person. Trace at a verified home street.

### OpenWeb Ninja (Zillow)

`/search` is the working endpoint (comps-style endpoints 404). `home_status` must be exactly `RECENTLY_SOLD` or `FOR_SALE`. Every search caps at 41 rows with `totalPages=1`: partition by `min_price` / `max_price` bands and split saturated bands. `price_min` / `price_max` are silently ignored: check the echoed `parameters` object. `dateSold` is epoch ms, `soldPrice` a display string (use `unformattedPrice`). MLS sales only: auctions and off-market transfers are on the county record. `homeType: LOT` can be a house sold at land value.

### Notice sites, proxies and scraping services

- Apify proxy session ids accept only letters, digits, `_` and `.`; a hyphen returns 407, which reads like a bad password. `proxy_resolver._safe_session` rewrites it.
- Scrapfly ASP clears Cloudflare on county and genealogy pages but each scrape is a fresh session: an ASP.NET detail page fetched outside the session that ran the search comes back empty. People-search aggregators ban ASP.
- Some city and county sites answer 403 to any datacenter address whatever the headers: try direct, then fall back to a residential fetch, and verify every source from the box.
- County assessor and tax portals: a fuzzy search's first result is not your parcel (match house number and street); some searches are literal about street suffixes (abbreviate, then drop the suffix); some pages need a browser User-Agent and ship an empty table filled by a JSON POST; some print the house number last. A "due" amount includes the current year's bill: delinquent means delinquent and unpaid.

### Claude, the Agent SDK and OpenRouter

- Structured outputs reject very large schemas ("compiled grammar is too large"): ask for JSON in text and force the shape in code (`researcher.conform`), then enforce sources.
- Small Claude models may think by default: send thinking disabled with low effort for short rewrites, and retry without it on a 400.
- OpenRouter: send `provider.require_parameters: true` with tool requests so a host that drops tools is never chosen, and `data_collection: deny`. Reject a research memo with fewer than three tool calls: a host that ignored the tools returns a confident memo with nothing behind it.
- Audio transcription invents speech on short clips: accept a transcript only at a plausible words per second of real audio (measured with ffprobe).

### Slack

Events carry channel IDS, not names; resolve with `conversations.info`. A mention arrives twice (`app_mention` and `message`): dedupe on channel plus ts. A private channel is invisible to the app until it is invited. Acknowledge a Socket Mode envelope before handling it (three seconds). An open socket proves nothing: with Socket Mode off for the app, the connection opens and stays silent. The app's scopes cannot create channels.

### Instantly and ZeroBounce

Instantly schedules accept a fixed list of time zones that excludes `America/New_York` (use `America/Detroit`); bare text with `<br>` is stripped on save (one `<div>` per line, then read back); analytics take `ids=`, not `id=`. ZeroBounce takes `verify_plus` per call. A commercial email needs a postal address (CAN-SPAM).

## Fly gotchas

- **Secrets override `[env]`.** A secret and an `[env]` line with the same name: the secret wins and the file lies. Ask the running box.
- Each `fly secrets set` is a release: set them in one call with `--stage`, then deploy (the sync scripts do this, values masked).
- Never auto-stop a webhook receiver (`auto_stop_machines = false`, `min_machines_running = 1`): a stopped machine drops the webhook and nothing replays it. A failing HTTP health check can restart a healthy machine mid-send; the texting app has none on purpose. A liveness probe must never wait on the lock the worker holds, or a slow query becomes a reboot loop.
- `nohup` does not survive `fly ssh console` (Fly kills the process group on hangup). Start long jobs as `setsid -f sh -c 'exec python -u ... > /data/<log> 2>&1 < /dev/null'` and poll the log or the database.
- `fly ssh console -C` runs without a shell: wrap builtins in `sh -c`. The brain image has no `ps`, `pgrep` or `kill`; read `/proc`. A process search whose pattern appears in its own command line always matches itself.
- From Git Bash on Windows a remote `/data/...` argument is rewritten to a local Git path: set `MSYS_NO_PATHCONV=1` for `fly ssh sftp` and `fly ssh console -C`.
- Losing a volume means re-scraping and re-paying for every handled item: never delete one to start clean.
- A killed or dropped run leaves a partial batch: read the held count back before trusting it.

## Workstation gotchas

- A `\b` or `\n` inside a regex or Python patch passed through a bash heredoc reaches the file as a backspace byte or a real newline, and triple quotes or apostrophes break the heredoc. Write patch scripts and regexes with the file tool.
- A function-local `from agents import store` makes `store` local for the WHOLE function: an earlier use in that function raises UnboundLocalError.
- In a checkout inside a cloud-synced folder, `shutil.rmtree` of a generated folder can die with WinError 5 while the sync client holds it: overwrite in place and prune only what is stale.
- Windows paths are limited to 260 characters; a venv under a deep temp path can fail to install.
- A background shell command given a timeout is killed at it: launch long paid jobs detached and read their log.
- Subagents share the session scratchpad: give each its own subfolder and let none delete anything.

## Extending SiftStack

| To add | Do this |
|---|---|
| An autopilot stage kind | A module in `src/agents/autopilot/` with `run_<kind>`, one line in `registry.EXTRA`, a stage block in `team/autopilot.yaml` with `mode: "plan"`, and a selftest section |
| A workflow the team runs on request | A module in `src/agents/workflows/` with `run(task)` and `render(task, result)`, one line in `workflows.REGISTRY`, a roster entry with owner, spend, triggers, inputs, `status: live`, then `cli.py team` |
| A batch kind | A release function and one line in `agents/release.py` `RELEASE` (and `FILE_KINDS` when a person uploads a file between steps) |
| A selftest section | `src/agents/selftest_<name>.py` and one line in `selftest.EXTRA_SECTIONS` |
| A Fly secret | The name in the app's `deploy/sync_*_secrets.py` list |
| A DataSift route | Prefer a documented route; otherwise review it in `tools/datasift_api_audit.py` `KNOWN` and pass `--check` |
| A runner stage | A name in `ALL_STAGES` in `ftm_runner.py`, off unless named in `--stages`, placed before `county` if it only stages rows |
| A skill | `skills/<name>/SKILL.md` with frontmatter and a category, then `build_skills.py --marketplace --build --manifest` |

Every new behavior ships behind a switch that defaults to what the system did before, so a deploy that sets nothing changes nothing.
