"""Automated recon for kcoj.kycourts.net/dockets.

Opens the page, auto-identifies the county/division/date controls by inspecting
option text, submits the form for Jefferson + District + today, dumps the
resulting DOM and a screenshot so we can see exactly what a docket row looks
like and build a real scraper against it.

Writes everything to output/kcoj_recon/.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent.parent / "output" / "kcoj_recon"
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://kcoj.kycourts.net/dockets"
COUNTY = "JEFFERSON"   # match against option text (case-insensitive)
DIVISION = "DISTRICT"


async def dump_controls(page) -> list[dict]:
    return await page.evaluate(
        """() => {
            const out = [];
            for (const el of document.querySelectorAll('select, input, textarea, button, [role="combobox"], [role="button"]')) {
                const rect = el.getBoundingClientRect();
                const item = {
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || null,
                    type: el.type || null,
                    name: el.name || null,
                    id: el.id || null,
                    class: el.className || null,
                    placeholder: el.placeholder || null,
                    value: el.value || null,
                    aria_label: el.getAttribute('aria-label') || null,
                    text: (el.innerText || '').slice(0, 100) || null,
                    visible: rect.width > 0 && rect.height > 0,
                };
                if (el.tagName === 'SELECT') {
                    item.options = [...el.options].map(o => ({
                        value: o.value, text: o.text,
                    }));
                }
                out.push(item);
            }
            return out;
        }"""
    )


async def find_select_by_option(page, needle: str) -> int | None:
    """Return the 0-based index into document.querySelectorAll('select') of the
    select whose options contain `needle` (case-insensitive). Returns index, not
    selector string, because these selects have neither id nor name."""
    result = await page.evaluate(
        """(needle) => {
            const selects = document.querySelectorAll('select');
            for (let i = 0; i < selects.length; i++) {
                const s = selects[i];
                for (const o of s.options) {
                    if (o.text.toUpperCase().includes(needle.toUpperCase())) {
                        return i;
                    }
                }
            }
            return -1;
        }""",
        needle,
    )
    return None if result is None or result < 0 else result


async def main() -> None:
    headless = "--headed" not in sys.argv
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=150)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
        )
        page = await context.new_page()

        print(f"[recon] GET {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        # SPA hydration — give React/Angular a few seconds to mount controls
        await asyncio.sleep(6)
        # Wait for any <select> to appear as a crude "SPA is alive" signal
        try:
            await page.wait_for_selector("select", state="attached", timeout=20_000)
        except Exception:
            print("[recon] WARNING — no <select> appeared within 20s after domcontentloaded")

        (OUT / "01_initial.html").write_text(await page.content(), encoding="utf-8")
        controls_before = await dump_controls(page)
        (OUT / "01_controls_before.json").write_text(
            json.dumps(controls_before, indent=2), encoding="utf-8"
        )
        await page.screenshot(path=str(OUT / "01_initial.png"), full_page=True)
        print(f"[recon] initial dump: {len(controls_before)} controls")

        # Identify county select by option content (no id/name on it)
        county_idx = await find_select_by_option(page, COUNTY)
        print(f"[recon] county select idx: {county_idx}")
        if county_idx is None:
            print("[recon] ABORT — no county select found")
            await browser.close()
            return

        # Set county via nth-of-type-free approach: use the nth select
        county_handle = (await page.query_selector_all("select"))[county_idx]
        await county_handle.select_option(label="JEFFERSON")
        await asyncio.sleep(1)

        # Division is a pair of radio inputs (#circuit, #district)
        district_radio = await page.query_selector("#district")
        if district_radio:
            await district_radio.check()
            print("[recon] checked #district radio")
        else:
            print("[recon] WARNING — #district radio not found")
        await asyncio.sleep(1)

        # Date input is type=date and already defaults to today; set explicitly to be safe
        date_input = await page.query_selector('input[type="date"]')
        if date_input:
            today = datetime.now().strftime("%Y-%m-%d")
            await date_input.fill(today)
            print(f"[recon] date set to {await date_input.input_value()!r}")
        else:
            print("[recon] WARNING — no date input found")

        # Submit
        submit = await page.query_selector('button[type="submit"]')
        if submit:
            await submit.click()
            print("[recon] clicked submit")
        else:
            print("[recon] WARNING — no submit button found")

        # Wait for the docket to render — don't wait for networkidle (SPA keeps
        # connections open); instead sleep and then look for new DOM nodes.
        await asyncio.sleep(8)

        # Post-query state
        (OUT / "02_after.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "02_after.png"), full_page=True)
        controls_after = await dump_controls(page)
        (OUT / "02_controls_after.json").write_text(
            json.dumps(controls_after, indent=2), encoding="utf-8"
        )

        # Snapshot tables + any row-bearing container
        snap = await page.evaluate(
            """() => {
                const tables = [...document.querySelectorAll('table')].map(t => ({
                    id: t.id || null,
                    class: t.className || null,
                    rows: t.rows.length,
                    headerText: t.rows[0] ? t.rows[0].innerText.slice(0, 300) : null,
                    sampleRow1: t.rows[1] ? t.rows[1].innerText.slice(0, 400) : null,
                    sampleRow2: t.rows[2] ? t.rows[2].innerText.slice(0, 400) : null,
                    sampleRow3: t.rows[3] ? t.rows[3].innerText.slice(0, 400) : null,
                }));
                // Case number text hunts
                const bodyText = document.body.innerText;
                const caseNums = [...bodyText.matchAll(/\\b\\d{2}-[A-Z]{1,3}-\\d{1,6}\\b/g)]
                    .map(m => m[0]).slice(0, 40);
                return { tables, caseNums, bodyTextPreview: bodyText.slice(0, 5000) };
            }"""
        )
        (OUT / "02_snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")

        print(f"\n[recon] tables: {len(snap['tables'])}")
        for t in snap["tables"]:
            print(f"  - id={t['id']!r} rows={t['rows']} header={t['headerText']!r}")
        print(f"[recon] case-number-like strings found: {len(snap['caseNums'])}")
        if snap["caseNums"]:
            print(f"         first few: {snap['caseNums'][:10]}")

        await browser.close()
        print(f"\n[recon] DONE. Artifacts in {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
