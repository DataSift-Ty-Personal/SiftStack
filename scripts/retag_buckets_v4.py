"""Retag-only — run tagger v4 against existing wrapper lists in DataSift.

Use case: bucketed CSVs already uploaded successfully but tagger failed
(selectors broke). Records are in DataSift in their per-bucket wrapper
lists. We just need to apply tags + add to notice-type lists.

Usage:
    PYTHONPATH=src python scripts/retag_buckets_v4.py \
        --bucket "SiftStack 2026-05-05 - foreclosure-franklin" foreclosure franklin 13 \
        --bucket "SiftStack 2026-05-05 - foreclosure-greene" foreclosure greene 9 \
        --bucket "SiftStack 2026-05-05 - foreclosure-montgomery" foreclosure montgomery 5 \
        --bucket "SiftStack 2026-05-05 - lis_pendens-franklin" lis_pendens franklin 31 \
        --bucket "SiftStack 2026-05-05 - probate-franklin" probate franklin 28
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bucket", action="append", nargs=4, metavar=("LIST_NAME", "NOTICE_TYPE", "COUNTY", "COUNT"),
        required=True,
        help="Bucket descriptor (repeatable): wrapper list name, notice_type, county, expected count",
    )
    args = parser.parse_args()

    if not (os.environ.get("DATASIFT_EMAIL") and os.environ.get("DATASIFT_PASSWORD")):
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set in .env")
        return 1

    buckets = []
    for entry in args.bucket:
        list_name, notice_type, county, count = entry
        buckets.append({
            "list_name": list_name,
            "notice_type": notice_type.lower(),
            "county": county.lower(),
            "count": int(count),
        })

    logger.info("Retag v4: %d buckets", len(buckets))
    for b in buckets:
        logger.info("  %s — %s/%s (%d records)", b["list_name"], b["notice_type"], b["county"], b["count"])

    from playwright.async_api import async_playwright
    from datasift_core import login
    from datasift_post_upload_tagger import apply_tags_to_buckets
    from slack_notifier import notify_tagger_result

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        await login(page)
        logger.info("Logged in to DataSift")

        result = await apply_tags_to_buckets(page, buckets)
        logger.info("Tagger result: %s", result.get("message", ""))

        try:
            notify_tagger_result(result)
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)

        await browser.close()

    logger.info("=== Per-bucket summary ===")
    for grp in result.get("groups", []):
        logger.info(
            "  %s: filtered=%s tags=%s list=%s verified=%s%s",
            grp.get("list_name"),
            grp.get("filtered_count"),
            grp.get("tags_added"),
            grp.get("list_added"),
            grp.get("verified"),
            f" error={grp.get('error')}" if grp.get("error") else "",
        )

    failed = [g for g in result.get("groups", []) if g.get("error") or not g.get("verified")]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
