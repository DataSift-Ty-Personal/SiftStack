"""Durable event log + conversation state for the SMS agent.

smrtPhone purges its own webhook logs after 30 days and has no replay, so this
is the system of record. The receiver writes here before it does anything else:
if every downstream step fails, the event is still recoverable.

Everything is stdlib sqlite3 in WAL mode. One writer (the worker) and many
readers is exactly the shape this handles well.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- smrtphone | datasift
    event_type    TEXT,
    dedupe_key    TEXT UNIQUE,            -- smsId / callId / composite
    payload       TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    processed_at  TEXT,
    outcome       TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events(processed_at) WHERE processed_at IS NULL;

-- phone -> reisift record. Populated when we send (we know who we texted) and
-- backfillable from a queue CSV. This is how an inbound reply finds its record:
-- the smsIncoming payload carries no record id.
CREATE TABLE IF NOT EXISTS phone_map (
    phone         TEXT PRIMARY KEY,       -- 10 digits
    record_uuid   TEXT,
    owner_uuid    TEXT,
    first_name    TEXT,
    address       TEXT,
    context       TEXT,                   -- JSON blob of deal facts for the responder
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    phone         TEXT PRIMARY KEY,
    from_number   TEXT,                   -- sticky: every reply leaves from here
    record_uuid   TEXT,
    state         TEXT NOT NULL DEFAULT 'active',  -- active | paused | closed | opted_out
    ai_turns      INTEGER NOT NULL DEFAULT 0,
    last_inbound  TEXT,
    last_outbound TEXT,
    paused_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL,
    direction     TEXT NOT NULL,          -- in | out
    body          TEXT NOT NULL,
    from_number   TEXT,
    sms_id        TEXT,
    author        TEXT,                   -- ai | human | seed
    intent        TEXT,
    confidence    REAL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id);

-- Nothing sends directly. Everything queues here so quiet hours, per-number
-- caps and pacing are enforced in one place, and a crash cannot double-send.
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL,
    from_number   TEXT,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued | sent | failed | cancelled | held
    not_before    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    intent        TEXT,
    confidence    REAL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_queued ON outbox(status, not_before);

CREATE TABLE IF NOT EXISTS sends (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_number   TEXT NOT NULL,
    phone         TEXT NOT NULL,
    sent_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_number_day ON sends(from_number, sent_at);

-- Escalations wait here before posting. A seller sends a thought across two or
-- three texts; posting each one separately turns the channel into noise and
-- buries the lead. One post per burst, one post per conversation.
CREATE TABLE IF NOT EXISTS escalations (
    phone         TEXT PRIMARY KEY,
    record_uuid   TEXT,
    due_at        TEXT NOT NULL,      -- flush after the burst settles
    first_seen    TEXT NOT NULL,
    posted_at     TEXT,               -- set once it has gone to Slack
    intent        TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_due ON escalations(posted_at, due_at);

CREATE TABLE IF NOT EXISTS suppression (
    phone         TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,          -- opt_out | wrong_number | dead | manual
    created_at    TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_phone(n: Any) -> str:
    d = "".join(ch for ch in str(n or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.DB_PATH), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _local.conn = c
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except Exception:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


def init() -> Path:
    _conn()
    return config.DB_PATH


# ------------------------------------------------------------------ events

def record_event(source: str, event_type: str, dedupe_key: str, payload: dict) -> Optional[int]:
    """Persist a raw webhook. Returns the row id, or None if already seen.

    Retries carry the same smsId, and smrtPhone gives no delivery guarantee, so
    dedupe is on the key rather than on arrival order.
    """
    try:
        with tx() as c:
            cur = c.execute(
                "INSERT INTO events(source, event_type, dedupe_key, payload, received_at)"
                " VALUES (?,?,?,?,?)",
                (source, event_type, dedupe_key, json.dumps(payload), now()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def event_exists(dedupe_key: str) -> bool:
    """Has this event already been recorded? Read-only, consumes nothing."""
    row = _conn().execute(
        "SELECT 1 FROM events WHERE dedupe_key=? LIMIT 1", (dedupe_key,)
    ).fetchone()
    return row is not None


def finish_event(event_id: int, outcome: str, error: str = "") -> None:
    with tx() as c:
        c.execute(
            "UPDATE events SET processed_at=?, outcome=?, error=? WHERE id=?",
            (now(), outcome, error or None, event_id),
        )


def pending_events(limit: int = 50) -> list[sqlite3.Row]:
    return list(
        _conn().execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id LIMIT ?", (limit,)
        )
    )


# ------------------------------------------------------------------ mapping

def map_phone(
    phone: str,
    record_uuid: str = "",
    owner_uuid: str = "",
    first_name: str = "",
    address: str = "",
    context: Optional[dict] = None,
) -> None:
    p = clean_phone(phone)
    if not p:
        return
    with tx() as c:
        c.execute(
            """INSERT INTO phone_map(phone, record_uuid, owner_uuid, first_name, address, context, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                 record_uuid=COALESCE(NULLIF(excluded.record_uuid,''), phone_map.record_uuid),
                 owner_uuid =COALESCE(NULLIF(excluded.owner_uuid,''),  phone_map.owner_uuid),
                 first_name =COALESCE(NULLIF(excluded.first_name,''),  phone_map.first_name),
                 address    =COALESCE(NULLIF(excluded.address,''),     phone_map.address),
                 context    =COALESCE(excluded.context, phone_map.context),
                 updated_at =excluded.updated_at""",
            (
                p,
                record_uuid,
                owner_uuid,
                first_name,
                address,
                json.dumps(context) if context else None,
                now(),
            ),
        )


def lookup_phone(phone: str) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM phone_map WHERE phone=?", (clean_phone(phone),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["context"] = json.loads(d["context"]) if d.get("context") else {}
    return d


# ------------------------------------------------------------- conversations

def get_conversation(phone: str) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM conversations WHERE phone=?", (clean_phone(phone),)).fetchone()
    return dict(row) if row else None


def ensure_conversation(phone: str, from_number: str = "", record_uuid: str = "") -> dict:
    p = clean_phone(phone)
    with tx() as c:
        c.execute(
            """INSERT INTO conversations(phone, from_number, record_uuid, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                 from_number=COALESCE(NULLIF(conversations.from_number,''), excluded.from_number),
                 record_uuid=COALESCE(NULLIF(conversations.record_uuid,''), excluded.record_uuid),
                 updated_at=excluded.updated_at""",
            (p, from_number, record_uuid, now(), now()),
        )
    return get_conversation(p)  # type: ignore[return-value]


def update_conversation(phone: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(
            f"UPDATE conversations SET {cols} WHERE phone=?",
            (*fields.values(), clean_phone(phone)),
        )


def pause_conversation(phone: str, reason: str) -> None:
    update_conversation(phone, state="paused", paused_reason=reason)


def bump_ai_turns(phone: str) -> int:
    with tx() as c:
        c.execute(
            "UPDATE conversations SET ai_turns=ai_turns+1, updated_at=? WHERE phone=?",
            (now(), clean_phone(phone)),
        )
    conv = get_conversation(phone)
    return int(conv["ai_turns"]) if conv else 0


# ---------------------------------------------------------------- messages

def set_meta(key: str, value: str) -> None:
    with tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (key, value))


def get_meta(key: str) -> str:
    with tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else ""


def mark_notified(phone: str, kind: str) -> bool:
    """Claim the right to post one notification of `kind` for this thread.

    True the first time, False afterwards. Keeps a person who asks the same
    question twice from producing two identical Slack posts, which is how a
    channel stops being read.
    """
    phone = clean_phone(phone)
    with tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS notifications ("
            " phone TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,"
            " PRIMARY KEY (phone, kind))"
        )
        row = c.execute(
            "SELECT 1 FROM notifications WHERE phone=? AND kind=?", (phone, kind)
        ).fetchone()
        if row:
            return False
        c.execute(
            "INSERT INTO notifications(phone, kind, created_at) VALUES (?,?,?)",
            (phone, kind, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return True


def recent_inbound_exists(phone: str, body: str, minutes: int) -> bool:
    """Has this exact inbound already been stored recently?

    Identity by content, because smrtPhone gives the same text two different
    ids depending on which surface you read it from.
    """
    phone = clean_phone(phone)
    body = (body or "").strip()
    if not phone or not body:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    row = _conn().execute(
        "SELECT 1 FROM messages WHERE phone=? AND direction='in' AND TRIM(body)=?"
        " AND created_at >= ? LIMIT 1",
        (phone, body, cutoff),
    ).fetchone()
    if row:
        return True

    # Also look at events not yet processed. An inbound becomes a message only
    # when the worker gets to it, so between the webhook landing and that moment
    # the message table is empty and the backstop poller cannot tell the reply
    # has already been received. That window is how "Nope" got classified twice
    # with two different confidences on 2026-08-12.
    row = _conn().execute(
        "SELECT 1 FROM events WHERE processed_at IS NULL AND event_type='smsIncoming'"
        " AND payload LIKE ? AND payload LIKE ? AND received_at >= ? LIMIT 1",
        (f"%{body}%", f"%{phone}%", cutoff),
    ).fetchone()
    return row is not None


def stamp_intent(phone: str, sms_id: str, intent: str, confidence: float) -> None:
    """Record how an inbound was read, on the message itself.

    The body has to be stored before classification so the model can see the
    thread, which left every inbound row with a blank intent. That is the one
    column you want when asking why a lead was or was not flagged, so it gets
    written back rather than living only in the log.
    """
    with tx() as c:
        if sms_id:
            c.execute(
                "UPDATE messages SET intent=?, confidence=? WHERE sms_id=? AND direction='in'",
                (intent, confidence, sms_id),
            )
        else:
            c.execute(
                "UPDATE messages SET intent=?, confidence=? WHERE id=("
                "  SELECT id FROM messages WHERE phone=? AND direction='in'"
                "  ORDER BY id DESC LIMIT 1)",
                (intent, confidence, clean_phone(phone)),
            )


def add_message(
    phone: str,
    direction: str,
    body: str,
    from_number: str = "",
    sms_id: str = "",
    author: str = "",
    intent: str = "",
    confidence: Optional[float] = None,
) -> None:
    # One real text must appear once. The same smsId arrives from more than one
    # place: the webhook, a webhook retry, and the reconcile poller that exists
    # precisely because smrtPhone drops some. Event-level dedupe caught the
    # retries but not the poller, so live threads carried each reply three
    # times, which inflates every count in the digest and makes a transcript
    # read as though a seller repeated themselves.
    if sms_id:
        with tx() as c:
            row = c.execute(
                "SELECT 1 FROM messages WHERE sms_id=? AND direction=? LIMIT 1",
                (sms_id, direction),
            ).fetchone()
        if row:
            return

    with tx() as c:
        c.execute(
            "INSERT INTO messages(phone, direction, body, from_number, sms_id, author, intent,"
            " confidence, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                clean_phone(phone),
                direction,
                body,
                from_number,
                sms_id,
                author,
                intent,
                confidence,
                now(),
            ),
        )
    field = "last_inbound" if direction == "in" else "last_outbound"
    update_conversation(phone, **{field: now()})


def phone_for_sms_id(sms_id: str) -> str:
    """Resolve a delivery callback that carries only an smsId back to a number."""
    if not sms_id:
        return ""
    row = _conn().execute(
        "SELECT phone FROM messages WHERE sms_id=? ORDER BY id DESC LIMIT 1", (sms_id,)
    ).fetchone()
    return row["phone"] if row else ""


def thread(phone: str, limit: int = 40) -> list[dict]:
    rows = _conn().execute(
        "SELECT direction, body, author, created_at FROM messages WHERE phone=?"
        " ORDER BY id DESC LIMIT ?",
        (clean_phone(phone), limit),
    )
    return [dict(r) for r in reversed(list(rows))]


# ------------------------------------------------------------------ outbox

def queue_message(
    phone: str,
    body: str,
    from_number: str = "",
    not_before: str = "",
    status: str = "queued",
    intent: str = "",
    confidence: Optional[float] = None,
) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO outbox(phone, from_number, body, status, not_before, intent, confidence,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                clean_phone(phone),
                from_number,
                body,
                status,
                not_before or None,
                intent,
                confidence,
                now(),
            ),
        )
        return int(cur.lastrowid)


def due_outbox(limit: int = 25) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM outbox WHERE status='queued' AND (not_before IS NULL OR not_before<=?)"
        " ORDER BY id LIMIT ?",
        (now(), limit),
    )
    return [dict(r) for r in rows]


def mark_outbox(row_id: int, status: str, error: str = "") -> None:
    with tx() as c:
        c.execute(
            "UPDATE outbox SET status=?, error=?, attempts=attempts+1,"
            " sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?",
            (status, error or None, status, now(), row_id),
        )


def cancel_queued(phone: str, reason: str = "superseded") -> int:
    """Drop anything still pending for this number, queued OR held.

    Used when a human takes over or the contact opts out, so a reply written
    ten minutes ago never lands after the conversation has moved on. Held
    drafts count: a draft that is approved tomorrow would otherwise send into
    a conversation a human already took over.
    """
    with tx() as c:
        cur = c.execute(
            "UPDATE outbox SET status='cancelled', error=?"
            " WHERE phone=? AND status IN ('queued','held')",
            (reason, clean_phone(phone)),
        )
        return cur.rowcount


def cancel_queued_all(reason: str = "superseded") -> int:
    """Cancel every pending message across all numbers."""
    with tx() as c:
        return c.execute(
            "UPDATE outbox SET status='cancelled', error=? WHERE status IN ('queued','held')",
            (reason,),
        ).rowcount


def record_send(from_number: str, phone: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO sends(from_number, phone, sent_at) VALUES (?,?,?)",
            (from_number, clean_phone(phone), now()),
        )


def sends_today(from_number: str) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM sends WHERE from_number=? AND sent_at>=?",
        (from_number, day),
    ).fetchone()
    return int(row["n"]) if row else 0


def last_send_at(from_number: str) -> Optional[str]:
    row = _conn().execute(
        "SELECT sent_at FROM sends WHERE from_number=? ORDER BY id DESC LIMIT 1", (from_number,)
    ).fetchone()
    return row["sent_at"] if row else None


def number_load() -> dict[str, int]:
    day = datetime.now(timezone.utc).date().isoformat()
    rows = _conn().execute(
        "SELECT from_number, COUNT(*) AS n FROM sends WHERE sent_at>=? GROUP BY from_number",
        (day,),
    )
    return {r["from_number"]: int(r["n"]) for r in rows}


# -------------------------------------------------------------- suppression

# ------------------------------------------------------------- escalations

def queue_escalation(phone: str, record_uuid: str, intent: str, debounce_seconds: int) -> str:
    """Hold an escalation open for a moment so a burst posts once.

    Returns 'new', 'extended' (the burst is still arriving) or 'already_posted'.
    """
    from datetime import timedelta

    p = clean_phone(phone)
    due = (datetime.now(timezone.utc) + timedelta(seconds=debounce_seconds)).isoformat(
        timespec="seconds"
    )
    row = _conn().execute("SELECT posted_at FROM escalations WHERE phone=?", (p,)).fetchone()
    if row and row["posted_at"]:
        return "already_posted"
    with tx() as c:
        c.execute(
            """INSERT INTO escalations(phone, record_uuid, due_at, first_seen, intent)
               VALUES (?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                 due_at=excluded.due_at,
                 record_uuid=COALESCE(NULLIF(excluded.record_uuid,''), escalations.record_uuid),
                 intent=excluded.intent""",
            (p, record_uuid, due, now(), intent),
        )
    return "extended" if row else "new"


def due_escalations() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM escalations WHERE posted_at IS NULL AND due_at<=? ORDER BY due_at",
        (now(),),
    )
    return [dict(r) for r in rows]


def mark_escalation_posted(phone: str) -> None:
    with tx() as c:
        c.execute("UPDATE escalations SET posted_at=? WHERE phone=?", (now(), clean_phone(phone)))


def escalation_expired(phone: str, quiet_days: int) -> bool:
    """A months-later revival deserves a fresh post rather than silence."""
    from datetime import timedelta

    row = _conn().execute(
        "SELECT posted_at FROM escalations WHERE phone=?", (clean_phone(phone),)
    ).fetchone()
    if not row or not row["posted_at"]:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=quiet_days)).isoformat(timespec="seconds")
    return str(row["posted_at"]) < cutoff


def clear_escalation(phone: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM escalations WHERE phone=?", (clean_phone(phone),))


def suppress(phone: str, reason: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO suppression(phone, reason, created_at) VALUES (?,?,?)"
            " ON CONFLICT(phone) DO UPDATE SET reason=excluded.reason",
            (clean_phone(phone), reason, now()),
        )


def is_suppressed(phone: str) -> Optional[str]:
    row = _conn().execute(
        "SELECT reason FROM suppression WHERE phone=?", (clean_phone(phone),)
    ).fetchone()
    return row["reason"] if row else None


# ------------------------------------------------------------------ report

def stats() -> dict:
    c = _conn()

    def one(sql: str, *args) -> int:
        row = c.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    return {
        "events": one("SELECT COUNT(*) FROM events"),
        "events_unprocessed": one("SELECT COUNT(*) FROM events WHERE processed_at IS NULL"),
        "conversations": one("SELECT COUNT(*) FROM conversations"),
        "paused": one("SELECT COUNT(*) FROM conversations WHERE state='paused'"),
        "opted_out": one("SELECT COUNT(*) FROM conversations WHERE state='opted_out'"),
        "inbound": one("SELECT COUNT(*) FROM messages WHERE direction='in'"),
        "outbound": one("SELECT COUNT(*) FROM messages WHERE direction='out'"),
        "queued": one("SELECT COUNT(*) FROM outbox WHERE status='queued'"),
        "held": one("SELECT COUNT(*) FROM outbox WHERE status='held'"),
        "sent": one("SELECT COUNT(*) FROM outbox WHERE status='sent'"),
        "suppressed": one("SELECT COUNT(*) FROM suppression"),
        "mapped_phones": one("SELECT COUNT(*) FROM phone_map"),
    }
