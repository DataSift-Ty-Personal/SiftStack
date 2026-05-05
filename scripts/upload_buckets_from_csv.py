"""Split-upload validation — take an existing big CSV, split by (notice_type,
county), upload each bucket with its own wrapper list, and run v4 tagger.

Usage:
    PYTHONPATH=src python scripts/upload_buckets_from_csv.py \
        --csv output/backfill/datasift_dms_2026-05-05.csv \
        --csv output/backfill/datasift_heirs_2026-05-05.csv \
        --date 2026-05-05

Reads the input CSVs row-by-row, groups rows by (Notice Type, County),
writes one bucket CSV per group, then uploads each via upload_datasift_split.
The new tagger v4 filters only by the per-bucket wrapper list name.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

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


def split_csv_into_buckets(
    csv_paths: list[Path],
    date_str: str,
    output_dir: Path,
) -> list[dict]:
    """Read input CSVs, group rows by (Notice Type, County), write one CSV per bucket.

    Returns bucket descriptors: [{"path", "label", "list_name", "count",
    "notice_type", "county"}, ...]
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")

    # First pass: discover headers and group rows
    all_rows: list[dict] = []
    headers: list[str] = []
    for path in csv_paths:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not headers:
                headers = list(reader.fieldnames or [])
            for row in reader:
                all_rows.append(row)
        logger.info("Loaded %d rows from %s", sum(1 for _ in csv.DictReader(open(path))), path.name)

    if not all_rows:
        logger.error("No rows in input CSVs")
        return []

    # Group by (Notice Type, County)
    buckets: dict[tuple[str, str], list[dict]] = {}
    skipped = 0
    for row in all_rows:
        nt = (row.get("Notice Type") or "").strip().lower()
        cty = (row.get("County") or "").strip().lower()
        if not nt or not cty:
            skipped += 1
            continue
        buckets.setdefault((nt, cty), []).append(row)

    if skipped:
        logger.warning("Skipped %d rows missing Notice Type or County", skipped)

    # Write one CSV per bucket
    descriptors: list[dict] = []
    for (nt, cty), rows in sorted(buckets.items()):
        label = f"{nt}-{cty}"
        bucket_path = output_dir / f"datasift_{label}_{date_str}_{timestamp}.csv"
        with bucket_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        list_name = f"SiftStack {date_str} - {label}"
        descriptors.append({
            "path": bucket_path,
            "label": label,
            "list_name": list_name,
            "count": len(rows),
            "notice_type": nt,
            "county": cty,
        })
        logger.info("  Bucket %s: %d records → %s", label, len(rows), bucket_path.name)

    return descriptors


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", action="append", required=True, help="Input CSV (repeatable)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Date string for wrapper list names")
    parser.add_argument("--no-enrich", action="store_true")
    parser.add_argument("--no-skip-trace", action="store_true")
    parser.add_argument("--out-dir", default=str(ROOT / "output" / "backfill" / "buckets"))
    args = parser.parse_args()

    if not (os.environ.get("DATASIFT_EMAIL") and os.environ.get("DATASIFT_PASSWORD")):
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set in .env")
        return 1

    csv_paths = [Path(p) for p in args.csv]
    for p in csv_paths:
        if not p.exists():
            logger.error("CSV not found: %s", p)
            return 1

    logger.info("Split-upload validation: %d input CSV(s), date=%s", len(csv_paths), args.date)

    buckets = split_csv_into_buckets(csv_paths, args.date, Path(args.out_dir))
    if not buckets:
        return 1

    logger.info("Built %d buckets — starting upload + tagger v4", len(buckets))

    from datasift_uploader import upload_datasift_split
    from slack_notifier import notify_tagger_result

    result = await upload_datasift_split(
        buckets,
        enrich=not args.no_enrich,
        skip_trace=not args.no_skip_trace,
        notices=None,
    )

    if result.get("success"):
        logger.info("✓ All buckets uploaded: %s", result.get("message", ""))
    else:
        logger.error("✗ Upload chain failed: %s", result.get("message", ""))

    if result.get("tag_result"):
        try:
            notify_tagger_result(result["tag_result"])
            logger.info("Sent tagger v4 verification to Slack")
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)

    # Print per-bucket summary
    logger.info("=== Per-bucket summary ===")
    for upload in result.get("uploads", []):
        logger.info("  Upload %s: success=%s", upload.get("label"), upload.get("success"))
    for grp in (result.get("tag_result") or {}).get("groups", []):
        logger.info(
            "  Tag %s: filtered=%s tags=%s list=%s verified=%s%s",
            grp.get("list_name"),
            grp.get("filtered_count"),
            grp.get("tags_added"),
            grp.get("list_added"),
            grp.get("verified"),
            f" error={grp.get('error')}" if grp.get("error") else "",
        )

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
