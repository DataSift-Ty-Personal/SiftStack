"""Scrapfly-native tnpublicnotice scraper. No Playwright, no local browser.

Why this exists instead of patching scraper.py: every fetch goes through
Scrapfly's residential browser, so the machine running this needs no Chromium,
no Playwright, and no clean IP. That is what makes the runner portable enough to
move off Apify later.

The one non-obvious constraint drives the whole design: each Scrapfly call gets
a FRESH ASP.NET cookieless session (the /(S(sid))/ path changes per call), so a
detail page requested in a separate call lands in a session that never ran a
search and comes back as an unpopulated shell. Search and click therefore happen
in a single call, once per notice.

    python src/scrapfly_scrape.py --county Knox --days 7 --max 5
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import asdict
from datetime import date, timedelta

import config
from foreclosure_filter import is_valid_foreclosure
from notice_parser import parse_notice_html
from scrapfly_client import ScrapflyNoticeClient

logger = logging.getLogger(__name__)

# County checkbox ids on Search.aspx
COUNTY_CHECKBOX = {"Knox": 46, "Blount": 4}

SEL_KEYWORD = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_GO = "#ctl00_ContentPlaceHolder1_as1_btnGo"


def _county_script(county: str) -> str:
    idx = COUNTY_CHECKBOX.get(county)
    if idx is None:
        return "return 'no county filter';"
    return (
        f"var c=document.getElementById('ctl00_ContentPlaceHolder1_as1_lstCounty_{idx}');"
        "if(c){c.checked=true;}"
        "return c ? 'county set' : 'county checkbox missing';"
    )


def search_ids(client: ScrapflyNoticeClient, session: str, keyword: str,
               county: str) -> list[str]:
    """One call: run the search, return the notice IDs on the first results page."""
    scen = [
        {"wait_for_selector": {"selector": SEL_KEYWORD, "timeout": 8000}},
        {"fill": {"selector": SEL_KEYWORD, "value": keyword}},
        {"click": {"selector": SEL_GO}},
        {"wait": 8000},
    ]
    # The search is genuinely flaky: the results panel sometimes does not
    # populate within the scenario budget and comes back with the bare form.
    # Retry rather than treat an empty grid as "no notices".
    html = ""
    pairs = []
    for attempt in range(1, 4):
        resp, err = client._scrape(client._config(
            config.BASE_URL + "/Search.aspx", session,
            js_scenario=scen, rendering_wait=2000))
        if not resp:
            logger.warning("search attempt %d failed: %s", attempt, str(err)[:120])
            continue
        html = client._content(resp)
        pairs = re.findall(r"Details\.aspx\?SID=[a-z0-9]+&amp;ID=(\d+)", html)
        logger.info("search attempt %d: %d chars, %d result links",
                    attempt, len(html), len(pairs))
        if pairs:
            break
    if not pairs:
        logger.error("search returned no result links after 3 attempts "
                     "(last page %d chars) - treat as FAILURE, not an empty day", len(html))
        return []
    seen, out = set(), []
    for nid in pairs:
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


async def scrape(county: str, keyword: str, max_results: int,
                 session: str = "tnpn-native") -> list:
    client = ScrapflyNoticeClient()
    if not client.login(session):
        logger.error("Scrapfly login failed; aborting")
        return []

    ids = search_ids(client, session, keyword, county)
    logger.info("search returned %d notice ids for %s", len(ids), county)
    if not ids:
        return []

    notices, fetched, failed = [], 0, 0
    for i in range(min(max_results, len(ids))):
        res = client.fetch_notice_via_search(
            keyword, index=i, session=session, county=county, want_screenshot=True)
        if not res.ok:
            failed += 1
            logger.warning("  [%d] fetch failed: %s", i, res.error)
            continue
        fetched += 1
        notice = await parse_notice_html(
            res.content_html, county, "foreclosure",
            res.url or config.BASE_URL, config.ANTHROPIC_API_KEY,
            pdf_fetcher=lambda u: client.fetch_binary(u, session))
        kept = is_valid_foreclosure(notice)
        logger.info("  [%d] %s | addr=%r owner=%r auction=%r",
                    i, "KEEP" if kept else "drop",
                    notice.address, notice.owner_name, notice.auction_date)
        if kept:
            notices.append(notice)

    # A run that fetched nothing is a failure, not an empty day. This is the
    # exact condition that sat unreported for 19 days in July 2026.
    if fetched == 0:
        logger.error("STALE RUN: 0 of %d notice fetches succeeded (%d failed). "
                     "Check the gate type, residential proxy, and session walk.",
                     len(ids), failed)
    return notices


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", default="Knox", choices=sorted(COUNTY_CHECKBOX))
    ap.add_argument("--keyword", default="substitute trustee")
    ap.add_argument("--max", type=int, default=5)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s")
    for noisy in ("scrapfly", "urllib3", "anthropic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    notices = asyncio.run(scrape(a.county, a.keyword, a.max))
    print("\n%d notices kept" % len(notices))
    for n in notices:
        print("  %-34s %-26s %s" % (n.address or "(no address)",
                                    (n.owner_name or "")[:26], n.auction_date or ""))
    return 0 if notices else 1


if __name__ == "__main__":
    raise SystemExit(main())
