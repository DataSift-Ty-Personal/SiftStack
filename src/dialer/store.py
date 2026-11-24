"""SQLite on the dialer's own volume.

One file, WAL mode, a connection per thread. Twilio's webhooks, the caller's browser and the write-back
worker all touch it at once, so every write is a short transaction and the two places a race would
cost something (claiming the next record in a power-dialer queue, and the first dial of a record in a
day, which is what raises its attempt counter) take an IMMEDIATE lock.

What is kept: the callers and their sign-ins, every call with every Twilio callback it produced, the
queue each power-dialer session walks, the write-back jobs, and one row per DataSift write with the
value it replaced (so a wrong disposition can be put back).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from . import config

_local = threading.local()
_INIT_LOCK = threading.Lock()
_READY: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
    identity    TEXT PRIMARY KEY,            -- the Twilio client identity: lowercase letters, digits, _
    name        TEXT NOT NULL,               -- the first name the team knows them by
    role        TEXT NOT NULL DEFAULT 'caller',   -- caller | manager (a manager may listen in)
    caller_id   TEXT,                        -- the number they dial from, 10 digits
    user_uuid   TEXT,                        -- their DataSift user, for tasks
    active      INTEGER NOT NULL DEFAULT 1,
    vm_file     TEXT,                        -- the voicemail drop recording on the volume
    vm_token    TEXT,                        -- the unguessable name Twilio fetches it by
    seen_at     TEXT,                        -- last time their phone page asked for a token
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signin_codes (
    code_hash   TEXT PRIMARY KEY,
    identity    TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id          TEXT PRIMARY KEY,
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
    id              TEXT PRIMARY KEY,
    identity        TEXT,                    -- the caller
    direction       TEXT NOT NULL DEFAULT 'outbound',
    kind            TEXT NOT NULL DEFAULT 'manual',  -- manual | queue | callback | inbound | monitor | vm_record
    session_id      INTEGER,
    item_id         INTEGER,
    record_uuid     TEXT,
    owner_uuid      TEXT,
    number          TEXT,                    -- the homeowner's 10 digits
    from_number     TEXT,                    -- our number on the call
    status          TEXT NOT NULL DEFAULT 'prepared',
    agent_sid       TEXT,                    -- the caller's browser leg
    lead_sid        TEXT,                    -- the homeowner's leg
    conference_name TEXT,
    conference_sid  TEXT,
    record_allowed  INTEGER NOT NULL DEFAULT 0,
    amd             TEXT,                    -- what answering-machine detection said
    started_at      TEXT,
    answered_at     TEXT,
    ended_at        TEXT,
    seconds         INTEGER,
    end_status      TEXT,                    -- completed | no-answer | busy | failed | canceled
    sip_code        TEXT,
    recording_sid   TEXT,
    recording_seconds INTEGER,
    voicemail_sid   TEXT,                    -- an inbound caller's message
    vm_dropped      INTEGER NOT NULL DEFAULT 0,
    disposition     TEXT,
    note            TEXT,
    callback_at     TEXT,
    disposed_at     TEXT,
    disposed_by     TEXT,
    writeback_state TEXT NOT NULL DEFAULT '',
    warnings        TEXT,
    detail          TEXT,                    -- the record summary shown to the caller (JSON)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_number ON calls(number, created_at);
CREATE INDEX IF NOT EXISTS idx_calls_identity ON calls(identity, created_at);
CREATE INDEX IF NOT EXISTS idx_calls_record ON calls(record_uuid);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_sid);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent_sid);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);

CREATE TABLE IF NOT EXISTS call_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT,
    source      TEXT NOT NULL,               -- twilio | caller | system
    kind        TEXT NOT NULL,
    payload     TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_call ON call_events(call_id, id);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT,
    kind        TEXT NOT NULL,               -- writeback | texting | kpi | slack | fetch_vm | fetch_voicemail
    payload     TEXT,
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued | done | failed | dry
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  TEXT,
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(state, not_before);

CREATE TABLE IF NOT EXISTS writes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT,
    job_id      INTEGER,
    target      TEXT,                        -- record:<uuid> | owner:<uuid> | phone:<last4>
    action      TEXT NOT NULL,
    before      TEXT,
    after       TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    verified    INTEGER,
    mode        TEXT NOT NULL,               -- live | dry
    detail      TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_writes_call ON writes(call_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    TEXT NOT NULL,
    preset      TEXT NOT NULL,
    preset_uuid TEXT,
    state       TEXT NOT NULL DEFAULT 'open',     -- open | paused | done
    allow       TEXT,                        -- the gate rules this preset lifts (JSON list)
    loaded      INTEGER NOT NULL DEFAULT 0,  -- rows read from the preset so far
    total       INTEGER,                     -- the preset's count when it was opened
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    record_uuid TEXT NOT NULL,
    row         TEXT,                        -- the search row (the only place the registry flag shows)
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | done | skipped
    reason      TEXT,
    numbers     TEXT,                        -- the callable numbers, in the order they are dialed (JSON)
    next_number INTEGER NOT NULL DEFAULT 0,
    owner_uuid  TEXT,
    claimed_by  TEXT,
    claimed_at  TEXT,
    done_at     TEXT,
    UNIQUE(session_id, record_uuid)
);
CREATE INDEX IF NOT EXISTS idx_items_next ON queue_items(session_id, state, seq);

CREATE TABLE IF NOT EXISTS attempt_days (
    record_uuid TEXT NOT NULL,
    day         TEXT NOT NULL,
    PRIMARY KEY (record_uuid, day)
);
CREATE TABLE IF NOT EXISTS handraises (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe      TEXT UNIQUE,
    phone       TEXT NOT NULL,
    record_uuid TEXT,
    owner_uuid  TEXT,
    body        TEXT,
    program     TEXT,
    source      TEXT,
    received_at TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',     -- open | called | closed
    claimed_by  TEXT,
    handled_at  TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k           TEXT PRIMARY KEY,
    v           TEXT,
    at          TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _connect() -> sqlite3.Connection:
    path = config.S.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=OFF")
    return c


def conn() -> sqlite3.Connection:
    """This thread's connection to the configured file (a new file after config.reload gets a new one)."""
    key = str(config.S.db_path)
    have = getattr(_local, "conns", None)
    if have is None:
        have = _local.conns = {}
    c = have.get(key)
    if c is None:
        c = have[key] = _connect()
    if key not in _READY:
        with _INIT_LOCK:
            if key not in _READY:
                c.executescript(SCHEMA)
                _READY.add(key)
    return c


