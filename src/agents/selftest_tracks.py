"""Two-tracks selftest: the lane order with the Top 1,000 and the agency lane, the shipped playbook,
the stages that work two lanes, the Top 1,000 ranked by owner and kept current (plan and live, with
undo), and the agency's monthly file (the 90-day rule, the size cap, both release steps and the
night marking).

Zero network: the autopilot's fake DataSift, extended with the tag and list index and the add-lists
route, and a fake SiftMap search. Every name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, ET, PRESETS, _rec
from .selftest_phase0 import status_ev, tag_ev
from .selftest_textlane import Crm, at, mob

COUNTIES = [{"fips": "47093", "name": "Knox", "state": "TN"}]
NOV = datetime(2026, 11, 11, 1, 0, tzinfo=ET)        # the first night slot of a November refresh
DEC = datetime(2026, 12, 2, 1, 0, tzinfo=ET)
FEB = datetime(2027, 2, 15, 1, 0, tzinfo=ET)         # 96 days after the November file went out
# The preset titles the 2026-11-13 restructure created in the account, exactly.
PRIORITY_TITLES = {
    "Priority - 00 Needs Skipped", "Priority - 01 Skipped No Numbers", "Priority - 01 Ready to Text",
    "Priority - 02 Texted, Not Yet Called", "Priority - 02 Ready to Call", "Priority - 03 Call Attempt 1",
    "Priority - 04 Call Attempt 2", "Priority - 05 Call Attempt 3", "Priority - 06 Call Attempt 4",
    "Priority - Mail 01 Handwritten Letter", "Priority - Mail 02 Family Postcard", "Priority - Mail 03 Soft-Offer Check",
    "Priority - Mail 04 Handwritten Letter", "Priority - Mail 05 Family Postcard", "Priority - Mail 06 Soft-Offer Check",
    "Agency - 01 Queue", "Agency - 02 Sent", "Agency - 03 Came Back",
}
TRACK_PRESETS = {
    "Agency - 01 Queue": lambda r: "Pool" in r["tags"],
    "Priority - 02 Ready to Call": lambda r: ("FTM" in r["tags"] or "Top 1000" in r["tags"]) and r["owner"]["phones"]
                                             and r["predictivecall_attempts"] == 0,
}


class TrackCRM(Crm):
    """The fake DataSift plus the tag and list index (uuid and title), the documented add-lists
    route, and the custom date field read back by its label."""
    fields = {"last mailed": "f-last-mailed", "sent to agency": "f-sent"}
    BASE_TAGS = ["Do Not Market", "Estate Sold", "FTM", "Heir Identified", "Litigator", "Obituary", "Pool",
                 "Sold", "Top 1000", "Auction Soon", "recently sold"]

    def __init__(self, records, **kw):
        super().__init__(records, **kw)
        self.broken_tags = False
        self.opened = []

    def record(self, u):
        self.opened.append(u)
        return super().record(u)

    def _titles(self, key: str) -> list:
        out = set(self.BASE_TAGS if key == "tags" else ["Agency", "DNC", "Obituary", "Probate"])
        for r in self.recs.values():
            out |= set(r.get(key) or [])
        return sorted(out)

    def call(self, path, method="GET", body=None, headers=None):
        # Read piece by piece, never matched against a pattern: the API audit reads route-shaped strings.
        parts = [p for p in path.split("?")[0].split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "internal"] and parts[2] in ("tag", "list") and method == "GET":
            key = "tags" if parts[2] == "tag" else "lists"
            return {"results": [{"uuid": "%s-%s" % (parts[2], t.lower()), "title": t} for t in self._titles(key)]}
        if len(parts) == 5 and parts[:3] == ["api", "internal", "property"] and parts[4] == "add-lists" and method == "POST":
            u, title = parts[3], body["lists"]
            self.writes.append(("add_lists", u, title))
            if title not in self.recs[u].setdefault("lists", []):
                self.recs[u]["lists"].append(title)
            return {"new_lists": [title]}
        return super().call(path, method, body, headers)

    def set_custom_field(self, u, field_uuid, value):
        super().set_custom_field(u, field_uuid, value)
        label = {v: k for k, v in self.fields.items()}.get(field_uuid, field_uuid)
        self.custom.setdefault(u, {})["Sent to Agency" if label == "sent to agency" else label] = value

    def add_tags(self, u, add, rec=None):
        self.tag_tries = getattr(self, "tag_tries", []) + [u]
        if getattr(self, "gone_first", False) and not getattr(self, "gone_seen", None):
            self.gone_seen = u
            err = RuntimeError("HTTP 404 on POST add-tags")
            err.code = 404
            raise err
        if self.broken_tags:
            self.writes.append(("add_tags", u, tuple(add)))
            return
        super().add_tags(u, add, rec)


class FakeMap:
    """SiftMap's search over the fake account: the buy box value range, the in-account tag exclusion,
    the county, and pages by a 1-based row offset and a page size."""

    def __init__(self, crm, outside: int = 0):
        self.crm, self.calls, self.bodies, self.outside = crm, 0, [], outside

    def row(self, r: dict, tags: list) -> dict:
        return {"id": "d-" + r["uuid"], "address": "%s, Knoxville, TN 37917" % r["address"]["street"], "county": "Knox",
                "fips": "47093", "estimatedValue": r.get("estimate_value"), "equityPercent": r.get("equity_percent"),
                "investor_off_market_score": r.get("investor_score", 60), "realtor_score": r.get("realtor_score", 10),
                "absenteeOwner": False, "corporateOwned": bool(r["owner"].get("company")),
                "propertyUse": "Residential (General) (Single)", "mlsListed": False, "saved": True, "saved_uuid": r["uuid"],
                "saved_tags": tags, "saved_lists": ["list-" + x.lower() for x in r.get("lists") or []],
                "distressors": {k: {"is_active": True} for k in r.get("flags") or []}}

    def __call__(self, crm, body):
        self.calls += 1
        self.bodies.append(copy.deepcopy(body))
        f = body.get("filters") or {}
        lo, hi = f.get("value_min", 0), f.get("value_max", 10 ** 9)
        out_tags = set(f.get("in_my_account_tags_exclude") or [])
        county = ((body.get("addresses") or [{}])[0]).get("county")
        rows = []
        for r in sorted(self.crm.recs.values(), key=lambda x: x["uuid"]):
            v = r.get("estimate_value") or 0
            tags = ["tag-" + t.lower() for t in r["tags"]]
            if not lo <= v <= hi or county != "Knox" or out_tags & set(tags):
                continue
            rows.append(self.row(r, tags))
        # Houses SiftMap returns although they are not in the account: what a filter that stopped
        # holding would look like. They sit in the value band like any other row.
        rows += [{"id": "x-%d" % i, "saved": False, "saved_uuid": None, "estimatedValue": 150000 + i}
                 for i in range(self.outside) if lo <= 150000 + i <= hi and county == "Knox"]
        start = int(body.get("result_index") or 1) - 1
        return {"total_results": len(rows), "data": rows[start:start + int(body.get("size") or 25)]}


def house(u, street, owner, value=200000, equity=25.0, tags=(), lists=(), phones=(), investor=60, **kw):
    r = _rec(u, street, phones=[dict(p) for p in phones], **kw)
    r["tags"] = list(tags) + ["Pool"]
    r["lists"] = list(lists)
    r["owner"]["uuid"] = owner
    r["estimate_value"], r["equity_percent"], r["investor_score"] = value, equity, investor
    return r


def account() -> list:
    """Owners A to R. A holds two houses; C, D, F and I are out whole for what one house says; E keeps
    one house of two; K, L and R hold the tag today and score nothing, and R is a live lead now."""
    first = mob(5101)
    return [
        house("a1", "1 Track St", "o-a", phones=[first]), house("a2", "2 Track St", "o-a", phones=[first]),
        house("b1", "3 Track St", "o-b", value=300000, equity=30.0),
        house("c1", "4 Track St", "o-c", tags=["FTM"]), house("c2", "5 Track St", "o-c"),
        house("d1", "6 Track St", "o-d", status="Hot Lead"), house("d2", "7 Track St", "o-d"),
        house("e1", "8 Track St", "o-e", status="not_interested"), house("e2", "9 Track St", "o-e", phones=[mob(5102)]),
        house("f1", "10 Track St", "o-f", tags=["Do Not Market"]), house("f2", "11 Track St", "o-f"),
        house("g1", "12 Track St", "o-g", tags=["recently sold"]),
        house("h1", "13 Track St", "o-h", equity=-10.0),
        house("i1", "14 Track St", "o-i", lists=["Obituary"]),
        house("j1", "15 Track St", "o-j", value=800000),
        house("k1", "16 Track St", "o-k", value=100000, equity=10.0, tags=["Top 1000"], investor=20),
        house("l1", "17 Track St", "o-l", value=100000, equity=10.0, tags=["Top 1000"], investor=20, sms_attempts=1),
        house("m1", "18 Track St", "o-m", value=100000, equity=20.0, phones=[mob(5103, "Dial Second")]),
        house("n1", "19 Track St", "o-n", value=250000, equity=40.0),
        house("p1", "20 Track St", "o-p", value=250000, equity=40.0, phones=[mob(5104)], first="", last="",
              company="Example Holdings LLC"),
        # The account's search row shows Q's second number flagged from the do-not-call registry.
        house("q1", "21 Track St", "o-q", value=220000, equity=30.0, phones=[mob(5105), mob(5106)],
              row_phone={"number": "8655555106", "doNotCall": True}),
        house("r1", "22 Track St", "o-r", value=100000, equity=10.0, tags=["Top 1000"], investor=20, status="Warm Lead"),
    ]


def playbook(tmp: Path, top_mode="plan", agency_mode="plan", **top_over) -> dict:
    cohort = Path(tmp) / "tracks_d4d.json"
    cohort.write_text(json.dumps([{"saved_uuid": "m1", "score": 100, "rank": 1}]), encoding="utf-8")
    # These checks were written for the old Dream 100 points, so the blocks pin them; the deal-odds
    # path (the default since 2026-11-14) has its own section, selftest_deal_odds.
    top = {"kind": "top1000", "owner": "analysis", "mode": top_mode, "schedule": ["1st 00:20", "2nd 00:20"], "lane": "top1000",
           "presets": ["Agency - 01 Queue"], "counties": COUNTIES, "size": 2, "finalists": 1.5, "read_fields": False,
           "pool_lists": ["Agency"], "d4d_cohort": str(cohort), "max_reads": 50, "max_writes": 50, "score": "dream100"}
    top.update(top_over)
    return {
        "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
        "caps": {"daily_usd": 25, "record_usd": 1.0},
        "people": {"callers": ["Adriana"]},
        "lanes": {"ftm": {"tags": ["FTM"]}, "top1000": {"tags": ["Top 1000"], "timeline": True}, "agency": {"timeline": True}},
        "stages": {
            "t_top": top,
            "t_agency": {"kind": "agencyexport", "owner": "ops", "mode": agency_mode, "schedule": "every 60 min 00:00-05:00 daily",
                         "lane": "agency", "presets": ["Agency - 01 Queue"], "counties": COUNTIES, "size": 3, "margin": 0.5,
                         "after_stage": "t_top", "read_fields": False, "release_writes": 1, "max_writes": 50,
                         "list_name": "Example agency list", "score": "dream100"},
            "t_limits": {"kind": "limits", "owner": "ops", "mode": "plan", "schedule": "19:20 Mon-Fri"},
            "t_text": {"kind": "textlane", "owner": "ops", "mode": "live", "schedule": "08:45 Mon-Fri", "lane": "ftm",
                       "lanes": ["ftm", "top1000"], "series": "opening", "presets": ["Priority - 02 Ready to Call"],
                       "labels": {"ftm": "FTM", "top1000": "Top 1000"}, "daily_owners": 2},
            "t_two": {"kind": "assign", "owner": "analysis", "mode": "plan", "schedule": "every 30 min", "lane": "ftm",
                      "lanes": ["ftm", "top1000"], "presets": ["Priority - 02 Ready to Call"], "backlog": True},
        },
    }


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import top1000
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD)
    # Its own data folder inside the suite's: the month files below must not meet another section's.
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = Path(tmp) / "two_tracks", 100000.0, 5.0
    config.MODE = "dry"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS.update(TRACK_PRESETS)
    saved_search, saved_split = top1000.search, top1000.SPLIT_AT
    saved_env = os.environ.pop(top1000.ALLOW_ENV, None)
    try:
        _run(check, Path(tmp))
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        top1000.search, top1000.SPLIT_AT = saved_search, saved_split
        if saved_env is not None:
            os.environ[top1000.ALLOW_ENV] = saved_env
        else:
            os.environ.pop(top1000.ALLOW_ENV, None)
        _wipe()
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = saved
        config.MODE = "dry"


def _wipe() -> None:
    """Start from an empty autopilot memory: this section shares the selftest's file with the others."""
    from . import store
    from .autopilot import limits, scrub
    limits.ensure()
    scrub.ensure()
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones",
                      "ap_texts", "ap_dnc"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _run(check, tmp: Path) -> None:
    _wipe()
    _lane_checks(check, tmp)
    _shipped_checks(check)
    _two_lane_checks(check, tmp)
    _wipe()
    _text_checks(check, tmp)
    _wipe()
    _scoring_checks(check, tmp)
    _wipe()
    _cycle_checks(check, tmp)
    _wipe()
    _remember_checks(check, tmp)


