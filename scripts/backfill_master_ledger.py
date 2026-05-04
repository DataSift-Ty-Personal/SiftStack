"""One-time backfill — push records from recent Apify runs into Master Ledger.

Pulls the DataSift-formatted CSVs that each Apify run saved to its
Key-Value Store, transforms each row to the Master Ledger schema, and
POSTs to the GSHEET_WEBHOOK_URL endpoint in batches.

The Apps Script's upsert logic (match by address+city) means re-running
this script is safe — duplicate addresses update in place vs append.

Usage:
    PYTHONPATH=src python scripts/backfill_master_ledger.py \
        --runs <runId1>,<runId2>,<runId3>
    # Or default — uses last 3 succeeded scheduler runs
    PYTHONPATH=src python scripts/backfill_master_ledger.py --auto
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
ACTOR_ID = "4YUHiatee35sIhQb2"  # SiftStack Actor ID

# Read from .env if env var unset
if not APIFY_TOKEN:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("APIFY_TOKEN="):
                APIFY_TOKEN = line.split("=", 1)[1].strip()
                break

if not APIFY_TOKEN:
    # Fall back to apify CLI token cache
    cli_auth = Path.home() / ".apify" / "auth.json"
    if cli_auth.exists():
        try:
            APIFY_TOKEN = json.loads(cli_auth.read_text()).get("token", "")
        except Exception:
            pass


def _get_webhook_url() -> str:
    env_path = ROOT / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("GSHEET_WEBHOOK_URL="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("GSHEET_WEBHOOK_URL not in .env")


def _apify_get(path: str, **params) -> dict:
    r = requests.get(
        f"https://api.apify.com{path}",
        params={"token": APIFY_TOKEN, **params},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def _list_recent_runs(n: int = 3) -> list[str]:
    runs = _apify_get(f"/v2/acts/{ACTOR_ID}/runs", desc=1, limit=n + 5).get("items", [])
    return [r["id"] for r in runs if r.get("status") == "SUCCEEDED"][:n]


def _fetch_run_csvs(run_id: str) -> tuple[str, list[tuple[str, str]]]:
    """Returns (run_date_iso, [(label, csv_text), ...])."""
    run = _apify_get(f"/v2/actor-runs/{run_id}")
    run_date = (run.get("finishedAt") or run.get("startedAt") or "")[:10]
    kvs_id = run.get("defaultKeyValueStoreId")
    if not kvs_id:
        return run_date, []

    keys = _apify_get(f"/v2/key-value-stores/{kvs_id}/keys").get("items", [])
    out = []
    for key_entry in keys:
        key = key_entry["key"]
        if not (key.startswith("datasift_") and key.endswith(".csv")):
            continue
        r = requests.get(
            f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}",
            params={"token": APIFY_TOKEN},
            timeout=60,
        )
        if r.ok:
            label = key.replace("datasift_", "").replace(".csv", "")
            out.append((label, r.text))
    return run_date, out


def _ds_row_to_ledger(row: dict, run_date: str) -> dict:
    """DataSift CSV row → Master Ledger row dict."""
    first = (row.get("Owner First Name") or "").strip()
    last = (row.get("Owner Last Name") or "").strip()
    owner_name = f"{first} {last}".strip()

    # auction_date can land in any of three columns depending on notice type
    auction = (
        row.get("Foreclosure Date")
        or row.get("Tax Auction Date")
        or row.get("Probate Open Date")
        or ""
    ).strip()

    return {
        "run_date": run_date,
        "date_added": (row.get("Date Added") or "").strip(),
        "notice_type": (row.get("Notice Type") or "").strip(),
        "county": (row.get("County") or "").strip(),
        "address": (row.get("Property Street Address") or "").strip(),
        "city": (row.get("Property City") or "").strip(),
        "state": (row.get("Property State") or "").strip(),
        "zip": (row.get("Property ZIP Code") or "").strip(),
        "owner_name": owner_name,
        "owner_deceased": (row.get("Owner Deceased") or "").strip(),
        "decedent_name": (row.get("Decedent Name") or "").strip(),
        "date_of_death": (row.get("Date of Death") or "").strip(),
        "decision_maker": (row.get("Decision Maker") or row.get("Personal Representative") or "").strip(),
        "dm_relationship": (row.get("DM Relationship") or "").strip(),
        "dm_confidence": (row.get("DM Confidence") or "").strip(),
        "has_heirs": "yes" if (row.get("DM 2 Name") or "").strip() else "no",
        "primary_phone": (row.get("Phone 1") or "").strip(),
        "phone_tier": "",  # not in DataSift CSV
        "email_1": (row.get("Email 1") or "").strip(),
        "estimated_value": (row.get("Estimated Value") or "").strip(),
        "equity_percent": (row.get("Equity Percentage") or "").strip(),
        "mls_status": (row.get("MSL Status") or "").strip(),
        "auction_date": auction,
        "redemption_status": "",
        "tax_delinquent_amount": (row.get("Tax Deliquent Value") or "").strip(),
        "source_url": (row.get("Source URL") or "").strip(),
        "obituary_url": (row.get("Obituary URL") or "").strip(),
        "dedup_status": "backfill",
        "datasift_uploaded": "yes",
        "datasift_tagged": "unknown",
        "mike_status": "New",
        "last_touched": "",
        "mike_notes": "",
    }


def _post_batch(url: str, rows: list[dict]) -> dict:
    r = requests.post(
        url,
        data=json.dumps({"type": "records", "records": rows}),
        headers={"Content-Type": "application/json"},
        timeout=60,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", help="Comma-separated Apify run IDs")
    parser.add_argument("--auto", action="store_true", help="Use last 3 succeeded runs")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="Just print row counts, don't POST")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        logger.error("APIFY_TOKEN not set (env, .env, or ~/.apify/auth.json)")
        return 1

    if args.runs:
        run_ids = [r.strip() for r in args.runs.split(",") if r.strip()]
    elif args.auto:
        run_ids = _list_recent_runs(3)
    else:
        logger.error("Specify --runs <ids> or --auto")
        return 1

    logger.info("Backfilling %d runs: %s", len(run_ids), run_ids)

    webhook = _get_webhook_url()
    total_rows = 0
    total_runs = 0

    for run_id in run_ids:
        try:
            logger.info("── Run %s ──", run_id)
            run_date, csvs = _fetch_run_csvs(run_id)
            if not csvs:
                logger.warning("  No DataSift CSVs in KVS for run %s", run_id)
                continue
            run_rows = []
            for label, csv_text in csvs:
                reader = csv.DictReader(io.StringIO(csv_text))
                for row in reader:
                    ledger_row = _ds_row_to_ledger(row, run_date)
                    if ledger_row.get("address"):
                        run_rows.append(ledger_row)
                logger.info("  %s: %d rows", label, sum(1 for _ in csv.DictReader(io.StringIO(csv_text))))

            logger.info("  → %d total ledger rows from %s", len(run_rows), run_date)

            if args.dry_run:
                continue

            for i in range(0, len(run_rows), args.batch_size):
                batch = run_rows[i:i + args.batch_size]
                resp = _post_batch(webhook, batch)
                logger.info(
                    "  POST batch %d–%d → appended=%s updated=%s total=%s",
                    i, i + len(batch),
                    resp.get("appended_rows"), resp.get("updated_rows"),
                    resp.get("total_rows"),
                )
            total_rows += len(run_rows)
            total_runs += 1

        except Exception as e:
            logger.error("Run %s failed: %s", run_id, e)

    logger.info("Done — %d rows pushed across %d runs", total_rows, total_runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
