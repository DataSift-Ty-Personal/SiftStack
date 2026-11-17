"""Contact rate by week (Ty, 2026-11-16, from a friend's "Contact Rate Bot": "for the records that are
actively being marketed to or signed out. Probably a better way to think about it").

A dial count says how hard the team worked. This says whether the owners we picked up became
conversations. Each week is a cohort, counted once and cumulatively: the owners whose first outbound
touch (a dial or a text) in the log copy fell in that week, and how many of them have been reached at
any time since. So an older week keeps rising as callbacks land, and the newest week is still filling.

REACHED is the catalog's own contact (team/kpi.yaml, `contact`: a decision maker reached, once per owner
however many calls it took), read from the log copy as a conversation (an answered call of the
catalog's minimum length either way, or an inbound text: kpi.conversation_events) or a number the dialer
or a person marked CORRECT. A twenty-second "yes, this is me, not interested" is a contact too short to be
a conversation; a voicemail greeting answers a call and is never a contact.

`cohort: signed_out` counts records handed to a person instead: the first assignment in the window,
mapped to the owner through the nightly owner-lane map (an assignment is filed under the record, a call
under the owner).

BY LANE uses the owner's lane in that map today (lead, heirs, hot auction, first to market, obituary,
Top 1,000, agency): the list the owner sits on now, not the one they sat on that week.

The log copy reaches back 45 days on a new box. An owner first seen in the copy's first week may have
been worked long before it, so that week is left out of every cohort and the report says where the
copy starts. Read only: it changes nothing anywhere, in plan mode or live.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity, kpi, lanes
from .stages import Ctx

log = logging.getLogger(__name__)

TOUCHES = ("owner.call.made", "owner.sms.sent")
REACHED_MARKS = {"CORRECT", "CORRECT_DNC"}
GRACE = timedelta(minutes=10)          # an answered call can be logged a moment before its dial
LANE_WORDS = {"lead": "leads", "heirs": "heirs", "hot_auction": "hot auction", "ftm": "first to market",
              "obituary": "obituary", "top1000": "Top 1,000", "agency": "agency and the rest"}
DEFAULTS = {"weeks": 6, "cohort": "worked", "count_correct": True, "lanes_in_head": 3, "first_week_out": True,
            "include_current": False, "sync": True}


def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    out["weeks"] = max(1, min(26, int(out["weeks"])))
    out["cohort"] = str(out["cohort"]).strip().lower()
    return out


def block_problems(block: dict) -> list[str]:
    """A mistyped block is refused when the playbook loads."""
    out = []
    if str(block.get("cohort", "worked")).strip().lower() not in ("worked", "signed_out"):
        out.append("cohort must be worked or signed_out")
    try:
        if not 1 <= int(block.get("weeks", 6)) <= 26:
            out.append("weeks must be 1 to 26")
    except (TypeError, ValueError):
        out.append("weeks must be a whole number")
    return out


# ---------------------------------------------------------------------- weeks
def week_of(when: datetime, zone: ZoneInfo) -> tuple[int, int]:
    iso = when.astimezone(zone).isocalendar()
    return iso[0], iso[1]


def week_start(year: int, week: int, zone: ZoneInfo) -> datetime:
    d = date.fromisocalendar(year, week, 1)
    return datetime(d.year, d.month, d.day, tzinfo=zone)


def last_weeks(now: datetime, n: int, zone: ZoneInfo, include_current: bool = False) -> list[tuple[int, int]]:
    """The last n ISO weeks, oldest first: the n complete weeks before this one (the way the Monday
    report reads), or ending with this week when include_current is set."""
    y, w = week_of(now, zone)
    start = week_start(y, w, zone) - (timedelta(0) if include_current else timedelta(days=7))
    return [week_of(start - timedelta(days=7 * i), zone) for i in range(n - 1, -1, -1)]


# ---------------------------------------------------------------------- the cohorts
def _record_owners() -> dict:
    """record uuid -> owner uuid, from the nightly owner-lane map."""
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT owner_uuid, records FROM ap_owner_lane").fetchall():
            try:
                for rec in json.loads(r["records"] or "{}"):
                    out[rec] = r["owner_uuid"]
            except ValueError:
                continue
    return out


def first_worked(since: datetime, until: datetime, how: str, unmapped: list | None = None) -> dict:
    """owner uuid -> the moment the owner joined a cohort: the first outbound dial or text in the window
    (worked), or the first assignment to a person (signed_out)."""
    first: dict = {}
    if how == "signed_out":
        owners = _record_owners()
        for e in activity.events(("property.assigned",), since=since, until=until):
            if not e["data"].get("to"):
                continue                           # an unassignment hands the record to nobody
            o = owners.get(e["resource"])
            if not o:
                if unmapped is not None:
                    unmapped.append(e["resource"])
                continue
            if o not in first or e["when"] < first[o]:
                first[o] = e["when"]
        return first
    for e in activity.events(TOUCHES, since=since, until=until):
        if e["event_type"] == "owner.call.made" and e["data"].get("direction") == "inbound":
            continue
        o = e["resource"]
        if o and (o not in first or e["when"] < first[o]):
            first[o] = e["when"]
    return first


def reached(first: dict, until: datetime, cat, count_correct: bool = True) -> dict:
    """owner uuid -> the first time the owner was reached at or after joining the cohort."""
    if not first:
        return {}
    since = min(first.values()) - GRACE
    got: dict = {}

    def take(o: str, when: datetime) -> None:
        t = first.get(o)
        if t is not None and when >= t - GRACE and (o not in got or when < got[o]):
            got[o] = when

    for c in kpi.conversation_events(cat, since=since, until=until):
        take(c["owner"], c["when"])
    if count_correct:
        for e in activity.events(activity.PHONE_STATUS, since=since, until=until):
            if str(e["data"].get("to") or "").strip().upper() in REACHED_MARKS:
                take(e["resource"], e["when"])
    return got


def _pct(n: int, d: int) -> str:
    return "%.1f%%" % (100.0 * n / d) if d else "-"


def measure(now: datetime, tz: str, cat, *, weeks: int = 6, cohort: str = "worked", count_correct: bool = True,
            first_week_out: bool = True, include_current: bool = False) -> dict:
    """The whole report as plain values (no request of any kind): weeks, lanes, totals and where the log
    copy starts. Shared by the stage and by a one-off read on the box."""
    zone = ZoneInfo(tz)
    wanted = last_weeks(now, weeks, zone, include_current)
    since = week_start(*wanted[0], zone)
    until = min(now, week_start(*wanted[-1], zone) + timedelta(days=7))
    reach = kpi.log_reach(TOUCHES if cohort == "worked" else ("property.assigned",))
    unmapped: list = []
    # First touches are read from the start of the copy, not the window: an owner first worked before
    # the window is old work, and a touch inside it does not make them a new member of a later week.
    first = first_worked(min(since, reach) if reach else since, until, cohort, unmapped)
    cut = None
    if first_week_out and reach:
        cut = reach + timedelta(days=7)          # an owner first seen in the copy's first week may be older work
        first = {o: t for o, t in first.items() if t >= cut}
    got = reached(first, now, cat, count_correct)  # reached at ANY time since, up to now: each week is cumulative
    rows = []
    for y, w in wanted:
        start = week_start(y, w, zone)
        end = start + timedelta(days=7)
        if cut and end <= cut:
            continue                              # the copy cannot see this week's first touches
        members = [o for o, t in first.items() if start <= t < end]
        rows.append({"year": y, "week": w, "start": start.date().isoformat(), "owners": len(members),
                     "reached": sum(1 for o in members if o in got),
                     "clipped": bool(cut and start < cut), "current": end > now})
    lo = week_start(rows[0]["year"], rows[0]["week"], zone) if rows else until
    members = {o for o, t in first.items() if lo <= t < until}
    lane_rows: dict = {}
    for o in members:
        ln = (lanes.owner_lane(o) or {}).get("lane") or "none"
        row = lane_rows.setdefault(ln, {"lane": ln, "owners": 0, "reached": 0})
        row["owners"] += 1
        row["reached"] += 1 if o in got else 0
    order = list(lanes.ORDER) + ["none"]
    total = sum(r["owners"] for r in rows)
    return {"weeks": rows, "lanes": sorted(lane_rows.values(), key=lambda r: order.index(r["lane"]) if r["lane"] in order else 99),
            "owners": total, "reached": sum(r["reached"] for r in rows), "cohort": cohort,
            "log_starts": reach.isoformat(timespec="seconds") if reach else "", "unmapped": len(set(unmapped))}


# ---------------------------------------------------------------------- the stage
def run_contactrate(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    cat = kpi.for_stage(ctx) or kpi.load()
    if st["sync"]:
        kpi.sync_log(ctx, ["calls", "texts", "state"])
    m = measure(ctx.now, ctx.policy.tz, cat, weeks=st["weeks"], cohort=st["cohort"],
                count_correct=bool(st["count_correct"]), first_week_out=bool(st["first_week_out"]),
                include_current=bool(st["include_current"]))
    ctx.out.found = m["owners"]
    if not m["weeks"]:
        ctx.out.head = "Contact rate: the log copy does not reach back far enough yet to count a whole week."
        return
    who = "owners first worked" if m["cohort"] == "worked" else "owners first signed out to a person"
    first, last = m["weeks"][0], m["weeks"][-1]
    head = "Contact rate, weeks %d to %d: %s of %s %s were reached (%s)." % (
        first["week"], last["week"], kpi.num(m["reached"]), kpi.num(m["owners"]), who, _pct(m["reached"], m["owners"]))
    shown = [r for r in m["lanes"] if r["lane"] != "none" and r["owners"]][:int(st["lanes_in_head"])]
    if shown:
        head += " By lane: " + ", ".join("%s %s" % (LANE_WORDS.get(r["lane"], r["lane"]), _pct(r["reached"], r["owners"]))
                                         for r in shown) + "."
    ctx.out.head = head
    for r in m["weeks"]:                           # the six lines Slack shows: one a week, oldest first
        note = ", this week so far" if r["current"] else (", from %s only" % m["log_starts"][:10]) if r["clipped"] else ""
        ctx.out.line("Week %d: %s of %s reached (%s)%s" % (r["week"], kpi.num(r["reached"]), kpi.num(r["owners"]),
                                                         _pct(r["reached"], r["owners"]), note))
    for r in m["lanes"]:
        if r["owners"]:
            ctx.out.line("%s: %s of %s reached (%s)" % (LANE_WORDS.get(r["lane"], "not on a lane today").capitalize(),
                                                        kpi.num(r["reached"]), kpi.num(r["owners"]), _pct(r["reached"], r["owners"])))
    ctx.out.line("Reached means a conversation (an answered call of %s seconds or more, or a text reply)%s. Each week counts "
                 "the owners first worked that week, once, and how many of them have been reached since." % (
                     cat.definition("conversation")["min_seconds"], ", or a number marked correct" if st["count_correct"] else ""))
    if m["log_starts"]:
        ctx.out.line("The log copy starts %s; owners first seen in its first week are left out." % m["log_starts"][:10])
    if m["unmapped"]:
        ctx.out.tally["assignments to records not on the owner-lane map"] = m["unmapped"]
    kpi.save_report("contact_rate", ctx.now, m)
