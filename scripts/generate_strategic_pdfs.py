"""Convert MASTER-PLAN.md and AARON-PATH-OFF-W2.md to PDFs.

Reuses the rendering pipeline from generate_sop_pdfs.py. Separate script
so the SOP generator doesn't pick up partner-confidential docs.

Usage: python scripts/generate_strategic_pdfs.py
Output: docs/pdf/MASTER-PLAN.pdf, docs/pdf/AARON-PATH-OFF-W2.pdf
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_sop_pdfs import md_to_html, PDF_DIR, DOCS  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


TARGETS = ["MASTER-PLAN.md", "AARON-PATH-OFF-W2.md"]


async def convert_all() -> int:
    PDF_DIR.mkdir(exist_ok=True)
    paths = [DOCS / name for name in TARGETS]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"Missing: {[p.name for p in missing]}")
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        for md in paths:
            pdf = PDF_DIR / md.with_suffix(".pdf").name
            html = md_to_html(md)
            await page.set_content(html, wait_until="load")
            await page.pdf(
                path=str(pdf),
                format="Letter",
                margin={"top": "0.6in", "right": "0.6in", "bottom": "0.6in", "left": "0.6in"},
                print_background=True,
            )
            size_kb = pdf.stat().st_size / 1024
            print(f"  ✓ {md.name:30} → {pdf.name:30} ({size_kb:.0f} KB)")

        await browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(convert_all()))