def init() -> None:
    conn()


@contextmanager
def tx(immediate: bool = False) -> Iterator[sqlite3.Connection]:
    c = conn()
    c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _row(r) -> Optional[dict]:
    return dict(r) if r is not None else None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- callers
def add_caller(identity: str, name: str, role: str = "caller", caller_id: str = "", user_uuid: str = "") -> dict:
    # Twilio client identities take ASCII letters, digits and underscores only: a hyphen (or an accented
    # letter) makes every call to that browser fail.
    ident = "".join(ch for ch in identity.lower() if (ch.isascii() and ch.isalnum()) or ch == "_")[:121]
    if not ident:
        raise ValueError("an identity needs at least one letter or digit")
    if role not in ("caller", "manager"):
        raise ValueError("role is caller or manager")
    with tx() as c:
        c.execute("INSERT INTO callers(identity, name, role, caller_id, user_uuid, created_at) VALUES (?,?,?,?,?,?)"
                  " ON CONFLICT(identity) DO UPDATE SET name=excluded.name, role=excluded.role,"
                  " caller_id=COALESCE(NULLIF(excluded.caller_id,''), callers.caller_id),"
                  " user_uuid=COALESCE(NULLIF(excluded.user_uuid,''), callers.user_uuid), active=1",
                  (ident, name.strip() or ident, role, caller_id, user_uuid, now()))
    return get_caller(ident)


