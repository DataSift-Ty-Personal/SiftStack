"""Route B MMS sender: drives the smrtPhone web-app Inbox to text each foreclosure
homeowner their auction-notice screenshot + the personalized message.

Consumes the send plan (mms_send_plan.csv / mms_send_queue.csv), reuses the saved
smrtPhone session (smrtphone_state.json), downloads each Dropbox screenshot to a
local file, and per recipient: opens a NEW conversation to their number, types the
message, attaches the image, and (only with --commit) sends. Writes the idempotency
ledger immediately on a confirmed send (golden rule #17) so nobody is double-texted.

DRY by default (walks every step, screenshots, STOPS before the Send click).
The smrtPhone SPA is flaky -> resilient waits + per-step screenshots.

  # dry walkthrough to YOUR OWN phone (no send), screenshots every step:
  python src/mms_sender.py --to 8651234567 --limit 1 --headed
  # real TEST send to your own phone:
  python src/mms_sender.py --to 8651234567 --limit 1 --commit --headed
  # live run (uses the plan's real recipients):
  python src/mms_sender.py --commit
"""
import argparse
import asyncio
import csv
import random
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SS = Path(r"c:\Users\Tyrus\OneDrive\SiftStack")
API = Path(r"c:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api")
sys.path.insert(0, str(SS / "src"))
sys.path.insert(0, str(API))
from playwright.async_api import async_playwright  # noqa: E402
import datasift_core as dc  # noqa: E402
try:
    from mms_ledger import record_send, already_sent, _clean  # noqa: E402
except Exception:  # pragma: no cover
    def _clean(n):
        d = "".join(c for c in str(n or "") if c.isdigit())
        return d[1:] if len(d) == 11 and d.startswith("1") else d
    def already_sent(*a, **k): return False
    def record_send(*a, **k): return {}
try:
    from mms_schedule import tz_for  # noqa: E402  (area-code -> IANA tz for the quiet-hours guard)
except Exception:
    def tz_for(_n): return "America/New_York"

BASE = "https://phone.smrt.studio"
STATE = SS / "smrtphone_state.json"
PLAN = SS / "output" / "mms_send_plan.csv"
QUEUE = SS / "output" / "mms_send_queue.csv"
MEDIA_DIR = SS / "output" / "mms_media"
SHOTS = API / "out" / "sender"
MSG_PLACEHOLDER = "Write your message here"
NEW_CONVO_PLACEHOLDER = "phone number or user"  # input.prompt: "Contact name , phone number or user"


def ensure_raw(u: str) -> str:
    if not u or "raw=1" in u:
        return u
    if "dl=0" in u:
        return u.replace("dl=0", "raw=1")
    if "dl=1" in u:
        return u.replace("dl=1", "raw=1")
    return u + ("&" if "?" in u else "?") + "raw=1"


def download_media(url: str, uuid: str) -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / f"{uuid or 'img'}.png"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    req = urllib.request.Request(ensure_raw(url), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        dest.write_bytes(r.read())
    return dest


async def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(SHOTS / f"{name}.png"))
    except Exception as e:
        print(f"    shot FAIL {name}: {str(e)[:50]}", flush=True)


async def safe_goto(page, url, settle=4500, timeout=60000):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        print(f"    goto warn {url}: {str(e)[:50]}", flush=True)
    await page.wait_for_timeout(settle)


async def _inbox_ready(page) -> bool:
    """Wait until the inbox is interactive (the flaky SPA needs this before clicks)."""
    for _ in range(8):
        try:
            for txt in ("Sort by", "My Inbox"):
                el = page.get_by_text(txt, exact=False).first
                if await el.count() and await el.is_visible():
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    return False


async def dismiss_blocking_modals(page) -> bool:
    """Close any SmrtPhone overlay that covers the compose UI - chiefly the dialer's
    'Allow microphone' prompt, which pops mid-run and blocks every send after it. Mic
    permission is granted at the context level, so acknowledging it is safe. Returns
    True if it dismissed something."""
    for label in ("I understand", "Cancel"):
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(500)
                print(f"    (dismissed blocking modal: '{label}')", flush=True)
                return True
        except Exception:
            pass
    return False