# ---------------------------------------------------------------------- an agency file sent before the stage existed
def _remember_checks(check, tmp) -> None:
    from .autopilot import agencyexport, ledger
    print("  an agency file sent before the stage existed")
    p = Path(tmp) / "two_tracks_earlier_file.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Record ID", "First Name", "Property Address"])
        for row in (["q1", "Pat", "21 Track St"], ["", "Lee", "99 Track St"], ["m1", "Sam", "18 Track St"], ["q1", "Pat", "21 Track St"]):
            w.writerow(row)
    ledger.set_fact("m1", agencyexport.FACT_SENT, "2026-12-01")
    out = agencyexport.remember([str(p)], "2026-11-06")
    check(out == {"rows": 4, "remembered": 1, "kept_a_later_day": 1, "rows_without_a_record_id": 1, "day": "2026-11-06"}
          and ledger.fact("q1", agencyexport.FACT_SENT) == "2026-11-06" and ledger.fact("m1", agencyexport.FACT_SENT) == "2026-12-01",
          "a file sent before the stage existed seeds the 90-day memory by its Record ID column, and a later day is kept")
    try:
        agencyexport.remember([str(p)], "11/06/2026")
        check(False, "a day that is not YYYY-MM-DD is refused before anything is remembered")
    except ValueError:
        check(ledger.fact("q1", agencyexport.FACT_SENT) == "2026-11-06",
              "a day that is not YYYY-MM-DD is refused before anything is remembered")


