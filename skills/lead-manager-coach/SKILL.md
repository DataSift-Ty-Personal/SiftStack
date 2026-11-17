---
name: lead-manager-coach
description: Pull lead-management follow-up and qualification calls from your SmrtPhone account, transcribe with real tonality notes, and grade them against the DataSift lead manager rubric - qualifying questions (condition, timeline, motivation, price), 4 pillars, roadblocks, objection handling, next-action discipline. Produces per-call coaching reports, scorecards, and an Excel workbook. Use when asked to review lead manager calls, follow-up call quality, or qualification call coaching.
version: 2.0.0
---

# Lead Manager Coach

Grades lead-management calls (follow-ups, qualification, nurture touches,
revivals) against the DataSift Lead Manager playbook. Same pipeline as the
cold-call-coach skill; this skill applies the lead-management rubric to calls
the triage step classified as `lead_management`.

## Requirements, setup, and costs

Identical to the cold-call-coach skill (same scripts, same session, same .env):
`python scripts/smrtphone_login.py` once, `OPENROUTER_API_KEY` in `.env`,
optional `roster.json`. Transcription costs about $0.002 per audio minute.

## Pipeline

```bash
python scripts/pull_calls.py --min-seconds 60 --days 7
python scripts/transcribe.py
```

Then take the `lead_management` group with `worth_grading: true` from
`output/call_coaching/review_queue.json`. Transcripts live at
`output/call_coaching/transcripts/{call_id}.md` and include a DELIVERY SUMMARY
with real audio observations plus a label check line.

## Grading procedure

1. Read `references/rubric.md` fully (gate, short-call rule, auto-fails, 5
   categories weighted 10/30/20/20/20, anchors, templates, the required SCORES
   JSON footer).
2. Read the whole transcript once; verify AGENT/SELLER labels by content; route
   full vs short (short = a decline inside ~30 seconds; doubt resolves to full).
3. Score criterion by criterion against the 0/3/5 anchors (quote required),
   write one report per call to
   `output/call_coaching/reports/lead_management/{call_id}_{caller}.md` with
   the CRITERION SCORES table and SCORES JSON footer, then a per-caller
   scorecard.
4. Export: `python scripts/export_excel.py --dir output/call_coaching/reports/lead_management`

## Grading quality protocol (mandatory verify pass)

Same as cold-call-coach: verbatim quotes only, evidence per criterion, N/A
discipline with listed redistribution, math that foots exactly with a matching
JSON footer, call-audible content only, and full vs short calls never ranked
in one column.

## Failure modes

Session expired: re-run `python scripts/smrtphone_login.py`. Key error: set
OPENROUTER_API_KEY in `.env`. A call misclassified between cold_call and
lead_management: trust your read of the transcript, note the reroute.

## Related

First-touch dials: `cold-call-coach`. Offer/negotiation calls: `closer-coach`.
This skill grades call QUALITY only; CRM hygiene is never part of the score.
