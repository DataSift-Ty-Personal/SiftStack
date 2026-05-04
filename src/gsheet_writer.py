"""Append SiftStack daily summary rows to a Google Sheet via Apps Script webhook.

The webhook is deployed from `scripts/gsheet_apps_script.js` (paste-into-Sheet,
deploy as web app). The deploy URL goes into `.env` as `GSHEET_WEBHOOK_URL`.

This module is called at the end of each daily run alongside Slack notify.
Failures are non-fatal — we log a warning and continue. The CSV at
`output/daily_summary.csv` is the canonical local record; the Sheet is
just a convenience surface for at-a-glance browsing.

Why Apps Script vs Google service account:
  - 5-min setup (paste + deploy) vs 30-min service-account ritual
  - No JSON keys to manage, rotate, or leak
  - Daily-once volume is way under Apps Script rate limits
  - Sheet ownership stays clean — Aaron's account owns it, no service-
    account email cluttering the share list
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


def append_summary_to_gsheet(summary: dict[str, Any]) -> bool:
    """POST one summary row to the Google Sheet webhook.

    Returns True on success, False on any failure (logged as warning).
    Never raises — the daily run must not abort because the Sheet append failed.
    """
    url = (config.GSHEET_WEBHOOK_URL or "").strip()
    if not url:
        logger.info("GSHEET_WEBHOOK_URL not set — skipping Google Sheet append")
        return False

    try:
        resp = requests.post(
            url,
            data=json.dumps(summary),
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
            if body.get("status") == "ok":
                logger.info("Google Sheet append OK — row %s, %s",
                            body.get("row", "?"), body.get("appended", ""))
                return True
            logger.warning("Google Sheet returned non-ok status: %s", body)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Google Sheet response not JSON: %s", resp.text[:200])
    except requests.exceptions.Timeout:
        logger.warning("Google Sheet append timed out after %ds", REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.warning("Google Sheet append failed: %s", e)
    except Exception as e:
        logger.warning("Google Sheet append unexpected error: %s", e)

    return False


# ── Master Ledger (per-record rolling log) ─────────────────────────────


# Columns Mike + Aaron see in the Master Ledger tab. Order matters — first
# row of the Sheet is set from the first record we ever push, then locked.
LEDGER_COLUMNS = [
    "run_date",
    "date_added",
    "notice_type",
    "county",
    "address",
    "city",
    "state",
    "zip",
    "owner_name",
    "owner_deceased",
    "decedent_name",
    "date_of_death",
    "decision_maker",
    "dm_relationship",
    "dm_confidence",
    "has_heirs",
    "primary_phone",
    "phone_tier",
    "email_1",
    "estimated_value",
    "equity_percent",
    "mls_status",
    "auction_date",
    "redemption_status",
    "tax_delinquent_amount",
    "source_url",
    "obituary_url",
    "dedup_status",
    "datasift_uploaded",
    "datasift_tagged",
]


def _record_to_row(notice, run_date: str, dedup_status: str = "new") -> dict:
    """Flatten a NoticeData record to a Sheet row dict matching LEDGER_COLUMNS."""
    has_heirs = "yes" if (getattr(notice, "heir_map_json", "") or "").strip() else "no"
    return {
        "run_date": run_date,
        "date_added": getattr(notice, "date_added", "") or "",
        "notice_type": getattr(notice, "notice_type", "") or "",
        "county": getattr(notice, "county", "") or "",
        "address": getattr(notice, "address", "") or "",
        "city": getattr(notice, "city", "") or "",
        "state": getattr(notice, "state", "") or "",
        "zip": getattr(notice, "zip", "") or "",
        "owner_name": getattr(notice, "owner_name", "") or "",
        "owner_deceased": getattr(notice, "owner_deceased", "") or "",
        "decedent_name": getattr(notice, "decedent_name", "") or "",
        "date_of_death": getattr(notice, "date_of_death", "") or "",
        "decision_maker": getattr(notice, "decision_maker_name", "") or "",
        "dm_relationship": getattr(notice, "decision_maker_relationship", "") or "",
        "dm_confidence": getattr(notice, "dm_confidence", "") or "",
        "has_heirs": has_heirs,
        "primary_phone": getattr(notice, "primary_phone", "") or "",
        "phone_tier": getattr(notice, "primary_phone_tier", "") or "",
        "email_1": getattr(notice, "email_1", "") or "",
        "estimated_value": getattr(notice, "estimated_value", "") or "",
        "equity_percent": getattr(notice, "equity_percent", "") or "",
        "mls_status": getattr(notice, "mls_status", "") or "",
        "auction_date": getattr(notice, "auction_date", "") or "",
        "redemption_status": getattr(notice, "redemption_window_status", "") or "",
        "tax_delinquent_amount": getattr(notice, "tax_delinquent_amount", "") or "",
        "source_url": getattr(notice, "source_url", "") or "",
        "obituary_url": getattr(notice, "obituary_url", "") or "",
        "dedup_status": dedup_status,
        "datasift_uploaded": "",
        "datasift_tagged": "",
    }


def append_records_to_gsheet(
    notices,
    run_date: str,
    dedup_status_map: dict | None = None,
    upload_status: str = "",
    tag_status: str = "",
    batch_size: int = 200,
) -> int:
    """POST notices to the Master Ledger tab in chunks (one row per record).

    The Apps Script handler routes type='records' payloads to the Master
    Ledger tab and bulk-appends them. Failures are logged + non-fatal.

    Args:
        notices: iterable of NoticeData
        run_date: ISO date string (YYYY-MM-DD) — when this run executed
        dedup_status_map: optional {normalized_address → "new"|"duplicate"|"carried"}
        upload_status: "yes"/"no"/"" — same value for every record this run
        tag_status: "yes"/"no"/"partial"/"" — same value for every record this run
        batch_size: how many records per POST (Apps Script time limit ~6 min)

    Returns:
        Number of records successfully appended (sum across batches).
    """
    url = (config.GSHEET_WEBHOOK_URL or "").strip()
    if not url:
        logger.info("GSHEET_WEBHOOK_URL not set — skipping Master Ledger append")
        return 0

    notices_list = list(notices)
    if not notices_list:
        return 0

    rows = []
    for n in notices_list:
        row = _record_to_row(n, run_date)
        if dedup_status_map:
            try:
                from datasift_dedup import normalize_address
                key = normalize_address(n)
                if key and key in dedup_status_map:
                    row["dedup_status"] = dedup_status_map[key]
            except Exception:
                pass
        if upload_status:
            row["datasift_uploaded"] = upload_status
        if tag_status:
            row["datasift_tagged"] = tag_status
        rows.append(row)

    appended = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            resp = requests.post(
                url,
                data=json.dumps({"type": "records", "records": batch}),
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT * 2,
                allow_redirects=True,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") == "ok":
                count = body.get("appended_rows", len(batch))
                appended += count
                logger.info("Master Ledger: +%d rows (total: %s)",
                            count, body.get("total_rows", "?"))
            else:
                logger.warning("Master Ledger batch error: %s", body)
        except Exception as e:
            logger.warning("Master Ledger batch %d-%d failed: %s",
                           i, i + len(batch), e)

    return appended
