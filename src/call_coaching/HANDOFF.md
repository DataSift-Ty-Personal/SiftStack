# Call Coaching System: Architecture and Replication Handoff

Author: DataSift / Volunteer Home Buyers. Last updated 2026-07-10.
Audience: an engineer who wants to stand up the same call-QA system from scratch, on their own CRM/dialer.

This document is self-contained. Read it top to bottom and you can rebuild the whole thing.

---

## 1. What this system does

It turns raw sales-call recordings into graded, coach-ready reports, automatically.

The loop, once per review cycle (daily or weekly):

1. Pull the call log and download the actual call recordings from the dialer.
2. Transcribe each recording with an audio model (so tonality is heard, not guessed) and triage it (real conversation vs voicemail vs wrong number; which pipeline: cold call / lead management / closing).
3. Grade every real conversation against a rubric built from the company's own call playbook. Each criterion is scored 0 to 5 with a supporting quote from the transcript.
4. Produce per-call coaching reports, per-caller scorecards, and a team summary, all with clickable links to the recording and the CRM record.
5. Export to Word and Google Docs for sharing.

It runs three parallel "coaches" off one engine: **cold calling**, **lead management**, and **closing**. Same pipeline, three different rubrics.

Design principles that matter:
- **Every score cites a verbatim quote.** No unsupported claims. This is enforced by an adversarial verification pass.
- **The audio is actually heard.** Tonality (pace, energy, silence, interruptions) comes from an audio-capable transcription model, not inferred from text.
- **Short calls are graded fairly** via proration, not thrown away. A 30-second clean "no" is judged only on the criteria it could reach.
- **Nothing is hand-typed into tables.** Property/record columns and links are injected programmatically so 45-row tables never drift.

---

## 2. Architecture at a glance

```
DIALER (SmrtPhone web app)                 KNOWLEDGE BASE (call playbook)
        |                                          |
        | pull_calls.py                            | (one-time) build-call-rubrics workflow
        v                                          v
  call_log.json + recordings/*.mp3          3 rubrics (references/rubric.md per skill)
        |                                          |
        | transcribe.py (audio -> text + tonality) |
        v                                          |
  transcripts/*.md + review_queue.json             |
        |                                          |
        | enrich_records.py (address per call)     |
        v                                          |
  record_map.json                                  |
        |                                          |
        +--------------------+---------------------+
                             |
                             | GRADING (Claude, fanned out via Workflow tool)
                             |   read rubric + transcript -> score -> verify quotes -> write report
                             v
             reports/{pipeline}/{call_id}_{caller}.md   (per-call)
             reports/{pipeline}/_scorecard_{caller}.md  (per-caller)
             reports/_team_summary.md
                             |
                             | add_property_column.py  (inject Property->record links)
                             | export_docs.py          (-> Word)   export_html.py (-> Google Docs)
                             v
             reports/docx/*.docx  +  Google Drive folder
```

Two halves:
- **The engine** (deterministic Python, in `src/call_coaching/`): pull, transcribe, enrich, export. Runs the same every time.
- **The grading** (Claude, driven by three skills + the Workflow tool): reads rubric + transcript, produces the judgments. This is where the intelligence lives.

---

## 3. Prerequisites

### Accounts and credentials
- **A dialer that records calls.** We use SmrtPhone (phone.smrt.studio). Any dialer works if you can (a) list calls with duration + a link to the CRM record, and (b) download the recording audio. See Section 6 for how we reverse-engineered SmrtPhone; adapt that section for your dialer.
- **An audio-capable LLM.** We use Google Gemini 2.5 Flash via OpenRouter (`OPENROUTER_API_KEY`). Roughly $0.002 per audio-minute. Any model that accepts audio input works (Gemini, GPT-4o-audio, etc.).
- **Claude Code / Claude with the Workflow + Skill tools** for the grading half. The skills live in `~/.claude/skills/`.
- **(Optional) Google Drive connector** for uploading the finished Google Docs.

### Software
- Python 3.12, a venv at the repo root (`.venv/`).
- `playwright` + chromium (one-time session capture only).
- `python-docx` (Word export), `docx2pdf` + `pywin32` (optional, for visual PDF verification on Windows; needs Word installed).
- ffmpeg (only if you need to split calls over ~25 minutes before transcription).
- No paid transcription service, no dialer API subscription. The only per-run cost is the LLM audio calls.

### The one manual step: capturing the dialer session
The dialer has no public API, so the engine authenticates with the browser session cookies. Capture them once with a headed login (`smrtphone_login.py` in the sibling `_api` project): a Chromium window opens, you log in, it saves `smrtphone_state.json` at the repo root. Re-run whenever the session expires (the engine tells you when).

