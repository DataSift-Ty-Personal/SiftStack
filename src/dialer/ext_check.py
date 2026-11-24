"""The Chrome extension, loaded for real, against a stand-in DataSift page and the dialer on loopback.

    python -m dialer.ext_check [--headed]

Chromium starts with extensions/sift-dialer loaded. Requests to https://app.reisift.io are answered with a
small record page (phone numbers in spans, one in an input box, one added after the page loads, the way
a React page redraws); the dialer runs on loopback with the selftest's fakes. It checks: Call buttons appear
beside the numbers and not in the input; clicking one dials through the dialer tab, with the record id from
the address bar; the call bar inside the DataSift page shows the live call; an outcome picked in the bar
is saved by the dialer.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
EXT = SRC.parent / "extensions" / "sift-dialer"
REC = "11111111-2222-3333-4444-555555555555"

FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>DataSift</title></head><body>
<div id="root"><div class="record"><h1>Pat Sample</h1><div>100 Example St, Knoxville, TN</div>
<div class="phones"><div class="phone"><span>(865) 555-0144</span> <span class="tag">Dial First</span></div>
<div class="phone"><span>865-555-0145</span></div></div>
<label>Add a phone <input id="newphone" value="(865) 555-0199"></label></div></div>
<script>setTimeout(function () { var d = document.createElement('div'); d.className = 'phone';
d.innerHTML = '<span>(865) 555-0177</span>'; document.querySelector('.phones').appendChild(d); }, 700);</script>
</body></html>"""

RESULTS = {"pass": 0, "fail": []}


def ok(cond, label):
    if cond:
        RESULTS["pass"] += 1
    else:
        RESULTS["fail"].append(label)
    print(("  ok   " if cond else "  FAIL ") + label)
    return cond


