"""The KPI tracker (Ty, 2026-11-16): one snapshot three times a weekday, twelve tabs and one short Slack
update. Zero network: events seeded into the log copy, a fake records search, its own database and data
folder, and a fake (or absent) kpisinks module, so nothing here can reach a sheet or a channel. Every
id, name, street and number is invented; numbers use the never-assigned 865-000 exchange.

Every act the tracker counts is read through the KPI sources (agents/kpi_sources). The same fixture is
also counted the way the tracker counted before them, straight off the log copy with the shared helpers,
and the two must agree number for number while only the built-in sources are connected. Then a texting
export, a dialer file that disagrees with the log, a source that cannot be read, the agency's own files
and a log copy that is behind are each connected in turn: the gaps fill, and an issue is said, never a
changed number.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import types
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import config
from .selftest_autopilot import EMPLOYEES

TZ = "America/New_York"
ZONE = ZoneInfo(TZ)
NOON = datetime(2026, 11, 18, 17, 35, tzinfo=timezone.utc)        # Wednesday 12:35pm Eastern
MORNING = datetime(2026, 11, 18, 12, 50, tzinfo=timezone.utc)     # Wednesday 7:50am Eastern
EVENING = datetime(2026, 11, 19, 0, 35, tzinfo=timezone.utc)      # Wednesday 7:35pm Eastern
SCHEDULE = ["07:45 Mon-Fri", "12:30 Mon-Fri", "19:30 Mon-Fri"]
LINE = "8650000555"                                                # a mail tracking line
MISSING = object()
SYNCED = "2026-11-19T01:00:00+00:00"                               # the log copy, read through after the evening run
ENV_KEYS = ("AGENTS_KPI_TZ", "AGENTS_KPI_AGREE", "AGENTS_KPI_CALL_MATCH_S", "AGENTS_KPI_TEXT_MATCH_S",
            "AGENTS_KPI_MARGIN_HOURS", "AGENTS_KPI_SILENT_MIN", "AGENTS_KPI_RAW_DAYS", "AGENTS_KPI_EVENT_DAYS",
            "AGENTS_KPI_ECHO_MINUTES", "AGENTS_KPI_LOG_SLACK_MINUTES", "AGENTS_AGENCY_RETURNS_DIR")


# ---------------------------------------------------------------------- the log, as the account writes it
def _call(i, ts, kind, owner, number, duration=0, caller="", direction="outbound", line="8650000001"):
    inbound = direction == "inbound"
    call = {"direction": direction, "origin_number": number if inbound else line,
            "destination_number": line if inbound else number, "duration": duration, "uuid": "call-%s" % i}
    if caller:
        call["user"] = {"first_name": caller, "uuid": "u-" + caller.lower()}
    return {"id": "kt-c%s" % i, "timestamp": ts, "event_type": "owner.call.%s" % kind, "resource": owner,
            "source": "kixie", "payload": {"call": call}}


def _status(i, ts, record, old, new):
    return {"id": "kt-s%s" % i, "timestamp": ts, "event_type": "property.status.updated", "resource": record,
            "source": "internal-api", "author": "person@example.com", "payload": {"property": {"status": [old, new]}}}


def _mark(i, ts, owner, number, to):
    return {"id": "kt-p%s" % i, "timestamp": ts, "event_type": "owner.phone.status.updated", "resource": owner,
            "source": "kixie", "payload": {"owner": {"phone": number, "status": ["UNKNOWN", to]}}}


def _task(i, ts, record, kind="call", due="", done=False):
    et = "task.completed" if done else "task.created"
    return {"id": "kt-t%s%s" % (i, "d" if done else ""), "timestamp": ts, "event_type": et, "resource": "task-%s" % i,
            "source": "internal-api", "payload": {"task": {"assigned_to_property": record, "title": "Call",
                                                            "event_type": kind, "due_date": due}}}


def _text(i, ts, owner):
    return {"id": "kt-x%s" % i, "timestamp": ts, "event_type": "owner.sms.received", "resource": owner,
            "payload": {"sms": {"direction": "inbound", "origin_number": "8650000105", "destination_number": "8650000001"}}}


def _seed_log() -> None:
    from .autopilot import activity
    activity._store([
        # October: where the log copy starts
        _call(1, "2026-10-06 15:00:00", "made", "o20", "8650000120", caller="Emerson"),
        _call(2, "2026-10-06 15:01:00", "answered", "o20", "8650000120", duration=65, caller="Emerson"),
        _status(1, "2026-10-07 15:10:00", "r20", "", "new_lead"),
        {"id": "kt-g1", "timestamp": "2026-10-08 15:00:00", "event_type": "property.tags.added", "resource": "r20",
         "payload": {"tags": ["Deep Prospected"]}},
        # last week, Nov 9 to 15
        _call(3, "2026-11-09 15:00:00", "made", "o10", "8650000110", caller="Emerson"),
        _call(4, "2026-11-09 15:01:00", "answered", "o10", "8650000110", duration=80, caller="Emerson"),
        _call(5, "2026-11-09 16:00:00", "made", "o11", "8650000111", caller="Emerson"),
        _status(2, "2026-11-10 15:05:00", "r10", "", "new_lead"),
        _task(1, "2026-11-11 16:00:00", "r10", kind="appointment", due="2026-11-14"),
        _status(3, "2026-11-05 15:00:00", "r11", "", "new_lead"),
        _status(4, "2026-11-12 15:00:00", "r11", "new_lead", "under_contract"),
        _status(5, "2026-11-13 15:00:00", "r11", "under_contract", "closed"),
        _status(10, "2026-11-10 14:00:00", "r7", "", "new_lead"),
        # Monday Nov 16
        _status(6, "2026-11-16 15:05:00", "rb5", "", "new_lead"),                       # a board record became a lead
        _call(6, "2026-11-16 15:00:00", "received", "o12", "8650000112", duration=90, direction="inbound", line=LINE),
        _status(7, "2026-11-16 15:05:00", "r12", "", "new_lead"),                       # a call back on the mail line
        _call(20, "2026-11-16 15:29:00", "made", "ob1", "8650000131", caller="Emerson"),
        _call(21, "2026-11-16 15:30:00", "answered", "ob1", "8650000131", duration=75, caller="Emerson"),
        # Tuesday Nov 17: two callers, and a dial with no name
        _call(7, "2026-11-17 15:00:00", "made", "o6", "8650000106", caller="Emerson"),
        _call(8, "2026-11-17 15:01:00", "answered", "o6", "8650000106", duration=120, caller="Emerson"),
        _call(9, "2026-11-17 15:05:00", "made", "o7", "8650000107", caller="Sloane"),
        _call(10, "2026-11-17 15:06:00", "answered", "o7", "8650000107", duration=30, caller="Sloane"),
        _call(11, "2026-11-17 15:10:00", "made", "o8", "8650000108"),
        _status(8, "2026-11-17 15:02:00", "r6", "", "new_lead"),
        _status(9, "2026-11-17 15:30:00", "r6", "new_lead", "Hot Lead"),
        _status(11, "2026-11-17 20:00:00", "r7", "new_lead", "under_contract"),
        _status(14, "2026-11-17 16:00:00", "r14", "", "new_lead"),
        _call(22, "2026-11-17 16:09:00", "made", "o14", "8650000114", caller="Emerson"),      # 9 minutes to the first call
        # Wednesday Nov 18, the morning block (dials carry no name; one caller answers)
        _call(12, "2026-11-18 14:05:00", "made", "o1", "8650000101"),
        _call(13, "2026-11-18 14:06:00", "answered", "o1", "8650000101", duration=95, caller="Emerson"),
        _mark(1, "2026-11-18 14:07:00", "o1", "8650000101", "CORRECT"),
        _status(12, "2026-11-18 14:08:00", "r1", "", "new_lead"),
        _call(14, "2026-11-18 14:10:00", "made", "o2", "8650000102"),
        _call(15, "2026-11-18 14:11:00", "answered", "o2", "8650000102", duration=20, caller="Emerson"),
        _mark(2, "2026-11-18 14:12:00", "o2", "8650000102", "WRONG"),
        _call(16, "2026-11-18 14:20:00", "made", "o3", "8650000103"),
        _call(17, "2026-11-18 14:21:00", "noanswer", "o3", "8650000103", caller="Emerson"),
        _mark(3, "2026-11-18 14:22:00", "o3", "8650000103", "DNC"),
        _text(1, "2026-11-18 15:00:00", "o5"),
        _status(13, "2026-11-18 15:30:00", "r9", "", "new_lead"),
        _task(2, "2026-11-18 16:00:00", "r1", kind="appointment"),
        _call(18, "2026-11-18 16:40:00", "made", "o4", "8650000104"),
        _call(19, "2026-11-18 16:41:00", "answered", "o4", "8650000104", duration=70, caller="Emerson"),
        # tasks on the leads
        _task(3, "2026-11-16 14:00:00", "rL2", due="2026-11-17"),        # overdue
        _task(4, "2026-11-17 14:00:00", "rL3", due="2026-11-18"),        # due today
        _task(5, "2026-11-15 14:00:00", "rL1", due="2026-11-16"),        # done
        _task(5, "2026-11-16 15:00:00", "rL1", done=True),
        _task(7, "2026-11-10 14:00:00", "r99", due="2026-11-10"),        # not a lead: never the lead manager's
        {"id": "kt-k1", "timestamp": "2026-11-16 14:30:00", "event_type": "card.moved", "resource": "card-1",
         "payload": {"card": {"prop": "rL2"}, "extra_info": {"card": {"prop": "rL2", "column": ["New Lead", "HOT Leads"],
                                                                       "board": "Lead Management"}}}},
    ], "")
    _synced(SYNCED)


def _synced(through: str) -> None:
    """How far the log copy reaches, as the stages' syncs record it: the DataSift source says so when it is short."""
    from . import store
    for g in ("calls", "texts", "state", "board"):
        store.kv_set("autopilot:log:%s:through" % g, through)


