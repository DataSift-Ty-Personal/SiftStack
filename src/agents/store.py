"""The brain's memory: webhook events, the task queue and the spend ledger, in one SQLite file.

One file on the app's own volume, one writer (the worker thread), so no database service and
nothing shared with another customer. Every webhook is logged before it is routed, because
DataSift's payload shape is undocumented and the log is how a surprise gets diagnosed.

Team columns (build 1.0.63): a task now carries the thread it belongs to (the memory spine every
handoff on one job shares), its hop count, the Slack thread it answers in, the Claude session it
can resume, and the question it is waiting on. They are added with ALTER TABLE, so a brain that
was deployed before the team keeps its queue and its ledger.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, received TEXT, source TEXT, route TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, agent TEXT, workflow TEXT,
    record_uuid TEXT, subject TEXT, status TEXT, attempts INTEGER DEFAULT 0,
    started TEXT, finished TEXT, result TEXT, error TEXT, event_id INTEGER);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_record ON tasks(workflow, record_uuid);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, day TEXT, task_id INTEGER, usd REAL, what TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, at TEXT);
-- The autopilot's memory (src/agents/autopilot): what it did to which record, what it paid,
-- every phone it scored (so a number is never bought twice), facts it learned about a record,
-- and the outreach batches it staged for a human to release.
CREATE TABLE IF NOT EXISTS ap_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, day TEXT, run_id TEXT, stage TEXT,
    record_uuid TEXT, action TEXT, detail TEXT, usd REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ap_events_record ON ap_events(record_uuid, action);
CREATE INDEX IF NOT EXISTS ap_events_day ON ap_events(day, stage);
CREATE TABLE IF NOT EXISTS ap_phones (
    number TEXT PRIMARY KEY, at TEXT, score INTEGER, tier TEXT, line_type TEXT, litigator INTEGER);
CREATE TABLE IF NOT EXISTS ap_facts (
    record_uuid TEXT, key TEXT, value TEXT, at TEXT, PRIMARY KEY (record_uuid, key));
CREATE TABLE IF NOT EXISTS ap_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, stage TEXT, kind TEXT, status TEXT,
    items TEXT, usd REAL DEFAULT 0, released TEXT, result TEXT);
-- Phase 0 of the build plan (2026-11-13). ap_log: one shared copy of the account's activity log
-- (calls, texts, status and tag changes), read once and used by every stage. ap_changes: what a
-- live stage changed and the value before, so a run can be put back. ap_owner_lane: the one lane
-- each owner sits in, worked out across all of that owner's records. ap_rate: the request budget
-- every process of this app shares.
CREATE TABLE IF NOT EXISTS ap_log (
    id TEXT PRIMARY KEY, at TEXT, event_type TEXT, resource TEXT, resource_type TEXT, source TEXT,
    author TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ap_log_res ON ap_log(resource, event_type, at);
CREATE INDEX IF NOT EXISTS ap_log_type ON ap_log(event_type, at);
CREATE TABLE IF NOT EXISTS ap_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, run_id TEXT, stage TEXT, record_uuid TEXT,
    what TEXT, before TEXT, after TEXT, undone TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ap_changes_run ON ap_changes(run_id);
CREATE INDEX IF NOT EXISTS ap_changes_record ON ap_changes(record_uuid);
CREATE TABLE IF NOT EXISTS ap_owner_lane (
    owner_uuid TEXT PRIMARY KEY, lane TEXT, records TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS ap_rate (
    app TEXT, win TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (app, win));
-- One row per caller per day, written from the call log each evening (build plan 6.2).
CREATE TABLE IF NOT EXISTS ap_capture (
    day TEXT, caller TEXT, dials INTEGER, answered INTEGER, conversations INTEGER, talk_s INTEGER,
    owners INTEGER, numbers INTEGER, PRIMARY KEY (day, caller));
"""

# (column, declaration). Order matters only for readability; each is added if missing.
TEAM_COLUMNS = (
    ("thread_id", "TEXT DEFAULT ''"),
    ("parent_id", "INTEGER DEFAULT 0"),
    ("hop", "INTEGER DEFAULT 0"),
    ("source", "TEXT DEFAULT ''"),
    ("mode", "TEXT DEFAULT 'direct'"),       # direct: run the workflow; agent: an employee turn
    ("payload", "TEXT DEFAULT '{}'"),
    ("slack_channel", "TEXT DEFAULT ''"),
    ("slack_thread", "TEXT DEFAULT ''"),
    ("session_id", "TEXT DEFAULT ''"),
    ("question", "TEXT DEFAULT ''"),
    ("not_before", "TEXT DEFAULT ''"),       # a queued task is not claimed before this (files settling)
)

# queued -> running -> done | failed | waiting (asked the operator) -> answered (a reply resumed it)
OPEN = ("queued", "running")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_thread() -> str:
    return uuid.uuid4().hex


