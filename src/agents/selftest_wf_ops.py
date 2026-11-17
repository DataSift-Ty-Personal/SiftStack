"""Quinn's operations workflows selftest: call coaching, the weekly KPI report, the batch QA and
outreach health, plus the evidence behind retiring daily_digest. Zero network: the recording host,
OpenRouter, Claude and DataSift are fakes, and every name, street, number and link is invented.
"""
from __future__ import annotations

import copy
import json
import os
import types
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
EM, EN = chr(0x2014), chr(0x2013)
REC = "https://rec.example.test/calls/%s.mp3"
LOCKED = "https://locked.example.test/calls/%s.mp3"

TRANSCRIPT = ("AGENT: Hey, is this Pat? I was calling about the property on Maple Lane. [warm tone]\n"
              "SELLER: Yes, this is Pat.\n"
              "AGENT: So what would you take for it?\n"
              "SELLER: I have not thought about it.\n\n"
              "DELIVERY SUMMARY:\n- pace: conversational")

REPORT = """CALL GRADE REPORT
Caller: Ada    Date/Time: Thu Nov 12    Lead source/list: first to market
Auto-fail check: PASS

TOP 3 STRENGTHS (quote the transcript)
1. "Hey, is this Pat? I was calling about the property on Maple Lane": confirmed the owner first.

TOP 3 IMPROVEMENT AREAS (quote, then prescribe the playbook line)
1. They said: "So what would you take for it". Use instead: "What is a ballpark figure that you would like to sell the property for?". Price came first.

```json
{"call_id": "c-1", "caller": "ada", "call_type": "full", "duration_seconds": 240, "outcome": "callback",
 "auto_fail": "PASS", "total": 61.0, "band": "Developing",
 "categories": {"1": 3.0, "2": 2.2, "3": 4.0, "4": 2.0, "5": 1.6},
 "criteria": {"1.1": 4, "2.4": 1}, "recording_url": "https://rec.example.test/calls/should-not-keep.mp3"}
```

```json
{"focus": "fix", "criterion": "2.4", "said": "So what would you take for it",
 "instead": "What is a ballpark figure that you would like to sell the property for?", "why": "Price came before motivation."}
```
"""

NOT_SCORED = 'One line: a voicemail, nothing to score.\n```json\n{"call_type": "not_scored", "total": null, "band": null}\n```\n'


# ---------------------------------------------------------------------- fakes
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop
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
    """Stands where anthropic.Anthropic() stands. Each reply is a text, or (text, stop_reason)."""

    def __init__(self, *replies):
        self.replies = list(replies) or [REPORT]
        self.calls = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        text, stop = (r if isinstance(r, tuple) else (r, "end_turn"))
        return _Stream(_Msg(text, stop))


class _Resp:
    def __init__(self, data, ctype="audio/mpeg", url="https://rec.example.test/calls/a.mp3"):
        self._d, self.headers, self._u = data, {"Content-Type": ctype}, url

    def read(self, n=-1):
        return self._d if n is None or n < 0 else self._d[:n]

    def geturl(self):
        return self._u

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(resp=None, exc=None):
    calls = []

    def op(req, timeout=60):
        calls.append(req.full_url)
        if exc is not None:
            raise exc
        return resp
    op.calls = calls
    return op


class QaCRM:
    """Reads only. Any write a check might make fails the test, so 'it never releases' is proven."""

    def __init__(self, recs):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in recs}
        self.reads = []

    def record(self, u):
        self.reads.append(u)
        return copy.deepcopy(self.recs.get(u) or {})

    def __getattr__(self, name):
        raise AssertionError("the QA tried to call %s on DataSift" % name)


class FakePolicy:
    def __init__(self, stages):
        from .autopilot import gate
        self.tz, self.days, self.hours = "America/New_York", frozenset(range(5)), (540, 1080)
        self.gate, self.lanes, self.stages = gate.GateConfig(), None, stages
        self.daily_usd, self.record_usd, self.outreach_daily_usd, self.callers = 25.0, 1.0, 60.0, []

    def stage(self, sid):
        return self.stages[sid]


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(month: int, day: int, hour: int = 11, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


def call_ev(i, owner, when, kind="made", line="8655550201", number="8655550101", secs=0, caller="Ada"):
    return {"id": "wo-call-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": number, "origin_number": line, "direction": "outbound",
                                 "duration": secs, "source": "kixie", "uuid": "c-%s" % i, "extra": {},
                                 "user": {"first_name": caller, "uuid": "u-%s" % caller.lower()},
                                 "recording": REC % ("log-%s" % i) if kind == "answered" else ""}}}


def status_ev(i, rec, when, old, new):
    return {"id": "wo-st-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def phone_ev(i, owner, when, number, new, source="kixie"):
    return {"id": "wo-ph-%s" % i, "event_type": "owner.phone.status.updated", "resource": owner, "resource_type": "owner",
            "source": source, "author": "system", "timestamp": _ts(when),
            "payload": {"owner": {"phone": number, "status": ["UNKNOWN", new]}}}


def text_in(i, owner, when, number):
    return {"id": "wo-sms-%s" % i, "event_type": "owner.sms.received", "resource": owner, "resource_type": "owner",
            "source": "smarter-contact", "author": "system", "timestamp": _ts(when),
            "payload": {"sms": {"origin_number": number, "destination_number": "8655550000", "direction": "inbound",
                                "status": "received"}}}


def task_ev(i, prop, when, kind="appointment"):
    return {"id": "wo-task-%s" % i, "event_type": "task.created", "resource": "task-%s" % i, "resource_type": "task",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"task": {"assigned_to_property": prop, "title": "Walkthrough", "event_type": kind}}}


def card_ev(i, prop, when, column):
    return {"id": "wo-card-%s" % i, "event_type": "card.moved", "resource": "card-%s" % i, "resource_type": "card",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"card": {"prop": prop}, "extra_info": {"card": {"prop": prop, "column": ["HOT Leads", column],
                                                                        "board": ["Acquisitions"]}}}}


def _rec(u, street, owner, first="Pat", last="Example", phones=(), status=None, tags=("FTM",)):
    return {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": owner, "first_name": first, "last_name": last, "phones": [dict(p) for p in phones],
                      "address": {"street": "9 Mail Rd", "city": "Knoxville", "state": "TN", "postal_code": "37917"}},
            "tags": list(tags), "lists": [], "status": status}


def _ph(number, tier="Dial First", status="UNKNOWN", kind="MOBILE"):
    return {"number": number, "type": kind, "tags": [tier], "status": status}


