# SOP — Mike's Daily KPI Tracker & Rolling Log

**Audience:** Mike (data manager, SMS outreach) + Aaron (owner reviewing performance).
**Purpose:** Define exactly what Mike needs to hit each day on SMS + data processes, capture it in a rolling log, surface trends weekly. **No outbound calls in scope** — Mike's lane is SMS + DataSift hygiene.

---

## What Mike does (the lanes)

Mike has **two responsibilities**:

1. **SMS outreach** — work the daily batch through Day 1/2/3 cadence per preset
2. **Data process oversight** — verify the autonomous run produced clean records each morning, flag issues, action red flags from [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md)

That's it. He doesn't dial, he doesn't appraise, he doesn't negotiate. Hot leads ping Aaron via Slack. Aaron handles calls + closes.

---

## Daily KPI targets

### Input KPIs — SMS (non-negotiable daily)

| KPI | Daily target | Why |
|---|---|---|
| **SMS Day 1 sent** | 100% of qualifying new records (Tier 0-2 phones) | Every new record gets Day 1 outreach within 24 hrs |
| **SMS Day 2 sent** | 100% of records on Day 2 cadence | Per [SOP-OUTREACH-CADENCE.md](SOP-OUTREACH-CADENCE.md) |
| **SMS Day 3 sent** | 100% of records on Day 3 cadence | Same |
| **Records tagged after SMS** | 100% (`sms_sent_day1`, `sms_sent_day2`, `sms_sent_day3`) | Drives next-day cadence; if not tagged, breaks |
| **Replies tagged + responded** | 100% same-day | Use response handlers from [SMS V2 §"Response handlers"](SOP-SMS-TEMPLATES-V2.md) |

### Input KPIs — Data process oversight (daily)

| KPI | Daily target | Why |
|---|---|---|
| **Verify autonomous run landed** | Daily — within 1 hr of starting work | Catches Apify failures fast |
| **Slack summary received** | Should land 10-12 AM EDT after autonomous run | If missing → check Apify Console |
| **Today's records visible in DataSift** | Yes | Confirms upload + tagging worked |
| **Mike's preset filters return non-zero records** | Yes | Validates tag flow ([SOP-TAG-FLOW.md](SOP-TAG-FLOW.md)) |
| **Red flags noted in log** | All | Per [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) §1-12 |

### Outcome KPIs (system + Mike's quality drive these)

| KPI | Daily target | Weekly target |
|---|---|---|
| **SMS reply rate** | 8-15% of Day 1 SMS sent | track per variant A/B/C |
| **Positive replies** (interested/question, NOT stop/no) | 2-5% of Day 1 SMS sent | 5-15/week |
| **Hot leads escalated to Aaron via Slack** | 1-3/day | 5-15/week |
| **Records moved to "Interested" status** | Variable | 3-8/week |

### Stop-loss KPIs (red flags requiring escalation)

| Metric | Threshold | Action |
|---|---|---|
| **SMS reply rate** | < 5% for 3 consecutive days | A/B test new variants from [SMS V2](SOP-SMS-TEMPLATES-V2.md) |
| **Positive reply rate** | < 1% for 5 consecutive days | Data quality audit (preset filters, phone tiers, names) |
| **Records with zero touches at Day 7+** | Any | Rolling log gap → meeting with Aaron |
| **Autonomous run fails 2+ days in a row** | Yes | Escalate to Aaron, work from CSVs manually until fixed |

---

## Conversion funnel — the math Mike works toward

```
Daily autonomous run produces:    ~100 new records (target steady state)
                                    ↓
Mike's Day 1 SMS goes to:           ~75 records (Tier 0-2 phones, skip 4-5)
                                    ↓
Reply rate at 10%:                  ~7 replies
                                    ↓
Positive replies at 30% of replies: ~2 interested per day
                                    ↓
Hot leads tagged + escalated:       ~1.5/day = 7-10/week
                                    ↓
Aaron's calls + closing:            (out of Mike's lane)
```

