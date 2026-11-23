"""Cold email in the brain (2026-11-20): the four jobs that ran as Windows scheduled tasks run as stages,
ONE install is their home, the gate has its own email channel, and the cutover moves them in one step.

Zero network: Instantly, ZeroBounce, the CRM, Slack, the classifier and the child processes of a top-up
are fakes, and every file lives in the section's own temporary folder (the real state folder is never
read or written). Every name, address and email here is invented; streets are Example and Sample streets.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
ENV_KEYS = ("COLD_EMAIL_STATE_DIR", "COLD_EMAIL_INSTALL", "AGENTS_DATA_DIR", "FLY_APP_NAME",
            "COLD_EMAIL_HAND_RAISE_NEW_LEAD", "COLD_EMAIL_HANDOFF_NAME")
BOX = "siftstack-agents"
STAGES = {
    "cold_email_replies": {"kind": "coldreplies", "owner": "sales", "mode": "live", "schedule": ["every 5 min"]},
    "cold_email_topup": {"kind": "coldtopup", "owner": "ops", "mode": "live", "schedule": ["20:15 daily"],
                         "campaign": "Agency queue email 1", "preset": "Agency - 01 Queue", "days_queued": 3,
                         "min_batch": 100, "build_timeout_min": 120},
    "cold_email_health": {"kind": "coldhealth", "owner": "ops", "mode": "live", "schedule": ["08:00 Mon-Fri"]},
    "cold_email_stats": {"kind": "coldstats", "owner": "ops", "mode": "live", "schedule": ["09:05 Mon"]},
}


def _pol(mode: str = "live"):
    from .autopilot import policy
    stages = {sid: dict(s, mode=mode) for sid, s in STAGES.items()}
    pb = {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
          "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]}, "stages": stages}
    return policy.parse(pb, EMPLOYEES)


class FakeInstantly:
    def __init__(self, emails=(), status=1, leads=1000, contacted=900, daily=250, sent=500, bounced=3, accounts=None):
        self.emails, self.status, self.leads, self.contacted = list(emails), status, leads, contacted
        self.daily, self.sent, self.bounced, self.calls, self.blocked = daily, sent, bounced, [], []
        self.accs = accounts if accounts is not None else [{"email": "a%d@example.com" % i, "status": 1, "warmup_status": 1}
                                                            for i in range(3)]

    def call(self, method, path, params=None, json=None, **kw):
        self.calls.append((method, path))
        if path == "/emails":
            since = (params or {}).get("min_timestamp_created") or ""
            return {"items": [e for e in self.emails if e["timestamp_created"] >= since]}
        if path == "/campaigns/analytics":
            return [{"campaign_id": "c1", "leads_count": self.leads, "contacted_count": self.contacted,
                     "emails_sent_count": self.sent, "bounced_count": self.bounced}]
        raise AssertionError("the selftest's Instantly does not answer %s %s" % (method, path))

    def campaigns(self):
        self.calls.append(("GET", "/campaigns"))
        return [{"id": "c1", "name": "Agency queue email 1", "status": self.status, "daily_limit": self.daily}]

    def accounts(self):
        self.calls.append(("GET", "/accounts"))
        return self.accs

    def warmup_analytics(self, emails):
        return {"aggregate_data": {e: {"health_score": 97} for e in emails}}

    def block(self, values):
        self.blocked += list(values)
        return {"valid_count": len(values)}


class FakeCRM:
    def __init__(self):
        self.notes, self.lists, self.reads = [], [], 0

    def record(self, uuid):
        self.reads += 1
        return {"uuid": uuid, "address": {"street": "100 Example St", "city": "Knoxville", "state": "TN"},
                "owner": {"first_name": "Pat", "phones": []}}

    def message(self, uuid, text, pin=False):
        self.notes.append(text)
        return "m1"

    def add_lists(self, uuid, lists):
        self.lists += list(lists)


def _email(i, text, ts, sender=None, auto=False):
    return {"id": "e%d" % i, "timestamp_created": ts, "lead": sender or "owner%d@example.com" % i,
            "body": {"text": text}, "is_auto_reply": auto}


class Rig:
    """Points the cold email modules at a temporary state folder and swaps every outside call for a fake."""

    def __init__(self, cs, daily, replies, state: Path):
        self.cs, self.daily, self.replies, self.state = cs, daily, replies, state
        self.saved = []
        self.posts, self.classified = [], []
        self.ok_post = True
        self.api = FakeInstantly()
        self.crm = FakeCRM()
        self.kinds = {}
        self.credits = 50000
        self.child_calls = []
        self.child_fail = ""

    def set(self, obj, name, value):
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def install(self):
        r, d = self.replies, self.daily
        self.set(r, "STATE", self.state / "replies_state.json")
        self.set(r, "LEDGER", self.state / "ledger.json")
        self.set(r, "LOG", self.state / "replies.jsonl")
        for name, value in (("STATE_DIR", self.state), ("REPLIES_LOG", self.state / "replies.jsonl"),
                            ("TASK_LOG", self.state / "replies_task.log"), ("STATE", self.state / "daily_state.json"),
                            ("HOLD", self.state / "hold.json")):
            self.set(d, name, value)
        self.set(r, "Instantly", lambda: self.api)
        self.set(d, "Instantly", lambda: self.api)
        self.set(r, "webhook", lambda: "https://hooks.example.test/text-leads")
        self.set(r, "post", self._post)
        self.set(r, "classify", self._classify)
        real_handle = r.handle

        def handle(e, ledger, api, crm_box, commit):
            crm_box["crm"] = self.crm                        # the fake CRM, never the real client
            return real_handle(e, ledger, api, crm_box, commit)
        self.set(r, "handle", handle)
        import zerobounce
        rig = self

        class FakeZB:
            def credits(self):
                return rig.credits
        self.set(zerobounce, "ZeroBounce", FakeZB)
        self.set(d, "subprocess", types.SimpleNamespace(run=self._child, TimeoutExpired=subprocess.TimeoutExpired))

    def restore(self):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        self.saved = []

    def _post(self, text):
        self.posts.append((self.ok_post, text))
        return self.ok_post

    def _classify(self, text, street):
        self.classified.append(text)
        if self.replies.STOP_RX.search(text or ""):
            return "stop", "asked to stop", 1.0
        return self.kinds.get(text, "not_interested"), "test", 0.9

    def _child(self, args, **kw):
        """build_list.py writes tonight's leads; campaign.py load adds them to the ledger."""
        self.child_calls.append([Path(str(a)).name for a in args[2:4]])
        script = Path(str(args[2])).name
        if self.child_fail == script:
            return types.SimpleNamespace(returncode=2, stdout="", stderr="it broke for owner9@example.com")
        if script == "build_list.py":
            slug = args[args.index("--slug") + 1]
            out = self.cs.state_dir() / "lists" / slug / "leads.json"
            self.cs.write_json(out, [{"owner": "o-new1"}, {"owner": "o-new2"}])
        elif script == "campaign.py":
            led = json.loads((self.cs.state_dir() / "ledger.json").read_text(encoding="utf-8"))
            led["owners"].update({"o-new1": {"record": "r-n1"}, "o-new2": {"record": "r-n2"}})
            self.cs.write_json(self.cs.state_dir() / "ledger.json", led)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def seed(self, emails, ledger_extra=None, state=None):
        ledger = {"owners": {}, "emails": {}}
        for e in emails:
            ledger["emails"][e["lead"]] = "o-" + e["id"]
            ledger["owners"]["o-" + e["id"]] = {"record": "r-" + e["id"]}
        ledger.update(ledger_extra or {})
        self.cs.write_json(self.state / "ledger.json", ledger)
        self.cs.write_json(self.state / "replies_state.json",
                           state or {"seen": [], "since": "2026-03-05T14:00:00.000Z"})
        self.api.emails = list(emails)


