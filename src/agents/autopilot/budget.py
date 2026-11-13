"""One request budget for the account (build plan 0.14).

DataSift allows 120 requests a minute and 5,000 an hour for the WHOLE account, shared by every
key-based tool on it: this brain, the first-to-market box, the texting app, scripts, Zaps. About
80 a minute can be sustained. The brain's share is 45 a minute and 2,800 an hour; the
first-to-market box gets 20 and the texting app 10, and the rest stays in reserve.

Every process of one app shares one counter, kept in the app's own SQLite file, because a clock
that fires three stages at 8:00 and a person running the CLI beside it must not each believe they
own the whole share. Two fixed windows are counted (the minute and the hour). A request that does
not fit the minute waits for the next one; a request that does not fit the hour raises, because a
stage that sleeps forty minutes inside a run is worse than a stage that says "the hourly budget is
used" and runs again at its next slot.

Priority work (a reply disposition, speed to lead) may use the whole minute. Everything else stops
at 80% of it, which is what keeps a long scan from starving the thing a person is waiting on.

At night (20:00 to 06:00 business time) the other apps are idle, so the long scans the plan puts
on the night list run against a larger share.

One window is quiet: while the first-to-market box makes its daily run (06:30 business time, 10 to
40 minutes) this app drops to a trickle, so the run that brings the day's records in never waits
on a scan. DATASIFT_RATE_QUIET names the window ("06:25-07:15" by default, "off" for an install
with no such run).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import store

log = logging.getLogger(__name__)

RESERVE = 0.2            # the part of each minute only priority work may use
NIGHT = (20, 6)          # business-local hours when the larger share applies
TZ = "America/New_York"


class BudgetExhausted(RuntimeError):
    """The app's share of the hour is used. The caller should stop and say so, not sleep."""


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return default


def app() -> str:
    return os.environ.get("DATASIFT_RATE_APP") or "brain"


def is_night(t: float) -> bool:
    h = datetime.fromtimestamp(t, timezone.utc).astimezone(ZoneInfo(TZ)).hour
    return h >= NIGHT[0] or h < NIGHT[1]


def quiet_window() -> tuple[int, int] | None:
    """The quiet window as minutes of the business day (start, end), or None when it is off."""
    raw = (os.environ.get("DATASIFT_RATE_QUIET") or "06:25-07:15").strip().lower()
    if raw in ("off", "none", "0", "no"):
        return None
    try:
        a, b = raw.split("-", 1)
        ah, am = a.split(":")
        bh, bm = b.split(":")
        start, end = int(ah) * 60 + int(am), int(bh) * 60 + int(bm)
    except ValueError:
        log.warning("DATASIFT_RATE_QUIET=%r is not HH:MM-HH:MM; the default window applies", raw)
        return 6 * 60 + 25, 7 * 60 + 15
    return (start, end) if start != end else None


def is_quiet(t: float) -> bool:
    win = quiet_window()
    if not win:
        return False
    local = datetime.fromtimestamp(t, timezone.utc).astimezone(ZoneInfo(TZ))
    m = local.hour * 60 + local.minute
    start, end = win
    return start <= m < end if start < end else (m >= start or m < end)


def per_min(t: float | None = None) -> int:
    t = time.time() if t is None else t
    if is_quiet(t):
        return _int("DATASIFT_RATE_QUIET_PER_MIN", 12)
    if is_night(t):
        return _int("DATASIFT_RATE_NIGHT_PER_MIN", 70)
    return _int("DATASIFT_RATE_PER_MIN", 45)


def per_hour(t: float | None = None) -> int:
    t = time.time() if t is None else t
    if is_night(t):
        return _int("DATASIFT_RATE_NIGHT_PER_HOUR", 3800)
    return _int("DATASIFT_RATE_PER_HOUR", 2800)


def gap(t: float | None = None) -> float:
    """Seconds between two requests that spreads the minute's share evenly."""
    return round(60.0 / max(1, per_min(t)) * 0.9, 3)


