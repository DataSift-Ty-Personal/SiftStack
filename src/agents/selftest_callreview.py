"""Call review: the attempt log on every lead and the evening call screen. Zero network: calls seeded
into the brain's log copy, a fake DataSift with an owner message board and the task routes, and
stand-ins for the recording link, the transcriber and Claude (the FETCH, TRANSCRIBE and ASK hooks in
autopilot/callscreen.py); its own database and data folder. Every name, street, number and id here is
invented; numbers use the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)          # Monday 11:00am Eastern
ROWS = {"attemptlog": ("attemptlog", "run_attemptlog", "free", False, False),
        "callscreen": ("callscreen", "run_callscreen", "paid", False, True)}
EM, EN = chr(0x2014), chr(0x2013)
HOST = "rec.example.test"
FORBIDDEN = ("sms", "predictivecall", "skip-trace", "add-notes", "/assign/", "/status/", "smarter", "text", "mail")


def T(h: int, m: int = 0, day: int = 16, month: int = 11) -> datetime:
    return datetime(2026, month, day, h, m, tzinfo=UTC)


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------- the account
class FakeApiError(RuntimeError):
    def __init__(self, code: int, path: str):
        self.code = code
        super().__init__("HTTP %d on %s" % (code, path))


def _rec(u, street, owner, status=None, first="Pat", last="Example", company=None, tags=(), tasks=0, assigned=None):
    return {"uuid": u, "status": status, "tags": [{"title": t} for t in tags], "lists": [], "assigned_to": assigned,
            "address": {"street": street, "city": "Knoxville", "state": "TN"}, "tasks": {"incomplete": tasks},
            "owner": {"uuid": owner, "first_name": first, "last_name": last, "company": company, "phones": [],
                      "deceased": False}}


class FakeCRM:
    """Records and search rows, the owner's message board (list, read, create, edit, pin, unpin, delete)
    and the task routes. Every route call is kept, and anything else is refused out loud."""

    def __init__(self, recs: dict):
        self.recs = recs
        self.boards: dict[str, list] = {}
        self.tasks: dict[str, dict] = {}
        self.paths: list = []
        self.bodies: list = []
        self.n = 0
        self.pin_noop = False          # a pin route that answers and does nothing
        self.refuse_post = False       # a create route that answers 400 (the schema's owner number missing)

    def _id(self, prefix: str) -> str:
        self.n += 1
        return "%s-%04d" % (prefix, self.n)

    def owner_records(self, owner, cap=250):
        rows = [r for r in self.recs.values() if (r.get("owner") or {}).get("uuid") == owner]
        return [{"uuid": r["uuid"], "status": r.get("status"), "tasks": dict(r.get("tasks") or {}),
                 "address": dict(r["address"]),
                 "owner": {k: r["owner"].get(k) for k in ("uuid", "first_name", "last_name", "company")}}
                for r in rows][:cap]

    def record(self, u):
        return copy.deepcopy(self.recs[u])

    def custom_fields(self, u):
        return {}

    def message(self, u, text, pin=False):
        raise AssertionError("a call review stage wrote an add-notes note")

    def call(self, path, method="GET", body=None, headers=None):
        self.paths.append((method, path))
        self.bodies.append((method, path, copy.deepcopy(body)))
        p = path.split("?")[0]
        bits = [b for b in p.split("/") if b]
        if bits[:3] == ["api", "internal", "owner"] and len(bits) >= 5 and bits[4] == "message":
            board = self.boards.setdefault(bits[3], [])
            if len(bits) == 5:
                if method == "GET":
                    return {"count": len(board), "results": [dict(m) for m in reversed(board)]}
                if method == "POST":
                    if self.refuse_post:
                        raise FakeApiError(400, path)
                    m = {"uuid": self._id("msg"), "message": body["message"], "pinned": False}
                    board.append(m)
                    return dict(m)
            m = next((x for x in board if x["uuid"] == bits[5]), None) if len(bits) > 5 else None
            if m is None:
                raise FakeApiError(404, path)
            if len(bits) == 6:
                if method == "GET":
                    return dict(m)
                if method == "PATCH":
                    m["message"] = body["message"]
                    return dict(m)
                if method == "DELETE":
                    board.remove(m)
                    return None
            if len(bits) == 7 and method == "POST" and bits[6] in ("pin", "unpin"):
                if bits[6] == "unpin":
                    m["pinned"] = False
                elif not self.pin_noop:
                    m["pinned"] = True
                return None
        if p == "/api/internal/task/" and method == "POST":
            t = dict(body, uuid=self._id("task"))
            self.tasks[t["uuid"]] = t
            return dict(t)
        if bits[:3] == ["api", "internal", "task"] and len(bits) == 4 and method == "GET":
            return dict(self.tasks[bits[3]])
        raise AssertionError("unexpected DataSift call: %s %s" % (method, path))

    def writes(self) -> list:
        return [x for x in self.paths if x[0] != "GET"]

    def ours(self, owner: str) -> list:
        return [m for m in self.boards.get(owner, []) if m["message"].startswith("Attempt log")]

    def pinned(self, owner: str) -> list:
        return [m for m in self.boards.get(owner, []) if m["pinned"]]


# ---------------------------------------------------------------------- the log
def call_ev(kind, i, owner, when, call, secs=0, rec="", caller="", number="8650000101", inbound=False):
    return {"id": "%s-%s" % (kind, i), "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"direction": "inbound" if inbound else "outbound",
                                 "destination_number": "8650000099" if inbound else number,
                                 "origin_number": number if inbound else "8650000099", "duration": secs,
                                 "recording": rec, "uuid": call, "source": "kixie",
                                 "user": {"first_name": caller, "uuid": "u-emerson"} if caller else {}}}}


def dial(i, owner, when, number="8650000101", answered=None, noanswer=False, rec="", caller="Emerson") -> list:
    """One outbound call as the dialer logs it: the dial, then an answer (with its length) or a no answer."""
    cid = "c-%s" % i
    out = [call_ev("made", i, owner, when, cid, number=number)]
    if answered is not None:
        out.append(call_ev("answered", i, owner, when + timedelta(seconds=20), cid, secs=answered, rec=rec, caller=caller,
                           number=number))
    if noanswer:
        out.append(call_ev("noanswer", i, owner, when + timedelta(seconds=30), cid, number=number))
    return out


def status_ev(i, rec, when, old, new):
    return {"id": "st-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def phone_ev(i, owner, when, number, old, new):
    return {"id": "ph-%s" % i, "event_type": "owner.phone.status.updated", "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"owner": {"phone": number, "status": [old, new]}}}


def playbook(sid: str, kind: str, mode: str = "live", **over) -> dict:
    stage = {"kind": kind, "owner": "sales" if kind == "attemptlog" else "ops", "mode": mode,
             "schedule": ["every 15 min 08:00-19:30 Mon-Sat"] if kind == "attemptlog" else ["19:45 Mon-Fri"],
             "sync": False, "read_fields": False}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]}, "stages": {sid: stage}}


def _register():
    """Until the orchestrator adds the registry lines, this section registers the two kinds for itself
    and takes them out again afterwards. Once the lines are in registry.py this does nothing."""
    from .autopilot import policy, registry, sweep
    added = [k for k in ROWS if k not in registry.EXTRA]
    if not added:
        return lambda: None
    kinds, no_presets, reports = policy.KINDS, policy.NO_PRESETS, set(sweep.REPORT_KINDS)
    for k in added:
        registry.EXTRA[k] = ROWS[k]
        policy.SPEND_BY_KIND[k] = ROWS[k][2]
    policy.KINDS = kinds + tuple(added)
    policy.NO_PRESETS = no_presets + tuple(added)
    sweep.REPORT_KINDS |= {k for k in added if ROWS[k][4]}

    def undo():
        for k in added:
            registry.EXTRA.pop(k, None)
            policy.SPEND_BY_KIND.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = kinds, no_presets
        sweep.REPORT_KINDS.clear()
        sweep.REPORT_KINDS.update(reports)
    return undo


class _Stage:
    def __init__(self, sid: str, params: dict):
        self.id, self.params = sid, params

    def get(self, k, d=None):
        v = self.params.get(k, d)
        return d if v is None else v


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import callscreen
    from call_coaching import transcribe as tr
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY)
    hooks = (callscreen.FETCH, callscreen.TRANSCRIBE, callscreen.ASK)
    post = tr.POST
    config.DATA_DIR = Path(tmp) / "callreview"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    undo_reg = _register()

    def _no_openrouter(body, timeout):
        raise AssertionError("the call review selftest tried to reach OpenRouter")
    tr.POST = _no_openrouter
    try:
        _pure(check)
        _attempt_runs(check)
        _screen_runs(check)
    finally:
        callscreen.FETCH, callscreen.TRANSCRIBE, callscreen.ASK = hooks
        tr.POST = post
        undo_reg()
        config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY = keep


# ---------------------------------------------------------------------- the rules on their own
def _pure(check) -> None:
    from .autopilot import attemptlog as al, callscreen as cs
    print("  reading a recording")
    talk = "AGENT: Hi, this is Emerson.\nSELLER: Yes, go ahead.\n\nDELIVERY SUMMARY:\n- pace: SELLER: sounded rushed"
    vm = "VOICEMAIL: Please leave a message.\nAGENT_VM: Hi, this is Emerson, please call me back."
    greet = "VOICEMAIL: The mailbox is full."
    check(al.call_kind(talk) == "talk" and al.call_kind(vm) == "voicemail_left" and al.call_kind(greet) == "voicemail"
          and al.call_kind("AGENT: Hello? Hello?") == "nobody",
          "a call with a SELLER line is a conversation; without one it is a voicemail, with or without our message")
    check([a for a, _ in al.turns(talk)] == ["AGENT", "SELLER"],
          "the transcriber's delivery summary is not part of the call: reading stops there")
    print("  folding a call's states")
    evs = []
    for e in dial("f1", "own-x", T(14), answered=40) + dial("f2", "own-x", T(14, 5), noanswer=True):
        e = copy.deepcopy(e)
        e["when"] = datetime.fromisoformat(e["timestamp"].replace(" ", "T")).replace(tzinfo=UTC)
        d = e["payload"]["call"]
        e["data"] = {"call": d["uuid"], "direction": d["direction"], "duration": d["duration"], "number": d["destination_number"],
                     "recording": d["recording"], "caller": (d.get("user") or {}).get("first_name") or ""}
        evs.append(e)
    folded = al.fold(evs)
    check(sorted(folded) == ["c-f1", "c-f2"] and folded["c-f1"]["answered"]["seconds"] == 40
          and folded["c-f2"]["noanswer"] and folded["c-f2"]["answered"] is None,
          "the dial and its answer fold into one call on the call id; a no-answer row marks a call never answered")
    loose = copy.deepcopy(evs[1])
    loose["data"]["call"], loose["id"] = "", "loose-answer"
    pair = al.fold([evs[0], loose])
    check(len(pair) == 1, "an answer row with no call id joins the dial to the same number just before it, never a second call")
    print("  checking a quote")
    tx = ("AGENT: Honestly we could probably do around 120 thousand for it, as is.\n"
          "SELLER: That seems low to me, but maybe if the price is right.\n"
          "AGENT: We could.\nSELLER: talk tomorrow\nAGENT: pay more then")
    sp = cs.speakers(tx)
    check(cs.quoted("we could probably do around 120 thousand", sp["agent"]) and not cs.quoted("we could probably do around 120 thousand", sp["seller"]),
          "a quote is matched in one speaker's own words")
    check(not cs.quoted("we could pay more then", sp["agent"]),
          "a quote is matched inside one turn: two halves said apart are not one sentence")
    check(not cs.quoted("maybe if", sp["seller"], 3) and cs.quoted("maybe if the price is right", sp["seller"], 3),
          "a quote shorter than three words proves nothing")
    check(cs.has_number("around 120 thousand") and cs.has_number("about seventy percent of value")
          and not cs.has_number("below what it is worth"), "a price quote must carry a number, in digits or in words")
    check(cs.refuses("I'm not interested, don't call again") and not cs.refuses("give me a few weeks to think"),
          "a quote that says no is a refusal, whatever the model made of the rest of the call")
    check(cs.objection_of("third_party") == "third party" and cs.objection_of("Trust") == "trust"
          and cs.objection_of("the price") == "price" and cs.objection_of("timing") == "time"
          and cs.objection_of("weather") == "other", "the missed objection is named as price, time, third party or trust")
    raw = {"price": {"found": True, "quote": "we could probably do around 120 thousand"},
           "soft_yes": {"found": True, "quote": "maybe if the price is right"},
           "no_refusal": {"found": True, "quote": "That seems low to me"},
           "missed_objection": {"found": "true", "objection": "price", "quote": "it is worth way more"}}
    kept, dropped = cs.validate(raw, tx, list(cs.CHECKS))
    check(set(kept) == {"price", "soft_yes", "no_refusal"} and dropped == {"missed_objection": "the quote is not in the call"},
          "every finding whose quote is in the right speaker's words is kept, and the rest dropped with the reason")
    check(cs.scrub('call me at 865-000-0123 or (865) 000-0145, Robin', ["Robin", "Example"]) ==
          "call me at [number] or [number], [name]", "a quote in the channel carries no phone number and no owner name")
    check(cs.scrub("$120,000 and 1,250 square feet") == "$120,000 and 1,250 square feet", "a price is not a phone number")
    print("  the blocks")
    bad = _Stage("x", {"max_owners": "many", "settle_minutes": -1, "statuses": [], "caller_names": "yes"})
    probs = al.block_problems(bad)
    check(len(probs) == 4 and al.block_problems(_Stage("x", {})) == [],
          "an attempt log block with a wrong setting is refused when the playbook loads: %s" % probs)
    bad = _Stage("y", {"max_calls": -2, "call_events": ["owner.call.made"], "model": "", "transcribe": "on",
                       "no_statuses": "not_interested"})
    probs = cs.block_problems(bad)
    check(len(probs) == 5 and cs.block_problems(_Stage("y", {})) == [],
          "a call screen block with a wrong setting is refused when the playbook loads: %s" % probs)
    print("  the prompt")
    check(all(("- %s:" % k) in cs.SYSTEM for k in cs.CHECKS) and "copied word for word" in cs.SYSTEM
          and "Never guess" in cs.SYSTEM, "the model is told the quote rule and each check")
    u = cs.user_text("AGENT: hi", ["price", "no_refusal"], ["the record was marked Not Interested 3 min after the call"])
    check(u.startswith("Checks for this call: price, no_refusal.") and "marked Not Interested" in u,
          "the model is asked only the checks that apply, with what the code found after the call")
    for name, text in (("SYSTEM", cs.SYSTEM), ("HEADER", al.HEADER), ("CONTINUED", al.CONTINUED)):
        check(EM not in text and EN not in text, "no long dash and no en dash in the %s text" % name)


# ---------------------------------------------------------------------- the attempt log, run by run
def _attempt_recs() -> dict:
    out = {}
    for r in (_rec("r-a", "11 Example St", "own-a", "Warm Lead"),
              _rec("r-b", "22 Sample Ave", "own-b", "Hot Lead"),
              _rec("r-p", "5 Prospect Way", "own-p"),
              _rec("r-ni", "7 Turn Ct", "own-ni", "not_interested"),
              _rec("r-new", "9 Fresh Ln", "own-new", "new_lead"),
              _rec("r-vm", "33 Quiet Ln", "own-vm", "Cold Lead"),
              _rec("r-wait", "44 Wait Rd", "own-wait", "Warm Lead"),
              _rec("r-llc", "8 Holding Ct", "own-llc", "Warm Lead", first=None, last=None, company="Example Holdings LLC"),
              _rec("r-dnm", "10 Quiet Pl", "own-dnm", "Warm Lead", tags=["Do Not Market"]),
              _rec("r-fresh", "12 Soon St", "own-fresh", "Warm Lead"),
              _rec("r-short", "14 Brief Ct", "own-short", "Warm Lead"),
              _rec("r-c", "16 Delete Dr", "own-c", "Warm Lead"),
              _rec("r-rb", "18 Pin St", "own-rb", "Warm Lead"),
              _rec("r-rb2", "20 Next St", "own-rb2", "Warm Lead"),
              _rec("r-f1", "22 Fail St", "own-f1", "Warm Lead"),
              _rec("r-f2", "24 Wait St", "own-f2", "Warm Lead")):
        out[r["uuid"]] = r
    return out


TALK_NI = ("AGENT: Hi, this is Emerson about Turn Court.\nSELLER: We have thought about it and we will keep the house.\n"
           "AGENT: Understood, thank you.")
TALK_NEW = ("AGENT: Hi, Emerson about Fresh Lane.\nSELLER: Yes, we would like to sell before the spring.\n"
            "AGENT: Great, let me set up a time to see it.")
VM = "VOICEMAIL: Please leave a message after the tone.\nAGENT_VM: Hi, this is Emerson about Quiet Lane, call me back."


def _attempt_runs(check) -> None:
    from .autopilot import activity, attemptlog as al, leadnotes, ledger, policy, sweep
    activity.reset()
    rows = []
    rows += [call_ev("made", "zz", "own-zz", T(15, 0, day=25, month=10), "c-zz")]     # the oldest call in the copy
    rows += [status_ev("a1", "r-a", T(15, day=10), "prospecting", "new_lead"), status_ev("a2", "r-a", T(15, day=12), "new_lead", "Warm Lead")]
    rows += dial("a1", "own-a", T(14), noanswer=True)
    rows += dial("a2", "own-a", T(14, 30), number="8650000102", answered=15)
    rows += [phone_ev("a2", "own-a", T(14, 31), "8650000102", "UNKNOWN", "WRONG")]
    rows += [status_ev("b1", "r-b", T(15, day=9), "prospecting", "Hot Lead")]
    rows += dial("b1", "own-b", T(14, 10), noanswer=True)
    rows += dial("p1", "own-p", T(14, 20), noanswer=True)
    rows += [status_ev("ni1", "r-ni", T(15, day=11), "prospecting", "Warm Lead"),
             status_ev("ni2", "r-ni", T(15, 5), "Warm Lead", "not_interested")]
    rows += dial("ni1", "own-ni", T(15), answered=200, rec="https://%s/c-ni1.mp3" % HOST)
    rows += [status_ev("new1", "r-new", T(15, 20), "prospecting", "new_lead")]
    rows += dial("new0", "own-new", T(14), noanswer=True)
    rows += dial("new1", "own-new", T(15, 10), answered=300, rec="https://%s/c-new1.mp3" % HOST)
    for i, day in (("vmo1", 1), ("vmo2", 3), ("vmo3", 5)):
        rows += dial(i, "own-vm", T(15, day=day), noanswer=True)
    rows += dial("vm1", "own-vm", T(14, 40), answered=40)
    rows += [status_ev("w1", "r-wait", T(15, day=12), "prospecting", "Warm Lead")]
    rows += dial("w1", "own-wait", T(15, 30), answered=200, rec="https://%s/c-w1.mp3" % HOST)
    rows += dial("w2", "own-wait", T(15, 40), noanswer=True)
    rows += [status_ev("llc1", "r-llc", T(15, day=12), "prospecting", "Warm Lead")]
    rows += dial("llc1", "own-llc", T(14, 50), noanswer=True)
    rows += [status_ev("d1", "r-dnm", T(15, day=12), "prospecting", "Warm Lead")]
    rows += dial("d1", "own-dnm", T(14, 55), noanswer=True)
    rows += [status_ev("f1", "r-fresh", T(15, day=12), "prospecting", "Warm Lead")]
    rows += [call_ev("made", "f1", "own-fresh", T(15, 55), "c-f1")]                    # five minutes before the run
    rows += [status_ev("s1", "r-short", T(15, day=12), "prospecting", "Warm Lead")]
    rows += dial("s1", "own-short", T(14, 45), answered=75)
    rows += [status_ev("c1", "r-c", T(15, day=12), "prospecting", "Warm Lead")]
    rows += dial("c1", "own-c", T(14, 5), noanswer=True)
    activity._store(rows)
    for cid, text in (("c-ni1", TALK_NI), ("c-new1", TALK_NEW), ("c-vm1", VM)):
        f = leadnotes.transcript_file(cid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    ledger.set_fact("call:c-ni1", "leadnote", {"status": "noted"})          # lead notes wrote a note for that call
    ledger.set_fact("call:c-new1", "leadnote", {"status": "skipped"})       # and none for this one
    crm = FakeCRM(_attempt_recs())
    crm.boards["own-b"] = [{"uuid": "msg-person-b", "message": "Seller prefers a text after 5pm.", "pinned": True}]

    def go(sid, now, mode="live", **over):
        over.setdefault("gate_allow", ["not_interested"])
        return sweep.run(sid, crm=crm, now=now, pol=policy.parse(playbook(sid, "attemptlog", mode, **over), EMPLOYEES),
                         sources={}, scorer=False)

    def line_of(owner: str) -> list[str]:
        return [ln for m in crm.ours(owner) for ln in m["message"].split("\n")]

    # ------------------------------------------------------------------ plan mode
    print("  the attempt log in plan mode")
    plan = go("al_plan", NOW, mode="plan")
    check(not crm.writes(), "a plan run writes nothing to the account: %s" % crm.writes()[:3])
    check(plan.head.startswith("Would write 8 call lines on 7 attempt logs (7 new)"),
          "a plan run says the lines it would write and on how many logs: %r" % plan.head)
    check(plan.tally.get("skipped: not a lead when dialed") == 2,
          "a dial to a house that was not a lead at the time is never logged (a prospect, and the dial an hour before a lead)")
    check(plan.tally.get("skipped: a company or trust owns it (people only)") == 1, "a house a company owns is never logged")
    check(plan.tally.get("held back: tagged Do Not Market") == 1, "the gate runs on every lead record before a line is written")
    check(any("would be left unpinned" in x and "22 Sample Ave" in x for x in plan.lines),
          "a plan run says which new log would sit unpinned under a person's pinned note")
    check(sweep.posts({"id": 1, "workflow": "al_plan", "source": "schedule"}, plan.as_dict()) == [],
          "a plan-mode attempt log with nothing wrong posts nothing on its own")
    again = go("al_plan", NOW, mode="plan")
    check(again.acted == 0 and "Would write" not in again.head, "a second plan run counts no call twice: %r" % again.head)

    # ------------------------------------------------------------------ live
    print("  the attempt log, live")
    config.MODE = "live"
    live = go("al_live", NOW)
    check(al.KEY != al.PLAN_KEY and ledger.fact("call:c-a1", al.PLAN_KEY).get("status") == "written"
          and ledger.fact("call:c-a1", al.KEY).get("status") == "written",
          "plan marks are their own: a call a plan run counted is still written by the live run")
    a = crm.ours("own-a")
    check(len(a) == 1 and a[0]["pinned"], "a lead's first call starts ONE log, pinned")
    lines = a[0]["message"].split("\n")
    check(lines[0] == al.HEADER and lines[1] == "House: 11 Example St, Knoxville, a lead since Nov 10.",
          "the log is headed by what it is, the house and the day it became a lead: %r" % lines[:2])
    check(lines[2:] == ["Mon Nov 16, 9:00 AM: attempt 1, no answer",
                        "Mon Nov 16, 9:30 AM: attempt 2, answered, 15 s, number marked wrong (Emerson)"],
          "one dated line per call, numbered, with what the log says happened: %r" % lines[2:])
    check(len(crm.ours("own-b")) == 1 and not crm.ours("own-b")[0]["pinned"] and len(crm.pinned("own-b")) == 1,
          "under a person's pinned note the new log goes in unpinned: a lead never carries two pinned notes")
    check(not crm.boards.get("own-p") and not crm.boards.get("own-llc") and not crm.boards.get("own-dnm"),
          "nothing is written for a prospect, a company's house or a house the gate holds")
    check("Mon Nov 16, 10:00 AM: attempt 1, talked 3 min, see the lead note (Emerson)" in line_of("own-ni"),
          "a conversation lead notes wrote up says so; the call that ended the lead is logged on the status it was made under")
    check("Mon Nov 16, 10:10 AM: attempt 1, talked 5 min (Emerson)" in line_of("own-new"),
          "the dial just before a record became a lead is the call that made it one: attempt 1, no lead note pointer")
    vm_lines = line_of("own-vm")
    check("Mon Nov 16, 9:40 AM: attempt 4, voicemail, message left (Emerson)" in vm_lines,
          "a recording with no seller on it is a voicemail; the count holds the calls before the log began")
    check("House: 33 Quiet Ln, Knoxville, a lead since before Oct 25 (as far back as the call log here reaches)." in vm_lines
          and "Earlier calls while a lead: 3, before this log began." in vm_lines,
          "a lead older than the log copy says how far back the count reaches, and how many calls came first")
    short = line_of("own-short")
    check("Mon Nov 16, 9:45 AM: attempt 1, answered, 75 s (Emerson)" in short
          and not [x for x in short if "voicemail" in x or "talked" in x],
          "an answered call with no transcript says only that it was answered and how long: never a guess from the length")
    check(not crm.ours("own-wait") and not crm.ours("own-fresh"),
          "a call still settling, or still waiting for lead notes' transcript, is not written yet")
    check(live.head.startswith("Wrote 8 call lines on 7 attempt logs (7 new); 3 calls wait for the next run."),
          "a live run says what it wrote and what waits: %r" % live.head)
    check(not [x for x in crm.paths if any(w in x[1] for w in FORBIDDEN)] and all("/message/" in x[1] for x in crm.paths),
          "the attempt log only ever touches the owner's message board: no text, no call, no status, no counter")
    new_logs = [x for x in crm.writes() if x[0] == "POST" and x[1].endswith("/message/")]
    reads_back = [x for x in crm.paths if x[0] == "GET" and x[1].count("/") == 7 and "?" not in x[1]]
    check(len(new_logs) == 7 and len(reads_back) >= 7, "every new log is read back after it is written")
    ch = [c for c in ledger.changes(record_uuid="r-a")]
    check(ch and ch[0]["what"] == "attempt_note" and ch[0]["before"] is None and "attempt 2" in ch[0]["after"]["text"],
          "each live write records what it replaced, so the run can be put back")
    posts = sweep.posts({"id": 2, "workflow": "al_live", "source": "schedule"}, live.as_dict())
    check("attemptlog" in sweep.QUIET_LIVE_KINDS and not [p for p in posts if p["kind"] == "pipeline"],
          "a live attempt log run posts no update: it acts on nearly every call and would post about 40 times a day")
    for owner in list(crm.boards):
        check(len(crm.pinned(owner)) <= 1, "never two pinned notes on one board (%s)" % owner)
    for m in [m for b in crm.boards.values() for m in b]:
        check(EM not in m["message"] and EN not in m["message"], "no long dash and no en dash in a note")

    # ------------------------------------------------------------------ a person edits, unpins, deletes
    print("  the attempt log after a person touched it")
    a[0]["message"] += "\nEmerson: the seller asked for a call after 5pm."
    crm.boards["own-b"][0]["pinned"] = False                  # the person's own note is unpinned
    deleted = crm.ours("own-c")[0]["uuid"]
    crm.boards["own-c"] = [m for m in crm.boards["own-c"] if m["uuid"] != deleted]
    activity._store(dial("a3", "own-a", T(16, 10), noanswer=True) + dial("b2", "own-b", T(16, 20), noanswer=True)
                    + dial("c2", "own-c", T(16, 30), noanswer=True))
    third = go("al_live", NOW + timedelta(hours=3))
    a = crm.ours("own-a")
    lines = a[0]["message"].split("\n") if a else []
    check(len(a) == 1 and lines[-2:] == ["Emerson: the seller asked for a call after 5pm.",
                                         "Mon Nov 16, 11:10 AM: attempt 3, no answer"],
          "the next call is added to the SAME log, after the line a person typed, which stays: %r" % lines[-3:])
    check(len(crm.ours("own-b")) == 1 and crm.ours("own-b")[0]["pinned"] and len(crm.pinned("own-b")) == 1,
          "a log left unpinned under a person's note is pinned once that note is no longer pinned")
    check(len(crm.ours("own-c")) == 1 and crm.ours("own-c")[0]["uuid"] != deleted and crm.ours("own-c")[0]["pinned"]
          and third.tally.get("a person deleted an attempt log, so a new one was started") == 1,
          "a log a person deleted is left deleted, and the next call starts a new one")
    wait = line_of("own-wait")
    check(wait[-2:] == ["Mon Nov 16, 10:30 AM: attempt 1, answered, 3 min (Emerson)", "Mon Nov 16, 10:40 AM: attempt 2, no answer"],
          "a call held for its transcript is written once the wait is over, honestly, and the later call after it, in order: %r" % wait[-2:])
    check(line_of("own-fresh")[-1] == "Mon Nov 16, 10:55 AM: attempt 1, no answer",
          "a dial no answer row ever followed is written as no answer once the log has caught up")
    a[0]["pinned"] = False                                     # a person unpins the log
    activity._store(dial("a4", "own-a", T(19, 10), noanswer=True))
    fourth = go("al_live", NOW + timedelta(hours=4))
    a = crm.ours("own-a")
    check(len(a) == 2 and not a[0]["pinned"] and a[1]["pinned"] and len(crm.pinned("own-a")) == 1
          and fourth.tally.get("a person unpinned an attempt log, so a new one was started") == 1,
          "a log a person unpinned stays as they left it, and the next call starts a new pinned one")
    check("Earlier calls while a lead: 3, before this log began." in a[1]["message"]
          and a[1]["message"].endswith("Mon Nov 16, 2:10 PM: attempt 4, no answer"),
          "the new log keeps counting from the lead's start")

    # ------------------------------------------------------------------ a write that does not read back
    print("  the attempt log when a write does not read back")
    crm.pin_noop = True
    activity._store([status_ev("rb", "r-rb", T(15, day=12), "prospecting", "Warm Lead"),
                     status_ev("rb2", "r-rb2", T(15, day=12), "prospecting", "Warm Lead")]
                    + dial("rb1", "own-rb", T(20, 10), noanswer=True) + dial("rb2", "own-rb2", T(20, 12), noanswer=True))
    fifth = go("al_live", NOW + timedelta(hours=5))
    check(any(p.startswith("the attempt log on 18 Pin St, Knoxville did not read back as written (not pinned)") for p in fifth.problems),
          "a log whose pin did not hold is named as a problem: %s" % fifth.problems)
    check(not crm.boards.get("own-rb2") and any("held by a board write that failed or did not read back" in x for x in fifth.lines),
          "after a write that did not read back, no other log is written that run")
    check(ledger.fact("own-rb", al.KEY).get("pinned") is False,
          "the pin is remembered as it was read back, not as it was asked for")
    crm.pin_noop = False
    activity._store(dial("rb3", "own-rb", T(21, 10), noanswer=True))
    go("al_live", NOW + timedelta(hours=6))
    rb = crm.ours("own-rb")
    check(len(rb) == 1 and rb[0]["pinned"] and rb[0]["message"].endswith("attempt 2, no answer") and crm.ours("own-rb2"),
          "so the next run adds to the same log (and pins it) instead of starting a second one; the held owner is written")
    crm.refuse_post = True
    activity._store([status_ev("f1", "r-f1", T(15, day=12), "prospecting", "Warm Lead"),
                     status_ev("f2", "r-f2", T(15, day=12), "prospecting", "Warm Lead")]
                    + dial("fl1", "own-f1", T(22, 10), noanswer=True) + dial("fl2", "own-f2", T(22, 12), noanswer=True))
    seventh = go("al_live", NOW + timedelta(hours=7))
    tried = [x for x in crm.paths if x[0] == "POST" and x[1] in ("/api/internal/owner/own-f1/message/", "/api/internal/owner/own-f2/message/")]
    check(len(tried) == 1 and len([p for p in seventh.problems if p.startswith("could not write the attempt log on")]) == 1
          and not crm.boards.get("own-f1") and not crm.boards.get("own-f2"),
          "a board that refuses a log stops that run's writes after the first refusal, named once: %s" % seventh.problems)
    crm.refuse_post = False
    go("al_live", NOW + timedelta(hours=8))
    check(crm.ours("own-f1") and crm.ours("own-f1")[0]["message"].endswith("Mon Nov 16, 5:10 PM: attempt 1, no answer")
          and crm.ours("own-f2"), "the refused calls wait, and the next run writes them once the board takes them")

    # ------------------------------------------------------------------ undo
    print("  undoing the attempt log")
    edits = [c for c in ledger.changes(record_uuid="r-a") if isinstance(c["before"], dict) and "text" in c["before"]]
    first_log = [c for c in ledger.changes(record_uuid="r-a") if c["before"] is None][0]
    msg = crm.boards["own-a"][0]
    al.undo_step(crm, edits[0])
    check(msg["message"] == edits[0]["before"]["text"], "undo puts back the text an edit replaced")
    al.undo_step(crm, first_log)
    check(msg not in crm.boards["own-a"], "undo deletes a log the run started (the board's own documented route)")
    check(not [x for x in crm.paths if any(w in x[1] for w in FORBIDDEN)],
          "nothing the attempt log did ever reached a text, a call or a status route")


# ---------------------------------------------------------------------- the call screen, run by run
S = {
    "s1": ("AGENT: Hi, this is Emerson about the house on Price Road.\nSELLER: Okay, what would you pay?\n"
           "AGENT: Honestly we could probably do around 120 thousand for it, as is.\nSELLER: That seems low to me.\n\n"
           "DELIVERY SUMMARY:\n- pace: conversational, the seller might think about it"),
    "s2": "AGENT: Hi, Emerson here about your place on Ask Avenue.\nSELLER: I want at least 150 thousand for it, no less.\nAGENT: I understand.",
    "s3": "AGENT: Hello, this is Emerson about the house on Open Lane.\nSELLER: Maybe if the price is right, call me back next month.\nAGENT: Will do.",
    "s4": "AGENT: Hi, Emerson about Task Court.\nSELLER: Call me back after the holidays and we can talk.\nAGENT: Sounds good.",
    "s5": "AGENT: Hi, Emerson about the house on Lead Way.\nSELLER: I would consider selling it this spring.\nAGENT: Great.",
    "s6": ("AGENT: Hi, Emerson about Pause Drive.\nSELLER: This is Robin. Can you give me a few weeks to talk it over with my husband?\n"
           "AGENT: Of course."),
    "s7": "AGENT: Hi, Emerson about Firm Street.\nSELLER: I'm not interested, please don't call again.\nAGENT: Understood.",
    "s8": "AGENT: Hi, Emerson about Plain Road.\nSELLER: We just bought this house last year.\nAGENT: Congratulations.",
    "s9": ("VOICEMAIL: Please leave a message after the tone.\n"
           "AGENT_VM: Hi, this is Emerson, we buy houses as is and could pay up to 90 thousand cash, call me back."),
    "s11": "AGENT: Hi, Emerson about Buy Boulevard.\nSELLER: Not right now, my mother still lives there.\nAGENT: Okay.",
    "s12": "AGENT: Hi, Emerson about Coach Court.\nSELLER: You can text me at 865-000-0123 with the number you have in mind.\nAGENT: I will.",
    "q1a": "AGENT: Hi, Emerson about Quiet Lane, a first call.\nSELLER: Tell me more about how this works for us.",
    "q1b": "AGENT: Emerson again about Quiet Lane.\nSELLER: I need to talk to my brother first, he owns half.\nAGENT: Okay, what timeline works?",
    "q1c": "AGENT: Following up on Quiet Lane.\nSELLER: I have not heard back from my brother yet.\nAGENT: No problem.",
    "q2a": "AGENT: Checking in on Contract Road.\nSELLER: How do I know you will actually close on this?\nAGENT: Let's get the inspection set.",
    "late": "AGENT: Hi, Emerson about Plain Road again.\nSELLER: Still not selling, sorry.\nAGENT: Thanks anyway.",
    "x1": "AGENT: Hi, Emerson about Cap Court.\nSELLER: We might sell next year.\nAGENT: Thanks.",
    "x2": "AGENT: Hi, Emerson about Cap Lane.\nSELLER: Not interested in selling at all.\nAGENT: Okay.",
    "z1": "AGENT: Hi, Emerson about Cap Court again.\nSELLER: Nothing has changed since we spoke.\nAGENT: Thanks.",
}


def _reply(d: dict) -> str:
    return "Here is the screen.\n```json\n%s\n```" % json.dumps(d)


R = {
    "s1": _reply({"price": {"found": True, "quote": "we could probably do around 120 thousand for it"},
                  "soft_yes": {"found": True, "quote": "might think about it"}}),
    "s2": _reply({"price": {"found": True, "quote": "I want at least 150 thousand"}, "soft_yes": {"found": False, "quote": ""}}),
    "s3": _reply({"price": {"found": False, "quote": ""}, "soft_yes": {"found": True, "quote": "Maybe if the price is right"}}),
    "s4": _reply({"soft_yes": {"found": True, "quote": "Call me back after the holidays"}}),
    "s5": _reply({"soft_yes": {"found": True, "quote": "I would consider selling it this spring"}}),
    "s6": _reply({"soft_yes": {"found": False, "quote": ""},
                  "no_refusal": {"found": True, "quote": "This is Robin. Can you give me a few weeks to talk it over"}}),
    "s7": _reply({"no_refusal": {"found": True, "quote": "I'm not interested, please don't call again"}}),
    "s8": json.dumps({"price": {"found": False, "quote": ""}, "soft_yes": {"found": False, "quote": ""}}),
    "s9": _reply({"price": {"found": True, "quote": "could pay up to 90 thousand cash"}}),
    "s11": _reply({"price": {"found": False}, "soft_yes": {"found": False}}),
    "s12": _reply({"soft_yes": {"found": True, "quote": "text me at 865-000-0123 with the number you have in mind"}}),
    "q1b": _reply({"missed_objection": {"found": True, "objection": "third party", "quote": "I need to talk to my brother first"}}),
    "q1c": _reply({"missed_objection": {"found": False, "objection": "", "quote": ""}}),
    "q2a": _reply({"missed_objection": {"found": True, "objection": "trust",
                                        "quote": "How do I know you will actually close on this"}}),
    "late": _reply({"soft_yes": {"found": False}}),
    "x1": _reply({"soft_yes": {"found": False}}),
    "x2": _reply({"soft_yes": {"found": False}}),
    "z1": _reply({"soft_yes": {"found": False}}),
}
LOG: dict = {"fetch": [], "transcribe": [], "ask": []}


def _fetch(url, max_bytes):
    stem = str(url).rsplit("/", 1)[-1].split(".")[0]
    LOG["fetch"].append(stem)
    return {"ok": True, "code": "ok", "fmt": "mp3", "audio": stem.encode("utf-8"), "host": HOST, "size": 10}


def _transcribe(audio, fmt, caller):
    stem = audio.decode("utf-8")
    LOG["transcribe"].append(stem)
    return S[stem.replace("c-", "")], {"cost": 0.006}


def _ask(system, user, model, max_tokens):
    found = next((k for k, t in S.items() if t in user), "")
    LOG["ask"].append((found, model, user.split("\n", 1)[0]))
    return R.get(found, "nothing"), {"input_tokens": 3000, "output_tokens": 200}, "end_turn"


def _screen_recs() -> dict:
    out = {}
    for r in (_rec("r-s1", "31 Price Rd", "own-s1"), _rec("r-s2", "32 Ask Ave", "own-s2"),
              _rec("r-s3", "33 Open Ln", "own-s3", assigned="u-emerson"), _rec("r-s4", "34 Task Ct", "own-s4", tasks=1),
              _rec("r-s5", "35 Lead Way", "own-s5", "new_lead"),
              _rec("r-s6", "36 Pause Dr", "own-s6", "not_interested", first="Robin", last="Sample"),
              _rec("r-s7", "37 Firm St", "own-s7", "not_interested"), _rec("r-s8", "38 Plain Rd", "own-s8"),
              _rec("r-s9", "39 Tone Ct", "own-s9"), _rec("r-s10", "40 Brief St", "own-s10"),
              _rec("r-s11", "41 Buy Blvd", "own-s11"), _rec("r-s12", "42 Coach Ct", "own-s12"),
              _rec("r-s13", "43 Holding Way", "own-s13", first=None, last=None, company="Example Holdings LLC"),
              _rec("r-q1", "44 Quiet Ln", "own-q1", "Ghosting Lead"), _rec("r-q2", "45 Contract Rd", "own-q2", "Warm Lead"),
              _rec("r-x1", "46 Cap Ct", "own-x1"), _rec("r-x2", "47 Cap Ln", "own-x2")):
        out[r["uuid"]] = r
    return out


def _answered(i, owner, when, secs, rec=True, inbound=False):
    kind = "received" if inbound else "answered"
    return call_ev(kind, i, owner, when, "c-%s" % i, secs=secs, rec=("https://%s/c-%s.mp3" % (HOST, i)) if rec else "",
                   caller="Emerson", inbound=inbound)


def _screen_runs(check) -> None:
    from . import store
    from .autopilot import activity, callscreen as cs, leadnotes, ledger, policy, sweep
    from .workflows import call_coaching
    activity.reset()
    for k in LOG:
        LOG[k].clear()
    cs.FETCH, cs.TRANSCRIBE, cs.ASK = _fetch, _transcribe, _ask
    config.MODE = "dry"
    now = T(23, 45)                                          # 6:45pm Eastern: the window ends at 4:45pm
    rows = []
    for i, (owner, h, secs) in enumerate((("own-s1", 14, 240), ("own-s2", 14, 120), ("own-s3", 15, 90), ("own-s4", 15, 100),
                                          ("own-s5", 16, 130), ("own-s6", 16, 110), ("own-s7", 17, 95), ("own-s8", 17, 70),
                                          ("own-s9", 18, 90), ("own-s10", 18, 45), ("own-s11", 19, 240), ("own-s12", 19, 150),
                                          ("own-s13", 20, 300))):
        rows.append(_answered(owner.replace("own-", ""), owner, T(h, (5 * i) % 60), secs))
    rows += [status_ev("s5", "r-s5", T(16, 30), "prospecting", "new_lead"),
             status_ev("s6", "r-s6", T(16, 28), "Warm Lead", "not_interested"),
             status_ev("s7", "r-s7", T(17, 32), "Warm Lead", "not_interested")]
    rows += [_answered("q1a", "own-q1", T(15, day=1), 120), _answered("q1b", "own-q1", T(15, day=5), 150),
             _answered("q1c", "own-q1", T(15, day=9), 180), status_ev("q1", "r-q1", now - timedelta(hours=1), "Warm Lead", "Ghosting Lead"),
             _answered("q2a", "own-q2", T(15, day=8), 140), status_ev("q2", "r-q2", now - timedelta(hours=2), "under_contract", "Warm Lead"),
             _answered("late", "own-s8", T(22, 30), 200)]     # inside the settle window: next evening's
    activity._store(rows)
    for k, text in S.items():
        if k in ("s11", "s12", "x1", "x2", "z1", "late"):      # these have no transcript on hand at the start
            continue
        f = leadnotes.transcript_file("c-%s" % k)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    call_coaching.keep({"call_id": "c-s12", "status": "graded", "transcript": S["s12"]}, 0)
    crm = FakeCRM(_screen_recs())

    def go(sid, now_, mode="live", **over):
        return sweep.run(sid, crm=crm, now=now_, pol=policy.parse(playbook(sid, "callscreen", mode, **over), EMPLOYEES),
                         sources={}, scorer=False)

    # ------------------------------------------------------------------ plan mode
    print("  the call screen in plan mode")
    plan = go("cs_plan", now, mode="plan", daily_usd=0.50)
    check(not LOG["fetch"] and not LOG["transcribe"] and not LOG["ask"],
          "a plan run opens no recording, transcribes nothing and asks no model")
    check(ledger.spent() == 0 and store.spent() == 0 and plan.usd == 0 and not crm.writes(),
          "a plan run spends nothing and writes nothing")
    check(plan.head == "Would screen 14 calls since Sun Nov 15, 4:45 PM: 13 transcripts on hand, 1 to transcribe, about $0.04.",
          "a plan run says how many calls it would screen, the transcripts on hand and what the rest would cost: %r" % plan.head)
    check(plan.tally.get("skipped: a company or trust owns it (people only)") == 1, "a call on a company's house is not screened")
    check(any(x.startswith("2 calls followed by Not Interested or Dead Lead") and "36 Pause Dr, Knoxville; 37 Firm St" in x
              for x in plan.lines)
          and any(x.startswith("2 leads went quiet or fell apart since the last run") for x in plan.lines),
          "a plan run names the calls a no followed and the leads that went quiet: %s" % plan.lines)
    check(cs.flags(live=False) == [], "a plan run raises no flag: it never read a call")
    again = go("cs_plan", now, mode="plan", daily_usd=0.50)
    check(again.head == "" and sweep.posts({"id": 3, "workflow": "cs_plan", "source": "schedule"}, again.as_dict()) == [],
          "a second plan run counts nothing twice, and a report with nothing in it posts nothing")

    # ------------------------------------------------------------------ live
    print("  the call screen, live")
    config.MODE = "live"
    live = go("cs_live", now, daily_usd=0.50)
    asked = {x[0] for x in LOG["ask"]}
    check(LOG["transcribe"] == ["c-s11"] and LOG["fetch"] == ["c-s11"],
          "only the call nobody transcribed is bought; lead notes' and call coaching's transcripts are read as they are")
    check(leadnotes.transcript_file("c-s11").is_file(), "a transcript the screen bought is kept in lead notes' store: paid once")
    check(asked == {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s11", "s12", "q1b", "q1c", "q2a"},
          "every settled call of a minute or more is screened, and a quiet lead's last two calls: %s" % sorted(asked))
    check("s10" not in asked and "q1a" not in asked and "late" not in asked,
          "a call under a minute, a quiet lead's older calls and a call still settling are not sent")
    check(all(x[1] == "claude-haiku-5-5" for x in LOG["ask"]), "the screen comes from Claude Haiku 5.5")
    first = {x[0]: x[2] for x in LOG["ask"]}
    check(first["s8"] == "Checks for this call: price, soft_yes." and first["s9"] == "Checks for this call: price."
          and first["s6"] == "Checks for this call: price, soft_yes, no_refusal."
          and first["q1b"] == "Checks for this call: missed_objection.",
          "the model is asked only what applies: the refusal check only after a no, only the price on a voicemail, "
          "the objection only on a quiet lead's calls")
    got = {(r["call_id"], r["kind"]) for r in cs.flags(live=True)}
    check(got == {("c-s1", "price"), ("c-s3", "soft_yes"), ("c-s6", "no_refusal"), ("c-s9", "price"), ("c-s12", "soft_yes"),
                  ("c-q1b", "missed_objection"), ("c-q2a", "missed_objection")},
          "the flags that hold: each quoted from the call in the right speaker's words: %s" % sorted(got))
    t = live.tally
    check(t.get("findings dropped (soft yes with no next step): the quote is not in the call") == 1,
          "a quote that is not in the call (even one from the transcriber's summary) is dropped")
    check(t.get("findings dropped (price on the phone): the quote is the other speaker's") == 1,
          "a seller naming their own price is not a price given on the phone")
    check(t.get("findings dropped (no that was not a refusal): the quote is a refusal") == 1,
          "a no that was a real refusal is never flagged as one")
    check(t.get("soft yeses that were followed up (an open task)") == 1 and t.get("soft yeses that were followed up (the owner is a lead)") == 1,
          "a soft yes followed by a task or a lead status is not flagged: the code reads what followed, not the model")
    tasks = [b for b in crm.bodies if b[0] == "POST" and b[1] == "/api/internal/task/"]
    check(len(tasks) == 1 and tasks[0][2]["assigned_to_property"] == "r-s3" and tasks[0][2]["assigned_to_users"] == ["u-emerson"]
          and tasks[0][2]["title"] == "Call back: the seller sounded open on the Nov 16 call",
          "a soft yes nothing followed gets one follow-up task for the record's assignee")
    check(any(x[0] == "GET" and x[1].startswith("/api/internal/task/") for x in crm.paths),
          "the first follow-up task is read back")
    check(any(p.startswith("A soft yes on 42 Coach Ct, Knoxville has no one assigned") for p in live.problems),
          "a soft yes with nobody assigned to the record is named instead of given to nobody")
    check(not [x for x in crm.paths if any(w in x[1] for w in FORBIDDEN)],
          "the call screen never texts, calls, or changes a status or a counter: %s" % [x for x in crm.paths][:4])
    check(live.head == "Screened 14 calls since Sun Nov 15, 4:45 PM: 7 flagged (2 price on the phone, 2 soft yes with no next step, "
          "1 no that was not a refusal, 2 objection nobody asked about).", "the head counts the flags by kind: %r" % live.head)
    text = "\n".join(live.lines)
    check('31 Price Rd, Knoxville (Mon Nov 16, 9:00 AM, Emerson): a price on the phone: "we could probably do around 120 thousand for it"' in live.lines,
          "each flag names the street, the call and the quote: %s" % live.lines[:2])
    check("A follow-up task is set for Emerson" in text and "another person who has a say nobody asked about" in text
          and "trust in us nobody asked about" in text and "marked Not Interested 3 min after the call, but the seller said" in text,
          "the lines say what each flag is in plain words")
    check("865-000-0123" not in text and "[number]" in text and "Robin" not in text and "[name]" in text,
          "no phone number and no owner name reaches the channel")
    check(HOST not in text and ".mp3" not in text and EM not in text and EN not in text,
          "no recording link and no long dash in the report")
    check(live.usd > 0 and ledger.spent(stage="cs_live") > 0, "what the screen paid is booked")
    posts = sweep.posts({"id": 4, "workflow": "cs_live", "source": "schedule"}, live.as_dict())
    check([p["kind"] for p in posts].count("chat") == 1 and "issues" in [p["kind"] for p in posts],
          "the report goes to the team channel for the owner; a problem goes to the issues channel")
    n_ask, n_tasks = len(LOG["ask"]), len(tasks)
    second = go("cs_live", now + timedelta(minutes=30), daily_usd=0.50)
    check(len(LOG["ask"]) == n_ask and len([b for b in crm.bodies if b[1] == "/api/internal/task/"]) == n_tasks
          and second.head == "", "a call is screened once, and its follow-up task is made once")

    # ------------------------------------------------------------------ caps, keys and the real call
    print("  the call screen's caps and keys")
    activity._store([_answered("x1", "own-x1", T(14, day=17), 120), _answered("x2", "own-x2", T(14, 30, day=17), 120)])
    later = T(23, 45, day=17)
    leadnotes.transcript_file("c-late").write_text(S["late"], encoding="utf-8")   # yesterday's late call, written up since
    capped = go("cs_cap", later, max_transcriptions=1)
    check(LOG["transcribe"][-1:] == ["c-x1"] and "c-x2" not in LOG["transcribe"] and "late" in {x[0] for x in LOG["ask"]}
          and capped.head == "Screened 2 calls since Mon Nov 16, 4:45 PM: nothing flagged. 1 call waits for the next run."
          and any("held by the run's transcription cap" in x for x in capped.lines),
          "past the run's transcription cap a call waits for the next run; a call that settled after yesterday's run is "
          "screened today: %r" % capped.head)
    go("cs_cap", later + timedelta(hours=1), max_transcriptions=1)
    check("c-x2" in LOG["transcribe"], "the call held by the cap is screened the next run")
    activity._store([_answered("y1", "own-x1", T(14, day=18), 120)])
    leadnotes.transcript_file("c-y1").write_text(S["x1"], encoding="utf-8")
    n_ask = len(LOG["ask"])
    broke = go("cs_broke", T(23, 45, day=18), daily_usd=0.0)
    check(len(LOG["ask"]) == n_ask and any("stage cap" in x for x in broke.lines),
          "past the stage's own dollars a day nothing is screened, and the call waits")
    cs.ASK, key = None, config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = ""
    try:
        nokey = go("cs_nokey", T(23, 45, day=18))
    finally:
        cs.ASK, config.ANTHROPIC_API_KEY = _ask, key
    check(any(p.startswith("The call screen needs ANTHROPIC_API_KEY on the brain") for p in nokey.problems)
          and len(LOG["ask"]) == n_ask, "with no Claude key the screen says so and screens nothing")
    # No OpenRouter key: a call nobody transcribed waits (it is not skipped for good) and is screened once the key is
    # there. keys_missing is stood in for, so the test never reads the environment or a .env file.
    activity._store([_answered("z1", "own-x1", T(14, day=19), 120)])
    real_keys = cs.keys_missing
    cs.keys_missing = lambda: {"screen": "", "audio": "OPENROUTER_API_KEY"}
    try:
        noaudio = go("cs_noaudio", T(23, 45, day=19))
    finally:
        cs.keys_missing = real_keys
    check(any(p.startswith("The call screen needs OPENROUTER_API_KEY on the brain") and p.endswith("those calls wait")
              for p in noaudio.problems) and "c-z1" not in LOG["transcribe"]
          and ledger.fact("call:c-z1", cs.KEY) is None,
          "with no transcription key a call nobody transcribed waits, never skipped for good")
    go("cs_noaudio", T(23, 50, day=19))
    check("c-z1" in LOG["transcribe"] and (ledger.fact("call:c-z1", cs.KEY) or {}).get("status") == "screened",
          "once the key is there the waiting call is transcribed and screened")
    activity._store([_answered("z2", "own-x2", T(14, day=10), 120)])
    old = go("cs_old", T(23, 45, day=19), first_hours=300)
    check(old.tally.get("closed: waited more than 7 days") == 1 and "c-z2" not in LOG["transcribe"]
          and "c-z2" not in {x[0] for x in LOG["ask"]},
          "a call that waited past the age limit is closed, so the window moves on")
    import anthropic
    seen = {}

    class _Msg:
        content = [type("B", (), {"type": "text", "text": "{}"})()]
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
        stop_reason = "end_turn"

    class _Client:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kw):
            seen.update(kw)
            return _Msg()
    real, cs.ASK = anthropic.Anthropic, None
    anthropic.Anthropic = _Client
    try:
        cs._ask("system", "user", "claude-haiku-5-5", 1500)
    finally:
        anthropic.Anthropic, cs.ASK = real, _ask
    check(seen.get("thinking") == {"type": "disabled"} and seen.get("output_config") == {"effort": "low"}
          and seen.get("model") == "claude-haiku-5-5",
          "the real call asks Claude Haiku 5.5 with thinking off and effort low, as the voice rewrite does")