**Mike's job ends at "hot lead tagged + Slack ping to Aaron".** Everything downstream is Aaron's lane.

If Mike hits ~75 SMS/day and reply rates stay above 8%, the funnel produces deals on the back end. If reply rates drop, swap A/B variants per SMS V2.

---

## The Rolling Log — Mike's daily journal in DataSift terms

A rolling log is **one row per day, accumulating week over week**. Mike adds a row at end of day. Aaron reviews weekly.

### Why a rolling log vs traditional KPI dashboard

- **Traditional dashboard:** "this week vs last week" — loses day-level texture
- **Rolling log:** every day shows up as a row, with quantitative + qualitative entries
- **Pattern detection:** if Tuesdays consistently underperform, the log shows it
- **Audit trail:** months later, "what happened on April 30" is one cell lookup

### Columns Mike fills out daily

| Column | What goes in |
|---|---|
| **Date** | YYYY-MM-DD |
| **Day of week** | Mon / Tue / etc. |
| **Autonomous run status** | ✓ landed / ✗ failed / ⚠ late / ⚠ partial |
| **New records added today** | Count from Slack summary or DataSift filter |
| **SMS Day 1 sent** | Count |
| **SMS Day 2 sent** | Count |
| **SMS Day 3 sent** | Count |
| **Total SMS sent today** | Sum of above |
| **Replies received** | Count (any reply, including STOP) |
| **Positive replies** | Count (interested / question, NOT not_interested or stop) |
| **STOPs** | Count (TCPA opt-outs) |
| **Hot leads tagged** | Count of records Mike tagged `hot` |
| **Records moved to Interested status** | Count |
| **Issues observed** | Free text (e.g., "Greene LP returned 0 again", "Tier 5 was 22% of batch") |
| **Issues resolved** | Free text (e.g., "Tagged orphan tax_sale records manually") |
| **Notes for Aaron** | Free text — anything Aaron should know |

That's 14 columns + notes — should take Mike ~5 min to fill in at end of day.

---

## The Google Sheet structure

**Sheet name:** `SiftStack Rolling Log + KPIs`
**Owner:** Aaron, shared edit with Mike

### Tab 1 — `Daily Log` (Mike fills daily)

The 14 columns above. One row per day. Pre-populated with date column for next 90 days so Mike just fills in the data.

### Tab 2 — `Weekly Rollup` (auto-formulas, no editing)

Aggregates Tab 1 into Monday-Sunday weekly buckets. Columns:

| Week of (Mon) | Records added | Total SMS | Replies | Reply rate | Positive replies | Pos rate | Hot leads | Run failures (count) | Top 3 issues |
|---|---|---|---|---|---|---|---|---|---|

Aaron's Friday review surface.

### Tab 3 — `Per-Preset Conversion` (auto-formulas)

For each of Mike's 10 active presets, weekly:

| Preset | SMS sent (week) | Replies | Reply rate | Positive | Pos rate | Hot leads | Best variant (A/B/C) |
|---|---|---|---|---|---|---|---|

Tells us which segments perform. Reallocate Mike's time toward winners. Drop or refresh losers.

### Tab 4 — `Issues Tracker` (rolling, with status)

Issues Mike notes in Tab 1 get pulled here for tracking until resolved:

| Date logged | Issue | Severity | Owner | Status | Date resolved |
|---|---|---|---|---|---|

Severity: `low` (note for Friday review), `medium` (review this week), `high` (Aaron same-day).

---

## Concrete daily targets per preset volume

Assuming steady-state ~100 new records/day from autonomous run:

