"""Lanes selftest: the channel map, the five-attempt round, tier discipline, the second version of
the trace waterfall, research by list, returned mail, tranches and lane caps, the hot auction
tasks and the deed check, second chances, the weekly caller book and the dialer file.

Zero network. The same fake DataSift as the autopilot selftest, extended with the documented task
and scheduled-export routes; fake skip trace sources, a fake scorer and a fake county roll. Every
name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import ADRIANA, EMPLOYEES, ET, MARCUS, PRESETS, T0, TINAA, FakeCRM, FakeScorer, FakeSource, _rec
from .selftest_phase0 import MOBILE1, _et, _ts, dial, status_ev

# The registry lines for the four new stage kinds, exactly as the integration adds them to
# autopilot/registry.py. Until they are there, this section installs them for its own run.
KINDS = {
    "secondchance": ("secondchance", "run_secondchance", "free", False, False),
    "callerbook": ("callerbook", "run_callerbook", "free", True, False),
    "dialexport": ("dialexport", "run_dialexport", "free", True, False),
    "auctiontasks": ("auctiontasks", "run_auctiontasks", "free", False, False),
}
CALL_PRESETS = ["FTM - 02 Ready to Call", "FTM - 03 Call Attempt 1", "FTM - 04 Call Attempt 2",
                "FTM - 05 Call Attempt 3", "FTM - 06 Call Attempt 4"]


class Gone(Exception):
    code = 404


class LaneCRM(FakeCRM):
    """The autopilot's fake DataSift plus what the lane stages use: the documented task and
    scheduled-export routes, and the search-row fields the published reference lists."""

    def __init__(self, records, users=(ADRIANA, TINAA)):
        super().__init__(records, users)
        self.tasks: dict = {}
        self.exports: dict = {}
        self.reads: list = []
        self.log_reads: list = []
        self.task_ids = True
        self.refresh_calls: list = []
        self.quote = 0.0

    def record(self, u):
        self.reads.append(u)
        return super().record(u)

    def record_logs(self, u, event_types=None, limit=250):
        self.log_reads.append(u)
        return super().record_logs(u, event_types, limit)

    def _row(self, r):
        row = super()._row(r)
        ow = r["owner"]
        row["owner"] = {"uuid": ow["uuid"], "first_name": ow.get("first_name"), "last_name": ow.get("last_name"),
                        "company": ow.get("company"), "address": ow.get("address")}
        row["last_skip_traced"] = r.get("last_skip_traced") or ""
        row["owner_do_not_mail_ever"] = bool(ow.get("do_not_mail_ever"))
        open_n = sum(1 for t in self.tasks.values() if t.get("assigned_to_property") == r["uuid"] and not t.get("completed"))
        row["tasks"] = {"total": open_n, "complete": 0, "incomplete": open_n}
        return row

    def skiptrace_scoped(self, uuids, dry_run, tags=None, force_refresh=False):
        self.refresh_calls.append((len(uuids), dry_run, force_refresh))
        if dry_run:
            return {"number_of_records": len(uuids) if self.scope_ok else 66242, "cost": self.quote}
        return super().skiptrace_scoped(uuids, dry_run, tags, force_refresh)

    def call(self, path, method="GET", body=None, headers=None):
        # The path is read piece by piece, not matched against a pattern: the API audit reads every
        # route-shaped string in the source, and a pattern here would look like a route nobody reviewed.
        parts = [p for p in path.split("?")[0].split("/") if p]
        kind = parts[2] if parts[:2] == ["api", "internal"] and len(parts) > 2 else ""
        if kind == "task" and len(parts) == 3 and method == "POST":
            uid = "task-%d" % (len(self.tasks) + 1)
            self.tasks[uid] = dict(body, uuid=uid, completed=None)
            self.writes.append(("task", body.get("assigned_to_property") or "", body.get("title")))
            return {"uuid": uid} if self.task_ids else {}
        if kind == "task" and len(parts) == 5 and parts[4] == "complete" and method == "POST":
            self.tasks[parts[3]]["completed"] = "2026-11-11T21:00:00.000000Z"
            self.writes.append(("task_complete", parts[3]))
            return ""
        if kind == "task" and len(parts) == 4 and method == "GET":
            if parts[3] not in self.tasks:
                raise Gone("no such task")
            return dict(self.tasks[parts[3]])
        if kind == "filter-preset" and len(parts) == 5 and parts[4] == "scheduled-export":
            if method == "GET":
                if parts[3] not in self.exports:
                    raise Gone("no scheduled export")
                return dict(self.exports[parts[3]], uuid="export-1")
            self.exports[parts[3]] = dict(body)
            self.writes.append(("export", parts[3], method))
            return dict(body, uuid="export-1")
        return super().call(path, method, body, headers)


def _phone(number, tier="Dial First", kind="MOBILE", status="UNKNOWN"):
    return {"number": number, "type": kind, "tags": [tier] if tier else [], "status": status}


def _r(u, street, tags, **kw):
    """One invented record with exactly these tags (the shared helper's default tags are first to market)."""
    lists = kw.pop("lists", [])
    county = kw.pop("county", "Knox")
    r = _rec(u, street, **kw)
    r["tags"] = list(tags)
    r["lists"] = list(lists)
    r["address"]["county"] = county
    return r


BASE = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana"]},
    "lanes": {"ftm": {"tags": ["FTM"]}},
}


def pb(stages: dict, lanes_block: dict | None = None, **top) -> dict:
    """A playbook: the shared base, these stages, and any top-level block that is overridden."""
    d = copy.deepcopy(BASE)
    d["stages"] = copy.deepcopy(stages)
    if lanes_block is not None:
        d["lanes"] = copy.deepcopy(lanes_block)
    for k, v in top.items():
        d[k] = copy.deepcopy(v)
    return d


def stage(kind, owner, presets=None, mode="live", **settings):
    s = {"kind": kind, "owner": owner, "mode": mode, "schedule": "08:00 Mon-Fri"}
    if presets is not None:
        s["presets"] = list(presets)
    s.update(settings)
    return s


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import activity, auctiontasks, policy, registry
    saved_cfg = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.MODE = "live"
    store.init()
    saved_presets = dict(PRESETS)
    added = {k: v for k, v in KINDS.items() if k not in registry.EXTRA}
    saved_policy = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    registry.EXTRA.update(added)
    policy.KINDS = policy.KINDS + tuple(added)
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])
    for i in range(1, 5):
        PRESETS["FTM - %02d Call Attempt %d" % (i + 2, i)] = (
            lambda r, n=i: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == n)
    PRESETS.update({
        "FTM - Arrivals": lambda r: "FTM" in r["tags"],
        "FTM - Active": lambda r: "FTM" in r["tags"],
        "04 Return Mail -> DP": lambda r: False,
        "Strong - 02 Ready to Call": lambda r: "Priority 2" in r["tags"] and r["predictivecall_attempts"] == 0,
        "P1 - Rung 1": lambda r: "Rung 1" in r["tags"],
        "P1 - Rung 2": lambda r: "Rung 2" in r["tags"],
        "Bulk - Small": lambda r: "Small" in r["tags"],
        "Bulk - Mid": lambda r: "Mid" in r["tags"],
        "Bulk - Big": lambda r: "Big" in r["tags"],
    })
    try:
        _run(check, tmp)
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved_policy[0], saved_policy[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved_policy[1])
        auctiontasks.lookup_owner = None
        activity.reset()
        _wipe(store)          # leave nothing behind: the next section starts from an empty autopilot memory
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = saved_cfg
        config.MODE = "dry"


