"""Verify _filter_by_tag: applies a real tag, and REFUSES a tag that does not exist.

The refusal case is the whole point. A filter that quietly does nothing sends
export_phone_enrichment on to export the entire account and Trestle-score it.

Read-only: applies filters and reads the record count. Nothing is written.

Run:  .venv/bin/python src/_probe_tag_filter_verify.py [real_tag]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from datasift_core import create_browser, login  # noqa: E402
from datasift_uploader import _filter_by_tag, _navigate_to_records  # noqa: E402

REAL_TAG = sys.argv[1] if len(sys.argv) > 1 else "FTM"
FAKE_TAG = "siftstack_no_such_cohort_9999-99-99"

# The Records grid renders one row per property; counting rows is more reliable
# than scraping a total that may not be rendered as plain text.
COUNT_JS = """() => {
  const txt = document.body.innerText;
  const m = txt.match(/([\\d,]+)\\s+(?:Property\\s+)?Records?\\b/i)
         || txt.match(/of\\s+([\\d,]+)/i);
  const rows = document.querySelectorAll('[class*="TableRow"], [class*="RecordRow"]').length;
  return {label: m ? m[1] : null, rows};
}"""


async def count(page):
    try:
        return await page.evaluate(COUNT_JS)
    except Exception:
        return {"label": None, "rows": -1}


async def main():
    async with create_browser(headless=False) as (browser, context, page):
        if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
            print("login failed")
            return

        await _navigate_to_records(page)
        await page.wait_for_timeout(3000)
        baseline = await count(page)
        print(f"unfiltered record count: {baseline}")

        print(f"\n--- real tag: {REAL_TAG!r} ---")
        ok_real = await _filter_by_tag(page, REAL_TAG)
        await page.wait_for_timeout(3000)
        filtered = await count(page)
        print(f"  returned {ok_real}, count now {filtered}")
        # A filter that "applied" but left the grid identical is the exact
        # failure mode this work exists to kill, so treat it as a failure.
        narrowed = (ok_real and filtered["label"] is not None
                    and filtered["label"] != baseline["label"])
        await page.screenshot(path="datasift_verify_real.png")

        print(f"\n--- nonexistent tag: {FAKE_TAG!r} ---")
        await _navigate_to_records(page)
        await page.wait_for_timeout(3000)
        ok_fake = await _filter_by_tag(page, FAKE_TAG)
        print(f"  returned {ok_fake}")

        print("\n=== VERDICT ===")
        print(f"  real tag applied      : {'PASS' if ok_real else 'FAIL'}")
        print(f"  grid actually narrowed: {'PASS' if narrowed else 'FAIL/UNPROVEN'}"
              f"   ({baseline['label']} -> {filtered['label']})")
        print(f"  fake tag refused      : {'PASS' if not ok_fake else 'FAIL — would export everything'}")


if __name__ == "__main__":
    asyncio.run(main())
