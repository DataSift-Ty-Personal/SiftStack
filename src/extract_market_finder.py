"""Extract Market Finder data from DataSift.ai, through the published API or the browser.

Two sources, one JSON shape (the shape county_market_report.py and market_analyzer.py read):

  api      the Market Map routes DataSift publishes (/api/internal/market-finder/...), over the Open
           API key. No browser, no password, so it runs on the agents box and on a customer's
           install. pull_market_finder_api() is the entry point; the team's market_report workflow
           calls it.
  browser  logs into the DataSift app, selects state and county on the Market Finder page and reads
           the ZIP and neighborhood tables page by page plus the summary panel. Kept as the fallback
           on a workstation.

IMPORTANT: Always extracts EVERY row (every page of the table, every page of the API). Never
truncate or stop at the visible rows.

Usage:
    python src/extract_market_finder.py --state "Tennessee" --county "Knox"
    python src/extract_market_finder.py --state "Tennessee" --county "Knox" --headless
    python src/extract_market_finder.py --state "Tennessee" --county "Knox,Blount"
    python src/extract_market_finder.py --state TN --county Knox --source api

The command line stays on the browser unless --source api (or MARKET_FINDER_SOURCE=api) is given:
the API's query parameter names are not in the published reference (only the routes and their
responses are), so a pull checks itself (every ZIP row must belong to the county, every page must
be read) and the browser stays the default until one live API pull has passed those checks.

Output: JSON file(s) in --output-dir with extracted market data.

Requires: api: REISIFT_API_KEY. browser: playwright, python-dotenv, and DATASIFT_EMAIL and
          DATASIFT_PASSWORD in .env or environment.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


# ── Browser helpers, loaded only when the browser path runs ───────────
# datasift_core drives the DataSift web app with a password login. Importing it at the top made
# every caller of this module (the API pull included) a browser-and-password module: the agents box
# has neither, and the API path needs neither. The names below keep the browser code unchanged.

def _core():
    import datasift_core  # noqa: PLC0415  (the browser path only)
    return datasift_core


def create_browser(headless: bool = False):
    return _core().create_browser(headless=headless)


async def login(page, email: str = None, password: str = None) -> bool:
    return await _core().login(page, email, password)


async def dismiss_popups(page) -> None:
    await _core().dismiss_popups(page)


async def screenshot(page, name: str) -> None:
    await _core().screenshot(page, name)


async def wait_for_spa(page, ms: int = 5000) -> None:
    await _core().wait_for_spa(page, ms)


def _market_finder_url() -> str:
    return _core().DATASIFT_MARKET_FINDER_URL

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
        elif col in ("zip_code", "neighborhood"):
            d[col] = val
        else:
            d[col] = _parse_int(val)
        d[f"{col}_raw"] = val
    return d


# ── Aggressive Popup Dismissal ───────────────────────────────────────

async def _dismiss_all_popups(page) -> None:
    """Aggressively dismiss ALL popups, overlays, and NPS surveys.

    Must be called before ANY interaction: Beamer NPS iframe blocks
    ALL pointer events globally.
    """
    await dismiss_popups(page)

    # Extra: remove any remaining fixed/absolute overlays and iframes
    await page.evaluate("""() => {
        // Remove Beamer NPS survey (blocks ALL pointer events)
        const nps = document.getElementById('npsIframeContainer');
        if (nps) nps.remove();

        // Remove Beamer push modal (also blocks ALL pointer events)
        const pushModal = document.getElementById('beamerPushModal');
        if (pushModal) pushModal.remove();

        // Remove ALL Beamer elements
        document.querySelectorAll(
            '[id*="beamer"], [class*="beamer"], [id*="nps"], [class*="nps"], '
            + '[id*="Beamer"], [class*="Beamer"], [class*="pushModal"]'
        ).forEach(el => el.remove());

        // Remove any blocking iframes
        document.querySelectorAll('iframe').forEach(f => {
            const style = getComputedStyle(f);
            if (style.position === 'fixed' || style.position === 'absolute') {
                f.remove();
            }
        });

        // Remove fullscreen overlays that intercept clicks
        document.querySelectorAll('[style*="position: fixed"], [style*="position:fixed"]').forEach(el => {
            const rect = el.getBoundingClientRect();
            // Only remove if it covers a large area (likely an overlay)
            if (rect.width > 300 && rect.height > 200) {
                const text = el.textContent || '';
                if (text.includes('recommend') || text.includes('survey') ||
                    text.includes('notification') || text.includes('How likely')) {
                    el.remove();
                }
            }
        });
    }""")
    await page.wait_for_timeout(500)


# ── Navigation ────────────────────────────────────────────────────────

async def _navigate_to_market_finder(page) -> bool:
    """Navigate to Market Finder page."""
    await page.goto(_market_finder_url(), wait_until="domcontentloaded")
    await wait_for_spa(page, 5000)
    await _dismiss_all_popups(page)

    # Check if we landed on Market Finder
    mf_check = await page.evaluate("""() => {
        return document.body.innerText.includes('Market Finder') ||
               document.body.innerText.includes('Investor Transactions') ||
               document.querySelector('[class*="MarketFinder"]') !== null;
    }""")

    if mf_check:
        logger.info("Navigated to Market Finder via direct URL")
        return True

    # Fallback: click sidebar link
    logger.info("Direct URL didn't land on Market Finder, trying sidebar")
    sidebar_link = page.get_by_text("Market Finder", exact=False)
    if await sidebar_link.count() > 0:
        await sidebar_link.first.click()
        await wait_for_spa(page, 5000)
        await _dismiss_all_popups(page)
        logger.info("Navigated to Market Finder via sidebar link")
        return True

    logger.error("Could not navigate to Market Finder")
    return False


async def _select_state(page, state: str) -> bool:
    """Select a state from the Market Finder state dropdown.

    DataSift uses styled-components dropdowns (no native <select>).
    Strategy: find the first dropdown in the Market Finder area,
    click it, search/select the state.
    """
    await _dismiss_all_popups(page)

    try:
        # Strategy 1: Find styled-component Select dropdowns in the main area
        # Use JS to enumerate all Select-like elements and click the first one
        found = await page.evaluate("""(state) => {
            // Find all dropdown-like elements
            const candidates = [
                ...document.querySelectorAll('[class*="Selectstyles__Select"]'),
                ...document.querySelectorAll('[class*="SelectValue"]'),
                ...document.querySelectorAll('[class*="select-container"]'),
                ...document.querySelectorAll('[class*="dropdown"]'),
                ...document.querySelectorAll('[class*="Select__"]'),
            ];

            // Filter to ones in the main content area (x > 200 to skip sidebar)
            const mainCandidates = candidates.filter(el => {
                const rect = el.getBoundingClientRect();
                return rect.x > 200 && rect.width > 80 && rect.height > 20;
            });

            // Return info about what we found
            return {
                total: candidates.length,
                main: mainCandidates.length,
                texts: mainCandidates.slice(0, 5).map(el => ({
                    text: el.innerText.trim().substring(0, 50),
                    class: el.className.substring(0, 80),
                    x: Math.round(el.getBoundingClientRect().x),
                    y: Math.round(el.getBoundingClientRect().y),
                }))
            };
        }""", state)
        logger.info("Found %d total dropdown candidates, %d in main area: %s",
                     found["total"], found["main"], json.dumps(found["texts"], indent=2))

        # Strategy 2: Try clicking the first dropdown in the top area
        # Look for the search/filter bar near the top
        search_area = await page.evaluate("""() => {
            // Find text inputs or search boxes in the Market Finder area
            const inputs = document.querySelectorAll('input[type="text"], input[type="search"], input:not([type])');
            const results = [];
            for (const inp of inputs) {
                const rect = inp.getBoundingClientRect();
                if (rect.x > 200 && rect.y < 200) {
                    results.push({
                        placeholder: inp.placeholder || '',
                        value: inp.value || '',
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        class: inp.className.substring(0, 80),
                    });
                }
            }
            return results;
        }""")
        logger.info("Found %d search inputs in top area: %s", len(search_area), json.dumps(search_area, indent=2))

        # Strategy 3: Try to find and click "Search Screen" or state selection area
        # Look for the top controls area
        top_clicks = [
            'text="Search Screen"',
            'text="Select States"',
            'text="States"',
            '[placeholder*="state" i]',
            '[placeholder*="search" i]',
            '[placeholder*="Search" i]',
        ]

        for selector in top_clicks:
            el = page.locator(selector)
            if await el.count() > 0:
                logger.info("Found element via '%s', clicking...", selector)
                await el.first.click(force=True)
                await page.wait_for_timeout(1500)
                await _dismiss_all_popups(page)
                break

        # Strategy 4: Try to use any visible input/search box to type the state
        # After clicking, look for a text input where we can type
        for selector in [
            'input[placeholder*="search" i]',
            'input[placeholder*="state" i]',
            'input:not([type="hidden"])',
            '[class*="SearchInput"] input',
            '[class*="search"] input',
        ]:
            inp = page.locator(selector)
            visible_inputs = []
            for i in range(await inp.count()):
                if await inp.nth(i).is_visible():
                    visible_inputs.append(inp.nth(i))

            if visible_inputs:
                logger.info("Found visible input via '%s', typing state '%s'", selector, state)
                await visible_inputs[0].fill(state)
                await page.wait_for_timeout(1000)

                # Look for the state in the dropdown options
                state_option = page.get_by_text(state, exact=True)
                if await state_option.count() > 0:
                    await state_option.first.click()
                    await page.wait_for_timeout(3000)
                    logger.info("Selected state: %s", state)
                    return True

                # Try partial match
                state_option = page.locator(f'[class*="Option"]:has-text("{state}")')
                if await state_option.count() > 0:
                    await state_option.first.click()
                    await page.wait_for_timeout(3000)
                    logger.info("Selected state via option class: %s", state)
                    return True

                # Clear and try next input
                await visible_inputs[0].fill("")
                await page.wait_for_timeout(500)

        # Strategy 5: Click on the state on the map
        logger.info("Trying to click on Tennessee on the US map")
        tn_on_map = page.locator('path[data-name="Tennessee"], path[title="Tennessee"]')
        if await tn_on_map.count() > 0:
            await tn_on_map.first.click()
            await page.wait_for_timeout(3000)
            logger.info("Clicked Tennessee on map")
            return True

        # Strategy 6: Try using JS to click by evaluating all clickable elements
        clicked = await page.evaluate("""(state) => {
            // Find any element containing the state name that looks clickable
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.children.length === 0 && el.textContent.trim() === state) {
                    const rect = el.getBoundingClientRect();
                    if (rect.x > 200 && rect.width > 20) {
                        el.click();
                        return { clicked: true, tag: el.tagName, x: rect.x, y: rect.y };
                    }
                }
            }
            return { clicked: false };
        }""", state)

        if clicked.get("clicked"):
            await page.wait_for_timeout(3000)
            logger.info("Clicked state element via JS: %s", clicked)
            return True

        logger.error("All strategies failed to select state '%s'", state)
        await screenshot(page, "market_finder_state_debug")
        return False

    except Exception as e:
        logger.error("Failed to select state '%s': %s", state, e)
        await screenshot(page, "market_finder_state_error")
        return False


async def _select_county(page, county: str) -> bool:
    """Select a county using the 'Select Counties' input in the top filter bar.

    The Market Finder top bar has: Select States | Select Counties | ZIP Codes | ...
    Each is an InputMultiSearch with a specific placeholder. We target the
    'Select Counties' input directly, type the county name, and click the
    matching dropdown option.
    """
    await _dismiss_all_popups(page)

    try:
        # Wait for state selection to settle
        await page.wait_for_timeout(2000)

        # Target the Select Counties input specifically by placeholder
        county_input = page.locator('[placeholder="Select Counties"]')
        if await county_input.count() == 0:
            # Fallback: try case-insensitive
            county_input = page.locator('[placeholder*="county" i]')

        if await county_input.count() == 0:
            logger.error("Select Counties input not found")
            await screenshot(page, "market_finder_county_input_missing")
            return False

        logger.info("Found Select Counties input, clicking and typing '%s'", county)
        await county_input.first.click(force=True)
        await page.wait_for_timeout(1000)

        # Type the county name to filter the dropdown
        await county_input.first.fill(county)
        await page.wait_for_timeout(1500)

        # Click the matching dropdown option
        # Look for options in dropdown containers
        option_clicked = False
        for selector in [
            f'[class*="Option"]:has-text("{county}")',
            f'[class*="option"]:has-text("{county}")',
            f'[class*="Result"]:has-text("{county}")',
            f'[class*="Item"]:has-text("{county}")',
            f'li:has-text("{county}")',
        ]:
            opt = page.locator(selector)
            if await opt.count() > 0:
                await opt.first.click()
                option_clicked = True
                logger.info("Clicked county option via '%s'", selector)
                break

        if not option_clicked:
            # Try clicking any element with the county text that's in a dropdown
            county_text = page.get_by_text(county, exact=False)
            for i in range(min(await county_text.count(), 5)):
                el = county_text.nth(i)
                bbox = await el.bounding_box()
                # Dropdown options should appear below the input bar (y > 120)
                # and in the main area (x > 200)
                if bbox and bbox["y"] > 120 and bbox["x"] > 200 and bbox["height"] < 60:
                    await el.click()
                    option_clicked = True
                    logger.info("Clicked county dropdown option at y=%d", int(bbox["y"]))
                    break

        if not option_clicked:
            logger.error("County option '%s' not found in dropdown", county)
            await screenshot(page, "market_finder_county_dropdown_empty")
            return False

        # Wait for the view to transition from state-level to county-level
        await page.wait_for_timeout(5000)

        # Verify: check if table now shows ZIP codes instead of counties
        verify = await page.evaluate("""() => {
            const cells = document.querySelectorAll('td, [class*="Cell"]');
            for (const cell of cells) {
                const text = cell.innerText.trim();
                // ZIP codes are 5-digit numbers
                if (/^\\d{5}$/.test(text)) return { hasZips: true, sampleZip: text };
            }
            return { hasZips: false };
        }""")

        if verify.get("hasZips"):
            logger.info("County selected: table now shows ZIP codes (sample: %s)", verify.get("sampleZip"))
        else:
            logger.warning("County may not have loaded: no ZIP codes visible in table yet")
            # Give extra time
            await page.wait_for_timeout(3000)

        logger.info("Selected county: %s", county)
        return True

    except Exception as e:
        logger.error("Failed to select county '%s': %s", county, e)
        await screenshot(page, "market_finder_county_error")
        return False


# ── Data Extraction ───────────────────────────────────────────────────

async def _extract_all_table_rows(page) -> list[list[str]]:
    """Extract ALL rows from the Market Finder data table across all pages.

    CRITICAL: The Market Finder uses PAGINATION (not infinite scroll).
    The table shows 20 rows per page with "Page X of Y" controls.
    This function extracts every page to get EVERY ZIP/neighborhood.

    The table is div-based (Tablestyles__TableContainer with TableRow/TableCell
    styled-components), NOT an HTML <table>.
    """
    all_rows = []
    seen_keys = set()

    # JS to extract current page's rows from the styled-components table
    EXTRACT_PAGE_JS = """() => {
        const rows = [];

        // Strategy 1: Styled-components TableContainer (Market Finder's actual structure)
        const container = document.querySelector('[class*="Tablestyles__TableContainer"]') ||
                          document.querySelector('[class*="TableContainer"]');
        if (container) {
            const rowEls = container.querySelectorAll('[class*="TableRow"], [class*="Row"]');
            for (const rowEl of rowEls) {
                const cells = rowEl.querySelectorAll('[class*="TableCell"], [class*="Cell"]');
                if (cells.length >= 7) {
                    const row = Array.from(cells).map(c => c.innerText.trim());
                    if (row.some(v => v.length > 0)) rows.push(row);
                }
            }
            if (rows.length > 0) return rows;
        }

        // Strategy 2: Standard HTML <table> (fallback)
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            const rect = table.getBoundingClientRect();
            if (rect.x < 150 || rect.width < 300) continue;
            const trs = table.querySelectorAll('tbody tr, tr');
            for (const tr of trs) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 7) {
                    rows.push(Array.from(cells).map(c => c.innerText.trim()));
                }
            }
        }
        return rows;
    }"""

    # JS to get pagination info: {from, to, total, currentPage, totalPages}
    PAGINATION_JS = """() => {
        const text = document.body.innerText;
        const rangeMatch = text.match(/(\\d+)-(\\d+) of (\\d+)/);
        const pageMatch = text.match(/of (\\d+)/);

        // Find current page input value
        const pageInput = document.querySelector(
            '[class*="Pagination"] input'
        );
        const currentPage = pageInput ? parseInt(pageInput.value) || 1 : 1;

        return {
            from: rangeMatch ? parseInt(rangeMatch[1]) : 0,
            to: rangeMatch ? parseInt(rangeMatch[2]) : 0,
            total: rangeMatch ? parseInt(rangeMatch[3]) : 0,
            currentPage,
            totalPages: pageMatch ? parseInt(pageMatch[1]) : 1,
        };
    }"""

    # JS to click the "next page" button (right-most button in pagination)
    NEXT_PAGE_JS = """() => {
        const pagContainer = document.querySelector(
            '[class*="PaginationInnerContainer"]'
        );
        if (!pagContainer) return false;

        // The pagination has: [prev_btn] [page_input] [of N] [next_btn]
        // Next button is the LAST <button> in the container
        const buttons = pagContainer.querySelectorAll('button');
        if (buttons.length >= 2) {
            const nextBtn = buttons[buttons.length - 1];
            nextBtn.click();
            return true;
        }
        return false;
    }"""

    # First, scroll the main page body down so pagination controls are in view
    await page.evaluate("""() => {
        const body = document.querySelector('[class*="AdminPage__AdminPageBody"]');
        if (body) body.scrollTop = body.scrollHeight;
    }""")
    await page.wait_for_timeout(1000)

    # Extract page by page
    max_pages = 50  # Safety limit
    for page_num in range(max_pages):
        # Get pagination info
        pag = await page.evaluate(PAGINATION_JS)
        logger.info("Page %d: showing %d-%d of %d (page %d of %d)",
                     page_num + 1, pag["from"], pag["to"], pag["total"],
                     pag["currentPage"], pag["totalPages"])

        # Extract current page rows
        data = await page.evaluate(EXTRACT_PAGE_JS)
        new_rows = 0
        for row in data:
            key = row[0] if row else ""
            if key and key not in seen_keys and key.upper() not in (
                "ZIP CODE", "ZIP", "NEIGHBORHOOD", ""
            ):
                seen_keys.add(key)
                all_rows.append(row)
                new_rows += 1

        logger.debug("  Extracted %d new rows from page %d", new_rows, page_num + 1)

        # Check if we've got all rows
        if pag["total"] > 0 and len(all_rows) >= pag["total"]:
            logger.info("Got all %d rows", len(all_rows))
            break

        # Check if this is the last page
        if pag["to"] >= pag["total"] or pag["currentPage"] >= pag["totalPages"]:
            logger.info("Reached last page (%d of %d)", pag["currentPage"], pag["totalPages"])
            break

        # Click next page
        clicked = await page.evaluate(NEXT_PAGE_JS)
        if not clicked:
            logger.warning("Could not click next page button")
            break

        # Wait for new data to load
        await page.wait_for_timeout(2000)

    logger.info("Extracted %d total rows across %d pages (expected %d)",
                len(all_rows), page_num + 1, pag.get("total", "?"))
    return all_rows


async def _extract_summary_panel(page) -> dict:
    """Extract right panel summary cards from Market Finder."""
    summary = await page.evaluate("""() => {
        const data = {};

        // Extract from summary cards (label + value pairs)
        const cards = document.querySelectorAll(
            '[class*="Card"], [class*="card"], '
            + '[class*="Summary"], [class*="summary"], '
            + '[class*="Stat"], [class*="stat"], '
            + '[class*="Metric"], [class*="metric"]'
        );

        for (const card of cards) {
            const rect = card.getBoundingClientRect();
            // Only right panel cards (x > 600)
            if (rect.x < 500 || rect.width < 50) continue;

            const text = card.innerText.trim();
            const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
            if (lines.length >= 2) {
                const label = lines[0].toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();
                const value = lines[1];
                if (label && value && label.length < 50) {
                    data[label] = value;
                }
            }
        }

        // Also try specific regex patterns on the page text
        const allText = document.body.innerText;
        const patterns = {
            'homeownership_rate': /Homeownership Rate[\\s:]*([\\d.]+%)/i,
            'market_rent': /Market Rent[\\s:]*\\$([\\d,.]+)/i,
            'gross_rental_yield': /Gross Rental Yield[\\s:]*([\\d.]+%)/i,
            'median_home_value_summary': /Median Home Value[\\s:]*\\$([\\d,.KkMm]+)/i,
            'homes_on_market_summary': /Homes on Market[\\s:]*([\\d,.KkMm]+)/i,
            'mo_investor_trans': /Mo\\.? Investor Transactions[\\s:]*([\\d,.]+)/i,
            'homes_sold_last_month': /Homes Sold Last Month[\\s:]*([\\d,.]+)/i,
        };

        for (const [key, pattern] of Object.entries(patterns)) {
            const match = allText.match(pattern);
            if (match) data[key] = match[1];
        }

        return data;
    }""")

    logger.info("Extracted summary panel: %d fields: %s", len(summary), list(summary.keys()))
    return summary


async def _switch_view(page, view_name: str) -> bool:
    """Toggle between ZIP Codes and Neighborhoods views.

    The view toggle is a styled-components Select dropdown (not a tab or button).
    It contains options: "ZIP Codes" and "Neighborhoods".
    From the DOM: class="Selectstyles__SelectContainer-..."

    IMPORTANT: Check current view first: if already on the requested view,
    don't click (clicking would toggle AWAY from it).
    """
    try:
        # Find the view toggle in the top bar (y < 150, x > 500)
        select_value = page.locator('[class*="SelectValue"]')
        for i in range(await select_value.count()):
            el = select_value.nth(i)
            bbox = await el.bounding_box()
            if bbox and bbox["y"] < 150 and bbox["x"] > 500:
                current_text = (await el.inner_text()).strip()
                logger.info("View toggle shows '%s', requested '%s'", current_text, view_name)

                # Already on the requested view: do nothing
                if view_name in current_text:
                    logger.info("Already on %s view: no switch needed", view_name)
                    return True

                # Need to switch: open dropdown and click the target option
                await el.click(force=True)
                await page.wait_for_timeout(1000)

                # Click the target option
                option = page.locator(f'[class*="SelectOption"]:has-text("{view_name}")')
                if await option.count() > 0:
                    await option.first.click(force=True)
                    await page.wait_for_timeout(3000)
                    logger.info("Switched to %s view", view_name)
                    return True

                # Fallback: text match
                option = page.get_by_text(view_name, exact=True)
                if await option.count() > 0:
                    await option.first.click(force=True)
                    await page.wait_for_timeout(3000)
                    logger.info("Switched to %s view via text click", view_name)
                    return True

                # Close dropdown without selecting
                await page.keyboard.press("Escape")
                logger.warning("Could not find '%s' option in dropdown", view_name)
                return False

        logger.warning("View toggle not found in top bar")
        return False
    except Exception as e:
        logger.error("Failed to switch to %s: %s", view_name, e)
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

    Extracts ALL ZIP codes and ALL neighborhoods: scrolls through the
    entire table to ensure complete data. Never truncates.

    Returns:
        {
            "success": bool,
            "state": str,
            "county": str,
            "zip_data": [{"zip_code": "37920", ...}, ...],
            "neighborhood_data": [{"neighborhood": "Colonial Village", ...}, ...],
            "summary": {"median_home_value": "...", ...},
            "extracted_at": "2026-04-11T...",
            "zip_count": int,
            "neighborhood_count": int,
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
        "zip_count": 0,
        "neighborhood_count": 0,
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

        # Aggressively dismiss popups before any interaction
        await _dismiss_all_popups(page)
        await screenshot(page, "market_finder_loaded")

        # Step 3: Select state
        if not await _select_state(page, state):
            await screenshot(page, "market_finder_state_failed")
            return result

        await _dismiss_all_popups(page)
        await screenshot(page, f"market_finder_state_{state}")

        # Step 4: Select county
        if not await _select_county(page, county):
            await screenshot(page, "market_finder_county_failed")
            return result

        await _dismiss_all_popups(page)
        await screenshot(page, f"market_finder_county_{county}")

        # Step 5: Extract ZIP code data (default view)
        await _switch_view(page, "ZIP Codes")
        await page.wait_for_timeout(2000)

        zip_rows = await _extract_all_table_rows(page)
        for row in zip_rows:
            result["zip_data"].append(_row_to_dict(row, ZIP_COLUMNS))

        result["zip_count"] = len(result["zip_data"])
        logger.info("Extracted %d ZIP code records", result["zip_count"])
        await screenshot(page, f"market_finder_zips_{county}")

        # Step 6: Extract ALL neighborhood data
        if await _switch_view(page, "Neighborhoods"):
            await page.wait_for_timeout(2000)
            nbr_rows = await _extract_all_table_rows(page)
            for row in nbr_rows:
                result["neighborhood_data"].append(
                    _row_to_dict(row, NEIGHBORHOOD_COLUMNS)
                )

            result["neighborhood_count"] = len(result["neighborhood_data"])
            logger.info("Extracted %d neighborhood records", result["neighborhood_count"])
            await screenshot(page, f"market_finder_neighborhoods_{county}")

        # Step 7: Extract summary panel
        result["summary"] = await _extract_summary_panel(page)

        result["success"] = len(result["zip_data"]) > 0
        logger.info(
            "Market Finder extraction %s: %d ZIPs, %d neighborhoods",
            "succeeded" if result["success"] else "FAILED",
            result["zip_count"],
            result["neighborhood_count"],
        )

    save_result(result, output_dir)
    return result


def save_result(result: dict, output_dir: str | Path) -> Path:
    """Write one extraction the way the browser always has: market_finder_<state>_<county>_<stamp>.json."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    filename = f"market_finder_{result['state']}_{result['county']}_{datetime.now():%Y%m%d_%H%M%S}.json"
    json_file = out_path / filename
    json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info("Saved extraction to %s", json_file)
    return json_file


