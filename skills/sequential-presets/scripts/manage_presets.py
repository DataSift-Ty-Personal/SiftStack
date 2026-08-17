"""Manage DataSift filter presets via Playwright automation.

Discovers existing presets, adds Sold exclusion to all presets,
and creates new presets from template configurations.

Usage:
    python manage_presets.py --discover
    python manage_presets.py --add-sold-exclusion
    python manage_presets.py --all

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

try:
    from datasift_core import (
        create_browser, login, dismiss_popups, screenshot, wait_for_spa,
        DATASIFT_RECORDS_URL,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from datasift_core import (
        create_browser, login, dismiss_popups, screenshot, wait_for_spa,
        DATASIFT_RECORDS_URL,
    )

logger = logging.getLogger(__name__)

# Known preset folder names (case variations exist in DataSift UI)
KNOWN_FOLDERS = [
    "00 Niche Sequential Marketing",
    "00 NICHE SEQUENTIAL MARKETING",
    "01. Bulk Sequential Marketing",
    "01 BULK SEQUENTIAL MARKETING",
    "01. Bulk Sequential",
]


async def _navigate_to_records(page) -> None:
    """Navigate to Records page."""
    if "/records" not in page.url:
        await page.goto(DATASIFT_RECORDS_URL, wait_until="domcontentloaded")
    await wait_for_spa(page, 5000)
    await dismiss_popups(page)


async def _open_filter_panel(page) -> bool:
    """Open the filter panel on the Records page."""
    await dismiss_popups(page)

    filter_link = page.locator('#Records__Filters_Trigger')
    if await filter_link.count() == 0:
        filter_link = page.locator('a:has-text("Filter Records")')

    if await filter_link.count() > 0:
        await filter_link.first.click()
        await page.wait_for_timeout(2000)
        logger.info("Opened filter panel")
        return True

    logger.warning("No Filter Records link found")
    return False


async def _scroll_to_presets(page) -> bool:
    """Scroll the filter panel to reveal the Filter Presets section at the bottom.

    The filter panel is a scrollable <div>, NOT the viewport.
    Playwright's scroll_into_view_if_needed() doesn't work for this.
    """
    scrolled = await page.evaluate("""() => {
        const panels = document.querySelectorAll(
            '[class*="FilterPanel"], [class*="filter-panel"], '
            + '[class*="RecordsFilters"], [class*="Sidebar"]'
        );
        for (const panel of panels) {
            if (panel.scrollHeight > panel.clientHeight) {
                panel.scrollTop = panel.scrollHeight;
                return true;
            }
        }
        // Fallback: find element with "Filter Presets" text and scroll to it
        const allEls = document.querySelectorAll('*');
        for (const el of allEls) {
            const t = (el.innerText || '').trim();
            if (t === 'Filter Presets' || t === 'FILTER PRESETS') {
                el.scrollIntoView({behavior: 'instant', block: 'start'});
                let parent = el.parentElement;
                for (let i = 0; i < 10 && parent; i++) {
                    if (parent.scrollHeight > parent.clientHeight) {
                        parent.scrollTop = parent.scrollHeight;
                    }
                    parent = parent.parentElement;
                }
                return true;
            }
        }
        return false;
    }""")
    await page.wait_for_timeout(2000)
    return scrolled


async def discover_presets(page) -> dict:
    """Discover all preset folders and preset names.

    Returns dict of {folder_name: [preset_names]}.
    """
    await _navigate_to_records(page)
    if not await _open_filter_panel(page):
        return {}

    await _scroll_to_presets(page)
    await screenshot(page, "discover_presets_scrolled")

    folders_data = {}

    for folder_name_guess in KNOWN_FOLDERS:
        folder_el = page.get_by_text(folder_name_guess, exact=False)
        if await folder_el.count() > 0:
            await folder_el.first.scroll_into_view_if_needed()
            await folder_el.first.click()
            await page.wait_for_timeout(1000)

            presets = await page.evaluate(r"""(folderText) => {
                const presets = [];
                const allEls = document.querySelectorAll('*');
                let folderEl = null;
                for (const el of allEls) {
                    if (el.innerText && el.innerText.trim().indexOf(folderText) !== -1
                        && el.children.length < 5) {
                        folderEl = el;
                        break;
                    }
                }
                if (!folderEl) return presets;
                let container = folderEl.parentElement;
                if (!container) return presets;
                const items = container.querySelectorAll('a, [role="button"], span, div');
                for (const item of items) {
                    const t = item.innerText ? item.innerText.trim() : '';
                    if (t && /^\d/.test(t) && t.length > 2 && t.length < 60
                        && !presets.includes(t)) {
                        presets.push(t);
                    }
                }
                return presets;
            }""", folder_name_guess[:20])

            if presets:
                folders_data[folder_name_guess] = presets
                logger.info("Folder '%s': %d presets", folder_name_guess, len(presets))
                for p in presets:
                    logger.info("  - %s", p)

    return folders_data


async def _load_preset(page, preset_name: str) -> bool:
    """Click a preset name to load it into the filter panel."""
    preset_el = page.get_by_text(preset_name, exact=True)
    if await preset_el.count() == 0:
        preset_el = page.get_by_text(preset_name, exact=False)

    if await preset_el.count() > 0:
        await preset_el.first.click()
        await page.wait_for_timeout(2000)
        logger.info("Loaded preset: %s", preset_name)
        return True

    logger.warning("Preset not found: %s", preset_name)
    return False


async def _add_sold_exclusion(page, preset_name: str) -> bool:
    """Add 'Property Status - Do not include - Sold' to a loaded preset.

    Looks for the Property Status filter block. If it doesn't exist,
    adds one. Sets it to "Do not include" -> "Sold".
    """
    try:
        # Check if Property Status block already exists
        status_block = page.locator('text="Property Status"')
        if await status_block.count() == 0:
            # Add a new filter block for Property Status
            add_filter = page.locator('text="Add new filter block"')
            if await add_filter.count() > 0:
                await add_filter.first.click()
                await page.wait_for_timeout(1000)

                # Search for Property Status
                search = page.locator('input[placeholder*="search" i]')
                if await search.count() > 0:
                    await search.first.fill("Property Status")
                    await page.wait_for_timeout(500)

                status_option = page.get_by_text("Property Status", exact=True)
                if await status_option.count() > 0:
                    await status_option.first.click()
                    await page.wait_for_timeout(1000)

        # Set to "Do not include"
        # Find the include/exclude toggle near Property Status
        await dismiss_popups(page)

        # Look for "Do not include" option
        do_not_include = page.get_by_text("Do not include", exact=True)
        if await do_not_include.count() > 0:
            await do_not_include.first.click()
            await page.wait_for_timeout(500)

        # Select "Sold" from the status options
        sold_option = page.get_by_text("Sold", exact=True)
        if await sold_option.count() > 0:
            await sold_option.first.click()
            await page.wait_for_timeout(500)
            logger.info("Added Sold exclusion to %s", preset_name)
            return True

        logger.warning("Could not find Sold option for %s", preset_name)
        return False
    except Exception as e:
        logger.error("Failed to add Sold exclusion to %s: %s", preset_name, e)
        return False


async def _save_preset(page, preset_name: str) -> bool:
    """Save the current preset (overwrite existing, NOT save as new)."""
    try:
        save_btn = page.locator('button:has-text("Save")')
        # Filter out "Save New" — we want the plain "Save" button
        for i in range(await save_btn.count()):
            btn = save_btn.nth(i)
            text = await btn.text_content()
            if text and text.strip() == "Save":
                await btn.click()
                await page.wait_for_timeout(1000)

                # Confirm overwrite dialog if it appears
                confirm = page.locator('button:has-text("Yes"), button:has-text("Confirm"), button:has-text("OK")')
                if await confirm.count() > 0:
                    await confirm.first.click()
                    await page.wait_for_timeout(1000)

                logger.info("Saved preset: %s", preset_name)
                await screenshot(page, f"preset_saved_{preset_name[:15]}")
                return True

        logger.warning("Save button not found for %s", preset_name)
        return False
    except Exception as e:
        logger.error("Failed to save preset %s: %s", preset_name, e)
        return False


async def add_sold_exclusion_all(
    headless: bool = False,
    output_dir: str = "./output",
) -> dict:
    """Discover all presets and add Sold exclusion to each one.

    Returns summary dict with results per preset.
    """
    results = {"discovered": {}, "updated": [], "failed": [], "timestamp": datetime.now().isoformat()}

    async with create_browser(headless=headless) as (browser, context, page):
        if not await login(page):
            logger.error("Login failed")
            return results

        # Discover presets
        folders = await discover_presets(page)
        results["discovered"] = folders

        if not folders:
            logger.warning("No presets discovered")
            return results

        # Update each preset
        for folder_name, presets in folders.items():
            for preset_name in presets:
                logger.info("Updating preset: %s / %s", folder_name, preset_name)

                # Navigate back to records and open filter panel
                await _navigate_to_records(page)
                if not await _open_filter_panel(page):
                    results["failed"].append(preset_name)
                    continue

                await _scroll_to_presets(page)

                # Expand folder
                folder_el = page.get_by_text(folder_name, exact=False)
                if await folder_el.count() > 0:
                    await folder_el.first.click()
                    await page.wait_for_timeout(1000)

                # Load preset
                if not await _load_preset(page, preset_name):
                    results["failed"].append(preset_name)
                    continue

                # Add sold exclusion
                if await _add_sold_exclusion(page, preset_name):
                    if await _save_preset(page, preset_name):
                        results["updated"].append(preset_name)
                    else:
                        results["failed"].append(preset_name)
                else:
                    results["failed"].append(preset_name)

    # Save results
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    report_file = out_path / f"preset_update_report_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Report saved: %s", report_file)

    return results


def main():
    parser = argparse.ArgumentParser(description="Manage DataSift filter presets")
    parser.add_argument("--discover", action="store_true", help="Discover all presets")
    parser.add_argument("--add-sold-exclusion", action="store_true",
                        help="Add Sold exclusion to all presets")
    parser.add_argument("--all", action="store_true",
                        help="Discover + add Sold exclusion")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if args.all or args.add_sold_exclusion:
        results = asyncio.run(add_sold_exclusion_all(
            headless=args.headless, output_dir=args.output_dir,
        ))
        print(f"Updated: {len(results['updated'])} presets")
        print(f"Failed: {len(results['failed'])} presets")
        if results["failed"]:
            print(f"  Failed: {', '.join(results['failed'])}")

    elif args.discover:
        async def _discover():
            async with create_browser(headless=args.headless) as (browser, context, page):
                if not await login(page):
                    print("Login failed")
                    return
                folders = await discover_presets(page)
                for folder, presets in folders.items():
                    print(f"\n{folder}:")
                    for p in presets:
                        print(f"  - {p}")

        asyncio.run(_discover())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