---

## 4. The three skills

Each skill is a directory in `~/.claude/skills/`:

```
cold-call-coach/     SKILL.md + references/rubric.md
lead-manager-coach/  SKILL.md + references/rubric.md
closer-coach/        SKILL.md + references/rubric.md
```

`SKILL.md` is the operating procedure Claude follows: when to use it, the exact commands, the grading procedure, the rules (quote everything, verify speaker labels, link every call ID, no em dashes), and the failure modes. `references/rubric.md` is the scoring standard.

All three share one engine and one triage. The only difference is which rubric applies to which `pipeline` bucket from the triage step.

### Rubric structure (identical shape, different content)
Every rubric has:
- **How to Use** (4 steps: gate, auto-fail scan, score each criterion 0-5, compute weighted total).
- **Applicability Gate** (which calls get scored; voicemails/wrong-numbers get a log entry, not a score).
- **Auto-Fail Conditions** (compliance/conduct: DNC ignored, misrepresentation, abuse; overrides the numeric score).
- **6 weighted categories** summing to 100%, each with 3-6 criteria rows: Criterion | What Great Looks Like (with a real playbook quote) | Common Failure | Score Anchors (0, 3, 5).
- **Grade Bands** with a coaching directive each.
- **Tonality Evaluation Guide** (transcript-observable proxies only).
- **Perfect Call Definition** and **Coaching Output Format** (the exact template every report fills in).

The category weights per pipeline (this is where the three diverge):

| | Cold Calling | Lead Management | Closing |
|---|---|---|---|
| Cat 1 | Opener + Decision-Maker Confirm (15%) | Opening + Continuity (10%) | Handoff Open + Discovery Deepening (15%) |
| Cat 2 | Four Pillars Discovery (30%) | Four Pillars Qualification (30%) | Money Conversation + Offer (25%) |
| Cat 3 | Objection Handling + List Sensitivity (15%) | Roadblocks + Logistics (15%) | Objection Handling + Negotiation (20%) |
| Cat 4 | Tonality + Call Control (10%) | Rapport, Tonality, Call Control (15%) | Commitment Locking + Next Steps (25%) |
| Cat 5 | Close + Next Step / TARP (20%) | Next Action + Handoff (20%) | Tonality + Human Delivery (10%) |
| Cat 6 | Disposition + Sift Hygiene (10%) | Sift Documentation + Hygiene (10%) | ABL Discipline + Offer Gating (5%) |

Grade bands (all three): 90-100 Elite, 75-89 Strong, 60-74 Developing, 40-59 Needs Work, below 40 Retrain. Each band carries a prescribed action (Elite = save as a training tape; Retrain = pull from live dials until recertified).

### How the rubrics were built (replication)
Do not hand-write rubrics. We generated them from the company's actual playbook with a multi-agent workflow (`build-call-rubrics`), four phases:
1. **Digest** (5 parallel agents): read the playbook docs (cold caller / lead manager / closer scripts + trainings), the lead-manager training transcript, elite-call transcripts, plus outside research on call-QA scorecard design. Each returns a structured extract with exact quotes.
2. **Synthesize** (3 agents, one per pipeline): turn the digests into a full rubric with weighted categories and 0/3/5 anchors, every "what great looks like" example quoted from the source.
3. **Verify** (3 adversarial agents): try to break each rubric. They caught fabricated criteria (e.g. an invented "8-16 questions is healthy" benchmark) and removed them.
4. **Revise**: apply the confirmed fixes; install to `references/rubric.md`.

The point: the rubric only contains things the company's playbook actually says, verified, not coaching cliches. Feed it your own playbook and you get your own rubrics.

---

## 5. The engine, script by script

All scripts live in `src/call_coaching/`, run from the repo root with the venv python. Total ~1,350 lines. Order below is the run order.

### `pull_calls.py` (193 lines) - get the calls + audio
- Authenticates with the saved dialer cookies (`smrtphone_state.json`).
- Calls the dialer's internal call-log endpoint (`POST /logs/calls/filtered`, a DataTables server-side query) and pages through the whole log.
- Each row yields: duration, disposition, caller name, the CRM record URL, and a direct recording URL.
- Filters to the calls worth reviewing (`--min-seconds 60` by default; `--include-dispositions "Correct Number"` pulls all correct-contact calls down to a 15s floor), downloads the MP3s to `recordings/`.
- Writes `call_log.json` (everything) and `calls_to_review.json` (the filtered set).
- Session expired -> exits 2 with a clear message telling you to re-run the login capture.