# ── The published API (DataSift API 2.0, Market Map) ──────────────────
#
# The routes and their responses are documented (developers.datasift.ai, read 2026-11-13); the
# query parameters are not (every Market Map route lists `parameters: []`). The names below are the
# ones DataSift's own Market Finder page sends (read from the app's code and proven live on the key,
# 2026-11-13): a county is `fips`, a page is `offset` + `limit`. `county=` and `page=` are SILENTLY
# ignored (county= returned all 33,784 ZIPs in the country). They can be changed without a code
# change: MARKET_FINDER_API_PARAMS='{"county": "...", ...}' and MARKET_FINDER_API_GROUPS. A name
# the server does not know is silently ignored, which reads exactly like a real answer, so the pull
# proves each filter by what comes back instead of trusting that it was accepted.

API_ROUTES = {
    "states": "/api/internal/market-finder/states/",
    "counties": "/api/internal/market-finder/counties/",
    "zips": "/api/internal/market-finder/counties/zips/",
    "neighborhoods": "/api/internal/market-finder/counties/neighborhoods/",
    "metrics": "/api/internal/market-finder/metrics/",
    "paginated": "/api/internal/market-finder/paginated/",
}
API_PARAMS = {"state": "state", "county": "fips", "group": "geo_group", "offset": "offset", "size": "limit"}
API_GROUPS = {"zip": "zip", "neighborhood": "neighborhood"}
API_PAGE_SIZE = 100
API_MAX_PAGES = 200          # 20,000 rows: no county has that many ZIPs or neighborhoods


