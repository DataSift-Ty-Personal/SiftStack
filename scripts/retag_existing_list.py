"""Re-run tagger v3 against records already uploaded to DataSift.

Use case: auto-upload succeeded but tagger v3 failed (selectors broke).
Records are in DataSift in a wrapper list; we just need to apply the
per-(notice_type, county) tags + add to notice-type lists.

Pulls notices from the source CSV (so we know which (type, county)
groups to iterate), but does NOT re-upload — only runs the tagger
against the existing wrapper list.

Usage:
    PYTHONPATH=src python scripts/retag_existing_list.py \
        --wrapper "SiftStack 2026-05-05 - DMs" \
        --csv output/backfill/datasift_dms_2026-05-05.csv
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
    from models import NoticeData
    notices = []
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = NoticeData(
                address=(row.get("Property Street Address") or "").strip(),
                city=(row.get("Property City") or "").strip(),
                state=(row.get("Property State") or "").strip(),
                zip=(row.get("Property ZIP Code") or "").strip(),
                county=(row.get("County") or "").strip(),
                notice_type=(row.get("Notice Type") or "").strip(),
                date_added=(row.get("Date Added") or "").strip(),
                owner_deceased=(row.get("Owner Deceased") or "").strip(),
            )
            notices.append(n)
    return notices


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wrapper", required=True, help="DataSift wrapper list name (e.g. 'SiftStack 2026-05-05 - DMs')")
    parser.add_argument("--csv", required=True, action="append", help="Source CSV (repeatable)")
    args = parser.parse_args()

    if not (os.environ.get("DATASIFT_EMAIL") and os.environ.get("DATASIFT_PASSWORD")):
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set in .env")
        return 1

    notices = []
    for csv_path_str in args.csv:
        csv_path = Path(csv_path_str)
        if not csv_path.exists():
            logger.error("CSV not found: %s", csv_path)
            return 1
        loaded = _csv_to_notices(csv_path)
        notices.extend(loaded)
        logger.info("Loaded %d notices from %s", len(loaded), csv_path.name)

    logger.info("Running tagger v3 against wrapper list: %s (%d notices)", args.wrapper, len(notices))

    from playwright.async_api import async_playwright
    from datasift_core import login
    from datasift_post_upload_tagger import apply_tags_and_lists_to_uploaded_records
    from slack_notifier import notify_tagger_result

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        await login(page)
        logger.info("Logged in to DataSift")

        result = await apply_tags_and_lists_to_uploaded_records(
            page, args.wrapper, notices,
        )
        logger.info("Tagger result: %s", result.get("message", ""))

        try:
            notify_tagger_result(result)
            logger.info("Sent verification ping to Slack")
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)

        await browser.close()

    failed = [g for g in result.get("groups", []) if g.get("error") or not g.get("verified")]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
