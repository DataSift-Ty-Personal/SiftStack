"""Buildable lots selftest (2026-11-23): Atlas's workflow that runs the buildable lot finder in Slack.

Zero network and no process is started: the background launcher, the phase runner, the liveness check
and the memory reading are fakes, and one invented county (Testfield County, OH, FIPS 99001) stands in
for a real one. Every street, owner, id and figure here is invented.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

ENV = ("VACANT_LAND_DIR", "BUILDABLE_LOTS_MIN_FREE_MB", "BUILDABLE_LOTS_QUICK_FREE_MB", "BUILDABLE_LOTS_PHASE_MIN",
       "SLACK_BOT_TOKEN", "TRESTLE_API_KEY")
TESTFIELD = {"fips": "99001", "name": "Testfield", "state": "OH", "label": "Testfield OH", "slug": "testfield_oh",
             "gis": "", "layers": {}}


def run(check, tmp: Path) -> None:
    import vacant_land as vl
    from . import store
    from .workflows import buildable_lots as bl
    keep = (config.DATA_DIR, config.RUNS_DIR, config.MODE, config.DB_PATH)
    keep_counties, keep_out = dict(vl.COUNTIES), vl.OUT_ROOT
    keep_fakes = (bl.LAUNCHER, bl.RUNNER, bl.ALIVE, bl.MEMORY)
    env = {k: os.environ.get(k) for k in ENV}
    for k in ENV:
        os.environ.pop(k, None)
    try:
        _resolve(check, bl)                            # against the real county table, before it is swapped
        # Its own database too: config.DB_PATH is fixed when config loads, so a section run on its own
        # would otherwise write into the real agents.db.
        config.DATA_DIR, config.RUNS_DIR, config.MODE = tmp / "bl_data", tmp / "bl_runs", "dry"
        config.DB_PATH = tmp / "bl_data" / "agents.db"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        store.init()
        vl.COUNTIES = {"99001": dict(TESTFIELD), "39159": keep_counties["39159"]}
        vl.OUT_ROOT = str(tmp / "bl_repo_output")      # the repo's own output: empty here
        _start(check, bl, store)
        _background(check, bl, store)
        _failure(check, bl, store)
        _roster(check)
        _hygiene(check)
    finally:
        config.DATA_DIR, config.RUNS_DIR, config.MODE, config.DB_PATH = keep
        vl.COUNTIES, vl.OUT_ROOT = keep_counties, keep_out
        bl.LAUNCHER, bl.RUNNER, bl.ALIVE, bl.MEMORY = keep_fakes
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _no_dash(s: str) -> bool:
    return chr(0x2014) not in s and chr(0x2013) not in s


# ---------------------------------------------------------------------------- which county

def _resolve(check, bl) -> None:
    union = bl.vl.COUNTIES["39159"]
    for text in ("", "39159", "Union OH", "Union County, Ohio", "union county oh", "Union"):
        c, why = bl.resolve_county(text)
        check(c is union, "%r is Union County OH: %s" % (text, why))
    for text in ("Union County, TN", "Union County Tennessee", "Knox TN", "12345"):
        c, why = bl.resolve_county(text)
        check(c is None and "set up" in why, "%r is refused, not read as Union OH: %s" % (text, why))
    check(bl._share("55%") == 0.55 and bl._share("0.6") == 0.6 and bl._share("65") == 0.65, "offer shares read")
    check(bl._share("") is None and bl._share("abc") is None and bl._share("0") is None, "no share from nothing")
    check(bl._yes("yes") and bl._yes("Refresh") and not bl._yes("") and not bl._yes("no"), "refresh read")


# ---------------------------------------------------------------------------- starting a run

def _origin(store, inputs: dict, details: str = "") -> dict:
    tid, _ = store.enqueue("analysis", "buildable_lots", subject="buildable lots", source="agent:analysis",
                           payload={"inputs": inputs, "details": details}, slack_channel="C0TEST",
                           slack_thread="1700000000.000100")
    return store.task(tid)


def _start(check, bl, store) -> None:
    launched = []

    def launcher(argv, env, log_path):
        launched.append((argv, env, log_path))
        return 4242

    bl.LAUNCHER, bl.ALIVE, bl.MEMORY = launcher, (lambda pid: False), (lambda: 1500.0)
    os.environ["SLACK_BOT_TOKEN"], os.environ["TRESTLE_API_KEY"] = "xoxb-test", "trestle-test"
    t = _origin(store, {"county": "Testfield OH", "offer_low": "65%", "offer_high": "0.55"})
    r = bl.run(t)
    check(r["ok"] and r["mode"] == "started" and r["phases"] == list(bl.FULL),
          "a county with no run starts the full screen: %s" % r)
    argv, env, log_path = launched[-1]
    check("--background" in argv and argv[argv.index("--fips") + 1] == "99001"
          and argv[argv.index("--phases") + 1] == ",".join(bl.FULL), "the background command: %s" % argv)
    check(argv[argv.index("--offer-low") + 1] == "0.55" and argv[argv.index("--offer-high") + 1] == "0.65",
          "a reversed offer band is put in order: %s" % argv)
    check(env.get("VACANT_LAND_BUDGET") == "agents" and env.get("VACANT_LAND_DIR") == str(bl.data_root()),
          "the run counts against the shared budget and writes to the volume folder")
    check("SLACK_BOT_TOKEN" not in env and "TRESTLE_API_KEY" not in env and env.get("AGENTS_MODE") == "dry",
          "the run carries no Slack token and no vendor key, in dry mode")
    st = bl.read_status(bl.vl.COUNTIES["99001"])
    check(st.get("state") == "starting" and st.get("origin") == t["id"], "the start is written first: %s" % st)
    text = bl.render(t, r)
    check("Started buildable lots for Testfield OH" in text and "About 30 minutes" in text and _no_dash(text), text)

    r2 = bl.run(_origin(store, {"county": "99001"}))
    check(r2["mode"] == "running" and len(launched) == 1, "a second ask while it starts does not start another")
    c = bl.vl.COUNTIES["99001"]
    bl.write_status(c, dict(st, state="running", pid=4242, phase="topo"))
    bl.ALIVE = lambda pid: pid == 4242
    r3 = bl.run(_origin(store, {"county": "Testfield"}))
    check(r3["mode"] == "running" and r3["phase"] == "topo", "a running county says which step: %s" % r3)
    check("now on slope and low spots" in bl.render({"id": 1}, r3), bl.render({"id": 1}, r3))
    r4 = bl.run(_origin(store, {"county": "Union OH"}))
    check(r4["mode"] == "busy" and not r4["ok"] and "one county at a time" in r4["problem"],
          "another county waits while one runs: %s" % r4)
    bl.write_status(c, dict(st, state="running", pid=99, phase="topo"))   # the box restarted: that pid is gone
    bl.MEMORY = lambda: 300.0
    r5 = bl.run(_origin(store, {"county": "99001"}))
    check(r5["mode"] == "refused" and "memory" in r5["problem"], "too little memory refuses the run: %s" % r5)
    bl.MEMORY = lambda: 1500.0
    d = bl.county_dir(c)
    d.mkdir(parents=True, exist_ok=True)
    (d / "screen.json").write_text("[]", encoding="utf-8")
    r6 = bl.run(_origin(store, {"county": "99001"}))
    check(r6["mode"] == "started" and r6["phases"] == list(bl.QUICK) and "--refresh" not in launched[-1][0],
          "a county with a run is re-scored, not re-screened: %s" % r6.get("phases"))
    bl.write_status(c, {"state": "done"})
    r7 = bl.run(_origin(store, {"county": "99001", "refresh": "yes"}))
    check(r7["phases"] == list(bl.FULL) and "--refresh" in launched[-1][0], "refresh=yes runs it all again")
    bl.write_status(c, {"state": "done"})


# ---------------------------------------------------------------------------- the run itself

def _files(d: Path) -> None:
    """What vacant_land.py's export leaves, invented."""
    (d / "Testfield_OH_Buildable_Lots_20261123.xlsx").write_bytes(b"PK test workbook")
    (d / "mail_list.csv").write_text("Rank,Tier,DataSift id\n1,A,880001\n", encoding="utf-8")
    (d / "pull_list.csv").write_text("Rank,Tier,Why,DataSift id\n1,A,on the mail list,880001\n2,C,owner not "
                                     "visible yet,880002\n", encoding="utf-8")
    # As the engine really leaves them: score writes scored.json before export prices the lots, so the
    # offers live only in export.json's top lots (the first box run read them from scored.json and
    # printed "offer no figure to no figure").
    (d / "scored.json").write_text(json.dumps([
        {"mailable": True, "tier": "A", "address": "10 Sample Rd", "city": "Testfield", "acres": 0.6,
         "segment": "Out-of-state owner"},
        {"mailable": False, "tier": "C", "address": "12 Sample Rd", "city": "Testfield", "acres": 1.1}]),
        encoding="utf-8")
    (d / "export.json").write_text(json.dumps({
        "run": "2026-11-23T10:00:00", "county": "Testfield OH", "in_window": 40, "marketable": 12, "mailable": 1,
        "verdicts": {"BUILDABLE": 5, "LIKELY BUILDABLE": 5, "VERIFY": 2, "BUILT SINCE REAPPRAISAL": 20,
                     "NOT BUILDABLE": 6, "PUBLIC OR COMMON": 2},
        "tiers": {"A": 1}, "owners": {"siftmap cache": 1, "": 11}, "owner_unknown": 1, "to_pull": 2,
        "comps": 9, "comp_median": 60000, "offer_band": [0.5, 0.65], "list": "Testfield OH - Buildable Lots",
        "top": [{"tier": "A", "address": "10 Sample Rd", "city": "Testfield", "acres": 0.6,
                 "segment": "Out-of-state owner", "offer_low": 30000, "offer_high": 39000},
                {"tier": "B", "address": "14 Sample Rd", "city": "Testfield", "acres": 0.9,
                 "segment": "Local owner", "offer_low": None, "offer_high": None}]}),
        encoding="utf-8")


