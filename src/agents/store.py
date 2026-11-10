"""The brain's memory: webhook events, the task queue and the spend ledger, in one SQLite file.

One file on the app's own volume, one writer (the worker thread), so no database service and
nothing shared with another customer. Every webhook is logged before it is routed, because
DataSift's payload shape is undocumented and the log is how a surprise gets diagnosed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def log_event(source: str, route: str, payload) -> int:
    with _LOCK, _db() as c:
        cur = c.execute("INSERT INTO events (received, source, route, payload) VALUES (?,?,?,?)",
                        (now(), source, route, json.dumps(payload, default=str)[:200000]))
        return cur.lastrowid


def enqueue(agent: str, workflow: str, record_uuid: str, subject: str = "", event_id: int = 0,
            force: bool = False) -> tuple[int, bool]:
    """(task id, created). A record already queued, running, or finished inside the dedupe window
    is not queued again: one status flip that DataSift delivers twice must not pay twice."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.DEDUPE_HOURS)).isoformat(timespec="seconds")
    with _LOCK, _db() as c:
        if not force:
            row = c.execute(
                "SELECT id FROM tasks WHERE workflow=? AND record_uuid=? AND "
                "(status IN ('queued','running') OR (status='done' AND finished>=?)) ORDER BY id DESC LIMIT 1",
                (workflow, record_uuid, cutoff)).fetchone()
            if row:
                return row["id"], False
        cur = c.execute(
            "INSERT INTO tasks (created, agent, workflow, record_uuid, subject, status, event_id) "
            "VALUES (?,?,?,?,?,'queued',?)", (now(), agent, workflow, record_uuid, subject, event_id))
        return cur.lastrowid, True


def claim_next() -> dict | None:
    with _LOCK, _db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
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


def task(task_id: int) -> dict | None:
    with _LOCK, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
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


def spent(task_id: int | None = None, day: str | None = None) -> float:
    q, args = "SELECT COALESCE(SUM(usd),0) FROM spend WHERE 1=1", []
    if task_id is not None:
        q, args = q + " AND task_id=?", args + [task_id]
    if day is not None:
        q, args = q + " AND day=?", args + [day]
    with _LOCK, _db() as c:
        return float(c.execute(q, args).fetchone()[0])


def counts() -> dict:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def recent(n: int = 10) -> list[dict]:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT id, created, workflow, subject, status, error FROM tasks "
                         "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
