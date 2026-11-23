"""A new hire's first week: every answered call graded each evening, the notes to the owner first
(autopilot/newhire.py). Zero network: the call log is seeded into the brain's copy; the recording
host, the transcriber, the triage and the grader are the call coaching workflow's own stand-ins;
Slack and DataSift are fakes. Every name, street, number and link here is invented, and the numbers
use the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import json
import os
import types
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .selftest_autopilot import EMPLOYEES

ET = ZoneInfo("America/New_York")
EM, EN = chr(0x2014), chr(0x2013)
STAGE = "nh_week"
REC = "https://rec.example.test/nh/%s.mp3"
LOCKED = "https://locked.example.test/nh/%s.mp3"
TEAM, THREAD = "CTEAMTEST", "1800000000.000100"

T1 = ("AGENT: Hi, is this Pat? This is Dana, I was calling about the house on Maple Lane. [warm tone]\n"
      "SELLER: Yes, this is Pat Rivers.\n"
      "AGENT: I know this is a little out of the blue, do you have any plans for it?\n"
      "SELLER: We might sell next spring.\n"
      "AGENT: So what would you take for it?\n"
      "SELLER: I have not thought about a price.\n"
      "AGENT: Is 865-000-0142 still the best number for you?\n"
      "SELLER: It is.\n"
      "AGENT: Okay, cool, I will let you go then.\n\n"
      "DELIVERY SUMMARY:\n- pace: conversational")
T7 = ("AGENT: Hello, am I speaking with Lou? It is Dana about the property on Birch Road.\n"
      "SELLER: This is Lou.\n"
      "AGENT: Is now a bad time?\n"
      "SELLER: I am driving, call me later.\n"
      "AGENT: Sure, when is better for you?\n"
      "SELLER: Friday morning.\n"
      "AGENT: Friday morning it is, talk then.")
T9 = ("AGENT: Hi, is this Kim? Dana here about the place on Oak Court.\n"
      "SELLER: Not interested, take me off your list.\n"
      "AGENT: Okay, no problem, have a good day.")
TVM = "VOICEMAIL: You have reached a voicemail box. Please leave a message."

REPORT_1 = """CALL GRADE REPORT
Caller: Dana    Date/Time: Tue Nov 17    Lead source/list: first to market
Call length: 4:00    Outcome: callback
Auto-fail check: PASS

PILLARS CAPTURED: [M: "We might sell next spring"] [T: missed] [C: missed] [P: missed]

TOP 3 STRENGTHS (quote the transcript)
1. "Hi, is this Pat? This is Dana, I was calling about the house on Maple Lane": named the owner and the street first.
2. "I know this is a little out of the blue, do you have any plans for it": named the awkwardness, then asked soft.
3. "Is 865-000-0142 still the best number for you": confirmed the number on the call.

TOP 3 IMPROVEMENT AREAS (quote, then prescribe the playbook line)
1. They said: "So what would you take for it". Use instead: "Do you have a ballpark figure in mind?". Price came before motivation.
2. They said: "Okay, cool, I will let you go then". Use instead: "Would Thursday or Friday work for a short call with my partner?". No next step was booked.
3. They said: "I know this is a little out of the blue, do you have any plans for it". Use instead: "Do you have any plans for the house on Maple Lane?". Name the street in the question.

ONE DRILL FOR THIS WEEK
Ten openers that end on a booked time; pass is ten of ten.

```json
{"call_id": "c1", "caller": "dana", "call_type": "full", "duration_seconds": 240, "outcome": "callback",
 "auto_fail": "PASS", "total": 63.0, "band": "Developing",
 "categories": {"1": 4.0, "2": 3.0, "3": 3.5, "4": 3.0, "5": 2.5},
 "criteria": {"1.1": 5, "1.2": 4, "1.3": 4, "2.4": 1, "5.4": 1}, "recording_url": "https://rec.example.test/nh/keep-out.mp3"}
```

```json
{"focus": "fix", "criterion": "2.4", "said": "So what would you take for it", "instead": "Do you have a ballpark figure in mind?",
 "why": "Price came before motivation."}
```
"""

REPORT_7 = """CALL GRADE REPORT
Caller: Dana    Date/Time: Tue Nov 17    Lead source/list: first to market
Auto-fail check: PASS

TOP 3 STRENGTHS (quote the transcript)
1. "Sure, when is better for you": took the delay small and set a time.
2. "Friday morning it is, talk then": confirmed the callback out loud.
3. "Hello, am I speaking with Lou": confirmed the decision maker first.

TOP 3 IMPROVEMENT AREAS (quote, then prescribe the playbook line)
1. They said: "Is now a bad time". Use instead: "Did I catch you at an okay time?". A no question invites a no.
2. They said: "I can pay cash this week, how about that". Use instead: "No pressure at all, I just wanted to reach out.". Never push a number on call one.
3. They said: "It is Dana about the property on Birch Road". Use instead: "Hi, this is Dana, I am calling about the house on Birch Road.". Lead with your own name.

```json
{"call_id": "c7", "caller": "dana", "call_type": "full", "duration_seconds": 180, "outcome": "callback",
 "auto_fail": "PASS", "total": 80.0, "band": "Strong",
 "categories": {"1": 4.0, "2": 3.0, "3": 4.0, "4": 4.0, "5": 3.0},
 "criteria": {"1.1": 5, "2.4": 3, "3.4": 4, "5.4": 2}, "recording_url": ""}
```
"""

REPORT_9 = """SHORT CALL REPORT
Caller: Dana    Date/Time: Wed Nov 18    Lead source/list: first to market
Call length: 0:40    Outcome: opt-out
Auto-fail check: PASS

WHAT HAPPENED: The owner asked to come off the list and the caller let it go.

ONE FIX: They said "Okay, no problem, have a good day". Use instead: "Totally fair, can I ask what made you say that?".
ONE DRILL: ten reps of the soft convert line.