def run(check, tmp: Path) -> None:
    keep_env = {k: os.environ.get(k) for k in ENV_KEYS}
    keep_cfg = (config.MODE, config.LIVE_STAGES)
    base = Path(tmp) / "coldemail"
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    os.environ["COLD_EMAIL_STATE_DIR"] = str(state)
    os.environ["COLD_EMAIL_INSTALL"] = BOX
    os.environ["COLD_EMAIL_HAND_RAISE_NEW_LEAD"] = "0"
    os.environ["COLD_EMAIL_HANDOFF_NAME"] = "Emerson"
    from .autopilot import coldemail
    cs, daily, replies = coldemail.mods()
    keep_root = cs.ROOT
    cs.ROOT = base                                   # no .env here: the section reads no real setting
    rig = Rig(cs, daily, replies, state)
    rig.install()
    try:
        _coldstate(check, cs, base)
        _gate(check)
        _replies(check, rig)
        _stages(check, rig)
        _playbook(check)
        _cutover(check, cs, base)
    finally:
        rig.restore()
        cs.ROOT = keep_root
        config.MODE, config.LIVE_STAGES = keep_cfg
        for k, v in keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------- where the state lives, who runs it
def _coldstate(check, cs, base: Path) -> None:
    keep = {k: os.environ.get(k) for k in ("COLD_EMAIL_STATE_DIR", "AGENTS_DATA_DIR", "COLD_EMAIL_INSTALL", "FLY_APP_NAME")}
    try:
        os.environ["COLD_EMAIL_STATE_DIR"] = str(base / "explicit")
        os.environ["AGENTS_DATA_DIR"] = str(base / "data")
        first = cs.state_dir()
        os.environ.pop("COLD_EMAIL_STATE_DIR")
        second = cs.state_dir()
        os.environ.pop("AGENTS_DATA_DIR")
        third = cs.state_dir()
        check(first == base / "explicit" and second == base / "data" / "cold_email"
              and third == base / "output" / "cold_email",
              "the state folder: COLD_EMAIL_STATE_DIR, else the brain's AGENTS_DATA_DIR/cold_email, else the "
              "workstation's output/cold_email (%s, %s, %s)" % (first, second, third))
        os.environ.pop("COLD_EMAIL_INSTALL", None)
        os.environ["FLY_APP_NAME"] = "siftstack-agents"
        on_fly = cs.install_name()
        os.environ.pop("FLY_APP_NAME")
        on_pc = cs.install_name()
        os.environ["COLD_EMAIL_INSTALL"] = "named-here"
        check(on_fly == "siftstack-agents" and on_pc == "workstation" and cs.install_name() == "named-here",
              "this install is COLD_EMAIL_INSTALL, else the Fly app's name, else the workstation")
        home = base / "homes"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["COLD_EMAIL_INSTALL"] = "workstation"
        pc_owns = cs.owns(home)[0]
        os.environ["COLD_EMAIL_INSTALL"] = BOX
        ok, why = cs.owns(home)
        check(pc_owns and not ok and "workstation" in why,
              "with no home.json the workstation runs cold email and the brain does nothing (%s)" % why)
        cs.claim(BOX, home, by="selftest")
        d = cs.read_home(home)
        check(cs.owns(home)[0] and d.get("home") == BOX and d.get("since") and d.get("by") == "selftest",
              "the cutover names the box the home, with when and by whom")
        os.environ["COLD_EMAIL_INSTALL"] = "workstation"
        refused = False
        try:
            cs.require("handle replies", home)
        except SystemExit as exc:
            refused = exc.code == 3
        check(refused, "once the box is the home, a run by hand on the PC stops with exit code 3")
        os.environ["COLD_EMAIL_INSTALL"] = BOX
        (home / "home.json").write_text("{not json", encoding="utf-8")
        check(not cs.owns(home)[0] and cs.home_name(home) == "",
              "a home.json nobody can read names nobody: no install acts")
        a, b = cs.RunLock("replies", home), cs.RunLock("replies", home)
        first_ok, second_ok = a.acquire(), b.acquire()
        other = cs.RunLock("topup", home)
        other_ok = other.acquire()
        a.release()
        again = b.acquire()
        b.release()
        other.release()
        check(first_ok and not second_ok and other_ok and again,
              "one reply pass at a time on a machine; another job's lock is its own; a released lock is free again")
        cs.write_json(home / "x.json", {"a": 1})
        check(json.loads((home / "x.json").read_text(encoding="utf-8")) == {"a": 1}
              and not [p for p in home.iterdir() if ".tmp" in p.name], "a state file is written whole, with no temp left")
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------- the gate's email channel
def _gate(check) -> None:
    from .autopilot import gate
    from sms_agent import gate_rules

    def rec(**kw):
        r = {"uuid": "g1", "status": None, "tags": [], "lists": [], "owner": {"phones": []}}
        r.update(kw)
        return r
    check("email" in gate.CHANNELS and "email" in gate.OUTREACH, "the gate has an email channel, an outreach channel")
    returned = rec(tags=["Return Mail"])
    check(gate.check(returned, "mail").code == "return_mail" and gate.check(returned, "email").ok,
          "mail that came back from the house holds the next letter, never an email")
    check(gate.check(rec(do_not_mail_ever=True), "email").code == "do_not_mail",
          "an owner who asked for no mail gets no marketing email either")
    mail_only = rec(tags=["Mail Only"])
    check(gate.check(mail_only, "email").ok and gate.check(mail_only, "call").code == "mail_only",
          "Mail Only means no calls and no texts; an email is not held by it")
    for label, r, code in (("Do Not Market", rec(tags=["Do Not Market"]), "do_not_market"),
                           ("the DNC list", rec(lists=["DNC"]), "opted_out"),
                           ("a sold house", rec(tags=["recently sold"]), "sold"),
                           ("a possible death", rec(lists=["Obituary"]), "deceased"),
                           ("a lead", rec(status="Hot Lead"), "lead")):
        check(gate.check(r, "email").code == code, "%s holds an email (%s)" % (label, code))
    lane = gate.check(rec(tags=["Top 1000"]), "email", owner={"lane": "ftm", "records": {}})
    check(lane.code == "owner_lane", "an owner in a higher lane on another record is held from email")
    check(gate.check_row({"status": None, "owner_do_not_mail_ever": True}, "email").code == "do_not_mail"
          and gate.check_row({"status": "not_interested"}, "email").code == "finished",
          "a search row says no to an email the same way it says no to mail")
    check(gate_rules.RULES == gate.RULES and gate_rules.CHANNELS == ("text", "call"),
          "the texting app's copy carries the same rule table and still answers only for a text and a call")


