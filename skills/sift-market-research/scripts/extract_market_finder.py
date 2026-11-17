"""Extract Market Finder data from DataSift.ai via Playwright automation.

Logs into DataSift, navigates to Market Finder, selects state/county,
and extracts ZIP code + neighborhood data tables plus summary panel.

Usage:
    python extract_market_finder.py --state "Tennessee" --county "Knox"
    python extract_market_finder.py --state "Tennessee" --county "Knox" --headless
    python extract_market_finder.py --state "Tennessee" --county "Knox,Blount"

Output: JSON file(s) in --output-dir with extracted market data.

Requires: playwright, python-dotenv
          DATASIFT_EMAIL and DATASIFT_PASSWORD in .env or environment
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Support running from skill ZIP (scripts/) or from src/
try:
    from datasift_core import (
        create_browser,
        login,
        dismiss_popups,
        screenshot,
        wait_for_spa,
        DATASIFT_MARKET_FINDER_URL,
    )
except ImportError:
    # Running from SiftStack src/ directory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from datasift_core import (
        create_browser,
        login,
        dismiss_popups,
        screenshot,
        wait_for_spa,
        DATASIFT_MARKET_FINDER_URL,
    )

logger = logging.getLogger(__name__)

# ── Column Definitions ────────────────────────────────────────────────

ZIP_COLUMNS = [
    "zip_code",
    "total_inv_trans_6mo",
    "homes_on_market",
    "homes_sold_last_month",
    "median_days_on_market",
    "median_home_value",
    "median_sale_price",
]

NEIGHBORHOOD_COLUMNS = [
    "neighborhood",
    "total_inv_trans_6mo",
    "homes_on_market",
    "homes_sold_last_month",
    "median_days_on_market",
    "median_home_value",
    "median_sale_price",
]


def _parse_currency(value: str) -> float | None:
    """Parse currency string like '$304,569' to float."""
    if not value:
        return None
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    """Parse integer string, stripping commas."""
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def _row_to_dict(row: list[str], columns: list[str]) -> dict:
    """Convert a raw table row to a named dict with parsed types."""
    d = {}
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else ""
        if col in ("median_home_value", "median_sale_price"):
            d[col] = _parse_currency(val)
            d[f"{col}_raw"] = val
        elif col in ("zip_code", "neighborhood"):
            d[col] = val
        else:
            d[col] = _parse_int(val)
            d[f"{col}_raw"] = val
        d[f"{col}_raw"] = val
    return d


# ── Navigation ────────────────────────────────────────────────────────

async def _navigate_to_market_finder(page) -> bool:
    """Navigate to Market Finder page."""
    # Try direct URL first
    await page.goto(DATASIFT_MARKET_FINDER_URL, wait_until="domcontentloaded")
    await wait_for_spa(page, 5000)
    await dismiss_popups(page)

    # Verify we're on Market Finder (check for state selector or heat map)
    mf_indicator = page.locator(
        'text="Select States", '
        'text="Market Finder", '
        '[class*="MarketFinder"], '
        '[class*="market-finder"]'
    )
    if await mf_indicator.count() > 0:
        logger.info("Navigated to Market Finder via direct URL")
        return True

    # Fallback: click sidebar link
    logger.info("Direct URL didn't land on Market Finder, trying sidebar")
    sidebar_link = page.get_by_text("Market Finder", exact=False)
    if await sidebar_link.count() > 0:
        await sidebar_link.first.click()
        await wait_for_spa(page, 5000)
        await dismiss_popups(page)
        logger.info("Navigated to Market Finder via sidebar link")
        return True

    logger.error("Could not navigate to Market Finder")
    return False


async def _select_state(page, state: str) -> bool:
    """Select a state from the Market Finder state dropdown."""
    try:
        # Click "Select States" dropdown
        state_dropdown = page.locator(
            'text="Select States", '
            '[placeholder*="state" i], '
            'button:has-text("Select States")'
        ).first
        await state_dropdown.click()
        await page.wait_for_timeout(1000)

        # Type to search/filter, then click the matching option
        # Try typing in an input field if one appears
        search_input = page.locator(
            '[class*="SelectOption"] input, '
            '[class*="search"] input, '
            'input[placeholder*="search" i], '
            'input[placeholder*="state" i]'
        )
        if await search_input.count() > 0:
            await search_input.first.fill(state)
            await page.wait_for_timeout(500)

        # Click the state option
        state_option = page.get_by_text(state, exact=True)
        if await state_option.count() == 0:
            # Try partial match
            state_option = page.get_by_text(state, exact=False)

        if await state_option.count() > 0:
            await state_option.first.click()
            await page.wait_for_timeout(2000)
            logger.info("Selected state: %s", state)
            return True

        logger.error("State '%s' not found in dropdown", state)
        return False
    except Exception as e:
        logger.error("Failed to select state '%s': %s", state, e)
        return False


async def _select_county(page, county: str) -> bool:
    """Select a county from the Market Finder county dropdown."""
    try:
        # Click "Select Counties" dropdown
        county_dropdown = page.locator(
            'text="Select Counties", '
            '[placeholder*="county" i], '
            'button:has-text("Select Counties")'
        ).first
        await county_dropdown.click()
        await page.wait_for_timeout(1000)

        # Search for county
        search_input = page.locator(
            '[class*="SelectOption"] input, '
            '[class*="search"] input, '
            'input[placeholder*="search" i], '
            'input[placeholder*="county" i]'
        )
        if await search_input.count() > 0:
            await search_input.first.fill(county)
            await page.wait_for_timeout(500)

        # Click county option — format may be "County Name STATE" or just "County Name"
        county_option = page.get_by_text(county, exact=False)
        if await county_option.count() > 0:
            await county_option.first.click()
            await page.wait_for_timeout(3000)
            logger.info("Selected county: %s", county)
            return True

        logger.error("County '%s' not found in dropdown", county)
        return False
    except Exception as e:
        logger.error("Failed to select county '%s': %s", county, e)
        return False


# ── Data Extraction ───────────────────────────────────────────────────

async def _extract_data_table(page) -> list[list[str]]:
    """Extract the visible data table, handling lazy loading via scroll.

    Market Finder uses infinite scroll — some counties have 100+ ZIP codes.
    Scrolls the table container until no new rows appear.
    """
    all_rows = []
    seen_keys = set()
    prev_count = 0

    for attempt in range(50):
        # Extract visible rows via JS
        data = await page.evaluate("""() => {
            const rows = [];

            // Strategy 1: standard <table>
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const trs = table.querySelectorAll('tbody tr, tr');
                for (const tr of trs) {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length >= 7) {
                        const row = Array.from(cells).map(c => c.innerText.trim());
                        rows.push(row);
                    }
                }
                if (rows.length > 0) return rows;
            }

            // Strategy 2: div-based table (styled-components)
            const tableContainer = document.querySelector(
                '[class*="Table"], [class*="table"], [class*="DataTable"], '
                + '[class*="MarketFinder"] [class*="data"], [role="table"]'
            );
            if (tableContainer) {
                const divRows = tableContainer.querySelectorAll(
                    '[class*="Row"]:not([class*="Header"]), '
                    + '[class*="row"]:not([class*="header"]), '
                    + '[role="row"]'
                );
                for (const dr of divRows) {
                    const cells = dr.querySelectorAll(
                        '[class*="Cell"], [class*="cell"], [role="cell"], td, '
                        + '[class*="Column"], [class*="column"]'
                    );
                    if (cells.length >= 5) {
                        const row = Array.from(cells).map(c => c.innerText.trim());
                        rows.push(row);
                    }
                }
            }
            return rows;
        }""")

        for row in data:
            # Use first cell as dedup key (ZIP code or neighborhood name)
            key = row[0] if row else ""
            if key and key not in seen_keys:
                seen_keys.add(key)
                all_rows.append(row)

        if len(all_rows) == prev_count and attempt > 0:
            logger.debug("No new rows after scroll %d (total: %d)", attempt, len(all_rows))
            break

        prev_count = len(all_rows)

        # Scroll the table container down to trigger lazy loading
        await page.evaluate("""() => {
            // Find the scrollable container (may be the table parent or a wrapper div)
            const candidates = [
                document.querySelector('[class*="TableBody"]'),
                document.querySelector('[class*="table-body"]'),
                document.querySelector('[class*="Table"]'),
                document.querySelector('[class*="DataTable"]'),
                document.querySelector('[role="table"]'),
                document.querySelector('table')?.parentElement,
            ].filter(Boolean);

            for (const el of candidates) {
                if (el.scrollHeight > el.clientHeight) {
                    el.scrollTop = el.scrollHeight;
                    return;
                }
            }
            // Fallback: scroll the window
            window.scrollTo(0, document.body.scrollHeight);
        }""")
        await page.wait_for_timeout(1500)

    logger.info("Extracted %d data rows from table", len(all_rows))
    return all_rows


async def _extract_summary_panel(page) -> dict:
    """Extract right panel summary cards from Market Finder."""
    summary = await page.evaluate("""() => {
        const data = {};

        // Extract text from summary cards
        // Cards typically show: label + value pairs
        const cards = document.querySelectorAll(
            '[class*="Card"], [class*="card"], '
            + '[class*="Summary"], [class*="summary"], '
            + '[class*="Stat"], [class*="stat"]'
        );

        for (const card of cards) {
            const text = card.innerText.trim();
            // Parse "Median Home Value\\n$364.4K" style cards
            const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
            if (lines.length >= 2) {
                const label = lines[0].toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();
                const value = lines[1];
                if (label && value) {
                    data[label] = value;
                }
            }
        }

        // Also try to get homeownership rate, property types, etc.
        const allText = document.body.innerText;

        // Look for specific patterns
        const patterns = {
            'homeownership_rate': /Homeownership Rate[\\s:]*([\\d.]+%)/i,
            'market_rent': /Market Rent[\\s:]*\\$([\\d,.]+)/i,
            'gross_rental_yield': /Gross Rental Yield[\\s:]*([\\d.]+%)/i,
        };

        for (const [key, pattern] of Object.entries(patterns)) {
            const match = allText.match(pattern);
            if (match) data[key] = match[1];
        }

        return data;
    }""")

    logger.info("Extracted summary panel: %d fields", len(summary))
    return summary


async def _switch_to_neighborhood_view(page) -> bool:
    """Toggle from ZIP Code view to Neighborhood view."""
    try:
        # Look for toggle/tab that says "Neighborhoods"
        toggle = page.locator(
            'text="Neighborhoods", '
            'button:has-text("Neighborhoods"), '
            '[class*="Toggle"]:has-text("Neighborhoods"), '
            'label:has-text("Neighborhoods"), '
            'a:has-text("Neighborhoods")'
        )
        if await toggle.count() > 0:
            await toggle.first.click()
            await page.wait_for_timeout(3000)
            logger.info("Switched to Neighborhood view")
            return True

        logger.warning("Neighborhoods toggle not found")
        return False
    except Exception as e:
        logger.error("Failed to switch to Neighborhoods: %s", e)
        return False


async def _switch_to_zip_view(page) -> bool:
    """Toggle to ZIP Code view."""
    try:
        toggle = page.locator(
            'text="ZIP Codes", '
            'button:has-text("ZIP Codes"), '
            '[class*="Toggle"]:has-text("ZIP"), '
            'label:has-text("ZIP Codes"), '
            'a:has-text("ZIP Codes")'
        )
        if await toggle.count() > 0:
            await toggle.first.click()
            await page.wait_for_timeout(3000)
            logger.info("Switched to ZIP Code view")
            return True
        return False
    except Exception as e:
        logger.error("Failed to switch to ZIP Codes: %s", e)
        return False


# ── Main Extraction ───────────────────────────────────────────────────

async def extract_market_finder(
    state: str,
    county: str,
    *,
    email: str | None = None,
    password: str | None = None,
    headless: bool = False,
    output_dir: str = "./output",
) -> dict:
    """Extract Market Finder data for a state/county.

    Args:
        state: State name (e.g., "Tennessee")
        county: County name (e.g., "Knox")
        email: DataSift email (reads from .env if not provided)
        password: DataSift password (reads from .env if not provided)
        headless: Run browser headless (default False for debugging)
        output_dir: Directory to save JSON output

    Returns:
        {
            "success": bool,
            "state": str,
            "county": str,
            "zip_data": [{"zip_code": "37920", ...}, ...],
            "neighborhood_data": [{"neighborhood": "Colonial Village", ...}, ...],
            "summary": {"median_home_value": "...", ...},
            "extracted_at": "2026-04-11T...",
        }
    """
    result = {
        "success": False,
        "state": state,
        "county": county,
        "zip_data": [],
        "neighborhood_data": [],
        "summary": {},
        "extracted_at": datetime.now().isoformat(),
    }

    async with create_browser(headless=headless) as (browser, context, page):
        # Step 1: Login
        logged_in = await login(page, email, password)
        if not logged_in:
            logger.error("Login failed")
            await screenshot(page, "market_finder_login_failed")
            return result

        # Step 2: Navigate to Market Finder
        if not await _navigate_to_market_finder(page):
            await screenshot(page, "market_finder_nav_failed")
            return result

        await screenshot(page, "market_finder_loaded")

        # Step 3: Select state
        if not await _select_state(page, state):
            await screenshot(page, "market_finder_state_failed")
            return result

        await dismiss_popups(page)
        await screenshot(page, f"market_finder_state_{state}")

        # Step 4: Select county
        if not await _select_county(page, county):
            await screenshot(page, "market_finder_county_failed")
            return result

        await dismiss_popups(page)
        await screenshot(page, f"market_finder_county_{county}")

        # Step 5: Extract ZIP code data (default view)
        await _switch_to_zip_view(page)
        await page.wait_for_timeout(2000)

        zip_rows = await _extract_data_table(page)
        # Skip header row if present
        for row in zip_rows:
            if row and row[0].upper() not in ("ZIP CODE", "ZIP", ""):
                result["zip_data"].append(_row_to_dict(row, ZIP_COLUMNS))
            elif row and row[0].upper() in ("ZIP CODE", "ZIP"):
                logger.debug("Skipping header row: %s", row)

        logger.info("Extracted %d ZIP code records", len(result["zip_data"]))
        await screenshot(page, f"market_finder_zips_{county}")

        # Step 6: Extract neighborhood data
        if await _switch_to_neighborhood_view(page):
            await page.wait_for_timeout(2000)
            nbr_rows = await _extract_data_table(page)
            for row in nbr_rows:
                if row and row[0].upper() not in ("NEIGHBORHOOD", ""):
                    result["neighborhood_data"].append(
                        _row_to_dict(row, NEIGHBORHOOD_COLUMNS)
                    )
                elif row and row[0].upper() == "NEIGHBORHOOD":
                    logger.debug("Skipping header row: %s", row)

            logger.info(
                "Extracted %d neighborhood records", len(result["neighborhood_data"])
            )
            await screenshot(page, f"market_finder_neighborhoods_{county}")

        # Step 7: Extract summary panel
        result["summary"] = await _extract_summary_panel(page)

        result["success"] = len(result["zip_data"]) > 0
        logger.info(
            "Market Finder extraction %s: %d ZIPs, %d neighborhoods",
            "succeeded" if result["success"] else "FAILED",
            len(result["zip_data"]),
            len(result["neighborhood_data"]),
        )

    # Save JSON output
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    filename = f"market_finder_{state}_{county}_{datetime.now():%Y%m%d_%H%M%S}.json"
    json_file = out_path / filename
    json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info("Saved extraction to %s", json_file)

    return result


async def extract_multiple_counties(
    state: str,
    counties: list[str],
    *,
    headless: bool = False,
    output_dir: str = "./output",
) -> list[dict]:
    """Extract Market Finder data for multiple counties in one session.

    Logs in once and iterates through counties to avoid repeated logins.
    """
    results = []

    async with create_browser(headless=headless) as (browser, context, page):
        logged_in = await login(page)
        if not logged_in:
            logger.error("Login failed")
            return results

        for county in counties:
            logger.info("--- Extracting %s County, %s ---", county, state)

            if not await _navigate_to_market_finder(page):
                logger.error("Failed to navigate to Market Finder for %s", county)
                continue

            if not await _select_state(page, state):
                logger.error("Failed to select state %s", state)
                continue

            await dismiss_popups(page)

            if not await _select_county(page, county):
                logger.error("Failed to select county %s", county)
                continue

            await dismiss_popups(page)

            result = {
                "success": False,
                "state": state,
                "county": county,
                "zip_data": [],
                "neighborhood_data": [],
                "summary": {},
                "extracted_at": datetime.now().isoformat(),
            }

            # Extract ZIP codes
            await _switch_to_zip_view(page)
            await page.wait_for_timeout(2000)
            zip_rows = await _extract_data_table(page)
            for row in zip_rows:
                if row and row[0].upper() not in ("ZIP CODE", "ZIP", ""):
                    result["zip_data"].append(_row_to_dict(row, ZIP_COLUMNS))

            # Extract neighborhoods
            if await _switch_to_neighborhood_view(page):
                await page.wait_for_timeout(2000)
                nbr_rows = await _extract_data_table(page)
                for row in nbr_rows:
                    if row and row[0].upper() not in ("NEIGHBORHOOD", ""):
                        result["neighborhood_data"].append(
                            _row_to_dict(row, NEIGHBORHOOD_COLUMNS)
                        )

            # Extract summary
            result["summary"] = await _extract_summary_panel(page)
            result["success"] = len(result["zip_data"]) > 0

            # Save individual JSON
            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            filename = f"market_finder_{state}_{county}_{datetime.now():%Y%m%d_%H%M%S}.json"
            json_file = out_path / filename
            json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

            results.append(result)
            logger.info(
                "%s County: %d ZIPs, %d neighborhoods",
                county,
                len(result["zip_data"]),
                len(result["neighborhood_data"]),
            )

    return results


# ── CLI Entry Point ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract Market Finder data from DataSift.ai"
    )
    parser.add_argument(
        "--state", required=True, help="State name (e.g., Tennessee)"
    )
    parser.add_argument(
        "--county",
        required=True,
        help="County name(s), comma-separated (e.g., Knox or Knox,Blount)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--output-dir",
        default="./output",
        help="Output directory for JSON files (default: ./output)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    counties = [c.strip() for c in args.county.split(",")]

    if len(counties) == 1:
        result = asyncio.run(
            extract_market_finder(
                state=args.state,
                county=counties[0],
                headless=args.headless,
                output_dir=args.output_dir,
            )
        )
        if result["success"]:
            print(f"Extracted {len(result['zip_data'])} ZIP codes, "
                  f"{len(result['neighborhood_data'])} neighborhoods")
        else:
            print("Extraction failed — check screenshots for debugging")
            sys.exit(1)
    else:
        results = asyncio.run(
            extract_multiple_counties(
                state=args.state,
                counties=counties,
                headless=args.headless,
                output_dir=args.output_dir,
            )
        )
        for r in results:
            status = "OK" if r["success"] else "FAILED"
            print(f"  {r['county']}: {status} — {len(r['zip_data'])} ZIPs, "
                  f"{len(r['neighborhood_data'])} neighborhoods")


if __name__ == "__main__":
    main()