```json
{"call_id": "c9", "caller": "dana", "call_type": "short", "duration_seconds": 40, "outcome": "no", "auto_fail": "PASS",
 "total": null, "band": null, "opener": 2.5, "conversion": 1, "conversion_attempted": false,
 "criteria": {"1.1": 3, "1.2": 2, "3.3": 1}, "recording_url": ""}
```
"""
MARKERS = (("Maple Lane", REPORT_1), ("Birch Road", REPORT_7), ("Oak Court", REPORT_9))
OWNERS = {"own-1": {"first_name": "PAT", "last_name": "RIVERS"}, "own-7": {"first_name": "Lou", "last_name": "Quill"},
          "own-9": {"first_name": "Kim", "last_name": "Ash"}}


# ---------------------------------------------------------------------- fakes
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text):
        self.content, self.stop_reason = [_Block(text)], "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=0,
                                           cache_creation_input_tokens=8000)


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClaude:
    """Stands where anthropic.Anthropic() stands; the report is chosen by the street in the transcript."""

    def __init__(self):
        self.calls = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        text = str(kw["messages"][0]["content"])
        return _Stream(_Msg(next((rep for mark, rep in MARKERS if mark in text), REPORT_1)))


class FakeCRM:
    """Owner names only. Any other call on the account fails the check that makes it."""

    def __init__(self):
        self.reads = []

    def owner(self, uuid):
        self.reads.append(uuid)
        return dict(OWNERS.get(uuid) or {"first_name": "Sam", "last_name": "Doe"})


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class FakeSlack:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        if url.endswith("chat.postMessage"):
            self.posts.append(dict(json or {}))
        return _Resp({"ok": True, "ts": "1800000000.%06d" % (500 + len(self.posts))})

    def get(self, url, params=None, timeout=None, headers=None):
        return _Resp({"ok": False})


FETCHED: list = []
TRANSCRIBED: list = []
TRANSCRIPTS = {"c1": T1, "c2": TVM, "c7": T7, "c9": T9}


def fake_fetch(url, probe, max_bytes):
    FETCHED.append((url, probe))
    host = urllib.parse.urlsplit(url).hostname
    if host == "locked.example.test":
        return {"ok": False, "code": "login", "host": host, "fmt": "", "size": 0, "audio": b"",
                "why": "the recording link needs a login the brain does not have (locked.example.test answered 401)"}
    stem = url.rsplit("/", 1)[-1].split(".")[0]
    return {"ok": True, "code": "ok", "why": "", "host": host, "fmt": "mp3", "size": 9000,
            "audio": b"" if probe else b"ID3" + stem.encode()}


def fake_transcribe(audio, fmt, caller):
    stem = audio[3:].decode()
    TRANSCRIBED.append(stem)
    return TRANSCRIPTS.get(stem, TRANSCRIPTS.get(stem.rstrip("0123456789") + "1", T1)), {"cost": 0.01}


def fake_classify(text):
    if text.startswith("VOICEMAIL"):
        return {"call_type": "voicemail", "worth_grading": False}, {}
    # The triage reads every conversation as lead management: the new hire's ROLE must pick the rubric.
    return {"call_type": "conversation", "pipeline": "lead_management", "worth_grading": True}, {}


# ---------------------------------------------------------------------- helpers
def et(month, day, hour, minute=0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


def evening(day, month=11) -> datetime:
    return et(month, day, 18, 45)


def call_ev(i, when, owner, caller, secs, rec="", inbound=False, kind="answered", caller_uuid=""):
    return {"id": "nh-ev-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "payload": {"call": {"direction": "inbound" if inbound else "outbound",
                                 "origin_number": "8650000099" if inbound else "8650000001",
                                 "destination_number": "8650000001" if inbound else "8650000099", "duration": secs,
                                 "source": "kixie", "uuid": "call-%s" % i, "recording": rec, "extra": {},
                                 "user": {"first_name": caller, "uuid": caller_uuid}}}}


def playbook(people, stage=STAGE, mode="live", **extra) -> dict:
    block = {"kind": "newhire", "owner": "ops", "mode": mode, "schedule": ["18:45 Mon-Fri"], "people": people, "sync": False}
    block.update(extra)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Dana"]}, "stages": {stage: block}}


def _register() -> dict:
    """Until the orchestrator adds the kind to the registry, register it for this section only."""
    from . import release
    from .autopilot import policy, registry, sweep
    saved = {"extra": dict(registry.EXTRA), "kinds": policy.KINDS, "spend": dict(policy.SPEND_BY_KIND),
             "nopresets": policy.NO_PRESETS, "reports": set(sweep.REPORT_KINDS), "release": dict(release.RELEASE)}
    registry.EXTRA.setdefault("newhire", ("newhire", "run_newhire", "paid", False, True))
    if "newhire" not in policy.KINDS:
        policy.KINDS = tuple(policy.KINDS) + ("newhire",)
    policy.SPEND_BY_KIND.setdefault("newhire", "paid")
    if "newhire" not in policy.NO_PRESETS:
        policy.NO_PRESETS = tuple(policy.NO_PRESETS) + ("newhire",)
    sweep.REPORT_KINDS.add("newhire")
    release.RELEASE.setdefault("newhire", ("autopilot.newhire", "release"))
    return saved


def _unregister(saved: dict) -> None:
    from . import release
    from .autopilot import policy, registry, sweep
    registry.EXTRA.clear()
    registry.EXTRA.update(saved["extra"])
    policy.KINDS, policy.NO_PRESETS = saved["kinds"], saved["nopresets"]
    policy.SPEND_BY_KIND.clear()
    policy.SPEND_BY_KIND.update(saved["spend"])
    sweep.REPORT_KINDS.clear()
    sweep.REPORT_KINDS.update(saved["reports"])
    release.RELEASE.clear()
    release.RELEASE.update(saved["release"])


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import slack_app, store
    from .autopilot import newhire
    from .workflows import call_coaching as cc
    cfg_keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.SLACK_BOT_TOKEN, config.SLACK_CHANNEL,
                config.RELEASERS, config.PUBLIC_URL, config.ANTHROPIC_API_KEY, config.SLACK_VOICE)
    hooks = (cc.FETCH, cc.TRANSCRIBE, cc.CLASSIFY, cc.CLAUDE, newhire.POLICY, slack_app._http)
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    saved = _register()
    config.DATA_DIR = Path(tmp) / "newhire"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PUBLIC_URL, config.SLACK_VOICE, config.SLACK_CHANNEL = "https://agents.example.test", "plain", TEAM
    config.SLACK_BOT_TOKEN, config.RELEASERS, config.MODE = "", "UOWNER", "dry"
    fake = FakeSlack()
    slack_app._http = fake
    cc.FETCH, cc.TRANSCRIBE, cc.CLASSIFY = fake_fetch, fake_transcribe, fake_classify
    claude = FakeClaude()
    cc.CLAUDE = claude
    store.init()
    try:
        _rules(check, newhire)
        _quiet(check, newhire)
        _first_day(check, store, newhire, cc, claude, fake)
        _release(check, store, newhire, fake)
        _thread_edit(check, store, newhire, fake)
        _where_notes_go(check, store, newhire, fake)
        _caps(check, store, newhire, claude)
        _day_timing(check, store, newhire)
        _refusals(check, store, newhire, cc, claude)
        _free_box(check, store, newhire, claude)
        _hygiene(check)
    finally:
        (cc.FETCH, cc.TRANSCRIBE, cc.CLASSIFY, cc.CLAUDE, newhire.POLICY, slack_app._http) = hooks
        (config.DATA_DIR, config.DB_PATH, config.MODE, config.SLACK_BOT_TOKEN, config.SLACK_CHANNEL,
         config.RELEASERS, config.PUBLIC_URL, config.ANTHROPIC_API_KEY, config.SLACK_VOICE) = cfg_keep
        if env_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = env_key
        _unregister(saved)


def _sweep(stage, pol, now, task=None, plan=False, crm=None):
    from .autopilot import sweep
    return sweep.run(stage, task, plan=plan, crm=crm or FakeCRM(), now=now, pol=pol)


def _task(store, stage=STAGE, source="schedule", thread=""):
    tid, _ = store.enqueue("ops", stage, "", subject="new hire week", force=True, source=source)
    if thread:
        store.set_slack(tid, TEAM, thread)
    return store.task(tid)


# ---------------------------------------------------------------------- the block and the days
def _rules(check, newhire) -> None:
    from .autopilot import policy
    print("  the block and the first week")
    pol = policy.parse(playbook([{"name": "Dana", "role": "caller", "start": "2026-11-16", "slack_user": "U0NEWHIRE1"}]), EMPLOYEES)
    check(newhire.block_problems(pol.stage(STAGE)) == [], "a block naming each new hire's name, role and start day loads")
    bad = playbook([{"name": "Gus", "role": "intern", "start": "soon", "slack_user": "gus"},
                    {"name": "Gus", "role": "caller", "start": "2026-11-16", "phone": "x"}], max_calls_per_day=0)
    try:
        policy.parse(bad, EMPLOYEES)
        refused = ""
    except policy.PolicyError as exc:
        refused = str(exc)
    check("has role 'intern'" in refused and "needs a start day" in refused and "not a Slack member id" in refused
          and "listed twice" in refused and "does not take" in refused and "max_calls_per_day must be" in refused,
          "a mistyped new hire is refused when the playbook loads: an unknown role, no start day, a bad Slack id, "
          "a name listed twice, a key it does not take, a cap out of range")
    roles = {newhire._role(r) for r in ("caller", "Cold Caller", "lead manager", "lead_manager", "closer")}
    check(roles == {"cold_call", "lead_management", "closing"},
          "the role picks the rubric: caller to cold calling, lead manager to lead management, closer to closing")
    days = pol.days
    check(newhire.day_number(date(2026, 11, 16), date(2026, 11, 17), days) == 2
          and newhire.day_number(date(2026, 11, 14), date(2026, 11, 16), days) == 1
          and newhire.day_number(date(2026, 11, 16), date(2026, 11, 23), days) == 6
          and newhire.day_number(date(2026, 11, 16), date(2026, 11, 15), days) == 0,
          "the first week counts business days from the start: a Saturday start makes Monday day 1, the next Monday is day 6")
    two = policy.parse(playbook([{"name": "Dana", "role": "caller", "start": "2026-11-16"},
                                 {"name": "Eli", "role": "closer", "start": "2026-11-02"}]), EMPLOYEES)
    check(newhire.in_first_week(two, date(2026, 11, 17)) == {"Dana"},
          "the coaching picks can ask who is graded every day this week, so nobody gets two sets of notes on the same calls")
    check(newhire.recent_days(date(2026, 11, 16), 1, days) == [date(2026, 11, 13), date(2026, 11, 16)]
          and newhire.recent_days(date(2026, 11, 17), 0, days) == [date(2026, 11, 17)],
          "a run looks at today and the business days before it it may catch up (Friday, on a Monday)")
    check(newhire.parse_edit("edit 12: Dana, two things\nfrom today") == (12, "Dana, two things\nfrom today")
          and newhire.parse_edit("Edit batch #12: x &amp; y") == (12, "x & y")
          and newhire.parse_edit("edit 12 later today") is None and newhire.parse_edit("edit 12:   ") is None,
          "an edit is read by a pattern that needs the batch number and a colon; ordinary talk is not an edit")
    s = newhire.scrub("Is this Pat? Mr. Quill at (865) 000-0199 or pat@example.test said so, and Dana is here.",
                      ["PAT", "RIVERS"], ["Dana"])
    check(s == "Is this [name]? [name] at [number] or [email] said so, and Dana is here.",
          "the scrub takes out the owner's name, a name after a title, a phone number and an email, and keeps the new hire's name")
    long = "\n\n".join("Call %d\n" % i + "\n".join("- point %d %s" % (j, "x" * 80) for j in range(6)) for i in range(20))
    parts = newhire.chunks(long)
    check(len(parts) > 1 and all(len(p) <= newhire.CHUNK for p in parts) and "\n\n".join(parts) == long,
          "long notes are cut into Slack-sized messages between calls, and nothing is lost")


def _quiet(check, newhire) -> None:
    from . import store
    from .autopilot import policy, sweep
    print("  nobody listed")
    pol = policy.parse(playbook([]), EMPLOYEES)
    out = _sweep(STAGE, pol, evening(17), plan=True)
    check(out.head.startswith("Nobody is listed as a new hire") and not out.problems,
          "with nobody listed the stage grades nothing and says so")
    again = _sweep(STAGE, pol, evening(18), plan=True)
    check(again.head == "" and sweep.posts({"id": 1, "workflow": STAGE, "source": "schedule"}, again.as_dict()) == [],
          "it says so once: the next run is quiet and posts nothing")
    asked = _sweep(STAGE, pol, evening(18), task=_task(store, source="slack"), plan=True)
    check(asked.head.startswith("Nobody is listed"), "asked in Slack, it answers every time")


# ---------------------------------------------------------------------- a first-week day, plan then live
def _seed_dana_tuesday() -> None:
    from .autopilot import activity
    activity._store([
        call_ev("c1", et(11, 17, 9, 12), "own-1", "Dana", 240, REC % "c1"),
        call_ev("c2", et(11, 17, 10, 5), "own-2", "Dana", 45, REC % "c2"),
        call_ev("c3", et(11, 17, 11, 30), "own-3", "Dana", 10, REC % "c3"),
        call_ev("c4", et(11, 17, 13, 0), "own-4", "Dana", 120, ""),
        call_ev("c5", et(11, 17, 14, 0), "own-5", "Emerson", 200, REC % "c5"),
        call_ev("c6", et(11, 17, 14, 30), "own-6", "Dana", 100, REC % "c6", inbound=True, kind="received"),
        call_ev("c7", et(11, 17, 15, 15), "own-7", "Dana", 180, REC % "c7"),
        call_ev("c8", et(11, 17, 16, 0), "own-8", "Eli", 150, REC % "c8"),
    ])


DANA = {"name": "Dana", "role": "caller", "start": "2026-11-16", "slack_user": "U0NEWHIRE1"}
ELI = {"name": "Eli", "role": "closer", "start": "2026-11-02"}


def _first_day(check, store, newhire, cc, claude, fake) -> None:
    from .autopilot import ledger, policy, sweep
    print("  a first-week day: plan, then live")
    _seed_dana_tuesday()
    pol = policy.parse(playbook([DANA, ELI]), EMPLOYEES)
    newhire.POLICY = pol
    FETCHED.clear()
    TRANSCRIBED.clear()
    out = _sweep(STAGE, pol, evening(17), plan=True)
    want = sum(cc.estimate({"seconds": s}, cc.settings())["total"] for s in (240, 45, 180))
    check(not TRANSCRIBED and not claude.calls and not ledger.batches() and ledger.spent() == 0 and store.spent() == 0,
          "plan mode transcribes nothing, grades nothing, stages nothing and pays nothing")
    check(out.head.startswith("Would grade up to 3 of Dana's calls for Tue Nov 17 (day 2 of 5)") and ("$%.2f" % want) in out.head
          and abs(out.would_usd - round(want, 2)) < 0.006,
          "plan mode says how many of the day's calls it would grade and what that would cost at most")
    check(len(FETCHED) == 1 and FETCHED[0][1] is True, "plan mode opens one recording per host, first bytes only, to prove it holds audio")
    check("Dana made no answered call with a recording on Mon Nov 16 (day 1 of 5)." in out.lines,
          "a first-week day with no answered call is named, so the owner sees a new hire who did not dial")
    check(not any("Eli" in x for x in [out.head] + out.lines) and not any("c8" in u or "c5" in u for u, _ in FETCHED),
          "a person past their first week is not graded, and another caller's recordings are never opened")
    again = _sweep(STAGE, pol, evening(17) + timedelta(minutes=5), plan=True)
    check(again.head == "" and not again.lines, "a plan run does not say the same finished day twice")

    config.MODE = "live"
    FETCHED.clear()
    task = _task(store)
    crm = FakeCRM()
    out = _sweep(STAGE, pol, evening(17), task=task, crm=crm)
    store.finish(task["id"], "done", result=out.as_dict())
    store.set_slack(task["id"], TEAM, THREAD)            # what the worker does with the first post
    check(sorted(TRANSCRIBED) == ["c1", "c2", "c7"],
          "every answered call of the day with a recording is transcribed; not the short one, the one with no "
          "recording, the inbound one or another caller's, and the plan run's marks do not stop the live run")
    check(len(claude.calls) == 2 and all("Cold Calling Call Rubric" in kw["system"][0]["text"] for kw in claude.calls),
          "only the conversations are graded (the voicemail is not), each on the rubric of the new hire's role, not the triage's")
    batches = ledger.batches(stage=STAGE)
    full = ledger.batch(batches[-1]["id"]) if batches else {}
    bid = full.get("id")
    check(len(batches) == 1 and full["kind"] == "newhire" and full["status"] == "staged" and out.batch == bid,
          "the day's notes are staged as one batch for the owner")
    check(not fake.posts, "the stage posts nothing itself: nothing reaches the new hire before the owner releases it")
    note = newhire._note(full["items"][0]["notes_id"])
    draft = note["draft"]
    check(draft.startswith("Dana, here are the notes on your calls of Tue Nov 17, day 2 of your first week.")
          and "Call 1, 9:12am, 4 min: 63 (Developing)" in draft and "Call 2, 3:15pm, 3 min: 70 (Developing)" in draft,
          "each graded call carries its time, length and score")
    first_call = draft.split("Call 2,")[0]
    check(first_call.count("\n- ") == 6 and "Keep doing:" in first_call and "Change:" in first_call,
          "each call gets three things to keep and three to change")
    check('You said "So what would you take for it". Try instead: "Do you have a ballpark figure in mind?" Price came before motivation.'
          in draft, "a change quotes the call, gives the playbook line, then says why")
    check('"Hi, is this [name]? This is Dana, I was calling about the house on Maple Lane"' in draft
          and not any(w in draft for w in ("Pat", "PAT", "Rivers", "Lou", "Quill")),
          "homeowner names are taken out of the quotes, the one asked for by name too, and the new hire's own name stays")
    check("[number]" in draft and "865-000-0142" not in draft and "8650000099" not in draft,
          "a phone number said on the call never reaches the notes")
    check("Maple Lane" in draft and "Birch Road" in draft, "a street stays: it is not a name")
    check("I can pay cash this week" not in draft, "a point quoting words the call does not hold is left out of the notes")
    check("Your day" in draft and "2 calls graded, average 66.5 (Developing)." in draft
          and "Best 70 at 3:15pm, lowest 63 at 9:12am." in draft and "1 more call was not graded: 1 voicemail." in draft,
          "the day summary gives the count, the average, the best and the lowest, and what was not graded")
    check("Keep doing: 1.1 Decision-maker confirmed first" in draft and "Focus tomorrow: 5.4 Specific either-or booking" in draft,
          "the day ends on the strongest point to keep and the weakest to work on, by the rubric's own criteria")
    file_text = (Path(out.run_dir) / "files" / full["items"][0]["file"]).read_text(encoding="utf-8")
    check(file_text.startswith("For you first: Dana does not see this part.") and "I can pay cash this week" in file_text
          and "did not foot" in file_text and draft in file_text,
          "the notes file opens with a part only the owner reads: the dropped quote and the grader's arithmetic check")
    lines = "\n".join([out.head] + out.lines)
    check(all(x not in t for t in (draft, file_text, lines, json.dumps(full, default=str)) for x in ("rec.example.test", EM, EN)),
          "no recording link and no long or en dash in the notes, the file, the Slack lines or the batch")
    unquoted = "I have not thought about a price"             # a line of the call no point quotes
    check(unquoted in str((cc.stored("call-c1") or {}).get("transcript")) and all(unquoted not in t for t in (file_text, lines))
          and str(out.run_dir).startswith(str(config.DATA_DIR)),
          "the transcript stays in the brain's database: only quotes reach the notes, and the file sits in the data folder")
    paid = ledger.spent(ledger.day(evening(17)), STAGE)
    rows = newhire._rows("Dana", "2026-11-17")
    check(paid > 0 and abs(paid - sum(r["usd"] for r in rows.values())) < 1e-6 and paid <= 10.0,
          "what was paid is in the ledger under this stage, call by call, inside its dollars a day")
    kept = cc.stored("call-c1") or {}
    check(kept.get("status") == "graded" and kept.get("pipeline") == "cold_call" and (cc.stored("call-c2") or {}).get("status") == "not_gradeable",
          "each result is kept in call coaching's own store, so the weekly picks never pay for the same call again")
    check(set(crm.reads) == {"own-1", "own-7"}, "the owner's name is read once per graded call's owner, and for nobody else")
    posts = sweep.posts({"id": task["id"], "workflow": STAGE, "source": "schedule"}, out.as_dict())
    check([p["kind"] for p in posts] == ["chat"] and ("batch %d" % bid) in posts[0]["text"] and "waiting for you" in posts[0]["text"],
          "one short line goes to the team channel: whose notes wait and the batch to release")
    check(("/files/%d/" % task["id"]) in lines and "edit %d:" % bid in lines and "release batch %d" % bid in lines,
          "the line links the notes and says how to change and release them")
    TRANSCRIBED.clear()
    claude.calls.clear()
    out2 = _sweep(STAGE, pol, evening(17) + timedelta(minutes=10), task=_task(store), crm=FakeCRM())
    check(not TRANSCRIBED and not claude.calls and len(ledger.batches(stage=STAGE)) == 1 and out2.head == "",
          "a second run the same evening pays for nothing again and stages no second batch")
    store.kv_set("newhire:selftest:batch", str(bid))
    store.kv_set("newhire:selftest:draft", draft)


# ---------------------------------------------------------------------- the owner releases, with an edit
def _release(check, store, newhire, fake) -> None:
    from . import release, worker
    from .autopilot import ledger
    print("  the owner reads, edits and releases")
    bid = int(store.kv_get("newhire:selftest:batch"))
    draft = store.kv_get("newhire:selftest:draft")
    config.SLACK_BOT_TOKEN = "xoxb-test"
    dry = newhire.release(bid, commit=False)
    check(dry.get("dry") and dry.get("would_send_to") == "Dana by direct message" and not fake.posts
          and ledger.batch(bid)["status"] == "staged", "a dry release says where the notes would go and sends nothing")
    r = newhire.slack_edit((bid, "Dana, keep the opener. Book the next step on every call."), "USOMEONE", TEAM, THREAD)
    note = newhire._note(ledger.batch(bid)["items"][0]["notes_id"])
    check(r.get("edit") == "refused" and not note["edited"] and "Only a person named as a releaser" in fake.posts[-1]["text"],
          "only a named releaser can change what a new hire gets")
    edited = "Dana, two things from your day.\n- Keep the opener, it named the street.\n- Book the next step on every call & say the day."
    r = newhire.slack_edit((bid, edited), "UOWNER", TEAM, THREAD)
    check(r.get("edit") == "saved" and "Saved your version of batch %d" % bid in fake.posts[-1]["text"],
          "the owner's version is saved against the batch and the thread is told")
    check("Your version replaces the draft." in newhire.release_note(ledger.batch(bid))
          and "/files/" in newhire.release_note(ledger.batch(bid)),
          "asked to release, the brain says who gets the notes, where to read them and that the owner's version replaces the draft")
    t0 = datetime(2026, 11, 17, 23, 50, tzinfo=timezone.utc)
    before = len(fake.posts)
    asked = release.slack_command(("release", bid, False), "UOWNER", TEAM, THREAD, now=t0)
    check(asked.get("release") == "asked" and not any(p.get("channel") == "U0NEWHIRE1" for p in fake.posts),
          "asking to release shows the batch and sends the new hire nothing")
    conf = release.slack_command(("confirm", bid, False), "UOWNER", TEAM, THREAD, now=t0 + timedelta(minutes=2))
    worker.run_task(store.task(conf["task"]))
    dms = [p for p in fake.posts[before:] if p.get("channel") == "U0NEWHIRE1"]
    check(len(dms) == 1 and dms[0]["text"] == edited.replace("&", "&amp;") and "released" in fake.posts[-1]["text"],
          "after the owner's confirm the worker sends the owner's version to the new hire by direct message")
    note = newhire._note(ledger.batch(bid)["items"][0]["notes_id"])
    check(ledger.batch(bid)["status"] == "released" and note["draft"] == draft and note["edited"] == edited
          and note["sent"] == edited and note["edited_by"] == "UOWNER",
          "the draft and the owner's version are both kept, next to what was sent")
    ed = [e for e in newhire.edits() if e["batch"] == bid]
    check(len(ed) == 1 and ed[0]["draft"] == draft and ed[0]["edited"] == edited and ed[0]["changed"],
          "the owner's changes can be read back draft beside edit, for feeding into the coaching instructions later")
    again = release.slack_command(("release", bid, False), "UOWNER", TEAM, THREAD, now=t0 + timedelta(minutes=4))
    check(again.get("why") == "not staged", "a released batch cannot be released a second time")


def _thread_edit(check, store, newhire, fake) -> None:
    from .autopilot import activity, ledger, policy
    print("  an edit left as a plain reply in the thread")
    activity._store([call_ev("c9", et(11, 18, 10, 0), "own-9", "Dana", 200, REC % "c9")])
    pol = policy.parse(playbook([DANA, ELI]), EMPLOYEES)
    task = _task(store)
    out = _sweep(STAGE, pol, evening(18), task=task)
    store.finish(task["id"], "done", result=out.as_dict())
    store.set_slack(task["id"], TEAM, "1800000000.000300")
    bid = out.batch
    draft = newhire._note(ledger.batch(bid)["items"][0]["notes_id"])["draft"] if bid else ""
    check(bid and "Call 1, 10:00am, 3 min: a short call, scored on its own scale" in draft
          and 'You said "Okay, no problem, have a good day". Try instead: "Totally fair, can I ask what made you say that?"' in draft
          and "Kim" not in draft, "a short call's one fix becomes its change, and the day before is not graded again")
    for user, text in (("UOWNER", "edit %d: Dana, short and kind today. Ask one more question." % bid),
                       ("USOMEONE", "edit %d: something nobody approved" % bid),
                       ("UOWNER", "edit 999: notes for another batch")):
        store.enqueue("ops", "ask", "", subject="edit", payload={"text": text, "user": user}, mode="agent", source="slack",
                      slack_channel=TEAM, slack_thread="1800000000.000300")
    newhire.POLICY = policy.parse(playbook([ELI]), EMPLOYEES)
    before = len(fake.posts)
    r = newhire.release(bid, commit=True)
    check(r.get("sent") == 0 and "no longer listed" in r["problems"][0] and len(fake.posts) == before
          and ledger.batch(bid)["status"] == "staged",
          "a new hire taken out of the playbook after the notes were staged gets nothing, and the batch stays staged")
    newhire.POLICY = pol
    r = newhire.release(bid, commit=True)
    dms = [p for p in fake.posts[before:] if p.get("channel") == "U0NEWHIRE1"]
    note = newhire._note(ledger.batch(bid)["items"][0]["notes_id"])
    check(r.get("sent") == 1 and len(dms) == 1 and dms[0]["text"] == "Dana, short and kind today. Ask one more question."
          and note["edited_by"] == "UOWNER" and note["draft"] == draft,
          "a releaser's edit left as a plain reply in the thread is used, another person's and another batch's are not, "
          "and both texts are kept")


def _where_notes_go(check, store, newhire, fake) -> None:
    from .autopilot import activity, ledger, policy
    print("  where the notes go")
    fay = {"name": "Fay", "role": "lead manager", "start": "2026-11-19"}
    activity._store([call_ev("f1", et(11, 19, 9, 40), "own-1", "Fay", 240, REC % "f1")])
    TRANSCRIPTS["f1"] = T1
    config.RELEASERS = ""
    pol = policy.parse(playbook([fay], stage="nh_fay"), EMPLOYEES)
    newhire.POLICY = pol
    out = _sweep("nh_fay", pol, evening(19), task=_task(store, stage="nh_fay"))
    check(out.batch and any("AGENTS_RELEASERS" in p for p in out.problems),
          "with nobody named to release batches, the stage says the notes can only be sent from the command line")
    config.RELEASERS = "UOWNER"
    config.SLACK_BOT_TOKEN = ""
    before = len(fake.posts)
    r = newhire.release(out.batch, commit=True)
    check(r.get("sent") == 0 and "not connected" in r["problems"][0] and len(fake.posts) == before
          and ledger.batch(out.batch)["status"] == "staged",
          "with the Slack app not connected nothing is sent, not even to a webhook, and the batch stays staged")
    config.SLACK_BOT_TOKEN = "xoxb-test"
    r = newhire.release(out.batch, commit=True)
    went = [p for p in fake.posts[before:] if p.get("channel") == TEAM]
    check(r.get("sent") == 1 and len(went) == 1 and went[0]["text"].startswith("Fay, here are the notes"),
          "a new hire with no Slack id gets the notes in the team channel")


# ---------------------------------------------------------------------- caps
def _caps(check, store, newhire, claude) -> None:
    from .autopilot import activity, ledger, policy
    print("  calls a day and dollars a day")
    gil = {"name": "Gil", "role": "caller", "start": "2026-11-19"}
    activity._store([call_ev("g%d" % i, et(11, 19, 10 + i, 0), "own-1", "Gil", 240, REC % ("g%d" % i)) for i in (1, 2, 3)])
    pol = policy.parse(playbook([gil], stage="nh_gil", max_calls_per_day=1), EMPLOYEES)
    newhire.POLICY = pol
    TRANSCRIBED.clear()
    out = _sweep("nh_gil", pol, evening(19), task=_task(store, stage="nh_gil"))
    full = ledger.batch(out.batch) if out.batch else {"items": [{}]}
    text = (Path(out.run_dir) / "files" / full["items"][0]["file"]).read_text(encoding="utf-8") if out.batch else ""
    check(TRANSCRIBED == ["g1"] and text.count("Not graded: the") == 2 and "limit of 1 graded calls" in text,
          "calls a day stops grading after the cap, and the owner's part lists the calls left ungraded")
    hal = {"name": "Hal", "role": "caller", "start": "2026-11-20"}
    activity._store([call_ev("h1", et(11, 20, 10, 0), "own-1", "Hal", 240, REC % "h1")])
    pol = policy.parse(playbook([hal], stage="nh_hal", daily_usd=0.05), EMPLOYEES)
    newhire.POLICY = pol
    claude.calls.clear()
    out = _sweep("nh_hal", pol, evening(20), task=_task(store, stage="nh_hal"))
    paid = ledger.spent(ledger.day(evening(20)), "nh_hal")
    check(not claude.calls and not ledger.batches(stage="nh_hal") and paid <= 0.05 + 1e-9
          and any("Hal's calls" in p and "daily_usd" in p for p in out.problems),
          "dollars a day: a grade that would pass the cap is not paid for, no notes are staged, and the owner is told why")


# ---------------------------------------------------------------------- when a day is written up
def _day_timing(check, store, newhire) -> None:
    from .autopilot import activity, ledger, policy
    print("  a day is written up once it is over, and a missed evening is caught up")
    ivy = {"name": "Ivy", "role": "closer", "start": "2026-11-23"}
    activity._store([call_ev("i1", et(11, 23, 9, 30), "own-1", "Ivy", 240, REC % "i1")])
    pol = policy.parse(playbook([ivy], stage="nh_ivy"), EMPLOYEES)
    newhire.POLICY = pol
    TRANSCRIBED.clear()
    mid = _sweep("nh_ivy", pol, et(11, 23, 14, 0), task=_task(store, stage="nh_ivy"))
    check(TRANSCRIBED == ["i1"] and not ledger.batches(stage="nh_ivy")
          and mid.head.startswith("Graded 1 of Ivy's calls for Mon Nov 23 (day 1 of 5) so far") and "6pm" in mid.head,
          "a run during the calling day grades what is in and stages nothing yet")
    TRANSCRIBED.clear()
    eve = _sweep("nh_ivy", pol, evening(23), task=_task(store, stage="nh_ivy"))
    check(not TRANSCRIBED and eve.batch and len(ledger.batches(stage="nh_ivy")) == 1,
          "the evening run stages the day's notes without paying for the same call again")
    jo = {"name": "Jo", "role": "caller", "start": "2026-11-23", "uuid": "u-jo"}
    activity._store([call_ev("j1", et(11, 24, 10, 0), "own-7", "Josephine", 180, REC % "j1", caller_uuid="u-jo"),
                     call_ev("j2", et(11, 25, 10, 0), "own-7", "Josephine", 180, REC % "j2", caller_uuid="u-jo")])
    TRANSCRIPTS["j1"], TRANSCRIPTS["j2"] = T7, T7
    pol = policy.parse(playbook([jo], stage="nh_jo"), EMPLOYEES)
    newhire.POLICY = pol
    planned = _sweep("nh_jo", pol, evening(24), plan=True)
    check(planned.head.startswith("Would grade up to 1 of Jo's calls for Tue Nov 24"),
          "a call made under another display name is still the new hire's when it carries their user id")
    out = _sweep("nh_jo", pol, evening(25), task=_task(store, stage="nh_jo"))
    days = sorted(ledger.batch(b["id"])["items"][0]["day"] for b in ledger.batches(stage="nh_jo"))
    check(days == ["2026-11-24", "2026-11-25"] and out.head.startswith("Notes for 2 new hire days are waiting for you"),
          "an evening the box missed is caught up the next evening as its own batch; a plan run's mark does not stop it")


# ---------------------------------------------------------------------- what stops a run
def _refusals(check, store, newhire, cc, claude) -> None:
    from .autopilot import activity, ledger, policy
    print("  a wrong name, missing keys, a login wall, a call graded before")
    gus = {"name": "Gus Example", "role": "caller", "start": "2026-11-26"}
    activity._store([call_ev("x1", et(11, 26, 10, 0), "own-1", "Gus", 240, REC % "x1")])
    pol = policy.parse(playbook([gus], stage="nh_gus"), EMPLOYEES)
    out = _sweep("nh_gus", pol, evening(26), plan=True)
    check(any("no call names Gus Example as the caller, but calls name Gus" in p for p in out.problems),
          "a name that does not match the log exactly is never guessed at: the owner is told what the log writes")
    kai = {"name": "Kai", "role": "caller", "start": "2026-11-26"}
    activity._store([call_ev("k1", et(11, 26, 11, 0), "own-1", "Kai", 240, REC % "k1")])
    pol = policy.parse(playbook([kai], stage="nh_kai"), EMPLOYEES)
    newhire.POLICY = pol
    keep_claude = cc.CLAUDE
    cc.CLAUDE, config.ANTHROPIC_API_KEY = None, ""
    os.environ.pop("ANTHROPIC_API_KEY", None)
    TRANSCRIBED.clear()
    try:
        out = _sweep("nh_kai", pol, evening(26), task=_task(store, stage="nh_kai"))
    finally:
        cc.CLAUDE = keep_claude
    check(not TRANSCRIBED and not ledger.batches(stage="nh_kai") and any("ANTHROPIC_API_KEY" in p for p in out.problems),
          "with no grading key on the box nothing is paid for: no call is transcribed that could not then be graded")
    lee = {"name": "Lee", "role": "caller", "start": "2026-11-27"}
    activity._store([call_ev("l1", et(11, 27, 10, 0), "own-1", "Lee", 240, LOCKED % "l1"),
                     call_ev("l2", et(11, 27, 11, 0), "own-1", "Lee", 240, LOCKED % "l2")])
    pol = policy.parse(playbook([lee], stage="nh_lee"), EMPLOYEES)
    newhire.POLICY = pol
    plan = _sweep("nh_lee", pol, evening(27), plan=True)
    FETCHED.clear()
    live = _sweep("nh_lee", pol, evening(27), task=_task(store, stage="nh_lee"))
    check("behind a recording login" in plan.head and any("locked.example.test" in p for p in plan.problems)
          and len(FETCHED) == 1 and not ledger.batches(stage="nh_lee")
          and any("need a login the brain does not have" in p for p in live.problems),
          "a recording host behind a login is found in plan mode, asked only once live, and nothing is staged")
    mo = {"name": "Mo", "role": "caller", "start": "2026-11-30"}
    activity._store([call_ev("m1", et(11, 30, 10, 0), "own-1", "Mo", 240, REC % "m1")])
    grade = cc._engine()[0]
    g = grade.grade(T1, {"caller": "Mo", "kind": "worst"}, "cold_call", client=FakeClaude())
    cc.keep({"call_id": "call-m1", "period": "week:2026-11-27", "caller": "Mo", "kind": "worst", "at": "", "seconds": 240,
             "status": "graded", "why": "", "pipeline": "cold_call", "call_type": "full", "total": g["scores"]["total"],
             "band": g["scores"]["band"], "auto_fail": "PASS", "criterion": "2.4", "note": g["note"], "report": g["report"],
             "transcript": T1, "problems": [], "usd": 0.1}, 0)
    pol = policy.parse(playbook([mo], stage="nh_mo"), EMPLOYEES)
    newhire.POLICY = pol
    TRANSCRIBED.clear()
    claude.calls.clear()
    out = _sweep("nh_mo", pol, evening(30), task=_task(store, stage="nh_mo"))
    check(not TRANSCRIBED and not claude.calls and out.batch and ledger.spent(ledger.day(evening(30)), "nh_mo") == 0,
          "a call call coaching already graded on the same rubric is used as it is, and nothing is paid for it again")


def _free_box(check, store, newhire, claude) -> None:
    """On a box in AGENTS_MODE=free the week pays only when AGENTS_LIVE_STAGES names the stage (2026-11-19).
    The stage grades through call coaching's grade_one on its own run's rule (ctx.live, config.stage_live)
    and never through the workflow's run(), so the check that kept the workflow dry there is not on its path."""
    from .autopilot import activity, ledger, policy
    print("  a free box grades a new hire's day only when AGENTS_LIVE_STAGES names the stage")
    nia = {"name": "Nia", "role": "caller", "start": "2026-12-01"}
    activity._store([call_ev("n1", et(12, 1, 10, 0), "own-1", "Nia", 240, REC % "n1")])
    TRANSCRIPTS["n1"] = T1
    pol = policy.parse(playbook([nia], stage="nh_nia"), EMPLOYEES)
    newhire.POLICY = pol
    keep = (config.MODE, config.LIVE_STAGES)
    try:
        config.MODE, config.LIVE_STAGES = "free", frozenset()
        TRANSCRIBED.clear()
        claude.calls.clear()
        out = _sweep("nh_nia", pol, evening(1, 12), task=_task(store, stage="nh_nia"))
        check(not out.live and not TRANSCRIBED and not claude.calls and not ledger.batches(stage="nh_nia")
              and out.head.startswith("Would grade up to 1 of Nia's calls"),
              "on a free box a live new hire week that AGENTS_LIVE_STAGES does not name only plans: nothing paid or staged")
        config.LIVE_STAGES = frozenset({"nh_nia"})
        out = _sweep("nh_nia", pol, evening(1, 12), task=_task(store, stage="nh_nia"))
        check(out.live and TRANSCRIBED == ["n1"] and len(claude.calls) == 1 and out.batch
              and ledger.batch(out.batch)["kind"] == "newhire",
              "named in AGENTS_LIVE_STAGES the same week grades the day on that box and stages the notes for the owner")
    finally:
        config.MODE, config.LIVE_STAGES = keep


def _hygiene(check) -> None:
    print("  hygiene")
    here = Path(__file__).resolve().parent
    for f in (here / "autopilot" / "newhire.py", here / "selftest_newhire.py"):
        text = f.read_text(encoding="utf-8")
        check(EM not in text and EN not in text and "\x08" not in text,
              "%s holds no long dash, no en dash and no backspace byte" % f.name)
