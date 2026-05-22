# Phase 2g — Auto Skip-Trace for KY Probate (buildable spec)

**Status:** Ready to build. Decisions locked (bottom). Depends on **2e** (name resolver — for the identity guard) and is gated by **2h** (fit score — to control credit spend). Parent: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md). Evidence: [probate_enrichment_lessons.md](probate_enrichment_lessons.md) — **~66% (≈85/128)** of cases had no DM phone without skip trace; it is the single most common gap.

## The reframe (important)

Skip trace is **not missing** — it's built and runs in the **daily pipeline**. The bug is narrower than "no auto skip-trace":

1. **`deep-prospect` doesn't actually trace.** [deep_prospector.py:129-148](../src/deep_prospector.py#L129-L148) `_run_level_1` only *counts existing phones* and writes the note "Would call tracerfy here in production" — it never calls Tracerfy. The daily Apify pipeline ([main.py:402-424](../src/main.py#L402-L424)) **does** call it. So leads worked via `deep-prospect` get no phones; leads through the daily scrape do.
2. **There is no death/staleness guard anywhere.** Grep for forewarn/death-index/is_deceased in the skip tracer returns nothing — the Davis (dialing a husband **dead since 2012**) and Armstrong (wrong-Barry, age 80) false positives are unguarded.
3. **The heir/DM address backfill is TN-only.** [tracerfy_skip_tracer.py:147](../src/tracerfy_skip_tracer.py#L147) calls `obituary_enricher._lookup_dm_address` whose waterfall is "Knox Tax → Serper/Firecrawl → DDG" and defaults `state … or "TN"` ([:174](../src/tracerfy_skip_tracer.py#L174)). KY heirs without a known address get **silently dropped** from the trace ([:227-229](../src/tracerfy_skip_tracer.py#L227-L229)).

So 2g = **make the existing skip trace correct + universal for KY probate**, not build it from scratch.

## What already exists and works (reuse, don't rewrite)

- **`batch_skip_trace(notices, max_signing_traces=5, lookup_heir_addresses=True, address_lookup_api_key=None)`** ([tracerfy_skip_tracer.py:188](../src/tracerfy_skip_tracer.py#L188)) — full Tracerfy batch: builds CSV, submits, polls queue, populates DM#1 phones to flat `NoticeData` fields and heirs' to `heir_map_json`. Traces all signing-authority heirs. Returns stats incl `credits_exhausted`. ~$0.02/contact.
- **Trestle scoring** — `score_record_phones(notices, api_key, add_litigator=False)` ([phone_validator.py:384](../src/phone_validator.py#L384)), `call_trestle` ([:77](../src/phone_validator.py#L77), supports `litigator_checks`), `assign_tier` + `DEFAULT_TIERS` ([:40,128](../src/phone_validator.py#L40)) — the 5-tier dial-priority system. Already invoked in the daily pipeline ([main.py:439-446](../src/main.py#L439-L446)).
- **DP-candidate gate** — both Tracerfy and Trestle currently fire only on `owner_deceased == "yes" or heir_map_json or decision_maker_name` ([main.py:403-406](../src/main.py#L403-L406)). This is the placeholder fit-gate until 2h exists.
- **Death signals already on the record** — the obituary enricher produces `preceded_in_death` (names known dead) and `survivors` (names known living) ([obituary_enricher.py:286-293](../src/obituary_enricher.py#L286-L293)). Plus the SSDI/Ancestry enricher. These are **free** inputs for the guard — no paid Forewarn needed for v1.

## Design

A thin **guard + KY-backfill layer** wrapped around the existing functions, plus fixing the `deep-prospect` call path. No replacement of Tracerfy/Trestle.

```
candidates (post-2h fit gate)
        │
        ▼
2g-A KY-aware address backfill ──→ batch_skip_trace()         [reuse]
        │                                   │ phones land on NoticeData / heir_map_json
        ▼                                   ▼
2g-B death/identity guard  ←──────── suppress stale/wrong-person phones
        │
        ▼
score_record_phones(add_litigator=True)  [reuse]  → tiers
        │
        ▼
2g-C fallback when empty: attorney (AP) → AOC-805 queue
        │
        ▼
2g-D credits_exhausted → 2j re-poll queue
```

## Build tasks (ordered, each independently committable)

### 2g-1 — Make `deep-prospect` actually skip-trace (fix the bug)
In [deep_prospector.py:129-148](../src/deep_prospector.py#L129-L148), `_run_level_1` should, when no phones exist, call `batch_skip_trace([notice])` (single-record batch) instead of writing "Would call tracerfy here in production." Respect a `--no-skip-trace` flag and the 2h gate.
- *Acceptance:* running `deep-prospect` on a deceased-owner CSV with no phones produces populated phone fields (mock Tracerfy in test); the "Would call tracerfy" string is gone.

### 2g-2 — KY-aware DM/heir address backfill
Make the address waterfall market-aware. In `obituary_enricher._lookup_dm_address` (the function `_lookup_missing_heir_addresses` calls) dispatch on `state`: **TN → Knox Tax** (existing); **KY → `kentucky_pva_lookup.search_by_owner` → people search**. Fix the `state … or "TN"` default at [tracerfy_skip_tracer.py:174](../src/tracerfy_skip_tracer.py#L174) to inherit the notice's state. Use the **2e name resolver** to feed owner-search variants.
- *Acceptance:* a KY probate notice with a signing heir lacking an address gets a KY address via PVA (not a TN Knox-Tax miss); the heir is no longer dropped from the trace batch.

### 2g-3 — Death/identity guard (net-new)
A `guard_traced_contacts(notice)` pass that runs **after** `batch_skip_trace`, **before** Trestle:
- **Death-suppression:** drop any phone/email whose associated contact name matches a `preceded_in_death` entry (Davis's husband) or an SSDI-confirmed death. Cross-check the DM name too.
- **Identity confirmation:** reuse **2e `disambiguate()`** on the traced contact's returned age/address vs. the expected DM (corroborate by `known_addresses` = decedent parcel + prior addresses, and `expected_dod`). Below threshold → mark phones `unconfirmed`, don't promote to DM#1 flat fields (Armstrong wrong-Barry).
- Record what was suppressed in a `skip_trace_guard_notes` field for audit.
- *Acceptance:* fixtures — a phone tied to a `preceded_in_death` name is dropped; a same-name-wrong-age contact is flagged `unconfirmed` not promoted.

### 2g-4 — Attorney (AP) / AOC-805 fallback when trace is empty
When `batch_skip_trace` returns no usable, guard-passing phone for the DM:
- Fall back to the **estate attorney** (`estate_attorney_name` + phone from 2c) as the contact channel, tagged `contact_via_attorney`.
- If no attorney either, set `repoll_after` / flag for an **AOC-805 petition-image pull** (petitioner address+phone by statute) — manual queue in v1.
- *Acceptance:* a DM with 0 guard-passing phones but a known attorney yields an attorney contact tagged `contact_via_attorney`; one with neither is queued.

### 2g-5 — Gate behind 2h fit + turn litigator checks on for probate
- Replace the placeholder DP-candidate filter ([main.py:403-406](../src/main.py#L403-L406)) with the **2h `wholesale_fit_score`** gate once 2h lands (until then, keep the existing filter and add a TODO).
- Call `score_record_phones(..., add_litigator=True)` for probate (currently `False`) — litigator risk matters for cold outreach (lessons doc).
- *Acceptance:* below-fit leads are not submitted to Tracerfy (credit saving verified by submitted-count); scored phones carry a litigator-risk flag.

### 2g-6 — Credits-exhausted → 2j re-poll
`batch_skip_trace` already returns `credits_exhausted` ([:220](../src/tracerfy_skip_tracer.py#L220)). When set, enqueue the unfinished records to the **2j re-poll queue** (KVS) instead of dropping them, and surface it in the Slack run summary.
- *Acceptance:* simulating `credits_exhausted=True` enqueues the remainder with a `repoll_after` date.

## Schema / config

- `NoticeData`: `skip_trace_guard_notes: str = ""` (2g-3), `contact_via_attorney: str = ""` (2g-4). Reuse existing `estate_attorney_*`, `repoll_after` (from 2j), phone/email + `heir_map_json` fields.
- Config: no new keys (TRACERFY_API_KEY, TRESTLE_API_KEY exist). Optional `SKIP_TRACE_MIN_FIT` threshold for 2g-5.

## Locked decisions

1. **Reuse `batch_skip_trace` + `score_record_phones` as-is.** 2g is a guard/backfill/wiring layer, not a reimplementation. *Non-negotiable* — they already work in the daily pipeline.
2. **Death guard sourced from free signals first** (`preceded_in_death`, SSDI, 2e disambiguation). Paid Forewarn is a future add-on, not a v1 dependency. *Revisit if* free signals miss too many stale phones.
3. **Below-confidence phones are flagged `unconfirmed`, never promoted to DM#1 flat fields.** Keeps them for manual review without auto-dialing the wrong person. *Non-negotiable* — Armstrong/Davis are the cautionary cases.
4. **Skip trace is gated behind 2h fit** to protect credits; until 2h ships, keep the existing deceased/DM filter as the gate. *Revisit* the gate definition when 2h lands.
5. **`add_litigator=True` for probate outreach.** *Revisit* only if Trestle litigator add-on cost becomes material.

## Out of scope

- Building 2h (fit score) — 2g consumes it; spec'd separately.
- Building 2j (re-poll queue) — 2g feeds it; spec'd in the parent plan.
- Paid Forewarn / death-index API integration (v2).
- Automating the AOC-805 petition-image fetch (manual queue in v1).
- TN/Knox path changes beyond making the waterfall market-aware.

## References
- Existing skip trace: [tracerfy_skip_tracer.py:188 `batch_skip_trace`](../src/tracerfy_skip_tracer.py#L188), backfill [:126](../src/tracerfy_skip_tracer.py#L126)
- Trestle scorer: [phone_validator.py:384 `score_record_phones`](../src/phone_validator.py#L384), tiers [:40](../src/phone_validator.py#L40)
- The bug: [deep_prospector.py:129 `_run_level_1`](../src/deep_prospector.py#L129)
- Daily-pipeline gate (the model): [main.py:402-446](../src/main.py#L402-L446)
- Free death signals: [obituary_enricher.py:286-293](../src/obituary_enricher.py#L286-L293)
- Identity guard dependency: [phase_2e_name_resolver_spec.md](phase_2e_name_resolver_spec.md)
- Parent / evidence: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md), [probate_enrichment_lessons.md](probate_enrichment_lessons.md)
