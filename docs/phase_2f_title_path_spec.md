# Phase 2f — Title-Path Classifier (buildable spec)

**Status:** Ready to build. Decisions locked (bottom). Runs after Step 3c/3d (deeds + PVA) and **before DM assignment** (2c). Parent: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md). Evidence: [probate_enrichment_lessons.md](probate_enrichment_lessons.md) — the CourtNet executor is the **wrong DM ~26% (≈33/128)** of the time because title bypasses or sits outside probate.

## Objective

Before we trust "CourtNet executor = the person who can sell," classify each lead's **title path** from the PVA owner string + latest deed (vs DOD), and set who the real DM is. This is the highest *correctness* lever in Phase 2.

## Current state (verified against code)

The pipeline already produces the inputs this classifier needs:
- **PVA owner string** — `kentucky_pva_lookup.search_by_owner` returns `PvaRow.owner` ([kentucky_pva_lookup.py:239](../src/kentucky_pva_lookup.py#L239)); Step 3d (`probate_property_lookup`) runs in [enrichment_pipeline.py:456-465](../src/enrichment_pipeline.py#L456-L465).
- **Deed history + dates** — `_fetch_deed_list`/`_parse_deed_list` ([jefferson_deeds_scraper.py:1111,1155](../src/jefferson_deeds_scraper.py#L1111)) give grantor/grantee/doc_type; `_fetch_pdetail` returns **`instrument_date`** ([:1277](../src/jefferson_deeds_scraper.py#L1277)) — the deed-date we compare to DOD. Step 3c deed history runs in the Jefferson block ([enrichment_pipeline.py:425](../src/enrichment_pipeline.py#L425)).
- **Trust detection already half-exists** — `_search_names_unique` rejects trust/LLC rows via `corp_re` (`\b(LLC|INC|...|TRUST|...)\b`, [jefferson_deeds_scraper.py:1093](../src/jefferson_deeds_scraper.py#L1093)). 2f **detects** trusts (keep) rather than rejecting them.
- **`date_of_death`** is on `NoticeData` (populated by the obituary enricher / docket).
- **No title-path concept exists today** — Step 3d just finds an address; nothing classifies whether probate is even the sale path. That's the gap.

## Design

New module `src/kentucky_title_classifier.py` (consistent with `kentucky_equity_estimator.py` being its own module). Hooks in as **Step 3f**, immediately after Step 3e equity ([enrichment_pipeline.py:500](../src/enrichment_pipeline.py#L500)) — at that point PVA owner + deed history are populated.

```python
def classify_title_path(notice: NoticeData) -> None:
    """Set notice.title_path + notice.dm_can_sell_without_probate in place."""
```

### Classification rules (first match wins, ordered)
1. **`no_property`** — Step 3d found nothing after the 2e name fan-out AND no deed history → true renter (Humphrey, Maupin, Peter, Skaggs). `dm_can_sell_without_probate=""`. → 2h drops.
2. **`out_of_estate`** — latest deed `instrument_date` is **after** `date_of_death` (sold post-death — Bell), OR the latest pre-death deed transferred the decedent **out** of title to an heir/third party (decedent no longer a current grantee — Caffee, Robbins, Harper, Hodges). → 2h drops/re-targets. Capture the new grantee as `current_property_holder`.
3. **`successor_trustee`** — PVA owner string matches a trust pattern (`REVOCABLE|LIVING TRUST|TRUST|QPRT|DECL(ARATION)? OF TRUST`) → DM = **successor trustee**; can sell **without closing probate**. Set `dm_can_sell_without_probate="yes"`. Flag `needs_trustee_research` (the trustee is in the Declaration-of-Trust instrument — pull the grantee chain; Sauer/Williams were ID'd this way). Cases: Sauer, Schrenger, Williams, Atlas (QPRT), Bryan, Byrd, Guss, Long, Morrison, Mudd-Keysferry, Palmer-Ball, Plymale.
4. **`surviving_owner`** — latest deed has **2+ grantees** (joint/TBE/JTWROS) AND a co-owner is **alive** (not the decedent, not in obituary `preceded_in_death`) → DM = surviving co-owner; real estate bypasses probate. `dm_can_sell_without_probate="yes"`. Cases: Hale, Karem, Koch, Pfeifer, Wagner, Layton, Martin-Bluffview.
5. **`standard_probate`** (default) — DM = CourtNet executor (2c proceeds normally). `dm_can_sell_without_probate="no"`.

### How 2c consumes it
2c (CourtNet party graph) reads `title_path`:
- `standard_probate` → DM = executor (current behavior).
- `successor_trustee` / `surviving_owner` → executor is captured but DM is the trustee / surviving owner; executor governs personal estate only. Don't overwrite the title-derived DM with the executor.
- `out_of_estate` / `no_property` → skip 2c (no probate-driven sale).

## Build tasks (ordered, each committable)

- **2f-1 — `classify_title_path` with rules 1–5 + unit tests.** Pure function over `NoticeData` (PVA owner string, deed list, DOD). *Acceptance:* table-driven tests, one per cited case — Sauer→successor_trustee, Karem→surviving_owner, Caffee→out_of_estate, Bell→out_of_estate(post-death), Humphrey→no_property, a clean case→standard_probate.
- **2f-2 — Trust-pattern detector + `needs_trustee_research` flag.** Reuse/extend `corp_re`; add successor-trustee extraction from the Declaration-of-Trust grantee chain via `_fetch_deed_list`. *Acceptance:* a trust-owner PVA string sets `successor_trustee`; the trust instrument's grantee is surfaced as the candidate trustee.
- **2f-3 — Wire as Step 3f in `run_enrichment_pipeline`** after Step 3e ([enrichment_pipeline.py:500](../src/enrichment_pipeline.py#L500)), Jefferson-gated like 3c/3d/3e. *Acceptance:* every Jefferson probate notice exits enrichment with a non-empty `title_path`.
- **2f-4 — Make 2c title-path-aware** ([kcoj_case_detail.py:411-480](../src/kcoj_case_detail.py#L411)) so the executor doesn't overwrite a title-derived DM. *Acceptance:* a `successor_trustee` notice keeps the trustee as DM even when a CourtNet executor exists.

## Schema / config
- `NoticeData`: `title_path`, `dm_can_sell_without_probate`, `pva_owner_string`, `current_property_holder` (some may already exist from Step 3c — verify and reuse), `needs_trustee_research: str = ""`.
- Config: `TRUST_OWNER_RE` pattern constant.

## Locked decisions
1. **Title path is decided from deeds+PVA, and it overrides the CourtNet executor as DM source** for trust/survivorship/out-of-estate. *Non-negotiable* — this is the whole point (~26% wrong-DM fix).
2. **`out_of_estate` and `no_property` are dropped by 2h, not silently deleted** — keep with the reason for audit (the user may still want to chase an out-of-estate heir). *Revisit* if audit volume is noise.
3. **Successor-trustee research is best-effort in v1** — set the flag + surface the Declaration-of-Trust grantee; if the trust agreement is unrecorded (private), fall back to the CourtNet executor as the contact and tag `trustee_unconfirmed` (Smith-Charles). *Revisit* with paid records if trustee-miss rate is high.
4. **"Co-owner alive" uses obituary `preceded_in_death` + 2e disambiguation**, not a paid death index, in v1.

## Out of scope
- Pulling unrecorded trust agreements (manual/paid).
- Non-Jefferson counties.

## References
- PVA owner row: [kentucky_pva_lookup.py:239,248](../src/kentucky_pva_lookup.py#L239)
- Deed dates: [jefferson_deeds_scraper.py:1242-1277 `_fetch_pdetail`](../src/jefferson_deeds_scraper.py#L1242)
- Trust detection seed: [jefferson_deeds_scraper.py:1093](../src/jefferson_deeds_scraper.py#L1093)
- Pipeline hook point: [enrichment_pipeline.py:477-500](../src/enrichment_pipeline.py#L477)
- DM assignment to make title-aware: [kcoj_case_detail.py:411-480](../src/kcoj_case_detail.py#L411)
- Parent / evidence: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md), [probate_enrichment_lessons.md](probate_enrichment_lessons.md)