```
Probate (~20 records/day, all 3 counties)
  Day 1 SMS:    18 records (skip 2 entity-owned)
  Reply target: 2-3 (probate has highest emotional weight, ~12% reply)
  Positive:     1 (~5% positive)

Sheriff Sale (~50 records/day, 3 counties)
  Day 1 SMS:    40 records (skip Tier 4-5 + commercials)
  Reply target: 4-6 (urgency drives ~10-12% reply)
  Positive:     1-2 (~3-5% positive)

Lis Pendens (~25 records/day once presets populating)
  Day 1 SMS:    20 records
  Reply target: 1-2 (early-stage, ~6-8% reply)
  Positive:     1 (~5% positive)

Tax Sale (~10 records/day, Mont only currently)
  Day 1 SMS:    8 records
  Reply target: ~1 (~10% reply)
  Positive:     0-1

Redemption Window (sporadic, 0-5 records when present)
  Day 1 SMS:    5 records when present
  Reply target: 1+ (~15% reply — high motivation)
  Positive:     1 (~10% — very high conversion)

DAILY TOTAL TARGETS:
  SMS sent:        ~70-90/day
  Total replies:   ~9-13/day (12% blended reply rate)
  Positive replies: ~3-5/day
  Hot leads escalated: 1-3/day
```

---

## Mike's morning checklist (9-11 AM)

```
☐ 9:00 AM — Open Slack, confirm overnight run summary landed
☐ 9:05 AM — Open DataSift, verify today's records visible (search tag = today's date)
☐ 9:10 AM — Apply preset 7 FTM_Probate_Mont → Day 1 SMS to Tier 0-2 (V2 templates)
☐ 9:25 AM — Apply preset 8 FTM_Probate_Franklin → Day 1 SMS
☐ 9:40 AM — Apply preset 9 FTM_Probate_Greene → Day 1 SMS
☐ 9:55 AM — Apply preset 4 FTM_SS_Mont → Day 1 SMS (urgency variant if <14 days)
☐ 10:10 AM — Apply preset 5 FTM_SS_Franklin → Day 1 SMS
☐ 10:25 AM — Apply preset 6 FTM_SS_Greene → Day 1 SMS
☐ 10:40 AM — Apply presets 1-3 FTM_LP_* → Day 1 SMS
☐ 10:55 AM — Apply preset 000. FTM_RW (redemption) → Day 1 SMS
☐ 11:10 AM — Verify all sent records tagged sms_sent_day1
```

## Mike's mid-day checklist (11 AM-2 PM)

```
☐ Apply preset 02. Needs Called Day 1 → Day 2 SMS for yesterday's batch
☐ Apply preset 03. Needs Called Day 2 → Day 3 SMS for 2-days-ago batch
☐ Tag with sms_sent_day2 / sms_sent_day3 as they go
☐ Watch DataSift inbox for replies — respond per V2 response handlers
☐ Tag positive replies → escalate hot leads to Aaron via Slack
```

## Mike's end-of-day checklist (4:00-5:00 PM)

```
☐ 4:00 PM — Apply Sold Property Cleanup if any deals closed today
☐ 4:15 PM — Open Rolling Log Sheet
☐ 4:20 PM — Pull counts from DataSift filters:
   - SMS day 1 sent today: filter records tagged sms_sent_day1 added today
   - SMS day 2 sent: filter sms_sent_day2 added today
   - SMS day 3 sent: filter sms_sent_day3 added today
   - Replies: filter responded_v* / interested / not_interested / stop added today
   - Hot tags: filter hot tag added today
☐ 4:30 PM — Type counts into today's row in Tab 1
☐ 4:35 PM — Note any issues observed (text in "Issues observed" column)
☐ 4:40 PM — If issue is medium/high severity, add to Issues Tracker (Tab 4)
☐ 4:45 PM — Save sheet
☐ 4:50 PM — Quick scan: did I miss any preset today? Any record untouched?
☐ 5:00 PM — Done
```

---

## Friday weekly review (per [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md))

Tab 2 (Weekly Rollup) and Tab 4 (Issues Tracker) are the inputs to the Friday review. Mike posts the Slack summary to Aaron at 4:30 PM Friday with:

