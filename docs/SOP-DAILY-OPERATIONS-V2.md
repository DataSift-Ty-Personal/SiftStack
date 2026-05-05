# SiftStack Daily Operations — Mike's Daily Workflow (V3)

**Replaces:** all prior daily operations SOPs.
**Audience:** Mike (data manager) + Aaron (owner) + Kareem (ISA, downstream).
**Last updated:** 2026-05-05 — Mike-runs-DataSift architecture.

---

## What this stack actually does

**SiftStack delivers clean per-bucket CSVs to Mike every morning.** That's it. Everything that happens AFTER the CSVs land is Mike's workflow in DataSift.

DataSift has no API. Even DataSift's own owner uses a data manager for CSV upload + tagging — Mike does the same here. We tried full automation; it kept fighting DataSift's UI. So we stopped fighting. SiftStack handles the actually-hard parts (scrape, enrich, dedup) autonomously and hands Mike clean CSVs.

---

## Daily flow (autonomous side)

```
06:00 AM  Apify Actor wakes (cloud)
   ↓
06:00–07:30  Scrape 5 OH portals + enrich + Tracerfy phones + obituary research
   ↓
07:30–07:35  Persistent dedup (skip addresses already in our 250+ uploaded set)
   ↓
07:35–07:40  Split into per-(notice_type, county) bucket CSVs
   ↓
07:40       Save 5-9 CSVs to Apify KVS + Master Ledger Sheet update
   ↓
07:45       Slack ping with download links + deep-prospect summary
   ↓
~09:00 AM   ← MIKE STARTS
```

**SiftStack's job ends at the Slack ping.** No auto-upload, no auto-tagging.

---

## Mike's daily workflow (~30 min total)

### Step 1 — Read Slack ping (1 min)

8:30-9:00 AM. Open Slack, find the daily SiftStack post. Confirm:
- Total scraped looks reasonable (50-300 normal range)
- County breakdown present
- Deep prospecting section lists HIGH-confidence DM addresses by name
- 5-9 bucket CSV download links present in the follow-up message

If anything looks broken (no Slack ping, 0 records, error messages) → ping Aaron, hold the day.

### Step 2 — Download all bucket CSVs (2 min)

The Slack ping has links like:
```
inbox: Mike — today's bucket CSVs ready for upload:

  • foreclosure-franklin.csv — 13 records
  • foreclosure-greene.csv — 9 records
  • foreclosure-montgomery.csv — 5 records
  • lis_pendens-franklin.csv — 31 records
  • probate-franklin.csv — 28 records
```

Click each link → download to laptop. Total of 5-9 small CSVs.

### Step 3 — Upload each CSV to DataSift (~15 min for 5-9 buckets)

For EACH downloaded CSV, run DataSift's upload wizard:

1. DataSift → **Upload File** → Add Data → "Uploading a new list not in DataSift yet"
2. **List name**: copy the bucket name with date prefix:
   - `SiftStack 2026-05-05 - foreclosure-franklin`
   - `SiftStack 2026-05-05 - probate-franklin`
   - etc. (one per CSV)
