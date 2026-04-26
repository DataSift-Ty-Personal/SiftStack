# SOP — DataSift UI Navigation Guide

**Audience:** Mike (Data Manager) — daily DataSift workflow.
**Purpose:** Where to click, what to search for, how to find yesterday's records and apply preset filters. This is the "physical UI" companion to [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md).

---

## Login

URL: **https://app.reisift.io**

Credentials are in the password manager — reset via the standard reset flow if expired. Multi-device login is fine; the system maintains your session.

---

## The 5 places you'll be every day

### 1. Records (main workspace)

**Left sidebar → Records**

This is where you spend 80% of your time. Default view shows ALL records across ALL lists. Use **Filter Presets** (next section) to narrow to today's work.

**Key UI elements:**
- **Top search bar** — full-text search by name, address, or tag
- **List dropdown (top-left of grid)** — limit to one list at a time
- **Filter button (funnel icon)** — opens the filter panel for ad-hoc queries OR loading saved presets
- **Bulk action bar** (appears when you select records) — Manage / Send To / Delete
- **Status pills** (color-coded) — quick visual on where each record is in the pipeline

### 2. Filter Presets (Mike's primary daily tool)

**Records → Filter button (funnel icon) → Filter Presets section (scroll down) → Load**

You'll see a folder list. Mike's setup includes:

```
1. FTM_LP_Mont       2. FTM_LP_Franklin       3. FTM_LP_Greene
4. FTM_SS_Mont       5. FTM_SS_Franklin       6. FTM_SS_Greene
7. FTM_Probate_Mont  8. FTM_Probate_Franklin  9. FTM_Probate_Greene
+ 11 other folders
```

**To work today's leads:**
1. Click the funnel icon
2. Scroll to "Filter Presets" section, click to expand
3. Click `7. FTM_Probate_Mont` (or whichever county/type combo)
4. Click "Load"
5. The records grid now shows ONLY records matching that preset's filter criteria

**To clear:** Click "Clear" in the same Filter Presets section.

### 3. Activity Tab (background job tracker)

**Left sidebar → Activity**

This is where DataSift's background jobs live: uploads, enrichment runs, skip traces, sequence executions. After the daily 8:30 AM Slack ping, check Activity to confirm:
- "Upload completed" for both DMs and Heirs CSVs
- "Enrich Property Information" finished
- "Skip Trace" finished
- Any failed jobs have red status — investigate

### 4. Sequences (automated workflows)

**Left sidebar → Sequences**

Mike's setup includes 5 folders:
- DEEP PROSPECTING (4 sequences)
- default (4 sequences)
- DIRECT MAIL (sequences for the upcoming mail program)
- PROBATE SENSEI FLOW (probate-specific cadence)
- STATUS SEQUENCES (e.g., "Sold Property Cleanup")

**You don't need to manually run sequences.** They auto-fire when records hit their trigger conditions (e.g., a tag is added, a status changes). Your job is to know which sequence is doing what when you tag a record.

The **most important sequence to know:** "Sold Property Cleanup" (in Transactions/STATUS SEQUENCES folder). Triggered by adding the `Sold` tag → automatically removes record from active lists, clears tasks, clears assignee. **You add `Sold` tag → DataSift handles everything else.**

### 5. Lists (organizing records by category)

**Left sidebar → Records → top-left list dropdown**

