"""Master ledger — generates 3 CSVs and upserts them to Google Drive each
daily run. Single source of truth for "every record SiftStack has ever seen."

Three files end up in the configured Drive folder:

  1. WHO_master_ledger_active.csv
     One row per record currently in notice_state (the active set). Refreshed
     each run — overwrites the file in place. Open in Drive → "Open with
     Google Sheets" for sortable/filterable view.

  2. WHO_master_ledger_daily_summary.csv
     One row per daily run — total scraped, NEW, carried forward, aged out,
     by-type/by-county breakdown, run duration, cost. Appends each run.

  3. WHO_master_ledger_aged_out.csv
     Records that aged out of state today. Appends each run. Aaron's
     historical archive — "show me everything we've ever scraped, even
     records that retired."

The point: Aaron + Mike can audit "is this case new or have we worked it
before?" without having to dig through Apify logs or DataSift activity.

Usage (called from main.py at the end of each daily run):

    from master_ledger import update_ledger_csvs
    update_ledger_csvs(
        records=notices,
        run_stats=notice_state_stats,
        run_meta={
            "run_date": date.today().isoformat(),
            "build_version": "1.0.5",
            "run_duration_min": elapsed_min,
            "cost": total_cost,
            "slack_fired": slack_ok,
        },
        drive_folder_id=config.GOOGLE_DRIVE_FOLDER_ID,
        service_account_key_b64=config.GOOGLE_SERVICE_ACCOUNT_KEY,
    )

If GOOGLE_DRIVE_FOLDER_ID or service account key is empty, this is a no-op
(logs a warning and returns gracefully — pipeline doesn't fail).
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path

from drive_uploader import upsert_file
from models import NoticeData
from notice_state import load_state

logger = logging.getLogger(__name__)


# ── Output filenames (stable, file IDs persist across upsert) ─────────

ACTIVE_RECORDS_FILENAME = "WHO_master_ledger_active.csv"
DAILY_SUMMARY_FILENAME = "WHO_master_ledger_daily_summary.csv"
AGED_OUT_FILENAME = "WHO_master_ledger_aged_out.csv"


# ── Active records sheet — columns ─────────────────────────────────────

ACTIVE_COLUMNS = [
    "canonical_key",
    "county",
    "notice_type",
    "case_number",
    "parcel_id",
    "property_address",
    "property_city",
    "property_zip",
    "owner_name",
    "decedent_name",
    "first_scraped",
    "last_scraped",
    "scrape_count",
    "redemption_window_status",
    "redemption_days_remaining",
    "auction_date",
    "sheriff_sale_held_date",
    "confirmation_hearing_date",
    "owner_deceased",
    "date_of_death",
    "decision_maker_name",
    "decision_maker_relationship",
    "dm_confidence",
    "estimated_value",
    "equity_percent",
    "primary_phone",
    "mailable",
    "obituary_url",
    "source_url",
    "last_run_id",
]


# ── Daily summary sheet — columns ──────────────────────────────────────

DAILY_SUMMARY_COLUMNS = [
    "run_date",
    "build_version",
    "today_scraped",
    "new_records",
    "updated_records",
    "carried_forward",
    "aged_out",
    "total_active",
    "no_key",
    "probate_count",
    "foreclosure_count",
    "lis_pendens_count",
    "tax_sale_count",
    "franklin_count",
    "montgomery_count",
    "greene_count",
    "redemption_open",
    "redemption_closing",
    "owner_deceased_count",
    "uploaded_to_datasift",
    "skipped_unchanged",
    "run_duration_min",
    "estimated_cost_usd",
    "slack_fired",
]


# ── Public entry point ─────────────────────────────────────────────────

def update_ledger_csvs(
    records: list[NoticeData],
    run_stats: dict | None,
    run_meta: dict,
    drive_folder_id: str,
    service_account_key_b64: str,
    upload_count: int = 0,
    skip_count: int = 0,
) -> dict[str, str]:
    """Generate the 3 ledger CSVs, write them locally, upsert to Drive.

    Returns dict mapping filename → Drive webViewLink (empty string if a
    particular file failed to upload).
    """
    if not drive_folder_id or not service_account_key_b64:
        logger.warning("Master ledger: GOOGLE_DRIVE_FOLDER_ID or service account key missing — skipping")
        return {}

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    links: dict[str, str] = {}

    # ── 1. Active records CSV ─────────────────────────────────────────
    try:
        active_path = output_dir / ACTIVE_RECORDS_FILENAME
        _write_active_records_csv(active_path, run_meta.get("run_id", ""))
        link = upsert_file(
            active_path, drive_folder_id, service_account_key_b64,
            filename=ACTIVE_RECORDS_FILENAME, mimetype="text/csv",
        )
        if link:
            links[ACTIVE_RECORDS_FILENAME] = link
    except Exception as e:
        logger.warning("Active records CSV failed: %s", e)

    # ── 2. Daily summary CSV (append-only) ────────────────────────────
    try:
        summary_path = output_dir / DAILY_SUMMARY_FILENAME
        _append_daily_summary(
            summary_path, records, run_stats, run_meta,
            upload_count=upload_count, skip_count=skip_count,
        )
        link = upsert_file(
            summary_path, drive_folder_id, service_account_key_b64,
            filename=DAILY_SUMMARY_FILENAME, mimetype="text/csv",
        )
        if link:
            links[DAILY_SUMMARY_FILENAME] = link
    except Exception as e:
        logger.warning("Daily summary CSV failed: %s", e)

    # ── 3. Aged-out archive CSV (append-only) ─────────────────────────
    # We don't have direct access to records aged out in this run from
    # state alone — notice_state.merge_with_today already pruned them.
    # The aged_out count from run_stats is informational only here; the
    # archive entry records the COUNT pruned today, not the rows themselves.
    # (Capturing the actual rows would require notice_state to expose
    # pruned entries — future enhancement.)
    try:
        aged_count = run_stats.get("aged_out", 0) if run_stats else 0
        if aged_count > 0:
            aged_path = output_dir / AGED_OUT_FILENAME
            _append_aged_out_count(aged_path, run_meta, aged_count)
            link = upsert_file(
                aged_path, drive_folder_id, service_account_key_b64,
                filename=AGED_OUT_FILENAME, mimetype="text/csv",
            )
            if link:
                links[AGED_OUT_FILENAME] = link
    except Exception as e:
        logger.warning("Aged-out archive CSV failed: %s", e)

    logger.info("Master ledger updated: %d files uploaded to Drive", len(links))
    return links


# ── Internal writers ───────────────────────────────────────────────────

def _write_active_records_csv(path: Path, run_id: str) -> None:
    """Refresh the active records CSV from the current notice_state."""
    state = load_state()
    rows = []
    for entry in state.values():
        record_dict = entry.get("record") or {}
        rows.append({
            "canonical_key": entry.get("key", ""),
            "county": entry.get("county", ""),
            "notice_type": entry.get("notice_type", ""),
            "case_number": record_dict.get("case_number", ""),
            "parcel_id": record_dict.get("parcel_id", ""),
            "property_address": record_dict.get("address", ""),
            "property_city": record_dict.get("city", ""),
            "property_zip": record_dict.get("zip", ""),
            "owner_name": record_dict.get("owner_name", ""),
            "decedent_name": record_dict.get("decedent_name", ""),
            "first_scraped": entry.get("first_scraped", ""),
            "last_scraped": entry.get("last_scraped", ""),
            "scrape_count": entry.get("scrape_count", 1),
            "redemption_window_status": record_dict.get("redemption_window_status", ""),
            "redemption_days_remaining": record_dict.get("redemption_window_days_remaining", ""),
            "auction_date": record_dict.get("auction_date", ""),
            "sheriff_sale_held_date": record_dict.get("sheriff_sale_held_date", ""),
            "confirmation_hearing_date": record_dict.get("confirmation_hearing_date", ""),
            "owner_deceased": record_dict.get("owner_deceased", ""),
            "date_of_death": record_dict.get("date_of_death", ""),
            "decision_maker_name": record_dict.get("decision_maker_name", ""),
            "decision_maker_relationship": record_dict.get("decision_maker_relationship", ""),
            "dm_confidence": record_dict.get("dm_confidence", ""),
            "estimated_value": record_dict.get("estimated_value", ""),
            "equity_percent": record_dict.get("equity_percent", ""),
            "primary_phone": record_dict.get("primary_phone", ""),
            "mailable": record_dict.get("mailable", ""),
            "obituary_url": record_dict.get("obituary_url", ""),
            "source_url": record_dict.get("source_url", ""),
            "last_run_id": record_dict.get("run_id", "") or run_id,
        })

    # Sort: most recently scraped first
    rows.sort(key=lambda r: r.get("last_scraped", ""), reverse=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ACTIVE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Active records CSV: %d rows → %s", len(rows), path)


def _append_daily_summary(
    path: Path,
    records: list[NoticeData],
    run_stats: dict | None,
    run_meta: dict,
    upload_count: int,
    skip_count: int,
) -> None:
    """Append one row per daily run."""
    by_type: dict[str, int] = {}
    by_county: dict[str, int] = {}
    redemption_open = 0
    redemption_closing = 0
    deceased_count = 0
    for r in records:
        by_type[r.notice_type] = by_type.get(r.notice_type, 0) + 1
        by_county[r.county] = by_county.get(r.county, 0) + 1
        if r.redemption_window_status == "open":
            redemption_open += 1
        elif r.redemption_window_status == "closing":
            redemption_closing += 1
        if r.owner_deceased == "yes":
            deceased_count += 1

    stats = run_stats or {}
    row = {
        "run_date": run_meta.get("run_date", date.today().isoformat()),
        "build_version": run_meta.get("build_version", ""),
        "today_scraped": stats.get("today_scraped", ""),
        "new_records": stats.get("new", ""),
        "updated_records": stats.get("updated", ""),
        "carried_forward": stats.get("carried_forward", ""),
        "aged_out": stats.get("aged_out", ""),
        "total_active": stats.get("total_active", len(records)),
        "no_key": stats.get("no_key", ""),
        "probate_count": by_type.get("probate", 0),
        "foreclosure_count": by_type.get("foreclosure", 0),
        "lis_pendens_count": by_type.get("lis_pendens", 0),
        "tax_sale_count": by_type.get("tax_sale", 0),
        "franklin_count": by_county.get("Franklin", 0),
        "montgomery_count": by_county.get("Montgomery", 0),
        "greene_count": by_county.get("Greene", 0),
        "redemption_open": redemption_open,
        "redemption_closing": redemption_closing,
        "owner_deceased_count": deceased_count,
        "uploaded_to_datasift": upload_count,
        "skipped_unchanged": skip_count,
        "run_duration_min": run_meta.get("run_duration_min", ""),
        "estimated_cost_usd": run_meta.get("cost", ""),
        "slack_fired": "yes" if run_meta.get("slack_fired") else "no",
    }

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_SUMMARY_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    logger.info("Daily summary appended → %s", path)


def _append_aged_out_count(path: Path, run_meta: dict, aged_count: int) -> None:
    """Append a single row noting how many records aged out today."""
    row = {
        "run_date": run_meta.get("run_date", date.today().isoformat()),
        "aged_out_count": aged_count,
        "build_version": run_meta.get("build_version", ""),
    }
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["run_date", "aged_out_count", "build_version"])
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    logger.info("Aged-out summary appended (%d records) → %s", aged_count, path)