def _seed_tables() -> None:
    from .autopilot import alarms, capture, dealsource, ftmpipeline, kpi, lanes, ledger
    for owner, lane, recs in (("o1", "ftm", {"r1": "ftm"}), ("o2", "top1000", {"r2": "top1000"}),
                              ("o6", "lead", {"r6": "lead"}), ("o9", "lead", {"r9": "lead"}),
                              ("o10", "lead", {"r10": "lead"}), ("o12", "lead", {"r12": "lead"}),
                              ("o7", "lead", {"r7": "lead"}), ("o11", "lead", {"r11": "lead"}),
                              ("o14", "lead", {"r14": "lead"}), ("ob5", "lead", {"rb5": "lead"}),
                              ("o20", "agency", {"r20": "agency", "r21": "agency"})):
        lanes.set_owner_lane(owner, lane, recs)
    for rec, owner in (("r1", "o1"), ("r9", "o9"), ("r12", "o12"), ("rb5", "ob5"), ("r6", "o6"), ("r14", "o14")):
        ledger.set_fact(rec, "lead_owner", {"owner": owner, "street": "%s Sample Rd" % rec[1:]})
    for rec, col, due, owner, lane in (("rb1", "Day 1", "2026-11-18", "ob1", "ftm"), ("rb2", "Day 3", "2026-11-19", "ob2", "obituary"),
                                       ("rb3", "Research", "", "ob3", "obituary"),
                                       ("rb4", "Exhausted", "2026-11-10", "ob4", "hot_auction")):
        ledger.set_fact(rec, ftmpipeline.PLAN_FACT, {"column": col, "due": due, "owner": owner, "lane": lane, "card": "plan-" + rec})
    ledger.set_fact("rb5", ftmpipeline.PLAN_FACT, None)                 # a card the board took off
    ledger.set_fact("ra1", "auction_dates", {"foreclosure_date": "2026-11-25"})
    ledger.set_fact("ra2", "auction_dates", {"foreclosure_date": "2026-12-20"})
    # the mail lane's own memory: a staged batch, a released one, two pieces the ledger logged
    ledger.batch_create("ftm_mail", "mail", [{"record_uuid": "m1", "piece": "handwritten_letter", "step": 1},
                                             {"record_uuid": "m2", "piece": "handwritten_letter", "step": 1}], 6.0)
    sent = ledger.batch_create("ftm_mail", "mail", [{"record_uuid": "m3", "piece": "family_postcard", "step": 2}], 0.75)
    ledger.batch_set(sent, "sent", {"sent": 1})
    ledger.event("run-a", "ftm_mail", "m4", "mail", {"piece": "handwritten_letter", "step": 1}, usd=3.0,
                 now=datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc), tz=TZ)
    ledger.event("run-b", "ftm_mail", "m3", "mail", {"piece": "family_postcard", "step": 2}, usd=0.75,
                 now=datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc), tz=TZ)
    ledger.event("run-c", "ftm_retrace", "r30", "trace", {"source": "tracerfy"}, usd=0.15,
                 now=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc), tz=TZ)
    ledger.batch_create("obituary_lane", "obituary_mail", [{"record_uuid": "ob-m1", "series": "soft", "letter": 1,
                                                            "piece": "handwritten_letter", "price": 1.89}], 1.89)
    dealsource.save({"record_uuid": "r11", "closed_at": "2026-11-13T15:00:00+00:00", "closed_day": "2026-11-13",
                     "street": "11 Sample Rd", "lane": "first-to-market", "list": "Foreclosure", "channel": "mail",
                     "source": "not interested follow-up", "days": 20, "generated_by": "Emerson", "carried_by": "Emerson",
                     "closed_by": "Sloane", "detail": {"fee": 0}})
    # the daily capture's stored Tuesday, as the 19:15 stage wrote it
    start = kpi.day_start("2026-11-17", TZ)
    for who, r in capture.day_rows(start, start + (kpi.day_start("2026-11-18", TZ) - start), 60).items():
        capture.save("2026-11-17", who, r)
    a = alarms.Alarm(code="no_lead", subject="the account", what="520 dials since the last new lead", check="Check the list.",
                     win="since the last lead")
    alarms.raise_once("2026-11-18", a, a.text(), EVENING)
    b = alarms.Alarm(code="answer_rate", subject="Emerson", what="answer rate 30% for Emerson; look at the caller ID lines",
                     check="", win="today")
    alarms.raise_once("2026-11-18", b, b.text(), EVENING)


# ---------------------------------------------------------------------- a fake records search
class FakeCRM:
    """Counts by what the filter holds. Remembers every filter it was asked, so a test can see that a live
    count was the clean tab and that the cap held."""
    calls = 0
    LISTS = {"foreclosure": "L-fc", "probate": "L-pr", "condemned": "L-co", "obituary": "L-ob"}   # no Liens list
    TOTAL = {"L-fc": 120, "L-pr": 80, "L-co": 10, "L-ob": 300, "T-ftm": 500, "T-heir identified": 12}
    WEEK = {"L-fc": 3, "L-pr": 2, "L-co": 0, "L-ob": 5}
    MONTH = {"L-fc": 9, "L-pr": 6, "L-co": 1, "L-ob": 20}
    MAILED = {"T-ftm": 120, "L-ob": 40, "T-heir identified": 0}
    MAILED_MONTH = {"T-ftm": 15, "L-ob": 4, "T-heir identified": 0}
    PRESETS = {"Priority - 01 Ready to Text": 41, "Priority - 02 Ready to Call": 87}

    def __init__(self, broken_lists: bool = False):
        self.musts = []
        self.broken_lists = broken_lists

    def preset_must(self, title):
        if title not in self.PRESETS:
            raise KeyError("no preset titled %r in this account" % title)
        return {"_preset": title, "last_direct_mailed": ["36-months", "month"]}

    def count(self, must):
        self.musts.append(copy.deepcopy(must))
        if "_preset" in must:
            return self.PRESETS[must["_preset"]]
        key = (must.get("any_lists") or must.get("any_tags") or [""])[0]
        if "created" in must:
            return (self.WEEK if must["created"][0] == "2026-11-16" else self.MONTH).get(key, 0)
        if "directmail_attempts" in must:
            return self.MAILED.get(key, 0)
        if "last_direct_mailed" in must:
            return self.MAILED_MONTH.get(key, 0)
        return self.TOTAL.get(key, 0)

    def rows(self, must, cap=10000):
        self.musts.append({"lead_rows": True})
        return [{"uuid": "rL1", "status": "new_lead", "tasks": {"incomplete": 0}, "owner": {"uuid": "oL1"}},
                {"uuid": "rL2", "status": "Hot Lead", "tasks": {"incomplete": 1}, "owner": {"uuid": "o-hot"}},
                {"uuid": "rL3", "status": "Warm Lead", "tasks": {"incomplete": 2}, "owner": {"uuid": "o6"}},
                {"uuid": "rL4", "status": "under_contract", "tasks": {"incomplete": 0}, "owner": {"uuid": "oL4"}}]

    def list_uuid(self, title):
        if self.broken_lists:
            raise RuntimeError("the list index answered 502")
        return self.LISTS.get(title.strip().lower(), "")

    def tag_uuid(self, title):
        return "T-" + title.strip().lower()


# ---------------------------------------------------------------------- the playbook and the catalog
PLAYBOOK = {
    "business": {"tz": TZ, "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson", "Sloane"]},
    "stages": {
        "ftm_mail": {"kind": "mail", "owner": "ops", "mode": "plan", "schedule": "10:00 Mon-Fri", "lane": "ftm",
                     "vendor": "csv", "send": "staged",
                     "pieces": [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "handwritten_letter"},
                                {"preset": "FTM - Mail 02 Family Postcard", "piece": "family_postcard"}]},
        "obituary_lane": {"kind": "obituary", "owner": "research", "mode": "plan", "schedule": "09:40 Mon-Fri",
                          "presets": ["Obituary - All"]},
        "top1000_attempts": {"kind": "attempts", "owner": "ops", "mode": "plan", "schedule": "every 60 min",
                             "lane": "top1000", "presets": ["Priority - 01 Ready to Text", "Priority - 02 Ready to Call"]},
        "deal_sources": {"kind": "dealsource", "owner": "ops", "mode": "plan", "schedule": "03:10 daily",
                         "mail_tracking_lines": [LINE], "researched_tags": ["Deep Prospected"]},
        "agency_scorecard": {"kind": "agencyscore", "owner": "ops", "mode": "plan", "schedule": "8th 08:10",
                             "monthly_fee": 1800},
        "ftm_auction": {"kind": "auction", "owner": "analysis", "mode": "plan", "schedule": "07:45 daily", "presets": [],
                        "scope_tags": ["FTM"], "critical_days": 14},
    },
}
# The two campaigns timed from a date (stage kind mailcadence), added to the playbook for their own check:
# pieces keyed by id, one the SiftMail account cannot order (product 18), a SiftMail floor of 20.
CADENCE = {
    "auction_mail": {"kind": "mailcadence", "owner": "ops", "mode": "plan", "schedule": "09:45 Mon-Fri", "lane": "ftm",
                     "anchor": "sale_date", "vendor": "siftmail", "internal_routes": True, "send": "staged",
                     "scope": {"lists": ["Foreclosure"], "tags": ["FTM"]}, "min_batch": 20,
                     "clock": {"fill": {"first": "A1", "middle": ["A2", "A3"], "last": ["A4"]}},
                     "prices": {"handwritten_letter": 1.89, "options_letter": 1.23, "snap_pack": 1.28, "postcard": 0.74},
                     "pieces": [{"id": "A1", "piece": "handwritten_letter", "product_id": 16, "envelope": 16, "design": ""},
                                {"id": "A2", "piece": "options_letter", "product_id": 5, "envelope": 5, "design": ""},
                                {"id": "A3", "piece": "snap_pack", "product_id": 18, "design": "", "fallback": "A4"},
                                {"id": "A4", "piece": "postcard", "product_id": 14, "design": ""}]},
    "probate_mail": {"kind": "mailcadence", "owner": "ops", "mode": "plan", "schedule": "10:15 Mon-Fri", "lane": "ftm",
                     "anchor": "filed_date", "vendor": "siftmail", "internal_routes": True, "send": "staged",
                     "scope": {"lists": ["Probate"], "tags": ["FTM"], "not_tags": ["Wrong House"]},
                     "require": "decedent_house", "min_batch": 20,
                     "clock": {"steps": [{"piece": "PB1", "at": "on_entry"}, {"piece": "EVG_T4", "after_days": 14}],
                               "after_month_12": {"every_days": 75, "rotate": ["EVG_T4"], "until_month": 24}},
                     "prices": {"letter": 1.10, "postcard": 0.74},
                     "pieces": [{"id": "PB1", "piece": "letter", "product_id": 5, "envelope": 5, "design": ""},
                                {"id": "EVG_T4", "piece": "postcard", "product_id": 14, "design": ""}]},
}