# ---------------------------------------------------------------------- the lane order and the table
def _lane_checks(check, tmp) -> None:
    from .autopilot import lanes, policy
    print("  lanes: the two tracks")
    check(lanes.ORDER == ("lead", "heirs", "hot_auction", "ftm", "obituary", "top1000", "agency"),
          "the lanes run lead, heirs, hot auction, first to market, obituary, the Top 1,000, then the agency track")
    base = {"uuid": "z1", "status": None, "tags": [], "lists": []}
    check(lanes.lane_of(dict(base, tags=["Top 1000"])) == "top1000" and lanes.lane_of(dict(base, tags=["FTM", "Top 1000"])) == "ftm"
          and lanes.lane_of(dict(base, tags=["Top 1000"], lists=["Obituary"])) == "obituary",
          "the Top 1000 tag puts a record in the Top 1,000 lane, under first to market and the obituary lane")
    check(lanes.lane_of(dict(base, tags=["Priority 1"])) == "agency" and lanes.lane_of(dict(base, tags=["Priority 2"])) == "agency"
          and lanes.lane_of(base) == "agency" and lanes.top_lane([]) == "agency",
          "the Priority 1 and Priority 2 tags no longer decide a lane: everything else in the buy box is the agency track")
    check(all(lanes.rank(old) == lanes.rank("agency") for old in ("priority1", "strong", "bulk")) and lanes.rank("warp") == len(lanes.ORDER),
          "an owner row still naming a retired lane ranks with the agency lane until the nightly check rewrites it")
    try:
        policy.parse(dict(playbook(tmp), lanes={"strong": {"tags": ["Priority 2"]}}), EMPLOYEES)
        check(False, "a playbook naming a retired lane is refused")
    except policy.PolicyError as exc:
        check("retired on 2026-11-13" in str(exc) and "agency" in str(exc),
              "a playbook naming a retired lane is refused, and told where its records went")
    pol = policy.parse(playbook(tmp), EMPLOYEES)
    L = pol.lanes
    co = lambda lane, ch, day=0, counter=0, facts=None: lanes.channel_open(L, lane, ch, day, counter, facts, 2)  # noqa: E731
    check(L.timeline("top1000")["mail_pieces"] == 6 and not co("top1000", "mail", 0, 0, {"foreclosure": True})[0]
          and co("top1000", "mail", 0, 0, {"no_number": True})[0] and co("ftm", "mail", 0, 0, {"foreclosure": True})[0],
          "the Top 1,000 gets six mail pieces and is never mailed at once for a foreclosure; first to market still is")
    check(not co("top1000", "research", 3, 2)[0] and co("top1000", "research", 3, 5)[0],
          "Top 1,000 research waits for the call round to end")
    closed = [ch for ch in ("assign", "text", "call", "mail", "followup_text") if co("agency", ch, 30, 9)[0]]
    check(not closed and "calling agency" in co("agency", "call", 2)[1] and co("agency", "score")[0] and co("agency", "trace")[0],
          "the agency lane is worked outside: nothing is assigned, texted, called or mailed from here")
    check(lanes.describe(L, "agency")[0].startswith("Worked by the calling agency") and lanes.name("top1000") == "Top 1,000",
          "the agency lane prints what it is, and the Top 1,000 is named the way a person writes it")


