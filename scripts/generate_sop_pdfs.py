"""Convert all SOP markdown docs to PDFs via Playwright (Chromium print).

Usage: python scripts/generate_sop_pdfs.py
Output: docs/pdf/SOP-*.pdf
"""

import asyncio
import sys
from pathlib import Path

import markdown
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PDF_DIR = DOCS / "pdf"

CSS_STYLES = """
@page { size: Letter; margin: 0.6in; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.5;
    color: #222;
    max-width: 7.2in;
    margin: 0 auto;
}
h1 { font-size: 22pt; color: #111; margin-top: 0; padding-bottom: 8pt; border-bottom: 2pt solid #333; }
h2 { font-size: 16pt; color: #222; margin-top: 18pt; padding-bottom: 4pt; border-bottom: 1pt solid #ccc; page-break-after: avoid; }
h3 { font-size: 13pt; color: #333; margin-top: 14pt; page-break-after: avoid; }
h4 { font-size: 11pt; color: #444; margin-top: 10pt; }
p { margin: 6pt 0; }
ul, ol { margin: 6pt 0; padding-left: 22pt; }
li { margin: 2pt 0; }
table { border-collapse: collapse; margin: 10pt 0; width: 100%; font-size: 9.5pt; page-break-inside: avoid; }
th, td { border: 0.5pt solid #999; padding: 5pt 8pt; text-align: left; vertical-align: top; }
th { background: #eaeaea; font-weight: 600; }
code { background: #f0f0f0; padding: 1pt 4pt; border-radius: 2pt; font-family: "SF Mono", Monaco, Menlo, monospace; font-size: 9pt; }
pre { background: #f5f5f5; padding: 8pt 10pt; border-radius: 3pt; overflow-x: auto; page-break-inside: avoid; }
pre code { background: transparent; padding: 0; font-size: 8.5pt; line-height: 1.35; }
blockquote { border-left: 3pt solid #ccc; margin: 8pt 0; padding: 0 12pt; color: #555; font-style: italic; }
hr { border: none; border-top: 0.5pt solid #ccc; margin: 14pt 0; }
a { color: #0a58ca; text-decoration: none; }
strong { color: #000; }
.footer { margin-top: 20pt; padding-top: 6pt; border-top: 0.5pt solid #ccc; font-size: 8pt; color: #888; text-align: center; }
"""


def md_to_html(md_path: Path) -> str:
    """Render markdown to a complete HTML document."""
    md_text = md_path.read_text(encoding="utf-8")
    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    title = md_path.stem.replace("-", " ").replace("_", " ")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>{CSS_STYLES}</style>
</head>
<body>
{html_body}
<div class="footer">SiftStack SOP — {md_path.name}</div>
</body>
</html>"""


async def convert_all() -> int:
    PDF_DIR.mkdir(exist_ok=True)
    sops = sorted(DOCS.glob("SOP-*.md"))
    if not sops:
        print("No SOP-*.md files found")
        return 1

    print(f"Converting {len(sops)} SOPs → PDFs ...")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        for md in sops:
            pdf = PDF_DIR / md.with_suffix(".pdf").name
            try:
                html = md_to_html(md)
                await page.set_content(html, wait_until="load")
                await page.pdf(
                    path=str(pdf),
                    format="Letter",
                    margin={"top": "0.6in", "right": "0.6in", "bottom": "0.6in", "left": "0.6in"},
                    print_background=True,
                )
                size_kb = pdf.stat().st_size / 1024
                print(f"  ✓ {md.name:35} → {pdf.name:35} ({size_kb:.0f} KB)")
            except Exception as e:
                print(f"  ✗ {md.name}: {type(e).__name__}: {e}")
                await browser.close()
                return 2

        await browser.close()

    print(f"\nAll PDFs written to {PDF_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(convert_all()))