Key flags: `--min-seconds`, `--include-dispositions`, `--days N`, `--max-calls N`, `--list` (survey only, no downloads).

### `transcribe.py` (216 lines) - audio to graded-ready transcript
Two LLM passes per recording, parallelized (`WORKERS=4`), model `google/gemini-2.5-flash`:
1. **Audio -> transcript.** Diarized (AGENT / SELLER / VOICEMAIL), with bracketed delivery notes inline (`[long pause 4s]`, `[interrupts]`, `[warm tone]`) and a `DELIVERY SUMMARY` block (pace, energy, talk-balance percentages, notable audio moments). The AGENT/SELLER labels are decided by content, anchored on the caller name, because on callbacks the seller speaks first and naive diarization swaps them.
2. **Transcript -> triage JSON.** `call_type` (conversation / voicemail / no_contact / wrong_number / dead_air), `pipeline` (cold_call / lead_management / closing), `worth_grading` (bool), one-line summary.
- Writes `transcripts/{call_id}.md` (metadata header incl. the recording URL and CRM record link, transcript, delivery summary) and `transcripts/{call_id}.json`.
- Aggregates into `review_queue.json`, grouped by pipeline, with a `not_gradeable` bucket.
- Applies the roster maps (Section 8): `EXCLUDED_CALLERS` are skipped entirely; `CALLER_ROLES` pins a caller's pipeline so a "follow-up sounding" call from a known cold caller is still graded as a cold call.

### `enrich_records.py` (119 lines) - property address per call
- For every graded call, extracts the property address spoken on the call (a cheap parallel LLM pass over each transcript) and pairs it with the CRM record URL from `call_log.json`.
- Writes `record_map.json`: `{call_id: {address, owner, contact, reisift_url, to_num}}`.
- ~85% of calls yield a clean street address; the rest fall back to the owner name (address never spoken on estate/inherited calls). The record link is always present.

### GRADING (not a script - Claude via the skills, see Section 7)

### `add_property_column.py` (148 lines) - make rows identifiable
- Injects a "Property (opens record)" column into every markdown table whose first column is a Call ID, with the address linked to the CRM record. Adds a `- property:` line to each per-call report header.
- Idempotent (skips already-patched files). Falls back to `call_log.json` for calls not in `record_map.json` (e.g. gate-skipped wrong numbers), and to the owner name when no street was spoken.
- Run after grading, before export.

### `export_docs.py` (463 lines) - Word rendering
- Patches every per-call report with a Recording line (streaming URL + local MP3 path).
- Renders every report, scorecard, and summary to styled Word at `reports/docx/`: navy shaded table headers, right-aligned numbers, a color-coded score banner per call (green Strong, amber Developing, orange Needs Work, red Retrain), band-colored cells, a "Listen to this call" callout under the title. Crucially it unwraps the fenced CALL GRADE REPORT block so scores render as real tables, not a monospace wall.

### `export_html.py` (213 lines) - Google Docs rendering
- Same design system as the Word exporter but emits inline-styled HTML. Upload via the Drive connector as `text/html` and Google converts it to a styled Google Doc (colors preserved). Very large docs can go up as `text/markdown` instead (clean tables, links preserved, color lost) for reliability.

---

## 6. The dialer integration (the hard part), and how to redo it for any dialer

SmrtPhone has no public recordings API. Everything works off the logged-in web session. This is the reusable method:

1. **Capture the session.** Headed Playwright login saves cookies + localStorage to `smrtphone_state.json`. All later calls are plain HTTP with those cookies, no browser.
2. **Find the endpoints without guessing.** SmrtPhone is a Symfony app using FOSJsRouting, which publishes its entire route table at `GET /js/routing?callback=fos.Router.setData` (~1,187 routes). We dumped it and grepped for `call`, `record`, `log`. That surfaced `POST /logs/calls/filtered` (the call log) directly. Guessing endpoint paths just 404s; find the app's route manifest or sniff the XHR instead.
3. **Learn the payload by sniffing.** We opened the call-log page under headless Playwright and captured the actual `/logs/calls/filtered` XHR: a DataTables form (columns id, user, created_at, direction, status, disposition, from/to number, price, duration, recording_sid, ...). `pull_calls.py` replays that form.
4. **The recording URLs are public once known.** Each row's `hasRec` field is a direct `https://rec.smrtphone.io/{uuid}.mp3` that downloads with no auth. (Yours may differ; some dialers require the session cookie on the audio fetch too.)
5. **The CRM record link** rides along in the log row (SmrtPhone stores it in the `podio_id` field, which for this tenant is the reisift record URL).

