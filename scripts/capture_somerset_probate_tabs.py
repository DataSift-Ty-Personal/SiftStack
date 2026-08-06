"""Somerset probate parties-tab re-test.

The code comment in nj_middlesex_probate.py (line 587-591) claims the Somerset
parties tab is login-gated. That claim was made months ago and has never been
retested. Middlesex and Somerset run the same Bluestone platform, so if
Middlesex's Parties tab loads without login (verified 2026-08 — see saved
fixtures), Somerset's likely does too.

Somerset flow differs from Middlesex:
  - No stable detail-URL — rows expose postback buttons only, so we can't
    just GET the detail page. We reuse Somerset's existing search flow:
    submit the range filter, click a row's "View Details" ASPxButton via
    __doPostBack, wait for the detail panel to render, click the Parties
    tab, then save the resulting HTML.

Usage (from repo root):
    python scripts/capture_somerset_probate_tabs.py                # last 10 days, save 3 cases
    python scripts/capture_somerset_probate_tabs.py --days-back=30
    python scripts/capture_somerset_probate_tabs.py --max-cases=5
    python scripts/capture_somerset_probate_tabs.py --headless     # only if headed trips CF

Output:
    tests/fixtures/somerset_probate_detail_{i}.html               (initial detail state)
    tests/fixtures/somerset_probate_detail_{i}_tab_parties.html   (after clicking Parties)
    tests/fixtures/somerset_probate_capture_manifest.json
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from nj_middlesex_probate import (  # noqa: E402
    SOMERSET,
    USER_AGENT,
    _check_cloudflare_block,
    _parse_grid_rows,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


async def click_tab_and_wait(page, tab_display_name: str) -> tuple[bool, str]:
    tab = page.locator(f'a.dxtc-link:has(span:text-is("{tab_display_name}"))').first
    if await tab.count() == 0:
        return False, f'tab anchor for "{tab_display_name}" not found'
    try:
        await tab.click(timeout=6000)
    except Exception as e:
        return False, f'click failed: {e}'
    try:
        await page.wait_for_function(
            """() => {
                const p = document.querySelector('#ContentPlaceHolder1_ASPxLoadingPanel1');
                return !p || p.style.display === 'none' || p.offsetParent === null;
            }""",
            timeout=8000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(1500)
    return True, "ok"


async def run(days_back: int, max_cases: int, headless: bool) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = SOMERSET

    # Range preset — Somerset's date filter is a dropdown, not a single day.
    if days_back <= 5:
        range_label = "Last 5 Days"
    elif days_back <= 10:
        range_label = "Last 10 Days"
    else:
        range_label = "Last 30 Days"

    manifest: dict = {"county": cfg.name, "cases": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await ctx.new_page()

        await page.goto(cfg.search_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        await _check_cloudflare_block(page, cfg.name)

        # Reveal the date filter section
        await page.click(cfg.show_dates_button_selector, timeout=8000)
        await page.wait_for_timeout(1200)

        # Date Type = File Date
        await page.locator('[id$="search_create_or_issue_date_B-1"]').first.click()
        await page.wait_for_timeout(700)
        await page.locator(
            '[id$="search_create_or_issue_date_DDD_L_LBT"] td:has-text("File Date")'
        ).first.click()
        await page.wait_for_timeout(700)

        # Range = Last N Days
        await page.locator('[id$="search_entry_date_range_B-1"]').first.click()
        await page.wait_for_timeout(700)
        await page.locator(
            f'[id$="search_entry_date_range_DDD_L_LBT"] td:has-text("{range_label}")'
        ).first.click()
        await page.wait_for_timeout(700)

        # Submit
        await page.click(cfg.search_button_selector)
        await page.wait_for_timeout(5000)

        # Snapshot the initial grid
        (FIXTURE_DIR / "somerset_probate_grid.html").write_text(
            await page.content(), encoding="utf-8"
        )

        html = await page.content()
        rows = _parse_grid_rows(html, cfg, require_detail_link=False)
        if not rows:
            print("No rows returned — try widening --days-back", file=sys.stderr)
            await browser.close()
            return
        print(f"Somerset: {len(rows)} row(s) — will exercise first {min(max_cases, len(rows))}")

        for i, row in enumerate(rows[:max_cases], start=1):
            ridx = row["row_idx"]
            decedent = row.get("decedent_name", "(unknown)")
            print(f"\n  row {ridx} ({decedent}):")

            # Fire the row's detail postback (mirrors _somerset_extract_records)
            cnt = await page.locator(
                f'input[id$="cell{ridx}_29_ASPxButtonViewDAta_{ridx}_I"]'
            ).count()
            if cnt == 0:
                print(f"    detail button input not in DOM — skipping")
                continue
            try:
                target = await page.evaluate(
                    """(ridx) => {
                        const input = document.querySelector(
                            `input[id$="cell${ridx}_29_ASPxButtonViewDAta_${ridx}_I"]`
                        );
                        if (!input) return null;
                        const name = input.getAttribute('name');
                        if (!name) return null;
                        if (typeof __doPostBack === 'function') {
                            __doPostBack(name, '');
                            return name;
                        }
                        return null;
                    }""",
                    ridx,
                )
                if not target:
                    print("    __doPostBack target not found")
                    continue
                await page.wait_for_timeout(3500)
            except Exception as e:
                print(f"    postback failed: {e}")
                continue

            # Save the initial detail state
            initial = FIXTURE_DIR / f"somerset_probate_detail_{i}.html"
            initial.write_text(await page.content(), encoding="utf-8")
            print(f"    saved initial: {initial.name}")

            # Click the Parties tab — the whole point of the test
            ok, msg = await click_tab_and_wait(page, "Parties")
            parties_status = "ok" if ok else f"ERR({msg})"
            parties = FIXTURE_DIR / f"somerset_probate_detail_{i}_tab_parties.html"
            parties.write_text(await page.content(), encoding="utf-8")
            print(f"    Parties tab click: {parties_status} — saved: {parties.name}")

            manifest["cases"].append({
                "index": i,
                "row_idx": ridx,
                "decedent_grid_row": decedent,
                "initial_file": initial.name,
                "parties_file": parties.name,
                "parties_click_status": parties_status,
            })

            # Return to the results grid so the next row's button is present.
            try:
                await page.locator('[id$="ASPxPageControl_Search_hit_T0T"]').first.click(timeout=5000)
                await page.wait_for_timeout(1500)
            except Exception:
                print("    couldn't return to results — stopping")
                break

        await browser.close()

    manifest_path = FIXTURE_DIR / "somerset_probate_capture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest: {manifest_path}")


def main() -> None:
    days_back = 10
    max_cases = 3
    headless = False
    for arg in sys.argv[1:]:
        if arg.startswith("--days-back="):
            days_back = int(arg.split("=", 1)[1])
        elif arg.startswith("--max-cases="):
            max_cases = int(arg.split("=", 1)[1])
        elif arg == "--headless":
            headless = True
        elif arg == "--headed":
            headless = False
        else:
            print(f"unknown arg: {arg}", file=sys.stderr)
            sys.exit(2)
    asyncio.run(run(days_back, max_cases, headless))


if __name__ == "__main__":
    main()