async def open_new_conversation(page, to_number: str, attempts: int = 3) -> bool:
    """Open the 'Compose Message' modal (icon-only Message button, top-left trio;
    fallback the conversation-header '+' div), then fill the recipient number.
    The buttons are icon-only (no text/aria) so we target by screen position."""
    for attempt in range(attempts):
        await dismiss_blocking_modals(page)
        await _inbox_ready(page)
        await page.wait_for_timeout(1200)
        await page.evaluate("""() => {
          // icon-only 'Message' button (top-left Call/Message/Video trio, ~x80 y63)
          let b=[...document.querySelectorAll('button')].find(x=>{const r=x.getBoundingClientRect();
            return Math.abs(r.left-80)<30 && Math.abs(r.top-63)<30;});
          // fallback: the conversation-list header '+' (a <div> with an svg, top of the column)
          if(!b){ b=[...document.querySelectorAll('div,span,[role=button]')].find(e=>{const r=e.getBoundingClientRect();
            return r.width>0&&r.width<46&&r.height<46&&r.top<70&&r.left>225&&r.left<460&&e.querySelector('svg');}); }
          if(b) b.click();
        }""")
        await page.wait_for_timeout(2000)
        # the Compose Message recipient field (NOT the conversation-list '...or user' search)
        prompt = page.locator("input.prompt:not([placeholder*='user'])").first
        try:
            await prompt.wait_for(state="visible", timeout=5000)
        except Exception:
            await shot(page, f"retry{attempt}_nocompose")
            await page.wait_for_timeout(1200)
            continue
        await dismiss_blocking_modals(page)
        await prompt.click()
        await prompt.fill(to_number)
        await page.wait_for_timeout(2500)  # let the number lookup/result render
        return True
    return False


async def select_number_result(page, to_number: str) -> bool:
    """Pick the 'send to this number' result the prompt offers (or press Enter)."""
    digits = _clean(to_number)
    picked = await page.evaluate("""(digits) => {
      const norm = s => (s||'').replace(/\\D/g,'');
      const opts = [...document.querySelectorAll('[class*=result],[class*=Result],[class*=option],[role=option],li,[class*=item]')];
      const hit = opts.find(o => norm(o.innerText).includes(digits));
      if (hit) { hit.scrollIntoView({block:'center'}); hit.click(); return true; }
      return false;
    }""", digits)
    if not picked:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2500)
    return True


async def type_message(page, text: str) -> bool:
    box = page.locator("textarea.messageTextArea, textarea[placeholder*='Type your SMS'], "
                       f"[placeholder*='{MSG_PLACEHOLDER}']").first
    try:
        await box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False
    await box.click()
    await box.fill(text)
    await page.wait_for_timeout(800)
    return True


async def click_send_message(page) -> bool:
    """Click the modal's 'Send Message' (sends the text SMS, opens the conversation)."""
    try:
        await page.get_by_role("button", name="Send Message").first.click(timeout=6000)
        await page.wait_for_timeout(2200)
        return True
    except Exception:
        sent = await page.evaluate("""() => { const b=[...document.querySelectorAll('button')]
          .find(x=>/send message/i.test(x.innerText||'')); if(b){b.click();return true;} return false; }""")
        await page.wait_for_timeout(2200)
        return bool(sent)


# --- The conversation thread + reply box + image attach live in the 'main-iframe'
#     (validated 2026-06-24). Main-frame queries never see them. ---

async def get_main_iframe(page, tries: int = 30):
    for _ in range(tries):
        fr = page.frame(name="main-iframe")
        if fr:
            return fr
        await page.wait_for_timeout(500)
    return None


async def wait_for_iframe_replybox(fr, page, tries: int = 24) -> bool:
    """Wait (gated, fine-grained) for the conversation reply box (its hidden file input)
    to render in the iframe - returns the instant it's ready so the image goes right after."""
    for _ in range(tries):
        try:
            if await fr.locator("input[type=file]").count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False


def _number_patterns(to: str) -> list[str]:
    d = _clean(to)
    if len(d) != 10:
        return [d]
    return [d, f"1{d}", f"+1{d}", f"({d[:3]}) {d[3:6]}-{d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}", f"{d[:3]}.{d[3:6]}.{d[6:]}"]


async def verify_conversation_open(fr, page, to: str) -> bool:
    """SAFETY: confirm the OPEN conversation belongs to the intended recipient before
    attaching an image. A fresh inbox nav auto-opens the most-recent conversation (the
    one just texted) - but we NEVER attach to an unverified thread."""
    pats = _number_patterns(to)
    for ctx in (fr, page):
        try:
            txt = await ctx.evaluate("() => (document.body && document.body.innerText) || ''")
        except Exception:
            txt = ""
        if any(p in (txt or "") for p in pats):
            return True
    return False


async def attach_image_iframe(fr, page, media_path: Path) -> bool:
    """Set the screenshot on the iframe reply box's hidden file input (no chooser), then
    wait ONLY until the preview actually renders (gated poll, not a blind 5s sleep) so the
    image goes right after the text instead of trailing it."""
    try:
        await fr.locator("input[type=file]").first.set_input_files(str(media_path))
    except Exception as e:
        print(f"    iframe attach FAIL: {str(e)[:60]}", flush=True)
        return False
    for _ in range(24):  # ~6s ceiling; returns the instant the preview shows (usually ~1s)
        try:
            n = await fr.evaluate("""() => document.querySelectorAll('img[src^="blob:"],img[src*="preview"],[class*=preview] img').length""")
        except Exception:
            n = 0
        if n > 0:
            await page.wait_for_timeout(250)  # let the preview settle a beat before send
            return True
        await page.wait_for_timeout(250)
    return True  # set_input_files succeeded; proceed even if the preview never confirmed


