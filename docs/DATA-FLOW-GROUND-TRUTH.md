# Data Flow Ground Truth — Where Records Live at Every Stage

**Audience:** Aaron + Mike when confused about "where did my data go?"
**Purpose:** Single durable doc tracing every record from courthouse scrape → Mike's daily action.

---

## The 6 stages

```
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 1 — SCRAPED                                                       │
│  Where: Apify dataset (cloud) OR output/test_oh_*.csv (local)            │
│  Contains: Raw NoticeData — case # + address + auction date + names     │
└────────────────┬─────────────────────────────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 2 — ENRICHED                                                      │
│  Where: In-memory during pipeline run                                    │
│  Adds:   Smarty USPS validation, Zillow Zestimate + equity,              │
│          Obituary heir map, Tracerfy phones, Trestle phone tiers         │
└────────────────┬─────────────────────────────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 3 — FORMATTED                                                     │
│  Where: output/datasift_upload_DMs_{date}.csv (primary)                  │
│         output/datasift_upload_Heirs_{date}.csv (secondary contacts)     │
│  Contains: 41-column DataSift CSV with tags, lists, custom fields        │
└────────────────┬─────────────────────────────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 4 — UPLOADED TO DATASIFT (the central canvas)                     │
│                                                                          │
│  Each record lands in TWO places at once:                                │
│  • Wrapper list:  "SiftStack {date} - DMs"  (audit trail per upload)     │
│  • Notice-type list: Foreclosure / Probate / Tax Sale / Lis Pendens      │
│                                                                          │
│  Tags applied automatically:                                             │
│  • Always:    Courthouse Data, ftm                                       │
│  • By type:   ftm-probate / ftm-ss / ftm-lp / ftm-ts                     │
│  • By county: franklin / montgomery / greene                             │
│  • By date:   2026-04, 2026-05                                           │
│  • By status: living / deceased / has_heirs / has_dm_address             │
│  • By tier:   (Trestle phone scoring populates Phone 1-N fields)         │
└────────────────┬─────────────────────────────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 5 — FILTERED INTO MIKE'S PRESETS                                  │
│                                                                          │
│  Mike's 9 county-by-type FTM presets each combine:                       │
│    • List filter (Foreclosure / Probate / Lis Pendens)                   │
│    • Tag filter (ftm-ss / ftm-probate / ftm-lp)                          │
│    • County filter (Montgomery / Franklin / Greene)                      │
│    • Property structure type (single family residential)                 │
│                                                                          │
│  When Mike clicks "7. FTM_Probate_Mont" → he sees ONLY:                  │
│    Probate records, tagged ftm-probate, in Montgomery County             │
└────────────────┬─────────────────────────────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 6 — MIKE WORKS THE LEAD                                           │
│  • Triage by phone tier (Tier 0 "Dial Now" first)                        │
│  • SMS via DataSift's Launch Control / REISimpli                         │
│  • Phone calls (after caller is hired)                                   │
│  • Tag progressively: sms_sent → called_day1 → ... → hot OR not_interested│
│  • Hot leads → Slack ping to Aaron → Aaron closes                        │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## "I don't see data in my preset — what's wrong?"

Walk this checklist top-to-bottom. The first FALSE answers your question.

### Q1: Did today's scheduled run actually fire?

- Apify Console → Schedules → "Last fired" timestamp on the OH Daily Run schedule
- ✅ Yes (today's date) → continue to Q2
- ❌ No (older date) OR no schedule listed → schedule didn't fire / doesn't exist
  - **Fix:** Verify schedule is enabled with cron `0 11 * * *`. If missing, create it.

### Q2: Did today's run succeed?

- Apify Console → Runs → today's row → status column
- ✅ SUCCEEDED → continue to Q3
- ❌ FAILED / TIMED OUT / ABORTED → check the run log for the error
  - **Common:** RealAuction credential rotation, Tracerfy out of credits, DataSift password change

### Q3: Did the run actually upload to DataSift?

- Apify run log → search for "DataSift upload"
- Should see: "Uploading CSV 1/2 (DMs)..." then "Uploading CSV 2/2 (Heirs)..." then "DataSift upload: Uploaded 2/2 CSVs"
- ❌ If missing → DataSift Playwright login failed, or `upload_datasift` was off

### Q4: Did the upload land in DataSift?

- DataSift → Records → top search bar → type today's date in MM/DD/YYYY format
- Should see records with today's date
- ❌ Empty → upload reported success but records didn't materialize. Check DataSift Activity tab for queued enrichment jobs

### Q5: Are the tags correct on a sample record?

- DataSift → click any of today's records → Tags field
- Should include: `Courthouse Data`, `ftm`, `ftm-{type}`, `{county}`, `{YYYY-MM}`
- ❌ Missing tags → CSV column mapping issue during upload (Tags column wasn't mapped)

### Q6: Does Mike's preset filter match?

- Open Mike's preset (e.g., `7. FTM_Probate_Mont`)
- Click the gear/edit icon to see filter criteria
- Cross-reference with what Stage 4 applied:
  - Tag filter must include `ftm-probate` (not `ftm-lp` — known copy-paste error from before our migration)
  - County filter must be `Montgomery`
  - List filter must include `Probate`
- ❌ Filter says `ftm-lp` for a Probate preset → fix it: change to `ftm-probate`

---

## "Where is record X right now?"

To find any specific record (e.g., "147 Betz Rd Columbus"):

1. **DataSift search bar:** type `147 Betz` — searches across address, mailing addr, owner names
2. **If found** → click the record → look at:
   - Lists membership (top of detail panel)
   - Tags (Tags section)
   - Status (colored pill)
   - Notes (full enrichment summary including obituary, heirs, phones)
3. **If not found** → record was never uploaded. Possible reasons:
   - The source scraper (Franklin foreclosure / RealAuction) didn't run
   - The record fell into vacant land filter / entity filter before upload
   - Today's scheduled run aborted

---

## "Why doesn't my data look like Saturday's?"

When the daily run is healthy, you should see:

- ~80-150 new records per day (dedup against master ledger removes repeats)
- Mix of probate (~30%), foreclosure (~50%), tax sale (~15%), LP (~5%) typical
- ~70-80% mailable (DPV match Y, name parsed, address present)
- ~10-20% deceased records with completed heir maps
- Phone tiers should distribute roughly: 10% Dial Now, 30% Dial First, 30% Dial Second, 20% Dial Third, 10% Drop

If today's batch is wildly outside these ranges, something upstream changed. See [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md).

---

## "Where do master ledger CSVs live?"

The master ledger (added in commit 9a0ed6f) is the cross-run dedup memory:

- `output/master_ledger_*.csv` — cumulative record of every notice ever scraped
- Used by daily runs to skip records that already uploaded — ONLY new + changed records reach DataSift
- This is why the daily count drops over time as the same records age out

Mike doesn't need to look at this; it's automatic.

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — Mike's morning playbook
- [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) — exact tag-by-tag breakdown
- [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — diagnostic playbook
- [CLAUDE.md](../CLAUDE.md) — full operational reference