def _keys(t: float) -> tuple[str, str]:
    g = time.gmtime(t)
    return time.strftime("m:%Y-%m-%dT%H:%M", g), time.strftime("h:%Y-%m-%dT%H", g)


def _take(name: str, t: float, priority: bool) -> tuple[float, str]:
    """Try to count one request at time t. Returns (0, "") when counted, else (seconds to wait,
    "minute" or "hour")."""
    mkey, hkey = _keys(t)
    cap_m, cap_h = per_min(t), per_hour(t)
    if not priority:
        cap_m = max(1, int(cap_m * (1 - RESERVE)))
    with store._LOCK, store._db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            have = {r["win"]: r["n"] for r in c.execute(
                "SELECT win, n FROM ap_rate WHERE app=? AND win IN (?,?)", (name, mkey, hkey))}
            if have.get(hkey, 0) >= cap_h:
                c.execute("COMMIT")
                return 3600 - (t % 3600) + 0.5, "hour"
            if have.get(mkey, 0) >= cap_m:
                c.execute("COMMIT")
                return 60 - (t % 60) + 0.2, "minute"
            for k in (mkey, hkey):
                c.execute("INSERT INTO ap_rate (app, win, n) VALUES (?,?,1) "
                          "ON CONFLICT(app, win) DO UPDATE SET n=n+1", (name, k))
            if int(t) % 97 == 0:            # now and then, drop windows more than a day old
                old_m, old_h = _keys(t - 86400)
                c.execute("DELETE FROM ap_rate WHERE app=? AND ((win LIKE 'm:%' AND win<?) OR (win LIKE 'h:%' AND win<?))",
                          (name, old_m, old_h))
            c.execute("COMMIT")
            return 0.0, ""
        except Exception:
            c.execute("ROLLBACK")
            raise


def acquire(priority: bool = False, name: str | None = None, clock=time.time, sleep=time.sleep,
            max_wait: float = 180.0) -> float:
    """Count one request against the app's share, waiting for the next minute if this one is full.
    Returns the seconds waited. Raises BudgetExhausted when the hour is used up."""
    name = name or app()
    waited = 0.0
    while True:
        t = clock()
        try:
            wait, which = _take(name, t, priority)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                store.init()
                continue
            log.warning("rate budget unavailable (%s); pacing by gap only", exc)
            return waited
        if not wait:
            return waited
        _note(which, t)
        if which == "hour":
            raise BudgetExhausted("the %s app has used its %d DataSift requests for this hour; it resumes at the top "
                                  "of the hour" % (name, per_hour(t)))
        if waited + wait > max_wait:
            raise BudgetExhausted("the %s app waited %.0f seconds for a free slot in its %d requests a minute"
                                  % (name, waited, per_min(t)))
        sleep(wait)
        waited += wait


def _note(which: str, t: float) -> None:
    """Count a refusal so the digest can report it (plan 0.14: each app reports its own)."""
    day = time.strftime("%Y-%m-%d", time.gmtime(t))
    key = "autopilot:rate:%s:%s" % (which, day)
    try:
        store.kv_set(key, str(int(store.kv_get(key) or 0) + 1))
    except Exception:  # noqa: BLE001
        pass


def usage(name: str | None = None, t: float | None = None) -> dict:
    """What this app has used in the current minute and hour, and how often it had to wait today."""
    name = name or app()
    t = time.time() if t is None else t
    mkey, hkey = _keys(t)
    with store._LOCK, store._db() as c:
        have = {r["win"]: r["n"] for r in c.execute(
            "SELECT win, n FROM ap_rate WHERE app=? AND win IN (?,?)", (name, mkey, hkey))}
    day = time.strftime("%Y-%m-%d", time.gmtime(t))
    return {"app": name, "minute": have.get(mkey, 0), "minute_cap": per_min(t), "hour": have.get(hkey, 0),
            "hour_cap": per_hour(t), "night": is_night(t), "quiet": is_quiet(t),
            "minute_waits_today": int(store.kv_get("autopilot:rate:minute:%s" % day) or 0),
            "hour_refusals_today": int(store.kv_get("autopilot:rate:hour:%s" % day) or 0)}
