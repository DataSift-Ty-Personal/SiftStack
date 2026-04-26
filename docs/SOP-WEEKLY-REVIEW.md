# SOP — Friday Weekly Review

**Audience:** Mike + future caller (run weekly), Aaron (review weekly).
**Purpose:** Catch trends, tune the system, flag issues before they compound. 30-min ritual every Friday at 4:00 PM.

---

## Why this matters

Daily ops can absorb you. Without a weekly step-back, you'll:
- Miss patterns (e.g., "Franklin probate quality has been dropping for 3 weeks")
- Burn out on the same broken workflow without fixing it
- Lose track of what's actually working vs what feels productive
- Forget to give Aaron the high-signal feedback he needs to tune the pipeline

This is a **30-minute ritual every Friday at 4:00 PM**. Block your calendar for it.

---

## The 30-minute structure

```
00:00 - 00:05   Numbers (5 min)
00:05 - 00:15   Wins + Misses (10 min)
00:15 - 00:25   Issues + Asks (10 min)
00:25 - 00:30   Next-week priorities (5 min)
```

---

## Step 1 — Numbers (5 min)

Open DataSift Records, filter by date range = last 7 days, capture:

| Metric | This Week | Last Week | Trend |
|---|---|---|---|
| **Total new records** (from autonomous runs) | | | |
| **Probate** | | | |
| **Foreclosure** | | | |
| **Tax Sale** (currently no preset) | | | |
| **Total SMS sent** | | | |
| **Total calls made** | | | |
| **Conversations had** (calls with >60 sec engagement) | | | |
| **Hot leads escalated to Aaron** | | | |
| **Deals under contract** (status = Under Contract) | | | |
| **Deals closed** (status = Closed) | | | |
| **Deals lost** (status = Dead) | | | |

Don't worry about precision — ballpark is fine. The trend matters more than the absolute number.

**Save these in a Google Sheet** ("SiftStack Weekly Numbers") so you can see 12-week trends at a glance.

---

## Step 2 — Wins + Misses (10 min)

### Wins (3-5 bullets)

What worked this week? Be specific:

- "The Montgomery probate Day 1 SMS got a 22% response rate — way up from 12% last week"
- "Tier 1 phone scoring is dialing in — every Tier 1 call this week resulted in a conversation"
- "Aaron closed the Cridge probate deal — full pipeline path validated end-to-end"

### Misses (3-5 bullets)

What didn't work? Be honest, not defensive:

- "Wasted 3 hours Tuesday on Franklin foreclosures with bad addresses — Smarty validation flagged 12/40 as undeliverable but I called anyway"
- "Lost the Henderson lead because I didn't follow up by Thursday — they sold to someone else Friday"
- "Greene County had 0 records all week — system says portal license is still disabled, no action available"

---

## Step 3 — Issues + Asks (10 min)

### Open issues (things broken or fragile)

| Issue | Severity | Owner | Plan |
|---|---|---|---|
| | | | |

Examples:
- "DataSift's Skip Trace returned weird numbers for 5 records this week — feels like an API hiccup, not our pipeline. Severity: low. Owner: monitor. Plan: continue, escalate if rate exceeds 10%."
- "Two Slack pings came in at 9:15 AM instead of 8:30 AM — both Tuesday and Thursday. Severity: medium. Owner: Aaron. Plan: check Apify run timing."
- "Tax Sale records (40+/week) have no preset workflow — they sit in DataSift with no automation. Severity: medium. Owner: Aaron. Plan: decide: build FTM_TaxSale presets or filter them out at scraper level."

### Asks for Aaron

Things you need from him to do your job better:

- "Please confirm preset 7-9 (FTM_Probate_*) are now filtering on `ftm-probate` not `ftm-lp` — I think it changed but want to verify"
- "Can we get a Tier 0 score band (>=95) so I know which to dial *first* of the Tier 1s?"
- "I'd like a Slack ping when a `hot` tag goes through — I want a real-time alert vs checking DataSift"

---

## Step 4 — Next-week priorities (5 min)

Pick 3 things to focus on next week. No more, no less.

```
1. ____________________________________
2. ____________________________________
3. ____________________________________
```

Examples of good priorities:
- "Test a longer SMS opener for foreclosure (Day 1) and measure response delta"
- "Catch up on the 47 callbacks scheduled for next week — block 9-11 AM Tue/Thu"
- "Backfill last week's `not_interested` records with notes — my dispositions were sloppy"

Examples of bad priorities (too vague):
- "Make more calls"
- "Improve conversion"
- "Be better at probate"

---

## Output format — what to send Aaron

After the 30 minutes, paste this in Slack to Aaron:

```
🗓️ Weekly Review — Week of [Monday's date]

📊 Numbers:
  - X new records (Y probate, Z foreclosure)
  - X SMS, Y calls, Z conversations
  - X hot leads escalated, Y deals closed, Z lost

🏆 Wins:
  - [bullet]
  - [bullet]
  - [bullet]

⚠️ Misses:
  - [bullet]
  - [bullet]

🚨 Open issues for you:
  - [issue + ask]
  - [issue + ask]

🎯 Next week priorities:
  1. [priority]
  2. [priority]
  3. [priority]
```

Keep it tight. Aaron should be able to read it in 2 minutes and respond with adjustments.

---

## What Aaron does with it

**Within 24 hours** (i.e., by Saturday afternoon), Aaron:
1. Reads the review
2. Replies with: validation of priorities, fixes for any flagged issues, adjustments to pipeline
3. If issues are systemic (e.g., scraper consistently broken on a county) — schedules a code change for the following Monday
4. If priorities are off — pushes back and re-aligns

**This is the rhythm.** Mike runs the daily, Aaron runs the system, Friday is when they sync.

---

## Quarterly review (every 12 weeks)

Once per quarter, pull a longer view:

- 12-week trend on every metric (build the Google Sheet for this)
- Cost-per-deal-closed (total SiftStack ops cost ÷ deals closed in quarter)
- Conversion rates by source (probate vs foreclosure, by county)
- Cost-per-county (Franklin vs Montgomery vs Greene — does Greene justify continued investment?)
- Pipeline mix (% of deals from probate vs foreclosure vs tax sale)
- Mike + caller workload distribution

This is a 90-min sit-down with Aaron. Block it.

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — daily playbook
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — lead scoring framework
- [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) — when to escalate problems mid-week (don't wait until Friday)
