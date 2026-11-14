"""Daily capture per caller (build plan 6.2) and the first alarms (6.3).

Each evening, after calling hours, every caller's day is written down from the account's own call
log: how many dials, to how many owners, how many were answered, how many of those were real
conversations, and the talk time. Nobody fills in a sheet, and the number does not depend on which
dialer was used, because the log is the same one the attempt keeper reads.

What the log can and cannot say:
  * a DIAL is `owner.call.made`; an ANSWER is the dialer's second event for the same call, with a
    duration. An answer is not a conversation: a voicemail greeting answers too. A CONVERSATION
    here is an answered call that lasted at least `conversation_seconds` (60 by default).
  * leads are counted from status changes to a new lead, for the account, not per caller: the log
    names who dialed but not whose call produced the lead.

Alarms, each one saying what to check (they are judged over the trailing days, never on one day):
  * 500 dials with no new lead: check the caller ID spam flags before blaming the list;
  * talk time low for the dial count: the dials are not reaching people;
  * dials well under the caller's floor.

The day's rows are kept, so a week, a month or 90 days per caller is one query away. A caller is
judged on 90 days, not on a week.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity
from .stages import Ctx

log = logging.getLogger(__name__)

NEW_LEAD = {"new_lead"}


def save(day: str, caller: str, row: dict) -> None:
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_capture (day, caller, dials, answered, conversations, talk_s, owners, numbers) "
                  "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day, caller) DO UPDATE SET dials=excluded.dials, "
                  "answered=excluded.answered, conversations=excluded.conversations, talk_s=excluded.talk_s, "
                  "owners=excluded.owners, numbers=excluded.numbers",
                  (day, caller, row["dials"], row["answered"], row["conversations"], row["talk_s"], row["owners"],
                   row["numbers"]))


def rows(since_day: str = "", caller: str = "") -> list[dict]:
    q, args = "SELECT * FROM ap_capture WHERE 1=1", []
    if since_day:
        q += " AND day>=?"
        args.append(since_day)
    if caller:
        q += " AND caller=?"
        args.append(caller)
    with store._LOCK, store._db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY day, caller", args).fetchall()]


def day_rows(start: datetime, end: datetime, conversation_seconds: int = 60) -> dict:
    """caller -> the day's numbers, from the stored log between two moments."""
    out: dict[str, dict] = defaultdict(lambda: {"dials": 0, "answered": 0, "conversations": 0, "talk_s": 0,
                                                "_owners": set(), "_numbers": set()})
    for e in activity.events((activity.DIAL, "owner.call.answered"), since=start, until=end):
        d = e["data"]
        if d.get("direction") == "inbound":
            continue
        who = (d.get("caller") or "").strip() or "unknown"
        r = out[who]
        if e["event_type"] == activity.DIAL:
            r["dials"] += 1
            r["_owners"].add(e["resource"])
            r["_numbers"].add(d.get("number") or "")
        else:
            secs = int(d.get("duration") or 0)
            r["answered"] += 1
            r["talk_s"] += secs
            if secs >= conversation_seconds:
                r["conversations"] += 1
    done = {}
    for who, r in out.items():
        done[who] = {"dials": r["dials"], "answered": r["answered"], "conversations": r["conversations"],
                     "talk_s": r["talk_s"], "owners": len(r["_owners"] - {""}), "numbers": len(r["_numbers"] - {""})}
    # A dial event carries no caller name (the dialer adds it on the answer). When exactly one caller
    # answered calls that day, the unnamed dials are that caller's.
    named = [w for w in done if w != "unknown"]
    if "unknown" in done and len(named) == 1:
        u, n = done.pop("unknown"), done[named[0]]
        for k in ("dials", "owners", "numbers"):
            n[k] += u[k]
    return done


