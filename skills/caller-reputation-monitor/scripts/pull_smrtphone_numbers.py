"""
pull_smrtphone_numbers.py - pull your account's outbound caller-ID numbers (DIDs)
from SmrtPhone into config/numbers.json, using the saved browser session
(smrtphone_state.json, created by smrtphone_login.py).

Authoritative source: the SmrtPhone dialer's `/dialerConfigs` response, field
`allCallerIds` (a map of E.164 -> {name, carrier, address}). We capture it by
loading the dialer page and reading the XHR. This is the full caller-ID inventory
and scales automatically as you add numbers in SmrtPhone.

Self-contained: needs only Playwright (pip install playwright; python -m playwright install chromium).

Modes:
  (default)   DISCOVER: print the numbers found, write nothing.
  --commit    Write the numbers into config/numbers.json (preserves registration/excluded/business_name).
  --headed    Show the browser window (default headless).

Read-only against SmrtPhone (loads the dialer page and reads the config XHR). Sends/dials/buys nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SUBDIR = Path(__file__).resolve().parent
NUMBERS_FILE = SUBDIR / "config" / "numbers.json"
STATE = Path(os.environ.get("SMRTPHONE_STATE", str(SUBDIR / "smrtphone_state.json")))
BASE = "https://phone.smrt.studio"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

ARGS = None


def _loads_maybe(v):
    """allCallerIds comes back as a JSON-encoded string; decode if needed."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}


def digits(s: str) -> str:
    d = "".join(ch for ch in str(s) if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def pretty(n: str) -> str:
    return f"({n[:3]}) {n[3:6]}-{n[6:]}" if len(n) == 10 else n


async def fetch_caller_ids() -> dict:
    """Return {e164: {carrier,...}} from SmrtPhone dialerConfigs. {} on failure."""
    if not STATE.exists():
        print(f"FATAL: no session at {STATE}. Run smrtphone_login.py first.", flush=True)
        return {}
    holder = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not ARGS.headed)
        ctx = await browser.new_context(storage_state=str(STATE),
                                        viewport={"width": 1440, "height": 900},
                                        user_agent=UA)
        page = await ctx.new_page()

        async def cap(r):
            if r.url.endswith("/dialerConfigs"):
                try:
                    holder["body"] = await r.text()
                except Exception:
                    pass
        page.on("response", lambda r: asyncio.create_task(cap(r)))
        try:
            await page.goto(BASE + "/dialer/campaigns", wait_until="domcontentloaded")
            for _ in range(16):  # wait up to ~8s for the config XHR
                if "body" in holder:
                    break
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(1500)
            if "login" in page.url.lower():
                print("SESSION EXPIRED (bounced to login). Re-run smrtphone_login.py.", flush=True)
                return {}
            if "body" not in holder:
                print("Could not capture /dialerConfigs. Is the session valid?", flush=True)
                return {}
            j = json.loads(holder["body"])
            return _loads_maybe(j.get("allCallerIds")) or _loads_maybe(j.get("callerIds"))
        finally:
            await browser.close()


async def run() -> int:
    cids = await fetch_caller_ids()
    if not cids:
        return 1

    rows = []
    for e164, meta in sorted(cids.items()):
        d = digits(e164)
        if len(d) != 10:
            continue
        carrier = meta.get("carrier") if isinstance(meta, dict) else ""
        rows.append({"e164": f"+1{d}", "d10": d, "carrier": carrier or ""})

    print(f"\nSmrtPhone caller IDs found: {len(rows)}", flush=True)
    for r in rows:
        print(f"   {pretty(r['d10'])}  carrier={r['carrier']}", flush=True)

    if not ARGS.commit:
        print("\n(discovery only; re-run with --commit to write config/numbers.json)", flush=True)
        return 0

    cfg = json.loads(NUMBERS_FILE.read_text(encoding="utf-8-sig")) if NUMBERS_FILE.exists() else {}
    cfg.pop("_comment", None)
    today = datetime.date.today().isoformat()

    # Metadata-preserving merge: SmrtPhone is the authoritative roster, but keep each
    # surviving number's warm-up date (`added`) and `caller` assignment so we do NOT
    # reset the lifecycle. New numbers get added=today; numbers no longer in SmrtPhone
    # drop out of the roster (reported below).
    existing = {digits(n.get("number", "")): n for n in cfg.get("numbers", []) or []}
    live10 = {r["d10"] for r in rows}
    merged, added_new = [], []
    for r in rows:
        prev = existing.get(r["d10"], {})
        merged.append({
            "number": r["e164"],
            "label": prev.get("label") or (f"SmrtPhone ({r['carrier']})" if r["carrier"] else "SmrtPhone"),
            "caller": prev.get("caller", ""),
            "added": prev.get("added") or today,
        })
        if not prev:
            added_new.append(r["d10"])
    dropped = [d for d in existing if d not in live10]

    cfg["numbers"] = merged
    NUMBERS_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWROTE {len(merged)} number(s) to {NUMBERS_FILE} "
          f"(preserved added/caller; {len(added_new)} new, {len(dropped)} dropped)", flush=True)
    if added_new:
        print("  NEW (added today):   " + ", ".join(pretty(d) for d in added_new), flush=True)
    if dropped:
        print("  DROPPED (gone from SmrtPhone): " + ", ".join(pretty(d) for d in dropped), flush=True)
    return 0


def main() -> int:
    global ARGS
    ap = argparse.ArgumentParser(description="Pull SmrtPhone caller IDs into numbers.json")
    ap.add_argument("--commit", action="store_true", help="write numbers.json")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ARGS = ap.parse_args()
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
