# BACKLOG

Deferred work — surfaced during builds but punted to keep scope focused.

## Search reliability

- **Add Serper fallback to `obituary_enricher._search_obituary` to eliminate DDGS non-determinism.** DDGS returns inconsistent result counts across consecutive identical queries (verified 2026-05-10: same query returned 6 candidates one minute, 0 the next). Affects (a) Phase 2 recall in `deep_prospecting/phases/phase_2_genealogy.py` and (b) the weekly cron's obit enrichment in `modal_app.nj_weekly_all`. Implementation sketch: prepend a Serper "site:legacy.com OR site:echovita.com OR site:tributearchive.com" query and merge results before falling back to DDGS. Cost ~ $0.001/run.

## Obituary parse accuracy

- **`obituary_enricher._parse_obituary_with_llm` returns `match=False` for valid obits when obit city ≠ property city.** Common false-negative pattern: decedent dies at out-of-town hospital or nursing home, so the obit's geo doesn't match property city. Verified against Olive Geczik (Milltown property, died at Robert Wood Johnson University Hospital in East Brunswick — LLM says match=False, conf=low). The Phase 2 workaround (`obit_parse_raw` in `_siftstack_bridge.py`) calls the LLM directly and does its own first+surname token match. Cron is presumably missing equivalent cases. Fix path: revise OBITUARY_PROMPT to weight name match heavier than geo match, and add an eval set against historical false negatives before shipping.

## Skip-trace coverage

- **TPS and FPS hard-blocked at all transports.** TruePeopleSearch returns "Please enable JS" through Firecrawl; FastPeopleSearch is Cloudflare-walled. `deep_prospecting/phases/phase_skiptrace.py` runs CBC-only with honest substitution in `SkipTraceResult.site_state` (`tps BLOCKED`, `fps BLOCKED`, `cbc HIT`). Affects the `"Verified 2+ Sites"` tag derivation in `datasift_csv_writer.py` — that source flag will never fire on the current source mix (one source = `"Found via CBC"` only). Long-term fix: paid skip-trace API (Trestle / IDI) to restore multi-source verification and reach DMs that CBC doesn't index (e.g. tenants, recent movers, low-property-history individuals).

