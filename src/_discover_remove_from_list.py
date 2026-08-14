"""Discover the reisift 'remove from list' / 'delete populated list' network call.
Logs into ty+2, opens the Lists page, expands the 'default' folder, locates the
'FTM Foreclosure Blount 2026-06-22' list, and dumps the row controls + screenshots,
while capturing every write (POST/PATCH/PUT/DELETE) request to reisift."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from datasift_core import create_browser, login  # noqa: E402
from sift_upload_wizard import _dismiss_popups  # noqa: E402

OUT = Path("output/_rmlist")
OUT.mkdir(parents=True, exist_ok=True)
TARGET = "FTM Foreclosure Blount 2026-06-22"

captured = []


def _on_request(req):
    try:
        if req.method in ("POST", "PATCH", "PUT", "DELETE") and "reisift.io" in req.url:
            captured.append({"m": req.method, "url": req.url, "body": (req.post_data or "")[:400]})
    except Exception:
        pass


async def dump_controls(page, label):
    info = await page.evaluate(
        """(target) => {
          const vis = e => { const r = e.getBoundingClientRect(); return r.width>0 && r.height>0; };
          // rows/elements that mention the target list
          const hits = [...document.querySelectorAll('*')].filter(e => {
            const t=(e.textContent||'').trim(); return t.includes(target) && t.length < 80 && vis(e);
          }).map(e => ({ t:e.textContent.trim().slice(0,60), tag:e.tagName, cls:(e.className||'').toString().slice(0,46) }));
          // any clickable icons/buttons on the page
          const ctrls = [...document.querySelectorAll('button,[role=button],svg,a,[class*=Menu],[class*=menu],[class*=elete],[class*=rash],[class*=ption]')]
            .filter(vis).map(e => ({ t:(e.textContent||'').trim().slice(0,20), cls:(e.className||'').toString().slice(0,50) }))
            .filter(x => /elete|rash|emove|enu|ption|dots|\\.\\.\\./i.test(x.cls+x.t)).slice(0,30);
          return { hits, ctrls };
        }""",
        TARGET,
    )
    (OUT / f"{label}.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"{label}.png"))
    print(f"[{label}] target-hits={len(info['hits'])} ctrls={len(info['ctrls'])}")
    return info


async def main():
    async with create_browser(headless=True) as (browser, context, page):
        page.on("request", _on_request)
        await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        await page.goto("https://app.reisift.io/lists", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await _dismiss_popups(page)
        await dump_controls(page, "01_lists")

        # Expand the 'default' folder
        folder = page.locator('text="default"')
        if await folder.count() > 0:
            await folder.first.click()
            await page.wait_for_timeout(2500)
        info = await dump_controls(page, "02_folder_expanded")
        print("  target hits:", json.dumps(info["hits"])[:600])
        print("  controls:", json.dumps(info["ctrls"])[:800])

        print("\n=== captured writes so far ===")
        for r in captured:
            print(" ", r["m"], r["url"][-70:], "|", r["body"][:80])


if __name__ == "__main__":
    asyncio.run(main())