def _wipe(store) -> None:
    """Start from an empty autopilot memory: this section runs after others on the same file."""
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import (activity, attempts, auctiontasks, dialexport, gate, laneroutes, lanes, ledger, policy, registry,
                            secondchance, sources, stages, sweep, undo)
    _wipe(store)

    def go(stage_id, crm, pol, now=T0, **kw):
        """One stage run with no real source and a fake scorer, so nothing here can reach a vendor."""
        kw.setdefault("sources", {})
        kw.setdefault("scorer", FakeScorer())
        return sweep.run(stage_id, crm=crm, now=now, pol=pol, **kw)

    def parse(stages_block, lanes_block=None, **top):
        return policy.parse(pb(stages_block, lanes_block, **top), EMPLOYEES)

    def ctx_for(pol, stage_id, crm, live=True, now=T0):
        st = pol.stage(stage_id)
        return stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-" + stage_id, now=now, live=live,
                          out=stages.Summary(st.id, st.kind, st.owner, st.mode, live), scorer=FakeScorer())

    print("  the new stage kinds")
    check(registry.problems() == [], "each new stage kind loads from its own module: %s" % (registry.problems() or "ok"))
    pol_kinds = parse({"a": stage("secondchance", "analysis"), "b": stage("callerbook", "analysis", ["P1 - Rung 1"]),
                       "c": stage("dialexport", "ops", ["Bulk - Big"]), "d": stage("auctiontasks", "analysis")})
    check(all(pol_kinds.stage(s).spend == "free" for s in "abcd"), "none of the four new kinds is a paid or an outreach stage")
    try:
        parse({"b": stage("callerbook", "analysis")})
        check(False, "a caller book or a dialer file with no preset is refused")
    except policy.PolicyError:
        check(True, "a caller book or a dialer file with no preset is refused")

    # ------------------------------------------------------------------ the channel map (plan 4.8)
    print("  channel map")
    plain = lanes.Lanes()
    check(lanes.channel_open(plain, "priority1", "mail", 0)[0] and lanes.channel_open(plain, "bulk", "call", 0)[0],
          "a lane the playbook has not switched on is not restricted, whatever the channel map writes for it")
    pol_map = parse({"a": stage("secondchance", "analysis")}, {
        "ftm": {"tags": ["FTM"]}, "hot_auction": {"tags": ["Auction Soon"], "timeline": True},
        "priority1": {"tags": ["Priority 1"], "timeline": True},
        "strong": {"tags": ["Priority 2"], "timeline": {"mail_pieces": 4}}, "bulk": {"timeline": True}})
    M = pol_map.lanes
    co = lambda lane, ch, day, counter=0, facts=None, wd=2: lanes.channel_open(M, lane, ch, day, counter, facts, wd)[0]  # noqa: E731
    check(co("priority1", "text", 0) and co("priority1", "call", 1) and not co("priority1", "mail", 3, 2) and co("priority1", "mail", 3, 5),
          "Priority 1, switched on: text first, calls beside the texts, mail when the round ends")
    check(M.timeline("priority1")["mail_pieces"] == 6 and M.timeline("strong")["mail_pieces"] == 4
          and M.timeline("strong")["round_ends_at"] == 5,
          "six mail pieces below first to market, and an override changes one value of a row, not the row")
    check(not co("priority1", "research", 3, 2) and co("priority1", "research", 3, 5)
          and co("priority1", "research", 0, 0, {"deceased": True}) and co("ftm", "research", 0, 0),
          "research waits for exhaustion on Priority 1, a possible death goes at once, and first to market is unchanged")
    check(co("priority1", "research", 0, 0, {"possible_death": True}) and co("strong", "research", 0, 0, {"returned_mail": True})
          and co("bulk", "research", 0, 0, {"research_class": "exhausted"}) and not co("bulk", "research", 0, 0, {"research_class": "vacant_mailing"}),
          "the research queue's own words for a possible death, returned mail and an exhausted record are read as well")
    check(not co("bulk", "call", 1) and co("bulk", "call", 3) and not co("bulk", "call", 40, 6),
          "bulk: three texts, then the power dialer from day 3, to attempt 6")
    check(not co("bulk", "mail", 30, 5) and co("bulk", "mail", 30, 6) and not co("bulk", "followup_text", 30, 6),
          "bulk mail opens only when the round is over, and bulk has no follow-up texts")
    check(co("hot_auction", "call", 20, 5) and not co("ftm", "call", 20, 5),
          "the hot auction lane keeps calling after the round; first to market does not")
    check(co("ftm", "door_knock", 0, 0, {"days_to_sale": 6}) and not co("ftm", "door_knock", 0, 0, {"days_to_sale": 9})
          and not co("priority1", "door_knock", 0) and not lanes.channel_open(plain, "bulk", "door_knock", 0, 0, {})[0],
          "a door knock is only for a sale inside a week, in every lane, written or not")
    check(any("power dialer" in ln for ln in lanes.describe(M, "bulk")) and any("door knock" in ln for ln in lanes.describe(M, "hot_auction"))
          and len(lanes.describe(M, "ftm")) == 5, "each written lane prints its own timeline, and first to market still prints five lines")
    try:
        parse({"a": stage("secondchance", "analysis")}, {
            "obituary": {"timeline": True}, "strong": {"tranche": {"size": 0}}, "bulk": {"caps": {"weekly": 5}},
            "priority1": {"tranche": {"size": 10, "speed": 3}}})
        check(False, "a lane block with a missing timeline, a bad tranche and an unknown cap is refused")
    except policy.PolicyError as exc:
        check(all(k in str(exc) for k in ("obituary", "tranche.size", "weekly", "speed")),
              "a lane block with a missing timeline, a bad tranche and an unknown cap is refused, all at once")

    # ------------------------------------------------------------------ the five-attempt round (plan 1.2)
    print("  five-attempt round")
    activity.reset()
    pol5 = parse({"k_attempts": stage("attempts", "ops", CALL_PRESETS, lane="ftm"),
                  "k_assign": stage("assign", "analysis", CALL_PRESETS, lane="ftm", backlog=True)})
    check(attempts.counters_of(pol5.stage("k_attempts"))["FTM - 06 Call Attempt 4"] == 4,
          "the fifth call preset, Call Attempt 4, holds counter 4")
    late = _r("r5", "5 Round St", ["FTM"], phones=[dict(MOBILE1)], predictivecall_attempts=4)
    crm5 = LaneCRM([late])
    crm5.rec_logs = {"r5": [{"event_type": "property.created", "timestamp": _ts(_et(10, 20)), "payload": {}}]}
    crm5.log_rows = [dial("r5-%d" % i, "o-r5", _et(11, 3 + i, 10)) for i in range(5)]
    s5 = go("k_attempts", crm5, pol5, plan=True)
    check(s5.acted == 1 and "4 to 5" in s5.head, "a record in Call Attempt 4 with a fifth call day is moved to 5 by the attempt keeper")
    check(not lanes.channel_open(pol5.lanes, "ftm", "call", 6, 5)[0] and lanes.channel_open(pol5.lanes, "ftm", "mail", 6, 5)[0]
          and lanes.channel_open(pol5.lanes, "ftm", "call", 5, 4)[0],
          "the round ends when the counter reads 5: a fifth attempt is allowed, a sixth is not, and mail opens")
    check(stages.round_ends_at(ctx_for(pol5, "k_assign", crm5)) == 5, "a stage reads the round's end from its lane's timeline")
    crm5b = LaneCRM([_r("r6", "6 Round St", ["FTM"], phones=[dict(MOBILE1)], predictivecall_attempts=4)])
    go("k_assign", crm5b, pol5)
    check(crm5b.recs["r6"]["assigned_to"] == "u-adriana", "a record on its fifth attempt is still in the lane the assign stage reads")
    raw = policy.raw_stages()
    gaps = [sid for sid, s in raw.items() if isinstance(s, dict) and s.get("kind") in ("attempts", "assign", "score")
            and any("Call Attempt 3" in str(p) for p in (s.get("presets") or []))
            and not any("Call Attempt 4" in str(p) for p in (s.get("presets") or []))]
    check(not gaps, "every call stage in the playbook that reads Call Attempt 3 also reads Call Attempt 4: %s" % (gaps or "ok"))

    # ------------------------------------------------------------------ helpers the lanes share
    print("  shared helpers")
    order = stages.dial_order([_d("1", "Dial First", "LANDLINE"), _d("2", "Dial Second", "MOBILE"), _d("3", "Dial First", "MOBILE"),
                               _d("4", "Dial Second", "LANDLINE", "CORRECT")], agreed={"2"})
    check([n["number"] for n in order] == ["4", "2", "3", "1"],
          "dial order: the number a person confirmed, then mobiles (two sources before one, then by tier), and landlines last")
    check(stages.mask("(865) 555-0142") == "ending 0142" and stages.mask("") == "no number", "a printed number shows its last four digits only")
    check(sources.line_kind("Mobile") == "MOBILE" and sources.line_kind("Landline") == "LANDLINE"
          and sources.line_kind("NonFixedVOIP") == "VOIP" and sources.line_kind("") == "", "the scorer's line type is read into DataSift's words")
    whole = sources.subject_of(_r("h1", "1 Whole St", ["FTM"]))
    no_mail = _r("h2", "2 Half St", ["FTM"])
    no_mail["owner"]["address"] = {"street": "", "city": "", "state": "", "postal_code": ""}
    check(sources.missing_for_trace(whole) == [] and sources.missing_for_trace(sources.subject_of(no_mail)) == ["no mailing address"]
          and sources.missing_for_trace(sources.subject_of(_r("h3", "3 Half St", ["FTM"], first=""))) == ["no full name"],
          "a record is complete for a trace only with a first name, a last name and a mailing address")
    check(sources.subject_of(_r("h4", "4 Co St", ["FTM"], first="", last="", company="Invented Holdings LLC")).org
          and not sources.subject_of(_r("h5", "5 Blank St", ["FTM"], first="", last="")).org,
          "a company is told apart from a record whose name is simply blank")
    check(laneroutes.task_body("Call  back") == {"title": "Call back", "all_day": True, "event_type": "task", "source": "autopilot"}
          and laneroutes.task_body("T", user_uuid="u-1", property_uuid="r-1")["assigned_to_users"] == ["u-1"],
          "a task body leaves out every key that has no value, and names one user as assignee")
    check(laneroutes.end_of_day(date(2026, 11, 11), "America/New_York") == "2026-11-12T04:59:59.999Z",
          "an all-day due date is the last moment of the business-local day, in UTC")
    check(laneroutes.business_day(date(2026, 11, 14)) == date(2026, 11, 16) and laneroutes.business_day(date(2026, 11, 13), 1) == date(2026, 11, 16),
          "a task is never due on a weekend")
    try:
        laneroutes.export_body(["t"], ["RINGING"], "f", date(2026, 11, 11))
        check(False, "an export body with a phone status DataSift does not have is refused")
    except ValueError:
        check(True, "an export body with a phone status DataSift does not have is refused")
    check(laneroutes.get_task(LaneCRM([]), "task-404") is None, "a task that no longer exists reads as gone, not as an error")
    check(laneroutes.task_person("u-marcus", [ADRIANA_P, MARCUS_P]) == MARCUS_P
          and laneroutes.task_person("u-else", [ADRIANA_P, MARCUS_P], {"u-adriana": 2}) == MARCUS_P and laneroutes.task_person("", []) is None,
          "a task goes to the caller who holds the record, else to the caller with the fewest tasks this run")

    _tiers(check, go, parse, ledger, undo)
    _waterfall(check, go, parse, ctx_for, ledger, stages, store)
    _research_by_list(check, go, parse)
    _returned_mail(check, go, parse, ledger)
    _tranches(check, go, parse, ctx_for, lanes, ledger, stages, store)
    _auction(check, go, parse, auctiontasks, gate, ledger)
    _second_chances(check, go, parse, activity, ledger, secondchance)
    _caller_book(check, go, parse, ledger)
    _dial_export(check, go, parse, activity, dialexport, laneroutes, tmp)


ADRIANA_P = {"name": "Adriana", "uuid": "u-adriana"}
MARCUS_P = {"name": "Marcus", "uuid": "u-marcus"}


def _d(number, tier, kind, status="UNKNOWN"):
    return {"number": number, "tier": tier, "type": kind, "status": status}


def _fresh() -> None:
    """Each group of checks starts from an empty autopilot memory, so one group's spend or facts
    cannot decide another's."""
    from . import store
    _wipe(store)


def _by_number(crm, uuid) -> dict:
    return {p["number"]: p for p in crm.recs[uuid]["owner"]["phones"]}