def _background(check, bl, store) -> None:
    c = bl.vl.COUNTIES["99001"]
    calls = []

    def runner(argv, env, log_path, timeout_s):
        calls.append(argv)
        phase = argv[argv.index("--phase") + 1]
        d = bl.data_root() / c["slug"]
        d.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ran %s\n" % phase, encoding="utf-8")
        if phase == "export":
            _files(d)
        return 0

    bl.RUNNER = runner
    origin = _origin(store, {"county": "99001", "refresh": "yes"}, details="buildable lots in Testfield")
    rc = bl.background("99001", list(bl.FULL), origin["id"], refresh=True, offer_low=0.55, offer_high=0.7)
    check(rc == 0 and [a[a.index("--phase") + 1] for a in calls] == list(bl.FULL), "every phase runs in order")
    check(all(a[a.index("--owners") + 1] == "account" for a in calls) and not any("siftmap" in a for a in calls),
          "the owners always come from the account, never a SiftMap detail pull")
    check("--refresh" in calls[0] and not any("--refresh" in a for a in calls[1:]), "refresh re-pulls the county only")
    ex = calls[-1]
    check(ex[ex.index("--offer-low") + 1] == "0.55" and ex[ex.index("--offer-high") + 1] == "0.7", "the band reaches export")
    st = bl.read_status(c)
    check(st["state"] == "done" and st["finished"] and st["rc"] == 0, "the status says done: %s" % st)
    with store._LOCK, store._db() as db:
        row = db.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY id DESC LIMIT 1", (origin["id"],)).fetchone()
    follow = dict(row) if row else {}
    check(follow.get("workflow") == "buildable_lots" and follow.get("slack_channel") == "C0TEST"
          and follow.get("slack_thread") == "1700000000.000100" and follow.get("thread_id") == origin["thread_id"],
          "the end queues the workflow again on the asker's thread")
    p = store.payload_of(follow)
    check(p.get("after_run", {}).get("state") == "done" and p.get("inputs", {}).get("county") == "99001",
          "the follow-up carries the result and the ask: %s" % p)
    r = bl.run(follow)
    check(r["ok"] and r["mode"] == "done", "the follow-up collects the run: %s" % r.get("problem"))
    check(r["files"] == ["Testfield_OH_Buildable_Lots_20261123.xlsx", "testfield_oh_mail_list.csv",
                         "testfield_oh_pull_list.csv"], "the files, the CSVs named for the county: %s" % r["files"])
    files = Path(r["run_dir"]) / "files"
    check((files / "testfield_oh_pull_list.csv").read_text(encoding="utf-8").startswith("Rank,Tier,Why,DataSift id"),
          "the pull list keeps the DataSift id column list_pull reads")
    from .workflows import list_pull
    ids, why = list_pull.resolve_ids("testfield_oh_pull_list.csv")
    check(ids == ["880001", "880002"], "list_pull finds the pull list by its name and reads its ids: %s %s" % (ids, why))
    text = bl.render(follow, r)
    check("40 vacant lots in the window, 12 can carry a house, 1 mailable now (A 1, B 0, C 0)" in text, text)
    check("Screened out: 20 already built on, 6 cannot carry a house, 2 public or common land." in text, text)
    check("Top lot: 10 Sample Rd, Testfield, 0.6 ac, tier A" in text and "$30,000 to $39,000" in text, text)
    check("Top lot: 14 Sample Rd, Testfield, 0.9 ac, tier B, local owner, not priced (no lot comp)." in text
          and "no figure" not in text, "a lot with no comp says so instead of printing empty prices: %s" % text)
    check("pull testfield_oh_pull_list.csv onto the list \"Testfield OH - Buildable Lots\"" in text
          and "nothing is pulled without your approved count" in text, text)
    check(_no_dash(text) and "SiftMap detail" not in text, "the post is plain")