def _conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


@contextmanager
def _db():
    c = _conn()
    try:
        yield c
    finally:
        c.close()


def init() -> None:
    with _LOCK, _db() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        for name, decl in TEAM_COLUMNS:
            if name not in have:
                c.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (name, decl))
        c.execute("CREATE INDEX IF NOT EXISTS tasks_thread ON tasks(thread_id)")
        c.execute("CREATE INDEX IF NOT EXISTS tasks_slack ON tasks(slack_channel, slack_thread)")


def log_event(source: str, route: str, payload) -> int:
    with _LOCK, _db() as c:
        cur = c.execute("INSERT INTO events (received, source, route, payload) VALUES (?,?,?,?)",
                        (now(), source, route, json.dumps(payload, default=str)[:200000]))
        return cur.lastrowid


def kv_get(key: str, default: str = "") -> str:
    with _LOCK, _db() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with _LOCK, _db() as c:
        c.execute("INSERT INTO kv (key, value, at) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at", (key, str(value), now()))


def open_task(workflow: str) -> int:
    """The id of a queued or running task for this workflow, else 0. The clock asks before it fires,
    so a slow run is never stacked under a second copy of itself."""
    with _LOCK, _db() as c:
        row = c.execute("SELECT id FROM tasks WHERE workflow=? AND status IN ('queued','running') "
                        "ORDER BY id DESC LIMIT 1", (workflow,)).fetchone()
        return row["id"] if row else 0


def seen_once(key: str) -> bool:
    """True the first time a key is seen, False ever after. Slack retries and sends a mention twice
    (app_mention plus message), and either would otherwise queue the same ask twice."""
    with _LOCK, _db() as c:
        try:
            c.execute("INSERT INTO seen (key, at) VALUES (?,?)", (key, now()))
            return True
        except sqlite3.IntegrityError:
            return False


def enqueue(agent: str, workflow: str, record_uuid: str = "", subject: str = "", event_id: int = 0,
            force: bool = False, *, payload: dict | None = None, thread_id: str = "", parent_id: int = 0,
            hop: int = 0, source: str = "", mode: str = "direct", slack_channel: str = "",
            slack_thread: str = "", session_id: str = "", not_before: str = "") -> tuple[int, bool]:
    """(task id, created). A record already queued, running, or finished inside the dedupe window
    is not queued again: one status flip that DataSift delivers twice must not pay twice. Tasks
    with no record (a Slack ask) are never deduped this way."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.DEDUPE_HOURS)).isoformat(timespec="seconds")
    with _LOCK, _db() as c:
        if record_uuid and not force:
            row = c.execute(
                "SELECT id FROM tasks WHERE workflow=? AND record_uuid=? AND "
                "(status IN ('queued','running') OR (status='done' AND finished>=?)) ORDER BY id DESC LIMIT 1",
                (workflow, record_uuid, cutoff)).fetchone()
            if row:
                return row["id"], False
        cur = c.execute(
            "INSERT INTO tasks (created, agent, workflow, record_uuid, subject, status, event_id, thread_id, "
            "parent_id, hop, source, mode, payload, slack_channel, slack_thread, session_id, not_before) "
            "VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?)",
            (now(), agent, workflow, record_uuid, subject, event_id, thread_id or new_thread(), parent_id,
             hop, source, mode, json.dumps(payload or {}, default=str), slack_channel, slack_thread, session_id,
             not_before))
        return cur.lastrowid, True


def later(seconds: float) -> str:
    """A not_before stamp `seconds` from now, in the same form as now() so the two compare as text."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def queued_ask(channel: str, thread_ts: str, agent: str) -> dict | None:
    """The employee turn on this Slack thread that has not started yet, if there is one. A second
    message that arrives while it waits joins it instead of starting a turn of its own."""
    if not (channel and thread_ts):
        return None
    with _LOCK, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE slack_channel=? AND slack_thread=? AND agent=? AND "
                        "status='queued' AND mode='agent' ORDER BY id DESC LIMIT 1",
                        (channel, thread_ts, agent)).fetchone()
        return dict(row) if row else None


def join_ask(task_id: int, text: str, not_before: str) -> bool:
    """Add a follow-up message to a task that has not started, and push its start back. False when
    the worker claimed it in the meantime (the caller then queues a turn of its own)."""
    with _LOCK, _db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT payload, not_before FROM tasks WHERE id=? AND status='queued'", (task_id,)).fetchone()
        if not row:
            c.execute("COMMIT")
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        extra = (text or "").strip()
        if extra:
            payload["text"] = ((payload.get("text") or "").rstrip() + "\n" + extra).strip()
        start = max(row["not_before"] or "", not_before or "")
        c.execute("UPDATE tasks SET payload=?, not_before=? WHERE id=?",
                  (json.dumps(payload, default=str), start, task_id))
        c.execute("COMMIT")
        return True