class MarketFinderApiError(RuntimeError):
    """The API answered in a shape this pull cannot read, or a filter was not honored."""


def api_settings() -> tuple[dict, dict]:
    """(query parameter names, group values): the defaults above with any env override on top."""
    params, groups = dict(API_PARAMS), dict(API_GROUPS)
    for env, into in (("MARKET_FINDER_API_PARAMS", params), ("MARKET_FINDER_API_GROUPS", groups)):
        raw = (os.environ.get(env) or "").strip()
        if raw:
            try:
                into.update({str(k): str(v) for k, v in json.loads(raw).items()})
            except (ValueError, AttributeError) as exc:
                raise MarketFinderApiError("%s is not a JSON object: %s" % (env, exc)) from exc
    return params, groups


def _items(r) -> list:
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        for k in ("items", "results", "data"):
            if isinstance(r.get(k), list):
                return r[k]
    return []


def _fold(s) -> str:
    s = " ".join(str(s or "").replace(",", " ").split()).casefold()
    return s[: -len(" county")] if s.endswith(" county") else s


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _money_raw(v) -> str:
    """The table's own display form ("$304,569"), so the parsed and raw columns match the browser's."""
    v = _num(v)
    return "" if v is None else "${:,.0f}".format(v)


def _int_raw(v) -> str:
    v = _num(v)
    return "" if v is None else "{:,}".format(int(round(v)))