def _catalog(drop: str = ""):
    """The real catalog file with the figures these checks count on pinned, and no typed costs, so a figure
    the owner changes later cannot move a check."""
    from .autopilot import kpi
    data = yaml.safe_load(kpi.path().read_text(encoding="utf-8"))
    f = data["figures"]
    f["goal.revenue_per_year"].update(value=360000)
    f["goal.average_deal"].update(value=15000)
    f["funnel.contracts_that_close"].update(value=75)
    for key, lo, hi in (("funnel.leads_per_deal", 5, 15), ("funnel.dials_per_lead", 50, 100),
                        ("caller.conversations_per_day", 20, 25)):
        f[key].pop("value", None)
        f[key].update(low=lo, high=hi)
    f["caller.dials_per_day"].update(value=150)
    f["goal.at_risk_share"].update(value=80)
    data["inputs"] = {"monthly_costs": {"dialer": None, "texting": None, "salaries": None, "subscriptions": None}}
    if drop:
        f.pop(drop, None)
    return kpi.parse(data, "selftest")


def _stage(**params):
    from .autopilot import policy
    p = {"sync": False}
    p.update(params)
    return policy.Stage(id="kpi_tracker", kind="kpitracker", owner="ops", mode="plan", summary="", schedule=list(SCHEDULE),
                        presets=[], params=p)


def _ctx(pol, st, crm, now):
    from .autopilot import stages
    out = stages.Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=False)
    return stages.Ctx(policy=pol, stage=st, crm=crm, run_id="selftest", now=now, live=False, out=out)


def _tab(snap, name):
    return next((t for t in snap["tabs"] if t["name"] == name), None)


def _row(t, first, second=None):
    for r in t["rows"]:
        if r and r[0] == first and (second is None or (len(r) > 1 and r[1] == second)):
            return r
    return None


def _text_of(snap) -> str:
    return json.dumps(snap, default=str)


# ---------------------------------------------------------------------- the same fixture, counted the way it was before the sources
def _clock(t) -> str:
    return t.astimezone(ZONE).strftime("%I:%M %p").lstrip("0") if t else ""


def _old_callers(s, w) -> dict:
    """caller -> (dials, owners, numbers, answered, conversations, talk minutes, no answers, first dial), the way the
    tracker read the log before the sources: capture.day_rows, kpi.caller_days, and its own dial span and no
    answers, which folded an unnamed dial into the one caller the capture named."""
    from .autopilot import activity, capture, kpi
    rows = capture.day_rows(w.start, w.end, s.conv_s)
    named = {k for k in rows if k not in ("unknown", kpi.UNASSIGNED)}
    extras: Counter = Counter()
    for r in kpi.caller_days(s.cat, w.first.isoformat(), w.last_day.isoformat(), s.tz):
        extras[r["caller"]] += int(r.get("call_backs") or 0) + int(r.get("texts") or 0)
    noans: Counter = Counter()
    for e in activity.events("owner.call.noanswer", since=w.start, until=w.end):
        if e["data"].get("direction") != "inbound":
            noans[(e["data"].get("caller") or "").strip() or "unknown"] += 1
    if noans.get("unknown") and len(named) == 1:
        noans[next(iter(named))] += noans.pop("unknown")
    first: dict = {}
    for e in activity.events((activity.DIAL, "owner.call.answered"), since=w.start, until=w.end):
        if e["data"].get("direction") == "inbound":
            continue
        who = (e["data"].get("caller") or "").strip() or "unknown"
        first[who] = min(first.get(who, e["when"]), e["when"])
    if "unknown" in first and len(named) == 1:
        only = next(iter(named))
        first[only] = min(first.get(only, first["unknown"]), first.pop("unknown"))
    from .workflows import kpi_report
    credited, _ = kpi_report.credit(s.cat, kpi.new_leads(since=w.start, until=w.end), s.omap())
    out = {}
    for who in (set(rows) | set(extras) | set(noans) | set(first) | set(credited)) - {"unknown", kpi.UNASSIGNED}:
        r = rows.get(who) or {}
        out[who] = (r.get("dials", 0), r.get("owners", 0), r.get("numbers", 0), r.get("answered", 0),
                    r.get("conversations", 0) + extras.get(who, 0), round(r.get("talk_s", 0) / 60.0), noans.get(who, 0),
                    _clock(first.get(who)))
    r = rows.get("unknown") or {}
    loose = (r.get("dials", 0), r.get("owners", 0), r.get("numbers", 0), r.get("answered", 0),
             r.get("conversations", 0) + extras.get(kpi.UNASSIGNED, 0) + extras.get("unknown", 0),
             round(r.get("talk_s", 0) / 60.0), noans.get("unknown", 0), "")
    if any(loose[:7]):
        out[kpi.UNASSIGNED] = loose
    return out


def _new_callers(s, w) -> dict:
    table, _ = s.caller_table(w)
    return {r[0]: (r[4], r[5], r[6], r[7], r[8], r[9], r[14], r[1]) for r in table[:-1]}


def _old(s, w) -> dict:
    """One window's numbers off the log copy with the shared helpers: the way the tracker counted before the
    KPI sources."""
    from .autopilot import activity, capture, contactrate, dealsource, kpi
    from .workflows import kpi_report
    cat = s.cat
    rows = capture.day_rows(w.start, w.end, s.conv_s)
    extras = sum(int(r.get("call_backs") or 0) + int(r.get("texts") or 0)
                 for r in kpi.caller_days(cat, w.first.isoformat(), w.last_day.isoformat(), s.tz))
    leads = kpi.new_leads(since=w.start, until=w.end)
    heard = {e["resource"] for e in activity.events(activity.CALLS + activity.TEXTS + activity.PHONE_STATUS,
                                                    since=w.start, until=w.end) if e["resource"]}
    reached = contactrate.reached({o: w.start + contactrate.GRACE for o in heard}, w.end, cat) if heard else {}
    fam = (("Wrong", ("WRONG", "WRONG_DNC")), ("Dead", ("DEAD",)), ("DNC", ("DNC", "CORRECT_DNC", "WRONG_DNC")))
    marks = {name: set() for name, _ in fam}
    for e in activity.events(activity.PHONE_STATUS, since=w.start, until=w.end):
        to = str(e["data"].get("to") or "").strip().upper()
        for name, statuses in fam:
            if to in statuses:
                marks[name].add((e["resource"], e["data"].get("number") or ""))
    lead_owners = {o for o, (lane, _) in s.lanes_map().items() if lane == "lead"}
    attempts = sum(1 for e in activity.events((activity.DIAL, "owner.sms.sent"), since=w.start, until=w.end)
                   if e["data"].get("direction") != "inbound" and e["resource"] in lead_owners)
    with_leads = sum(1 for c in kpi.conversation_events(cat, since=w.start, until=w.end) if c["owner"] in lead_owners)
    return {"dials": kpi.dials(since=w.start, until=w.end), "answered": sum(r["answered"] for r in rows.values()),
            "talk_min": round(sum(r["talk_s"] for r in rows.values()) / 60.0),
            "conversations": sum(r["conversations"] for r in rows.values()) + extras,
            "owners": len(activity.owners_dialed(w.start, w.end)),
            "correct": kpi.correct_numbers(cat, since=w.start, until=w.end),
            "leads": sorted((x["record"], x["when"]) for x in leads), "credit": kpi_report.credit(cat, leads, s.omap()),
            "contracts": len(kpi.status_changes(s.o["contract_statuses"], since=w.start, until=w.end)),
            "closed": len(kpi.status_changes(s.o["closed_statuses"], since=w.start, until=w.end)),
            "qualified": len(kpi.status_changes(s.o["qualified_statuses"], since=w.start, until=w.end)),
            "appointments": kpi_report.appointments(w.start, w.end)["total"], "reached": len(reached),
            "worked": set(contactrate.first_worked(w.start, w.end, "worked")),
            "marks": {k: len(v) for k, v in marks.items()}, "attempts": {"attempts": attempts, "conversations": with_leads},
            "spend": dealsource.spend(w.first, min(w.nxt, s.today + timedelta(days=1))),
            "mail": len(dealsource.ledger_between("mail", w.start, w.end)),
            # A caller's row is a day's (Today and the Callers tracker): only a day window has one to compare.
            "callers": _old_callers(s, w) if w.nxt - w.first == timedelta(days=1) else None}