def claim_next() -> dict | None:
    with _LOCK, _db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM tasks WHERE status='queued' AND (not_before IS NULL OR not_before='' "
                        "OR not_before<=?) ORDER BY id LIMIT 1", (now(),)).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE tasks SET status='running', started=?, attempts=attempts+1 WHERE id=?",
                  (now(), row["id"]))
        c.execute("COMMIT")
        return dict(row) | {"status": "running", "attempts": row["attempts"] + 1}


def finish(task_id: int, status: str, result: dict | None = None, error: str = "") -> None:
    with _LOCK, _db() as c:
        c.execute("UPDATE tasks SET status=?, finished=?, result=?, error=? WHERE id=?",
                  (status, now(), json.dumps(result or {}, default=str), error[:4000], task_id))


def wait_for_operator(task_id: int, question: str, session_id: str, result: dict | None = None) -> None:
    """The employee asked the operator something and ended its turn. A reply in the same Slack
    thread resumes it (bus.resume); nothing else wakes it."""
    with _LOCK, _db() as c:
        c.execute("UPDATE tasks SET status='waiting', finished=?, question=?, session_id=?, result=? WHERE id=?",
                  (now(), question[:4000], session_id, json.dumps(result or {}, default=str), task_id))


def mark(task_id: int, status: str) -> None:
    with _LOCK, _db() as c:
        c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def set_slack(task_id: int, channel: str, thread_ts: str) -> None:
    """A task that started without a Slack thread (a webhook) adopts the thread of its first post,
    so the handoffs and follow-ups it causes land under that same message."""
    with _LOCK, _db() as c:
        c.execute("UPDATE tasks SET slack_channel=?, slack_thread=? WHERE id=?", (channel, thread_ts, task_id))


def task(task_id: int) -> dict | None:
    with _LOCK, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def payload_of(t: dict) -> dict:
    try:
        return json.loads(t.get("payload") or "{}")
    except ValueError:
        return {}


def result_of(t: dict) -> dict:
    try:
        return json.loads(t.get("result") or "{}")
    except ValueError:
        return {}


def thread(thread_id: str) -> list[dict]:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE thread_id=? ORDER BY id", (thread_id,)).fetchall()
    return [dict(r) for r in rows]


def on_slack_thread(channel: str, thread_ts: str) -> list[dict]:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE slack_channel=? AND slack_thread=? ORDER BY id",
                         (channel, thread_ts)).fetchall()
    return [dict(r) for r in rows]


def waiting_on(channel: str, thread_ts: str) -> dict | None:
    with _LOCK, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE slack_channel=? AND slack_thread=? AND status='waiting' "
                        "ORDER BY id DESC LIMIT 1", (channel, thread_ts)).fetchone()
    return dict(row) if row else None


def requeue_stale() -> int:
    """A deploy or crash mid-task leaves it 'running' forever. Retry once, then give up loudly."""
    with _LOCK, _db() as c:
        n = c.execute("UPDATE tasks SET status='queued' WHERE status='running' AND attempts<2").rowcount
        c.execute("UPDATE tasks SET status='failed', error='interrupted twice (restart mid-run)' "
                  "WHERE status='running'")
        return n


def add_spend(task_id: int, usd: float, what: str) -> None:
    if usd <= 0:
        return
    with _LOCK, _db() as c:
        c.execute("INSERT INTO spend (at, day, task_id, usd, what) VALUES (?,?,?,?,?)",
                  (now(), now()[:10], task_id, round(usd, 4), what))


RESEARCH_WORKFLOWS = ("deep_prospect",)


def spent(task_id: int | None = None, day: str | None = None, research: bool | None = None) -> float:
    """Dollars spent. `research` splits the day's total: True is only what deep prospects spent,
    False is everything else. Research has its own daily budget (build plan 0.9), so a morning of
    research cannot starve the skip traces, and a day of traces cannot stop the research."""
    q, args = "SELECT COALESCE(SUM(usd),0) FROM spend WHERE 1=1", []
    if task_id is not None:
        q, args = q + " AND task_id=?", args + [task_id]
    if day is not None:
        q, args = q + " AND day=?", args + [day]
    if research is not None:
        marks = ",".join("?" * len(RESEARCH_WORKFLOWS))
        q += " AND task_id %s (SELECT id FROM tasks WHERE workflow IN (%s))" % ("IN" if research else "NOT IN", marks)
        args = args + list(RESEARCH_WORKFLOWS)
    with _LOCK, _db() as c:
        return float(c.execute(q, args).fetchone()[0])


def today() -> str:
    return datetime.now(timezone.utc).isoformat()[:10]


def counts() -> dict:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def recent(n: int = 10) -> list[dict]:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT id, created, agent, workflow, subject, status, error FROM tasks "
                         "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
