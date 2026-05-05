"""One-shot — push pre-built DataSift CSVs through the upload + tagger flow.

Use case: backfilling DataSift when Apify generated CSVs but didn't auto-upload
them (build < 1.0.10), or recovering from a failed daily run.

The CSVs must already match the DataSift schema (use write_datasift_split_csvs
to generate them, or pull from an Apify run's KVS).

Usage:
    PYTHONPATH=src python scripts/push_csvs_to_datasift.py \
        output/backfill/datasift_dms_2026-05-04.csv \
        output/backfill/datasift_heirs_2026-05-04.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load .env so DATASIFT_EMAIL / DATASIFT_PASSWORD are picked up
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k and not os.environ.get(k):
            os.environ[k] = v

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _csv_to_notices(csv_path: Path):
    """Reconstruct minimal NoticeData stubs from a DataSift CSV.

    Only fields the post-upload tagger v3 needs (notice_type, county) +
    a few for context. The actual record content is already in the CSV
    that DataSift will ingest.
    """
    from models import NoticeData
    notices = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n = NoticeData(
                address=(row.get("Property Street Address") or "").strip(),
                city=(row.get("Property City") or "").strip(),
                state=(row.get("Property State") or "").strip(),
                zip=(row.get("Property ZIP Code") or "").strip(),
                county=(row.get("County") or "").strip(),
                notice_type=(row.get("Notice Type") or "").strip(),
                date_added=(row.get("Date Added") or "").strip(),
                owner_name=f"{(row.get('Owner First Name') or '').strip()} {(row.get('Owner Last Name') or '').strip()}".strip(),
                owner_deceased=(row.get("Owner Deceased") or "").strip(),
            )
            notices.append(n)
    return notices


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_paths", nargs="+", help="One or more DataSift-formatted CSVs to upload")
    parser.add_argument("--no-enrich", action="store_true", help="Skip DataSift enrichment after upload")
    parser.add_argument("--no-skip-trace", action="store_true", help="Skip DataSift skip trace after upload")
    args = parser.parse_args()

    if not (os.environ.get("DATASIFT_EMAIL") and os.environ.get("DATASIFT_PASSWORD")):
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set in .env")
        return 1

    from datasift_uploader import upload_datasift_split, upload_to_datasift
    from slack_notifier import notify_tagger_result

    csv_infos = []
    all_notices = []
    for path_str in args.csv_paths:
        path = Path(path_str)
        if not path.exists():
            logger.error("CSV not found: %s", path)
            return 1
        notices = _csv_to_notices(path)
        all_notices.extend(notices)
        # Label inferred from filename: dms / heirs
        label = "DMs" if "dms" in path.name.lower() else "Heirs" if "heirs" in path.name.lower() else "Records"
        csv_infos.append({
            "path": path,
            "label": label,
            "list_name": f"SiftStack {path.name.split('_')[-1].replace('.csv','')} - {label}",
            "count": len(notices),
        })
        logger.info("Queued %s — %d records (label=%s)", path.name, len(notices), label)

    logger.info("Total: %d records across %d CSVs", len(all_notices), len(csv_infos))
    logger.info("Starting DataSift upload + post-upload tagger v3...")

    if len(csv_infos) > 1:
        result = await upload_datasift_split(
            csv_infos,
            enrich=not args.no_enrich,
            skip_trace=not args.no_skip_trace,
            notices=all_notices,
        )
    else:
        result = await upload_to_datasift(
            csv_infos[0]["path"],
            enrich=not args.no_enrich,
            skip_trace=not args.no_skip_trace,
            notices=all_notices,
        )

    if result.get("success"):
        logger.info("✓ Upload OK: %s", result.get("message", ""))
    else:
        logger.error("✗ Upload FAILED: %s", result.get("message", ""))

    if result.get("tag_result"):
        try:
            notify_tagger_result(result["tag_result"])
            logger.info("Sent per-bucket tagger verification to Slack")
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
