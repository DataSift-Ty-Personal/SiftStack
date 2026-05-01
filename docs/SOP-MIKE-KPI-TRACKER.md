# SOP — Mike's Daily KPI Tracker

**Audience:** Mike (data manager) + Aaron (owner reviewing performance).
**Purpose:** Define exactly what Mike needs to hit each day, how it gets tracked, and the Google Sheet that makes performance visible without weekly debate.

---

## Daily KPI targets (the goals)

Mike's day is measured on **5 input metrics** and **3 outcome metrics**. Inputs are what he controls (effort + activity). Outcomes are what the system + market produce.

### Input KPIs (Mike's controllables — non-negotiable daily)

| KPI | Daily target | Why it matters |
|---|---|---|
| **SMS sent — Day 1 (new records)** | 100% of qualifying records (Tier 0-2 phones) | Every new record from autonomous run gets Day 1 outreach within 24 hrs of upload |
| **SMS sent — Day 2/3 follow-ups** | 100% of records eligible per cadence | No leads slip through cracks — if cadence says Day 2, Day 2 SMS goes out |
| **Phone calls attempted** | Min 30 dials/day (Tier 0-2 records on Day 4+) | After SMS stage, calls are next. 30 dials = ~2 hrs of dial time |
| **Records tagged after each touch** | 100% (sms_sent_dayX, called_dayX, etc.) | If Mike doesn't tag, the cadence breaks tomorrow |
| **Status updates on engaged records** | 100% same-day | "New" → "Contacted" → "Interested" → "Appointment" — keeps pipeline visible |

### Outcome KPIs (system + market produce these — Mike influences via quality)

| KPI | Daily target | Weekly target |
|---|---|---|
| **SMS reply rate** | 8-15% of Day 1 SMS sent | Track per variant (A/B/C) |
| **Positive replies** (interested/question, NOT stop/no) | 2-5% of Day 1 SMS sent | 5-15 positive replies per week |
| **Hot leads escalated to Aaron** | 1-3 per day | 5-15 per week |
| **Calls connected** (>60 sec convo) | 30%+ of dials = ~9 connections from 30 dials | 45+ connections per week |
| **Appointments set** | Variable (target: 2-5/week) | 2-5 booked calls with Aaron |

### Stop-loss KPIs (red flags requiring escalation)

| Metric | Threshold | Action |
|---|---|---|
| **SMS reply rate** | < 5% for 3 consecutive days | Pause cadence, A/B test new variants |
| **Positive reply rate** | < 1% for 5 consecutive days | Tag/preset/data quality audit |
| **Connection rate (calls)** | < 20% for 3 days | Phone tier scoring may be off; check Trestle |
| **Records with zero touches at Day 7+** | Any | Mike's slacking — meeting with Aaron |

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
Convert to call within 48 hrs:      ~1.5 calls per day
                                    ↓
Appointment set rate at 40%:        ~0.6 appointments per day = 3-4/week
                                    ↓
Aaron closes at 25-30% of apptmts:  ~1 deal per week → 4-5/month
```

**Healthy funnel = 1 deal/week from ~75 SMS/day input.** That's the math Mike works toward. If the conversion rates above hold and volume is at 75 SMS/day, deals follow.

If reply rates are below 8%, either:
- Templates aren't resonating (A/B test new variants — see [SOP-SMS-TEMPLATES-V2.md](SOP-SMS-TEMPLATES-V2.md))
- Phone tiers are off (data quality issue)
- Wrong segments being worked

---

## The Google Sheet Mike fills out daily

**Sheet name:** `Mike's Daily KPI Tracker`
**Owner:** Aaron's Google account (shared edit access with Mike)
**Why a Sheet, not DataSift:** DataSift has the raw data but not the layout that makes daily performance + weekly trends pop visually.

### Sheet structure

3 tabs:

#### Tab 1: `Daily Log` (Mike fills out at end of each day)

One row per day. Columns:

| Date | SMS Day1 sent | SMS Day2 sent | SMS Day3 sent | Calls attempted | Calls connected | Hot leads tagged | Appointments set | Notes / Blockers |
|---|---|---|---|---|---|---|---|---|
| 2026-05-01 | 73 | 45 | 22 | 28 | 9 | 2 | 0 | Phone tier 5 was 18% today — high |
| 2026-05-02 | ... | ... | ... | ... | ... | ... | ... | ... |

**Mike's end-of-day routine (5 min):**
- Open Sheet
- Pull counts from DataSift (filter today, count records with each tag)
- Type into row, save

**Mike's morning check (1 min):**
- Glance at yesterday's row
- Note any KPI miss → adjust today's plan

