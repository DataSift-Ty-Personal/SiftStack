"""The issue fixer's watch (tools/issue_fixer/watch.py): the three outside checks on time, one post per
watchdog a day shared with the late GitHub backup through the relay, and the log watch. Zero network: every
outside call is a fake. Invented names, example domains and 555 numbers only.

Run: python -m pytest tests/test_issue_fixer_watch.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "issue_fixer"))

import fixer as F  # noqa: E402
import watch as W  # noqa: E402

# 2026-11-20 14:35 UTC is 9:35 in the morning, business time.
MORNING = datetime(2026, 11, 20, 14, 35, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ISSUE_FIXER", "SLACK_", "AGENTS_SLACK", "SIFTSTACK_MAIN", "AGENTS_RELAY")):
            monkeypatch.delenv(k, raising=False)
    main = tmp_path / "main"
    (main / "output").mkdir(parents=True)
    (main / ".env").write_text("SLACK_BOT_TOKEN=x\nAGENTS_SLACK_ISSUES_CHANNEL=C1\n", encoding="utf-8")
    monkeypatch.setattr(F, "MAIN", main)
    monkeypatch.setattr(F, "STATE", main / "output" / "issue_fixer")
    monkeypatch.setattr(F, "now_utc", lambda: MORNING)
    return tmp_path


def settings(**env):
    base = {"SLACK_BOT_TOKEN": "x", "AGENTS_SLACK_ISSUES_CHANNEL": "C1",
            "ISSUE_FIXER_RELAY_URL": "https://brain.example.com/hooks/slack/relay-secret-1/watchdog"}
    base.update(env)
    return F.Settings(base)


class FakeWorld(W.World):
    """The checks answer from a script; the relay, the channel, the logs and the fixer's ledger are lists."""

    def __init__(self, st, verdicts=None, relay_ok=True, channel=None, pages=None, ledger=None):
        super().__init__(st, sh=None, slack=object())
        self.verdicts = verdicts or {}
        self.relay_ok, self.channel = relay_ok, list(channel or [])
        self.pages, self.ledger = pages or {}, ledger or {"issues": {}}
        self.relayed, self.posts, self.checked, self.asked_logs = [], [], [], []

    def check(self, name):
        self.checked.append(name)
        v = self.verdicts.get(name, ("ok", "fine"))
        if isinstance(v, Exception):
            raise v
        return v

    def relay(self, url, text):
        if not self.relay_ok:
            return False, "connection refused"
        self.relayed.append(text)
        return True, "HTTP 200"

    def today_texts(self, now):
        return list(self.channel)

    def post(self, text, username):
        self.posts.append({"text": text, "username": username})
        self.channel.append(text)
        return "1.1"

    def logs_page(self, app, token):
        self.asked_logs.append((app, token))
        queue = self.pages.get(app) or []
        if isinstance(queue, Exception):
            raise queue
        if not queue:
            return [], token
        rows = queue.pop(0)
        return rows, rows[-1]["next"] if rows and rows[-1].get("next") else str(int(token or 0) + 1)

    def fixer_ledger(self):
        return self.ledger


def tick(world, now, **kw):
    return W.tick(now=now, st=world.st, world=world, **kw)


# ---------------------------------------------------------------------- the verdicts
def test_ftm_verdict_reads_the_printed_line_never_the_exit_code():
    out = ("\x1b[2mConnecting to fdaa:0:1::2...\x1b[0m\n"
           "RED 2026-11-20 last_run=2026-11-20T11:30:47 :: Stage liens FAILED: the recorder dropped the line\n"
           "Error: ssh shell: Process exited with status 1")
    assert W.ftm_verdict(1, out) == ("red", "RED 2026-11-20 last_run=2026-11-20T11:30:47 :: Stage liens FAILED: "
                                            "the recorder dropped the line")
    assert W.ftm_verdict(0, "GREEN 2026-11-20 last_run=x :: all clear")[0] == "ok"
    assert W.ftm_verdict(1, "AMBER 2026-11-20 last_run=x :: a feed is quiet")[0] == "ok", "amber is the all-clear"
    status, detail = W.ftm_verdict(1, "Connecting to fdaa...\nError: token xoxb-123-456-abcdef rejected")
    assert status == "unreachable" and "flyctl exit 1" in detail and "xoxb" not in detail, "a box that cannot be reached is red"