def _failure(check, bl, store) -> None:
    c = bl.vl.COUNTIES["99001"]

    def runner(argv, env, log_path, timeout_s):
        phase = argv[argv.index("--phase") + 1]
        log_path.write_text("working\nSTOPPED: the app has used its 2800 DataSift requests for this hour\n",
                            encoding="utf-8")
        return 4 if phase == "hydrate" else 0

    bl.RUNNER = runner
    origin = _origin(store, {"county": "99001"})
    rc = bl.background("99001", list(bl.QUICK), origin["id"])
    st = bl.read_status(c)
    check(rc == 1 and st["state"] == "failed" and st["failed_phase"] == "hydrate" and st["rc"] == 4,
          "a phase that stops ends the run there: %s" % st)
    with store._LOCK, store._db() as db:
        row = db.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY id DESC LIMIT 1", (origin["id"],)).fetchone()
    r = bl.run(dict(row))
    check(not r["ok"] and r["mode"] == "failed" and r["phase"] == "hydrate" and "2800 DataSift requests" in r["log"],
          "the failure says which step and why: %s" % r)
    text = bl.render({"id": 1}, r)
    check("stopped at owners from the account" in text and "asking again continues from there" in text
          and _no_dash(text), text)
    bl.write_status(c, {"state": "done"})


# ---------------------------------------------------------------------------- the roster and the source

def _roster(check) -> None:
    from . import team
    roster = team.load()
    w = roster.workflows.get("buildable_lots")
    check(w is not None and w.owner == "analysis" and w.live and w.spend == "free", "Atlas owns it, live and free")
    check(w is not None and set(w.inputs) == {"county", "refresh", "offer_low", "offer_high"}, "its inputs")
    check(w is not None and "vacantland" in w.covers and "vacant_land" in w.sources, "it runs the Buildable Lot Finder")
    land = roster.workflows.get("land_desk")
    check(land is not None and "vacantland" not in land.covers, "the land desk no longer claims the lot finder")


def _hygiene(check) -> None:
    root = Path(__file__).resolve().parents[1]
    src = (root / "agents" / "workflows" / "buildable_lots.py").read_text(encoding="utf-8")
    check(_no_dash(src) and chr(8) not in src, "no dash and no backspace byte in the workflow")
    check("get_detail" not in src, "the workflow itself never reaches for a SiftMap detail")
    eng = (root / "vacant_land.py").read_text(encoding="utf-8")
    check('"--owners", choices=("account", "siftmap"), default="account"' in eng,
          "the lot finder's owner source defaults to the account")
