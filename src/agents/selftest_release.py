"""Selftest: releasing a staged batch from Slack (agents/release.py). Zero network: Slack is a fake
and the batch kind under test is a fake registered for the test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config


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
        _free_box(check, release, store, worker, ledger, fake, t0)
        _drain(check, store, t0)
        for d in (chr(0x2014), chr(0x2013)):
            check(all(d not in p.get("text", "") for p in fake.posts), "nothing the release posts carries a long dash")
    finally:
        release.RELEASE.pop("selftest_kind", None)
        slack_app._http, config.SLACK_BOT_TOKEN, config.MODE, config.RELEASERS = door, token, mode, who


def _free_box(check, release, store, worker, ledger, fake, t0) -> None:
    """A free box (2026-11-19): a batch is released where its stage would act on its own."""
    import copy

    from .autopilot import policy
    from .selftest_autopilot import EMPLOYEES
    from .selftest_textlane import PLAYBOOK
    print("  a free box releases what its live stages staged")
    # t_full: a live stage whose kind spends nothing; t_open: a live outreach stage; t_scrub: in plan mode.
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    saved = (release._policy, config.LIVE_STAGES, config.MODE)
    release._policy = lambda: pol
    try:
        config.MODE, config.LIVE_STAGES = "free", frozenset()
        free_b = ledger.batch_create("t_full", "selftest_kind", [{"record_uuid": "f1"}], 0)
        paid_b = ledger.batch_create("t_open", "selftest_kind", [{"record_uuid": "f2"}], 0)
        plan_b = ledger.batch_create("t_scrub", "selftest_kind", [{"record_uuid": "f3"}], 0)
        gone_b = ledger.batch_create("no_such_stage", "selftest_kind", [{"record_uuid": "f4"}], 0)
        cmd_b = ledger.batch_create("run_script", "command", [{"script": "x"}], 0)
        got = {k: release.rule(ledger.batch(b)) for k, b in (("free", free_b), ("paid", paid_b), ("plan", plan_b),
                                                             ("gone", gone_b), ("cmd", cmd_b))}
        check(got["free"][0] and not got["paid"][0] and not got["plan"][0] and not got["gone"][0] and not got["cmd"][0]
              and "AGENTS_LIVE_STAGES" in got["paid"][2] and "plan mode" in got["plan"][2]
              and "not in the playbook" in got["gone"][2] and "live box only" in got["cmd"][2],
              "on a free box only a live stage that spends nothing may release; the rest are refused with the reason")

        CALLS.clear()
        release.slack_command(("release", free_b, False), "UBOSS", "CFREE", "1801.1", now=t0)
        asked = fake.posts[-1]["text"]
        r = release.slack_command(("confirm", free_b, False), "UBOSS", "CFREE", "1801.1", now=t0 + timedelta(minutes=1))
        worker.run_task(store.task(r.get("task") or 0) or {})
        check("will not" not in asked and r.get("release") == "queued" and CALLS == [(free_b, True)]
              and "3 of 3 sent" in fake.posts[-1]["text"],
              "a free box releases the attempt keeper's kind of batch: a live stage that spends nothing (the 11-17 wait)")

        release.slack_command(("release", paid_b, False), "UBOSS", "CFREE", "1801.2", now=t0)
        asked = fake.posts[-1]["text"]
        r = release.slack_command(("confirm", paid_b, False), "UBOSS", "CFREE", "1801.2", now=t0 + timedelta(minutes=1))
        check("AGENTS_LIVE_STAGES" in asked and "will not release" in asked and r.get("release") == "refused"
              and r.get("why") == "stage not live" and "Nothing was released" in fake.posts[-1]["text"]
              and ledger.batch(paid_b)["status"] == "staged" and all(c[0] != paid_b for c in CALLS),
              "an outreach stage's batch is refused on a free box when AGENTS_LIVE_STAGES does not name it, said at the "
              "ask and at the confirm")
        try:
            release.run(paid_b, commit=True)
            check(False, "the command line refuses the same batch with --commit")
        except SystemExit as exc:
            check("AGENTS_LIVE_STAGES" in str(exc) and all(c != (paid_b, True) for c in CALLS),
                  "the command line refuses the same batch with --commit, by the same rule")
        release.run(paid_b, commit=False)
        check(CALLS[-1] == (paid_b, False), "a dry look at it still works on any box")

        config.LIVE_STAGES = frozenset({"t_open"})
        release.run(paid_b, commit=True)
        check(CALLS[-1] == (paid_b, True), "named in AGENTS_LIVE_STAGES, the outreach stage's batch is released on a free box")
        config.LIVE_STAGES = frozenset({"t_scrub", "no_such_stage", "run_script"})
        check(not release.rule(ledger.batch(plan_b))[0] and not release.rule(ledger.batch(gone_b))[0]
              and not release.rule(ledger.batch(cmd_b))[0],
              "naming a stage does not release it while it is in plan mode, outside the playbook, or a script command")

        again = ledger.batch_create("t_open", "selftest_kind", [{"record_uuid": "f5"}], 0)
        config.LIVE_STAGES = frozenset({"t_open"})
        release.slack_command(("release", again, False), "UBOSS", "CFREE", "1801.3", now=t0)
        r = release.slack_command(("confirm", again, False), "UBOSS", "CFREE", "1801.3", now=t0 + timedelta(minutes=1))
        config.LIVE_STAGES = frozenset()
        worker.run_task(store.task(r.get("task") or 0) or {})
        check(r.get("release") == "queued" and all(c[0] != again for c in CALLS) and "not released" in fake.posts[-1]["text"]
              and "AGENTS_LIVE_STAGES" in fake.posts[-1]["text"],
              "the worker checks the rule again: a stage taken off AGENTS_LIVE_STAGES after the confirm releases nothing")

        config.MODE = "dry"
        check(not any(release.rule(ledger.batch(b))[0] for b in (free_b, paid_b)) and release.rule(ledger.batch(free_b))[1] == "dry box",
              "a dry box still refuses everything")
        config.MODE = "live"
        check(all(release.rule(ledger.batch(b))[0] for b in (free_b, paid_b, plan_b, gone_b, cmd_b)),
              "a live box still allows everything, a plan stage's and a script command's batch included")
    finally:
        release._policy, config.LIVE_STAGES, config.MODE = saved


def _drain(check, store, t0) -> None:
    """The agency file's later marks wait on the same rule as its release."""
    import json

    from .autopilot import agencyexport, policy
    from .autopilot.stages import Ctx, Summary
    print("  the agency file's later marks follow the release rule")

    class Crm:
        def __init__(self):
            self.writes = []

        def call(self, path, method="GET", body=None, **kw):
            self.writes.append((method, path, body))

        def set_custom_field(self, u, field, value):
            self.writes.append(("field", u, field, value))

    st = policy.Stage(id="rel_agency", kind="agencyexport", owner="ops", mode="live", summary="", schedule=[], presets=[])
    saved = (config.MODE, config.LIVE_STAGES)
    try:
        for live_list, want in ((frozenset(), False), (frozenset({"rel_agency"}), True)):
            store.kv_set(agencyexport.QUEUE_KEY, json.dumps([{"batch": 1, "day": "2026-03-02", "list": "Agency",
                                                              "field": "f-sent", "records": ["ra1"], "before": {}}]))
            config.MODE, config.LIVE_STAGES = "free", live_list
            crm = Crm()
            out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=False)
            agencyexport.drain(Ctx(policy=None, stage=st, crm=crm, run_id="t", now=t0, live=False, out=out), {"max_writes": 5})
            wrote = bool(crm.writes) and ("POST", "/api/internal/property/ra1/add-lists/", {"lists": "Agency"}) in crm.writes
            check(wrote == want and (want or any("wait to be marked" in ln for ln in out.lines)),
                  "on a free box the marks %s" % ("go once AGENTS_LIVE_STAGES names the stage" if want else
                                                  "wait while the outreach stage is not named in AGENTS_LIVE_STAGES"))
        store.kv_set(agencyexport.QUEUE_KEY, "[]")
    finally:
        config.MODE, config.LIVE_STAGES = saved
