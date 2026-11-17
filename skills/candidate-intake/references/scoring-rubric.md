# Scoring Rubric

Scoring is done by `scripts/score_candidates.py`, never by eyeballing. The
script is deterministic: the same candidate with the same config always gets
the same score. Your job before scoring is to fill the Candidate Record fields
honestly; the script does the rest.

## How the score is built (0-100)

Five weighted components, weights from `config.scoring.weights`:

| Component | Default weight | How it is earned |
|-----------|---------------|------------------|
| Must-haves | 40 | Fraction of must-have criteria matched via keywords in the candidate's text |
| Experience | 20 | `years_experience / experience_full_credit_years`, capped at 1.0 |
| English | 15 | `english_level` mapped through `config.scoring.english_levels` |
| Screeners | 15 | Fraction of screener questions whose answer contains an ideal keyword |
| Nice-to-haves | 10 | Fraction of nice-to-have criteria matched |

Keyword matching is case-insensitive substring matching against the combined
text of `role_fit_notes`, `screener_answers`, and `raw_text`. That is why
Candidate Records must carry the raw application text.

## Filling the inputs honestly

- `years_experience`: only what the candidate stated or the resume shows.
  Ranges take the low end ("3-5 years" -> 3). Unknown stays blank (scores 0 for
  experience; that is correct, not harsh).
- `english_level`: one of `fluent`, `advanced`, `conversational`, `basic`,
  `none`. Judge from their writing quality, stated fluency, or video. If you
  cannot tell, leave it blank; the script gives neutral credit
  (`unknown_english_credit`, default 0.5) instead of guessing either way.
- `screener_answers`: verbatim answers only. If a channel never asked the
  screeners, blank. Unanswered screeners score 0, which naturally boosts
  candidates who actually answered.

## Tiers

From `config.scoring.tiers`:

- **Interview**: score >= `interview_min` (default 75)
- **Maybe**: score >= `maybe_min` (default 50)
- **Dismiss**: below `maybe_min`

The script also writes `score_reason`, one line like:
`2/2 must-haves, 4 yrs exp, fluent English, 1/2 screeners, missing: US hours`.
Present that line in the ranked review; it is the user's at-a-glance "why".

## Tuning

If the user disagrees with where candidates land, tune the config, not the
records: adjust keywords first (most misses are vocabulary, not weights), then
thresholds, then weights. Re-run the script after any change; scores update
everywhere consistently. Never hand-edit a score in the sheet, it will be
overwritten by the next re-score and hides the rubric problem.

## Running the script

```
python scripts/score_candidates.py \
  --config .candidate-intake-config.json \
  --in candidates.json \
  --out scored.json
```

Input is a JSON array of Candidate Records. Output is the same array with
`score`, `tier`, and `score_reason` filled. The script prints a one-line
summary per candidate to stderr so you can sanity-check without opening the
file.
