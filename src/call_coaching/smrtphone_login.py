"""smrtphone_login.py - capture a SmrtPhone browser session for pull_calls.py.

Opens a headed Chromium at phone.smrt.studio, waits for YOU to log in by hand
(SSO, 2FA, whatever the tenant requires), then writes the Playwright storage
state to SiftStack/smrtphone_state.json - the file pull_calls.py reads.

Replaces the _api/smrtphone_login.py referenced in the Deal Room Coaching Call
project, which is not present on this machine.

USAGE (from the SiftStack root):
  .venv/bin/python src/call_coaching/smrtphone_login.py
  .venv/bin/python src/call_coaching/smrtphone_login.py --timeout 600

The script polls until the app shell is reachable, so just log in and leave the
window alone - it saves and closes itself. Sessions expire; re-run when
pull_calls.py exits 2.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "smrtphone_state.json"
BASE = "https://phone.smrt.studio"
LANDING = BASE + "/logs/calls"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


AUTH_PAGES = ("login", "sign-in", "signin", "two-factor", "2fa", "otp", "verify",
              "password", "forgot")


def logged_in(page) -> bool:
    """True once the URL is on the app and off every auth page.

    URL-only on purpose: the app shell renders inside `main-iframe`, so
    top-level DOM checks see an empty page even when fully logged in.
    """
    url = (page.url or "").lower()
    if "smrt.studio" not in url:
        return False
    return not any(tok in url for tok in AUTH_PAGES)


def capture(timeout_s: int) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      user_agent=UA)
        page = context.new_page()
        print(f"Opening {LANDING}")
        print("Log in in the browser window. This script saves and closes itself.\n")
        try:
            page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"WARN: initial navigation failed ({e}); continue in the window.")

        try:
            page.bring_to_front()
        except Exception:
            pass

        deadline = time.time() + timeout_s
        last_url = ""
        while time.time() < deadline:
            try:
                if logged_in(page):
                    break
                url = page.url or ""
                if url != last_url:
                    print(f"\n  now at: {url}", flush=True)
                    last_url = url
            except Exception:
                pass  # mid-navigation; retry
            remaining = int(deadline - time.time())
            print(f"  waiting for login... {remaining}s left", end="\r", flush=True)
            time.sleep(2)
        else:
            # Timed out - but if a session exists anyway, keep it.
            state = context.storage_state()
            smrt = [c for c in state.get("cookies", [])
                    if "smrt.studio" in (c.get("domain") or "")]
            if smrt:
                STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
                STATE.chmod(0o600)
                print(f"\nTIMED OUT waiting for the app URL, but found {len(smrt)} "
                      f"smrt.studio cookies - saved them to {STATE} anyway. "
                      "Test with pull_calls.py --list.")
                browser.close()
                return 0
            print("\nTIMED OUT - no session captured. Re-run with a longer --timeout.")
            browser.close()
            return 1

        # Settle, then make sure the call log itself loaded under the session.
        print("\nLogged in. Loading the call log to confirm the session...")
        try:
            page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"WARN: could not reload the call log ({e}); saving anyway.")

        state = context.storage_state()
        smrt_cookies = [c for c in state.get("cookies", [])
                        if "smrt.studio" in (c.get("domain") or "")]
        if not smrt_cookies:
            print("ERROR: no smrt.studio cookies in the session. Nothing saved.")
            browser.close()
            return 1

        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        STATE.chmod(0o600)
        browser.close()

    print(f"Saved {len(smrt_cookies)} smrt.studio cookies -> {STATE}")
    print("Next: .venv/bin/python src/call_coaching/pull_calls.py --list")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for you to finish logging in (default 300)")
    args = ap.parse_args()
    return capture(args.timeout)


if __name__ == "__main__":
    sys.exit(main())
