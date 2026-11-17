"""Create and manage DataSift sequences via Playwright automation.

Handles the sequence builder's React DnD behavior (drag triggers,
conditions, and actions) which requires slow mouse-based dragging
instead of Playwright's native drag_to().

Usage:
    python manage_sequences.py --create-sold-sequence
    python manage_sequences.py --list
    python manage_sequences.py --create-custom --title "My Sequence" --folder "Lead Management"

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
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from datasift_core import (
        create_browser, login, dismiss_popups, screenshot, wait_for_spa,
    )

logger = logging.getLogger(__name__)

SEQUENCES_URL = "https://app.reisift.io/sequences"


async def _mouse_drag(page, source_el, target_el) -> bool:
    """Drag source element to target using slow mouse movements.

    DataSift's sequence builder uses React DnD which has draggable="false"
    on cards. Playwright's native drag doesn't work. Must use slow mouse
    drag: mouse.move() -> mouse.down() -> 20 incremental steps -> mouse.up()
    """
    try:
        src_box = await source_el.bounding_box()
        tgt_box = await target_el.bounding_box()

        if not src_box or not tgt_box:
            logger.warning("Cannot get bounding boxes for drag")
            return False

        src_x = src_box["x"] + src_box["width"] / 2
        src_y = src_box["y"] + src_box["height"] / 2
        tgt_x = tgt_box["x"] + tgt_box["width"] / 2
        tgt_y = tgt_box["y"] + tgt_box["height"] / 2

        await page.mouse.move(src_x, src_y)
        await page.wait_for_timeout(500)
        await page.mouse.down()
        await page.wait_for_timeout(500)

        # Move in 20 incremental steps
        steps = 20
        for i in range(1, steps + 1):
            x = src_x + (tgt_x - src_x) * i / steps
            y = src_y + (tgt_y - src_y) * i / steps
            await page.mouse.move(x, y)
            await page.wait_for_timeout(50)

        await page.wait_for_timeout(500)
        await page.mouse.up()
        await page.wait_for_timeout(1000)

        logger.info("Drag completed: (%d,%d) -> (%d,%d)", src_x, src_y, tgt_x, tgt_y)
        return True
    except Exception as e:
        logger.error("Mouse drag failed: %s", e)
        return False


async def _navigate_to_sequences(page) -> bool:
    """Navigate to the Sequences page."""
    await page.goto(SEQUENCES_URL, wait_until="domcontentloaded")
    await wait_for_spa(page, 5000)
    await dismiss_popups(page)
    return True


async def list_sequences(page) -> list[dict]:
    """List all existing sequences."""
    await _navigate_to_sequences(page)
    await screenshot(page, "sequences_list")

    sequences = await page.evaluate("""() => {
        const seqs = [];
        const items = document.querySelectorAll(
            '[class*="SequenceItem"], [class*="sequence-item"], '
            + 'tr, [role="row"]'
        );
        for (const item of items) {
            const text = item.innerText.trim();
            if (text && text.length > 2 && text.length < 200) {
                const lines = text.split('\\n').filter(l => l.trim());
                if (lines.length > 0) {
                    seqs.push({
                        name: lines[0].trim(),
                        details: lines.slice(1).join(' | ').trim(),
                    });
                }
            }
        }
        return seqs;
    }""")

    logger.info("Found %d sequences", len(sequences))
    return sequences


async def create_sold_sequence(page) -> bool:
    """Create the 'Sold Property Cleanup' sequence.

    Trigger: Property Tags Added
    Condition: Tag = "Sold"
    Actions:
      1. Change Status to Sold
      2. Remove from all Lists
      3. Clear all Tasks
      4. Clear Assignee

    This is the standard cleanup sequence that fires when
    a property is tagged as Sold via SiftMap.
    """
    await _navigate_to_sequences(page)

    # Click Create button
    create_btn = page.locator('button:has-text("Create"), a:has-text("Create")')
    if await create_btn.count() == 0:
        logger.error("Create button not found")
        await screenshot(page, "sequence_no_create_btn")
        return False

    await create_btn.first.click()
    await page.wait_for_timeout(2000)
    await dismiss_popups(page)

    # Set title
    title_input = page.locator(
        'input[placeholder*="title" i], '
        'input[placeholder*="name" i], '
        'input[name*="title" i]'
    )
    if await title_input.count() > 0:
        await title_input.first.fill("Sold Property Cleanup")
        await page.wait_for_timeout(500)
        logger.info("Set title: Sold Property Cleanup")

    # Set folder to Transactions
    folder_dropdown = page.locator(
        'select:has(option:has-text("Select a folder")), '
        '[class*="select" i]:has-text("Select a folder")'
    )
    if await folder_dropdown.count() == 0:
        folder_dropdown = page.get_by_text("Select a folder", exact=False)

    if await folder_dropdown.count() > 0:
        await folder_dropdown.first.click()
        await page.wait_for_timeout(1000)

        transactions = page.get_by_text("Transactions", exact=True)
        if await transactions.count() > 0:
            await transactions.first.click()
            await page.wait_for_timeout(500)
            logger.info("Selected folder: Transactions")
        else:
            try:
                await page.select_option('select', label="Transactions")
                logger.info("Selected folder via select_option")
            except Exception:
                logger.warning("Could not select Transactions folder")

    await screenshot(page, "sequence_title_folder")

    # ── Drag "Property Tags Added" trigger ──
    logger.info("Looking for 'Property Tags Added' trigger card...")

    trigger_card = page.locator('text="Property Tags Added"').first
    drop_zone = page.locator('text=/[Dd]rag and drop/').first

    if await trigger_card.count() > 0 and await drop_zone.count() > 0:
        await _mouse_drag(page, trigger_card, drop_zone)
        await dismiss_popups(page)
        await screenshot(page, "sequence_trigger_placed")
    else:
        logger.error("Trigger card or drop zone not found")
        await screenshot(page, "sequence_trigger_failed")
        return False

    # ── Add condition: Tag = "Sold" ──
    # Navigate to conditions
    conditions_tab = page.locator(
        'text="Conditions", button:has-text("Conditions"), a:has-text("Conditions")'
    )
    if await conditions_tab.count() > 0:
        await conditions_tab.first.click()
        await page.wait_for_timeout(2000)
        await dismiss_popups(page)

    # Look for condition card to drag (e.g., "Tag" or "Property Tag")
    condition_card = page.locator('text=/^Tag$|Property Tag/').first
    condition_drop = page.locator('text=/[Dd]rag and drop/').first

    if await condition_card.count() > 0 and await condition_drop.count() > 0:
        await _mouse_drag(page, condition_card, condition_drop)
        await page.wait_for_timeout(1000)

    # Configure condition: select "Sold" tag
    sold_input = page.locator('input[placeholder*="tag" i], input[placeholder*="search" i]')
    if await sold_input.count() > 0:
        await sold_input.first.fill("Sold")
        await page.wait_for_timeout(500)
        sold_option = page.get_by_text("Sold", exact=True)
        if await sold_option.count() > 0:
            await sold_option.first.click()
            await page.wait_for_timeout(500)

    await dismiss_popups(page)
    await screenshot(page, "sequence_condition_sold")

    # ── Navigate to Actions tab ──
    actions_nav = page.locator(
        'text="Set the Following Actions", '
        'button:has-text("Actions"), '
        'a:has-text("Actions")'
    )
    if await actions_nav.count() > 0:
        await actions_nav.first.click()
        await page.wait_for_timeout(2000)
    else:
        # Try direct URL navigation
        current_url = page.url
        if "/sequences/" in current_url:
            actions_url = current_url.rstrip("/") + "/actions"
            await page.goto(actions_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

    await dismiss_popups(page)
    await screenshot(page, "sequence_actions_tab")

    # ── Drag action cards ──
    # Actions: Change Status, Remove Lists, Clear Tasks, Clear Assignee
    action_texts = [
        "Change Status",
        "Remove from Lists",
        "Clear Tasks",
        "Clear Assignee",
    ]

    for idx, action_text in enumerate(action_texts):
        action_card = page.get_by_text(action_text, exact=False).first
        action_drop = page.locator('text=/[Dd]rag and drop/').first

        if idx > 0:
            # Need "Add new Action +" for 2nd+ actions
            add_action = page.locator('text=/Add new Action|Add Action/i')
            if await add_action.count() > 0:
                await add_action.first.click()
                await page.wait_for_timeout(1000)

        if await action_card.count() > 0 and await action_drop.count() > 0:
            await _mouse_drag(page, action_card, action_drop)
            await page.wait_for_timeout(1000)
            await dismiss_popups(page)
            logger.info("Placed action: %s", action_text)
        else:
            logger.warning("Action card '%s' or drop zone not found", action_text)

    # Configure "Change Status" to "Sold"
    # Look for the status dropdown in the action configuration
    status_dropdown = page.locator('[class*="Select"]:has-text("Select")').last
    if await status_dropdown.count() > 0:
        await status_dropdown.click()
        await page.wait_for_timeout(500)
        sold_status = page.get_by_text("Sold", exact=True)
        if await sold_status.count() > 0:
            await sold_status.first.click()
            await page.wait_for_timeout(500)

    await screenshot(page, "sequence_actions_configured")

    # ── Save ──
    save_btn = page.locator('button:has-text("Save Sequence"), button:has-text("Save")')
    if await save_btn.count() > 0:
        await save_btn.first.click()
        await page.wait_for_timeout(3000)

        # Check for duplicate name error
        error_toast = page.locator('text=/different sequence title/i')
        if await error_toast.count() > 0:
            logger.warning("Duplicate name detected, retrying with V2 suffix")
            # Clear and re-enter title
            if await title_input.count() > 0:
                await title_input.first.fill("Sold Property Cleanup V2")
                await page.wait_for_timeout(500)
                await save_btn.first.click()
                await page.wait_for_timeout(3000)

        await dismiss_popups(page)
        await screenshot(page, "sequence_saved")
        logger.info("Sequence created successfully")
        return True

    logger.error("Save button not found")
    return False


def main():
    parser = argparse.ArgumentParser(description="Manage DataSift sequences")
    parser.add_argument("--list", action="store_true", help="List all sequences")
    parser.add_argument("--create-sold-sequence", action="store_true",
                        help="Create the Sold Property Cleanup sequence")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if args.list:
        async def _list():
            async with create_browser(headless=args.headless) as (browser, context, page):
                if not await login(page):
                    print("Login failed")
                    return
                seqs = await list_sequences(page)
                for s in seqs:
                    print(f"  {s['name']}")
                    if s.get("details"):
                        print(f"    {s['details']}")

        asyncio.run(_list())

    elif args.create_sold_sequence:
        async def _create():
            async with create_browser(headless=args.headless) as (browser, context, page):
                if not await login(page):
                    print("Login failed")
                    return
                ok = await create_sold_sequence(page)
                print("Sold Property Cleanup sequence created" if ok else "Failed to create sequence")

        asyncio.run(_create())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
