# Priority-1 Tag: 3-Source Owner Skip Trace + Trestle

Give every record you tag **"Priority 1"** in DataSift maximum phone coverage by
skip-tracing the **subject property owner** through three sources, then scoring the
combined numbers so you dial the best ones first.

The three sources stack because reisift merges phones by address:

1. **DataSift built-in skip trace** — already runs inside DataSift.
2. **Tracerfy** — this tool (~$0.02/record).
3. **Enformion / Endato** — this tool (~$0.10 affiliate rate, up to ~$0.35 rack; misses are free).

Then **Trestle** scores every unique number and tags the dial tier
(81–100 Dial First … ≤20 Drop).

This is **owner-only** — no heir resolution. Deep prospecting's Enformion heir path
is separate and unchanged.

---

## One-time setup

1. **Enformion credentials** in `.env` (from api.enformion.com → Keys):
   ```
   ENFORMION_AP_NAME=<Access Profile Name>
   ENFORMION_AP_PASSWORD=<Access Profile Password>
   ```
   (Tracerfy and Trestle keys — `TRACERFY_API_KEY`, `TRESTLE_API_KEY` — are already
   in your `.env`.) Never commit `.env`; it is gitignored.

2. **The tag**: in DataSift, tag the records you want maxed-out coverage on as
   `Priority 1`. Keep them on a list (the default merge target is a list literally
   named `Priority 1`; pass `--list "<Your List>"` to target another).

---

## Each run

1. **Export the segment.** In DataSift → Records → filter by the `Priority 1` tag →
   Manage → Export. Download the CSV.

2. **Dry preview** (no API calls, no spend, no CRM change) — always look first:
   ```bash
   python src/priority_skiptrace.py --csv output/priority1.csv
   ```
   It prints how many owners it would trace (entity/LLC owners are dropped — no person
   to trace) and the estimated cost.

3. **Run the chain** (bills Tracerfy + Enformion, merges phones into the list by
   address, then Trestle-scores and tags tiers):
   ```bash
   python src/priority_skiptrace.py --csv output/priority1.csv --run
   ```
   Add `--headed` to watch the browser merge steps, `--limit N` to test on a handful,
   `--no-trestle` to stop before scoring.

**Run the first one supervised.** `--run` spends money (per-record Tracerfy + per-match
Enformion) and writes phones into your live CRM. Watch a small `--limit 5 --headed`
pass end-to-end before trusting a full unattended run.

---

## What happens under the hood

- Reads the DataSift export via the shared reader (`read_philly_datasift_csv` — a
  generic DataSift-export parser despite the Philly-era name).
- Tracerfy traces each owner; the found phones merge into the list (Add-Data upsert by
  address).
- Enformion traces each owner separately; those phones merge in too, so the two
  providers **accumulate** on top of DataSift's own skip trace rather than overwriting.
- Trestle scores every accumulated number and tags the dial tier.

Reusable parts only — `batch_skip_trace` (Tracerfy), `person_search`/`enf_phones`
(Enformion, owner-level via `enformion_ftm`), `run_upload` (merge), `score_and_tag`
(Trestle). Nothing in the deep-prospecting heir path was modified.

---

## Not automated (on purpose)

The DataSift export is manual, and the run bills money + mutates the CRM, so there is
no unattended schedule. If you later want it scheduled, the piece to build first is an
automated export-by-tag (browser automation) — call it out and we'll spike it.
