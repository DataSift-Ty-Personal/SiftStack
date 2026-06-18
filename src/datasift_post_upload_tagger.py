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

from playwright.async_api import Page

from datasift_core import dismiss_popups as _dismiss_popups, screenshot as _screenshot

logger = logging.getLogger(__name__)


# Mirrors the type → tag map in datasift_formatter._build_tags
NOTICE_TYPE_TO_FTM_TAG = {
    "foreclosure": "ftm-ss",
    "tax_foreclosure": "ftm-tf",
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
    "tax_foreclosure": "Tax Foreclosure",
    "probate": "Probate",
    "tax_sale": "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "eviction": "Eviction",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
}


async def _kill_stale_overlays(page: Page) -> None:
    """Disable pointer-event interception on stale overlays without removing
    them — asideOverlay IS the filter panel's container, so deleting it
    removes the panel itself (broke the first patched run).

    Strategy:
      - asideOverlay → set pointer-events:none so it doesn't intercept clicks
        meant for elements behind it; React state stays intact
      - Beamer / NPS modals → remove (these have no functional content we need)
    """
    try:
        await page.evaluate(
            """
            () => {
                document.querySelectorAll('#asideOverlay').forEach(el => {
                    el.style.pointerEvents = 'none';
                });
                document.querySelectorAll('#beamerPushModal, #npsIframeContainer').forEach(el => el.remove());
            }
            """
        )
    except Exception:
        pass


async def _dismiss_autocomplete(page: Page) -> None:
    """Dismiss any open autocomplete dropdown without removing it from DOM.

    Used between sequential filter-block setups so the next field's click
    isn't intercepted by the previous field's still-rendered suggestions.
    """
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def _open_filter_panel(page: Page) -> bool:
    """Click 'Filter Records' to open the right-side filter overlay.

    Hardened against the asideOverlay-intercepts-clicks failure mode that
    blocked v3's first run on Apify (5 of 5 buckets failed). Strategy:
      1. Dismiss popups + kill overlays from prior interactions
      2. Try Playwright click with force=True (bypasses pointer-event check)
      3. If that fails, fall back to JS click via element handle
    """
    await _dismiss_popups(page)
    await _kill_stale_overlays(page)

    filter_link = page.locator('#Records__Filters_Trigger')
    if await filter_link.count() == 0:
        filter_link = page.locator('a:has-text("Filter Records")')
    if await filter_link.count() == 0:
        logger.warning("No Filter Records link found")
        return False

    # Attempt 1: Playwright force-click (bypasses pointer-event interception)
    try:
        await filter_link.first.click(force=True, timeout=5000)
        await page.wait_for_timeout(2000)
        await _dismiss_popups(page)
        return True
    except Exception as e:
        logger.warning("Force-click on filter trigger failed: %s — trying JS click", e)

    # Attempt 2: JS click via element evaluate
    try:
        await filter_link.first.evaluate("el => el.click()")
        await page.wait_for_timeout(2000)
        await _dismiss_popups(page)
        return True
    except Exception as e:
        logger.warning("JS click on filter trigger also failed: %s", e)
        return False


async def _add_filter_block(page: Page, block_name: str) -> bool:
    """Type into the filter-block search and click the matching block name.

    Examples of block_name: 'All Lists (AND)', 'All Tags (AND)',
    'Notice Type' (custom field), 'Property County' (custom field).
    """
    # Inside the active panel — DO NOT touch asideOverlay (the panel itself).
    # Just dismiss any leftover autocomplete from a previous filter block.
    await _dismiss_autocomplete(page)

    search = page.locator('#RecordsFilters__Filter_Blocks__Search')
    if await search.count() == 0:
        search = page.locator('input[placeholder*="filter block"]')
    if await search.count() == 0:
        logger.warning("Filter block search input not found")
        return False

    # Force-click + JS-fill bypasses InputSuggestionsContainer interception
    try:
        await search.first.click(force=True, timeout=5000)
    except Exception:
        try:
            await search.first.evaluate("el => el.focus()")
        except Exception:
            pass
    await search.first.fill("")
    await page.wait_for_timeout(300)
    await search.first.fill(block_name.split()[0])  # e.g. "Notice" for "Notice Type"
    await page.wait_for_timeout(1500)

    option = page.locator(f'text="{block_name}"')
    if await option.count() > 0:
        try:
            await option.first.click(force=True, timeout=5000)
        except Exception:
            try:
                await option.first.evaluate("el => el.click()")
            except Exception as e:
                logger.warning("Failed to click filter block option %r: %s", block_name, e)
                return False
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
    """Click 'Apply Filters' then kill the lingering panel overlay.

    DataSift re-renders #asideOverlay after Apply Filters in a way that
    blocks the records grid below — checkboxes, Manage button, etc. We
    explicitly kill the overlay's pointer-events after applying so the
    grid is clickable again.
    """
    apply_btn = page.locator('text="Apply Filters"')
    applied = False
    if await apply_btn.count() > 0:
        try:
            await apply_btn.first.click(force=True, timeout=5000)
        except Exception:
            try:
                await apply_btn.first.evaluate("el => el.click()")
            except Exception:
                pass
        await page.wait_for_timeout(3000)
        applied = True
    else:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(2000)

    # Critical: kill the post-apply overlay re-render so subsequent
    # records-grid interactions (select all, Manage menu) work.
    await _kill_stale_overlays(page)
    # Press Escape to ensure panel is fully dismissed
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass
    await _kill_stale_overlays(page)
    return applied