def _new(s, w) -> dict:
    """The same window through the KPI sources, as the tracker counts it now."""
    c = s.calls(w)
    return {"dials": c["dials"], "answered": c["answered"], "talk_min": c["talk_min"], "conversations": c["conversations"],
            "owners": c["owners"], "correct": s.correct(w), "leads": sorted((x["record"], x["when"]) for x in s.leads(w)),
            "credit": s.credit(w), "contracts": s.changes("contract_statuses", w), "closed": s.changes("closed_statuses", w),
            "qualified": s.changes("qualified_statuses", w), "appointments": s.appts(w)["total"], "reached": s.reached(w),
            "worked": s.worked_set(w), "marks": s.marks(w), "attempts": s.lead_attempts(w), "spend": s.spend(w),
            "mail": len(s.acts(w, "mail")),
            "callers": _new_callers(s, w) if w.nxt - w.first == timedelta(days=1) else None}


def _both_ways(check, s, when: str) -> None:
    """Every window the tabs count over, both ways, with only the built-in sources connected."""
    wins = list(s.months()) + list(s.weeks()) + [s.since_days(90), s.since_days(20)]
    d = date(2026, 10, 5)
    while d <= s.today:
        wins.append(s.day(d, current=(d == s.today)))
        d += timedelta(days=1)
    bad = []
    for w in wins:
        old, new = _old(s, w), _new(s, w)
        for k in old:
            if old[k] != new[k]:
                bad.append("%s, %s: before %r, now %r" % (w.label, k, old[k], new[k]))
    check(not bad, "%s: the same fixture counted both ways agrees on every number in %d windows (dials, answers, talk, "
                   "conversations, owners, correct numbers and marks, leads and their credit, contracts, closings, "
                   "qualified, appointments, reach, lead attempts, spend, mail, and every caller's row on every day)%s"
          % (when, len(wins), (": " + "; ".join(bad[:4])) if bad else ""))
    month = s.months()[-1]
    check(_new_callers(s, month).get("Emerson", (0,))[0] > _old_callers(s, month).get("Emerson", (0,))[0],
          "%s: over a month (a split the sheet does not show) a dial is its own answerer's even when two callers worked "
          "the month, where the old reading left every unnamed dial not tied" % when)
    lanes_old, lanes_new = {}, {}
    from .autopilot import activity, contactrate, kpi
    for w in s.weeks():
        out = {}
        for e in activity.events(activity.DIAL, since=w.start, until=w.end):
            if e["data"].get("direction") != "inbound":
                r = out.setdefault(s.owner_lane(e["resource"]) or "none", [0, set(), 0, 0])
                r[0] += 1
                r[1].add(e["resource"])
        for cv in kpi.conversation_events(s.cat, since=w.start, until=w.end):
            out.setdefault(s.owner_lane(cv["owner"]) or "none", [0, set(), 0, 0])[2] += 1
        seen = set()
        for e in activity.events(activity.PHONE_STATUS, since=w.start, until=w.end):
            k = (e["resource"], e["data"].get("number") or "")
            if str(e["data"].get("to") or "").upper() in contactrate.REACHED_MARKS and k not in seen:
                seen.add(k)
                out.setdefault(s.owner_lane(e["resource"]) or "none", [0, set(), 0, 0])[3] += 1
        lanes_old[w.label] = {ln: (v[0], len(v[1]), v[2], v[3]) for ln, v in out.items()}
        lanes_new[w.label] = {ln: (v["dials"], len(v["owners"]), v["conversations"], v["correct"]) for ln, v in s.by_lane(w)}
    check(lanes_old == lanes_new, "%s: the by-pipeline block agrees both ways, week by week" % when)


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import kpi, kpitracker
    from .kpi_sources import registry
    keep = (config.DATA_DIR, config.DB_PATH, config.SLACK_VOICE, config.MODE)
    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    saved_adapters = dict(registry.ADAPTERS)
    sinks = kpitracker.__name__.rsplit(".", 1)[0] + ".kpisinks"
    had = sys.modules.get(sinks, MISSING)
    config.DATA_DIR = Path(tmp) / "kpitracker"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SLACK_VOICE, config.MODE = "plain", "dry"
    for k in ENV_KEYS:                            # a setting on this machine must never move a check
        os.environ.pop(k, None)
    os.environ["AGENTS_AGENCY_RETURNS_DIR"] = str(config.DATA_DIR / "agency_returns")
    sys.modules[sinks] = None                     # the real sinks are never reached from here
    store.init()
    try:
        _checks(check, sinks)
    finally:
        kpi.override(None)
        registry.ADAPTERS.clear()
        registry.ADAPTERS.update(saved_adapters)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if had is MISSING:
            sys.modules.pop(sinks, None)
        else:
            sys.modules[sinks] = had
        config.DATA_DIR, config.DB_PATH, config.SLACK_VOICE, config.MODE = keep


