# SiftStack — Plain-English Walkthrough

**Read this once and you'll understand the whole system.**

This doc tells the story of what happens between the moment SiftStack wakes up at 7 AM and the moment Mike opens DataSift at 9 AM. Then it explains what Mike does next, and what to validate so we know everything's working.

If you're new to SiftStack: read sections 1–4 in order.
If you're Mike onboarding: focus on sections 4 + 5 (after the morning run).
If you're auditing: section 6 has every checkpoint.

---

## 1. What SiftStack does at its core

Every weekday morning, **before anyone wakes up**, SiftStack scrapes 9 Ohio public-records websites, enriches every record it finds with property data + obituary research + skip-trace phones, and lands the cleaned-up records inside DataSift — already sorted into the right preset buckets, already tagged, already scored by call priority.

The point is to **save Mike from doing any list pulling, list cleaning, or skip tracing manually**. He just opens DataSift at 9 AM and works the leads. Everything upstream of that is automated.

The system runs on Apify (cloud) — nothing depends on Aaron's laptop being on. It fires every morning regardless.

---

## 2. The chain of events (chronological, every morning)

### Step 1 — 7:00 AM: Wake up + scrape

A scheduled cron job on Apify fires the SiftStack Actor. The Actor (a Python script in a Docker container) loads the credentials from input config and starts scraping 9 OH public-records portals in parallel:

| County | Portals scraped |
|---|---|
| **Franklin** | Probate court, RealAuction sheriff sales, Common Pleas (lis pendens) |
| **Montgomery** | Probate court, county sheriff sales (ColdFusion site), Common Pleas (lis pendens), RealAuction sheriff sales (backup) |
| **Greene** | Probate court, county sheriff sales (ASP.NET site), Common Pleas (lis pendens), RealAuction sheriff sales (backup) |

Each scraper produces a list of "raw notices" — case numbers, names, dates, addresses (when available). Foreclosure scrapes go back 35 days so post-auction records stay visible for the redemption watcher.

Output of this step: ~300–400 raw records, county/notice_type tagged but not yet enriched.

### Step 2 — ~7:25 AM: Memory check (dedup)

