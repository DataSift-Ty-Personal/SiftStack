"""Selftest: releasing a staged batch from Slack (agents/release.py). Zero network: Slack is a fake
and the batch kind under test is a fake registered for the test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class _Slack:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        if url.endswith("chat.postMessage"):
            self.posts.append(dict(json or {}))
        return _Resp({"ok": True, "ts": "1800000000.%06d" % len(self.posts)})

    def get(self, url, params=None, timeout=None, headers=None):
        return _Resp({"ok": False})


CALLS = []


def fake_release(batch_id, commit=False, **kw):
    CALLS.append((batch_id, commit))
    return {"batch": batch_id, "sent": 3 if commit else 0, "of": 3, "usd": 1.5 if commit else 0, "problems": []}


def run(check, tmp) -> None:
    from . import config, intake, release, slack_app, store, team, worker
    from .autopilot import ledger

    store.init()
    fake = _Slack()
    door, token, mode, who = slack_app._http, config.SLACK_BOT_TOKEN, config.MODE, config.RELEASERS
    slack_app._http, config.SLACK_BOT_TOKEN, config.RELEASERS = fake, "xoxb-test", "UBOSS"
    release.RELEASE["selftest_kind"] = (__name__.split(".", 1)[1], "fake_release")
    roster = team.load()
    t0 = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
    try:
        bid = ledger.batch_create("ftm_mail", "selftest_kind", [{"record_uuid": "x1"}, {"record_uuid": "x2"},
                                                                {"record_uuid": "x3"}], 1.5)
        check(release.parse("release batch %d" % bid) == ("release", bid, False)
              and release.parse(team.clean("<@U0BOT> confirm #%d uploaded" % bid)) == ("confirm", bid, True)
              and release.parse("please release the batch") is None
              and release.parse("release batch %d and the next one" % bid) is None,
              "a release and a confirm are read by pattern (a mention of the app is fine), and ordinary words are not a command")

        def say(text, user="UBOSS", ts="1800.1"):
            return intake.slack_event({"type": "message", "channel": "CONE", "ts": ts, "text": text, "user": user},
                                      roster)

        n = len(fake.posts)
        r = say("release batch %d" % bid, user="USOMEONE", ts="1800.2")
        check(r.get("release") == "refused" and "Only a person named as a releaser" in fake.posts[-1]["text"]
              and not store.kv_get(release._key("USOMEONE", bid)),
              "a person who is not a named releaser is told so, and nothing is set up for them to confirm")
        r = say("confirm %d" % bid, ts="1800.3")
        check(r.get("release") == "refused" and "Ask first" in fake.posts[-1]["text"],
              "a confirm with no release asked first is refused: the person must see the batch before it goes")
        config.MODE = "dry"
        r = say("release batch %d" % bid, ts="1800.4")
        check(r.get("release") == "asked" and "3 item(s)" in fake.posts[-1]["text"] and "$1.50" in fake.posts[-1]["text"]
              and "dry mode" in fake.posts[-1]["text"],
              "a release says what the batch holds and what it costs, and warns when the brain is in dry mode")
        r = say("confirm %d" % bid, ts="1800.5")
        check(r.get("release") == "refused" and r.get("why") == "dry box" and ledger.batch(bid)["status"] == "staged"
              and not CALLS, "on a dry brain a confirm releases nothing and the batch stays staged")

        config.MODE = "live"
        store.kv_set(release._key("UBOSS", bid), (t0 - timedelta(minutes=16)).isoformat(timespec="seconds"))
        r = release.slack_command(("confirm", bid, False), "UBOSS", "CONE", "1800.6", now=t0)
        check(r.get("why") == "expired" and not CALLS, "a confirm more than 15 minutes after the release is refused")
        release.slack_command(("release", bid, False), "UBOSS", "CONE", "1800.7", now=t0)
        r = release.slack_command(("confirm", bid, False), "UBOSS", "CONE", "1800.7", now=t0 + timedelta(minutes=2))
        task = store.task(r.get("task") or 0) or {}
        check(r.get("release") == "queued" and task.get("workflow") == release.WORKFLOW and not CALLS,
              "a timely confirm on a live brain queues the release for the worker; nothing is sent inside Slack's reply")
        r2 = release.slack_command(("confirm", bid, False), "UBOSS", "CONE", "1800.7", now=t0 + timedelta(minutes=3))
        check(r2.get("why") == "not asked", "one confirm is one release: a second confirm needs a new release")

        config.MODE = "dry"
        worker.run_task(task)
        check(not CALLS and "not released" in fake.posts[-1]["text"],
              "the worker checks the brain again: put back in dry mode after the confirm, it releases nothing")
        config.MODE = "live"
        store.kv_set(release._key("UBOSS", bid), "")
        release.slack_command(("release", bid, False), "UBOSS", "CONE", "1800.8", now=t0)
        r = release.slack_command(("confirm", bid, False), "UBOSS", "CONE", "1800.8", now=t0 + timedelta(minutes=1))
        worker.run_task(store.task(r["task"]))
        check(CALLS == [(bid, True)] and "3 of 3 sent" in fake.posts[-1]["text"],
              "on a live brain the worker releases the batch through its kind's own function and posts the result")

        tools_seen = {w.name for w in roster.workflows.values()}
        check(release.WORKFLOW not in tools_seen,
              "release_batch is not on the roster, so no employee has it as a tool and a model can never release")
        other = ledger.batch_create("ftm_mail", "no_such_kind", [{"record_uuid": "y1"}], 0)
        r = release.slack_command(("release", other, False), "UBOSS", "CONE", "1800.9", now=t0)
        check(r.get("why") == "unknown kind", "a batch of a kind nothing can release is refused, not guessed at")
        for d in (chr(0x2014), chr(0x2013)):
            check(all(d not in p.get("text", "") for p in fake.posts), "nothing the release posts carries a long dash")
    finally:
        release.RELEASE.pop("selftest_kind", None)
        slack_app._http, config.SLACK_BOT_TOKEN, config.MODE, config.RELEASERS = door, token, mode, who
