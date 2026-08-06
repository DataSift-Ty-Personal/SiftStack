"""One-shot fixture capture: save raw HTML of live Middlesex probate detail pages.

Runs a headed Playwright session against the Middlesex surrogate portal,
searches the last N days of death dates, then saves the raw detail-page HTML
for the first M cases it finds. Also dumps the search-results grid HTML so
we can see the row structure.

No dedup state, no enrichment, no CSV, no upload — just HTML capture.

Usage (from repo root):
    python scripts/capture_middlesex_probate_fixture.py                  # last 14 days, save first 5 details
    python scripts/capture_middlesex_probate_fixture.py --days=30
    python scripts/capture_middlesex_probate_fixture.py --max-details=10
    python scripts/capture_middlesex_probate_fixture.py --headless       # only if headed hits CF

Output:
    tests/fixtures/middlesex_probate_detail_1.html   (first case detail page)
    tests/fixtures/middlesex_probate_detail_2.html   (second, etc)
    tests/fixtures/middlesex_probate_grid.html       (search results grid)
    tests/fixtures/middlesex_probate_manifest.json   (pk_ids + which days they came from)
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from nj_middlesex_probate import (  # noqa: E402
    MIDDLESEX,
    _check_cloudflare_block,
    _parse_grid_rows,
    _submit_day,
    USER_AGENT,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


async def capture(days_back: int, max_details: int, headless: bool) -> dict:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = MIDDLESEX
    today = datetime.now()

    manifest: dict = {
        "captured_at": today.isoformat(),
        "county": cfg.name,
        "days_back": days_back,
        "grid_file": None,
        "details": [],
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await ctx.new_page()

        await page.goto(cfg.search_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        await _check_cloudflare_block(page, cfg.name)

        all_rows: list[dict] = []
        grid_saved = False
        for offset in range(days_back):
            day = today - timedelta(days=offset)
            try:
                rows = await _submit_day(page, day, cfg)
            except Exception as e:
                print(f"  {day:%Y-%m-%d}: submit failed — {e}", file=sys.stderr)
                continue
            if not rows:
                continue
            print(f"  {day:%Y-%m-%d}: {len(rows)} row(s)")
            if not grid_saved:
                grid_path = FIXTURE_DIR / "middlesex_probate_grid.html"
                grid_path.write_text(await page.content(), encoding="utf-8")
                manifest["grid_file"] = grid_path.name
                grid_saved = True
                print(f"    -> saved grid: {grid_path.name}")
            for r in rows:
                r["_from_day"] = day.strftime("%Y-%m-%d")
                all_rows.append(r)
            if len(all_rows) >= max_details:
                break

        if not all_rows:
            print("No probate rows found in window. Try widening --days=.", file=sys.stderr)
            await browser.close()
            return manifest

        # Save first N detail pages by navigating to their stable GET URLs.
        # Using the browser context so we replay any set cookies (CF-friendly).
        for idx, row in enumerate(all_rows[:max_details], start=1):
            url = row["detail_url"]
            pk_id = row["pk_id"]
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)  # let DevExpress hydrate
                html = await page.content()
            except Exception as e:
                print(f"  detail {idx} (pk={pk_id}) failed: {e}", file=sys.stderr)
                continue

            out = FIXTURE_DIR / f"middlesex_probate_detail_{idx}.html"
            out.write_text(html, encoding="utf-8")
            manifest["details"].append({
                "index": idx,
                "pk_id": pk_id,
                "detail_url": url,
                "grid_row_decedent": row.get("decedent_name", ""),
                "from_day": row["_from_day"],
                "file": out.name,
                "html_bytes": len(html),
            })
            print(f"  -> saved detail {idx}: {out.name} (pk={pk_id}, {len(html)} bytes)")

        await browser.close()

    manifest_path = FIXTURE_DIR / "middlesex_probate_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest: {manifest_path}")
    return manifest


def main() -> None:
    days_back = 14
    max_details = 5
    headless = False
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days_back = int(arg.split("=", 1)[1])
        elif arg.startswith("--max-details="):
            max_details = int(arg.split("=", 1)[1])
        elif arg == "--headless":
            headless = True
        elif arg == "--headed":
            headless = False
        else:
            print(f"unknown arg: {arg}", file=sys.stderr)
            sys.exit(2)
    asyncio.run(capture(days_back, max_details, headless))


if __name__ == "__main__":
    main()