For a different dialer: capture the session, find the call-list request (route manifest or DevTools network tab), replay it, and locate the recording URL + record link fields. The rest of the pipeline is dialer-agnostic.

Gotchas we hit:
- An empty/malformed POST to the log endpoint hangs the server; always send the full DataTables form.
- The web app is a React SPA; the HTML shell has no data. You must call the JSON endpoints, not scrape the page.

---

## 7. The grading half (Claude + Workflow)

Grading is not a script; it is Claude following a skill, fanned out with the Workflow tool for volume. The pattern:

**Per call (one agent each):**
1. Read the pipeline's `references/rubric.md` in full.
2. Read `transcripts/{call_id}.md` (transcript AND delivery summary). Verify AGENT/SELLER labels by content first; correct if swapped.
3. Re-check the gate: if it is not a real conversation (voicemail, wrong-number-only, internal test call), skip it with a reason and write no report.
4. Score every criterion 0-5, each with a verbatim quote. Apply short-call proration per the rubric.
5. Compute the weighted score, assign the band, write the report to `reports/{pipeline}/{call_id}_{caller}.md` using the rubric's Coaching Output Format exactly.

**Verify (one adversarial agent per report, pipelined):** re-read the transcript and check that every quote in the report actually exists, the weighted math and N/A proration follow the rubric, the band matches the score, no fabricated quotes, no em dashes. Fix confirmed defects in place. This pass routinely catches and repairs ~1 in 3 reports. It is the anti-hallucination layer and it is not optional.

**Roll up (barrier, then parallel):** one agent per caller reads all that caller's reports and writes the scorecard (scores table, average + trend, the one recurring gap with quotes, two strengths, one drill with a binary pass condition). Then one agent writes the team summary, and one writes the combined review.

The canonical shape (see the Workflow tool docs): `pipeline(calls, grade, verify, fix)` so each call verifies as soon as it is graded, then a `parallel(...)` barrier for the rollups that need every result. For fewer than ~10 calls you can grade inline without the workflow.

**A standing feature worth copying: the "Did yesterday's coaching land?" check.** Each run samples the newest day's calls and checks whether the specific advice from the prior run showed up (did the opener change, did voicemails start including a callback number). This turns the report from a scorecard into a feedback loop and is where the real value is: our data showed one caller adopted the coached opener (inputs changed) while her score still fell, which correctly exposed the next gap.

---

## 8. Configuration and roster

Two maps in `transcribe.py`, keep them current when the team changes:

```python
CALLER_ROLES = {           # pin a caller's pipeline (they only make this kind of call)
    "tinaa george": "cold_call",
    "adriana mondragon": "cold_call",
}
EXCLUDED_CALLERS = {        # departed; never queue their historical calls
    "javier marroquin",
    "john yunque",
}
```

**Current active roster (2026-07): Tinaa George and Adriana Mondragon, both cold callers.** Javier Marroquin and John Yunque departed and are excluded. (Rami Eid appears once as an internal audio-test call, gated out, never a real team member.)

Why the maps exist: the LLM triage sometimes mislabels a first-touch call as "lead management" when the caller happens to reference prior contact. Pinning known cold callers to `cold_call` prevents grading them against the wrong (lead-manager) rubric, which would unfairly penalize them for skipping duties they were never doing.

Environment (`.env` at repo root):
- `OPENROUTER_API_KEY` - the only required key for the engine.
- Dialer session lives in `smrtphone_state.json`, not `.env`.

Tunables: `MODEL` (transcription/extraction model), `WORKERS` (parallelism, 4-6), `PAGE_SIZE` (log paging), `--min-seconds` / `--include-dispositions` (what counts as reviewable).

---

## 9. End-to-end run (the whole thing, in order)

```bash
cd <repo root>

# 1. Pull log + recordings (all correct-contact conversations, incl. short ones)
.venv/Scripts/python.exe src/call_coaching/pull_calls.py --min-seconds 60 --include-dispositions "Correct Number"

# 2. Transcribe + triage (writes transcripts/ and review_queue.json)
.venv/Scripts/python.exe src/call_coaching/transcribe.py

# 3. Extract property address per graded call
.venv/Scripts/python.exe src/call_coaching/enrich_records.py

# --- GRADING (Claude): invoke the cold-call-coach skill; it reads review_queue.json,
#     grades the cold_call group via the Workflow fan-out, writes reports + scorecards. ---

# 4. Inject the Property (opens record) column into every table
.venv/Scripts/python.exe src/call_coaching/add_property_column.py

# 5. Render Word docs
.venv/Scripts/python.exe src/call_coaching/export_docs.py

# 6. Render Google-Docs HTML for the headline docs (then upload via the Drive connector)
.venv/Scripts/python.exe src/call_coaching/export_html.py output/call_coaching/reports/_team_summary.md /tmp/summary.html
```