# ---------------------------------------------------------------------- the reply watcher's guarantees
def _fresh(rig, emails, **kw):
    for name in ("replies_state.json", "ledger.json", "replies.jsonl", "replies_heartbeat.json"):
        p = rig.state / name
        if p.exists():
            p.unlink()
    rig.posts, rig.classified, rig.ok_post = [], [], True
    rig.api = FakeInstantly()
    rig.crm = FakeCRM()
    rig.seed(emails, **kw)


def _seen(rig):
    return set(json.loads((rig.state / "replies_state.json").read_text(encoding="utf-8")).get("seen") or [])


def _replies(check, rig) -> None:
    r = rig.replies
    hand = _email(1, "yes, make me an offer", "2026-03-05T15:00:00.000Z")
    rig.kinds = {"yes, make me an offer": "interested"}
    _fresh(rig, [hand])
    rig.set(r, "webhook", lambda: "")
    out = r.run_pass(commit=True)
    r.webhook = lambda: "https://hooks.example.test/text-leads"
    check(out["skipped"] and "text leads channel" in out["problems"][0] and not rig.api.calls,
          "with no text leads channel a committed pass reads nothing: a hand raise read then would reach nobody")
    (rig.state / "replies_state.json").unlink()
    out = r.run_pass(commit=True)
    check(out["skipped"] and "replies_state.json" in out["problems"][0] and not rig.api.calls,
          "with no reply state a committed pass reads nothing (the old default re-read two days and posted them again)")
    rig.seed([hand])
    (rig.state / "ledger.json").write_text("{half a ledg", encoding="utf-8")
    out = r.run_pass(commit=True)
    check(out["skipped"] and "ledger.json could not be read" in out["problems"][0] and not rig.api.calls,
          "an unreadable ledger stops the pass: read as empty, a stop would never reach the DNC list")
    rig.seed([hand])
    (rig.state / "replies_state.json").write_text("{nope", encoding="utf-8")
    out = r.run_pass(commit=True)
    check(out["skipped"] and "replies_state.json could not be read" in out["problems"][0],
          "a broken reply state stops the pass instead of starting two days back")

    _fresh(rig, [hand])
    rig.ok_post = False
    out = r.run_pass(commit=True)
    check(out["handled"] == 0 and out["retry"] == 1 and "e1" not in _seen(rig) and not rig.crm.notes,
          "a hand raise the channel refuses is not marked handled, and nothing is written for it")
    rig.ok_post = True
    out = r.run_pass(commit=True)
    oks = [ok for ok, text in rig.posts if "Call within 5 minutes" in text]
    check(out["handled"] == 1 and oks == [False, True] and "e1" in _seen(rig) and len(rig.crm.notes) == 1,
          "the next pass posts it: one hand raise reaches the text leads channel, one note on the record")

    two = [_email(1, "No thanks", "2026-03-05T15:00:00.000Z"), _email(2, "Not interested", "2026-03-05T15:05:00.000Z")]
    _fresh(rig, two)
    real_handle = r.handle

    def dies_on_second(e, ledger, api, crm_box, commit):
        if e["id"] == "e2":
            raise SystemExit("the box was redeployed mid-pass")
        return real_handle(e, ledger, api, crm_box, commit)
    r.handle = dies_on_second
    try:
        r.run_pass(commit=True)
    except SystemExit:
        pass
    r.handle = real_handle
    seen_after_crash = _seen(rig)
    out = r.run_pass(commit=True)
    check(seen_after_crash == {"e1"} and out["handled"] == 1 and rig.api.blocked.count("owner1@example.com") == 1,
          "a pass that dies halfway has saved each reply it finished, so the next pass never handles one twice")

    _fresh(rig, [hand])
    held = rig.cs.RunLock("replies", rig.state)
    held.acquire()
    try:
        out = r.run_pass(commit=True)
    finally:
        held.release()
    check(out["skipped"] == "another reply pass is running" and not rig.api.calls and not rig.posts,
          "while one pass runs, a second one on the same machine reads nothing")

    _fresh(rig, [hand])
    r.run_pass(commit=True)
    hb = rig.cs.heartbeat(rig.state)
    age = rig.daily._watcher_age_min()
    check(hb.get("at") and hb.get("install") == BOX and hb.get("handled") == 1 and age is not None and age < 5,
          "a committed pass leaves its heartbeat, and the morning health check reads it wherever it runs")

    _fresh(rig, [hand, _email(2, "out of office", "2026-03-05T15:06:00.000Z", auto=True)])
    p = r.peek()
    check(p["new"] == 1 and p["auto"] == 1 and not rig.classified and _seen(rig) == set(),
          "a plan counts the new replies without the classifier (no model call) and writes nothing")

    _fresh(rig, [_email(1, "No", "2026-03-05T15:00:00.000Z")])
    real_msg = rig.crm.message

    def no_notes(uuid, text, pin=False):
        raise RuntimeError("HTTP 404 on POST /api/internal/property/x/add-notes/")
    rig.crm.message = no_notes
    out = r.run_pass(commit=True)
    rig.crm.message = real_msg
    check(out["handled"] == 1 and any("could not write the cold email reply note" in x for x in out["problems"]),
          "a note that cannot be written is passed over for the reply, and said out loud as a problem")

    _fresh(rig, [_email(1, "No", "2026-03-05T15:00:00.000Z")])
    rig.api.block = lambda values: (_ for _ in ()).throw(RuntimeError("Instantly answered 502"))
    for _ in range(r.SAY_AFTER):
        out = r.run_pass(commit=True)
    del rig.api.block
    check(out["retry"] == 1 and any("failed %d passes in a row" % r.SAY_AFTER in x for x in out["problems"]),
          "a reply that keeps failing is said out loud well before it goes to a person")


