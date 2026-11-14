"""Research lane selftest: the ranked research queue and its mark, the heir lane, the obituary
pipeline, the probate change list and the paid lookup queue. Zero network: the same fake DataSift
as the autopilot selftest, extended with the board, task and list routes this lane uses. Every
name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import ADRIANA, EMPLOYEES, ET, PRESETS, T0, FakeCRM, _rec

DANA = {"uuid": "u-dana", "first_name": "Dana", "is_active": True}
COL = "aaaaaaaa-0000-4000-8000-000000000001"       # the research column
DONE = "aaaaaaaa-0000-4000-8000-000000000002"      # where a finished card goes

# kind -> the registry line each one needs (module, run function, spend class, needs a preset, always reports)
KINDS = {
    "researchqueue": ("researchqueue", "run_researchqueue", "paid", False, False),
    "heirlane": ("heirlane", "run_heirlane", "outreach", False, False),
    "obituary": ("obituary", "run_obituary", "outreach", True, False),
    "probate": ("probate", "run_probate", "free", False, False),
    "paidlookup": ("paidlookup", "run_paidlookup", "free", True, False),
}

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0, "outreach_daily_usd": 60},
    "people": {"callers": ["Adriana"]},
    "gate": {"read_fields": True},
    "stages": {
        "research_queue": {"kind": "researchqueue", "owner": "research", "mode": "live",
                           "schedule": "every 30 min 07:00-18:00 Mon-Fri", "presets": ["RQ - Feed"],
                           "card_column": {"title": "Check Relatives", "uuid": COL}, "card_done_column": DONE,
                           "statuses": [], "max_open": 1000},
        "heir_lane": {"kind": "heirlane", "owner": "ops", "mode": "live", "schedule": "08:20 Mon-Fri",
                      "gate_allow": ["heir", "in_research"], "heir_caller": "Dana"},
        "obituary_lane": {"kind": "obituary", "owner": "research", "mode": "live", "schedule": "09:30 Mon-Fri",
                          "presets": ["OB - All"], "gate_allow": ["deceased"], "call_presets": ["OB - Call"]},
        "probate_honest": {"kind": "probate", "owner": "analysis", "mode": "live", "schedule": "Mon 07:40"},
        "paid_lookups": {"kind": "paidlookup", "owner": "ops", "mode": "live", "schedule": "08:30 Mon-Fri",
                         "presets": ["PL - No Number"], "assistant": "Dana"},
    },
}


class NotFound(Exception):
    code = 404


class RCRM(FakeCRM):
    """The fake DataSift with the documented board, task and list routes."""

    def __init__(self, records, users=(ADRIANA, DANA)):
        super().__init__(records, users)
        self.cards: dict[str, dict] = {}
        self.columns = {COL: "Check Relatives", DONE: "Researched"}
        self.tasks: list[dict] = []
        self.log: list[tuple] = []
        self.reads = 0
        self.card_n = 0

    def record(self, u):
        self.reads += 1
        return super().record(u)

    def _match(self, r, must):
        if "all_lists" in must:
            have = {t.lower() for t in r.get("lists", [])}
            if not {x.replace("list-", "") for x in must["all_lists"]} <= have:
                return False
        return super()._match(r, must)

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
            return {"results": [{"uuid": "board-dp", "title": "Deep Prospecting"}, {"uuid": "board-lm", "title": "Lead Management"}]}
        m = re.fullmatch(r"/api/internal/siftline/board/column/([^/]+)/card/", p)
        if m and method == "POST":
            self.card_n += 1
            cid = "card-%d" % self.card_n
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
        if p == "/api/internal/task/" and method == "POST":
            tid = "task-%d" % (len(self.tasks) + 1)
            self.tasks.append(dict(body, uuid=tid, completed=None))
            return {"uuid": tid}
        m = re.fullmatch(r"/api/internal/task/([^/]+)/", p)
        if m:
            t = next((t for t in self.tasks if t["uuid"] == m.group(1)), None)
            if t is None:
                raise NotFound("no such task")
            return dict(t)
        m = re.fullmatch(r"/api/internal/property/([^/]+)/(add|remove)-lists/", p)
        if m:
            rec = self.recs[m.group(1)]
            rec.setdefault("lists", [])
            if m.group(2) == "add" and body["lists"] not in rec["lists"]:
                rec["lists"].append(body["lists"])
            if m.group(2) == "remove":
                rec["lists"] = [x for x in rec["lists"] if x != body["lists"]]
            return {}
        return super().call(path, method, body, headers)

    def changed(self) -> list:
        """Every write that reached the fake account: the action routes and the raw non-GET calls."""
        return list(self.writes) + [x for x in self.log if x[0] != "GET"]


def R(u, street, **kw):
    """A record: no tags unless given, an owner DataSift has already traced."""
    kw.setdefault("tags", [])
    r = _rec(u, street, **kw)
    r.setdefault("lists", [])
    r["owner"].setdefault("skiptraced", True)
    return r


def _day(n: int) -> str:
    return (T0 + timedelta(days=n)).astimezone(ET).date().isoformat()


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _dial(i, owner, when, number, kind="made", duration=40):
    return {"id": "rs-call-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": number, "origin_number": "8655550000", "direction": "outbound",
                                 "duration": duration, "source": "kixie", "owner": owner, "uuid": "rc-%s" % i, "extra": {},
                                 "user": {"first_name": "Dana", "uuid": "u-dana"}}}}


def _wipe(store) -> None:
    """An empty autopilot memory, so a record id or a day's spend from an earlier check cannot leak in."""
    from .autopilot import limits
    limits.ensure()                     # the house text ledger, created on first use
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones", "ap_texts"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _stock_release_refuses(batch_id: int, crm, pol) -> bool:
    """Does the ordinary mail release stop on this batch? The release command sends every batch that
    is not a counter correction to mail.release, which would book an heir letter as a piece to the
    owner and a text or a probate change as mail. It stops today because these items are not mail
    pieces. If that ever changes, this check fails before a person can release one the wrong way."""
    from .autopilot import mail
    try:
        mail.release(batch_id, crm=crm, pol=pol, commit=True, uploaded=True, now=T0)
    except (Exception, SystemExit):  # noqa: BLE001  (any refusal counts; what was written is checked by the caller)
        return True
    return False


def _register(policy, registry) -> dict:
    """Make the five kinds known for this section when their registry lines are not in yet. What is
    already registered is left exactly as it is."""
    saved = {"extra": dict(registry.EXTRA), "kinds": policy.KINDS, "spend": dict(policy.SPEND_BY_KIND),
             "no_presets": policy.NO_PRESETS}
    for kind, row in KINDS.items():
        registry.EXTRA.setdefault(kind, row)
    policy.KINDS = tuple(dict.fromkeys(tuple(policy.KINDS) + tuple(KINDS)))
    policy.SPEND_BY_KIND.update({k: registry.EXTRA[k][2] for k in KINDS})
    policy.NO_PRESETS = tuple(dict.fromkeys(tuple(policy.NO_PRESETS) + tuple(k for k in KINDS if not registry.EXTRA[k][3])))
    return saved


def _unregister(policy, registry, saved: dict) -> None:
    registry.EXTRA.clear()
    registry.EXTRA.update(saved["extra"])
    policy.KINDS, policy.NO_PRESETS = saved["kinds"], saved["no_presets"]
    policy.SPEND_BY_KIND.clear()
    policy.SPEND_BY_KIND.update(saved["spend"])


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry
    keep = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.MODE)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.RESEARCH_DAILY_CAP_USD = 100000.0
    config.MODE = "live"
    store.init()
    saved_presets = dict(PRESETS)
    saved_kinds = _register(policy, registry)
    PRESETS.update({
        "RQ - Feed": lambda r: "RQ" in r["tags"],
        "OB - All": lambda r: "OB" in r["tags"],
        "OB - Call": lambda r: "OB" in r["tags"] and bool(r["owner"]["phones"]),
        "PL - No Number": lambda r: "PL" in r["tags"],
        "Paid Lookup Queue": lambda r: "Paid Lookup" in r["tags"],
    })
    try:
        _run(check, tmp, store, policy, registry)
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        _unregister(policy, registry, saved_kinds)
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.MODE = keep
        config.MODE = "dry"


