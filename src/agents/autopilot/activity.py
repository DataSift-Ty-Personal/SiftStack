"""One shared copy of the account's activity log (build plan 0.5, 0.12, 0.14, 0.23).

Four stages need the same facts: the attempt keeper (who was dialed on which day), the sequence
watchdog (did a status or tag change produce what it should have), the touch audit (was every call
and text allowed) and the revocation step (who just said no, died, sold or became a lead). Reading
the log once per stage would spend the request budget four times, so it is read ONCE into the
brain's own database and every stage reads it there.

What the published route gives (GET /api/internal/property/logs/, probed 2026-11-12):
  * newest first, 100 rows a page at most, a cursor instead of an offset;
  * `any_event_type` is the only event filter it honors (event_type and event_types are dropped
    without an error), and without it nine rows in ten are auto-update noise;
  * a call is an OWNER event: `resource` is the owner's uuid, never a property's. One dial is
    `owner.call.made`; an answer is a second event, `owner.call.answered`, with the duration and
    the recording. Inbound calls are `owner.call.received` and `owner.call.missed`.

The sync walks forward one day at a time, oldest first, and records how far it got only when a
day is complete. A run that stops early (the request cap, a restart) resumes at the same day, so
a long first backfill can never leave a hole behind a newer high-water mark.

Only what the stages use is kept: numbers, direction, duration, who dialed, the status or tag that
changed. Names and message bodies are not stored.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store

log = logging.getLogger(__name__)

DIAL = "owner.call.made"
CALLS = (DIAL, "owner.call.answered", "owner.call.noanswer", "owner.call.missed", "owner.call.received")
COUNTERS = ("property.predictivecall.attempts.increased", "property.predictivecall.attempts.decreased")
PHONE_STATUS = ("owner.phone.status.updated",)
STATE = ("property.status.updated", "property.tags.added", "property.tags.removed", "property.assigned",
         "property.lists.added", "property.created")
TEXTS = ("owner.sms.sent", "owner.sms.received", "owner.sms.failed", "owner.sms.delivered")
BOARD = ("card.created", "card.moved", "task.created", "task.deleted", "task.completed")

GROUPS = {
    "calls": CALLS + COUNTERS + PHONE_STATUS,
    "state": STATE,
    "texts": TEXTS,
    "board": BOARD,
}
FLOOR_DAYS = 45          # how far back the first sync reaches
KEEP_DAYS = 200
# Each sync re-reads this much before its high-water mark. A dialer reports a call with the time it
# was made, and the row can land in the log later than that; rows are stored by id, so reading
# the same hours again costs a few pages and never a duplicate.
OVERLAP = timedelta(hours=6)


def _digits(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def parse_ts(text: str) -> datetime | None:
    """The log writes UTC as 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return datetime.fromisoformat(str(text).strip().replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair(v):
    """A changed field arrives as [old, new]; one value means only the new one was logged."""
    if isinstance(v, list):
        if len(v) >= 2:
            return v[0], v[-1]
        if len(v) == 1:
            return None, v[0]
        return None, None
    return None, v


def slim(e: dict) -> tuple[str, dict]:
    """(resource to file the event under, the few fields the stages use). Card and task events are
    filed under the PROPERTY they belong to, so one lookup by record finds them."""
    et = e.get("event_type") or ""
    p = e.get("payload") or {}
    resource = e.get("resource") or ""
    extra = p.get("extra_info") or {}
    by = ((extra.get("source") or {}).get("title") or "") if isinstance(extra.get("source"), dict) else ""
    if et.startswith("owner.call."):
        c = p.get("call") or {}
        inbound = (c.get("direction") or "").lower().startswith("in")
        user = c.get("user") or {}
        ext = c.get("external_user") or {}
        return resource, {"number": _digits(c.get("origin_number") if inbound else c.get("destination_number")),
                          "line": _digits(c.get("destination_number") if inbound else c.get("origin_number")),
                          "direction": "inbound" if inbound else "outbound", "duration": c.get("duration") or 0,
                          "source": c.get("source") or e.get("source") or "", "status": (c.get("extra") or {}).get("status") or "",
                          "caller": (user.get("first_name") or ext.get("name") or ""), "caller_uuid": user.get("uuid") or "",
                          "call": c.get("uuid") or "", "recording": c.get("recording") or ""}
    if et.startswith("owner.sms."):
        s = p.get("sms") or {}
        inbound = (s.get("direction") or "").lower().startswith("in")
        return resource, {"number": _digits(s.get("origin_number") if inbound else s.get("destination_number")),
                          "direction": "inbound" if inbound else "outbound", "status": s.get("status") or "",
                          "source": s.get("source") or e.get("source") or "", "integration": s.get("integration") or ""}
    if et == "owner.phone.status.updated":
        o = p.get("owner") or {}
        old, new = _pair(o.get("status"))
        return resource, {"number": _digits(o.get("phone")), "from": old or "", "to": new or ""}
    if et == "property.status.updated":
        old, new = _pair((p.get("property") or {}).get("status"))
        return resource, {"from": old or "", "to": new or "", "by": by}
    if et in ("property.tags.added", "property.tags.removed"):
        return resource, {"tags": [str(t) for t in (p.get("tags") or [])]}
    if et == "property.lists.added":
        return resource, {"lists": [str(t) for t in (p.get("lists") or p.get("system_lists") or [])]}
    if et == "property.assigned":
        old, new = _pair((p.get("property") or {}).get("assigned_to_uuid"))
        return resource, {"from": old or "", "to": new or "", "by": by}
    if et.startswith("property.predictivecall.attempts."):
        old, new = _pair((p.get("property") or {}).get("predictivecall_attempts"))
        return resource, {"from": old, "to": new}
    if et.startswith("card."):
        c = p.get("card") or {}
        ec = extra.get("card") or {}
        prop = ec.get("prop") or c.get("prop") or c.get("prop_uuid") or ""
        col_old, col_new = _pair(ec.get("column"))
        board = ec.get("board")
        return (prop or resource), {"card": resource, "column_from": col_old or "", "column": col_new or "",
                                    "board": (board[-1] if isinstance(board, list) and board else board) or "", "by": by}
    if et.startswith("task."):
        t = p.get("task") or {}
        return (t.get("assigned_to_property") or resource), {"task": resource, "title": t.get("title") or "",
                                                              "kind": t.get("event_type") or "", "due": t.get("due_date") or "",
                                                              "by": by}
    return resource, {}


def _store(rows: list[dict]) -> int:
    n = 0
    with store._LOCK, store._db() as c:
        for e in rows:
            at = parse_ts(e.get("timestamp") or "")
            if not at or not e.get("id"):
                continue
            resource, data = slim(e)
            cur = c.execute("INSERT OR IGNORE INTO ap_log (id, at, event_type, resource, resource_type, source, author, data) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (str(e["id"]), at.isoformat(timespec="seconds"), e.get("event_type") or "", resource,
                             e.get("resource_type") or "", e.get("source") or "",
                             "user" if "@" in str(e.get("author") or "") else str(e.get("author") or "")[:40],
                             json.dumps(data, default=str)))
            n += cur.rowcount or 0
    return n


