"""The autopilot's memory, in the brain's SQLite file (tables in store.SCHEMA).

Only live actions are written here. A plan run must never look, to a later live run, like work that
already happened: a record that was only planned for Tracerfy has not been traced by Tracerfy.

    events   what was done to which record, by which stage, at what cost
    phones   every number ever scored, so the same number on another record is not bought twice
    facts    small things learned about a record (its auction dates, its assignee, first seen)
    batches  outreach staged for a person to release (mail drops)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store

BUSINESS_TZ = "America/New_York"


def day(now: datetime | None = None, tz: str = BUSINESS_TZ) -> str:
    """The business-local date. Caps reset at local midnight, not UTC midnight."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date().isoformat()


def event(run_id: str, stage: str, record_uuid: str, action: str, detail: dict | None = None,
          usd: float = 0.0, now: datetime | None = None, tz: str = BUSINESS_TZ) -> None:
    now = now or datetime.now(timezone.utc)
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_events (at, day, run_id, stage, record_uuid, action, detail, usd) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (now.isoformat(timespec="seconds"), day(now, tz), run_id, stage, record_uuid or "", action,
                   json.dumps(detail or {}, default=str), round(float(usd or 0), 4)))


def events(record_uuid: str = "", action: str = "", since: datetime | None = None, stage: str = "") -> list[dict]:
    q, args = "SELECT * FROM ap_events WHERE 1=1", []
    if record_uuid:
        q += " AND record_uuid=?"
        args.append(record_uuid)
    if action:
        q += " AND action=?"
        args.append(action)
    if stage:
        q += " AND stage=?"
        args.append(stage)
    if since:
        q += " AND at>=?"
        args.append(since.isoformat(timespec="seconds"))
    with store._LOCK, store._db() as c:
        out = []
        for r in c.execute(q + " ORDER BY id", args).fetchall():
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except ValueError:
                d["detail"] = {}
            out.append(d)
        return out


def tried_sources(record_uuid: str) -> set[str]:
    return {e["detail"].get("source", "") for e in events(record_uuid, "trace")} - {""}


def handed_off(record_uuid: str, to: str) -> bool:
    return any(e["detail"].get("to") == to for e in events(record_uuid, "handoff"))


def spent(day_: str = "", stage: str = "", record_uuid: str = "", action: str = "", exclude_action: str = "") -> float:
    """Dollars the autopilot actually paid. Mail is outreach spend with its own cap, so callers split
    on action="mail" / exclude_action="mail"."""
    q, args = "SELECT COALESCE(SUM(usd),0) AS s FROM ap_events WHERE 1=1", []
    if day_:
        q += " AND day=?"
        args.append(day_)
    if stage:
        q += " AND stage=?"
        args.append(stage)
    if record_uuid:
        q += " AND record_uuid=?"
        args.append(record_uuid)
    if action:
        q += " AND action=?"
        args.append(action)
    if exclude_action:
        q += " AND action<>?"
        args.append(exclude_action)
    with store._LOCK, store._db() as c:
        return float(c.execute(q, args).fetchone()["s"] or 0)


def counts_since(since: datetime) -> dict:
    """(stage, action) -> (records, usd) since a moment, for the digest."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT stage, action, COUNT(DISTINCT record_uuid) AS n, COALESCE(SUM(usd),0) AS usd "
                         "FROM ap_events WHERE at>=? GROUP BY stage, action",
                         (since.isoformat(timespec="seconds"),)).fetchall()
        return {(r["stage"], r["action"]): (r["n"], float(r["usd"])) for r in rows}


# ------------------------------------------------------------------ facts
def fact(record_uuid: str, key: str, max_age: timedelta | None = None):
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT value, at FROM ap_facts WHERE record_uuid=? AND key=?", (record_uuid, key)).fetchone()
    if not r:
        return None
    if max_age is not None:
        at = datetime.fromisoformat(r["at"])
        if datetime.now(timezone.utc) - at > max_age:
            return None
    try:
        return json.loads(r["value"])
    except ValueError:
        return r["value"]


def set_fact(record_uuid: str, key: str, value) -> None:
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_facts (record_uuid, key, value, at) VALUES (?,?,?,?) "
                  "ON CONFLICT(record_uuid, key) DO UPDATE SET value=excluded.value, at=excluded.at",
                  (record_uuid, key, json.dumps(value, default=str), store.now()))


def first_seen(record_uuid: str, scope: str, now: datetime | None = None) -> datetime:
    """When this stage first saw the record (recorded on first sight, plan or live). Used only for
    waiting periods and the backlog line, never as evidence that anything was done."""
    key = "first_seen:%s" % scope
    v = fact(record_uuid, key)
    if v:
        return datetime.fromisoformat(v)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    set_fact(record_uuid, key, stamp)
    return datetime.fromisoformat(stamp)


# ------------------------------------------------------------------ phones
def phone(number: str) -> dict | None:
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT * FROM ap_phones WHERE number=?", (number,)).fetchone()
        return dict(r) if r else None


def set_phone(number: str, score, tier: str, line_type: str = "", litigator: bool = False) -> None:
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_phones (number, at, score, tier, line_type, litigator) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(number) DO UPDATE SET at=excluded.at, score=excluded.score, tier=excluded.tier, "
                  "line_type=excluded.line_type, litigator=excluded.litigator",
                  (number, store.now(), score, tier, line_type or "", 1 if litigator else 0))


# ------------------------------------------------------------------ batches
def batch_create(stage: str, kind: str, items: list[dict], usd: float) -> int:
    with store._LOCK, store._db() as c:
        cur = c.execute("INSERT INTO ap_batches (created, stage, kind, status, items, usd) VALUES (?,?,?,?,?,?)",
                        (store.now(), stage, kind, "staged", json.dumps(items, default=str), round(usd, 2)))
        return cur.lastrowid


def batch(batch_id: int) -> dict | None:
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT * FROM ap_batches WHERE id=?", (batch_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["items"] = json.loads(d.get("items") or "[]")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def batches(status: str = "", stage: str = "") -> list[dict]:
    q, args = "SELECT id, created, stage, kind, status, usd, items FROM ap_batches WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if stage:
        q += " AND stage=?"
        args.append(stage)
    with store._LOCK, store._db() as c:
        out = []
        for r in c.execute(q + " ORDER BY id", args).fetchall():
            d = dict(r)
            d["count"] = len(json.loads(d.pop("items") or "[]"))
            out.append(d)
        return out


def batch_set(batch_id: int, status: str, result: dict | None = None) -> None:
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_batches SET status=?, released=?, result=? WHERE id=?",
                  (status, store.now(), json.dumps(result or {}, default=str), batch_id))


def staged_record_uuids(stage: str) -> set[str]:
    """Records already sitting in a staged (unreleased) batch, so the next run does not stage them again."""
    out: set[str] = set()
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT items FROM ap_batches WHERE stage=? AND status='staged'", (stage,)).fetchall():
            for it in json.loads(r["items"] or "[]"):
                out.add(it.get("record_uuid") or "")
    return out - {""}
