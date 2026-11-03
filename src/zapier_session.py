#!/usr/bin/env python3
"""Hold a signed-in Zapier session, for building Zaps through the editor.

Zapier has no API for creating a Zap, and Claude's Zapier MCP connector can run
actions but not build automations. So Zaps are built in the editor, in a browser
profile a person signs into once.

    python src/zapier_session.py --phase login      # headed; YOU sign in

Same pattern as smarter_contact_setup.py: a persistent profile under output/
(gitignored), real Chrome without the automation flag so Google sign-in is not
refused, no password stored by this script.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "output" / "zapier" / "browser_profile"
SHOTS = ROOT / "output" / "zapier"
HOME = "https://zapier.com/app/home"


async def _context(pw, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    kw = dict(user_data_dir=str(PROFILE), headless=headless, viewport=None,
              args=["--disable-blink-features=AutomationControlled"])
    try:
        return await pw.chromium.launch_persistent_context(channel="chrome", **kw)
    except Exception:
        return await pw.chromium.launch_persistent_context(**kw)


def _signed_in(url: str) -> bool:
    return url.startswith("https://zapier.com/app/") and "/login" not in url \
        and "/sign-up" not in url


async def login(timeout_min: int) -> int:
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if not _signed_in(page.url):
            print(f"SIGN IN to Zapier in the window that just opened (waiting up to {timeout_min} min)")
            deadline = time.time() + timeout_min * 60
            while time.time() < deadline and not _signed_in(page.url):
                await asyncio.sleep(2)
        if not _signed_in(page.url):
            print(f"FAILED: still on {page.url} after {timeout_min} min")
            await ctx.close()
            return 2
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(SHOTS / "after_login.png"))
        print(f"SIGNED IN: {page.url}  (session saved in {PROFILE.relative_to(ROOT)})")
        await ctx.close()
        return 0


NOISE = ("google", "segment", "sentry", "datadog", "intercom", "hotjar", "fullstory",
         "doubleclick", "facebook", "linkedin", "bing", "clarity", "launchdarkly", "statsig")


async def discover() -> int:
    """Map the pages a Zap build touches: request URLs and methods only (headers
    and bodies carry the session and are never written to disk)."""
    import json
    calls: list[tuple[str, str, str]] = []
    report = {}
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("request", lambda r: calls.append((page.url.split("?")[0], r.method, r.url.split("?")[0]))
                if r.resource_type in ("xhr", "fetch") and not any(n in r.url for n in NOISE) else None)
        for i, url in enumerate(("https://zapier.com/app/home", "https://zapier.com/app/assets/connections",
                                 "https://zapier.com/app/zaps", "https://zapier.com/webintent/create-zap")):
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)
            if not _signed_in(page.url) and "editor" not in page.url:
                report[url] = {"landed": page.url, "note": "not signed in?"}
                continue
            await page.screenshot(path=str(SHOTS / f"d{i}_{url.rstrip('/').split('/')[-1]}.png"))
            report[url] = {"landed": page.url.split("?")[0], "text": (await page.evaluate(
                "() => document.body.innerText"))[:1500]}
        await ctx.close()
    report["api"] = sorted({(m, u) for _, m, u in calls})
    (SHOTS / "discover.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    hosts = sorted({u.split("/")[2] for _, u in report["api"]})
    print(f"{len(report['api'])} distinct calls on {hosts}")
    for k, v in report.items():
        if k != "api":
            print(f"{k} -> {v.get('landed')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=["login", "discover"], required=True)
    ap.add_argument("--timeout", type=int, default=60, help="minutes to wait for sign-in")
    a = ap.parse_args()
    if a.phase == "discover":
        return asyncio.run(discover())
    return asyncio.run(login(a.timeout))


if __name__ == "__main__":
    sys.exit(main())