# ---------------------------------------------------------------------- the four stages, through the sweep
def _stages(check, rig) -> None:
    from .autopilot import sweep
    cs = rig.cs
    nocrm = type("NoCRM", (), {"calls": 0})()
    config.MODE, config.LIVE_STAGES = "free", frozenset()
    home = rig.state / "home.json"
    if home.exists():
        home.unlink()
    _fresh(rig, [_email(1, "yes please call", "2026-03-05T15:00:00.000Z")])
    out = sweep.run("cold_email_replies", crm=nocrm, now=NOW, pol=_pol())
    plan = sweep.board_plan({"id": 1, "workflow": "cold_email_replies", "source": "schedule"}, out.as_dict())
    check(out.head.startswith("Cold email runs on workstation") and not out.problems and not rig.api.calls
          and plan["section"] is None and not plan["issues"] and not plan["alerts"],
          "before the cutover the brain's reply stage does nothing, reads nothing and posts nothing: %r" % out.head)
    stats = sweep.run("cold_email_stats", crm=nocrm, now=NOW, pol=_pol())
    check(not stats.head and not stats.lines and sweep.board_plan({"id": 2, "workflow": "cold_email_stats", "source": "schedule"},
                                                                 stats.as_dict())["section"] is None,
          "the weekly report says nothing at all on an install that is not the home")

    cs.claim(BOX, rig.state, by="selftest")
    two = [_email(1, "yes please call", "2026-03-05T15:00:00.000Z"), _email(2, "No thanks", "2026-03-05T15:02:00.000Z")]
    rig.kinds = {"yes please call": "interested"}
    _fresh(rig, two)
    out = sweep.run("cold_email_replies", crm=nocrm, now=NOW, pol=_pol())
    check(out.live and out.acted == 2 and out.head == "2 cold email replies handled: 1 hand raise, 1 said no."
          and out.lines and "text leads channel" in out.lines[0],
          "on the box that is the home, the free reply stage acts on its schedule: %r" % out.head)
    hand = [t for ok, t in rig.posts if ok]
    check(len(hand) == 1 and "Call within 5 minutes" in hand[0] and rig.api.blocked == ["owner2@example.com"],
          "the hand raise goes to the text leads channel, the no to Instantly's block list")
    plan = sweep.board_plan({"id": 3, "workflow": "cold_email_replies", "source": "schedule"}, out.as_dict())
    check(plan["dept"] == "marketing" and plan["section"] and plan["section"].get("entry", "").startswith("2 cold email replies")
          and not plan["issues"], "a pass that handled replies is one line on the marketing board")

    _fresh(rig, [_email(1, "yes please call", "2026-03-05T15:00:00.000Z")])
    out = sweep.run("cold_email_replies", crm=nocrm, now=NOW, pol=_pol(), plan=True)
    check(not out.live and out.head == "Would read 1 new cold email reply." and not rig.classified and _seen(rig) == set(),
          "a plan run reads the count only: no classifier, nothing written")

    rig.api = FakeInstantly(leads=100, contacted=100, daily=250)
    rig.child_calls = []
    out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    check(not out.live and out.head.startswith("Would build about 750 leads from Agency - 01 Queue") and not rig.child_calls,
          "the top-up is outreach: on a free box not listed in AGENTS_LIVE_STAGES it plans and builds nothing: %r" % out.head)
    config.LIVE_STAGES = frozenset({"cold_email_topup"})
    out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    check(out.live and out.acted == 2 and out.head.startswith("2 new leads loaded into the cold email campaign; 0 were queued") and
          [c[0] for c in rig.child_calls] == ["build_list.py", "campaign.py"],
          "listed in AGENTS_LIVE_STAGES, the top-up builds tonight's list and loads it: %r" % out.head)
    rig.child_calls, rig.child_fail = [], "build_list.py"
    out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    rig.child_fail = ""
    plan = sweep.board_plan({"id": 4, "workflow": "cold_email_topup", "source": "schedule"}, out.as_dict())
    check(out.problems and "build failed" in out.problems[0] and "owner9@example.com" not in out.problems[0]
          and plan["issues"], "a failed build is said in #issues, with no homeowner's address in it")
    rig.child_calls, rig.credits = [], 10
    out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    rig.credits = 50000
    check(out.problems and "ZeroBounce is down to 10 credits" in out.problems[0] and not rig.child_calls,
          "short of ZeroBounce credits, nothing is built and the reason is said")
    rig.api = FakeInstantly(leads=2000, contacted=500, daily=250)
    out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    check(out.head == "Enough cold email leads queued: 1,500, or 6.0 sending days at 250 a day." and not out.problems
          and not sweep.posts({"id": 5, "workflow": "cold_email_topup", "source": "schedule"}, out.as_dict()),
          "with enough queued the top-up posts nothing: %r" % out.head)
    held = cs.RunLock("topup", cs.state_dir())
    held.acquire()
    try:
        rig.api = FakeInstantly(leads=100, contacted=100)
        out = sweep.run("cold_email_topup", crm=nocrm, now=NOW, pol=_pol())
    finally:
        held.release()
    check(out.tally.get("skipped: another top-up is running") == 1 and not rig.api.calls,
          "a second top-up on the same machine skips")
    config.LIVE_STAGES = frozenset()

    cs.beat(rig.state, new=0, handled=0)              # the reply watcher just ran, as it does every five minutes
    rig.api = FakeInstantly(accounts=[{"email": "a1@example.com", "status": 1, "warmup_status": 1},
                                      {"email": "a2@example.com", "status": 3, "warmup_status": 1}])
    out = sweep.run("cold_email_health", crm=nocrm, now=NOW, pol=_pol())
    plan = sweep.board_plan({"id": 6, "workflow": "cold_email_health", "source": "schedule"}, out.as_dict())
    check(out.problems and "1 of 2 inboxes are not connected" in out.problems[0] and plan["issues"],
          "an inbox that dropped is said in #issues, as the PC's morning check said it")
    rig.api = FakeInstantly()
    out = sweep.run("cold_email_health", crm=nocrm, now=NOW, pol=_pol())
    check(not out.problems and out.head == "Cold email is healthy." and
          not sweep.posts({"id": 7, "workflow": "cold_email_health", "source": "schedule"}, out.as_dict()),
          "a healthy morning posts nothing")
    rig.api = FakeInstantly(sent=400, bounced=4)
    (rig.state / "replies.jsonl").write_text(
        json.dumps({"kind": "interested", "at": datetime.now(timezone.utc).isoformat()}) + "\n"
        + json.dumps({"kind": "stop", "at": datetime.now(timezone.utc).isoformat()}) + "\n", encoding="utf-8")
    out = sweep.run("cold_email_stats", crm=nocrm, now=NOW, pol=_pol())
    posts = sweep.posts({"id": 8, "workflow": "cold_email_stats", "source": "schedule"}, out.as_dict())
    check(out.head.startswith("Cold email last week: 400 sent, 2 replies (0.5%), 1 interested, 4 bounced.")
          and out.lines and "1 asked to stop" in out.lines[0] and [p["kind"] for p in posts] == ["pipeline"],
          "the Monday numbers are a team report, the head and one line: %r" % out.head)
    check(not any(ch in "\n".join([out.head] + out.lines) for ch in (chr(0x2014), chr(0x2013))),
          "no long dash in what the stages say")


