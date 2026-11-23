"""Deeper research pass selftest (missed workflow 8): research that came back thin is sent back once,
deeper, and then to a person. Zero network: the fake DataSift from the autopilot selftest with the
board routes the queue uses, research results and run folders written here by hand, and the research
engine's models, lookups and vendors replaced by recorders. Every name, street, number and id here is
invented.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from . import config
from .selftest_autopilot import EMERSON, EMPLOYEES, PRESETS, T0, FakeCRM, _rec

COL = "cccccccc-0000-4000-8000-000000000001"       # the research column
DONE = "cccccccc-0000-4000-8000-000000000002"      # where a finished card goes
FEED = "R2 - Feed"
TAG = "Research Needs a Person"
STAGE = "deeper_queue"
NUM = "8650000201"                                 # the never-assigned 865-000 exchange
ENGINE_ENV = ("AGENTS_RESEARCH_ENGINE", "AGENTS_RESEARCH_MODEL", "AGENTS_RESEARCH_SECOND", "AGENTS_RESEARCH_REFEREE",
              "AGENTS_ANALYSIS_MODEL", "AGENTS_RESEARCH_SEARCHES", "AGENTS_RESEARCH_PAGES")

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0, "outreach_daily_usd": 60},
    "people": {"callers": ["Emerson"]},
    "gate": {"read_fields": True},
    "stages": {
        STAGE: {"kind": "researchqueue", "owner": "research", "mode": "live",
                "schedule": "every 30 min 08:00-17:00 Mon-Fri", "presets": [FEED],
                "card_column": {"title": "Check Relatives", "uuid": COL}, "card_done_column": DONE,
                "statuses": [], "max_open": 1000, "daily_usd": 100000},
    },
}

# What a finished first pass leaves in its run folder when nothing about it is thin.
GOOD_RESEARCH = {"owner_status": "Living",
                 "owner_status_evidence": [{"claim": "named in a 2026 notice", "url": "https://notices.example.test/a",
                                            "quote": "q"}],
                 "identity_proof": [{"claim": "age matches", "url": "https://records.example.test/b", "quote": "q"}],
                 "people": [{"name": "Corwin Ashgrove", "source_url": "https://records.example.test/b"}],
                 "decedents": [], "red_flags": [], "unresolved": []}
GOOD_ANALYSIS = {"owner_status": "Living", "decision_maker": "Corwin Ashgrove",
                 "must_sign": [{"who": "Corwin Ashgrove", "status": "Required"}],
                 "people": [{"name": "Corwin Ashgrove", "role": "owner", "living": "Living"}]}


class CardCRM(FakeCRM):
    """The fake DataSift with the documented board and card routes the queue uses."""

    def __init__(self, records):
        super().__init__(records, users=(EMERSON,))
        self.cards: dict[str, dict] = {}
        self.columns = {COL: "Check Relatives", DONE: "Researched"}
        self.log: list[tuple] = []
        self.reads = 0
        self.card_n = 0

    def record(self, u):
        self.reads += 1
        return super().record(u)

    def _row(self, r):
        row = super()._row(r)
        ow = r["owner"]
        row["owner"] = {"uuid": ow["uuid"], "first_name": ow.get("first_name"), "last_name": ow.get("last_name"),
                        "company": ow.get("company")}
        return row

    def call(self, path, method="GET", body=None, headers=None):
        p = path.split("?")[0]
        self.log.append((method, p, copy.deepcopy(body)))
        if p == "/api/internal/siftline/board/":
            return {"results": [{"uuid": "board-dp", "title": "Deep Prospecting"}]}
        m = re.fullmatch(r"/api/internal/siftline/board/column/([^/]+)/card/", p)
        if m and method == "POST":
            self.card_n += 1
            cid = "card-r2-%d" % self.card_n
            self.cards[cid] = {"uuid": cid, "prop_uuid": body["prop"], "column": m.group(1)}
            return {"uuid": cid}
        if m:
            rows = [c for c in self.cards.values() if c["column"] == m.group(1)]
            return {"count": len(rows), "results": [{"uuid": c["uuid"], "prop_uuid": c["prop_uuid"]} for c in rows]}
        m = re.fullmatch(r"/api/internal/siftline/board/column/([^/]+)/card/([^/]+)/timeline/", p)
        if m:
            now = self.cards[m.group(2)]["column"]
            return {c: {"title": t, "order": i, "time": 0, "current": c == now} for i, (c, t) in enumerate(self.columns.items())}
        m = re.fullmatch(r"/api/internal/siftline/board/column/([^/]+)/card/([^/]+)/", p)
        if m and method == "PATCH":
            self.cards[m.group(2)]["column"] = body["column"]
            return {"uuid": m.group(2)}
        return super().call(path, method, body, headers)

    def changed(self) -> list:
        return list(self.writes) + [x for x in self.log if x[0] != "GET"]


class FakeRun:
    """A run folder the research engine reads and writes, nothing more."""

    def __init__(self, root: Path):
        self.dir = root
        for sub in ("research", "analysis"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def p(self, *parts):
        return self.dir.joinpath(*parts)

    def load(self, name, default=None):
        f = self.p(name)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default

    def save(self, name, obj):
        self.p(name).write_text(json.dumps(obj), encoding="utf-8")


def R(u, street, **kw):
    """A record on the feed: owned by a person DataSift has already traced, on the Probate list."""
    kw.setdefault("tags", ["R2"])
    r = _rec(u, street, **kw)
    r.setdefault("lists", ["Probate"])
    r["owner"].setdefault("skiptraced", True)
    return r


def folder(root: Path, slug: str, name: str = "", **files) -> Path:
    """A run folder holding the files a deep prospect leaves. Each case gets its own folder."""
    d = root / (name or slug)
    (d / "research").mkdir(parents=True, exist_ok=True)
    (d / "analysis").mkdir(parents=True, exist_ok=True)
    where = {"research": "research/%s.json" % slug, "sidecar": "research/%s_open.json" % slug,
             "analysis": "analysis/%s.json" % slug, "scores": "scores.json"}
    for key, rel in where.items():
        if files.get(key) is not None:
            (d / rel).write_text(json.dumps(files[key]), encoding="utf-8")
    return d


def good_folder(root: Path, slug: str, name: str = "", **over) -> Path:
    files = {"research": GOOD_RESEARCH, "sidecar": {"engine": "open", "disputed": False}, "analysis": GOOD_ANALYSIS,
             "scores": {NUM: {"tier": "Dial First"}}}
    files.update(over)
    return folder(root, slug, name, **files)


def result(slug: str, run_dir, **over) -> dict:
    """A deep prospect's result as the worker keeps it on the task, with nothing thin about it."""
    r = {"slug": slug, "street": slug.replace("-", " "), "run_dir": str(run_dir), "pdf": "", "pdf_name": "",
         "headline": "Owner researched.", "owner_status": "Living", "decision_maker": "Corwin Ashgrove",
         "must_sign": [{"who": "Corwin Ashgrove", "status": "Required", "why": "owner", "short": "Corwin"}],
         "call_order": [{"who": "Corwin Ashgrove", "number": NUM, "why": "own mobile"}], "open_items": [],
         "written_back": False, "live": True, "skipped": [], "spent_usd": 1.1, "log": ["== final analysis ok ($0.10)"],
         "order": "trace_first", "research_level": 2, "verified": [], "heir_shares": None, "writeback": ""}
    r.update(over)
    return r


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def env(**values):
    """These environment settings exactly as given for the block, put back afterwards."""
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _wipe(store) -> None:
    """An empty autopilot memory, so a record id or a day's handoffs from an earlier check cannot leak in.
    Only ever on a temp database: anything else is refused before a row is touched."""
    import tempfile
    db = str(Path(config.DB_PATH).resolve()).lower()
    if not db.startswith(str(Path(tempfile.gettempdir()).resolve()).lower()):
        raise RuntimeError("refusing to wipe %s: a selftest wipes only a database in the temp folder" % config.DB_PATH)
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.MODE,
            config.RUNS_DIR, config.DB_PATH)
    saved_env = {k: os.environ.get(k) for k in ENGINE_ENV}
    saved_presets = dict(PRESETS)
    # Its own database, always. Run on its own (outside selftest.main), a section that wipes the autopilot
    # tables wipes the workstation's real agents.db: the re-open section did exactly that on 2026-11-19,
    # and an earlier standalone run had left a probate fixture batch ("pn_due") in it.
    config.DB_PATH = tmp / "selftest_research2.db"
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.RESEARCH_DAILY_CAP_USD = 100000.0
    config.MODE = "live"
    store.init()
    for k in ENGINE_ENV:
        os.environ.pop(k, None)
    os.environ["AGENTS_RESEARCH_ENGINE"] = "open"      # the box's engine: the only one that reads a depth
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
    from .research import open_research as orx
    _wipe(store)
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    o = rq.options(None)
    runs = tmp / "r2_runs"
    utc_day = datetime.now(timezone.utc).isoformat()[:10]
    calls: list[tuple[str, dict]] = []

    def enq(agent, wf, u, subject, **kw):
        """The brain's own queue, so a deeper pass is a real task row carrying its payload."""
        calls.append((u, dict(kw)))
        return store.enqueue(agent, wf, u, subject, **kw)

    def with_stage(**params):
        p = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
        p.stage(STAGE).params.update(params)
        return p

    def researched(u, slug, res_over=None, files_over=None, status="done", payload=None, name=""):
        """A research task on the record that has already run, as the worker leaves it."""
        d = good_folder(runs, slug, name, **(files_over or {}))
        tid, _ = store.enqueue("research", "deep_prospect", u, slug, force=True, payload=payload)
        if status == "done":
            store.finish(tid, "done", result(slug, d, **(res_over or {})))
        else:
            store.finish(tid, status, {}, error="boom")
        return tid

    def verdict(slug, res_over=None, files_over=None, opts=None):
        d = good_folder(tmp / "r2_judge", slug, **(files_over or {}))
        return rq.judge(result(slug, d, **(res_over or {})), opts or o)

    # ------------------------------------------------------------------ the five rules
    print("  thin research")
    good = verdict("j-good")
    check(good["verdict"] == "enough" and good["reasons"] == [] and good["unjudged"] == [],
          "a finished file with the owner's status, a signer, a Dial First number and two sources is enough")
    v = verdict("j-unknown", {"owner_status": "Unknown"})
    check(v["verdict"] == "thin" and v["reasons"] == [rq.THIN_WORDS["owner_unknown"]],
          "the owner's status came back Unknown: thin")
    nobody = {"analysis": {"owner_status": "Deceased", "decision_maker": "", "must_sign": [],
                           "people": [{"name": "Marigold Ashgrove", "role": "none", "living": "Deceased"}]}}
    v = verdict("j-nobody", {"decision_maker": "", "must_sign": [], "call_order": []}, nobody)
    check(v["verdict"] == "thin" and rq.THIN_WORDS["no_decision_maker"] in v["reasons"]
          and rq.THIN_WORDS["no_number"] not in v["reasons"],
          "nobody named to sign or decide: thin, and the number rule does not pile on where there is nobody to reach")
    role = {"analysis": {"owner_status": "Deceased", "decision_maker": "Not Determined",
                         "must_sign": [{"who": "the heirs of Marigold Ashgrove"}], "people": []}}
    v = verdict("j-role", {"decision_maker": "Not Determined", "must_sign": [{"who": "the heirs of Marigold Ashgrove"}]}, role)
    check(rq.THIN_WORDS["no_decision_maker"] in v["reasons"],
          "a line about a role ('the heirs of ...') and a decision maker of 'Not Determined' name nobody")
    owner = {"analysis": {"owner_status": "Living", "decision_maker": "", "must_sign": [],
                          "people": [{"name": "Corwin Ashgrove", "role": "owner", "living": "Living"}]}}
    v = verdict("j-owner", {"decision_maker": "", "must_sign": []}, owner)
    check(v["verdict"] == "enough", "a living owner the analysis names counts as the person who signs")
    v = verdict("j-third", files_over={"scores": {NUM: {"tier": "Dial Third"}}})
    check(v["verdict"] == "thin" and v["reasons"] == [rq.THIN_WORDS["no_number"]],
          "a signer is named but the only number is Dial Third: thin")
    dead_line = {"analysis": {"owner_status": "Living", "decision_maker": "Corwin Ashgrove",
                              "must_sign": [{"who": "Corwin Ashgrove"}],
                              "people": [{"name": "Corwin Ashgrove", "role": "owner", "living": "Living"},
                                         {"name": "Marigold Ashgrove", "role": "none", "living": "Deceased"}]}}
    v = verdict("j-deadline", {"call_order": [{"who": "Marigold Ashgrove", "number": NUM}]}, dead_line)
    check(v["verdict"] == "thin" and rq.THIN_WORDS["no_number"] in v["reasons"],
          "a Dial First number on the call order that belongs to a person recorded as dead reaches nobody")
    v = verdict("j-dry", {"live": False, "call_order": [], "log": []})
    check(v["verdict"] == "enough" and any("dry run" in x for x in v["unjudged"]),
          "a dry run's empty call order is not judged: a dry run traces and scores nothing")
    v = verdict("j-capped", {"skipped": ["smartskip skipped: about $0.15 would pass the cap (run $5)"], "call_order": []})
    check(v["verdict"] == "enough" and any("cap" in x for x in v["unjudged"]),
          "a run a cap cut short is not judged on its numbers: the missing number is the cap's doing")
    v = verdict("j-nofinal", {"call_order": [], "log": []})
    check(v["verdict"] == "enough" and any("final analysis did not run" in x for x in v["unjudged"]),
          "a live run whose final analysis never ran is not judged on its numbers either")
    v = verdict("j-dispute", files_over={"sidecar": {"engine": "open", "disputed": True}})
    check(v["verdict"] == "thin" and v["reasons"] == [rq.THIN_WORDS["dispute"]],
          "the two researchers disagreed and nobody settled it: thin")
    flagged = dict(GOOD_RESEARCH, red_flags=["DISPUTED, needs a person to check before anyone is called: x"])
    v = verdict("j-flag", files_over={"sidecar": None, "research": flagged})
    check(rq.THIN_WORDS["dispute"] in v["reasons"], "and so is a file carrying the engine's DISPUTED red flag")
    bare = dict(GOOD_RESEARCH, owner_status_evidence=[{"claim": "x", "url": "http://notices.example.test/a/", "quote": "q"}],
                identity_proof=[{"claim": "y", "url": "https://notices.example.test/a#top", "quote": "q"}], people=[])
    v = verdict("j-thinsrc", files_over={"research": bare})
    check(v["verdict"] == "thin" and v["reasons"] == [rq.THIN_WORDS["few_sources"] % 2],
          "one page cited twice is one source, and fewer than two sources is thin")
    v = verdict("j-onesrc", files_over={"research": bare}, opts=dict(o, thin_min_sources=1))
    check(v["verdict"] == "enough", "the source floor is a setting (thin_min_sources)")
    v = verdict("j-rules", {"owner_status": "Unknown"}, opts=dict(o, thin_rules=["no_number"]))
    check(v["verdict"] == "enough", "the rules are a setting: with only no_number on, an Unknown owner alone is not thin")
    stopped = rq.judge(result("j-stop", "nowhere", stopped="The deed records show the owner no longer owns this house."), o)
    check(rq.judge({}, o)["verdict"] == "unjudged" and stopped["verdict"] == "unjudged" and stopped.get("stopped")
          and rq.judge(result("j-none", "nowhere", owner_status="", headline=""), o)["verdict"] == "unjudged",
          "what is not research is never judged: an empty result, a run the deed stopped, a run with no analysis")
    away = rq.judge(result("j-away", str(tmp / "r2_not_here")), o)
    check(away["verdict"] == "enough" and any("not on this box" in x for x in away["unjudged"]),
          "a result whose run folder is not on this box is judged on what it carries; a call order with a number gets the benefit of the doubt")

    # ------------------------------------------------------------------ tax owed, read off the record's own field
    print("  tax owed")
    day = T0.astimezone(timezone(timedelta(hours=-5))).date()
    owes = R("r2_tax", "291 Thin St", tax_delinquent_value=500)
    paid = rq.signals_of(owes, {"Tax delinquency amount": 0}, o, today=day)
    paid_text = rq.signals_of(owes, {"Tax delinquency amount": "0.00"}, o, today=day)
    blank = rq.signals_of(owes, {}, o, today=day)
    check(paid["tax_owed"] == 0 and paid_text["tax_owed"] == 0 and rq.rank(paid, day, o).reason == "probate"
          and "behind on taxes" not in paid["distress"],
          "a record whose own tax field reads 0 ranks as owing nothing, whatever the vendor's stale figure says")
    check(blank["tax_owed"] == 500 and rq.rank(blank, day, o).reason == "probate, owes $500 in county tax",
          "with the record's own field empty, the vendor's figure is still read")

    # ------------------------------------------------------------------ the depth both modules agree on
    print("  the depth a deeper pass carries")
    check(orx.depth_of(None) == {} and orx.depth_of({}) == {} and orx.depth_of({"research_depth": {}}) == {}
          and orx.depth_of({"research_depth": "deeper"}) == {} and orx.depth_of("research_depth") == {},
          "a payload without a depth, every first pass included, reads as no depth at all")
    d = orx.depth_of({"research_depth": {"searches": 999, "pages": -3, "referee": "Always", "lookups_first": "yes",
                                         "why": ["b" * 500] + ["a"] * 9, "cap_usd": 50, "after_task": "x"}})
    check(d["pass"] == 2 and d["searches"] == 40 and "pages" not in d and d["referee"] == "always"
          and d["lookups_first"] is False and len(d["why"]) == 6 and len(d["why"][0]) == 200 and d["cap_usd"] == 5.0
          and d["after_task"] == 0,
          "a depth is clamped and read strictly: 999 searches is 40, a bad number is dropped, only a real true turns a switch on")
    sent = orx.depth_of(rq.depth_payload(o, {"reasons": [rq.THIN_WORDS["owner_unknown"]], "task": 7}))
    check(sent["searches"] == 24 and sent["pages"] == 20 and sent["referee"] == "always" and sent["referee_searches"] == 10
          and sent["referee_pages"] == 12 and sent["family_lookups"] == 8 and sent["lookups_first"] is True
          and sent["cap_usd"] == 2.0 and sent["why"] == [rq.THIN_WORDS["owner_unknown"]] and sent["after_task"] == 7,
          "what the queue writes is exactly what the research reads: the two modules agree on every setting")
    info = dict(rq.SLOT, tasks=2, done=True, failed=True, deeper_tasks=1, deeper_failed=True, last_deeper=True,
                finished="2026-01-01T00:00:00+00:00", done_task=1)
    check(rq.eligible(info, T0, dict(o, max_attempts=9, retry_days=0)) == (False, "researched twice, the second time deeper", False),
          "a record whose deeper pass failed is never retried, however many tries the playbook allows a first pass")

    # ------------------------------------------------------------------ the queue: plan, then live
    print("  the deeper pass in the queue")
    crm = CardCRM([R("r2_thin", "201 Thin St"), R("r2_good", "202 Thin St"), R("r2_new", "203 Thin St"),
                   R("r2_lead", "204 Thin St", status="Hot Lead"), R("r2_heir", "205 Thin St", tags=["R2", "Heir Identified"]),
                   R("r2_ni", "206 Thin St", status="not_interested")])
    t_thin = researched("r2_thin", "201-thin-st", {"owner_status": "Unknown"})
    researched("r2_good", "202-thin-st")
    for u, slug in (("r2_lead", "204-thin-st"), ("r2_heir", "205-thin-st"), ("r2_ni", "206-thin-st")):
        researched(u, slug, {"owner_status": "Unknown"})
    plan = sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq, plan=True)
    check(not crm.changed() and not calls and not ledger.events(action="handoff"),
          "a planned run writes nothing and hands nothing over, the deeper pass included")
    check("Would send 1 record(s) back for a deeper pass, about $2.50" in plan.head and "Would hand 1 record(s) to research" in plan.head
          and abs(plan.would_usd - (1.21 + 2.50)) < 1e-6 and plan.acted == 2,
          "the plan says what the deeper pass would cost, apart from the first passes: %s" % plan.head)
    check(any(ln.startswith("201 Thin St, Knoxville: a deeper pass") and "Unknown" in ln for ln in plan.lines),
          "the plan's line names the house and why its first research came back thin")
    seen = ledger.fact("r2_thin", rq.THIN_FACT)
    check(isinstance(seen, dict) and seen["verdict"] == "thin" and seen["task"] == t_thin
          and seen["reasons"] == [rq.THIN_WORDS["owner_unknown"]],
          "the verdict is remembered in plan mode: it is knowledge about the record, not an action")
    check(ledger.fact("r2_thin", rq.DUE_FACT) is None and not rq.waiting_for_deeper("r2_thin"),
          "a plan run's verdict holds no other stage up: in plan mode the deeper pass never comes")
    t = plan.tally
    check(t.get("already researched") == 1, "research that came back with enough is left alone: %s" % dict(t))
    check(t.get("not queued: a lead, left to the lead manager") == 1, "a lead is never sent back for a deeper pass")
    check(t.get("not queued: already settled (tag Heir Identified)") == 1,
          "a record whose heirs are settled is not sent back, however thin its file reads")
    check(t.get("held back: status not_interested") == 1, "the gate still holds a thin record whose status says no")

    base = store.spent(day=utc_day, research=True)
    live = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=30), pol=pol, enqueue=enq)
    deep_calls = [kw for u, kw in calls if u == "r2_thin"]
    first_calls = [kw for u, kw in calls if u == "r2_new"]
    check(len(deep_calls) == 1 and deep_calls[0].get("force") is True and rq.DEPTH_KEY in (deep_calls[0].get("payload") or {}),
          "a live run sends the thin record back once, its depth in the task's payload, forced past the 24-hour repeat guard")
    depth = deep_calls[0]["payload"][rq.DEPTH_KEY] if deep_calls else {}
    check(depth.get("searches") == 24 and depth.get("pages") == 20 and depth.get("referee") == "always"
          and depth.get("lookups_first") is True and depth.get("family_lookups") == 8 and depth.get("after_task") == t_thin
          and depth.get("why") == [rq.THIN_WORDS["owner_unknown"]],
          "the deeper settings: more searches and page opens, the referee on every record, the family's index rows and the free lookups first, and why")
    check(first_calls == [{"source": "autopilot:%s" % STAGE}],
          "a first pass is handed exactly as before: no payload and no force, so every first-pass default stays as it was")
    idx = rq.research_index()["r2_thin"]
    check(idx["deeper_tasks"] == 1 and idx["deeper_open"] and idx["deeper_handoffs"] == 1 and rq.waiting_for_deeper("r2_thin"),
          "the deeper task is on the brain's queue with its payload, and another stage can see a deeper pass is coming")
    ev = ledger.events("r2_thin", "handoff")
    check(len(ev) == 1 and ev[0]["detail"].get("depth") == "deeper" and ev[0]["detail"].get("price") == 2.5
          and ev[0]["detail"].get("after_task") == t_thin and ev[0]["detail"].get("thin") == [rq.THIN_WORDS["owner_unknown"]],
          "the handoff is in the ledger as the deeper pass, at its own price, with the task it follows")
    check("Research Queued" in crm.recs["r2_thin"]["tags"] and len([c for c in crm.cards.values() if c["prop_uuid"] == "r2_thin"]) == 1,
          "the record is marked queued again, with one card")
    check("Sent 1 record(s) back for a deeper pass, about $2.50" in live.head
          and any(ln.startswith("201 Thin St, Knoxville: a deeper pass, the first came back thin") for ln in live.lines),
          "the live run says what it sent and why: %s" % live.head)
    ctx = SimpleNamespace(now=T0 + timedelta(minutes=30), policy=pol)
    b = rq.research_budget(ctx, rq.options(pol.stage(STAGE)))
    db = rq.deeper_budget(ctx, rq.options(pol.stage(STAGE)))
    check(abs(b["used"] - base - (1.21 + 2.50)) < 0.011 and abs(db["used"] - 2.50) < 1e-6,
          "a deeper pass counts against the day's research budget at its own price until its task finishes, and against its own budget")

    calls.clear()
    sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=60), pol=pol, enqueue=enq)
    check(not [u for u, _ in calls if u == "r2_thin"], "while the deeper pass is queued the record is not sent again")

    store.finish(idx["task"], "done", result("201-thin-st", runs / "201-thin-st", owner_status="Unknown",
                                             research_depth={"pass": 2}))
    calls.clear()
    third = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=90), pol=pol, enqueue=enq)
    check(not [u for u, _ in calls if u == "r2_thin"], "a deeper pass that is still thin is never sent a third time")
    check(TAG in crm.recs["r2_thin"]["tags"] and ("add_tags", "r2_thin", (TAG,)) in crm.writes
          and "Marked 1 record(s) for a person" in third.head,
          "it gets the person tag instead: %s" % third.head)
    mark = ledger.fact("r2_thin", rq.PERSON_FACT)
    check(isinstance(mark, dict) and mark.get("tagged") and ledger.events("r2_thin", "research_person")
          and any(c["what"] == "add_tags" and c["after"] == [TAG] for c in ledger.changes(record_uuid="r2_thin")),
          "the mark is remembered, said in the ledger and recorded as a change that can be put back")
    card = next(c for c in crm.cards.values() if c["prop_uuid"] == "r2_thin")
    check("Research Queued" not in crm.recs["r2_thin"]["tags"] and card["column"] == DONE,
          "when the deeper pass finishes the queue's own tag comes off and the card moves on, as after a first pass")
    check(any(ln.startswith("201 Thin St, Knoxville: left for a person after a deeper pass") for ln in third.lines),
          "the line names the house and says it is left for a person")
    crm.recs["r2_thin"]["tags"].remove(TAG)
    before = len(crm.writes)
    calls.clear()
    fourth = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=120), pol=pol, enqueue=enq)
    check(TAG not in crm.recs["r2_thin"]["tags"] and fourth.tally.get("left for a person after a deeper pass") == 1
          and not [w for w in crm.writes[before:] if w[1] == "r2_thin"] and not [u for u, _ in calls if u == "r2_thin"],
          "a person who takes the tag off has taken it off: it is never written again, and the record is never sent again")

    # a deeper pass that comes back with enough
    _wipe(store)
    crm = CardCRM([R("r2_ok", "211 Thin St")])
    nobody_files = {"analysis": {"owner_status": "Deceased", "decision_maker": "", "must_sign": [], "people": []}}
    researched("r2_ok", "211-thin-st", {"decision_maker": "", "must_sign": [], "call_order": []}, nobody_files)
    calls.clear()
    sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq)
    check(len(calls) == 1 and calls[0][1].get("payload"), "nobody named to sign: the record goes back for a deeper pass")
    d_ok = good_folder(runs, "211-thin-st", "211-thin-st-pass2")
    store.finish(rq.research_index()["r2_ok"]["task"], "done", result("211-thin-st", d_ok, research_depth={"pass": 2}))
    calls.clear()
    after = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=30), pol=pol, enqueue=enq)
    check(not calls and TAG not in crm.recs["r2_ok"]["tags"] and after.tally.get("researched twice, the second time deeper") == 1,
          "a deeper pass that came back with enough settles it: no person tag, and no third pass")

    # a deeper pass that fails
    _wipe(store)
    crm = CardCRM([R("r2_fail", "221 Thin St")])
    researched("r2_fail", "221-thin-st", {"owner_status": "Unknown"})
    calls.clear()
    sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq)
    store.finish(rq.research_index()["r2_fail"]["task"], "failed", {}, error="boom")
    calls.clear()
    lenient = with_stage(max_attempts=5, retry_days=0)
    sweep.run(STAGE, crm=crm, now=T0 + timedelta(days=1), pol=lenient, enqueue=enq)
    check(not calls, "a deeper pass that failed is never retried, even where the playbook would retry a failed first pass")
    check(TAG in crm.recs["r2_fail"]["tags"] and "the deeper pass failed" in str((ledger.fact("r2_fail", rq.PERSON_FACT) or {}).get("why")),
          "it goes to a person, saying the deeper pass failed")
    check("Research Queued" not in crm.recs["r2_fail"]["tags"],
          "and the queue's mark comes off: a failed deeper pass is finished, not still waiting for research")

    # ------------------------------------------------------------------ the caps
    print("  the deeper pass's own caps")
    _wipe(store)
    crm = CardCRM([R("r2_cap1", "231 Thin St"), R("r2_cap2", "232 Thin St")])
    researched("r2_cap1", "231-thin-st", {"owner_status": "Unknown"})
    researched("r2_cap2", "232-thin-st", {"owner_status": "Unknown"})
    calls.clear()
    one = sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq)
    waits = [u for u in ("r2_cap1", "r2_cap2") if u not in [x for x, _ in calls]]
    check(len(calls) == 1 and one.tally.get("a deeper pass waits: this run's deeper passes are used") == 1,
          "one deeper pass a run (deeper_max_per_run): the second thin record waits")
    check("1 thin record(s) wait for a deeper pass, about $2.50 at $2.50 a record" in one.head
          and "1 thin record(s) wait for a deeper pass" in rq.digest_line(),
          "the run and the morning line say how many thin records wait and what they would cost")
    check(len(waits) == 1 and rq.waiting_for_deeper(waits[0]) and isinstance(ledger.fact(waits[0], rq.DUE_FACT), dict),
          "a live run that could not send one yet says so where another stage can read it")

    _wipe(store)
    crm = CardCRM([R("r2_day1", "241 Thin St"), R("r2_day2", "242 Thin St")])
    researched("r2_day1", "241-thin-st", {"owner_status": "Unknown"})
    researched("r2_day2", "242-thin-st", {"owner_status": "Unknown"})
    daily = with_stage(deeper_daily_usd=3.0)
    calls.clear()
    sweep.run(STAGE, crm=crm, now=T0, pol=daily, enqueue=enq)
    calls.clear()
    held = sweep.run(STAGE, crm=crm, now=T0 + timedelta(minutes=30), pol=daily, enqueue=enq)
    check(not calls and held.tally.get("a deeper pass waits: the day's deeper-pass budget is used") == 1
          and "deeper-pass budget is used ($2.50 of $3.00)" in held.head,
          "deeper passes have their own day budget: $3.00 buys one at $2.50, and the run says which budget held: %s" % held.head)
    sweep.run(STAGE, crm=crm, now=T0 + timedelta(days=1), pol=daily, enqueue=enq)
    check(len(calls) == 1, "the deeper budget is a day's: the next day it sends the next one")

    _wipe(store)
    crm = CardCRM([R("r2_tight", "251 Thin St"), R("r2_fresh", "252 Thin St")])
    researched("r2_tight", "251-thin-st", {"owner_status": "Unknown"})
    base = store.spent(day=utc_day, research=True)
    calls.clear()
    tight = sweep.run(STAGE, crm=crm, now=T0, pol=with_stage(daily_usd=base + 2.0), enqueue=enq)
    check([u for u, _ in calls] == ["r2_fresh"] and tight.tally.get("a deeper pass waits: the day's research budget is used") == 1,
          "a deeper pass also draws on the research budget: $2.00 left holds the $2.50 deeper pass and still buys a $1.21 first pass")

    _wipe(store)
    crm = CardCRM([R("r2_claude", "261 Thin St")])
    researched("r2_claude", "261-thin-st", {"owner_status": "Unknown"})
    calls.clear()
    with env(AGENTS_RESEARCH_ENGINE="claude"):
        claude = sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq)
    check(not calls and claude.tally.get("thin research waits: the deeper pass needs the low-cost research engine") == 1
          and any("low-cost research engine" in p for p in claude.problems),
          "on a box running the Claude researcher a thin record is not sent back: that engine reads no depth and would only repeat the first pass")

    _wipe(store)
    crm = CardCRM([R("r2_off", "271 Thin St")])
    researched("r2_off", "271-thin-st", {"owner_status": "Unknown"})
    calls.clear()
    off = sweep.run(STAGE, crm=crm, now=T0, pol=with_stage(deeper=False), enqueue=enq)
    check(not calls and off.tally.get("already researched") == 1 and ledger.fact("r2_off", rq.THIN_FACT) is None,
          "with the deeper pass switched off a thin record is simply already researched, and nothing is judged")
    later = sweep.run(STAGE, crm=crm, now=T0, pol=with_stage(max_judge=0), enqueue=enq, plan=True)
    check(later.tally.get("research to judge on a later run") == 1 and ledger.fact("r2_off", rq.THIN_FACT) is None,
          "a run reads at most max_judge new results; the rest are judged on a later run")

    _wipe(store)
    crm = CardCRM([R("r2_plan", "281 Thin St")])
    researched("r2_plan", "281-thin-st", {"owner_status": "Unknown"})
    researched("r2_plan", "281-thin-st", {"owner_status": "Unknown", "research_depth": {"pass": 2}},
               payload={rq.DEPTH_KEY: {"pass": 2}}, name="281-thin-st-pass2")
    calls.clear()
    planned = sweep.run(STAGE, crm=crm, now=T0, pol=pol, enqueue=enq, plan=True)
    check(not crm.changed() and not calls and planned.tally.get("would mark for a person after a deeper pass") == 1
          and "Would mark 1 record(s) for a person" in planned.head and ledger.fact("r2_plan", rq.PERSON_FACT) is None,
          "a plan run says it would mark the record for a person, writes nothing and remembers no mark")

    # ------------------------------------------------------------------ the research engine at depth
    print("  the research engine at depth")
    slug = "401-depth-ln"
    events: list[str] = []
    asks: list[dict] = []
    lookups: list[tuple] = []
    shaped_json = {"owner_of_record": "Corwin Ashgrove", "owner_status": "Living",
                   "owner_status_evidence": [{"claim": "named in a 2026 notice", "url": "https://obits.example.test/a",
                                              "quote": "q"}], "red_flags": [], "unresolved": []}

    def make_run(root: Path) -> FakeRun:
        rn = FakeRun(root)
        rn.save("records.json", [{"slug": slug, "record": {"owner": {"first_name": "Corwin", "last_name": "Ashgrove"},
                                                          "address": {"county": "Knox"}}}])
        rn.save("people.json", [{"slug": slug, "people": [{"key": "p1", "first": "Corwin", "last": "Ashgrove",
                                                           "is_subject": True, "phones": [{"number": "8650000401"}]}]}])
        return rn

    def fake_lookup(last, first=""):
        events.append("index %s" % first)
        lookups.append((last, first))
        return "No obituary in the index for %s %s. That is not proof the person is alive." % (first, last)

    def fake_investigate(model, system, ask, web, searches, pages):
        events.append("model %s" % model)
        asks.append({"model": model, "system": system, "ask": ask, "searches": searches, "pages": pages})
        web.seen.add("https://obits.example.test/a")
        fact = {"owner_status": "Living", "decedents": [], "must_sign": ["Thaddeus Ashgrove"]}
        return {"model": model, "memo": "memo " + "x" * 500 + "\nFACT_SHEET_JSON: " + json.dumps(fact), "fact": fact,
                "error": "", "seconds": 1, "searches": 3, "pages": 2, "usd": 0.01, "prompt_tokens": 1,
                "completion_tokens": 1, "steps": 1, "hosts": {}}

    def fake_chat(model, messages, tools=None, max_tokens=20000):
        return {"choices": [{"message": {"content": json.dumps(shaped_json)}}], "usage": {"cost": 0.001}}

    with patched(orx, investigate=fake_investigate, lookup_obituary_index=fake_lookup, chat=fake_chat):
        first = make_run(tmp / "r2_engine" / "first")
        orx.research(first, {"street": "401 Depth Ln"})
        side = first.load("research/%s_open.json" % slug)
        check([a["searches"] for a in asks] == [14, 14] and [a["pages"] for a in asks] == [12, 12],
              "a first pass runs at the defaults it always had: 14 searches and 12 page opens for each researcher")
        check(all("first_pass" not in a["ask"] and "obituary_index_for_family" not in a["ask"]
                  and not a["ask"].startswith(orx.DEEPER_ASK) for a in asks),
              "and its brief carries nothing a deeper pass adds")
        check(len(asks) == 2 and side["referee"] is None and "depth" not in side,
              "two researchers who agree on a first pass call no referee, and its sidecar records no depth")
        check(lookups == [("Ashgrove", "Corwin")], "a first pass asks the obituary index about the owner only")

        second = make_run(tmp / "r2_engine" / "second")
        second.save("analysis/%s.json" % slug, {
            "owner_status": "Unknown", "decision_maker": "", "must_sign": [{"who": "Delphine Ashgrove"}],
            "people": [{"name": "Delphine Ashgrove", "relationship": "daughter", "living": "Unknown", "role": "signer"},
                       {"name": "Thaddeus Ashgrove", "relationship": "son", "living": "Living", "role": "signer"}],
            "open_items": ["Delphine's status is unverified"]})
        second.save("research/%s.json" % slug, {"owner_status": "Unknown", "people": [], "red_flags": [],
                                                "decedents": [{"name": "Marigold Ashgrove", "dod": "2025-03-04"}],
                                                "unresolved": ["Cross-check note: one researcher names Delphine"]})
        deep = orx.depth_of(rq.depth_payload(o, {"reasons": [rq.THIN_WORDS["owner_unknown"]], "task": 11}))
        events.clear()
        asks.clear()
        lookups.clear()
        orx.research(second, {"street": "401 Depth Ln"}, depth=deep)
        side = second.load("research/%s_open.json" % slug)
        researchers = [a for a in asks if a["model"] != orx.DEFAULT_REFEREE]
        referee = [a for a in asks if a["model"] == orx.DEFAULT_REFEREE]
        check([a["searches"] for a in researchers] == [24, 24] and [a["pages"] for a in researchers] == [20, 20]
              and all("at most 24 searches and 20 page opens" in a["system"] for a in researchers),
              "a deeper pass gives each researcher 24 searches and 20 page opens, and its rules say so")
        first_model = next(i for i, e in enumerate(events) if e.startswith("model"))
        check({("Ashgrove", "Delphine"), ("Ashgrove", "Thaddeus"), ("Ashgrove", "Marigold")} <= set(lookups)
              and all(i < first_model for i, e in enumerate(events) if e.startswith("index")),
              "the obituary index is asked about every person the first pass named, before any model runs")
        ask = researchers[0]["ask"] if researchers else ""
        check(ask.startswith(orx.DEEPER_ASK) and '"first_pass"' in ask and rq.THIN_WORDS["owner_unknown"] in ask
              and '"obituary_index_for_family"' in ask and "Delphine's status is unverified" in ask,
              "the deeper brief opens with the first pass's open points and carries what it concluded and the family's index rows")
        check(len(referee) == 1 and referee[0]["searches"] == 10 and referee[0]["pages"] == 12
              and referee[0]["ask"].startswith(orx.REFEREE_DEEPER_ASK) and side["lead"] == orx.DEFAULT_REFEREE,
              "the referee settles every deeper pass, disputed or not, on its own deeper budget, and its memo is the one that ships")
        check((side.get("depth") or {}).get("pass") == 2, "the sidecar records the depth the pass ran at")
        with env(AGENTS_RESEARCH_REFEREE="off"):
            asks.clear()
            orx.research(make_run(tmp / "r2_engine" / "third"), {"street": "401 Depth Ln"}, depth=deep)
            deep_ref = [a for a in asks if a["model"] == orx.DEFAULT_REFEREE]
            asks.clear()
            orx.research(make_run(tmp / "r2_engine" / "fourth"), {"street": "401 Depth Ln"})
            first_ref = [a for a in asks if a["model"] == orx.DEFAULT_REFEREE]
        check(len(deep_ref) == 1 and not first_ref,
              "a box with its referee switched off still gives a deeper pass one, and still gives a first pass none")

    # ------------------------------------------------------------------ the deep prospect reads its depth
    print("  the deep prospect reads its depth")
    _deep_prospect(check, tmp, store, rq, orx, o)

    # ------------------------------------------------------------------ hygiene
    print("  source hygiene")
    src = Path(__file__).resolve().parent
    files = [src / "autopilot" / "researchqueue.py", src / "research" / "deep_prospect.py",
             src / "research" / "open_research.py", Path(__file__).resolve()]
    text = [f.read_text(encoding="utf-8") for f in files]
    check(all(chr(8212) not in t and chr(8211) not in t and chr(8) not in t for t in text),
          "no long dash and no backspace byte in the queue, the two research modules or this test")


