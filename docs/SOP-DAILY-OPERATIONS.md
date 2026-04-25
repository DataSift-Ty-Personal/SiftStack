# SOP — Daily Operations (Mike's Morning Playbook)

**Audience:** Data Manager (Mike) and Owner (Aaron).
**Purpose:** Everything that happens between 6:00 AM and 9:00 AM every day, what you should see when you walk in, and the exact daily DataSift workflow.

---

## The 3-hour automation window (no human required)

Every morning the SiftStack Apify Actor wakes at **6:00 AM ET** and runs unattended. By the time you start at 9:00 AM, all of this has already happened in the cloud:

```
06:00 AM  Apify Actor wakes
06:00–06:30  Scrape 6 Ohio portals (Franklin / Montgomery / Greene × probate / foreclosure)
06:30–07:00  Dedupe + run 10-step enrichment pipeline:
              - Smarty (USPS validation, ZIP+4, geocode, vacancy detection)
              - OH County Auditor (probate property lookup — fills decedent's address)
              - Zillow / OpenWebNinja (Zestimate, MLS status, equity, beds/baths/sqft)
              - Obituary search (DOD, heir map, decision-maker ranking, DOD sanity check)
              - Tracerfy skip trace (phones + emails for executor + heirs)
              - Trestle phone scoring (5-tier dial priority)
07:00–07:30  Generate per-record PDF deep-prospecting reports (deceased / heir / DM cases)
07:30–07:45  Format 41-column CSV, upload to DataSift via Playwright
07:45–08:15  DataSift runs Enrich Property Info + Skip Trace in background
08:15–08:30  Final tag verification + list assignment
08:30 AM     Slack/Discord summary posted to your channel
```

**By 8:30 AM** new Ohio leads are sitting in DataSift, tagged, listed, skip-traced, phone-tier-scored, with PDF deep-prospecting reports attached. Your laptop does not need to be on.

---

## Where to look first thing (in order)

### 1. Slack/Discord channel — your 8:30 AM summary

The morning post is the single source of truth. It looks like this:

```
🌅 SiftStack Ohio Daily — 2026-04-27

📊 Summary:
  47 new records (22 probate, 25 foreclosure)
  8 deceased owners with completed heir maps
  $4.18 enrichment spend (Tracerfy + Trestle)

📍 By county:
  Franklin     21 records  (12 probate, 9 foreclosure)
  Montgomery   18 records  (8 probate, 10 foreclosure)
  Greene        8 records  (0 probate*, 8 foreclosure)
                            *portal license disabled at source

📞 Phone tiers (decision-makers only):
  Tier 1 (81-100, Dial First):  19  ⭐
  Tier 2 (61-80, Dial Second):  14
  Tier 3 (41-60, Dial Third):    8
  Tier 4 (21-40, Dial Fourth):   4
  Tier 5 (0-20, Drop):           2

🏆 Top 5 by phone score:
  1. PATRICIA CRIDGE (Montgomery probate) — DM: JONATHAN CRIDGE — 95
  2. WILLIAM LONG (Montgomery probate) — DM: JACQUELINE LONG — 92
  3. ...
```

**If you don't see this post by 8:45 AM, see [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — Section 1.**

### 2. DataSift — `app.reisift.io/dashboard`

Login. You should see:

