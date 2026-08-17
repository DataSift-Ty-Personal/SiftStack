# Grading Reliability (measured 2026-07-16)

Method: 5 real calls, each graded by 3 fully independent graders working blind
from the same rubric and transcript (15 grades total). Call mix: two full
engaged calls (4:34 and 9:15, one with swapped speaker labels), one boundary
callback call (1:36), and two short-call declines.

## Results

| Call | Type | Grader totals | Spread | Band agreement |
|---|---|---|---|---|
| A (4:34 full) | full | 46.1 / 46.0 / 48.3 | 2.3 pts | 3/3 Needs Work |
| B (9:15 full, swapped labels) | full | 31.6 / 36.8 / 36.0 | 5.2 pts | 3/3 Retrain |
| C (1:36 callback) | boundary | see note | n/a | 2/3 (gate disagreement) |
| D (0:41 decline) | short | opener 2.33/2.25/2.25, conversion 3/3/3 | 0.08 | n/a |
| E (1:23 opt-out) | short | opener 2.0/2.0/2.0, conversion 3/2/3 | 1 crit pt | n/a |

- All 3 graders independently detected and corrected the swapped AGENT/SELLER
  labels on call B.
- Per-criterion agreement: the large majority of criterion scores were identical
  across graders; no criterion differed by more than 2 points, and most
  differences were 1 point on adjacent anchors.
- Call C exposed the one systematic ambiguity: a brief call ending in an
  arranged callback was routed "short" by one grader and "full" by two. The
  gate has since been tightened: short = a DECLINE inside ~30 seconds; calls
  ending in a callback or deferral are full calls with N/A categories, and
  when in doubt graders grade full. The two full-call grades of call C landed
  64.7 and 67.5 (2.8 pts apart, same band).

## Published tolerance

- **Full-call totals are reproducible within about +/-3 points** (worst
  observed pair difference 5.2 on the longest, messiest call; typical 2-3).
- **Grade bands agreed in every same-type comparison.** Treat totals within 5
  points as equivalent performance; never rank two callers on a gap smaller
  than that.
- **Short-call scores are highly stable** (opener averages within 0.1, identical
  conversion scores in 5 of 6 pairs).

## Standing rules derived from this study

1. When totals from different runs differ by 5 points or less, treat them as
   the same score.
2. Gate doubt resolves to "full call."
3. Any auto-fail or band-boundary call (within 3 points of a band edge) should
   get a second independent grade before it drives a personnel decision.