# ---------------------------------------------------------------------- tier discipline (plan 1.14)
def _tiers(check, go, parse, ledger, undo) -> None:
    print("  tier discipline")
    _fresh()

    def numbers():
        return [{"number": "8655550301", "status": "UNKNOWN", "tags": [], "type": "UNKNOWN"},          # scores Dial First, a mobile
                {"number": "8655550309", "status": "UNKNOWN", "tags": [], "type": "UNKNOWN"},          # scores Drop, a landline
                {"number": "8655550319", "status": "CORRECT", "tags": [], "type": "MOBILE"},           # scores Drop, but a person confirmed it
                {"number": "8655550329", "status": "UNKNOWN", "tags": ["Drop"], "type": "LANDLINE"}]   # tiered Drop before the rule

    pol = parse({"k_score": stage("score", "research", ["FTM - 02 Ready to Call"], lane="ftm", drop_status="DEAD", write_line_type=True),
                 "k_plain": stage("score", "research", ["FTM - 02 Ready to Call"], lane="ftm")})
    crm_p = LaneCRM([_r("td0", "10 Tier St", ["FTM"], phones=numbers())])
    plan = go("k_score", crm_p, pol, plan=True)
    check(not crm_p.writes and plan.tally.get("Drop numbers marked dead") == 1 and _by_number(crm_p, "td0")["8655550329"]["status"] == "UNKNOWN",
          "a plan run lists the Drop number it would mark dead and writes nothing")
    crm = LaneCRM([_r("td1", "11 Tier St", ["FTM"], phones=numbers())])
    go("k_score", crm, pol)
    ph = _by_number(crm, "td1")
    check(ph["8655550309"]["status"] == "DEAD" and "Drop" in ph["8655550309"]["tags"], "a number that scores Drop is marked dead")
    check(ph["8655550329"]["status"] == "DEAD", "a Drop number tiered before the rule existed is marked dead too")
    check(ph["8655550319"]["status"] == "CORRECT" and ph["8655550301"]["status"] == "UNKNOWN",
          "a number a person confirmed keeps its mark whatever it scores, and a good number is left as it was")
    check(ph["8655550301"]["type"] == "MOBILE" and ph["8655550309"]["type"] == "LANDLINE" and ph["8655550319"]["type"] == "MOBILE",
          "the scorer's line type is written where DataSift had none, and never over one it had")
    run_id = [c for c in ledger.changes(record_uuid="td1") if c["what"] == "phones"][0]["run_id"]
    undo.run(run_id, crm=crm, commit=True)
    back = _by_number(crm, "td1")
    check(back["8655550309"]["status"] == "UNKNOWN" and back["8655550329"]["status"] == "UNKNOWN" and back["8655550309"]["tags"] == [],
          "marking a number dead records what it replaced, and undo puts every number back as it was")
    crm_o = LaneCRM([_r("td2", "12 Tier St", ["FTM"], phones=numbers())])
    go("k_plain", crm_o, pol)
    old = _by_number(crm_o, "td2")
    check(old["8655550309"]["status"] == "UNKNOWN" and old["8655550329"]["status"] == "UNKNOWN" and old["8655550309"]["type"] == "UNKNOWN"
          and "Drop" in old["8655550309"]["tags"], "a scoring stage without the settings tiers a Drop number and leaves its status and type alone, as before")


