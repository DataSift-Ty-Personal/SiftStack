"""The dialer's selftest: every path end to end, zero network.

    python -m dialer.selftest            (from src/, with the repo .venv)

A fake Twilio (FakeVoice) and an in-memory DataSift (FakeSift, which answers the same routes the real
client calls) stand in for both vendors, and a guard refuses any real socket, so this is safe to run on a
machine whose .env holds live keys. The web app is driven through FastAPI's test client exactly as a
browser and Twilio would drive it: a sign-in link, the session cookie, a signed Twilio webhook, the
status callbacks in order, a disposition, and then the DataSift writes it produced are read back from the
fake and compared field by field.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS = {"pass": 0, "fail": 0, "failures": []}
SECTION = [""]


def ok(cond, label: str) -> bool:
    if cond:
        RESULTS["pass"] += 1
    else:
        RESULTS["fail"] += 1
        RESULTS["failures"].append("%s: %s" % (SECTION[0], label))
    return bool(cond)


def eq(got, want, label: str) -> bool:
    return ok(got == want, "%s (got %r, want %r)" % (label, got, want))


# ---------------------------------------------------------------------- guards
class NoNetwork(Exception):
    pass


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    return str(host) in LOOPBACK


def _no_connect(self, address, *a, **k):
    # asyncio's own event loop opens a loopback socket pair on Windows (the test client needs it);
    # anything that leaves the machine is refused.
    if _is_loopback(address):
        return _REAL_CONNECT(self, address)
    raise NoNetwork("the selftest may not open a network connection to %r" % (address,))


def _no_connect_ex(self, address, *a, **k):
    if _is_loopback(address):
        return _REAL_CONNECT_EX(self, address)
    raise NoNetwork("the selftest may not open a network connection to %r" % (address,))


def install_guards():
    socket.socket.connect = _no_connect          # type: ignore[assignment]
    socket.socket.connect_ex = _no_connect_ex    # type: ignore[assignment]
    try:
        import requests

        def blocked(*a, **k):
            raise NoNetwork("requests is blocked in the selftest; patch the call you mean")
        requests.sessions.Session.request = blocked   # type: ignore[assignment]
    except ImportError:
        pass


# ---------------------------------------------------------------------- fakes
class FakeVoice:
    def __init__(self):
        self.ops = []
        self.n = 0
        self.fail_dial = None
        self.fetch = {}

    def _sid(self, p):
        self.n += 1
        return "%s%030d" % (p, self.n)

    def dial_lead(self, call, from_number, to_e164, record, amd, ring_seconds):
        if self.fail_dial:
            raise RuntimeError(self.fail_dial)
        sid, conf = self._sid("CA"), self._sid("CF")
        self.ops.append(("dial_lead", call["id"], from_number, to_e164, record, amd, ring_seconds))
        return {"call_sid": sid, "conference_sid": conf}

    def add_participant(self, conference, from_number, to, label, **kw):
        self.ops.append(("add_participant", conference, from_number, to, label, kw))
        return {"call_sid": self._sid("CA"), "conference_sid": conference}

    def update_participant(self, conference_sid, call_sid, **kw):
        self.ops.append(("update_participant", conference_sid, call_sid, kw))

    def hold(self, conference_sid, call_sid, on):
        self.ops.append(("hold", conference_sid, call_sid, on))

    def remove_participant(self, conference_sid, call_sid):
        self.ops.append(("remove_participant", conference_sid, call_sid))

    def redirect(self, call_sid, twiml):
        self.ops.append(("redirect", call_sid, twiml))

    def end_conference(self, conference_sid):
        self.ops.append(("end_conference", conference_sid))

    def end(self, call_sid):
        self.ops.append(("end", call_sid))

    def fetch_call(self, call_sid):
        return self.fetch.get(call_sid, {"status": "in-progress"})

    def recording_bytes(self, sid, ext="mp3"):
        self.ops.append(("recording_bytes", sid))
        return b"ID3" + b"\x00" * 64

    def kinds(self):
        return [o[0] for o in self.ops]


def phone_obj(number, tier="", status="UNKNOWN", type_="MOBILE", extra_tags=()):
    tags = ([{"title": tier}] if tier else []) + [{"title": t} for t in extra_tags]
    return {"number": number, "type": type_, "status": status, "tags": tags, "is_connected": True}


def record(uuid, owner_uuid, phones, *, tags=(), lists=(), status="", attempts=0, mail_state="TN", state="TN",
           first="Pat", last="Sample", deceased=False, street="100 Example St"):
    return {"uuid": uuid, "status": status, "tags": [{"title": t} for t in tags], "lists": [{"title": x} for x in lists],
            "predictivecall_attempts": attempts, "mls": "",
            "address": {"street": street, "city": "Knoxville", "state": state, "postal_code": "37900"},
            "owner": {"uuid": owner_uuid, "first_name": first, "last_name": last, "deceased": deceased,
                      "address": {"street": "200 Sample Ave", "state": mail_state}, "phones": phones}}


class FakeSift:
    """Answers the routes sift.Sift calls, in memory, and records every write."""

    def __init__(self):
        self.records = {}
        self.presets = {}          # title -> (folder, must, uuid)
        self.rows_for = {}         # preset uuid -> list of search rows
        self.writes = []
        self.tasks = []
        self.messages = []
        self.fail_next = {}        # route fragment -> HTTP code, once

    def owner_record(self, owner_uuid):
        return [r for r in self.records.values() if (r.get("owner") or {}).get("uuid") == owner_uuid]

    def send(self, method, url, headers, body):
        path = url.split("apiv2.reisift.io", 1)[-1]
        for frag, code in list(self.fail_next.items()):
            if frag in path:
                del self.fail_next[frag]
                return code, '{"detail": "simulated"}', {}
        import re
        m = re.match(r"^/api/internal/property/([0-9a-z-]+)/$", path)
        if m and method == "GET":
            r = self.records.get(m.group(1))
            return (200, json.dumps(r), {}) if r else (404, "{}", {})
        if path == "/api/internal/property/" and method == "POST":
            must = (body or {}).get("query", {}).get("must", {})
            if "search" in must:
                d = must["search"]
                rows = [{"uuid": r["uuid"], "status": r["status"], "phone": {"number": d}} for r in self.records.values()
                        if any(p["number"] == d for p in r["owner"]["phones"])]
            else:
                rows = list(self.rows_for.get(must.get("_preset"), []))
            off, lim = body.get("offset", 0), body.get("limit", 250)
            return 200, json.dumps({"count": len(rows), "results": rows[off:off + lim]}), {}
        if path.startswith("/api/internal/filter-preset-folder/?"):
            folders = sorted({v[0] for v in self.presets.values()})
            return 200, json.dumps({"results": [{"uuid": "fold-%d" % i, "title": f} for i, f in enumerate(folders)]}), {}
        m = re.match(r"^/api/internal/filter-preset-folder/fold-(\d+)/filter-preset/", path)
        if m:
            folders = sorted({v[0] for v in self.presets.values()})
            f = folders[int(m.group(1))]
            out = [{"uuid": v[2], "title": t, "filters": {"must": v[1]}} for t, v in self.presets.items() if v[0] == f]
            return 200, json.dumps({"results": out}), {}
        m = re.match(r"^/api/internal/owner/([0-9a-z-]+)/upsert-phones/$", path)
        if m:
            self.writes.append(("upsert_phone", m.group(1), body["phones"][0]))
            for r in self.owner_record(m.group(1)):
                ph = r["owner"]["phones"]
                for i, p in enumerate(ph):
                    if p["number"] == body["phones"][0]["number"]:
                        newp = dict(body["phones"][0])
                        newp["tags"] = [{"title": t} for t in newp.get("tags", [])]
                        ph[i] = newp
            return 200, json.dumps({"added": [], "removed": []}), {}
        m = re.match(r"^/api/internal/property/([0-9a-z-]+)/(predictivecall-attempts|message|status|add-tags|add-lists)/$", path)
        if m:
            uid, what = m.group(1), m.group(2)
            r = self.records[uid]
            self.writes.append((what, uid, body))
            if what == "predictivecall-attempts":
                r["predictivecall_attempts"] = body["predictivecall_attempts"]
                return 204, "", {}
            if what == "message":
                self.messages.append((uid, body["message"]))
                return 201, json.dumps({"uuid": "msg-%d" % len(self.messages)}), {}
            if what == "status":
                r["status"] = body["status"]
            if what == "add-tags":
                r["tags"].append({"title": body["tags"]})
            if what == "add-lists":
                r["lists"].append({"title": body["lists"]})
            return 200, "{}", {}
        if path == "/api/internal/task/" and method == "POST":
            self.tasks.append(body)
            self.writes.append(("task", body.get("assigned_to_property"), body))
            return 201, json.dumps({"uuid": "task-%d" % len(self.tasks)}), {}
        return 404, '{"detail": "no such route in the fake: %s %s"}' % (method, path), {}

    def count(self, what):
        return sum(1 for w in self.writes if w[0] == what)


# ---------------------------------------------------------------------- setup
def fresh(tmp: Path, **over):
    from dialer import config, sift, store, twilio_voice
    db = tmp / ("db_%d.sqlite" % (len(list(tmp.glob("db_*.sqlite"))) + 1))
    base = dict(data_dir=tmp, db_path=db, public_url="https://dialer.example.test", calls="live", writeback="live",
                record="one_party", amd="off", vm_drop="off", account_sid="AC" + "1" * 32, auth_token="t" * 32,
                api_key_sid="SK" + "2" * 32, api_key_secret="s" * 32, twiml_app_sid="AP" + "3" * 32,
                sift_key="test-key", sift_gap_s=0.0, texting_url="", texting_token="", kpi_url="", slack_webhook="",
                handraise_token="hr-token", secret="selftest-secret", test_numbers=(), gate={})
    base.update(over)
    config.reload(base)
    store.init()
    fs = FakeSift()
    client = sift.Sift(key="test-key", gap_s=0.0)
    client.send = fs.send
    sift.set_client(client)
    fv = FakeVoice()
    twilio_voice.set_voice(fv)
    return fs, fv


AT_9_30_ET = datetime(2026, 11, 18, 14, 30, tzinfo=timezone.utc)     # a Wednesday, 9:30am in Knoxville
AT_8_ET = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)         # 8:00am Knoxville
AT_8_30_PM_ET = datetime(2026, 11, 19, 1, 30, tzinfo=timezone.utc)   # 8:30pm Knoxville


def signed_post(tc, path, params, *, bad=False):
    from twilio.request_validator import RequestValidator
    from dialer import config
    full = config.S.public_url + path
    sig = RequestValidator(config.S.auth_token).compute_signature(full, params)
    if bad:
        sig = sig[:-4] + "AAAA"
    return tc.post(path, data=params, headers={"X-Twilio-Signature": sig})


def signin(tc, identity):
    from dialer import auth
    link = auth.link(identity)
    code = link.split("#", 1)[1]
    r = tc.post("/api/signin", json={"code": code}, headers={"X-Dialer": "1"})
    return r


# ---------------------------------------------------------------------- sections
def sec_phone():
    from dialer import phone
    eq(phone.digits("+1 (865) 555-0144"), "8655550144", "digits strips +1 and punctuation")
    eq(phone.e164("865.555.0144"), "+18655550144", "e164")
    eq(phone.pretty("8655550144"), "(865) 555-0144", "pretty")
    ok("America/Chicago" in phone.zones("4235550144") and "America/New_York" in phone.zones("4235550144"),
       "423 straddles Eastern and Central, both are checked")
    eq(phone.zones("8655550144"), ["America/New_York"], "865 is Eastern only")
    eq(phone.state("8655550144"), "TN", "865 is Tennessee")
    eq(phone.state("4155550144"), "CA", "415 is California")
    ok(not phone.valid("1235550144"), "an area code cannot start with 1")
    ok(not phone.valid("9005550144"), "900 lines are refused")
    ok(phone.valid("8655550144"), "a Knoxville number is valid")
    ok(not phone.valid("8095550144"), "a Dominican Republic number (809) is refused")
    ok(not phone.valid("4165550144"), "a Toronto number is refused")
    eq(phone.state("2065550152"), "WA", "Seattle is Washington (libphonenumber says Washington State)")
    eq(phone.state_code("Tennessee"), "TN", "state names map to codes")


def sec_rules(tmp):
    from dialer import config, rules, store
    fresh(tmp)
    ok(rules.hours("8655550144", AT_9_30_ET)[0], "9:30am Knoxville is inside the house hours")
    ok(not rules.hours("8655550144", AT_8_ET)[0], "8:00am Knoxville is before the house hours")
    ok(not rules.hours("4235550144", AT_9_30_ET)[0], "a 423 number may be Central (8:30am there): it waits")
    ok(not rules.hours("4155550144", AT_9_30_ET)[0], "6:30am in California is refused")
    ok(not rules.hours("8655550144", AT_8_30_PM_ET)[0], "8:30pm Knoxville is after the house hours")
    rec = record("r1", "o1", [phone_obj("8655550144", "Dial First")])
    c = rules.manual(rec, "8655550144", now=AT_9_30_ET)
    ok(c.ok and not c.ask, "a clean record at 9:30am may be dialed with nothing to confirm")
    ok(c.record_ok, "a Tennessee number with a Tennessee owner is recorded")
    c = rules.manual(rec, "8655550144", now=AT_8_ET)
    ok(not c.ok, "the house hours stop a call at 8am")
    dnm = record("r2", "o2", [phone_obj("8655550145", "Dial First")], tags=("Do Not Market",))
    c = rules.manual(dnm, "8655550145", now=AT_9_30_ET)
    ok(not c.ok and any("Do Not Market" in s for s in c.stop), "Do Not Market stops a manual call")
    dead = record("r3", "o3", [phone_obj("8655550146", "Dial First")], tags=("Verify Deceased",))
    c = rules.manual(dead, "8655550146", now=AT_9_30_ET)
    ok(c.ok and any("died" in s for s in c.ask), "a death on the record asks, it does not stop")
    lead = record("r4", "o4", [phone_obj("8655550147", "Dial First")], status="Hot Lead")
    c = rules.manual(lead, "8655550147", now=AT_9_30_ET)
    ok(c.ok and not c.ask, "a lead manager calls a Hot Lead with nothing to confirm")
    dnc = record("r5", "o5", [phone_obj("8655550148", "Dial First", status="CORRECT_DNC")])
    c = rules.manual(dnc, "8655550148", now=AT_9_30_ET)
    ok(not c.ok and any("do-not-call" in s for s in c.stop), "a phone marked do-not-call stops the call")
    lit = record("r6", "o6", [phone_obj("8655550149", "Dial First", extra_tags=("Litigator",))])
    ok(not rules.manual(lit, "8655550149", now=AT_9_30_ET).ok, "a litigator's number stops the call")
    config.reload(dict(config.S.__dict__, calls="test", test_numbers=("8655550100",)))
    ok(not rules.manual(rec, "8655550144", now=AT_9_30_ET).ok, "test mode stops a number not on the test list")
    ok(rules.manual(None, "8655550100", now=AT_9_30_ET).ok, "test mode dials a test number")
    config.reload(dict(config.S.__dict__, calls="live", test_numbers=()))
    # A number that called us: the house hours and the marketing holds become questions; the law does not.
    cid = store.new_call(identity="a", direction="inbound", kind="inbound", number="8655550145", status="ended")
    with store.tx() as cx:
        cx.execute("UPDATE calls SET created_at=? WHERE id=?", ((AT_8_ET - timedelta(minutes=20)).isoformat(), cid))
    c = rules.manual(dnm, "8655550145", now=AT_8_ET)
    ok(c.ok and c.callback, "a callback at 8am to an owner who called in may go (the legal hours allow it)")
    ok(any("Do Not Market" in s for s in c.ask), "on a callback, Do Not Market becomes a question")
    c = rules.manual(dnm, "8655550145", now=datetime(2026, 11, 18, 12, 30, tzinfo=timezone.utc))
    ok(not c.ok, "a callback at 7:30am is still before the legal hours")
    # Recording consent.
    eq(rules.may_record("8655550144", "TN")[0], True, "TN number and TN owner: recorded")
    eq(rules.may_record("4155550144", "TN")[0], False, "a California number is not recorded")
    eq(rules.may_record("8655550144", "Florida")[0], False, "an owner living in Florida is not recorded")
    config.reload(dict(config.S.__dict__, record="off"))
    eq(rules.may_record("8655550144", "TN")[0], False, "DIALER_RECORD=off records nothing")
    config.reload(dict(config.S.__dict__, record="one_party"))
    # State rules.
    fl = record("r7", "o7", [phone_obj("3055550150", "Dial First")], mail_state="FL")
    eq(rules.state_rule("3055550150", fl).get("max_per_24h"), 3, "Florida caps an owner at 3 dials in 24 hours")
    ok(rules.state_rule("3055550150", fl).get("manual_only"), "Florida waits for the caller's click")
    for i in range(3):
        store.new_call(identity="a", kind="manual", number="3055550150", owner_uuid="o7", status="ended")
    ok(any("24 hours" in s for s in rules.manual(fl, "3055550150", now=AT_9_30_ET).stop), "the fourth dial in 24 hours is stopped")
    sunday = datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc)
    pa = record("r8", "o8", [phone_obj("2155550151", "Dial First")], mail_state="PA")
    ok(any("Sunday" in s for s in rules.manual(pa, "2155550151", now=sunday).stop), "Pennsylvania: no Sunday calls")
    ok(rules.state_rule("2065550152").get("no_prerecorded"), "Washington: no recorded voicemail")
    # The gate settings: the registry flag is honored unless the gate block lifts it.
    row = {"uuid": "r1", "phone": {"number": "8655550144", "doNotCall": True}}
    ok(not rules.queue(rec, row, [], now=AT_9_30_ET)[0], "the power dialer holds a registry-flagged number by default")
    config.reload(dict(config.S.__dict__, gate={"registry_flag": False}))
    ok(rules.queue(rec, row, [], now=AT_9_30_ET)[0], "gate.registry_flag false (the playbook's choice) lifts it")
    config.reload(dict(config.S.__dict__, gate={}))
    eq(rules.allow_for_preset({"any_property_status": ["Hot Lead", "Warm Lead"]}), ["lead"], "a lead preset lifts the lead rule")
    eq(rules.allow_for_preset({"any_property_status": ["not_interested"]}), ["not_interested"],
       "a second-chance preset lifts Not Interested")
    eq(rules.allow_for_preset({"tags": ["FTM"]}), [], "a prospecting preset lifts nothing")
    order = rules.order_numbers([{"number": "1", "tier": "Dial Second", "status": "UNKNOWN", "type": "MOBILE"},
                                 {"number": "2", "tier": "Dial First", "status": "UNKNOWN", "type": "LANDLINE"},
                                 {"number": "3", "tier": "", "status": "CORRECT", "type": "LANDLINE"},
                                 {"number": "4", "tier": "Dial First", "status": "UNKNOWN", "type": "MOBILE"}])
    eq([p["number"] for p in order], ["3", "4", "2", "1"], "confirmed first, then tier, mobiles first in a tier")


def sec_dispositions():
    from dialer import dispositions as d
    t = d.TABLE
    eq(d.phone_target(t["do_not_contact"], "CORRECT"), "CORRECT_DNC", "a stop keeps what was known (Correct)")
    eq(d.phone_target(t["do_not_contact"], "UNKNOWN"), "DNC", "a stop on an unknown number is DNC")
    eq(d.phone_target(t["not_interested"], "CORRECT_DNC"), "", "an opt-out is never undone by a later Correct")
    eq(d.phone_target(t["not_interested"], "CORRECT"), "", "Correct twice writes nothing")
    eq(d.phone_target(t["wrong_number"], "UNKNOWN"), "WRONG", "a wrong number marks the phone Wrong")
    eq(d.phone_target(t["relative"], "UNKNOWN"), "", "family is never a wrong number: the status is left alone")
    ok("Relative" in t["relative"].phone_tags, "family is tagged Relative")
    eq(d.status_target(t["call_back"], "Hot Lead", "new_lead"), "", "Call back later never moves a Hot Lead down")
    eq(d.status_target(t["call_back"], "", "new_lead"), "follow_up", "Call back later sets Follow Up on a prospect")
    eq(d.status_target(t["interested"], "", "new_lead"), "new_lead", "Interested makes a lead")
    eq(d.status_target(t["do_not_contact"], "Hot Lead", "new_lead"), "", "a stop writes no status")
    eq(d.suggest("no-answer"), "no_answer", "a ring-out suggests No answer")
    eq(d.suggest("failed", "404"), "bad_number", "a 404 suggests Dead number")
    eq(d.suggest("completed", answered=True, vm_dropped=True), "voicemail", "a drop suggests Left voicemail")
    ok(all(r.sms in d.SMS_KINDS or not r.sms for r in t.values()), "every texting kind is one the texting app knows")
    try:
        from sms_agent import dispositions as sms_d
        ok(all(r.sms in sms_d.TABLE or r.sms == "listed" or not r.sms for r in t.values()),
           "each row's texting kind is a row of the texting app's table")
    except Exception as exc:  # noqa: BLE001 - the texting app's table needs its own config; report, do not fail
        ok(True, "texting table not importable here (%s)" % str(exc)[:40])


def sec_call_flow(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, calls, config, store, writeback
    fs, fv = fresh(tmp)
    fs.records["rec-1"] = record("rec-1", "own-1", [phone_obj("8655550144", "Dial First"),
                                                    phone_obj("8655550145", "Dial Second", type_="LANDLINE")], attempts=2)
    store.add_caller("casey", "Casey", caller_id="8655550100", user_uuid="user-1")
    store.add_caller("morgan", "Morgan", role="manager", caller_id="8655550101")
    with TestClient(appmod.app, base_url="https://testserver") as tc:
        eq(tc.get("/health").json()["ok"], True, "health answers without the database")
        eq(tc.get("/api/me").status_code, 401, "no cookie, no API")
        r = signin(tc, "casey")
        eq(r.status_code, 200, "a sign-in link signs in")
        eq(signin(tc, "casey").status_code, 200, "a second link works too")
        me = tc.get("/api/me").json()
        eq(me["name"], "Casey", "the page knows who is calling")
        tok = tc.get("/api/phone-token").json()
        ok(tok.get("ok") and tok["token"].count(".") == 2, "a Twilio access token is issued")
        eq(tc.post("/api/call/prepare", json={"number": "8655550144"}).status_code, 403,
           "a write without X-Dialer is refused")
        H = {"X-Dialer": "1"}
        calls_now = AT_9_30_ET

        import dialer.rules as rules_mod
        real_now = rules_mod.datetime

        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return calls_now if tz is None else calls_now.astimezone(tz)
        rules_mod.datetime = FixedNow
        try:
            res = tc.post("/api/call/prepare", json={"number": "8655550144", "record_uuid": "rec-1"}, headers=H).json()
        finally:
            rules_mod.datetime = real_now
        ok(res.get("ok"), "a clean call is prepared: %s" % (res.get("stop") or res.get("ask") or ""))
        cid = res["call"]["id"]
        eq(res["call"]["record"]["owner"], "Pat Sample", "the record rides along with the call")
        eq(res["call"]["recorded"], True, "a Tennessee call is recorded")
        # Twilio asks what the browser leg should do; the signature must match the public URL.
        params = {"CallSid": "CAagent0001", "From": "client:casey", "call_id": cid, "AccountSid": config.S.account_sid}
        eq(signed_post(tc, "/twilio/voice", params, bad=True).status_code, 403, "a bad signature is refused")
        r = signed_post(tc, "/twilio/voice", params)
        eq(r.status_code, 200, "a signed voice webhook is answered")
        ok("<Conference" in r.text and "dlr_%s" % cid in r.text, "the browser leg joins the call's own conference")
        ok('endConferenceOnExit="true"' in r.text, "the caller leaving ends the conference")
        ok("dial_lead" in fv.kinds(), "the homeowner is dialed right after the TwiML is sent")
        d = [o for o in fv.ops if o[0] == "dial_lead"][0]
        eq((d[2], d[3], d[4]), ("+18655550100", "+18655550144", True), "from the caller's number, to the homeowner, recorded")
        call = store.get_call(cid)
        ok(call["lead_sid"].startswith("CA") and call["conference_sid"].startswith("CF"), "both sids are kept")
        lead = call["lead_sid"]
        q = "?call_id=%s&leg=lead" % cid
        signed_post(tc, "/twilio/status" + q, {"CallSid": lead, "CallStatus": "ringing"})
        eq(store.get_call(cid)["status"], "ringing", "ringing")
        signed_post(tc, "/twilio/status" + q, {"CallSid": lead, "CallStatus": "in-progress"})
        eq(store.get_call(cid)["status"], "answered", "answered")
        eq(tc.post("/api/call/%s/dispose" % cid, json={"key": "not_interested"}, headers=H).json().get("ok"), False,
           "no outcome while the call is still up")
        signed_post(tc, "/twilio/status" + q, {"CallSid": lead, "CallStatus": "ringing"})
        eq(store.get_call(cid)["status"], "answered", "a late ringing callback never rewinds the call")
        signed_post(tc, "/twilio/recording?call_id=%s" % cid,
                    {"RecordingSid": "RE" + "9" * 32, "RecordingStatus": "completed", "RecordingDuration": "71"})
        signed_post(tc, "/twilio/status" + q, {"CallSid": lead, "CallStatus": "completed", "CallDuration": "75"})
        c = store.get_call(cid)
        eq((c["status"], c["seconds"], c["end_status"]), ("ended", 75, "completed"), "the end is recorded")
        ok(("end_conference", c["conference_sid"]) in fv.ops, "the server ends the conference when the homeowner hangs up")
        r = tc.post("/api/call/%s/dispose" % cid, json={"key": "not_interested", "note": "Plans to keep it for the grandkids."},
                    headers=H).json()
        ok(r.get("ok"), "the caller dispositions the call")
        eq(r["next"], {}, "a manual call has no queue to move")
        eq(tc.post("/api/call/%s/dispose" % cid, json={"key": "interested"}, headers=H).json().get("ok"), False,
           "an outcome is set once")
        jobs = store.jobs_for(cid)
        eq(sorted(j["kind"] for j in jobs if j["kind"] != "auto_dispose"), ["writeback"], "one write-back job queued")
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), cid))
        writeback.drain()
        eq(fs.records["rec-1"]["predictivecall_attempts"], 3, "the attempt counter moved from 2 to 3")
        ups = [w for w in fs.writes if w[0] == "upsert_phone"]
        eq(len(ups), 1, "one phone upsert")
        sent = ups[0][2]
        eq((sent["number"], sent["status"], sent["type"], sent["tags"]), ("8655550144", "CORRECT", "MOBILE", ["Dial First"]),
           "the whole phone object is sent: status Correct, type and tier kept")
        eq(fs.records["rec-1"]["status"], "not_interested", "the record is Not Interested")
        eq(len(fs.messages), 1, "one note")
        note = fs.messages[0][1]
        ok("Not interested" in note and "grandkids" in note and "1m 15s" in note, "the note says what happened: %s" % note)
        ok("/rec/%s/" % cid in note, "the note carries the signed recording link")
        ok("(865) 555-0144" in note, "the note names the number")
        ws = store.writes_for(cid)
        ok(all(w["ok"] and w["mode"] == "live" for w in ws), "every write is logged as done")
        ok(any(w["action"] == "phone" and w["verified"] == 1 for w in ws), "the phone write was read back")
        eq(store.get_call(cid)["writeback_state"], "done", "the call's write-back is done")
        # A retried job never repeats what already landed.
        jid = [j for j in store.jobs_for(cid) if j["kind"] == "writeback"][0]["id"]
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET state='queued', not_before=? WHERE id=?", (store.now(), jid))
        writeback.drain()
        eq(len(fs.messages), 1, "a re-run posts no second note")
        eq(fs.records["rec-1"]["predictivecall_attempts"], 3, "a re-run does not raise the counter again")
        # The recording link plays for anyone holding it, and only for that call.
        from dialer import links
        good = links.recording_link(cid).split(config.S.public_url, 1)[1]
        eq(tc.get(good).status_code, 200, "the signed recording link plays")
        eq(tc.get("/rec/%s/%s" % (cid, "0" * 32)).status_code, 404, "a forged link does not")
        # The second call to the same record the same day does not move the counter.
        rules_mod.datetime = FixedNow
        try:
            res2 = tc.post("/api/call/prepare", json={"number": "8655550145", "record_uuid": "rec-1"}, headers=H).json()
            ok(res2.get("need_confirm") and any("not_interested" in a for a in res2.get("ask", [])),
               "the record is Not Interested now, so a second number asks first: %s" % res2.get("ask"))
            res2 = tc.post("/api/call/prepare", json={"number": "8655550145", "record_uuid": "rec-1", "confirm": True},
                           headers=H).json()
        finally:
            rules_mod.datetime = real_now
        ok(res2.get("ok"), "confirmed, the second number may be dialed")
        cid2 = res2["call"]["id"]
        signed_post(tc, "/twilio/voice", {"CallSid": "CAagent0002", "From": "client:casey", "call_id": cid2})
        lead2 = store.get_call(cid2)["lead_sid"]
        signed_post(tc, "/twilio/status?call_id=%s&leg=lead" % cid2, {"CallSid": lead2, "CallStatus": "no-answer"})
        c2 = store.get_call(cid2)
        eq((c2["status"], c2["seconds"]), ("ended", 0), "a ring-out ends with no talk time")
        eq(calls._public(c2)["suggest"], "no_answer", "the page is offered No answer")
        ok(tc.post("/api/call/%s/dispose" % cid2, json={"key": "interested"}, headers=H).json().get("ok") is False,
           "Interested is refused on a call nobody answered")
        ok(tc.post("/api/call/%s/dispose" % cid2, json={"key": "no_answer"}, headers=H).json().get("ok"), "No answer")
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), cid2))
        writeback.drain()
        eq(fs.records["rec-1"]["predictivecall_attempts"], 3, "a second dial the same day is the same attempt")
        eq(len(fs.messages), 1, "a no-answer writes no note")
        # Someone else's call cannot be touched; a manager's can be listened to.
        signin(tc, "morgan")
        ok(tc.get("/api/active").json().get("ok"), "a manager sees the live calls")
        signin(tc, "casey")
        eq(tc.get("/api/active").status_code, 403, "a caller does not")
        # A prepared call that waited too long is refused when Twilio asks about it.
        rules_mod.datetime = FixedNow
        try:
            res3 = tc.post("/api/call/prepare", json={"number": "8655550144", "record_uuid": "rec-1", "confirm": True},
                           headers=H).json()
        finally:
            rules_mod.datetime = real_now
        ok(res3.get("ok"), "a confirmed re-dial (already dialed today) is prepared")
        cid3 = res3["call"]["id"]
        with store.tx() as cx:
            cx.execute("UPDATE calls SET created_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), cid3))
        r = signed_post(tc, "/twilio/voice", {"CallSid": "CAagent0003", "From": "client:casey", "call_id": cid3})
        ok("expired" in r.text and "<Conference" not in r.text, "an expired call is not connected")
        r = signed_post(tc, "/twilio/voice", {"CallSid": "CAagent0004", "From": "client:morgan", "call_id": cid3})
        ok("<Conference" not in r.text, "another caller's browser cannot take this call")


def sec_controls(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, config, store
    fs, fv = fresh(tmp)
    fs.records["rec-2"] = record("rec-2", "own-2", [phone_obj("8655550160", "Dial First")])
    fs.records["rec-3"] = record("rec-3", "own-3", [phone_obj("2065550161", "Dial First")], mail_state="WA")
    store.add_caller("casey", "Casey", caller_id="8655550100")
    store.add_caller("morgan", "Morgan", role="manager", caller_id="8655550101")
    H = {"X-Dialer": "1"}
    with TestClient(appmod.app, base_url="https://testserver") as tc:
        signin(tc, "casey")

        def live_call(number, rec):
            cid = store.new_call(identity="casey", kind="manual", number=number, from_number="8655550100",
                                 record_uuid=rec, owner_uuid="own-x", status="prepared", record_allowed=1)
            signed_post(tc, "/twilio/voice", {"CallSid": "CAag" + cid[:6], "From": "client:casey", "call_id": cid})
            lead = store.get_call(cid)["lead_sid"]
            signed_post(tc, "/twilio/status?call_id=%s&leg=lead" % cid, {"CallSid": lead, "CallStatus": "in-progress"})
            return cid, store.get_call(cid)

        cid, c = live_call("8655550160", "rec-2")
        r = tc.post("/api/call/%s/vmdrop" % cid, headers=H).json()
        ok(not r["ok"] and "off" in r["error"], "voicemail drop is off by default")
        config.reload(dict(config.S.__dict__, vm_drop="on"))
        r = tc.post("/api/call/%s/vmdrop" % cid, headers=H).json()
        ok(not r["ok"] and "record your voicemail" in r["error"], "a drop needs the caller's recording first")
        store.update_caller("casey", vm_file="vm_a.mp3", vm_token="tok123")
        (tmp / "media").mkdir(exist_ok=True)
        (tmp / "media" / "vm_a.mp3").write_bytes(b"ID3fake")
        r = tc.post("/api/call/%s/vmdrop" % cid, headers=H).json()
        ok(r["ok"], "the drop goes once the line picked up")
        red = [o for o in fv.ops if o[0] == "redirect"]
        ok(red and red[-1][1] == c["lead_sid"] and "/media/vm/tok123.mp3" in red[-1][2] and "<Hangup" in red[-1][2],
           "the homeowner's leg plays the recording and hangs up")
        ok(("end_conference", c["conference_sid"]) in fv.ops, "the caller is freed at once")
        eq(tc.get("/media/vm/tok123.mp3").status_code, 200, "Twilio can fetch the recording by its token")
        eq(tc.get("/media/vm/nope.mp3").status_code, 404, "a guessed token gets nothing")
        cid_wa, c_wa = live_call("2065550161", "rec-3")
        r = tc.post("/api/call/%s/vmdrop" % cid_wa, headers=H).json()
        ok(not r["ok"] and "not allowed" in r["error"], "no recorded voicemail to Washington even when drop is on")
        config.reload(dict(config.S.__dict__, vm_drop="off"))
        cid, c = live_call("8655550160", "rec-2")
        ok(tc.post("/api/call/%s/hold" % cid, json={"on": True}, headers=H).json()["ok"], "hold")
        ok(("hold", c["conference_sid"], c["lead_sid"], True) in fv.ops, "hold is a participant update")
        r = tc.post("/api/call/%s/transfer" % cid, json={"identity": "morgan"}, headers=H).json()
        ok(r["ok"], "a warm transfer to a teammate")
        ok(any(o[0] == "update_participant" and o[3] == {"end_conference_on_exit": False} for o in fv.ops),
           "the caller may leave without ending the call")
        ok(any(o[0] == "add_participant" and o[3] == "client:morgan" for o in fv.ops), "the closer's phone page rings")
        r = tc.post("/api/call/%s/transfer" % cid, json={"number": "8655550199"}, headers=H).json()
        ok(r["ok"] and any(o[0] == "add_participant" and o[3] == "+18655550199" for o in fv.ops), "or a phone number")
        # A manager listens in through their own browser leg.
        signin(tc, "morgan")
        ok(tc.post("/api/call/%s/monitor" % cid, headers=H).json()["ok"], "a manager may listen to a live call")
        r = signed_post(tc, "/twilio/voice", {"CallSid": "CAmgr0001", "From": "client:morgan", "monitor": cid, "mode": "whisper"})
        ok('coach="%s"' % c["agent_sid"] in r.text, "whisper coaches the caller's own leg")
        r = signed_post(tc, "/twilio/voice", {"CallSid": "CAmgr0002", "From": "client:morgan", "monitor": cid, "mode": "listen"})
        ok('muted="true"' in r.text, "listen joins muted")
        signin(tc, "casey")
        r = signed_post(tc, "/twilio/voice", {"CallSid": "CAmgr0003", "From": "client:casey", "monitor": cid, "mode": "listen"})
        ok("<Conference" not in r.text, "a caller who is not a manager cannot listen in")
        ok(tc.post("/api/call/%s/hangup" % cid, headers=H).json()["ok"], "hang up")
        ok(("end", c["lead_sid"]) in fv.ops, "hang up ends the homeowner's leg")
        # A dial Twilio refuses frees the caller and says why.
        fv.fail_dial = "21211 invalid To"
        cid = store.new_call(identity="casey", kind="manual", number="8655550160", from_number="8655550100",
                             record_uuid="rec-2", status="prepared")
        signed_post(tc, "/twilio/voice", {"CallSid": "CAag777", "From": "client:casey", "call_id": cid})
        c = store.get_call(cid)
        ok(c["status"] == "failed" and ("end", "CAag777") in fv.ops, "a refused dial ends the caller's leg and is marked failed")
        fv.fail_dial = None


def sec_power_dialer(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, config, store
    import dialer.rules as rules_mod
    fs, fv = fresh(tmp)
    fs.presets["FTM - 02 Ready to Call"] = ("02. FORECLOSURE - CALL", {"_preset": "p1", "tags": ["FTM"]}, "p1")
    fs.presets["Hot Leads - follow up"] = ("15. CALLER QUEUES", {"_preset": "p2", "any_property_status": ["Hot Lead"]}, "p2")
    fs.presets["FTM - Weekly 01"] = ("03. FORECLOSURE - MAIL (WEEKLY)", {"_preset": "p3"}, "p3")
    fs.records["q1"] = record("q1", "qo1", [phone_obj("8655550171", "Dial First")], tags=("Do Not Market",))
    fs.records["q2"] = record("q2", "qo2", [phone_obj("8655550172", "Dial Third")])
    fs.records["q3"] = record("q3", "qo3", [phone_obj("8655550173", "Dial Second", type_="LANDLINE"),
                                            phone_obj("8655550174", "", status="CORRECT"),
                                            phone_obj("8655550175", "Dial First")])
    fs.records["q4"] = record("q4", "qo4", [phone_obj("8655550176", "Dial First")])
    fs.rows_for["p1"] = [{"uuid": u, "status": ""} for u in ("q1", "q2", "q3", "q4")]
    fs.records["h1"] = record("h1", "ho1", [phone_obj("8655550177", "Dial First")], status="Hot Lead")
    fs.rows_for["p2"] = [{"uuid": "h1", "status": "Hot Lead"}]
    store.add_caller("casey", "Casey", caller_id="8655550100")
    store.add_caller("riley", "Riley", caller_id="8655550102")
    H = {"X-Dialer": "1"}

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return AT_9_30_ET if tz is None else AT_9_30_ET.astimezone(tz)
    real = rules_mod.datetime
    rules_mod.datetime = FixedNow
    try:
        with TestClient(appmod.app, base_url="https://testserver") as tc:
            signin(tc, "casey")
            titles = [p["title"] for p in tc.get("/api/presets").json()["presets"]]
            ok("FTM - 02 Ready to Call" in titles and "Hot Leads - follow up" in titles, "call and caller-queue presets are offered")
            ok("FTM - Weekly 01" not in titles, "a mail preset is not")
            s = tc.post("/api/session/start", json={"preset": "FTM - 02 Ready to Call"}, headers=H).json()
            ok(s["ok"], "a session starts on a call preset")
            sid = s["session"]["id"]
            n = tc.post("/api/session/%d/next" % sid, headers=H).json()
            eq(n["record"]["uuid"], "q3", "Do Not Market and a record with no callable number are passed over")
            eq([p["number"] for p in n["item"]["numbers"]], ["8655550174", "8655550175", "8655550173"],
               "the confirmed number first, then Dial First, then Dial Second")
            reasons = store.skip_reasons(sid)
            ok(any("Do Not Market" in k for k in reasons) and any("no usable number" in k or "no callable" in k for k in reasons),
               "the skips are kept with their reasons: %s" % reasons)
            item = n["item"]
            # Another caller on the same preset never gets the record Casey holds.
            signin(tc, "riley")
            s2 = tc.post("/api/session/start", json={"preset": "FTM - 02 Ready to Call"}, headers=H).json()
            n2 = tc.post("/api/session/%d/next" % s2["session"]["id"], headers=H).json()
            eq(n2["record"]["uuid"], "q4", "the second caller gets the next record, not the claimed one")
            signin(tc, "casey")
            res = tc.post("/api/call/prepare", json={"number": item["number"], "record_uuid": "q3", "session_id": sid,
                                                     "item_id": item["id"]}, headers=H).json()
            ok(res["ok"], "the queue's call is prepared without a question")
            cid = res["call"]["id"]
            signed_post(tc, "/twilio/voice", {"CallSid": "CAq1", "From": "client:casey", "call_id": cid})
            lead = store.get_call(cid)["lead_sid"]
            signed_post(tc, "/twilio/status?call_id=%s&leg=lead" % cid, {"CallSid": lead, "CallStatus": "no-answer"})
            r = tc.post("/api/call/%s/dispose" % cid, json={"key": "no_answer"}, headers=H).json()
            eq(r["next"], {"record_done": False, "number": "8655550175"}, "no answer moves to the record's next number")
            n = tc.post("/api/session/%d/next" % sid, headers=H).json()
            eq((n["record"]["uuid"], n["item"]["number"]), ("q3", "8655550175"), "the same record, the next number")
            res = tc.post("/api/call/prepare", json={"number": "8655550175", "record_uuid": "q3", "session_id": sid,
                                                     "item_id": item["id"]}, headers=H).json()
            cid = res["call"]["id"]
            signed_post(tc, "/twilio/voice", {"CallSid": "CAq2", "From": "client:casey", "call_id": cid})
            lead = store.get_call(cid)["lead_sid"]
            signed_post(tc, "/twilio/status?call_id=%s&leg=lead" % cid, {"CallSid": lead, "CallStatus": "in-progress"})
            signed_post(tc, "/twilio/status?call_id=%s&leg=lead" % cid, {"CallSid": lead, "CallStatus": "completed", "CallDuration": "40"})
            signed_post(tc, "/twilio/recording?call_id=%s" % cid, {"RecordingSid": "RE" + "5" * 32, "RecordingStatus": "completed",
                                                                 "RecordingDuration": "38"})
            later = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
            r = tc.post("/api/call/%s/dispose" % cid, json={"key": "call_back", "callback_at": later}, headers=H).json()
            eq(r["next"], {"record_done": True}, "a reached owner finishes the record")
            from dialer import writeback
            with store.tx() as cx:
                cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), cid))
            writeback.drain()
            eq(len(fs.tasks), 1, "Call back later makes one task")
            eq((fs.tasks[0]["assigned_to_property"], fs.tasks[0]["due_date"]), ("q3", later), "on the record, at the time given")
            eq(fs.records["q3"]["status"], "follow_up", "and Follow Up on the record")
            n = tc.post("/api/session/%d/next" % sid, headers=H).json()
            ok(n.get("done") or n["record"]["uuid"] != "q3", "the finished record does not come back")
            # A lead queue works leads; the person rules still hold.
            s3 = tc.post("/api/session/start", json={"preset": "Hot Leads - follow up"}, headers=H).json()
            n3 = tc.post("/api/session/%d/next" % s3["session"]["id"], headers=H).json()
            eq(n3["record"]["uuid"], "h1", "a preset that asks for Hot Leads calls Hot Leads")
            eq(tc.post("/api/session/start", json={"preset": "FTM - Weekly 01"}, headers=H).json()["ok"], False,
               "a preset the dialer does not offer cannot be started")
    finally:
        rules_mod.datetime = real


def sec_inbound(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, store
    fs, fv = fresh(tmp)
    fs.records["rec-9"] = record("rec-9", "own-9", [phone_obj("8655550180", "Dial First")])
    store.add_caller("casey", "Casey", caller_id="8655550100")
    store.new_call(identity="casey", kind="manual", number="8655550180", from_number="8655550100", record_uuid="rec-9",
                   status="ended", lead_sid="CAold")
    with TestClient(appmod.app, base_url="https://testserver") as tc:
        r = signed_post(tc, "/twilio/inbound", {"CallSid": "CAin1", "From": "+18655550180", "To": "+18655550100"})
        ok("<Record" in r.text and "<Client" not in r.text, "with the caller's page closed, a call-back goes to voicemail")
        store.update_caller("casey", seen_at=store.now())
        r = signed_post(tc, "/twilio/inbound", {"CallSid": "CAin2", "From": "+18655550180", "To": "+18655550100"})
        ok("<Identity>casey</Identity>" in r.text, "with the page open, the caller who dialed them rings")
        cid = [c for c in store.recent_calls("casey", 10) if c["lead_sid"] == "CAin2"][0]["id"]
        eq(store.get_call(cid)["record_uuid"], "rec-9", "the call-back knows its record")
        r = signed_post(tc, "/twilio/inbound_done?call_id=%s" % cid, {"DialCallStatus": "no-answer"})
        ok("<Record" in r.text, "not picked up: voicemail")
        signed_post(tc, "/twilio/voicemail?call_id=%s" % cid, {"RecordingSid": "RE" + "7" * 32, "RecordingDuration": "22"})
        c = store.get_call(cid)
        eq((c["status"], c["voicemail_sid"][:2]), ("ended", "RE"), "the voicemail is kept")
        from dialer import rules
        ok(rules.recently_called_us("8655550180"), "and the number counts as a callback for two hours")


def sec_handraise(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, events, store
    fresh(tmp)
    store.add_caller("casey", "Casey", caller_id="8655550100")
    with TestClient(appmod.app, base_url="https://testserver") as tc:
        eq(tc.post("/hooks/handraise", json={"phone": "8655550190"}).status_code, 404, "no token, no route")
        before = events.HUB.latest()
        r = tc.post("/hooks/handraise", json={"phone": "8655550190", "record_uuid": "rec-h", "body": "yes what is your offer",
                                              "id": "ev-1"}, headers={"Authorization": "Bearer hr-token"})
        ok(r.json()["ok"] and r.json()["id"], "the texting app's hand raise lands")
        ok(any(e[1] == "handraise" for e in events.HUB.since("casey", before)), "every caller's page hears it")
        r = tc.post("/hooks/handraise", json={"phone": "8655550190", "id": "ev-1"}, headers={"Authorization": "Bearer hr-token"})
        ok(r.json()["duplicate"], "the same hand raise twice is one")
        signin(tc, "casey")
        eq(len(tc.get("/api/handraises").json()["handraises"]), 1, "it waits at the top of the phone page")
        store.close_handraises("8655550190", "called", "casey")
        eq(len(tc.get("/api/handraises").json()["handraises"]), 0, "and leaves once someone calls")


def sec_jobs(tmp):
    """Dry mode, a failing step that retries, the texting app and the KPI receiver."""
    import requests
    from dialer import config, store, writeback
    fs, fv = fresh(tmp, writeback="dry", texting_url="https://texting.example.test", texting_token="tx",
                   kpi_url="https://brain.example.test/hooks/kpi/s/siftdialer")
    fs.records["rec-j"] = record("rec-j", "own-j", [phone_obj("8655550200", "Dial First")])
    store.add_caller("casey", "Casey", caller_id="8655550100")
    cid = store.new_call(identity="casey", kind="manual", number="8655550200", from_number="8655550100", record_uuid="rec-j",
                         owner_uuid="own-j", status="ended", lead_sid="CAj", answered_at=store.now(), ended_at=store.now(),
                         seconds=90, disposition="do_not_contact", started_at=store.now())
    posted = []

    class R:
        def __init__(self, code=200, body=None):
            self.status_code, self._b = code, body or {"ok": True}

        def json(self):
            return self._b
    real_post = requests.post
    requests.post = lambda url, json=None, headers=None, timeout=None: (posted.append((url, json, headers)) or R())
    try:
        writeback.plan(store.get_call(cid))
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), cid))
        writeback.drain()
        eq(fs.writes, [], "dry mode sends nothing to DataSift")
        acts = sorted(w["action"] for w in store.writes_for(cid))
        eq(acts, ["call_attempts", "list:DNC", "note", "phone", "tag:Do Not Market"], "dry mode logs every step it would take")
        tx = [p for p in posted if "texting" in p[0]]
        ok(tx and tx[0][1]["sms_kind"] == "stop_all" and tx[0][2]["authorization"] == "Bearer tx",
           "the texting app is told it was a stop, with the bearer token")
        kp = [p for p in posted if "/hooks/kpi/" in p[0]]
        ok(kp and kp[0][1]["kind"] == "call" and kp[0][1]["tool"] == "siftstack-dialer" and kp[0][1]["seconds"] == 90,
           "the KPI receiver gets the call in the flat shape")
        # A failing step retries without repeating the steps that landed.
        config.reload(dict(config.S.__dict__, writeback="live", texting_url="", kpi_url=""))
        cid2 = store.new_call(identity="casey", kind="manual", number="8655550200", from_number="8655550100",
                              record_uuid="rec-j", owner_uuid="own-j", status="ended", lead_sid="CAj2",
                              answered_at=store.now(), ended_at=store.now(), seconds=30, disposition="interested",
                              started_at=store.now())
        fs.fail_next["/message/"] = 500
        writeback.plan(store.get_call(cid2))
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE call_id=?", (store.now(), cid2))
        writeback.drain()
        j = [x for x in store.jobs_for(cid2) if x["kind"] == "writeback"][0]
        eq(j["state"], "queued", "a failed step leaves the job queued for a retry")
        ups = fs.count("upsert_phone")
        with store.tx() as cx:
            cx.execute("UPDATE jobs SET not_before=? WHERE id=?", (store.now(), j["id"]))
        writeback.drain()
        eq(fs.count("upsert_phone"), ups, "the retry does not write the phone again")
        eq(len(fs.messages), 1, "and posts the note once")
        eq(fs.records["rec-j"]["status"], "new_lead", "Interested made the record a lead")
        # The texting app refusing (an older build) retries; it never silently drops.
        config.reload(dict(config.S.__dict__, texting_url="https://texting.example.test", texting_token="tx"))
        requests.post = lambda url, json=None, headers=None, timeout=None: R(404)
        from dialer import texting
        try:
            texting.call_outcome(store.get_call(cid2))
            ok(False, "a 404 from the texting app must raise so the job retries")
        except RuntimeError:
            ok(True, "a 404 from the texting app raises so the job retries")
    finally:
        requests.post = real_post


def sec_auto_dispose(tmp):
    from dialer import store, writeback
    fs, fv = fresh(tmp, writeback="dry")
    store.add_caller("casey", "Casey", caller_id="8655550100")
    a = store.new_call(identity="casey", kind="manual", number="8655550210", status="ended", end_status="no-answer",
                       lead_sid="CAa")
    b = store.new_call(identity="casey", kind="manual", number="8655550211", status="ended", end_status="completed",
                       lead_sid="CAb", answered_at=store.now(), seconds=200)
    eq(writeback.run_auto_dispose(store.get_call(a)).get("auto"), "no_answer", "a ring-out nobody marked becomes No answer")
    eq(store.get_call(a)["disposition"], "no_answer", "and is saved")
    ok(writeback.run_auto_dispose(store.get_call(b)).get("left_for_a_person"), "an answered call is left for a person")


def sec_security(tmp):
    from fastapi.testclient import TestClient
    from dialer import app as appmod, config, links
    fresh(tmp)
    with TestClient(appmod.app, base_url="https://testserver") as tc:
        config.reload(dict(config.S.__dict__, public_url=""))
        eq(tc.post("/twilio/status?call_id=x", data={"CallStatus": "ringing"}).status_code, 403,
           "with no public URL no Twilio webhook can be verified, so none is taken")
        config.reload(dict(config.S.__dict__, public_url="https://dialer.example.test"))
        eq(tc.post("/twilio/status?call_id=x", data={"CallStatus": "ringing"}).status_code, 403, "an unsigned webhook is refused")
        r = tc.get("/phone")
        ok("Content-Security-Policy" in r.headers and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"],
           "the pages carry a strict CSP")
        eq(tc.post("/api/signin", json={"code": "nope"}, headers={"X-Dialer": "1"}).status_code, 400, "a bad code signs in nobody")
        ok(links.ok("rec", "abc", links.sig("rec", "abc")) and not links.ok("rec", "abd", links.sig("rec", "abc")),
           "a signed link is bound to its call")


def sec_events():
    from dialer import events
    h = events.Hub()
    a = h.publish("casey", "call", {"x": 1})
    h.publish("riley", "call", {"x": 2})
    eq([e[2]["x"] for e in h.since("casey", 0)], [1], "a caller hears only their own events")
    eq(h.since("casey", a), [], "nothing newer than the last seen")


def sec_config():
    from dialer import config
    s = config.reload({"public_url": "", "account_sid": "", "sift_key": "", "calls": "test", "test_numbers": ()})
    p = " ".join(config.problems(s))
    ok("DIALER_PUBLIC_URL" in p and "Twilio is not set up" in p and "REISIFT_API_KEY" in p and "DIALER_TEST_NUMBERS" in p,
       "the doctor names every missing piece")
    eq(config._hours("09:00-19:00", (0, 1)), (540, 1140), "hours parse")
    eq(config._hours("19:00-09:00", (0, 1)), (0, 1), "a backwards window is refused")
    eq(config._numbers("+1 865 555 0100, 8655550101;bad"), ("8655550100", "8655550101"), "test numbers parse")
    s = config.reload({"amd": "auto_drop", "vm_drop": "off"})
    ok(any("auto_drop" in x for x in config.problems(s)), "auto voicemail needs voicemail drop switched on")


def sec_static():
    """The files the page and the image need are present and carry no dash characters."""
    root = Path(__file__).resolve().parent
    for f in ("static/phone.html", "static/phone.js", "static/phone.css", "static/signin.html"):
        ok((root / f).exists(), "%s exists" % f)
    bad = []
    for p in list(root.rglob("*.py")) + list(root.rglob("*.html")) + list(root.rglob("*.js")) + list(root.rglob("*.css")):
        if "vendor" in p.parts:
            continue
        t = p.read_text(encoding="utf-8")
        if chr(0x2014) in t or chr(0x2013) in t:
            bad.append(p.name)
        if "\x08" in t or "\x00" in t:
            bad.append(p.name + " (a control byte)")
    eq(bad, [], "no em or en dash, and no stray control byte, in the dialer's own files")


SECTIONS = [("phone numbers", lambda t: sec_phone()), ("rules", sec_rules), ("dispositions", lambda t: sec_dispositions()),
            ("a call end to end", sec_call_flow), ("in-call controls", sec_controls), ("the power dialer", sec_power_dialer),
            ("inbound and voicemail", sec_inbound), ("hand raises", sec_handraise), ("jobs", sec_jobs),
            ("auto disposition", sec_auto_dispose), ("security", sec_security), ("events", lambda t: sec_events()),
            ("settings", lambda t: sec_config()), ("files", lambda t: sec_static())]


def main(argv: list[str] | None = None) -> int:
    os.environ["DIALER_WORKER"] = "0"
    import logging
    logging.basicConfig(level=logging.WARNING)
    for name in ("httpx", "httpx2", "dialer"):
        logging.getLogger(name).setLevel(logging.ERROR)
    install_guards()
    only = set(argv or sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="dialer_selftest_", ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        for name, fn in SECTIONS:
            if only and not any(o in name for o in only):
                continue
            SECTION[0] = name
            try:
                fn(tmp)
            except Exception:  # noqa: BLE001 - a crash is a failure with its trace, never a silent stop
                RESULTS["fail"] += 1
                RESULTS["failures"].append("%s: crashed\n%s" % (name, traceback.format_exc()))
        from dialer import store
        for c in getattr(store._local, "conns", {}).values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    total = RESULTS["pass"] + RESULTS["fail"]
    for f in RESULTS["failures"]:
        print("FAIL", f)
    print("dialer selftest: %d of %d passed" % (RESULTS["pass"], total))
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
