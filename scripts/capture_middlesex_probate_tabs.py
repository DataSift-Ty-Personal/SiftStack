"""Extend fixture capture — click each detail-page tab and save its callback-rendered HTML.

The Middlesex detail page tabs (Tab_case, Tab_parties, Tab_assets, TAB_ORDER,
Tab_Image, Tab_Doc) are lazy-loaded via DevExpress ASPxCallback. The initial
GET HTML only contains Tab_case + Tab_parties bodies. Everything else needs a
tab-click to fire the callback and render its content into the DOM.

This script loads every detail_url from the existing manifest, clicks each
non-case tab in turn, waits for the ASPxCallback to complete, and saves the
resulting `document.documentElement.outerHTML` per (case, tab) pair. That lets
us grep for the executor's address across every tab's rendered body without
guessing at the callback protocol.

Usage:
    python scripts/capture_middlesex_probate_tabs.py
    python scripts/capture_middlesex_probate_tabs.py --only=2,3   # subset of fixtures
    python scripts/capture_middlesex_probate_tabs.py --headed     # already default

Output:
    tests/fixtures/middlesex_probate_detail_{i}_tab_{name}.html
    tests/fixtures/middlesex_probate_tab_capture_manifest.json
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from nj_middlesex_probate import USER_AGENT  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Tab names as they appear in the DevExpress tab strip. Skip Tab_case (already
# rendered) and Tab_parties (also already rendered). Keep display-string case:
# the strip shows "Parties", "Assets", "Order", "Image", "Documents".
TABS_TO_EXERCISE = [
    ("Tab_assets", "Assets"),
    ("TAB_ORDER", "Order"),
    ("Tab_Image", "Image"),
    ("Tab_Doc", "Documents"),
]


async def click_tab_and_wait(page, tab_display_name: str) -> tuple[bool, str]:
    """Click a tab by its visible span text, wait for the ASPxCallback to settle.

    Returns (success, message). We wait for network idle briefly then look for
    the ASPxLoadingPanel to disappear (DevExpress overlays it during callback).
    """
    # The tab-strip anchor: <a class="dxtc-link"><span class="dx-vam">Parties</span></a>
    tab = page.locator(f'a.dxtc-link:has(span:text-is("{tab_display_name}"))').first
    if await tab.count() == 0:
        return False, f'tab anchor for "{tab_display_name}" not found'
    try:
        await tab.click(timeout=6000)
    except Exception as e:
        return False, f'click failed: {e}'
    # DevExpress overlays a loading panel; wait for it to hide.
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
    # Also give the tab body a moment to finish stitching content.
    await page.wait_for_timeout(1200)
    return True, "ok"


async def process_case(page, entry: dict) -> dict:
    idx = entry["index"]
    url = entry["detail_url"]
    print(f"\n  detail_{idx} pk={entry['pk_id']} ({entry.get('grid_row_decedent', '')})")
    result = {"index": idx, "pk_id": entry["pk_id"], "tabs": {}}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        print(f"    load failed: {e}")
        result["load_error"] = str(e)
        return result

    for tab_id, tab_display in TABS_TO_EXERCISE:
        ok, msg = await click_tab_and_wait(page, tab_display)
        status = "ok" if ok else f"ERR({msg})"
        html = await page.content()
        out = FIXTURE_DIR / f"middlesex_probate_detail_{idx}_tab_{tab_id}.html"
        out.write_text(html, encoding="utf-8")
        result["tabs"][tab_id] = {
            "display_name": tab_display,
            "click_status": status,
            "file": out.name,
            "html_bytes": len(html),
        }
        print(f"    tab={tab_id:<12} click={status:<25} saved={out.name} ({len(html):,} B)")

    return result


async def main_async(only_indices: set[int] | None, headless: bool) -> None:
    manifest_in = FIXTURE_DIR / "middlesex_probate_manifest.json"
    if not manifest_in.exists():
        print(f"missing {manifest_in} — run capture_middlesex_probate_fixture.py first",
              file=sys.stderr)
        sys.exit(1)
    m = json.loads(manifest_in.read_text())
    details = m["details"]
    if only_indices:
        details = [d for d in details if d["index"] in only_indices]
    print(f"Exercising tabs on {len(details)} case(s)")

    out_manifest: dict = {
        "source_manifest": manifest_in.name,
        "cases": [],
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await ctx.new_page()
        for entry in details:
            case_result = await process_case(page, entry)
            out_manifest["cases"].append(case_result)
        await browser.close()

    out_path = FIXTURE_DIR / "middlesex_probate_tab_capture_manifest.json"
    out_path.write_text(json.dumps(out_manifest, indent=2), encoding="utf-8")
    print(f"\nManifest: {out_path}")


def main() -> None:
    only_indices: set[int] | None = None
    headless = False
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only_indices = {int(s) for s in arg.split("=", 1)[1].split(",") if s.strip()}
        elif arg == "--headless":
            headless = True
        elif arg == "--headed":
            headless = False
        else:
            print(f"unknown arg: {arg}", file=sys.stderr)
            sys.exit(2)
    asyncio.run(main_async(only_indices, headless))


if __name__ == "__main__":
    main()
