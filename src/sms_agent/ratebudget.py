"""This app's share of the DataSift request budget (build plan 0.14).

DataSift allows 120 requests a minute and 5,000 an hour for the WHOLE account, shared by every
key-based tool on it: the brain, the first-to-market box, this app, scripts, Zaps. About 80 a
minute can be sustained. The plan gives the brain 45, the first-to-market box 20 and this app 10,
and keeps the rest in reserve.

Before this, nothing in this app counted. The standalone client paces itself half a second apart,
which is 120 a minute from ONE process: the account's entire allowance, with a campaign build on
its own thread beside the worker and a person running the CLI beside both. A morning build that
trips the throttle does not fail loudly, it makes every other tool on the account wait.

The design is the brain's (src/agents/autopilot/budget.py), which this image cannot import:

  * One counter for the whole app, kept in the app's own SQLite file, so the receiver, the inline
    worker, the campaign thread and a CLI opened over ssh all draw on the same share.
  * Two fixed windows are counted, the minute and the hour.
  * Reply dispositions and speed to lead go first. Priority work may use the whole minute;
    everything else stops at 80% of it, so a scan can never starve the write a homeowner's
    opt-out is waiting on.
  * While the first-to-market box makes its morning run, scans drop to a trickle. Priority work
    is not slowed by that window.

One thing differs from the brain on purpose. The brain's stages run again at their next slot, so
when its hour is used it raises. A reply disposition has no next slot: the event is processed
once. So priority work here is DELAYED, never dropped. It waits for the next minute, and if the
wait runs past RATE_MAX_WAIT it goes anyway and the overrun is counted where the digest shows it.
An opt-out that reaches DataSift a minute late is fine. One that never reaches it is not.

Off by default (SMS_AGENT_RATE_BUDGET). With it off the client is not wrapped at all. `count`
wraps it and counts every request without ever delaying one, and notes each request a limit
would have delayed: a week of that is how the share should be set. `on` makes the count the limit.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

APP = "texting"
RESERVE = 0.2            # the part of each minute only priority work may use

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_budget (
    app TEXT NOT NULL, win TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (app, win));
CREATE TABLE IF NOT EXISTS rate_notes (
    day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, kind));
"""


class BudgetExhausted(RuntimeError):
    """This app's share is used and the caller is not priority work. Stop and say so."""


class BudgetBusy(BudgetExhausted):
    """The share is used right now and the caller asked not to wait (the worker's send loop)."""


# ---------------------------------------------------------------------- who is asking
@contextmanager
def priority():
    """Everything inside is reply disposition or speed-to-lead work: it may use the whole minute
    and is never refused."""
    was = getattr(_local, "priority", False)
    _local.priority = True
    try:
        yield
    finally:
        _local.priority = was


@contextmanager
def nowait():
    """Everything inside must not sleep on the budget. The worker's send loop is the one thread
    that also drains replies, so a read it can do without is skipped, never waited for."""
    was = getattr(_local, "nowait", False)
    _local.nowait = True
    try:
        yield
    finally:
        _local.nowait = was


def enabled() -> bool:
    return bool(config.RATE_BUDGET)


# ---------------------------------------------------------------------- the share
def quiet_window() -> tuple[int, int] | None:
    """The quiet window as minutes of the business day (start, end), or None when it is off."""
    raw = (config.RATE_QUIET or "").strip().lower()
    if raw in ("", "off", "none", "0", "no"):
        return None
    try:
        a, b = raw.split("-", 1)
        ah, am = a.split(":")
        bh, bm = b.split(":")
        start, end = int(ah) * 60 + int(am), int(bh) * 60 + int(bm)
    except ValueError:
        log.warning("SMS_AGENT_RATE_QUIET=%r is not HH:MM-HH:MM; no quiet window applies", raw)
        return None
    return (start, end) if start != end else None


def is_quiet(t: float) -> bool:
    win = quiet_window()
    if not win:
        return False
    local = datetime.fromtimestamp(t, timezone.utc).astimezone(ZoneInfo(config.BUSINESS_TZ))
    m = local.hour * 60 + local.minute
    start, end = win
    return start <= m < end if start < end else (m >= start or m < end)


