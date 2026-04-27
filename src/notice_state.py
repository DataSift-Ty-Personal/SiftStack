"""Persistent notice-state cache — cross-run dedup + enrichment carry-forward.

Why this module exists
======================
Every daily SiftStack run scrapes the past 7-35 days of records from each OH
portal. Without persistence, the pipeline re-enriches the SAME records each
day — burning Smarty/Zillow/obituary API calls on data we already have.

Today (broken state):
  - Daily run scrapes ~166 records, mostly seen before
  - Pipeline runs full enrichment on every one (Smarty, Zillow, obituary,
    Tracerfy) — duplicate API costs daily
  - DataSift dedups on upload (so Mike sees no duplicates), but the
    enrichment was wasted upstream
  - Run time: ~2h 10m mostly because obituary search doesn't know which
    records were already confirmed deceased

With this module:
  - Every record gets a canonical key (case_number > parcel_id > fallback)
  - Each daily run merges today's scrape with prior state — fields from
    prior enrichment carry forward
  - Pipeline's existing "is field already set?" guards naturally skip
    re-enriching records → near-zero API cost on already-known records
  - Truly NEW records get full enrichment
  - Slack summary now reports: "X new, Y carried forward, Z aged out"

This module REPLACES foreclosure_case_state.py — single source of truth
for ALL distress types (foreclosure, probate, lis_pendens, tax_sale,
redemption-window).

State schema (output/notice_state.json)
========================================
{
  "Franklin:case:24CV003703": {
    "key": "Franklin:case:24CV003703",
    "county": "Franklin",
    "notice_type": "foreclosure",
    "first_scraped": "2026-04-22",
    "last_scraped": "2026-04-27",
    "scrape_count": 6,
    "record": {<full NoticeData fields as dict>}
  },
  ...
}

Retention rules (per distress type, days from first_scraped)
============================================================
  - foreclosure:   90 days  (covers pre-auction + redemption + buffer)
  - lis_pendens:   180 days (cases mature to sheriff sale in ~6 months)
  - probate:       365 days (probate often takes 6-18 months to resolve)
  - tax_sale:      90 days
  - other:         60 days

Plus special rule: foreclosure records with redemption_window_status=closed
expire 7 days after confirmation (window is gone; brief buffer for Mike).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from models import NoticeData

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────────────

STATE_FILE = config.OUTPUT_DIR / "notice_state.json"

# Legacy state file from foreclosure_case_state.py — auto-migrated on first load
LEGACY_FORECLOSURE_STATE_FILE = config.OUTPUT_DIR / "foreclosure_case_state.json"

# Per-distress retention (days from first_scraped) — see module docstring
RETENTION_DAYS_BY_TYPE: dict[str, int] = {
    "foreclosure":   90,
    "lis_pendens":   180,
    "probate":       365,
    "tax_sale":      90,
    "tax_delinquent": 90,
    "eviction":      90,
    "code_violation": 180,
    "divorce":       365,
}
DEFAULT_RETENTION_DAYS = 60

# Foreclosure records past redemption-confirmation hold a short tail in state
# so Mike can still see "Sold + window closed" in DataSift briefly.
RETENTION_AFTER_REDEMPTION_CLOSED_DAYS = 7

# Active redemption-watch window — used by get_active_foreclosure_case_numbers
REDEMPTION_WATCH_WINDOW_DAYS = 45


# ── Public API ─────────────────────────────────────────────────────────

def compute_record_key(r: NoticeData) -> str:
    """Canonical join key for cross-run dedup.

    Priority order:
      1. case_number (most stable for foreclosure / lis_pendens / probate)
      2. parcel_id (foreclosure with auditor data)
      3. (county, notice_type, decedent_name, date_added) — probate fallback
      4. source_url — last resort

    Returns "" if no usable key — record can't be deduped, gets re-enriched
    every run (rare; should only happen on malformed records).
    """
    if r.case_number:
        return f"{r.county}:case:{r.case_number}"
    if r.parcel_id:
        return f"{r.county}:parcel:{r.parcel_id}"
    if r.county and r.notice_type and r.decedent_name and r.date_added:
        # Probate fallback — decedent name + filing date is unique enough
        return f"{r.county}:{r.notice_type}:dec:{r.decedent_name}:{r.date_added}"
    if r.source_url:
        return f"url:{r.source_url}"
    return ""


def load_state() -> dict[str, dict]:
    """Load persisted state. Auto-migrates legacy foreclosure_case_state.json
    on first read after upgrade. Returns dict keyed by canonical record key.
    """
    state = _load_primary()
    legacy = _load_legacy_foreclosure_state()
    if legacy:
        # Merge legacy into primary, primary wins on conflicts
        for k, v in legacy.items():
            state.setdefault(k, v)
        # Save the merged state and remove the legacy file (one-time migration)
        _write_state(state)
        try:
            LEGACY_FORECLOSURE_STATE_FILE.unlink()
            logger.info(
                "Migrated %d entries from %s → %s (legacy file removed)",
                len(legacy), LEGACY_FORECLOSURE_STATE_FILE.name, STATE_FILE.name,
            )
        except OSError:
            pass
    return state


def merge_with_today(today_records: list[NoticeData]) -> tuple[list[NoticeData], dict]:
    """Merge today's scrape with persisted state.

    Returns:
        (records, stats) where:
        - records: list of NoticeData = union of today's scrape AND
          carried-forward records from state. Today's data wins on
          conflicts; previously-enriched fields carry forward to today's
          records when today's are blank.
        - stats: dict with keys 'new', 'carried_forward', 'aged_out',
          'updated' — for Slack summary + audit log.

    The pipeline runs against the returned `records` list. The pipeline's
    own "is field already set?" guards (e.g. obituary_enricher line 2121)
    will skip re-enriching records whose fields were carried forward from
    state, near-zeroing the API cost on already-known records.
    """
    state = load_state()
    today_iso = date.today().isoformat()

    # Build lookup of today's records by key
    today_by_key: dict[str, NoticeData] = {}
    no_key_records: list[NoticeData] = []
    for r in today_records:
        key = compute_record_key(r)
        if not key:
            no_key_records.append(r)
            continue
        today_by_key[key] = r

    # Merge: today + carried forward
    merged: list[NoticeData] = []
    new_count = 0
    updated_count = 0
    carried_count = 0

    for key, today_record in today_by_key.items():
        if key in state:
            # Existing record — merge carry-forward fields, update meta
            entry = state[key]
            cached_record = _entry_to_notice(entry)
            _merge_carry_forward_fields(today_record, cached_record)
            entry["last_scraped"] = today_iso
            entry["scrape_count"] = entry.get("scrape_count", 1) + 1
            entry["record"] = dataclasses.asdict(today_record)
            updated_count += 1
        else:
            # New record — add to state
            state[key] = {
                "key": key,
                "county": today_record.county,
                "notice_type": today_record.notice_type,
                "first_scraped": today_iso,
                "last_scraped": today_iso,
                "scrape_count": 1,
                "record": dataclasses.asdict(today_record),
            }
            new_count += 1
        merged.append(today_record)

    # Carry forward records from state that aren't in today's scrape
    today_keys = set(today_by_key.keys())
    for key, entry in state.items():
        if key in today_keys:
            continue
        carried = _entry_to_notice(entry)
        merged.append(carried)
        carried_count += 1

    # Records without a usable key still flow through (no dedup, but no loss)
    merged.extend(no_key_records)

    # Prune aged entries from state and merged list both
    pruned_state, aged_count = _prune_aged(state)
    pruned_merged = _prune_aged_list(merged, today_iso)

    # Save updated state
    _write_state(pruned_state)

    stats = {
        "today_scraped": len(today_records),
        "new": new_count,
        "updated": updated_count,
        "carried_forward": carried_count,
        "aged_out": aged_count,
        "no_key": len(no_key_records),
        "total_active": len(pruned_merged),
    }
    logger.info(
        "Notice state merge: %d scraped today (%d new, %d updated), "
        "%d carried from prior runs, %d aged out, %d active total",
        stats["today_scraped"], new_count, updated_count, carried_count,
        aged_count, stats["total_active"],
    )
    return pruned_merged, stats


def get_active_foreclosure_case_numbers(window_days: int = REDEMPTION_WATCH_WINDOW_DAYS) -> list[str]:
    """Return foreclosure case_numbers within the redemption-watch window.

    Used by redemption_watcher.py — replaces the same-named function in
    the (deprecated) foreclosure_case_state module.
    """
    state = load_state()
    cutoff = date.today() - timedelta(days=window_days)
    out: list[str] = []
    for entry in state.values():
        if entry.get("notice_type") != "foreclosure":
            continue
        record = _entry_to_notice(entry)
        if not record.case_number:
            continue
        if record.redemption_window_status == "closed":
            continue
        ref_date = (
            _parse_iso(record.sheriff_sale_held_date)
            or _parse_iso(record.auction_date)
            or _parse_iso(entry.get("first_scraped", ""))
        )
        if ref_date is None or ref_date >= cutoff:
            out.append(record.case_number)
    return out


def get_state_summary() -> dict:
    """Return a summary dict of current state (for diagnostics + Slack)."""
    state = load_state()
    by_type: dict[str, int] = {}
    by_county: dict[str, int] = {}
    redemption_open = 0
    redemption_closing = 0
    for entry in state.values():
        nt = entry.get("notice_type", "unknown")
        ct = entry.get("county", "unknown")
        by_type[nt] = by_type.get(nt, 0) + 1
        by_county[ct] = by_county.get(ct, 0) + 1
        rec = entry.get("record", {})
        rws = rec.get("redemption_window_status", "")
        if rws == "open":
            redemption_open += 1
        elif rws == "closing":
            redemption_closing += 1
    return {
        "total_active": len(state),
        "by_type": by_type,
        "by_county": by_county,
        "redemption_open": redemption_open,
        "redemption_closing": redemption_closing,
    }


# ── Internal helpers ───────────────────────────────────────────────────

def _load_primary() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Notice state file unreadable (%s) — starting fresh", e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Notice state has unexpected shape — starting fresh")
        return {}
    return raw


def _load_legacy_foreclosure_state() -> dict[str, dict]:
    """Load the legacy foreclosure_case_state.json format (list of entries)
    and convert to the new dict-keyed format."""
    if not LEGACY_FORECLOSURE_STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(LEGACY_FORECLOSURE_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for entry in raw:
        record_dict = entry.get("record") or {}
        # Reconstruct a NoticeData briefly to compute the canonical key
        valid_fields = {f.name for f in dataclasses.fields(NoticeData)}
        filtered = {k: v for k, v in record_dict.items() if k in valid_fields}
        try:
            r = NoticeData(**filtered)
        except (TypeError, KeyError):
            continue
        key = compute_record_key(r)
        if not key:
            continue
        out[key] = {
            "key": key,
            "county": r.county,
            "notice_type": r.notice_type or "foreclosure",
            "first_scraped": entry.get("first_seen_date") or "",
            "last_scraped": entry.get("last_updated_date") or "",
            "scrape_count": 1,
            "record": record_dict,
        }
    return out


def _write_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


def _entry_to_notice(entry: dict) -> NoticeData:
    record_dict = entry.get("record") or {}
    valid_fields = {f.name for f in dataclasses.fields(NoticeData)}
    filtered = {k: v for k, v in record_dict.items() if k in valid_fields}
    try:
        return NoticeData(**filtered)
    except TypeError:
        return NoticeData()


def _merge_carry_forward_fields(target: NoticeData, source: NoticeData) -> None:
    """Carry forward enrichment fields from `source` (cached) onto `target`
    (today's scrape) when target's field is blank.

    Only carries forward EXPENSIVE-to-recompute fields. Identifying fields
    (case_number, parcel_id) and date fields stay as today's scrape (most
    recent).
    """
    enrichment_fields = (
        # Smarty (~$0.01/record)
        "zip_plus4", "latitude", "longitude", "dpv_match_code", "vacant", "rdi",
        # Zillow (~$0.01/record)
        "mls_status", "mls_listing_price", "mls_last_sold_date", "mls_last_sold_price",
        "estimated_value", "estimated_equity", "equity_percent",
        "property_type", "bedrooms", "bathrooms", "sqft", "year_built", "lot_size",
        # Obituary (~$0.001/record × 6-10 sec compute)
        "owner_deceased", "date_of_death", "obituary_url",
        "decision_maker_name", "decision_maker_relationship",
        "decision_maker_status", "decision_maker_source",
        "decision_maker_street", "decision_maker_city", "decision_maker_state", "decision_maker_zip",
        "decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status",
        "decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status",
        "obituary_source_type", "heir_search_depth",
        "heirs_verified_living", "heirs_verified_deceased", "heirs_unverified",
        "heir_map_json", "signing_chain_count", "signing_chain_names",
        "dm_confidence", "dm_confidence_reason", "missing_data_flags",
        # Mailability flag
        "mailable",
        # Entity research
        "entity_type", "entity_person_name", "entity_person_role",
        "entity_research_source", "entity_research_confidence",
        # Tracerfy / Trestle (skip trace + phone scoring — paid)
        "primary_phone",
        "mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5",
        "landline_1", "landline_2", "landline_3",
        "email_1", "email_2", "email_3", "email_4", "email_5",
        # County assessor
        "tax_delinquent_amount", "tax_delinquent_years",
        # Owner mailing (often comes from auditor lookup)
        "owner_street", "owner_city", "owner_state", "owner_zip",
        # Deceased owner detection
        "deceased_indicator", "tax_owner_name",
        # PDF report link
        "report_url",
        # Redemption fields (set by watcher)
        "sheriff_sale_held_date", "confirmation_hearing_date",
        "redemption_window_status", "redemption_window_days_remaining",
    )
    for field_name in enrichment_fields:
        if not getattr(target, field_name, "") and getattr(source, field_name, ""):
            setattr(target, field_name, getattr(source, field_name))


def _parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _retention_days(notice_type: str) -> int:
    return RETENTION_DAYS_BY_TYPE.get(notice_type, DEFAULT_RETENTION_DAYS)


def _is_aged(entry: dict, today: date) -> bool:
    """Whether a state entry has aged past its retention window."""
    record = _entry_to_notice(entry)

    # Special case: foreclosure with redemption_closed → 7-day tail
    if (
        entry.get("notice_type") == "foreclosure"
        and record.redemption_window_status == "closed"
    ):
        close_ref = (
            _parse_iso(record.confirmation_hearing_date)
            or _parse_iso(record.sheriff_sale_held_date)
        )
        if close_ref and (today - close_ref).days > RETENTION_AFTER_REDEMPTION_CLOSED_DAYS:
            return True
        return False

    # Standard retention
    anchor = (
        _parse_iso(entry.get("first_scraped"))
        or _parse_iso(record.date_added)
        or _parse_iso(record.auction_date)
    )
    if anchor is None:
        return False  # no usable timestamp → keep one cycle
    retention = _retention_days(entry.get("notice_type", ""))
    return (today - anchor).days > retention


def _prune_aged(state: dict[str, dict]) -> tuple[dict[str, dict], int]:
    today = date.today()
    pruned: dict[str, dict] = {}
    aged = 0
    for key, entry in state.items():
        if _is_aged(entry, today):
            aged += 1
            continue
        pruned[key] = entry
    return pruned, aged


def _prune_aged_list(records: list[NoticeData], today_iso: str) -> list[NoticeData]:
    """Same retention rules applied to a list of NoticeData (used to filter
    the merged list before returning to caller)."""
    today = date.today()
    out = []
    for r in records:
        # Build a synthetic entry to reuse the aging logic
        entry = {
            "notice_type": r.notice_type,
            "first_scraped": r.date_added or today_iso,
            "record": dataclasses.asdict(r),
        }
        if not _is_aged(entry, today):
            out.append(r)
    return out


# ── Standalone diagnostic ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Notice state inspector")
    parser.add_argument("--summary", action="store_true", help="Show state summary")
    parser.add_argument("--show", action="store_true", help="Show all state entries")
    parser.add_argument("--reset", action="store_true", help="Wipe state file (with confirmation)")
    parser.add_argument(
        "--active-foreclosure",
        action="store_true",
        help="Show foreclosure case_numbers within redemption watch window",
    )
    args = parser.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            confirm = input(f"Delete {STATE_FILE}? [y/N]: ").strip().lower()
            if confirm == "y":
                STATE_FILE.unlink()
                print(f"Deleted {STATE_FILE}")
            else:
                print("Aborted")
        else:
            print(f"State file does not exist: {STATE_FILE}")
        raise SystemExit(0)

    if args.summary:
        s = get_state_summary()
        print(f"Total active records: {s['total_active']}")
        print(f"By type: {s['by_type']}")
        print(f"By county: {s['by_county']}")
        print(f"Redemption open: {s['redemption_open']}")
        print(f"Redemption closing: {s['redemption_closing']}")

    if args.show:
        state = load_state()
        for key, entry in sorted(state.items()):
            r = _entry_to_notice(entry)
            print(
                f"  {key[:40]:40} {entry.get('notice_type','?'):12} "
                f"first={entry.get('first_scraped','-'):10} "
                f"last={entry.get('last_scraped','-'):10} "
                f"scrapes={entry.get('scrape_count','?'):3} "
                f"redemption={r.redemption_window_status or '-':8} "
                f"deceased={r.owner_deceased or '-':3}"
            )

    if args.active_foreclosure:
        active = get_active_foreclosure_case_numbers()
        print(f"Active foreclosure case_numbers (in {REDEMPTION_WATCH_WINDOW_DAYS}-day watch window): {len(active)}")
        for cn in active:
            print(f"  {cn}")