def _no_dashes(text: str) -> bool:
    return EM not in (text or "") and EN not in (text or "")


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import store, workflows
    from .autopilot import kpi
    from .workflows import call_coaching, kpi_report, outreach_health, qa_batch
    from call_coaching import grade, transcribe

    store.init()
    saved_env = {k: os.environ.get(k) for k in ("AGENTS_COACH_RUN_CAP_USD", "AGENTS_COACH_ROLES", "AGENTS_QA_MAX_READS",
                                                  "ANTHROPIC_API_KEY", "AGENTS_COACH_MAX_CALLS", "SELFTEST_WF_OPS_KEY")}
    saved_cfg = (config.MODE, config.DAILY_CAP_USD, config.ANTHROPIC_API_KEY)
    saved_hooks = (call_coaching.FETCH, call_coaching.TRANSCRIBE, call_coaching.CLASSIFY, call_coaching.CLAUDE,
                   call_coaching.NOW, kpi_report.NOW, qa_batch.CRM, qa_batch.POLICY, qa_batch.NOW,
                   outreach_health.NOW, outreach_health.POLICY, transcribe.POST)
    saved_override = kpi._OVERRIDE[0]
    saved_registry = dict(workflows.REGISTRY)
    with store._LOCK, store._db() as c:
        for table in ("ap_capture", "ap_log", "ap_batches", "ap_owner_lane", "ap_facts", "ap_phones", "spend"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%' OR key LIKE 'wf:%'")
    try:
        _transcribe_checks(check, transcribe)
        _grade_checks(check, grade)
        _coaching_checks(check, tmp, store, call_coaching, grade)
        kpi.override(None)
        _kpi_checks(check, tmp, store, kpi_report, kpi)
        _qa_checks(check, tmp, store, qa_batch)
        _outreach_checks(check, tmp, store, outreach_health)
        _registry_checks(check, workflows)
        _decision_checks(check)
        _hygiene_checks(check)
    finally:
        (call_coaching.FETCH, call_coaching.TRANSCRIBE, call_coaching.CLASSIFY, call_coaching.CLAUDE,
         call_coaching.NOW, kpi_report.NOW, qa_batch.CRM, qa_batch.POLICY, qa_batch.NOW,
         outreach_health.NOW, outreach_health.POLICY, transcribe.POST) = saved_hooks
        config.MODE, config.DAILY_CAP_USD, config.ANTHROPIC_API_KEY = saved_cfg
        kpi.override(saved_override)
        workflows.REGISTRY.clear()
        workflows.REGISTRY.update(saved_registry)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------- transcription and the recording link
def _transcribe_checks(check, transcribe) -> None:
    print("  transcription and recording links")
    check(transcribe.audio_format(b"ID3\x04rest") == "mp3" and transcribe.audio_format(b"\xff\xfb\x90\x00") == "mp3"
          and transcribe.audio_format(b"RIFF....WAVE") == "wav" and transcribe.audio_format(b"<!DOCTYPE html>") == "",
          "audio is recognised by its first bytes, and a web page is not audio")
    audio = b"ID3" + b"\x00" * 9000
    op = opener(_Resp(audio))
    r = transcribe.fetch_recording(REC % "a", opener=op)
    check(r["ok"] and r["fmt"] == "mp3" and r["audio"] == audio and r["host"] == "rec.example.test",
          "a recording link that opens without a login hands back the audio")
    r = transcribe.fetch_recording(REC % "a", opener=opener(_Resp(audio)), probe=True)
    check(r["ok"] and r["audio"] == b"", "a probe proves the link holds audio and keeps none of it")
    r = transcribe.fetch_recording(REC % "a", opener=opener(_Resp(b"<html><form>Sign in</form></html>", "text/html")))
    check(not r["ok"] and r["code"] == "login" and "needs a login" in r["why"],
          "a link that opens a login page is a link that needs a login, never a guess at a cookie")
    r = transcribe.fetch_recording(REC % "a", opener=opener(exc=urllib.error.HTTPError(REC % "a", 403, "Forbidden", {}, None)))
    check(r["code"] == "login" and "403" in r["why"], "a 403 from the recording host is a login wall")
    r = transcribe.fetch_recording(REC % "a", opener=opener(exc=urllib.error.HTTPError(REC % "a", 404, "Gone", {}, None)))
    check(r["code"] == "gone", "a 404 is a recording that is gone")
    r = transcribe.fetch_recording(REC % "a", opener=opener(_Resp(b"not audio at all", "application/octet-stream",
                                                                  url="https://rec.example.test/login?next=x")))
    check(r["code"] == "login", "a redirect that lands on a sign-in address reads as a login wall")
    r = transcribe.fetch_recording("https://rec.example.test/auth/2026/a.mp3", opener=opener(_Resp(audio, url="https://rec.example.test/auth/2026/a.mp3")))
    check(r["ok"], "audio is audio whatever its address says: a path with 'auth' in it is not mistaken for a login")
    op = opener(_Resp(audio))
    r = transcribe.fetch_recording("file:///etc/passwd", opener=op)
    check(r["code"] == "bad_link" and not op.calls, "only a web address is opened")
    r = transcribe.fetch_recording(REC % "big", opener=opener(_Resp(audio)), max_bytes=100)
    check(r["code"] == "too_big", "a recording past the size cap is not sent")

    bodies = []

    def fake_post(body, timeout):
        bodies.append(body)
        return {"choices": [{"message": {"content": "AGENT: hi"}}], "usage": {"cost": 0.004}}
    transcribe.POST = fake_post
    text, usage = transcribe.transcribe_bytes(b"ID3abc", "mp3", "Ada")
    sent = bodies[-1]
    check(text == "AGENT: hi" and transcribe.billed(usage) == 0.004 and sent["usage"] == {"include": True}
          and sent["messages"][0]["content"][1]["input_audio"]["format"] == "mp3" and "Ada" in sent["messages"][0]["content"][0]["text"],
          "transcription asks OpenRouter for the bill, sends the audio's own format and names our caller for the labels")
    transcribe.POST = lambda body, timeout: {"choices": [{"message": {"content": "no json here"}}]}
    cls, _ = transcribe.classify_text("AGENT: hello")
    check(cls.get("worth_grading") is False and cls.get("call_type") == "unknown",
          "a triage reply with no JSON is a call nobody grades, never a guessed classification")
    os.environ["SELFTEST_WF_OPS_KEY"] = "from-the-environment"
    check(transcribe._env("SELFTEST_WF_OPS_KEY") == "from-the-environment",
          "a key is read from the environment first (a Fly secret), so the box needs no .env")
    transcribe.POST = None


# ---------------------------------------------------------------------- grading
def _grade_checks(check, grade) -> None:
    print("  grading against the rubric")
    for p in grade.PIPELINES:
        w = grade.weights(grade.rubric(p))
        check(w and abs(sum(w.values()) - 100.0) < 0.01, "the %s rubric's own category weights are read and sum to 100" % p)
    w = grade.weights(grade.rubric("cold_call"))
    check(grade.recompute({"1": 3.0, "2": 2.2, "3": 4.0, "4": 2.0, "5": 1.6}, w) == 50.6,
          "the total is recomputed the rubric's way (its own worked example gives 50.6)")
    check(grade.recompute({"1": 5.0, "2": None, "3": 5.0, "4": 5.0, "5": 5.0}, w) == 100.0,
          "an N/A category's weight is spread over the scored ones")
    check(grade.band(92) == "Elite" and grade.band(75) == "Strong" and grade.band(59.9) == "Needs Work" and grade.band(10) == "Retrain",
          "bands follow the rubric: 90 Elite, 75 Strong, 60 Developing, 40 Needs Work, under 40 Retrain")
    names = grade.criterion_names(grade.rubric("cold_call"))
    check(names.get("2.4", "").startswith("Price last"), "criterion names are read from the rubric for the notes")

    fc = FakeClaude(REPORT)
    g = grade.grade(TRANSCRIPT, {"caller": "Ada", "kind": "worst", "why": "the owner said no", "when": "Thu Nov 12"},
                    "cold_call", client=fc)
    kw = fc.calls[-1]
    sc = g["scores"]
    check(sc and sc["total"] == 50.6 and sc["band"] == "Needs Work" and any("did not foot" in p for p in g["problems"]),
          "a total that does not foot is replaced by the categories' sum and said, never kept")
    check("recording_url" not in sc, "the recording link is dropped from what is kept, even when the model writes one")
    check(g["note"] and g["note"]["focus"] == "fix" and g["note"]["criterion"] == "2.4" and not g["unverified"],
          "the owner's note comes back with its criterion, and every quote in the report is in the transcript")
    sys_text = kw["system"][0]["text"]
    check(kw["system"][0].get("cache_control") == {"type": "ephemeral"} and "Cold Calling Call Rubric" in sys_text
          and kw["output_config"] == {"effort": grade.effort_of()} and kw["betas"] == [grade.FALLBACK_BETA]
          and kw["extra_body"] == {"fallbacks": "default"} and kw["model"] == grade.model_of(),
          "the rubric is the cached system prompt, with the brain's refusal fallback and the coaching effort")
    check("example.test" not in json.dumps(kw["messages"]), "the grader is never sent a recording link")
    check(g["usd"] == round(grade.usage_usd({"input_tokens": 1000, "output_tokens": 2000, "cache_creation_input_tokens": 8000},
                                            grade.model_of()), 4) and g["usd"] > 0,
          "the cost is read from the reply's own token counts")

    bad = REPORT.replace("So what would you take for it\". Use", "I will pay cash today, how about that\". Use")
    g = grade.grade(TRANSCRIPT, {"kind": "worst"}, "cold_call", client=FakeClaude(bad))
    check(g["unverified"] and any("not in the transcript" in p for p in g["problems"]),
          "a quote the call never held is flagged for the owner (the skill's verify pass)")
    g = grade.grade(TRANSCRIPT, {"kind": "best"}, "cold_call", client=FakeClaude("A fine call, no numbers."))
    check(g["scores"] is None and any("without its SCORES JSON" in p for p in g["problems"]),
          "no SCORES JSON, no grade: nothing is recorded as a score")
    g = grade.grade(TRANSCRIPT, {"kind": "best"}, "cold_call", client=FakeClaude(("", "refusal")))
    check(g["scores"] is None and "declined" in g["problems"][0], "a refusal is no grade, said plainly")
    g = grade.grade(TRANSCRIPT, {"kind": "best"}, "cold_call", client=FakeClaude(NOT_SCORED))
    check(g["scores"]["call_type"] == "not_scored" and g["scores"]["total"] is None,
          "a call the rubric's gate does not score is kept as not scored, with no number")
    dashed = REPORT.replace("confirmed the owner first", "confirmed the owner first %s good" % EM).replace("4.0", "4.0")
    g = grade.grade(TRANSCRIPT, {"kind": "best"}, "cold_call", client=FakeClaude(dashed))
    check(_no_dashes(g["report"]), "no long dash or en dash survives into the report a person reads")


# ---------------------------------------------------------------------- call coaching
def _coaching_checks(check, tmp, store, cc, grade) -> None:
    from .autopilot import coachpick
    print("  call coaching")
    os.environ["AGENTS_COACH_RUN_CAP_USD"] = "5"
    os.environ.pop("AGENTS_COACH_ROLES", None)
    os.environ.pop("AGENTS_COACH_MAX_CALLS", None)
    period = "week:2026-11-13"
    picks = [("c1", "Ada", "best", 240, REC % "c1"), ("c2", "Ada", "best", 200, REC % "c2"),
             ("c3", "Ada", "worst", 90, REC % "c3"), ("c4", "Ben", "worst", 70, LOCKED % "c4"),
             ("c5", "Ben", "best", 300, LOCKED % "c5")]
    for cid, who, kind, secs, link in picks:
        coachpick._keep(period, who, [{"call": cid, "kind": kind, "when": _et(11, 12, 14), "seconds": secs,
                                       "recording": link, "why": "picked for the selftest"}])
    calls = [{"caller": who, "kind": kind, "call": cid, "at": _et(11, 12, 14).astimezone(timezone.utc).isoformat(),
              "seconds": secs, "recording": link, "why": "picked for the selftest"} for cid, who, kind, secs, link in picks]
    payload = {"details": "Review pack", "inputs": {"period": period, "notes_to": "owner", "calls": calls}}

    fetched, said = [], []

    def fake_fetch(url, probe, max_bytes):
        fetched.append((url, probe))
        host = urllib.parse.urlsplit(url).hostname
        if host == "locked.example.test":
            return {"ok": False, "code": "login", "host": host, "fmt": "", "size": 0, "audio": b"",
                    "why": "the recording link needs a login the brain does not have (locked.example.test answered 401)"}
        cid = url.rsplit("/", 1)[-1].split(".")[0]
        return {"ok": True, "code": "ok", "why": "", "host": host, "fmt": "mp3", "size": 9000,
                "audio": b"" if probe else b"ID3" + cid.encode()}

    def fake_transcribe(audio, fmt, caller):
        cid = audio[3:].decode()
        said.append(cid)
        if cid == "c7":
            return "", {}
        return (("VOICEMAIL: leave a message\n" if cid == "c2" else "") + TRANSCRIPT), {"cost": 0.01}

    def fake_classify(text):
        if text.startswith("VOICEMAIL"):
            return {"call_type": "voicemail", "worth_grading": False}, {}
        return {"call_type": "conversation", "pipeline": "cold_call", "worth_grading": True}, {}

    cc.FETCH, cc.TRANSCRIBE, cc.CLASSIFY = fake_fetch, fake_transcribe, fake_classify
    claude = FakeClaude(REPORT)
    cc.CLAUDE = claude

    def task(p, source="autopilot:coaching_picks"):
        tid, _ = store.enqueue("ops", "call_coaching", "", subject="Call review", force=True, payload=p, source=source)
        return store.task(tid)

    # ---- dry: every free step, nothing paid
    config.MODE = "dry"
    t = task(payload)
    r = cc.run(t)
    st = {c["call"]: c["status"] for c in r["calls"]}
    check(st == {"c1": "would_grade", "c2": "would_grade", "c3": "would_grade", "c4": "stopped", "c5": "stopped"},
          "dry: each recording link is opened to prove it holds audio; calls on a host behind a login are stopped")
    check(all(p for _, p in fetched) and not any(u.endswith("c5.mp3") for u, _ in fetched),
          "dry reads only the first bytes, and a host that needed a login is not asked again")
    check(not said and not claude.calls and store.spent(task_id=t["id"]) == 0.0,
          "dry transcribes nothing, grades nothing and pays nothing")
    check(abs(r["would_usd"] - 0.93) < 0.011, "dry says what the paid steps would cost (about $0.93 for three calls)")
    text = cc.render(t, r)
    check("dry" in text and "$0.93" in text and "needs a login" in text and len(text.splitlines()) <= 3,
          "the dry post is short: what was picked, what would be paid, and the login wall")
    check("c4.mp3" not in text and "c4.mp3" not in json.dumps(r), "the recording link is in neither the post nor the stored result")

    # ---- live: transcribed, triaged, graded inside the caps
    config.MODE = "live"
    fetched.clear()
    t = task(payload)
    r = cc.run(t)
    st = {c["call"]: c["status"] for c in r["calls"]}
    check(st == {"c1": "graded", "c2": "not_gradeable", "c3": "graded", "c4": "stopped", "c5": "stopped"},
          "live: two conversations graded, the voicemail kept as not gradeable, the locked host stopped")
    check(sorted(said) == ["c1", "c2", "c3"] and len(claude.calls) == 2,
          "only calls with audio are transcribed, and only gradeable ones reach the grader")
    check(not any(u.endswith("c5.mp3") for u, _ in fetched) and all(not p for _, p in fetched),
          "live fetches each recording once in full, and stops asking a host that needs a login")
    one = grade.usage_usd({"input_tokens": 1000, "output_tokens": 2000, "cache_creation_input_tokens": 8000}, grade.model_of())
    want = 3 * 0.01 + 3 * 0.001 + 2 * one
    check(abs(store.spent(task_id=t["id"]) - want) < 0.0005 and abs(r["usd"] - want) < 0.0005,
          "every paid step is recorded against the run: transcription as billed, the triage, the grades")
    rows = {c["call"]: c for c in r["calls"]}
    check(rows["c1"]["total"] == 50.6 and rows["c1"]["band"] == "Needs Work" and rows["c3"]["criterion"] == "2.4",
          "a graded call keeps its checked total, band and the criterion the note is about")
    d = Path(config.RUNS_DIR) / ("call_coaching-%d" % t["id"]) / "files"
    md = d / "call_review_the_week_ending_nov_13.md"
    xl = d / "call_review_the_week_ending_nov_13.xlsx"
    check(md.is_file() and xl.is_file() and r["run_dir"] == str(d.parent) and sorted(r["files"]) == sorted([md.name, xl.name]),
          "the notes and the scores are written to the task's run folder, where the brain serves them")
    body = md.read_text(encoding="utf-8")
    check(body.startswith("# Call review, the week ending Nov 13") and "For the owner first" in body
          and "Fix first (2.4 Price last" in body and "Not graded" in body,
          "the notes are addressed to the owner first, one thing to fix per worst call, and say what was not graded")
    from openpyxl import load_workbook
    wb = load_workbook(xl)
    cells = " ".join(str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None)
    check("example.test/calls" not in body and "example.test/calls" not in cells and _no_dashes(body) and _no_dashes(cells),
          "no recording link and no long dash in either file")
    check([ws.title for ws in wb.worksheets] == ["Calls", "Notes for the owner", "Sources"],
          "the workbook holds the calls, the owner's notes and where it all came from")
    text = cc.render(t, r)
    lines = text.splitlines()
    check(lines[0].startswith(":clipboard: *Call review, the week ending Nov 13*: 2 of 5 call(s) graded") and "For you first" in lines[0]
          and len(lines) <= 5, "the post is a glance: how many were graded, what it cost, and that the owner reads first")
    check(any(l.startswith("Ada: best 50.6, worst 50.6; fix first: 2.4 Price last") for l in lines),
          "each caller gets one line: best and worst scores and the first thing to fix")
    check("Notes: https://agents.example.test/files/%d/" % t["id"] in text and "Pat" not in text and "c1.mp3" not in text,
          "the post links the signed files and carries no homeowner's name and no recording link")
    check(_no_dashes(text), "the post carries no long dash")
    saved_url, config.PUBLIC_URL = config.PUBLIC_URL, ""
    check("no public link" in cc.render(t, r), "with no public address set, the post says where the notes are instead of linking nothing")
    config.PUBLIC_URL = saved_url

    # ---- asked again: nothing is paid twice
    said.clear()
    claude.calls.clear()
    t2 = task(payload)
    r2 = cc.run(t2)
    st2 = {c["call"]: (c["status"], c["reused"]) for c in r2["calls"]}
    check(st2["c1"] == ("graded", True) and st2["c2"] == ("not_gradeable", True) and not said and not claude.calls
          and store.spent(task_id=t2["id"]) == 0.0, "a call already graded is read from the store, never paid for again")

    # ---- the scope an employee passes from Slack
    check(len(cc.picks_for("latest")) == 5 and {p["call"] for p in cc.picks_for("Ben")} == {"c4", "c5"}
          and len(cc.picks_for(period)) == 5 and cc.picks_for("Nobody") == [],
          "an ask picks the latest pack, one caller's newest picks, or one period, from what the stage kept")
    check(cc.label_of(["week:2026-11-13"]) == "the week ending Nov 13" and cc.label_of(["day:2026-11-13:Ada"]) == "Ada on Nov 13",
          "a pack is named the way a person says it")
    r = cc.run(task({"inputs": {"scope": "Nobody"}}, source="agent:ops"))
    check(r.get("nothing") and "two best and two worst" in cc.render({"id": 0}, r),
          "with nothing picked it says how calls get picked instead of acting as if it ran")

    # ---- the caps
    def one_call(cid, who="Cy", secs=60):
        return {"inputs": {"period": "week:2026-11-20", "calls": [{"caller": who, "kind": "worst", "call": cid,
                                                                   "at": _et(11, 19, 10).astimezone(timezone.utc).isoformat(),
                                                                   "seconds": secs, "recording": REC % cid, "why": "test"}]}}
    said.clear()
    os.environ["AGENTS_COACH_RUN_CAP_USD"] = "0.001"
    t3 = task(one_call("c6"))
    r = cc.run(t3)
    check(r["calls"][0]["status"] == "left" and "this run's cap" in r["calls"][0]["why"] and not said
          and store.spent(task_id=t3["id"]) == 0.0, "the run's cap is asked before the first paid step, and holds")
    os.environ["AGENTS_COACH_RUN_CAP_USD"] = "5"
    saved_cap = config.DAILY_CAP_USD
    config.DAILY_CAP_USD = store.spent(day=store.today(), research=False) + 0.002
    t3 = task(one_call("c6"))
    r = cc.run(t3)
    check(r["calls"][0]["status"] == "left" and "daily cap" in r["calls"][0]["why"] and not said,
          "the brain's daily cap is asked as well")
    config.DAILY_CAP_USD = saved_cap

    # ---- no transcript, no grade
    claude.calls.clear()
    r = cc.run(task(one_call("c7")))
    check(r["calls"][0]["status"] == "no_transcript" and not claude.calls, "an empty transcript is never graded")

    # ---- ungraded, then graded again without paying for the transcript twice
    said.clear()
    cc.CLAUDE = FakeClaude("No numbers in this one.")
    r = cc.run(task(one_call("c8")))
    check(r["calls"][0]["status"] == "ungraded" and said == ["c8"], "a report without its SCORES JSON is kept as ungraded")
    said.clear()
    cc.CLAUDE = claude
    claude.calls.clear()
    cls_seen = []
    cc.CLASSIFY = lambda text: (cls_seen.append(1), fake_classify(text))[1]
    r = cc.run(task(one_call("c8")))
    check(r["calls"][0]["status"] == "graded" and not said and not cls_seen and len(claude.calls) == 1,
          "asked again, an ungraded call is graded from its kept transcript: no second transcription, no second triage")
    cc.CLASSIFY = fake_classify

    # ---- not scored by the gate
    cc.CLAUDE = FakeClaude(NOT_SCORED)
    r = cc.run(task(one_call("c9")))
    check(r["calls"][0]["status"] == "not_scored" and r["calls"][0]["total"] is None,
          "a call the rubric does not score is kept as not scored, with no number")

    # ---- a caller pinned to a pipeline
    os.environ["AGENTS_COACH_ROLES"] = "Dee=lead_management"
    lm = FakeClaude(REPORT)
    cc.CLAUDE = lm
    cc.run(task(one_call("c10", who="Dee")))
    check(lm.calls and "Lead Management Call Rubric" in lm.calls[-1]["system"][0]["text"],
          "AGENTS_COACH_ROLES pins a caller's calls to one rubric, whatever the triage says")
    os.environ.pop("AGENTS_COACH_ROLES", None)

    # ---- a live box missing the grading key pays for nothing
    cc.CLAUDE = None
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    config.ANTHROPIC_API_KEY = ""
    said.clear()
    r = cc.run(task(one_call("c11")))
    check(r["calls"][0]["status"] == "would_grade" and not said and any("ANTHROPIC_API_KEY" in p for p in r["problems"]),
          "without the grading key a live run transcribes nothing: paying for calls it cannot grade is money for nothing")
    if saved_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = saved_key
    cc.CLAUDE = claude

    # ---- the worker runs it once it is registered
    from . import workflows, worker
    workflows.REGISTRY[cc.WORKFLOW] = cc.WORKFLOW
    check(coachpick.wired("call_coaching"), "registered, the coaching picks stage sees a handler and queues its packs here")
    tw = task(payload)
    worker.run_task(tw)
    done = store.task(tw["id"])
    check(done["status"] == "done" and store.result_of(done).get("run_dir"),
          "the worker runs the workflow, keeps the result and the run folder the file links need")
    config.MODE = "dry"


# ---------------------------------------------------------------------- the weekly KPI report
def _kpi_checks(check, tmp, store, kr, kpi) -> None:
    from .autopilot import activity, capture, ledger
    print("  weekly kpi report")
    now = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc)        # Monday 8am Eastern
    kr.NOW = now
    s, e, label, prob = kr.window("last", now, "America/New_York")
    check((s.isoformat(), e.isoformat(), label, prob) == ("2026-11-09", "2026-11-15", "Nov 9 to Nov 15", ""),
          "Monday's report reads last week, Monday to Sunday")
    check(kr.window("this", now, "America/New_York")[:2] == (now.astimezone(ET).date(), now.astimezone(ET).date())
          and kr.window("2026-11-04", now, "America/New_York")[2] == "Nov 2 to Nov 8"
          and kr.window("someday", now, "America/New_York")[3], "this week, any day's week, and a word it cannot read is said")
    for day, who, dials, ans, conv, talk in (("2026-11-09", "Ada", 160, 120, 6, 3600), ("2026-11-10", "Ada", 100, 80, 4, 2400),
                                             ("2026-11-10", "Ben", 50, 30, 1, 600)):
        capture.save(day, who, {"dials": dials, "answered": ans, "conversations": conv, "talk_s": talk, "owners": dials - 10,
                                "numbers": dials})
    ledger.set_fact("r-lead-1", "owner_uuid", "o-lead-1")
    activity._store([
        call_ev("k1", "o-lead-1", _et(11, 10, 10), kind="answered", secs=120, caller="Ada"),
        status_ev("k2", "r-lead-1", _et(11, 10, 10, 30), "", "new_lead"),
        status_ev("k3", "r-lead-2", _et(11, 11, 12), "", "new_lead"),
        status_ev("k4", "r-lead-1", _et(11, 11, 13), "new_lead", "Hot Lead"),
        phone_ev("k5", "o-lead-1", _et(11, 10, 10, 5), "8655550101", "CORRECT"),
        text_in("k6", "o-text-1", _et(11, 9, 15), "8655550111"),
        task_ev("k7", "r-lead-1", _et(11, 11, 15)),
        card_ev("k8", "r-lead-3", _et(11, 12, 16), "Appointment Booked"),
        status_ev("k9", "r-old", _et(11, 2, 12), "", "new_lead"),
    ])
    for g in ("calls", "state", "board"):
        store.kv_set("autopilot:log:%s:through" % g, now.isoformat(timespec="seconds"))
    t, _ = store.enqueue("ops", "kpi_report", "", subject="kpi", payload={"inputs": {"week": "last"}}, source="schedule")
    tk = store.task(t)
    r = kr.run(tk)
    cat = kpi.load()
    tm = r["team"]
    check(tm["dials"] == 310 and tm["answered"] == 230, "dials and answers are the daily capture's, every caller summed")
    check(tm["conversations"] == 12, "conversations count the capture's calls of a minute and the inbound text, the catalog's way")
    check(r["leads"] == 2 and r["leads_credited"] == {"Ada": 1} and r["leads_unplaced"] == 1,
          "a lead is counted once, credited to the caller whose conversation came before it, and one it cannot place is said")
    check(r["correct"] == {"total": 1, "by_call": 1, "other": 0}, "a number marked correct by the dialer is a correct number found on a call")
    check(r["appointments"] == {"total": 2, "tasks": 1, "cards": 1}, "appointments come from appointment tasks and the Appointment Booked column")
    ada = next(c for c in r["callers"] if c["caller"] == "Ada")
    risk = cat.share("goal.at_risk_share")
    check(ada["calling_days"] == 2 and ada["dials_per_day"] == 130.0
          and ada["marks"]["dials"] == kpi.mark(130.0, cat.value("caller.dials_per_day"), risk),
          "a caller is judged per calling day against the catalog's floor, marked the catalog's way")
    check(r["funnel"]["dials_per_lead"]["value"] == 155.0 and r["funnel"]["dials_per_correct_number"]["value"] == 310.0,
          "the funnel ratios are worked from the week's own numbers")
    check(not r["notes"] or all("log copy" not in n for n in r["notes"][:1]) and any("credited to no caller" in n for n in r["notes"]),
          "a lead placed with nobody is said in the workbook")
    xl = Path(r["run_dir"]) / "files" / r["files"][0]
    from openpyxl import load_workbook
    wb = load_workbook(xl)
    check(xl.name == "kpi_2026-11-09_to_2026-11-15.xlsx"
          and [ws.title for ws in wb.worksheets] == ["Summary", "Callers", "Days", "How it is counted"],
          "the workbook carries the summary, each caller, each day and how it is counted")
    text = kr.render(tk, r)
    lines = text.splitlines()
    check(lines[0] == ":bar_chart: *KPIs, Nov 9 to Nov 15*: 310 dials, 230 answered, 12 conversations, 1 correct number(s), "
                      "2 lead(s), 2 appointment(s)." and len(lines) <= 5,
          "the post is a glance: the week's numbers on one line")
    check(lines[1].startswith("Ada, 2 calling day(s): 130 dials a day (%s)" % ada["marks"]["dials"])
          and "Workbook: https://agents.example.test/files/%d/" % t in text and _no_dashes(text),
          "one line per caller with the marks, and the signed workbook link")
    if r.get("unconfirmed"):
        check("not yet confirmed" in text, "a floor nobody confirmed is said to be unconfirmed")
    saved_url, config.PUBLIC_URL = config.PUBLIC_URL, ""
    check("no public link" in kr.render(tk, r), "with no public address set, the post says where the workbook is")
    config.PUBLIC_URL = saved_url
    store.kv_set("autopilot:log:calls:through", _et(11, 12, 18).isoformat(timespec="seconds"))
    r = kr.run(tk)
    check(any("reads through Nov 12" in n for n in r["notes"]), "a log copy that stops before the week ends is said")
    t2, _ = store.enqueue("ops", "kpi_report", "", subject="kpi", payload={"inputs": {"week": "2026-10-07"}}, source="slack")
    r = kr.run(store.task(t2))
    check(r["team"].get("dials", 0) == 0 and "No calling day is captured" in kr.render(store.task(t2), r),
          "a week with nothing captured says why the numbers are zero")
    from .autopilot import crm as crm_mod
    real = crm_mod.CRM
    crm_mod.CRM = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the KPI report reached DataSift"))
    try:
        kr.run(tk)
        check(True, "the KPI report reads only what the brain already holds: no DataSift request")
    except AssertionError:
        check(False, "the KPI report reads only what the brain already holds: no DataSift request")
    finally:
        crm_mod.CRM = real
    real_load = kpi.load
    kpi.load = lambda p=None: (_ for _ in ()).throw(kpi.KpiError(["the catalog has no figure 'caller.dials_per_day'"]))
    try:
        r = kr.run(tk)
        check(r.get("catalog") == "broken" and "kpi.yaml" in kr.render(tk, r), "a catalog that does not load stops the report out loud")
    finally:
        kpi.load = real_load
    kr.NOW = None