Mike's lists include:
- **Foreclosure** — sheriff sale records (active)
- **Probate** — estate filings (active)
- **Tax Sale** — tax foreclosures (40 records/week from Montgomery sheriff portal, which serves both mortgage foreclosures AND treasurer's tax sales on the same page). **Action item:** Mike to create 3 new presets `FTM_TaxSale_Mont`, `FTM_TaxSale_Franklin`, `FTM_TaxSale_Greene` mirroring the Probate/SS structure but filtering on tag `ftm-ts` instead of `ftm-ss`/`ftm-probate`. Until then, search records by tag `ftm-ts` to find them.
- **SiftStack 2026-04-25 - DMs** — yesterday's autonomous run upload (decision-makers)
- **SiftStack 2026-04-25 - Heirs** — yesterday's run, secondary heir contacts
- Plus older bulk lists, named lists, etc.

Each daily run creates two new lists named `SiftStack YYYY-MM-DD - DMs` and `SiftStack YYYY-MM-DD - Heirs`. **Don't delete these** — they're the audit trail.

---

## Daily morning click-through (5 min)

1. **Login** — app.reisift.io
2. **Click Activity** — confirm last night's jobs completed cleanly
3. **Click Records** — should see today's date appear in recent records
4. **Click Filter (funnel) → Filter Presets → Load `7. FTM_Probate_Mont`** — work through Montgomery probate first
5. **Triage by phone tier:** sort by Phone 1 (DataSift's auto-skip-trace populates this) → work Tier 1+2 first
6. **As you contact each record:** add the appropriate tag (`sms_sent`, `called_day1`, `not_interested`, `hot`, etc.) — these tags drive next-day preset matching

**End of day click-through (3 min):**

1. **Filter today's records** with the `Sold` tag — apply Sold Property Cleanup if any
2. **Filter `hot` tag** — make sure those are escalated to Aaron
3. **Filter `callback_scheduled` tag** — verify the calendar dates are accurate

---

## Common UI tasks — exact clicks

### Add a tag to one record

1. Click the record to open detail view
2. Top-left, find "Tags" section
3. Click "+ Add Tag"
4. Type the tag name → press Enter
5. Tag appears in the record's tag list and sorts/filters update across the system

### Add a tag to multiple records (bulk)

1. In Records grid, check the box next to each record
2. Or use "Select all" at the top
3. Bulk action bar appears at bottom of screen
4. Click "Manage" → "Add Tag"
5. Type the tag → "Add"

### Update a record's status

1. Open the record
2. Click the colored status pill near the top (default: New)
3. Pick from: New / Contacted / Interested / Appointment / Offer / Under Contract / Closed / Dead / Sold
4. Status auto-saves

### Apply a different filter preset

1. Click funnel icon
2. If a preset is currently loaded, click "Clear" first
3. Filter Presets → expand folder → click new preset → Load

### Send a record to a sequence

1. Open the record
2. Click "Send To" → "Sequence"
3. Pick the sequence (e.g., "Probate Day 1 SMS")
4. Confirm — the sequence starts firing automatically based on its rules

### Find a specific person

- **By name:** top search bar → "John Smith" — searches across owner names, decision makers, decedents
- **By address:** top search bar → "123 Main St" — searches property + mailing addresses
- **By tag:** funnel → "Search for tags..." → type the tag → results filter live

### Bulk export records to CSV

1. Apply your filter (e.g., today's records by tag)
2. "Send To" → "Export"
3. Pick columns to include
4. Download CSV

---

## Things you should NOT do

- ❌ **Don't delete the daily SiftStack-{date}-DMs lists** — audit trail
- ❌ **Don't manually edit records that came from SiftStack** — your edits will be overwritten on the next run unless you tag them with something the pipeline preserves
- ❌ **Don't manually run skip trace on records that already have `skip_traced_YYYY-MM` tag** — wasted credits
- ❌ **Don't change preset filters without telling Aaron** — he tunes them based on data quality patterns
- ❌ **Don't add records to "Probate" or "Foreclosure" list manually** — the pipeline owns these. Use a personal list if you need to track manual entries.

---

## When something looks wrong

**See [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) for the diagnostic playbook.**

Quick reference:
- "Filter preset shows 0 records when I expected ~20" → tag drift, see Red Flags Section 4
- "DataSift won't let me upload" → see Red Flags Section 6
- "Records have no phone numbers" → see Red Flags Section 3

---

## Mobile vs desktop

**Use desktop for:** daily triage, bulk tagging, preset management, Activity review.
**Use mobile (DataSift mobile app) for:** quick status updates while on a call, tagging `hot` immediately after a positive conversation.

The mobile app sometimes lags behind desktop on UI features — if a button doesn't exist on mobile, switch to desktop.

---

## Keyboard shortcuts (desktop, Chrome/Safari)

| Shortcut | What it does |
|---|---|
| `/` | Focus search bar |
| `f` | Toggle filter panel |
| `j` / `k` | Next / previous record (when grid is focused) |
| `Enter` | Open selected record |
| `Esc` | Close detail view |
| `Cmd+K` (Mac) / `Ctrl+K` (Windows) | Quick search |

These are inconsistent across DataSift versions — verify they work in your build before relying on them.

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — what to do each morning
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — what to say
- [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — when something's broken
- [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) — what tags mean
