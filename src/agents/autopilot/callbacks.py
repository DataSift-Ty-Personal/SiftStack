"""The morning callback list (Ty, 2026-11-15: "setting up any other workflow that a person would be
executing and trying our best to get it autonomous"). The lesson it automates: "The lead manager's
day starts by clearing the queue: every missed call, voicemail and text message in the dialer
inboxes is called back and processed before any task preset."

Read only: it posts one list to the pipeline channel and changes nothing in DataSift, so it is
useful in plan mode from its first morning.

The window opens at the end of the last business day's hours (Friday evening, on a Monday) and
closes now. On the list: an owner who called in and reached nobody, left a voicemail or texted in,
and whom nobody has called or texted back since. Each owner is looked up once, for the house and
who it is assigned to. A quiet morning posts nothing.

The calls and texts come from the brain's shared copy of the account log (activity.py), refreshed
here first: an owner call or text is filed under the OWNER, which is why the house is looked up.
Smarter Contact replies are not in DataSift's log; the texting agent answers and escalates those.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import activity

INBOUND = ("owner.call.missed", "owner.call.received", "owner.sms.received")
REPLIES = ("owner.call.made", "owner.sms.sent")


def window_start(now: datetime, tz: str, hours: tuple, days) -> datetime:
    """The end of the last business day's hours before `now`."""
    zone = ZoneInfo(tz)
    d = now.astimezone(zone).date()
    for _ in range(8):
        d -= timedelta(days=1)
        if d.weekday() in set(days):
            return datetime(d.year, d.month, d.day, int(hours[1]) // 60, int(hours[1]) % 60, tzinfo=zone)
    return now - timedelta(days=1)


def waiting(since: datetime, now: datetime) -> dict:
    """owner uuid -> the inbound calls and texts nobody has answered since, oldest first."""
    out: dict[str, list] = {}
    for e in activity.events(INBOUND, since=since, until=now):
        d = e.get("data") or {}
        if d.get("direction") == "outbound":
            continue
        if e["event_type"] == "owner.call.received" and int(d.get("duration") or 0) > 0:
            continue                          # an inbound call somebody picked up
        out.setdefault(e["resource"], []).append(e)
    for owner, evs in list(out.items()):
        last_in = max(x["when"] for x in evs)
        back = activity.events(REPLIES, resource=owner, since=last_in, until=now)
        if any((x.get("data") or {}).get("direction") != "inbound" for x in back):
            del out[owner]                    # somebody already called or texted back
    return out


def describe(evs: list, tz: str) -> str:
    """"missed call 7:42pm", "2 texts, last 8:05am", "voicemail 6:10pm and a text 7:00am"."""
    zone = ZoneInfo(tz)

    def at(e) -> str:
        return e["when"].astimezone(zone).strftime("%I:%M%p").lstrip("0").lower()

    def what(e) -> str:
        if e["event_type"] == "owner.sms.received":
            return "text"
        return "voicemail" if (e.get("data") or {}).get("recording") else "missed call"

    kinds: dict[str, list] = {}
    for e in evs:
        kinds.setdefault(what(e), []).append(e)
    parts = []
    for k, es in kinds.items():
        last = max(es, key=lambda x: x["when"])
        parts.append(("%s %s" % (k, at(last))) if len(es) == 1 else ("%d %ss, last %s" % (len(es), k, at(last))))
    return " and ".join(parts)


def run_callbacks(ctx) -> None:
    st = ctx.stage
    if st.get("sync", True):
        for group in ("calls", "texts"):
            try:
                activity.sync(ctx.crm, group, now=ctx.now, max_calls=int(st.get("sync_calls", 60)))
            except Exception as exc:  # noqa: BLE001  (a stale copy still says who is waiting)
                ctx.out.problem("could not refresh the %s log before the callback list (%s)" % (group, str(exc)[:80]))
    tz = ctx.policy.tz
    since = window_start(ctx.now, tz, ctx.policy.hours, ctx.policy.days)
    todo = waiting(since, ctx.now)
    ctx.out.found = len(todo)
    if not todo:
        return                                # a quiet morning: no head, so nothing posts
    try:
        names = {u.get("uuid"): (u.get("first_name") or u.get("name") or "") for u in ctx.crm.users() or []}
    except Exception:  # noqa: BLE001
        names = {}
    lines = []
    order = sorted(todo.items(), key=lambda kv: min(x["when"] for x in kv[1]))
    for owner, evs in order[:int(st.get("max_owners", 25))]:
        where, who = "an owner with no house on file", ""
        try:
            rows = ctx.crm.owner_records(owner, cap=5)
            if rows:
                rec = ctx.crm.record(rows[0]["uuid"])
                a = rec.get("address") or {}
                where = ", ".join(p for p in (a.get("street"), a.get("city")) if p) or where
                who = names.get(rec.get("assigned_to") or "", "")
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not look up the house for a waiting owner (%s)" % str(exc)[:80])
        lines.append("%s: %s%s" % (where, describe(evs, tz), (", " + who) if who else ""))
    loc = since.astimezone(ZoneInfo(tz))
    since_txt = "%s %d%s" % (loc.strftime("%A"), loc.hour % 12 or 12, "pm" if loc.hour >= 12 else "am")
    n = len(todo)
    ctx.out.head = "Call back first: %d owner%s reached out since %s and nobody has answered yet." % (
        n, "" if n == 1 else "s", since_txt)
    for ln in lines:
        ctx.out.line(ln)
    if n > len(lines):
        ctx.out.line("and %d more" % (n - len(lines)))
