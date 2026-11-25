---
name: team-performance-chain
description: >
  Know what the team did, coach it, hire, and write the work down. Pulls dials, conversations, correct numbers, leads, appointments and pace against your goals from your DataSift account on the Open API key, grades real calls with the cold call, lead manager and closer coaches (one note per caller, read by the owner first), takes a hire from the role and its one number to a scored applicant sheet, turns screen recordings into SOPs with agent-runnable twins, and posts one daily scoreboard. Use when someone says "how did the team do this week", "pull my KPIs", "grade my callers", "coach these calls", "I need to hire a cold caller", "process these applicants", "turn this recording into an SOP", or "set up a daily scoreboard".
---

# Team and performance

This chain answers three questions every week: what did the team do, how good were the calls, and
who and what do we need next. The result is a daily scoreboard, a weekly KPI report, one coaching
note per caller, a scored hiring pipeline, and SOPs a new hire (or an agent) can follow. It is for
the owner or the operations lead who runs the team.

## Who does what

In Claude Code, one session runs every step.

On the Slack team, Quinn owns the chain. Times are business-local:

| Step | Agent | When |
| --- | --- | --- |
| 1. KPIs | Quinn (`kpi_report` Mondays 08:00, `goal_sheet` Mondays 07:40, `contact_rate` Mondays 09:10, `kpi_alarms` weekdays 19:25) | on schedule, or "KPIs for last week" |
| 2. Coaching | Quinn (`coaching_picks` weekdays 19:30 picks each caller's two best and two worst calls; `call_coaching` grades them) | on schedule, or "grade the latest picks" |
| 3. Hiring | in conversation (team-hiring and candidate-intake work through your own Chrome); Quinn's `new_hire_week` (weekdays 18:45) grades a new hire's first five days of calls | when you start a hire |
| 4. SOPs | Quinn writes it in the thread with playbook-creator (`sop_writer` is planned) | on an ask |
| 5. Scoreboard | Quinn (`kpi_tracker` weekdays 07:45, 12:30, 19:30) edits one message a day | on schedule |

The handoffs: a KPI miss names the caller whose calls get graded first; a rubric point missed across
callers becomes an SOP or a training; a role that keeps breaking becomes a hire. Coaching notes go
to the owner first, never straight to a caller.

## Before you start

| Key or account | Used for | Cost |
| --- | --- | --- |
| `REISIFT_API_KEY`, your DataSift Open API key (an account admin creates it; it never expires) | KPIs and the scoreboard | free; reads only. DataSift allows 120 requests a minute per account, shared by every tool on the key, and the puller paces at about 80 |
| A dialer connected to DataSift that writes calls into the activity log (Kixie, smrtPhone, smrtDialer and Aircall do) | dials and conversations in the KPIs | your dialer's plan |
| SmrtPhone, with call recording on | the coach skills | your SmrtPhone plan, and a one-time sign-in in the browser window the script opens |
| `OPENROUTER_API_KEY` | transcribing calls and screen recordings | about $0.002 an audio minute; a week of coaching for 3 callers (about 60 calls of 2 minutes) is about $0.25 |
| Claude in Chrome, with your Google account | candidate intake (Gmail, Facebook, Indeed, one Google Sheet) | free |
| A Slack incoming webhook (optional) | the daily scoreboard post | free |
| `ffmpeg`, and Node with `docx` and `@mermaid-js/mermaid-cli` | SOP video frames and the Word build | free |

Install the component skills:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-core@siftstack
/plugin install kpi-engine@siftstack
/plugin install cold-call-coach@siftstack
/plugin install lead-manager-coach@siftstack
/plugin install closer-coach@siftstack
/plugin install team-hiring@siftstack
/plugin install candidate-intake@siftstack
/plugin install playbook-creator@siftstack
```

Then ask for the SiftStack doctor (from `siftstack-core`): it says which keys are set and what each
missing one costs.

Run everything from your SiftStack folder (the starter folder or a clone of
github.com/DataSift-Ty-Personal/SiftStack), with `REISIFT_API_KEY` and `OPENROUTER_API_KEY` in its
`.env`. A component skill's own script is written as `<skill>/scripts/<file>`: it sits in that
skill's installed folder, or under `skills/` in a clone. The KPI puller writes its reports beside its
own script and the coach scripts write under the folder you run them from, and a plugin update
replaces the installed folder, so the steps copy those scripts into `output/team-performance-chain/`
once and run the copies.

## Parameters

- **days** (optional, default: 7): the trailing days the KPI report covers.
- **date_from** (optional, default: none): a start date (YYYY-MM-DD); with date_to it replaces days.
- **date_to** (optional, default: today): the end date.
- **tz** (optional, default: America/New_York): the time zone days are counted in.
- **coach_days** (optional, default: 7, and 14 for closing calls): how far back to pull recordings.
- **min_seconds** (optional, default: 60): the shortest call worth grading.
- **max_calls** (optional, default: 20): recordings downloaded per run, newest first.
- **slack_webhook** (optional, default: none): where the daily scoreboard posts.
- **role** (optional, default: none): the role being hired (data manager, prospector, lead manager, acquisitions, dispo).
- **sop_source** (optional, default: none): a narrated screen recording, or a transcript.
- **budget_usd** (optional, default: 5): the most a week's transcription may spend.
- **dry_run** (optional, default: true): survey the calls before downloading any (`pull_calls.py --list`), and send no applicant anything until the owner picks.

## Steps

### 1. Pull the KPIs on the key

Read the account's activity log for the period and grade it: dials, answered calls, conversations,
correct numbers, dispositions, leads (new lead statuses included), appointments, talk time, per
caller and per day, with pace toward appointments and contracts.

Copy `kpi-engine/scripts/pull_kpis.py` into `output/team-performance-chain/kpi/` once, then from
your SiftStack folder:

```
python output/team-performance-chain/kpi/pull_kpis.py --selftest
python output/team-performance-chain/kpi/pull_kpis.py --days 7 --xlsx --detail
python output/team-performance-chain/kpi/pull_kpis.py --from <YYYY-MM-DD> --to <YYYY-MM-DD> --xlsx
python output/team-performance-chain/kpi/pull_kpis.py --show-benchmarks
```

- The script reads `REISIFT_API_KEY` from the environment or a `.env` in the folder you run from, and
  sends only GET requests: it never changes the account. `--selftest` checks the counting rules with
  no network.
- Targets live in a `benchmarks.json` beside the copy: your admin users under `excluded_callers`,
  your custom lead statuses under `lead_statuses`. The ranges in
  `kpi-engine/references/kpi-catalog.md` are starting points to tune.
- Output: `kpi_<from>_<to>.md`, `.csv`, `.xlsx`, and with `--detail` a `_records.csv`, all in
  `output/team-performance-chain/kpi/`.
- Slack team: Quinn posts the week's report on Mondays; "KPIs for last week" asks for it.

This step needs the kpi-engine version that runs on the Open API key: `pull_kpis.py --help` says "on
your Open API key". If yours says anything else, update the skill (in `/plugin`, under the siftstack
marketplace) before you run it. You MUST NOT give the script anything from DataSift but the Open API
key, because the key is the supported way in and it never expires. You MUST NOT collapse the answer,
conversation and contact rates into one "connect rate", because an answer includes voicemail
pickups and the three move apart in practice.

**Check:** run `--selftest`; then pick one caller and one day and compare the report's dials with
the dialer's own count; confirm leads include the New Lead statuses.

**Then:** the per-caller table (conversations under the floor, dials per correct number) picks whose
calls step 2 grades first.

### 2. Coach the calls

Grade real conversations against the DataSift rubrics: `cold-call-coach` for first-touch dials,
`lead-manager-coach` for follow-up and qualification calls, `closer-coach` for offer and negotiation
calls. One note per caller comes out of it.

The coach skills read SmrtPhone only. They pull the call log and recordings through your SmrtPhone
sign-in, because SmrtPhone offers no other way to reach them. If your team dials on another dialer,
these skills cannot pull your calls today: export a handful of recordings from your dialer,
transcribe them, and grade them against the same rubric by hand (the "Coaching without a dialer API"
section of `docs/setup/no-api-playbook.md`).

The three coach skills ship the same scripts. Copy the files in the cold-call-coach skill's scripts
folder into `output/team-performance-chain/coaching/`, put a `.env` with `OPENROUTER_API_KEY` there
(and, if you like, a `roster.json` made from `roster.example.json` to pin each caller's role and hide
callers who left), then work inside that folder:

```
cd output/team-performance-chain/coaching
python smrtphone_login.py
python pull_calls.py --list --days 7
python pull_calls.py --min-seconds 60 --days 7 --max-calls 20
python transcribe.py --limit 20
```

- `smrtphone_login.py` runs once: sign in to SmrtPhone in the window it opens. It saves
  `smrtphone_state.json`, a live sign-in that lasts weeks.
- `--list` surveys the log with no downloads. The transcription sorts each call into cold_call,
  lead_management, closing or not gradeable (`output/call_coaching/review_queue.json`).
- Grade each `worth_grading: true` call with its skill's rubric
  (`cold-call-coach/references/rubric.md`, `lead-manager-coach/references/rubric.md`,
  `closer-coach/references/rubric.md`), one report per call under
  `output/call_coaching/reports/<group>/`, then export each group:

```
python export_excel.py --dir output/call_coaching/reports/cold_call
python export_excel.py --dir output/call_coaching/reports/lead_management
python export_excel.py --dir output/call_coaching/reports/closing
```

- For closing calls, widen the pull to `--days 14` (or 30) when the queue is empty.
- Output: the transcripts, the per-call reports and one workbook per group, all under
  `output/team-performance-chain/coaching/output/call_coaching/`; then write one note per caller to
  `output/team-performance-chain/coaching/notes_<week>.md` for the owner.
- Slack team: Quinn grades from the recording link your dialer writes onto DataSift's call events
  (Kixie writes one that opens without a sign-in), so the Slack path is not tied to SmrtPhone. It
  spends only inside its caps, and the notes go to the owner first.

You MUST NOT commit, upload or share `smrtphone_state.json`, because it is a live sign-in to your
SmrtPhone account. You MUST grade only real conversations, because voicemails and wrong numbers
scored as calls drag every average down. You MUST NOT score anything that is not audible on the call (a disposition, a
CRM field), because the rubric grades the call. You MUST NOT rank full calls and short calls in one
column, because they are scored on different scales. Treat totals within 5 points as the same, and
get a second grade before a personnel decision at a band boundary.

**Check:** every quote in a report appears word for word in its transcript; each report's table
adds up and matches its SCORES footer; a transcript with more words than about 3 a second of audio
is invented, so do not grade it.

**Then:** the caller notes go to the owner; a rubric point most callers miss goes to step 4 as an SOP
or a training.

### 3. Hire and bring people in

Define the role and its one number before anyone applies, then collect every applicant into one
scored sheet and let the owner pick who hears back.

- `team-hiring`: what is breaking names the role; the role gets one North Star number (for a
  prospector, 150 dials a day and at least 2 real leads passed off); then the country and pay band,
  the job description, the Indeed and Facebook post, the interview against the number, and day-one
  onboarding (`team-hiring/references/roles-and-kpis.md`, `pay-and-geography.md`,
  `job-descriptions.md`, `interview-and-onboarding.md` in the same folder).
- `candidate-intake`: applicants from Indeed, Gmail, a Facebook post, Messenger or pasted text go into
  one Google Sheet, scored, deduped, and ranked. It works through Claude in Chrome on your own
  signed-in accounts; setup writes `.candidate-intake-config.json` from
  `candidate-intake/assets/config.template.json`. The two scripts:

```
python candidate-intake/scripts/score_candidates.py --config output/team-performance-chain/hiring/<role>/.candidate-intake-config.json \
    --in output/team-performance-chain/hiring/<role>/candidates.json --out output/team-performance-chain/hiring/<role>/scored.json
python candidate-intake/scripts/prepare_rows.py --scored output/team-performance-chain/hiring/<role>/scored.json \
    --sheet-csv output/team-performance-chain/hiring/<role>/current_sheet.csv --out output/team-performance-chain/hiring/<role>/rows.tsv
```

- Output: `output/team-performance-chain/hiring/<role>/` (the job description, the post, the scored
  candidates and the rows pasted into the sheet).
- Once someone starts: their daily, weekly and monthly task list, the SOPs (step 4) and their number,
  on day one. On the Slack team, Quinn's `new_hire_week` grades every answered call of their first
  five business days and holds the notes for the owner.

You MUST state the role's number before the offer, because a number introduced after someone accepts
reads as a bait and switch. You MUST NOT post a role you cannot describe in daily tasks, because the
person will fail at a job nobody defined. You MUST NOT message any applicant before the owner has
seen the ranked list and chosen, because outreach to the wrong person cannot be taken back; read the
compose box back before each send.

**Check:** after each run the sheet's row count went up by exactly the new rows; nobody appears
twice (matched on email, phone or profile).

**Then:** the hire's onboarding pulls the SOPs from step 4; their first week of calls goes through
step 2 every day.

### 4. Write the SOPs

Turn a narrated screen recording (or a transcript) into a Word SOP plus a `.sop.md` twin an agent can
run, with `playbook-creator`.

```
python playbook-creator/scripts/transcribe_video.py <recording>.mp4 \
    --frames-dir output/team-performance-chain/sops/<name>/images --crop 1920:910:0:125 \
    --out output/team-performance-chain/sops/<name>/<name>.transcript.md
node playbook-creator/scripts/build_docx.js output/team-performance-chain/sops/<name>/<name>.md \
    output/team-performance-chain/sops/<name>/<name>.docx --title "<Title>"
python playbook-creator/scripts/validate_sop.py output/team-performance-chain/sops/<name>/<name>.sop.md
```

- The transcript comes back in three parts: the narration, every action with its exact label, and
  every judgment call (which become the SOP's rules and the twin's MUST NOT lines). The frames are the
  screenshots. `--crop` cuts browser chrome from a 1080p full-screen recording.
- No `OPENROUTER_API_KEY`: paste your recorder's own transcript (Loom, Zoom and Fireflies all make
  one) and cut frames with ffmpeg at its timestamps.
- Output: `output/team-performance-chain/sops/<name>/` (the `.docx`, the `.md` source, the `.sop.md`
  twin, the frames).

You MUST validate the twin after every edit, because the twin is not done until the validator
passes. You MUST check every frame against its step and blur any customer data before the SOP leaves
the team, because a frame can catch a page mid-load or a seller's record.

**Check:** the validator exits clean; each step has its frame; the 5th grade reading level holds.

**Then:** the SOP goes into the next hire's day-one pack (step 3) and into training for the rubric
point it fixes (step 2).

### 5. The daily scoreboard

One post a day that says what the team did, so nobody has to ask.

- **Claude Code:** after the last dial each weekday, and a weekly workbook on Fridays:

```
python output/team-performance-chain/kpi/pull_kpis.py --days 1 --slack <webhook-url>
python output/team-performance-chain/kpi/pull_kpis.py --days 7 --xlsx
```

- **Slack team:** Quinn's `kpi_tracker` posts one message a day in the morning and edits it at
  midday and in the evening (an edit notifies nobody). Each tab is also published as a page and a
  CSV at a fixed signed link; a Google Sheet mirrors a tab with `=IMPORTDATA("<tab csv link>")`, no
  Google setup needed.

**Check:** yesterday's dials on the scoreboard match step 1's report for the same day. A day that
reads zero dials is a finding: confirm the run happened and the dialer wrote its calls before you
believe it.

**Then:** Monday's weekly report (step 1) and the week's coaching (step 2) start from the scoreboard's
trends.

## Stand it up

First run, on about 10 records:

1. KPIs: `--selftest`, then `--days 1`, then `--days 7 --xlsx --detail`.
2. Coaching: `pull_calls.py --list --days 7`, then `--max-calls 10`, `transcribe.py --limit 10`, and
   grade whatever is gradeable.
3. Hiring: 10 applicants pasted in through candidate-intake's manual channel.
4. One SOP from one short recording.

It is working when one caller-day of dials matches the dialer, leads include New Lead statuses, a few
calls are graded with word-for-word quotes and tables that add up, the 10 applicants come back ranked
with a reason each, and the SOP's twin validates.

Then let it run:

- **Your operating system's scheduler** (Task Scheduler or cron): the daily scoreboard post, the
  Friday workbook, and the daily call pull and transcription.
- **A Claude Code session on Fridays** for the grading, which needs Claude's judgment: "grade this
  week's calls". A Claude Code routine (`/schedule`) can run it only where it can reach the copied
  folder, its `.env` and the SmrtPhone sign-in.
- **The Slack team:** deploy the agents brain once (`python deploy/agents_up.py`, dry by default,
  then `--commit`). Quinn's KPI, coaching and scoreboard stages then run on their own.
- **Weekly:** compare the scoreboard with one hand count, read each caller's rubric trend, and read
  a new hire's first-week notes.

## What to watch

- **Dials per correct number.** About 9 when you dial scored phones, about 32 blind.
- **Conversations per caller per day.** 5 to 8 calls of 60 seconds or more, at a floor of 150 dials.
- **Leads per caller per dialing day.** 2 to 3, and 15 to 20 leads to a contract.
- **Each role's one number.** Appointments set per dial hour for a cold caller, lead to offer for a
  lead manager, offer to contract (25% or better) for a closer.
- **Rubric scores per caller, week over week.** A move under 5 points is noise.

## Built in the app

What the API cannot do, as a click-through:

1. **Connect your dialer to DataSift** (Settings, Integrations) so calls land in the activity log.
   Dialers that write only statuses (CallTools and ReadyMode) give the KPIs no dials, and texts from
   Smarter Contact and Launch Control never reach the log.
2. **Name appointment tasks so the KPIs see them.** A completed task counts as an appointment when its
   title holds "appointment", "appt", "meeting" or "consult".
3. **Turn on call recording** in SmrtPhone for every caller you want coached.
4. **A Slack incoming webhook** for the channel the scoreboard posts to.

## Where it works

- KPIs and the scoreboard: any DataSift account, any county, on the Open API key. Dials come only
  from dialers that write calls into the activity log.
- The coach skills: SmrtPhone teams. Any other dialer: grade by hand against the same rubrics. The
  Slack team's coaching works with any dialer whose call events carry a recording link that opens
  without a sign-in.
- Hiring and SOPs: anywhere.

## Gotchas

- **"Answered" is not a conversation.** Dialers mark voicemail pickups answered, and an "answered"
  call under about 30 seconds is usually a greeting. Read the duration.
- **Leads hide in New Lead.** First-touch leads land in New Lead and No Contact New Lead. Count those,
  and add your custom statuses, or leads read zero while callers produce them.
- **Admin activity pollutes caller tables.** Put owner and admin users in `excluded_callers`.
- **A dial can name nobody.** Some dialers name the caller only on the answer; unattributed dials
  still count in the account totals.
- **Audio models invent speech on short clips.** A transcript longer than about 3 words a second of
  real audio is made up. Never grade it.
- **The SmrtPhone sign-in expires.** `pull_calls.py` exiting 2, or an HTML page where data should be,
  means sign in again with `smrtphone_login.py`.
- **Excel holds a lock.** With a workbook open, a run cannot save over it. Close it first.
- **Scripts write beside themselves.** Run the copies in `output/team-performance-chain/`, never the
  installed folder, which a plugin update replaces.