async def _clear_filters(page: Page) -> None:
    """Clear all filter blocks (returns Records to default state)."""
    await _dismiss_popups(page)
    await _kill_stale_overlays(page)
    clear_link = page.locator('text="Clear Filters"')
    if await clear_link.count() > 0:
        try:
            await clear_link.first.click(force=True, timeout=5000)
        except Exception:
            try:
                await clear_link.first.evaluate("el => el.click()")
            except Exception:
                pass
        await page.wait_for_timeout(2000)


async def _select_all_records(page: Page) -> bool:
    """Click 'Select all' header checkbox + 'Select all matching' link.

    Hardened with kill-overlays before each click + force-click + JS click
    fallbacks. The post-Apply-Filters asideOverlay re-render blocks plain
    clicks on the records grid.
    """
    await _kill_stale_overlays(page)
    # Header checkbox first — try multiple selectors so we hit the right one
    for sel in [
        'thead input[type="checkbox"]',
        '[class*="HeaderRow"] input[type="checkbox"]',
        'input[type="checkbox"]:visible',
    ]:
        cb = page.locator(sel).first
        if await cb.count() > 0:
            try:
                await cb.click(force=True, timeout=3000)
                await page.wait_for_timeout(800)
                break
            except Exception:
                try:
                    await cb.evaluate("el => el.click()")
                    await page.wait_for_timeout(800)
                    break
                except Exception:
                    continue
    # Then "Select all matching" link
    select_all_link = page.locator('text="Select all"')
    if await select_all_link.count() > 0:
        try:
            await select_all_link.first.click(force=True, timeout=3000)
            await page.wait_for_timeout(1000)
        except Exception:
            try:
                await select_all_link.first.evaluate("el => el.click()")
            except Exception:
                pass
    await _kill_stale_overlays(page)
    return True


async def _open_manage_menu(page: Page) -> bool:
    """Click the Manage dropdown to reveal Add Tag / Add to List options.

    Hardened: kill overlays first, try multiple selectors, force-click +
    JS-click fallbacks. The Manage button only appears AFTER records are
    selected, and asideOverlay can intercept the click.
    """
    await _kill_stale_overlays(page)
    for sel in [
        'button:has-text("Manage")',
        '[class*="Manage"] >> text="Manage"',
        'text="Manage"',
        'a:has-text("Manage")',
    ]:
        btn = page.locator(sel)
        if await btn.count() == 0:
            continue
        try:
            await btn.first.click(force=True, timeout=3000)
            await page.wait_for_timeout(1500)
            return True
        except Exception:
            try:
                await btn.first.evaluate("el => el.click()")
                await page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


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