def _pct_raw(v) -> str:
    """A share as the page shows it. The reference does not say whether the API sends 0.072 or 7.2."""
    v = _num(v)
    if v is None:
        return ""
    return "%.1f%%" % (v * 100 if v <= 1 else v)


def api_row(name: str, metrics: dict, columns: list[str]) -> dict:
    """One table row from the API: the same columns, parsed the same way, as the browser reads off the
    page. The six numbers map onto the table's columns:
      total_inv_trans_6mo   num_investor_transactions (the table's six-month investor count)
      homes_on_market       homes_on_market
      homes_sold_last_month number_of_homes_sold
      median_days_on_market median_days_on_market
      median_home_value     median_property_value
      median_sale_price     median_sale_price"""
    m = metrics or {}
    raw = [str(name), _int_raw(m.get("num_investor_transactions")), _int_raw(m.get("homes_on_market")),
           _int_raw(m.get("number_of_homes_sold")), _int_raw(m.get("median_days_on_market")),
           _money_raw(m.get("median_property_value")), _money_raw(m.get("median_sale_price"))]
    return _row_to_dict(raw, columns)


def api_summary(m: dict) -> dict:
    """The county summary panel, keyed the way the browser keys it (label first, then the regex keys),
    so county_market_report reads Market Rent and Gross Rental Yield the same way from either source."""
    m = m or {}
    owners, renters = _num(m.get("num_owners")), _num(m.get("num_renters"))
    own = owners / (owners + renters) if owners is not None and renters is not None and owners + renters > 0 else None
    out = {
        "median home value": _money_raw(m.get("median_property_value")),
        "homes on market": _int_raw(m.get("homes_on_market")),
        "mo investor transactions": _int_raw(m.get("median_monthly_investor_transactions")),
        "homes sold last month": _int_raw(m.get("number_of_homes_sold")),
        "market rent": _money_raw(m.get("median_property_rental_value")),
        "gross rental yield": _pct_raw(m.get("gross_rental_yield")),
        "homeownership rate": _pct_raw(own),
        "market_rent": _money_raw(m.get("median_property_rental_value")).lstrip("$"),
        "gross_rental_yield": _pct_raw(m.get("gross_rental_yield")),
        "homeownership_rate": _pct_raw(own),
        "median_home_value_summary": _money_raw(m.get("median_property_value")).lstrip("$"),
        "homes_on_market_summary": _int_raw(m.get("homes_on_market")),
        "mo_investor_trans": _int_raw(m.get("median_monthly_investor_transactions")),
        "homes_sold_last_month": _int_raw(m.get("number_of_homes_sold")),
    }
    return {k: v for k, v in out.items() if v}


