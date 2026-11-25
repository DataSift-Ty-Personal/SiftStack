---
name: kpi-engine
description: Pull and grade your cold-calling and lead-generation KPIs straight from your DataSift (REISift) account - dials, answer/conversation/contact rates, correct numbers, dispositions, leads (including new_lead statuses most reports miss), talk time, and full funnel pacing toward appointments and contracts - over any date range, per caller and account-wide, exported as markdown, CSV, Excel, or a Slack digest. Use when asked for KPI reports, weekly caller numbers, lead pacing, "how many dials/correct numbers/leads this week", or to set up recurring KPI tracking.
---

# KPI Engine - universal DataSift KPI reporting

Pulls your real calling activity from DataSift's account activity log (every call, text, and disposition event, caller-attributed and timestamped) on your own DataSift Open API key, and turns it into a graded KPI report: per caller, per day, and account-wide, with funnel pacing toward deals.

Why the activity log: DataSift's dashboard widgets are not exposed to scripts and cannot cleanly isolate "what happened on the phones this week." The activity log is the one date-accurate, caller-attributed source.

## Quick start

1. Get your DataSift Open API key. An account admin creates it in DataSift (developers.datasift.ai explains it). It never expires.
2. Give it to the script: set the environment variable `REISIFT_API_KEY`, or put the line `REISIFT_API_KEY=<your key>` in a `.env` file in the folder you run from (or next to the script).
3. Run:

```bash
python scripts/pull_kpis.py --days 7                 # trailing week
python scripts/pull_kpis.py --from 2026-07-06 --to 2026-07-16
python scripts/pull_kpis.py --days 1                 # today
python scripts/pull_kpis.py --days 7 --xlsx          # also build an Excel workbook
python scripts/pull_kpis.py --days 7 --detail        # + record-level CSV (one row per record worked)
python scripts/pull_kpis.py --days 7 --slack <webhook-url>   # post digest to Slack
```

No dependencies for markdown/CSV output (pure standard library). Excel output needs `pip install openpyxl`. It reads the account log 100 events a request, so a busy week takes a minute or two. `--detail` adds a few record reads (50 owners a request) to put an address on each row. Every request is a GET: nothing in your account changes. DataSift allows 120 requests a minute per account, shared by every tool on it, and the script paces itself at about 80. `python scripts/pull_kpis.py --selftest` checks the counting rules with no network.

## What you get

- **Volume:** dials, answered, no-answer, records touched, talk time, first/last call window, dials per hour.
- **Three rates, never collapsed into one "connect rate":**
  - answer rate = answered / dials (loosest - includes voicemail pickups)
  - conversation rate = answered calls of 60s+ / dials (meaningful = 120s+)
  - contact rate = correct numbers / dials (right party confirmed)
- **Dispositions:** correct / wrong / dead / DNC numbers; not interested, follow-ups, dead leads.
- **Leads that actually count:** most reports only count Cold/Warm/Hot Lead statuses and miss that first-touch leads land in `new_lead` and `No Contact New Lead` - so leads read 0 while your callers produce 2-3 a day. This skill counts the full lead-status set (configurable in `benchmarks.json`).
- **Funnel pacing:** dials per correct number vs the ~9 (phone-scored) / ~32 (blind) benchmarks, correct numbers toward the 100-correct ~= 1-deal ratio, projected appointments from your appointment-take rate, projected contracts from your leads-per-contract ratio.
- **Per-caller scorecards:** dial floor (150/day) and conversation floor (5/day) MET or BELOW per caller, scaled by days active, plus lead targets.

## Benchmarks are baselines, not rules

The numbers in `references/kpi-catalog.md` and the script's defaults (150-dial floor, 2-3 leads/caller/day, 25% appointment take, 1 contract per 15-20 leads, 32:1 -> 9:1 dials per correct) are working baselines from a live operation and the DataSift 5-Day Deal Flow Challenge. Treat them as min-max starting points and tune them to your market in a `benchmarks.json` next to the script (the script prints its full config with `--show-benchmarks`).

Tips for clean numbers:
- Add the emails of your admin and owner users to `excluded_callers` in `benchmarks.json` so testing and admin activity does not pollute caller tables.
- A dialer such as Kixie often names the caller only on the answered call, not on the dial. The script places an unnamed dial by its call, then by the one caller seen on that line that day, then by the one caller who worked that day. Whatever is left shows as Unattributed and still counts in the account totals.
- Appointments count from completed tasks with appointment/appt/meeting in the title - make logging those part of your lead-handoff routine or the appointment column stays empty.
- Statuses are matched by exact name. If your account uses custom status names, add them to `lead_statuses`.

## Recurring use

- Windows: Task Scheduler -> run `python pull_kpis.py --days 1 --slack <webhook>` daily at 9pm; weekly on Friday with `--days 7 --xlsx`.
- Mac/Linux: same via cron.
- The report is deterministic from your account data: re-running a range is safe and just refreshes the outputs.

## Reference

`references/kpi-catalog.md` holds the full KPI catalog: every metric with its formula, the benchmark ladder (dials -> correct numbers -> leads -> appointments -> contracts), role-level KPIs for prospectors / lead managers / closers, and where each number comes from.