- **CBC parser depth varies per record.** Donald Mozdzen's CBC detail page returned 1 phone / 0 emails / 0 associates versus Catherine Geczik's 3 / 4 / 7 — both records are HIT/parsed/no-error, but the data depth diverges. Possible causes: paywall section served conditionally, unhandled markup variant (Don J Mozdzen's canonical-name page may render differently than the "common case"), or Firecrawl's waitFor=5000ms isn't enough for the slower-rendering sections. Affects production yield on similar records. Audit path: capture Firecrawl markdown for a low-yield CBC record, diff against a high-yield one, identify the missing section structure. Then either extend the parser or extend the wait.

- **`phase_skiptrace_unresolved` for heirs with no public-record footprint.** Surfaced in v2-slice2 on Catherine's case: Dr. Ashley M. Geczik (granddaughter) returned `EMPTY` from both CBC and Tracerfy — younger generations, especially professionals who never owned a home or registered to vote in their birth-name state, have minimal public-record exposure. Both finders are address-record-anchored, so a renter / never-owner is invisible to them. Mitigation paths in priority order: (a) Slice 3 BV paste-and-parse helper — BeenVerified's social-media-graph data aggregator surfaces these cases manually; (b) Trestle Reverse Address as a third automated source — keyed off the decedent's property, may return co-listed relatives that include the missing heir; (c) social-profile dorks via Serper (LinkedIn / Facebook). Cost-controlled fallbacks only — don't blow the per-record ceiling chasing absent records.

## Phase 2 / pre-probate gaps

- **`phase_2_no_obit_found` still surfacing on Marie + Maryann after v2-slice2.** DDGS non-determinism (BACKLOG item above) is the proximate cause — same query returns 6 candidates one minute, 0 the next. Slice 3 Serper fallback should resolve. Re-test these two records once the fallback lands and confirm `level=L3` with extracted heirs, not the current `L2` + warning.

- **Pre-probate detection blindspot — Michael Raspa pattern.** Title is still in the living owner's name (no death signal from MOD-IV), but the owner may have died very recently (obit exists, probate not yet filed). Phase 1 has no signal to fire on. Slice 3 should consider running a cheap obit dork (Serper-only, no Haiku parse) on **every** L1 case as a secondary check — only escalate to full Phase 2 / 2.5 if the dork surfaces a personal-obit URL (not a city listing page). Budget impact: ~$0.001/L1 record (one Serper call) ≈ $0.005 per weekly cron run at current volumes. Worth the recall lift.

- **`(ESTATE)` marker false-positives on alive owners — surfaced during Slice 3 validation.** Phase 1's `title_owner_estate_marker` heuristic fires whenever MOD-IV stores `LASTNAME, FIRST (ESTATE)`. Slice 3 testing surfaced two cases where the marker is wrong:
  - **Daniel S Bernshock (Linden NJ)** is **alive** (84 yo per nationalpublicdata; named as a surviving son in his late mother Sophia's 2018 obit). The `(ESTATE)` marker on his title is almost certainly from his late wife Ann's 2010 estate that affected joint title — Daniel inherited and the marker stayed on the parcel record.
  - **Marie Schwichtenberg (East Brunswick NJ)** has no personal obit indexed online despite the `(ESTATE)` marker. Could be (a) alive with a similar historical-spouse-estate marker, (b) very old / very local death that's not online, or (c) the 2005 "Maria Schwichtenberg" obit at legacy.com IS her mother and "Marie" is actually the heir, not the decedent. Needs operator eyeball to disambiguate.

  Fix path: validate `(ESTATE)` death signal via a cheap Serper obit dork on Phase 1's title_owner BEFORE entering Phase 2. If Serper finds the title_owner as a *surviving* family member of a different decedent's obit, flip the death_signal to False and route as L1 with the marker as advisory only. If Serper finds nothing for the title_owner specifically, keep the marker but lower confidence on the death_signal. Budget: ~$0.001 per `(ESTATE)`-marker record (very small fraction of cohort).

  **STATUS:** Shipped in v4-slice4 (`phases/phase_1_title.validate_estate_marker`). Validator does 2 Serper queries (decedent + survivor) + 2 Haiku boolean calls (~$0.012 per `(ESTATE)`-marker record). Verified on the 7-record cohort: Daniel S. Bernshock + Marie Schwichtenberg both flipped L2/L3 → L1 with `phase_1_estate_marker_advisory_spouse_estate` warning. Catherine's `title_owner_mismatch_same_surname` heuristic doesn't trigger the validator — L3 routing unchanged.

## Validator observability

- **Track `(ESTATE)`-marker validator flip rate across production backlog.** Slice 4's `validate_estate_marker` is heuristic-on-heuristic — Phase 1's `(ESTATE)` marker fires first, validator decides if it's a real death. Need observed flip rate to know if the underlying heuristic is well-calibrated:
  - If **flip rate > 30%** across the production backlog: the `(ESTATE)` marker is too noisy. Rethink the upstream heuristic — maybe drop it entirely and rely on the cheaper Serper dork as the primary death signal, not as a corrector.
  - If **flip rate < 5%** across the production backlog: the validator is overkill — the original heuristic was reasonably accurate. Consider stripping back to save the ~$0.012/`(ESTATE)`-record validator cost.
  - If **5%–30%** (likely): validator is doing real work, keep it.

  Track via: count records where `lead.warnings` contains `phase_1_estate_marker_advisory_spouse_estate` vs total records where Phase 1 hit the `(ESTATE)` path. Add a counter to `run_batch.py`'s summary output. Defer wiring until 50+ production records observed.

## Operator runbook (DataSift round-trip)

Observed during v2-slice2 operational validation (2026-05-10). Workflow docs, not bugs — but if the friction shows up weekly, candidates for automation in Slice 3+.

- **DataSift import wizard "Add tags" step is mandatory for outcome tags.** At the "Add tags" step of the upload wizard, click through and accept the Tags column processing. Without it DataSift treats the Tags column as read-only on existing-record overlays and CLI-generated outcome tags (`Deep Prospecting Complete - NUMBERS ADDED`, `Verified Living Heir Found`, `Deep Prospected via CLI YYYY-MM-DD`) won't transfer. With it engaged, comma-separated tags in the CSV cell route correctly as individual tag entries. Optional Slice 3+ automation: Playwright pass to engage this step post-import; only worth building if it becomes weekly friction.

- **Notes column → stored as a record comment, doesn't round-trip via CSV.** DataSift writes the Notes cell content as a comment attached to the record (operator-visible in the web UI), but the Notes column on CSV re-export comes back empty. To read the full deep prospecting block (People & Star Markers + 9-section research pack), open the record in DataSift's web UI — don't expect it back via CSV export. Not actionable on our side; it's how DataSift treats the Notes field.

- **Phone Status blank → coerced to UNKNOWN on DataSift import.** CLI-added phones land with `Phone Status = ""` (intentional — fresh slots not yet dialed). DataSift's import coerces empty Phone Status to `UNKNOWN`. The UI displays the slot as blank to the operator (matching pre-dial intent), but CSV re-export shows `UNKNOWN`. Cosmetic only, no workflow impact — operator's dial decision doesn't change.