#### Tab 2: `Weekly Rollup` (auto-formulas — Mike doesn't edit)

Aggregates Daily Log into week-over-week view. Columns:

| Week of | Total SMS | Total replies | Reply rate | Positive replies | Pos rate | Hot leads | Apptmts | Deals closed | Cost (Tracerfy + Trestle + DataSift) | Cost per deal |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-04-26 | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |

**Aaron reviews this weekly during Friday Review** ([SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md)).

#### Tab 3: `Per-Preset Conversion` (auto-formulas)

Tracks reply rate + conversion by preset, so we know which segments perform:

| Preset | SMS sent (week) | Replies | Reply rate | Positive | Pos rate | Hot leads | Notes |
|---|---|---|---|---|---|---|---|
| FTM_LP_Mont | ... | ... | ... | ... | ... | ... | A/B variant winning: B |
| FTM_LP_Franklin | ... | ... | ... | ... | ... | ... | ... |
| ... (all 10 presets) | ... | ... | ... | ... | ... | ... | ... |

**Tells us where the leverage is.** If FTM_Probate_Franklin converts 4x better than FTM_SS_Greene, prioritize Mike's time accordingly.

---

## How the Sheet gets data

### Phase 1 (week 1) — Manual

Mike fills out Tab 1 (Daily Log) by hand at end of each day. He pulls counts from DataSift's filter UI:

- SMS Day 1 sent today: filter records with `sms_sent_day1` tag added today
- Replies: filter `responded_v1*` or `interested` or `not_interested` or `stop` tags added today
- Calls attempted: filter `called_day1`/`called_day2`/`called_day3` tags added today
- Hot leads: filter `hot` tag added today
- Appointments: filter status changed to `Appointment` today

Tabs 2 & 3 auto-aggregate via Sheet formulas (SUMIFS / COUNTIFS).

### Phase 2 (week 3+) — Auto-population

Once we've validated the manual flow, I'll extend `gsheet_writer.py` to write Mike's daily KPI row automatically:

- After each daily run, the SiftStack pipeline appends Mike's row data to Tab 1
- Mike just verifies + adds qualitative notes
- ~2 minutes/day of his time vs 5-10 manual

The data we already track in [`daily_summary.csv`](../src/daily_summary.py) covers most of this — we just need to also pull Mike's tagging activity from DataSift (records with each tag count, per day).

---

## Concrete daily targets per preset volume

Assuming steady-state ~100 new records/day from autonomous run, here's the breakdown:

```
Probate (~20 records/day, all 3 counties combined)
  Day 1 SMS:   18 records (skip 2 entity-owned)
  Reply target: 2-3 (probate has highest emotional weight, ~12% reply)
  Positive:     1 (probate converts at ~5% positive)

Foreclosure / Sheriff Sale (~50 records/day, 3 counties)
  Day 1 SMS:   40 records (skip Tier 4-5 + commercials)
  Reply target: 4-6 (urgency drives ~10-12% reply)
  Positive:     1-2 (~3-5% positive)

Lis Pendens (~25 records/day, 3 counties — once Mike's prsts populate)
  Day 1 SMS:   20 records
  Reply target: 1-2 (early-stage, lower urgency, ~6-8% reply)
  Positive:     1 (~5% positive)

Tax Sale (~10 records/day, Mont only for now)
  Day 1 SMS:   8 records
  Reply target: ~1 (mixed motivation, ~10% reply)
  Positive:     0-1

Redemption Window (varies — sporadic)
  Day 1 SMS:   ~5 records when present
  Reply target: 1+ (high motivation, ~15% reply)
  Positive:     1 (very high conversion when in window)

DAILY TOTAL TARGETS:
  SMS sent:        ~70-90/day
  Total replies:   ~9-13/day (12% blended reply rate)
  Positive replies: ~3-5/day
  Hot leads escalated to Aaron: 1-3/day
  Appointments booked: 0-1/day (~3-4/week)
```

---

## Mike's morning checklist

Print this and put it next to his monitor:

