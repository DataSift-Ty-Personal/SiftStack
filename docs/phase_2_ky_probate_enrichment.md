# Phase 2 — Kentucky Probate Enrichment Pipeline

**Status:** Queued. Phase 1 (KCOJ docket scraper) complete. This document is the scoping plan — not a locked spec. Review the "Open decisions" section before building.

## Objective

Take the 33 daily decedent records from `kcoj_scraper.py` and produce marketable leads:

1. **Identify** the decedent (done in Phase 1)
2. **Confirm property ownership** in Jefferson County, KY
3. **Find the likely executor** and their contact information
4. **Estimate equity** in the property

Records that fail step 2 (no property found) should be dropped before skip tracing to save Tracerfy/Trestle credits.

## Architecture

Runs inside the existing `enrichment_pipeline.py`, gated on `notice_type == "probate" AND state == "KY"`. Four workstreams, mostly independent:

```
KCOJ scraper (Phase 1)
        │
        ▼  decedent_name, case_number
┌───────────────────────┐
│ 2a. Jefferson PVA     │  ← HARD FILTER: drop if no property
│    property lookup    │
└───────────────────────┘
        │  parcel_id, address, assessed_value
        ▼
┌───────────────────────┐     ┌───────────────────────┐
│ 2b. Jefferson Deeds   │     │ 2c. CourtNet case     │
│    name search        │     │    detail (executor)  │
└───────────────────────┘     └───────────────────────┘
        │  mortgage_balance             │  executor_name
        ▼                               ▼
┌───────────────────────┐     ┌───────────────────────┐
│ 2d. Equity estimator  │     │ Existing deep         │
│                       │     │ prospecting pipeline  │
└───────────────────────┘     │ (skip trace, phones,  │
        │  equity_pct         │  obituary, ancestry)  │
        └──────────┬──────────┴───────────┬───────────┘
                   ▼                      ▼
             NoticeData fully populated → DataSift upload
```

## Workstream details

### 2a. Jefferson PVA property lookup

- **Source:** https://jeffersonpva.ky.gov/property-search/ (public, no login)
- **New module:** `src/kentucky_pva_lookup.py`
- **Input:** `NoticeData.decedent_name`
- **Output fields populated:** `address`, `city`, `state`, `zip`, `parcel_id`, `tax_owner_name`, plus a new or reused assessed-value field
- **Critical behavior:** if PVA returns no match, set a `no_property_found` flag and drop from pipeline (save credits)
- **Unknowns requiring recon:** form structure, CAPTCHA presence, rate limits, how name normalization works (does "SMITH, JOHN" match "JOHN SMITH"?), handling of multiple hits per name
- **Budget:** 3–4 hours

### 2b. Jefferson Deeds name search

- **Source:** https://search.jeffersondeeds.com/ (existing, HTTP-only)
- **Extend:** [src/jefferson_deeds_scraper.py](../src/jefferson_deeds_scraper.py) — add owner-name search alongside existing instrument-type/date-range search
- **Input:** `decedent_name` (from PVA-normalized form)
- **Output:** deed history — acquisition deed (original purchase price), any mortgages (original loan amount + date), any liens
- **Derived field:** `mortgage_balance` (rough estimate: original amount × amortization curve from years elapsed; assume 30-year fixed at 6% unless loan type is obvious)
- **Budget:** 2 hours

### 2c. KCOJ CourtNet case detail

- **Source:** https://kcoj.kycourts.net/CourtNet/Search/Index (guest access, terms-acceptance checkbox)
- **New module:** `src/kcoj_case_detail.py`
- **Input:** `case_number` (from Phase 1)
- **Output:** parties list from the case detail page:
  - Administrator / Executor / Fiduciary (primary target)
  - Attorney representing the estate
  - Heirs named in the petition (if visible to guest access)