# ---------------------------------------------------------------------- the waterfall, second version (plan 1.13, 1.15)
def _waterfall(check, go, parse, ctx_for, ledger, stages, store) -> None:
    print("  trace waterfall, second version")
    _fresh()
    water = stage("retrace", "research", ["FTM - 01 Skipped No Numbers", "FTM - 02 Ready to Call"], lane="ftm",
                  sources=["smartskip", "tracerfy"], every_record_sources=1, agreement_tag="Two Sources", keep_numbers=2,
                  write_tiers=["Dial First", "Dial Second"], reserve_tiers=["Dial Third"], fixup_queue=2,
                  fixup_tag="Needs Fix-Up", entity="principals", after_sources="none")
    pol = parse({"k_water": water,
                 "k_old": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["tracerfy"], after_sources="none")})

    def recs():
        w3 = _r("w3", "3 Water St", ["FTM"])
        w3["owner"]["address"] = {"street": "", "city": "", "state": "", "postal_code": ""}
        return [_r("w1", "1 Water St", ["FTM"], phones=[_phone("8655550401")]),
                _r("w2", "2 Water St", ["FTM"]), w3,
                _r("w4", "4 Water St", ["FTM"], first=""),
                _r("w5", "5 Water St", ["FTM"], last=""),
                _r("w6", "6 Water St", ["FTM"], first="", last="", company="Invented Holdings LLC"),
                _r("w7", "7 Water St", ["FTM", "Do Not Market"], phones=[_phone("8655550471")])]

    def srcs():
        return {"smartskip": FakeSource("smartskip", 0.15, "match", {"w1": ["8655550401", "8655550411", "8655550421", "8655550431"]}),
                "tracerfy": FakeSource("tracerfy", 0.02, "row", {"w2": ["8655550442", "8655550449"]})}

    queued = []

    def enq(agent, wf, record_uuid="", subject="", **kw):
        queued.append((agent, wf, record_uuid))
        return len(queued), True

    crm_p, s_p = LaneCRM(recs()), srcs()
    plan = go("k_water", crm_p, pol, plan=True, sources=s_p, enqueue=enq)
    check(not s_p["smartskip"].calls and not s_p["tracerfy"].calls and not crm_p.writes and not queued,
          "a planned run of the new waterfall calls no source, writes nothing and hands off nothing")
    check(plan.tally.get("traces planned via smartskip") == 2 and plan.tally.get("traces planned via tracerfy") == 1,
          "the plan puts the second source on every record and the third only on the record with no number")
    crm, s = LaneCRM(recs()), srcs()
    live = go("k_water", crm, pol, sources=s, enqueue=enq)
    traced = {u for call in s["smartskip"].calls + s["tracerfy"].calls for u in call}
    check(set(s["smartskip"].calls[0]) == {"w1", "w2"} and s["tracerfy"].calls == [["w2"]],
          "the second source runs on every record, the one that already has a number included, and the third only on the miss")
    p1 = _by_number(crm, "w1")
    check("Two Sources" in p1["8655550401"]["tags"] and stages.agreed_numbers("w1") == {"8655550401"},
          "a number two sources agree on is marked, on the phone and in the autopilot's own memory")
    check(set(p1) == {"8655550401", "8655550411"} and live.tally.get("numbers past the 2 kept per person") == 2,
          "a person keeps at most two usable numbers: the one already on the record counts, and the best new one fills the room")
    p2 = _by_number(crm, "w2")
    check(set(p2) == {"8655550442"} and live.tally.get("low-tier numbers kept off the record") == 1,
          "a Drop number stays off the record, and a Dial Third lands only because the record had nothing better")
    check(not traced & {"w3", "w4", "w5"}, "a record with no full name or no mailing address never reaches a paid source")
    check("Needs Fix-Up" in crm.recs["w3"]["tags"] and "Needs Fix-Up" in crm.recs["w4"]["tags"] and "Needs Fix-Up" not in crm.recs["w5"]["tags"]
          and live.tally.get("incomplete, waiting for room in the fix-up queue (2 at a time)") == 1,
          "the fix-up queue holds its size at a time: two are tagged for a person, the third waits for room")
    check((ledger.fact("w3", "fixup") or {}).get("why") == ["no mailing address"] and (ledger.fact("w4", "fixup") or {}).get("why") == ["no full name"],
          "the queue is the autopilot's own memory first, with what each record lacks")
    check(queued == [("research", "entity_principals", "w6")] and "Mail Only" not in crm.recs["w6"]["tags"] and "w6" not in traced,
          "a company goes to the principal finder, is not parked as Mail Only and is not traced as if it were a person")
    check("w7" not in traced and any("Do Not Market" in k for k in live.tally), "a record the gate holds is not traced, number or no number")
    crm.recs["w3"]["owner"]["address"] = {"street": "33 Mail Rd", "city": "Knoxville", "state": "TN", "postal_code": "37917"}
    s2 = srcs()
    live2 = go("k_water", crm, pol, now=T0 + timedelta(days=1), sources=s2, enqueue=enq)
    check("Needs Fix-Up" not in crm.recs["w3"]["tags"] and s2["smartskip"].calls == [["w3"]] and "Needs Fix-Up" in crm.recs["w5"]["tags"],
          "a record that is fixed leaves the queue and is traced, and the next one takes its place")
    check(len(queued) == 1 and live2.tally.get("company owners waiting on a named person") == 1,
          "a company is handed to the principal finder once, then waits until a person is named")
    check(set(stages._fixup_open(T0 + timedelta(days=1))) == {"w4", "w5"} and stages._fixup_open(T0 + timedelta(days=20)) == {},
          "a record no run has seen for two weeks gives up its place in the fix-up queue")
    check("w1" not in {u for call in s2["smartskip"].calls + s2["tracerfy"].calls for u in call},
          "a record that has had every stacked source and has a number is not paid for again")
    crm.reads.clear()
    third = go("k_water", crm, pol, now=T0 + timedelta(days=2), sources=srcs(), enqueue=enq)
    check("w1" not in crm.reads and third.tally.get("already has a callable number and every stacked source") == 1,
          "and once that is known the record is not opened again for a week")
    crm.reads.clear()
    go("k_water", crm, pol, now=T0 + timedelta(days=10), sources=srcs(), enqueue=enq)
    check("w1" in crm.reads, "after a week it is read once more, in case its numbers have gone bad")
    crm_c, s_c = LaneCRM([_r("wc1", "8 Water St", ["FTM"], phones=[_phone("8655550481")])]), srcs()
    capped = go("k_water", crm_c, parse({"k_water": dict(water, daily_usd=0)}), sources=s_c, enqueue=enq)
    check(not s_c["smartskip"].calls and any("not traced today" in k for k in capped.tally),
          "past the day's cap no source is called, and the record waits for tomorrow")
    pol_lane = parse({"k_water": dict(water, lane="strong", presets=["Strong - 02 Ready to Call"])},
                     {"ftm": {"tags": ["FTM"]}, "strong": {"tags": ["Priority 2"], "caps": {"daily_usd": 0.10}}})
    crm_l, s_l = LaneCRM([_r("wl1", "9 Water St", ["Priority 2"], phones=[_phone("8655550491")])]), srcs()
    lane_run = go("k_water", crm_l, pol_lane, sources=s_l, enqueue=enq)
    check(not s_l["smartskip"].calls and lane_run.tally.get("not traced today (strong lane cap $0.10/day)") == 1,
          "a lane's own daily cap stops that lane's paid traces")
    check(ctx_for(pol, "k_water", crm).budget(0.15)[0], "a lane with no cap of its own is held only by the caps it always had")
    half = _r("wo1", "10 Water St", ["FTM"])
    half["owner"]["address"] = {"street": "", "city": "", "state": "", "postal_code": ""}
    s_o = {"tracerfy": FakeSource("tracerfy", 0.02, "row", {})}
    go("k_old", LaneCRM([half]), pol, sources=s_o)
    check(s_o["tracerfy"].calls == [["wo1"]], "a retrace stage with none of the new settings behaves as it did: nothing is held for fix-up")
    pol_d = parse({"k_drop": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["tracerfy"],
                                   after_sources="none", drop_status="DEAD")})
    crm_d = LaneCRM([_r("wd1", "11 Water St", ["FTM"])])
    go("k_drop", crm_d, pol_d, sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {"wd1": ["8655550459", "8655550451"]})})
    landed = _by_number(crm_d, "wd1")
    check(landed["8655550459"]["status"] == "DEAD" and landed["8655550451"]["status"] == "UNKNOWN",
          "a retrace stage that still writes every number writes a Drop number as dead on arrival")
    pol_two = parse({"k_two": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["smartskip", "tracerfy"],
                                    every_record_sources=2, keep_numbers=2, write_tiers=["Dial First", "Dial Second"],
                                    after_sources="none")})
    crm_t = LaneCRM([_r("wk1", "12 Water St", ["FTM"])])
    s_t = {"smartskip": FakeSource("smartskip", 0.15, "match", {"wk1": ["8655550511", "8655550521", "8655550531"]}),
           "tracerfy": FakeSource("tracerfy", 0.02, "row", {"wk1": ["8655550541", "8655550531", "8655550511"]})}
    two = go("k_two", crm_t, pol_two, sources=s_t)
    check(set(_by_number(crm_t, "wk1")) == {"8655550511", "8655550521"} and s_t["tracerfy"].calls == [["wk1"]],
          "with two sources on every record the limit holds across both: the second source adds nothing past it")
    check(stages.agreed_numbers("wk1") == {"8655550511"} and two.tally.get("numbers past the 2 kept per person") == 2,
          "a number the rules kept off the record is never called agreed, and it is counted once however many sources return it")

    # the 14-day look-back
    _fresh()
    pol_lb = parse({"k_look": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["tracerfy"],
                                    after_sources="none", lookback_days=14, lookback_min_records=2)})
    old = T0 - timedelta(days=16)
    for u in ("lb1", "lb2"):
        ledger.event("old-run", "k_look", u, "trace", {"source": "smartskip"}, now=old)
    for u in ("lb3", "lb4"):
        ledger.event("old-run", "k_native", u, "native_trace", {"scope": "records"}, now=old)
    ledger.event("new-run", "k_look", "lb5", "trace", {"source": "smartskip"}, now=T0 - timedelta(days=2))
    crm_lb = LaneCRM([_r("lb1", "1 Look St", ["Old"], phones=[_phone("8655550601", status="CORRECT")]),
                      _r("lb2", "2 Look St", ["Old"], phones=[_phone("8655550611")]), _r("lb3", "3 Look St", ["Old"]),
                      _r("lb4", "4 Look St", ["Old"], phones=[_phone("8655550621")]), _r("lb5", "5 Look St", ["Old"])])
    lb = go("k_look", crm_lb, pol_lb, sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {})})
    check(any("smartskip, 14 days on: 2 of 2 record(s) have a number, 1 a confirmed correct one (50%)" in ln for ln in lb.lines),
          "two weeks after a pass it is judged by what the callers confirmed, not by what the source returned")
    check(any("DataSift's own trace found a number on 50% of 2 record(s)" in p for p in lb.problems),
          "when the first trace finds numbers on under nine records in ten, that is said as a problem")
    n_reads = len(crm_lb.reads)
    go("k_look", crm_lb, pol_lb, now=T0 + timedelta(hours=1), sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {})})
    check(len(crm_lb.reads) == n_reads and "lb5" not in crm_lb.reads, "each pass is judged once, and a pass younger than 14 days is not judged yet")

    # the 180-day re-trace on DataSift's own trace
    _fresh()
    pol_rf = parse({"k_refresh": stage("native_trace", "research", ["FTM - Active"], lane="ftm", retrace_after_days=180),
                    "k_paid": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["tracerfy"])},
                   caps={"daily_usd": 100, "record_usd": 1.0})
    stale = (T0 - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    recent = (T0 - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [_r("rf%02d" % i, "%d Refresh St" % (i + 1), ["FTM"], phones=[_phone("86555507%02d" % i)], last_skip_traced=stale) for i in range(22)]
    rows += [_r("rn%d" % i, "%d Recent St" % (i + 1), ["FTM"], phones=[_phone("865555080%d" % i)], last_skip_traced=recent) for i in range(3)]
    rows.append(_r("rs1", "1 Gone St", ["FTM"], status="sold", last_skip_traced=stale))
    crm_rf = LaneCRM(rows)
    rp = go("k_refresh", crm_rf, pol_rf, plan=True)
    check(rp.acted == 22 and not crm_rf.writes and "last traced 180 or more days ago" in rp.head,
          "the plan names the 22 active records last traced over 180 days ago, and leaves out the recent and the sold")
    crm_rf.quote = 44.0
    go("k_refresh", crm_rf, pol_rf)
    check(("skiptrace", tuple("rf%02d" % i for i in range(22))) in crm_rf.writes and (22, False, True) in crm_rf.refresh_calls,
          "the live run asks DataSift to trace exactly those records again, dry run first")
    check(len(ledger.events(action="native_refresh")) == 22 and not ledger.events("rf00", "native_trace"),
          "the 180-day trace is logged under its own action")
    check(ledger.spent(record_uuid="rf00") == 2.0 and ctx_for(pol_rf, "k_paid", crm_rf).budget(0.15, "rf00")[0],
          "it never counts toward a record's lifetime cap, even on an account that bills for it")
    again = go("k_refresh", crm_rf, pol_rf, now=T0 + timedelta(days=1))
    check(again.acted == 0 and "No active record" in again.head, "a record traced again is not due for another 180 days")
    few = LaneCRM([_r("rq%d" % i, "%d Few St" % (i + 1), ["FTM"], phones=[_phone("865555090%d" % i)], last_skip_traced=stale) for i in range(5)])
    fw = go("k_refresh", few, pol_rf, now=T0 + timedelta(days=2))
    check(not few.writes and "20 or more per job" in fw.head, "under 20 due records no trace job is created: they wait for more to come due")


# ---------------------------------------------------------------------- liens and condemned records (plan 1.17)
def _research_by_list(check, go, parse) -> None:
    print("  research by list, routes by list")
    _fresh()
    rules = {"Condemned": "arrival", "Foreclosure": "arrival", "Probate": "arrival", "Liens": "exhausted"}
    pol = parse({"k_arrive": stage("handoff", "research", ["FTM - Arrivals"], lane="ftm", workflow="deep_prospect", agent="research",
                                   per_day=10, research_by_list=rules, research_default="never"),
                 "k_lien": stage("retrace", "research", ["FTM - 01 Skipped No Numbers"], lane="ftm", sources=["tracerfy"],
                                 entity="deep_prospect", after_sources="deep_prospect", research_by_list=rules),
                 "k_route": stage("assign", "analysis", ["FTM - 02 Ready to Call"], lane="ftm", backlog=True,
                                  routes={"Liens": ["Marcus"], "Probate": ["Adriana"]})},
                people={"callers": ["Adriana", "Marcus"]})
    recs = [_r("e1", "1 List St", ["FTM"], lists=["Condemned"], phones=[_phone("8655551001")]),
            _r("e2", "2 List St", ["FTM"], lists=["Liens"], phones=[_phone("8655551011")]),
            _r("e3", "3 List St", ["FTM"], lists=["Liens"], phones=[_phone("8655551021")], predictivecall_attempts=5),
            _r("e4", "4 List St", ["FTM"], lists=["Liens", "Foreclosure"], phones=[_phone("8655551031")]),
            _r("e5", "5 List St", ["FTM"], phones=[_phone("8655551041")]),
            _r("e6", "6 List St", ["FTM"], lists=["Liens"], phones=[_phone("8655551051", status="WRONG")]),
            _r("e12", "12 List St", ["FTM"], lists=["Liens"], directmail_attempts=12),
            _r("e13", "13 List St", ["FTM"], lists=["Liens"], directmail_attempts=3)]
    sent = []

    def enq(agent, wf, record_uuid="", subject="", **kw):
        sent.append((wf, record_uuid))
        return len(sent), True

    crm = LaneCRM(recs, users=(ADRIANA, MARCUS))
    s = go("k_arrive", crm, pol, enqueue=enq)
    check({u for _, u in sent} == {"e1", "e3", "e4", "e6", "e12"},
          "a condemned record is researched on arrival, a lien only once it is exhausted, and a record on both lists follows the more eager rule")
    check("e6" in {u for _, u in sent} and "e12" in {u for _, u in sent} and "e13" not in {u for _, u in sent},
          "exhausted means nothing is left to try: five attempts, every number wrong or dead, or no number at all and the mail lane run out")
    check(s.tally.get("to research, by list: Condemned") == 1 and s.tally.get("to research, by list: Liens") == 3
          and s.tally.get("to research, by list: Foreclosure") == 1, "the stage counts what it sent to research by list")
    check(s.tally.get("research waits: Liens records are researched once they are exhausted") == 2
          and s.tally.get("research waits: other records are not researched") == 1, "what waits is counted under its reason")
    reads = len(crm.reads)
    s2 = go("k_arrive", crm, pol, now=T0 + timedelta(hours=1), enqueue=enq)
    check(len(crm.reads) == reads and s2.tally.get("research waits (checked earlier today)") == 3,
          "a record that is waiting on its list rule is not read again the same day")
    sent.clear()
    crm2 = LaneCRM([_r("e7", "7 List St", ["FTM"], lists=["Liens"], deceased=True),
                    _r("e8", "8 List St", ["FTM"], lists=["Liens"], first="", last="", company="Invented Capital LLC")])
    r = go("k_lien", crm2, pol, sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {})}, enqueue=enq)
    check(sent == [("deep_prospect", "e7")] and r.tally.get("research waits: Liens records are researched once they are exhausted") == 1,
          "a possible death is never held back by a list rule, while a lien held by a company waits like any other lien")
    crm3 = LaneCRM([_r("e9", "9 List St", ["FTM"], lists=["Liens"], phones=[_phone("8655551091")]),
                    _r("e10", "10 List St", ["FTM", "Probate"], phones=[_phone("8655551101")])], users=(ADRIANA, MARCUS))
    go("k_route", crm3, pol)
    check(crm3.recs["e9"]["assigned_to"] == "u-marcus" and crm3.recs["e10"]["assigned_to"] == "u-adriana",
          "an assign route keys on a list as well as a tag: the lien record goes to the lien caller")
    bad = parse({"k_typo": stage("handoff", "research", ["FTM - Arrivals"], lane="ftm", workflow="deep_prospect", agent="research",
                                 research_by_list={"Liens": "whenever"})})
    sb = go("k_typo", LaneCRM([_r("e11", "11 List St", ["FTM"], lists=["Liens"])]), bad, enqueue=enq)
    check(any("whenever" in p for p in sb.problems) and ("deep_prospect", "e11") not in sent,
          "a list rule the stage cannot read is said as a problem, and those records are not researched on a guess")
    sent.clear()
    dead = go("k_arrive", LaneCRM([_r("e14", "14 List St", ["FTM"], lists=["Liens"], deceased=True, phones=[_phone("8655551141")])],
                                  users=(ADRIANA, MARCUS)), pol, now=T0 + timedelta(days=2), enqueue=enq)
    check(sent == [("deep_prospect", "e14")] and dead.tally.get("to research, by list: a possible death") == 1,
          "a handoff stage does not hold a possible death on its list rule either: a dead owner on the Liens list goes at once")


