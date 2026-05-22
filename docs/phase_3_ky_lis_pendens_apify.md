# Phase 3 — Kentucky Lis Pendens on Apify (daily, deduped)

**Status:** Ready to build. Decisions locked (see "Locked decisions" at the bottom). Companion to [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md) (probate). Lis pendens was never covered by that doc; this fills the gap.

## Context

This repo is a fork of `tyvhb/SiftStack` (Knoxville/Knox & Blount, TN), being ported to Louisville/Jefferson County, KY. Upstream's model is **one Apify Actor daily run** (`scrape → enrich → skip trace → DataSift upload → Slack notify`) where every notice type is just another SavedSearch riding the same run. So "automate lis pendens on Apify" is **not** a separate actor — it's making the existing Jefferson Deeds (JCD) lis pendens source safe and complete on the daily schedule, the same way probate (KCOJ) already is.

## Objective

Make `lis_pendens` (Jefferson County Deeds / `source="jcd"`) a first-class participant in the daily Apify run:

1. **Run** on the daily schedule (it currently isn't, by default)
2. **Dedup** across runs so the same open LP filing isn't re-pushed to DataSift every morning
3. **Skip redundant work** — don't re-fetch/OCR the filed PDF for instruments already seen

Probate already has all three (build complete). Lis pendens has none of them yet.

## Current state (verified against code, 2026-05-21)

The JCD lis pendens **scraper works** — `scrape_jefferson_deeds()` ([src/jefferson_deeds_scraper.py:527](../src/jefferson_deeds_scraper.py#L527)) returns `NoticeData` and is routed via `source="jcd"` ([src/config.py:144](../src/config.py#L144)) through `scrape_all`'s JCD branch ([src/scraper.py:825-864](../src/scraper.py#L825-L864)). But three gaps block safe daily automation:

### Gap 1 — Not in the default daily run
Actor input schema default `types` = `["foreclosure"]` ([.actor/input_schema.json:28](../.actor/input_schema.json#L28)). Unless the schedule overrides `types`, the daily run never scrapes `lis_pendens`.

### Gap 2 — Zero cross-run dedup (re-pushes every day)
- JCD `detail_url` is `pdetail.php?instnum={instnum}&year={year}&db={db}&cnum={cnum}` ([jefferson_deeds_scraper.py:140-143](../src/jefferson_deeds_scraper.py#L140-L143)) — **no `ID=` param.**
- The Apify dedup helper `_notice_id()` extracts `[?&]ID=(\d+)` ([main.py:249-252](../src/main.py#L249-L252)). For a JCD URL it returns `""`.
- In `push_batch` ([main.py:254-264](../src/main.py#L254-L264)): `nid` is falsy → record is never skipped and never recorded in `seen_ids` → **every LP filing in the rolling window is pushed to the dataset/DataSift on every run.**
- `scrape_all`'s JCD branch ([scraper.py:829-864](../src/scraper.py#L829-L864)) `extend`s and `on_batch`es all notices and **never consults or updates `seen_ids`** — that dedup lives only in the TNPN browser path (`_scrape_search`, [scraper.py:415/452](../src/scraper.py#L415)).
- Daily window default is `last_n_business_days(7)` ([scraper.py:838](../src/scraper.py#L838)), so each filing re-emits ~5 times before it ages out.

### Gap 3 — Redundant PDF fetch/OCR for already-seen filings
`scrape_jefferson_deeds(fetch_details=True)` fetches and OCRs the filed document for **every** record to extract the address ([jefferson_deeds_scraper.py:601-603](../src/jefferson_deeds_scraper.py#L601-L603), with a polite `_delay()` each). This is the expensive part. Any dedup done *after* the scraper returns still pays this cost daily. Dedup must gate the detail fetch.

## The model to mirror: KCOJ probate (already done)

| Concern | KCOJ (done) | JCD (this phase) |
|---|---|---|
| Stable dedup key | `case_number` | `instnum`/`year`/`db` (already parsed, [:183-194](../src/jefferson_deeds_scraper.py#L183-L194)) |
| Cache load/save | `load_kcoj_seen_cases` / `save_kcoj_seen_cases` ([kcoj_scraper.py:58-79](../src/kcoj_scraper.py#L58)) | new `load_jcd_seen` / `save_jcd_seen` |
| State file | `KCOJ_SEEN_CASES_FILE` (+ prune days) | new `JCD_SEEN_FILE` (+ prune days) |
| Passed into scraper | `seen_cases=` param, filtered before expensive work | `seen_instruments=` param, filtered before PDF fetch |
| scrape_all wiring | `kcoj_seen_cases` dict + `on_kcoj_search_complete` ([scraper.py:780-819](../src/scraper.py#L780-L819)) | `jcd_seen` dict + `on_jcd_search_complete` |
| Apify KVS persistence | `kvs.get_value("kcoj_seen_cases")` / `persist_kcoj_seen_cases` ([main.py:336/350-354/363](../src/main.py#L336)) | `kvs.get_value("jcd_seen_instruments")` / `persist_jcd_seen` |

## Workstreams

Each task lists exact files, signatures, and the KCOJ code it mirrors. Implement in order; each is independently committable.

### 3a. Config constants + JCD seen-instrument cache

**Config** — add to [src/config.py](../src/config.py) directly below the existing KCOJ constants ([config.py:31-32](../src/config.py#L31-L32)):
```python
JCD_SEEN_FILE = PROJECT_ROOT / "jcd_seen_instruments.json"
JCD_SEEN_PRUNE_DAYS = 120   # LP filings resolve faster than probate; covers the rolling window + slack
```

**Cache functions** — add to [src/jefferson_deeds_scraper.py](../src/jefferson_deeds_scraper.py), mirroring `load_kcoj_seen_cases` / `save_kcoj_seen_cases` ([kcoj_scraper.py:58-79](../src/kcoj_scraper.py#L58-L79)) exactly:
```python
def _instrument_key(instnum: str, year: str, db: str) -> str:
    """Stable dedup key per recorded instrument (globally unique in JCD)."""
    return f"{instnum}-{year}-{db}"

def load_jcd_seen() -> dict[str, str]:
    """Load previously-emitted instrument keys, pruning entries older than
    config.JCD_SEEN_PRUNE_DAYS. Value = YYYY-MM-DD first-emitted date."""
    from datetime import timedelta
    data = config.load_state(config.JCD_SEEN_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=config.JCD_SEEN_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: d for k, d in data.items() if d >= cutoff}
    if len(pruned) < len(data):
        logger.info("JCD: pruned %d instruments older than %d days",
                    len(data) - len(pruned), config.JCD_SEEN_PRUNE_DAYS)
    return pruned

def save_jcd_seen(seen: dict[str, str]) -> None:
    config.save_state(config.JCD_SEEN_FILE, seen)
```
- **Acceptance:** unit test — write a dict with one fresh + one >120-day-old key, `save_jcd_seen` then `load_jcd_seen`, assert the stale key is dropped.
- **Budget:** 1 hour.

### 3b. Dedup inside `scrape_jefferson_deeds` (gate the PDF fetch)

In [src/jefferson_deeds_scraper.py](../src/jefferson_deeds_scraper.py#L527):
- **Signature:** add `seen_instruments: dict[str, str] | None = None` to `scrape_jefferson_deeds(...)`.
- **Placement:** insert the skip check at the **top of the `for i, rec in enumerate(records):` loop** ([:586](../src/jefferson_deeds_scraper.py#L586)) — i.e. **before** the `if fetch_details and rec.get("view_img"):` block ([:601](../src/jefferson_deeds_scraper.py#L601)), so the `_fetch_address_from_document` + `_delay()` cost (Gap 3) is never paid for an already-seen instrument:
```python
today_str = datetime.now().strftime("%Y-%m-%d")
for i, rec in enumerate(records):
    key = _instrument_key(rec["instnum"], rec["year"], rec["db"])
    if seen_instruments is not None and key in seen_instruments:
        continue                       # already emitted on a prior run — skip fetch + emit
    ...                                # existing detail-fetch + NoticeData build
    if seen_instruments is not None:
        seen_instruments[key] = today_str   # mark seen AFTER successful build (see Decision 4)
    notices.append(notice)
```
- Mark seen only after the `NoticeData` is appended, so a mid-record exception doesn't permanently suppress a filing.
- Mirrors `scrape_kcoj`'s pre-work `seen_cases` check ([kcoj_scraper.py:401-410](../src/kcoj_scraper.py#L401-L410)).
- **Acceptance:** call twice in-process with the same `seen_instruments` dict against a fixed date range — second call returns `[]` and performs zero `_fetch_address_from_document` calls (assert via mock/log count).
- **Budget:** 1–2 hours.

### 3c. Wire dedup through `scrape_all` + Apify KVS

**`scrape_all`** ([src/scraper.py:690](../src/scraper.py#L690)) — mirror the `kcoj_seen_cases` / `on_kcoj_search_complete` pair:
- Add kwargs `jcd_seen: dict[str, str] | None = None` and `on_jcd_search_complete=None`.
- In the JCD branch ([scraper.py:825-864](../src/scraper.py#L825-L864)):
  - Before the loop: `from jefferson_deeds_scraper import load_jcd_seen, save_jcd_seen` and `if jcd_seen is None: jcd_seen = load_jcd_seen()`.
  - Pass `seen_instruments=jcd_seen` into `scrape_jefferson_deeds(...)` ([scraper.py:845](../src/scraper.py#L845)).
  - In the post-search persistence block ([scraper.py:857-864](../src/scraper.py#L857)): add `save_jcd_seen(jcd_seen)` and `if on_jcd_search_complete is not None: await on_jcd_search_complete(jcd_seen)`.

**main.py Apify path** ([src/main.py:316-366](../src/main.py#L316)) — mirror the KCOJ KVS block:
- After loading `kcoj_seen_cases` ([main.py:336](../src/main.py#L336)): `jcd_seen = await kvs.get_value("jcd_seen_instruments") or {}` + log count.
- Add an async `persist_jcd_seen(seen)` next to `persist_kcoj_seen_cases` ([main.py:350-354](../src/main.py#L350-L354)) that does `await kvs.set_value("jcd_seen_instruments", seen)`.
- In the `scrape_all(...)` call ([main.py:357-366](../src/main.py#L357-L366)): pass `jcd_seen=jcd_seen, on_jcd_search_complete=persist_jcd_seen`.

**Do not** touch `_notice_id` / `push_batch` — instrument-key dedup upstream is the single source of truth; the `push_batch` no-op for JCD URLs becomes harmlessly redundant.
- **Acceptance:** local two-run test (`python src/main.py daily --types lis_pendens`) — second run logs the loaded cache count and emits 0 new LP notices for an unchanged window; `jcd_seen_instruments.json` exists and grows only with genuinely new filings.
- **Budget:** 2 hours.

### 3d. Turn lis pendens on in the daily schedule

- **Schema default stays `["foreclosure"]`** (Decision 1) — do **not** change [.actor/input_schema.json:28](../.actor/input_schema.json#L28).
- **Schedule input:** set the Louisville Apify schedule's `types` to `["foreclosure", "lis_pendens", "probate"]` (explicit). This is an Apify Console config change, not code.
- **Docs:** add one line to CLAUDE.md's "Actor Input" section noting `lis_pendens` must be listed explicitly in `types` (default is foreclosure-only) and that JCD dedup persists via the `jcd_seen_instruments` KVS key.
- **Acceptance:** the scheduled run's log shows a `JCD search: LIS PENDENS Jefferson County` line and pushes LP records on day 1, zero duplicates on day 2.
- **Budget:** 0.5 hour.

## Schema / config changes

- `config.py`: `JCD_SEEN_FILE = PROJECT_ROOT / "jcd_seen_instruments.json"`, `JCD_SEEN_PRUNE_DAYS = 120`.
- No `NoticeData` changes — `instnum`/`year`/`db`/`case_num` already live on the parsed record dict; only the dedup cache is new.
- KVS key: `jcd_seen_instruments` (parallels `kcoj_seen_cases`, `seen_notice_ids`).
- `.gitignore`: add `jcd_seen_instruments.json` (mirrors `kcoj_seen_cases.json` — local state, KVS is the cloud source of truth).

## Build order

3a → 3b → 3c → 3d. 3a is the cache primitive; 3b consumes it inside the scraper (and delivers the cost savings); 3c makes it survive across Apify runs; 3d flips it on in the schedule. 3d can be done first as a one-line schedule change to start *running* lis pendens immediately (accepting daily duplicates) if there's urgency, then 3a–3c remove the duplicates.

## Locked decisions

These are settled — build to them. Each notes what would trigger revisiting.

1. **Schema default `types` stays `["foreclosure"]`.** The Louisville schedule sets `types` explicitly (3d). Keeps a fresh deploy from silently turning on all sources. *Revisit if* the only deployment is the KY schedule and the explicit-list step proves error-prone.
2. **`JCD_SEEN_PRUNE_DAYS = 120`.** Long enough to cover the rolling 7-business-day window many times over; an LP still active past 120 days re-emits once (acceptable). *Revisit after* observing real LP resolution times in production.
3. **Dedup gates inside `scrape_jefferson_deeds`, before the PDF fetch** (3b), not after the scraper returns. Saves the daily OCR/fetch cost for already-seen instruments. *Non-negotiable* — the cost saving is the point.
4. **Mark an instrument "seen" only after its `NoticeData` is successfully built and appended** (3b). A mid-record exception won't permanently suppress a filing, but a successful emit with a weak/empty address is still marked seen (no auto-retry). *Revisit if* OCR address-misses prove common — then gate "seen" on a non-empty resolved address to allow one retry.

## Out of scope for Phase 3

- Lis pendens enrichment beyond what the daily pipeline already does (the JCD scraper already does PVA-by-parcel for address; deeper mortgage/equity work is the probate Phase 2 territory and can be shared later).
- KY counties other than Jefferson (different clerk site).
- Backfilling historical LP filings (the scraper supports `historical` mode, but a daily schedule only needs the rolling window).
- Teaching `_notice_id`/`push_batch` about non-TNPN URL schemes — superseded by instrument-key dedup.

## References

- JCD scraper: [src/jefferson_deeds_scraper.py](../src/jefferson_deeds_scraper.py) (`scrape_jefferson_deeds`, `_parse_results_table`)
- scrape_all routing + KCOJ dedup template: [src/scraper.py:690-874](../src/scraper.py#L690-L874)
- KCOJ seen-case cache (the pattern to mirror): [src/kcoj_scraper.py:54-79](../src/kcoj_scraper.py#L54)
- Apify KVS wiring: [src/main.py:316-366](../src/main.py#L316)
- Actor input schema: [.actor/input_schema.json](../.actor/input_schema.json)
- Probate companion plan: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md)