# ---------------------------------------------------------------------- batch QA
def _qa_checks(check, tmp, store, qa) -> None:
    from . import release
    from .autopilot import ledger, limits, policy, scrub
    print("  batch qa")
    now = _et(11, 17, 9, 0).astimezone(timezone.utc)                    # a Tuesday morning
    today = "2026-11-17"
    qa.NOW = now
    stages = {"t_text": policy.Stage("t_text", "textlane", "ops", "plan", "", [], [], {"lane": "ftm"}),
              "t_mail": policy.Stage("t_mail", "mail", "ops", "plan", "", [], [], {}),
              "t_att": policy.Stage("t_att", "attempts", "ops", "plan", "", [], [], {})}
    qa.POLICY = FakePolicy(stages)
    recs = [_rec("r1", "1 Test St", "o1", "Pat", "Example", [_ph("8655550101"), _ph("8655550102")]),
            _rec("r2", "2 Test St", "o2", "E", "Henry", [_ph("8655550103", kind="LANDLINE")]),
            _rec("r3", "3 Test St", "o3", "Smith", "John", [_ph("8655550104")], status="new_lead"),
            _rec("r4", "4 Test St", "o4", "Dee", "Doe", [_ph("8655550101", status="WRONG")]),
            _rec("r5", "5 Test St", "o5", "Lou", "Low", [_ph("8655550105")]),
            _rec("r6", "6 Test St", "o6", "Ann", "Other")]
    crm = QaCRM(recs)
    qa.CRM = crm
    send = _et(11, 17, 11).astimezone(timezone.utc).isoformat(timespec="seconds")

    def text_item(u, owner, first, last, numbers):
        return {"record_uuid": u, "owner_uuid": owner, "street": "%s Test St" % u[1:], "day": today, "series": "opening",
                "step": 1, "system": "smarter_contact", "campaign": True, "audience": "named", "first_name": first,
                "last_name": last, "numbers": [{"number": n, "send_at": send} for n in numbers], "price": 0.02}
    scrub.note("8655550102", True)
    ledger.set_phone("8655550103", 55, "Dial First", "Landline", False)
    limits.stage_text(stage="other", batch_id=777, owner_uuid="o5", record_uuid="r5x", numbers=[{"number": "8655550105"}],
                      system="smarter_contact", series="opening", step=1, touch="other-o5", tz="America/New_York", when=now)
    limits.release_text(777, "r5x", ["8655550105"])
    b1 = ledger.batch_create("t_text", "text", [
        text_item("r1", "o1", "Pat", "Example", ["8655550101", "8655550102"]),
        text_item("r2", "o2", "E", "Henry", ["8655550103"]),
        text_item("r3", "o3", "SMITH JOHN", "", ["8655550104"]),
        text_item("r4", "o4", "Dee", "Doe", ["8655550101"]),
        text_item("r5", "o5", "Lou", "Low", ["8655550105"])], 0.12)
    b2 = ledger.batch_create("t_mail", "mail", [{"record_uuid": "r6", "piece": "handwritten_letter", "name": "By The Chancery Court",
                                                 "first_name": "", "street": "9 Mail Rd", "city": "Knoxville", "state": "TN",
                                                 "zip": "3791", "property_street": "6 Test St"}], 1.89)
    b3 = ledger.batch_create("t_att", "attempts", [{"record_uuid": "r1", "owner_uuid": "o1", "street": "1 Test St, Knoxville",
                                                    "have": 0, "want": 1}], 0)
    b4 = ledger.batch_create("t_text", "text", [text_item("r1", "o1", "Pat", "Example", ["8655550101"])], 0.02)
    calls_before = list(release.RELEASE)

    def ask(batch, source="slack"):
        tid, _ = store.enqueue("ops", "qa_batch", "", subject="qa", payload={"inputs": {"batch": str(batch)}}, source=source)
        return store.task(tid)
    real_run = release.run
    release.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the QA released a batch"))
    try:
        os.environ["AGENTS_QA_MAX_READS"] = "2"
        t = ask(b1)
        r = qa.run(t)
        one = r["batches"][0]
        check(one["read"] == 2 and one["checkable"] == 5 and crm.reads == ["r1", "r2"],
              "reads are bounded: the first record, then the gate on as many as the cap allows")
        os.environ["AGENTS_QA_MAX_READS"] = "60"
        crm.reads.clear()
        t = ask(b1)
        r = qa.run(t)
        f = r["batches"][0]["findings"]
        check(r["batches"][0]["readback"]["ok"] is True and r["batches"][0]["read"] == 5,
              "the first record is read back and matches what the batch holds; every record is checked when the cap allows")
        check(f.get("a number on the do-not-call registry (never texted)") == 1, "a number the registry flagged is named: it is never texted")
        check(f.get("a landline or other line that cannot take a text") == 1,
              "a landline on a text is named once, whether the scoring cache or the record says so")
        check(f.get("a name that should not reach a person: an initial, not a first name") == 1
              and f.get("a name that should not reach a person: several words in the first-name field") == 1,
              "junk first names Smarter Contact would print raw are named")
        check(f.get("the gate now holds the record: already a lead (new_lead)") == 1, "a record that became a lead since staging is named")
        check(f.get("the same number is in the batch twice") == 2 and f.get("a number marked wrong, dead or do-not-call") == 1,
              "a number twice in the batch, and a number now marked wrong, are named")
        check(f.get("the house limits refuse it: the owner already has a text today") == 1,
              "the house limits are asked: an owner already texted today is named")
        check(f.get("the record also waits in another staged batch") == 1 and f.get("a number also waits in another staged batch") == 2,
              "a record or a number already waiting in another staged batch is named (the number on both texts that carry it)")
        md = (Path(r["run_dir"]) / "files" / r["files"][0]).read_text(encoding="utf-8")
        check("0102" in md and "8655550102" not in md and "Nothing here was released" in md,
              "the detail shows a number's last four digits only, and says nothing was released")
        text = qa.render(t, r)
        check(text.startswith(":mag: Batch %d, 5 owner text(s) from t text:" % b1) and "Nothing was released" in text
              and len(text.splitlines()) <= 4 and _no_dashes(text), "the post is one line a batch, and says nothing was released")
        r = qa.run(ask(b2))
        f = r["batches"][0]["findings"]
        check(f.get("a name that should not reach a person: notice or court wording in the name") == 1
              and f.get("a mailing ZIP that is not five digits") == 1
              and f.get("read-back of the first record: the owner's name on the record changed since the batch was staged") == 1,
              "a mail batch: court wording on the envelope, a bad ZIP, and a read-back that does not match")
        crm.reads.clear()
        r = qa.run(ask(b3))
        check(r["batches"][0]["touch"] is False and crm.reads == ["r1"] and r["batches"][0]["readback"]["ok"],
              "a batch that is not a touch gets the read-back and the duplicate checks, not the gate")
        check(all(ledger.batch(b)["status"] == "staged" for b in (b1, b2, b3, b4)) and list(release.RELEASE) == calls_before,
              "every batch is still staged afterwards: the QA never releases, and DataSift saw reads only")
        crm.recs["r1"]["address"]["street"] = "100 Other Rd"
        r = qa.run(ask(b4))
        check(any("the street on the record is 100 Other Rd" in k for k in r["batches"][0]["findings"]),
              "a read-back that finds a different street says so")
        crm.recs["r1"]["address"]["street"] = "1 Test St"
        b5 = ledger.batch_create("t_text", "text", [text_item("r5", "o5", "Lou", "Low", ["8655550105"])], 0.02)
        r = qa.run(ask("all", source="schedule"))
        check([x["batch"] for x in r["batches"]] == [b5] and r["unchanged"] == 4,
              "on its schedule it checks only a batch nobody has looked at in this form; one already asked about is not posted again")
        r = qa.run(ask("all", source="schedule"))
        check(not r["batches"] and r["unchanged"] == 5 and qa.render({"source": "schedule"}, r) == "",
              "a scheduled run with nothing new says nothing")
        items = ledger.batch(b5)["items"] + [text_item("r2", "o2", "E", "Henry", ["8655550103"])]
        ledger.batch_items_set(b5, items)
        r = qa.run(ask("all", source="schedule"))
        check([x["batch"] for x in r["batches"]] == [b5], "a batch whose items changed is checked again on the schedule")
        r = qa.run(ask(999))
        check("There is no batch 999" in qa.render({}, r), "a batch number that does not exist is said, not guessed")
        ledger.batch_set(b4, "sent", {})
        r = qa.run(ask(b4))
        check("is sent" in qa.render({}, r), "a batch already released is not checked again")
    finally:
        release.run = real_run
        qa.NOW = None


