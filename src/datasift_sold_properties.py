"""DataSift Sold Properties / Investor Transactions scraper.

Scrapes the /research/transactions surface (login-gated). The page exposes
recently closed property sales filtered to INVESTOR purchases, by state,
county, and month. There is no public REST API and no CSV export — we
drive the React app via URL params and read rows out of the rendered
table.

Discovered via recon (see scripts/recon_sold_county2.py + the JSON dumps
in output/recon_sold_properties/):

  Base URL:
    https://app.reisift.io/research/transactions

  Query params:
    filter   — URL-encoded JSON (see FilterDict below)
    page     — 1-indexed page number, 10 rows per page

  Filter JSON shape:
    {
      "productType": "INVESTOR" | "ALL",  # "INVESTOR" == Investor tab
      "applyAllFilters": true,
      "states":   [{"value": <int>, "label": "<state name>"}],
      "counties": [{"value": <int>, "label": "Jefferson, KY",
                    "state_id": <int>, "code": "<5-digit FIPS>"}],
      "dates":    ["YYYY-MM-01"],   # one entry per month bucket
      "aiScore":  [<min>, <max>],   # 0..100, irrelevant for buyer prospecting
    }

  Per-row DOM (one TableRowContainer per investor sale, 10 per page):
    cell[0] = NAME             (buyer entity / individual)
    cell[1] = PROPERTY ADDRESS (e.g. "1053 Oakwood Ave, Louisville, KY")
    cell[2] = COUNTY           (e.g. "Jefferson, KY")
    cell[3] = SALE AMOUNT      (e.g. "$72,000")
    cell[4] = WAS INV.?        (checkmark icon — always true under
                                productType=INVESTOR)
    cell[5] = DISTRESSORS      (count + names — IGNORED for buyer list)
    cell[6] = PREDICTIVE AI    (0..100 — IGNORED for buyer list)
    cell[7] = IN MY RECORDS    (checkmark or empty)

Only NAME, PROPERTY ADDRESS, COUNTY, SALE AMOUNT, and IN MY RECORDS are
captured per the data scope agreed for buyer prospecting.

Known county lookups (extend as needed):
  Jefferson, KY → value=1456, code="21111", state_id=36 (Kentucky)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

from playwright.async_api import Page, async_playwright

from datasift_core import login

logger = logging.getLogger(__name__)

DATASIFT_TRANSACTIONS_URL = "https://app.reisift.io/research/transactions"
# Tab path suffixes — the Investor tab is REQUIRED to actually filter the
# table to investor purchases. Without it the table shows ALL transactions
# (including non-investor sales). The `productType` filter param controls
# the stats panel, NOT the table.
DATASIFT_INVESTOR_URL = "https://app.reisift.io/research/transactions/investor"

# DataSift's internal state IDs (not FIPS). Discovered via URL inspection;
# extend by visiting the page with a state set and reading the URL.
STATE_LOOKUP: dict[str, dict] = {
    "KY": {"value": 36, "label": "Kentucky"},
    # add more as we discover them: TN, IN, etc.
}

# Counties keyed by (state_abbr, county_name). Extend by setting the
# County dropdown in the UI and reading the URL.
COUNTY_LOOKUP: dict[tuple[str, str], dict] = {
    ("KY", "Jefferson"): {
        "value": 1456,
        "label": "Jefferson, KY",
        "state_id": 36,
        "code": "21111",
    },
}


# ── Data model ────────────────────────────────────────────────────────


@dataclass
class SoldProperty:
    """One row from the Sold Properties / Investor Transactions table."""
    buyer_name: str = ""
    property_address: str = ""
    county: str = ""
    sale_amount_raw: str = ""        # original "$72,000" string
    sale_amount: int = 0             # parsed integer
    in_my_records: bool = False
    # Bookkeeping — which slice of the page this row came from.
    sale_month: str = ""             # "YYYY-MM"
    state: str = ""                  # 2-letter abbr
    county_filter: str = ""          # county we filtered by


# ── URL building ──────────────────────────────────────────────────────


def _build_filter(
    state_abbr: str,
    county_name: str,
    sale_month: str,
    product_type: str = "INVESTOR",
) -> dict:
    """Construct the filter JSON for one (state, county, month) slice."""
    state = STATE_LOOKUP.get(state_abbr.upper())
    if state is None:
        raise KeyError(
            f"Unknown state {state_abbr!r}. Add it to STATE_LOOKUP "
            f"in datasift_sold_properties.py."
        )
    county = COUNTY_LOOKUP.get((state_abbr.upper(), county_name))
    if county is None:
        raise KeyError(
            f"Unknown county {(state_abbr, county_name)!r}. Add it to "
            f"COUNTY_LOOKUP in datasift_sold_properties.py — visit the "
            f"page in the UI, set the county, and copy the JSON value "
            f"out of the URL."
        )
    # sale_month is "YYYY-MM"; the page wants "YYYY-MM-01"
    if not re.match(r"^\d{4}-\d{2}$", sale_month):
        raise ValueError(f"sale_month must be 'YYYY-MM', got {sale_month!r}")
    return {
        "productType": product_type,
        "applyAllFilters": True,
        "states": [state],
        "dates": [f"{sale_month}-01"],
        "aiScore": [0, 100],
        "counties": [county],
    }


def _build_url(filter_obj: dict, page: int = 1, investor_only: bool = True) -> str:
    """Build the Sold Properties URL.

    investor_only=True hits the /investor tab path, which actually filters
    the rendered table to investor purchases. The base /transactions path
    shows all sales regardless of `productType` filter param.
    """
    encoded = urllib.parse.quote(json.dumps(filter_obj, separators=(",", ":")))
    base = DATASIFT_INVESTOR_URL if investor_only else DATASIFT_TRANSACTIONS_URL
    return f"{base}?filter={encoded}&page={page}"


def month_range(start_month: str, end_month: str) -> Iterator[str]:
    """Yield 'YYYY-MM' strings inclusive from start_month to end_month."""
    start = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    if end < start:
        start, end = end, start
    cur = start
    while cur <= end:
        yield cur.strftime("%Y-%m")
        # bump to first of next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)


# ── Page helpers ──────────────────────────────────────────────────────


_AMOUNT_RE = re.compile(r"-?\$?([\d,]+)")


def _parse_amount(raw: str) -> int:
    """'$72,000' → 72000; '' → 0."""
    if not raw:
        return 0
    m = _AMOUNT_RE.search(raw)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


async def _dismiss_beamer(page: Page) -> None:
    """Dismiss the Beamer push modal (blocks ALL pointer events on first land).

    Tries clicking 'No, Thanks' first, then nukes the modal nodes from the
    DOM as a backstop. Safe to call repeatedly.
    """
    try:
        no_thanks = page.get_by_role(
            "button", name=re.compile(r"no.*thanks?", re.IGNORECASE)
        )
        if await no_thanks.count() > 0:
            await no_thanks.first.click(force=True, timeout=2000)
            await page.wait_for_timeout(300)
    except Exception:
        pass
    try:
        await page.evaluate("""() => {
            for (const id of ['beamerPushModal', 'beamerOverlay',
                              'beamerSelector', 'npsIframeContainer']) {
                const el = document.getElementById(id);
                if (el) el.remove();
            }
            for (const el of document.querySelectorAll(
                '[role="dialog"][class*="push"], [class*="beamer" i]'
            )) {
                el.remove();
            }
        }""")
    except Exception:
        pass


async def _read_page_rows(page: Page) -> list[dict]:
    """Read the rendered TableRowContainer children. Each row is a list of
    8 cell text values per the schema documented at the top of this file.
    """
    return await page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll(
            '[class*="TableRowContainer"]'
        ));
        return rows.map(r => {
            const cells = Array.from(r.querySelectorAll(
                '[class*="TableCellContainer"]'
            )).map(c => (c.textContent || '').trim());
            // Detect whether the "In My Records" cell (last) has a
            // checkmark by looking for an SVG inside.
            const lastCell = r.querySelectorAll(
                '[class*="TableCellContainer"]'
            );
            const inMyRecordsCell = lastCell[lastCell.length - 1];
            const inMyRecords = inMyRecordsCell
                ? !!inMyRecordsCell.querySelector('svg')
                : false;
            return { cells, inMyRecords };
        });
    }""")