def get_caller(identity: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM callers WHERE identity=?", (identity,)).fetchone())


def callers(active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM callers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return [dict(r) for r in conn().execute(q)]


def caller_by_number(number: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM callers WHERE caller_id=? AND active=1 ORDER BY seen_at DESC",
                               (number,)).fetchone())


def update_caller(identity: str, **fields) -> None:
    allowed = {"name", "role", "caller_id", "user_uuid", "active", "vm_file", "vm_token", "seen_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with tx() as c:
        c.execute("UPDATE callers SET %s WHERE identity=?" % ", ".join("%s=?" % k for k in sets),
                  (*sets.values(), identity))


def caller_by_vm_token(token: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM callers WHERE vm_token=?", (token,)).fetchone())


# ---------------------------------------------------------------------- sign-in
def new_code(identity: str, days: int = 7) -> str:
    """A one-time sign-in code. Only its hash is kept."""
    code = secrets.token_urlsafe(24)
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("INSERT INTO signin_codes(code_hash, identity, expires_at) VALUES (?,?,?)", (_hash(code), identity, exp))
    return code


def use_code(code: str) -> Optional[str]:
    """The identity a code signs in, once. A used or expired code signs in nobody."""
    with tx(immediate=True) as c:
        r = c.execute("SELECT * FROM signin_codes WHERE code_hash=?", (_hash(code or ""),)).fetchone()
        if r is None or r["used_at"] or (parse(r["expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc):
            return None
        c.execute("UPDATE signin_codes SET used_at=? WHERE code_hash=?", (now(), r["code_hash"]))
        return r["identity"]


def new_web_session(identity: str, days: int) -> str:
    sid = secrets.token_urlsafe(24)
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("INSERT INTO web_sessions(id, identity, created_at, expires_at) VALUES (?,?,?,?)", (sid, identity, now(), exp))
    return sid


def web_session(sid: str) -> Optional[str]:
    r = conn().execute("SELECT * FROM web_sessions WHERE id=?", (sid,)).fetchone()
    if r is None or r["revoked"] or (parse(r["expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc):
        return None
    cal = get_caller(r["identity"])
    return r["identity"] if cal and cal["active"] else None


def revoke_sessions(identity: str) -> int:
    with tx() as c:
        return c.execute("UPDATE web_sessions SET revoked=1 WHERE identity=? AND revoked=0", (identity,)).rowcount


# ---------------------------------------------------------------------- calls
CALL_FIELDS = {"identity", "direction", "kind", "session_id", "item_id", "record_uuid", "owner_uuid", "number",
               "from_number", "status", "agent_sid", "lead_sid", "conference_name", "conference_sid", "record_allowed",
               "amd", "started_at", "answered_at", "ended_at", "seconds", "end_status", "sip_code", "recording_sid",
               "recording_seconds", "voicemail_sid", "vm_dropped", "disposition", "note", "callback_at", "disposed_at",
               "disposed_by", "writeback_state", "warnings", "detail"}


def new_call(**fields) -> str:
    cid = uuid.uuid4().hex[:20]
    fields = {k: v for k, v in fields.items() if k in CALL_FIELDS}
    for k in ("warnings", "detail"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    fields.setdefault("conference_name", "dlr_" + cid)
    ts = now()
    cols = ["id", "created_at", "updated_at", *fields]
    with tx() as c:
        c.execute("INSERT INTO calls(%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                  (cid, ts, ts, *fields.values()))
    return cid


def get_call(call_id: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM calls WHERE id=?", (call_id or "",)).fetchone())


def call_by_sid(sid: str) -> Optional[dict]:
    if not sid:
        return None
    return _row(conn().execute("SELECT * FROM calls WHERE lead_sid=? OR agent_sid=? ORDER BY created_at DESC LIMIT 1",
                               (sid, sid)).fetchone())


def update_call(call_id: str, **fields) -> None:
    sets = {k: v for k, v in fields.items() if k in CALL_FIELDS}
    for k in ("warnings", "detail"):
        if k in sets and not isinstance(sets[k], str):
            sets[k] = json.dumps(sets[k])
    if not sets:
        return
    sets["updated_at"] = now()
    with tx() as c:
        c.execute("UPDATE calls SET %s WHERE id=?" % ", ".join("%s=?" % k for k in sets), (*sets.values(), call_id))


def set_call_if(call_id: str, expect: tuple, **fields) -> bool:
    """Update only while the call is in one of `expect` statuses (a late callback never rewinds a call)."""
    sets = {k: v for k, v in fields.items() if k in CALL_FIELDS}
    sets["updated_at"] = now()
    with tx() as c:
        n = c.execute("UPDATE calls SET %s WHERE id=? AND status IN (%s)" % (
            ", ".join("%s=?" % k for k in sets), ", ".join("?" * len(expect))), (*sets.values(), call_id, *expect)).rowcount
    return bool(n)


def dials_since(number: str, since_iso: str) -> int:
    """Outbound calls that actually reached Twilio for this number since a moment."""
    return int(conn().execute(
        "SELECT COUNT(*) FROM calls WHERE number=? AND direction='outbound' AND created_at>=?"
        " AND status NOT IN ('prepared','blocked') AND kind NOT IN ('monitor','vm_record')",
        (number, since_iso)).fetchone()[0])


def owner_dials_since(owner_uuid: str, since_iso: str) -> int:
    """Outbound dials to any of an owner's numbers since a moment (the state per-owner caps)."""
    return int(conn().execute(
        "SELECT COUNT(*) FROM calls WHERE owner_uuid=? AND direction='outbound' AND created_at>=?"
        " AND status NOT IN ('prepared','blocked') AND kind NOT IN ('monitor','vm_record')",
        (owner_uuid, since_iso)).fetchone()[0])


def last_inbound(number: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM calls WHERE number=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1",
                               (number,)).fetchone())


def last_outbound(number: str) -> Optional[dict]:
    return _row(conn().execute(
        "SELECT * FROM calls WHERE number=? AND direction='outbound' AND kind NOT IN ('monitor','vm_record')"
        " AND status NOT IN ('prepared','blocked') ORDER BY created_at DESC LIMIT 1", (number,)).fetchone())


def active_calls() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM calls WHERE status IN ('connecting','ringing','answered') AND kind NOT IN ('monitor','vm_record')"
        " ORDER BY created_at")]


def recent_calls(identity: str = "", limit: int = 50, since: str = "") -> list[dict]:
    q, args = "SELECT * FROM calls WHERE kind NOT IN ('monitor','vm_record')", []
    if identity:
        q += " AND identity=?"
        args.append(identity)
    if since:
        q += " AND created_at>=?"
        args.append(since)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn().execute(q, args)]


def stale_calls(older_than_minutes: int = 5) -> list[dict]:
    """Calls Twilio should have finished by now but no final callback ever arrived for."""
    cut = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")
    return [dict(r) for r in conn().execute(
        "SELECT * FROM calls WHERE status IN ('connecting','ringing','answered') AND updated_at<?", (cut,))]


def add_event(call_id: str, source: str, kind: str, payload: Any = None) -> None:
    with tx() as c:
        c.execute("INSERT INTO call_events(call_id, source, kind, payload, at) VALUES (?,?,?,?,?)",
                  (call_id, source, kind, json.dumps(payload) if payload is not None else None, now()))


def events_for(call_id: str) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM call_events WHERE call_id=? ORDER BY id", (call_id,))]


# ---------------------------------------------------------------------- jobs and the write log
def add_job(call_id: str, kind: str, payload: Any = None, delay_s: int = 0) -> int:
    nb = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
    with tx() as c:
        cur = c.execute("INSERT INTO jobs(call_id, kind, payload, not_before, created_at) VALUES (?,?,?,?,?)",
                        (call_id, kind, json.dumps(payload or {}), nb, now()))
        return int(cur.lastrowid)


def due_jobs(limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM jobs WHERE state='queued' AND (not_before IS NULL OR not_before<=?) ORDER BY id LIMIT ?",
        (now(), int(limit)))]


def claim_job(job_id: int) -> bool:
    with tx(immediate=True) as c:
        return bool(c.execute("UPDATE jobs SET state='running', attempts=attempts+1 WHERE id=? AND state='queued'",
                              (job_id,)).rowcount)


def finish_job(job_id: int, state: str, result: Any = None, error: str = "", retry_in_s: int = 0) -> None:
    if retry_in_s:
        nb = (datetime.now(timezone.utc) + timedelta(seconds=retry_in_s)).isoformat(timespec="seconds")
        with tx() as c:
            c.execute("UPDATE jobs SET state='queued', not_before=?, error=? WHERE id=?", (nb, error[:500], job_id))
        return
    with tx() as c:
        c.execute("UPDATE jobs SET state=?, result=?, error=?, done_at=? WHERE id=?",
                  (state, json.dumps(result) if result is not None else None, error[:500] or None, now(), job_id))


def requeue_running() -> int:
    """A job a restart cut off goes back in the queue once."""
    with tx() as c:
        return c.execute("UPDATE jobs SET state='queued' WHERE state='running'").rowcount


def jobs_for(call_id: str) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM jobs WHERE call_id=? ORDER BY id", (call_id,))]


def add_write(call_id: str, job_id: int, target: str, action: str, before: Any, after: Any, ok: bool,
              mode: str, verified: Optional[bool] = None, detail: str = "") -> None:
    with tx() as c:
        c.execute("INSERT INTO writes(call_id, job_id, target, action, before, after, ok, verified, mode, detail, at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (call_id, job_id, target, action, json.dumps(before), json.dumps(after), 1 if ok else 0,
                   None if verified is None else (1 if verified else 0), mode, detail[:500], now()))


def writes_for(call_id: str) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM writes WHERE call_id=? ORDER BY id", (call_id,))]


def first_attempt_today(record_uuid: str, day: str) -> bool:
    """True the first time a record is dialed on a business day (that dial raises its attempt counter)."""
    with tx(immediate=True) as c:
        return bool(c.execute("INSERT OR IGNORE INTO attempt_days(record_uuid, day) VALUES (?,?)",
                              (record_uuid, day)).rowcount)


def forget_attempt_day(record_uuid: str, day: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM attempt_days WHERE record_uuid=? AND day=?", (record_uuid, day))


# ---------------------------------------------------------------------- power-dialer sessions
def new_session(identity: str, preset: str, preset_uuid: str, allow: list, total: int) -> int:
    with tx() as c:
        cur = c.execute("INSERT INTO sessions(identity, preset, preset_uuid, allow, total, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?)", (identity, preset, preset_uuid, json.dumps(allow), total, now(), now()))
        return int(cur.lastrowid)


def get_session(session_id: int) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())


def update_session(session_id: int, **fields) -> None:
    allowed = {"state", "loaded", "total"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    sets["updated_at"] = now()
    with tx() as c:
        c.execute("UPDATE sessions SET %s WHERE id=?" % ", ".join("%s=?" % k for k in sets), (*sets.values(), session_id))


def open_session_for(identity: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM sessions WHERE identity=? AND state IN ('open','paused')"
                               " ORDER BY id DESC LIMIT 1", (identity,)).fetchone())


def add_items(session_id: int, rows: list[dict]) -> int:
    """Queue search rows in order. A record already in this session is not queued twice."""
    added = 0
    with tx() as c:
        seq = int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM queue_items WHERE session_id=?", (session_id,)).fetchone()[0])
        for r in rows:
            uid = r.get("uuid")
            if not uid:
                continue
            seq += 1
            n = c.execute("INSERT OR IGNORE INTO queue_items(session_id, seq, record_uuid, row) VALUES (?,?,?,?)",
                          (session_id, seq, uid, json.dumps(r)[:20000])).rowcount
            added += n
    return added


def claim_next_item(session_id: int, identity: str, busy_records: set | None = None) -> Optional[dict]:
    """The next pending record, claimed for this caller. A record another caller holds right now, in this
    session or any other, is passed over (it stays pending)."""
    with tx(immediate=True) as c:
        held = {r[0] for r in c.execute("SELECT record_uuid FROM queue_items WHERE state='claimed'")}
        held |= set(busy_records or ())
        for r in c.execute("SELECT * FROM queue_items WHERE session_id=? AND state='pending' ORDER BY seq LIMIT 200",
                           (session_id,)).fetchall():
            if r["record_uuid"] in held:
                continue
            c.execute("UPDATE queue_items SET state='claimed', claimed_by=?, claimed_at=? WHERE id=?", (identity, now(), r["id"]))
            out = dict(r)
            out.update(state="claimed", claimed_by=identity)
            return out
    return None


def get_item(item_id: int) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM queue_items WHERE id=?", (item_id,)).fetchone())


def update_item(item_id: int, **fields) -> None:
    allowed = {"state", "reason", "numbers", "next_number", "owner_uuid", "done_at", "claimed_by", "claimed_at"}
    sets = {k: (json.dumps(v) if k == "numbers" and not isinstance(v, str) else v) for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with tx() as c:
        c.execute("UPDATE queue_items SET %s WHERE id=?" % ", ".join("%s=?" % k for k in sets), (*sets.values(), item_id))


def release_stale_items(minutes: int = 30) -> int:
    """A record claimed and then left (the tab closed mid-session) goes back to pending."""
    cut = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with tx() as c:
        return c.execute("UPDATE queue_items SET state='pending', claimed_by=NULL, claimed_at=NULL"
                         " WHERE state='claimed' AND claimed_at<?", (cut,)).rowcount


def session_counts(session_id: int) -> dict:
    out = {"pending": 0, "claimed": 0, "done": 0, "skipped": 0}
    for r in conn().execute("SELECT state, COUNT(*) n FROM queue_items WHERE session_id=? GROUP BY state", (session_id,)):
        out[r["state"]] = int(r["n"])
    return out


def skip_reasons(session_id: int) -> dict:
    out: dict = {}
    for r in conn().execute("SELECT reason, COUNT(*) n FROM queue_items WHERE session_id=? AND state='skipped'"
                            " GROUP BY reason ORDER BY n DESC", (session_id,)):
        out[r["reason"] or "skipped"] = int(r["n"])
    return out


# ---------------------------------------------------------------------- hand raises (from the texting app)
def add_handraise(phone: str, record_uuid: str = "", owner_uuid: str = "", body: str = "", program: str = "",
                  source: str = "texting", dedupe: str = "") -> Optional[int]:
    with tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO handraises(dedupe, phone, record_uuid, owner_uuid, body, program, source,"
                        " received_at) VALUES (?,?,?,?,?,?,?,?)",
                        (dedupe or None, phone, record_uuid, owner_uuid, (body or "")[:600], program, source, now()))
        return int(cur.lastrowid) if cur.rowcount else None


def open_handraises(limit: int = 30) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM handraises WHERE state='open' ORDER BY id DESC LIMIT ?", (limit,))]


def close_handraises(phone: str, state: str = "called", by: str = "") -> int:
    with tx() as c:
        return c.execute("UPDATE handraises SET state=?, claimed_by=?, handled_at=? WHERE phone=? AND state='open'",
                         (state, by, now(), phone)).rowcount


# ---------------------------------------------------------------------- kv
def kv_get(k: str, default: Any = None) -> Any:
    r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["v"])
    except (TypeError, ValueError):
        return default


def kv_set(k: str, v: Any) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(k, v, at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, at=excluded.at",
                  (k, json.dumps(v), now()))
