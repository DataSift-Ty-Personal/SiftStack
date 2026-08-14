"""Read-only SmrtPhone inbox snapshot - see how the foreclosure MMS threads are doing
(which homeowners replied, what the list looks like). Loads the saved session, dismisses
the mic modal, screenshots the inbox, and dumps the visible conversation-list text.
Sends NOTHING. Safe to run anytime.

  python src/mms_inbox_snapshot.py
"""
import asyncio
import sys
from pathlib import Path

SS = Path(r"c:\Users\Tyrus\OneDrive\SiftStack")
sys.path.insert(0, str(SS / "src"))
from playwright.async_api import async_playwright  # noqa: E402
import datasift_core as dc  # noqa: E402

BASE = "https://phone.smrt.studio"
STATE = SS / "smrtphone_state.json"
SHOT = Path(r"c:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\out\sender\inbox_snapshot.png")


async def main():
    if not STATE.exists():
        print(f"FATAL: no SmrtPhone session at {STATE}. Run _api/smrtphone_login.py first.")
        return
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(STATE), viewport=dc.DEFAULT_VIEWPORT,
                                             user_agent=dc.DEFAULT_USER_AGENT, permissions=["microphone"])
        try:
            await context.grant_permissions(["microphone"], origin=BASE)
        except Exception:
            pass
        page = await context.new_page()
        try:
            await page.goto(BASE + "/inboxV2/", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("goto warn:", str(e)[:60])
        await page.wait_for_timeout(6000)
        for label in ("I understand", "Cancel"):
            try:
                b = page.get_by_role("button", name=label, exact=True).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=2000)
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                pass
        if "/login" in page.url.lower():
            print("SESSION EXPIRED - re-run _api/smrtphone_login.py to refresh smrtphone_state.json")
            await browser.close()
            return
        await page.wait_for_timeout(1500)
        SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(SHOT))
        # best-effort: dump the inbox unread badge + the conversation-list rows
        info = await page.evaluate("""() => {
          const out = {unread: null, rows: []};
          const badge = [...document.querySelectorAll('*')].find(e =>
            /inbox/i.test(e.textContent||'') && e.querySelector && e.querySelector('[class*=badge],[class*=count]'));
          const rows = [...document.querySelectorAll('li,[class*=conversation],[class*=ConversationItem]')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.left < 470 && r.width > 120 && r.height > 30 && r.height < 120; });
          const seen = new Set();
          for (const r of rows) {
            const t = (r.innerText||'').replace(/\\s+/g,' ').trim();
            if (t && t.length < 120 && !seen.has(t)) { seen.add(t); out.rows.push(t); }
          }
          return out;
        }""")
        print("URL:", page.url)
        print("screenshot ->", SHOT)
        print(f"\nconversation list ({len(info.get('rows', []))} rows):")
        for r in info.get("rows", [])[:40]:
            print("  -", r)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