def _checks(check, sinks: str) -> None:
    from . import store
    from .autopilot import activity, kpi, kpitracker as kt, policy, sweep
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    cat = _catalog()
    kpi.override(cat)

    # ---------------------------------------------------------------- which run
    st = _stage()
    check(kt.which_run(st, MORNING, TZ) == "morning" and kt.which_run(st, NOON, TZ) == "noon"
          and kt.which_run(st, EVENING, TZ) == "evening",
          "the run is read off the stage's own schedule slot: the day's first slot is the morning, the last the evening")
    check(kt.which_run(st, datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc), TZ) == "morning",
          "a look before the day's first slot reads as the morning view")
    check(kt.which_run(st, datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc), TZ) == "noon",
          "on a day with no slot (a Saturday) the hour decides")
    check(kt.which_run(_stage(run="evening"), MORNING, TZ) == "evening", "the run setting wins over the clock")

    # ---------------------------------------------------------------- the block
    check(not kt.block_problems(_stage()), "an empty block is a good block")
    bad = kt.block_problems(_stage(run="midnight", tab_runs={"Lists": ["dawn"], "Nope": ["morning"]}, record_url="x",
                                   months=40, qualified_statuses="Hot Lead"))
    check(len(bad) == 6, "a mistyped run, tab name, tab run, record link, month count or status list is refused (%d)" % len(bad))

    _seed_log()
    _seed_tables()
    crm = FakeCRM()

    # ---------------------------------------------------------------- the noon snapshot
    ctx = _ctx(pol, st, crm, NOON)
    kt.run_kpitracker(ctx)
    snap = kt.latest()
    check(snap.get("run") == "noon" and snap.get("tz") == TZ and snap.get("refreshed", "").startswith("2026-11-18T17:35"),
          "the snapshot says which run it is, in which zone, refreshed when")
    check([t["name"] for t in snap["tabs"]] == list(kt.TAB_ORDER),
          "twelve tabs in the agreed order: %s" % [t["name"] for t in snap["tabs"]])
    check(all(set(t) == {"name", "note", "columns", "rows", "failed"} and isinstance(t["rows"], list)
              and all(isinstance(c, (str, int, float)) for r in t["rows"] for c in r) for t in snap["tabs"]),
          "every tab keeps the contract kpisinks reads: name, note, columns, rows of text and numbers, failed")
    check(set(snap) == {"refreshed", "run", "tz", "tabs", "slack", "problems", "notes"} and isinstance(snap["slack"]["lines"], list),
          "the snapshot keeps the agreed top-level keys")
    saved = config.DATA_DIR / "kpi" / "kpi_tracker_2026-11-18.json"
    check(saved.is_file() and json.loads(saved.read_text(encoding="utf-8"))["run"] == "noon",
          "the snapshot is saved on the box's volume with kpi.save_report")
    check(ctx.out.acted == 0 and ctx.out.head.startswith("KPI tracker, noon run:"),
          "the stage acts on nothing and its head is a short summary for autopilot last: %r" % ctx.out.head)
    check(ctx.out.tally.get("not published: the sheet and Slack sinks (kpisinks) are not built yet") == 1
          and not any("sinks" in p for p in ctx.out.problems),
          "with no kpisinks module the run says so as a tally line and does not crash or raise a problem")

    sb = _tab(snap, "Scoreboard")
    check(sb["columns"][:4] == ["Section", "Measure", "Oct 2026", "Nov 2026 (so far)"],
          "the Scoreboard's months start where the call log copy starts: %r" % sb["columns"][:4])
    dials = _row(sb, "Activity", "Dials")
    check(dials[2:4] == [1, 11] and dials[-1] == "behind",
          "dials by month come from the log (1 in October, 11 this month) and the month's pace is marked: %r" % dials)
    conv = _row(sb, "Activity", "Conversations")
    check(conv[3] == 7, "conversations count calls of a minute either way and an inbound text, the catalog's way: %r" % conv)
    check(_row(sb, "Pipeline", "New leads")[3] == 9 and _row(sb, "Pipeline", "Contracts signed")[3] == 2
          and _row(sb, "Pipeline", "Deals closed")[3] == 1,
          "this month: 9 new leads, 2 contracts signed and 1 deal closed, each on its own row")
    check(_row(sb, "Pipeline", "Qualified leads")[3] == 1, "a lead moved to Hot Lead is qualified")
    for label in ("Texts sent", "Text replies"):
        r = _row(sb, "Activity", label)
        check(r[2:4] == [kt.GAP, kt.GAP], "%s reads not tracked yet, never zero: %r" % (label, r[2:4]))
    check(_row(sb, "Pipeline", "Offers made")[2:4] == [kt.GAP, kt.GAP] and _row(sb, "Pipeline", "Revenue closed")[3] == kt.GAP,
          "offers and revenue (no fee field) are gaps, not zeros")
    check("not tracked yet" in sb["note"] and "no texting tool is connected" in sb["note"]
          and "cli.py kpisources connect" in sb["note"],
          "the gaps' reasons are in the tab's note: no texting tool is connected, and the command that connects one")
    week_head = next(i for i, r in enumerate(sb["rows"]) if r and r[0].startswith("Last 6 weeks"))
    check(sb["rows"][week_head + 1][-1] == "Week of Nov 16 (so far)", "the weekly block ends with this week so far")

    td = _tab(snap, "Today")
    adri = _row(td, "Emerson")
    check(adri[1:5] == ["9:05 AM", "11:40 AM", 2.6, 4], "a caller's first and last dial (each dial's own moment, so the "
          "last is 11:40, not the 11:41 answer) and hours on the dialer; unnamed dials go to the one caller who worked "
          "the day: %r" % adri[:5])
    check(adri[8] == 2 and adri[14] == 1 and adri[15] == 1,
          "the caller's own conversations, no answers and leads credited: %r" % adri)
    check(_row(td, kt.NOT_TIED)[8] == 1, "an inbound text with no caller is the team's, on its own row, not a caller's")
    team = _row(td, "Team")
    check(team[4] == 4 and team[8] == 3 and team[10:14] == [1, 1, 0, 1] and team[15] == 2 and team[16] == 1,
          "the team row: 4 dials, 3 conversations, 1 correct, 1 wrong, 0 dead, 1 do-not-call, 2 leads, 1 appointment: %r" % team)
    above = td["rows"][:td["rows"].index(team)]
    check(team[4] == sum(r[4] for r in above if isinstance(r[4], int)) and team[8] == sum(r[8] for r in above),
          "the team's dials and conversations are the callers' rows added up")
    check(_row(td, "Leads with no next task (now)")[1] == 1 and _row(td, "Lead tasks overdue (log copy)")[1] == 1
          and _row(td, "Lead tasks due today (log copy)")[1] == 1 and _row(td, "Leads not called yet")[1] == 1,
          "the lead manager's day: one lead with no task, one task overdue, one due today, one lead not called yet")
    check(_row(td, "Texts sent")[1] == kt.GAP, "texts sent read not tracked yet on the Today tab")
    board = [r for r in td["rows"] if r and str(r[0]).startswith("County board")]
    check([r[2] for r in board] == [1, 1, 0, 1, 0, 0, 1], "the county board's cards by column: %r" % [r[2] for r in board])
    check(_row(td, "Top 1,000", "Priority - 02 Ready to Call")[2] == 87, "the Top 1,000 presets are counted live")

    cl = _tab(snap, "Callers")
    tue = [r for r in cl["rows"] if r and r[0] == "Tue Nov 17"]
    check(any(r[1] == kt.NOT_TIED and r[5] == 1 for r in tue) and any(r[1] == "Sloane" for r in tue),
          "with two callers a dial with no name stays not tied to a caller: %r" % [r[:6] for r in tue])
    check(any(r and r[0] == "Emerson" and r[1] == 1 for r in cl["rows"]), "the 90-day block judges each caller (goals.caller_lines)")
    check(_row(cl, "By attempt (Day 1 to Day 5, Exhausted)")[1] == kt.GAP and _row(cl, "Contracts credited to a caller")[1] == kt.GAP,
          "by attempt and contracts credited read not tracked yet")
    wide = kt.Snap(_ctx(pol, _stage(caller_days=40), FakeCRM(), NOON), cat, kt.options(_stage(caller_days=40)), "noon")
    days = {r[0] for r in wide.tab_callers()["rows"] if r}
    check("Tue Oct 6" in days and "Fri Oct 2" not in days,
          "the daily tracker starts where the call log copy starts: a day before it would read as a day of no calls")
    check(kt.status_word("new_lead") == "New lead" and kt.status_word("Hot Lead") == "Hot Lead",
          "a status is shown the way a person reads it")

    ld = _tab(snap, "Leads")
    check(_row(ld, "Leads with no next task")[1] == 1 and _row(ld, "Hot leads gone quiet")[1] == 1
          and _row(ld, "Warm leads gone quiet")[1] == 0,
          "a hot lead with no talk inside two days is quiet; a warm lead talked to yesterday is not")
    ledger_rows = [r for r in ld["rows"] if r and str(r[0]).startswith("https://app.reisift.io/records/")]
    check(len(ledger_rows) == 10, "the lead ledger holds every lead this month and last (%d)" % len(ledger_rows))
    r12 = next(r for r in ledger_rows if r[0].endswith("/r12"))
    check(r12[3] == "mail" and r12[7] == kt.GAP, "a call back on a mail tracking line made r12 a mail lead; first offer is a gap")
    r11 = next(r for r in ledger_rows if r[0].endswith("/r11"))
    check(r11[9] == "Thu Nov 12" and r11[10] == "Fri Nov 13" and r11[11] == 7,
          "the ledger dates each step from the log: contract, closed, seven days lead to contract: %r" % r11)
    check("Sample Rd" not in _text_of(snap), "no street reaches the sheet while the ledgers show record links only")
    check(_row(ld, "HOT Leads") is not None and _row(ld, "HOT Leads")[1] == 1, "leads by board column read the last card move")

    dl = _tab(snap, "Deals")
    check(_row(dl, "Contracts signed")[2] == 2 and _row(dl, "Offers made")[1:4] == [kt.GAP] * 3,
          "the closer's numbers: contracts counted, offers a gap")
    led = next(r for r in dl["rows"] if r and str(r[0]).endswith("/r11"))
    check(led[6] == "Fri Nov 13" and led[10] == "Emerson" and led[12] == "Sloane",
          "the contract ledger carries the closing and the deal sources' names: %r" % led)

    ftm = _tab(snap, "First to Market")
    check(_row(ftm, "On the board")[1] == 4 and _row(ftm, "Due a call today (Day 1 to Day 5)")[1] == 1,
          "four cards on the board, one due a call today")
    check(_row(ftm, "Liens")[1] == "no list with this title" and _row(ftm, "Foreclosure")[1:4] == [3, 9, 120],
          "new records per feed this week and month, and a list the account lacks is said")
    check(_row(ftm, "Owners reached")[1] == 1 and _row(ftm, "New leads")[1] == 1,
          "owners reached and leads from every record the board ever held, a card it took off included")
    check("plan runs placed" in ftm["note"], "the note says the cards are where the plan runs placed them")

    dm = _tab(snap, "Direct Mail")
    s1 = _row(dm, "First to market", 1)
    s2 = _row(dm, "First to market", 2)
    check(s1[2:7] == ["handwritten letter", "FTM - Mail 01 Handwritten Letter", 3.0, 2, 0] and s1[7:11] == [0, 1, 0, 3.0],
          "step 1 from the stage's own pieces list: two staged, one mailed in October: %r" % s1)
    check(s2[5:11] == [0, 1, 1, 1, 0.75, 0.75], "step 2: one released and mailed this month at its billed cost: %r" % s2)
    check(_row(dm, "Obituary", "soft 1")[5] == 1, "an obituary letter's step comes from the letter itself")
    counters = _row(dm, "First to market", 500)
    check(counters is not None and counters[2:5] == [120, 15, 380] and _row(dm, "Obituary", 300)[2:5] == [40, 4, 260],
          "DataSift's own mail counters per lane: records, mailed once, mailed this month, never mailed: %r" % counters)
    check(_row(dm, "Leads from mail this month")[1] == 1 and _row(dm, "Leads per 1,000 pieces")[1] == 1000.0
          and _row(dm, "Deals from mail to date (the deal sources' channel)")[1] == 1,
          "response: one lead from mail on one piece this month, and the deal the deal sources tie to mail")

    ch = _tab(snap, "Channels")
    check(_row(ch, "Agency", "Leads back from its file")[2] == kt.GAP and _row(ch, "Agency", "Monthly fee ($)")[2] == 1800,
          "agency leads back are a gap until a file comes back; the fee is the scorecard's")
    check(_row(ch, "Texting (Smarter Contact)", "Sent, delivered, replies, hand raises, opt-outs")[2] == kt.GAP,
          "Smarter Contact's sends are a gap")
    check(_row(ch, "Not Interested and rehash", "Deals from a Not Interested owner called again")[2] == 1,
          "deals by source come from the deal sources' rows")

    gl = _tab(snap, "Goals")
    check(_row(gl, "Leads")[1] == 30.0 and _row(gl, "Dials")[1] == 3000.0,
          "the goal is worked back the goal sheet's way (goals.needs): 30 leads and 3,000 dials a month")
    check(_row(gl, "Offers per contract")[1] == kt.GAP, "offers per contract is a gap")
    check(_tab(snap, "How it is counted")["rows"][0][0] == "Conversation", "the definitions come from team/kpi.yaml")

    lines = snap["slack"]["lines"]
    check(lines[0] == "KPI tracker refreshed Wed, Nov 18 at 12:35 PM.", "the first line says when: %r" % lines[0])
    check(lines[1] == "Today so far: 4 dials, 3 conversations, 1 correct number, 2 new leads (Emerson 1, 1 credited to nobody).",
          "the noon update starts with today so far: %r" % lines[1])
    check(lines[2] == "Pace by noon: Emerson 4 dials (150 by end of day).", "then the pace by noon: %r" % lines[2])
    check(lines[3] == "Month so far: 9 leads, 2 contracts, 1 closed. Pace: leads behind, contracts on track, dials behind.",
          "the month line carries the Scoreboard's own marks: %r" % lines[3])
    check("Speed to lead" not in "\n".join(lines) and "1 lead has no next task." in lines,
          "a part with nothing to say is dropped (no lead was timed today); the rest of its line stays")
    check("On deck: 1 county card due a call today, 1 hot auction record inside 14 days." in lines,
          "on deck: the board's cards due today and the sales inside the auction stage's critical window")
    check("Needs a person: 2 staged batches waiting to be released." in lines, "the batches waiting on a person are said")
    check(len(lines) <= 9 and len("\n".join(lines)) <= 700, "the update stays short: %d lines" % len(lines))
    check(not any(kt.PHONE_SHAPE.search(ln) for ln in lines) and "Sample Rd" not in "\n".join(lines),
          "no phone number and no street in the Slack lines")
    check(all(ch not in "\n".join(lines) for ch in (chr(0x2014), chr(0x2013))), "no long dash in the Slack lines")
    check(all(ln == kt.voice.clean(ln) for ln in lines), "every line is already in the cleaned voice")
    check(all(m.get("property_type") == "clean" for m in crm.musts if "_preset" not in m and "lead_rows" not in m),
          "every live count is the app's first tab (clean records)")
    posts = sweep.posts({"id": 1, "workflow": "kpi_tracker", "source": "schedule"},
                        dict(ctx.out.as_dict(), problems=["the Lists tab could not be built this run"]))
    check(all(p["kind"] == "issues" for p in posts),
          "registered without always_reports, sweep.posts sends this stage's problems only, never the update itself")

    # ---------------------------------------------------------------- the sources, with only the built-in ones connected
    hc = _tab(snap, "How it is counted")
    src = {r[0]: r for r in hc["rows"] if r and r[0] in ("The DataSift log", "The ledger", "The agency's files")}
    check(set(src) == {"The DataSift log", "The ledger", "The agency's files"}
          and src["The DataSift log"][1] == "calls, texts, phone statuses, stages"
          and src["The ledger"][1] == "mail pieces, spend" and "the agency's rows only" in src["The agency's files"][1],
          "How it is counted lists each connected source with what it provides: %r" % [r[:2] for r in src.values()])
    check(src["The DataSift log"][2] == "Wed Nov 18 11:41 AM" and src["The DataSift log"][3].startswith("ok, complete through"),
          "and when it last reported and how far it is complete: %r" % src["The DataSift log"][2:])
    check(_row(hc, "On the box") is not None and _row(hc, "On the box")[1].startswith("3 sources switched on"),
          "and the box's own count of sources (kpi_sources.public_health), counts only")
    check(not any("DataSift log copy" in p or "do not agree" in p for p in snap["problems"])
          and _row(hc, "Offers are not counted: no status, board column or tag is named as an offer yet.") is not None,
          "a clean read raises no source issue; the DataSift source's own note is listed as a note")
    s_noon = kt.Snap(_ctx(pol, st, FakeCRM(), NOON), cat, kt.options(st), "noon")
    _both_ways(check, s_noon, "noon")
    s_eve = kt.Snap(_ctx(pol, st, FakeCRM(), EVENING), cat, kt.options(st), "evening")
    _both_ways(check, s_eve, "evening")
    tbl, _ = s_noon.caller_table(s_noon.day_win)
    old_last = max(e["when"] for e in activity.events(("owner.call.made", "owner.call.answered"),
                                                      since=s_noon.day_win.start, until=s_noon.day_win.end))
    check(tbl[0][2] == "11:40 AM" and _clock(old_last) == "11:41 AM",
          "the one cell that moved: a caller's last dial is the dial's own moment (11:40), where it read the answer's (11:41)")
    check(json.loads(json.dumps(s_noon.acts(s_noon.day_win, "calls")[0].to_dict()))["kind"] == "call"
          and "Event(" not in store.kv_get(kt.KV_LATEST) and "Event(" not in store.kv_get(kt.KV_FACTS),
          "an act is stored as its own plain form (to_dict), never as its printed self")

    # ---------------------------------------------------------------- the same day, the morning and the evening
    store.kv_set(kt.KV_LATEST, "")
    store.kv_set(kt.KV_BUILT, "")
    store.kv_set(kt.KV_FACTS, "")
    mctx = _ctx(pol, st, FakeCRM(), MORNING)
    kt.run_kpitracker(mctx)
    msnap = kt.latest()
    ml = msnap["slack"]["lines"]
    check(msnap["run"] == "morning" and ml[1] == "Tuesday final: 4 dials, 1 conversation, 0 correct numbers, 2 new leads "
          "(Emerson 1, 1 credited to nobody).", "the morning reads the last calling day final: %r" % ml[1:2])
    check(ml[2] == "Last week: 2 dials, 2 leads, 1 appointment, 1 contract signed.", "then last week: %r" % ml[2])
    check(ml[3].startswith("Month so far: ") and "Pace:" in ml[3], "then the month and its pace")
    check("Speed to lead: median 9 minutes. 1 lead has no next task." in ml,
          "the morning times the last calling day's leads: 9 minutes to the first call")
    check(_tab(msnap, "Today")["rows"][0][0] == "Emerson" and "Tuesday final" in _tab(msnap, "Today")["note"],
          "the morning Today tab is the last calling day")
    lists = _tab(msnap, "Lists")
    check(not lists["failed"] and _row(lists, "Agency") is not None and _row(lists, "Agency")[2] == 2
          and _row(lists, "Liens")[1] == "no list with this title" and _row(lists, "Agency")[9] == kt.GAP,
          "the Lists tab: owners and records by lane, county lists counted live, doors per deal a gap")
    costs = _tab(msnap, "Costs")
    check(_row(costs, "Dialer ($ a month, typed in team/kpi.yaml)")[1] == kt.GAP
          and _row(costs, "Cost per lead ($)")[1] == kt.GAP and _row(costs, "Mail (the autopilot's ledger) ($)")[1:3] == [3.0, 0.75],
          "costs: the ledger's spend by month; an untyped cost and the cost per lead read not tracked yet")

    nctx = _ctx(pol, st, FakeCRM(), NOON)
    kt.run_kpitracker(nctx)
    nsnap = kt.latest()
    kept = [t["name"] for t in nsnap["tabs"] if "this tab is rebuilt at the" in t["note"]]
    check(kept == ["Direct Mail", "Lists", "Costs"], "at noon the morning-only tabs are carried, not rebuilt: %r" % kept)
    check(_tab(nsnap, "Lists")["rows"] == lists["rows"] and "Built at Wed Nov 18 7:50 AM" in _tab(nsnap, "Lists")["note"],
          "a carried tab is the earlier run's copy, and its note says when it was built")
    check(nctx.out.tally.get("tabs kept from an earlier run") == 3 and any("Kept from an earlier run" in n for n in nsnap["notes"]),
          "the run says which tabs it kept")

    ectx = _ctx(pol, st, FakeCRM(), EVENING)
    kt.run_kpitracker(ectx)
    el = kt.latest()["slack"]["lines"]
    check(el[1].startswith("Today final: 4 dials") and el[2] == "Dials today: Emerson 4 (behind); the floor is 150 dials a day.",
          "the evening turns today so far into today final and marks each caller against the floor: %r" % el[1:3])
    check(el[-1] == "Alarms today: 520 dials since the last new lead.",
          "the evening adds the day's alarms as the last line, and leaves a caller ID check out: %r" % el[-1])
    check(not _tab(kt.latest(), "Direct Mail")["note"].count("this tab is rebuilt"),
          "the Direct Mail tab is rebuilt at the evening run")

    # ---------------------------------------------------------------- a line that looks like it carries a phone is dropped
    s = kt.Snap(_ctx(pol, st, FakeCRM(), EVENING), cat, kt.options(st), "evening")
    s.facts.update(month={"label": "Nov", "leads": 1, "contracts": 0, "closed": 0}, marks={},
                   alarms=["call 8650000101 back"])
    got = s.slack_lines([])
    check(not any("8650000101" in ln for ln in got) and any("phone number" in p for p in s.problems),
          "a Slack line carrying a phone-shaped number is left out, and the problem is said")
    check(any(ln == "Month so far: 1 lead, 0 contracts, 0 closed. Pace: not judged yet." for ln in got)
          and any(ln.startswith("Dials today: none") for ln in got),
          "the pace lines show even when there is nothing to pace, and an unjudged pace says so")
    done, total = s.month_days(None)
    late, _ = s.month_days(datetime(2026, 11, 17).date())
    check(done == 0.0 and total == 21 and 1.9 < late < 2.1,
          "the month's pace counts only the weekdays the log copy holds: none on an empty copy (%s), two from Nov 17 (%s)"
          % (done, late))

    # ---------------------------------------------------------------- a failed source fails its own tabs only
    store.kv_set(kt.KV_LATEST, "")
    fctx = _ctx(pol, st, FakeCRM(broken_lists=True), NOON)
    kt.run_kpitracker(fctx)
    fsnap = kt.latest()
    failed = [t["name"] for t in fsnap["tabs"] if t["failed"]]
    check(set(failed) == {"First to Market", "Direct Mail", "Channels", "Lists"}
          and {t["name"] for t in fsnap["tabs"] if not t["failed"]} == set(kt.TAB_ORDER) - set(failed),
          "a list index that fails stops the four tabs that read lists and every other tab is built: %r" % failed)
    ft = _tab(fsnap, "First to Market")
    check(ft["rows"] == [] and ft["note"].startswith("Could not be built this run") and "502" in ft["note"],
          "a failed tab carries no rows and says why; the sheet keeps its last good version")
    check(any(p.startswith("the First to Market tab could not be built") for p in fctx.out.problems),
          "the failure is a problem the issues channel can carry")
    check(any(ln.startswith("Needs a person:") and "could not be built" in ln for ln in fsnap["slack"]["lines"]),
          "the update says which tabs could not be built")

    # ---------------------------------------------------------------- a figure the catalog lacks stops the tab that needs it
    kpi.override(_catalog(drop="role.closer_offers_per_day"))
    store.kv_set(kt.KV_LATEST, "")
    dctx = _ctx(pol, st, FakeCRM(), NOON)
    kt.run_kpitracker(dctx)
    dsnap = kt.latest()
    dfailed = [t["name"] for t in dsnap["tabs"] if t["failed"]]
    check(set(dfailed) == {"Scoreboard", "Deals"} and "role.closer_offers_per_day" in _tab(dsnap, "Deals")["note"],
          "a missing figure stops the tabs that need it, with its name, and only those: %r" % dfailed)
    check(_row(_tab(dsnap, "How it is counted"), "role.closer_offers_per_day")[1].startswith("missing from team/kpi.yaml"),
          "the How it is counted tab names the missing figure")
    check(any(ln.startswith("Month so far: not read this run") for ln in dsnap["slack"]["lines"]),
          "with the Scoreboard stopped, the month line says so instead of quoting an old number")
    kpi.override(cat)

    # ---------------------------------------------------------------- the live counts are bounded
    store.kv_set(kt.KV_LATEST, "")
    capped = FakeCRM()
    cctx = _ctx(pol, _stage(max_counts=3), capped, NOON)
    kt.run_kpitracker(cctx)
    csnap = kt.latest()
    check(len([m for m in capped.musts if "lead_rows" not in m]) <= 3 and kt.NOT_READ in _text_of(csnap)
          and "not read this run" in _tab(csnap, "First to Market")["note"],
          "past max_counts a live count reads not read this run, and the note says why")

    # ---------------------------------------------------------------- the sinks
    seen = {}

    def fake_publish(ctx, snap):
        seen["snap"] = snap
        return {"sheet": "https://sheets.example.test/kpi", "slack": "updated", "problems": ["the sheet is shared with no one yet"]}

    mod = types.ModuleType(sinks)
    mod.publish = fake_publish
    sys.modules[sinks] = mod
    try:
        pctx = _ctx(pol, st, FakeCRM(), NOON)
        kt.run_kpitracker(pctx)
        check(seen.get("snap") is not None and len(seen["snap"]["tabs"]) == 12 and seen["snap"] == kt.latest(),
              "with kpisinks built, publish gets the very snapshot that was saved")
        check("the sheet is shared with no one yet" in pctx.out.problems and pctx.out.tally.get("sheet updated") == 1,
              "the sinks' problems become the stage's problems")

        def broken(ctx, snap):
            raise RuntimeError("the sheet answered 403")

        mod.publish = broken
        bctx = _ctx(pol, st, FakeCRM(), NOON)
        kt.run_kpitracker(bctx)
        check(any(p.startswith("the KPI tracker could not update the sheet and Slack") for p in bctx.out.problems),
              "a sink that fails is a problem, and the snapshot is still saved")
    finally:
        sys.modules[sinks] = None

    # ---------------------------------------------------------------- the preview publishes nothing
    out = config.DATA_DIR / "preview.md"
    psnap = kt.preview("kpi_tracker", str(out), "", offline=False, sync=False, now=NOON, crm=FakeCRM(), pol=pol)
    text = out.read_text(encoding="utf-8")
    check(out.is_file() and "## Scoreboard" in text and "## Slack" in text and "Nothing was published" in text
          and psnap["run"] == "noon", "the preview writes every tab and the Slack lines as text")
    check(all(ch not in text for ch in (chr(0x2014), chr(0x2013))), "no long dash in the preview")
    osnap = kt.preview("kpi_tracker", "", "morning", offline=True, sync=False, now=NOON, pol=pol)
    check(osnap["run"] == "morning" and kt.NOT_READ in _text_of(osnap),
          "an offline preview reads nothing from DataSift and says so in the cells")

    # ---------------------------------------------------------------- the campaigns timed from a date, and more sources
    _cadence_mail(check, kt, cat)
    _texting(check, kt, pol, st)
    _disagreement(check, kt, pol, st)
    _broken_source(check, kt, pol, st)
    _agency_files(check, kt, pol, st)
    _behind_and_mapped(check, kt, pol, st)

    # ---------------------------------------------------------------- house style
    src = Path(kt.__file__).read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
    check(all(ch not in src for ch in (chr(0x2014), chr(0x2013))) and "\x08" not in src,
          "no long dash and no backspace byte in the tracker or its selftest")