def wait_for(fn, timeout=10.0, step=0.15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--port", type=int, default=8194)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["DIALER_WORKER"] = "0"
    import requests
    import uvicorn
    from playwright.sync_api import sync_playwright

    from dialer import app as appmod, auth, config, sift, store, twilio_voice
    from dialer.selftest import FakeSift, FakeVoice, phone_obj, record
    from dialer.ui_check import FAKE_SDK

    tmp = Path(tempfile.mkdtemp(prefix="dialer_ext_"))
    base = "http://127.0.0.1:%d" % a.port
    config.reload(dict(data_dir=tmp, db_path=tmp / "ext.db", public_url=base, calls="live", writeback="dry",
                       account_sid="AC" + "1" * 32, auth_token="t" * 32, api_key_sid="SK" + "2" * 32, api_key_secret="s" * 32,
                       twiml_app_sid="AP" + "3" * 32, sift_key="test-key", sift_gap_s=0.0, validate_webhooks=False,
                       call_hours=(0, 1440), legal_hours=(0, 1440), secret="ext-secret", gate={}))
    store.init()
    fs = FakeSift()
    fs.records[REC] = record(REC, "own-x", [phone_obj("8655550144", "Dial First"), phone_obj("8655550145", "Dial Second")])
    client = sift.Sift(key="test-key", gap_s=0.0)
    client.send = fs.send
    sift.set_client(client)
    twilio_voice.set_voice(FakeVoice())
    store.add_caller("casey", "Casey", caller_id="8655550100")
    code = auth.link("casey").split("#", 1)[1]
    server = uvicorn.Server(uvicorn.Config(appmod.app, host="127.0.0.1", port=a.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_for(lambda: server.started, 10)

    profile = tempfile.mkdtemp(prefix="dialer_ext_profile_")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chromium", headless=not a.headed, bypass_csp=True,
            args=["--disable-extensions-except=%s" % EXT, "--load-extension=%s" % EXT])
        ctx.route("**/static/vendor/twilio.min.js", lambda r: r.fulfill(status=200, body=FAKE_SDK, content_type="application/javascript"))
        ctx.route("https://app.reisift.io/**", lambda r: r.fulfill(status=200, body=FIXTURE, content_type="text/html"))
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = sw.url.split("/")[2]
        ok(bool(ext_id), "the extension loads (id %s)" % ext_id)

        opt = ctx.new_page()
        opt.goto("chrome-extension://%s/options.html" % ext_id)
        opt.fill("#dialerUrl", base)
        opt.click("#save")
        opt.wait_for_function("document.getElementById('msg').textContent.length > 0", timeout=8000)
        ok(opt.locator("#msg").inner_text() == "Saved.", "the options page saves the dialer's address: %s" % opt.locator("#msg").inner_text())
        opt.close()

        phone = ctx.new_page()
        phone.goto(base + "/signin#" + code)
        phone.wait_for_url(base + "/phone", timeout=10000)
        phone.wait_for_function("document.getElementById('lineState').textContent === 'Phone ready'", timeout=8000)
        ok(True, "the dialer tab is signed in and ready")

        sift_page = ctx.new_page()
        sift_page.goto("https://app.reisift.io/records/properties/%s" % REC)
        sift_page.wait_for_selector("button[data-sift-dialer]", timeout=8000)
        sift_page.wait_for_timeout(1500)
        nums = sift_page.eval_on_selector_all("button[data-sift-dialer]", "els => els.map(e => e.dataset.number)")
        ok("8655550144" in nums and "8655550145" in nums, "Call buttons appear beside the numbers: %s" % nums)
        ok("8655550177" in nums, "a number DataSift adds after the page loads gets one too")
        ok("8655550199" not in nums, "a number typed in an input box does not")
        ok(len(nums) == len(set(nums)), "one button per number")
        sift_page.click("button[data-sift-dialer][data-number='8655550144']")
        c = wait_for(lambda: (lambda r: r[0] if r and r[0]["lead_sid"] else None)(store.recent_calls("casey", 1)))
        ok(bool(c) and c["number"] == "8655550144" and c["record_uuid"] == REC,
           "clicking Call dials through the dialer tab, on the record in the address bar")
        if c:
            requests.post(base + "/twilio/status?call_id=%s&leg=lead" % c["id"], data={"CallSid": c["lead_sid"], "CallStatus": "in-progress"})
            bar_text = lambda: sift_page.evaluate("() => { var h = document.querySelector('div[data-sift-dialer]'); return h && h.shadowRoot ? h.shadowRoot.textContent : ''; }")
            ok(wait_for(lambda: "Talking" in bar_text()), "the call bar inside DataSift shows the live call")
            ok("Hang up" in bar_text(), "with its controls")
            requests.post(base + "/twilio/status?call_id=%s&leg=lead" % c["id"],
                          data={"CallSid": c["lead_sid"], "CallStatus": "completed", "CallDuration": "42"})
            requests.post(base + "/twilio/recording?call_id=%s" % c["id"],
                          data={"RecordingSid": "RE" + "6" * 32, "RecordingStatus": "completed", "RecordingDuration": "40"})
            ok(wait_for(lambda: "How did it go?" in bar_text()), "when it ends, the bar offers the outcomes")
            if a.out:
                sift_page.screenshot(path=str(Path(a.out) / "ext_bar.png"))
            sift_page.evaluate("""() => { var h = document.querySelector('div[data-sift-dialer]');
                var b = Array.prototype.find.call(h.shadowRoot.querySelectorAll('button'), x => x.textContent === 'Not interested');
                b.click(); }""")
            got = wait_for(lambda: (store.get_call(c["id"]) or {}).get("disposition"))
            ok(got == "not_interested", "an outcome picked in the bar is saved by the dialer (%s)" % got)
        ctx.close()
    server.should_exit = True
    print("ext check: %d passed, %d failed" % (RESULTS["pass"], len(RESULTS["fail"])))
    for f in RESULTS["fail"]:
        print("FAIL", f)
    return 0 if not RESULTS["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
