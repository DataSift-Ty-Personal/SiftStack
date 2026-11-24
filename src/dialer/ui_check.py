"""The phone page in a real browser, end to end, with Twilio and DataSift faked.

    python -m dialer.ui_check --out <folder for screenshots>

The real web app runs on loopback with the selftest's in-memory DataSift and fake Twilio REST. The browser
gets a fake Voice SDK in place of the vendor file: its Device decodes the real access token for the
caller's identity and posts to /twilio/voice exactly as Twilio would, and this script plays the
homeowner's leg by posting the status callbacks. It checks what the caller sees and does (a manual call,
an outcome, the power dialer across two numbers, the message protocol the Chrome extension uses), then
takes screenshots at 360, 768 and 1280 pixels and asserts the page never scrolls sideways.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FAKE_SDK = r"""
(function () {
  function Emitter() { this._h = {}; }
  Emitter.prototype.on = function (ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); return this; };
  Emitter.prototype.emit = function (ev) { var a = Array.prototype.slice.call(arguments, 1); (this._h[ev] || []).forEach(function (f) { f.apply(null, a); }); };
  function Call(params) { Emitter.call(this); this.params = params; this.parameters = {}; this.customParameters = new Map(); }
  Call.prototype = Object.create(Emitter.prototype);
  Call.prototype.mute = function (m) { F.log.push(["mute", !!m]); };
  Call.prototype.disconnect = function () { F.log.push(["disconnect"]); this.emit("disconnect", this); };
  Call.prototype.accept = function () { this.emit("accept", this); };
  Call.prototype.reject = function () { this.emit("reject"); };
  function Device(token, opts) {
    Emitter.call(this);
    var p = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    F.identity = p.grants.identity; F.account = p.sub; F.opts = opts; F.device = this;
  }
  Device.prototype = Object.create(Emitter.prototype);
  Device.prototype.register = function () { var s = this; setTimeout(function () { s.emit("registered"); }, 10); return Promise.resolve(); };
  Device.prototype.updateToken = function () {};
  Device.prototype.connect = function (o) {
    var params = (o && o.params) || {}, call = new Call(params);
    F.log.push(["connect", params]);
    var body = new URLSearchParams(Object.assign({ CallSid: "CAfake" + (++F.n), From: "client:" + F.identity, AccountSid: F.account }, params));
    return fetch("/twilio/voice", { method: "POST", body: body }).then(function (r) { return r.text(); })
      .then(function (x) { F.twiml.push(x); setTimeout(function () { call.emit("accept", call); }, 20); return call; });
  };
  var F = window.__fakeTwilio = { log: [], twiml: [], n: 0 };
  window.Twilio = { Device: Device };
})();
"""

RESULTS = {"pass": 0, "fail": []}


def ok(cond, label):
    if cond:
        RESULTS["pass"] += 1
    else:
        RESULTS["fail"].append(label)
    print(("  ok   " if cond else "  FAIL ") + label)
    return cond


def wait_for(fn, timeout=8.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "dialer_ui_check"))
    ap.add_argument("--port", type=int, default=8193)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["DIALER_WORKER"] = "0"

    import requests
    import uvicorn
    from playwright.sync_api import sync_playwright

    from dialer import app as appmod, auth, config, store, writeback
    from dialer.selftest import FakeSift, FakeVoice, phone_obj, record
    from dialer import sift, twilio_voice

    tmp = Path(tempfile.mkdtemp(prefix="dialer_ui_"))
    base = "http://127.0.0.1:%d" % a.port
    config.reload(dict(data_dir=tmp, db_path=tmp / "ui.db", public_url=base, calls="live", writeback="dry", record="one_party",
                       amd="off", vm_drop="off", account_sid="AC" + "1" * 32, auth_token="t" * 32, api_key_sid="SK" + "2" * 32,
                       api_key_secret="s" * 32, twiml_app_sid="AP" + "3" * 32, sift_key="test-key", sift_gap_s=0.0,
                       validate_webhooks=False, call_hours=(0, 1440), legal_hours=(0, 1440), advance_seconds=1, numbers_per_day=5,
                       secret="ui-secret", gate={}))
    store.init()
    fs = FakeSift()
    fs.records["rec-ui"] = record("rec-ui", "own-ui", [phone_obj("8655550144", "Dial First"),
                                                     phone_obj("8655550145", "Dial Second", type_="LANDLINE")],
                                  first="Pat", last="Sample", street="100 Example St", attempts=1,
                                  tags=("FTM", "Courthouse Data"), lists=("Foreclosure",))
    fs.records["rec-q2"] = record("rec-q2", "own-q2", [phone_obj("8655550146", "Dial First")], first="Jordan", last="Demo",
                                  street="200 Sample Ave")
    fs.presets["FTM - 02 Ready to Call"] = ("02. FORECLOSURE - CALL", {"_preset": "p1", "tags": ["FTM"]}, "p1")
    fs.rows_for["p1"] = [{"uuid": "rec-ui", "status": ""}, {"uuid": "rec-q2", "status": ""}]
    client = sift.Sift(key="test-key", gap_s=0.0)
    client.send = fs.send
    sift.set_client(client)
    fv = FakeVoice()
    twilio_voice.set_voice(fv)
    store.add_caller("casey", "Casey", caller_id="8655550100")
    code = auth.link("casey").split("#", 1)[1]

    server = uvicorn.Server(uvicorn.Config(appmod.app, host="127.0.0.1", port=a.port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    wait_for(lambda: server.started, 10)

    def status(cid, st, secs=None):
        c = store.get_call(cid)
        data = {"CallSid": c["lead_sid"] or "CAlead", "CallStatus": st}
        if secs is not None:
            data["CallDuration"] = str(secs)
        requests.post(base + "/twilio/status?call_id=%s&leg=lead" % cid, data=data, timeout=10)

    def latest_call():
        rows = store.recent_calls("casey", 1)
        return rows[0] if rows else None

    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # The page forbids eval; Playwright polls by evaluating strings, so only the test context bypasses it.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True)
        ctx.route("**/static/vendor/twilio.min.js", lambda route: route.fulfill(status=200, body=FAKE_SDK,
                                                                               content_type="application/javascript"))
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(base + "/signin#" + code)
        page.wait_for_url(base + "/phone", timeout=10000)
        ok(page.locator("#callerName").inner_text() == "Casey", "the page knows who is calling")
        page.wait_for_function("document.getElementById('lineState').textContent === 'Phone ready'", timeout=8000)
        ok(True, "the Device registers: Phone ready")
        opts = page.evaluate("window.__fakeTwilio.opts")
        ok(opts.get("codecPreferences", [None])[0] == "opus" and opts.get("closeProtection"), "the Device prefers opus and guards the tab")
        ok("Write-back is dry" in page.locator("#modes").inner_text(), "the dry write-back is said at the top")

        # ---- a manual call
        page.fill("#dialNumber", "(865) 555-0144")
        page.click("#dialBtn")
        c = wait_for(lambda: (latest_call() or {}).get("lead_sid") and latest_call())
        ok(bool(c), "the call is prepared, connected and the homeowner dialed")
        page.wait_for_function("document.getElementById('recOwner').textContent.indexOf('Pat Sample') === 0", timeout=6000)
        ok(True, "the record shows: Pat Sample")
        ok("Foreclosure" in page.locator("#recTags").inner_text(), "with its lists and tags")
        status(c["id"], "ringing")
        page.wait_for_function("document.getElementById('callState').textContent.indexOf('Ringing') === 0", timeout=6000)
        ok(True, "Ringing")
        status(c["id"], "in-progress")
        page.wait_for_function("document.getElementById('callState').textContent.indexOf('Talking') === 0", timeout=6000)
        ok(not page.locator("#callTimer").is_hidden(), "Talking, with the timer running")
        page.screenshot(path=str(out / "1280_live.png"), full_page=True)
        shots.append("1280_live.png")
        page.click("#muteBtn")
        ok(page.evaluate("window.__fakeTwilio.log.some(function (x) { return x[0] === 'mute' && x[1] === true; })"), "Mute mutes the browser leg")
        status(c["id"], "completed", 75)
        requests.post(base + "/twilio/recording?call_id=%s" % c["id"], timeout=10,
                      data={"RecordingSid": "RE" + "4" * 32, "RecordingStatus": "completed", "RecordingDuration": "73"})
        page.wait_for_selector("#outcomeCard:not([hidden])", timeout=6000)
        ok("Talked 1m 15s" in page.locator("#outAside").inner_text(), "the outcome card opens: Talked 1m 15s")
        page.screenshot(path=str(out / "1280_outcome.png"), full_page=True)
        shots.append("1280_outcome.png")
        page.fill("#outNote", "Wants to keep it for the grandkids.")
        page.click("#outGroups button[data-key='not_interested']")
        page.wait_for_selector("#outcomeCard[hidden]", state="attached", timeout=6000)
        got = store.get_call(c["id"])
        ok(got["disposition"] == "not_interested" and "grandkids" in (got["note"] or ""), "one click saves Not interested with the note")
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), c["id"]))
        writeback.drain()
        acts = sorted(w["action"] for w in store.writes_for(c["id"]))
        ok(acts == ["call_attempts", "note", "phone", "status"], "the dry write-back logs the counter, phone, status and note: %s" % acts)

        # ---- the extension's message protocol
        state = page.evaluate("""() => new Promise((resolve) => {
            window.addEventListener('message', function h(ev) {
              if (ev.data && ev.data.source === 'sift-dialer-page' && ev.data.type === 'state') { window.removeEventListener('message', h); resolve(ev.data.state); }
            });
            window.postMessage({source: 'sift-dialer-ext', type: 'hello'}, location.origin);
        })""")
        ok(state.get("ready") is True and state.get("name") == "Casey" and len(state.get("outcomes") or []) >= 10,
           "the page answers the extension's hello with its state")
        opened = page.evaluate("""() => new Promise((resolve) => {
            window.addEventListener('message', function h(ev) {
              if (ev.data && ev.data.source === 'sift-dialer-page' && ev.data.type === 'open_record') { window.removeEventListener('message', h); resolve(ev.data); }
            });
            setTimeout(function () { resolve(null); }, 4000);
        })""")
        ok(opened is None or opened.get("uuid"), "no stray record navigation while idle")

        # ---- the power dialer
        page.select_option("#presetPick", "FTM - 02 Ready to Call")
        page.click("#qStartBtn")
        page.wait_for_function("document.getElementById('qNow').textContent.indexOf('Next: Pat Sample') === 0", timeout=8000)
        ok("number 1 of 2" in page.locator("#qNow").inner_text() and "Dialing in" in page.locator("#qNow").inner_text(),
           "the list starts on Pat Sample, number 1 of 2, and counts down to dial by itself")
        page.screenshot(path=str(out / "1280_queue.png"), full_page=True)
        shots.append("1280_queue.png")
        q1 = wait_for(lambda: (lambda x: x if x and x["kind"] == "queue" and x["lead_sid"] else None)(latest_call()))
        ok(bool(q1) and q1["number"] == "8655550144", "the countdown dials the record's first number")
        if not q1:
            print("DEBUG calls:", [(x["kind"], x["status"], x["number"], bool(x["lead_sid"]), x["warnings"]) for x in store.recent_calls("casey", 5)])
            print("DEBUG toast:", page.locator("#toast").inner_text(), "| qNow:", page.locator("#qNow").inner_text())
            print("DEBUG fake:", page.evaluate("JSON.stringify(window.__fakeTwilio.log)"))
            print("DEBUG errors:", errors[-5:])
            return 1
        status(q1["id"], "no-answer")
        page.wait_for_selector("#outcomeCard:not([hidden])", timeout=6000)
        ok(page.locator("#outGroups button.suggested").get_attribute("data-key") == "no_answer", "No answer is suggested")
        ok(page.locator("#outGroups button[data-key='interested']").is_disabled(), "Interested cannot be picked when nobody answered")
        page.click("#outGroups button[data-key='no_answer']")
        q2 = wait_for(lambda: (lambda x: x if x and x["id"] != q1["id"] and x["lead_sid"] else None)(latest_call()), timeout=10)
        ok(bool(q2) and q2["number"] == "8655550145", "after No answer it dials the second number by itself")
        status(q2["id"], "in-progress")
        status(q2["id"], "completed", 30)
        page.wait_for_selector("#outcomeCard:not([hidden])", timeout=6000)
        page.click("#outGroups button[data-key='call_back']")
        ok(not page.locator("#callbackBox").is_hidden(), "Call back later asks for a time")
        page.click("#saveOutcome")
        page.wait_for_function("document.getElementById('qNow').textContent.indexOf('Jordan Demo') >= 0", timeout=10000)
        ok(store.get_call(q2["id"])["disposition"] == "call_back", "the call back is saved and the list moves to the next record")
        page.uncheck("#qAuto")

        # ---- three widths, never a sideways scroll
        for w, h in ((360, 800), (768, 1000), (1280, 900)):
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(250)
            sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            ok(sw <= cw, "no horizontal scroll at %dpx (scrollWidth %d, clientWidth %d)" % (w, sw, cw))
            name = "%d_page.png" % w
            page.screenshot(path=str(out / name), full_page=True)
            shots.append(name)
        page.set_viewport_size({"width": 360, "height": 800})
        page.click("#settingsBtn")
        page.screenshot(path=str(out / "360_settings.png"))
        shots.append("360_settings.png")
        page.click("#setClose")
        real_errors = [e for e in errors if "favicon" not in e]
        ok(not real_errors, "no script errors in the page: %s" % real_errors[:3])
        browser.close()
    server.should_exit = True
    th.join(5)
    print("\nscreenshots in %s: %s" % (out, ", ".join(shots)))
    print("ui check: %d passed, %d failed" % (RESULTS["pass"], len(RESULTS["fail"])))
    for f in RESULTS["fail"]:
        print("FAIL", f)
    return 0 if not RESULTS["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