def _deep_prospect(check, tmp: Path, store, rq, orx, o) -> None:
    """deep_prospect.run with every vendor, model and DataSift step replaced by a recorder, in dry mode."""
    import deep_prospect_record as dpr
    import dream100_dp as dp
    from .research import deep_prospect, writeback as wb
    u, slug = "r2_dp", "301-depth-ln"
    rec = {"uuid": u, "address": {"street": "301 Depth Ln", "city": "Knoxville", "county": "Knox"},
           "owner": {"first_name": "Corwin", "last_name": "Ashgrove"}, "notes": "Notice to creditors of the estate"}
    row_ = {"uuid": u, "owner_uuid": "o-r2-dp", "street": "301 Depth Ln", "city": "Knoxville", "zip": "37900",
            "owner": "Corwin Ashgrove", "mail_street": "301 Depth Ln", "lists": ["Probate"]}
    steps: list[str] = []
    seen: list[tuple] = []

    def records(args, rn, row):
        steps.append("records")
        rn.save("records.json", [{"rank": 1, "slug": slug, "uuid": u, "owner_uuid": "o-r2-dp", "row": dict(row),
                                  "record": rec, "messages": [], "custom_fields": [], "timeline": [],
                                  "title_owner": "ASHGROVE CORWIN"}])

    def research(*a, **k):
        steps.append("research")
        seen.append((len(a), dict(k)))
        return {}, 0.02

    def analyze(rn, row, final):
        return {}, 0.0

    def trace(recs):
        steps.append("trace")
        return [{}]

    def index(last, first=""):
        steps.append("index")
        return "No obituary in the index for %s %s." % (first, last)

    def quiet(*a, **k):
        return None

    deep_engine = SimpleNamespace(name="open", research=research, analyze=analyze, cap=0.5, label="research (fake)",
                                  takes_depth=True)
    flat_engine = SimpleNamespace(name="claude", research=research, analyze=analyze, cap=0.5, label="research (fake)")
    cleared = {k: None for k in wb.SETTING_NAMES}
    cleared["AGENTS_DEED_DIR"] = str(tmp / "r2_no_deeds")
    config.RUNS_DIR, config.MODE = tmp / "r2_dp_runs", "dry"

    def go(engine, payload=None) -> dict:
        steps.clear()
        seen.clear()
        tid, _ = store.enqueue("research", "deep_prospect", u, subject="selftest", force=True, payload=payload)
        task = store.task(tid)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patched(dpr, row_for=lambda uuid, crm=None: dict(row_), patch=lambda row: None,
                                        phase_records=records, phase_merge=quiet, heir_rows=lambda rn, row: ([], []),
                                        phase_heirs=quiet, phase_gapfill=quiet, verify_pairs=lambda rn, row: [],
                                        phase_verify=quiet, phase_build=quiet, phase_writeback=quiet))
            stack.enter_context(patched(dp, load_env=lambda: {}, smartskip_rows=trace, phase_smartskip=quiet,
                                        numbers_to_score=lambda rn: {}, phase_score=quiet))
            stack.enter_context(patched(orx, engine=lambda: engine, lookup_obituary_index=index))
            stack.enter_context(env(**cleared))
            return deep_prospect.run(task)

    look = config.RUNS_DIR / slug / "free_look.json"
    r = go(deep_engine)
    check(seen == [(2, {})] and "research_depth" not in r and not look.exists() and steps == ["records", "trace", "research"],
          "a task with no payload runs today's chain exactly: the engine gets its two arguments, nothing more, and no free look is read")
    payload = rq.depth_payload(o, {"reasons": [rq.THIN_WORDS["owner_unknown"]], "task": 5})
    r = go(deep_engine, payload)
    got = seen[0][1].get("depth") if seen else {}
    check(seen and seen[0][0] == 2 and got == orx.depth_of(payload) and got.get("searches") == 24,
          "a task carrying a depth hands it to an engine that takes one")
    check(steps[:4] == ["records", "index", "trace", "research"] and isinstance(json.loads(look.read_text(encoding="utf-8")), dict)
          and any(x.startswith("== free look ok") for x in r["log"]),
          "a deeper pass reads the free lookups first (the notice, the obituary index, a saved deed) even in today's trace-first order")
    check((r.get("research_depth") or {}).get("pass") == 2
          and any(x == "== deeper pass: %s" % rq.THIN_WORDS["owner_unknown"] for x in r["log"]),
          "its result says it was the deeper pass, and its log says why it ran")
    r = go(flat_engine, payload)
    check(seen == [(2, {})] and any("takes no depth" in x for x in r["log"]),
          "an engine that reads no depth gets today's two-argument call even on a deeper pass, and the log says so")
