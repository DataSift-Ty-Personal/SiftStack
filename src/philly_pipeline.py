"""Philadelphia enrichment pipeline — shared by run_philly_micro.py and run_philly_daily.py.

Both entry points call run_pipeline() with their own defaults:

    run_philly_micro.py  →  run_pipeline(sources, lookback, limit=10,   upload=False, slack=False)
    run_philly_daily.py  →  run_pipeline(sources, lookback, limit=None,  upload=True,  slack=True)

All six alignment changes live here exactly once:
  1. Tracerfly scoped to DP candidates (PROBATE_ESTATE only)
  2. Trestle scoped to same DP candidates
  3. Cross-source dedup by parcel_id → address
  4. Smarty RDI commercial filter
  5. Validation gate (drop records missing address / city / zip)
  6. (Bug B fix lives in tracerfy_skip_tracer.py — OPA-aware name split)
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
import re
import time
from pathlib import Path

import config
from datasift_formatter import write_datasift_csv
from enrichment_pipeline import _validate_records
from notice_parser import NoticeData
from philadelphia_scrapers import run_philly_scrape

try:
    from address_standardizer import retry_with_geocoded_city, standardize_addresses
    _SMARTY_AVAILABLE = True
except (ImportError, ModuleNotFoundError) as _e:
    logging.getLogger(__name__).warning(
        "address_standardizer import failed: %s — Smarty step will be skipped", _e
    )
    _SMARTY_AVAILABLE = False
    standardize_addresses = retry_with_geocoded_city = None  # type: ignore

logger = logging.getLogger(__name__)

# Build a stable source_id → notice_type map from config once at import time.
_SOURCE_NOTICE_TYPE: dict[str, str] = {
    s.source_id: s.notice_type for s in config.PHILLY_SOURCES
}

# ── Smarty parcel cache ─────────────────────────────────────────────────────
# Persists Smarty results by OPA parcel_id so bid4assets (full auction list
# re-scraped every run) doesn't burn API credits for already-standardized parcels.

_SMARTY_CACHE_FILE = Path(__file__).resolve().parent.parent / "smarty_parcel_cache.json"
_SMARTY_CACHE_TTL_DAYS = 90   # re-verify with Smarty after 90 days
_SMARTY_CACHE_FIELDS = (
    "address", "city", "state", "zip", "zip_plus4",
    "dpv_match_code", "rdi", "vacant", "latitude", "longitude",
)


def _load_smarty_cache() -> dict:
    if _SMARTY_CACHE_FILE.exists():
        try:
            with open(_SMARTY_CACHE_FILE) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_smarty_cache(cache: dict) -> None:
    try:
        with open(_SMARTY_CACHE_FILE, "w") as fh:
            json.dump(cache, fh)
    except OSError as exc:
        logger.warning("Failed to save Smarty cache: %s", exc)


def _apply_smarty_cache(
    notices: list[NoticeData], cache: dict
) -> tuple[list[NoticeData], int]:
    """Apply cached Smarty results in-place; return (api_needed, cache_hits).

    Records with a fresh cache entry get their address/geo fields populated
    directly and are excluded from the Smarty API call.  Records with no entry
    or an expired entry (> TTL days old) are returned in api_needed.
    """
    today = datetime.date.today()
    api_needed: list[NoticeData] = []
    hits = 0
    for n in notices:
        pid = (n.parcel_id or "").strip()
        entry = cache.get(pid) if pid else None
        if entry:
            try:
                age = (today - datetime.date.fromisoformat(entry["cached_at"])).days
            except (KeyError, ValueError):
                age = _SMARTY_CACHE_TTL_DAYS + 1
            if age <= _SMARTY_CACHE_TTL_DAYS:
                for field in _SMARTY_CACHE_FIELDS:
                    val = entry.get(field, "")
                    if val:
                        setattr(n, field, val)
                hits += 1
                continue
        api_needed.append(n)
    return api_needed, hits


def _update_smarty_cache(notices: list[NoticeData], cache: dict) -> int:
    """Write successfully standardized records back into cache. Returns count added."""
    today = datetime.date.today().isoformat()
    added = 0
    for n in notices:
        pid = (n.parcel_id or "").strip()
        if pid and n.dpv_match_code:
            cache[pid] = {field: getattr(n, field, "") or "" for field in _SMARTY_CACHE_FIELDS}
            cache[pid]["cached_at"] = today
            added += 1
    return added


# ── Pipeline helpers ────────────────────────────────────────────────────────


def _opa_meta(notice: NoticeData) -> dict:
    try:
        return json.loads(notice.heir_map_json) if notice.heir_map_json else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _is_dp_candidate(notice: NoticeData) -> bool:
    """True if this notice requires Tracerfly / Trestle deep-prospecting treatment.

    heir_map_json is intentionally excluded — OPA enrichment populates it with
    owner-status metadata for all records, making it useless as a DP signal.
    Only fire Tracerfly when the obituary enricher has confirmed a deceased owner.
    """
    return (
        notice.notice_type == "PROBATE_ESTATE"
        or notice.owner_deceased == "yes"
        or bool(notice.decision_maker_name)
    )


def compute_distress_tier(notice: NoticeData) -> tuple[int, list[str]]:
    """Returns (tier 0-4, contributing_signals).
    Each unique signal contributes 1 point, capped at 4.
    """
    signals: list[str] = []
    all_types = set((notice.all_notice_types or notice.notice_type or "").split(";"))

    if notice.notice_type == "CODE_VIOLATION" or "CODE_VIOLATION" in all_types:
        signals.append("code_violation")

    if notice.tax_delinquent_amount:
        try:
            if float(notice.tax_delinquent_amount) > 0:
                signals.append("tax_delinquent")
        except (ValueError, TypeError):
            pass
    if "TAX_DELINQUENT" in all_types and "tax_delinquent" not in signals:
        signals.append("tax_delinquent")

    if (notice.vacant or "").upper() == "Y":
        signals.append("vacant")

    if notice.notice_type == "IMMINENTLY_DANGEROUS" or "IMMINENTLY_DANGEROUS" in all_types:
        signals.append("imminently_dangerous")

    if notice.notice_type in ("SHERIFF_MORTGAGE_FORECLOSURE", "TAX_SALE") or \
       any(t in all_types for t in ("SHERIFF_MORTGAGE_FORECLOSURE", "TAX_SALE")):
        signals.append("auction_pending")

    if notice.notice_type == "EVICTION" or "EVICTION" in all_types:
        signals.append("eviction_filed")

    if notice.notice_type == "PROBATE_ESTATE" or "PROBATE_ESTATE" in all_types \
            or notice.owner_deceased == "yes":
        signals.append("probate")

    if notice.notice_type == "LIS_PENDENS" or "LIS_PENDENS" in all_types:
        signals.append("lis_pendens")

    if getattr(notice, "expired_permit", "") == "yes":
        signals.append("expired_permit")

    tier = min(len(signals), 4)

    # Floor for time-sensitive niches. A sheriff sale with a hard auction date
    # carries one signal and scored Cold -- below a stacked code violation --
    # so anyone filtering "cold = skip" would bury the county's best FTM data
    # (PA is judicial: Final Judgment / Lis Pendens are the top benchmark rows).
    # Deadline-driven signals never rank below Warm regardless of count.
    _DEADLINE_SIGNALS = {"probate", "auction_pending", "imminently_dangerous",
                         "lis_pendens"}
    if tier < 2 and _DEADLINE_SIGNALS.intersection(signals):
        tier = 2

    return tier, signals


# Priority order for canonical notice_type when merging (higher index = higher priority)
_NOTICE_TYPE_PRIORITY = [
    "CODE_VIOLATION", "TAX_DELINQUENT", "EVICTION", "IMMINENTLY_DANGEROUS",
    "TAX_SALE", "SHERIFF_MORTGAGE_FORECLOSURE", "LIS_PENDENS", "PROBATE_ESTATE",
]


def _dedup_notices(notices: list[NoticeData]) -> tuple[list[NoticeData], int]:
    """Merge duplicate parcel records instead of dropping them.

    Groups by parcel_id (primary) or normalized address (fallback).
    For each group, picks one primary record (highest priority notice_type),
    merges tags, and records all source notice_types in all_notice_types.
    """
    from collections import defaultdict

    # Group by parcel_id or address
    groups: dict[str, list[NoticeData]] = defaultdict(list)
    key_order: list[str] = []
    for n in notices:
        pid = (n.parcel_id or "").strip()
        addr = (n.address or "").strip().lower()
        key = pid if pid else addr
        if not key:
            key = f"__nokey_{id(n)}"  # unique key for unkeyed records
        if key not in groups:
            key_order.append(key)
        groups[key].append(n)

    merged: list[NoticeData] = []
    merged_count = 0

    for key in key_order:
        group = groups[key]
        if len(group) == 1:
            n = group[0]
            n.all_notice_types = n.notice_type
            n.signal_sources = n.source_url  # approximation; actual source_id not on record
            merged.append(n)
            continue

        # Multiple records for same parcel — merge
        merged_count += len(group) - 1

        # Pick primary by priority
        def priority(n: NoticeData) -> int:
            try:
                return _NOTICE_TYPE_PRIORITY.index(n.notice_type)
            except ValueError:
                return -1

        primary = max(group, key=priority)

        # Collect all notice_types and tags
        all_types = list(dict.fromkeys(n.notice_type for n in group if n.notice_type))
        primary.all_notice_types = ";".join(all_types)
        primary.signal_sources = ";".join(
            dict.fromkeys(n.source_url for n in group if n.source_url)
        )

        # Merge numeric/bool fields — take max/union
        for n in group:
            if n is primary:
                continue
            # Tax delinquency overlay
            if n.tax_delinquent_amount and not primary.tax_delinquent_amount:
                primary.tax_delinquent_amount = n.tax_delinquent_amount
                primary.tax_delinquent_years  = n.tax_delinquent_years
            # Deceased/obituary info
            if n.owner_deceased == "yes" and primary.owner_deceased != "yes":
                primary.owner_deceased      = n.owner_deceased
                primary.date_of_death       = n.date_of_death
                primary.obituary_url        = n.obituary_url
                primary.decision_maker_name = n.decision_maker_name
                primary.heir_map_json       = n.heir_map_json
            # Expired permit flag
            if n.expired_permit == "yes" and primary.expired_permit != "yes":
                primary.expired_permit = "yes"
            # Owner status / violation depth. CODE_VIOLATION sits lowest in
            # _NOTICE_TYPE_PRIORITY, so whenever a violation merges with any
            # other source the violation record LOSES primary — taking its
            # owner_status and violation_count_for_parcel with it. Those are
            # exactly the stacked records worth filtering on, so carry the
            # values onto the primary when it doesn't already have them.
            try:
                _src = json.loads(n.heir_map_json) if n.heir_map_json else {}
                _dst = json.loads(primary.heir_map_json) if primary.heir_map_json else {}
            except (json.JSONDecodeError, TypeError):
                continue
            _carried = False
            if _src.get("owner_status") and not _dst.get("owner_status"):
                _dst["owner_status"] = _src["owner_status"]
                _carried = True
            _sv = _src.get("violation_count_for_parcel") or 0
            _dv = _dst.get("violation_count_for_parcel") or 0
            try:
                if int(_sv) > int(_dv):
                    _dst["violation_count_for_parcel"] = int(_sv)
                    _carried = True
            except (ValueError, TypeError):
                pass
            if _carried:
                primary.heir_map_json = json.dumps(_dst)

        merged.append(primary)

    if merged_count:
        logger.info(
            "Dedup merge: %d parcels merged from %d source records (%d → %d)",
            merged_count, len(notices), len(notices), len(merged),
        )
    return merged, merged_count


def _filter_rdi_commercial(notices: list[NoticeData]) -> tuple[list[NoticeData], int]:
    """Drop records Smarty flagged as RDI='Commercial'.

    Aligned with TN enrichment_pipeline._filter_commercial.  Only fires when rdi
    is explicitly 'Commercial' — empty rdi (Smarty not run or no match) passes through.
    """
    result = [n for n in notices if (n.rdi or "").lower() != "commercial"]
    removed = len(notices) - len(result)
    if removed:
        logger.info("RDI filter: removed %d commercial properties", removed)
    return result, removed


def _count_phones(notice: NoticeData) -> int:
    """Count how many phone fields are populated on a notice."""
    fields = ("primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
              "mobile_5", "landline_1", "landline_2", "landline_3")
    return sum(1 for f in fields if getattr(notice, f, ""))


# ── Niche list uploads (Option B-1) ────────────────────────────────────────
# Map Philly notice_type → DataSift niche list name.
# Each daily run uploads once to the "SiftStack {date}" bucket (for skip trace
# targeting), then once per non-empty notice_type to its persistent niche list.
_NICHE_LISTS: dict[str, str] = {
    "CODE_VIOLATION":               "Code Enforcement",
    "SHERIFF_MORTGAGE_FORECLOSURE": "Foreclosure",
    "PROBATE_ESTATE":               "Probate",
    "TAX_SALE":                     "Tax Sale",
    "EVICTION":                     "Eviction",
    "TAX_DELINQUENT":               "Tax Delinquent",
    "IMMINENTLY_DANGEROUS":         "Code Enforcement",
    "LIS_PENDENS":                  "Pre-Foreclosure",
}


# ── Cross-run upload ledger ─────────────────────────────────────────────────
# Persistent record of every key the pipeline has seen/uploaded, so re-scraped
# records don't re-upload and bump their DataSift date. CWD-relative like the
# phone and tax-delinquent caches; the GHA workflow persists it via actions/cache.
#
# v2 (2026-08): value is a dict per key instead of a bare membership set, so the
# Bid4Assets job can DIFF the standing auction inventory run-over-run instead of
# blindly skipping re-lists:
#   uploaded        bool — record was actually uploaded to DataSift
#   sale_date       str  — last auction date seen (auction records only)
#   address/zip     str  — situs address, kept so overlay upserts (postponement
#                          date bumps, gone-rechecks) can target the record by
#                          address after the NoticeData object is long gone
#   recheck_tagged  bool — auction-gone/past-date recheck tag already sent once
# v1 files (flat JSON list of uploaded keys) are converted on load.
_UPLOAD_LEDGER_PATH = Path("data/cache/uploaded_ledger.json")

# ── Bid4Assets auction overlay constants ────────────────────────────────────
# Sale-date admission bands adopted 2026-07-30: <14 days to sale is too late to
# work a deal, 14-90 days is the workable window, >90 days waits (the record
# re-enters banding each run until it rolls into the window). Past-date or
# vanished listings on records we DID upload trigger a tag-only disposition
# recheck instead of silent ledger skips.
_AUCTION_NOTICE_TYPES = {"SHERIFF_MORTGAGE_FORECLOSURE", "TAX_SALE"}
_AUCTION_MIN_DAYS = 14
_AUCTION_MAX_DAYS = 90

# Which scrape source feeds each auction notice_type — gone-detection only runs
# for a notice_type when its source was scraped THIS run and returned rows
# (otherwise a source outage would mark the whole inventory as vanished).
_AUCTION_SOURCE_FOR_TYPE = {
    "SHERIFF_MORTGAGE_FORECLOSURE": "bid4assets_mortgage",
    "TAX_SALE": "bid4assets_tax",
}


def _days_to_sale(auction_date: str) -> int | None:
    """Days from today to auction_date (YYYY-MM-DD); None if unparseable."""
    if not auction_date:
        return None
    try:
        dt = datetime.datetime.strptime(auction_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (dt - datetime.date.today()).days


def _ledger_key(n: NoticeData) -> str:
    """Stable identity for a record: (parcel_id or norm address) + notice_type.

    Scoped to the notice_type, not the property alone. The original property-only
    key stopped the same violation re-uploading every day inside the lookback
    window — still the main job, and the notice_type suffix preserves it. But it
    also blocked a parcel that reappeared on a genuinely NEW source: a June code
    violation that hits the tax-sale list in August was dropped at Step 3a before
    tier scoring ran, so it never joined the Tax Sale niche list and kept its
    day-one distress tag forever. Including notice_type lets that through, and the
    date bump is correct there — the property really did just get hotter.

    Note: this changes the key format, so keys written under the old scheme
    ("pid:X") never match and each parcel gets one final re-upload as the ledger
    re-seeds. One-time cost, bounded by the current ledger size.
    """
    ntype = (getattr(n, "notice_type", "") or "").strip()
    pid = (getattr(n, "parcel_id", "") or "").strip()
    if pid:
        return f"pid:{pid}:{ntype}"
    addr = re.sub(r"\s+", " ", (n.address or "").strip().lower())
    return f"addr:{addr}:{ntype}"


def _load_upload_ledger() -> dict[str, dict]:
    try:
        data = json.loads(_UPLOAD_LEDGER_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):
        # v1 → v2: every v1 key was an uploaded record
        return {k: {"uploaded": True} for k in data}
    return data if isinstance(data, dict) else {}


def _save_upload_ledger(ledger: dict[str, dict]) -> None:
    _UPLOAD_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _UPLOAD_LEDGER_PATH.write_text(json.dumps(ledger, sort_keys=True))


def _write_niche_list_csvs(notices: list[NoticeData]) -> list[dict]:
    """Split records by notice_type and write one DataSift CSV per niche list.

    Only generates CSVs for notice_types that have ≥1 record.  Returns a list
    of dicts: [{path, label, list_name, count}] in _NICHE_LISTS iteration order.
    """
    result: list[dict] = []
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    for notice_type, list_name in _NICHE_LISTS.items():
        # Use all_notice_types (merged compound signals) so a parcel flagged by
        # multiple sources appears in each matching niche list simultaneously.
        # DataSift handles multi-list membership at the CRM layer — no duplicates.
        subset = [
            n for n in notices
            if notice_type in (n.all_notice_types or n.notice_type or "").split(";")
        ]
        if not subset:
            logger.info("Niche list '%s': 0 records — skipping upload", list_name)
            continue
        safe = list_name.lower().replace(" ", "_")
        path = write_datasift_csv(
            subset,
            filename=f"philly_niche_{safe}_{len(subset)}recs_{timestamp}.csv",
        )
        result.append({"path": path, "label": list_name, "list_name": list_name, "count": len(subset)})
        logger.info("Niche CSV '%s': %d records → %s", list_name, len(subset), path)
    return result


# ── Bid4Assets auction overlay CSVs ─────────────────────────────────────────
# Minimal Add-Data upserts targeted at the existing niche lists. Deliberately
# tiny column sets: address (the upsert key), Tags, Lists, and — for
# postponements only — the sale-date column, so nothing else on the record
# (notes, phones, statuses) can be disturbed by the merge.

_OVERLAY_DATE_COL = {
    "SHERIFF_MORTGAGE_FORECLOSURE": "Foreclosure Date",
    "TAX_SALE": "Tax Auction Date",
}


def _fmt_mdy(iso_date: str) -> str:
    """YYYY-MM-DD → M/D/YYYY (DataSift date column format)."""
    try:
        dt = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except (ValueError, TypeError):
        return iso_date or ""


def _write_auction_overlay_csvs(
    postponed: list[dict], recheck: list[dict]
) -> list[dict]:
    """Write per-notice-type overlay CSVs for postponement date bumps and
    gone/past-date rechecks.

    Row dicts carry: ntype, address, city, state, zip, sale_date (postponed
    only), tags (list). Returns [{path, label, list_name, count, kind, keys}]
    where keys is the list of ledger keys the rows came from (committed only
    after that CSV's upload succeeds).
    """
    out: list[dict] = []
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    for kind, rows in (("postponed", postponed), ("recheck", recheck)):
        by_type: dict[str, list[dict]] = {}
        for r in rows:
            by_type.setdefault(r["ntype"], []).append(r)
        for ntype, subset in by_type.items():
            list_name = _NICHE_LISTS.get(ntype)
            if not list_name:
                continue
            date_col = _OVERLAY_DATE_COL.get(ntype)
            fieldnames = [
                "Property Street Address", "Property City", "Property State",
                "Property ZIP Code", "Tags", "Lists",
            ]
            if kind == "postponed" and date_col:
                fieldnames.append(date_col)
            safe = list_name.lower().replace(" ", "_")
            path = Path("output") / f"auction_overlay_{kind}_{safe}_{len(subset)}recs_{timestamp}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames)
                w.writeheader()
                for r in subset:
                    row = {
                        "Property Street Address": r["address"],
                        "Property City": r.get("city") or "Philadelphia",
                        "Property State": r.get("state") or "PA",
                        "Property ZIP Code": r.get("zip", ""),
                        "Tags": ",".join(r["tags"]),
                        "Lists": list_name,
                    }
                    if kind == "postponed" and date_col:
                        row[date_col] = _fmt_mdy(r.get("sale_date", ""))
                    w.writerow(row)
            out.append({
                "path": path,
                "label": f"{kind} → {list_name}",
                "list_name": list_name,
                "count": len(subset),
                "kind": kind,
                "keys": [r["key"] for r in subset],
                "sale_by_key": {r["key"]: r.get("sale_date", "") for r in subset},
            })
            logger.info("Auction overlay CSV (%s, %s): %d rows → %s",
                        kind, list_name, len(subset), path)
    return out


def _commit_auction_seen_refresh(
    seen_pending: dict[str, dict],
    overlay_refresh: list[tuple[str, "NoticeData"]],
) -> None:
    """Commit seen-only entries and refresh uploaded entries' address/zip
    (post-Smarty) + first-sight sale_date backfill for v1-era entries.

    sale_date on entries that already have one is deliberately NOT touched
    here — postponement bumps commit only after their overlay upload lands,
    so a failed overlay retries next run instead of being swallowed.
    """
    if not seen_pending and not overlay_refresh:
        return
    ledger = _load_upload_ledger()
    for k, ent in seen_pending.items():
        ledger[k] = ent
    for k, n in overlay_refresh:
        ent = ledger.setdefault(k, {"uploaded": True})
        ent["address"] = n.address
        ent["city"]    = n.city
        ent["state"]   = n.state
        ent["zip"]     = n.zip
        if not ent.get("sale_date") and n.auction_date:
            ent["sale_date"] = n.auction_date
    _save_upload_ledger(ledger)
    logger.info("Upload ledger: %d seen-only entries, %d refreshed (total %d)",
                len(seen_pending), len(overlay_refresh), len(ledger))


async def _run_auction_overlays(
    overlay_postponed_notices: list[dict],
    overlay_recheck_notices: list[dict],
    overlay_recheck_rows: list[dict],
) -> list[dict]:
    """Upload the auction overlay CSVs as Add-Data upserts into the existing
    niche lists (one browser session per CSV, mirroring Step 10b). On each
    successful upload, commits that CSV's ledger mutations: postponed rows
    bump sale_date; recheck rows set recheck_tagged so they fire only once.

    Overlays never create lists — existing_list=True only. Target addresses
    prefer the ledger-stored (uploaded) form over the current scrape's.
    """
    ledger = _load_upload_ledger()

    def _row(it: dict, with_sale: bool) -> dict:
        n = it["notice"]
        ent = ledger.get(it["key"], {})
        r = {
            "key": it["key"], "ntype": it["ntype"],
            "address": ent.get("address") or n.address,
            "city":    ent.get("city")    or n.city,
            "state":   ent.get("state")   or n.state,
            "zip":     ent.get("zip")     or n.zip,
            "tags": it["tags"],
        }
        if with_sale:
            r["sale_date"] = it["sale_date"]
        return r

    postponed_rows = [_row(it, True) for it in overlay_postponed_notices]
    recheck_rows   = [_row(it, False) for it in overlay_recheck_notices]
    recheck_rows.extend(overlay_recheck_rows)   # gone rows are already row-shaped

    if not postponed_rows and not recheck_rows:
        return []

    csv_infos = _write_auction_overlay_csvs(postponed_rows, recheck_rows)
    if not csv_infos:
        return []

    from playwright.async_api import async_playwright
    from datasift_core import login as _ds_login
    from datasift_uploader import upload_csv as _upload_csv

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    results: list[dict] = []
    for info in csv_infos:
        try:
            async with async_playwright() as _pw:
                _browser = await _pw.chromium.launch(headless=True)
                _ctx = await _browser.new_context(
                    viewport={"width": 1280, "height": 720}, user_agent=_UA,
                )
                _page = await _ctx.new_page()
                _ok = await _ds_login(_page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
                if _ok:
                    _r = await _upload_csv(
                        _page, info["path"],
                        list_name=info["list_name"],
                        existing_list=True,
                    )
                else:
                    _r = {"success": False, "message": "DataSift login failed"}
                await _browser.close()
        except Exception as _exc:
            _r = {"success": False, "message": str(_exc)}
            logger.error("Auction overlay upload '%s' error: %s", info["label"], _exc)

        if _r.get("success"):
            led = _load_upload_ledger()
            for key in info["keys"]:
                ent = led.setdefault(key, {"uploaded": True})
                if info["kind"] == "postponed":
                    new_sale = info["sale_by_key"].get(key, "")
                    if new_sale:
                        ent["sale_date"] = new_sale
                else:
                    ent["recheck_tagged"] = True
            _save_upload_ledger(led)

        _status = "OK" if _r.get("success") else f"FAILED: {_r.get('message', '')}"
        logger.info("Auction overlay '%s' (%d rows): %s",
                    info["label"], info["count"], _status)
        results.append({**{k: v for k, v in info.items() if k != "sale_by_key"},
                        "success": _r.get("success", False),
                        "message": _r.get("message", "")})
    return results


# ── DataSift CSV reader (for --resume-from) ─────────────────────────────────

def _parse_sift_date(s: str) -> str:
    """Convert M/D/YYYY → YYYY-MM-DD. Passes through YYYY-MM-DD and empty strings."""
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    try:
        dt = datetime.datetime.strptime(s.strip(), "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return s


def read_philly_datasift_csv(path: str | Path) -> list[NoticeData]:
    """Read a DataSift-formatted Philadelphia CSV back into NoticeData objects.

    Used by --resume-from to re-run enrichment (Smarty, RDI, validation) and
    re-upload with corrected Lists column without re-scraping or re-tracing.
    Phone and email fields are preserved from the original run.
    """
    notices: list[NoticeData] = []
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            # Reconstruct owner_name: entity records have empty First/Last
            first = r.get("Owner First Name", "").strip()
            last  = r.get("Owner Last Name", "").strip()
            entity_contact = r.get("Entity Contact", "").strip()
            if first or last:
                owner_name = f"{first} {last}".strip()
            elif entity_contact:
                owner_name = entity_contact   # preserves LLC/LP name for entity detection
            else:
                owner_name = ""

            # Auction date: each notice type uses a different built-in column
            nt = r.get("Notice Type", "")
            if "TAX_SALE" in nt or "tax_sale" in nt:
                auction_date = _parse_sift_date(r.get("Tax Auction Date", ""))
            elif "FORECLOSURE" in nt or "foreclosure" in nt:
                auction_date = _parse_sift_date(r.get("Foreclosure Date", ""))
            elif "PROBATE" in nt or "probate" in nt:
                auction_date = _parse_sift_date(r.get("Probate Open Date", ""))
            else:
                auction_date = ""

            n = NoticeData(
                notice_type   = nt,
                address       = r.get("Property Street Address", ""),
                city          = r.get("Property City", ""),
                state         = r.get("Property State", ""),
                zip           = r.get("Property ZIP Code", ""),
                owner_name    = owner_name,
                owner_street  = r.get("Mailing Street Address", ""),
                owner_city    = r.get("Mailing City", ""),
                owner_state   = r.get("Mailing State", ""),
                owner_zip     = r.get("Mailing ZIP Code", ""),
                parcel_id     = r.get("Parcel ID", ""),
                county        = r.get("County", ""),
                date_added    = _parse_sift_date(r.get("Date Added", "")),
                auction_date  = auction_date,
                source_url    = r.get("Source URL", ""),
                # Phones preserved from original run
                primary_phone = r.get("Phone 1", ""),
                mobile_1      = r.get("Phone 2", ""),
                mobile_2      = r.get("Phone 3", ""),
                mobile_3      = r.get("Phone 4", ""),
                mobile_4      = r.get("Phone 5", ""),
                mobile_5      = r.get("Phone 6", ""),
                landline_1    = r.get("Phone 7", ""),
                landline_2    = r.get("Phone 8", ""),
                landline_3    = r.get("Phone 9", ""),
                email_1       = r.get("Email 1", ""),
                email_2       = r.get("Email 2", ""),
                email_3       = r.get("Email 3", ""),
                email_4       = r.get("Email 4", ""),
                email_5       = r.get("Email 5", ""),
                # Property enrichment
                estimated_value     = r.get("Estimated Value", ""),
                mls_status          = r.get("MSL Status", ""),
                mls_last_sold_date  = _parse_sift_date(r.get("Last Sale Date", "")),
                mls_last_sold_price = r.get("Last Sale Price", ""),
                equity_percent      = r.get("Equity Percentage", ""),
                tax_delinquent_amount = r.get("Tax Deliquent Value", ""),  # DataSift typo
                tax_delinquent_years  = r.get("Tax Delinquent Year", ""),
                year_built  = r.get("Year Built", ""),
                sqft        = r.get("Living SqFt", ""),
                bedrooms    = r.get("Bedrooms", ""),
                bathrooms   = r.get("Bathrooms", ""),
                # Deep prospecting
                owner_deceased          = r.get("Owner Deceased", ""),
                date_of_death           = r.get("Date of Death", ""),
                decedent_name           = r.get("Decedent Name", ""),
                decision_maker_name     = r.get("Decision Maker", ""),
                decision_maker_relationship = r.get("DM Relationship", ""),
                dm_confidence           = r.get("DM Confidence", ""),
                obituary_url            = r.get("Obituary URL", ""),
                # Entity
                entity_type        = r.get("Entity Type", ""),
                entity_person_name = r.get("Entity Contact", ""),
            )
            notices.append(n)
    logger.info("Resume: read %d records from %s", len(notices), path)
    return notices


# ── Main entry point ────────────────────────────────────────────────────────


async def run_pipeline(
    sources: list[str],
    lookback: int = 30,
    limit: int | None = None,
    upload_datasift: bool = False,
    notify_slack: bool = False,
    filename: str | None = None,
    resume_from: str | None = None,
    phone_scoring: bool = True,
) -> dict:
    """Run the full Philadelphia enrichment pipeline.

    Args:
        sources:          List of enabled source_ids to scrape.
        lookback:         Days to look back for each source.
        limit:            Max records per source (None = no cap, i.e. production).
        upload_datasift:  Upload CSV to DataSift via Playwright after generation.
        notify_slack:     Send run summary to Slack/Discord webhook.
        filename:         Optional CSV filename override.

    Returns a dict with:
        csv_path           Path to the written DataSift CSV
        records            list[NoticeData] — final records (post all filters)
        records_by_source  dict[source_id → list] — post-cap, pre-filter (for display)
        stats              dict — per-step counts and costs
        upload_result      dict | None
    """
    t_start = time.time()

    stats: dict = {
        "scraped_by_source":    {},
        "opa_matched_by_source": {},
        "f2_dropped_by_source": {},
        "capped_by_source":     {},
        "total_after_cap":      0,
        "dedup_removed":        0,
        "total_after_dedup":    0,
        "smarty_matched":       0,
        "smarty_skipped":       0,
        "smarty_cache_hits":    0,
        "smarty_api_calls":     0,
        "smarty_cache_added":   0,
        "rdi_removed":          0,
        "validation_removed":   0,
        "already_uploaded_skipped": 0,
        "dp_candidates":        0,
        "tracerfy":             {},
        "tracerfy_cost":        0.0,
        "trestle_scored":       0,
        "trestle_cost":         0.0,
        "probate_dropped":      0,
        "csv_written":          0,
        "total_cost":           0.0,
        "elapsed_s":            0.0,
        "auction_banded_out":   {},
        "auction_postponed":    0,
        "auction_past_recheck": 0,
        "auction_gone":         0,
    }

    # ── 1–3. Scrape / Cap / Dedup  (or resume from existing CSV) ────────────
    records_by_source: dict[str, list[NoticeData]] = {}

    # Bid4Assets auction-overlay state, populated by Step 3a on real runs:
    #   fresh_pairs               (ledger_key, notice) for every newly-admitted record
    #   ledger_seen_pending       seen-but-not-admitted entries (banded out / nameless)
    #   overlay_postponed_notices sale-date changes on already-uploaded records
    #   overlay_recheck_notices   past-date-but-still-listed uploaded records
    #   overlay_recheck_rows      uploaded records GONE from the standing inventory
    #   overlay_refresh           (key, notice) for uploaded records seen this run —
    #                             their ledger address/zip is refreshed post-Smarty
    fresh_pairs: list[tuple[str, NoticeData]] = []
    ledger_seen_pending: dict[str, dict] = {}
    overlay_postponed_notices: list[dict] = []
    overlay_recheck_notices: list[dict] = []
    overlay_recheck_rows: list[dict] = []
    overlay_refresh: list[tuple[str, NoticeData]] = []

    if resume_from:
        # --resume-from: skip scrape, cap, and dedup — read records directly from
        # an existing DataSift CSV.  Smarty / RDI / validation still run so the
        # new CSV gets standardized addresses and the fixed Lists column.
        # Tracerfly and Trestle are also skipped — phones are already in the CSV.
        logger.info("── Resume mode: reading records from %s ──", resume_from)
        notices = read_philly_datasift_csv(resume_from)
        stats["total_after_cap"]   = len(notices)
        stats["total_after_dedup"] = len(notices)
        for sid in sources:
            records_by_source[sid] = []   # empty — not available in resume mode
        logger.info("Resume: %d records loaded (scrape/dedup skipped)", len(notices))
    else:
        # Normal path: scrape all sources
        logger.info("── Step 1: Scrape ──")
        payload = await run_philly_scrape(
            source_ids=sources,
            lookback_days=lookback,
            evictions_max_detail=limit,
        )
        raw_results: dict[str, list[NoticeData]] = payload["results"]
        f2_dropped:  dict[str, int]              = payload["filter2_dropped"]

        for sid in sources:
            all_recs = raw_results.get(sid, [])
            stats["scraped_by_source"][sid]    = len(all_recs)
            stats["f2_dropped_by_source"][sid] = f2_dropped.get(sid, 0)
            stats["opa_matched_by_source"][sid] = sum(
                1 for n in all_recs if _opa_meta(n).get("opa_match") is True
            )

        # ── 2. Cap per source ────────────────────────────────────────────────
        notices: list[NoticeData] = []
        for sid in sources:
            recs = raw_results.get(sid, [])
            capped = recs[:limit] if limit is not None else recs
            records_by_source[sid] = capped
            stats["capped_by_source"][sid] = len(capped)
            notices.extend(capped)
        stats["total_after_cap"] = len(notices)
        logger.info("After cap: %d total records", len(notices))

        # ── 3. Dedup by parcel_id → address ─────────────────────────────────
        logger.info("── Step 3: Dedup ──")
        notices, removed = _dedup_notices(notices)
        stats["dedup_removed"]   = removed
        stats["total_after_dedup"] = len(notices)

        # ── 3a. Cross-run upload ledger (first-to-market delta) ─────────────
        # _dedup_notices only dedupes WITHIN this run. Records that reappear on
        # later days (probate notices publish ~3 weeks; violations/evictions
        # persist in the lookback window) would otherwise re-upload every run,
        # bumping their DataSift date and re-filling each day's SiftStack
        # bucket. The ledger gives the pipeline cross-run memory: anything
        # already uploaded is dropped here, so the daily bucket holds only
        # genuinely-new records (Trestle still scores that bucket — untouched).
        # Skipped for --resume-from (records already came from a prior upload)
        # and dry runs (limit set / upload disabled), which must not consume it.
        if upload_datasift and limit is None:
            from datasift_formatter import _get_contact_info
            from philadelphia_scrapers import _set_meta

            ledger = _load_upload_ledger()
            fresh: list[NoticeData] = []
            skipped = 0

            # Auction-record identity of everything in TODAY'S scrape, for
            # gone-detection below.
            present_auction_keys = {
                _ledger_key(n) for n in notices
                if n.notice_type in _AUCTION_NOTICE_TYPES
            }

            for n in notices:
                k = _ledger_key(n)
                ent = ledger.get(k)

                if n.notice_type not in _AUCTION_NOTICE_TYPES:
                    # Non-auction sources: original behavior — one upload ever.
                    if ent is not None:
                        skipped += 1
                        continue
                    fresh.append(n)
                    fresh_pairs.append((k, n))
                    continue

                # ── Auction record (Bid4Assets) ─────────────────────────────
                new_sale = (n.auction_date or "").strip()
                days = _days_to_sale(new_sale)

                if ent is not None and ent.get("uploaded"):
                    # Already in DataSift: never re-upload the full record.
                    # Instead diff the sale date and refresh the ledger entry.
                    skipped += 1
                    old_sale = (ent.get("sale_date") or "").strip()
                    if new_sale and old_sale and new_sale != old_sale:
                        tag = ("auction-postponed" if new_sale > old_sale
                               else "auction-date-changed")
                        overlay_postponed_notices.append(
                            {"key": k, "notice": n, "ntype": n.notice_type,
                             "sale_date": new_sale, "tags": [tag]}
                        )
                    elif days is not None and days < 0 and not ent.get("recheck_tagged"):
                        # Past sale date but still listed — likely sold at
                        # auction; flag for disposition recheck (tag-only).
                        overlay_recheck_notices.append(
                            {"key": k, "notice": n, "ntype": n.notice_type,
                             "tags": ["auction-gone-recheck"]}
                        )
                    # Entry refresh (address backfill happens post-Smarty; the
                    # sale_date is only bumped after the overlay upload lands).
                    overlay_refresh.append((k, n))
                    continue

                # New (or previously seen-but-not-admitted) auction record:
                # sale-date admission bands + nameless gate. Stacked records
                # (auction merged with another distress signal at dedup) skip
                # the bands — multiple signals justify admission on their own.
                all_types = set((n.all_notice_types or n.notice_type or "").split(";"))
                if not all_types <= _AUCTION_NOTICE_TYPES:
                    band = "window"   # stacked: admit regardless of sale timing
                elif days is None:
                    band = "no_date"
                elif days < _AUCTION_MIN_DAYS:
                    band = "too_late"
                elif days > _AUCTION_MAX_DAYS:
                    band = "not_yet"
                else:
                    band = "window"
                    c = _get_contact_info(n)
                    if not (c["first"] or c["last"] or c["entity_name"]):
                        # Nameless even after OPA — dropped per 2026-08-06
                        # decision. Stays seen-only; re-gated every run in
                        # case a later scrape/OPA row names it.
                        band = "nameless"

                if band == "window":
                    # auction-window marks records inside the 14-90-day band
                    # (queue-prep ranks them top, assigned to the closer);
                    # stacked records admitted outside the band don't get it.
                    if days is not None and _AUCTION_MIN_DAYS <= days <= _AUCTION_MAX_DAYS:
                        _set_meta(n, extra_tags=["auction-window"])
                    fresh.append(n)
                    fresh_pairs.append((k, n))
                else:
                    stats["auction_banded_out"][band] = (
                        stats["auction_banded_out"].get(band, 0) + 1
                    )
                    ledger_seen_pending[k] = {
                        "uploaded": False, "sale_date": new_sale,
                    }
                    skipped += 1

            # Gone-detection: uploaded auction records that vanished from the
            # standing inventory (sold / withdrawn / cured). Scoped per
            # notice_type to sources that actually scraped rows this run, so a
            # source outage can't mark the whole book as vanished. Fires once
            # per record (recheck_tagged) and only for entries that carry the
            # uploaded address to target.
            for ntype, sid in _AUCTION_SOURCE_FOR_TYPE.items():
                if sid not in sources or stats["scraped_by_source"].get(sid, 0) == 0:
                    continue
                suffix = f":{ntype}"
                for k, ent in ledger.items():
                    if (not k.endswith(suffix) or not ent.get("uploaded")
                            or ent.get("recheck_tagged") or not ent.get("address")
                            or k in present_auction_keys):
                        continue
                    overlay_recheck_rows.append({
                        "key": k, "ntype": ntype,
                        "address": ent["address"], "zip": ent.get("zip", ""),
                        "city": ent.get("city", ""), "state": ent.get("state", ""),
                        "tags": ["auction-gone-recheck"],
                    })

            stats["already_uploaded_skipped"] = skipped
            stats["auction_postponed"] = len(overlay_postponed_notices)
            stats["auction_gone"] = len(overlay_recheck_rows)
            stats["auction_past_recheck"] = len(overlay_recheck_notices)
            logger.info(
                "── Step 3a: Upload ledger — %d new, %d skipped "
                "(auction bands: %s | postponed: %d | past-date recheck: %d | gone: %d) ──",
                len(fresh), skipped,
                stats["auction_banded_out"] or "none",
                len(overlay_postponed_notices), len(overlay_recheck_notices),
                len(overlay_recheck_rows),
            )
            notices = fresh

        # ── 3b. Tax delinquency enrichment overlay ───────────────────────────
        # Enrich non-TAX_DELINQUENT records that have a parcel_id but no tax data yet.
        logger.info("── Step 3b: Tax delinquency enrichment ──")
        try:
            from philadelphia_scrapers import _enrich_tax_delinquency
            td_enriched = await _enrich_tax_delinquency(notices)
            stats["tax_delinquent_enriched"] = td_enriched
            logger.info("Tax delinquency enrichment: %d records enriched", td_enriched)
        except Exception as exc:
            logger.warning("Tax delinquency enrichment failed (non-fatal): %s", exc)
            stats["tax_delinquent_enriched"] = 0

        # ── 3c. Expired permit enrichment overlay ────────────────────────────
        logger.info("── Step 3c: Expired permit enrichment ──")
        try:
            from philadelphia_scrapers import _enrich_expired_permits
            ep_enriched = await _enrich_expired_permits(notices)
            stats["expired_permit_enriched"] = ep_enriched
            logger.info("Expired permit enrichment: %d records flagged", ep_enriched)
        except Exception as exc:
            logger.warning("Expired permit enrichment failed (non-fatal): %s", exc)
            stats["expired_permit_enriched"] = 0

    # ── 4. Smarty address standardization (cache-aware) ─────────────────────
    logger.info("── Step 4: Smarty ──")
    if _SMARTY_AVAILABLE and config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
        smarty_cache = _load_smarty_cache()
        # Overlay-refresh notices (already-uploaded auction records seen this
        # run) ride the same standardization so their ledger address matches
        # the form DataSift holds — near-total cache hits since their parcels
        # were standardized when first uploaded.
        smarty_input = notices + [n for _, n in overlay_refresh]
        api_needed, cache_hits = _apply_smarty_cache(smarty_input, smarty_cache)
        stats["smarty_cache_hits"] = cache_hits

        if api_needed:
            standardize_addresses(api_needed, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN)
            retry_with_geocoded_city(api_needed, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN)
            stats["smarty_cache_added"] = _update_smarty_cache(api_needed, smarty_cache)
            _save_smarty_cache(smarty_cache)

        stats["smarty_api_calls"] = len(api_needed)
        stats["smarty_matched"]   = sum(1 for n in notices if n.dpv_match_code)
        stats["smarty_skipped"]   = sum(1 for n in notices if not n.address.strip())
        logger.info(
            "Smarty: %d API calls, %d cache hits, %d newly cached, %d total matched",
            stats["smarty_api_calls"], cache_hits,
            stats["smarty_cache_added"], stats["smarty_matched"],
        )
    else:
        reason = "SDK unavailable" if not _SMARTY_AVAILABLE else "credentials not set"
        logger.info("Smarty skipped (%s)", reason)

    # Auction ledger bookkeeping that does NOT depend on today's upload:
    # seen-only entries (banded-out/nameless records) and address/sale-date
    # refresh of already-uploaded entries (post-Smarty, so overlay upserts
    # target the exact address form DataSift holds). No-op on dry/micro runs.
    _commit_auction_seen_refresh(ledger_seen_pending, overlay_refresh)

    # ── 5. RDI commercial filter ─────────────────────────────────────────────
    logger.info("── Step 5: RDI commercial filter ──")
    notices, stats["rdi_removed"] = _filter_rdi_commercial(notices)

    # ── 6. Validation gate ───────────────────────────────────────────────────
    logger.info("── Step 6: Validation gate ──")
    before_validation = len(notices)
    notices = _validate_records(notices)
    stats["validation_removed"] = before_validation - len(notices)
    logger.info("Validation: %d removed, %d remaining",
                stats["validation_removed"], len(notices))

    if not notices:
        logger.warning("No records remaining after filters — aborting pipeline")
        # Auction overlays are independent of new admissions — a run with zero
        # fresh records can still carry postponement date bumps and gone
        # rechecks for the standing inventory, so flush them before returning.
        if upload_datasift and (overlay_postponed_notices
                                or overlay_recheck_notices or overlay_recheck_rows):
            logger.info("── Auction overlays (no new records this run) ──")
            stats["auction_overlay_results"] = await _run_auction_overlays(
                overlay_postponed_notices, overlay_recheck_notices,
                overlay_recheck_rows,
            )
        stats["elapsed_s"] = time.time() - t_start
        return {
            "csv_path": None,
            "records": [],
            "records_by_source": records_by_source,
            "stats": stats,
            "upload_result": None,
        }

    # ── 6.5: Obituary enrichment (non-probate records with owner_name) ────────
    # Only runs in full production mode (upload=True) — obituary search is slow
    # and rate-limited; micro-runs / dry-runs skip it entirely.
    logger.info("── Step 6.5: Obituary enrichment ──")
    obit_candidates = [
        n for n in notices
        if n.notice_type != "PROBATE_ESTATE" and (n.owner_name or "").strip()
    ]
    stats["obit_candidates"] = len(obit_candidates)
    if obit_candidates and not resume_from and upload_datasift:
        try:
            from obituary_enricher import enrich_obituary_data
            if config.ANTHROPIC_API_KEY:
                enrich_obituary_data(
                    obit_candidates,
                    api_key=config.ANTHROPIC_API_KEY,
                    skip_heir_verification=True,   # faster for Philly — no Knox Tax API
                    skip_dm_address=True,
                    skip_ancestry=True,
                )
                matched_count = sum(1 for n in obit_candidates if n.owner_deceased == "yes")
                stats["obit_matched"] = matched_count
                logger.info("Obituary enrichment: %d candidates, %d matched",
                            len(obit_candidates), matched_count)
            else:
                logger.info("Obituary enrichment: ANTHROPIC_API_KEY not set — skipping")
                stats["obit_matched"] = 0
        except Exception as exc:
            logger.warning("Obituary enrichment failed (non-fatal): %s", exc)
            stats["obit_matched"] = 0
    else:
        if resume_from:
            logger.info("Obituary enrichment: skipped (resume mode)")
        elif not upload_datasift:
            logger.info("Obituary enrichment: skipped (no-upload / dry-run mode)")
        else:
            logger.info("Obituary enrichment: 0 candidates — skipped")
        stats["obit_matched"] = 0

    # ── 6.6: Distress tier scoring ───────────────────────────────────────────
    # Tier is presentation/sorting metadata only — NOT a pipeline gate.
    # Every record uploads regardless of tier; DataSift presets do downstream
    # segmentation by distress_tier_X_Name tag.
    logger.info("── Step 6.6: Distress tier scoring ──")
    stats["tier0_dropped"] = 0   # no longer dropped

    from collections import Counter
    tier_dist = Counter(compute_distress_tier(n)[0] for n in notices)
    tier_str = "  ".join(f"T{t}={c}" for t, c in sorted(tier_dist.items()))
    logger.info("Distress tier distribution: %s", tier_str)
    stats["tier_distribution"] = dict(tier_dist)

    # ── 7. Tracerfly — DP candidates only (PROBATE_ESTATE) ──────────────────
    # Skipped in resume mode — phones already preserved from the original run.
    logger.info("── Step 7: Tracerfly (DP candidates only) ──")
    dp_candidates = [n for n in notices if _is_dp_candidate(n)]
    non_dp = len(notices) - len(dp_candidates)
    stats["dp_candidates"] = len(dp_candidates)

    if resume_from:
        logger.info("Tracerfly: skipped (resume mode — phones preserved from source CSV)")
    elif dp_candidates:
        logger.info("Tracerfly: %d DP candidates (%d non-DP skipped → DataSift bundled skip trace)",
                    len(dp_candidates), non_dp)
        if config.TRACERFY_API_KEY:
            from tracerfy_skip_tracer import batch_skip_trace
            tracerfy_stats = batch_skip_trace(
                dp_candidates,
                max_signing_traces=5,
                lookup_heir_addresses=False,
            )
            stats["tracerfy"]      = tracerfy_stats
            stats["tracerfy_cost"] = float(tracerfy_stats.get("cost", 0))
            logger.info("Tracerfly: %d/%d matched, %d phones, %d emails, $%.4f",
                        tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
                        tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
                        stats["tracerfy_cost"])
        else:
            logger.info("Tracerfly: TRACERFY_API_KEY not set — skipping")
    else:
        logger.info("Tracerfly: 0 DP candidates — skipped")

    # ── 8. Trestle phone scoring — DP candidates only ────────────────────────
    # Skipped in resume mode — Trestle scores already preserved from source CSV.
    logger.info("── Step 8: Trestle phone scoring (DP candidates only) ──")
    if resume_from:
        logger.info("Trestle: skipped (resume mode — scores preserved from source CSV)")
    elif dp_candidates and config.TRESTLE_API_KEY:
        from phone_validator import score_record_phones
        phone_results = score_record_phones(
            dp_candidates,
            api_key=config.TRESTLE_API_KEY,
            add_litigator=False,
        )
        stats["trestle_scored"] = len(phone_results)
        stats["trestle_cost"]   = stats["trestle_scored"] * 0.01
        logger.info("Trestle: %d phones scored, est. $%.4f",
                    stats["trestle_scored"], stats["trestle_cost"])
    elif dp_candidates:
        logger.info("Trestle: TRESTLE_API_KEY not set — skipping")
    else:
        logger.info("Trestle: no DP candidates — skipped")

    # ── 9. DataSift CSV generation ───────────────────────────────────────────
    logger.info("── Step 9: DataSift CSV ──")

    # Pre-compute probate_dropped to match what write_datasift_csv will drop (Bug 3).
    stats["probate_dropped"] = sum(
        1 for n in notices
        if n.notice_type == "PROBATE_ESTATE"
        and not _opa_meta(n).get("opa_match")
        and not n.address
    )

    if filename is None:
        if resume_from:
            tag = "resume"
        elif limit is not None:
            tag = "micro"
        else:
            tag = "daily"
        filename = f"philly_{tag}_{len(notices)}recs_{time.strftime('%Y%m%d_%H%M%S')}.csv"

    csv_path = write_datasift_csv(notices, filename=filename)
    stats["csv_written"] = len(notices) - stats["probate_dropped"]
    logger.info("CSV written: %s  (%d records, %d probate dropped)",
                csv_path, stats["csv_written"], stats["probate_dropped"])

    # ── 10. DataSift upload ──────────────────────────────────────────────────
    upload_result: dict | None = None
    if upload_datasift:
        logger.info("── Step 10: DataSift upload ──")
        try:
            from datasift_formatter import write_datasift_split_csvs
            from datasift_uploader import upload_datasift_split, upload_to_datasift

            csv_infos = write_datasift_split_csvs(notices)
            for info in csv_infos:
                logger.info("DataSift CSV (%s): %s", info["label"], info["path"])

            if len(csv_infos) > 1:
                upload_result = await upload_datasift_split(
                    csv_infos, enrich=True, skip_trace=True,
                )
            else:
                upload_result = await upload_to_datasift(
                    csv_infos[0]["path"], enrich=True, skip_trace=True,
                )

            if upload_result and upload_result.get("success"):
                logger.info("DataSift upload: %s", upload_result.get("message", "OK"))
            else:
                logger.error("DataSift upload failed: %s",
                             upload_result.get("message") if upload_result else "no result")
        except Exception as exc:
            logger.error("DataSift upload error: %s", exc, exc_info=True)
            upload_result = {"success": False, "message": str(exc)}

        # Commit today's new records to the ledger ONLY if the upload landed —
        # on failure they stay off the ledger so the next run retries them
        # instead of silently dropping them. Auction records that survived to
        # upload store their post-Smarty address + sale date so later overlay
        # upserts (postponements, gone-rechecks) can target them; records
        # admitted at 3a but dropped by RDI/validation are ledgered WITHOUT an
        # address, which excludes them from every overlay stream (they were
        # never uploaded — an upsert would create a stray record).
        if fresh_pairs and upload_result and upload_result.get("success"):
            ledger = _load_upload_ledger()
            final_ids = {id(n) for n in notices}
            for k, n in fresh_pairs:
                ent: dict = {"uploaded": True}
                if n.notice_type in _AUCTION_NOTICE_TYPES and id(n) in final_ids:
                    ent.update({
                        "sale_date": n.auction_date, "address": n.address,
                        "city": n.city, "state": n.state, "zip": n.zip,
                    })
                ledger[k] = ent
            _save_upload_ledger(ledger)
            logger.info("Upload ledger: recorded %d new keys (total %d)",
                        len(fresh_pairs), len(ledger))

    # ── 10b. Niche list uploads (Option B-1) ────────────────────────────────
    # Each niche list upload runs in its OWN browser session to avoid shared
    # page-state corruption (open dropdowns, stale wizard steps) that caused
    # all 5 uploads to fail when sharing one session.
    # Enrich and skip trace are NOT triggered here — bucket only.
    niche_results: list[dict] = []
    if upload_datasift:
        niche_csvs = _write_niche_list_csvs(notices)
        if niche_csvs:
            logger.info("── Step 10b: Niche list uploads (%d lists) ──", len(niche_csvs))
            from playwright.async_api import async_playwright
            from datasift_core import login as _ds_login
            from datasift_uploader import upload_csv as _upload_csv

            _UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )

            for _info in niche_csvs:
                try:
                    async with async_playwright() as _pw:
                        _browser = await _pw.chromium.launch(headless=True)
                        _ctx = await _browser.new_context(
                            viewport={"width": 1280, "height": 720},
                            user_agent=_UA,
                        )
                        _page = await _ctx.new_page()
                        _ok = await _ds_login(_page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
                        if _ok:
                            # Try adding to existing niche list.
                            # Fall back to creating new on first-ever run.
                            _r = await _upload_csv(
                                _page, _info["path"],
                                list_name=_info["list_name"],
                                existing_list=True,
                            )
                            if not _r.get("success"):
                                logger.info("  '%s': existing list not found — creating new",
                                            _info["list_name"])
                                _r = await _upload_csv(
                                    _page, _info["path"],
                                    list_name=_info["list_name"],
                                    existing_list=False,
                                )
                        else:
                            _r = {"success": False, "message": "DataSift login failed"}
                        await _browser.close()
                except Exception as _exc:
                    _r = {"success": False, "message": str(_exc)}
                    logger.error("Niche upload '%s' error: %s", _info["list_name"], _exc)

                _status = "OK" if _r.get("success") else f"FAILED: {_r.get('message', '')}"
                logger.info("Niche upload '%s' (%d records): %s",
                            _info["list_name"], _info["count"], _status)
                niche_results.append({
                    **_info,
                    "success": _r.get("success", False),
                    "message": _r.get("message", ""),
                })

    # ── 10c. Auction overlay uploads (Bid4Assets diff engine) ───────────────
    # Postponement date bumps + gone/past-date rechecks for the standing
    # auction inventory. Independent of today's fresh upload result — a failed
    # Step 10 must not swallow a postponement signal.
    overlay_results: list[dict] = []
    if upload_datasift and (overlay_postponed_notices
                            or overlay_recheck_notices or overlay_recheck_rows):
        logger.info("── Step 10c: Auction overlay uploads ──")
        overlay_results = await _run_auction_overlays(
            overlay_postponed_notices, overlay_recheck_notices,
            overlay_recheck_rows,
        )
        stats["auction_overlay_results"] = overlay_results

    # ── 11. Phone scoring (wait for skip trace → Trestle → upload tags) ────────
    # Runs only when: upload happened AND phone_scoring=True AND not micro-run.
    # Polls the daily bucket for phones (up to 3 × 5 min), then scores with
    # Trestle and uploads tier tags back.  Failures are non-fatal — pipeline
    # continues and reports the failure in the Slack summary.
    phone_result: dict = {}
    if upload_datasift and phone_scoring and limit is None:
        logger.info("── Step 11: Phone scoring (wait for skip trace) ──")
        try:
            from phone_scorer import score_and_tag
            bucket_list = f"SiftStack {datetime.date.today().isoformat()}"
            phone_result = await score_and_tag(
                list_name=bucket_list,
                email=config.DATASIFT_EMAIL,
                password=config.DATASIFT_PASSWORD,
                api_key=config.TRESTLE_API_KEY or "",
                do_upload=True,
                max_retries=3,
                wait_seconds=300,
            )
            trestle_cost_phone = phone_result.get("cost", 0.0)
            stats["trestle_cost"]  = stats.get("trestle_cost", 0.0) + trestle_cost_phone
            logger.info(
                "Phone scoring: %d found, %d scored, $%.2f  (upload: %s)",
                phone_result.get("phones_found", 0),
                phone_result.get("phones_scored", 0),
                trestle_cost_phone,
                "OK" if phone_result.get("upload_ok") else "FAIL",
            )
        except Exception as exc:
            logger.error("Phone scoring failed: %s", exc, exc_info=True)
            phone_result = {"skipped": True, "message": str(exc)}
    elif upload_datasift and phone_scoring and limit is not None:
        logger.info("Phone scoring skipped (micro-run — limit set)")
    elif upload_datasift and not phone_scoring:
        logger.info("Phone scoring skipped (--no-phone-scoring)")

    # ── Totals ───────────────────────────────────────────────────────────────
    stats["total_cost"] = stats["tracerfy_cost"] + stats.get("trestle_cost", 0.0)
    stats["elapsed_s"]  = time.time() - t_start

    # ── 12. Combined Slack summary ────────────────────────────────────────────
    if notify_slack and config.SLACK_WEBHOOK_URL:
        try:
            from slack_notifier import _send_webhook
            _send_webhook(_build_slack_summary(
                sources=sources,
                stats=stats,
                notices=notices,
                upload_result=upload_result,
                niche_results=niche_results,
                phone_result=phone_result,
                resume_from=resume_from,
            ))
            logger.info("Slack notification sent")
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

    return {
        "csv_path":          csv_path,
        "records":           notices,
        "records_by_source": records_by_source,
        "stats":             stats,
        "upload_result":     upload_result,
        "niche_results":     niche_results,
        "phone_result":      phone_result,
    }


def _build_slack_summary(
    sources: list[str],
    stats: dict,
    notices: list,
    upload_result: dict | None,
    niche_results: list[dict],
    phone_result: dict,
    resume_from: str | None,
) -> str:
    """Build a single combined Slack message for the full daily run."""
    lines: list[str] = []
    mode = "Resume" if resume_from else "Daily"

    # Determine if any DataSift upload failed
    bucket_failed = upload_result is not None and not upload_result.get("success")
    niche_failed = any(not r.get("success") for r in (niche_results or []))
    upload_failed = bucket_failed or niche_failed

    status = "⚠️ UPLOAD FAILED" if upload_failed else "Run Complete"
    lines.append(f"*SiftStack Philly — {mode} {status}*")

    # Scrape counts
    if not resume_from:
        src_parts = []
        for sid in sources:
            n = stats["scraped_by_source"].get(sid, 0)
            src_parts.append(f"{sid.replace('_', ' ')}={n}")
        lines.append(f"Scrape: {' | '.join(src_parts)}")
    else:
        lines.append(f"Source CSV: {resume_from}")

    # Filters
    # already-sent = the upload-ledger delta (Step 3a). It's the number that
    # proves "only new records went up" — a large scrape with already-sent 0
    # on a re-listing source (bid4assets) means the ledger cache was lost.
    lines.append(
        f"Filters: dedup −{stats['dedup_removed']}  "
        f"RDI −{stats['rdi_removed']}  "
        f"validation −{stats['validation_removed']}  "
        f"tier0 −{stats.get('tier0_dropped', 0)}  "
        f"already-sent −{stats.get('already_uploaded_skipped', 0)}  "
        f"→ {stats['csv_written']} records"
    )

    # Auction overlay (Bid4Assets diff engine)
    banded = stats.get("auction_banded_out") or {}
    if banded or stats.get("auction_postponed") or stats.get("auction_gone") \
            or stats.get("auction_past_recheck"):
        band_str = "  ".join(f"{k}={v}" for k, v in sorted(banded.items())) or "none"
        ov = stats.get("auction_overlay_results") or []
        ov_ok = sum(1 for r in ov if r.get("success"))
        lines.append(
            f"Auction: banded-out [{band_str}]  "
            f"postponed {stats.get('auction_postponed', 0)}  "
            f"past-recheck {stats.get('auction_past_recheck', 0)}  "
            f"gone {stats.get('auction_gone', 0)}"
            + (f"  (overlays {ov_ok}/{len(ov)} ✓)" if ov else "")
        )

    # Distress tier distribution
    tier_dist = stats.get("tier_distribution", {})
    if tier_dist:
        tier_labels = {1: "Cold", 2: "Warm", 3: "Hot", 4: "Critical"}
        tier_str = "  ".join(
            f"T{t}({tier_labels.get(t, '?')})={c}"
            for t, c in sorted(tier_dist.items())
        )
        lines.append(f"Tiers: {tier_str}")

    # Bucket upload
    ur = upload_result or {}
    bucket_ok = "✓" if ur.get("success") else "✗"
    lines.append(
        f"Bucket: SiftStack {__import__('datetime').date.today()} — "
        f"{bucket_ok} ({stats['csv_written']} records)"
    )

    # Niche uploads
    if niche_results:
        niche_line = "  ".join(
            f"{'✓' if r['success'] else '✗'} {r['list_name']} ({r['count']})"
            for r in niche_results
        )
        lines.append(f"Niche: {niche_line}")
    else:
        lines.append("Niche: not run")

    # Phone scoring
    if phone_result:
        if phone_result.get("skipped"):
            msg = phone_result.get("message", "")
            prefix = "Phones: ✗ skipped" if "login" in msg.lower() or "failed" in msg.lower() else "Phones: skipped"
            lines.append(f"{prefix} — {msg}")
        else:
            tier_parts = "  ".join(
                f"{t}: {c}" for t, c in phone_result.get("tier_counts", {}).items() if c
            )
            tag_ok = "✓" if phone_result.get("upload_ok") else "✗"
            lines.append(
                f"Phones: {phone_result.get('phones_found', 0)} found → "
                f"{phone_result.get('phones_scored', 0)} scored  "
                f"tags {tag_ok}  |  {tier_parts}"
            )
    else:
        lines.append("Phones: not run")

    # Cost + elapsed
    lines.append(
        f"Cost: Tracerfly ${stats['tracerfy_cost']:.2f} + "
        f"Trestle ${stats.get('trestle_cost', 0.0):.2f} = "
        f"${stats['total_cost']:.2f}"
    )
    elapsed_min = stats["elapsed_s"] / 60
    lines.append(f"Elapsed: {elapsed_min:.0f} min")

    return "\n".join(lines)
