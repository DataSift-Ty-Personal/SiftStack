"""Offline test flows for notice screenshot capture + delivery.

Exercises the REAL capture path (a live headless Chromium via Playwright) and
the full delivery chain (host -> DataSift CSV Notes + custom field) WITHOUT
needing TNPN credentials, 2Captcha, or a live scrape. It renders a local HTML
fixture that mimics a tnpublicnotice.com foreclosure detail page, screenshots
it with notice_screenshot.capture_notice_screenshot(), then formats a DataSift
row so you can eyeball the whole flow.

Run:
    python src/test_notice_screenshot.py
    # then open the printed PNG path to view the captured screenshot

For a true end-to-end test against the live site (costs one 2Captcha solve),
see run_live() and the note at the bottom.
"""

import asyncio
import csv
import sys
from pathlib import Path

# Allow `python src/test_notice_screenshot.py` (put src on the import path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import datasift_formatter as df
from notice_parser import NoticeData
from notice_screenshot import capture_notice_screenshot, set_local_screenshot_urls


# A stand-in for a tnpublicnotice.com notice detail page: the structured
# metadata labels followed by the "Notice Content" legal body. Realistic enough
# that the captured PNG looks like the real artifact an investor would receive.
FIXTURE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
  body { font-family: Georgia, 'Times New Roman', serif; margin: 0; color: #1a1a2e; }
  .topbar { background: #16213e; color: #fff; padding: 14px 28px; font-size: 20px; font-weight: bold; }
  .wrap { max-width: 820px; margin: 0 auto; padding: 24px 28px 60px; }
  .meta { background: #f5f6fa; border: 1px solid #dcdde1; border-radius: 6px; padding: 16px 20px; margin-bottom: 22px; }
  .meta div { margin: 4px 0; font-size: 14px; }
  .meta b { display: inline-block; width: 200px; color: #0f3460; }
  h2 { color: #16213e; border-bottom: 2px solid #e94560; padding-bottom: 6px; }
  .body { font-size: 14px; line-height: 1.7; text-align: justify; }
</style></head><body>
  <div class="topbar">Tennessee Public Notice &nbsp;|&nbsp; Notice Details</div>
  <div class="wrap">
    <div class="meta">
      <div><b>Publication Name:</b> Knoxville News Sentinel</div>
      <div><b>Publication City and State:</b> Knoxville, TN</div>
      <div><b>Publication County:</b> Knox</div>
      <div><b>Notice Publish Date:</b> Thursday, June 11, 2026</div>
    </div>
    <h2>Notice Content</h2>
    <div class="body">
      <p><b>SUBSTITUTE TRUSTEE'S SALE</b></p>
      <p>Sale at public auction will be on July 16, 2026, at 10:00 AM local time,
      at the north door of the City County Building, Knoxville, Tennessee,
      conducted by the Substitute Trustee.</p>
      <p>Default having been made in the payment of the debts and obligations
      secured by a Deed of Trust executed on March 4, 2018, by John Q. Public,
      conveying certain property therein described to a trustee, the entire
      indebtedness having been declared due and payable.</p>
      <p>The real estate is commonly known as 123 Test Street, Knoxville,
      Knox County, Tennessee 37902, and is described as Lot 7, Block C of the
      Sample Hills Subdivision as recorded in the Register's Office for Knox
      County, Tennessee.</p>
      <p>This sale is subject to all matters shown on any applicable recorded
      plat; any unpaid taxes; and any prior liens or encumbrances.</p>
    </div>
  </div>
</body></html>
"""


def _is_png(path: Path) -> bool:
    """Validate the PNG magic-number header so we know it is a real image."""
    with open(path, "rb") as f:
        return f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_gating() -> None:
    """The scraper only captures configured types: foreclosure in, probate out."""
    assert "foreclosure" in config.NOTICE_SCREENSHOT_TYPES, config.NOTICE_SCREENSHOT_TYPES
    assert "probate" not in config.NOTICE_SCREENSHOT_TYPES, config.NOTICE_SCREENSHOT_TYPES
    assert config.CAPTURE_NOTICE_SCREENSHOTS is True
    print("[gating]   OK   foreclosure captured, probate skipped (default scope)")


async def test_capture() -> Path:
    """Drive the real capture function with a live headless browser."""
    from playwright.async_api import async_playwright

    out_dir = config.NOTICE_SCREENSHOT_DIR / "test"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        await page.set_content(FIXTURE_HTML, wait_until="load")
        shot = await capture_notice_screenshot(
            page, notice_id="999999", address="123 Test Street", output_dir=out_dir,
        )
        await browser.close()

    assert shot is not None, "capture_notice_screenshot returned None"
    assert shot.exists() and shot.stat().st_size > 0, "PNG missing or empty"
    assert _is_png(shot), "file is not a valid PNG"
    print(f"[capture]  OK   {shot}  ({shot.stat().st_size:,} bytes)")
    return shot


def test_failure_is_safe() -> None:
    """A capture failure must return None, never raise (records never dropped)."""

    class BrokenPage:
        async def screenshot(self, *a, **k):
            raise RuntimeError("simulated browser crash")

    result = asyncio.run(capture_notice_screenshot(BrokenPage(), notice_id="1"))
    assert result is None, "failed capture should return None, not raise"
    print("[safety]   OK   capture failure returns None (record is kept)")


def test_delivery(shot: Path) -> Path:
    """The hosted URL must reach the DataSift custom field AND the Notes."""
    n = NoticeData(
        notice_type="foreclosure", county="Knox", address="123 Test Street",
        city="Knoxville", zip="37902", owner_name="John Q Public",
        source_url="https://www.tnpublicnotice.com/?ID=999999",
    )
    n.notice_screenshot_path = str(shot)
    set_local_screenshot_urls([n])
    assert n.notice_screenshot_url == str(shot), n.notice_screenshot_url

    out = df.write_datasift_csv([n], filename="_notice_screenshot_test.csv")
    with open(out, newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["Notice Screenshot"] == str(shot), row["Notice Screenshot"]
    assert "Notice Screenshot:" in row["Notes"], row["Notes"]
    print(f"[delivery] OK   field + Notes carry the link; CSV: {out}")
    return out


def main() -> None:
    print("Notice screenshot test flows (offline, no credentials)\n")
    test_gating()
    test_failure_is_safe()
    shot = asyncio.run(test_capture())
    test_delivery(shot)
    print("\nAll notice-screenshot test flows passed.")
    print(f"Open this PNG to see the captured artifact:\n  {shot}")


if __name__ == "__main__":
    main()