- **Critical behavior:** if executor is found, populate `owner_name` and it flows into the existing deep-prospecting pipeline (phone, email, residential address via Tracerfy/Trestle)
- **Unknowns requiring recon:** guest session cookie handling, case search form structure, what fields are visible without paid access, rate limits
- **Same robots.txt caveat as Phase 1** — polite cadence, single run per day per case
- **Budget:** 4–5 hours

### 2d. Equity estimator

- **New module:** `src/kentucky_equity_estimator.py` (small — could also just be a function in enrichment_pipeline)
- **Input:** `assessed_value` (from PVA), `mortgage_balance` (from Deeds)
- **Output:** populates existing `estimated_equity` and `equity_percent` on `NoticeData` (these fields exist but are currently only Zillow-populated for TN records)
- **Fallback policy:** if mortgage balance unknown, use `assessed_value × 0.85` as a conservative equity floor (assumes 15% remaining mortgage on a paid-down home — common for probate decedents who are often older)
- **Budget:** 1–2 hours

## Schema changes

`NoticeData` may need:
- `pva_assessed_value: str = ""` (new, or reuse `estimated_value` if we want a single "value" field)
- `mortgage_balance_estimate: str = ""` (new)
- `no_property_found: str = ""` (new flag for drop audit logs)
- `executor_name: str = ""` (new — distinct from `owner_name` which is populated for non-probate records; avoids semantic overload)

Decide on consolidation vs new fields during implementation — goal is minimum churn to the 40+ existing fields.

## Daily Apify schedule integration

Phase 1 is already KVS-aware (`kcoj_seen_cases` persists across runs). Phase 2 modules need the same treatment:

- PVA name-to-parcel lookups should cache by decedent name → parcel result (to avoid re-querying PVA every day for the same recurring case)
- Deeds searches same story
- CourtNet case detail should cache by case_number since case parties rarely change once the estate opens

Cache files: `kcoj_pva_cache.json`, `kcoj_deeds_cache.json`, `kcoj_case_detail_cache.json` — all gitignored, all loaded from KVS in Apify mode.

## Open decisions (need user confirmation before build)

1. **PVA no-match policy** — drop vs. flag-for-review. Default recommendation: **drop**, with a weekly audit log of drops so spot-checks catch false negatives (e.g., decedent transferred to spouse pre-death).

2. **Existing `property_lookup.py` module** — already referenced from `main.py` for TN probate. Investigate: can it extend to KY, or is a separate Kentucky module cleaner? Expected answer: separate module, because KY/TN assessor APIs differ substantially.

3. **Equity data source** — PVA assessed value alone (free, ~70–85% of market) vs. PVA + Zillow overlay (more accurate, costs OpenWebNinja credits). Default recommendation: **PVA alone for the filter**, Zillow-enrich only for records the user has actually engaged (status moves past "New Lead").

4. **Build order** — 2a must come first (it's the drop filter). 2b, 2c, 2d can be built in any order after 2a. Recommendation: 2a → 2c → 2b → 2d. Rationale: 2c's executor contact info is the highest-value downstream signal; 2b/2d are nice-to-haves that refine existing records.

## Out of scope for Phase 2

- Kentucky counties other than Jefferson (would require a different PVA site, different deeds site, same KCOJ scraper but different division)
- CourtNet 2.0 paid subscription integration (evaluate only if guest access proves too limited)
- Probate publication notice aggregation (KY doesn't require it)
- Backfilling historical dockets (portal supports per-date only; backfill would require a long-running loop with user review)

## References

- Phase 1 scraper: [src/kcoj_scraper.py](../src/kcoj_scraper.py)
- Existing deep prospecting: [src/deep_prospecting.skill](../Skills%20for%20REI/) (REI skill library)
- Existing probate pipeline (TN): [src/property_lookup.py](../src/property_lookup.py)
- Existing deeds scraper: [src/jefferson_deeds_scraper.py](../src/jefferson_deeds_scraper.py)