def _run(kt, pol, st, now=NOON):
    ctx = _ctx(pol, st, FakeCRM(), now)
    kt.run_kpitracker(ctx)
    return ctx, kt.latest()


# ---------------------------------------------------------------------- the campaigns timed from a date (mailcadence)
def _cadence_mail(check, kt, cat) -> None:
    from .autopilot import ledger, mailcadence as mc, policy
    print("  the date-timed mail campaigns")
    pb = copy.deepcopy(PLAYBOOK)
    pb["stages"].update(copy.deepcopy(CADENCE))
    pol = policy.parse(pb, EMPLOYEES)
    check(all(k in (pol.stages or {}) and pol.stages[k].kind == "mailcadence" for k in CADENCE),
          "the auction and probate campaigns load as stages of kind mailcadence")
    ledger.event("run-d", "auction_mail", "ca1", "mail", {"piece": "handwritten_letter", "step": 1, "vendor": "siftmail"},
                 usd=1.89, now=datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc), tz=TZ)
    ledger.event("run-e", "probate_mail", "cp1", "mail", {"piece": "letter", "step": 1, "vendor": "siftmail"},
                 usd=1.10, now=datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc), tz=TZ)
    ledger.batch_create("auction_mail", "mail", [{"record_uuid": "ca2", "piece": "options_letter", "step": 2,
                                                  "merge": {"design": "A2"}}], 1.23)
    for rec, design, since, stage in (("cw1", "A1", "2026-11-16", "auction_mail"), ("cw2", "A1", "2026-11-17", "auction_mail"),
                                      ("cw3", "PB1", "2026-11-17", "probate_mail")):
        ledger.set_fact(rec, mc.WAIT_FACT, {"since": since, "design": design})
        ledger.set_fact(rec, mc.CLAIM_FACT, {"stage": stage, "anchor": "", "until": "2026-12-31", "why": "selftest"})
    ledger.set_fact("cw4", mc.WAIT_FACT, {})                 # a record that cleared the floor waits no more
    st = _stage()
    s = kt.Snap(_ctx(pol, st, FakeCRM(), NOON), cat, kt.options(st), "noon")
    dm = s.guarded("Direct Mail", s.tab_mail)
    check(not dm["failed"] and dm["columns"][0] == "Campaign", "the Direct Mail tab builds with the date-timed campaigns in it")
    order = [r[1] for r in dm["rows"] if r and r[0] == "Auction"]
    check(order == ["A1", "A2", "A3", "A4"] and [r[1] for r in dm["rows"] if r and r[0] == "Probate"] == ["PB1", "EVG_T4"],
          "each step of a date-timed campaign is a piece id from its own pieces list, under the campaign's name: %r" % order)
    a1, a2, a3 = _row(dm, "Auction", "A1"), _row(dm, "Auction", "A2"), _row(dm, "Auction", "A3")
    check(a1[2:5] == ["handwritten letter", "", 1.89] and a1[5:11] == [0, 0, 1, 1, 1.89, 1.89],
          "A1: one mailed this month, at what SiftMail billed: %r" % a1)
    check(a2[5] == 1 and a2[4] == 1.23, "A2: one staged and waiting on release, keyed from the batch's step: %r" % a2)
    check(a3[4] == kt.CANNOT_ORDER and a3[5:11] == [kt.NONE_YET] * 6,
          "A3, a piece SiftMail cannot order, reads cannot be ordered yet, never a row of zeros: %r" % a3)
    pb1 = _row(dm, "Probate", "PB1")
    check(pb1[7:9] == [0, 1] and pb1[10] == 1.1, "PB1: mailed in October, so none this month and one to date: %r" % pb1)
    check("Auction A3 (snap pack) cannot be ordered yet: SiftMail has no price for mail type 18 in this account, so its "
          "fallback A4 goes in its place." in dm["note"], "the piece that cannot be ordered is a note that names its fallback")
    check("Auction pieces waiting on SiftMail's floor of 20 records a campaign: A1 2 records, the longest since Mon Nov 16."
          in dm["note"] and "Probate pieces waiting on SiftMail's floor of 20 records a campaign: PB1 1 record, the longest "
          "since Tue Nov 17." in dm["note"],
          "the records waiting on the SiftMail floor are a note per campaign, from the campaign's own wait memory")
    check(not any("waiting" in str(c) for r in dm["rows"] for c in r) and "cw4" not in dm["note"],
          "the waits are notes, never a column of zeros, and a record that cleared the floor is not one of them")
    check(_row(dm, "Pieces mailed this month (every lane)")[1] == 2,
          "the month's pieces count the campaign's piece beside the monthly lane's")


