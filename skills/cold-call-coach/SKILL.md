---
name: cold-call-coach
description: Pull real cold-call recordings from your SmrtPhone account, transcribe them with real tonality notes (an audio model hears the calls), and grade every conversation against the DataSift cold-calling rubric - opener, motivation probing (4 pillars), objection handling, tonality, close quality. Produces per-call coaching reports, per-caller scorecards, and one clean Excel workbook. Use when asked to review cold calls, grade callers, run call coaching, or audit call quality.
version: 2.0.0
---

# Cold Call Coach

Grades real first-touch cold calls against the DataSift cold-calling playbook.
The pipeline pulls recordings straight from your SmrtPhone web session (no API
exists for this), transcribes them with an audio-capable model so tonality
observations are heard rather than guessed, then Claude grades each gradeable
conversation against `references/rubric.md` and exports everything to Excel.

## Requirements and setup (once)

1. Python 3.10+ with: `pip install playwright openpyxl` then `playwright install chromium`
2. SmrtPhone login: `python scripts/smrtphone_login.py` (headed window opens,
   log in once; the session is saved to `smrtphone_state.json` and lasts weeks)
3. OpenRouter key for transcription: copy `scripts/env.example` to `.env` and
   set `OPENROUTER_API_KEY` (get one at openrouter.ai)
4. Optional team roster: copy `scripts/roster.example.json` to `roster.json` to
   pin caller roles (a dedicated cold caller's calls never route to another
   rubric) and exclude departed callers

## Costs (transparent)

- Transcription: about $0.002 per audio minute (Gemini 2.5 Flash via OpenRouter).
  A week of coaching for a 3-caller team (say 60 gradeable calls averaging 2
  minutes) costs roughly $0.25 in transcription.
- Recording downloads and the call log are free (your existing SmrtPhone plan).
- Grading runs inside your Claude session.

## Pipeline

Run from your project folder:

```bash
python scripts/pull_calls.py --min-seconds 60 --days 7   # call log + MP3s
python scripts/transcribe.py                              # audio -> transcripts + triage
```

Outputs land in `output/call_coaching/`:
- `call_log.json` / `calls_to_review.json` - every call, then the review set
- `transcripts/{call_id}.md` - diarized transcript with inline delivery notes
  ([long pause 4s], [rushed], [warm tone]) plus a DELIVERY SUMMARY block (pace,
  agent energy, talk balance, notable audio moments) and a label check line
- `review_queue.json` - calls grouped: cold_call / lead_management / closing /
  not_gradeable

Only grade calls in the `cold_call` group with `worth_grading: true`.
Voicemails, wrong numbers, and dead air are logged, never scored.

## Grading procedure

1. Read `references/rubric.md` fully (applicability gate, short-call rule,
   auto-fails, 5 categories weighted 15/30/20/15/20, score anchors, both report
   templates, the required SCORES JSON footer).
2. Read the worked examples in `references/calibration/` before your first
   session and stay consistent with them.
3. For each call: read the whole transcript once; verify AGENT/SELLER labels by
   content (our agent is whoever asks about buying the property); route full vs
   short (short = a DECLINE inside ~30 seconds; doubt resolves to full); score
   each reachable criterion against its 0/3/5 anchors (1, 2, 4 interpolate).
4. Write one report per call to
   `output/call_coaching/reports/cold_call/{call_id}_{caller}.md` using the
   rubric's templates exactly, including the CRITERION SCORES table and the
   SCORES JSON footer.
5. Roll up a per-caller scorecard, then export the workbook:

```bash
python scripts/export_excel.py
```

One styled Excel file: Summary (full calls, /100, band colored), Short Calls
(opener + conversion on their own scale), Coaching Detail, Caller Scorecard,
Criterion Scores.

## Grading quality protocol (mandatory verify pass)

- Every quote must appear verbatim in the transcript (check by substring).
- Every criterion score cites a quote or an explicit absence.
- N/A only where the rubric allows; list every N/A and redistributed weight.
- Recompute the math: tables must foot exactly and the JSON footer must match
  the tables number for number.
- Grade ONLY what is audible on the call. CRM state and dispositions are never
  scored.
- Full calls and short calls are different scales; never rank them together.

## Measured reliability

Independent graders reproduce full-call totals within about +/-3 points and
agreed on the grade band in every test comparison; see
`references/reliability.md`. Treat totals within 5 points as equivalent, and
get a second grade before any band-boundary personnel decision.

## Failure modes

- `pull_calls.py` exits 2 or reports an HTML response: session expired; re-run
  `python scripts/smrtphone_login.py`.
- `transcribe.py` key error: set OPENROUTER_API_KEY in `.env`.
- Recording URL 404: the recording was deleted in SmrtPhone; log and skip.
- Transcripts can truncate on very long calls; N/A the unreachable categories
  and note it.

## Related

- Follow-up and qualification calls: the `lead-manager-coach` skill.
- Offer and negotiation calls: the `closer-coach` skill.
- This skill grades call QUALITY, not dial volume KPIs.
