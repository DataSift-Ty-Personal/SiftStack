---
name: closer-coach
description: Pull acquisitions/closing calls from your SmrtPhone account, transcribe with real tonality notes, and grade them against the DataSift closer rubric - discovery deepening, the money conversation, multi-option offer presentation, objection handling, commitment locking. Produces per-call coaching reports, closer scorecards, and an Excel workbook. Use when asked to review closing calls, offer calls, negotiation quality, or acquisitions call coaching.
version: 2.0.0
---

# Closer Coach

Grades acquisitions calls (offer presentation, negotiation, renegotiation,
contract talk, in-person appointment recordings) against the DataSift Closer
playbook. Same pipeline as the cold-call-coach skill; this skill applies the
closing rubric to calls the triage step classified as `closing`.

## Requirements, setup, and costs

Identical to the cold-call-coach skill (same scripts, same session, same .env):
`python scripts/smrtphone_login.py` once, `OPENROUTER_API_KEY` in `.env`,
optional `roster.json`. Transcription costs about $0.002 per audio minute.

## Pipeline

```bash
python scripts/pull_calls.py --min-seconds 60 --days 14
python scripts/transcribe.py
```

Then take the `closing` group with `worth_grading: true` from
`output/call_coaching/review_queue.json`. Closing calls are rarer and longer
than cold calls; if the queue is empty, widen the window (`--days 30`) or drop
`--min-seconds` to 45. Very long calls (over ~25 min) may need the MP3 split
with ffmpeg (`-f segment -segment_time 900`) and the parts transcribed in order.

## Grading procedure

1. Read `references/rubric.md` fully (gate with the in-person variant,
   auto-fails, 6 categories weighted 15/25/20/25/10/5, anchors, template, the
   required SCORES JSON footer).
2. Read the whole transcript once; verify AGENT/SELLER labels by content.
3. Score criterion by criterion against the 0/3/5 anchors (quote required),
   compute the weighted /100 and band, and write one report per call to
   `output/call_coaching/reports/closing/{call_id}_{caller}.md` with the
   CRITERION SCORES table and SCORES JSON footer. Closing calls deserve depth:
   include a negotiation timeline (each price or term mentioned, who moved,
   what triggered the move) and a "the moment it was won or lost" section
   quoting the pivotal exchange.
4. Export: `python scripts/export_excel.py --dir output/call_coaching/reports/closing`

## Grading quality protocol (mandatory verify pass)

Same as cold-call-coach: verbatim quotes only, evidence per criterion, N/A
discipline with listed redistribution (N/A when the situation never arose,
e.g. renegotiation criteria on a first-offer call), math that foots exactly
with a matching JSON footer, and call-audible content only.

## Failure modes

Session expired: re-run `python scripts/smrtphone_login.py`. Key error: set
OPENROUTER_API_KEY in `.env`. If a call is really lead management, grade it
with that skill instead and note the reroute.

## Related

First-touch dials: `cold-call-coach`. Qualification follow-ups:
`lead-manager-coach`. This skill grades call QUALITY only.