# ---------------------------------------------------------------------- more sources, connected one at a time
def _texting(check, kt, pol, st) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import file_import
    print("  a texting tool connected")
    check(ks.connect("sc_texts", "file_import", tool="smartercontact", settings={"rows": "text", "tz": TZ},
                     now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)) == [],
          "a texting tool's export connects as a file source")
    data = "\n".join(["Message ID,Date,Direction,Phone,Status,Agent",
                      "SC-1,11/17/2026 2:00 PM,Outgoing,8650000143,delivered,Emerson",
                      "SC-2,11/18/2026 10:15 AM,Outgoing,8650000141,delivered,Emerson",
                      "SC-3,11/18/2026 10:20 AM,Outgoing,8650000142,failed,Emerson",
                      "SC-4,11/18/2026 10:40 AM,Incoming,8650000141,received,"]).encode("utf-8")
    got = file_import.ADAPTER.on_file(ks.connection("sc_texts"), "texts.csv", data, now=NOON)
    check(len(got.events) == 4 and not got.problems, "its four rows are read as texts: %r" % got.problems)
    _ctx_, snap = _run(kt, pol, st)
    sb = _tab(snap, "Scoreboard")
    check(_row(sb, "Activity", "Texts sent")[2:4] == [kt.GAP, 2] and _row(sb, "Activity", "Text replies")[2:4] == [kt.GAP, 2],
          "texts sent and replies fill from the texting tool (a failed text is not sent; the log's reply counts too), and "
          "the month before it was connected stays not tracked yet: %r" % (_row(sb, "Activity", "Texts sent")[2:4],))
    check("no texting tool is connected" not in sb["note"] and "Smarter Contact file" in sb["note"],
          "the note says where the texts come from now")
    td = _tab(snap, "Today")
    check(_row(td, "Texts sent")[1] == 1 and _row(td, "Text replies")[1] == 2 and _row(td, "Team")[8] == 4,
          "today: one text sent, two replies, and the tool's reply is one more conversation, the catalog's way")
    ch = _tab(snap, "Channels")
    check(_row(ch, "Texting (Smarter Contact)", "Texts sent")[2:4] == [2, kt.GAP]
          and _row(ch, "Texting (Smarter Contact)", "Replies")[2:4] == [1, kt.GAP]
          and _row(ch, "Texting (every source)", "Replies")[2] == 2
          and _row(ch, "Texting (Smarter Contact)", "Sent, delivered, replies, hand raises, opt-outs") is None,
          "Channels splits the texts by tool, and the old gap row goes once the tool counts")
    cl = _tab(snap, "Callers")
    head = next((i for i, r in enumerate(cl["rows"]) if r and str(r[0]).startswith("Texts sent, ")), None)
    check(head is not None and cl["rows"][head + 2] == ["Emerson", 2] and _row(cl, "Texts a caller sent") is None,
          "Callers shows the week's texts by the person the tool names, and that row is no longer a gap")
    ks.disconnect("sc_texts")
    ks.forget("sc_texts")
    _ctx_, back = _run(kt, pol, st)
    check(_row(_tab(back, "Scoreboard"), "Activity", "Texts sent")[3] == kt.GAP,
          "switched off again, the texts read not tracked yet, never zero")