def _get(call, route: str, query: dict | None = None):
    path = API_ROUTES[route] + ("?" + urlencode(query) if query else "")
    return call(path)


def _find_state(rows: list, state: str) -> dict:
    want = _fold(state)
    for r in rows:
        if want in (_fold(r.get("name")), _fold(r.get("abbreviation")), _fold(r.get("code"))):
            return r
    raise MarketFinderApiError("Market Finder lists no state called %r" % state)


def _find_county(rows: list, county: str, st: dict) -> dict:
    marks = {_fold(st.get("name")), _fold(st.get("abbreviation")), _fold(st.get("code"))} - {""}
    hits = [r for r in rows if _fold(r.get("name")) == _fold(county)
            and (not r.get("state") or _fold(r.get("state")) in marks)]
    if not hits:
        raise MarketFinderApiError("Market Finder lists no county called %r in %s (%d counties came back)"
                                   % (county, st.get("name") or st.get("abbreviation"), len(rows)))
    if len({str(h.get("code")) for h in hits}) > 1:
        raise MarketFinderApiError("%d counties called %r came back for %s; the state filter was not honored"
                                   % (len(hits), county, st.get("name")))
    return hits[0]


def _page_all(call, query: dict, params: dict, page_size: int, label: str) -> tuple[list, int | None, str | None]:
    """Every page of one paginated listing: (rows, the total the server reports, its geoGroup echo).
    A page that adds nothing new ends the walk, so a paging parameter the server ignores (it sends the
    first page again) is caught by the total check instead of looping. Pages by offset, as the app
    does; a `page` name set in MARKET_FINDER_API_PARAMS pages by number instead."""
    rows, seen, total, echo = [], set(), None, None
    for page in range(1, API_MAX_PAGES + 1):
        at = {params["page"]: page} if params.get("page") else {params.get("offset", "offset"): (page - 1) * page_size}
        r = _get(call, "paginated", dict(query, **at, **{params["size"]: page_size}))
        if not isinstance(r, dict) or not isinstance(r.get("data"), list):
            raise MarketFinderApiError("%s: the paginated listing came back as %s, not {data, total}"
                                       % (label, type(r).__name__))
        if total is None:
            total = r.get("total") if isinstance(r.get("total"), int) else None
            echo = r.get("geoGroup")
        new = 0
        for row in r["data"]:
            pf = row.get("property_fields") or {}
            code = str(pf.get("code") or pf.get("name") or "")
            if code and code not in seen:
                seen.add(code)
                rows.append(row)
                new += 1
        if not new or (total is not None and len(rows) >= total):
            break
    return rows, total, echo


