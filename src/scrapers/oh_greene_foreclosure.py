"""Greene County, Ohio sheriff sales (foreclosure) scraper.

Pulls upcoming sheriff sale listings from the county's
`apps.greenecountyohio.gov/sheriff/sheriffsales.aspx` portal.

Portal characteristics (researched against live site + Wayback capture):
  * Tech stack: ASP.NET WebForms with DevExpress `ASPxGridView` controls.
    Hidden form fields __VIEWSTATE / __EVENTVALIDATION / __VIEWSTATEGENERATOR
    are present but the initial GET response already contains the full
    rendered data for all four grids — NO postback is required to read
    upcoming foreclosure sales.
  * No login, no CAPTCHA, no filters (date range etc. not exposed).
  * Four grids render on a single tab control:
      - MainBody_tabData_dgvCurrentForeclosures  (upcoming foreclosure sales)
      - MainBody_tabData_dgvCurrentTreasurerSales (upcoming tax/treasurer sales)
      - MainBody_tabData_dgvPastForeclosures
      - MainBody_tabData_dgvPastTreasurerSales
    We only read the two *Current* grids — past sales already happened.
  * DevExpress pagination uses client-side callbacks
    (`ASPx.GVPagerOnClick(...)`) rather than plain `__doPostBack`. The
    "Current Foreclosures" grid typically holds a single page of ~5-15 rows
    for Greene's monthly cadence, so pagination is unnecessary. If a grid
    ever exceeds the default page size, `MAX_CURRENT_ROWS_SENTINEL` will
    warn in logs.
  * Greene is a small county — sheriff sales run ~monthly. A 30-day window
    is the realistic lookback / look-ahead.
  * The ASP.NET application throws unhandled exceptions intermittently and
    302-redirects every request to `/error/error.html` when it does. The
    scraper retries with exponential backoff (2s, 4s, 8s) before giving up.

Grid columns (foreclosure): SALE DATE, STATUS, SALE DATE 2, STATUS 2,
CASE NO, ADDRESS, CITY, PARCEL NO, APPRAISAL, JUDGMENT, DEPOSIT, ATTORNEY,
PURCHASER, PURCHASE PRICE, NOTES.

Treasurer-sale grid uses START BID in place of APPRAISAL/JUDGMENT/DEPOSIT.

Notable schema gaps:
  * No plaintiff / defendant / owner column. `owner_name` is left blank;
    the enrichment pipeline will resolve the owner via parcel ID against
    the Greene County auditor.
  * No filing/publish date. We use `auction_date` (SALE DATE 2 if the first
    sale returned "No Bid", else SALE DATE) as `date_added` so the daily
    pipeline has a concrete date to key on.
  * No per-case detail page link. `source_url` points at the list page.

NoticeData contract per CLAUDE.md:
  * county = "Greene", state = "OH", notice_type = "foreclosure"
  * auction_date = effective sheriff sale date (YYYY-MM-DD)
  * address/city populated; zip left blank (not exposed by the portal)
  * raw_text preserves the full parsed row for downstream classification
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://apps.greenecountyohio.gov"
LIST_URL = f"{BASE_URL}/sheriff/sheriffsales.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# DevExpress grid element IDs — verified from live HTML capture.
GRID_CURRENT_FORECLOSURES = "MainBody_tabData_dgvCurrentForeclosures"
GRID_CURRENT_TREASURER = "MainBody_tabData_dgvCurrentTreasurerSales"

# Foreclosure grid column order (15 columns).
FORECLOSURE_COLS = [
    "sale_date", "status", "sale_date_2", "status_2",
    "case_no", "address", "city", "parcel_no",
    "appraisal", "judgment", "deposit", "attorney",
    "purchaser", "purchase_price", "notes",
]
# Treasurer-sale grid column order (13 columns — no appraisal/judgment/deposit,
# just START BID).
TREASURER_COLS = [
    "sale_date", "status", "sale_date_2", "status_2",
    "case_no", "address", "city", "parcel_no",
    "start_bid", "purchaser", "purchase_price", "attorney", "notes",
]

# DevExpress data-row classes — match either variant.
DATA_ROW_CLS_RE = re.compile(r"dxgvDataRow")

# Configured retry schedule per task instructions: 2s, 4s, 8s.
RETRY_BACKOFF_SECS = [2, 4, 8]

# Sentinel: if the Current grid ever returns >= this many rows, surface a
# warning — we may be missing paginated data and should revisit.
MAX_CURRENT_ROWS_SENTINEL = 40

# Error-page signature used by the portal when the ASP.NET app is down.
ERROR_PAGE_TITLE = "Greene County Website Error"


@dataclass
class _SaleRow:
    """Parsed representation of one grid row — keyed by column name."""
    data: dict[str, str]
    grid_name: str  # "foreclosure" | "treasurer"

    @property
    def sale_date(self) -> date | None:
        return _parse_mdy(self.data.get("sale_date", ""))

    @property
    def sale_date_2(self) -> date | None:
        return _parse_mdy(self.data.get("sale_date_2", ""))

    @property
    def effective_auction_date(self) -> date | None:
        """Sale Date 2 applies when the first sale returned 'No Bid'."""
        if self.data.get("status", "").strip().lower() == "no bid":
            return self.sale_date_2 or self.sale_date
        return self.sale_date


class Scraper(NoticeScraper):
    """Greene County Sheriff — upcoming foreclosure sales scraper."""

    county = "Greene"
    notice_type = "foreclosure"
    source_name = "Greene County Sheriff Sales"
    source_url = LIST_URL

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull upcoming sheriff sales with auction dates on/after `since_date`.

        Default window: 30 days back (captures recently-added listings whose
        auction dates may be slightly in the past if the listing just posted).
        """
        if since_date is None:
            since_date = date.today() - timedelta(days=30)
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Sync implementation ────────────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        logger.info(
            "Greene foreclosure scrape: looking for sales on/after %s",
            since_date.isoformat(),
        )

        soup = self._fetch_list_page()
        if soup is None:
            logger.warning(
                "Greene portal unreachable after retries — returning empty list"
            )
            return []

        foreclosure_rows = self._parse_grid(
            soup, GRID_CURRENT_FORECLOSURES, FORECLOSURE_COLS, "foreclosure"
        )
        treasurer_rows = self._parse_grid(
            soup, GRID_CURRENT_TREASURER, TREASURER_COLS, "treasurer"
        )
        logger.info(
            "Parsed %d current foreclosure rows, %d current treasurer rows",
            len(foreclosure_rows), len(treasurer_rows),
        )

        if len(foreclosure_rows) >= MAX_CURRENT_ROWS_SENTINEL:
            logger.warning(
                "Current foreclosures grid returned %d rows — may be paginated; "
                "revisit pagination handling if this recurs.",
                len(foreclosure_rows),
            )

        all_rows = foreclosure_rows + treasurer_rows
        records: list[NoticeData] = []
        for row in all_rows:
            auction = row.effective_auction_date
            if auction is None:
                logger.debug(
                    "Row missing sale date — case %s — skipping",
                    row.data.get("case_no", "?"),
                )
                continue
            if auction < since_date:
                logger.debug(
                    "Row case=%s auction=%s before cutoff %s — skipping",
                    row.data.get("case_no", "?"), auction, since_date,
                )
                continue
            record = self._to_notice_data(row, auction)
            records.append(record)
            logger.info(
                "  %s  %s  %s, %s  (%s)",
                auction.isoformat(),
                row.data.get("case_no", "?"),
                row.data.get("address", "?"),
                row.data.get("city", "?"),
                row.grid_name,
            )

        records.sort(key=lambda r: (r.auction_date, r.raw_text))
        logger.info("Greene foreclosure scrape done — %d records", len(records))
        return records

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _fetch_list_page(self) -> BeautifulSoup | None:
        """GET the sheriff sales page with exponential-backoff retry.

        The Greene ASP.NET app intermittently 302-redirects every request to
        `/error/error.html`. We detect the error-page title and retry up to
        MAX_RETRIES times with the configured backoff schedule.
        """
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        })

        for attempt in range(config.MAX_RETRIES):
            backoff = RETRY_BACKOFF_SECS[min(attempt, len(RETRY_BACKOFF_SECS) - 1)]
            try:
                resp = session.get(LIST_URL, timeout=30, allow_redirects=True)
                if resp.status_code != 200:
                    logger.debug(
                        "GET %s → %d (attempt %d)",
                        LIST_URL, resp.status_code, attempt + 1,
                    )
                elif ERROR_PAGE_TITLE in resp.text or "unable to process" in resp.text.lower():
                    logger.info(
                        "Greene portal returned error page (attempt %d/%d) — "
                        "ASP.NET app likely throwing; will retry in %ds",
                        attempt + 1, config.MAX_RETRIES, backoff,
                    )
                else:
                    resp.encoding = resp.encoding or "utf-8"
                    return BeautifulSoup(resp.text, "html.parser")
            except requests.RequestException as e:
                logger.debug(
                    "GET %s raised %s (attempt %d)", LIST_URL, e, attempt + 1
                )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(backoff)
        return None

    # ── Grid parsing ───────────────────────────────────────────────────
    def _parse_grid(
        self,
        soup: BeautifulSoup,
        grid_id: str,
        columns: list[str],
        grid_name: str,
    ) -> list[_SaleRow]:
        """Extract data rows from a single DevExpress grid by element id.

        Returns empty list if the grid isn't present or contains no data rows.
        """
        grid = soup.find("table", id=grid_id)
        if grid is None:
            logger.warning("Grid %s not found in page HTML", grid_id)
            return []

        rows: list[_SaleRow] = []
        for tr in grid.find_all("tr"):
            cls = tr.get("class") or []
            if not any(DATA_ROW_CLS_RE.search(c) for c in cls):
                continue
            # Only take direct <td> children — nested tables pollute the count.
            cells = tr.find_all("td", recursive=False)
            values = [_clean(td.get_text(" ", strip=True)) for td in cells]
            if len(values) < len(columns):
                logger.debug(
                    "Grid %s: row has %d cells, expected %d — skipping",
                    grid_id, len(values), len(columns),
                )
                continue
            # Map by position. If the grid has extra trailing cells
            # (DevExpress sometimes appends a filler column), trim to our schema.
            data = dict(zip(columns, values[:len(columns)]))
            # Skip the legend/instructions row if any column is a header label.
            if data.get("sale_date", "").upper() == "SALE DATE":
                continue
            rows.append(_SaleRow(data=data, grid_name=grid_name))
        return rows

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(self, row: _SaleRow, auction: date) -> NoticeData:
        """Project one parsed row into the pipeline's NoticeData shape."""
        data = row.data
        # Treasurer-sales tab is a tax foreclosure — reclassify accordingly.
        # (The CLAUDE.md notice_type enum reserves `tax_sale` for this.)
        notice_type = "tax_sale" if row.grid_name == "treasurer" else self.notice_type

        address = data.get("address", "").strip()
        city = _normalize_city(data.get("city", ""))

        # Build a stable-ish source URL anchor using the case number so
        # downstream dedup can reference specific cases even though the
        # portal itself has no detail pages.
        case_no = data.get("case_no", "").strip()
        source_url = f"{LIST_URL}#case={case_no}" if case_no else LIST_URL

        # Raw text capture — useful for LLM classification + auditing.
        raw_lines = [
            f"Case No: {case_no}",
            f"Sale Date: {data.get('sale_date', '')}",
            f"Status: {data.get('status', '')}",
            f"Sale Date 2: {data.get('sale_date_2', '')}",
            f"Status 2: {data.get('status_2', '')}",
            f"Address: {address}",
            f"City: {city}",
            f"Parcel No: {data.get('parcel_no', '')}",
            f"Attorney: {data.get('attorney', '')}",
            f"Notes: {data.get('notes', '')}",
        ]
        if row.grid_name == "foreclosure":
            raw_lines.append(f"Appraisal: {data.get('appraisal', '')}")
            raw_lines.append(f"Judgment: {data.get('judgment', '')}")
            raw_lines.append(f"Deposit: {data.get('deposit', '')}")
        else:
            raw_lines.append(f"Start Bid: {data.get('start_bid', '')}")

        # date_added fallback: auction date. The portal doesn't expose
        # filing or publish date so the auction date is the most actionable
        # chronological anchor.
        return NoticeData(
            date_added=auction.isoformat(),
            auction_date=auction.isoformat(),
            county=self.county,
            state="OH",
            notice_type=notice_type,
            source_url=source_url,
            address=address,
            city=city,
            zip="",  # Not exposed by portal
            # Owner intentionally blank — no plaintiff/defendant column.
            # Enrichment pipeline resolves via parcel ID.
            owner_name="",
            parcel_id=data.get("parcel_no", "").strip(),
            raw_text="\n".join(raw_lines),
        )


# ── Module-level helpers ───────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Collapse whitespace and strip non-breaking spaces."""
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return _WS_RE.sub(" ", cleaned).strip()


def _parse_mdy(raw: str) -> date | None:
    """Parse an MM/DD/YYYY string to a date, tolerating empty/garbage input."""
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_city(raw: str) -> str:
    """Portal stores city names in all caps — Title-case for downstream use."""
    cleaned = _clean(raw)
    if not cleaned:
        return ""
    # Preserve intentional casing for 2-letter words we shouldn't title-case.
    return cleaned.title()


# Unused helper — silences linter noise from `random` import kept for
# consistency with sibling scrapers; not called because a single GET is
# sufficient (no per-row requests).
def _unused_jitter() -> None:  # pragma: no cover
    random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)


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

    parser = argparse.ArgumentParser(description="Test Greene foreclosure scraper")
    parser.add_argument(
        "--days", type=int, default=30,
        help="Look back N days from today (default 30)",
    )
    parser.add_argument(
        "--output", type=str,
        default="output/test_oh_greene_foreclosure.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} Greene foreclosure records since {since}")

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