async def _read_total_count(page: Page) -> int:
    """Read the total record count for the current filter.

    The Sold Properties dashboard shows multiple counts ("Total Transactions",
    "Investor Transactions", etc.). With productType=INVESTOR set, the
    relevant total is the "Investor Transactions" stat. We read by label.
    """
    try:
        result = await page.evaluate("""() => {
            // Walk every leaf element looking for the label text, then
            // return the sibling number.
            const labels = ['Investor Transactions', 'Total Transactions'];
            for (const el of document.querySelectorAll('div, span, p')) {
                const txt = (el.textContent || '').trim();
                for (const lbl of labels) {
                    if (txt === lbl) {
                        // walk up to find the card and look for a number
                        let cur = el.parentElement;
                        for (let i = 0; i < 4 && cur; i++) {
                            const numEl = Array.from(cur.querySelectorAll('*'))
                                .find(e => e.children.length === 0
                                       && /^[\\d,]+$/.test((e.textContent || '').trim()));
                            if (numEl) return {
                                label: lbl,
                                value: (numEl.textContent || '').trim()
                            };
                            cur = cur.parentElement;
                        }
                    }
                }
            }
            return null;
        }""")
        if result and result.get("value"):
            return int(result["value"].replace(",", ""))
    except Exception as e:
        logger.debug("total-count read failed: %s", e)
    return 0


def _row_to_sold(row: dict, *, sale_month: str, state: str, county: str) -> SoldProperty:
    cells: list[str] = row.get("cells") or []
    # Pad in case the page gave us fewer cells (defensive)
    while len(cells) < 8:
        cells.append("")
    sale_raw = cells[3]
    return SoldProperty(
        buyer_name=cells[0],
        property_address=cells[1],
        county=cells[2],
        sale_amount_raw=sale_raw,
        sale_amount=_parse_amount(sale_raw),
        in_my_records=bool(row.get("inMyRecords")),
        sale_month=sale_month,
        state=state,
        county_filter=county,
    )