def _neighborhood_names(listing) -> dict:
    """code -> name from the county's neighborhood listing (its item shape is not documented)."""
    out = {}
    for it in _items(listing):
        if isinstance(it, dict):
            code = it.get("code") or it.get("id") or it.get("uuid")
            name = it.get("name") or it.get("title")
            if code and name:
                out[str(code)] = str(name)
    return out


def pull_market_finder_api(state: str, county: str, call, *, page_size: int = API_PAGE_SIZE) -> dict:
    """One county's Market Finder numbers over the published API, in the browser extractor's shape.

    `call(path)` is a GET on the Open API key that returns parsed JSON (the agents box passes its
    budgeted CRM). Never raises for a refusal: `success` is False and `problem` says what was wrong,
    so a caller reports it instead of scoring a county off rows that belong to another one.

    The checks, each one the failure a silently ignored filter would cause:
      * every ZIP row must be one of the county's own ZIP codes (counties/zips/),
      * every page the server reports must be read (a page parameter it ignores stops short),
      * the listing must echo the group asked for when it echoes one at all.
    """
    params, groups = api_settings()
    result = {
        "success": False, "state": state, "county": county,
        "zip_data": [], "neighborhood_data": [], "summary": {},
        "extracted_at": datetime.now().isoformat(), "zip_count": 0, "neighborhood_count": 0,
        "source": "api", "api_checks": {}, "problem": "",
    }
    checks = result["api_checks"]
    try:
        st = _find_state(_items(_get(call, "states")), state)
        counties = _items(_get(call, "counties", {params["state"]: st.get("code") or st.get("abbreviation")}))
        co = _find_county(counties, county, st)
        fips = str(co.get("code") or "")
        if not fips:
            raise MarketFinderApiError("county %r came back with no code" % county)
        checks.update({"state": st.get("name") or state, "state_abbreviation": st.get("abbreviation") or "",
                       "county": co.get("name") or county, "county_code": fips})

        zips_listing = _items(_get(call, "zips", {params["county"]: fips}))
        county_zips = {str(z.get("zip_code") or "")[:5] for z in zips_listing
                       if str(z.get("fips") or "") in ("", fips)} - {""}
        checks["county_zip_codes"] = len(county_zips)
        if not county_zips:
            raise MarketFinderApiError("the county's ZIP listing came back empty, so the ZIP rows cannot be "
                                       "checked against it")

        rows, total, echo = _page_all(call, {params["group"]: groups["zip"], params["county"]: fips},
                                      params, page_size, "zip codes")
        codes = [str((r.get("property_fields") or {}).get("code") or "")[:5] for r in rows]
        stray = sorted({c for c in codes if c not in county_zips})
        checks.update({"zip_rows": len(rows), "zip_total": total, "zip_group_echo": echo,
                       "zip_rows_outside_county": len(stray)})
        if echo not in (None, "", groups["zip"]):
            raise MarketFinderApiError("asked for %r rows and the listing says %r; the group parameter is "
                                       "named differently (MARKET_FINDER_API_PARAMS)" % (groups["zip"], echo))
        if stray:
            raise MarketFinderApiError("%d of %d ZIP rows are not in %s County (%s ...): the county filter "
                                       "was ignored, so these numbers are not the county's"
                                       % (len(stray), len(rows), co.get("name") or county, ", ".join(stray[:3])))
        if total is not None and len(rows) < total:
            raise MarketFinderApiError("read %d of the %d ZIP rows the server reports; the page parameter "
                                       "was not honored" % (len(rows), total))
        if not rows:
            raise MarketFinderApiError("no ZIP rows came back for %s County" % (co.get("name") or county))
        result["zip_data"] = [api_row(c, r.get("metrics") or {}, ZIP_COLUMNS) for c, r in zip(codes, rows)]

        names = _neighborhood_names(_get(call, "neighborhoods", {params["county"]: fips}))
        nrows, ntotal, necho = _page_all(call, {params["group"]: groups["neighborhood"], params["county"]: fips},
                                         params, page_size, "neighborhoods")
        checks.update({"neighborhood_rows": len(nrows), "neighborhood_total": ntotal,
                       "neighborhood_group_echo": necho, "neighborhood_names_listed": len(names)})
        if necho not in (None, "", groups["neighborhood"]):
            raise MarketFinderApiError("asked for %r rows and the listing says %r" % (groups["neighborhood"], necho))
        if ntotal is not None and len(nrows) < ntotal:
            raise MarketFinderApiError("read %d of the %d neighborhood rows the server reports"
                                       % (len(nrows), ntotal))
        nb_codes = [str((r.get("property_fields") or {}).get("code") or "") for r in nrows]
        if set(nb_codes) & county_zips:
            raise MarketFinderApiError("the neighborhood listing came back with ZIP codes: the group "
                                       "parameter was ignored")
        if names and any(c not in names for c in nb_codes):
            raise MarketFinderApiError("%d neighborhood rows are not in the county's own neighborhood listing"
                                       % sum(1 for c in nb_codes if c not in names))
        result["neighborhood_data"] = [
            api_row(names.get(c) or (r.get("property_fields") or {}).get("name") or c, r.get("metrics") or {},
                    NEIGHBORHOOD_COLUMNS) for c, r in zip(nb_codes, nrows)]

        metrics = _get(call, "metrics", {params["county"]: fips})
        result["summary"] = api_summary(metrics if isinstance(metrics, dict) else {})
    except MarketFinderApiError as exc:
        result["problem"] = str(exc)
        return result
    result["zip_count"] = len(result["zip_data"])
    result["neighborhood_count"] = len(result["neighborhood_data"])
    result["success"] = result["zip_count"] > 0
    return result