```
☐ 9:00 AM — Open Slack, read overnight summary
☐ 9:05 AM — Open DataSift, apply yesterday's date filter to verify autonomous upload landed
☐ 9:10 AM — Apply preset 7 FTM_Probate_Mont → Day 1 SMS to Tier 0-2 phones (V2 templates)
☐ 9:25 AM — Apply preset 8 FTM_Probate_Franklin → Day 1 SMS
☐ 9:40 AM — Apply preset 9 FTM_Probate_Greene → Day 1 SMS
☐ 9:55 AM — Apply preset 4 FTM_SS_Mont → Day 1 SMS (urgency variant if <14 days)
☐ 10:10 AM — Apply preset 5 FTM_SS_Franklin → Day 1 SMS
☐ 10:25 AM — Apply preset 6 FTM_SS_Greene → Day 1 SMS
☐ 10:40 AM — Apply preset 1-3 FTM_LP_* → Day 1 SMS
☐ 10:55 AM — Apply preset 000 FTM_RW (redemption) → Day 1 SMS
☐ 11:10 AM — TAG every record sent with sms_sent_day1
☐ 11:15 AM — Review responses from yesterday's batch → tag/respond
☐ 11:30 AM — Phone call window opens — work Day 4+ records
☐  ...
☐ 4:30 PM — End-of-day: fill out Tab 1 of KPI Tracker Sheet
☐ 4:45 PM — Tag any deals closed → Sold Property Cleanup auto-fires
☐ 5:00 PM — Done
```

---

## Weekly Friday review (Mike + Aaron, 30 min)

Every Friday at 4 PM:

1. **Mike opens KPI Tracker → Tab 2 (Weekly Rollup)** — pulls this week vs last 4 weeks
2. **Discusses 3 wins, 3 misses, 3 priorities** for next week (per [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md))
3. **Reviews Tab 3 (Per-Preset Conversion)** — which preset is winning, which needs A/B refresh
4. **Aaron commits to fixes** Mike can't do himself (preset edits, scraper changes, etc)
5. **Pin commitments in Slack**

---

## When Mike falls below targets

If for 3+ days in a row Mike's input KPIs miss target:

- **Day 1 SMS sent < 100% of qualifying records** → ping Aaron same-day
- **Calls attempted < 20/day** → flag in Friday review
- **Records untagged after touches** → immediate retraining (1-on-1)

If outcome KPIs miss for a week:
- **Reply rate < 5%** → schedule emergency A/B test session, refresh templates
- **Positive rate < 1%** → audit data quality (phone tiers, address validation, preset filters)

---

## How Aaron uses the Sheet

**Daily (1 min):** glance at yesterday's row in Tab 1. Anything red? Slack Mike.
**Weekly (10 min):** Tab 2 trend lines. Are reply rates rising or falling? Cost per deal trending right way?
**Monthly (30 min):** Tab 3 per-preset analysis. Reallocate Mike's time toward winners. Drop or revamp losers.
**Quarterly (1 hr):** Big-picture review with cost data. Is the unit economics working? Should we scale up volume or improve quality first?

---

## Setup steps (one-time)

### Step 1 — Aaron creates the Sheet

```
1. Open https://sheets.google.com → Create new
2. Name: "Mike's Daily KPI Tracker"
3. Tab 1: rename "Sheet1" → "Daily Log"
4. Add headers from §"Daily Log" above
5. Tab 2: create "Weekly Rollup" — add formulas from §below
6. Tab 3: create "Per-Preset Conversion"
7. Share with Mike's Google account: edit access
8. Pin to Mike's Drive
```

### Step 2 — Tab 2 Weekly Rollup formulas

Paste these into A1 of Tab 2 (assuming Tab 1 has 30 rows of data starting row 2):

```
=ARRAYFORMULA(IF(LEN(A2:A100)>0, "Week of "&TEXT(A2:A100-WEEKDAY(A2:A100,2)+1, "yyyy-mm-dd"), ""))
```

Rest of weekly aggregation:

```
B (Total SMS): =SUMIFS('Daily Log'!B:B, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
C (Replies): =SUMIFS('Daily Log'!E:E, 'Daily Log'!A:A, ">="&A2, 'Daily Log'!A:A, "<="&A2+6)
D (Reply rate): =IFERROR(C2/B2, "")
... etc
```

I'd recommend Aaron build this once + reuses every week. ~30 min one-time setup.

### Step 3 — Mike's daily routine for the Sheet

```
Every day at 4:30 PM:
1. Open the Sheet
2. New row in Tab 1 with today's date
3. From DataSift, count records tagged sms_sent_day1 today → enter in column B
4. Repeat for other tags (Day 2, Day 3, called_day1, hot, etc.)
5. Notes column: anything weird, blockers, ideas
6. Save
```

---

## See also

- [SOP-SMS-TEMPLATES-V2.md](SOP-SMS-TEMPLATES-V2.md) — the actual SMS templates
- [SOP-OUTREACH-CADENCE.md](SOP-OUTREACH-CADENCE.md) — when to send what (drives the daily counts)
- [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md) — Friday review framework that uses these KPIs
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — defines what "hot lead" means for the escalation KPI