# ---------------------------------------------------------------------- outreach health
def _outreach_checks(check, tmp, store, oh) -> None:
    from .autopilot import activity, ledger, policy
    print("  outreach health")
    now = _et(11, 18, 8, 5).astimezone(timezone.utc)
    oh.NOW = now
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_batches")
        c.execute("DELETE FROM ap_log WHERE event_type LIKE 'owner.call.%'")
    stages = {"t_audit": policy.Stage("t_audit", "audit", "ops", "plan", "", [], [], {}),
              "t_scrub": policy.Stage("t_scrub", "scrub", "ops", "plan", "", [], [], {"calls": "hold"})}
    oh.POLICY = FakePolicy(stages)
    rows = []
    for i in range(20):
        rows.append(call_ev("l1-%d" % i, "o-l1-%d" % i, _et(11, 16, 10, i), line="8655550201"))
        rows.append(call_ev("l2-%d" % i, "o-l2-%d" % i, _et(11, 16, 11, i), line="8655550202"))
    for i in range(2):
        rows.append(call_ev("l1a-%d" % i, "o-l1-%d" % i, _et(11, 16, 10, i), kind="answered", line="8655550201", secs=50))
    for i in range(15):
        rows.append(call_ev("l2a-%d" % i, "o-l2-%d" % i, _et(11, 16, 11, i), kind="answered", line="8655550202", secs=90))
    for i in range(5):
        rows.append(call_ev("l3-%d" % i, "o-l3-%d" % i, _et(11, 16, 12, i), line="8655550203"))
    activity._store(rows)
    store.kv_set("autopilot:log:calls:through", now.isoformat(timespec="seconds"))
    store.kv_set("autopilot:scrub:t_scrub", json.dumps({"day": "2026-11-17", "flagged": 4, "shown": 10, "share": 40}))

    def ask(source="slack"):
        tid, _ = store.enqueue("ops", "outreach_health", "", subject="oh", payload={}, source=source)
        return store.task(tid)
    t = ask("schedule")
    r = oh.run(t)
    lines = {x["line"]: x for x in r["lines"]}
    check(lines["0201"]["state"] == "rest" and "answered on 10% of 20 dials" in lines["0201"]["why"]
          and lines["0202"]["state"] == "fine" and lines["0203"]["state"] == "too few dials to judge",
          "each caller ID line is read from the log copy: a line answered on 10% of its dials is one to rest")
    text = oh.render(t, r)
    check(text.startswith(":satellite_antenna: *Outreach health, Nov 18*:") and "Rest the line ending 0201" in text
          and "texting side" in text and len(text.splitlines()) <= 4 and _no_dashes(text),
          "the post names the line to rest and says plainly that delivery is read on the texting side")
    check("8655550201" not in text and "8655550201" not in (Path(r["run_dir"]) / "files" / r["files"][0]).read_text(encoding="utf-8"),
          "a line is named by its last four digits only, in the post and in the file")
    body = (Path(r["run_dir"]) / "files" / r["files"][0]).read_text(encoding="utf-8")
    check("What the brain cannot see, and where to read it" in body and "deliverability" in body and "Smarter Contact" in body,
          "the file says what only the texting side can see and how to read it there")
    r = oh.run(ask("schedule"))
    check(r["quiet"] and oh.render({}, r) == "", "on its schedule the same finding is not said again the next morning")
    store.kv_set("autopilot:last:t_audit", json.dumps({"at": now.isoformat(), "acted": 3, "head": "Yesterday: 45 dial(s).",
                                                       "problems": ["3: dialed an owner the gate holds: tagged Do Not Market"]}))
    day = (now.astimezone(ET).date() - timedelta(days=1)).isoformat()
    store.kv_set("autopilot:limits:day:%s" % day, json.dumps({"breaches": {"a caller ID line carried more than 25 dials": 2},
                                                              "totals": {"dials": 45, "lines": 3, "texts": 0}}))
    ledger.batch_create("t_text", "text", [{"record_uuid": "r9", "owner_uuid": "o9", "day": "2026-11-16", "numbers": []}], 0.02)
    r = oh.run(ask("schedule"))
    text = oh.render({}, r)
    check(not r["quiet"] and "2 limit(s) broken Nov 17" in text and "touch audit 3 finding(s)" in text and "1 batch(es) waiting" in text,
          "a new finding is said: the limits broken, the touch audit and the batches waiting")
    check(any("staged for Nov 16 and will expire unreleased" in f for f in r["findings"]),
          "a text batch staged for an earlier day is named before it expires unreleased")
    store.kv_set("autopilot:last:t_audit", json.dumps({"at": (now - timedelta(days=3)).isoformat(), "acted": 3, "head": "old",
                                                       "problems": ["3: dialed an owner the gate holds: tagged Do Not Market"]}))
    r = oh.run(ask("slack"))
    body = (Path(r["run_dir"]) / "files" / r["files"][0]).read_text(encoding="utf-8")
    check(not any("touch audit" in f for f in r["findings"]) and "has not run in the last 36 hours" in body,
          "a touch audit that stopped running adds nothing: last week's findings are not this morning's")
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_log WHERE event_type LIKE 'owner.call.%'")
        c.execute("DELETE FROM ap_batches")
    store.kv_set("autopilot:last:t_audit", "{}")
    store.kv_set("autopilot:limits:day:%s" % day, json.dumps({"breaches": {}, "totals": {}}))
    r = oh.run(ask("slack"))
    check(not r["quiet"] and not r["findings"] and oh.render({}, r).startswith(":satellite_antenna:"),
          "asked in Slack it always answers, even when nothing needs a person")
    oh.NOW = None