# ---------------------------------------------------------------------- returned mail (plan 1.7)
def _returned_mail(check, go, parse, ledger) -> None:
    print("  returned mail")
    _fresh()
    block = stage("handoff", "research", ["04 Return Mail -> DP"], lane="ftm", require_tags=["FTM"], workflow="deep_prospect",
                  agent="research", per_day=10,
                  return_signals={"statuses": ["Return Mail"], "tags": ["Returned Mail"], "flag": True, "flag_scope_tags": ["FTM"]},
                  opt_out_tags=["Mail Opt Out"], return_tag="Return Mail", returns_scan={"assignee": "Adriana", "weekday": "mon"})
    pol = parse({"k_return": block, "k_nobody": dict(block, returns_scan={"weekday": "mon"}),
                 "k_plain": stage("handoff", "research", ["04 Return Mail -> DP"], lane="ftm", require_tags=["FTM"],
                                  workflow="deep_prospect", agent="research")})

    def recs():
        return [_r("m1", "1 Return St", ["FTM"], status="Return Mail"),
                _r("m2", "2 Return St", ["FTM", "Returned Mail"]),
                _r("m3", "3 Return St", ["FTM"], do_not_mail_ever=True),
                _r("m4", "4 Return St", ["FTM", "Mail Opt Out"], do_not_mail_ever=True),
                _r("m5", "5 Return St", ["FTM", "Do Not Market"], owner_dnm=True),
                _r("m6", "6 Return St", ["FTM"]),
                _r("m7", "7 Return St", ["Priority 1"], do_not_mail_ever=True)]

    sent = []

    def enq(agent, wf, record_uuid="", subject="", **kw):
        sent.append((wf, record_uuid))
        return len(sent), True

    crm_p = LaneCRM(recs())
    p = go("k_return", crm_p, pol, plan=True, enqueue=enq)
    check(p.acted == 3 and not crm_p.writes and not sent and not crm_p.tasks and ledger.fact("m3", "mail_flag") is None,
          "a plan run names the three returns, writes no tag, creates no task and does not remember a return it did not mark")
    crm = LaneCRM(recs())
    s = go("k_return", crm, pol, enqueue=enq)
    check({u for _, u in sent} == {"m1", "m2", "m3"},
          "returned mail reaches research whichever way it was recorded: the status set by hand, a mail house's tag, or the do-not-mail flag")
    check("Return Mail" in crm.recs["m3"]["tags"] and "Return Mail" not in crm.recs["m4"]["tags"] and "Return Mail" not in crm.recs["m5"]["tags"]
          and ledger.fact("m3", "mail_flag") == "return" and ledger.fact("m4", "mail_flag") == "opt_out",
          "a do-not-mail flag with an opt-out tag is an opt-out; one without is a return, tagged as one the day it is first seen")
    check(s.tally.get("do-not-mail set by an opt-out, not a return") == 2 and "m7" not in crm.reads,
          "an opt-out is counted and never researched as a return, and the flag is only looked for on the stage's own lane")
    check(any("mail came back: the do-not-mail flag" in ln for ln in s.lines), "each handoff says how the return was recorded")
    tasks = list(crm.tasks.values())
    check(len(tasks) == 1 and tasks[0]["assigned_to_users"] == ["u-adriana"] and "assigned_to_property" not in tasks[0]
          and "returned letters" in tasks[0]["title"], "the weekly returned-letters task is created for the person the playbook names")
    go("k_return", crm, pol, now=T0 + timedelta(days=1), enqueue=enq)
    check(len(crm.tasks) == 1 and len(sent) == 3, "that task is created once a week, and a returned record is handed to research once")
    crm_n = LaneCRM(recs())
    nb = go("k_nobody", crm_n, pol, enqueue=enq)
    check(any("nobody named yet" in ln for ln in nb.lines) and not crm_n.tasks, "with nobody named, the weekly scan is said and no task is created")
    pl = go("k_plain", LaneCRM(recs()), pol, enqueue=enq)
    check(pl.found == 0 and pl.acted == 0, "a handoff stage that names no return signals reads only its presets, as before")


# ---------------------------------------------------------------------- tranches and lane caps (plan 4.1)
def _tranches(check, go, parse, ctx_for, lanes, ledger, stages, store) -> None:
    print("  tranches and lane caps")
    _fresh()
    strong = ["Strong - 02 Ready to Call"]
    pol = parse({"s_assign": stage("assign", "analysis", strong, lane="strong", tranche=True, backlog=True),
                 "s_score": stage("score", "research", strong, lane="strong", tranche=True),
                 "s_keeper": stage("attempts", "ops", strong, lane="strong"),
                 "s_digest": stage("digest", "ops", strong),
                 "f_score": stage("score", "research", ["FTM - 02 Ready to Call"], lane="ftm"),
                 "x_none": stage("assign", "analysis", ["FTM - 02 Ready to Call"], lane="ftm", tranche=True, backlog=True),
                 "b_size": stage("assign", "analysis", ["Bulk - Big"], lane="bulk", tranche=2, backlog=True)},
                {"ftm": {"tags": ["FTM"]},
                 "strong": {"tags": ["Priority 2"], "presets": strong, "caps": {"daily_usd": 0.02},
                            "tranche": {"size": 3, "min_days": 7, "check_hours": 0, "carry_over": 0.34}}})
    crm = LaneCRM([_r("st%d" % i, "%d Strong St" % i, ["Priority 2"],
                      phones=[{"number": "86555511%02d" % i, "status": "UNKNOWN", "tags": [], "type": "MOBILE"}]) for i in range(1, 9)])

    def held():
        return sorted(u for u, r in crm.recs.items() if r["assigned_to"])

    p = go("s_assign", crm, pol, plan=True)
    check(p.acted == 3 and p.tally.get("would draw tranche 1 of the strong lane") == 3 and lanes.tranche_state("strong") is None
          and not crm.writes, "a plan run works out the first tranche and leaves none behind for a live run to inherit")
    a1 = go("s_assign", crm, pol)
    check(held() == ["st1", "st2", "st3"] and lanes.tranche_state("strong")["n"] == 1
          and a1.tally.get("behind the current tranche, waiting their turn") == 5,
          "the lane is worked a tranche at a time: three of eight records are drawn and the other five wait")
    n0 = len(crm.reads)
    sc = go("s_score", crm, pol)
    check(set(crm.reads[n0:]) <= {"st1", "st2", "st3"} and len(crm.reads) > n0, "every stage of the lane works the same tranche: scoring reads only its records")
    check(sc.tally.get("numbers scored") == 1 and sc.tally.get("numbers left unscored (strong lane cap $0.02/day)") == 2,
          "the lane's own daily cap stops the lane's scoring after the first number")
    crm_f = LaneCRM([_r("fs1", "1 Other St", ["FTM"], phones=[{"number": "8655551201", "status": "UNKNOWN", "tags": [], "type": "MOBILE"}])])
    check(go("f_score", crm_f, pol).tally.get("numbers scored") == 1, "another lane is not held by the Strong lane's cap")
    pol_m = parse({"s_mail": stage("mail", "ops", lane="strong", vendor="csv", pieces=[{"preset": "Strong - 02 Ready to Call", "piece": "postcard"}])},
                  {"ftm": {"tags": ["FTM"]}, "strong": {"tags": ["Priority 2"], "caps": {"daily_usd": 5, "outreach_daily_usd": 2}}})
    mail_ctx = ctx_for(pol_m, "s_mail", LaneCRM([]))
    check(mail_ctx.budget(1.5)[0] and mail_ctx.budget(2.5) == (False, "strong lane cap $2.00/day"),
          "a lane's mail has its own cap as well, apart from its data cap")
    a2 = go("s_assign", crm, pol, now=T0 + timedelta(days=1))
    check(a2.acted == 0 and held() == ["st1", "st2", "st3"], "the next day nothing more is drawn: the backlog waits behind the tranche")
    crm.recs["st1"]["predictivecall_attempts"] = 1
    a3 = go("s_assign", crm, pol, now=T0 + timedelta(days=8))
    check(a3.acted == 0 and lanes.tranche_state("strong")["n"] == 1 and lanes.tranche_state("strong")["waiting"] == 2,
          "after a week the tranche is checked: two of its three records still wait to be dialed, so the next is not drawn")
    crm.recs["st2"]["predictivecall_attempts"] = 1
    a4 = go("s_assign", crm, pol, now=T0 + timedelta(days=8, hours=1))
    check(held() == ["st1", "st2", "st3", "st4", "st5", "st6"] and lanes.tranche_state("strong")["n"] == 2
          and any("Drew tranche 2" in ln for ln in a4.lines), "once all but a few have been dialed the next tranche is drawn, never a record twice")
    check(len(stages.stage_rows(ctx_for(pol, "s_keeper", crm))) == 6,
          "a stage that does not ask for the tranche (the attempt keeper) sees every record in its presets")
    later = T0 + timedelta(days=8, hours=2)
    lines = stages.lane_lines(ctx_for(pol, "s_digest", crm, now=later))
    check(len(lines) == 1 and lines[0].startswith("Strong lane: tranche 2, 3 record(s)") and "spent on it" in lines[0]
          and "3 still waiting, 3 behind it" in lines[0], "the digest gets one line per lane: tranche size, cost and what is left")
    check(any(ln.startswith("Strong lane:") for ln in go("s_digest", crm, pol, now=later).lines), "that line is in the morning digest")
    crm_x = LaneCRM([_r("xn1", "1 None St", ["FTM"], phones=[_phone("8655551301")])])
    x = go("x_none", crm_x, pol)
    check(x.acted == 0 and not crm_x.writes and any("no tranche size" in pr for pr in x.problems),
          "a stage told to work in tranches with no size set does nothing and says so, it does not work the whole backlog")
    crm_b = LaneCRM([_r("bz%d" % i, "%d Size St" % i, ["Big"], phones=[_phone("86555514%02d" % i)]) for i in range(1, 6)])
    b = go("b_size", crm_b, pol)
    check(b.acted == 2 and lanes.tranche_state("bulk")["size"] == 2, "a stage may carry the tranche size itself: two records are drawn from its own presets")