async def apply_tags_to_buckets(
    page: Page,
    buckets: list[dict],
    hard_timeout_seconds: int = 1500,
) -> dict:
    """v4 split-upload tagger — one bulk-tag pass per bucket (wrapper list).

    Architecture (vs. v3):
      v3 grouped one big upload by (notice_type, county) AFTER upload, then
      filtered records by Notice Type + County custom-field filter blocks.
      Those filter blocks rely on Step 4 column-mapping at upload time —
      which silently fails — so the filters returned 0 records and tagger
      failed every group.

      v4 takes pre-bucketed wrapper lists. Each bucket = one wrapper list
      that we set at upload time. We filter records by wrapper list NAME
      only (built-in filter, always works). Apply tags + add to list in one
      bulk operation per bucket. No custom-field dependency, no Step 4
      dependency.

    Per-bucket flow:
      1. Clear filters → open filter panel
      2. Add 'All Lists (AND)' filter block, set value = bucket["list_name"]
      3. Apply filters → verify filtered_count > 0
      4. Select all → Manage → Add Tag → ftm-{type}, {county}, ftm
      5. Manage → Add to List → notice-type list (Probate / Foreclosure / ...)

    Args:
        page: Logged-in Playwright Page (already past upload).
        buckets: List of bucket descriptors from write_datasift_split_csvs:
            [{"list_name": str, "notice_type": str, "county": str,
              "count": int, ...}, ...]
        hard_timeout_seconds: Abort after this many seconds (default 25 min).

    Returns:
        Dict shape:
          {
            "success": bool,
            "timed_out": bool (optional),
            "elapsed_seconds": float,
            "message": str,
            "groups": [
              {"notice_type", "county", "list_name", "expected_records",
               "filtered_count", "tags_added", "list_added", "verified": bool,
               "error" (optional)}
            ],
            "lists_applied": list of notice-type lists we added to,
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

    if not buckets:
        result["message"] = "No buckets to tag"
        return result

    logger.info(
        "Post-upload tagging v4: %d buckets (one per wrapper list), hard timeout %ds",
        len(buckets), hard_timeout_seconds,
    )

    # Navigate to Records once
    await page.goto("https://app.reisift.io/records/properties", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    lists_applied: set[str] = set()

    for bucket in buckets:
        elapsed = _time.monotonic() - start
        if elapsed > hard_timeout_seconds:
            logger.warning(
                "Tagger v4 HARD TIMEOUT at %ds (budget %ds) — skipping remaining buckets",
                int(elapsed), hard_timeout_seconds,
            )
            result["success"] = False
            result["timed_out"] = True
            break

        list_name = bucket.get("list_name", "")
        notice_type = (bucket.get("notice_type") or "").lower()
        county = (bucket.get("county") or "").lower()
        expected = bucket.get("count", 0)

        ftm_tag = NOTICE_TYPE_TO_FTM_TAG.get(notice_type)
        target_list = NOTICE_TYPE_TO_LIST_NAME.get(notice_type)

        logger.info("── Bucket: %s (%d records) ──", list_name, expected)
        group_result: dict = {
            "notice_type": notice_type,
            "county": county,
            "list_name": list_name,
            "expected_records": expected,
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
                logger.warning("Could not open filter panel for %s — skipping", list_name)
                group_result["error"] = "filter_panel_unopenable"
                result["groups"].append(group_result)
                continue

            # 2. Filter by WRAPPER LIST NAME ONLY (no custom fields)
            if not await _add_filter_block(page, "All Lists (AND)"):
                logger.warning("Could not add Lists filter block for %s — skipping", list_name)
                group_result["error"] = "lists_filter_unaddable"
                result["groups"].append(group_result)
                continue
            if not await _set_list_filter_value(page, list_name):
                logger.warning("Could not set list filter to %r — skipping", list_name)
                group_result["error"] = "list_value_unsettable"
                result["groups"].append(group_result)
                continue

            await _apply_filters(page)
            await _screenshot(page, f"tagger_v4_filtered_{notice_type}_{county}")

            # 3. VERIFY filtered count > 0 before destructive ops
            filtered = await _read_filtered_count(page)
            group_result["filtered_count"] = filtered
            if filtered == 0:
                logger.warning(
                    "❌ %s — filter returned 0 records (expected %d). Wrapper list empty or filter didn't apply.",
                    list_name, expected,
                )
                group_result["error"] = "filter_returned_zero"
                result["success"] = False
                result["groups"].append(group_result)
                continue
            if filtered is not None and abs(filtered - expected) > 1:
                logger.warning(
                    "⚠ %s — filtered %d != expected %d (proceeding)",
                    list_name, filtered, expected,
                )
            group_result["verified"] = filtered is not None and filtered > 0

            # 4. Select all matching records
            await _select_all_records(page)
            await page.wait_for_timeout(1500)

            # 5. Open Manage and bulk-add tags
            if not await _open_manage_menu(page):
                logger.warning("Could not open Manage menu for %s — skipping", list_name)
                group_result["error"] = "manage_menu_unopenable"
                result["groups"].append(group_result)
                continue

            tags_to_add = ["ftm"]
            if ftm_tag:
                tags_to_add.append(ftm_tag)
            if county:
                tags_to_add.append(county)
            added = await _bulk_add_tags(page, tags_to_add)
            group_result["tags_added"] = added
            await page.wait_for_timeout(1500)

            # 6. Add bucket records to notice-type list (Probate / Foreclosure / ...)
            if target_list:
                await _open_manage_menu(page)
                if await _bulk_add_to_list(page, target_list):
                    group_result["list_added"] = True
                    lists_applied.add(target_list)

            result["groups"].append(group_result)

        except Exception as e:
            logger.warning("Tagging failed for bucket %s: %s", list_name, e)
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
