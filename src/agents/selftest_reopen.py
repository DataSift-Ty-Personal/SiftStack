"""A death reported after the research sends the record back (2026-11-19, 8026 Example Pike).

The research queue never looked at a researched record again, so when the owners' daughter texted that
both parents had died, six days after the research called the owner living, nothing re-read the record.
Now a death tag put on after the newest research, and still on the record, sends it back once. The dates
come from the shared copy of the account log. Zero network: the fake DataSift from the deeper-pass
selftest, a throwaway database, log rows written here by hand. Every name, street and id is invented.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, PRESETS, T0
from .selftest_research2 import ENGINE_ENV, FEED, PLAYBOOK, STAGE, CardCRM, R, _wipe, good_folder, result


def _log(store, u: str, at: datetime, kind: str, tags: list[str], n=[0]) -> None:   # noqa: B006
    n[0] += 1
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_log (id, at, event_type, resource, resource_type, source, author, data) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  ("reopen-%d" % n[0], at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                   "property.tags.%s" % kind, u, "property", "internal-api", "self", json.dumps({"tags": tags})))


def _finished(store, tid: int) -> datetime:
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT finished FROM tasks WHERE id=?", (tid,)).fetchone()
    from .autopilot.researchqueue import _when
    return _when(row["finished"])


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.MODE,
            config.RUNS_DIR, config.DB_PATH)
    saved_env = {k: os.environ.get(k) for k in ENGINE_ENV}
    saved_presets = dict(PRESETS)
    # Its own database, always. Run on its own (outside selftest.main, which points DB_PATH at a temp
    # file), this section once wrote into the workstation's real agents.db and its wipe cleared the
    # autopilot tables there (2026-11-19).
    config.DB_PATH = tmp / "selftest_reopen.db"
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.RESEARCH_DAILY_CAP_USD = 100000.0
    config.MODE = "live"
    store.init()
    for k in ENGINE_ENV:
        os.environ.pop(k, None)
    os.environ["AGENTS_RESEARCH_ENGINE"] = "open"
    PRESETS[FEED] = lambda r: "R2" in r["tags"]
    try:
        _run(check, tmp, store)
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        for k in ENGINE_ENV:
            os.environ.pop(k, None)
            if saved_env[k] is not None:
                os.environ[k] = saved_env[k]
        (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.MODE,
         config.RUNS_DIR, config.DB_PATH) = keep
        config.MODE = "dry"


def _run(check, tmp: Path, store) -> None:
    from .autopilot import ledger, policy, researchqueue as rq, sweep
    assert Path(config.DB_PATH).parent == Path(tmp), "this section only ever runs on its own temp database"
    _wipe(store)
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    runs = tmp / "reopen_runs"
    calls: list[tuple[str, dict]] = []

    def enq(agent, wf, u, subject, **kw):
        calls.append((u, dict(kw)))
        return store.enqueue(agent, wf, u, subject, **kw)

    def researched(u: str, slug: str) -> tuple[int, datetime]:
        d = good_folder(runs, slug)
        tid, _ = store.enqueue("research", "deep_prospect", u, slug, force=True)
        store.finish(tid, "done", result(slug, d))
        return tid, _finished(store, tid)

    dead_tags = ["R2", "Deep Prospected", "Verify Deceased"]
    crm = CardCRM([R("ro_dead", "301 Reopen St", tags=list(dead_tags)),
                   R("ro_before", "302 Reopen St", tags=list(dead_tags)),
                   R("ro_off", "303 Reopen St", tags=["R2", "Deep Prospected"]),
                   R("ro_away", "304 Reopen St", tags=["R2", "Deep Prospected", "Death Reported"]),
                   R("ro_new", "305 Reopen St", tags=["R2", "Verify Deceased"])])
    _, done_dead = researched("ro_dead", "301-reopen-st")
    _log(store, "ro_dead", done_dead + timedelta(hours=2), "added", ["Verify Deceased"])
    _, done_before = researched("ro_before", "302-reopen-st")
    _log(store, "ro_before", done_before - timedelta(days=2), "added", ["Verify Deceased"])
    _, done_off = researched("ro_off", "303-reopen-st")
    _log(store, "ro_off", done_off + timedelta(hours=1), "added", ["Verify Deceased"])
    _log(store, "ro_off", done_off + timedelta(hours=3), "removed", ["Verify Deceased"])
    # Researched on another box: only the researched tag says when, and the death came later.
    _log(store, "ro_away", datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc), "added", ["Deep Prospected"])
    _log(store, "ro_away", datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc), "added", ["Death Reported"])
    _log(store, "ro_new", datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc), "added", ["Verify Deceased"])

    idx = rq.death_signal_index(["Verify Deceased", "Death Reported"], "Deep Prospected")
    check(idx["ro_dead"]["signal"][0] == "Verify Deceased" and idx["ro_off"]["signal"] is None,
          "the index reads a death tag that is on the record and drops one that came off")
    check(idx["ro_away"]["researched"] == datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
          "research done on another box is dated by the researched tag")

    plan = sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq, plan=True)
    t = plan.tally
    check(t.get("going back to research: a death was reported after it") == 2,
          "two researched records carry a death reported after their research: %s" % dict(t))
    check(not calls and ledger.fact("ro_dead", rq.REOPEN_FACT) is None,
          "a planned run hands nothing over and remembers no re-open")
    check(t.get("already researched") == 2, "a death tag older than the research, or one taken off, leaves it alone")

    live = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=30), pol=pol, enqueue=enq)
    back = {u: kw for u, kw in calls if u in ("ro_dead", "ro_away")}
    check(set(back) == {"ro_dead", "ro_away"} and all(kw.get("force") is True for kw in back.values()),
          "both go back to research, forced past the 24-hour repeat guard")
    check(any(u == "ro_new" and not kw.get("force") for u, kw in calls),
          "a record never researched is an ordinary first pass, not a re-open")
    check(any(ln.startswith("301 Reopen St, Knoxville: back to research, Verify Deceased went on") for ln in live.lines),
          "the run says which record went back and why: %s" % live.lines[:4])
    fact = ledger.fact("ro_dead", rq.REOPEN_FACT)
    check(isinstance(fact, dict) and fact.get("signal_at") and len(fact.get("times") or []) == 1,
          "the re-open is remembered with the signal it answered")
    ev = ledger.events("ro_dead", "handoff")
    check(ev and "after its research" in str(ev[-1]["detail"].get("reopen")), "the handoff says it was a re-open")

    calls.clear()
    again = sweep.run(STAGE, crm=crm, now=T0 + timedelta(hours=1), pol=pol, enqueue=enq)
    check(not any(u in ("ro_dead", "ro_away") for u, _ in calls) and
          again.tally.get("going back to research: a death was reported after it") is None,
          "while the second research is open the record is not sent again")
    for u, _slug in (("ro_dead", ""), ("ro_away", "")):
        tid = rq.research_index()[u]["task"]
        store.finish(tid, "done", result("x", runs))
    calls.clear()
    after = sweep.run(STAGE, crm=crm, now=T0 + timedelta(hours=2), pol=pol, enqueue=enq)
    check(not any(u in ("ro_dead", "ro_away") for u, _ in calls),
          "once the second research finishes, the same death tag never sends it back again: %s" % dict(after.tally))

    o = rq.options(None)
    capped = {"signal": ("Verify Deceased", datetime(2026, 11, 18, tzinfo=timezone.utc)), "researched": None}
    info = {"done": True, "open": False, "finished": "2026-11-17T12:00:00+00:00", "last_handoff": ""}
    many = {"u": {"at": "2026-11-17T12:00:00+00:00", "signal_at": "2026-11-10T00:00:00+00:00",
                  "times": ["2026-11-01T00:00:00+00:00", "2026-11-17T12:00:00+00:00"]}}
    why, _ = rq.reopen_why("u", info, {"u": capped}, many, o, datetime(2026, 11, 19, tzinfo=timezone.utc))
    check(why == "", "no record goes back more than reopen_max times inside the window")
    off = rq.options(type("S", (), {"get": lambda self, k: False if k == "reopen_on_death" else None})())
    check(off["reopen_on_death"] is False, "the re-open is a setting (reopen_on_death)")