# ---------------------------------------------------------------------- the hot auction lane (plan 1.8)
def _auction(check, go, parse, auctiontasks, gate, ledger) -> None:
    print("  hot auction tasks and the deed check")
    today = T0.astimezone(ET).date()
    hot, base = ["FTM", "Courthouse Data", "Auction Soon"], ["FTM", "Courthouse Data"]
    passed = "Auction Date Passed"

    def day(n):
        return (today + timedelta(days=n)).isoformat()

    pol = parse({"k_hot": stage("auctiontasks", "analysis", lane="hot_auction", scope_tags=base, tag="Auction Soon",
                                package_assignee="Adriana", door_knock_assignee="Adriana", deed_check_tries=2),
                 "k_auction": stage("auction", "analysis", [], scope_tags=base, soon_days=45, after_sale_status=passed)})
    sale = {"h1": 20, "h2": 20, "h3": 5, "h4": 5, "h5": 10, "h6": 20, "p1": -1, "p2": -2, "d1": -50, "d2": -50, "d3": -50,
            "d4": -30, "d5": -50, "d6": -50, "d7": -50, "d8": -50, "d9": -50, "d10": -100}

    def crm_new():
        recs = [_r("h1", "1 Sale St", hot, phones=[_phone("8655552001"), _phone("8655552002", kind="LANDLINE")],
                   predictivecall_attempts=5, assigned_to="u-adriana"),
                _r("h2", "2 Sale St", hot, phones=[_phone("8655552011")], predictivecall_attempts=2),
                _r("h3", "3 Sale St", hot, phones=[_phone("8655552021", tier="Drop")], predictivecall_attempts=5, equity_percent=40),
                _r("h4", "4 Sale St", hot + ["Do Not Market"], phones=[_phone("8655552031")], predictivecall_attempts=5, equity_percent=40),
                _r("h5", "5 Sale St", hot, phones=[_phone("8655552041")], predictivecall_attempts=5),
                _r("h6", "6 Sale St", hot, phones=[_phone("8655552051")], predictivecall_attempts=5, status="Hot Lead"),
                _r("p1", "1 Past St", base, phones=[_phone("8655552061")]),
                _r("p2", "2 Past St", base, phones=[_phone("8655552071")], status="Hot Lead"),
                _r("d1", "1 Deed St", base, status=passed), _r("d2", "2 Deed St", base, status=passed),
                _r("d3", "3 Deed St", base, status=passed, last_sold=day(-40)), _r("d4", "4 Deed St", base, status=passed),
                _r("d5", "5 Deed St", base, status=passed, county="Sevier"), _r("d6", "6 Deed St", base, status=passed),
                _r("d7", "7 Deed St", base + ["Do Not Market"], status=passed),
                _r("d8", "8 Deed St", base, status=passed, first="Lee", last="Buyer"),
                _r("d9", "9 Deed St", base, status=passed), _r("d10", "10 Deed St", base, status=passed)]
        c = LaneCRM(recs)
        c.custom = {u: {"Foreclosure Date": day(n)} for u, n in sale.items()}
        return c

    roll_book = {"1 Deed St": "EXAMPLE PAT Q", "2 Deed St": "INVENTED INVESTMENTS LLC", "7 Deed St": "EXAMPLE PAT", "8 Deed St": "BUYER LEE"}
    looked = []

    def roll(street, county):
        looked.append(street)
        if county.strip().lower() not in ("knox", "blount"):
            raise LookupError("no county roll is wired for %s" % county)
        if street == "9 Deed St":
            raise OSError("the roll timed out")
        return {"owner": roll_book[street], "address": street.upper()} if street in roll_book else None

    auctiontasks.lookup_owner = roll
    check(auctiontasks.owner_matches("Pat", "Example", "", "EXAMPLE PAT Q") and not auctiontasks.owner_matches("Pat", "Example", "", "EXAMPLE LEE")
          and not auctiontasks.owner_matches("Pat", "Example", "", "SAMPLE PAT") and auctiontasks.owner_matches("", "", "Invented Holdings LLC", "INVENTED HOLDINGS LLC"),
          "the county's owner is the borrower only on surname and first name together; a company on the words of its name")
    q = auctiontasks.street_queries("100 Example Street")
    check("100 EXAMPLE ST" in q and "EXAMPLE ST 100" in q and q[0] == "100 Example Street",
          "a street is asked for the way the county writes it: suffix abbreviated, then the house number last")
    check(auctiontasks.same_house("100 Example Street", "EXAMPLE ST 100") and auctiontasks.same_house("100 Example Street", "100 EXAMPLE ST")
          and not auctiontasks.same_house("100 Example Street", "102 EXAMPLE ST") and not auctiontasks.same_house("100 Example Street", "100 EXAMPLE AVE"),
          "a parcel counts only when its own address is the same house: the search's first result is often the house next door")
    try:
        auctiontasks.county_owner("1 Any St", "Sevier")
        check(False, "a county with no roll wired is refused before any request is made")
    except LookupError:
        check(True, "a county with no roll wired is refused before any request is made")
    check(auctiontasks.sold_since(date(2026, 9, 1), "2026-09-20") and not auctiontasks.sold_since(date(2026, 9, 1), "2024-03-01")
          and not auctiontasks.sold_since(date(2026, 9, 1), ""),
          "the sold guard's rule: a last sale on or after the sale date means it sold; the owner's own purchase years before does not")

    _fresh()
    ledger.set_fact("d8", "borrower", {"first": "Pat", "last": "Example", "company": ""})
    crm_p = crm_new()
    pl = go("k_hot", crm_p, pol, plan=True)
    check(not crm_p.writes and not crm_p.tasks and pl.tally.get("call tasks") == 2 and crm_p.recs["p1"]["status"] is None,
          "a plan run lists the call tasks and the close-out it would make, and writes nothing")
    check(pl.tally.get("deed check: borrower still owns it") == 1 and crm_p.recs["d1"]["status"] == passed and ledger.fact("d1", "deed_check") is None,
          "in plan mode a still-owned answer is not remembered as settled, so the live run still brings the record back")

    _fresh()
    ledger.set_fact("d8", "borrower", {"first": "Pat", "last": "Example", "company": ""})
    looked.clear()
    crm = crm_new()
    s = go("k_hot", crm, pol)
    mine = lambda u: [t for t in crm.tasks.values() if t.get("assigned_to_property") == u]  # noqa: E731
    call1 = mine("h1")
    check(len(call1) == 1 and call1[0]["title"].startswith("Call before the sale on Dec 1") and call1[0]["assigned_to_users"] == ["u-adriana"],
          "a hot record whose five-day round is over gets a call task, for the caller who holds it")
    check("ending 2001, ending 2002" in call1[0]["notes"] and "8655552001" not in call1[0]["notes"] + json.dumps(s.as_dict()),
          "the task lists the numbers by their last four digits, the landline last, and nothing the stage prints carries a whole number")
    check(not mine("h2") and s.tally.get("still in the call round (fewer than 5 attempts)") == 1,
          "a hot record still inside its five-day round gets no extra task")
    check(len(mine("h5")) == 1 and not mine("h4") and not mine("h6") and any("Do Not Market" in k for k in s.tally) and any("already a lead" in k for k in s.tally),
          "an unassigned hot record's task goes to a caller; a Do Not Market record and a lead get none")
    pack = [t for t in mine("h3") if t["title"].startswith("Send a priority package")]
    knock = [t for t in crm.tasks.values() if t["title"].startswith("Door knocks before the sale")]
    check(len(pack) == 1 and s.tally.get("no usable number: mail, a package or a door knock only") == 1,
          "a critical record with buy box and equity confirmed is listed for a priority package even with no number to call")
    check(len(knock) == 1 and "3 Sale St" in knock[0]["notes"] and "5 Sale St" not in knock[0]["notes"],
          "a door knock is listed only for the sale inside a week, never for the one ten days out")
    check(s.tally.get("critical, no package: equity not confirmed") == 1, "a critical record with no equity figure gets no package: unknown is not confirmed")
    check(crm.recs["p1"]["status"] == passed and crm.recs["p2"]["status"] == "Hot Lead" and not gate.check(crm.record("p1"), "call").ok,
          "the day after the sale the record is closed out and every channel stops; a lead is left to the lead manager")
    check(crm.recs["d1"]["status"] is None and "Still Owned After Sale" in crm.recs["d1"]["tags"]
          and ledger.fact("d1", "deed_check")["verdict"] == "still_owned",
          "45 days after the sale, a record whose borrower is still on the county roll comes back into the lane, tagged")
    check(crm.recs["d2"]["status"] == passed and ledger.fact("d2", "deed_check")["verdict"] == "new_owner"
          and ledger.fact("d3", "deed_check")["verdict"] == "sold" and "3 Deed St" not in looked,
          "a new owner on the roll stays closed, and a sale on the record settles it without asking the county")
    check(ledger.fact("d8", "deed_check")["verdict"] == "new_owner",
          "the county's owner is compared with the borrower remembered while the record was hot, not with whoever the record names now")
    check(crm.recs["d7"]["status"] == passed and ledger.fact("d7", "deed_check")["verdict"] == "held",
          "a still-owned record whose owner is Do Not Market stays closed")
    check(crm.recs["d4"]["status"] == passed and ledger.fact("d4", "deed_check") is None and s.tally.get("waiting for day 45 after the sale") == 2,
          "a sale 30 days back is not checked yet: the check waits for day 45")
    check(any("5 Deed St" in pr and "no county roll" in pr for pr in s.problems), "a county with no roll wired is listed for a person to check")
    check(ledger.fact("d9", "deed_check") == {"tries": 1, "why": "the county roll did not answer (the roll timed out)",
                                              "at": T0.isoformat(timespec="seconds")} and crm.recs["d9"]["status"] == passed,
          "a county roll that does not answer is not an answer: the record stays closed and is tried again")
    check(ledger.fact("d10", "deed_check")["verdict"] == "unresolved" and any("10 Deed St" in pr and "100 days ago" in pr for pr in s.problems),
          "a sale too old for the check to mean anything is handed to a person once, not checked forever")
    n_tasks = len(crm.tasks)
    s1 = go("k_hot", crm, pol, now=T0 + timedelta(days=1))
    check(len(mine("h1")) == 1 and len(crm.tasks) == n_tasks and s1.tally.get("has a call task from the last 2 days") == 2,
          "the next day there is no second call task, and no second package or door-knock task for the same sale")
    check(any("6 Deed St" in pr and "2 tries" in pr for pr in s1.problems) and ledger.fact("d6", "deed_check")["verdict"] == "unresolved",
          "a record the county roll cannot settle is tried again, then handed to a person")
    s2 = go("k_hot", crm, pol, now=T0 + timedelta(days=2))
    check(len(mine("h1")) == 1 and s2.tally.get("the last call task is still open") == 2 and any("still open" in ln for ln in s2.lines),
          "two days on, a call task still open is named and not stacked under a second one")
    crm.tasks[call1[0]["uuid"]]["completed"] = "2026-11-13T15:00:00Z"
    go("k_hot", crm, pol, now=T0 + timedelta(days=2, hours=1))
    check(len(mine("h1")) == 2, "once the caller completes it, the next call task comes two days after the last")
    s3 = go("k_hot", crm, pol, now=T0 + timedelta(days=21))
    check(crm.recs["h1"]["status"] == passed and all(t["completed"] for t in mine("h1"))
          and s3.tally.get("open call tasks closed after the sale", 0) >= 1,
          "when the sale passes, the record is closed out and the call task still open is closed with it")
    sa = go("k_auction", crm, pol)
    check(crm.recs["d1"]["status"] is None and sa.tally.get("sale passed, borrower still owns it: left in the lane") == 1,
          "the auction stage does not close out a second time the record the deed check brought back")