def _run(check, tmp, store, policy, registry) -> None:
    from .autopilot import (activity, gate, heirlane, ledger, obituary, paidlookup, probate, researchqueue,
                            researchroutes, sweep, undo)
    rq = researchqueue
    _wipe(store)
    activity.reset()
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    today = T0.astimezone(ET).date()
    utc_day = datetime.now(timezone.utc).isoformat()[:10]
    handed: list[tuple] = []

    def enq(agent, wf, u, subject, **kw):
        handed.append((agent, wf, u, kw.get("source")))
        return (900000 + len(handed), True)         # an id no real task carries

    def with_stage(stage: str, **params):
        p = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
        p.stage(stage).params.update(params)
        return p

    # ------------------------------------------------------------------ registry
    print("  registry")
    check(registry.problems() == [], "the five stage kinds load from their registry lines: %s" % (registry.problems() or "ok"))
    check(pol.stage("research_queue").spend == "paid" and pol.stage("heir_lane").spend == "outreach"
          and pol.stage("obituary_lane").spend == "outreach" and pol.stage("probate_honest").spend == "free"
          and pol.stage("paid_lookups").spend == "free",
          "research handoffs are paid, heir and obituary mail is outreach, the probate list and the lookup queue are free")

    # ------------------------------------------------------------------ routes
    print("  routes")
    crm = RCRM([R("rr1", "1 Route St")])
    card = researchroutes.create_card(crm, COL, "rr1")
    check(crm.log[-1] == ("POST", "/api/internal/siftline/board/column/%s/card/" % COL, {"prop": "rr1"}) and card["uuid"],
          "a card is created in a column with the record as its prop, the documented body")
    researchroutes.move_card(crm, COL, card["uuid"], DONE)
    check(crm.cards[card["uuid"]]["column"] == DONE and crm.log[-1][0] == "PATCH" and crm.log[-1][2] == {"column": DONE},
          "a card is moved by naming the column it goes to")
    check(researchroutes.column_title(researchroutes.card_timeline(crm, DONE, card["uuid"]), DONE) == "Researched",
          "a card's timeline gives every column of its board by title, which is how a column id is read back")
    crm.cards["card-x"] = {"uuid": "card-x", "prop_uuid": "rr9", "column": COL}
    idx = researchroutes.cards_in_column(crm, COL)
    check(set(idx) == {"rr9"} and idx["rr9"]["uuid"] == "card-x" and researchroutes.card_property({"prop": {"uuid": "z"}}) == "z",
          "the cards of a column are read once and looked up by record, whichever key the listing uses")
    check(researchroutes.board_uuid(crm, " deep prospecting ") == "board-dp" and researchroutes.board_uuid(crm, "Nope") == "",
          "a board is found by its title, in any case")
    u1, t1, p1 = researchroutes.column_ref({"title": "Check Relatives", "uuid": COL}, "x")
    u2, t2, p2 = researchroutes.column_ref("Check Relatives")
    u3, _, p3 = researchroutes.column_ref({"title": "Check Relatives", "uuid": "not-a-uuid"})
    check(u1 == COL and not p1 and u2 == "" and "no route that lists" in p2 and u3 == "" and "not a column uuid" in p3,
          "a column is named by its id: a bare title cannot be resolved on documented routes, and says so")
    body = researchroutes.task_body("Call  again", property_uuid="rr1", user_uuid="u-dana", due="2026-11-12T04:59:59.999Z", notes="n")
    check(body["assigned_to_users"] == ["u-dana"] and "assigned_to_role" not in body and body["assigned_to_property"] == "rr1"
          and body["title"] == "Call again", "a task names one user, and a key with no value is left out")
    refused = 0
    for kw in ({}, {"user_uuid": "u", "role": "prospector"}):
        try:
            researchroutes.task_body("x", **kw)
        except ValueError:
            refused += 1
    check(refused == 2, "a task with no assignee, or with two, is refused before it is sent")
    made = researchroutes.create_task(crm, "Weekly task", user_uuid="u-dana")
    check(researchroutes.task_open(crm, made["uuid"]) is True and researchroutes.task_open(crm, "task-gone") is None,
          "a task reads as open until it is completed, and a deleted one reads as gone")
    crm.tasks[-1]["completed"] = "2026-11-12T15:00:00Z"
    check(researchroutes.task_open(crm, made["uuid"]) is False, "a completed task reads as done")
    researchroutes.add_list(crm, "rr1", "Probate")
    researchroutes.remove_list(crm, "rr1", "Probate")
    check(crm.log[-2][2] == {"lists": "Probate"} and crm.log[-1][2] == {"lists": "Probate"} and crm.recs["rr1"]["lists"] == [],
          "a list change sends one title as a string")
    eod = researchroutes.end_of_day(T0, "America/New_York")
    check(eod == "2026-11-12T04:59:59.999Z", "a due date is the last moment of the business day, in UTC")
    check(researchroutes.last4("(865) 555-0142") == "0142" and researchroutes.last4("12") == "", "a number is shown by its last four digits")
    ledger.set_fact("rr1", researchroutes.CARD_FACT, {"card": "card-led", "column": COL})
    check(researchroutes.find_card("rr1", "Deep Prospecting")["source"] == "ledger", "a record's card is looked for in the ledger first")
    activity._store([{"id": "rs-card-1", "event_type": "card.created", "resource": "card-log", "resource_type": "card",
                      "timestamp": _ts(T0 - timedelta(days=1)),
                      "payload": {"card": {"prop": "rr2"}, "extra_info": {"card": {"prop": "rr2", "column": [None, "Check Relatives"],
                                                                                    "board": [None, "Deep Prospecting"]}}}},
                     {"id": "rs-card-2", "event_type": "card.created", "resource": "card-lead", "resource_type": "card",
                      "timestamp": _ts(T0 - timedelta(days=1)),
                      "payload": {"card": {"prop": "rr3"}, "extra_info": {"card": {"prop": "rr3", "column": [None, "New Leads"],
                                                                                    "board": [None, "Lead Management"]}}}}])
    check(researchroutes.find_card("rr2", "Deep Prospecting")["card"] == "card-log"
          and researchroutes.find_card("rr3", "Deep Prospecting") is None,
          "a card the account's own sequence made is found in the log copy, and a card on another board is not taken for it")
    check(researchroutes.find_card("rr9", "Deep Prospecting", "", idx)["source"] == "column"
          and researchroutes.find_card("rr8", "Deep Prospecting", "", idx) is None,
          "then the column listing is asked, and a record with no card anywhere has none")

    # ------------------------------------------------------------------ the rank
    print("  rank")
    base = {"owner_kind": "person", "lane": "ftm", "sales": [], "held": "", "dead": [], "distress": []}

    def rk(**extra):
        return rq.rank(dict(base, **extra), today)

    order = [rk(asked=True), rk(sales=[_day(9)]), rk(probate=True, tax_owed=2140), rk(condemned=True, distress=["condemned"]),
             rk(obituary=True, obit_date=_day(-240), lane="obituary", distress=["vacant"]),
             rk(obituary=True, obit_date=_day(-120), lane="obituary", distress=["tax delinquent"]),
             rk(exhausted=True), rk(returned_mail=True), rk(vacant_mailing=True), rk(no_number=True),
             rk(owner_kind="trust"), rk(obituary=True, obit_date=_day(-100), lane="obituary"),
             rk(dead=["tag verify deceased"]), rk(via=["Some Preset"])]
    names = [r.cls for r in order]
    check(names == ["asked", "hot_auction", "probate", "condemned", "obituary_no_probate", "obituary_second_signal",
                    "exhausted", "returned_mail", "vacant_mailing", "no_number", "trust", "obituary_due", "death_linked",
                    "other"] and all(a.score > b.score for a, b in zip(order, order[1:])),
          "the rank runs sale ahead, probate, condemned, obituary with no probate, obituary with a second signal, "
          "no answer anywhere, returned mail, vacant mailing address, no number, trust, then the rest")
    check(rk(sales=[_day(9)]).score > rk(sales=[_day(30)]).score and rk(sales=[_day(9)]).reason.startswith("sale in 9 days"),
          "a sooner sale ranks ahead of a later one, and the reason says how many days are left")
    check(rk(sales=[_day(-40), _day(12)]).cls == "hot_auction", "a postponed sale counts by its new date")
    check(rk(probate=True, tax_owed=2140).score > rk(probate=True, tax_owed=300).score > rk(probate=True).score
          and "$2,140" in rk(probate=True, tax_owed=2140).reason, "probates rank by the county tax they owe")
    np_ = rk(obituary=True, obit_date=_day(-240), lane="obituary", distress=["vacant", "behind on taxes", "foreclosure"])
    check("no probate filed" in np_.reason and all(w in np_.reason for w in ("vacant", "behind on taxes", "foreclosure"))
          and np_.score > rk(obituary=True, obit_date=_day(-240), lane="obituary").score,
          "an obituary with no probate after six months is checked for a foreclosure, a vacancy and back taxes")
    young = rk(obituary=True, obit_date=_day(-20), lane="obituary", distress=["vacant"])
    check(not young.ok and "research opens at 90 days" in young.reason and rk(obituary=True, obit_date=_day(-20), sales=[_day(9)]).ok,
          "an obituary inside its first three months is not queued, unless a sale date is ahead")
    check(not rk(obituary=True, lane="obituary").ok, "an obituary with no date on the record waits until the date is known")
    parked = rk(dead=["owner flag"], lane="agency")
    check(not parked.ok and parked.cls == "parked" and "agency track" in parked.reason,
          "a dead owner in the agency track with nothing else going on is parked, not researched")
    check(rk(dead=["owner flag"], lane="agency", investor_score=91).ok and rk(dead=["owner flag"], lane="agency", equity_pct=82).ok
          and rk(dead=["owner flag"], lane="agency", distress=["tax delinquent"]).ok
          and not rk(dead=["owner flag"], lane="agency", investor_score=40, equity_pct=20).ok,
          "the investor score, the equity or a second signal ranks a parked record in")
    check(rk(dead=["Obituary list"], obituary=True, obit_date=_day(-100), lane="obituary").ok
          and rk(dead=["owner flag"], lane="ftm").ok and rk(dead=["owner flag"], lane="top1000").ok,
          "the parking rule is for the agency track only: the Top 1,000 is worked in-house")
    lead = rk(lead=True, sales=[_day(9)])
    check(not lead.ok and "lead manager" in lead.reason and rq.rank(dict(base, lead=True, sales=[_day(9)]), today,
                                                                   dict(rq.options(None), leads="research")).ok,
          "a lead is left to the lead manager unless the playbook says research may run beside one")
    check(not rk(owner_kind="company").ok and not rk(owner_kind="unnamed").ok and rk(owner_kind="trust").ok,
          "a company needs the entity path and is not queued; a trust is, last")
    check(not rk(sales=[_day(-2)]).ok and not rk(sales=[today.isoformat()]).ok and "sale date passed" in rk(sales=[_day(-2)]).reason,
          "a sale date that has passed, today included, takes the record out of the queue")
    check(not rk(held="sold since the notice or death").ok and not rk(held="listed or pending on the MLS").ok
          and not rk(held="no sale date on record").ok, "a house that sold, is listed, or is a foreclosure with no sale date is not queued")
    check(not rk(skip_tag="Deep Prospected").ok, "a record research already finished is not queued again")
    check(rq.rank(dict(base, via=["04 Return"]), today, dict(rq.options(None), hints={"04 Return": "returned_mail"})).cls == "returned_mail",
          "a feeding preset can say what the record itself cannot, such as mail that came back")
    check(rk(no_number=True, lane="top1000").reason == "a Top 1,000 owner with no number after every trace"
          and rk(no_number=True, lane="ftm").score > rk(no_number=True, lane="top1000").score,
          "a first-to-market record with no number goes ahead of a Top 1,000 record with none")
    check(all(re.search(r"[a-z]", r.reason) and "_" not in r.reason for r in order + [parked, lead, young]),
          "every rank comes with its reason in plain words")
    check(list(inspect.signature(rq.rank).parameters) == ["sig", "today", "o"]
          and "crm" not in inspect.signature(rq.signals_of).parameters and "ctx" not in inspect.signature(rq.signals_of).parameters,
          "the rank and its inputs take a record's own values: nothing they are given can make a request or a paid call")

    # ------------------------------------------------------------------ what the rank reads off a record
    print("  signals")
    dpb = rq.batch_rules()
    check(dpb is not None and callable(dpb.run_gate), "the backlog runner's own rules load, with no network at import")
    opts = rq.options(None)

    def sig_of(rec, custom=None, **kw):
        return rq.signals_of(rec, custom or {}, opts, today=today, gate_cfg=pol.gate, lanes_cfg=pol.lanes, dpb=dpb, **kw)

    fc = R("s1", "1 Signal St", tags=["FTM", "Courthouse Data"], lists=["Foreclosure"])
    s = sig_of(fc, {"Foreclosure Date": _day(9)})
    check(s["sales"] == [_day(9)] and s["held"] == "" and rq.rank(s, today).cls == "hot_auction",
          "the sale date is read from the Foreclosure Date field, where the uploader has written it since August")
    check(sig_of(fc, {})["held"] == "no sale date on record" and sig_of(fc, {"Foreclosure Date": _day(-3)})["held"] == "auction date passed",
          "the backlog runner's own check runs on that date: no date holds a foreclosure out, a passed date does too")
    check(sig_of(dict(fc, notes="Auction: 1/5/2027"), {})["sales"] == ["2027-01-05"]
          and sig_of(fc, {}, known_sales={"Foreclosure Date": _day(15)})["sales"] == [_day(15)],
          "a sale date in the notes, or one the auction stage already read, counts as well")
    pr = R("s2", "2 Signal St", lists=["Probate"], tax_delinquent_value="0")
    ps = sig_of(pr, {"Tax delinquency amount": "$2,140.00", "Total Delinquency": "99,999.00"})
    check(ps["probate"] and ps["tax_owed"] == 2140.0 and sig_of(R("s3", "3 Signal St", lists=["Probate"], tax_delinquent_value="858.00"))["tax_owed"] == 858.0,
          "tax owed is the tax field, or the record's own delinquent value, never the total that also holds liens")
    ob = R("s4", "4 Signal St", lists=["Obituary", "Tax Delinquent"], last_obituary_date=_day(-120) + "T00:00:00Z")
    obs = sig_of(ob)
    check(obs["obituary"] and obs["obit_date"] == _day(-120) and obs["distress"] == ["tax delinquent"] and obs["dead"] == ["Obituary list"]
          and not obs["dead_hard"], "an obituary record carries its date, its second signals, and a death that is a list, not a fact")
    check(sig_of(R("s5", "5 Signal St", phones=[{"number": "8655550301", "status": "WRONG", "tags": []}]))["exhausted"]
          and sig_of(R("s6", "6 Signal St", status="Exhausted"))["exhausted"]
          and sig_of(R("s7", "7 Signal St", predictivecall_attempts=5, directmail_attempts=12))["channels_done"],
          "no answer anywhere is every number marked bad, the Exhausted status, or the call round and the mail both finished")
    vac = R("s8", "8 Signal St")
    vac["owner"]["address"]["vacant"] = True
    check(sig_of(vac)["vacant_mailing"] and sig_of(R("s9", "9 Signal St", status="Return Mail"))["returned_mail"]
          and sig_of(R("s10", "10 Signal St", tags=["Return Mail"]))["returned_mail"],
          "a vacant mailing address and returned mail are read off the record")
    never = R("s11", "11 Signal St")
    never["owner"]["skiptraced"] = False
    never["skiptraced"] = False
    check(sig_of(R("s12", "12 Signal St"))["no_number"] and not sig_of(never)["no_number"],
          "no number means traced with nothing found; a record nobody has traced yet belongs to the trace stages first")
    check(rq.owner_kind({"first_name": "", "last_name": "", "company": "Example Family Trust"}) == "trust"
          and rq.owner_kind({"first_name": "", "last_name": "", "company": "Acme Holdings LLC"}) == "company"
          and rq.owner_kind({"first_name": "Pat", "last_name": "Example"}) == "person" and rq.owner_kind({}) == "unnamed",
          "an owner is a person, a trust, a company or unnamed")
    sold = R("s13", "13 Signal St", tags=["2026-08"], last_sold="2026-10-01")
    check(sig_of(sold)["held"] == "sold since the notice or death" and sig_of(R("s14", "14 Signal St", mls="Pending"))["held"].startswith("listed")
          and sig_of(R("s15", "15 Signal St", mls="Sold"))["held"] == "",
          "a sale since the notice and a listing hold a record out; an MLS value of Sold alone does not")
    check(sig_of(R("s16", "16 Signal St", tags=["Deep Prospected"]))["skip_tag"] == "Deep Prospected"
          and sig_of(R("s17", "17 Signal St", first="Estate of Pat", last="Example"))["dead"] == ["owner name"],
          "a finished record carries its mark, and an owner name that says estate counts as a possible death")
    bulk = R("s18", "18 Signal St", tags=["Priority 1"], auction_date=_day(20))
    court = R("s19", "19 Signal St", tags=["FTM", "Courthouse Data"], lists=["Tax Sale"], tax_auction_date=_day(20))
    stale = R("s20", "20 Signal St", lists=["Obituary"], last_obituary_date=_day(-120), auction_date=_day(-200))
    check(sig_of(bulk)["sales"] == [] and sig_of(court)["sales"] == [_day(20)] and sig_of(stale)["held"] == ""
          and rq.rank(sig_of(stale), today).ok,
          "a record's own auction fields are believed on courthouse records only: a bulk row's phantom date neither jumps the queue nor drops an obituary")
    bought = R("s21", "21 Signal St", tags=["Priority 1"], last_sold=_day(-200), mls="Pending")
    after_death = R("s22", "22 Signal St", lists=["Obituary"], last_obituary_date=_day(-300), last_sold=_day(-100))
    check(sig_of(dict(bought, mls="Off Market"))["held"] == "" and sig_of(bought)["held"].startswith("listed")
          and sig_of(after_death)["held"] == "sold since the notice or death",
          "a last sale counts as a sale only when there is a notice or a death for it to be since; the rest of the house check still runs")
    knox = R("s23", "23 Signal St", lists=["Obituary"], last_obituary_date=_day(-120))
    knox["address"]["county"] = "Knox"
    blount = copy.deepcopy(knox)
    blount["uuid"], blount["address"]["county"] = "s24", "Blount"
    check(rq.rank(sig_of(knox), today) == rq.rank(sig_of(blount), today),
          "a Blount record ranks on the same marks as a Knox one: nothing in the rank asks an obituary index")

    # ------------------------------------------------------------------ the queue
    print("  queue")
    base_open = rq.open_tasks()
    base_spent = store.spent(day=utc_day, research=True)

    def feed():
        hot = R("q_hot", "101 Queue St", tags=["RQ", "FTM", "Courthouse Data"], lists=["Foreclosure"],
                phones=[{"number": "8655550311", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}])
        hot2 = R("q_hot2", "102 Queue St", tags=["RQ", "FTM", "Courthouse Data"], lists=["Foreclosure"])
        vac_ = R("q_vac", "110 Queue St", tags=["RQ", "FTM"], phones=[{"number": "8655550312", "status": "UNKNOWN", "tags": ["Dial First"]}])
        vac_["owner"]["address"]["vacant"] = True
        recs = [
            hot, hot2,
            R("q_prob", "103 Queue St", tags=["RQ", "FTM"], lists=["Probate"]),
            R("q_prob0", "104 Queue St", tags=["RQ", "FTM"], lists=["Probate"]),
            R("q_cond", "105 Queue St", tags=["RQ"], lists=["Condemned"], phones=[{"number": "8655550313", "status": "UNKNOWN", "tags": ["Dial First"]}]),
            R("q_obnp", "106 Queue St", tags=["RQ"], lists=["Obituary"], last_obituary_date=_day(-240)),
            R("q_ob2", "107 Queue St", tags=["RQ"], lists=["Obituary", "Tax Delinquent"], last_obituary_date=_day(-120)),
            R("q_exh", "108 Queue St", tags=["RQ", "FTM"], phones=[{"number": "8655550314", "status": "WRONG", "tags": []}]),
            R("q_ret", "109 Queue St", tags=["RQ", "FTM"], status="Return Mail", phones=[{"number": "8655550315", "status": "UNKNOWN", "tags": ["Dial First"]}]),
            vac_,
            R("q_non", "111 Queue St", tags=["RQ", "Priority 1"]),
            R("q_trust", "112 Queue St", tags=["RQ"], first="", last="", company="Example Family Trust",
              phones=[{"number": "8655550316", "status": "UNKNOWN", "tags": ["Dial First"]}]),
            R("q_obdue", "113 Queue St", tags=["RQ"], lists=["Obituary"], last_obituary_date=_day(-100),
              phones=[{"number": "8655550317", "status": "UNKNOWN", "tags": ["Dial First"]}]),
            R("q_obyoung", "114 Queue St", tags=["RQ"], lists=["Obituary"], last_obituary_date=_day(-20)),
            R("q_parked", "115 Queue St", tags=["RQ", "Priority 2"], deceased=True, phones=[{"number": "8655550318", "status": "UNKNOWN", "tags": ["Dial First"]}]),
            R("q_lead", "116 Queue St", tags=["RQ", "FTM"], status="Hot Lead", lists=["Probate"]),
            R("q_llc", "117 Queue St", tags=["RQ"], first="", last="", company="Acme Holdings LLC"),
            R("q_passed", "118 Queue St", tags=["RQ", "FTM", "Courthouse Data"], lists=["Foreclosure"]),
            R("q_listed", "119 Queue St", tags=["RQ", "FTM"], lists=["Probate"], mls="Listed"),
            R("q_dnm", "120 Queue St", tags=["RQ", "FTM", "Do Not Market"], lists=["Probate"]),
            R("q_ni", "121 Queue St", tags=["RQ", "FTM"], status="not_interested", lists=["Probate"]),
            R("q_done", "122 Queue St", tags=["RQ", "FTM", "Deep Prospected"], lists=["Probate"]),
        ]
        c = RCRM(recs)
        c.custom = {"q_hot": {"Foreclosure Date": _day(9)}, "q_hot2": {"Foreclosure Date": _day(30)},
                    "q_passed": {"Foreclosure Date": _day(-3)},
                    "q_prob": {"Tax delinquency amount": "2140.00", "Total Delinquency": "99999.00"}}
        return c

    crm = feed()
    handed.clear()
    plan = sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq, plan=True)
    check(not crm.changed() and not handed and not ledger.events(action="handoff"),
          "a planned queue run writes nothing to the account and hands nothing over")
    check(plan.acted == 5 and "Would hand 5 record(s)" in plan.head and abs(plan.would_usd - 5 * 1.21) < 1e-6,
          "it says what it would hand over and what that would cost at the rate")
    check("8 waiting, about $9.68 at $1.21 a record" in plan.head,
          "it says how many are waiting and what the backlog would cost at the rate: %s" % plan.head)
    check([ln.split(":")[0] for ln in plan.lines[:5]] == ["101 Queue St, Knoxville", "102 Queue St, Knoxville", "103 Queue St, Knoxville",
                                                         "104 Queue St, Knoxville", "105 Queue St, Knoxville"]
          and "sale in 9 days" in plan.lines[0] and "owes $2,140" in plan.lines[2],
          "the top of the queue is the two sales by date, the two probates by tax owed, then the condemned house, each with its reason")
    t = plan.tally
    check(t.get("not queued: a lead, left to the lead manager") == 1 and t.get("held back: status not_interested") == 1
          and t.get("not queued: a company owns it: it needs the entity path, not a name search") == 1
          and t.get("not queued: the sale date passed") == 1 and t.get("not queued: listed or pending on the MLS") == 1
          and t.get("not queued: tagged Do Not Market") == 1 and t.get("not queued: already settled (tag Deep Prospected)") == 1
          and t.get("not queued: obituary 20 days ago: research opens at 90 days") == 1
          and t.get("not queued: a dead owner in the agency track with no second signal: parked") == 1,
          "everything left out of the queue is counted under its own reason")
    check(ledger.fact("q_hot", rq.SIG_FACT)["sales"] == [_day(9)] and ledger.fact("q_hot", rq.MARK_FACT) is None,
          "what a plan run learned about a record is remembered; a mark it only planned is not")
    reads_plan = crm.reads

    live = sweep.run("research_queue", crm=crm, now=T0 + timedelta(minutes=30), pol=pol, enqueue=enq)
    first = ["q_hot", "q_hot2", "q_prob", "q_prob0", "q_cond"]
    check([h[2] for h in handed] == first and all(h[:2] == ("research", "deep_prospect") and h[3] == "autopilot:research_queue" for h in handed),
          "a live run hands the top of the queue to the deep prospect workflow, in rank order, the way the handoff stage does")
    check(all("Research Queued" in crm.recs[u]["tags"] for u in first) and "Research Queued" not in crm.recs["q_obnp"]["tags"],
          "each handed record gets the tag Research Queued, and nothing else does")
    check(sorted(c["prop_uuid"] for c in crm.cards.values()) == sorted(first) and all(c["column"] == COL for c in crm.cards.values()),
          "each gets one card in the research column")
    check(all(crm.recs[u]["status"] is None for u in first) and "FTM" in crm.recs["q_hot"]["tags"],
          "a record whose owner is alive keeps its status and its lane's tag: it stays in its lane while it is researched")
    check(crm.reads - reads_plan == 5, "a record ranked from what is remembered is opened once more, right before it is handed over")
    ev = ledger.events("q_hot", "handoff")
    check(len(ev) == 1 and ev[0]["detail"]["to"] == "deep_prospect" and ev[0]["detail"]["class"] == "hot_auction"
          and ev[0]["usd"] == 0 and ledger.fact("q_hot", researchroutes.CARD_FACT)["card"],
          "the handoff is in the ledger with its reason and no dollars (the research task books its own), and the card id is kept")
    done_before = len(handed)
    again = sweep.run("research_queue", crm=crm, now=T0 + timedelta(minutes=60), pol=pol, enqueue=enq)
    second = ["q_obnp", "q_ob2", "q_exh", "q_ret", "q_vac"]
    check([h[2] for h in handed[done_before:]] == second and again.tally.get("already researched") == 5,
          "the next run hands the next five and never the same record twice")
    check(crm.recs["q_obnp"]["status"] == "Deep Prospecting" and crm.recs["q_ob2"]["status"] == "Deep Prospecting",
          "a record a dead owner already holds out of every lane gets the Deep Prospecting status")
    check(crm.recs["q_exh"]["status"] is None and crm.recs["q_vac"]["status"] is None and crm.recs["q_ret"]["status"] == "Return Mail",
          "no other record's status is touched, and a status that already means something is not overwritten")
    check(not [c for c in crm.cards.values() if c["prop_uuid"] in ("q_obnp", "q_ob2")] and again.tally.get("card left to the status") == 2
          and len(crm.cards) == 8,
          "when the status is set, the card is left to the account's own sequence, so the record never gets two")
    kinds = {w[0] for w in crm.writes}
    paths = {x[1] for x in crm.log if x[0] != "GET"}
    check(kinds <= {"add_tags", "status"} and all("/siftline/board/column/" in p for p in paths),
          "the queue writes a tag, a status and a card and nothing else: it never marks an owner deceased or changes an owner")
    ch = ledger.changes(record_uuid="q_obnp")
    check({c["what"] for c in ch} == {"add_tags", "status"} and [c for c in ch if c["what"] == "status"][0]["before"] is None
          and any(s["do"] == "set_status" and s["args"] is None for s in undo.plan(ch[0]["run_id"])),
          "every change is recorded with the value it replaced, so the run can be put back")
    check(json.loads(store.kv_get(rq.KV_LAST))["waiting"] == 3 and "3 waiting" in rq.digest_line(),
          "the queue's last numbers are kept for the morning readout")

    # the budget
    print("  queue budget")
    _wipe(store)
    crm = feed()
    handed.clear()
    tight = with_stage("research_queue", daily_usd=base_spent + 2.5)
    b1 = sweep.run("research_queue", crm=crm, now=T0, pol=tight, enqueue=enq)
    check(len(handed) == 2 and b1.acted == 2, "the queue stops at the day's research budget: $2.50 buys two records at $1.21")
    reads_before = crm.reads
    b2 = sweep.run("research_queue", crm=crm, now=T0 + timedelta(minutes=30), pol=tight, enqueue=enq)
    check(len(handed) == 2 and "research budget is used" in b2.head and "waiting, about $" in b2.head,
          "once the budget is used nothing more is handed over, and the run says how many wait and what they would cost")
    check(crm.reads == reads_before, "a run that cannot hand anything over opens no record")
    b3 = sweep.run("research_queue", crm=crm, now=T0 + timedelta(days=1), pol=tight, enqueue=enq)
    check(len(handed) == 4 and b3.acted == 2, "the budget is a day's: the next day it hands over again")
    _wipe(store)
    crm = feed()
    handed.clear()
    pricey = with_stage("research_queue", daily_usd=base_spent + 9.0, rate_usd=4.0)
    sweep.run("research_queue", crm=crm, now=T0, pol=pricey, enqueue=enq)
    check(len(handed) == 2, "the rate is a setting: at $4.00 a record the same $9.00 buys two")
    _wipe(store)
    crm = feed()
    handed.clear()
    for i in range(3):
        ledger.event("other-run", "ftm_retrace", "other-%d" % i, "handoff", {"to": "deep_prospect", "task": 0}, now=T0, tz=pol.tz)
    shared = with_stage("research_queue", daily_usd=base_spent + 5 * 1.21 + 0.1)
    sweep.run("research_queue", crm=crm, now=T0, pol=shared, enqueue=enq)
    check(len(handed) == 2, "a research handoff another stage made today counts against the same budget")
    _wipe(store)
    crm = feed()
    handed.clear()
    tid, _ = store.enqueue("research", "deep_prospect", "rs-spend-1", force=True)
    store.finish(tid, "done", {"street": "x"})
    store.add_spend(tid, 3.0, "research")
    spent_now = store.spent(day=utc_day, research=True)
    booked = with_stage("research_queue", daily_usd=spent_now + 1.5)
    sweep.run("research_queue", crm=crm, now=T0, pol=booked, enqueue=enq)
    check(len(handed) == 1 and abs(spent_now - base_spent - 3.0) < 1e-6,
          "research dollars already spent today, whoever asked for the research, come off the budget first")
    _wipe(store)
    crm = feed()
    handed.clear()
    config.RESEARCH_DAILY_CAP_USD = spent_now + 1.3
    capped = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", daily_usd=spent_now + 500), enqueue=enq)
    config.RESEARCH_DAILY_CAP_USD = 100000.0
    check(len(handed) == 1 and any("research budget on this box" in ln for ln in capped.lines),
          "the playbook's budget never reaches past the box's own research cap, and the run says which one held")
    _wipe(store)
    crm = feed()
    handed.clear()
    opened = [store.enqueue("research", "deep_prospect", "rs-open-%d" % i, force=True)[0] for i in range(2)]
    few = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", max_open=base_open + 3), enqueue=enq)
    check(len(handed) == 1, "no more research tasks are open at once than max_open: two open and room for three hands over one")
    full = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", max_open=base_open + 2), enqueue=enq)
    check(len(handed) == 1 and "still open" in full.head, "with the open tasks at the limit nothing is handed over, and the run says why")
    for t_id in opened:
        store.finish(t_id, "done", {})
    _wipe(store)
    crm = feed()
    handed.clear()
    capped_reads = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", max_reads=3), enqueue=enq, plan=True)
    check(crm.reads == 3 and capped_reads.tally.get("not ranked yet (opened on a later run)") == 17,
          "a run opens at most max_reads records to rank them, and says how many it has not ranked yet")

    # the second check, the gate, repeats, cards
    print("  queue checks and cards")
    _wipe(store)
    crm = feed()
    handed.clear()
    sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq, plan=True)
    crm.custom["q_hot"]["Foreclosure Date"] = _day(-1)          # the sale happened between the ranking and the handoff
    late = sweep.run("research_queue", crm=crm, now=T0 + timedelta(minutes=30), pol=pol, enqueue=enq)
    check("q_hot" not in [h[2] for h in handed] and late.tally.get("not queued: the sale date passed") == 2 and len(handed) == 5,
          "a record is checked again on a fresh read right before it is handed over: a sale that passed in between is not paid for")
    _wipe(store)
    lit = R("q_lit", "130 Queue St", tags=["RQ", "FTM"], lists=["Probate"], phones=[{"number": "8655550331", "status": "UNKNOWN", "tags": []}])
    crm = RCRM([lit])
    ledger.set_phone("8655550331", 60, "DNC", "Mobile", True)
    handed.clear()
    held = sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
    check(not handed and held.tally.get("held back: a litigator's number is on the record") == 1,
          "the gate runs on every record the queue picks up: a litigator's number holds it")
    _wipe(store)
    lead_rec = R("q_lead2", "131 Queue St", tags=["RQ", "FTM"], status="Hot Lead", lists=["Probate"], deceased=True)
    exh_rec = R("q_exh2", "132 Queue St", tags=["RQ", "FTM"], status="Exhausted", deceased=True, lists=["Probate"])
    crm = RCRM([lead_rec, exh_rec])
    handed.clear()
    sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", leads="research"), enqueue=enq)
    check({h[2] for h in handed} == {"q_lead2", "q_exh2"} and crm.recs["q_lead2"]["status"] == "Hot Lead"
          and crm.recs["q_exh2"]["status"] == "Exhausted" and not [w for w in crm.writes if w[0] == "status"],
          "a lead's status is never changed, even with a dead owner, and neither is a status that already means something")
    _wipe(store)
    asked = R("q_asked", "133 Queue St", tags=["Priority 2"], status="Deep Prospecting")
    crm = RCRM([asked, R("q_hot3", "134 Queue St", tags=["RQ", "FTM", "Courthouse Data"], lists=["Foreclosure"])])
    crm.custom = {"q_hot3": {"Foreclosure Date": _day(5)}}
    handed.clear()
    by_hand = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", statuses=["Deep Prospecting"]), enqueue=enq)
    check([h[2] for h in handed] == ["q_asked", "q_hot3"] and "a person set Deep Prospecting" in by_hand.lines[0]
          and crm.recs["q_asked"]["status"] == "Deep Prospecting",
          "a record a person put in Deep Prospecting by hand feeds the same queue, and goes first")
    # a failed run is retried once (the brain stamps its tasks with the real clock, so these runs follow it)
    _wipe(store)
    real = datetime.now(timezone.utc)
    crm = RCRM([R("q_fail", "135 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    handed.clear()
    sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
    f1, _ = store.enqueue("research", "deep_prospect", "q_fail", force=True)
    store.finish(f1, "failed", error="boom")
    same_day = sweep.run("research_queue", crm=crm, now=real + timedelta(hours=2), pol=pol, enqueue=enq)
    retry = sweep.run("research_queue", crm=crm, now=real + timedelta(days=2), pol=pol, enqueue=enq)
    check(len(handed) == 2 and same_day.tally.get("research failed: tried again after a day") == 1 and retry.acted == 1,
          "a research run that failed is handed over once more, a day later")
    check(len([c for c in crm.cards.values() if c["prop_uuid"] == "q_fail"]) == 1 and retry.tally.get("card reused") == 1,
          "a repeat run reuses the card the first run made: its id is in the ledger")
    f2, _ = store.enqueue("research", "deep_prospect", "q_fail", force=True)
    store.finish(f2, "failed", error="boom")
    final = sweep.run("research_queue", crm=crm, now=real + timedelta(days=5), pol=pol, enqueue=enq)
    check(len(handed) == 2 and final.tally.get("research failed 2 times: left for a person") == 1,
          "after a second failure the record is left for a person, not paid for every day")
    # a researched record is researched again only when the playbook says so
    _wipe(store)
    crm = RCRM([R("q_redo", "146 Queue St", tags=["RQ", "FTM", "Deep Prospected"], lists=["Probate"])])
    r1, _ = store.enqueue("research", "deep_prospect", "q_redo", force=True)
    store.finish(r1, "done", {})
    handed.clear()
    never_again = sweep.run("research_queue", crm=crm, now=real + timedelta(days=400), pol=pol, enqueue=enq)
    redo = with_stage("research_queue", redo_after_days=30)
    early = sweep.run("research_queue", crm=crm, now=real + timedelta(days=10), pol=redo, enqueue=enq)
    due = sweep.run("research_queue", crm=crm, now=real + timedelta(days=40), pol=redo, enqueue=enq)
    check(never_again.tally.get("already researched") == 1 and early.tally.get("already researched") == 1
          and [h[2] for h in handed] == ["q_redo"] and due.acted == 1,
          "a researched record is never researched again by the queue, unless the playbook sets how long to wait before a repeat")
    _wipe(store)
    crm = RCRM([R("q_twice", "147 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    handed.clear()
    sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
    t_a, _ = store.enqueue("research", "deep_prospect", "q_twice", force=True)
    store.finish(t_a, "failed", error="boom")
    sweep.run("research_queue", crm=crm, now=real + timedelta(days=2), pol=pol, enqueue=enq)
    t_b, _ = store.enqueue("research", "deep_prospect", "q_twice", force=True)
    store.finish(t_b, "done", {})
    sweep.run("research_queue", crm=crm, now=real + timedelta(days=3), pol=pol, enqueue=enq)
    check(len(handed) == 2 and "Research Queued" not in crm.recs["q_twice"]["tags"],
          "a record that needed a second run still loses the queue tag when its research finishes")
    # other ways into the queue: a tag, a list, a status
    _wipe(store)
    crm = RCRM([R("q_tag", "150 Queue St", tags=["Verify Deceased", "FTM"]), R("q_list", "151 Queue St", lists=["Condemned"]),
                R("q_stat", "152 Queue St", tags=["FTM"], status="Return Mail"), R("q_out", "153 Queue St", tags=["FTM"])])
    scope = with_stage("research_queue", scope_tags=["Verify Deceased"], scope_lists=["Condemned"], scope_statuses=["Return Mail"])
    scope.stage("research_queue").presets = []
    handed.clear()
    scoped = sweep.run("research_queue", crm=crm, now=T0, pol=scope, enqueue=enq)
    check([h[2] for h in handed] == ["q_list", "q_stat", "q_tag"] and scoped.found == 3,
          "a tag, a list or a status can feed the queue beside the presets, and the rank still decides the order")
    if hasattr(sweep.stages, "_return_rows"):
        _wipe(store)
        crm = RCRM([R("q_rs", "154 Queue St", tags=["FTM"], status="Return Mail")])
        returns = with_stage("research_queue", return_signals={"statuses": ["Return Mail"]})
        returns.stage("research_queue").presets = []
        handed.clear()
        back = sweep.run("research_queue", crm=crm, now=T0, pol=returns, enqueue=enq)
        check([h[2] for h in handed] == ["q_rs"] and back.lines and back.lines[0].endswith("mail came back"),
              "the handoff stage's own reading of returned mail feeds the queue as it is, so a return is seen however it was recorded")
    else:
        crm = RCRM([R("q_rs", "154 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
        handed.clear()
        back = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", return_signals={"statuses": ["Return Mail"]}), enqueue=enq)
        check(any("no reader for them" in p for p in back.problems) and len(handed) == 1,
              "with no reader for return signals in this build the queue says so and still works its presets")
    # which records are opened first, and when a record is opened again
    _wipe(store)
    crm = RCRM([R("q_p1", "160 Queue St", tags=["RQ", "FTM"], lists=["Probate"]), R("q_p2", "161 Queue St", tags=["RQ", "FTM"], lists=["Probate"]),
                R("q_soon", "162 Queue St", tags=["Auction Soon", "FTM", "Courthouse Data"], lists=["Foreclosure"])])
    crm.custom = {"q_soon": {"Foreclosure Date": _day(11)}}
    handed.clear()
    one = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", scope_tags=["Auction Soon"], max_reads=1), enqueue=enq)
    check([h[2] for h in handed] == ["q_soon"] and one.tally.get("not ranked yet (opened on a later run)") == 2 and crm.reads == 1,
          "when a run may open only some records, the ones a tag sent are opened first: a sale date is ranked on the first run")
    _wipe(store)
    crm = RCRM([R("q_turn", "163 Queue St", tags=["RQ", "FTM"], lists=["Obituary"], last_obituary_date=_day(-30))])
    soon = with_stage("research_queue", scope_tags=["Auction Soon"])
    handed.clear()
    early = sweep.run("research_queue", crm=crm, now=T0, pol=soon, enqueue=enq)
    crm.recs["q_turn"]["tags"] += ["Auction Soon", "Courthouse Data"]          # a foreclosure notice landed on the same house
    crm.recs["q_turn"]["lists"].append("Foreclosure")
    crm.custom["q_turn"] = {"Foreclosure Date": _day(12)}
    turned = sweep.run("research_queue", crm=crm, now=T0 + timedelta(hours=1), pol=soon, enqueue=enq)
    check(early.tally.get("not queued: obituary 30 days ago: research opens at 90 days") == 1
          and [h[2] for h in handed] == ["q_turn"] and "sale in 12 days" in turned.lines[0],
          "a record that turns up under a new source is opened again at once, not when its week is up: a new sale date is not ranked on old news")
    # research in flight, and finishing
    _wipe(store)
    crm = RCRM([R("q_fin", "136 Queue St", tags=["RQ", "FTM"], lists=["Probate"]),
                R("q_run", "137 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    handed.clear()
    running, _ = store.enqueue("research", "deep_prospect", "q_run", force=True)
    flight = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", max_open=rq.open_tasks() + 5), enqueue=enq)
    check([h[2] for h in handed] == ["q_fin"] and flight.tally.get("in research now") == 1,
          "a record whose research is already under way, whoever asked for it, is not handed over again")
    store.finish(running, "done", {})
    card_id = ledger.fact("q_fin", researchroutes.CARD_FACT)["card"]
    fin, _ = store.enqueue("research", "deep_prospect", "q_fin", force=True)
    store.finish(fin, "done", {"street": "136 Queue St"})
    closing = sweep.run("research_queue", crm=crm, now=T0 + timedelta(hours=3), pol=pol, enqueue=enq)
    check("Research Queued" not in crm.recs["q_fin"]["tags"] and crm.cards[card_id]["column"] == DONE
          and closing.tally.get("research finished: tag removed, card moved") == 1 and ledger.fact("q_fin", rq.MARK_FACT)["done"],
          "when the research task finishes, the queue tag comes off and the card moves on")
    again_closed = sweep.run("research_queue", crm=crm, now=T0 + timedelta(hours=4), pol=pol, enqueue=enq)
    check(not any(k.startswith("research finished") for k in again_closed.tally), "a record is closed out once")
    # cards: adopted, not doubled
    _wipe(store)
    activity.reset()
    crm = RCRM([R("q_seq", "138 Queue St", tags=["RQ", "FTM"], lists=["Probate"]),
                R("q_col", "139 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    activity._store([{"id": "rs-card-9", "event_type": "card.created", "resource": "card-seq", "resource_type": "card",
                      "timestamp": _ts(T0 - timedelta(hours=5)),
                      "payload": {"card": {"prop": "q_seq"}, "extra_info": {"card": {"prop": "q_seq", "column": [None, "Check Relatives"],
                                                                                      "board": [None, "Deep Prospecting"]}}}}])
    crm.cards["card-old"] = {"uuid": "card-old", "prop_uuid": "q_col", "column": COL}
    handed.clear()
    adopt = sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
    check(len(handed) == 2 and len(crm.cards) == 1 and adopt.tally.get("card reused") == 2
          and ledger.fact("q_seq", researchroutes.CARD_FACT)["card"] == "card-seq"
          and ledger.fact("q_col", researchroutes.CARD_FACT)["card"] == "card-old",
          "a card that already exists, in the log or in the column, is adopted and no second card is made")
    _wipe(store)
    crm = RCRM([R("q_nocol", "140 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    handed.clear()
    nocol = sweep.run("research_queue", crm=crm, now=T0, pol=with_stage("research_queue", card_column={"title": "Check Relatives"}), enqueue=enq)
    check(len(handed) == 1 and not crm.cards and "Research Queued" in crm.recs["q_nocol"]["tags"]
          and any("no route that lists" in p for p in nocol.problems),
          "with no column id the record is still tagged and handed over, no card is made, and the run says what is missing")
    _wipe(store)
    crm = RCRM([R("q_w1", "141 Queue St", tags=["RQ", "FTM"], lists=["Probate"]), R("q_w2", "142 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
    crm.columns = {COL: "Lead Intake", DONE: "Researched"}
    handed.clear()
    wrong = sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
    check(len(crm.cards) == 1 and len(handed) == 2 and any("titled 'Lead Intake'" in p for p in wrong.problems),
          "the first card is read back: a column id that belongs to another column stops the cards and is said out loud")
    if hasattr(sweep.stages, "research_open"):
        _wipe(store)
        crm = RCRM([R("q_lien", "143 Queue St", tags=["RQ", "FTM"], lists=["Liens"], phones=[{"number": "8655550341", "status": "UNKNOWN", "tags": ["Dial First"]}]),
                    R("q_lien_dead", "144 Queue St", tags=["RQ", "FTM"], lists=["Liens"], deceased=True)])
        handed.clear()
        by_list = with_stage("research_queue", research_by_list={"Liens": "exhausted"}, hints={"RQ - Feed": "vacant_mailing"})
        waits = sweep.run("research_queue", crm=crm, now=T0, pol=by_list, enqueue=enq)
        check([h[2] for h in handed] == ["q_lien_dead"] and any(k.startswith("research waits:") for k in waits.tally),
              "the list rule written for the handoff stage is asked, not rewritten: a lien waits until it is exhausted, a possible death does not")
        reads_before = crm.reads
        later = sweep.run("research_queue", crm=crm, now=T0 + timedelta(minutes=30), pol=by_list, enqueue=enq)
        check(crm.reads == reads_before and later.tally.get("research waits (checked earlier today)") == 1,
              "a record told to wait is not opened again the same day to be told again")
    else:
        check(True, "the list rule for research is not in this build, so the queue has nothing to ask")
    broken = rq.batch_rules
    rq.batch_rules = lambda: None
    try:
        crm = RCRM([R("q_norules", "145 Queue St", tags=["RQ", "FTM"], lists=["Probate"])])
        handed.clear()
        dead_box = sweep.run("research_queue", crm=crm, now=T0, pol=pol, enqueue=enq)
        check(not handed and not crm.changed() and any("did not load" in p for p in dead_box.problems),
              "a box that cannot load the house rules hands nothing over: a record nobody checked is not researched")
    finally:
        rq.batch_rules = broken

    _heirs(check, tmp, store, policy, pol, with_stage, enq, handed)
    _obituaries(check, tmp, store, policy, pol, with_stage)
    _probate(check, tmp, store, policy, pol, with_stage)
    _paid(check, tmp, store, policy, pol, with_stage)
    assert gate and heirlane and obituary and paidlookup and probate


# ====================================================================== the heir lane
def _research(store, tmp: Path, record: str, slug: str, result: dict, people: list | None = None, index: list | None = None) -> int:
    """A finished deep prospect on this box: the task with its result, and the run folder it names."""
    run_dir = Path(tmp) / "runs" / slug
    (run_dir / "analysis").mkdir(parents=True, exist_ok=True)
    full = dict(result, slug=slug, run_dir=str(run_dir), street=slug, written_back=True, live=True)
    (run_dir / "agent_result.json").write_text(json.dumps(full), encoding="utf-8")
    if people is not None:
        (run_dir / "analysis" / (slug + ".json")).write_text(json.dumps({"slug": slug, "people": people}), encoding="utf-8")
    if index is not None:
        (run_dir / "people.json").write_text(json.dumps([{"slug": slug, "people": index}]), encoding="utf-8")
    tid, _ = store.enqueue("research", "deep_prospect", record, force=True)
    store.finish(tid, "done", full)
    return tid


def _heirs(check, tmp, store, policy, pol, with_stage, enq, handed) -> None:
    from .autopilot import activity, heirlane, ledger, limits, sweep
    print("  heir copy")
    for word in ("died", "passed", "loss", "estate", "obituary", "probate", "inherit"):
        text = "Hi Kim, this is Dana. I heard about the %s and wanted to ask about 12 Oak St. Can we talk?" % word
        check(any("names a death" in p for p in heirlane.audit_copy(text, first="Kim", street="12 Oak St")),
              "heir copy that says %r is refused" % word)
    check(heirlane.audit_pools() == [], "every heir text and letter in the pool passes the audit: %s" % (heirlane.audit_pools()[:2] or "ok"))
    sample = [heirlane.render_text(t, "seed-%d" % i, "Kim", "12 Oak St", "Dana") for t in (1, 2) for i in range(12)]
    check(all("Kim" in s and "12 Oak St" in s and "Dana" in s and s.count("?") == 1 and len(s) <= 160 for s in sample),
          "an heir text greets the heir by name, names the property and the sender, asks one question and fits one message")
    check(heirlane.render_text(1, "a|b", "Kim", "12 Oak St", "Dana") == heirlane.render_text(1, "a|b", "Kim", "12 Oak St", "Dana")
          and len(set(sample)) > 3, "the wording is picked by the record and the heir, so a re-run writes the same words and neighbours differ")
    bad = heirlane.audit_copy("Hi Kim, see www.example.com about the foreclosure at 12 Oak St 37917!! Call? Now?", first="Kim", street="12 Oak St")
    check(len([p for p in bad if any(k in p for k in ("link", "names the list", "zip", "exclamation", "more than one question"))]) == 5,
          "the texting rules hold for an heir too: no link, no list, no zip, no stacked marks, one question")
    check(not heirlane.audit_copy("Hi Kim, I wanted to ask about 10432 Kingston Pike. Can we talk?", first="Kim", street="10432 Kingston Pike"),
          "a five-digit house number is not taken for a zip code")
    letter = heirlane.render_letter(1, "", "12 Oak St", "Dana", "865-555-0100")
    check(letter.startswith("Hi there,") and "12 Oak St" in letter and not heirlane.audit_copy(letter, street="12 Oak St", sms=False),
          "a letter to a signer with no usable first name greets nobody by a wrong one")
    check(heirlane.same_person("Kim Example", "kim a. example") and heirlane.same_person("Kim", "Kim Example")
          and not heirlane.same_person("James Example Sr", "James Example Jr") and not heirlane.same_person("Lee James Example", "James Example"),
          "two spellings name one person only when the first name agrees, and a Sr. is never his Jr.")

    print("  who signs")
    people = [{"name": "Kim Example", "relationship": "daughter", "living": "Living", "role": "signer", "ss_key": "k1",
               "trace_address": {"street": "45 Elm Ct", "city": "Maryville", "state": "TN", "zip": "37801"}},
              {"name": "Lee Example", "relationship": "son", "living": "Living", "role": "signer", "ss_key": "k2"},
              {"name": "Ray Example", "relationship": "brother", "living": "Unknown", "role": "signer", "ss_key": "k3"},
              {"name": "Moe Example", "relationship": "son", "living": "Deceased", "role": "none", "ss_key": "k4"}]
    index = [{"key": "k2", "street": "9 Birch Rd", "city": "Alcoa", "state": "tn", "zip": "37701-1234"},
             {"key": "k1", "street": "1 Wrong Way", "city": "Knoxville", "state": "TN", "zip": "37917"}]
    result = {"owner_status": "Deceased", "decision_maker": "Kim Example",
              "must_sign": [{"who": "Kim Example", "status": "Living", "why": "daughter", "short": "Kim (daughter)"},
                            {"who": "Lee Example and Moe Example", "status": "", "why": "sons", "short": "Lee, Moe"},
                            {"who": "Ray Example", "status": "UNVERIFIED: not shown living", "why": "brother", "short": "Ray"},
                            {"who": "the heirs of Pat Example", "status": "", "why": "", "short": ""}],
              "call_order": [{"who": "Lee Example", "number": "(865) 555-0202", "why": "best number"},
                             {"who": "Kim", "number": "8655550201", "why": "second"},
                             {"who": "Ray Example", "number": "8655550209", "why": "unverified"}]}
    heirs, notes = heirlane.heirs_of({"must_sign": result["must_sign"], "call_order": result["call_order"], "people": people,
                                      "index": {p["key"]: p for p in index}, "verified": None})
    check([h.name for h in heirs] == ["Lee Example", "Kim Example"] and heirs[0].numbers == ["8655550202"] and heirs[1].numbers == ["8655550201"],
          "the signers are worked in the order the research set its call order")
    check(notes["signer not verified as living: confirm before any call"] == 1 and not any(h.name == "Ray Example" for h in heirs),
          "a signer the research marked UNVERIFIED is never called as a signer")
    check(notes["a signer line names a role or a group, not a person"] == 1 and notes["signer the research does not show living"] == 1,
          "a line that names a group names nobody, and a signer the research shows dead is left out")
    short, n_short = heirlane.heirs_of({"must_sign": [{"who": "J Example", "status": "Living"}, {"who": "Example", "status": "Living"},
                                                      {"who": "Kim Example", "status": "Living"}],
                                        "call_order": [{"who": "J Example", "number": "8655550207"}], "people": [], "index": {}, "verified": None})
    check([h.name for h in short] == ["Kim Example"] and n_short["a signer named by an initial or one word only: left for a person"] == 2
          and not short[0].numbers,
          "a signer the research names by an initial or one word is left for a person: nobody is called or greeted on a guess")
    check((heirs[1].street, heirs[1].city, heirs[1].zip) == ("45 Elm Ct", "Maryville", "37801")
          and (heirs[0].street, heirs[0].state, heirs[0].zip) == ("9 Birch Rd", "TN", "37701") and heirs[0].relationship == "son",
          "each signer's address is their own: the one the analysis set, else the one their own trace returned")
    only_kim, n2 = heirlane.heirs_of({"must_sign": result["must_sign"], "call_order": result["call_order"], "people": people,
                                      "index": {}, "verified": ["Kim Example"]})
    check([h.name for h in only_kim] == ["Kim Example"] and n2["signer the research has not verified (no opened record, or no number in their name)"] == 1,
          "when the research result lists who is verified, that list decides")

    print("  heir lane")
    _wipe(store)
    activity.reset()

    def heir_rec(u, street):
        r = R(u, street, tags=["Heir Identified", "FTM"], lists=["Obituary"], status="Deep Prospecting", deceased=True,
              phones=[{"number": "8655550201", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First", "Daughter"]},
                      {"number": "8655550202", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First", "Son"]},
                      {"number": "8655550203", "type": "LANDLINE", "status": "UNKNOWN", "tags": ["Dial First"]}])
        return r

    h1 = heir_rec("h1", "12 Oak St")
    h2 = R("h2", "14 Oak St", tags=["Heir Identified"], deceased=True,
           phones=[{"number": "8655550211", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}])
    h3 = R("h3", "16 Oak St", tags=["Heir Identified"], deceased=True)
    crm = RCRM([h1, h2, h3])
    _research(store, tmp, "h1", "12-oak-st", result, people, index)
    _research(store, tmp, "h2", "14-oak-st",
              {"owner_status": "Deceased", "must_sign": [{"who": "Amy Sample", "status": "Living", "why": "", "short": ""}],
               "call_order": [{"who": "Amy Sample", "number": "8655550211", "why": ""}]},
              [{"name": "Amy Sample", "relationship": "niece", "living": "Living", "role": "signer", "ss_key": "a1",
                "trace_address": {"street": "3 Cedar Ln", "city": "Knoxville", "state": "TN", "zip": "37919"}}], [])
    plan = sweep.run("heir_lane", crm=crm, now=T0, pol=pol, plan=True)
    check(not crm.changed() and not ledger.batches() and plan.acted > 0 and "Would:" in plan.head,
          "a planned heir run writes nothing and stages nothing, and says what it would do")
    check(any("no research result on this box" in p for p in plan.problems) and plan.tally.get("verified signers") == 3,
          "a tagged record with no research result on this box is listed for a person: who signs is never guessed")
    live = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    check(crm.recs["h1"]["assigned_to"] == "u-dana" and crm.recs["h2"]["assigned_to"] == "u-dana" and crm.recs["h3"]["assigned_to"] is None,
          "researched records go to the one heir caller the playbook names")
    titles = [t["title"] for t in crm.tasks]
    check(len(crm.tasks) == 2 and titles[0] == "Call Lee Example (son) about 12 Oak St. Number ending 0202. Heir 1 of 2."
          and crm.tasks[0]["assigned_to_users"] == ["u-dana"] and crm.tasks[0]["assigned_to_property"] == "h1"
          and "Do not bring up a death" in crm.tasks[0]["notes"],
          "one heir at a time: the caller gets one task, for the first signer in the research's order, and is told to ask about the property only")
    mail_b = [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_MAIL]
    text_b = [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT]
    letters = ledger.batch(mail_b[0]["id"])["items"]
    texts = ledger.batch(text_b[0]["id"])["items"]
    check(len(mail_b) == 1 and len(text_b) == 1 and not [w for w in crm.writes if w[0] in ("message", "add_tags", "status", "upsert")],
          "letters and texts are staged for a person; nothing is sent and nothing is written on the record")
    by_heir = {(it["record_uuid"], it["name"]): it for it in letters}
    check(set(by_heir) == {("h1", "Lee Example"), ("h1", "Kim Example"), ("h2", "Amy Sample")}
          and by_heir[("h1", "Lee Example")]["street"] == "9 Birch Rd" and by_heir[("h1", "Kim Example")]["street"] == "45 Elm Ct"
          and by_heir[("h2", "Amy Sample")]["city"] == "Knoxville" and all(it["letter"] == 1 and it["of"] == 4 for it in letters),
          "every verified signer gets a letter of their own, at their own address")
    check(not any(it["street"] in ("12 Oak St", "14 Oak St", "9 Mail Rd") or "Pat" in it["name"] for it in letters)
          and all("Example" not in it["text"] and "Pat" not in it["text"] for it in letters),
          "no heir letter goes to the property or to the dead owner's mailing address, and none carries the dead owner's name")
    check(sorted((it["record_uuid"], it["name"], it["number"]) for it in texts) == [("h1", "Lee Example", "8655550202"), ("h2", "Amy Sample", "8655550211")]
          and all(it["first_name"] in it["text"] and it["property_street"] in it["text"] and not heirlane.DEATH_WORDS.search(it["text"]) for it in texts),
          "only the heir whose turn it is gets a text: by name, about the property, on their own number, and never about the death")
    files = [Path(tmp) / "mail" / ("batch_%d.csv" % mail_b[0]["id"]), Path(tmp) / "texts" / ("batch_%d.csv" % text_b[0]["id"])]
    check(all(f.is_file() for f in files) and len(list(csv.DictReader(files[0].open(encoding="utf-8")))) == 3,
          "each staged batch is also a file a person can hand to the mail house or load into the texting tool")
    same = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(hours=2), pol=pol)
    check(len(crm.tasks) == 2 and len(ledger.batches(status="staged")) == 2 and same.tally.get("already staged, waiting on release") == 3,
          "a second run the same day writes no second task and stages nothing twice")
    check(_stock_release_refuses(mail_b[0]["id"], crm, pol) and _stock_release_refuses(text_b[0]["id"], crm, pol)
          and not ledger.events("h1", "mail") and not crm.msgs and len(ledger.batches(status="staged")) == 2,
          "the ordinary mail release cannot book an heir batch as ordinary mail: it stops before it writes anything")

    print("  heir texts and the house limits")
    tid0 = text_b[0]["id"]
    held_rows = limits.rows(batch_id=tid0, statuses=("staged",))
    check(sorted((r["owner_uuid"], r["number"], r["system"], r["series"]) for r in held_rows)
          == [("o-h1", "8655550202", "smarter_contact", "heir"), ("o-h2", "8655550211", "smarter_contact", "heir")],
          "a staged heir text holds its owner's place in the house text ledger (the owner whose record carries the heir's number)")
    check(limits.room("o-h1", "text", T0 + timedelta(hours=1), policy=pol).code == "day",
          "so while it waits, no other texting stage may text that owner the same day")
    on_day = T0 + timedelta(hours=2, minutes=30)
    t_dry = heirlane.release(tid0, crm=crm, pol=pol, now=on_day)
    t1 = heirlane.release(tid0, crm=crm, pol=pol, commit=True, now=on_day)
    t2 = heirlane.release(tid0, crm=crm, pol=pol, commit=True, uploaded=True, now=on_day + timedelta(minutes=5))
    check(t_dry["would_hold"] == 0 and t1["pieces"] == 2 and t2["sent"] == 2
          and sorted(r["status"] for r in limits.rows(batch_id=tid0, statuses=("staged", "released", "dropped"))) == ["released", "released"]
          and ledger.events("h1", "heir_text")[0]["detail"]["heir"] == "lee example"
          and any("Texted Lee Example about the property" in m[1] for m in crm.msgs),
          "released on its day, each text is written down under the heir, and its place in the text ledger becomes a sent text")

    print("  heir turns")
    turn_start = datetime.fromisoformat(ledger.fact("h1", heirlane.TURN_FACT)["since"])
    activity._store([_dial("h%d" % i, "o-h1", turn_start + timedelta(days=i + 1, hours=1), "8655550202") for i in range(4)])
    mid = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=6), pol=pol)
    check(len(crm.tasks) == 2 and not any("Kim" in t["title"] for t in crm.tasks) and mid is not None,
          "four call days are not a full round: the first heir keeps the turn")
    activity._store([_dial("h9", "o-h1", turn_start + timedelta(days=7, hours=1), "8655550202")])
    nxt = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=8), pol=pol)
    check(crm.tasks[-1]["title"] == "Call Kim Example (daughter) about 12 Oak St. Number ending 0201. Heir 2 of 2." and nxt.acted >= 1,
          "after a full round with no conversation the next heir's turn starts, with its own task")
    kim_texts = [it for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT
                 for it in ledger.batch(b["id"])["items"] if it["name"] == "Kim Example"]
    check(len(kim_texts) == 1 and kim_texts[0]["number"] == "8655550201", "and the second heir is texted when her turn starts, not before")
    activity._store([_dial("h20", "o-h2", T0 + timedelta(days=1), "8655550211"),
                     _dial("h21", "o-h2", T0 + timedelta(days=1, minutes=1), "8655550211", kind="answered", duration=240)])
    tasks_before = len(crm.tasks)
    talked = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=9), pol=pol)
    check(talked.tally.get("a conversation happened: the caller has it", 0) >= 1 and len(crm.tasks) == tasks_before,
          "a real conversation holds the turn: the caller has it and no new task is written")

    print("  heir letters: release")
    mail_id = mail_b[0]["id"]
    notes_before = len(crm.msgs)
    dry = heirlane.release(mail_id, crm=crm, pol=pol, now=T0 + timedelta(days=9))
    check(dry["dry"] and dry["pieces"] == 3 and dry["would_hold"] == 0 and ledger.batch(mail_id)["status"] == "staged"
          and len(crm.msgs) == notes_before,
          "a dry release only counts, and says how many pieces would be held now")
    crm.recs["h2"]["status"] = "Hot Lead"                       # the niece called back: the record is a lead now
    step1 = heirlane.release(mail_id, crm=crm, pol=pol, commit=True, now=T0 + timedelta(days=9))
    kept = ledger.batch(mail_id)["items"]
    check(step1["pieces"] == 2 and step1["held"] == {"the record left the heir lane (now lead)": 1} and len(kept) == 2
          and len(list(csv.DictReader(Path(step1["file"]).open(encoding="utf-8")))) == 2 and ledger.batch(mail_id)["status"] == "staged",
          "at release every piece is checked again: a record that became a lead is taken out and the file is written fresh without it")
    day9 = ledger.day(T0 + timedelta(days=9), pol.tz)
    mail_before = ledger.spent(day9, action="mail")
    step2 = heirlane.release(mail_id, crm=crm, pol=pol, commit=True, uploaded=True, now=T0 + timedelta(days=9))
    sent = [e for e in ledger.events("h1", "mail") if e["detail"].get("series") == "heir"]
    check(step2["sent"] == 2 and ledger.batch(mail_id)["status"] == "sent" and {e["detail"]["heir"] for e in sent} == {"lee example", "kim example"}
          and all(e["detail"]["step"] == 1 and e["stage"] == "heir_lane" for e in sent),
          "once the file is uploaded each letter is written down under the heir it went to")
    check(abs(ledger.spent(day9, action="mail") - mail_before - 6.0) < 1e-6,
          "an heir letter is mail spend: it counts against the outreach budget, never the data budget")
    check("Mailed 2026-11" in crm.recs["h1"]["tags"] and any("heir letter 1 of 4 to Lee Example (son) at their own address" in m[1] for m in crm.msgs)
          and crm.recs["h1"]["status"] == "Deep Prospecting" and crm.recs["h1"]["owner"]["first_name"] == "Pat"
          and crm.recs["h1"]["owner"].get("deceased") is True,
          "the record gets the mailed tag and a note; its status, its owner and the owner's marks are left exactly as they were")
    try:
        heirlane.release(mail_id, crm=crm, pol=pol, commit=True, uploaded=True)
        check(False, "a batch is released once")
    except SystemExit:
        check(True, "a batch is released once")
    pulled = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=9, hours=1), pol=pol)
    left_texts = [it for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT for it in ledger.batch(b["id"])["items"]]
    check(not any(it["record_uuid"] == "h2" for it in left_texts) and pulled.tally.get("now a lead: the lead manager has it") == 1,
          "the roster stops the moment a record leaves the lane: its staged text is pulled on the next run")
    soon = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=20), pol=pol)
    check(not [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_MAIL] and soon is not None,
          "the next letter waits its month")
    month = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(days=41), pol=pol)
    second = [it for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_MAIL for it in ledger.batch(b["id"])["items"]]
    check(sorted((it["name"], it["letter"]) for it in second) == [("Kim Example", 2), ("Lee Example", 2)] and month.batch,
          "then each signer's second letter is staged, tracked heir by heir")
    kim_b = [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT]
    expired = [b for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT and b["status"] == "expired"]
    check(any("was never released" in p for p in month.problems) and expired
          and limits.rows(batch_id=expired[0]["id"], statuses=("expired",)) and not limits.rows(batch_id=expired[0]["id"]),
          "a text batch nobody released on its day is closed on the next run, said out loud, and gives the owner's place back")
    later = T0 + timedelta(days=41, minutes=10)
    heirlane.release(kim_b[0]["id"], crm=crm, pol=pol, commit=True, now=later)
    texted = heirlane.release(kim_b[0]["id"], crm=crm, pol=pol, commit=True, uploaded=True, now=later)
    check(len(kim_b) == 1 and texted["sent"] == 1
          and [e["detail"]["heir"] for e in ledger.events("h1", "heir_text")] == ["lee example", "kim example"]
          and any("Texted Kim Example about the property" in m[1] for m in crm.msgs),
          "then the heir whose turn it is is staged again, and her text is written down under her own name")

    print("  heir lane refusals")
    _wipe(store)
    activity.reset()
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    no_allow = with_stage("heir_lane", gate_allow=[])
    held = sweep.run("heir_lane", crm=crm, now=T0, pol=no_allow)
    check(not ledger.batches() and not crm.tasks and any("research first" in k for k in held.tally),
          "without the heir allow word the gate holds a dead owner's record: the lane opens only where the playbook says so")
    crm.recs["h1"]["tags"].append("Do Not Market")
    dnm = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    check(not ledger.batches() and not crm.tasks and any("Do Not Market" in k for k in dnm.tally),
          "nothing the lane is allowed lifts Do Not Market")
    crm.recs["h1"]["tags"].remove("Do Not Market")
    for n, want in ((9, 6), (2, 3)):
        _wipe(store)
        clamp = sweep.run("heir_lane", crm=RCRM([heir_rec("h1", "12 Oak St")]), now=T0, pol=with_stage("heir_lane", letters_per_heir=n))
        items = [it for b in ledger.batches() for it in ledger.batch(b["id"])["items"] if it.get("kind") == "letter"]
        check(items and all(it["of"] == want for it in items) and any("three to six" in p for p in clamp.problems),
              "a signer gets three to six letters: a setting of %d is read as %d, out loud" % (n, want))
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    crm.recs["h1"]["row_phone"] = {"number": "8655550202", "doNotCall": True}
    flagged = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    texts = [it for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT for it in ledger.batch(b["id"])["items"]]
    check(not any(it["number"] == "8655550202" for it in texts) and crm.tasks and "Kim" in crm.tasks[0]["title"] and flagged is not None,
          "a number the do-not-call registry flags is never texted or put on a call task; the turn passes to the next heir")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    for p in crm.recs["h1"]["owner"]["phones"]:
        p["tags"] = ["Dial Third"]
    low = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    check(not crm.tasks and [b["kind"] for b in ledger.batches()] == [heirlane.KIND_MAIL]
          and low.tally.get("calls and texts held back: no usable number for a call") == 1 and crm.recs["h1"]["assigned_to"] is None,
          "only the tiers the gate allows are dialed or texted (decision D12): a signer with a lower tier gets letters and no call")
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    for p in crm.recs["h1"]["owner"]["phones"]:
        if p["number"] in ("8655550201", "8655550202"):
            p["tags"] = ["Dial Third"]                          # the record keeps one good number, and it is nobody's
    crm.recs["h1"]["owner"]["phones"][2]["type"] = "MOBILE"
    _wipe(store)
    stray = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    check(not crm.tasks and [b["kind"] for b in ledger.batches()] == [heirlane.KIND_MAIL]
          and stray.tally.get("no usable number for any signer: letters only") == 1,
          "a good number on the record that the research ties to no signer is not dialed as an heir's")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    poor = sweep.run("heir_lane", crm=crm, now=T0, pol=policy.parse({**copy.deepcopy(PLAYBOOK), "caps": {"daily_usd": 25, "record_usd": 1.0,
                                                                                                         "outreach_daily_usd": 3.5}}, EMPLOYEES))
    items = [it for b in ledger.batches() if b["kind"] == heirlane.KIND_MAIL for it in ledger.batch(b["id"])["items"]]
    check(len(items) == 1 and any("outreach cap" in k for k in poor.tally), "heir letters stop at the day's outreach budget")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    saved_pool = heirlane.TEXTS[0][:]
    heirlane.TEXTS[0].append("Hi {first}, {sender} here. So sorry for your loss. Could we talk about {street}?")
    try:
        refused = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
        check(not ledger.batches() and any("heir copy refused" in p for p in refused.problems),
              "one variant that names a death stops the staging of every text and letter until the copy is fixed")
    finally:
        heirlane.TEXTS[0][:] = saved_pool
    _wipe(store)
    crm = RCRM([R("h5", "18 Oak St", tags=["Heir Identified"], deceased=True,
                  phones=[{"number": "8655550221", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}])])
    _research(store, tmp, "h5", "18-oak-st",
              {"owner_status": "Deceased", "must_sign": [{"who": "J Sample", "status": "Living"}, {"who": "Ray Sample", "status": "UNVERIFIED: unknown"}],
               "call_order": [{"who": "J Sample", "number": "8655550221"}]},
              [{"name": "J Sample", "living": "Living", "role": "signer"}], [])
    lone = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    check(lone.tally.get("no verified signer in the research: nothing to work") == 1 and not crm.tasks and not ledger.batches(),
          "a record whose only named signers are an initial and an unverified person has nobody to work")
    _wipe(store)
    crm = RCRM([R("h6", "20 Oak St", tags=["Heir Identified"], deceased=True,
                  phones=[{"number": "8655550231", "type": "LANDLINE", "status": "UNKNOWN", "tags": ["Dial First"]}])])
    _research(store, tmp, "h6", "20-oak-st",
              {"owner_status": "Deceased", "must_sign": [{"who": "Ann Sample", "status": "Living"}],
               "call_order": [{"who": "Ann Sample", "number": "8655550231"}]},
              [{"name": "Ann Sample", "living": "Living", "role": "signer"}], [])
    land = sweep.run("heir_lane", crm=crm, now=T0, pol=with_stage("heir_lane", heir_caller=None))
    check(len(crm.tasks) == 1 and not [b for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT]
          and land.tally.get("no mobile number of the heir's that may be texted") == 1
          and land.tally.get("signer with no address of their own in the research: no letter") == 1,
          "a signer with only a landline is called and not texted, and one with no address of their own gets no letter")
    check(crm.recs["h6"]["assigned_to"] == "u-adriana" and any("No heir caller is named" in ln for ln in land.lines),
          "until the playbook names the heir caller the first caller is used, and the run says so")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    real_first = heirlane.mail._clean_first
    heirlane.mail._clean_first = lambda raw: ""
    try:
        noname = sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
        letters = [it for b in ledger.batches() if b["kind"] == heirlane.KIND_MAIL for it in ledger.batch(b["id"])["items"]]
        check(not [b for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT] and crm.tasks
              and noname.tally.get("no first name for the heir: no text (a text goes out by name)") == 1
              and letters and all(it["text"].startswith("Hi there,") for it in letters),
              "an heir with no usable first name is called and written to, and is not texted: a text goes out by name or not at all")
    finally:
        heirlane.mail._clean_first = real_first
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    tb = [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT][0]
    for p in crm.recs["h1"]["owner"]["phones"]:
        if p["number"] == "8655550202":
            p["status"] = "WRONG"                               # a caller marked it between staging and release
    dry = heirlane.release(tb["id"], crm=crm, pol=pol, now=T0 + timedelta(minutes=20))
    gone = heirlane.release(tb["id"], crm=crm, pol=pol, commit=True, now=T0 + timedelta(minutes=20))
    check(dry["would_hold"] == 1 and "the number is no longer usable for a text" in dry["reasons"] and gone.get("cancelled")
          and ledger.batch(tb["id"])["status"] == "cancelled" and not ledger.events("h1", "heir_text")
          and not limits.rows(batch_id=tb["id"]),
          "a text is checked again at release: a number marked wrong since it was staged is taken out, and an empty batch is cancelled")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    sweep.run("heir_lane", crm=crm, now=T0, pol=pol)
    tb = [b for b in ledger.batches(status="staged") if b["kind"] == heirlane.KIND_TEXT][0]
    stale = heirlane.release(tb["id"], crm=crm, pol=pol, now=T0 + timedelta(days=1))
    check(stale["would_hold"] == 1 and any("not released that day" in k for k in stale["reasons"]),
          "a text batch is released on its day or not at all: the next morning every text in it is held")
    crm.recs["h1"]["status"] = "Hot Lead"
    sweep.run("heir_lane", crm=crm, now=T0 + timedelta(minutes=30), pol=pol)
    check(ledger.batch(tb["id"])["status"] == "cancelled" and not limits.rows(batch_id=tb["id"])
          and limits.room("o-h1", "text", T0 + timedelta(hours=1), policy=pol).ok,
          "a staged text pulled because the record left the lane gives the owner's place in the text ledger back")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    limits.stage_text(stage="text_opening", batch_id=9001, owner_uuid="o-h1", record_uuid="h1",
                      numbers=[{"number": "8655550209"}], system="smarter_contact", series="opening", step=1,
                      touch="opening:1", tz=pol.tz, when=T0)            # another lane already texted this owner today
    busy = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(minutes=5), pol=pol)
    check(not [b for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT] and crm.tasks
          and [b["kind"] for b in ledger.batches()] == [heirlane.KIND_MAIL]
          and busy.tally.get("no heir text today: the owner already has a text today") == 1,
          "an owner another lane texted today gets no heir text: one text a day holds across every texting stage")
    _wipe(store)
    crm = RCRM([heir_rec("h1", "12 Oak St")])
    late = sweep.run("heir_lane", crm=crm, now=T0 + timedelta(hours=3), pol=pol)      # 6:10pm on the business clock
    check(not [b for b in ledger.batches() if b["kind"] == heirlane.KIND_TEXT]
          and late.tally.get("no heir text today: today's sending window has closed") == 1,
          "no heir text is staged once the day's sending window has closed")
    assert handed is not None and enq is not None


# ====================================================================== the obituary pipeline
def _obituaries(check, tmp, store, policy, pol, with_stage) -> None:
    from .autopilot import activity, heirlane, ledger, obituary, sweep
    print("  obituary timeline")
    o = obituary.options(pol.stage("obituary_lane"))
    check([obituary.window(d, o) for d in (0, 29, 30, 89, 90, 400)] == ["hold", "hold", "soft", "soft", "research", "research"],
          "an obituary record is held for a month, gets a soft letter before month three, and is researched from three months")
    check(obituary.audit_copy_pool(o) == [], "the obituary letters pass the same audit as heir copy: no death, no list")
    check(obituary.family_name({"first_name": "PAT", "last_name": "EXAMPLE"}) == "The Example Family"
          and obituary.family_name({"company": "Example Family Trust"}) == "" and obituary.family_name({"first_name": "Pat"}) == "",
          "mail goes to the family by surname only; a trust, a company or an owner with no surname has no family to write to")

    def ob(u, street, days, **kw):
        lists = kw.pop("lists", ["Obituary"])
        tags = ["OB"] + kw.pop("tags", [])
        r = R(u, street, tags=tags, lists=lists, **kw)
        if days is not None:
            r["last_obituary_date"] = _day(-days)
        return r

    def build():
        twin = ob("o_twin", "77 Pine St", 150)
        other = R("p_twin", "77 Pine Street", lists=["Probate"], tags=["FTM"])
        trust = ob("o_trust", "90 Pine St", 45, first="", last="", company="Example Family Trust")
        c = RCRM([ob("o_hold", "70 Pine St", 10), ob("o_called", "71 Pine St", 12), ob("o_soft", "72 Pine St", 45),
                  ob("o_month", "73 Pine St", 100), ob("o_np", "74 Pine St", 200), ob("o_done", "75 Pine St", 120, tags=["Deep Prospected"]),
                  ob("o_prob", "76 Pine St", 150, lists=["Obituary", "Probate"], tags=["Obituary, No Probate"]),
                  twin, other, trust, ob("o_nodate", "78 Pine St", None),
                  ob("o_lead", "79 Pine St", 100, status="Hot Lead")])
        c.log_rows = [_dial("ob1", "o-o_called", T0 - timedelta(days=3), "8655550401")]
        return c

    _wipe(store)
    activity.reset()
    crm = build()
    plan = sweep.run("obituary_lane", crm=crm, now=T0, pol=pol, plan=True)
    check(not crm.changed() and not ledger.batches() and "Would:" in plan.head,
          "a planned obituary run writes nothing and stages nothing")
    check("2 inside the first month, 2 due a soft letter, 3 at three months or more" in plan.head,
          "the run says where the lane's records stand: %s" % plan.head)
    check(any("called inside their first month" in p and "71 Pine St" in p for p in plan.problems),
          "no calls in the first month: a dial made inside it is reported by street")
    live = sweep.run("obituary_lane", crm=crm, now=T0, pol=pol)
    batch = [b for b in ledger.batches(status="staged") if b["kind"] == obituary.KIND_MAIL]
    items = ledger.batch(batch[0]["id"])["items"] if batch else []
    by = {it["record_uuid"]: it for it in items}
    check(set(by) == {"o_soft", "o_month", "o_np"} and by["o_soft"]["series"] == obituary.SOFT and by["o_month"]["series"] == obituary.MONTHLY
          and by["o_month"]["letter"] == 1 and by["o_month"]["of"] == 12,
          "one soft letter goes before month three and the monthly mail starts at three months, twelve pieces at most")
    check(all(it["name"] == "The Example Family" and it["first_name"] == "" and "Pat" not in it["text"] and it["street"] == "9 Mail Rd" for it in items),
          "every piece is addressed to the family by surname: it never carries the first name of a person who may have died")
    check("o_hold" not in by and "o_called" not in by, "nothing goes out in the first month")
    check("o_done" not in by and live.tally.get("research is done: the obituary mail has stopped") == 1,
          "the obituary mail stops when research is done")
    check("o_prob" not in by and "o_lead" not in by and live.tally.get("probate: it has its own lane", 0) >= 1
          and live.tally.get("now a lead: the lead manager has it") == 1,
          "probate records are kept out of the obituary lane, and a lead is left to the lead manager")
    check(live.tally.get("no obituary date on the record: left alone") == 1
          and live.tally.get("not mailable: a company, a trust or an owner with no surname: no family to write to") == 1,
          "a record with no obituary date is left alone, and a trust is not written to as a family")
    check(live.tally.get("due for research (the research queue ranks them)") == 2,
          "every record at three months is counted as due for research; the research queue ranks them")
    check("Obituary, No Probate" in crm.recs["o_np"]["tags"] and "Obituary, No Probate" not in crm.recs["o_month"]["tags"],
          "an obituary with no probate filing after six months earns the tag Obituary, No Probate")
    check("Obituary, No Probate" not in crm.recs["o_prob"]["tags"]
          and live.tally.get("a probate was filed on an obituary record: moved to the probate lane") == 1,
          "the day a probate lands on an obituary record it is counted as moved and its six-month tag comes off")
    check("Probate Filed" in crm.recs["o_twin"]["tags"] and "Probate" not in crm.recs["o_twin"]["lists"]
          and "Probate Filed" not in crm.recs["p_twin"]["tags"],
          "a probate record at the same street marks the obituary record Probate Filed; it is never put on the Probate list itself")
    check(not [w for w in crm.writes if w[0] in ("status", "assign", "upsert")] and all(w[0] in ("add_tags", "remove_tags", "message") for w in crm.writes),
          "the pipeline writes tags and a note and nothing else: no status, no owner, no mark of a death")
    again = sweep.run("obituary_lane", crm=crm, now=T0 + timedelta(hours=2), pol=pol)
    check(len([b for b in ledger.batches(status="staged") if b["kind"] == obituary.KIND_MAIL]) == 1
          and again.tally.get("already staged, waiting on release") == 3, "a piece is not staged twice while its batch waits")

    print("  obituary mail: the gate and the release")
    crm.recs["o_month"]["lists"].append("Probate")              # a filing landed after the batch was staged
    bid = batch[0]["id"]
    check(_stock_release_refuses(bid, crm, pol) and not ledger.events("o_soft", "mail") and ledger.batch(bid)["status"] == "staged",
          "the ordinary mail release cannot book an obituary batch either: these letters are released by the obituary stage's own check")
    step1 = obituary.release(bid, crm=crm, pol=pol, commit=True, now=T0 + timedelta(days=1))
    check(step1["pieces"] == 2 and step1["held"] == {"a probate was filed: the record left the obituary lane": 1},
          "a piece is checked again at release: a probate filed in between takes it out of the file")
    done = obituary.release(bid, crm=crm, pol=pol, commit=True, uploaded=True, now=T0 + timedelta(days=1))
    soft = ledger.events("o_soft", "mail")
    check(done["sent"] == 2 and soft and soft[0]["detail"]["series"] == obituary.SOFT and soft[0]["usd"] == 3.0
          and any("the soft letter to the family" in m[1] for m in crm.msgs),
          "a released piece is booked as mail and noted on the record")
    later = sweep.run("obituary_lane", crm=crm, now=T0 + timedelta(days=20), pol=pol)
    items = [it for b in ledger.batches(status="staged") if b["kind"] == obituary.KIND_MAIL for it in ledger.batch(b["id"])["items"]]
    check(not any(it["record_uuid"] == "o_soft" for it in items) and later is not None, "the soft letter goes once")
    far = sweep.run("obituary_lane", crm=crm, now=T0 + timedelta(days=50), pol=pol)
    items = [it for b in ledger.batches(status="staged") if b["kind"] == obituary.KIND_MAIL for it in ledger.batch(b["id"])["items"]]
    by = {it["record_uuid"]: it for it in items}
    check(by.get("o_soft", {}).get("series") == obituary.MONTHLY and by["o_soft"]["letter"] == 1 and by.get("o_np", {}).get("letter") == 2 and far.batch,
          "after three months the same record moves onto the monthly mail, a month after its last piece")
    _wipe(store)
    crm = build()
    for i in range(12):
        ledger.event("old", "obituary_lane", "o_month", "mail", {"series": obituary.MONTHLY, "step": i + 1}, usd=1.25,
                     now=T0 - timedelta(days=400 - 30 * i), tz=pol.tz)
    twelve = sweep.run("obituary_lane", crm=crm, now=T0, pol=pol)
    check(twelve.tally.get("all 12 monthly pieces sent") == 1, "the monthly mail ends at twelve pieces")
    _wipe(store)
    crm = build()
    shut = sweep.run("obituary_lane", crm=crm, now=T0, pol=with_stage("obituary_lane", gate_allow=[]))
    check(not [b for b in ledger.batches() if b["kind"] == obituary.KIND_MAIL]
          and shut.tally.get("held back: the owner has died or may have (Obituary list); research first") == 4,
          "the hold lives in the gate: without the allow word every obituary piece is held, and the run says why")

    print("  obituary calls")
    _wipe(store)
    phone = [{"number": "8655550411", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}]
    crm = RCRM([ob("c_live", "80 Pine St", 120, phones=phone), ob("c_dead", "81 Pine St", 120, phones=phone),
                ob("c_wait", "82 Pine St", 120, phones=phone), ob("c_hold", "83 Pine St", 10, phones=phone)])
    _research(store, tmp, "c_live", "80-pine-st", {"owner_status": "Living", "must_sign": [], "call_order": []})
    _research(store, tmp, "c_dead", "81-pine-st", {"owner_status": "Deceased", "must_sign": [], "call_order": []})
    calls = sweep.run("obituary_lane", crm=crm, now=T0, pol=pol)
    check(crm.recs["c_live"]["assigned_to"] == "u-adriana" and all(crm.recs[u]["assigned_to"] is None for u in ("c_dead", "c_wait", "c_hold")),
          "an obituary record goes to a caller only once research has run and found the owner living")
    check(calls.tally.get("calls: waits for research") == 1 and calls.tally.get("calls: inside the first month, not handed out") == 1
          and calls.tally.get("calls: research did not find the owner living") == 1, "every record not handed out is counted with its reason")
    _wipe(store)
    crm = RCRM([ob("c_wait", "82 Pine St", 120, phones=phone), ob("c_hold", "83 Pine St", 10, phones=phone)])
    sweep.run("obituary_lane", crm=crm, now=T0, pol=with_stage("obituary_lane", calls="after_hold"))
    check(crm.recs["c_wait"]["assigned_to"] == "u-adriana" and crm.recs["c_hold"]["assigned_to"] is None,
          "the older rule is a setting: calls after the hold, and still never inside the first month")
    crm = RCRM([ob("c_wait", "82 Pine St", 120, phones=phone)])
    sweep.run("obituary_lane", crm=crm, now=T0, pol=with_stage("obituary_lane", calls="off"))
    check(crm.recs["c_wait"]["assigned_to"] is None, "and calls can be switched off altogether")
    _wipe(store)
    crm = RCRM([ob("c_a", "84 Pine St", 120, phones=phone), ob("c_b", "85 Pine St", 120, phones=phone), ob("c_c", "86 Pine St", 120, phones=phone)])
    capped = sweep.run("obituary_lane", crm=crm, now=T0, pol=with_stage("obituary_lane", calls="after_hold", max_assign=1))
    check(sum(1 for r in crm.recs.values() if r["assigned_to"]) == 1 and capped.tally.get("calls: left for the next run") == 2,
          "the caller's hand-out is capped a run: the rest wait and are counted, not opened")
    assert heirlane is not None


# ====================================================================== probate kept honest
def _probate(check, tmp, store, policy, pol, with_stage) -> None:
    from .autopilot import activity, ledger, probate, sweep
    print("  probate")
    check([probate.classify(x) for x in ("Wrong house: the PR's own home", "Wrong house: unrelated owner", "Dead: sold since mid-2025",
                                         "Work it: PR owns it and lives elsewhere", "Likely fine: name variant", "", "Something new")]
          == ["own_home", "unrelated", "sold", "inherited", "name_variant", "", "unknown"],
          "the audit's own suggestions map to five outcomes, and one it does not know is left for a person")
    audit = Path(tmp) / "probate" / "probate_address_audit_2099-01-01.csv"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "suggested", "pr_lives_there", "last_sold", "street", "uuid"])
        for u, label, street in (("pa_home", "Wrong house: the PR's own home", "201 Court St"), ("pa_other", "Wrong house: unrelated owner", "202 Court St"),
                                 ("pa_sold", "Dead: sold since mid-2025", "203 Court St"), ("pa_keep", "Work it: PR owns it and lives elsewhere", "204 Court St"),
                                 ("pa_name", "Likely fine: name variant", "205 Court St"), ("pa_lead", "Wrong house: the PR's own home", "206 Court St"),
                                 ("pa_fine", "", "207 Court St")):
            w.writerow(["x", label, "", "", street, u])

    def pr(u, street, **kw):
        kw.setdefault("tags", ["FTM", "probate"])
        kw.setdefault("lists", ["Probate"])
        return R(u, street, **kw)

    def build():
        no = pr("pn_due", "220 Court St", status="not_interested",
                phones=[{"number": "8655550501", "type": "MOBILE", "status": "CORRECT", "tags": ["Dial First"]},
                        {"number": "8655550502", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}])
        c = RCRM([pr("pa_home", "201 Court St"), pr("pa_other", "202 Court St"), pr("pa_sold", "203 Court St"), pr("pa_keep", "204 Court St"),
                  pr("pa_name", "205 Court St"), pr("pa_lead", "206 Court St", status="Warm Lead"), pr("pa_fine", "207 Court St"),
                  pr("pb_old", "210 Court St", probate_open_date="2024-06-01"), pr("pb_new", "211 Court St", probate_open_date="2026-01-15"),
                  pr("pb_tag", "212 Court St", tags=["FTM", "probate", "2024-08", "2026-02"]), pr("pb_none", "213 Court St"),
                  pr("pb_mo", "214 Court St", probate_open_date="2023-03-01", tags=["FTM", "probate", "Mail Only"]),
                  no, pr("pn_new", "221 Court St", status="not_interested", phones=[{"number": "8655550503", "status": "CORRECT", "tags": []}]),
                  pr("pn_nonum", "222 Court St", status="not_interested", phones=[{"number": "8655550504", "status": "UNKNOWN", "tags": ["Dial First"]}])])
        return c

    def fresh():
        """An empty memory, with the day each record said no already in the log copy (the copy
        reaches back months once the box has run a while; a first sync reaches 45 days)."""
        _wipe(store)
        activity.reset()
        activity._store([{"id": "rs-no-%s" % u, "event_type": "property.status.updated", "resource": u, "resource_type": "property",
                          "source": "internal-api", "author": "person@example.com", "timestamp": _ts(T0 - timedelta(days=days)),
                          "payload": {"property": {"status": ["prospecting", "not_interested"]}}}
                         for u, days in (("pn_due", 50), ("pn_new", 10), ("pn_nonum", 60))])
        return build()

    crm = fresh()
    plan = sweep.run("probate_honest", crm=crm, now=T0, pol=pol, plan=True)
    check(not crm.changed() and not ledger.batches() and "Would list 9 probate change(s)" in plan.head and "Nothing is changed without approval" in plan.head,
          "a planned probate run lists the changes and writes nothing: %s" % plan.head)
    live = sweep.run("probate_honest", crm=crm, now=T0, pol=pol)
    b = [x for x in ledger.batches(status="staged") if x["kind"] == probate.KIND]
    check(not crm.changed() and len(b) == 1 and b[0]["count"] == 9 and "Nothing was changed" in live.head,
          "never a silent edit: a live run stages the list for a person and changes nothing in the account")
    items = {(it["record_uuid"], it["part"]): it for it in ledger.batch(b[0]["id"])["items"]}
    check(items[("pa_home", "audit")]["action"] == "out" and items[("pa_other", "audit")]["action"] == "out"
          and items[("pa_sold", "audit")]["action"] == "sold" and items[("pa_keep", "audit")]["action"] == "keep"
          and items[("pa_name", "audit")]["action"] == "keep",
          "the split: the representative's own home and an unrelated owner's house come out, a house the estate sold is marked, an inherited house and a name variant stay")
    check(items[("pa_lead", "audit")]["action"] == "left" and "lead manager decides" in items[("pa_lead", "audit")]["why"]
          and ("pa_fine", "audit") not in items, "a flagged record that is a lead by now is left for the lead manager, and an unflagged one is not listed")
    check(items[("pb_old", "age")]["add_tags"] == ["Mail Only"] and "filed June 2024, 29 months ago" in items[("pb_old", "age")]["why"]
          and items[("pb_tag", "age")]["why"].endswith("(read from the notice month tag)") and ("pb_new", "age") not in items
          and ("pb_mo", "age") not in items,
          "a probate filed more than 24 months ago is listed for mail only, with the date it was read from; a newer one and one already mail only are not")
    check(("pb_none", "age") not in items and live.tally.get("no filing date on the record: left alone", 0) >= 1,
          "a probate with no filing date anywhere is left alone, never guessed old")
    task = items[("pn_due", "recall")]["task"]
    check(task["user"] == "Adriana" and task["title"] == "Call again: 220 Court St, Knoxville. Said no 50 days ago. Call only the confirmed number ending 0501."
          and ("pn_new", "recall") not in items and ("pn_nonum", "recall") not in items,
          "a probate that said no 45 days ago or more is listed for a re-call, for the prospecting caller, on the confirmed number only")
    check(live.tally.get("said no less than 45 days ago") == 1 and live.tally.get("said no, but no confirmed number to call again") == 1,
          "a no that is too recent, or has no confirmed number, gets no task")
    path = Path(tmp) / "probate" / ("changes_%d.csv" % b[0]["id"])
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    check(len(rows) == 9 and {r["action"] for r in rows} == {"comes out", "marked sold", "stays", "mail only", "re-call task", "left for a person"},
          "the list is written as a file a person can read before approving it")
    waiting = sweep.run("probate_honest", crm=crm, now=T0 + timedelta(days=1), pol=pol)
    check(len(ledger.batches()) == 1 and "still waiting on a person" in waiting.head and not crm.changed(),
          "while a list waits no second list is staged")
    dry = probate.release(b[0]["id"], crm=crm, pol=pol)
    check(dry["dry"] and dry["changes"] == 9 and not crm.changed() and ledger.batch(b[0]["id"])["status"] == "staged",
          "a dry approval only counts")
    check(_stock_release_refuses(b[0]["id"], crm, pol) and not crm.changed() and ledger.batch(b[0]["id"])["status"] == "staged",
          "the ordinary mail release cannot approve a probate list: it stops before it writes anything")
    res = probate.release(b[0]["id"], crm=crm, pol=pol, commit=True, now=T0 + timedelta(days=1))
    check("Wrong House" in crm.recs["pa_home"]["tags"] and "FTM" not in crm.recs["pa_home"]["tags"] and "Probate" not in crm.recs["pa_home"]["lists"]
          and "Wrong House" in crm.recs["pa_other"]["tags"],
          "approved: a wrong house leaves the first-to-market lane and the Probate list and is tagged for what it is")
    check("Estate Sold" in crm.recs["pa_sold"]["tags"] and not any("recently sold" in " ".join(r["tags"]).lower() for r in crm.recs.values())
          and crm.recs["pa_sold"]["status"] is None,
          "a house the estate sold gets its own tag; the recently sold tag and the Sold status are never written here")
    check(crm.recs["pa_keep"]["tags"] == ["FTM", "probate"] and crm.recs["pa_keep"]["lists"] == ["Probate"]
          and any(m[0] == "pa_keep" and "most likely inherited" in m[1] for m in crm.msgs),
          "an inherited house stays exactly as it was, with a note that says why")
    check(crm.recs["pa_lead"]["tags"] == ["FTM", "probate"] and crm.recs["pa_lead"]["status"] == "Warm Lead"
          and not any(m[0] == "pa_lead" for m in crm.msgs), "a lead is not changed by the list")
    check("Mail Only" in crm.recs["pb_old"]["tags"] and "Mail Only" in crm.recs["pb_tag"]["tags"] and "Mail Only" not in crm.recs["pb_new"]["tags"],
          "an old probate drops to mail only")
    check(len(crm.tasks) == 1 and crm.tasks[0]["assigned_to_users"] == ["u-adriana"] and crm.tasks[0]["assigned_to_property"] == "pn_due"
          and crm.recs["pn_due"]["status"] == "not_interested",
          "the re-call is one task on the record for the prospecting caller; the record's status is not touched")
    check(res["results"].get("comes out") == 2 and res["results"].get("left for a person") == 1 and ledger.batch(b[0]["id"])["status"] == "sent",
          "the approval reports what it did, by outcome")
    ch = ledger.changes(record_uuid="pa_home")
    check({c["what"] for c in ch} == {"add_tags", "remove_tags", "remove_lists"} and [c for c in ch if c["what"] == "remove_tags"][0]["before"] == ["FTM"],
          "every approved change is recorded with the value it replaced")
    check(probate.flagged_left(audit) == (6, 0) and len(ledger.events("pa_keep", probate.KIND)) == 1
          and ledger.events("pa_keep", probate.KIND)[0]["detail"]["why"],
          "the flagged count goes to zero, and each record's reason is in the ledger")
    after = sweep.run("probate_honest", crm=crm, now=T0 + timedelta(days=2), pol=pol)
    check("nothing to settle" in after.head and not ledger.batches(status="staged") and after.tally.get("flagged and not settled yet") == 0,
          "a settled record is not listed again")
    crm.tasks[0]["completed"] = None
    soon = sweep.run("probate_honest", crm=crm, now=T0 + timedelta(days=30), pol=pol)
    check(not ledger.batches(status="staged") and soon.tally.get("the last re-call task is still open") == 1,
          "a re-call that is still open is never doubled")
    crm.tasks[0]["completed"] = "2026-12-01T15:00:00Z"
    repeat = sweep.run("probate_honest", crm=crm, now=T0 + timedelta(days=47), pol=pol)
    b2 = [x for x in ledger.batches(status="staged") if x["kind"] == probate.KIND]
    again_items = ledger.batch(b2[0]["id"])["items"] if b2 else []
    check(len(b2) == 1 and sorted((it["record_uuid"], it["part"]) for it in again_items) == [("pn_due", "recall"), ("pn_new", "recall")]
          and repeat.batch and not crm.tasks[1:],
          "each repeat is timed from the last re-call: 45 days after it the record is listed again, and again for approval")

    print("  probate settings and refusals")
    crm = fresh()
    harsh_pol = with_stage("probate_honest", audit_actions={"inherited": "out"}, mail_only_months=36, recall_days=90)
    harsh = sweep.run("probate_honest", crm=crm, now=T0, pol=harsh_pol)
    hb = [x for x in ledger.batches(status="staged") if x["kind"] == probate.KIND]
    hitems = {(it["record_uuid"], it["part"]): it for it in ledger.batch(hb[0]["id"])["items"]} if hb else {}
    check(harsh.tally.get("said no less than 90 days ago") == 3 and len(hitems) == 6 and hitems[("pa_keep", "audit")]["action"] == "out"
          and not [k for k in hitems if k[1] != "audit"],
          "the split, the age and the wait are settings: an undecided decision ships as its recommended value and can be changed")
    crm = fresh()
    not_her = sweep.run("probate_honest", crm=crm, now=T0, pol=with_stage("probate_honest", not_callers=["Adriana"]), plan=True)
    check(any("no prospecting caller" in p for p in not_her.problems) and "re-call task" not in not_her.head,
          "the re-call never goes to a person the playbook keeps off it (the lead manager): with nobody else, none is listed and the run says so")
    crm = fresh()
    missing = sweep.run("probate_honest", crm=crm, now=T0, pol=with_stage("probate_honest", audit_csv=str(Path(tmp) / "nope.csv")), plan=True)
    check(any("No address audit file" in ln for ln in missing.lines) and "Would list 3" in missing.head,
          "with no audit file on the box the split waits for it and says so; the age rule and the re-calls still run")
    crm = fresh()
    other = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    other.stages["second_chance"] = policy.Stage(id="second_chance", kind="secondchance", owner="analysis", mode="plan", summary="",
                                                 schedule=[], presets=[], params={"intervals": {"probate": 30}})
    hand = sweep.run("probate_honest", crm=crm, now=T0, pol=other, plan=True)
    check("re-call task" not in hand.head and any("called back by the second_chance stage" in ln for ln in hand.lines),
          "when the playbook runs a second-chance stage the re-calls are left to it, so a caller is never handed the same owner twice")
    try:
        from .autopilot import secondchance as _sc
        known = callable(getattr(_sc, "interval_for", None))
    except Exception:  # noqa: BLE001
        known = False
    if known:
        check(any("after 30 days; this lane's rule is 45" in p for p in hand.problems),
              "and its wait for a probate is checked against the one decided here")
    else:
        check(not any("lane's rule" in p for p in hand.problems), "with no second-chance module in this build only the handover is said")
    crm = fresh()
    ledger.set_fact("pn_due", "second_chance", {"task": "t-1", "created": "2026-11-01T00:00:00+00:00"})
    seen = sweep.run("probate_honest", crm=crm, now=T0, pol=pol, plan=True)
    check(seen.tally.get("already handled by the second-chance stage") == 1 and "re-call task" not in seen.head,
          "a record the second-chance stage already wrote a task for is not listed again here")


# ====================================================================== paid lookups
def _paid(check, tmp, store, policy, pol, with_stage) -> None:
    from .autopilot import ledger, paidlookup, sweep
    print("  paid lookups")

    def build():
        dead = R("l_dead", "307 Lookup St", tags=["PL", "FTM", "Deep Prospected"], deceased=True, equity_percent="90", estimate_value="300000")
        c = RCRM([R("l_hot", "301 Lookup St", tags=["PL", "FTM", "Deep Prospected"], lists=["Foreclosure"]),
                  R("l_eq", "302 Lookup St", tags=["PL", "FTM", "Deep Prospected"], equity_percent="80.00", estimate_value="300000.00"),
                  R("l_low", "303 Lookup St", tags=["PL", "FTM", "Deep Prospected"], equity_percent="30", estimate_value="300000"),
                  R("l_untried", "304 Lookup St", tags=["PL", "FTM", "Deep Prospected"], equity_percent="90", estimate_value="300000"),
                  R("l_nores", "305 Lookup St", tags=["PL", "FTM"], equity_percent="90", estimate_value="300000"),
                  R("l_phone", "306 Lookup St", tags=["PL", "FTM", "Deep Prospected"], equity_percent="90", estimate_value="300000",
                    phones=[{"number": "8655550601", "type": "MOBILE", "status": "UNKNOWN", "tags": ["Dial First"]}]),
                  dead])
        c.custom = {"l_hot": {"Foreclosure Date": _day(12)}}
        for u in ("l_hot", "l_eq", "l_low", "l_nores", "l_phone", "l_dead"):
            for src in ("smartskip", "tracerfy"):
                ledger.event("old", "ftm_retrace", u, "trace", {"source": src, "found": 0}, now=T0 - timedelta(days=9), tz=pol.tz)
        ledger.event("old", "ftm_retrace", "l_untried", "trace", {"source": "smartskip", "found": 0}, now=T0 - timedelta(days=9), tz=pol.tz)
        return c

    _wipe(store)
    crm = build()
    plan = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol, plan=True)
    check(not crm.changed() and plan.acted == 2 and "would add 2" in plan.head, "a planned lookup run tags nothing: %s" % plan.head)
    t = plan.tally
    check(t.get("not worth a paid search (no sale close, equity under the floor)") == 1
          and t.get("not yet: not every paid trace has been tried yet") == 1 and t.get("not yet: research has not run yet") == 1
          and t.get("has a number a caller may dial") == 1 and any("research first" in k for k in t),
          "a record gets in only when every source missed it and it is worth the money; a dead owner's record is held for research")
    check(plan.lines[0].endswith("sale in 12 days") and "$240,000 of equity (80%)" in plan.lines[1],
          "the queue is worked sale date first, then the most equity, each with its reason")
    live = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol)
    check("Paid Lookup" in crm.recs["l_hot"]["tags"] and "Paid Lookup" in crm.recs["l_eq"]["tags"]
          and not any("Paid Lookup" in crm.recs[u]["tags"] for u in ("l_low", "l_untried", "l_nores", "l_phone", "l_dead")),
          "a live run tags the records that are worth a paid search, and no others")
    check(len(crm.tasks) == 1 and crm.tasks[0]["title"] == "Paid lookups: 2 record(s) in the preset Paid Lookup Queue"
          and crm.tasks[0]["assigned_to_users"] == ["u-dana"] and "assigned_to_property" not in crm.tasks[0],
          "one task a week tells the assistant which preset to open and how many records wait")
    again = sweep.run("paid_lookups", crm=crm, now=T0 + timedelta(days=1), pol=pol)
    check(len(crm.tasks) == 1 and again.acted == 0 and "2 waiting" in again.head, "the task is written once a week and a queued record is not queued twice")
    crm.recs["l_hot"]["owner"]["phones"] = [{"number": "8655550611", "type": "MOBILE", "status": "UNKNOWN", "tags": []}]
    crm.recs["l_eq"]["tags"].append("Paid Lookup Miss")
    result = sweep.run("paid_lookups", crm=crm, now=T0 + timedelta(days=2), pol=pol)
    check("Paid Lookup" not in crm.recs["l_hot"]["tags"] and "Paid Lookup" not in crm.recs["l_eq"]["tags"]
          and result.tally.get("came off the queue: a number was found") == 1 and result.tally.get("came off the queue: the search found nothing") == 1,
          "a record leaves the queue when a number is added to it, or when the assistant marks the search a miss")
    check(paidlookup.hit_rate() == (1, 2) and "1 of 2 searches found a number" in result.head, "the run reports the queue length and the hit rate")
    crm.recs["l_eq"]["tags"].remove("Paid Lookup Miss")
    once = sweep.run("paid_lookups", crm=crm, now=T0 + timedelta(days=3), pol=pol)
    check("Paid Lookup" not in crm.recs["l_eq"]["tags"] and once.acted == 0, "a record is searched once: a miss is not queued again")
    next_week = sweep.run("paid_lookups", crm=crm, now=T0 + timedelta(days=7), pol=pol)
    check(len(crm.tasks) == 1 and next_week is not None, "an empty queue writes no task")

    _wipe(store)
    crm = build()
    short = sweep.run("paid_lookups", crm=crm, now=T0, pol=with_stage("paid_lookups", max_queue=1))
    check("Paid Lookup" in crm.recs["l_hot"]["tags"] and "Paid Lookup" not in crm.recs["l_eq"]["tags"]
          and short.tally.get("worth a search, waiting for room in the queue") == 1,
          "the queue is short: with room for one, the sale goes in and the equity record waits")
    _wipe(store)
    crm = build()
    nobody = sweep.run("paid_lookups", crm=crm, now=T0, pol=with_stage("paid_lookups", assistant=None))
    check(not crm.tasks and any("No assistant is named" in ln for ln in nobody.lines) and "Paid Lookup" in crm.recs["l_hot"]["tags"],
          "until the playbook names the assistant no task is written, and the run says so")
    _wipe(store)
    crm = build()
    saved = PRESETS.pop("Paid Lookup Queue")
    try:
        nopreset = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol, plan=True)
        check(any("no preset titled 'Paid Lookup Queue'" in p for p in nopreset.problems),
              "the preset the assistant opens has to exist in the account, and a missing one is said out loud")
    finally:
        PRESETS["Paid Lookup Queue"] = saved
    _wipe(store)
    crm = build()
    crm.recs["l_hot"]["tags"].append("Paid Lookup")
    ledger.event("old", "paid_lookups", "l_hot", paidlookup.ACTION, {"add": True, "why": "sale", "numbers": []}, now=T0 - timedelta(days=3), tz=pol.tz)
    crm.recs["l_hot"]["status"] = "Hot Lead"
    gone = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol)
    check("Paid Lookup" not in crm.recs["l_hot"]["tags"] and gone.tally.get("came off the queue: a lead now") == 1 and paidlookup.hit_rate() == (0, 0),
          "a queued record that became a lead comes off the queue, and is neither a hit nor a miss")
    _wipe(store)
    crm = build()
    crm.custom["l_eq"] = {"Foreclosure Date": _day(-3)}                  # heavy equity, but the sale was three days ago
    crm.recs["l_low"].update(equity_percent="90", mls="Pending")          # heavy equity, but under contract
    stale = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol)
    check("Paid Lookup" not in crm.recs["l_eq"]["tags"] and "Paid Lookup" not in crm.recs["l_low"]["tags"]
          and stale.tally.get("not worth a paid search: the sale date passed") == 1
          and stale.tally.get("not worth a paid search: listed or pending on the MLS") == 1 and "Paid Lookup" in crm.recs["l_hot"]["tags"],
          "a house whose sale date passed or that is under contract is not worth a paid search, whatever its equity reads")
    waited = sweep.run("paid_lookups", crm=crm, now=T0 + timedelta(days=13), pol=pol)
    check("Paid Lookup" not in crm.recs["l_hot"]["tags"] and waited.tally.get("came off the queue: the sale date passed") == 1
          and paidlookup.hit_rate() == (0, 0),
          "a record whose sale date passes while it waits comes off the queue, and is neither a hit nor a miss")
    _wipe(store)
    crm = build()
    real_rules = paidlookup.researchqueue.batch_rules
    paidlookup.researchqueue.batch_rules = lambda: None
    try:
        blind = sweep.run("paid_lookups", crm=crm, now=T0, pol=pol)
        check(not crm.changed() and any("did not load" in p for p in blind.problems),
              "a box that cannot load the house rules adds nothing to the paid queue: a house nobody checked is not searched")
    finally:
        paidlookup.researchqueue.batch_rules = real_rules
