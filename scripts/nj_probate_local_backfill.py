"""Local Middlesex + Somerset + Ocean probate backfill.

Use when Modal's egress IPs are CF-blocked from the Bluestone portals.
Scrapes all three counties from the laptop (clean IP), combines, enriches,
writes a single CSV, optionally uploads to DataSift + Slack.

Run with:
  PYTHONPATH=src python scripts/nj_probate_local_backfill.py \
      --mx-days-back 180 --som-days-back 30 --ocean-days-back 180 \
      --upload-datasift --notify-slack
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def _scrape_all(
    mx_days_back: int, som_days_back: int, ocean_days_back: int, headless: bool,
):
    """Scrape Middlesex (DoD) + Somerset (File-Date) + Ocean (DoD) sequentially.

    Sequential rather than parallel to avoid Playwright contention in a
    single Python process — the DoD scans dominate wall-clock anyway, so
    parallelism saves <10%. Ocean is the same Bluestone deployment as
    Middlesex (Death-Date filter), so it uses a DoD scan too.
    """
    from nj_middlesex_probate import (
        scrape_middlesex_probates,
        scrape_somerset_probates,
        scrape_ocean_probates,
    )

    logger = logging.getLogger("nj_probate_backfill")

    logger.info("Middlesex: %d days DoD scan", mx_days_back)
    mx = await scrape_middlesex_probates(days_back=mx_days_back, headless=headless)
    logger.info("Middlesex: %d notices", len(mx))

    logger.info("Somerset: %d days file-date scan", som_days_back)
    som = await scrape_somerset_probates(days_back=som_days_back, headless=headless)
    logger.info("Somerset: %d notices", len(som))

    logger.info("Ocean: %d days DoD scan", ocean_days_back)
    ocean = await scrape_ocean_probates(days_back=ocean_days_back, headless=headless)
    logger.info("Ocean: %d notices", len(ocean))

    return mx, som, ocean


def main() -> int:
    p = argparse.ArgumentParser(description="Local Middlesex + Somerset probate backfill")
    p.add_argument("--mx-days-back", type=int, default=180,
                   help="Middlesex DoD window (default 180)")
    p.add_argument("--som-days-back", type=int, default=30,
                   help="Somerset file-date window (default 30)")
    p.add_argument("--ocean-days-back", type=int, default=180,
                   help="Ocean DoD window (default 180)")
    p.add_argument("--headed", action="store_true", help="Show browser windows")
    p.add_argument("--upload-datasift", action="store_true",
                   help="Upload combined CSV to DataSift after enrichment")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post run summary to Slack webhook")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("nj_probate_backfill")

    mx, som, ocean = asyncio.run(_scrape_all(
        mx_days_back=args.mx_days_back,
        som_days_back=args.som_days_back,
        ocean_days_back=args.ocean_days_back,
        headless=not args.headed,
    ))
    combined = mx + som + ocean
    if not combined:
        logger.error("All scrapers returned 0 records — nothing to enrich")
        return 1

    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=False,
        skip_dm_address=False,
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label=f"NJ Probate Backfill (Middlesex {args.mx_days_back}d + Somerset {args.som_days_back}d)",
    )
    enriched = run_enrichment_pipeline(combined, opts)

    from data_formatter import write_csv, write_csv_by_list
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = write_csv(enriched, f"nj_probate_backfill_{ts}.csv")
    logger.info("Combined CSV: %s (%d records)", csv_path, len(enriched))

    import config
    paused = config.SIFTSTACK_UPLOAD_PAUSED_TYPES
    upload_ready = [n for n in enriched if (n.notice_type or "").lower() not in paused]
    held_back = [n for n in enriched if (n.notice_type or "").lower() in paused]
    if held_back:
        held_csv = write_csv(held_back, f"nj_probate_backfill_{ts}_HELD_FOR_CLEANING.csv")
        logger.info("Held for cleaning: %d records (probate paused) -> %s",
                    len(held_back), held_csv)
    by_list = write_csv_by_list(enriched, prefix="probate_backfill") if enriched else []
    for list_name, path, count in by_list:
        logger.info("Per-list CSV: %s (%d) -> %s", list_name, count, path)

    if args.upload_datasift and upload_ready:
        from datasift_uploader import upload_to_datasift
        from datasift_formatter import write_datasift_split_csvs
        csv_infos = write_datasift_split_csvs(upload_ready, list_name="")
        for info in csv_infos:
            logger.info("DataSift uploading %s ...", info["path"].name)
            asyncio.run(upload_to_datasift(
                info["path"], enrich=True, skip_trace=True,
            ))

    if args.notify_slack and config.SLACK_WEBHOOK_URL:
        from slack_notifier import _send_webhook
        lines = [
            f"*NJ Probate Local Backfill — Middlesex {args.mx_days_back}d + Somerset {args.som_days_back}d + Ocean {args.ocean_days_back}d*",
            f"  Middlesex: {len(mx)} notices",
            f"  Somerset: {len(som)} notices",
            f"  Ocean: {len(ocean)} notices",
            f"Enriched total: {len(enriched)}",
            f"CSV: {csv_path.name}",
        ]
        if held_back:
            lines.append(f":pause_button: Held for cleaning: {len(held_back)} (probate paused)")
        _send_webhook("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(main())