# ---------------------------------------------------------------------- second chances (plan 4.4)
def _second_chances(check, go, parse, activity, ledger, secondchance) -> None:
    print("  second chances")
    _fresh()
    activity.reset()
    pol = parse({"k_second": stage("secondchance", "analysis"), "k_fast": stage("secondchance", "analysis", intervals={"probate": 30})})
    st = pol.stage("k_second")
    check(secondchance.interval_for(st, {"foreclosure", "priority 1"}) == ("auction", 30) and secondchance.interval_for(st, {"probate"}) == ("probate", 45)
          and secondchance.interval_for(st, {"priority 2"})[1] == 45 and secondchance.interval_for(st, {"tax delinquent"}) == ("tax", 90)
          and secondchance.interval_for(st, set()) == ("everything else", 90),
          "the clock is set by the list: 30 days with a sale ahead, 45 for probate and Priority 1 and 2, 90 for the rest, the shorter one winning")
    ni = "not_interested"

    def yes(number):
        return _phone(number, status="CORRECT")

    def recs():
        return [_r("n1", "1 Second St", ["FTM", "Foreclosure"], status=ni, phones=[yes("8655553001"), _phone("8655553002")], assigned_to="u-adriana"),
                _r("n2", "2 Second St", ["FTM", "Probate"], status=ni, phones=[yes("8655553011")]),
                _r("n3", "3 Second St", ["Priority 1"], status=ni, phones=[yes("8655553021")]),
                _r("n4", "4 Second St", ["Code Violation"], status=ni, phones=[yes("8655553031")]),
                _r("n5", "5 Second St", ["Bulk"], status=ni, phones=[yes("8655553041")]),
                _r("n6", "6 Second St", ["Foreclosure"], status=ni, phones=[_phone("8655553051")]),
                _r("n7", "7 Second St", ["Foreclosure", "Do Not Market"], status=ni, phones=[yes("8655553061")]),
                _r("n8", "8 Second St", ["Foreclosure"], status=ni, phones=[yes("8655553071")], deceased=True),
                _r("n9", "9 Second St", ["Foreclosure"], status="sold", phones=[yes("8655553081")])]

    said = {"n1": 31, "n2": 31, "n6": 31, "n7": 31, "n8": 31}      # days since each no, inside the shared log's reach
    older = {"n3": 46, "n4": 46}                                    # older than the shared log: only in the record's own log

    def crm_new(users=(ADRIANA, TINAA)):
        c = LaneCRM(recs(), users=users)
        c.log_rows = [status_ev(u, u, T0 - timedelta(days=d), None, ni) for u, d in said.items()]
        c.rec_logs = {u: [status_ev(u, u, T0 - timedelta(days=d), None, ni)] for u, d in older.items()}
        return c

    def for_rec(c, u):
        return [t for t in c.tasks.values() if t.get("assigned_to_property") == u]

    crm_p = crm_new()
    p = go("k_second", crm_p, pol, plan=True)
    check(p.acted == 2 and not crm_p.writes and not crm_p.tasks, "a plan run lists the two re-call tasks it would create and writes nothing")
    crm = crm_new()
    s = go("k_second", crm, pol)
    t1 = for_rec(crm, "n1")
    check(len(crm.tasks) == 2 and len(t1) == 1 and t1[0]["assigned_to_users"] == ["u-adriana"] and len(for_rec(crm, "n3")) == 1,
          "a no that is 31 days old on a foreclosure and one 46 days old on Priority 1 each get a re-call task")
    check("ending 3001" in t1[0]["notes"] and "3002" not in t1[0]["notes"] and "8655553001" not in t1[0]["notes"],
          "the task names only the number marked Correct, by its last four digits")
    check(s.tally.get("not due yet") == 3 and not for_rec(crm, "n2") and not for_rec(crm, "n4") and crm.recs["n1"]["status"] == ni,
          "a probate no at 31 days and a code no at 46 are not due yet, and the stage changes no status")
    check("n5" not in crm.reads and "n2" not in crm.reads, "a record that cannot be due yet is not read, and a record's class is remembered between runs")
    check(crm_p.log_reads.count("n3") == 1 and "n3" not in crm.log_reads and "n5" not in crm.log_reads and "n1" not in crm_p.log_reads,
          "a no older than the shared log is read from the record's own log once and remembered; a record with no such entry is timed from first sight")
    check(s.tally.get("no number is marked Correct, so there is nobody confirmed to call") == 1 and not for_rec(crm, "n6"),
          "a record with no number marked Correct gets no task: its other numbers are not called under this lane")
    check(any("Do Not Market" in k for k in s.tally) and any("has died" in k for k in s.tally)
          and not for_rec(crm, "n7") and not for_rec(crm, "n8") and not for_rec(crm, "n9"),
          "the stage lifts Not Interested and nothing else: Do Not Market, a death and a sold house still hold")
    nxt = go("k_second", crm, pol, now=T0 + timedelta(days=1))
    check(len(crm.tasks) == 2 and nxt.tally.get("the last re-call task is still open") == 2, "a task still open is never stacked under a second one")
    finished = T0 + timedelta(days=2)
    crm.tasks[t1[0]["uuid"]]["completed"] = finished.isoformat().replace("+00:00", "Z")
    go("k_second", crm, pol, now=T0 + timedelta(days=20))
    check(len(for_rec(crm, "n1")) == 1 and ledger.fact("n1", "second_chance")["last_recall"][:10] == finished.date().isoformat(),
          "when the caller completes the task, that day is remembered as the last re-call")
    go("k_second", crm, pol, now=T0 + timedelta(days=33))
    check(len(for_rec(crm, "n1")) == 2 and ledger.fact("n1", "second_chance")["n"] == 2,
          "each repeat is timed from the last completed re-call: 30 days after the caller finished, not 30 days after the first no")
    gone = for_rec(crm, "n3")[0]["uuid"]
    del crm.tasks[gone]
    sd = go("k_second", crm, pol, now=T0 + timedelta(days=34))
    check(sd.tally.get("the last re-call task was deleted, timed again from today") == 1 and not for_rec(crm, "n3"),
          "a task somebody deleted is not handed straight back: the clock starts again that day")
    _fresh()
    activity.reset()
    crm_f = crm_new()
    go("k_fast", crm_f, pol)
    check(len(for_rec(crm_f, "n2")) == 1, "the playbook can change a list's clock: probate at 30 days makes the 31-day-old no due")
    _fresh()
    activity.reset()
    crm_n = crm_new()
    nobody = go("k_second", crm_n, parse({"k_second": stage("secondchance", "analysis")}, people={"callers": ["Tinaa"]}))
    check(not crm_n.tasks and any("no active caller" in pr for pr in nobody.problems), "with no active caller no task is created, and that is said")
    _fresh()
    activity.reset()
    by_preset = parse({"k_preset": stage("secondchance", "analysis", ["Not Interested - FTM Foreclosure (30d)"])})
    check(go("k_preset", crm_new(), by_preset, plan=True).acted == 2, "the stage can read its records from the Not Interested presets instead of a status scan")
    _fresh()
    activity.reset()
    rot = parse({"k_rot": stage("secondchance", "analysis", max_reads=1)})
    crm_r = LaneCRM([_r("q1", "1 Turn St", ["Priority 1"], status=ni, phones=[yes("8655553091")]),
                     _r("q2", "2 Turn St", ["Priority 1"], status=ni, phones=[yes("8655553092")])], users=(ADRIANA, TINAA))
    for u in ("q1", "q2"):
        crm_r.tasks["task-" + u] = {"uuid": "task-" + u, "completed": None, "assigned_to_property": u}
        ledger.set_fact(u, "second_chance", {"task": "task-" + u})
    go("k_rot", crm_r, rot)
    go("k_rot", crm_r, rot, now=T0 + timedelta(days=1))
    looked = sorted(str((ledger.fact(u, "second_chance") or {}).get("looked") or "")[:10] for u in ("q1", "q2"))
    check(looked == [T0.date().isoformat(), (T0 + timedelta(days=1)).date().isoformat()],
          "with more open tasks than reads, each run reads the ones looked at longest ago, so every record is reached in turn")


# ---------------------------------------------------------------------- the weekly caller book (plan 4.2)
def _caller_book(check, go, parse, ledger) -> None:
    print("  weekly caller book")
    _fresh()
    monday = datetime(2026, 11, 16, 7, 30, tzinfo=ET).astimezone(timezone.utc)
    rungs = ["P1 - Rung 1", "P1 - Rung 2"]
    book = stage("callerbook", "analysis", rungs, book_size=3, caller_cap=5, best_caller="Marcus", best_rungs=1)
    people = {"callers": ["Adriana", "Marcus"]}
    pol = parse({"k_book": book, "k_dream": stage("callerbook", "analysis", rungs, book_size=1, dream100_caller="Adriana", dream100_per_day=2)},
                people=people)

    def recs():
        out = [_r("b%d" % i, "%d Book St" % i, ["Priority 1", "Rung 1" if i < 5 else "Rung 2"], phones=[_phone("86555540%02d" % i)])
               for i in range(1, 11)]
        out[1]["status"] = "Hot Lead"                   # b2: a lead is never put in a book
        out[5]["tags"].append("Do Not Market")           # b6
        return out

    crm_p = LaneCRM(recs(), users=(ADRIANA, MARCUS))
    p = go("k_book", crm_p, pol, now=monday, plan=True)
    check(p.acted == 6 and not crm_p.writes and "Would hand out" in p.head, "a plan run lists the book and assigns nothing")
    crm = LaneCRM(recs(), users=(ADRIANA, MARCUS))
    s = go("k_book", crm, pol, now=monday)

    def who(u):
        return crm.recs[u]["assigned_to"]

    check([who("b1"), who("b3"), who("b4")] == ["u-marcus"] * 3, "the best caller's book is filled first, from the top rung")
    check([who("b5"), who("b7"), who("b8")] == ["u-adriana"] * 3 and who("b9") is None and who("b10") is None,
          "each caller gets a book of the set size and nothing more: the rest wait for next Monday")
    check(who("b2") is None and who("b6") is None and any("already a lead" in k for k in s.tally) and any("Do Not Market" in k for k in s.tally),
          "a lead and a Do Not Market record are never put in a book")
    check("b9" not in crm.reads and "b10" not in crm.reads, "once every book is full the stage stops reading")
    check(len(ledger.changes(record_uuid="b1")) == 1 and ledger.changes(record_uuid="b1")[0]["what"] == "assign",
          "each assignment is recorded with what it replaced, so the week can be put back")
    mid = go("k_book", crm, pol, now=monday + timedelta(days=2))
    check(mid.acted == 0 and who("b9") is None and "Nothing is added until next Monday" in mid.head,
          "a run later the same week adds nothing, whatever is waiting")
    go("k_book", crm, pol, now=monday + timedelta(days=7))
    check(who("b9") and who("b10") and all(sum(1 for r in crm.recs.values() if r["assigned_to"] == u) <= 5 for u in ("u-adriana", "u-marcus")),
          "the next Monday the book goes out again, and nobody is taken past the cap")
    crm.recs["b11"] = _r("b11", "11 Book St", ["Priority 1", "Rung 2"], phones=[_phone("8655554011")])
    full = go("k_book", crm, parse({"k_book": dict(book, caller_cap=3)}, people=people), now=monday + timedelta(days=14))
    check(who("b11") is None and sum(1 for pr in full.problems if "at or over the cap of 3" in pr) == 2,
          "a caller at the cap gets nothing new and is named")
    _fresh()
    pol_t = parse({"k_top": stage("callerbook", "analysis", rungs, book_size=3, top_up=True)})
    crm_t = LaneCRM([_r("c%d" % i, "%d Short St" % i, ["Priority 1", "Rung 1"], phones=[_phone("86555542%02d" % i)]) for i in (1, 2)])
    go("k_top", crm_t, pol_t, now=monday)
    for i in (3, 4):
        crm_t.recs["c%d" % i] = _r("c%d" % i, "%d Short St" % i, ["Priority 1", "Rung 1"], phones=[_phone("86555542%02d" % i)])
    go("k_top", crm_t, pol_t, now=monday + timedelta(days=2))
    check(crm_t.recs["c3"]["assigned_to"] == "u-adriana" and crm_t.recs["c4"]["assigned_to"] is None,
          "with top_up a later run fills a book that came up short, and never starts a second one")
    _fresh()
    dream = [_r("dr%d" % i, "%d Dream St" % i, ["Dream"], lists=["Dream 100"], phones=[_phone("86555541%02d" % i)]) for i in range(1, 6)]
    dream[2]["status"] = "Warm Lead"
    crm_d = LaneCRM(dream, users=(ADRIANA, MARCUS))
    go("k_dream", crm_d, pol, now=monday)
    days = [t for t in crm_d.tasks.values() if t["title"].startswith("Dream 100")]
    check([t["title"] for t in days] == ["Dream 100: Monday", "Dream 100: Tuesday"] and all(t["assigned_to_users"] == ["u-adriana"] for t in days)
          and crm_d.recs["dr1"]["assigned_to"] == "u-adriana" and crm_d.recs["dr3"]["assigned_to"] is None,
          "the Dream 100 goes to one caller, laid out a day at a time, and a record that is already a lead is left out")
    _fresh()
    pol_dt = parse({"k_dream_top": stage("callerbook", "analysis", rungs, book_size=1, top_up=True, dream100_caller="Adriana",
                                         dream100_per_day=2)}, people=people)
    crm_dt = LaneCRM([_r("dt%d" % i, "%d Board St" % i, ["Dream"], lists=["Dream 100"], phones=[_phone("86555543%02d" % i)])
                      for i in range(1, 4)], users=(ADRIANA, MARCUS))
    go("k_dream_top", crm_dt, pol_dt, now=monday)
    laid = len(crm_dt.tasks)
    go("k_dream_top", crm_dt, pol_dt, now=monday + timedelta(days=2))
    check(laid == 2 and len(crm_dt.tasks) == 2, "a top-up run later in the week does not lay the Dream 100 out a second time")