def per_min(t: float | None = None, is_priority: bool = False) -> int:
    """What this caller may use in the current minute."""
    t = time.time() if t is None else t
    cap = max(1, int(config.RATE_PER_MIN))
    if is_priority:
        return cap
    if is_quiet(t):
        return max(1, min(cap, int(config.RATE_QUIET_PER_MIN)))
    return max(1, int(cap * (1 - RESERVE)))


def per_hour(t: float | None = None) -> int:
    return int(config.RATE_PER_HOUR) or max(1, int(config.RATE_PER_MIN)) * 60


def _keys(t: float) -> tuple[str, str]:
    g = time.gmtime(t)
    return time.strftime("m:%Y-%m-%dT%H:%M", g), time.strftime("h:%Y-%m-%dT%H", g)


# ---------------------------------------------------------------------- the counter
def _conn() -> sqlite3.Connection:
    """A connection of this module's own, to the app's database.

    Deliberately not the store's: the store opens a transaction around its own writes, and a
    request counted from inside one would be a transaction within a transaction on the same
    connection. A second connection to the same file simply waits its turn.
    """
    path = str(config.DB_PATH)
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", "") != path:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _local.conn, _local.path = c, path
    return c


def _take(t: float, is_priority: bool) -> tuple[float, str]:
    """Try to count one request at time t. (0, "") when counted, else (seconds to wait, which
    window is full: "minute" or "hour")."""
    mkey, hkey = _keys(t)
    cap_m, cap_h = per_min(t, is_priority), per_hour(t)
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        have = {r["win"]: r["n"] for r in c.execute(
            "SELECT win, n FROM rate_budget WHERE app=? AND win IN (?,?)", (APP, mkey, hkey))}
        if have.get(hkey, 0) >= cap_h:
            c.execute("COMMIT")
            return 3600 - (t % 3600) + 0.5, "hour"
        if have.get(mkey, 0) >= cap_m:
            c.execute("COMMIT")
            return 60 - (t % 60) + 0.2, "minute"
        _count(c, t)
        c.execute("COMMIT")
        return 0.0, ""
    except Exception:
        c.execute("ROLLBACK")
        raise


def _count(c: sqlite3.Connection, t: float) -> None:
    mkey, hkey = _keys(t)
    for k in (mkey, hkey):
        c.execute("INSERT INTO rate_budget (app, win, n) VALUES (?,?,1) "
                  "ON CONFLICT(app, win) DO UPDATE SET n=n+1", (APP, k))
    if int(t) % 97 == 0:            # now and then, drop windows more than a day old
        old_m, old_h = _keys(t - 86400)
        c.execute("DELETE FROM rate_budget WHERE app=? AND ((win LIKE 'm:%' AND win<?) OR (win LIKE 'h:%' AND win<?))",
                  (APP, old_m, old_h))


def _force_count(t: float) -> None:
    """Count one request whatever the windows hold (count mode, and a priority overrun)."""
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        _count(c, t)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def _count_only(t: float, is_priority: bool) -> float:
    """Count mode: the request is counted and never delayed. A request the limit WOULD have
    delayed is noted, so the digest shows what switching the limit on would cost."""
    try:
        need, which = _take(t, is_priority)
        if need:
            _note("would_" + which, t)
            _force_count(t)
    except sqlite3.Error as exc:
        log.warning("rate budget unavailable (%s); this request is not counted", exc)
    return 0.0


def _note(kind: str, t: float) -> None:
    """Count a wait, a refusal or an overrun, so the digest can report it (plan 0.14: each app
    reports its own)."""
    day = time.strftime("%Y-%m-%d", time.gmtime(t))
    try:
        _conn().execute("INSERT INTO rate_notes (day, kind, n) VALUES (?,?,1) "
                        "ON CONFLICT(day, kind) DO UPDATE SET n=n+1", (day, kind))
    except sqlite3.Error:
        pass