def default_source() -> str:
    """The command line's source: the browser, unless MARKET_FINDER_SOURCE says api. The browser stays
    the default until one live API pull has passed its own checks (the parameter names are unpublished)."""
    src = (os.environ.get("MARKET_FINDER_SOURCE") or "browser").strip().lower()
    return src if src in ("browser", "api") else "browser"


def _key_call():
    """A GET on the Open API key, budgeted like every read the agents make (src/agents/autopilot/crm.py)."""
    from agents.autopilot.crm import CRM  # noqa: PLC0415  (the command line's API path only)
    crm = CRM()
    return lambda path: crm.call(path)


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
    parser.add_argument(
        "--source",
        choices=["browser", "api"],
        default=default_source(),
        help="browser (the default) or api (the published routes, on REISIFT_API_KEY)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    counties = [c.strip() for c in args.county.split(",")]
    call = _key_call() if args.source == "api" else None

    for county in counties:
        if call is not None:
            result = pull_market_finder_api(args.state, county, call)
            if result["success"]:
                save_result(result, args.output_dir)
        else:
            result = asyncio.run(
                extract_market_finder(
                    state=args.state,
                    county=county,
                    headless=args.headless,
                    output_dir=args.output_dir,
                )
            )
        if result["success"]:
            print(f"  {county}: {result['zip_count']} ZIP codes, "
                  f"{result['neighborhood_count']} neighborhoods")
        elif call is not None:
            print(f"  {county}: the API pull was refused: {result['problem']}")
            print("  The browser still works: run again with --source browser")
            sys.exit(1)
        else:
            print(f"  {county}: EXTRACTION FAILED: check screenshots for debugging")
            sys.exit(1)


if __name__ == "__main__":
    main()