def test_sms_verdict_is_the_workflows():
    assert W.sms_verdict(200, '{"ok": true}')[0] == "ok"
    status, detail = W.sms_verdict(503, '{"ok": false, "minutes_ago": 41.5}')
    assert status == "stale" and "41.5 minutes" in detail
    assert "? minutes" in W.sms_verdict(500, "not json")[1]


def test_the_brain_is_judged_by_the_github_workflows_own_script(monkeypatch):
    hb = W.heartbeat()
    assert hb.__file__.replace("\\", "/").endswith("deploy/agents_heartbeat.py")
    page = {"ok": True, "worker_alive": False, "clock_alive": True, "autopilot": {}}
    monkeypatch.setattr(hb, "fetch", lambda url: (page, None))
    monkeypatch.setattr(W, "heartbeat", lambda: hb)
    w = W.World(settings(), slack=False)
    assert w.check_agents() == ("red", "The worker thread is dead, so nothing queued will run.")
    monkeypatch.setattr(hb, "fetch", lambda url: (None, ("unreachable", "The health page did not answer (x).")))
    assert w.check_agents()[0] == "unreachable"


def test_the_texting_check_reads_an_http_error_as_an_answer(monkeypatch):
    monkeypatch.setattr(W, "http_get", lambda url, timeout=25: (503, '{"minutes_ago": 12}'))
    assert W.World(settings(), slack=False).check_sms()[0] == "stale"

    def down(url, timeout=25):
        raise OSError("connection refused")
    monkeypatch.setattr(W, "http_get", down)
    status, detail = W.World(settings(), slack=False).check_sms()
    assert status == "unreachable" and "did not respond at all" in detail


# ---------------------------------------------------------------------- on time
def test_each_check_runs_on_its_own_clock():
    ws = {}
    assert W.due("sms", ws, MORNING) and W.due("agents", ws, MORNING) and W.due("ftm", ws, MORNING)
    ws = {"last_at": (MORNING - timedelta(minutes=5)).isoformat()}
    assert W.due("sms", ws, MORNING) and not W.due("agents", ws, MORNING), "the brain every 15 minutes"
    assert W.due("agents", dict(ws, streak=1), MORNING), "a bad verdict is checked again in 5 minutes"
    early = datetime(2026, 11, 20, 14, 10, tzinfo=timezone.utc)          # 9:10 business time
    assert not W.due("ftm", {}, early), "first-to-market waits for 9:30: the run and its 8:00 digest come first"
    assert not W.due("ftm", {"settled_day": "2026-11-20"}, MORNING), "and is settled once a day"
    assert W.due("ftm", {"settled_day": "2026-11-19"}, MORNING)


def test_a_red_is_confirmed_said_once_a_day_and_again_the_next_day():
    w = FakeWorld(settings(), verdicts={"agents": ("unreachable", "The health page did not answer (timed out).")})
    tick(w, MORNING)
    assert w.relayed == [], "one bad check is not an outage: a deploy's restart looks the same"
    tick(w, MORNING + timedelta(minutes=5))
    assert len(w.relayed) == 1 and w.relayed[0].startswith("*Agents brain is unreachable*")
    for m in (10, 15, 30, 60):
        tick(w, MORNING + timedelta(minutes=m))
    assert len(w.relayed) == 1, "once per watchdog per business day"
    tick(w, MORNING + timedelta(days=1))
    assert len(w.relayed) == 2, "still red the next day is said again"


def test_a_recovery_resets_the_count():
    w = FakeWorld(settings(), verdicts={"sms": ("stale", "The worker has not run a pass in 9 minutes.")})
    tick(w, MORNING)
    w.verdicts["sms"] = ("ok", "fine")
    tick(w, MORNING + timedelta(minutes=5))
    w.verdicts["sms"] = ("stale", "again")
    tick(w, MORNING + timedelta(minutes=10))
    assert w.relayed == [] and W.load_state()["watchdogs"]["sms"]["streak"] == 1


