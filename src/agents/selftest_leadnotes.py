"""Lead notes from the call recording (build plan 2.9; C8.21, C9.17). Zero network: calls seeded into
the brain's log copy, a fake DataSift, and stand-ins for the recording link, the transcriber and the
model (the FETCH, TRANSCRIBE and ASK hooks in autopilot/leadnotes.py); its own database and data
folder. Every name, street, number and id here is invented; numbers use the never-assigned 865-000
exchange.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

NOW1 = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)       # Monday 10:00am Eastern
NOW2 = NOW1 + timedelta(hours=1)
NOW3 = NOW1 + timedelta(hours=3)
DAY = "2026-11-16"
HOST = "rec.example.test"
ROW = ("leadnotes", "run_leadnotes", "paid", False, False)      # the registry line this section asks for
EM, EN = chr(0x2014), chr(0x2013)

# ---------------------------------------------------------------------- the calls and what was said
T = {
    "c1": ("AGENT: Hi, is this Pat? This is Emerson, calling about the house on Example Street.\n"
           "SELLER: Yes, this is Pat. [pause 2s] We are thinking about selling because we're moving closer to our daughter.\n"
           "AGENT: When would you want to be moved?\n"
           "SELLER: We need to be out by the end of January.\n"
           "AGENT: And how is the house holding up?\n"
           "SELLER: Honestly it needs a lot of work, the kitchen is original.\n"
           "AGENT: Do you have a number in mind?\n"
           "SELLER: I'd want around 150 for it.\n"
           "AGENT: Is anybody living there now?\n"
           "SELLER: I'm still living here.\n"
           "AGENT: Do you owe anything on it?\n"
           "SELLER: We owe maybe 40 thousand on it."),
    "c2": ("AGENT: Hello, this is Emerson with a question about your place on Sample Avenue.\n"
           "SELLER: Sure, I have a few minutes. My tenant moved out last month and I am tired of being a landlord.\n"
           "AGENT: Would you sell it?\n"
           "SELLER: Maybe this spring if the price is right."),
    "c10": ("VOICEMAIL: You have reached the voicemail box. Please leave a message after the tone.\n"
            "AGENT_VM: Hi, this is Emerson calling about your house on Quiet Lane, please call me back."),
    "c11": ("AGENT: Hi Pat, Emerson again, following up on the house.\n"
            "SELLER: Thanks for calling back. We talked it over and we would like to be gone by Christmas."),
    "c13": ("AGENT: Hi, this is Emerson about the house on Pair Road.\n"
            "SELLER: Right, the one my mother left me. I just want it gone before winter."),
    "w1": ("AGENT: Hi, Emerson about the house on Wait Lane.\n"
           "SELLER: We are ready to sell this month if the offer is fair."),
    "d1": ("AGENT: Hi, Emerson here about the house on Cap Street.\n"
           "SELLER: We want to sell because the stairs are too much for us now."),
    "d2": ("AGENT: Hello, this is Emerson about your place on Cap Street.\n"
           "SELLER: My brother and I inherited it and we both want out of it soon."),
    "d5": ("AGENT: Hi, Emerson here.\n"
           "SELLER: I already sold that house to my neighbor last spring."),
    "d6": ("AGENT: This is Emerson about the house on Coach Road.\n"
           "SELLER: It is empty now since my aunt passed and it needs a new roof."),
    "d9": ("AGENT: Hi, Emerson here about the house on Empty Court.\n"
           "SELLER: Can you call me back next week, I am at work right now."),
}


def _reply(answers: dict, fenced: bool = True) -> str:
    body = json.dumps({k: {"value": v, "quote": q} for k, (v, q) in answers.items()})
    return ("Here are the notes.\n```json\n%s\n```" % body) if fenced else body


R = {
    "c1": _reply({"reason": ("moving closer to their daughter", "we're moving closer to our daughter"),
                  "timeline": ("out by the end of January", "We need to be out by the end of January"),
                  "condition": ("needs a lot of work", "it needs a lot of work, the kitchen is original"),
                  "price": ("around 150", "I'd want around 150 for it"),
                  "asking_price": ("150", "I'd want around 150 for it"),
                  "occupancy": ("the owner lives there", "I'm still living here"),
                  "mortgage_balance": ("about 40 thousand", "We owe maybe 40 thousand on it"),
                  "months_behind": ("3 months", "we are three months behind"),       # not in the call
                  "other_offers": ("two other offers", ""),                           # no quote
                  "back_taxes": ("$2,500", "We owe maybe 40 thousand on it"),         # a number its quote does not hold
                  "firm": ("yes", "Yes"),                                             # one word proves nothing
                  "roof": ("Unknown", "")}),
    "c2": _reply({"reason": ("tired of being a landlord", "I am tired of being a landlord"),
                  "timeline": ("this spring", "Maybe this spring if the price is right"),
                  "occupancy": ("vacant, the tenant moved out", "My tenant moved out last month")}, fenced=False),
    "c11": _reply({"timeline": ("by Christmas", "we would like to be gone by Christmas")}),
    "c13": _reply({"reason": ("inherited from their mother", "the one my mother left me"),
                   "timeline": ("before winter", "I just want it gone before winter")}),
    "w1": _reply({"timeline": ("this month", "We are ready to sell this month")}),
    "d1": _reply({"reason": ("the stairs are too much", "the stairs are too much for us now")}),
    "d2": _reply({"reason": ("inherited with a brother", "My brother and I inherited it")}),
    "d5": "I could not find anything useful in this call.",
    "d6": _reply({"occupancy": ("empty", "It is empty now since my aunt passed"),
                  "condition": ("needs a new roof", "it needs a new roof")}),
    "d9": _reply({"reason": ("wants to move", "we have to move for my job"),          # never said
                  "timeline": ("next week", "")}),                                     # no quote
}
LOG: dict = {"fetch": [], "transcribe": [], "ask": []}


def _stem(url: str) -> str:
    return str(url).rsplit("/", 1)[-1].split(".")[0]


def _fetch(url, max_bytes):
    LOG["fetch"].append(_stem(url))
    stem = _stem(url)
    if stem.startswith("login-"):
        return {"ok": False, "code": "login", "host": HOST, "why": "needs a login"}
    if stem.startswith("gone-"):
        return {"ok": False, "code": "gone", "host": HOST, "why": "gone"}
    return {"ok": True, "code": "ok", "fmt": "mp3", "audio": stem.encode("utf-8"), "host": HOST, "size": 10}


def _transcribe(audio, fmt, caller):
    stem = audio.decode("utf-8")
    LOG["transcribe"].append(stem)
    return T[stem], {"cost": 0.004}


def _ask(system, user, model, max_tokens):
    found = next((k for k, t in T.items() if t in user), "")
    LOG["ask"].append((found, model, system))
    return R.get(found, "nothing"), {"input_tokens": 10000, "output_tokens": 2000}, "end_turn"


def _no_openrouter(body, timeout):
    raise AssertionError("the lead notes selftest tried to reach OpenRouter")


def _n(kind: str, stem: str) -> int:
    return sum(1 for x in LOG[kind] if (x[0] if isinstance(x, tuple) else x) == stem)


# ---------------------------------------------------------------------- the account
def _rec(u, street, owner, status=None, first="Pat", last="Example", company=None, tags=()):
    return {"uuid": u, "status": status, "tags": [{"title": t} for t in tags], "lists": [],
            "address": {"street": street, "city": "Knoxville", "state": "TN"},
            "owner": {"uuid": owner, "first_name": first, "last_name": last, "company": company, "phones": [],
                      "deceased": False}}


class FakeCRM:
    def __init__(self, recs, labels=None, values=None):
        self.recs = recs
        self.labels = dict(labels or {"Timeline": "cf-timeline"})          # label -> field uuid
        self.values = {u: dict(v) for u, v in (values or {}).items()}     # record -> {label: value}
        self.calls, self.reads, self.refuse = [], 0, set()

    def owner_records(self, owner, cap=250):
        self.reads += 1
        rows = [r for r in self.recs.values() if (r.get("owner") or {}).get("uuid") == owner]
        return [{"uuid": r["uuid"], "status": r.get("status"),
                 "owner": {k: r["owner"].get(k) for k in ("uuid", "first_name", "last_name", "company")}}
                for r in rows][:cap]

    def record(self, u):
        self.reads += 1
        return copy.deepcopy(self.recs[u])

    def custom_fields(self, u):
        self.reads += 1
        return dict(self.values.get(u, {}))

    def custom_field_uuid(self, label):
        return next((fu for lab, fu in self.labels.items() if lab.lower() == label.strip().lower()), "")

    def set_custom_field(self, u, fu, value):
        if fu in self.refuse:
            raise RuntimeError("HTTP 400: not a valid UUID")
        self.calls.append(("field", u, fu, value))
        label = next(lab for lab, x in self.labels.items() if x == fu)
        self.values.setdefault(u, {})[label] = value

    def message(self, u, text, pin=False):
        self.calls.append(("note", u, text))
        return ""

    def notes(self, u=None):
        return [x[2] for x in self.calls if x[0] == "note" and (u is None or x[1] == u)]

    def fields(self):
        return [x[1:] for x in self.calls if x[0] == "field"]


def _recs() -> dict:
    out = {}
    for r in (_rec("r-lead", "11 Example St", "own-lead", "Warm Lead"),
              _rec("r-long", "22 Sample Ave", "own-long", first="Robin", last="Sample"),
              _rec("r-prospect", "5 Prospect Way", "own-prospect"),
              _rec("r-llc", "7 Holding Ct", "own-llc", "Warm Lead", first=None, last=None, company="Example Holdings LLC"),
              _rec("r-dnm", "9 Quiet Pl", "own-dnm", "Warm Lead", tags=["Do Not Market"]),
              _rec("r-norec", "2 Silent Rd", "own-norec", "Warm Lead"),
              _rec("r-tiny", "4 Brief Ct", "own-tiny", "Warm Lead"),
              _rec("r-two-a", "3 Twin Rd", "own-two"), _rec("r-two-b", "5 Twin Rd", "own-two"),
              _rec("r-login", "13 Lock St", "own-login", "Warm Lead"),
              _rec("r-vm", "33 Quiet Ln", "own-vm", "Warm Lead"),
              _rec("r-pair-a", "44 Pair Rd", "own-pair", "Warm Lead", first="Casey", last="Pair"),
              _rec("r-pair-b", "46 Pair Rd", "own-pair", first="Casey", last="Pair"),
              _rec("r-wait", "8 Wait Ln", "own-wait", "Warm Lead"),
              _rec("r-cap1", "1 Cap St", "own-cap1", "Warm Lead"), _rec("r-cap2", "2 Cap St", "own-cap2", "Warm Lead"),
              _rec("r-cap3", "3 Cap St", "own-cap3", "Warm Lead"), _rec("r-gone", "6 Gone St", "own-gone", "Warm Lead"),
              _rec("r-age", "7 Old St", "own-age", "Warm Lead"), _rec("r-bad", "8 Bad St", "own-bad", "Warm Lead"),
              _rec("r-coach", "9 Coach Rd", "own-coach", "Warm Lead"), _rec("r-keys", "10 Key St", "own-keys", "Warm Lead"),
              _rec("r-none", "12 Empty Ct", "own-none", "Warm Lead")):
        out[r["uuid"]] = r
    return out


def _call(cid, owner, ts, seconds, rec=None, direction="outbound", caller="Emerson"):
    url = "" if rec == "" else "https://%s/%s.mp3" % (HOST, rec or cid)
    return {"id": "ev-" + cid, "event_type": "owner.call.answered", "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": ts,
            "payload": {"call": {"direction": direction, "origin_number": "8650000099",
                                 "destination_number": "8650000001", "duration": seconds, "recording": url,
                                 "uuid": cid, "source": "kixie", "user": {"first_name": caller, "uuid": "u-emerson"}}}}


def _status(i, rec, ts, old, new):
    return {"id": "st-" + i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": "internal-api", "author": "person@example.com", "timestamp": ts,
            "payload": {"property": {"status": [old, new]}}}


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def playbook(sid: str, mode: str = "plan", **over) -> dict:
    stage = {"kind": "leadnotes", "owner": "sales", "mode": mode, "schedule": ["every 30 min 08:00-20:00 Mon-Sat"],
             "sync": False, "read_fields": False}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]}, "stages": {sid: stage}}


# The block this build proposes for team/autopilot.yaml, as a mapping (it must parse clean).
SHIPPED = {"kind": "leadnotes", "owner": "sales", "mode": "plan", "schedule": ["every 30 min 08:00-20:00 Mon-Sat"],
           "summary": "x", "read_fields": False, "daily_usd": 2.00, "min_seconds": 90, "prospect_seconds": 180,
           "call_events": ["owner.call.answered"], "first_days": 2, "settle_minutes": 15, "max_age_days": 7,
           "max_transcriptions": 12, "max_model_calls": 12, "max_reads": 60, "max_minutes": 30,
           "audio_per_min": 0.003, "extract_usd": 0.003, "model": "claude-haiku-5-5", "max_tries": 3,
           "recording_grace_hours": 24, "keep_days": 60, "keep_calls": 20,
           "fields": {"reason": "Motivation", "timeline": "Timeline", "condition": "Condition",
                      "asking_price": "Asking Price", "occupancy": "Occupancy", "mortgage_balance": "Mortgage Balance",
                      "months_behind": "Months Behind"},
           "overwrite_fields": False, "handoff_statuses": ["Hot Lead", "underwrite"], "handoff_delay_minutes": 45,
           "handoff_wait_hours": 24, "sync": True, "sync_calls": 60}


def _register():
    """Until the orchestrator adds the registry line, this section registers the kind for itself and
    takes it out again afterwards. Once the line is in registry.py this does nothing."""
    from .autopilot import policy, registry
    if "leadnotes" in registry.EXTRA:
        return lambda: None
    registry.EXTRA["leadnotes"] = ROW
    kinds, no_presets = policy.KINDS, policy.NO_PRESETS
    policy.KINDS = kinds + ("leadnotes",)
    policy.NO_PRESETS = no_presets + ("leadnotes",)
    policy.SPEND_BY_KIND["leadnotes"] = "paid"

    def undo():
        registry.EXTRA.pop("leadnotes", None)
        policy.KINDS, policy.NO_PRESETS = kinds, no_presets
        policy.SPEND_BY_KIND.pop("leadnotes", None)
    return undo


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import leadnotes
    from call_coaching import transcribe as tr
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY)
    hooks = (leadnotes.FETCH, leadnotes.TRANSCRIBE, leadnotes.ASK)
    post = tr.POST
    config.DATA_DIR = Path(tmp) / "leadnotes"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    undo_reg = _register()
    tr.POST = _no_openrouter
    leadnotes.FETCH, leadnotes.TRANSCRIBE, leadnotes.ASK = _fetch, _transcribe, _ask
    for k in LOG:
        LOG[k].clear()
    try:
        _pure(check)
        _runs(check)
    finally:
        leadnotes.FETCH, leadnotes.TRANSCRIBE, leadnotes.ASK = hooks
        tr.POST = post
        undo_reg()
        config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY = keep


# ---------------------------------------------------------------------- the rules on their own
def _pure(check) -> None:
    from .autopilot import leadnotes as ln
    print("  what the call said")
    check(ln.normalize("SELLER: I'd want [pause 2s] around 150, maybe.") == "i'd want around 150 maybe",
          "a quote is read as words only: no speaker label, no delivery note, no punctuation")
    hay = ln.normalize(T["c1"])
    check(ln.said("I'd want around 150 for it", hay) and not ln.said("we are three months behind", hay),
          "a quote counts only when its words are in the call")
    check(not ln.said("Yes", hay) and not ln.said("", hay), "a one-word quote proves nothing and an empty one less")
    check(ln.said("We need to be out... end of January", hay) and not ln.said("We need to be out... end of March", hay),
          "an ellipsis joins pieces of the call, and every piece must be in it")
    check(not ln.said("the house on Example Stree", hay), "a quote must match whole words, not part of one")
    check(ln.numbers_hold("150", "I'd want around 150 for it") and ln.numbers_hold("$150,000", "I'd want around 150 for it")
          and ln.numbers_hold("about 40 thousand", "We owe maybe 40 thousand on it"),
          "a number in the answer agrees with the same number in its quote, written out or not")
    check(not ln.numbers_hold("$2,500", "We owe maybe 40 thousand on it")
          and not ln.numbers_hold("3 months", "we are a few months behind"),
          "a number its quote does not hold is refused (worked out or made up)")
    check(ln.numbers_hold("15 years", "the roof is about fifteen years old") and ln.numbers_hold("2019", "we redid it in 2019"),
          "a number the seller said in words, or a year, is held by its quote")
    check(ln.is_unknown("Not mentioned") and ln.is_unknown("unknown.") and not ln.is_unknown("none"),
          "Unknown in its many spellings is Unknown, and none (no HOA, no back taxes) is a real answer")
    check(ln.tidy("well" + EM + "maybe") == "well, maybe" and EN not in ln.tidy("4" + EN + "6"),
          "no long dash and no en dash reaches a note")
    raw = ln.parse_reply(R["c1"])
    check(isinstance(raw, dict) and ln.parse_reply("I could not find anything useful.") is None
          and isinstance(ln.parse_reply(R["c2"]), dict),
          "the answers are read out of a fenced or a bare JSON reply, and prose with no answers is no answers")
    keep, dropped = ln.validate(raw, T["c1"])
    check(set(keep) == {"reason", "timeline", "condition", "price", "asking_price", "occupancy", "mortgage_balance"},
          "every answer whose quote is in the call is kept")
    check(dropped == {"months_behind": "the quote is not in the call", "other_offers": "no quote",
                      "back_taxes": "a number its quote does not hold", "firm": "the quote is not in the call"},
          "an answer with no quote, a quote not in the call, a one-word quote or a number the quote does not hold "
          "is Unknown, with the reason")
    check("roof" not in keep and "roof" not in dropped, "an answer the model left Unknown stays Unknown")
    check("copied word for word" in ln.SYSTEM and all(('- %s:' % k) in ln.SYSTEM for k in ln.KEYS),
          "the model is told the quote rule and every item of the one fixed schema")
    print("  the handoff note")
    calls = [{"call": "a", "at": "2026-11-14T15:30:00+00:00", "caller": "Emerson",
              "answers": {"reason": {"value": "moving", "quote": "we are moving away"},
                          "timeline": {"value": "January", "quote": "out by January please"}}},
             {"call": "b", "at": "2026-11-16T14:55:00+00:00", "caller": "Emerson",
              "answers": {"timeline": {"value": "by Christmas", "quote": "gone by Christmas now"}}}]
    s = ln.summary_text(calls, "Hot Lead", "America/New_York")
    check(s.startswith("What we know before the handoff to Hot Lead, from 2 calls between Nov 14 and Nov 16")
          and 'Timeline: by Christmas ("gone by Christmas now", Nov 16)' in s and 'Reason: moving ("we are moving away", Nov 14)' in s,
          "the What we know note merges every call, the newest answer to each item winning, each with its day")
    check("Condition: Unknown" in s and "Price: Unknown" in s and "do not ask them again" in s,
          "the pillars nobody answered say Unknown, and the note says to confirm rather than ask again")


# ---------------------------------------------------------------------- the stage, run by run
def _runs(check) -> None:
    from . import store
    from .autopilot import activity, leadnotes as ln, ledger, policy, sweep, undo
    activity._store([
        _call("c1", "own-lead", "2026-11-14 15:30:00", 170),
        _call("c2", "own-long", "2026-11-16 12:00:00", 240),
        _call("c3", "own-prospect", "2026-11-16 12:30:00", 120),
        _call("c4", "own-llc", "2026-11-16 11:00:00", 200),
        _call("c5", "own-dnm", "2026-11-16 11:30:00", 200),
        _call("c6", "own-norec", "2026-11-16 10:00:00", 300, rec=""),
        _call("c7", "own-tiny", "2026-11-16 10:30:00", 40),
        _call("c8", "own-two", "2026-11-16 09:00:00", 300),
        _call("c9", "own-login", "2026-11-16 09:30:00", 200, rec="login-c9"),
        _call("c10", "own-vm", "2026-11-16 08:00:00", 120),
        _call("c11", "own-lead", "2026-11-16 14:55:00", 130),
        _call("c13", "own-pair", "2026-11-16 13:00:00", 150),
    ])
    # r-long's Timeline was typed by a person; r-pair-a's holds the Unknown the encumbrance stage writes.
    crm = FakeCRM(_recs(), values={"r-long": {"Timeline": "spring, per the owner"}, "r-pair-a": {"Timeline": "Unknown"}})

    def go(sid, crm_, now, mode="live", **over):
        return sweep.run(sid, crm=crm_, now=now, pol=policy.parse(playbook(sid, mode, **over), EMPLOYEES),
                         sources={}, scorer=False)

    def slack(out) -> str:
        return "\n".join([out.head] + list(out.lines) + list(out.problems))

    # ------------------------------------------------------------------ plan mode
    print("  a plan run")
    plan = go("ln_plan", crm, NOW1, mode="plan")
    check(not LOG["fetch"] and not LOG["transcribe"] and not LOG["ask"],
          "a plan run opens no recording, transcribes nothing and asks no model")
    check(not crm.calls and ledger.spent() == 0 and store.spent() == 0 and plan.usd == 0,
          "a plan run writes nothing to the account and spends nothing")
    check(plan.head == "Would transcribe 5 calls on 5 houses (about $0.06) and write a lead note on each.",
          "a plan run says how many calls it would transcribe and what that would cost: %r" % plan.head)
    check(plan.would_usd > 0.05 and plan.acted == 5, "the cost it would spend is reserved in the plan, not paid")
    check(plan.tally.get("skipped: not a lead, and no call of 3 min or more") == 1,
          "a short call on a prospect with no lead status and no real conversation is left alone")
    check(plan.tally.get("skipped: a company or trust owns it (people only)") == 1,
          "a house a company owns is never noted (people only)")
    check(plan.tally.get("held back: tagged Do Not Market") == 1, "the gate runs before any call is read for notes")
    check(plan.tally.get("skipped: the owner holds several houses and the call does not say which one") == 1,
          "an owner with several houses, none of them a lead, is held: a note on the wrong house is worse than none")
    missing = [p for p in plan.problems if p.startswith("Custom fields for the call answers do not exist yet")]
    check(missing and "Motivation, Condition, Asking Price, Occupancy, Mortgage Balance, Months Behind" in missing[0]
          and "Timeline" not in missing[0],
          "the fields that do not exist are named once, so they can be created; Timeline exists and is not named")
    posts = sweep.posts({"id": 1, "workflow": "ln_plan", "source": "schedule"}, plan.as_dict())
    check([p["kind"] for p in posts] == ["issues"], "in plan mode only the problem posts, to the issues channel")
    again = go("ln_plan", crm, NOW1, mode="plan")
    check(again.head == "" and again.acted == 0
          and sweep.posts({"id": 2, "workflow": "ln_plan", "source": "schedule"}, again.as_dict()) == [],
          "a second plan run counts nothing twice and posts nothing")

    # ------------------------------------------------------------------ live
    print("  a live run")
    config.MODE = "live"
    live = go("ln_live", crm, NOW1)
    check(ln.mark("call:c1", False).get("status") == "noted" and ln.mark("call:c1", True).get("status") == "noted",
          "plan marks are their own: a call a plan run counted is still noted by the live run")
    check(sorted(LOG["transcribe"]) == ["c1", "c10", "c13", "c2"] and sorted(x[0] for x in LOG["ask"]) == ["c1", "c13", "c2"],
          "each qualifying call is transcribed once; a voicemail goes to no model")
    check(all(x[1] == "claude-haiku-5-5" for x in LOG["ask"]), "the notes come from Claude Haiku 5.5")
    check(sorted(x[1] for x in crm.calls if x[0] == "note") == ["r-lead", "r-long", "r-pair-a"],
          "one note per call on the house the call was about, and none for a voicemail or a recording that would not open")
    check(crm.notes("r-pair-a") and not crm.notes("r-pair-b"),
          "an owner with two houses, one of them a lead, gets the note on the lead")
    note = crm.notes("r-lead")[0]
    lines = note.split("\n")
    check(lines[0] == "Lead notes from the call on Sat Nov 14, 2026 at 10:30 AM (Emerson called, 3 min).",
          "the note is headed by the call's date and who called: %r" % lines[0])
    check([ln_.split(":")[0] for ln_ in lines[1:5]] == ["Reason", "Timeline", "Condition", "Price"]
          and lines[1] == 'Reason: moving closer to their daughter ("we\'re moving closer to our daughter")',
          "the four pillars come first, in one order, each with the seller's own words")
    check("Asking price: 150" in lines and 'Occupancy: the owner lives there ("I\'m still living here")' in lines,
          "the intake answers known follow, and a quote already printed is not printed twice")
    check("three months" not in note and "2,500" not in note and "two other offers" not in note
          and not any(x.startswith("Firm on price:") for x in lines),
          "an answer that failed the quote rule never reaches the record")
    check(any(x.startswith("Not covered on this call: firm on price, other offers") for x in lines)
          and lines[-1] == ln.FOOTER, "what the call did not cover is listed, and the note says where it came from")
    check(EM not in note and EN not in note, "no long dash or en dash in a note")
    long_note = crm.notes("r-long")[0]
    check("Condition: Unknown" in long_note and "Price: Unknown" in long_note,
          "a pillar the call did not answer says Unknown, never a guess")
    check(("r-lead", "cf-timeline", "out by the end of January") in crm.fields()
          and ("r-pair-a", "cf-timeline", "before winter") in crm.fields(),
          "the Timeline field, which exists, is filled from the call")
    check(not [f for f in crm.fields() if f[0] == "r-long"]
          and live.tally.get("kept a value a person typed in Timeline") == 1,
          "a value a person typed in a field is never overwritten")
    pa = [x for x in ledger.changes() if x["what"] == "custom_field" and x["record_uuid"] == "r-pair-a"]
    check(pa and pa[0]["before"] == {"field_uuid": "cf-timeline", "value": "Unknown"},
          "a field holding the placeholder Unknown takes the call's answer, and Unknown is kept for undo")
    ch = [x for x in ledger.changes() if x["what"] == "custom_field" and x["record_uuid"] == "r-lead"]
    check(ch and ch[0]["before"] == {"field_uuid": "cf-timeline", "value": ""},
          "every field write keeps the value it replaced")
    steps = undo.plan(ch[0]["run_id"]) if ch else []
    check(any(s["do"] == "custom_field" and s["args"] == {"field_uuid": "cf-timeline", "value": ""} for s in steps),
          "undo puts the field back as it was")
    check(abs(ledger.spent(DAY, "ln_live") - 0.022) < 1e-6,
          "what was billed is booked against the stage's day: four transcriptions and three notes (%r)"
          % ledger.spent(DAY, "ln_live"))
    login = [p for p in live.problems if p.startswith("Some call recordings could not be opened")]
    check(login and "needs a login: 1 (%s)" % HOST in login[0] and "c9" not in LOG["transcribe"],
          "a recording behind a login is a problem in plain words, never a crash and never a guess")
    check(live.tally.get("closed: no seller on the recording (a voicemail or a greeting)") == 1,
          "a recording with no seller on it is closed without paying for notes")
    check(live.head == "Wrote lead notes from 3 calls on 3 houses.", "the head says what was done: %r" % live.head)
    f = ln.transcript_file("c1")
    check(f.is_file() and str(Path(config.DATA_DIR)) in str(f), "the transcript is kept in the brain's data folder only")
    said = slack(live) + "\n".join(p["text"] for p in sweep.posts({"id": 3, "workflow": "ln_live", "source": "schedule"},
                                                                   live.as_dict()))
    check(not any(w in said for w in ("daughter", "January", "150", "40 thousand", "8650000001", "login-c9", ".mp3")),
          "a Slack line carries counts and streets only: no quote, no phone number, no recording link")
    check("11 Example St, Knoxville: 7 answers, 4 of the 4 pillars" in live.lines, "each house noted is listed with its count")
    before = (len(LOG["fetch"]), len(LOG["transcribe"]), len(LOG["ask"]), len(crm.notes()))
    go("ln_live", crm, NOW1)
    check((len(LOG["fetch"]), len(LOG["transcribe"]), len(LOG["ask"]), len(crm.notes())) == before,
          "a call is never transcribed or noted twice")

    # ------------------------------------------------------------------ the handoff
    print("  the handoff note")
    activity._store([_status("h1", "r-lead", "2026-11-16 15:00:00", "Warm Lead", "Hot Lead"),
                     _status("h2", "r-lead", "2026-11-16 15:05:00", "Hot Lead", "underwrite")])
    crm.recs["r-lead"]["status"] = "underwrite"
    hand = go("ln_live", crm, NOW2)
    lead_notes = crm.notes("r-lead")
    summaries = [x for x in lead_notes if x.startswith("What we know")]
    check(len(lead_notes) == 3 and len(summaries) == 1,
          "the newer call gets its own note, and the move to Hot Lead one What we know note")
    s = summaries[0] if summaries else ""
    check(s.startswith("What we know before the handoff to Hot Lead, from 2 calls between Nov 14 and Nov 16")
          and 'Timeline: by Christmas ("we would like to be gone by Christmas", Nov 16)' in s
          and 'Reason: moving closer to their daughter ("we\'re moving closer to our daughter", Nov 14)' in s,
          "the What we know note merges both calls, the newest answer winning, each with its day")
    check(hand.tally.get("skipped: no new call notes since the last What we know note") == 1,
          "a second handoff with nothing new since the last What we know note writes no second one")
    check(("r-lead", "cf-timeline", "by Christmas") in crm.fields(),
          "a field this stage filled takes the newer call's answer")
    check(hand.head == "Wrote lead notes from 1 call on 1 house; 1 What we know note before a handoff.",
          "the head counts the call note and the handoff note: %r" % hand.head)
    n = len(crm.notes())
    go("ln_live", crm, NOW2)
    check(len(crm.notes()) == n, "a handoff is summarized once")

    print("  a handoff waits for its call")
    activity._store([_call("w1", "own-wait", _ts(NOW2 - timedelta(hours=2)), 150),
                     _status("hw", "r-wait", _ts(NOW2 - timedelta(hours=1)), "Warm Lead", "Hot Lead")])
    held = go("ln_wait", crm, NOW2, max_transcriptions=0)
    check(not crm.notes("r-wait") and "1 What we know note waits for a call still being noted" in held.head,
          "a What we know note waits while the call that led to the handoff is still to be noted")
    go("ln_wait", crm, NOW2)
    w = crm.notes("r-wait")
    check(len(w) == 2 and w[1].startswith("What we know before the handoff to Hot Lead, from 1 call on Nov 16"),
          "once the call is noted, the What we know note follows")

    # ------------------------------------------------------------------ caps and retries
    print("  caps and retries")
    off = {"handoff_statuses": []}
    activity.reset(["calls"])
    activity._store([_call("d1", "own-cap1", _ts(NOW3 - timedelta(hours=2)), 120),
                     _call("d2", "own-cap2", _ts(NOW3 - timedelta(minutes=90)), 120)])
    first = go("ln_caps", crm, NOW3, max_transcriptions=1, **off)
    check(crm.notes("r-cap1") and not crm.notes("r-cap2") and "1 call waits for the next run" in first.head
          and "Waiting: 1 call, held by the run's transcription cap." in first.lines,
          "no more transcriptions in a run than its cap; the rest wait and say why")
    go("ln_caps", crm, NOW3, max_transcriptions=1, **off)
    check(crm.notes("r-cap2"), "a call held by the cap is noted on the next run")

    activity.reset(["calls"])
    activity._store([_call("d3", "own-cap3", _ts(NOW3 - timedelta(hours=1)), 120)])
    tight = go("ln_budget", crm, NOW3, daily_usd=0.0001, **off)
    check(not _n("fetch", "d3") and not crm.notes("r-cap3") and any("stage cap" in x for x in tight.lines),
          "nothing is paid for past the stage's daily cap; the call waits")

    activity.reset(["calls"])
    activity._store([_call("d4", "own-gone", _ts(NOW3 - timedelta(hours=1)), 120, rec="gone-d4")])
    soon = go("ln_gone", crm, NOW3, **off)
    check(_n("fetch", "gone-d4") == 1 and "1 call waits for the next run" in soon.head and not soon.problems,
          "a recording that is gone within a day of the call is tried again (the dialer may still be writing it)")
    late = go("ln_gone", crm, NOW3 + timedelta(days=2), **off)
    check(_n("fetch", "gone-d4") == 2 and any("gone: 1" in p for p in late.problems),
          "after the grace period a gone recording is closed and named as a problem")
    go("ln_gone", crm, NOW3 + timedelta(days=2), **off)
    check(_n("fetch", "gone-d4") == 2, "a closed call is not opened again")

    activity.reset(["calls"])
    activity._store([_call("d8", "own-age", _ts(NOW3 - timedelta(hours=1)), 120, rec="gone-d8")])
    go("ln_age", crm, NOW3, max_age_days=1, **off)
    old = go("ln_age", crm, NOW3 + timedelta(hours=30), max_age_days=1, **off)
    check(_n("fetch", "gone-d8") == 1 and old.tally.get("closed: waited more than 1 day") == 1,
          "a call still not done after max_age_days is closed, so the scan never grows without end")

    activity.reset(["calls"])
    activity._store([_call("d5", "own-bad", _ts(NOW3 - timedelta(hours=1)), 120)])
    go("ln_bad", crm, NOW3, max_tries=2, **off)
    check(_n("transcribe", "d5") == 1 and _n("ask", "d5") == 1 and not crm.notes("r-bad")
          and ln.mark("call:d5", True).get("status") == "open",
          "a reply with no answers writes no note and the call is tried again")
    go("ln_bad", crm, NOW3, max_tries=2, **off)
    check(_n("transcribe", "d5") == 1 and _n("ask", "d5") == 2 and ln.mark("call:d5", True).get("status") == "closed",
          "the retry reuses the transcript (no second transcription) and gives up after max_tries")

    print("  reuse, refusals and keys")
    from .workflows import call_coaching
    call_coaching.keep({"call_id": "d6", "status": "graded", "transcript": T["d6"]}, 0)
    activity.reset(["calls"])
    activity._store([_call("d6", "own-coach", _ts(NOW3 - timedelta(hours=1)), 120)])
    crm2 = FakeCRM(_recs(), labels={"Timeline": "cf-timeline", "Occupancy": "cf-occupancy"})
    crm2.refuse = {"cf-occupancy"}
    spent0 = ledger.spent(DAY, "ln_coach")
    coach = go("ln_coach", crm2, NOW3, **off)
    check(not _n("fetch", "d6") and not _n("transcribe", "d6") and _n("ask", "d6") == 1 and crm2.notes("r-coach"),
          "a transcript the coaching workflow already paid for is used as it is")
    check(abs(ledger.spent(DAY, "ln_coach") - spent0 - 0.002) < 1e-6, "only the notes are paid for on a reused transcript")
    check(any(p.startswith("DataSift refused a call answer for the Occupancy field") for p in coach.problems)
          and "Occupancy:" in crm2.notes("r-coach")[0],
          "a field DataSift refuses is a problem in plain words, and the answer is still in the note")
    miss = [p for p in coach.problems if p.startswith("Custom fields for the call answers")]
    check(miss and "Occupancy" not in miss[0], "a field that exists is not named as missing")

    activity.reset(["calls"])
    activity._store([_call("d9", "own-none", _ts(NOW3 - timedelta(hours=1)), 120)])
    none = go("ln_none", crm, NOW3, **off)
    check(_n("ask", "d9") == 1 and not crm.notes("r-none") and none.tally.get("skipped: the call gave none of the answers") == 1
          and ln.mark("call:d9", True).get("status") == "skipped",
          "a call whose every answer fails the quote rule gets no note of Unknowns, and is not paid for again")

    activity.reset(["calls"])
    activity._store([_call("d7", "own-keys", _ts(NOW3 - timedelta(hours=1)), 120)])
    ln.ASK, config.ANTHROPIC_API_KEY = None, ""
    try:
        keys = go("ln_keys", crm, NOW3, **off)
    finally:
        ln.ASK = _ask
    check(any(p.startswith("Lead notes need ANTHROPIC_API_KEY on the brain") for p in keys.problems)
          and not _n("fetch", "d7") and not crm.notes("r-keys"),
          "with no Claude key on the box, nothing is paid for: no recording is opened and the problem says which key")

    # ------------------------------------------------------------------ the playbook block
    print("  the playbook block")
    bad = playbook("ln_typo", "plan", min_seconds="ninety", prospect_seconds=30, fields={"price_per_foot": "Price"},
                   handoff_statuses="Hot Lead", call_events=["owner.call.made"], sync="yes")
    try:
        policy.parse(bad, EMPLOYEES)
        why = ""
    except policy.PolicyError as exc:
        why = str(exc)
    check("min_seconds must be a whole number" in why and "prospect_seconds" in why and "price_per_foot" in why
          and "handoff_statuses lists" in why and "call_events lists" in why and "sync is true or false" in why,
          "a typo in the block is refused when the playbook loads, never read as a default")
    shipped = {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
               "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
               "stages": {"lead_notes": dict(SHIPPED)}}
    try:
        policy.parse(shipped, EMPLOYEES)
        ok = True
    except policy.PolicyError:
        ok = False
    check(ok, "the block proposed for team/autopilot.yaml parses clean")
    from .autopilot import registry
    check(registry.problems() == [], "the stage kind loads from the registry: %s" % (registry.problems() or "ok"))
    config.MODE = "dry"