def acquire(is_priority: bool | None = None, wait: bool | None = None, clock=time.time, sleep=time.sleep,
            max_wait: float | None = None) -> float:
    """Count one request against this app's share. Returns the seconds waited.

    Priority work waits for the next minute and is never refused. Other work waits too, up to
    `max_wait`, then raises BudgetExhausted; inside `nowait()` it raises BudgetBusy at once.
    """
    if not enabled():
        return 0.0
    pr = getattr(_local, "priority", False) if is_priority is None else bool(is_priority)
    if not config.RATE_ENFORCE:
        return _count_only(clock(), pr)
    may_wait = (not getattr(_local, "nowait", False)) if wait is None else bool(wait)
    limit = float(config.RATE_MAX_WAIT if max_wait is None else max_wait)
    waited = 0.0
    while True:
        t = clock()
        try:
            need, which = _take(t, pr)
        except sqlite3.Error as exc:
            # The counter is bookkeeping. It must never be the reason a reply is not handled.
            log.warning("rate budget unavailable (%s); this request is not counted", exc)
            return waited
        if not need:
            return waited
        _note(which, t)
        if not may_wait and not pr:
            raise BudgetBusy("the texting app's DataSift share is used for this %s" % which)
        if waited + need > limit:
            if pr:
                # Delayed long enough. Go, and leave a mark where the digest reads it.
                _note("overrun", t)
                try:
                    _force_count(t)
                except sqlite3.Error:
                    pass
                return waited
            raise BudgetExhausted("the texting app's DataSift share is used for this %s" % which)
        sleep(need)
        waited += need


def usage(t: float | None = None) -> dict:
    """What this app has used in the current minute and hour, and how often it had to wait today."""
    t = time.time() if t is None else t
    mkey, hkey = _keys(t)
    c = _conn()
    have = {r["win"]: r["n"] for r in c.execute(
        "SELECT win, n FROM rate_budget WHERE app=? AND win IN (?,?)", (APP, mkey, hkey))}
    day = time.strftime("%Y-%m-%d", time.gmtime(t))
    notes = {r["kind"]: r["n"] for r in c.execute("SELECT kind, n FROM rate_notes WHERE day=?", (day,))}
    return {"app": APP, "on": enabled(), "enforcing": bool(enabled() and config.RATE_ENFORCE),
            "minute": have.get(mkey, 0), "minute_cap": per_min(t, True),
            "minute_cap_scans": per_min(t, False), "hour": have.get(hkey, 0), "hour_cap": per_hour(t),
            "quiet": is_quiet(t), "minute_waits_today": int(notes.get("minute", 0)),
            "hour_waits_today": int(notes.get("hour", 0)), "overruns_today": int(notes.get("overrun", 0)),
            "would_wait_today": int(notes.get("would_minute", 0)) + int(notes.get("would_hour", 0))}


def line(t: float | None = None) -> str:
    """One sentence for the digest."""
    u = usage(t)
    text = "DataSift requests: %d of %d this hour, %d of %d this minute" % (
        u["hour"], u["hour_cap"], u["minute"], u["minute_cap"])
    if u["on"] and not u["enforcing"]:
        text += "; counting only, nothing is delayed"
        if u["would_wait_today"]:
            text += " (%d request(s) today would have waited under the limit)" % u["would_wait_today"]
    waits = u["minute_waits_today"] + u["hour_waits_today"]
    if waits:
        text += "; waited %d time(s) today" % waits
    if u["overruns_today"]:
        text += "; %d reply write(s) went over the share rather than be dropped" % u["overruns_today"]
    return text


# ---------------------------------------------------------------------- the client
def wrap(client):
    """Make every HTTP request this client sends count against the share. Returns the client.

    The standalone client funnels every attempt, retries included, through its requests session,
    so that is where the count goes: one count per request on the wire. A client without a
    session (the shared Deal Room one, on a workstation) is counted once per `_request` call.
    The class is not touched, only this one object, because other apps import it.
    """
    if client is None or getattr(client, "_rate_budgeted", False):
        return client
    session = getattr(client, "_session", None)
    if session is not None and callable(getattr(session, "request", None)):
        inner = session.request

        def request(*args, **kwargs):
            acquire()
            return inner(*args, **kwargs)

        session.request = request
    elif callable(getattr(client, "_request", None)):
        inner_request = client._request

        def _request(*args, **kwargs):
            acquire()
            return inner_request(*args, **kwargs)

        client._request = _request
    else:
        log.warning("rate budget: this CRM client has no request method to count; it runs uncounted")
        return client
    try:
        client._rate_budgeted = True
    except Exception:  # noqa: BLE001 - a client that refuses attributes is still wrapped
        pass
    return client