def test_a_check_that_cannot_run_is_a_box_nobody_can_see():
    w = FakeWorld(settings(), verdicts={"ftm": RuntimeError("fly is not installed")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    assert len(w.relayed) == 1 and w.relayed[0].startswith("*FTM pull is unreachable*")


def test_first_to_market_checks_once_a_day_and_settles():
    w = FakeWorld(settings(), verdicts={"ftm": ("ok", "GREEN 2026-11-20 last_run=x :: all clear")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    tick(w, MORNING + timedelta(hours=3))
    assert w.checked.count("ftm") == 1, "a green morning is settled for the day"


def test_when_the_relay_is_down_the_post_goes_straight_to_the_channel_once():
    w = FakeWorld(settings(), relay_ok=False, verdicts={"agents": ("unreachable", "The health page did not answer.")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    assert len(w.posts) == 1 and w.posts[0]["username"] == W.WATCH_NAME
    assert w.posts[0]["text"].startswith("Agents brain watchdog: the brain is unreachable.")
    assert w.posts[0]["username"] != F.USERNAME, "the fixer sorts the post: it is not the fixer's own"


def test_the_box_never_repeats_what_the_channel_already_has_today():
    w = FakeWorld(settings(ISSUE_FIXER_RELAY_URL=""), channel=[
        "Texting app watchdog: the app is stale. The receiver answered but the worker has not run a pass."],
        verdicts={"sms": ("stale", "again")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    assert w.posts == [] and W.load_state()["watchdogs"]["sms"]["posted_how"] == "already"


def test_without_the_relay_a_first_to_market_post_respects_the_one_problem_a_day():
    w = FakeWorld(settings(ISSUE_FIXER_RELAY_URL=""), channel=["First-to-market run: liens failed"],
                  verdicts={"ftm": ("red", "RED 2026-11-20 last_run=x :: Stage liens FAILED")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    assert w.posts == [], "the run report already said first-to-market was broken today"


def test_the_relay_check_says_whether_the_brain_reads_a_watchdog(capsys):
    relay = _relay()

    class Ask(FakeWorld):
        def __init__(self, st, answer):
            super().__init__(st)
            self.answer, self.asked = answer, []

        def relay_dry(self, url, text):
            self.asked.append((url, text))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer(text) if callable(self.answer) else self.answer

    new = Ask(settings(), lambda text: {"what": relay.classify(text), "posts": [
        {"kind": p["kind"], "group": p.get("group")} for p in relay.route(text)]})
    assert W.relay_check(new) == 0 and "relay ok" in capsys.readouterr().out
    assert new.asked[0][1].startswith("*Agents brain is red*")
    old = Ask(settings(), {"what": "other", "posts": [{"kind": "chat"}]})
    assert W.relay_check(old) == 1 and "deploy the brain first" in capsys.readouterr().out
    gone = Ask(settings(), OSError("connection refused"))
    assert W.relay_check(gone) == 1
    assert W.relay_check(Ask(settings(ISSUE_FIXER_RELAY_URL=""), {})) == 2


def test_the_relay_url_comes_from_the_relay_secret_too(monkeypatch):
    w = W.World(settings(ISSUE_FIXER_RELAY_URL=""), slack=False)
    assert w.relay_url() == ""
    monkeypatch.setenv("AGENTS_RELAY_SECRET", "relay-secret-2")
    assert w.relay_url() == "https://siftstack-agents.fly.dev/hooks/slack/relay-secret-2/watchdog"


# ---------------------------------------------------------------------- the pair: the box and the late backup
def _relay():
    os.environ.setdefault("AGENTS_DATA_DIR", tempfile.mkdtemp(prefix="watch_relay_"))
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from agents import relay
    return relay


def test_the_relay_reads_the_box_and_keeps_its_markers():
    relay = _relay()
    kinds = {"ftm": "ftm_watchdog", "agents": "agents_watchdog", "sms": "sms_watchdog"}
    for name, kind in kinds.items():
        for status in ("red", "unreachable", "stale"):
            raw = W.raw_message(name, status, "RED 2026-11-20 last_run=x :: Something broke.")
            assert relay.classify(raw) == kind
            out = relay.route(raw)
            lead, _subject, _group, marker = relay.WATCHDOGS[kind]
            assert out[0]["kind"] == "issues" and out[0]["text"].startswith(lead)
            assert W.WATCHDOGS[name]["lead"] == lead and W.WATCHDOGS[name]["marker"] == marker
            direct = W.direct_text(name, status, "RED 2026-11-20 last_run=x :: Something broke.")
            assert direct.split(". ", 1)[0] == out[0]["text"].split(". ", 1)[0], "the same words whichever way it went"
            assert W.said_today([direct], marker) and W.said_today([out[0]["text"]], marker)


def test_the_github_workflows_post_only_through_the_relay_in_words_it_reads():
    relay = _relay()
    wf = ROOT / ".github" / "workflows"
    heads = {"ftm-heartbeat.yml": "FTM pull", "agents-heartbeat.yml": "Agents brain", "sms-agent-heartbeat.yml": "SMS agent"}
    for name, head in heads.items():
        text = (wf / name).read_text(encoding="utf-8")
        hooks = set(re.findall(r"SLACK_WEBHOOK:\s*(.+)", text))
        assert hooks == {"${{ secrets.FTM_HEALTH_WEBHOOK }}"}, "%s posts through the relay and nothing else" % name
        assert "*%s is " % head in text
        assert relay.classify("*%s is red*\nx" % head) != "other"
    assert "--check --no-ledger-update" in (wf / "ftm-heartbeat.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- masking
def test_masking_keeps_secrets_people_and_places_out():
    line = ("ERROR crm: POST https://hooks.slack.com/services/T000/B000/abcdefghijkl failed for pat@example.com "
            "(865) 555-0101 at 12 Example St, token=abcd1234efgh, header Bearer fm2_abc123xyz, key sk-ant-api03-"
            "abcdefghijklmnopqrstu, jwt eyJhbGci.eyJzdWIi.c2lnbmF0dXJl, record 0d6f7c2e-1b2a-4c3d-9e8f-123456789abc, "
            "relay https://brain.example.com/hooks/slack/relay-secret-9/ftm, slack xoxb-1-2-abcdef, hex "
            "0123456789abcdef0123456789abcdef")
    out = W.mask(line)
    for bad in ("abcdefghijkl", "pat@example.com", "555-0101", "12 Example St", "abcd1234efgh", "fm2_abc123xyz",
                "sk-ant", "eyJhbGci", "0d6f7c2e", "relay-secret-9", "xoxb-1", "0123456789abcdef0123"):
        assert bad not in out, bad
    for kept in ("[email]", "[phone]", "[address]", "[uuid]", "[secret]", "ERROR crm: POST"):
        assert kept in out
    assert W.mask("KeyError: 'owner' in src/agents/relay.py deliver") == "KeyError: 'owner' in src/agents/relay.py deliver"
    assert W.mask("headers {'X-Api-Key': 'abc123def456', \"password\": \"hunter22\"}") == \
        "headers {'X-Api-Key': '[secret]', \"password\": \"[secret]\"}"
    assert "[secret]" in W.mask("authorization: Api-Key 1234567890abcdef")
    assert W.mask("pass in 41 minutes, line 241, in deliver") == "pass in 41 minutes, line 241, in deliver"


# ---------------------------------------------------------------------- reading the logs
def row(msg, at="2026-11-20T14:30:00.123456789Z", inst="m1", provider="app", nxt=""):
    r = {"message": msg, "timestamp": at, "instance": inst, "meta": {"event": {"provider": provider}}}
    if nxt:
        r["next"] = nxt
    return r


TRACE = [
    "2026-11-20 14:30:00,100 ERROR agents.relay: relay failed for a message from watchdog: boom",
    "Traceback (most recent call last):",
    '  File "/app/src/agents/relay.py", line 241, in deliver',
    "    ts = slack_app.post(channel, written)",
    '  File "/usr/local/lib/python3.12/site-packages/requests/api.py", line 59, in request',
    "    raise ConnectionError(e)",
    "ConnectionError: the Slack host did not answer",
    "2026-11-20 14:30:01,000 INFO agents.scheduler: next slot",
]


def test_a_traceback_is_one_crash_with_the_error_line_it_was_logged_with():
    keep = {}
    ev = W.scan([row(m) for m in TRACE], keep)
    assert len(ev) == 1 and ev[0]["kind"] == "crash"
    assert ev[0]["exc"] == "ConnectionError: the Slack host did not answer"
    assert (ev[0]["file"], ev[0]["func"]) == ("src/agents/relay.py", "deliver"), "our own frame, not the library's"
    assert ev[0]["pre"].startswith("agents.relay: relay failed")


def test_a_traceback_cut_by_a_page_is_still_one_crash():
    keep = {}
    first = W.scan([row(m) for m in TRACE[:4]], keep)
    second = W.scan([row(m) for m in TRACE[4:]], keep)
    assert first == [] and len(second) == 1 and second[0]["exc"].startswith("ConnectionError")


def test_a_chained_traceback_is_one_crash_with_the_last_exception():
    lines = ["Traceback (most recent call last):", '  File "/app/src/x.py", line 1, in a', "    a()",
             "ValueError: first", "", "During handling of the above exception, another exception occurred:", "",
             "Traceback (most recent call last):", '  File "/app/src/x.py", line 9, in b', "    b()",
             "KeyError: 'second'", "an ordinary line"]
    ev = W.scan([row(m) for m in lines], {})
    assert len(ev) == 1 and ev[0]["exc"] == "KeyError: 'second'" and ev[0]["func"] == "b"


def html_crash(logged_by):
    """The 2026-11-18 shape: an exception whose message is a whole HTML page, logged with its traceback."""
    rid = "0d6f7c2e-1b2a-4c3d-9e8f-123456789abc"
    return [
        "2026-11-18 21:58:39,100 ERROR %s: HTTP 404 on POST /api/internal/property/%s/add-notes/: <!doctype html>"
        % (logged_by, rid),
        '<html lang="en">', "<head>", "</head>", "</html>",
        "Traceback (most recent call last):",
        '  File "/app/src/datasift_api_upload.py", line 221, in call',
        "urllib.error.HTTPError: HTTP Error 404: Not Found", "",
        "During handling of the above exception, another exception occurred:", "",
        "Traceback (most recent call last):",
        '  File "/app/src/agents/worker.py", line 116, in _direct',
        '  File "/app/src/datasift_api_upload.py", line 235, in call',
        "    raise ApiError(e.code, method, path, text)",
        "datasift_api_upload.ApiError: HTTP 404 on POST /api/internal/property/%s/add-notes/:" % rid,
        "<!doctype html>", '<html lang="en">', "</html>",
        "2026-11-18 21:58:40,574 INFO httpx: HTTP Request: POST https://api.example.com/v1/messages"]


def test_an_exception_that_carries_a_page_is_still_one_crash_with_its_error_line():
    at = "2026-11-18T21:58:39.%06dZ"
    ev = W.scan([row(m, at=at % i) for i, m in enumerate(html_crash("agents.relay: relay failed for a message"))], {})
    assert len(ev) == 1 and ev[0]["kind"] == "crash", ev
    assert ev[0]["exc"].startswith("datasift_api_upload.ApiError: HTTP 404") and ev[0]["file"] == "src/datasift_api_upload.py"
    assert ev[0]["pre"].startswith("agents.relay: relay failed"), "the page between them does not cut the error line off"
    task = W.scan([row(m, at=at % i) for i, m in enumerate(html_crash("agents.worker: task 1691 failed"))], {})
    assert len(task) == 1 and W.noise_of(task[0]) == "task_failed", "the brain already posts every failed task itself"


def test_an_error_line_long_before_a_traceback_is_its_own_event():
    rows = [row("2026-11-20 14:00:00,000 ERROR sms_agent.crm: one thing", at="2026-11-20T14:00:00Z"),
            row("Traceback (most recent call last):", at="2026-11-20T14:00:09Z"),
            row('  File "/app/src/sms_agent/worker.py", line 3, in f', at="2026-11-20T14:00:09Z"),
            row("ValueError: another thing", at="2026-11-20T14:00:09Z"),
            row("2026-11-20 14:00:10,000 INFO later", at="2026-11-20T14:00:10Z")]
    ev = W.scan(rows, {})
    assert [e["kind"] for e in ev] == ["error", "crash"] and ev[1]["pre"] == ""


def test_error_levels_in_every_format_and_nothing_else():
    msgs = ["2026-11-20 11:38:49,970 [ERROR] address_lookup: the county API refused the search",
            "2026-11-20 13:44:04,390 CRITICAL vendor.api: the vendor is down",
            "ERROR:    Exception in ASGI application",
            "2026-11-20 13:44:04,390 WARNING agents.research: Serper refused the search",
            "2026-11-20 13:44:04,390 INFO sms_agent.worker: pass: {'failed': 0}",
            "  *liens* [FAILED] the stage failed"]
    keep = {}
    ev = W.scan([row(m, inst="m%d" % i) for i, m in enumerate(msgs)], keep) + W.flush(keep, MORNING, force=True)
    assert sorted(e["text"] for e in ev) == ["Exception in ASGI application", "address_lookup: the county API refused the search",
                                             "vendor.api: the vendor is down"]


def test_our_printed_failures_count_too():
    """The uploader prints a refused note with no level (one line a record, for days on a retired route)."""
    msgs = ["  NOTE NOT POSTED 1 Test Way HTTP 404 on POST /api/internal/property/0d6f7c2e-1b2a-4c3d-9e8f-"
            "123456789abc/add-notes/", "  FAIL 12 Sample Rd: the create route answered 403",
            "STOPPED: 9 Example St did not read back (status)", "  *liens* [FAILED] the stage failed",
            "   12/40 created=10 fields=10 failed=2 33s"]
    keep = {}
    ev = W.scan([row(m, inst="m%d" % i) for i, m in enumerate(msgs)], keep) + W.flush(keep, MORNING, force=True)
    texts = sorted(W.canonical(e) for e in ev)
    assert texts == ["FAIL: [address]: the create route answered 403",
                     "NOTE NOT POSTED: [address] HTTP 404 on POST /api/internal/property/[uuid]/add-notes/",
                     "STOPPED: [address] did not read back (status)"], texts


def test_platform_lines_and_known_noise_are_not_issues():
    keep = {}
    rows = [row("Configuring firecracker", provider="runner"),
            row("Traceback (most recent call last):"), row('  File "/app/src/ftm_schedule.py", line 262, in main'),
            row("    time.sleep(300)"), row("KeyboardInterrupt"), row("restart done"),
            row("2026-11-20 13:44:04,390 CRITICAL scrapfly.api: <-- 422 | ERR::SCRAPE::DNS_NAME_NOT_RESOLVED", inst="m2"),
            row("2026-11-20 13:44:05,000 INFO later", inst="m2")]
    events = W.scan(rows, keep)
    lw = W.load_state()["logs"]
    assert W.fold_events(lw, "siftstack-ftm", events, MORNING) == 0
    assert lw["noise"] == {"restart": 1, "scrapfly": 1}
    worker = [row(m) for m in ("2026-11-20 14:00:00,000 ERROR agents.worker: task 2277 failed: boom",
                               "Traceback (most recent call last):", '  File "/app/src/agents/worker.py", line 9, in _direct',
                               "RuntimeError: boom", "next line")]
    assert W.fold_events(lw, "siftstack-agents", W.scan(worker, {}), MORNING) == 0, \
        "the brain posts every failed task itself"


def logs_world(lines_by_tick, **kw):
    """A world whose siftstack-agents logs hold one page per tick."""
    pages = {"siftstack-agents": [], "siftstack-ftm": [], "siftstack": []}
    st = settings(ISSUE_FIXER_WATCH="off")
    w = FakeWorld(st, pages=pages, **kw)
    w.ticks = lines_by_tick
    return w


def feed(w, rows):
    w.pages["siftstack-agents"].append(rows)


ERR = "2026-11-20 14:%02d:00,000 ERROR sms_agent.crm: the add-notes route answered 404 for record %s"


def test_an_error_line_is_an_issue_at_three_a_day_and_said_once():
    w = logs_world([])
    tick(w, MORNING)                                   # the first read starts now: nothing from before
    assert all(t == W.ns(MORNING) for _app, t in w.asked_logs)
    for i, minute in enumerate((15, 30)):
        feed(w, [row(ERR % (minute, "0d6f7c2e-1b2a-4c3d-9e8f-12345678900%d" % i)), row("INFO next")])
        tick(w, MORNING + timedelta(minutes=minute))
    assert w.posts == [], "two in a day is not an issue yet"
    feed(w, [row(ERR % (45, "0d6f7c2e-1b2a-4c3d-9e8f-123456789009")), row("INFO next")])
    tick(w, MORNING + timedelta(minutes=45))
    assert len(w.posts) == 1 and w.posts[0]["username"] == W.LOG_NAME
    text = w.posts[0]["text"]
    assert text.startswith("Log watch: the agents brain keeps logging the same error (3 times today")
    assert "[uuid]" in text and "0d6f7c2e" not in text and "fly logs -a siftstack-agents" in text
    feed(w, [row(ERR % (59, "0d6f7c2e-1b2a-4c3d-9e8f-123456789008")), row("INFO next")])
    tick(w, MORNING + timedelta(minutes=60))
    assert len(w.posts) == 1, "once a business day"
    assert W.load_state()["logs"]["shapes"][next(iter(W.load_state()["logs"]["shapes"]))]["seen"] == 4, \
        "one problem, one shape, whatever record it names"
    assert F.is_issue_post({"ts": "5", "text": text, "username": W.LOG_NAME}), "the fixer sorts it like any issue"


def test_a_crash_is_an_issue_at_once():
    w = logs_world([])
    tick(w, MORNING)
    feed(w, [row(m) for m in TRACE])
    tick(w, MORNING + timedelta(minutes=15))
    assert len(w.posts) == 1 and "logged a crash (1 time today" in w.posts[0]["text"]
    assert "Where: src/agents/relay.py line 241, in deliver" in w.posts[0]["text"]


def test_one_problem_keeps_one_shape_in_the_fixers_ledger():
    s = {"app": "siftstack-agents", "kind": "error", "text": "sms_agent.crm: the add-notes route answered 404"}
    a = W.post_text_for(s, ["2026-11-20T14:00:00Z"] * 3)
    b = W.post_text_for(s, ["2026-11-21T19:12:00Z"] * 17)
    assert a != b and F.shape_key(a) == F.shape_key(b)


def test_the_fixers_verdict_decides_what_is_said_again():
    s = {"app": "siftstack-agents", "kind": "error", "text": "sms_agent.crm: the add-notes route answered 404",
         "hits": ["2026-11-20T13:00:00Z", "2026-11-20T13:10:00Z", "2026-11-20T14:20:00Z"]}
    key = F.shape_key(W.post_text_for(s, s["hits"]))
    assert W.hits_that_count(s, {"issues": {key: {"status": "closed"}}}, W.post_text_for(s, s["hits"]))[0] == []
    assert W.hits_that_count(s, {"issues": {key: {"status": "for_people"}}}, W.post_text_for(s, s["hits"]))[0] == []
    fixed = {"issues": {key: {"status": "fixed", "fixed_at": "2026-11-20T13:30:00+00:00"}}}
    hits, _ = W.hits_that_count(s, fixed, W.post_text_for(s, s["hits"]))
    assert hits == ["2026-11-20T14:20:00Z"], "after a fix, only what came after it (and its grace) counts"
    assert W.hits_that_count(s, {"issues": {}}, W.post_text_for(s, s["hits"]))[0] == s["hits"]


def test_a_shape_the_fixer_closed_is_never_posted_again():
    w = logs_world([])
    tick(w, MORNING)
    probe = {"app": "siftstack-agents", "kind": "crash"}
    feed(w, [row(m) for m in TRACE])
    keep = {}
    ev = W.scan([row(m) for m in TRACE], keep)[0]
    probe.update(text=W.canonical(ev), where="src/agents/relay.py line 241, in deliver",
                 pre=W.mask(ev["pre"]))
    w.ledger = {"issues": {F.shape_key(W.post_text_for(probe, ["x"])): {"status": "closed", "class": "by_design"}}}
    tick(w, MORNING + timedelta(minutes=15))
    assert w.posts == []


def test_a_bad_morning_is_capped(monkeypatch):
    monkeypatch.setattr(W, "LOG_MAX_POSTS_DAY", 2)
    w = logs_world([])
    tick(w, MORNING)
    crashes = []
    for i in range(4):
        crashes += ["Traceback (most recent call last):", '  File "/app/src/agents/mod%d.py", line 1, in f%d' % (i, i),
                    "    x()", "ValueError: problem number %s" % "abcd"[i], "INFO next"]
    feed(w, [row(m) for m in crashes])
    tick(w, MORNING + timedelta(minutes=15))
    assert len(w.posts) == 2


def test_logs_that_cannot_be_read_are_said_once_a_day_after_three_tries():
    w = logs_world([])
    w.pages["siftstack-agents"] = RuntimeError("HTTP Error 401: Unauthorized")
    for m in (0, 15, 30, 45, 60):
        tick(w, MORNING + timedelta(minutes=m))
    said = [p for p in w.posts if "could not read the logs of the agents brain" in p["text"]]
    assert len(said) == 1


def test_a_stopping_point_from_yesterday_reads_the_last_hour_only():
    w = logs_world([])
    state = W.load_state()
    state["logs"]["apps"]["siftstack-agents"] = {"token": W.ns(MORNING - timedelta(days=2))}
    W.save_state(state)
    tick(w, MORNING)
    first = [t for app, t in w.asked_logs if app == "siftstack-agents"][0]
    assert first == W.ns(MORNING - timedelta(hours=1))


def test_reading_pages_until_the_logs_run_out():
    w = logs_world([])
    t0, t1, t2 = (W.ns(MORNING - timedelta(minutes=m)) for m in (10, 6, 2))
    st = W.load_state()
    st["logs"]["apps"]["siftstack-agents"] = {"token": t0}
    W.save_state(st)
    feed(w, [row("INFO a", nxt=t1)])
    feed(w, [row("INFO b", nxt=t2)])
    tick(w, MORNING)
    asked = [t for app, t in w.asked_logs if app == "siftstack-agents"]
    assert asked == [t0, t1, t2] and W.load_state()["logs"]["apps"]["siftstack-agents"]["token"] == t2


# ---------------------------------------------------------------------- switches and the thread
def test_a_dry_tick_says_and_keeps_nothing():
    w = FakeWorld(settings(), verdicts={"sms": ("stale", "x")})
    lines = tick(w, MORNING, dry=True, force=True)
    tick(w, MORNING + timedelta(minutes=5), dry=True, force=True)
    assert w.relayed == [] and w.posts == [] and not W.state_path().exists()
    assert any(ln.startswith("sms stale") for ln in lines)


def test_quiet_mode_checks_and_logs_but_says_nothing():
    w = FakeWorld(settings(ISSUE_FIXER_WATCH="quiet"), verdicts={"sms": ("stale", "x")})
    tick(w, MORNING)
    tick(w, MORNING + timedelta(minutes=5))
    assert w.relayed == [] and w.posts == []
    assert W.load_state()["watchdogs"]["sms"]["posted_how"] == "quiet"


def test_the_off_switches():
    w = FakeWorld(settings(ISSUE_FIXER_WATCH="off", ISSUE_FIXER_LOGWATCH="off"))
    assert tick(w, MORNING) == ["the watch is off"] and w.checked == []
    W.switch(False)
    w = FakeWorld(settings())
    tick(w, MORNING)
    assert w.checked == []
    W.switch(True)
    tick(w, MORNING)
    assert w.checked == ["sms", "agents", "ftm"]


def test_the_watch_thread_keeps_ticking_through_a_hung_tick(monkeypatch):
    monkeypatch.setattr(W, "seconds_to_next", lambda now, *a, **k: 0.01)
    stop, calls = threading.Event(), []

    def run_tick():
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("tick", 290)
        if len(calls) >= 3:
            stop.set()
    t = W.start(run_tick=run_tick, stop=stop)
    t.join(5)
    assert len(calls) >= 3 and not t.is_alive()


def test_the_next_tick_is_on_the_five_minute_clock():
    t = datetime(2026, 11, 20, 14, 2, 30, tzinfo=timezone.utc)
    assert W.seconds_to_next(t) == 210            # 14:06, a minute after the mark
    assert W.seconds_to_next(datetime(2026, 11, 20, 14, 5, 50, tzinfo=timezone.utc)) == 310


def test_the_loop_starts_the_watch_but_a_single_run_does_not(monkeypatch):
    started = []
    monkeypatch.setattr(W, "start", lambda *a, **k: started.append(1))
    F.loop(sh=None, once=True, runner=lambda: 0)
    assert started == []
    assert F.start_watch() is None and started == [1]


def test_no_dash_in_the_watch():
    for path in (ROOT / "tools" / "issue_fixer" / "watch.py", ROOT / "tools" / "issue_fixer" / "PLAYBOOK.md"):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, path.name
