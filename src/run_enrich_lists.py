"""Run DataSift's property enrichment over one or more lists.

The API has no documented enrichment route. `/api/internal/property/enrich/`
exists but answers an empty POST with a count of EVERY property in the account
(50,770), and its trigger contract is unknown. Guessing at it risks running
owner enrichment account-wide, which would replace the personal representative
on every probate record with the deceased owner of record and undo the whole
point of the PR contact mapping. So this uses the proven browser path, which
sets the three toggles explicitly and visibly.

    python src/run_enrich_lists.py --lists Probate --headed     # watch it
    python src/run_enrich_lists.py --lists Probate,Foreclosure

TOGGLES, and the reason they matter:
    Enrich Property Information   ON    beds, baths, AVM, sqft, sale history
    Enrich Owners                 OFF   would overwrite our PR contact
    Swap Owners                   OFF   would swap the PR for the dead owner

`enrich_records` already enforces exactly that; this script only drives login
and iterates the lists.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_enrich_lists")


async def main_async(lists: list[str], headed: bool) -> int:
    from datasift_core import create_browser, load_cookies, login, save_cookies
    from datasift_uploader import enrich_records

    if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set")
        return 2

    async with create_browser(headless=not headed) as (browser, context, page):
        await load_cookies(context)
        if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
            logger.error("DataSift login failed")
            return 2
        await save_cookies(page)

        failures = 0
        for name in lists:
            logger.info("=== enriching list: %s ===", name)
            try:
                res = await enrich_records(page, name)
            except Exception as exc:
                logger.exception("Enrichment crashed for %s", name)
                res = {"success": False, "message": f"{type(exc).__name__}: {exc}"}
            status = "OK" if res.get("success") else "FAILED"
            logger.info("[%s] %s: %s", status, name, res.get("message", ""))
            if not res.get("success"):
                failures += 1

        return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lists", default="Probate,Foreclosure",
                    help="Comma-separated DataSift list names")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser. Worth doing the first time: the "
                         "enrichment modal is the one place a wrong toggle is "
                         "expensive, and this is where you see it.")
    a = ap.parse_args()
    names = [x.strip() for x in a.lists.split(",") if x.strip()]
    if not names:
        print("No lists given.")
        return 2
    return asyncio.run(main_async(names, a.headed))


if __name__ == "__main__":
    raise SystemExit(main())
