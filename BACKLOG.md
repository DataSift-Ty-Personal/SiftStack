# BACKLOG

Deferred work — surfaced during builds but punted to keep scope focused.

## Search reliability

- **Add Serper fallback to `obituary_enricher._search_obituary` to eliminate DDGS non-determinism.** DDGS returns inconsistent result counts across consecutive identical queries (verified 2026-05-10: same query returned 6 candidates one minute, 0 the next). Affects (a) Phase 2 recall in `deep_prospecting/phases/phase_2_genealogy.py` and (b) the weekly cron's obit enrichment in `modal_app.nj_weekly_all`. Implementation sketch: prepend a Serper "site:legacy.com OR site:echovita.com OR site:tributearchive.com" query and merge results before falling back to DDGS. Cost ~ $0.001/run.

## Obituary parse accuracy

- **`obituary_enricher._parse_obituary_with_llm` returns `match=False` for valid obits when obit city ≠ property city.** Common false-negative pattern: decedent dies at out-of-town hospital or nursing home, so the obit's geo doesn't match property city. Verified against Olive Geczik (Milltown property, died at Robert Wood Johnson University Hospital in East Brunswick — LLM says match=False, conf=low). The Phase 2 workaround (`obit_parse_raw` in `_siftstack_bridge.py`) calls the LLM directly and does its own first+surname token match. Cron is presumably missing equivalent cases. Fix path: revise OBITUARY_PROMPT to weight name match heavier than geo match, and add an eval set against historical false negatives before shipping.

## Skip-trace coverage

- **TPS and FPS hard-blocked at all transports.** TruePeopleSearch returns "Please enable JS" through Firecrawl; FastPeopleSearch is Cloudflare-walled. `deep_prospecting/phases/phase_skiptrace.py` runs CBC-only with honest substitution in `SkipTraceResult.site_state` (`tps BLOCKED`, `fps BLOCKED`, `cbc HIT`). Affects the `"Verified 2+ Sites"` tag derivation in `datasift_csv_writer.py` — that source flag will never fire on the current source mix (one source = `"Found via CBC"` only). Long-term fix: paid skip-trace API (Trestle / IDI) to restore multi-source verification and reach DMs that CBC doesn't index (e.g. tenants, recent movers, low-property-history individuals).

- **CBC parser depth varies per record.** Donald Mozdzen's CBC detail page returned 1 phone / 0 emails / 0 associates versus Catherine Geczik's 3 / 4 / 7 — both records are HIT/parsed/no-error, but the data depth diverges. Possible causes: paywall section served conditionally, unhandled markup variant (Don J Mozdzen's canonical-name page may render differently than the "common case"), or Firecrawl's waitFor=5000ms isn't enough for the slower-rendering sections. Affects production yield on similar records. Audit path: capture Firecrawl markdown for a low-yield CBC record, diff against a high-yield one, identify the missing section structure. Then either extend the parser or extend the wait.
