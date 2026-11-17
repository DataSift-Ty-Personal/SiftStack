"""Contact rate by week (a friend's Contact Rate Bot, Ty 2026-11-16). Zero network: events seeded into
the log copy, an owner-lane map, its own database. Every id and number here is invented; numbers use the
never-assigned 865-000 exchange.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .selftest_autopilot import EMPLOYEES

NOW = datetime(2026, 11, 16, 14, 10, tzinfo=timezone.utc)          # Monday 9:10am Eastern, week 47
TZ = "America/New_York"


def _call(i, ts, kind, owner, duration=0, direction="outbound"):
    return {"id": "cr%d" % i, "timestamp": ts, "event_type": kind, "resource": owner,
            "payload": {"call": {"direction": direction, "origin_number": "8650000001", "destination_number": "8650000099",
                                 "duration": duration}}}


def _text(i, ts, kind, owner, direction):
    return {"id": "cr%d" % i, "timestamp": ts, "event_type": kind, "resource": owner,
            "payload": {"sms": {"direction": direction, "origin_number": "8650000001", "destination_number": "8650000099"}}}


def _mark(i, ts, owner, to):
    return {"id": "cr%d" % i, "timestamp": ts, "event_type": "owner.phone.status.updated", "resource": owner,
            "payload": {"owner": {"phone": "8650000001", "status": ["UNKNOWN", to]}}}


def _assign(i, ts, record, to):
    return {"id": "cr%d" % i, "timestamp": ts, "event_type": "property.assigned", "resource": record,
            "payload": {"property": {"assigned_to_uuid": [None, to]}}}


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH)
    config.DATA_DIR = Path(tmp) / "contactrate"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        _checks(check)
    finally:
        config.DATA_DIR, config.DB_PATH = keep


def _seed() -> None:
    from .autopilot import activity, lanes
    activity._store([
        _call(1, "2026-10-01 15:00:00", "owner.call.made", "o-old"),          # the copy's first week: older work
        _call(2, "2026-10-27 15:00:00", "owner.call.made", "o-old"),
        _call(3, "2026-10-12 15:00:00", "owner.call.made", "o5"),             # week 42, then again in week 46
        _call(4, "2026-11-10 15:00:00", "owner.call.made", "o5"),
        _call(5, "2026-11-02 15:00:00", "owner.call.made", "o1"),             # week 45: reached by a real call
        _call(6, "2026-11-03 15:00:00", "owner.call.made", "o1"),
        _call(7, "2026-11-03 15:01:00", "owner.call.answered", "o1", duration=90),
        _call(8, "2026-11-03 16:00:00", "owner.call.made", "o2"),             # week 45: only a voicemail greeting
        _call(9, "2026-11-03 16:00:20", "owner.call.answered", "o2", duration=13),
        _call(10, "2026-11-10 15:00:00", "owner.call.made", "o3"),            # week 46: the dialer marked it correct
        _mark(11, "2026-11-12 15:00:00", "o3", "CORRECT"),
        _call(12, "2026-11-11 15:00:00", "owner.call.made", "o4"),            # week 46: a text reply
        _text(13, "2026-11-14 15:00:00", "owner.sms.received", "o4", "inbound"),
        _call(14, "2026-11-04 15:00:00", "owner.call.received", "o6", duration=120, direction="inbound"),   # never dialed
        _call(15, "2026-11-09 15:00:30", "owner.call.made", "o8"),            # answered logged a moment before the dial
        _call(16, "2026-11-09 15:00:00", "owner.call.answered", "o8", duration=75),
        _call(17, "2026-11-16 13:00:00", "owner.call.made", "o9"),            # this week: not a complete week yet
        _assign(21, "2026-10-01 14:00:00", "r-early", "u1"),                  # the assignment copy's first week
        _assign(18, "2026-11-02 14:00:00", "r1", "u1"),                       # o1's record signed out in week 45
        _assign(19, "2026-11-03 14:00:00", "r-unknown", "u1"),                # a record not on the lane map
        _assign(20, "2026-11-04 14:00:00", "r2", ""),                         # an unassignment hands it to nobody
    ], "")
    lanes.set_owner_lane("o1", "ftm", {"r1": "ftm"})
    lanes.set_owner_lane("o2", "top1000", {"r2": "top1000"})
    lanes.set_owner_lane("o3", "obituary", {"r3": "obituary"})
    lanes.set_owner_lane("o-early", "agency", {"r-early": "agency"})


def _checks(check) -> None:
    from .autopilot import contactrate as cr
    from .autopilot import kpi, policy, sweep
    zone = ZoneInfo(TZ)
    check(cr.last_weeks(NOW, 6, zone) == [(2026, w) for w in range(41, 47)]
          and cr.last_weeks(NOW, 2, zone, include_current=True) == [(2026, 46), (2026, 47)],
          "the report covers the six complete weeks before this one, the way the Monday post reads")
    _seed()
    cat = kpi.load()
    m = cr.measure(NOW, TZ, cat, weeks=6)
    weeks = {r["week"]: (r["owners"], r["reached"]) for r in m["weeks"]}
    check(weeks.get(42) == (1, 0) and weeks.get(45) == (2, 1) and weeks.get(46) == (3, 3),
          "each week counts the owners first worked that week: a real call, a correct mark and a text reply reach, a "
          "voicemail greeting does not (%r)" % weeks)
    check(weeks.get(44) == (0, 0), "an owner first seen in the copy's first week is older work, not a new week's member")
    check(m["owners"] == 6 and m["reached"] == 4, "an owner who only called in, and this week's first dial, are in no "
          "complete week: %d of %d" % (m["reached"], m["owners"]))
    check([r for r in m["weeks"] if r["week"] == 41][0]["clipped"] and m["log_starts"].startswith("2026-10-01"),
          "the first week the copy only partly sees is marked, with where the copy starts")
    lane = {r["lane"]: (r["owners"], r["reached"]) for r in m["lanes"]}
    check(lane.get("ftm") == (1, 1) and lane.get("top1000") == (1, 0) and lane.get("obituary") == (1, 1)
          and lane.get("none") == (3, 2), "by lane uses each owner's lane in the nightly map today (%r)" % lane)
    strict = cr.measure(NOW, TZ, cat, weeks=6, count_correct=False)
    check(strict["reached"] == 3, "with count_correct off, a correct mark alone is not a contact")
    signed = cr.measure(NOW, TZ, cat, weeks=6, cohort="signed_out")
    check(signed["owners"] == 1 and signed["reached"] == 1 and signed["unmapped"] == 1,
          "signed out counts the first assignment to a person, mapped to its owner; an unassignment and a record off "
          "the map are not members")
    check(cr.block_problems({"cohort": "everyone"}) and cr.block_problems({"weeks": 40}) and not cr.block_problems({}),
          "a mistyped cohort or week count is refused when the playbook loads")
    pb = {"business": {"tz": TZ, "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
          "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
          "stages": {"cr": {"kind": "contactrate", "owner": "ops", "mode": "plan", "schedule": ["09:10 Mon"], "sync": False}}}
    out = sweep.run("cr", crm=type("NoCRM", (), {"calls": 0})(), now=NOW, pol=policy.parse(pb, EMPLOYEES))
    check(out.head.startswith("Contact rate, weeks 41 to 46: 4 of 6 owners first worked were reached (66.7%).")
          and "first to market 100.0%" in out.head, "the head carries the six weeks overall and by lane: %r" % out.head)
    check(out.lines[:6] == ["Week 41: 0 of 0 reached (-), from 2026-10-01 only", "Week 42: 0 of 1 reached (0.0%)",
                            "Week 43: 0 of 0 reached (-)", "Week 44: 0 of 0 reached (-)", "Week 45: 1 of 2 reached (50.0%)",
                            "Week 46: 3 of 3 reached (100.0%)"],
          "the six lines Slack shows are one a week, oldest first: %r" % out.lines[:6])
    posts = sweep.posts({"id": 1, "workflow": "cr", "source": "schedule"}, out.as_dict())
    check([p["kind"] for p in posts] == ["pipeline"], "the contact rate is a team report: it goes to the pipeline channel")
    check(not any(ch in "\n".join([out.head] + out.lines) for ch in ("—", "–")), "no long dash in the report")