3. Skip through Tags step (we'll bulk-tag after upload)
4. Upload File: drop the bucket CSV
5. Map Columns: address fields auto-map; **drag Tags column → "Tags" field, drag Lists column → "Lists" field**. If Step 4 silently fails on Tags/Lists (which it sometimes does), don't worry — we bulk-tag after.
6. Finish Upload

Repeat for each bucket CSV. **DataSift dedups by address** — if a record was uploaded before, the new upload merges with the existing record (adds the new wrapper list to its memberships).

### Step 4 — Bulk-tag per wrapper list (~10 min for 5-9 buckets)

Step 3's Tags column mapping is unreliable. Always do this bulk-tag pass after upload to guarantee tags are applied:

For each wrapper list you just created:

1. Records → Filter → `List` IS `<wrapper list name>` → Apply
2. Sanity check: count matches the upload
3. Select all matching
4. **Manage → Add Tags** → enter the 3 tags from the table below
5. **Manage → Add to List** → enter the list name from the table below

| Wrapper list pattern | Tags to add | Add to list |
|---|---|---|
| `... - foreclosure-franklin` | `ftm`, `ftm-ss`, `franklin` | `Foreclosure` |
| `... - foreclosure-montgomery` | `ftm`, `ftm-ss`, `montgomery` | `Foreclosure` |
| `... - foreclosure-greene` | `ftm`, `ftm-ss`, `greene` | `Foreclosure` |
| `... - lis_pendens-franklin` | `ftm`, `ftm-lp`, `franklin` | `Lis Pendens` |
| `... - lis_pendens-montgomery` | `ftm`, `ftm-lp`, `montgomery` | `Lis Pendens` |
| `... - lis_pendens-greene` | `ftm`, `ftm-lp`, `greene` | `Lis Pendens` |
| `... - probate-franklin` | `ftm`, `ftm-probate`, `franklin` | `Probate` |
| `... - probate-montgomery` | `ftm`, `ftm-probate`, `montgomery` | `Probate` |
| `... - probate-greene` | `ftm`, `ftm-probate`, `greene` | `Probate` |

Skip any wrapper list that doesn't appear today (some buckets are 0-record on quiet days).

### Step 5 — Trestle phone tier scoring (~10 min, until 1.0.15 ships)

Background: SiftStack runs Trestle on deep-prospect records (deceased owners) automatically. Living-owner foreclosure / lis pendens records have phones from Tracerfy but no tier badge — Kareem can't prioritize calls without tier scoring.

Workflow until 1.0.15 (Trestle for all records pre-upload) ships:

1. **Export today's living-owner records:**
   - Records → Filter → `Date Added` IS today → `Owner Deceased` IS NOT yes
   - Manage → Export → Phone enrichment CSV → Download

2. **Run Trestle scoring locally** (Aaron set this up on Mike's laptop):
   ```
   PYTHONPATH=src python -m phone_validator --csv-path <downloaded>.csv --output scored.csv
   ```
   - Cost: ~$0.015/phone × ~3 phones/record × ~80 records = ~$3.60/day
   - Adds tier columns to the CSV

3. **Reimport scored CSV:**
   - DataSift → Upload File → Add Data
   - List name: `Trestle Scored 2026-05-05`
   - Drop scored.csv
   - DataSift dedups by address → phones merge in with their tier badges
   - Result: every record now has phone tier badges visible

4. **Verify a few records visually:**
   - Open 2-3 records → phone numbers section should show tier badges (color-coded icons next to each phone)

### Step 6 — Verify FTM presets populated (2 min)

For each FTM preset that should have today's records:

- `FTM_Probate_Franklin` → today's probate-Franklin count
- `FTM_SS_Mont` → today's foreclosure-Montgomery count
- `FTM_LP_Franklin` → today's lis-pendens-Franklin count
- ...etc

If any FTM preset shows 0 records when the bucket exists today → bulk-tag step 4 missed that bucket. Re-tag.

### Step 7 — Daily summary to Aaron (3 min)

WhatsApp Aaron the daily recap:

```
Daily SiftStack Summary — 2026-05-05
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓ Apify run: 174 scraped, 89 new (after dedup)
✓ Buckets uploaded: 5/5
   - probate-franklin (28)
   - lis_pendens-franklin (31)
   - foreclosure-franklin (13)
   - foreclosure-greene (9)
   - foreclosure-montgomery (5)
✓ Tags applied: 5/5 buckets
✓ Trestle scored: 87 records ($3.42)
✓ FTM presets verified
🔥 Top deep-prospects (HIGH confidence): 14 addresses
   (see Slack ping for names)
Issues: <none / list any>
Records ready for Kareem: 89
```

### Step 8 — Master Ledger Sheet update (passive — no Mike action)

The Master Ledger Sheet auto-updates every Apify run. Mike's `mike_status` and `mike_notes` columns are preserved across re-uploads (the Apps Script upserts by address). No action needed unless Aaron asks Mike to spot-check.

---

## What Kareem (ISA) gets after Mike's done

Mike's done at ~9:30 AM. Kareem starts ISA dial blocks per [ISA/ISA-DAILY-PLAYBOOK.md](ISA/ISA-DAILY-PLAYBOOK.md):

- Every new record in its FTM_*_County preset
- Every record has phone tier badges (Tier 0 best mobile → Tier 5 drop)
- Deep-prospect records flagged via `dm_verified` + `has_heirs` tags
- Master Ledger Sheet shows the day's intake

---

## County coverage / volume planning for Kareem

Estimated daily volume (based on 5/5 baseline):
- Franklin: ~75 records/day (probate 28, foreclosure 13, lis pendens 31, plus bulk)
- Montgomery: ~10 records/day (foreclosure 5, plus probate when active)
- Greene: ~10 records/day (foreclosure 9, plus probate when active)
- **Total FTM-priority queue: ~95 records/day**

ISA capacity reality:
- Power-dial cruise: 100-120 dials/day (per ISA-DAILY-PLAYBOOK)
- Connect rate: 25-30%
- Conversations: ~30/day
- Appointments target: 2/day (10/week KPI floor)

**Bottom line:** Kareem can handle all 3 counties at current volume on FTM workflow. Bulk Top-250 covers afternoons. Franklin Lis Pendens is the bulk of the call queue — prioritize there.

If volume scales beyond 150 records/day → focus on Franklin only, defer Montgomery/Greene to weekly sweeps.
If beyond 250 records/day → need second ISA, split by county.

---

## Validation gates — what each role validates and when

| Gate | When | Who | What |
|---|---|---|---|
| Apify run completes | 8:30 AM | System (Slack) | Scrape + CSV gen finishes without errors |
| Bucket CSVs delivered | 8:30 AM | Mike | Slack ping shows 5-9 download links |
| All buckets uploaded | 9:15 AM | Mike | DataSift Activity tab shows N Complete entries |
| Tags applied | 9:25 AM | Mike | Each wrapper list selected → records have ftm-* + county tags |
| Trestle scored | 9:35 AM | Mike | Records show phone tier badges |
| FTM presets populated | 9:40 AM | Mike | Each FTM_*_County preset count matches expected |
| Daily summary sent | 9:45 AM | Mike | Aaron receives WhatsApp recap |
| ISA AM dial block starts | 9:30 AM (Tue-Thu) | Kareem | Top 2 phone-tier records per preset dialing |
| EOD ISA report | 5:30-6:00 PM | Kareem | Group text: dials, connects, appointments |

---

## Quick-reference daily checklist for Mike

Print this. Tape it next to monitor.

- [ ] 8:30 AM — Read Slack ping
- [ ] 8:35 AM — Download all bucket CSVs (5-9 files)
- [ ] 8:40 AM — Upload bucket 1 to DataSift (wrapper list name = bucket name with date prefix)
- [ ] 8:45 AM — Upload bucket 2
- [ ] 8:50 AM — Upload bucket 3
- [ ] 8:55 AM — Upload bucket 4
- [ ] 9:00 AM — Upload bucket 5 (and beyond if applicable)
- [ ] 9:05 AM — Bulk-tag wrapper list 1 (filter by list, add 3 tags, add to type list)
- [ ] 9:08 AM — Bulk-tag wrapper list 2
- [ ] 9:11 AM — Bulk-tag wrapper list 3
- [ ] 9:14 AM — Bulk-tag wrapper list 4
- [ ] 9:17 AM — Bulk-tag wrapper list 5
- [ ] 9:20 AM — Export today's non-deceased records → run Trestle locally
- [ ] 9:30 AM — Reimport scored CSV
- [ ] 9:35 AM — Verify FTM presets populated (3-5 spot checks)
- [ ] 9:40 AM — WhatsApp Aaron the daily summary
- [ ] 9:45 AM — Kareem starts ISA dial blocks

---

## What Aaron is committing to fix this week (reduce Mike's manual time)

1. **1.0.14 (deployed today)** — Auto-upload disabled. CSV-only delivery to Mike. Reduces Mike's morning anxiety; eliminates fragile Playwright code.

2. **1.0.15** — Trestle for all records pre-upload (not just deep prospects). Eliminates Mike's Step 5 entirely. Cost ~$200/month additional. Net Mike's morning: drops to ~15 min.

3. **1.0.16** — Drive folder for deep-prospect PDFs. Permanent backup so Mike doesn't lose research after Apify KVS expires (~14 days).

4. **Mike's tagging tool** — A small browser extension or DataSift macro that bulk-tags + adds-to-list per wrapper list with one click. Eliminates Step 4 entirely. Net Mike's morning: ~5 min.

---

## Why this is a stable architecture

- SiftStack does the autonomous parts that need to be autonomous (scrape, enrich, dedup, bucket-split). All proven and reliable.
- Mike does the parts that genuinely benefit from human-in-the-loop (DataSift quirks, tag judgment, Trestle export/reimport). Reliable in his hands.
- No fragile Playwright UI automation in the critical path.
- When DataSift updates their UI, SiftStack doesn't break (we don't drive their UI). Mike adapts in real-time.
- When SiftStack has a scrape issue, Mike sees it immediately in the Slack ping (counts low, missing buckets) and can investigate or escalate to Aaron.

This is the operating model going forward.