# ---------------------------------------------------------------------- the registry, the roster, the decision
def _registry_checks(check, workflows) -> None:
    print("  registered workflows")
    for name in ("call_coaching", "kpi_report", "qa_batch", "outreach_health"):
        workflows.REGISTRY[name] = name
    check(workflows.problems() == [], "each workflow module loads with run and render: %s" % (workflows.problems() or "ok"))


def _decision_checks(check) -> None:
    from . import scheduler, team
    print("  daily_digest decision")
    roster = team.load()
    ad = roster.workflows.get("autopilot_digest")
    check(ad is not None and ad.owner == "ops" and ad.live and ad.autopilot and "slack" in ad.triggers
          and any(t.startswith("schedule:08:00") for t in ad.triggers),
          "the morning readout already runs on request: autopilot_digest is Quinn's live workflow with a Slack trigger and the 08:00 slot")
    dd = roster.workflows.get("daily_digest")
    check(dd is None or not dd.live, "daily_digest is not live, so the clock never fires a second 08:00 post beside the digest")
    for spec in ("Mon 08:00 America/New_York", "every 60 min 08:00-18:00 Mon-Fri America/New_York", "08:05 Mon-Fri America/New_York"):
        try:
            scheduler.parse(spec)
            ok = True
        except scheduler.ScheduleError:
            ok = False
        check(ok, "the proposed trigger %r is one the clock reads" % spec)


def _hygiene_checks(check) -> None:
    print("  source hygiene")
    src = Path(__file__).resolve().parents[1]
    mine = [src / "call_coaching" / n for n in ("grade.py", "transcribe.py", "pull_calls.py")]
    mine += [src / "agents" / "workflows" / n for n in ("call_coaching.py", "kpi_report.py", "qa_batch.py", "outreach_health.py")]
    mine.append(Path(__file__))
    texts = {p.name: p.read_text(encoding="utf-8") for p in mine}
    check(all(_no_dashes(t) for t in texts.values()), "no long dash or en dash in any file this track wrote")
    check(all("\x08" not in t for t in texts.values()), "no backspace byte in any file this track wrote")
    check("Deal Room" not in texts["pull_calls.py"].split('"""', 2)[-1],
          "the call puller no longer points at the Deal Room checkout in its code")