# ---------------------------------------------------------------------- the playbook on disk
def _shipped_checks(check) -> None:
    from . import release
    from .autopilot import kpi, lanes, pacing, policy, registry
    print("  the shipped playbook")
    pol = policy.load(policy.path(), employees=EMPLOYEES)
    named = [(sid, st.get("lane")) for sid, st in pol.stages.items() if st.get("lane") and str(st.get("lane")) not in lanes.ORDER]
    many = [(sid, x) for sid, st in pol.stages.items() for x in (st.get("lanes") or []) if str(x) not in lanes.ORDER]
    check(not named and not many, "no stage in the playbook names a removed lane: %s" % ((named + many) or "ok"))
    old = [sid for sid in pol.stages if sid.startswith(("hottest_", "strong_", "bulk_")) or sid == "caller_book"]
    check(old and all(pol.stage(sid).mode == "off" for sid in old), "every Hottest, Strong and Bulk stage is switched off")
    top, ag = pol.stage("top1000_refresh"), pol.stage("agency_export")
    check((top.kind, top.owner, top.mode, top.get("lane")) == ("top1000", "analysis", "plan", "top1000")
          and (ag.kind, ag.owner, ag.mode, ag.get("lane")) == ("agencyexport", "ops", "plan", "agency")
          and top.presets == ag.presets == ["Agency - 01 Queue"] and "Obituary" in top.get("exclude_lists"),
          "the Top 1,000 refresh (Atlas) and the agency export (Quinn) ship in plan, reading the agency queue")
    # Three pipelines (2026-11-14): each stage that worked both lanes is two now.
    from .autopilot import stages
    from .selftest_pipeline import TWINS, mixes
    check(all(stages.stage_lanes(pol.stage(s)) == ["ftm"] and stages.stage_lanes(pol.stage(t)) == ["top1000"] for s, t in TWINS.items()),
          "each stage that worked both lanes is two now: the county one works the ftm lane, its twin the Top 1,000")
    reads = [x for t in TWINS.values() for x in pol.stage(t).presets + [p.get("preset") for p in (pol.stage(t).get("pieces") or [])]]
    check(reads and all(x.startswith("Priority - ") for x in reads), "the Top 1,000 twins read the Priority presets")
    check(not [sid for sid, st in pol.stages.items() if mixes(st)], "no stage mixes the two pipelines")
    every = {t for st in pol.stages.values()
             for t in list(st.presets) + [p.get("preset") for p in (st.get("pieces") or []) if isinstance(p, dict)]}
    stray = sorted(t for t in every if (t.startswith(("Priority - ", "Agency - ")) and t not in PRIORITY_TITLES)
                   or t.strip() != t or t in ("Not Yet Called", "Priority - 02 Texted"))
    check(not stray, "every Priority and Agency preset the playbook names is a title the account holds, none split at "
                     "a comma by a [flow, list]: %s" % (stray or "ok"))
    check(pol.stage("ftm_texts").get("label") == "FTM" and pol.stage("top1000_texts").get("label") == "Top 1000"
          and pol.stage("top1000_texts").get("urgent_copy") is False,
          "the sale-date copy is for first to market only, and each pipeline gets its own group prefix")
    cat = kpi.load()
    check(set(cat.pacing["ai_lanes"]) == {"top1000", "agency"} and set(pacing.LADDER) <= set(lanes.ORDER)
          and all(ln in lanes.ORDER for ln in cat.pacing["preset_prefixes"]),
          "the KPI catalog and the pacing rules name only lanes that exist")
    check(registry.EXTRA.get("top1000") == ("top1000", "run_top1000", "free", True, False)
          and registry.EXTRA.get("agencyexport") == ("agencyexport", "run_agencyexport", "outreach", True, False)
          and not [p for p in registry.problems() if p.split(":")[0] in ("top1000", "agencyexport")],
          "both stage kinds are registered and load")
    check(release.RELEASE.get("agency") == ("autopilot.agencyexport", "release") and "agency" in release.FILE_KINDS
          and release.label("agency") == "owner(s) for the calling agency",
          "an agency file is a two-step file batch a named releaser releases")
    src = Path(__file__).resolve().parent / "autopilot"
    text = (src / "top1000.py").read_text(encoding="utf-8") + (src / "agencyexport.py").read_text(encoding="utf-8")
    check("add-properties" not in text and "properties/detail" not in text and chr(0x2014) not in text and chr(0x2013) not in text,
          "neither module can reach SiftMap's add or detail routes, and neither carries a dash")


# ---------------------------------------------------------------------- a stage that works two lanes
def _two_lane_checks(check, tmp) -> None:
    from .autopilot import lanes, ledger, policy, stages
    print("  stages that work two lanes")
    pol = policy.parse(playbook(tmp), EMPLOYEES)
    f1 = _rec("tf1", "31 Lane St", phones=[mob(5201)])
    f1["tags"] = ["FTM"]
    t1 = _rec("tt1", "32 Lane St", phones=[mob(5202)])
    t1["tags"] = ["Top 1000"]
    crm = TrackCRM([t1, f1])
    landed = datetime(2026, 11, 2, 10, 0, tzinfo=ET)
    crm.rec_logs["tt1"] = [tag_ev("in-tt1", "tt1", landed, ["Top 1000"])]
    st = pol.stage("t_two")
    ctx = stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-two", now=NOV, live=False,
                     out=stages.Summary(st.id, st.kind, st.owner, st.mode, False))
    check(stages.stage_lanes(st) == ["ftm", "top1000"] and stages.stage_lanes(pol.stage("t_top")) == ["top1000"],
          "a stage works its `lanes` list, highest first, or its one lane")
    rows = stages.stage_rows(ctx)
    check([(r["uuid"], r["_lane"]) for _, r in rows] == [("tf1", "ftm"), ("tt1", "top1000")],
          "its rows come lane by lane, first to market first, each carrying its lane before the record is opened")
    check(stages.record_lane(ctx, crm.record("tt1")) == "top1000" and stages.record_lane(ctx, crm.record("tf1")) == "ftm"
          and stages.record_lane(ctx, {"uuid": "zz", "tags": []}) == "ftm",
          "a full record is worked in its own lane, and a record that says nothing in the stage's first lane")
    when = stages.lane_entry(ctx, {"uuid": "tt1", "owner": {"uuid": "o-tt1"}}, "ftm")
    check(when == landed.astimezone(when.tzinfo) and ledger.fact("tt1", "lane_entry:top1000") and not ledger.fact("tt1", "lane_entry:ftm"),
          "the attempt keeper asking for the stage's lane gets the record's: a Top 1,000 house counts from its own tag")
    lanes.set_owner_lane("o-tt1", "ftm", {"other": "ftm", "tt1": "top1000"})
    one = stages.Ctx(policy=pol, stage=pol.stage("t_text"), crm=crm, run_id="t-one", now=NOV, live=False,
                     out=stages.Summary("t_text", "textlane", "ops", "live", False))
    v = ctx.verdict(crm.record("tt1"), "assign")
    check(v.code == "owner_lane" and ctx.verdict(crm.record("tf1"), "assign").ok,
          "the gate judges a Top 1,000 house as the lower lane it is: its owner's first-to-market record holds it")
    check(one.verdict(crm.record("tt1"), "assign", lane="ftm").ok,
          "judged in the stage's own lane the same house would have passed, which is the hole the record lane closes")
    out_t = dict(crm.record("tt1"), directmail_attempts=6)
    out_t["owner"]["phones"] = []
    out_f = dict(crm.record("tf1"), directmail_attempts=6)
    out_f["owner"]["phones"] = []
    check(stages.exhausted_of(ctx, out_t) and not stages.exhausted_of(ctx, out_f),
          "a Top 1,000 house with no number runs out after six pieces; a first-to-market one only after twelve")


