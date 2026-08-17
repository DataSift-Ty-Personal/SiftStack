# Setup and Config

Run this once per role, or whenever the role or criteria change. The output is
`.candidate-intake-config.json` in the working folder, shaped exactly like
`assets/config.template.json`.

## What to collect from the user

Ask in one or two grouped questions, not an interrogation. Collect:

1. **Role and company.** e.g. "Cold Caller" at "Volunteer Home Buyers". The
   role name appears in outreach templates, so keep it short and natural.
2. **Must-haves.** 2-4 dealbreaker criteria. For each, write a `label` and a
   list of lowercase `keywords` that would appear in a candidate's resume,
   answers, or notes if they meet it. Keywords are substring-matched, so prefer
   stems ("wholesal" matches wholesale, wholesaler, wholesaling).
3. **Nice-to-haves.** 2-4 bonus criteria, same shape.
4. **Screener questions.** 1-3 questions the user wants every candidate asked,
   plus `ideal_keywords` that a good answer would contain.
5. **Scoring weights.** Default to the template weights (must-haves 40,
   experience 20, english 15, screeners 15, nice-to-haves 10; sums to 100).
   Only change if the user asks. `experience_full_credit_years` is the years of
   relevant experience that earns full experience credit (default 2).
6. **Tier thresholds.** Default Interview >= 75, Maybe >= 50, otherwise
   Dismiss.
7. **Sender first name** for outreach signatures.
8. **Outreach templates** per channel. Start from the template file's examples,
   adapt to the role, and read them back for approval. No em dashes ever.
9. **Gmail search query** if they hire via email. Default:
   `subject:(application OR applying OR resume) newer_than:7d`.
10. **Facebook post URLs** if they posted the job in groups, so future runs
    know where to look.

## Create or adopt the master sheet

- **Adopt:** if the user has a sheet, open it in Chrome, confirm the tab and
  header row match `references/sheet-schema.md`. If headers differ, offer to
  add the missing columns to the right of theirs rather than reordering.
- **Create:** open sheets.new in Chrome, name the sheet
  "{Role} Candidates - {Company}", and paste the header row from
  `references/sheet-schema.md` into row 1. Freeze row 1 (View > Freeze > 1 row).
- Copy the sheet URL into the config.

## Save the config

Fill every field of `assets/config.template.json`, save it as
`.candidate-intake-config.json` in the working folder, and echo a one-line
summary: role, sheet, channels enabled, weights.

## Daily sweep (offer after a couple of manual runs)

The sweep is a scheduled run that pulls Gmail and Facebook, scores, dedupes,
appends, and reports a digest. Outreach stays manual unless the user explicitly
sets `daily_sweep.auto_outreach` to true, and even then only to Interview-tier
candidates using the approved templates.

If the user's environment supports scheduled agents (Claude Code `/schedule`,
cron, or a Co-Work routine), schedule a daily prompt like:

```
Run the candidate-intake skill daily sweep: load .candidate-intake-config.json,
pull new applicants from Gmail and the configured Facebook posts, score and
dedupe them, append to the master sheet, then post a digest of new candidates
grouped by tier. Do not send outreach.
```

Set the time to the user's morning so the digest is waiting for them. If no
scheduler is available, tell the user to just say "run my candidate sweep" each
morning; the skill behaves identically.
