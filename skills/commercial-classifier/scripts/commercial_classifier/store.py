"""The work directory: one sqlite file holding every phase's output keyed by
`rid`, plus phase state so `run` resumes where it stopped.

Re-running a phase invalidates every phase after it, so a changed crosswalk
can never leave a stale value band or owner roll-up behind.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

DB = "cc.sqlite"
PHASES = ["ingest", "geometry", "classify", "places", "poi", "value", "owners", "review", "export"]


def default_workdir(input_path):
    stem = os.path.splitext(os.path.basename(input_path))[0]
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:60] or "input"
    return os.path.join(os.path.dirname(os.path.abspath(input_path)), "cc_" + stem)


def connect(work):
    os.makedirs(work, exist_ok=True)
    con = sqlite3.connect(os.path.join(work, DB))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, json.dumps(v)))
    con.commit()


def get_meta(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def done(con, phase):
    return get_meta(con, "phase:" + phase) is not None


def mark_done(con, phase, stats=None):
    invalidate_after(con, phase)
    set_meta(con, "phase:" + phase, {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "stats": stats or {}})


def invalidate_after(con, phase):
    if phase not in PHASES:
        return
    for later in PHASES[PHASES.index(phase) + 1:]:
        con.execute("DELETE FROM meta WHERE k = ?", ("phase:" + later,))
    con.commit()


def stats_of(con, phase):
    m = get_meta(con, "phase:" + phase)
    return (m or {}).get("stats", {})


def create_table(con, name, cols, pk="rid"):
    con.execute("DROP TABLE IF EXISTS %s" % name)
    defs = ", ".join("%s %s%s" % (c, t, " PRIMARY KEY" if c == pk else "") for c, t in cols)
    con.execute("CREATE TABLE %s (%s)" % (name, defs))


def insert_many(con, name, cols, rows, chunk=5000):
    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (name, ", ".join(cols), ", ".join("?" * len(cols)))
    buf, n = [], 0
    for r in rows:
        buf.append(r)
        if len(buf) >= chunk:
            con.executemany(sql, buf)
            n += len(buf)
            buf = []
    if buf:
        con.executemany(sql, buf)
        n += len(buf)
    con.commit()
    return n


def table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def require(con, *phases):
    missing = [p for p in phases if not done(con, p)]
    if missing:
        raise RuntimeError("run %s first (cc %s --work <dir>)" % (missing[0], missing[0]))
