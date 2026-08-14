---
name: candidate-intake
description: >-
  Aggregate job applicants from many channels (Indeed, Gmail, Facebook group
  posts, Facebook Messenger, or pasted text) into one running, scored master
  Google Sheet, then review the ranked list and send screening outreach to the
  best ones. Use this whenever the user is hiring or recruiting and needs to
  collect, digest, score, dedupe, or track applicants who arrive through
  different places, or wants a single spreadsheet of everyone who applied.
  Trigger for "process these applicants", "who applied", "add candidates to my
  sheet", "score these resumes", "screen applicants", "review my Indeed /
  Facebook / email applications", "build my hiring pipeline", "set up candidate
  intake", or any request to turn incoming applications into a scored tracker
  and reach out to top candidates. Even if the user just says "I got a bunch of
  people who applied" or "here are some resumes", use this skill.
---

# Candidate Intake

Turn applicants who arrive through many different channels into one running,
scored master Google Sheet, then help the user pick who to contact and send the
outreach. This is a recruiting funnel in a box: it does not matter whether
someone applied on Indeed, emailed a resume, commented on a Facebook job post,
or sent a Messenger DM. They all land in the same sheet, scored the same way, so
the user has a single ranked view of every candidate.

```
  Indeed ─┐
  Gmail  ─┤
  FB post─┼─▶ normalize ─▶ score ─▶ dedupe ─▶  ONE master Google Sheet
  FB DM  ─┤                                          │
  paste  ─┘                                          ▼
                                    review ranked list ─▶ approved outreach
```

The guiding idea: **organize and score everyone first, then let the user decide
who gets outreach.** Never message people before the user has seen the ranked
list and chosen. That review gate is the whole point, so respect it.

## Prerequisites

This skill works almost entirely through the browser, so a community member can
use it with zero API keys or developer setup.

- **Claude in Chrome extension connected.** Gmail, Facebook, Indeed, and Google
  Sheets are all read and written through Chrome. If it is not connected, ask
  the user to connect it before proceeding.
- **A Google account with the applicant channels logged in** in that Chrome
  profile (Gmail, and Facebook if they use it).
- **A master Google Sheet.** Setup helps the user create one, or point at an
  existing sheet.
- **A config file**, `.candidate-intake-config.json`, saved in the working
  folder. It holds the role definition, scoring rubric, sheet URL, and outreach
  templates so every run and the daily sweep behave consistently.

## Step 0: Setup (run once, or when the role changes)

Before the first intake, check the working folder for
`.candidate-intake-config.json`.

- **If it is missing**, run setup: read `references/setup-and-config.md` and
  follow it. Setup collects the role and company, the must-have and nice-to-have
  criteria, the screener questions and their ideal answers, the scoring weights,
  the sender's first name, and the outreach templates. It then creates (or
  adopts) the master Google Sheet and writes the header row. Save the result to
  `.candidate-intake-config.json` using `assets/config.template.json` as the
  shape.
- **If it exists**, load it and continue. If the user says the role or criteria
  changed, re-run the relevant part of setup and save.

Do not hardcode a specific role into the workflow. The rubric is configurable on
purpose so the same skill works for a cold caller, a VA, an acquisitions rep, a
dispositions manager, or anything else the community hires for.

## The intake loop

Run these in order. Use a task list so the user can follow along.

1. **Load config.** Confirm the role and the master sheet URL out loud in one
   line so the user knows what they are adding to.
2. **Pick channels.** Ask which sources to pull this run, or default to every
   channel the config has enabled. Channels are independent; do whichever the
   user has new applicants in.
3. **Gather raw applicants** from each chosen channel. Each channel has its own
   reference with exact steps:
   - Indeed → `references/intake-indeed.md`
   - Gmail → `references/intake-gmail.md`
   - Facebook group post + Messenger → `references/intake-facebook.md`
   - Pasted email or text → `references/intake-manual.md`
4. **Normalize** every applicant into the Candidate Record (schema below).
   Collect them into a single JSON array for the run. Never invent data: if a
   field is not present in the source, leave it blank.
5. **Score** the array with `scripts/score_candidates.py` using the rubric in
   `references/scoring-rubric.md`. This adds a numeric score, a tier (Interview
   / Maybe / Dismiss), and a short reason to each record. Scoring is
   deterministic so the same candidate always lands the same way.