# ── Main scraper ──────────────────────────────────────────────────────


async def scrape_one_month(
    page: Page,
    *,
    state_abbr: str,
    county_name: str,
    sale_month: str,
    max_pages: int = 100,
) -> list[SoldProperty]:
    """Scrape every page of Investor Transactions for one (state, county, month).

    Returns one SoldProperty per row. The page renders 10 rows per page.
    Stops when a page returns no rows or when max_pages is hit.
    """
    filter_obj = _build_filter(state_abbr, county_name, sale_month)
    results: list[SoldProperty] = []
    seen_first_row: str | None = None  # to detect when we've gone past the end

    for page_num in range(1, max_pages + 1):
        url = _build_url(filter_obj, page_num)
        logger.info(
            "Sold properties: %s %s %s page %d",
            state_abbr, county_name, sale_month, page_num,
        )
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        if page_num == 1:
            await _dismiss_beamer(page)
            await page.wait_for_timeout(400)
            total = await _read_total_count(page)
            if total:
                logger.info(
                    "Sold properties: %s %s %s total investor transactions = %d",
                    state_abbr, county_name, sale_month, total,
                )

        rows = await _read_page_rows(page)
        if not rows:
            logger.info("No rows on page %d — stopping", page_num)
            break

        # Detect "we wrapped back to page 1 because we asked for too high
        # a page number" — if the same row appears as page 1's first row,
        # we've overshot.
        first_name = (rows[0].get("cells") or [""])[0]
        if page_num == 1:
            seen_first_row = first_name
        elif first_name == seen_first_row:
            logger.info("Page %d wrapped to first row — end reached", page_num)
            break

        for r in rows:
            results.append(_row_to_sold(
                r, sale_month=sale_month, state=state_abbr, county=county_name,
            ))

        # If fewer than 10 rows, we hit the last page
        if len(rows) < 10:
            logger.info("Page %d returned only %d rows — last page", page_num, len(rows))
            break

    logger.info(
        "Sold properties: %s %s %s scraped %d rows total",
        state_abbr, county_name, sale_month, len(results),
    )
    return results


async def scrape_sold_properties(
    *,
    state_abbr: str,
    county_name: str,
    start_month: str,
    end_month: str,
    headless: bool = True,
) -> list[SoldProperty]:
    """Scrape investor transactions for one (state, county) across a month range.

    Args:
        state_abbr: 2-letter state code (e.g. "KY")
        county_name: bare county name without state suffix (e.g. "Jefferson")
        start_month: "YYYY-MM" inclusive
        end_month:   "YYYY-MM" inclusive
        headless:    run Chromium headless (default True)
    """
    all_results: list[SoldProperty] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            ok = await login(page, None, None)
            if not ok:
                logger.error("DataSift login failed — cannot scrape sold properties")
                return []
            await page.wait_for_timeout(1200)

            for month in month_range(start_month, end_month):
                month_results = await scrape_one_month(
                    page,
                    state_abbr=state_abbr,
                    county_name=county_name,
                    sale_month=month,
                )
                all_results.extend(month_results)
        finally:
            await browser.close()

    return all_results


# ── CSV export ────────────────────────────────────────────────────────


def export_sold_csv(rows: list[SoldProperty], output_path: str | Path) -> str:
    """Write SoldProperty rows to CSV. Returns the absolute path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "buyer_name", "property_address", "county", "sale_amount_raw",
        "sale_amount", "in_my_records", "sale_month", "state", "county_filter",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))

    logger.info("Wrote %d sold properties to %s", len(rows), output_path)
    return str(output_path.resolve())


# ── CLI ───────────────────────────────────────────────────────────────


async def _cli() -> None:
    """Quick CLI for ad-hoc scrapes. The production entry point is in
    main.py via the buyer-prospect-jefferson mode."""
    import argparse
    parser = argparse.ArgumentParser(description="Scrape DataSift Sold Properties")
    parser.add_argument("--state", default="KY")
    parser.add_argument("--county", default="Jefferson")
    parser.add_argument("--start", required=True, help="Start month YYYY-MM")
    parser.add_argument("--end", required=True, help="End month YYYY-MM")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chromium headless (default: visible for debugging)")
    parser.add_argument("--output", default="",
                        help="Output CSV path (default: output/sold_<state>_<county>_<range>.csv)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    rows = await scrape_sold_properties(
        state_abbr=args.state,
        county_name=args.county,
        start_month=args.start,
        end_month=args.end,
        headless=args.headless,
    )
    if not rows:
        print("No rows scraped.")
        return

    if not args.output:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(os.environ.get("OUTPUT_DIR", "output"))
        args.output = str(
            out_dir / f"sold_{args.state}_{args.county}_"
            f"{args.start}_to_{args.end}_{ts}.csv"
        )
    path = export_sold_csv(rows, args.output)
    print(f"Wrote {len(rows)} rows → {path}")


if __name__ == "__main__":
    asyncio.run(_cli())