def reset(groups=None) -> None:
    """Forget the stored log and how far each group was read, so the next sync starts over. For a
    box whose log copy is suspect, and for the selftest."""
    names = list(groups or GROUPS)
    types = [t for g in names for t in GROUPS[g]]
    with store._LOCK, store._db() as c:
        # Only the rows of the groups being forgotten: dropping every row while clearing one group's
        # mark would leave the other groups believing they still hold what was just deleted.
        c.execute("DELETE FROM ap_log WHERE event_type IN (%s)" % ",".join("?" * len(types)), types)
    for g in names:
        store.kv_set("autopilot:log:%s:through" % g, "")
        store.kv_set("autopilot:log:%s:partial" % g, "")


def synced_through(group: str) -> datetime | None:
    v = store.kv_get("autopilot:log:%s:through" % group)
    return datetime.fromisoformat(v) if v else None


def sync(crm, group: str, now: datetime | None = None, floor_days: int = FLOOR_DAYS, max_calls: int = 400) -> dict:
    """Bring one group of event types up to `now`. Returns {added, calls, through, complete}."""
    now = now or datetime.now(timezone.utc)
    types = list(GROUPS[group])
    start = synced_through(group)
    # How far back the FIRST sync reaches belongs to the log, not to whichever stage happens to run
    # first: a stage that only needs three days must not leave the next one with a three-day log.
    floor_days = max(int(floor_days), FLOOR_DAYS)
    start = (start - OVERLAP) if start else now - timedelta(days=floor_days)
    added = calls = 0
    complete = True
    part_key = "autopilot:log:%s:partial" % group
    while start < now:
        end = min(start + timedelta(days=1), now)
        # A day can hold more than one run may read (a 15,000-record pull writes tens of thousands of
        # events in a day). The log comes newest first, so a run that stops early has everything
        # from the window's end down to the oldest row it saw. That point is kept, and the next run
        # reads only what is older. Without it the same day was read from the top every time and
        # the mark never moved past it.
        win_end, mark_end, upto = end, end, None
        try:
            part = json.loads(store.kv_get(part_key) or "{}")
        except ValueError:
            part = {}
        if part.get("start") == start.isoformat(timespec="seconds") and part.get("upto"):
            upto = datetime.fromisoformat(part["upto"])
            win_end = upto + timedelta(seconds=1)
            mark_end = datetime.fromisoformat(part["end"])
        cursor, day_done = "", True
        while True:
            if calls >= max_calls:
                day_done = False
                break
            r = crm.logs({"timestamp": [_z(start), _z(win_end)], "any_event_type": types}, limit=100, cursor=cursor)
            calls += 1
            rows = r.get("results") or []
            added += _store(rows)
            for e in rows:
                at = parse_ts(e.get("timestamp") or "")
                if at and (upto is None or at < upto):
                    upto = at
            cursor = r.get("next_cursor") or ""
            if not cursor or not rows:
                break
        if not day_done:
            if upto is not None:
                store.kv_set(part_key, json.dumps({"start": start.isoformat(timespec="seconds"),
                                                   "end": mark_end.isoformat(timespec="seconds"),
                                                   "upto": upto.isoformat(timespec="seconds")}))
            complete = False
            break
        store.kv_set(part_key, "")
        store.kv_set("autopilot:log:%s:through" % group, mark_end.isoformat(timespec="seconds"))
        start = mark_end
    if complete:
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        with store._LOCK, store._db() as c:
            c.execute("DELETE FROM ap_log WHERE at<?", (cutoff,))
    through = synced_through(group)
    return {"group": group, "added": added, "calls": calls, "complete": complete,
            "through": through.isoformat(timespec="seconds") if through else ""}


