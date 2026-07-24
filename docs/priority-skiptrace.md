# Priority Skip: 3-Source Owner Skip Trace + Trestle

Give any record you queue up maximum phone coverage by skip-tracing the **subject
property owner** through three sources, then scoring the combined numbers so you dial
the best ones first.

The three sources stack because reisift merges phones by address:

1. **DataSift built-in skip trace** — already runs inside DataSift.
2. **Tracerfy** — this tool (~$0.02/record).
3. **Enformion / Endato** — this tool (~$0.10 affiliate rate, up to ~$0.35 rack; misses are free).

Then **Trestle** scores every unique number on the list and tags the dial tier
(81–100 Dial First … ≤20 Drop).

This is **owner-only** — no heir resolution. Deep prospecting's Enformion heir path
is separate and unchanged.

Tool: `src/priority_skiptrace.py`.

---

## Universal queue + one-trace-per-property guard

Every priority tier (Priority 1, 2, 3, …) funnels into **one universal list**, default
**`Priority Skip`** (`--list` to override). The tool stamps every record it processes
with the global tag **`3source_skiptraced`** and **skips any record that already carries
it** — so no matter which tier a property arrives on, it gets the Tracerfy + Enformion
pass **exactly once, ever**. Entity/agency owners and `Do Not Market` / `Do Not Call`
rows are always dropped.

Per tier the workflow is identical: in DataSift, filter by the tier's tag → **Add to the
`Priority Skip` list** → export → run.

---

## One-time setup

**Enformion credentials** in `.env` (from api.enformion.com → Keys):
```
ENFORMION_AP_NAME=<Access Profile Name>
ENFORMION_AP_PASSWORD=<Access Profile Password>
```
Tracerfy and Trestle keys (`TRACERFY_API_KEY`, `TRESTLE_API_KEY`) and the DataSift login
(`DATASIFT_EMAIL` / `DATASIFT_PASSWORD`) are already in `.env`. Never commit `.env` — it
is gitignored. Enformion must be on a plan large enough for your volume (the free tier
caps at 100 lookups/month).

Playwright Chromium is required for the merge + scoring browser steps:
```
.venv/bin/playwright install chromium
```

---

## Each run

1. **Queue the records.** In DataSift → Records → filter by the priority tag →
   **Add to list `Priority Skip`**.
2. **Export.** With `Priority Skip` filtered, Manage → Export → download the CSV.
3. **Dry preview** (no API calls, no spend, no CRM change) — always look first:
   ```bash
   .venv/bin/python src/priority_skiptrace.py --csv ~/Downloads/"Priority Skip.csv"
   ```
   It reports records read, entity/agency and already-traced skips, new owners to
   trace, and the estimated cost.
4. **Run the chain** (bills Tracerfy + Enformion, merges phones into the list by
   address, then Trestle-scores and tags tiers):
   ```bash
   .venv/bin/python src/priority_skiptrace.py --csv ~/Downloads/"Priority Skip.csv" --run
   ```
   Flags: `--list "<name>"` target a different list, `--limit N` process only the first
   N (validation), `--headed` watch the browser, `--no-trestle` stop before scoring.

**Run a new setup supervised first.** `--run` spends money and writes to the live CRM.
On a first run (or after any automation change) do a `--limit 5` pass and confirm both
merges and the Trestle score come back OK before the full batch.

---

## What happens under the hood

- Reads the **REISift export** format via `_read_reisift_export` (First/Last/Business
  Name, Property address/city/state/zip, Tags). The subject **property address** is the
  merge key.
- **Tracerfy** traces each owner (`batch_skip_trace`); found phones merge into the list
  (Add-Data upsert by address).
- **Enformion** traces each owner separately (`person_search` / `enf_phones`, owner-level
  via `enformion_ftm`); those phones merge in too, so the providers **accumulate** on top
  of DataSift's own skip trace rather than overwriting.
- **Trestle** (`score_and_tag`) scores every number on the list and tags the dial tier.
- Each processed record is tagged `3source_skiptraced` (the guard) plus a dated
  `3source_skiptrace_YYYY-MM`.

Reusable parts only — nothing in the deep-prospecting heir path was modified. The
`asideOverlay` filter/manage backdrop is neutralized in `datasift_core._dismiss_popups`
so the merge and export clicks aren't intercepted.

---

## Good to know

- **Re-export after each run.** The guard reads the export CSV's tags, so re-export the
  `Priority Skip` list before the next run — then already-done records carry
  `3source_skiptraced` and are skipped. A stale export re-traces them (small cost;
  reisift dedupes phones).
- **Trestle scores the whole list each run**, so its cost scales with list size, not the
  size of the new batch. For a big list with a small new batch, `--no-trestle` on the
  trace run and a separate periodic scoring pass is cheaper.
- **Not scheduled.** The export is manual and the run bills money + writes to the CRM, so
  there is no unattended schedule. Automating the export-by-tag (browser automation) is
  the piece to build first if you want that.