async def _image_still_staged(fr) -> bool:
    """True if an image is STILL sitting in the compose box (preview thumbnail and/or the
    staged-image 'Send' button present) = NOT sent yet. This is the real 'did it send?'
    check - 'no preview img' alone gave false positives (an image stayed in the box yet
    we logged it sent)."""
    try:
        return await fr.evaluate("""() => {
          const prev = document.querySelectorAll('img[src^="blob:"],img[src^="data:"],[class*=preview] img').length;
          const sendBtn = [...document.querySelectorAll('button')].some(b => /^\\s*send\\s*$/i.test((b.innerText||'').trim()));
          return prev > 0 || sendBtn;
        }""")
    except Exception:
        return False


async def _click_image_send(fr, page) -> None:
    """Click the reply box's send control. A STAGED IMAGE shows a dedicated 'Send' text
    button - click THAT (the bare svg arrow doesn't reliably fire the MMS, which is what
    left images stuck in the box). Fall back to the arrow. bounding_box is page-relative
    even inside the iframe, so click via page.mouse."""
    try:
        btn = fr.get_by_role("button", name="Send", exact=True).first
        if await btn.count() and await btn.is_visible():
            b = await btn.bounding_box()
            if b:
                await page.mouse.click(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
                return
    except Exception:
        pass
    box = None
    for sel in ["svg.lucide-send", "[class*='lucide-send']", "button:has(svg)"]:
        try:
            b = await fr.locator(sel).last.bounding_box()
            if b:
                box = b
                break
        except Exception:
            pass
    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    else:
        await page.mouse.click(1111, 836)  # fallback: send arrow's usual spot


async def send_image_iframe(fr, page) -> bool:
    """Send the staged image and CONFIRM it left the compose box. Retries the send click
    (re-locating the control each time) if the image is still staged - 'preview cleared'
    alone was a false-positive trap, so we verify the image actually departed."""
    for attempt in range(3):
        await _click_image_send(fr, page)
        await page.wait_for_timeout(3000)
        if not await _image_still_staged(fr):
            return True  # image left the box = sent
        print(f"    image still in box, retrying send ({attempt + 1}/3)", flush=True)
        await page.wait_for_timeout(1500)
    return not await _image_still_staged(fr)


async def send_one(page, row, dry=True, idx=0):
    to = _clean(row["to_number"])
    label = f"{row.get('first_name','')}/{to}"
    print(f"  [{idx}] {label} | {row.get('county','')} auction {row.get('auction_date','')}", flush=True)
    media = download_media(row["media_url"], row.get("record_uuid", f"i{idx}"))
    print(f"    media -> {media.name} ({media.stat().st_size}b)", flush=True)

    await safe_goto(page, BASE + "/inboxV2/", settle=4500)
    await shot(page, f"{idx}_a_inbox")
    # --- step 1: compose the text in the new-message modal (SMS-only modal) ---
    if not await open_new_conversation(page, to):
        await shot(page, f"{idx}_x_modal_fail")
        return "FAIL: compose modal did not open"
    await select_number_result(page, to)
    await shot(page, f"{idx}_b_recipient")
    if not await type_message(page, row["message_text"]):
        await shot(page, f"{idx}_x_msg_fail")
        return "FAIL: message box not found"
    await shot(page, f"{idx}_c_text")

    if dry:
        return "DRY_OK (text composed; image needs a real send to open the thread)"

    # --- step 2: send the text -> creates + opens the conversation thread ---
    if not await click_send_message(page):
        await shot(page, f"{idx}_x_sendtext_fail")
        return "FAIL: could not send text"
    record_send(record_uuid=row.get("record_uuid", ""), phone=to, from_number=row.get("from_number", ""),
                county=row.get("county", ""), auction_date=row.get("auction_date", ""),
                message=row["message_text"], media_url=row["media_url"], status="text_sent", dry_run=False)
    await shot(page, f"{idx}_d_textsent")

    # --- step 3: open the created conversation (fresh inbox nav auto-opens the most-recent
    #     = the one just texted), VERIFY it's the right recipient, then attach + send the image ---
    await safe_goto(page, BASE + "/inboxV2/", settle=1500)
    fr = await get_main_iframe(page)
    if not fr:
        await shot(page, f"{idx}_x_no_iframe")
        return "PARTIAL: text sent, inbox iframe not found"
    await wait_for_iframe_replybox(fr, page)
    if not await verify_conversation_open(fr, page, to):
        await shot(page, f"{idx}_x_wrong_convo")
        return "PARTIAL: text sent, recipient conversation NOT verified -> image WITHHELD (safety)"
    if not await attach_image_iframe(fr, page, media):
        await shot(page, f"{idx}_x_attach_fail")
        return "PARTIAL: text sent, image attach failed"
    await shot(page, f"{idx}_e_attached")
    if not await send_image_iframe(fr, page):
        await shot(page, f"{idx}_x_imgsend_fail")
        return "PARTIAL: text sent, image not confirmed sent"
    await shot(page, f"{idx}_f_imagesent")
    record_send(record_uuid=row.get("record_uuid", ""), phone=to, from_number=row.get("from_number", ""),
                message="[auction screenshot]", media_url=row["media_url"], status="image_sent", dry_run=False)
    return "SENT (text + image)"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=str(PLAN))
    ap.add_argument("--commit", action="store_true", help="actually click Send (default: dry, stop before send)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--to", default="", help="override recipient (send ALL selected rows to THIS number - for a self-test)")
    ap.add_argument("--headed", action="store_true", default=True)
    ap.add_argument("--spacing-min", type=int, default=45, help="min seconds between sends (anti-burst)")
    ap.add_argument("--spacing-max", type=int, default=90, help="max seconds between sends")
    ap.add_argument("--window-start", type=int, default=8, help="quiet-hours: earliest recipient-local send hour")
    ap.add_argument("--window-end", type=int, default=21, help="quiet-hours: latest recipient-local send hour (21=9pm)")
    ap.add_argument("--no-window", action="store_true", help="disable the quiet-hours guard (testing/self-test only)")
    a = ap.parse_args()

    plan_path = Path(a.plan)
    if not plan_path.exists():
        plan_path = QUEUE
    rows = list(csv.DictReader(open(plan_path, encoding="utf-8")))
    if a.limit:
        rows = rows[: a.limit]
    if a.to:
        for r in rows:
            r["to_number"] = a.to
    print(f"plan: {plan_path.name} | {len(rows)} row(s) | mode={'COMMIT(send)' if a.commit else 'DRY(no send)'}"
          + (f" | OVERRIDE to {a.to}" if a.to else ""), flush=True)

    if not STATE.exists():
        print(f"FATAL: no smrtPhone session at {STATE}. Run _api/smrtphone_login.py first.")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not a.headed)
        context = await browser.new_context(storage_state=str(STATE),
                                             viewport=dc.DEFAULT_VIEWPORT, user_agent=dc.DEFAULT_USER_AGENT,
                                             permissions=["microphone"])
        try:
            await context.grant_permissions(["microphone"], origin=BASE)
        except Exception:
            pass
        page = await context.new_page()
        try:
            await safe_goto(page, BASE + "/dashboard", settle=2500)
            if "/login" in page.url.lower():
                print("FATAL: smrtPhone session expired. Re-run _api/smrtphone_login.py.")
                return
            results = []
            sent_count = 0
            for i, row in enumerate(rows, 1):
                if not a.to and already_sent(row.get("record_uuid", ""), row["to_number"]):
                    print(f"  [{i}] skip (already in ledger): {row.get('first_name','')}", flush=True)
                    results.append("SKIP_LEDGER")
                    continue
                # quiet-hours guard: never text outside the recipient's local window (area-code tz)
                if not a.no_window:
                    tzname = tz_for(row["to_number"])
                    local = datetime.now(ZoneInfo(tzname))
                    if not (a.window_start <= local.hour < a.window_end):
                        print(f"  [{i}] SKIP quiet-hours: {local.strftime('%H:%M')} {tzname.split('/')[-1]} "
                              f"(window {a.window_start:02d}-{a.window_end:02d} local)", flush=True)
                        results.append("SKIP_QUIET_HOURS")
                        continue
                # randomized spacing between actual sends (anti-burst / deliverability)
                if a.commit and sent_count > 0:
                    delay = random.randint(a.spacing_min, a.spacing_max)
                    print(f"    (spacing {delay}s before next send)", flush=True)
                    await asyncio.sleep(delay)
                try:
                    res = await send_one(page, row, dry=not a.commit, idx=i)
                except Exception as e:
                    res = f"ERROR: {str(e)[:80]}"
                    await shot(page, f"{i}_x_error")
                print(f"    -> {res}", flush=True)
                results.append(res)
                if a.commit and isinstance(res, str) and res.startswith("SENT"):
                    sent_count += 1
            print(f"\nsummary: {dict((r, results.count(r)) for r in set(results))}", flush=True)
            print(f"step screenshots -> {SHOTS}", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