6. **Dedupe and append.** Read `references/sheet-schema.md`. Export the current
   sheet rows, run `scripts/prepare_rows.py` to drop anyone already in the sheet
   (matched on email, phone, or profile handle) and build the paste block, then
   write the new rows into the master sheet through Chrome. Verify the row count
   went up by what you added.
7. **Present the ranked review.** Show the user a tight, ranked summary of who
   is new this run, grouped by tier, with the one-line reason and the channel
   each came from. Then ask, in one question, **who they want to reach out to.**
   Do not send anything yet.
8. **Send approved outreach.** For the candidates the user picked, send the
   screening message on the channel that candidate came in through, following
   `references/outreach.md`. Verify each message before sending.
9. **Offer the daily sweep.** Once the user has done this a couple of times,
   offer to schedule a daily run that checks Gmail and Facebook for new
   applicants, appends them, and reports a short digest with the top new
   qualifiers flagged for approval. See `references/setup-and-config.md` for the
   scheduling snippet.

## Candidate Record schema

Normalize every applicant, from every channel, into these fields. Consistency is
what lets one sheet hold everyone.

| Field | Meaning |
|-------|---------|
| `full_name` | Candidate's full name as given |
| `first_name` | First name only, for casual outreach |
| `channel` | Where they came from: Indeed, Gmail, Facebook Post, Facebook DM, Manual |
| `date_received` | When they applied or messaged (YYYY-MM-DD) |
| `email` | Email if available |
| `phone` | Phone if available |
| `profile_url` | Indeed profile, Facebook profile, or LinkedIn if available |
| `location` | City / region if stated |
| `resume_url` | Link to resume or video if provided |
| `years_experience` | Relevant years, parsed from resume or answers |
| `english_level` | Assessment of English fluency (see rubric) |
| `role_fit_notes` | Short notes on relevant experience for the role |
| `screener_answers` | The candidate's answers to the configured screener questions |
| `raw_text` | The original application text, kept for reference |
| `score` | Filled by the scoring script |
| `tier` | Interview / Maybe / Dismiss, filled by the scoring script |
| `score_reason` | One line on why they landed where they did |
| `outreach_status` | Not contacted / Contacted / Replied / Video received / Scheduled |

## Hard rules

These are non-negotiable because they came directly from how the user works.

- **Never use em dashes in any outreach.** Not one, ever. Write with commas,
  periods, or short sentences instead. This is a strict brand rule. Proofread
  each message for the "—" character before sending.
- **Keep outreach casual and first-name only.** Open with "Hey {first_name},".
  No formal salutations, no long paragraphs.
- **Verify before every send.** Read the compose box back (screenshot or page
  text) and confirm the name matches the conversation before hitting send.
  Messaging the wrong person is the easiest mistake to make and the hardest to
  take back.
- **Never fabricate.** Do not guess emails, phone numbers, resume details, or
  screener answers. A blank field is fine and honest; an invented one is not.
- **Dedupe before you append.** The same person often applies twice, sometimes
  on two different channels. Match on email, phone, or profile handle and update
  the existing row rather than creating a duplicate.
- **Respect the review gate.** Score and log everyone first. The user chooses
  who to contact. Outreach is never automatic unless the user has explicitly
  turned that on for the daily sweep.

## Reference map

Read the file for the step you are on. Do not load them all up front.

| When you are... | Read |
|-----------------|------|
| Setting up, or scheduling the daily sweep | `references/setup-and-config.md` |
| Scoring candidates or tuning the rubric | `references/scoring-rubric.md` |
| Writing to or reading the master sheet | `references/sheet-schema.md` |
| Pulling from Indeed | `references/intake-indeed.md` |
| Pulling from Gmail | `references/intake-gmail.md` |
| Pulling from a Facebook post or Messenger | `references/intake-facebook.md` |
| Parsing a pasted email or message | `references/intake-manual.md` |
| Sending screening messages | `references/outreach.md` |

## Scripts

- `scripts/score_candidates.py` — reads a JSON array of Candidate Records plus
  the config, writes back the same array with `score`, `tier`, and
  `score_reason` filled in. Deterministic weighted scoring.
- `scripts/prepare_rows.py` — takes the scored records and a CSV export of the
  current sheet, drops anyone already present, and prints a tab-separated block
  ready to paste into Google Sheets, plus a summary of what was added vs skipped.

Both are plain Python 3 with no dependencies beyond the standard library, so
they run anywhere.