# ---------------------------------------------------------------------- the text lane on two lanes
def _text_checks(check, tmp) -> None:
    from .autopilot import ledger, policy, sweep, textlane
    print("  the Priority text lane")
    config.MODE = "live"
    pb = playbook(tmp)
    f1 = _rec("xf1", "41 Text Ave", phones=[mob(5301)])
    f1["tags"] = ["FTM", "Auction Critical"]
    f1["owner"]["uuid"] = "o-xf1"
    t1 = _rec("xt1", "42 Text Ave", phones=[mob(5302)])
    t1["tags"] = ["Top 1000", "Auction Critical"]
    t1["owner"]["uuid"] = "o-xt1"
    crm = TrackCRM([t1, f1])
    crm.land("xf1", at(9, 7, 0), tags=("FTM",))
    crm.land("xt1", at(9, 7, 0), tags=("Top 1000",))
    for u in ("xf1", "xt1"):
        ledger.set_fact(u, "death_screen", {"at": "2026-11-09T12:00:00+00:00", "hit": "", "detail": ""})
    one = copy.deepcopy(pb)
    one["stages"]["t_text"].update(mode="plan", daily_owners=1)
    s = sweep.run("t_text", crm=crm, now=at(9), pol=policy.parse(one, EMPLOYEES))
    check(s.acted == 1 and any(ln.startswith("41 Text Ave") for ln in s.lines) and s.tally.get("left for tomorrow (daily cap 1 owners)") == 1,
          "under one day's cap the first-to-market owner is loaded and the Top 1,000 owner waits: first to market on top")
    pol = policy.parse(pb, EMPLOYEES)
    s = sweep.run("t_text", crm=crm, now=at(9), pol=pol)
    b = ledger.batch(s.batch) if s.batch else {"items": []}
    lanes_in = [it["lane"] for it in b["items"]]
    groups = [it["numbers"][0]["group"] for it in b["items"]]
    check(lanes_in == ["ftm", "top1000"] and groups == ["FTM 1109 Named East", "Top 1000 1109 Named East"]
          and "1 first-to-market, 1 Top 1,000" in s.head,
          "with room for both, each owner is staged in its own lane, with its lane's group prefix")
    out = textlane.release(s.batch, crm=crm, pol=pol, commit=True, now=at(9, 9, 0))
    load = json.loads(Path(out["load"]).read_text(encoding="utf-8")) if out.get("load") else {}
    check(out.get("owners") == 2 and sorted(load.get("lanes") or []) == ["ftm", "top1000"]
          and {c["lane"] for c in load.get("contacts") or []} == {"ftm", "top1000"},
          "the released load names both lanes, and every contact carries its own")
    s2 = sweep.run("t_text", crm=crm, now=at(10), pol=pol)
    b2 = ledger.batch(s2.batch) if s2.batch else {"items": []}
    sets = {it["lane"]: (it["step"], it["template_set"]) for it in b2["items"]}
    check(sets == {"ftm": (2, "ftm_urgent"), "top1000": (2, "p1")},
          "on day 1 both get text 2, and only the first-to-market owner gets the sale-date copy: a Top 1,000 house has no sale")
    config.MODE = "dry"


# ---------------------------------------------------------------------- the score and the exclusions, piece by piece
def _scoring_checks(check, tmp) -> None:
    from .autopilot import lanes, policy, stages, top1000
    print("  the Top 1,000 score and who is left out")
    build = top1000.scoring({"weights": "today"})
    try:
        full = top1000.house("s1", {"v": 500000, "e": 100.0, "i": 90, "r": 10, "flags": ["is_notice_of_default", "is_vacant_property",
                                                                                          "is_recent_deliquent"]},
                             [], [], "", {})
        build.score(full)
        sc = top1000.owner_score(build, [full])
        check(sc["missing"] == ["cond", "reach"] and round(sum(sc["pts"].values()), 1) == 68.9 and sc["score"] == 98.4,
              "a component the data does not have is left out and the rest are scaled back to 100: 68.9 of 70 reads 98.4")
        with_cond = top1000.house("s2", {"v": 100000, "e": 20.0, "i": 60, "r": 10}, [], [], "", {"s2": 100})
        build.score(with_cond)
        sc2 = top1000.owner_score(build, [with_cond])
        check("cond" in sc2["pts"] and sc2["missing"] == ["reach"] and sc2["score"] == 36.7,
              "a house with a D4D score is scored on condition too (36.7 of 100 for this one)")
        one = top1000.house("s3", {"v": 200000, "e": 25.0, "i": 60, "r": 10}, [], [], "", {})
        two = top1000.house("s4", {"v": 200000, "e": 25.0, "i": 60, "r": 10}, [], [], "", {})
        build.score(one)
        build.score(two)
        check(top1000.owner_score(build, [one, two])["room"] == 100000 and top1000.owner_score(build, [one])["score"] == 33.0
              and top1000.owner_score(build, [one, two])["score"] == 56.8,
              "an owner's deal size counts every house they hold: two $50,000 rooms score 56.8, one alone 33.0")
        check(top1000.reach_of(build, {"owner": {"phones": [mob(5401), mob(5402, "Dial Second")]}}) == (10.0, "Dial First")
              and top1000.reach_of(build, {"owner": {"dnc": True, "phones": [mob(5401)]}})[1] == "DNC, mail only"
              and top1000.reach_of(build, {"owner": {"phones": [dict(mob(5401), status="WRONG")]}})[0] == 0.0,
              "reach is the best dial tier on a number not marked wrong, and an opted-out owner is mail only")
        flagged = top1000.house("s5", {"v": 1, "e": 1, "flags": ["is_probate_properties", "is_eviction"], "abs": True}, [], ["Tax Delinquent"], "", {})
        check(flagged["lists"] == ["Absentee Owners", "Eviction", "Probate", "Tax Delinquent"],
              "SiftMap's distress flags count as Dream 100's list evidence, beside the account's own lists")
    finally:
        top1000.restore(build)
    pol = policy.parse(playbook(tmp), EMPLOYEES)
    st = pol.stage("t_top")
    crm = TrackCRM([])
    ctx = stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-x", now=NOV, live=False, out=stages.Summary(st.id, st.kind, st.owner, st.mode, False))
    ex = lambda tags=(), lists=(), status="", target="top1000", held=("Obituary",): top1000.exclusion(  # noqa: E731
        ctx, "u1", list(tags), list(lists), status, "", target, held_lists=held)
    check(ex(tags=["FTM"])[0] == "lane" and ex(status="Hot Lead")[0] == "lead" and ex(tags=["Do Not Market"])[0] == "do_not_market"
          and ex(tags=["Litigator"])[0] == "litigator" and ex(status="not_interested") == ("finished", "status not_interested")
          and ex(tags=["recently sold"])[0] == "sold",
          "a house is left out for a higher lane, a live lead, Do Not Market, a litigator, a finished status or a sale")
    check({"lane", "lead", "do_not_market", "litigator", "opted_out", "owner_lane", "deceased"} <= top1000.OWNER_LEVEL
          and "finished" not in top1000.OWNER_LEVEL and "sold" not in top1000.OWNER_LEVEL,
          "what is about the person leaves the whole owner out; what is about the house leaves only the house")
    check(ex(tags=["Top 1000"], target="agency") == ("lane", "already in the Top 1,000 lane") and ex(tags=["Top 1000"]) == ("", ""),
          "a Top 1,000 house is in the Top 1,000 and out of the agency track")
    no_list_marker = copy.deepcopy(pol)
    no_list_marker.lanes.markers["obituary"] = {"tags": ["Obituary"]}
    ctx2 = stages.Ctx(policy=no_list_marker, stage=st, crm=crm, run_id="t-y", now=NOV, live=False,
                      out=stages.Summary(st.id, st.kind, st.owner, st.mode, False))
    check(top1000.exclusion(ctx2, "u2", [], ["Obituary"], "", "", "top1000", held_lists=["Obituary"]) == ("lane", "on the Obituary list"),
          "a house on the Obituary list is left out by the stage itself, whatever the lane markers say")
    check(not top1000.may_write(False) and not top1000.may_write(True), "apply writes nothing without TOP1000_ALLOW=1")
    os.environ[top1000.ALLOW_ENV] = "1"
    check(top1000.may_write(True) and not top1000.may_write(False), "and nothing without --commit either; both switches write")
    os.environ.pop(top1000.ALLOW_ENV, None)
    check(top1000.block_problems(type(st)(id="b", kind="top1000", owner="analysis", mode="plan", summary="", schedule=[],
                                          presets=["x"], params={"page_size": 250, "counties": [], "weights": "fast"})) != []
          and top1000.block_problems(st) == [],
          "a block with a page over 100 rows, no county or an unknown weight set is refused when the playbook loads")