Outputs: `output/call_coaching/reports/` (markdown), `.../reports/docx/` (Word), and whatever you upload to Drive.

---

## 10. Lessons learned (save your colleague the pain)

- **Grade from audio, not text.** The single biggest quality win is that tonality observations come from a model that heard the call. A text-only transcript cannot tell you the agent sounded rushed or monotone.
- **Verify every report adversarially.** A single LLM grading pass hallucinates plausible quotes and arithmetic. The refute-pass caught fabrications in roughly a third of reports. Budget for it.
- **Speaker labels swap on callbacks.** When the seller returns the call and speaks first, naive diarization labels them AGENT. Anchor on the caller name and decide by content, and have the grader double-check before scoring.
- **Prorate short calls, do not discard them.** Most correct-contact calls are short "no"s. Grading them only on the criteria they could reach (opener, one conversion attempt, disposition) is what makes the data honest, and it surfaced the biggest team gap (nobody was making a conversion attempt on the no).
- **Pin caller roles.** Auto-triage will occasionally route a cold call to the lead-manager rubric and tank the score for the wrong reason. The `CALLER_ROLES` map is a two-line fix.
- **Inject links and columns programmatically.** Hand-editing 45-row tables to add record links guarantees drift. The `add_property_column.py` + link-in-every-row rules keep every deliverable consistent.
- **Transcription can garble spoken addresses.** That is exactly why the property cell links to the authoritative CRM record; the clean address lives there.
- **Find the route manifest before sniffing.** For a Symfony/FOSJsRouting dialer the full endpoint list is one request away. Saved hours of guessing.

---

## 11. Replication checklist

- [ ] Stand up a repo with a Python 3.12 venv; `pip install python-docx playwright` and `playwright install chromium`.
- [ ] Get an `OPENROUTER_API_KEY` (or any audio-capable LLM) into `.env`.
- [ ] Capture your dialer's browser session to a state file (headed Playwright login).
- [ ] Identify your dialer's call-list request and recording-URL field (route manifest or DevTools). Adapt `pull_calls.py` to it (this is the only dialer-specific code).
- [ ] Point `transcribe.py`, `enrich_records.py`, `add_property_column.py`, `export_docs.py`, `export_html.py` at your paths; they are otherwise dialer-agnostic.
- [ ] Build your rubrics from your own playbook with the `build-call-rubrics` workflow (digest -> synthesize -> verify -> revise). Install to each skill's `references/rubric.md`.
- [ ] Copy the three skill directories to `~/.claude/skills/` and edit the SKILL.md commands/paths.
- [ ] Set `CALLER_ROLES` and `EXCLUDED_CALLERS` for your team.
- [ ] Do one dry run on 3-5 calls, eyeball the reports, then scale to the full day/week via the Workflow fan-out.

---

## 12. File map

```
src/call_coaching/
  pull_calls.py          dialer log + recording download
  transcribe.py          audio -> transcript + delivery notes + triage
  enrich_records.py      property address per graded call -> record_map.json
  add_property_column.py inject Property(->record) column into tables
  export_docs.py         styled Word rendering
  export_html.py         styled Google-Docs HTML rendering
  HANDOFF.md             this document

~/.claude/skills/
  cold-call-coach/     SKILL.md + references/rubric.md   (6 categories, opener/pillars/TARP focus)
  lead-manager-coach/  SKILL.md + references/rubric.md   (qualification/next-action/STAB-EM focus)
  closer-coach/        SKILL.md + references/rubric.md   (money conversation/negotiation/commitment focus)

output/call_coaching/          (gitignored)
  call_log.json                every call pulled
  calls_to_review.json         filtered reviewable set
  recordings/{call_id}.mp3      downloaded audio
  transcripts/{call_id}.md|json transcript + triage
  review_queue.json            grouped by pipeline
  record_map.json              address + record link per call
  reports/
    {pipeline}/{call_id}_{caller}.md    per-call coaching report
    {pipeline}/_scorecard_{caller}.md   per-caller scorecard
    _team_summary.md
    review_*.md                          combined reviews
    docx/                                Word exports (mirror the tree)
```

Questions on any of this: the SKILL.md files carry the operating detail, and each engine script has a module docstring explaining its endpoints, payloads, and gotchas.
