"""Greene County, Ohio Lis Pendens (foreclosure case filing) scraper.

Greene County's Court of Common Pleas runs an Equivant/JWorks "eServices"
SPA at `courts.greenecountyohio.gov/eservices`. This is the same JWorks
platform that the probate court uses (`/probatejw`), and the gating
mechanism is identical:

  * The portal config endpoint
        GET /eservices/api/portal/PUBLIC
    explicitly returns ``"licenseEnabled": false``. Equivant uses the
    "license" toggle to gate the public Case Search panel — when it's off,
    navigating to /eservices/search.page renders a CourtView page whose
    body says "Portal License not available." There is no Case Search
    form to fill, no case-type dropdown to pick "Foreclosure" from, and
    no public case-detail URLs to crawl.
  * Other surfaces in the same portal config:
        - calendarEnabled: true   (calendar visible — but party-restricted)
        - efilingEnabled:  true   (e-filing for attorneys, no public read)
        - ediscoveryEnabled: false
        - eschedulingEnabled: false
    Only Calendar is partially exposed; the help banner spells out that
    "the user must be a party or associated with a party on the case for
    the event to be listed in the event list" — so non-parties see
    nothing actionable for foreclosure cases.
  * The only Wicket page that responds with rendered HTML
    (/eservices/search.page) emits a JavaScript-bootstrap "BrowserInfoPage"
    that posts back to the SPA — never reaches the search form because
    the license flag short-circuits the panel.
  * No public REST/JSON case-search endpoint exists. The SPA exposes only
    /api/login, /api/portal/{code}, /api/refresh, and address/state
    utilities; nothing case-related is mounted for unauthenticated users.
  * There is no alternative public docket published by the Greene County
    Clerk of Courts. Common Pleas case data is only accessible through
    JWorks, in person at the courthouse (45 N. Detroit St., Xenia, OH),
    or by phone request.

This file mirrors the pattern established by `oh_greene_probate.py` —
treat the scraper as a HEALTH PROBE rather than a data source. Each run
hits the portal config endpoint and checks `licenseEnabled`. While the
license remains disabled the scraper logs an info message and returns
[]. The day the court flips the switch back on, we'll see a WARNING
logged ("Greene Common Pleas license now enabled — implement search")
and can ship the real Wicket-driven scraper. Until then this file:

  1. Keeps the registry slot wired so daily-pipeline run summaries still
     mention Greene Lis Pendens (status = "no public access").
  2. Avoids spurious 4xx/5xx noise from probing dead URLs.
  3. Documents the exact gating mechanism so the next agent picks up in
     five minutes instead of redoing the SPA reverse-engineering.

NoticeData contract per CLAUDE.md is a no-op for now (zero records).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import requests

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

PORTAL_LANDING_URL = "https://courts.greenecountyohio.gov/eservices/"
PORTAL_CONFIG_URL = "https://courts.greenecountyohio.gov/eservices/api/portal/PUBLIC"
SEARCH_PAGE_URL = "https://courts.greenecountyohio.gov/eservices/search.page?prtlCd=PUBLIC"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


class Scraper(NoticeScraper):
    """Greene County Common Pleas — public Lis Pendens search currently disabled.

    Health-probes the JWorks portal config on each run. Returns [] until
    the court re-enables the public Case Search license, at which point
    we'll log a WARNING so the next pipeline run flags the change and the
    real Wicket-driven foreclosure case search can be implemented.
    """

    county = "Greene"
    notice_type = "lis_pendens"
    source_name = "Greene County Common Pleas — Foreclosure Cases (JWorks)"
    source_url = PORTAL_LANDING_URL

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Probe portal license status; return [] while public search is off."""
        if since_date is None:
            since_date = date.today() - timedelta(days=30)
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Sync probe ─────────────────────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        logger.info(
            "Greene lis_pendens scrape: probing portal license (since=%s)",
            since_date.isoformat(),
        )

        license_enabled = self._check_license_enabled()
        if license_enabled is None:
            logger.warning(
                "Greene Common Pleas portal config unreachable — treating as offline. "
                "If this persists, verify %s is still live.",
                PORTAL_CONFIG_URL,
            )
            return []

        if not license_enabled:
            logger.info(
                "Greene Common Pleas public Case Search is disabled "
                "(portal config: licenseEnabled=false). "
                "No public foreclosure case filings exposed by JWorks; "
                "calendar surface is party-restricted. Returning 0 records."
            )
            return []

        # If we get here, the court has re-enabled the public search since
        # this scraper was written. Loud warning so it gets noticed and
        # the real implementation can be added.
        logger.warning(
            "Greene Common Pleas portal license is now ENABLED — "
            "public Case Search may be available. "
            "Implement Wicket-driven foreclosure case search in "
            "scrapers.oh_greene_lis_pendens (template: "
            "oh_franklin_lis_pendens.py or oh_montgomery_foreclosure.py). "
            "Returning 0 records until that work lands."
        )
        return []

    # ── Helpers ────────────────────────────────────────────────────────
    def _check_license_enabled(self) -> bool | None:
        """GET /eservices/api/portal/PUBLIC and read the licenseEnabled flag.

        Returns:
            True/False if the config was fetched and parsed cleanly.
            None on network or schema error (treated as "unknown — skip").
        """
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = requests.get(
                    PORTAL_CONFIG_URL,
                    timeout=20,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json,text/plain,*/*",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    flag = data.get("licenseEnabled")
                    if isinstance(flag, bool):
                        return flag
                    logger.debug(
                        "Greene eServices portal config returned licenseEnabled=%r "
                        "(unexpected type) — treating as unknown",
                        flag,
                    )
                    return None
                logger.debug(
                    "GET %s → %d (attempt %d)",
                    PORTAL_CONFIG_URL, resp.status_code, attempt + 1,
                )
            except (requests.RequestException, ValueError) as e:
                logger.debug(
                    "Portal config probe failed (attempt %d): %s",
                    attempt + 1, e,
                )
        return None


# ── Standalone test harness ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import csv
    import sys
    from dataclasses import asdict
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test Greene lis_pendens scraper")
    parser.add_argument(
        "--days", type=int, default=30,
        help="Look back N days (default 30; ignored while portal is disabled)",
    )
    parser.add_argument(
        "--output", type=str,
        default="output/test_oh_greene_lis_pendens.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} Greene lis_pendens records since {since}")
    if not records:
        print(
            "(Expected — Greene Common Pleas public Case Search is currently "
            "disabled at the portal-license level (eServices "
            "licenseEnabled=false). Scraper will start returning data "
            "automatically once the court re-enables it.)"
        )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if records:
            writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        else:
            f.write("")
    print(f"Wrote {out_path}")
    sys.exit(0)