# ---------------------------------------------------------------------- dial mode and the dialer file (plan 4.3)
def _dial_export(check, go, parse, activity, dialexport, laneroutes, tmp) -> None:
    print("  dial mode and the dialer file")
    _fresh()
    activity.reset()
    check(dialexport.dial_mode(499, 500, 1000) == "single" and dialexport.dial_mode(500, 500, 1000) == "near"
          and dialexport.dial_mode(1000, 500, 1000) == "power", "under 500 a list is dialed one at a time, and from 1,000 it goes to the power dialer")
    lists = ["Bulk - Small", "Bulk - Mid", "Bulk - Big"]
    block = stage("dialexport", "ops", lists, lane="bulk", single_dial_under=3, power_dialer_over=6, rest_after_days=2)
    pol = parse({"k_dial": block, "k_sift": dict(block, datasift_export=True)}, {"ftm": {"tags": ["FTM"]}, "bulk": {"timeline": True}})

    def recs():
        out = [_r("sm%d" % i, "%d Small St" % i, ["Small"], phones=[_phone("86555550%02d" % i)]) for i in range(1, 3)]
        out += [_r("md%d" % i, "%d Mid St" % i, ["Mid"], phones=[_phone("86555551%02d" % i)]) for i in range(1, 5)]
        same_owner = _r("x3", "3 Big St", ["Big"], phones=[_phone("8655556021")])
        same_owner["owner"]["uuid"] = "o-x1"
        out += [_r("x1", "1 Big St", ["Big"], phones=[_phone("8655556001"), _phone("8655556002", kind="LANDLINE"), _phone("8655556003", tier="Dial Third")]),
                _r("x2", "2 Big St", ["Big"], phones=[_phone("8655556011", tier="Dial Second", status="NO_ANSWER")]),
                same_owner,
                _r("x4", "4 Big St", ["Big", "Do Not Market"], phones=[_phone("8655556031")]),
                _r("x5", "5 Big St", ["Big"], phones=[_phone("8655556041", tier="Drop")]),
                _r("x6", "6 Big St", ["Big"], phones=[_phone("8655556051", status="CORRECT")]),
                _r("x7", "7 Big St", ["Big"], phones=[_phone("8655556061")]),
                _r("x8", "8 Big St", ["Big"], phones=[_phone("8655556071")], status="not_interested"),
                _r("x9", "9 Big St", ["Big"], phones=[_phone("8655556081")], row_phone={"number": "8655556081", "doNotCall": True}),
                _r("x10", "10 Big St", ["Big"], phones=[_phone("8655556091")], predictivecall_attempts=6)]
        return out

    def crm_new():
        c = LaneCRM(recs())
        c.log_rows = [dial("x7a", "o-x7", T0 - timedelta(days=3), number="8655556061"),
                      dial("x7b", "o-x7", T0 - timedelta(days=2), number="8655556061")]
        return c

    folder = tmp / "dialexport"
    crm_p = crm_new()
    p = go("k_dial", crm_p, pol, plan=True)
    check(p.acted == 2 and "Would write a dialer file of 3 number(s) on 2 owner(s), 1 landline(s) last" in p.head
          and not folder.exists() and not crm_p.writes, "a plan run counts what the file would hold and writes no file")
    check("Bulk - Small: 2 record(s), dial them one at a time." in p.lines
          and any(ln.startswith("Bulk - Mid: 4 record(s), still one at a time") for ln in p.lines),
          "a small list is dialed one at a time and gets no file; a list close to the threshold is said to be close")
    crm = crm_new()
    s = go("k_dial", crm, pol)
    files = sorted(folder.rglob("*.csv"))
    rows = []
    if files:
        with files[0].open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    check(len(files) == 1 and [r["phone"] for r in rows] == ["8655556001", "8655556011", "8655556002"],
          "the live run writes one file: a row per number a cold call may use, and the landline last")
    check(bool(rows) and rows[0]["record_uuid"] == "x1" and rows[0]["tier"] == "Dial First" and rows[2]["line_type"] == "LANDLINE"
          and rows[0]["first_name"] == "Pat" and "8655556003" not in [r["phone"] for r in rows],
          "each row carries who to ask for and the tier, and a Dial Third number is not in the file")
    printed = json.dumps(s.as_dict())
    check("8655556001" not in printed and "ending 6001" in printed and bool(files) and str(files[0]) in s.head,
          "nothing the stage prints carries a whole number, and the head says where the file is on the box")
    check(s.tally.get("another record of an owner already in the file") == 1
          and s.tally.get("numbers resting (dialed on 2 separate days, no answer)") == 1 and s.tally.get("the call round is over") == 1
          and s.tally.get("no top-tier number that nobody has reached yet") == 1,
          "an owner appears once, a number dialed on enough separate days rests, a finished round and a confirmed number are left out")
    check(any("Do Not Market" in k for k in s.tally) and sum(n for k, n in s.tally.items() if "no usable number" in k) == 2
          and "x8" not in crm.reads and "x3" not in crm.reads,
          "a held record contributes no row (Do Not Market, a Drop-only record, the registry-flagged number), and a row held by its status is not even read")
    s2 = go("k_dial", crm, pol, now=T0 + timedelta(days=1))
    check(s2.acted == 0 and s2.tally.get("already in a file this week") == 3 and len(list(folder.rglob("*.csv"))) == 1,
          "a record in this week's file is not exported again, nor is its owner through another record")
    crm.recs["x2"]["tags"].append("Do Not Market")
    again = dialexport.recheck(files[0], "k_dial", crm=crm, pol=pol, now=T0) if files else {}
    kept = []
    if again:
        with open(again["file"], newline="", encoding="utf-8") as f:
            kept = [r["phone"] for r in csv.DictReader(f)]
    check(kept == ["8655556001", "8655556002"] and again.get("dropped") == 1,
          "before a file is loaded it can be checked again: an owner who opted out since it was built is dropped")
    _fresh()
    activity.reset()
    crm_s = crm_new()
    ps = go("k_sift", crm_s, pol, plan=True)
    check(not crm_s.exports and ps.tally.get("presets that would get DataSift's own scheduled export") == 1,
          "a plan run sets no scheduled export in DataSift")
    go("k_sift", crm_s, pol)
    body = crm_s.exports.get("Bulk - Big") or {}
    must = body.get("export_config", {}).get("columns", {}).get("phones", {}).get("must", {})
    check(list(crm_s.exports) == ["Bulk - Big"] and must.get("any_phone_tags") == ["tag-dial first", "tag-dial second"]
          and must.get("phone_status") == ["NO_ANSWER", "UNKNOWN"] and body.get("recurrence") == "weekly"
          and body["export_config"]["export_messages"] is False,
          "when asked, DataSift's own weekly export is set on the large list only, limited to the same tiers and statuses")
    later = go("k_sift", crm_s, pol, now=T0 + timedelta(days=8))
    check(later.tally.get("presets with DataSift's own scheduled export already set") == 1
          and [w for w in crm_s.writes if w[0] == "export"] == [("export", "Bulk - Big", "POST")], "that export is set once, not on every run")
    _fresh()
    activity.reset()
    pol_tq = parse({"k_tdial": stage("dialexport", "ops", ["Bulk - Big"], lane="bulk", tranche=True, single_dial_under=2,
                                     power_dialer_over=4, rest_after_days=0)},
                   {"ftm": {"tags": ["FTM"]}, "bulk": {"timeline": True, "tranche": {"size": 5}}})
    big = [_r("tq%d" % i, "%d Tranche Rd" % i, ["Big"], phones=[_phone("86555552%02d" % i)]) for i in range(1, 9)]
    tq = go("k_tdial", LaneCRM(big), pol_tq)
    tq_files = sorted(Path(tmp).rglob("k_tdial_*.csv"))
    in_file = set()
    if tq_files:
        with tq_files[-1].open(newline="", encoding="utf-8") as f:
            in_file = {r["record_uuid"] for r in csv.DictReader(f)}
    check(in_file == {"tq%d" % i for i in range(1, 6)} and tq.found == 5,
          "a lane worked in tranches is dialed a tranche at a time: the file holds the current tranche and nothing behind it")
