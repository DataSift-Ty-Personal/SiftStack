# More phone numbers and dial tiers for a list

DataSift skip traces a record when it lands. These tools go further for the records that matter most: a second and
a third skip trace source for the owners who still have no number, and a Trestle score on every number so your
callers dial the live ones first.

| Command | What it does | Costs |
|---|---|---|
| `python src/tracerfy_ftm.py` | Tracerfy skip trace for the owners on a list with no phone | about $0.02 a record |
| `python src/enformion_ftm.py` | Enformion person search for the same owners | about $0.10 to $0.35 a match, misses free |
| `python src/phone_tiers.py` | Trestle score and one dial tier on every untiered number | about $0.015 a number |

Each one reads your own account over your Open API key (`REISIFT_API_KEY` in `.env`), runs dry first (who it would
touch and the most it can cost), and buys nothing without `--commit`.

## The re-skips

```bash
python src/tracerfy_ftm.py                          # dry: the owners on the Foreclosure list with no phone
python src/tracerfy_ftm.py --commit                 # trace them and add the numbers to their records
python src/tracerfy_ftm.py --commit --score         # and score the new numbers into dial tiers
python src/enformion_ftm.py --limit 2 --commit      # a cheap live test of the third source
```

- `--list "<name>"` works any list (Foreclosure is the default). `--all` takes every record on it, not just the ones
  with no phone. `--csv <file>` traces a file's rows instead (address, city, state, zip and the owner's name).
- A company, trust or estate owner is left out and named: a person search cannot find a company.
- `--max-usd` (default $25) refuses a run that could cost more.
- Keys: `TRACERFY_API_KEY` for Tracerfy; `ENFORMION_AP_NAME` and `ENFORMION_AP_PASSWORD` for Enformion.

**How the numbers land.** Each record is found by its address, and only the numbers its owner does not already have
are added, so a number a caller already marked is never touched. The owner's skip trace counter goes up by one (or
the record would read as never traced and drop out of your Ready to Call presets). The first record is read back
before any other is written, and a mismatch stops the run. Nothing ever creates a record.

Owners still without a number after both sources are the ones to deep prospect.

## The dial tiers

```bash
python src/phone_tiers.py --list Foreclosure            # dry: how many numbers, what it would cost
python src/phone_tiers.py --list Foreclosure --commit   # score, write the tiers, read a sample back
```

| Activity score | Tier on the phone |
|---|---|
| 81 to 100 | Dial First |
| 61 to 80 | Dial Second |
| 41 to 60 | Dial Third |
| 21 to 40 | Dial Fourth |
| 0 to 20 | Drop |
| litigator risk | DNC instead of a tier |

Only numbers without a tier are scored, never one dispositioned wrong, dead or do-not-call. A number is bought once:
the answer is kept in `output/phone_tiers/scores.json`. Each phone ends with exactly one tier (a re-scored number
loses its old one), written once per owner; a sample of owners is read back after, and a phone that does not show
the tier written stops the run. Needs `TRESTLE_API_KEY`.