- New records in **Probate** list (today's date)
- New records in **Foreclosure** list (today's date)
- All records have `Courthouse Data` tag (this is the niche sequential trigger)
- Deceased records have `deceased` tag + DM confidence tag (`high_confidence` / `medium_confidence` / `low_confidence`)

### 3. Reports folder — `output/reports/` (local) or Google Drive (if configured)

PDF deep-prospecting reports for each deceased / heir / DM record. Open the highest-priority leads (Tier 1 phone tiers) and skim the signing chain before your first call.

---

## Mike's daily workflow

Once you've read the Slack summary and done your DataSift triage, here's the standard order of operations.

### Step 1 — Triage (8:30–9:00 AM)

In DataSift, filter to **today's new records** and sort by phone tier.

- **Tier 1 (81-100)**: SMS today, follow with phone today
- **Tier 2 (61-80)**: SMS today, follow with phone tomorrow
- **Tier 3 (41-60)**: SMS first, phone if no response in 48 hrs
- **Tier 4 (21-40)**: Mail-only sequence
- **Tier 5 (0-20)**: Drop, do not contact (litigator risk or junk number)

### Step 2 — Niche sequential setup (9:00–9:30 AM)

The 12 niche sequential presets handle the SMS → Call → Mail → Deep Prospecting cadence automatically. Today's records get picked up by:

- **00. Needs Skipped** — if any records lack phones (rare with Tracerfy)
- **01. SMS Day 1** — Tier 1 + Tier 2 records → SMS goes out
- See [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) for the full preset → record matching map

Verify each preset has the expected count:

```
DataSift → Records → Filter → Apply Preset "01. SMS Day 1"
```

If counts look wrong (e.g., 0 records when Slack said 19 Tier 1s), see [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — Section 4 (Tag Drift).

### Step 3 — High-value lead review (9:30–10:30 AM)

Open PDFs for the top 5 records by phone tier. Each PDF contains:

- Property summary (address, Zestimate, equity, beds/baths)
- Owner / decedent info
- Signing chain (PR/executor + ranked heirs with relationships)
- All phones with tiers + emails
- Deep-prospecting notes (DOD, obituary URL, heir verification status)

Read these BEFORE you make the first call — knowing the signing chain prevents wasted calls to the wrong person.

### Step 4 — Cold outreach (rest of morning)

Standard niche sequential cadence. The system has already pre-loaded scripts and templates in DataSift's **26 TCA sequence templates** (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4).

For probate calls:
- **Always speak to the PR/executor**, never the decedent
- **Open with the property** ("I saw you're handling [decedent]'s estate, and I noticed they had a property at [address]")
- **The PR may not have visited the property recently** — be prepared to explain condition / value

For foreclosure calls:
- **Speak to the homeowner (defendant)**, not the bank (plaintiff)
- **Sale date is in the record** — match urgency to days-until-auction
- **Ohio is judicial foreclosure** — there's a redemption period after the sheriff sale, more time than non-judicial states

### Step 5 — Update DataSift status throughout the day

Move records through the 9-status pipeline:
- **New → Contacted → Interested → Appointment → Offer → Under Contract → Closed → Dead → Sold**

Tag pattern enforced by the system:
- `contacted_YYYY-MM-DD` when first contact made
- `interested` when they engage positively
- `appointment_YYYY-MM-DD` when calendar booked

These tags drive the next-day filter presets — don't skip them.

### Step 6 — End-of-day cleanup (4:30–5:00 PM)

Run the **Sold Property Cleanup** sequence (in Transactions folder) — it auto-fires on the `Sold` tag and:
- Changes status to Sold
- Removes from active lists
- Clears assigned tasks
- Clears assignee

If you closed any deals today, manually tag `Sold` on those records before logging off.

---

## What you do NOT need to do

The system handles all of this — don't manually do it:

- ❌ Pull data from courthouse websites (the 6 scrapers do this)
- ❌ Look up property addresses for probate decedents (the OH Auditor lookup does this)
- ❌ Standardize addresses or check ZIP+4 (Smarty does this)
- ❌ Search obituaries for DOD or heirs (obituary enricher does this, with 3-year DOD sanity check)
- ❌ Manually skip-trace phones / emails (Tracerfy does this)
- ❌ Score phones for tier priority (Trestle does this)
- ❌ Generate PDF deep-prospecting reports (the report generator does this)
- ❌ Format the DataSift upload CSV (the formatter does this — 41 columns)
- ❌ Manually create lists or tags in DataSift (auto-created from CSV column data)

If you're spending more than 15 minutes/day on any of the items above, **that's a red flag** — see [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md).

---

## Manual override paths (when you need them)

You should rarely need these, but they exist:

### Run a single source manually
```bash
cd /Users/aaron/Desktop/SiftStack
source venv/bin/activate
PYTHONPATH=src python -m scrapers.oh_montgomery_probate --days 7
```

### Run the full daily pipeline manually (catches missed run)
```bash
python src/main.py daily --upload-datasift --notify-slack
```

### Re-enrich an existing CSV (e.g., before pushing to DataSift after a network blip)
```bash
python src/main.py csv-import --csv-path output/some_file.csv --csv-county Montgomery
```

### Scrape historical data (if onboarding new market or backfilling)
```bash
python src/main.py historical --counties Franklin --types probate --upload-datasift
```

---

## Daily checklist (printable)

```
☐  8:30 AM — Slack summary received
☐  8:45 AM — Open DataSift, verify today's record counts match Slack
☐  9:00 AM — Phone-tier triage complete (Tier 1 + 2 prioritized)
☐  9:30 AM — Niche sequential presets verified (counts match)
☐  10:30 AM — Top 5 PDFs reviewed
☐  Cold outreach started by 11:00 AM at the latest
☐  DataSift status updated as conversations progress
☐  4:30 PM — Sold Property Cleanup sequence run if any deals closed
☐  5:00 PM — End of day, log out
```

**If any morning step fails or the count looks off, [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) tells you what to check and who owns the fix.**
