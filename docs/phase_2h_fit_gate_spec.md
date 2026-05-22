# Phase 2h — Wholesale-Fit Score / Gate (buildable spec)

**Status:** Ready to build. Decisions locked (bottom). Runs at the **end of enrichment, before skip trace (2g)** — it's the credit-protection gate. Parent: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md). Evidence: [probate_enrichment_lessons.md](probate_enrichment_lessons.md) — **~25% (≈32/128)** were weak fits the naive pipeline would still spend skip-trace credits on.

## Objective

Score each enriched lead for wholesale fit and **drop the unworkable ones before paying for skip trace** (2g). Today the only gate is "is this a DP candidate" (deceased/heir/DM); there's no fit/economics gate.

## Current state (verified against code)

- **Existing filter pattern to mirror** — `run_enrichment_pipeline` already drops records via opt-out filters: `skip_vacant_filter` ([enrichment_pipeline.py:311](../src/enrichment_pipeline.py#L311)), `skip_entity_filter` ([:337](../src/enrichment_pipeline.py#L337)), `skip_commercial_filter` ([:592](../src/enrichment_pipeline.py#L592)). 2h adds a `skip_fit_filter` step in the same shape.
- **Economics inputs already computed** — `estimated_value`, `estimated_equity`, `equity_percent` are set by Step 3e (`kentucky_equity_estimator.enrich_equity`, [enrichment_pipeline.py:488-491](../src/enrichment_pipeline.py#L488)).
- **Title path (2f)** and **lien_flags (2d)** feed the score.
- **The skip-trace gate that 2h replaces** — [main.py:403-406](../src/main.py#L403-L406) currently selects Tracerfy candidates by `owner_deceased/heir_map_json/decision_maker_name`. 2h's `wholesale_fit_score` becomes the gate.
- **`market_analyzer.py`** scores ZIPs (6-factor), not individual leads — not directly reusable, but its weighted-composite shape is a fine template.

## Design

New module `src/wholesale_fit.py`. Hooks in as the **last enrichment step** (after Step 3f title classifier), gated by `PipelineOptions.skip_fit_filter`.

```python
@dataclass
class FitResult:
    score: int          # 0–100
    drop: bool          # hard fail → exclude from skip trace
    reason: str         # e.g. "out_of_estate", "below_min_equity", "luxury_tier"

def score_wholesale_fit(notice: NoticeData) -> FitResult: ...
```

### Scoring rules
**Hard drops (score 0, `drop=True`):**
- `title_path in ("no_property","out_of_estate")` (2f) — Humphrey/Caffee/Bell/Robbins
- `estimated_value` present and below `WHOLESALE_MIN_VALUE` AND condition near-teardown / vacant-lot (Cooper, Dorsey $5K lots)
- `equity_percent` computable and **≤ WHOLESALE_MIN_EQUITY_PCT** with an active senior mortgage (Jackson-Lorene 100% LTV, Spencer) — true negative equity

**Soft demotions (keep, lower score, set reason — NOT dropped):**
- `estimated_value > WHOLESALE_MAX_VALUE` → luxury tier, lists with a broker (Atlas $1.7M, Frindel, Smith-Charles, Jewell). Down-rank, don't delete — the user may still mail.
- Thin equity after `lien_flags` (HECM, Medicaid, judgment, tax/code near value — Presley, Walker, Thompson-Hale). Subtract per active lien flag.
- DM-sophistication proxy (v1, conservative): `entity_type` set, or a manual `dm_sophisticated` flag (Williams RIA, Zacharias, Moriarty flipper). v1 does NOT auto-detect occupation — leave a hook.

**Score composition (start simple, tune later):**
- Base 50. +equity bucket (0–30). +distress signal (code/tax/foreclosure/Medicaid = motivated seller, +0–15). −luxury demotion. −per-lien haircut. Clamp 0–100.

### The gate
- `score_wholesale_fit` sets `notice.wholesale_fit_score` + `notice.fit_drop_reason`.
- `drop=True` records are excluded from the notice list returned to skip trace (like the vacant/entity filters do), with a one-line audit log per drop.
- In [main.py:403-406](../src/main.py#L403-L406), replace the DP-candidate filter with `wholesale_fit_score >= SKIP_TRACE_MIN_FIT` (keep the deceased/DM condition as a secondary requirement).

## Build tasks (ordered, each committable)
- **2h-1 — `score_wholesale_fit` + unit tests.** *Acceptance:* table-driven tests — Humphrey→drop(no_property), Bell→drop(out_of_estate), Jackson-Lorene→drop(negative_equity), Atlas→keep low score(luxury), a clean free-and-clear mid-value→high score.
- **2h-2 — Wire as final enrichment step** behind `skip_fit_filter`, mirroring the vacant/commercial filter blocks; audit-log drops. *Acceptance:* dropped leads don't appear in the enriched output; counts logged.
- **2h-3 — Make skip trace consume the gate** ([main.py:403-406](../src/main.py#L403-L406)). *Acceptance:* below-fit leads are not submitted to Tracerfy (submitted-count drops); the change is also reflected in the deep-prospect path (2g-1).
- **2h-4 — Config knobs + Slack summary line** (how many dropped, by reason). *Acceptance:* run summary shows fit-gate drop breakdown.

## Schema / config
- `NoticeData`: `wholesale_fit_score`, `fit_drop_reason`, `dm_sophisticated: str = ""` (manual hook).
- Config: `WHOLESALE_MIN_VALUE` (e.g. 30000), `WHOLESALE_MAX_VALUE` (e.g. 450000 — tune to buyer box), `WHOLESALE_MIN_EQUITY_PCT` (e.g. 10), `SKIP_TRACE_MIN_FIT` (e.g. 40).
- `PipelineOptions`: `skip_fit_filter: bool = False`.

## Locked decisions
1. **Hard-drop only the unworkable** (no property, out of estate, true negative equity). Everything else is a **soft demotion that stays in the list** with a score+reason. *Non-negotiable* — never silently lose a lead the user might still mail; the gate only governs *paid* skip trace.
2. **Distress = positive signal, not negative.** Code/tax/foreclosure/Medicaid liens lower *equity* but raise *motivation*; the score reflects both. *Revisit* weights after first cohort.
3. **DM sophistication is a manual flag in v1**, not auto-detected. *Revisit* if an occupation/entity signal proves reliably extractable.
4. **Thresholds are config, not hardcoded**, so the buyer box can move without a code change. Defaults are starting points to calibrate against the first ~100 scored leads.

## Out of scope
- ML/learned scoring — heuristic v1 first, calibrate, then consider.
- Auto-detecting DM sophistication from occupation.
- ARV/repair-based MAO (that's `deal_analyzer` territory, downstream of lead qualification).

## References
- Filter pattern to mirror: [enrichment_pipeline.py:311,337,592](../src/enrichment_pipeline.py#L311)
- Economics inputs: [enrichment_pipeline.py:482-499](../src/enrichment_pipeline.py#L482)
- Gate to replace: [main.py:403-406](../src/main.py#L403)
- Scoring template: [market_analyzer.py](../src/market_analyzer.py)
- Inputs from siblings: [phase_2f_title_path_spec.md](phase_2f_title_path_spec.md) (title_path), parent plan 2d (lien_flags)
- Parent / evidence: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md), [probate_enrichment_lessons.md](probate_enrichment_lessons.md)