def _disagreement(check, kt, pol, st) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import file_import
    print("  two sources that disagree")
    check(ks.connect("kx_calls", "file_import", tool="kixie", settings={"rows": "call", "tz": TZ}) == [],
          "a dialer's call history connects as a file source")
    data = "\n".join(["Call ID,Date,Direction,Phone,Duration,Agent",
                      "K-1,11/18/2026 9:30 AM,Outgoing,8650000151,0:00,Emerson",
                      "K-2,11/18/2026 9:40 AM,Outgoing,8650000152,0:00,Emerson",
                      "K-3,11/18/2026 9:50 AM,Outgoing,8650000153,0:00,Emerson"]).encode("utf-8")
    file_import.ADAPTER.on_file(ks.connection("kx_calls"), "kixie calls.csv", data, now=NOON)
    ctx, snap = _run(kt, pol, st)
    td = _tab(snap, "Today")
    said = [p for p in snap["problems"] if "do not agree" in p]
    check(_row(td, "Team")[4] == 4 and _row(_tab(snap, "Scoreboard"), "Activity", "Dials")[3] == 11,
          "the Kixie file and the log disagree on Nov 18, and the dials stay what collect resolved: the log's 4")
    check(len(said) == 1 and "Kixie calls on Nov 18" in said[0] and "the Kixie file" in said[0] and said[0] in ctx.out.problems,
          "the disagreement is one problem on the snapshot: %r" % said)
    check(said and said[0] in td["note"] and said[0] in _tab(snap, "Scoreboard")["note"]
          and said[0] not in _tab(snap, "Costs")["note"],
          "and a note on every tab that reads calls, and on no tab that does not")
    hc = _tab(snap, "How it is counted")
    check(said and _row(hc, said[0]) is not None and _row(hc, said[0])[1] == "calls",
          "How it is counted lists the issue and what it touches")
    ks.disconnect("kx_calls")
    ks.forget("kx_calls")


BROKEN = None


def _broken_source(check, kt, pol, st) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import registry
    print("  a source that cannot be read")

    class Broken(ks.Adapter):
        id = "broken"
        provides = (ks.Provides("call", "api_key"),)

        def pull(self, conn, since, until, kinds, now=None):
            raise RuntimeError("the vendor is down")

    globals()["BROKEN"] = Broken()
    registry.ADAPTERS["broken"] = ("agents.selftest_kpitracker", "BROKEN")
    ks.connect("vendor_x", "broken", tool="vendorx")
    quiet = logging.getLogger("agents.kpi_sources")
    level = quiet.level
    quiet.setLevel(logging.CRITICAL)                 # the source's own traceback is not this check's output
    try:
        _ctx_, snap = _run(kt, pol, st)
    finally:
        quiet.setLevel(level)
    said = [p for p in snap["problems"] if "vendorx" in p]
    check(said and "could not be read" in said[0] and "the vendor is down" in said[0] and said[0] in _tab(snap, "Today")["note"],
          "a source that cannot be read is a problem and a note: %r" % said)
    check(_row(_tab(snap, "Today"), "Team")[4] == 4 and not any(t["failed"] for t in snap["tabs"]),
          "and every other source still counts: the dials are unchanged and no tab fails")
    ks.disconnect("vendor_x")
    del registry.ADAPTERS["broken"]


def _agency_files(check, kt, pol, st) -> None:
    from .workflows import agency_results as ar
    print("  the agency's own files")

    def row(n, cls, phone, day, disp):
        return {"row": n, "file": "a.csv", "disposition": disp, "class": cls, "phone": phone, "notes": "", "callback": "",
                "call_date": day, "reached": cls != "dead", "last": "", "address": "", "agent": "Sam"}

    ar.save_return({"id": "kta000000001", "kind": "records", "received": "2026-11-17T15:00:00+00:00", "day": "2026-11-17",
                    "month": "2026-11", "records": {
                        "ra-1": {"top": "lead", "state": "written", "phones": {"8650000161": "correct"}, "deceased": False,
                                 "rows": [row(2, "lead", "8650000161", "2026-11-17", "Hot Lead")]},
                        "ra-2": {"top": "", "state": "written", "phones": {"8650000162": "wrong"}, "deceased": False,
                                 "rows": [row(3, "wrong", "8650000162", "2026-11-16", "Wrong Number")]}}})
    ar.save_return({"id": "ktp000000001", "kind": "pdf", "received": "2026-11-17T16:00:00+00:00", "day": "2026-11-17",
                    "month": "2026-11", "numbers": [{"label": "Total Dials", "value": 1200, "kind": "count"},
                                                    {"label": "Connects", "value": 90, "kind": "count"}]})
    _ctx_, snap = _run(kt, pol, st)
    ch = _tab(snap, "Channels")
    check(_row(ch, "Agency", "Calls in its file")[2:4] == [2, kt.GAP],
          "the agency's calls fill from its own files, and a month no file of its holds reads not tracked yet")
    check(_row(ch, "Agency", "Total Dials, as its report says")[2:5] == [1200, kt.NONE_YET, "the events count this too"]
          and _row(ch, "Agency", "Connects, as its report says")[4] == "its own word, not ours",
          "its report's figures show under its own words; its connects are never our conversations")
    check(_row(ch, "Agency", "Leads back from its file")[2] == 1, "its leads back come from the same file")
    td, sb = _tab(snap, "Today"), _tab(snap, "Scoreboard")
    check(_row(td, "Team")[4] == 4 and _row(sb, "Activity", "Dials")[3] == 11 and _row(sb, "Pipeline", "New leads")[3] == 9
          and _row(sb, "Contact", "Correct numbers")[3] == 1,
          "and none of it lands among our callers' dials, the new leads or the correct numbers")
    hc = _tab(snap, "How it is counted")
    agency = _row(hc, "The agency's files")
    check(agency is not None and agency[2] == "Tue Nov 17 11:00 AM" and agency[3].startswith("ok"),
          "the agency's files report when its last file came: %r" % (agency,))


def _behind_and_mapped(check, kt, pol, st) -> None:
    print("  a log copy behind, and a stage map that differs")
    _synced("2026-11-18T15:00:00+00:00")
    try:
        _ctx_, snap = _run(kt, pol, st)
    finally:
        _synced(SYNCED)
    said = [p for p in snap["problems"] if "only through" in p]
    check(said and said[0] in _tab(snap, "Today")["note"] and _row(_tab(snap, "Today"), "Team")[4] == 4,
          "a log copy that is behind is a problem and a note, and what it holds is still counted: %r" % said)
    ctx, snap = _run(kt, pol, _stage(contract_statuses=["under_contract", "Contract Signed"]))
    check(any("contract_statuses" in p and "differ from what the DataSift source counts" in p for p in snap["problems"])
          and _row(_tab(snap, "Scoreboard"), "Pipeline", "Contracts signed")[3] == 2,
          "a contract status the DataSift source does not count is said, and the contracts are counted the source's way")