def events(types=(), resource: str = "", since: datetime | None = None, until: datetime | None = None,
           limit: int = 0) -> list[dict]:
    """Stored events, oldest first. `types` may be one type or several."""
    if isinstance(types, str):
        types = (types,)
    q, args = "SELECT * FROM ap_log WHERE 1=1", []
    if types:
        q += " AND event_type IN (%s)" % ",".join("?" * len(types))
        args += list(types)
    if resource:
        q += " AND resource=?"
        args.append(resource)
    if since:
        q += " AND at>=?"
        args.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
    if until:
        q += " AND at<?"
        args.append(until.astimezone(timezone.utc).isoformat(timespec="seconds"))
    q += " ORDER BY at, id"
    if limit:
        q += " LIMIT %d" % int(limit)
    out = []
    with store._LOCK, store._db() as c:
        for r in c.execute(q, args).fetchall():
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except ValueError:
                d["data"] = {}
            d["when"] = datetime.fromisoformat(d["at"])
            out.append(d)
    return out


def dial_days(owner_uuid: str, since: datetime | None = None, tz: str = "America/New_York") -> dict:
    """business-local date -> the set of numbers dialed that day, for one owner. An outbound dial
    is `owner.call.made`; the answered event repeats the same call and is not counted twice."""
    out: dict[str, set] = {}
    zone = ZoneInfo(tz)
    for e in events(DIAL, resource=owner_uuid, since=since):
        if e["data"].get("direction") == "inbound":
            continue
        day = e["when"].astimezone(zone).date().isoformat()
        out.setdefault(day, set()).add(e["data"].get("number") or "")
    return out


def owners_dialed(since: datetime, until: datetime | None = None) -> set[str]:
    q, args = "SELECT DISTINCT resource FROM ap_log WHERE event_type=? AND at>=?", [
        DIAL, since.astimezone(timezone.utc).isoformat(timespec="seconds")]
    if until:
        q += " AND at<?"
        args.append(until.astimezone(timezone.utc).isoformat(timespec="seconds"))
    with store._LOCK, store._db() as c:
        return {r["resource"] for r in c.execute(q, args).fetchall() if r["resource"]}