# ---------------------------------------------------------------------- the month: refresh, file, release, marking, 90 days
def _cycle_checks(check, tmp) -> None:
    import agency_call_list as acl

    from . import release, store
    from .autopilot import agencyexport, ledger, policy, sweep, top1000, undo
    print("  the Top 1,000 refresh")
    crm = TrackCRM(account())
    fake = FakeMap(crm)
    top1000.search = fake
    pol_plan = policy.parse(playbook(tmp), EMPLOYEES)

    # --- plan: the agency waits for the month's Top 1,000
    w = sweep.run("t_agency", crm=crm, now=NOV, pol=pol_plan, plan=True)
    check("Waiting for this month's Top 1,000" in w.head and not ledger.batches(), "the agency file waits for this month's Top 1,000")

    # --- plan with a small read cap: three runs to finish, nothing re-read
    capped = policy.parse(playbook(tmp, max_reads=2), EMPLOYEES)
    r1 = sweep.run("t_top", crm=crm, now=NOV, pol=capped, plan=True)
    reads1 = crm.reads
    body = fake.bodies[0] if fake.bodies else {}
    f = body.get("filters") or {}
    check(f.get("in_my_account_mode") == "in" and f.get("in_my_account_tags_exclude") == ["tag-estate sold", "tag-recently sold", "tag-sold"]
          and body.get("size") == 100 and body.get("result_index") == 1 and f.get("value_max") == 700000,
          "the sweep asks SiftMap only for houses already in the account, in the buy box, 100 to a page, with the sold tags left out")
    check("2 of 3 read" in r1.head and reads1 == 2 and not crm.writes, "a run stops at its read cap and says how far it got")
    r2 = sweep.run("t_top", crm=crm, now=NOV + timedelta(hours=1), pol=capped, plan=True)
    check("1 still to read" in r2.head and crm.reads - reads1 == 2, "the next run reads only what is left, the finalists first, then the holders")
    searches = fake.calls
    r3 = sweep.run("t_top", crm=crm, now=NOV + timedelta(hours=2), pol=capped, plan=True)
    check(fake.calls == searches and crm.reads - reads1 == 3, "a continuing run re-reads neither the sweep nor a record it already has")
    check(r3.head == "Would put the Top 1000 tag on 2 owners (3 houses), and 2 house(s) from last month keep it "
                     "(1 mid-round, 1 a live lead): 3 house(s) would get it and 1 would lose it. Nothing is written in plan mode."
          and not crm.writes and r3.acted == 4,
          "the plan says who would gain and lose the tag, and who keeps it from last month, and writes nothing")
    t = r3.tally
    check(t.get("left out: already in the first-to-market lane") == 1
          and t.get("left out: owner left out whole: already in the first-to-market lane on another house") == 1
          and t.get("left out: a live lead") == 2 and t.get("left out: owner left out whole: a live lead on another house") == 1
          and t.get("left out: status not_interested") == 1 and t.get("left out: tagged Do Not Market") == 1
          and t.get("left out: upside down (negative equity)") == 1 and t.get("left out: already in the obituary lane") == 1,
          "every house left out is counted under its reason, and an owner a house leaves out whole is said as that")
    check(t.get("left out: no SiftMap row in the buy box (outside the counties, the value or single family, or tagged out)") == 2,
          "a sold house and a house over $700,000 never come back from the sweep")
    state = top1000.month_state("t_top", "2026-11")
    check(state.get("owners") == ["o-a", "o-b"] and state.get("tagged") == ["a1", "a2", "b1", "l1", "r1"],
          "the two owners with the most opportunity win, by owner: both of A's houses get the tag with B's one")
    rows = list(csv.reader(Path(state["summary"]["file"]).open(encoding="utf-8")))
    check(rows[0][:4] == ["Rank", "Score", "Houses", "Best house"] and rows[1][:4] == ["1", "62.2", "2", "1 Track St"]
          and rows[2][:2] == ["2", "45.5"] and ["remove", "k1"] == rows[3][-2:] and rows[4][-2:] == ["kept while its round finishes", "l1"]
          and rows[5][-2:] == ["kept: a live lead, not touched", "r1"],
          "the ranking file lists the owners in order with every component, then the holders who fall out and why")
    check(any("2 of the 2 owners were scored without a condition score" in ln for ln in r3.lines)
          and any("1 holder house(s) fell out of the top 2 and keep the tag while their round finishes" in ln for ln in r3.lines)
          and any("1 holder house(s) fell out because they are live leads now" in ln for ln in r3.lines),
          "the run says how many owners had no condition score, and who keeps the tag while their round finishes")
    check("r1" not in crm.opened, "a holder who is a live lead now is never even opened")
    again = sweep.run("t_top", crm=crm, now=NOV + timedelta(hours=3), pol=capped, plan=True)
    check(again.head.startswith("Nothing to do: this month's Top 1,000 was settled") and fake.calls == searches,
          "a later run that month has nothing to do")

    # --- the agency plan: the file in the agency's own format, nothing staged
    print("  the agency export")
    a = sweep.run("t_agency", crm=crm, now=NOV + timedelta(hours=3), pol=pol_plan, plan=True)
    st_a = top1000.month_state("t_agency", "2026-11")
    csv_path = Path((st_a.get("summary") or {}).get("file") or "nowhere")
    data = list(csv.reader(csv_path.open(encoding="utf-8"))) if csv_path.is_file() else [[]]
    check(fake.calls == searches, "the agency reuses the month's SiftMap sweep: no search is made twice")
    check(a.head == "Would send 3 owners (3 houses) to the calling agency. Nothing is sent or marked in plan mode."
          and not ledger.batches() and not crm.writes, "the plan builds the file and stages nothing")
    check(data[0] == list(acl.COLS) and [r[0] for r in data[1:]] == ["q1", "e2", "m1"]
          and csv_path.with_suffix(".xlsx").is_file(),
          "the file is agency_call_list's own October format, one row per owner in score order, with its workbook")
    check(a.tally.get("left out: in this month's Top 1,000") == 3 and a.tally.get("left out: not sendable: no callable number") == 1
          and a.tally.get("left out: not sendable: entity owner in the CRM") == 1,
          "this month's Top 1,000 owners are kept out, and an owner the agency cannot dial is said and left out")
    unsend = top1000.facts_of(agencyexport.FACT_UNSENDABLE)
    check(set(unsend) == {"n1", "p1"} and unsend["n1"][0]["day"] == "2026-11-11", "an owner who could not be sent is remembered with the day")
    q_row = next((r for r in data[1:] if r and r[0] == "q1"), [])
    check("8655555105" in q_row and "8655555106" not in json.dumps(data)
          and a.tally.get("numbers held out of the file: on the do-not-call registry (D32, mail only)") == 1,
          "a number the account's own search rows show on the do-not-call registry is not in the agency file (decision D32)")
    one = {"phones": [{"number": "8655555106"}, {"number": "8655555105"}]}
    check(agencyexport.registry_hold({"registry_numbers": "send"}, [copy.deepcopy(one)]) == 0
          and agencyexport.registry_hold({"registry_numbers": "hold"}, [one]) == 1 and one["phones"] == [{"number": "8655555105"}],
          "the hold is a setting: send leaves the file as agency_call_list builds it")

    # --- live: the Top 1,000 writes its tag (a cap of two writes a run), then the agency stages its file
    print("  the Top 1,000 written, and put back")
    config.MODE = "live"
    pol_live = policy.parse(playbook(tmp, top_mode="live", agency_mode="live", max_writes=2), EMPLOYEES)
    w1 = sweep.run("t_top", crm=crm, now=NOV + timedelta(hours=4), pol=pol_live)
    check(crm.writes == [("add_tags", "a1", ("Top 1000",)), ("add_tags", "a2", ("Top 1000",))] and "2 of 4 tag change(s) done" in w1.head,
          "live, at most max_writes changes a run; the rest wait for the next run")
    w2 = sweep.run("t_top", crm=crm, now=NOV + timedelta(hours=5), pol=pol_live)
    tags = {u: "Top 1000" in crm.recs[u]["tags"] for u in ("a1", "a2", "b1", "k1", "l1", "r1")}
    check(tags == {"a1": True, "a2": True, "b1": True, "k1": False, "l1": True, "r1": True} and "got it and 1 lost it" in w2.head,
          "the next run finishes: three houses get the tag, the holder who fell out loses it, the one mid-round keeps it")
    check(not [w for w in crm.writes if len(w) > 1 and w[1] == "r1"] and "r1" not in crm.opened,
          "the live lead keeps the tag it came with: nothing reads or writes its record")
    runs = {e["run_id"] for e in ledger.events(action="top1000_add")} | {e["run_id"] for e in ledger.events(action="top1000_remove")}
    steps = [s for r in sorted(runs) for s in undo.plan(r)]
    check(len(ledger.changes()) == 4 and sorted(s["do"] for s in steps) == ["add_tags", "remove_tags", "remove_tags", "remove_tags"],
          "every tag change is recorded with what it replaced, so undo puts the tag back where it was")
    a2 = sweep.run("t_agency", crm=crm, now=NOV + timedelta(hours=6), pol=pol_live)
    b = ledger.batch(a2.batch) if a2.batch else None
    check(bool(b) and b["kind"] == "agency" and b["status"] == "staged" and [it["record_uuid"] for it in b["items"]] == ["q1", "e2", "m1"]
          and a2.tally.get("left out: not sendable last time (no callable number), not tried again for 90 days") == 1,
          "live, the file is staged as an agency batch for a person; an owner not sendable last time is not read again")
    note = agencyexport.release_note(b) if b else ""
    check(".csv" in note and "`confirm %d uploaded`" % b["id"] in note and "`confirm %d` first" % b["id"] in note,
          "release batch N shows where the file is and the two confirms")

    # --- release: dry, then confirm (a record that became a lead comes out of the file), then uploaded
    print("  the agency release")
    bid = b["id"]
    dry = release.run(bid, commit=False, crm=crm, pol=pol_live, now=NOV + timedelta(hours=7))
    check(dry.get("dry") and dry.get("owners") == 3 and dry.get("records") == 3 and not crm.writes[4:],
          "a dry release reports and writes nothing")
    crm.recs["e2"]["status"] = "Hot Lead"
    crm.log_rows = [status_ev("tk1", "e2", NOV + timedelta(hours=6, minutes=30), None, "Hot Lead")]
    c1 = release.run(bid, commit=True, crm=crm, pol=pol_live, now=NOV + timedelta(hours=8))
    info = agencyexport.info_of(bid)
    new_rows = list(csv.reader((Path(info["files"]) / info["csv"]).open(encoding="utf-8")))
    check(c1.get("held") == {"already a lead (hot lead)": 1} and [r[0] for r in new_rows[1:]] == ["q1", "m1"]
          and [it["record_uuid"] for it in ledger.batch(bid)["items"]] == ["q1", "m1"] and ledger.batch(bid)["status"] == "staged",
          "confirm checks what changed since staging: the owner who became a lead comes out and the file is rewritten")
    # The owner of 18 Track St becomes a lead after the confirm and before the upload: the file has gone
    # with them in it, so the upload says who to take off the agency's list and still marks them sent.
    crm.recs["m1"]["status"] = "Warm Lead"
    crm.log_rows = [status_ev("tk2", "m1", NOV + timedelta(hours=8, minutes=30), None, "Warm Lead")]
    up = release.run(bid, commit=True, uploaded=True, crm=crm, pol=pol_live, now=NOV + timedelta(hours=9))
    check(any("tell the agency to skip them: 18 Track St (already a lead (warm lead))" in p for p in up.get("problems") or []),
          "the upload checks again what changed since the confirm and names the owner the agency should skip")
    crm.recs["m1"]["status"] = None
    crm.log_rows = []
    marks = [w for w in crm.writes if w[0] in ("add_lists", "field")]
    check(up.get("sent") == 2 and up.get("results") == {"records marked now": 1, "left for the stage's night runs": 1}
          and marks == [("add_lists", "q1", "Agency"), ("field", "q1", "f-sent", "2026-11-11")] and ledger.batch(bid)["status"] == "sent",
          "uploaded: the first record goes on the Agency list with its date, is read back, and the rest wait for the night runs")
    check(ledger.fact("q1", agencyexport.FACT_SENT) == ledger.fact("m1", agencyexport.FACT_SENT) == "2026-11-11"
          and ledger.fact("e2", agencyexport.FACT_SENT) is None,
          "every record in the sent file is remembered as sent that day at once, whatever the marks do")
    d = sweep.run("t_agency", crm=crm, now=NOV + timedelta(hours=10), pol=pol_live)
    check([w for w in crm.writes if w[0] in ("add_lists", "field")][2:] == [("add_lists", "m1", "Agency"), ("field", "m1", "f-sent", "2026-11-11")]
          and "Marked 1 record(s)" in d.head and not json.loads(store.kv_get(agencyexport.QUEUE_KEY) or "[]"),
          "the stage's night run marks the rest of the file under the release's run id")
    ch = ledger.changes(run_id="agency-%d" % bid)
    steps = undo.plan("agency-%d" % bid)
    check(sorted(c["what"] for c in ch) == ["add_lists", "add_lists", "custom_field", "custom_field"]
          and sum(1 for s in steps if s["do"] == "custom_field" and s["args"]["value"] == "") == 2,
          "every mark is recorded with what it replaced: undo clears the date field it set")

    # --- December: nobody sent in the last 90 days is sent again; February: they are due again
    print("  ninety days")
    sweep.run("t_top", crm=crm, now=DEC, pol=pol_live)
    dec = sweep.run("t_agency", crm=crm, now=DEC + timedelta(hours=1), pol=pol_live)
    check(dec.head == "No owner in the agency track is due to be sent this month."
          and dec.tally.get("left out: sent to the agency in the last 90 days") == 2,
          "a month later the owners in November's file are not sent again: the 90-day rule is the autopilot's own memory")
    stale = ledger.batch_create("t_agency", "agency", [{"record_uuid": "zz", "owner_uuid": "o-zz", "records": ["zz"]}], 0.0)
    sweep.run("t_top", crm=crm, now=FEB, pol=pol_live)
    feb = sweep.run("t_agency", crm=crm, now=FEB + timedelta(hours=1), pol=pol_live)
    fb = ledger.batch(feb.batch) if feb.batch else {"items": []}
    check([it["record_uuid"] for it in fb["items"]] == ["q1", "m1"] and ledger.batch(stale)["status"] == "expired",
          "after 90 days they are due again, and a file nobody sent is closed when the next is staged")
    check(math.isclose(len(fb["items"]), 2) and feb.tally.get("left out: not sendable: no callable number") == 1,
          "an owner the agency could not dial is tried again after 90 days, and said again when still undialable")
    config.MODE = "dry"

    # --- a write that does not stick stops the run (a fresh data folder: the month above is settled on disk)
    print("  a write that does not stick")
    _wipe()
    config.DATA_DIR = tmp / "two_tracks_broken"
    crm_b = TrackCRM(account())
    crm_b.broken_tags = True
    top1000.search = FakeMap(crm_b)
    config.MODE = "live"
    bad = sweep.run("t_top", crm=crm_b, now=NOV, pol=policy.parse(playbook(tmp, top_mode="live"), EMPLOYEES))
    nxt = sweep.run("t_top", crm=crm_b, now=NOV + timedelta(hours=1), pol=policy.parse(playbook(tmp, top_mode="live"), EMPLOYEES))
    check(any("the tag did not stick on 1 Track St" in p for p in bad.problems) and len(crm_b.writes) == 1
          and any("nothing more is written until a person looks" in p for p in nxt.problems) and len(crm_b.writes) == 1,
          "a tag that does not read back stops the run after the first write, and the next run refuses until a person looks")
    config.MODE = "dry"

    # --- a record that is gone (404 on the tag write) is skipped and remembered, never a stop
    print("  a record that is gone")
    _wipe()
    config.DATA_DIR = tmp / "two_tracks_gone"
    crm_g = TrackCRM(account())
    crm_g.gone_first = True
    top1000.search = FakeMap(crm_g)
    config.MODE = "live"
    g1 = sweep.run("t_top", crm=crm_g, now=NOV, pol=policy.parse(playbook(tmp, top_mode="live"), EMPLOYEES))
    tries1 = list(crm_g.tag_tries)
    g2 = sweep.run("t_top", crm=crm_g, now=NOV + timedelta(hours=1), pol=policy.parse(playbook(tmp, top_mode="live"), EMPLOYEES))
    check(not any("stopped writing early" in p for p in g1.problems) and len(tries1) > 1
          and g1.tally.get("left out: the record is gone (404 when tagged)") == 1,
          "a record that answers 404 while being tagged is counted as gone and the run carries on with the rest")
    check(crm_g.tag_tries.count(crm_g.gone_seen) == 1 and not any("stopped writing early" in p for p in g2.problems),
          "a gone record is never tried again on a later run")
    config.MODE = "dry"

    # --- the sweep splits a value band that is too big, and says when the in-account filter did not hold
    print("  the sweep")
    _wipe()
    config.DATA_DIR = tmp / "two_tracks_sweep"
    crm_s = TrackCRM(account())
    top1000.search = FakeMap(crm_s, outside=10)
    top1000.SPLIT_AT = 6
    st = pol_plan.stage("t_top")
    from .autopilot import stages
    ctx = stages.Ctx(policy=pol_plan, stage=st, crm=crm_s, run_id="t-s", now=datetime(2026, 6, 2, 1, 0, tzinfo=ET), live=False,
                     out=stages.Summary(st.id, st.kind, st.owner, st.mode, False))
    got = top1000.month_sweep(ctx, top1000.options(st))
    top1000.SPLIT_AT = 9000
    check(got is not None and len(got) == 20 and "n1" in got and "g1" not in got and "j1" not in got,
          "a band past the split is read in halves until each fits, and every house in the buy box comes back once")
    check(any("not in the account" in p for p in ctx.out.problems), "rows that are not in the account are dropped and said")