The pipeline opens its memory file (`notice_state.json` stored in Apify's key-value store). For each scraped record, it asks: "Have I seen this before?"

- **YES** → carry forward all the enrichment we already did (Smarty validation, Zillow value, obituary research, phones, etc.) so we don't re-do it
- **NO** → flag as truly NEW; this record will run through the full enrichment pipeline

Output: every scraped record now has either fresh fields (NEW) or carried-forward fields (CACHED). Pipeline knows the count of each.

### Step 3 — ~7:30 AM: Redemption-window watch (foreclosure only)

Only fires on foreclosure records that have a case number. The watcher hits each county's Common Pleas court docket and looks for one of three events:

| Event found in docket | Action |
|---|---|
| "RETURN OF SALE FILED" or "ORDER OF SALE RETURNED" | Apply tag `redemption_open` — sale happened, redemption right is alive until court confirms |
| "CONFIRMATION HEARING SET" with a date in the next 14 days | Apply tag `redemption_closing` — urgent, ≤14 days until window closes |
| "ENTRY CONFIRMING SHERIFF SALE" or "ORDER CONFIRMING SALE" | Apply tag `redemption_closed` — window is gone, record retires from active presets |

This is why foreclosure records that already went to auction stay alive in the system — the watcher tracks them through the post-sale window so the FTM_RW presets can surface them while the homeowner still has rights.

### Step 4 — ~7:35 AM: Enrichment (only on NEW records — CACHED records skip)

For each NEW record (and any CACHED records missing key fields), run the full enrichment waterfall:

1. **Smarty USPS validation** — confirms the address is real, gets ZIP+4, geocodes, flags vacancy. Records that fail USPS validation are dropped (bad mail spend prevention).
2. **County Auditor lookup** (probate only) — finds the deceased person's actual property address from the county tax records. Probate court records don't include the property; we have to look it up.
3. **Zillow data** (via OpenWebNinja API) — pulls current Zestimate, MLS status, equity %, sqft, beds/baths, year built.
4. **Entity research** — if the owner is "ABC Holdings LLC," try to find the actual person behind the entity via Secretary of State search.
5. **Obituary search** — for records where someone might be deceased (probate decedents, unusually old foreclosure owners), search 7 search engines for an obituary, then run Claude Haiku on each candidate page to validate the match. Returns survivors list, date of death, executor name.
6. **Heir verification + DM ranking** (only on confirmed-deceased records) — search each named survivor separately to verify they're alive, rank them as Decision Maker 1/2/3, find their mailing addresses.
7. **Filter out**: vacant land, business owners (LLCs without identifiable people), commercial properties, records missing required fields.

Output: ~150–250 cleaned + enriched records. Probate records have full DM data; foreclosure/LP/tax_sale records have basic owner data + property fields.

### Step 5 — ~8:15 AM: Skip trace + phone scoring

For each record without cached phones:

1. **Tracerfy** ($0.02/record) — pulls phones + emails for the contact (DM if deceased, owner otherwise)
2. **Trestle** ($0.015/phone) — scores each phone Tier 0–5:
   - Tier 0–1 = "Dial First" (highest connect probability)
   - Tier 2 = "Dial Second"
   - Tier 3 = "Dial Third"
   - Tier 4 = "Dial Fourth" (mail-only worthwhile)
   - Tier 5 = "Drop" (junk or litigator-flagged)

Records WITH cached phones (from prior runs) skip this step entirely — the cache carries forward both phones and tier scores.

### Step 6 — ~8:25 AM: Upload-skip filter + DataSift CSV

This is new (build 1.0.5). The pipeline now compares each record against its prior upload state:

- **Did anything material change today?** (status flip, new redemption tag, owner_deceased changed, new DM, new equity value, new phones)
- **YES** → queued for DataSift upload
- **NO** → skipped (already in DataSift with same data)

Records uploaded periodically refresh anyway (every 30 days) to prevent DataSift drift. Default behavior: only push records that actually changed.

Output: `WHO records DMs.csv` + `WHO records Heirs.csv` (probate-deceased with heir data) saved to the Apify Key-Value Store. Aaron + Mike download manually from the link in the Slack notification.

### Step 7 — ~8:30 AM: DataSift upload + skip trace + sequence trigger

Mike (or Aaron) uploads the CSV to DataSift. DataSift:

1. Auto-maps the columns (the CSV headers match DataSift's exact field names)
2. Dedups by property address — existing records get NEW tags merged onto them, no duplicate rows
3. Runs DataSift's built-in unlimited skip trace on every record (free with the partnership) — adds any phones we missed
4. Triggers any preset filter the record matches → that preset's sequence fires Day-0 actions

### Step 8 — ~8:45 AM: Slack daily report

The pipeline sends a Slack message summarizing:
- Total scraped, NEW, carried forward, aged out (dedup stats)
- Per-type and per-county breakdown
- Redemption window counts (open / closing)
- Estimated cost
- Run duration
- Direct links to download the DataSift CSVs

### Step 9 — ~8:50 AM: Master ledger CSVs → Drive

Three CSVs upload to the configured Google Drive folder, replacing the prior day's files in place:

| File | Purpose |
|---|---|
| `WHO_master_ledger_active.csv` | Every active record currently in state — Mike opens this in Sheets to cross-reference any record he's working |
| `WHO_master_ledger_daily_summary.csv` | Append-only — one row per day. Aaron opens this for trend analysis, weekly review. |
| `WHO_master_ledger_aged_out.csv` | Append-only — counts of records that aged out today (audit trail) |

### Step 10 — 9:00 AM: Mike opens DataSift

End of automation. Mike's day starts.

---

## 3. Why the records show up where they do

Every record arrives in DataSift with multiple TAGS, and tags drive which PRESET (filter view) the record appears in.

### The routing tags

| Tag | What it means | Preset that catches it |
|---|---|---|
| `ftm-probate` | Probate court filing | `FTM_Probate_Mont` / `_Franklin` / `_Greene` (#7/#8/#9) |
| `ftm-ss` | Sheriff sale (foreclosure auction) | `FTM_SS_Mont` / `_Franklin` / `_Greene` (#4/#5/#6) |
| `ftm-lp` | Lis pendens (foreclosure case filing pre-auction) | `FTM_LP_Mont` / `_Franklin` / `_Greene` (#1/#2/#3) |
| `ftm-rw` | Redemption window (post-sheriff-sale, pre-confirmation) | `FTM_RW_Mont` / `_Franklin` / `_Greene` (#10/#11/#12) — Mike builds these |
| `ftm-ts` | Tax sale | (no preset yet — these orphan today) |

### The status tags (signals)

| Tag | Meaning |
|---|---|
| `redemption_open` | Sale held, hearing not yet imminent |
| `redemption_closing` | Hearing within 14 days — DROP EVERYTHING URGENT |
| `redemption_closed` | Court confirmed, window gone |
| `deceased` | Owner confirmed dead via obituary |
| `living` | Owner alive (default for foreclosure / LP) |
| `dm_verified` | Decision-maker verified alive (probate) |
| `has_dm_address` | Decision-maker mailing address found |

### Phone tier tags

| Tag | Score range | What it tells Mike |
|---|---|---|
| `Dial First` | 81–100 | Highest priority — connect rate ~50%+ |
| `Dial Second` | 61–80 | Strong number, prioritize |
| `Dial Third` | 41–60 | Medium, dial after Tier 1–2 |
| `Dial Fourth` | 21–40 | Mail-only worthwhile |
| `Drop` | 0–20 | Don't dial — junk or litigator |

---

## 4. What Mike does after 9 AM (the post-upload workflow)

### Open DataSift in this exact order, every morning

1. **Slack first** — read the daily SiftStack report. Note: total records, breakdown, anything flagged. Confirm the report came in (if it didn't, that's a red flag — see section 6).

2. **Priority #1 — `FTM_RW_*` Redemption windows** (Mike's panic button)
   - `FTM_RW_Mont` (#10)
   - `FTM_RW_Franklin` (#11)
   - `FTM_RW_Greene` (#12)
   - Sort by `Redemption Days Remaining` ASC. Top 3–5 records are URGENT — these have ≤14 days until the court closes the window forever.
   - Day 0 SMS + ISA call within 1 hour of opening these. If `Redemption Days Remaining ≤ 7`, drop everything else.

3. **Priority #2 — `FTM_Probate_*`** (highest converting niche)
   - `FTM_Probate_Mont` (#7)
   - `FTM_Probate_Franklin` (#8)
   - `FTM_Probate_Greene` (#9)
   - These have FULL deep prospecting: decision maker name, relationship, confidence, mailing address (often different from the property). Probate is the highest-conversion niche per industry data.

4. **Priority #3 — `FTM_SS_*` Sheriff sale** (auction approaching)
   - Sort by `Foreclosure Date` ASC — auctions closest to today come first.

5. **Priority #4 — `FTM_LP_*` Lis pendens** (long nurture)
   - Sort by `Date Added` DESC. These have the longest runway — work last, slower cadence.

### How to actually work a record

For each record Mike opens:

1. **Read the tags row** — gives him the routing context (probate vs foreclosure, redemption window status, county, phone tier)
2. **Read the custom fields** — Decision Maker name, Decedent Name, Confirmation Hearing Date, Property Address, Estimated Value, etc.
3. **If phone tier is "Dial First" or "Dial Second"** → call directly via Mike's phone tool
4. **If phone tier is "Dial Third" or "Dial Fourth"** → SMS first per [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md), call only if engagement
5. **If response from SMS or call** → qualify via 4-Pillars in [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md). Tag `interested` if engaging, `hot` + escalate to Aaron via Slack if 4/4 qualified

### What's already automated (Mike does NOT do)

- **DialForce bulk dialers** are running their own cold dial process across the broader 60k-record list — that's separate from Mike. He doesn't manage it.
- **Mail (OpenLetter)** fires automatically off DataSift sequences when a record enters its preset cadence. Mike doesn't push the mail button.
- **Skip trace** is automatic via DataSift's built-in tool. Mike doesn't run it manually.
- **Sequence cadence triggers** (Day 1 SMS reminders, Day 3 follow-up, Day 5 mail) fire from DataSift sequences. Mike just executes the action when prompted.

---

## 5. What Mike validates each morning (5-minute audit)

Before working leads, do a quick sanity check that the data is healthy. If 4+ of the following pass, the morning's data is good — start working. If 3 or fewer pass, ping Aaron in Slack.

### The 7-point audit

1. **Slack daily report received** — should land between 8:30 and 9:15 AM. If not by 10 AM, escalate.

2. **Master ledger active CSV updated in Drive** — open `WHO_master_ledger_active.csv` in Drive, check the most recent date in `last_scraped` column. Should be today's date.

3. **Spot-check 5 records in `FTM_RW_*` (or other priority preset)** — each should have:
   - [ ] Routing tags present (`ftm-rw` for redemption, `ftm-ss` for sheriff sale, etc.)
   - [ ] County tag matches the preset
   - [ ] Property address populated
   - [ ] At least one phone field populated

4. **Probate records show DM data** — open one record in `FTM_Probate_*`. Should have:
   - [ ] Decision Maker name (≠ Decedent name)
   - [ ] DM Relationship (executor / spouse / son / daughter)
   - [ ] DM Confidence (high / medium / low)
   - [ ] Mailing Address (if confidence is high, this should be the DM's address NOT the property)

5. **Phone tier tags present** — every record with a Tracerfy match should have one of `Dial First` / `Second` / `Third` / `Fourth` / `Drop`. If a record has phones but NO tier tag, Trestle didn't run on it.

6. **Redemption fields populated on FTM_RW records** — every record in `FTM_RW_*` should have non-empty:
   - [ ] Sheriff Sale Held Date
   - [ ] Confirmation Hearing Date (sometimes blank if hearing not scheduled yet)
   - [ ] Redemption Days Remaining

7. **Existing presets adjusted** — open one record in `FTM_SS_Franklin` and confirm it does NOT have `ftm-rw` tag. If it does, either Mike forgot to add the exclusion or the filter is broken.

### Daily summary CSV — what to check weekly

Open `WHO_master_ledger_daily_summary.csv` in Drive (renders as Sheets). Each row is one daily run. Look for trends:

- **Run duration** should be ~30–45 min once dedup hits steady state (April 28 was 1h 57m — that was warmup day)
- **NEW records** should be ~30–60/day in steady state. If it spikes >150 multiple days in a row, dedup may have broken
- **Carried forward** should be 200–400/day in steady state
- **Probate count** should be 30–60/day. If 0 multiple days, the property_lookup pipeline broke
- **Redemption open + closing** should be growing 1–5/week as cases progress through the courts

---

## 6. Trouble signals — escalate to Aaron

Slack Aaron immediately if:

| What you see | What it means | Why it matters |
|---|---|---|
| No Slack daily report by 10 AM | Pipeline failed | Need to check Apify Console + restart |
| `FTM_RW_*` empty 2+ days in active foreclosure season | Redemption watcher broken | Window closes for real cases, deals lost |
| Probate count = 0 in daily summary | Property lookup broken (regression) | Probate is highest-converting niche; don't lose it |
| Phone tier tags missing on 50%+ records | Trestle scoring broken | ISA dials waste time without priority |
| Same record showing in 2+ presets it shouldn't | Filter exclusion broken | Mike does duplicate work |
| Spot-check fails on 3+ of 5 records | Data flow broken upstream | Block work until fixed |
| Master ledger active CSV missing or stale | Drive integration broken | Audit trail down |
| `aged_out` count is unusually high (>50/day) | Retention rules misfired | Records dropping prematurely |

---

## 7. The high-level picture (one sentence per piece)

- **SiftStack** = the Python pipeline that scrapes + enriches + uploads
- **notice_state.json** = SiftStack's memory of every record it's ever seen
- **DataSift** = the CRM where Mike works leads (the "front-end")
- **OpenLetter** = mail vendor integrated into DataSift sequences
- **DialForce** = bulk cold-call agency (free) running parallel to Mike
- **Trestle** = phone-tier scoring API
- **Tracerfy** = skip-trace API for finding phones
- **Anthropic Haiku** = the LLM doing obituary parsing + heir analysis
- **Apify** = the cloud platform running SiftStack on a daily 7 AM cron
- **Master ledger** = the 3 CSVs in Drive that audit everything

The whole thing is built so that **the operations team only interacts with DataSift**. Everything upstream is automated. Mike's job is to convert leads, not pull lists.

---

## See also

- [docs/MIKE-MASTER-GUIDE.md](MIKE-MASTER-GUIDE.md) — Mike's reference doc (more detailed than this walkthrough)
- [docs/MIKE-PRESET-BUILD.md](MIKE-PRESET-BUILD.md) — the immediate task: build the 3 redemption presets + adjust 6 existing
- [docs/SOP-CADENCES.md](SOP-CADENCES.md) — full preset filter logic + sequence specs
- [docs/SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — exact SMS templates Mike sends
- [docs/SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md) — mail piece templates (OpenLetter)
- [docs/SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — diagnostic playbook when things break
- [docs/MASTER-PLAN.md](MASTER-PLAN.md) — strategic context (Travis-facing)
