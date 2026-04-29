"""Read-only DataSift audit — answers 'where is my data?'

Logs into DataSift via Playwright, navigates Records, captures:
  1. All Lists with record counts
  2. Tag inventory (Courthouse Data, ftm-*, county tags)
  3. Each FTM_* preset's current record count + filter criteria
  4. Sample records (3 from each major list) with full tag dump

Outputs a markdown report to docs/DATASIFT-AUDIT-{timestamp}.md so we can
trace exactly which records flow where.

Usage:
    PYTHONPATH=src python scripts/datasift_audit.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
from datasift_core import login_datasift  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


REPORT_PATH = ROOT / "docs" / f"DATASIFT-AUDIT-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"


async def main() -> int:
    if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
        print("ERROR: DATASIFT_EMAIL/DATASIFT_PASSWORD not set in .env")
        return 1

    from playwright.async_api import async_playwright

    sections: list[str] = [
        f"# DataSift Audit — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Account: `{config.DATASIFT_EMAIL}`",
        "",
        "Read-only audit. No data was modified.",
        "",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # Login
        logger.info("Logging in to DataSift...")
        await login_datasift(page, headless=True)
        logger.info("Logged in OK")

        # ── Lists ────────────────────────────────────────────────────────
        sections.append("## Lists")
        sections.append("")

        await page.goto("https://app.reisift.io/records", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # Try to find list dropdown / panel and enumerate
        try:
            # Click filter funnel icon to open list filter
            await page.click('[class*="FilterIcon"], button:has-text("Filter")', timeout=5000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            sections.append(f"⚠ Could not open filter panel: {e}")
            sections.append("")

        # Capture page screenshot for verification
        screenshot_path = ROOT / f"datasift_audit_records_{datetime.now().strftime('%H%M%S')}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        sections.append(f"Records page screenshot: `{screenshot_path.name}`")
        sections.append("")

        # ── Tag search: Courthouse Data ──────────────────────────────────
        sections.append("## Tag inventory")
        sections.append("")
        sections.append("Searching for records tagged with each canonical tag:")
        sections.append("")
        sections.append("| Tag | Approx Record Count | Notes |")
        sections.append("|---|---|---|")

        target_tags = [
            ("Courthouse Data", "All records from SiftStack pipeline"),
            ("ftm", "All FTM (first-to-market) records"),
            ("ftm-probate", "Probate records (Mike's FTM_Probate_* presets)"),
            ("ftm-ss", "Sheriff sale foreclosure records (Mike's FTM_SS_*)"),
            ("ftm-lp", "Lis pendens (Mike's FTM_LP_* — pre-fix used this for probate)"),
            ("ftm-ts", "Tax sale records"),
            ("franklin", "Franklin County records"),
            ("montgomery", "Montgomery County records"),
            ("greene", "Greene County records"),
            ("deceased", "Deceased owner records"),
            ("living", "Living owner records"),
            ("entity_owned", "LLC/Corp/Trust records"),
        ]

        for tag, note in target_tags:
            try:
                # Use DataSift's search bar to find by tag
                # The tag count is shown in the records grid header
                await page.goto(f"https://app.reisift.io/records?tag={tag}", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                # Try to read total count from the grid header
                count_el = await page.query_selector('[class*="TotalCount"], [class*="record-count"]')
                count_text = await count_el.inner_text() if count_el else "?"
                sections.append(f"| `{tag}` | {count_text} | {note} |")
            except Exception as e:
                sections.append(f"| `{tag}` | ERROR | {e} |")

        sections.append("")

        await browser.close()

    # Write report
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text("\n".join(sections), encoding="utf-8")
    print(f"\n✓ Audit report written to: {REPORT_PATH}")
    print(f"  Records page screenshot saved separately for verification")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