def run_capture(ctx: Ctx) -> None:
    st = ctx.stage
    zone = ZoneInfo(ctx.policy.tz)
    conv = int(st.get("conversation_seconds", 60))
    for group in ("calls", "state"):
        sync = activity.sync(ctx.crm, group, now=ctx.now, floor_days=int(st.get("floor_days", 45)),
                             max_calls=int(st.get("max_log_calls", 400)))
        if not sync["complete"]:
            ctx.out.problem("the %s log is still catching up (read through %s); today's numbers may be short" % (
                group, sync["through"] or "nothing yet"))
    local_now = ctx.now.astimezone(zone)
    today0 = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # The run after calling hours captures today; an early-morning run captures yesterday. Either
    # way the last `days` whole days are (re)written, so a late-arriving log row is never lost.
    last = today0 + timedelta(days=1) if local_now.hour >= int(st.get("day_ends_hour", 19)) else today0
    days = int(st.get("days", 3))
    shown = None
    for i in range(days, 0, -1):
        start, end = last - timedelta(days=i), last - timedelta(days=i - 1)
        day = start.date().isoformat()
        got = day_rows(start, end, conv)
        for who, r in got.items():
            save(day, who, r)          # the capture is the autopilot's own record, written in plan mode too
        if i == 1:
            shown = (day, got)
    day, got = shown
    total = sum(r["dials"] for r in got.values())
    ctx.out.found = total
    for who, r in sorted(got.items(), key=lambda kv: -kv[1]["dials"]):
        ctx.out.line("%s: %d dials to %d owners, %d answered, %d conversation(s), %d min of talk" % (
            who, r["dials"], r["owners"], r["answered"], r["conversations"], round(r["talk_s"] / 60)))
    leads = [e for e in activity.events("property.status.updated", since=last - timedelta(days=1), until=last)
             if str(e["data"].get("to") or "").lower() in NEW_LEAD]
    ctx.out.tally["new leads that day (the whole account)"] = len({e["resource"] for e in leads})

    # ---- alarms, over the trailing days
    floors = st.get("floors") or {}
    for who, r in got.items():
        f = floors.get("dials")
        if who != "unknown" and f and local_now.weekday() < 5 and 0 < r["dials"] < float(f) * float(st.get("floor_slack", 0.7)):
            ctx.out.problem("%s made %d dials on %s, under the floor of %d" % (who, r["dials"], day, int(f)))
        if r["dials"] >= int(st.get("min_dials_for_talk_alarm", 80)) and r["talk_s"] / max(1, r["dials"]) < float(st.get("min_talk_s_per_dial", 4)):
            ctx.out.problem("%s: %d dials and only %d min of talk on %s. The dials are not reaching people: check the "
                            "caller ID spam flags and the number tiers" % (who, r["dials"], round(r["talk_s"] / 60), day))
    since_lead = _dials_since_last_lead(last)
    limit = int(st.get("dials_without_a_lead", 500))
    if since_lead is not None and since_lead >= limit:
        ctx.out.problem("%d dials since the last new lead (the alarm is %d). Check the caller ID spam flags first, then the list"
                        % (since_lead, limit))
    ctx.out.acted = 0
    if not got:
        ctx.out.head = "No dials on %s." % day
    else:
        ctx.out.head = "%s: %d dials by %d caller(s), %d new lead(s)." % (
            day, total, len(got), ctx.out.tally["new leads that day (the whole account)"])


def _dials_since_last_lead(until: datetime) -> int | None:
    """Outbound dials since the last status change to a new lead, as far back as the log reaches.
    None when the log holds no lead at all (it cannot tell 'never' from 'before the log starts')."""
    leads = [e for e in activity.events("property.status.updated", until=until)
             if str(e["data"].get("to") or "").lower() in NEW_LEAD]
    if not leads:
        return None
    return sum(1 for e in activity.events(activity.DIAL, since=leads[-1]["when"], until=until)
               if e["data"].get("direction") != "inbound")


def recent_lines(days: int = 1) -> list[str]:
    """The last captured day(s) as plain lines, for the morning digest."""
    all_rows = rows()
    if not all_rows:
        return []
    last_days = sorted({r["day"] for r in all_rows})[-days:]
    out = []
    for r in all_rows:
        if r["day"] in last_days and r["dials"]:
            out.append("Calls %s, %s: %d dials, %d answered, %d conversation(s), %d min" % (
                r["day"][5:], r["caller"], r["dials"], r["answered"], r["conversations"], round(r["talk_s"] / 60)))
    return out
