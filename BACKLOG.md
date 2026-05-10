# BACKLOG

Deferred work — surfaced during builds but punted to keep scope focused.

## Search reliability

- **Add Serper fallback to `obituary_enricher._search_obituary` to eliminate DDGS non-determinism.** DDGS returns inconsistent result counts across consecutive identical queries (verified 2026-05-10: same query returned 6 candidates one minute, 0 the next). Affects (a) Phase 2 recall in `deep_prospecting/phases/phase_2_genealogy.py` and (b) the weekly cron's obit enrichment in `modal_app.nj_weekly_all`. Implementation sketch: prepend a Serper "site:legacy.com OR site:echovita.com OR site:tributearchive.com" query and merge results before falling back to DDGS. Cost ~ $0.001/run.

## Obituary parse accuracy

- **`obituary_enricher._parse_obituary_with_llm` returns `match=False` for valid obits when obit city ≠ property city.** Common false-negative pattern: decedent dies at out-of-town hospital or nursing home, so the obit's geo doesn't match property city. Verified against Olive Geczik (Milltown property, died at Robert Wood Johnson University Hospital in East Brunswick — LLM says match=False, conf=low). The Phase 2 workaround (`obit_parse_raw` in `_siftstack_bridge.py`) calls the LLM directly and does its own first+surname token match. Cron is presumably missing equivalent cases. Fix path: revise OBITUARY_PROMPT to weight name match heavier than geo match, and add an eval set against historical false negatives before shipping.

