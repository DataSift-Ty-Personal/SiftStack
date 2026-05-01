"""Post-upload tag + list application for DataSift records.

Background
----------
The DataSift upload wizard's Step 4 column mapping for the "Tags" and "Lists"
columns is unreliable — the drag-and-drop against styled-components frequently
silently fails, leaving records uploaded with only the batch tag from Step 2
("Courthouse Data") and missing all per-record tags (`ftm-probate`,
`montgomery`, `2026-04`, etc.) and missing notice-type list assignments
(Probate / Foreclosure / Tax Sale / Lis Pendens).

This module fixes that by applying tags + list assignments AFTER the upload
completes, using DataSift's same Records → Filter → Manage → Add Tag bulk
flow that Mike uses manually. Reliable because:
  - Bulk-tag operates on already-uploaded records via a well-tested UI flow
  - Each operation is discrete + verifiable in the page state
  - Independent of the brittle Step 4 drag-and-drop

Workflow per call
-----------------
For each (notice_type, county) combination present in the uploaded records:
  1. Filter Records: wrapper_list AND custom_field "Notice Type" = notice_type
  2. Select all matching
  3. Manage → Add Tag → bulk-apply: `ftm`, `ftm-{type-code}`, `{county-lower}`,
     `{YYYY-MM}`, plus deceased/living tag for that subset
  4. Manage → Add to List → add to corresponding notice-type list
     (Probate / Foreclosure / Tax Sale / Lis Pendens)

After this runs, Mike's `FTM_*_County` presets that filter on the same tags
will pick up the records correctly.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from playwright.async_api import Page

from datasift_core import dismiss_popups as _dismiss_popups, screenshot as _screenshot
from models import NoticeData

logger = logging.getLogger(__name__)


# Mirrors the type → tag map in datasift_formatter._build_tags
NOTICE_TYPE_TO_FTM_TAG = {
    "foreclosure": "ftm-ss",
    "tax_sale": "ftm-ts",
    "tax_delinquent": "ftm-lp",
    "probate": "ftm-probate",
    "lis_pendens": "ftm-lp",
    "eviction": "ftm-eviction",
    "code_violation": "ftm-cv",
    "divorce": "ftm-divorce",
}

# Mirrors NOTICE_TYPE_TO_LIST in datasift_formatter
NOTICE_TYPE_TO_LIST_NAME = {
    "foreclosure": "Foreclosure",
    "lis_pendens": "Lis Pendens",
    "probate": "Probate",
    "tax_sale": "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "eviction": "Eviction",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
}


async def _open_filter_panel(page: Page) -> bool:
    """Click 'Filter Records' to open the right-side filter overlay."""
    await _dismiss_popups(page)
    filter_link = page.locator('#Records__Filters_Trigger')
    if await filter_link.count() == 0:
        filter_link = page.locator('a:has-text("Filter Records")')
    if await filter_link.count() == 0:
        logger.warning("No Filter Records link found")
        return False
    await filter_link.first.click()
    await page.wait_for_timeout(2000)
    await _dismiss_popups(page)
    return True


async def _add_filter_block(page: Page, block_name: str) -> bool:
    """Type into the filter-block search and click the matching block name.

    Examples of block_name: 'All Lists (AND)', 'All Tags (AND)',
    'Notice Type' (custom field), 'Property County' (custom field).
    """
    search = page.locator('#RecordsFilters__Filter_Blocks__Search')
    if await search.count() == 0:
        search = page.locator('input[placeholder*="filter block"]')
    if await search.count() == 0:
        logger.warning("Filter block search input not found")
        return False

    await search.first.click()
    await search.first.fill("")
    await page.wait_for_timeout(300)
    await search.first.fill(block_name.split()[0])  # e.g. "Notice" for "Notice Type"
    await page.wait_for_timeout(1500)

    option = page.locator(f'text="{block_name}"')
    if await option.count() > 0:
        await option.first.click()
        await page.wait_for_timeout(1500)
        logger.debug("Added filter block: %s", block_name)
        return True
    logger.warning("Filter block %r not found in dropdown", block_name)
    return False


async def _set_list_filter_value(page: Page, list_name: str) -> bool:
    """In an 'All Lists' filter block, type the list name and click it."""
    inp = page.locator('input[placeholder*="Search for lists"]').last
    if await inp.count() == 0:
        return False
    await inp.click()
    await inp.fill(list_name)
    await page.wait_for_timeout(1500)
    option = page.locator(f'text="{list_name}"').last
    if await option.count() > 0:
        await option.click()
        await page.wait_for_timeout(1000)
        return True
    return False


async def _set_text_filter_value(page: Page, value: str, block_label: str) -> bool:
    """Set text value on a custom-field filter block (e.g., Notice Type, County).

    Searches for the most-recently-added empty text input within a filter block
    matching block_label.
    """
    # Custom field filter blocks have a text input where you type the value.
    # We find the input by looking for placeholder text like "Enter property status..."
    # which varies — easier to find the LAST visible text input in the panel.
    inputs = page.locator('aside input[type="text"]:visible, [class*="Filter"] input[type="text"]:visible')
    count = await inputs.count()
    # The most recently added filter block's input is typically the last one
    if count == 0:
        return False
    last_input = inputs.nth(count - 1)
    await last_input.click()
    await last_input.fill(value)
    await page.wait_for_timeout(800)
    # Press Enter or click + button to commit the value
    await last_input.press("Enter")
    await page.wait_for_timeout(500)
    return True


async def _apply_filters(page: Page) -> bool:
    """Click 'Apply Filters' button at the bottom of the filter panel."""
    apply_btn = page.locator('text="Apply Filters"')
    if await apply_btn.count() > 0:
        await apply_btn.first.click()
        await page.wait_for_timeout(3000)
        return True
    # Fallback: close panel with Escape
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(2000)
    return False


async def _clear_filters(page: Page) -> None:
    """Clear all filter blocks (returns Records to default state)."""
    await _dismiss_popups(page)
    clear_link = page.locator('text="Clear Filters"')
    if await clear_link.count() > 0:
        await clear_link.first.click()
        await page.wait_for_timeout(2000)


async def _select_all_records(page: Page) -> bool:
    """Click the 'Select all' header checkbox + 'Select all matching filter' link."""
    # Header checkbox first
    header_box = page.locator('input[type="checkbox"]:visible').first
    if await header_box.count() > 0:
        try:
            await header_box.click()
            await page.wait_for_timeout(800)
        except Exception:
            pass
    # Then click "Select all matching filter" link if present
    select_all_link = page.locator('text="Select all"')
    if await select_all_link.count() > 0:
        try:
            await select_all_link.first.click()
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            pass
    return True  # header checkbox click counts as success


async def _open_manage_menu(page: Page) -> bool:
    """Click the Manage dropdown to reveal Add Tag / Add to List options."""
    btn = page.locator('button:has-text("Manage")')
    if await btn.count() == 0:
        btn = page.locator('text="Manage"')
    if await btn.count() == 0:
        return False
    await btn.first.click()
    await page.wait_for_timeout(1500)
    return True


async def _bulk_add_tags(page: Page, tags: list[str]) -> int:
    """After Manage opened, click 'Add Tag' / 'Tag Records' and add each tag.

    Returns number of tags successfully added.
    """
    # Open the Add Tag option
    for opt_text in ['"Add Tag"', '"Tag Records"', '"Add Tags"']:
        opt = page.locator(f'text={opt_text}')
        if await opt.count() > 0:
            await opt.first.click()
            await page.wait_for_timeout(2000)
            break
    else:
        logger.warning("Add Tag option not found in Manage menu")
        return 0

    added = 0
    for tag in tags:
        try:
            tag_input = page.locator(
                'input[placeholder*="Search or add"], input[placeholder*="tag"]'
            ).last
            if await tag_input.count() == 0:
                logger.warning("Tag input not visible for %r", tag)
                continue
            await tag_input.click()
            await tag_input.fill(tag)
            await page.wait_for_timeout(1200)
            # Try to click the matching option in autocomplete dropdown
            option = page.locator(f'text="{tag}"').last
            opt_count = await option.count()
            clicked = False
            if opt_count >= 1:
                try:
                    await option.click()
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                # Press Enter to create new tag
                await tag_input.press("Enter")
            await page.wait_for_timeout(800)
            added += 1
            logger.info("  + tag: %s", tag)
        except Exception as e:
            logger.warning("Failed to add tag %r: %s", tag, e)

    # Confirm / submit the tag-add modal
    for btn_text in ['"Save"', '"Apply"', '"Confirm"', '"Add"']:
        btn = page.locator(f'button:has-text({btn_text})')
        if await btn.count() > 0:
            try:
                await btn.first.click()
                await page.wait_for_timeout(2000)
                break
            except Exception:
                continue
    return added


async def _bulk_add_to_list(page: Page, target_list: str) -> bool:
    """After Manage opened, click 'Add to List' and select target_list."""
    for opt_text in ['"Add to List"', '"Add to Lists"', '"Add Records to List"']:
        opt = page.locator(f'text={opt_text}')
        if await opt.count() > 0:
            await opt.first.click()
            await page.wait_for_timeout(2000)
            break
    else:
        logger.warning("Add to List option not found in Manage menu")
        return False

    # Type list name and select
    list_input = page.locator(
        'input[placeholder*="Search or add a new list"], input[placeholder*="list"]'
    ).last
    if await list_input.count() == 0:
        logger.warning("List input not visible")
        return False
    await list_input.click()
    await list_input.fill(target_list)
    await page.wait_for_timeout(1500)
    option = page.locator(f'text="{target_list}"').last
    if await option.count() >= 1:
        try:
            await option.click()
            await page.wait_for_timeout(1000)
        except Exception:
            await list_input.press("Enter")
    else:
        await list_input.press("Enter")

    # Confirm
    for btn_text in ['"Save"', '"Add"', '"Apply"']:
        btn = page.locator(f'button:has-text({btn_text})')
        if await btn.count() > 0:
            try:
                await btn.first.click()
                await page.wait_for_timeout(2000)
                logger.info("  + list: %s", target_list)
                return True
            except Exception:
                continue
    return False


async def _read_filtered_count(page: Page) -> int | None:
    """Read the visible record count after filters are applied.

    DataSift renders a count near the top of Records like "Records (47)" or
    "47 records". Returns None if the count couldn't be parsed — caller
    should treat that as "unverified" rather than zero.
    """
    import re as _re
    candidates = [
        '[class*="RecordsCount"]',
        '[class*="ResultsCount"]',
        '[class*="TotalCount"]',
        'h1, h2, h3',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        count = await loc.count()
        for i in range(min(count, 5)):
            try:
                txt = (await loc.nth(i).inner_text()).strip()
            except Exception:
                continue
            m = _re.search(r"(\d{1,6})", txt)
            if m and ("record" in txt.lower() or "result" in txt.lower() or "(" in txt):
                try:
                    return int(m.group(1))
                except ValueError:
                    continue
    return None


async def apply_tags_and_lists_to_uploaded_records(
    page: Page,
    wrapper_list_name: str,
    notices: list[NoticeData],
    hard_timeout_seconds: int = 1500,
) -> dict:
    """For each (notice_type, county) in notices, bulk-apply tags + add to list.

    v3 (2026-05-01) — restores county tag (v2 wrongly dropped it; Mike's
    FTM_*_County presets filter on county tag) + adds per-group verification.

      * Iterates per (notice_type, county) — up to 12 groups (4 types × 3
        counties). Each group ~2-3 min, total ~30 min for full daily batch.
      * Tags applied per group: `ftm`, `ftm-{type}`, `{county-lower}`. Notice
        type also adds the record to its notice-type list (Probate /
        Foreclosure / Lis Pendens) — once per type, not once per (type,county).
      * Per-group verification: reads the visible filtered-record count BEFORE
        bulk-tagging. If 0 (or unreadable), logs LOUD warning and skips the
        bulk-tag for that group — prevents silent "tagged nothing" failures.
      * Hard wall-clock timeout (default 25 min). Aborts remaining groups if
        we run past budget. Result includes `timed_out=True`.
      * Failure mode is loud but non-fatal: log warning, return partial result
        so caller can post per-group breakdown to Slack.

    Why county tag matters: Mike's presets filter on `Tag IS ftm-probate AND
    Tag IS franklin` etc. Without the county tag, records show in 0 presets.
    This is the routing bug that broke daily SMS cadences.

    Args:
        page: Logged-in Playwright Page (already past upload).
        wrapper_list_name: The dated wrapper list to filter by.
        notices: NoticeData records — drives which (type, county) groups run.
        hard_timeout_seconds: Abort after this many seconds (default 1500 = 25 min).

    Returns:
        Dict shape:
          {
            "success": bool,
            "timed_out": bool (optional),
            "elapsed_seconds": float,
            "message": str,
            "groups": [
              {"notice_type", "county", "expected_records", "filtered_count",
               "tags_added", "list_added", "verified": bool, "error" (optional)}
            ],
            "lists_applied": set of notice_types we added to their list,
          }
    """
    import time as _time

    result: dict = {
        "success": True,
        "groups": [],
        "message": "",
        "elapsed_seconds": 0,
        "lists_applied": [],
    }
    start = _time.monotonic()

    if not notices:
        result["message"] = "No notices to tag"
        return result

    # Group by (notice_type, county) — restores per-county routing.
    group_counts: dict[tuple[str, str], int] = defaultdict(int)
    for n in notices:
        nt = (n.notice_type or "").strip().lower()
        county = (n.county or "").strip().lower()
        if nt and county:
            group_counts[(nt, county)] += 1

    logger.info(
        "Post-upload tagging v3: %d (type, county) groups across %d records in %s "
        "(hard timeout: %ds)",
        len(group_counts), len(notices), wrapper_list_name, hard_timeout_seconds,
    )

    # Navigate to Records once
    await page.goto("https://app.reisift.io/records/properties", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    lists_applied: set[str] = set()  # only add-to-list once per notice_type

    for (notice_type, county), count in sorted(group_counts.items()):
        elapsed = _time.monotonic() - start
        if elapsed > hard_timeout_seconds:
            logger.warning(
                "Post-upload tagger HARD TIMEOUT at %ds (budget %ds) — skipping remaining groups",
                int(elapsed), hard_timeout_seconds,
            )
            result["success"] = False
            result["timed_out"] = True
            break

        ftm_tag = NOTICE_TYPE_TO_FTM_TAG.get(notice_type)
        target_list = NOTICE_TYPE_TO_LIST_NAME.get(notice_type)

        logger.info("── Group: %s / %s (%d records) ──", notice_type, county, count)
        group_result: dict = {
            "notice_type": notice_type,
            "county": county,
            "expected_records": count,
            "filtered_count": None,
            "tags_added": 0,
            "list_added": False,
            "verified": False,
        }

        try:
            # 1. Reset filters + open panel
            await _clear_filters(page)
            opened = await _open_filter_panel(page)
            if not opened:
                logger.warning("Could not open filter panel for %s/%s — skipping", notice_type, county)
                group_result["error"] = "filter_panel_unopenable"
                result["groups"].append(group_result)
                continue

            # 2. Filter by wrapper list AND Notice Type AND County
            await _add_filter_block(page, "All Lists (AND)")
            await _set_list_filter_value(page, wrapper_list_name)
            await _add_filter_block(page, "Notice Type")
            await _set_text_filter_value(page, notice_type, "Notice Type")
            await _add_filter_block(page, "County")
            await _set_text_filter_value(page, county, "County")
            await _apply_filters(page)
            await _screenshot(page, f"tagger_v3_filtered_{notice_type}_{county}")

            # 3. VERIFY filtered count > 0 before doing anything destructive
            filtered = await _read_filtered_count(page)
            group_result["filtered_count"] = filtered
            if filtered == 0:
                logger.warning(
                    "❌ %s/%s — filter returned 0 records (expected %d). Filters didn't apply. Skipping bulk-tag.",
                    notice_type, county, count,
                )
                group_result["error"] = "filter_returned_zero"
                result["success"] = False
                result["groups"].append(group_result)
                continue
            if filtered is not None and filtered != count:
                logger.warning(
                    "⚠ %s/%s — filtered count %d != expected %d (proceeding anyway)",
                    notice_type, county, filtered, count,
                )
            group_result["verified"] = filtered is not None and filtered > 0

            # 4. Select all matching records
            await _select_all_records(page)
            await page.wait_for_timeout(1500)

            # 5. Open Manage and bulk-add tag set (with county!)
            if not await _open_manage_menu(page):
                logger.warning("Could not open Manage menu for %s/%s — skipping", notice_type, county)
                group_result["error"] = "manage_menu_unopenable"
                result["groups"].append(group_result)
                continue

            tags_to_add = ["ftm"]
            if ftm_tag:
                tags_to_add.append(ftm_tag)
            tags_to_add.append(county)  # restored: drives FTM_*_County preset filters
            added = await _bulk_add_tags(page, tags_to_add)
            group_result["tags_added"] = added
            await page.wait_for_timeout(1500)

            # 6. Add to notice-type list (only once per type — first county hit)
            if target_list and notice_type not in lists_applied:
                await _open_manage_menu(page)
                if await _bulk_add_to_list(page, target_list):
                    group_result["list_added"] = True
                    lists_applied.add(notice_type)

            result["groups"].append(group_result)

        except Exception as e:
            logger.warning("Tagging failed for %s/%s: %s", notice_type, county, e)
            group_result["error"] = str(e)
            result["groups"].append(group_result)
            result["success"] = False

    result["lists_applied"] = sorted(lists_applied)
    result["elapsed_seconds"] = round(_time.monotonic() - start, 1)

    # Summary message highlights any group that failed verification
    failed = [g for g in result["groups"] if g.get("error") or not g.get("verified")]
    if failed:
        result["message"] = (
            f"Post-upload tagging v3: {len(result['groups'])} groups, "
            f"{len(failed)} failed/unverified in {result['elapsed_seconds']}s"
        )
    else:
        result["message"] = (
            f"Post-upload tagging v3: {len(result['groups'])} groups OK in {result['elapsed_seconds']}s"
        )
    logger.info(result["message"])
    return result