- Numbers (from Tab 2)
- 3 wins / 3 misses
- Top 3 open issues from Tab 4
- 3 priorities for next week

Aaron responds within 24 hrs. Pin commitments in Slack.

---

## Setup steps (one-time)

### Step 1 — Aaron creates the Sheet

```
1. Open https://sheets.google.com → Create new
2. Name: "SiftStack Rolling Log + KPIs"
3. Tab 1: rename "Sheet1" → "Daily Log"
4. Add column headers (14 + notes — see §"Columns Mike fills out daily")
5. Pre-populate Date column with next 90 days
6. Tab 2: create "Weekly Rollup"
7. Tab 3: create "Per-Preset Conversion"
8. Tab 4: create "Issues Tracker"
9. Share with Mike's Google account: edit access
10. Pin to Mike's Drive
```

CSV template at [`scripts/mike_kpi_tracker_template.csv`](../scripts/mike_kpi_tracker_template.csv) — import to seed Tab 1 with headers.

### Step 2 — Tab 2 Weekly Rollup formulas

Paste into Tab 2 cell A1 (assuming Tab 1 has the dates from row 2):

```
A: =ARRAYFORMULA(IF(LEN('Daily Log'!A2:A100)>0, "Week of "&TEXT('Daily Log'!A2:A100-WEEKDAY('Daily Log'!A2:A100,2)+1, "yyyy-mm-dd"), ""))
B (Records added): =SUMIFS('Daily Log'!D:D, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
C (Total SMS):     =SUMIFS('Daily Log'!H:H, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
D (Replies):       =SUMIFS('Daily Log'!I:I, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
E (Reply rate):    =IFERROR(D2/C2, "")
F (Positive):      =SUMIFS('Daily Log'!J:J, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
G (Pos rate):      =IFERROR(F2/C2, "")
H (Hot leads):     =SUMIFS('Daily Log'!L:L, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
```

### Step 3 — Tab 3 Per-Preset (Phase 2 — automated)

Phase 1: leave blank for first 2 weeks. Mike + Aaron eyeball Tab 1 + Tab 2 to spot trends.

Phase 2: I'll extend `gsheet_writer.py` to push per-preset reply rate from DataSift into Tab 3 each morning. Saves Mike from manually breaking down by preset.

---

## Phase 2 — Auto-population (after week 2 of manual flow)

Once Aaron + Mike have validated the manual flow works:

1. **Extend [`src/gsheet_writer.py`](../src/gsheet_writer.py)** to push Mike's daily row automatically after each autonomous run completes
2. **Source data:** existing `daily_summary.csv` covers volume metrics. Add a DataSift query for tag counts by date (per Mike's `sms_sent_day*` tags applied today)
3. **Mike's role shifts:** instead of typing 14 columns, he just verifies + adds qualitative notes (Issues observed, Notes for Aaron). Down to ~2 min/day.

---

## What Aaron does with the Sheet

**Daily (1 min):** glance at yesterday's row in Tab 1. Anything red? Slack Mike.

**Weekly (10 min on Fridays):** Tab 2 weekly trend lines. Tab 4 open issues. Plus Mike's Friday Slack summary.

**Monthly (30 min):** Tab 3 per-preset analysis. Reallocate Mike's time toward winners. Refresh A/B SMS variants on losers.

**Quarterly (1 hr):** Big-picture review with cost data. Unit economics check.

---

## See also

- [SOP-SMS-TEMPLATES-V2.md](SOP-SMS-TEMPLATES-V2.md) — sales-psychology-driven SMS templates Mike sends
- [SOP-OUTREACH-CADENCE.md](SOP-OUTREACH-CADENCE.md) — when to send what (drives the daily counts)
- [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md) — Friday review framework
- [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — what Mike escalates from data process oversight
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — how Mike scores responders before tagging `hot`
- [SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md) — how to filter + count records for the rolling log