# ---------------------------------------------------------------------- the playbook and the maps
def _playbook(check) -> None:
    from . import channels, voice
    from .autopilot import coldemail, policy, registry
    pol = policy.load()
    want = {"cold_email_replies": ("coldreplies", "free", ["every 5 min"]),
            "cold_email_topup": ("coldtopup", "outreach", ["20:15 daily"]),
            "cold_email_health": ("coldhealth", "free", ["08:00 Mon-Fri"]),
            "cold_email_stats": ("coldstats", "free", ["09:05 Mon"])}
    got = {sid: (pol.stage(sid).kind, pol.stage(sid).spend, pol.stage(sid).schedule) for sid in want if sid in pol.stages}
    check(got == want, "the playbook carries the four cold email stages on the PC's schedules (%r)" % got)
    check(all(pol.stage(s).mode == "live" for s in want if s in pol.stages),
          "all four are live in the playbook: home.json, not the mode, holds them until the cutover")
    check(all(registry.EXTRA[k][0] == "coldemail" for k in ("coldreplies", "coldtopup", "coldhealth", "coldstats"))
          and registry.EXTRA["coldstats"][4] and not registry.EXTRA["coldreplies"][4],
          "the four kinds are registered; only the Monday numbers are a report")
    check(all(channels.department(k) == "marketing" for k in ("coldreplies", "coldtopup", "coldhealth", "coldstats")),
          "cold email reports to the marketing board")
    check(voice.stage_name("cold_email_topup") == "Cold email top-up" and voice.stage_name("cold_email_replies") == "Cold email replies",
          "the stages have plain names")
    from .autopilot.policy import Stage
    bad = Stage(id="x", kind="coldtopup", owner="ops", mode="live", summary="", schedule=[], presets=[],
                params={"days_queued": 40, "preset": ""})
    probs = coldemail.block_problems(bad)
    check(any("days_queued" in p for p in probs) and any("preset" in p for p in probs),
          "a mistyped top-up block is refused when the playbook loads")


# ---------------------------------------------------------------------- the cutover helper
def _cutover(check, cs, base: Path) -> None:
    path = Path(config.ROOT) / "deploy" / "cold_email_cutover.py"
    if not path.is_file():
        check(True, "the cutover helper is not in this image (deploy/ is not copied to the box); tests/ covers it")
        return
    spec = importlib.util.spec_from_file_location("cold_email_cutover_selftest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.plan_text(base / "pc", "siftstack-agents")
    order = [text.find("/DISABLE"), text.find("sftp put"), text.find("write home.json naming"), text.find("AGENTS_LIVE_STAGES=")]
    check(all(i > 0 for i in order) and order == sorted(order),
          "the dry plan reads in order: stop the PC, copy the state, name the box the home, then the top-up's switch")
    check(mod.merged_live("ftm_texts,research_queue") == "ftm_texts,research_queue,cold_email_topup"
          and mod.merged_live("ftm_texts,cold_email_topup") == "ftm_texts,cold_email_topup"
          and mod.without_live("a,cold_email_topup,b") == "a,b",
          "the live stages already there are kept when the top-up is added, and kept when it comes off")
