# Phase 2j — Re-Poll Queue + No-Probate Branch (buildable spec)

**Status:** Ready to build. Decisions locked (bottom). Two related capabilities for the freshest/most-distressed leads. **Shares an heir-ID helper with [phase_3_ky_lis_pendens_apify.md](phase_3_ky_lis_pendens_apify.md) — build it once.** Parent: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md). Evidence: [probate_enrichment_lessons.md](probate_enrichment_lessons.md) — **~16% (≈21/128)** hit CourtNet/obit latency; **~9% (≈11/128)** had **no probate at all** (the McGarvey tax-foreclosure = most motivated).

## Objective

1. **Re-poll queue:** stop dropping fresh leads that aren't indexed yet (CourtNet 0 rows / obit not posted). Enqueue and re-search after a few business days.
2. **No-probate branch:** when a death surfaces as a lis pendens / tax foreclosure with UNKNOWN HEIRS + a Warning Order Attorney (no probate case), route to heir identification instead of dropping.

## Current state (verified against code)

- **KVS dedup pattern to mirror** — `load_kcoj_seen_cases`/`save_kcoj_seen_cases` keyed by case_number, KVS-persisted ([kcoj_scraper.py:58-79](../src/kcoj_scraper.py#L58), wired in [scraper.py:780-819](../src/scraper.py#L780) + [main.py:336,350-354](../src/main.py#L336)). The re-poll queue is the same shape, different key.
- **CourtNet returns 0 rows on fresh cases** — `search_case` ([kcoj_case_detail.py:341](../src/kcoj_case_detail.py#L341)) returns `[]` with no retry/queue today; the case is just dropped.
- **Deed-grantor history for heir ID** — `_fetch_deed_list`/`_parse_deed_list` ([jefferson_deeds_scraper.py:1111,1155](../src/jefferson_deeds_scraper.py#L1111)) already pull the grantor chain; the no-probate branch reuses this for affidavit-of-descent / prior-owner tracing.
- **Lis pendens scraper** ([jefferson_deeds_scraper.py:527](../src/jefferson_deeds_scraper.py#L527)) is the source that surfaces the no-probate / unknown-heir cases (Phase 3).
- **`repoll_after` field** is introduced by this spec (also referenced by 2g-6 for credits-exhausted).

## Design

### Part A — Re-poll queue
KVS-backed queue `kcoj_repoll_queue: dict[str, str]` keyed by `case_number` (or `decedent_name|date` when no case number yet) → `repoll_after` (YYYY-MM-DD). Mirrors the seen-cases plumbing.

- **Enqueue:** when 2c CourtNet returns 0 parties for a case the docket says exists, or the obituary search returns nothing for a just-filed decedent, set `repoll_after = today + REPOLL_DELAY_BUSINESS_DAYS` (default 4) and add to the queue. Cap retries at `REPOLL_MAX_ATTEMPTS` (default 3), then drop with an audit note.
- **Drain:** at the **start** of each daily run, re-search every queue entry whose `repoll_after <= today` *before* the normal scrape; on success, remove from queue and merge into the day's notices; on still-empty, bump `repoll_after` and increment attempts.
- **Persistence:** load/save via `config.load_state`/`save_state` locally and `kvs.get_value/set_value("kcoj_repoll_queue")` in Apify, exactly like `kcoj_seen_cases`.

### Part B — No-probate / unknown-heir branch
A shared helper `src/heir_identifier.py` used by both Phase 2 (probate-less deaths) and Phase 3 (lis pendens with unknown heirs).

```python
def identify_heirs(notice: NoticeData) -> list[dict]:
    """Best-effort heirs for a decedent with no usable probate party graph.
    Sources, in order: obituary survivors → affidavit of descent (deeds) →
    deed-grantor history → 2e name-variant people search. Returns heir dicts
    compatible with heir_map_json."""
```
- **Trigger:** a notice with `owner_deceased=yes` (or a death-indexed grantor on a lis pendens) but `title_path` resolvable AND no CourtNet parties / a Warning-Order-Attorney party type.
- **Sources:** obituary `survivors` (already extracted) → affidavit-of-descent instruments in deeds → prior deed grantor chain → 2e variant people-search. Populates `heir_map_json` so the existing skip-trace (2g) + report paths work unchanged.
- Cases: Combs, Cooper, Dorsey, McGarvey, Walker, Gonzalez, Herflicker, Rutter, Spencer, Thompson-Hale.

## Build tasks (ordered, each committable)
- **2j-1 — Re-poll queue store** (`load/save_repoll_queue`, KVS key) mirroring seen-cases. *Acceptance:* enqueue, persist, reload; entries past `repoll_after` are returned as "due."
- **2j-2 — Enqueue on 0-row CourtNet / empty obit** in 2c and the obituary step. *Acceptance:* a fresh case with 0 parties lands in the queue with a future `repoll_after`; capped at max attempts.
- **2j-3 — Drain at daily-run start** (before scrape), re-search due entries, merge successes, bump failures. *Acceptance:* a queued case that becomes indexed on day N is picked up and removed from the queue.
- **2j-4 — `heir_identifier.identify_heirs`** shared helper. *Acceptance:* a no-probate decedent with an obituary yields `heir_map_json` heirs; deed-grantor fallback fires when no obit.
- **2j-5 — Route no-probate deaths to the branch** (probate notices with no parties + lis pendens unknown-heir cases from Phase 3). *Acceptance:* a Warning-Order-Attorney lis pendens produces candidate heirs instead of being dropped.

## Schema / config
- `NoticeData`: `repoll_after`, `repoll_attempts: str = ""`, `heir_id_source: str = ""`.
- KVS key: `kcoj_repoll_queue` (parallels `kcoj_seen_cases`, `jcd_seen_instruments`).
- Config: `KCOJ_REPOLL_FILE`, `REPOLL_DELAY_BUSINESS_DAYS=4`, `REPOLL_MAX_ATTEMPTS=3`.
- `.gitignore`: add `kcoj_repoll_queue.json` (mirrors `kcoj_seen_cases.json`).

## Locked decisions
1. **Re-poll, don't drop, fresh 0-row leads** — delay 4 business days, max 3 attempts, then drop with audit. *Revisit* delay/attempts after observing real CourtNet indexing lag.
2. **Drain the queue at the START of the daily run**, before the new scrape, so re-found cases flow through the same enrichment as fresh ones. *Non-negotiable* — keeps one code path.
3. **One shared `heir_identifier`** for Phase 2 no-probate deaths and Phase 3 lis-pendens unknown heirs. *Non-negotiable* — same problem, build once.
4. **Heir ID is best-effort and feeds the existing `heir_map_json`/skip-trace path** — no new contact pipeline. Below-confidence heirs are queued for manual review (reuse 2e disambiguation). *Revisit* with paid genealogy if free sources miss.

## Out of scope
- Paid genealogy/Ancestry automation beyond the existing SSDI enricher.
- Automating affidavit-of-descent *filing* (we only *read* them).
- A late filing on a long-held free-and-clear home is **sell-intent, not stale** (Layton 2.5yr) — the DOD sanity check (parent plan) handles that nuance; 2j only handles *missing* data, not late data.

## References
- KVS pattern to mirror: [kcoj_scraper.py:58-79](../src/kcoj_scraper.py#L58), [main.py:336,350-354](../src/main.py#L336)
- 0-row CourtNet: [kcoj_case_detail.py:341](../src/kcoj_case_detail.py#L341)
- Deed-grantor history for heir ID: [jefferson_deeds_scraper.py:1111-1240](../src/jefferson_deeds_scraper.py#L1111)
- Phase 3 overlap: [phase_3_ky_lis_pendens_apify.md](phase_3_ky_lis_pendens_apify.md)
- Parent / evidence: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md), [probate_enrichment_lessons.md](probate_enrichment_lessons.md)
