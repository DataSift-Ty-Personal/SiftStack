# Phase 2e — Kentucky Name-Variant Resolver (buildable spec)

**Status:** Ready to build. Decisions locked (bottom). First task in the Phase 2 build order — every other workstream (2a PVA, 2b deeds, 2c CourtNet, 2g skip trace) depends on it. Parent plan: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md). Evidence: [probate_enrichment_lessons.md](probate_enrichment_lessons.md) — name quirks appeared in **~73% (≈94/128)** of cases.

## Objective

One module that, given a decedent name (+ optional obituary context), produces the **ordered set of name variants** to search across PVA, deeds, and CourtNet, and a **disambiguation guard** that scores candidate people so we never attach the wrong same-name person. It replaces today's scattered, partial, single-heuristic name handling.

## Current state (verified against code)

The machinery is half-built and **trapped inside the PVA module**:

- [kentucky_pva_lookup.py:500-599](../src/kentucky_pva_lookup.py#L500-L599) already has `_name_tokens()`, `_search_variations()` (LAST FIRST / LAST FIRST MIDDLE / ESTATE OF…, handles comma + natural order), and `_score_match()` (0..1: surname-required + first-name bonus + adjacency bonus + business penalty). **These are the core — promote, don't rewrite.**
- [property_lookup.py:313](../src/property_lookup.py#L313) `_maiden_name_variant()` is a **TN** penultimate-token *guess* (assumes `FIRST … MAIDEN MARRIED`). Useful fallback, but not authoritative and TN-scoped.
- [obituary_enricher.py:286-293](../src/obituary_enricher.py#L286-L293) LLM extracts `survivors` + `preceded_in_death` but **no maiden / also-known-as field** — the most reliable maiden-name source is missing.
- Two separate `_SUFFIX_RE` definitions exist ([kentucky_pva_lookup.py](../src/kentucky_pva_lookup.py) + [obituary_enricher.py:158](../src/obituary_enricher.py#L158)) — should be one.
- [kcoj_case_detail.py:341](../src/kcoj_case_detail.py#L341) `search_case()` is **case-number only** — there is no by-name CourtNet search. The maiden re-search (lesson: Jackson→GREATHOUSE returned 0 rows under JACKSON) therefore has a **dependency** (see Integration §3).

## Design

New module `src/kentucky_name_resolver.py`. It owns the canonical name primitives; PVA/deeds/obituary import **from it** (resolver imports nothing from them — no cycles).

```python
from dataclasses import dataclass

SUFFIX_RE = ...  # the single canonical suffix regex (JR|SR|II|III|IV...)

def name_tokens(name: str) -> list[str]: ...          # moved from kentucky_pva_lookup
def score_match(query: str, candidate: str) -> float: ...  # moved from kentucky_pva_lookup

@dataclass
class NameVariant:
    value: str        # normalized search string, e.g. "GREATHOUSE DOROTHY"
    fmt: str          # LAST_FIRST | LAST_FIRST_MIDDLE | ESTATE_OF | SURNAME_ONLY
    source: str       # primary | maiden_obit | maiden_positional | prior_married
                      #  | non_anglo_surname | name_change | typo_fuzzy
    confidence: float # ordering hint (search highest first, stop on strong hit)

def generate_variants(
    decedent_name: str,
    *,
    maiden_name: str | None = None,           # from obituary (preferred over positional guess)
    prior_surnames: list[str] | None = None,  # from obituary aka / name-change deeds
    enable_fuzzy: bool = False,               # typo tolerance, off by default
) -> list[NameVariant]: ...

@dataclass
class CandidatePerson:
    name: str
    age: int | None = None
    addresses: list[str] | None = None
    dod: str | None = None      # if a death index says this candidate is dead

@dataclass
class DisambigResult:
    person: CandidatePerson
    score: float
    reason: str

def disambiguate(
    query_name: str,
    candidates: list[CandidatePerson],
    *,
    expected_dod: str | None = None,
    known_addresses: list[str] | None = None,
    min_score: float = 0.6,
) -> DisambigResult | None: ...   # None → queue for manual, never auto-attach
```

## Variant generation rules (each cites the cases it fixes)

`generate_variants` emits, in confidence order:
1. **primary** — the existing `_search_variations` set (LAST FIRST, LAST FIRST MIDDLE, ESTATE OF…). Unchanged.
2. **maiden_obit** (conf ↑) — if `maiden_name` supplied, `{maiden} {first}` and `ESTATE OF {maiden} {first}`. Fixes Jackson→**GREATHOUSE** (0 rows otherwise), Ayers/Weartz, Faulkner/Higgins, Slone/Blocker, Wheatley/Prince.
3. **maiden_positional** (conf ↓, fallback only when no obit maiden) — port `_maiden_name_variant` penultimate heuristic for 4+ token names.
4. **prior_married** — for each surname in `prior_surnames`, a `{surname} {first}` variant. Fixes Underwood→Koenig→Price (3 surnames), Meehan/Marksberry, Lewis (dropped "III").
5. **non_anglo_surname** — emit a variant per surname when a compound/maternal surname is detected: Hispanic paternal+maternal (Farinas: García **and** Fariñas; Pena vs Martinez), hyphenated (Palmer-Ball, Purkhiser-Meredith → also each half), Slavic feminization (Lozinskaya↔Lozinskiy via `-aya/-iy` rule), compound (Gonzalez-Gonzalez = treat as one surname, not typo).
6. **typo_fuzzy** (only if `enable_fuzzy=True` AND exact passes returned nothing) — surname token within Levenshtein ≤1. Fixes clerk typos MEIER→"MILLER" (no — that's >1; see decision 4), "TOMPSON", "JACSON". Gated to avoid false positives.

All variants deduped preserving order (reuse the existing `dict.fromkeys` dedup).

## Disambiguation guard

`disambiguate()` = `score_match` (reuse, gives name-string similarity) **plus** corroboration so a high name score alone never wins:
- **Death guard** — drop any candidate whose `dod` is set (death index says they're dead): kills the Davis (husband d.2012) and Armstrong (wrong-Barry) false positives.
- **Age/DOD sanity** — if `expected_dod`/age is known, demote candidates whose age is implausible.
- **Address corroboration** — bonus when a candidate address overlaps `known_addresses` (decedent's parcel, prior addresses). Disambiguates 3 Thomas Shavers, 2 Richard A Wagners, 2 Donald R Riggs, wrong-Rachel-Williams.
- **Threshold** — return the top candidate only if `score ≥ min_score` (default 0.6) AND it beats the runner-up by a margin; otherwise return `None` (→ manual queue). **Never auto-attach a deed/lien/phone below threshold.**

## Integration points

1. **PVA** ([kentucky_pva_lookup.py:248 `search_by_owner`](../src/kentucky_pva_lookup.py#L248)) — replace the internal `_search_variations` call with `generate_variants(...)`; loop variants high-confidence-first, stop on a `score_match ≥ 0.6` hit. Use `disambiguate` when multiple parcels return.
2. **Deeds** ([jefferson_deeds_scraper.py:997 `_search_names_unique`](../src/jefferson_deeds_scraper.py#L997)) — feed it `generate_variants` so mortgage/lien chains under maiden/prior/co-borrower names are found (fixes Burkhart mortgage indexed only under wife MARY).
3. **CourtNet** — `search_case` is case-number only, so the resolver can't directly re-search CourtNet by name today. **Dependency:** the maiden re-search benefit is realized at the **KCOJ docket / case-number discovery** layer (where names → case numbers). This spec FEEDS that; adding a CourtNet by-name search is a separate task (flagged in parent plan 2c). For now, document and wire where name→case discovery happens.
4. **Obituary** ([obituary_enricher.py:286-293](../src/obituary_enricher.py#L286-L293)) — add `maiden_name` and `also_known_as` to the LLM extraction schema; pass them into `generate_variants`.

## Build tasks (ordered, each independently committable)

- **2e-1 — Extract & consolidate (refactor, no behavior change).** Create `kentucky_name_resolver.py`; move `name_tokens`/`score_match`/`_search_variations` + one canonical `SUFFIX_RE` out of `kentucky_pva_lookup.py`; update PVA + obituary to import from it. *Acceptance:* existing PVA/obituary tests still pass; no duplicate `_SUFFIX_RE`.
- **2e-2 — `generate_variants` with the 6 sources.** *Acceptance:* table-driven unit tests, one per cited case — Greathouse (maiden), Underwood (3 surnames), Farinas (2 surnames), Lozinskaya (feminization), Palmer-Ball (hyphen split), each asserting the expected variant appears at the right confidence.
- **2e-3 — `disambiguate` with the corroboration guard.** *Acceptance:* fixtures for 3 Thomas Shavers and a dead-spouse phone — wrong-age/dead candidates are rejected; below-threshold returns `None`.
- **2e-4 — Obituary maiden/aka extraction.** *Acceptance:* an obituary containing "née Smith" / "formerly Jones" yields `maiden_name`/`also_known_as`, and `generate_variants` consumes them.
- **2e-5 — Wire into PVA + deeds; document the CourtNet name→case dependency.** *Acceptance:* a decedent whose property is titled under the maiden name is found end-to-end via the PVA path in a local test.

## Locked decisions

1. **Single canonical module + one `SUFFIX_RE`.** Resolver owns the primitives; PVA/obituary import from it. De-dupes the two suffix regexes. *Revisit if* a cycle forces a different split.
2. **Obituary maiden > positional guess.** `maiden_obit` outranks `maiden_positional`; the penultimate heuristic is fallback-only. *Non-negotiable* — the positional guess is wrong on 3+-surname and non-Anglo names.
3. **Disambiguation default `min_score = 0.6` + margin-over-runner-up; below → manual queue, never auto-attach.** *Revisit* threshold after measuring false-attach rate on a labeled sample.
4. **Fuzzy/typo matching off by default, surname-token Levenshtein ≤1, only after exact passes fail.** Catches TOMPSON/JACSON; deliberately does NOT catch MEIER→MILLER (edit distance 2 — that needs a phonetic pass, out of scope). *Revisit* with a curated typo table if clerk typos recur.

## Out of scope

- **CourtNet by-name search** — this spec feeds the name→case discovery layer; adding a name search to `kcoj_case_detail` is a separate task (parent plan 2c).
- **TN `property_lookup` refactor** — separate market; the KY resolver may back it later but don't touch it now.
- Phonetic matching (Soundex/Metaphone) — only if Levenshtein proves insufficient.

## References
- Existing matcher to promote: [kentucky_pva_lookup.py:500-599](../src/kentucky_pva_lookup.py#L500-L599)
- TN maiden heuristic to port as fallback: [property_lookup.py:301-336](../src/property_lookup.py#L301-L336)
- Obituary schema to extend: [obituary_enricher.py:286-293](../src/obituary_enricher.py#L286-L293)
- CourtNet (case-number-only) consumer: [kcoj_case_detail.py:341](../src/kcoj_case_detail.py#L341)
- Parent plan / evidence: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md), [probate_enrichment_lessons.md](probate_enrichment_lessons.md)
