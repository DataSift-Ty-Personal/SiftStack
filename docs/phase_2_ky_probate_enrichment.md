# Phase 2 — Kentucky Probate Enrichment Pipeline

**Status:** Queued. Phase 1 (KCOJ docket scraper) complete. This document is the scoping plan — not a locked spec. Review the "Open decisions" section before building.

> **Revised after a 128-case review** (see [probate_enrichment_lessons.md](probate_enrichment_lessons.md)). The original 4-workstream happy path (PVA → deeds → CourtNet executor → equity) would mishandle ~60–70% of real cases. Capabilities **2e–2j** below were added from that evidence; the existing workstreams **2a/2c/2d** were tightened. Read the lessons doc first — every addition here is backed by named cases and frequency counts.

> **Implementation status (verified 2026-05-21):** the happy-path scaffolding is **already built and wired** — `kentucky_pva_lookup.py` (2a), Jefferson deed history (2b) at [enrichment_pipeline.py:425](../src/enrichment_pipeline.py#L425), `kentucky_pva_lookup.probate_property_lookup` (Step 3d), `kentucky_equity_estimator.enrich_equity` (Step 3e), and `kcoj_case_detail.py` (2c) all exist. The remaining work is the **correctness/coverage layer 2e–2j**, each with its own buildable spec:
> - **2e** name resolver → [phase_2e_name_resolver_spec.md](phase_2e_name_resolver_spec.md)
> - **2f** title-path classifier → [phase_2f_title_path_spec.md](phase_2f_title_path_spec.md)
> - **2g** auto skip-trace → [phase_2g_skip_trace_spec.md](phase_2g_skip_trace_spec.md)
> - **2h** wholesale-fit gate → [phase_2h_fit_gate_spec.md](phase_2h_fit_gate_spec.md)
> - **2j** re-poll + no-probate → [phase_2j_repoll_noprobate_spec.md](phase_2j_repoll_noprobate_spec.md)
>
> Before executing, **audit the existing 2a/2b/2c/2d modules against the tightened requirements** above — some watch-outs (predeceased-spouse owner strings, full-history lien sweep, maiden re-search) may be partially or fully missing in the current code.

## Objective

Take the 33 daily decedent records from `kcoj_scraper.py` and produce marketable leads:

1. **Identify** the decedent (done in Phase 1) — and reconcile decedent-vs-petitioner so we never invert them (the manual process inverted Rutter and Spencer)
2. **Resolve name variants** (maiden/prior/changed/suffix/non-Anglo) so lookups don't miss the case or attach the wrong person (~73% of cases had a name quirk)
3. **Confirm property ownership AND classify the title path** — standard probate vs. successor-trustee (revocable trust) vs. surviving-owner (joint/survivorship) vs. out-of-estate (deeded pre-death / already sold) vs. no-property (renter, drop). The title path determines *who can actually sell* — the CourtNet executor is the wrong DM ~26% of the time.
4. **Find the real decision-maker** and their contact information — full party graph, co-signer count, non-family PRs, attorney (AP) fallback
5. **Auto skip-trace the DM** with a staleness/death guard — this is the #1 gap (~66% of cases needed it), not a deferred manual step
6. **Estimate equity via a lien sweep**, not binary free-and-clear — net junior/Medicaid/tax/code liens and HECMs
7. **Score wholesale fit** and drop weak leads (no property, no/thin equity, luxury tier, sophisticated DM) before spending skip-trace credits

Records that fail ownership confirmation as **renters / no property** (Humphrey, Maupin, Peter, Skaggs) should be dropped before skip tracing to save credits — but a **trust-titled** property whose PVA owner string isn't a person's name must NOT be dropped (research the trustee instead).

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
- **Critical behavior:** if PVA returns no match, set a `no_property_found` flag and drop — BUT only after the name-variant fan-out (2e) has been tried, and only if the miss is a true renter (Humphrey/Maupin/Peter/Skaggs), not a trust-name miss. Capture the **full PVA owner string** verbatim (it feeds 2f title classification) and the **PVA "below normal condition" flag** (a distress signal — McCoomer, Mudd-Francis, Riggs, Hodges).
- **Watch-outs from real cases:** PVA owner string can still list a **predeceased spouse** (Rogers, Woods, Wagner, Tedder, Stratman) — don't treat the dead co-owner as a live signer. The user-supplied address is sometimes a **mailing address or stale residue** that isn't the subject property (Cooper/Regatta, Moffett/Schmidt, Schubert) — validate the parcel, and fall back to **owner-name search** when address-search misses (Riggs).
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
- **Critical behavior:** capture the **full party graph** (every P/EE/AA/AP/OP row), not just the executor. Then hand the DM to the title-path classifier (2f) — do NOT blindly set `owner_name` = executor, because for trust/survivorship cases the executor governs only the personal estate (Hale, Koch, Bryan). Count co-signers / co-heirs and set a "**N signatures required**" flag (Walker ~10, Rutter 11, Palmer-Ball/Mudd-Francis co-execs).
- **Watch-outs from real cases:**
  - **Maiden-name re-search is mandatory** — a `LAST=Jackson` search returned 0 rows; the case lived under maiden GREATHOUSE. Always retry under maiden/prior surnames from 2e.
  - **Reconcile DEC vs P/AA** before trusting input — the manual process inverted Rutter (Rose=admin alive, mother=decedent) and Spencer (son=DM, father=decedent, different surnames).
  - **Validate the EE/P resolves to a real person** — Preston's EE was an estate-name placeholder "PRESTON, DEAN"; Meier's petitioner was a clerk typo "MILLER".
  - **Non-family PRs (~13%)** — friend/in-law/professional/creditor executors won't share the decedent's surname or appear under a family link (Gross, Roberts, Sauer, McMenamin, Harper).
  - **Guest tier hides party addresses** (Gross, McCawley, O'Connor) — capture the **attorney (AP) as a fallback contact channel**, and flag for an AOC-805 petition-image pull (petitioner address+phone is in the petition by statute) when skip trace fails.
  - **Latency:** fresh filings return 0 rows (Parrino, McCoomer, Smith-Charles still missing at ~25 days) — enqueue for re-poll (2j), don't drop.
- **Unknowns requiring recon:** guest session cookie handling, case search form structure, what fields are visible without paid access, rate limits
- **Same robots.txt caveat as Phase 1** — polite cadence, single run per day per case
- **Budget:** 4–5 hours (raised scope: party graph + reconciliation + maiden re-search)

### 2d. Equity estimator → **lien/encumbrance sweep**

Binary "free-and-clear" is wrong ~36% of the time. This is now a lien sweep, not a subtraction.

- **New module:** `src/kentucky_equity_estimator.py`
- **Input:** `assessed_value` (PVA), the **full** deed/lien history (2b — search all pages, not the first 50; releases hide past the cap — Murphy, Mudd-Francis, Perrin)
- **Output:** `estimated_equity` + `equity_percent`, plus discrete lien-haircut flags
- **Encumbrances to net out (each is its own flag):**
  - Open mortgages **with no matching release** (presumed active — estimate, mark as estimate)
  - **HECM / reverse mortgages** — due-and-payable on death, negative-amortizing; do NOT straight-line a balance (Wheatley, Herflicker)
  - **State / judgment / credit-card liens** and **lis pendens** (Presley, Logsdon, McMenamin)
  - **Tax certificates / code-violation liens** — can exceed value on low-end homes → negative equity on a "mortgage-free" house (Walker, Thompson-Hale)
  - **Medicaid / MERP risk** — proxy via a **DMS noticed party** (Duckworth) or an **elder-law/Medicaid-specialist estate attorney** (Jenkins-Ruley, Underwood, Duckworth)
- **Fallback policy:** if mortgage balance unknown and no liens found, `assessed_value × 0.85` floor — but never report "free-and-clear = full equity" when any lien flag is set.
- **Budget:** 2–3 hours (was 1–2; lien sweep added)

## Capabilities added from the 128-case review (2e–2j)

These are the highest-ROI gaps the original happy path missed. Full evidence + counts in [probate_enrichment_lessons.md](probate_enrichment_lessons.md).

### 2e. Name-variant resolver (cross-cutting — runs before 2a/2b/2c)
- **New module:** `src/kentucky_name_resolver.py`
- **Generates** the candidate surname/format set for one decedent: maiden + prior-married (from obit/deed), legal name changes (recorded name-change instruments — Underwood, Lewis, Baker, Meehan), suffix variants (Jr/Sr/III), non-Anglo forms (Hispanic paternal+maternal, Slavic feminization, compound surnames), and known clerk-typo tolerance.
- **Same-name disambiguation guard:** when a lookup returns multiple people, score by **age + address history + DOD + obit cross-ref**; never auto-attach a deed/lien to a lead without a corroborating link (prevents the 3-Thomas-Shavers / 2-Richard-Wagners / wrong-Rachel-Williams false positives).
- **Budget:** 4–5 hours. **This unblocks ~73% of cases — build it first.**

### 2f. Title-path classifier (runs after 2a + 2b, before DM assignment)
- **New function** (in `enrichment_pipeline` or `kentucky_pva_lookup`).
- **Classifies each lead** from the PVA owner string + latest deed instrument (vs DOD) into one of:
  1. **standard-probate** → DM = CourtNet executor (2c)
  2. **successor-trustee** (owner string contains TRUST / QPRT / DECL OF TRUST) → DM = successor trustee from the trust instrument; can sell **without closing probate** (Sauer, Schrenger, Williams, Atlas, Mudd-Keysferry, Palmer-Ball)
  3. **surviving-owner** (joint/TBE/JTWROS, one owner still living) → DM = surviving co-owner, real estate bypasses probate (Hale, Karem, Koch, Pfeifer, Wagner)
  4. **out-of-estate** (deeded to heir pre-death, or already sold) → drop or re-target (Caffee, Robbins, Bell, Smith-Algonquin)
  5. **no-property / renter** → drop (Humphrey, Maupin, Peter, Skaggs)
- **Why it's pivotal:** routes the ~26% of cases where the CourtNet executor is the *wrong* DM. Sets `title_path` + `dm_can_sell_without_probate` on `NoticeData`.
- **Budget:** 4–6 hours.

### 2g. Auto skip-trace step (the #1 gap — ~66% of cases)
- **Wire Tracerfy + Trestle into the probate pipeline as a first-class auto-step** (the daily scrape already has Tracerfy; deep-prospect currently only checks if phones exist — that's the bug the data exposes).
- **Staleness/death guard:** reject phones tied to a person dead per the death index (Davis's husband d.2012; Armstrong wrong-Barry age 80). Cross-check age/relationship; when Forewarn conflicts with Clustrmaps age, defer to Forewarn.
- **Out-of-state pull** enabled (Morrison-CO, O'Connor, Poe, Perrin, Roberts-IN).
- **Fallback chain when skip trace is empty:** estate **attorney (AP)** → AOC-805 petition image → manual queue.
- **Gate:** only run after 2h fit-scoring passes, to conserve credits.
- **Budget:** 3–4 hours (mostly wiring + guard logic).

### 2h. Wholesale-fit score / gate (runs before 2g to save credits)
- **Scores each enriched lead** and drops/down-ranks weak fits (~25% of cases): no property, no/thin/negative equity-after-liens, value above a wholesale band (luxury → broker), DM-sophistication proxy (RIA/attorney/active-investor heir — Williams, Zacharias, Moriarty).
- Output: `wholesale_fit_score` + `fit_drop_reason`. Only passing leads reach the (paid) skip-trace step.
- **Budget:** 2–3 hours.

### 2j. Re-poll queue + no-probate branch (latency — ~16%, plus the ~9% with no probate)
- **Re-poll queue:** fresh CourtNet cases / obits that return 0 rows get enqueued and re-searched after 3–5 business days instead of being dropped (Parrino, McCoomer, Smith-Charles). KVS-backed, like the seen-case caches.
- **No-probate branch:** deaths that surface as **lis pendens / tax foreclosure with UNKNOWN HEIRS + Warning Order Attorney** (Combs, McGarvey, Walker, Gonzalez) route to an heir-identification path (affidavit of descent, deed-grantor history) — **this is the direct overlap with [phase_3_ky_lis_pendens_apify.md](phase_3_ky_lis_pendens_apify.md)**; build the shared heir-ID helper once.
- **Budget:** 3–4 hours.

### Revised build order
**2e (names) → 2a (PVA) → 2f (title path) → 2c (CourtNet party graph) → 2b (deeds) → 2d (lien sweep) → 2h (fit gate) → 2g (skip trace) → 2j (re-poll/no-probate).** Rationale: name resolution unblocks everything; title-path classification must precede DM assignment; fit-scoring must precede paid skip trace.

## Schema changes

`NoticeData` may need:
- `pva_assessed_value: str = ""` (new, or reuse `estimated_value` if we want a single "value" field)
- `mortgage_balance_estimate: str = ""` (new)
- `no_property_found: str = ""` (new flag for drop audit logs)
- `executor_name: str = ""` (new — distinct from `owner_name` which is populated for non-probate records; avoids semantic overload)

Added from the 128-case review:
- `title_path: str = ""` (2f — `standard_probate` | `successor_trustee` | `surviving_owner` | `out_of_estate` | `no_property`)
- `dm_can_sell_without_probate: str = ""` (2f — trust/survivorship shortcut)
- `pva_owner_string: str = ""` (verbatim PVA owner field — drives 2f, catches trust/predeceased-spouse)
- `name_variants: str = ""` (2e — pipe-delimited surnames/formats searched, for audit)
- `signatures_required: str = ""` (2c — co-heir/co-executor count)
- `attorney_name` / `attorney_phone` (2c — AP fallback contact)
- `lien_flags: str = ""` (2d — e.g. `hecm;medicaid;tax_cert;judgment;lis_pendens`)
- `wholesale_fit_score` / `fit_drop_reason` (2h)
- `repoll_after: str = ""` (2j — date to re-search a latent CourtNet/obit case)

Decide on consolidation vs new fields during implementation — goal is minimum churn to the 40+ existing fields.

## Daily Apify schedule integration

Phase 1 is already KVS-aware (`kcoj_seen_cases` persists across runs). Phase 2 modules need the same treatment:

- PVA name-to-parcel lookups should cache by decedent name → parcel result (to avoid re-querying PVA every day for the same recurring case)
- Deeds searches same story
- CourtNet case detail should cache by case_number since case parties rarely change once the estate opens

Cache files: `kcoj_pva_cache.json`, `kcoj_deeds_cache.json`, `kcoj_case_detail_cache.json` — all gitignored, all loaded from KVS in Apify mode.

## Open decisions (need user confirmation before build)

1. **PVA no-match policy** — drop vs. flag-for-review. Revised recommendation: **drop only true no-property/renter misses** (after the 2e name fan-out), and **never drop a trust-name miss** — route those to 2f trustee research. Keep a weekly audit log of drops so spot-checks catch false negatives (decedent transferred to spouse pre-death, maiden-name miss, trust title).

2. **Existing `property_lookup.py` module** — already referenced from `main.py` for TN probate. Investigate: can it extend to KY, or is a separate Kentucky module cleaner? Expected answer: separate module, because KY/TN assessor APIs differ substantially.

3. **Equity data source** — PVA assessed value alone (free, ~70–85% of market) vs. PVA + Zillow overlay (more accurate, costs OpenWebNinja credits). Default recommendation: **PVA alone for the filter**, Zillow-enrich only for records the user has actually engaged (status moves past "New Lead").

4. **Build order** — superseded by the "Revised build order" above (2e → 2a → 2f → 2c → 2b → 2d → 2h → 2g → 2j). Name resolution (2e) is now the true first step; title-path (2f) precedes DM assignment; fit-gate (2h) precedes paid skip trace.

5. **Skip trace scope (was out-of-scope, now IN)** — the original plan deferred skip trace to a separate manual step. The 128-case review shows ~66% of leads have no DM phone without it, so 2g brings Tracerfy/Trestle into the probate pipeline as an auto-step, gated behind the 2h fit score to control credit spend. Confirm the credit budget per daily run.

6. **AOC-805 petition fetch** — guest CourtNet hides party addresses ~10% of the time. Pulling the petition image (paid CourtNet doc access or probate-clerk request) recovers the petitioner's address+phone by statute. Decision: build the fallback as a *manual queue* first (flag the record), automate only if volume justifies it.

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
