"""A request budget for the first-to-market app's DataSift calls (build plan 0.14).

DataSift allows 120 requests a minute and 5,000 an hour for the WHOLE account, shared by every
tool that carries its key: the brain, this box, the texting app, scripts, Zaps. About 80 a minute
can be sustained. The plan gives the brain 45 a minute, this box 20 and the texting app 10, and
keeps the rest in reserve.

Until this module nothing on this box counted anything. The uploader slept a tenth of a second
between rows, the lien release pass slept 1.25 seconds between calls, the sold guard half a
second, and each believed it had the account to itself. That held while the box only ran once
each morning. It stops holding the day a long job (the yearly sold re-entry, a catch-up upload)
runs beside the morning pull, or beside the brain's own stages.

HOW IT WORKS. One SQLite file on the volume holds two fixed windows per bucket, the minute and the
hour, so every process on this box counts against one share. A request that does not fit the
minute waits for the next minute. A request that does not fit the hour raises BudgetExhausted,
because a stage that sleeps forty minutes inside a run is worse than a stage that says "the
hourly budget is used" and is picked up again tomorrow.

Two buckets, because DataSift counts them separately: "datasift" (apiv2.reisift.io) and "siftmap"
(map.reisift.io).

OFF BY DEFAULT. With FTM_RATE_BUDGET unset this module counts nothing and never sleeps, so a
deploy changes no timing until someone turns it on.

    FTM_RATE_BUDGET=1          turn the budget on
    FTM_RATE_PER_MIN=20        this box's share of the minute (datasift bucket)
    FTM_RATE_PER_HOUR=1200     this box's share of the hour
    FTM_SIFTMAP_PER_MIN=20     the same, for SiftMap
    FTM_SIFTMAP_PER_HOUR=1200
    FTM_RATE_DB=/data/ftm_rate.db   the shared counter (default: <state dir>/ftm_rate.db)

    python src/ftm_budget.py            # what this box has used this minute, hour and day
    python src/ftm_budget.py --selftest # zero network, throwaway file
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

log = logging.getLogger("ftm_budget")

DEFAULTS = {
    "datasift": {"per_min": 20, "per_hour": 1200, "env": "FTM_RATE"},
    "siftmap": {"per_min": 20, "per_hour": 1200, "env": "FTM_SIFTMAP"},
}
# The part of each minute only priority work may use. The morning upload is the priority work on
# this box; a long scan (the sold re-entry, the monthly audit) asks without priority and stops at
# 80% of the minute, so it can never starve the pull that brings the day's records in.
RESERVE = 0.2
_TRUE = ("1", "true", "yes", "on")
_warned = {"db": False}


class BudgetExhausted(RuntimeError):
    """This box's share of the hour is used. The caller stops and says so; it does not sleep."""


def enabled() -> bool:
    return (os.environ.get("FTM_RATE_BUDGET") or "").strip().lower() in _TRUE


def db_path() -> Path:
    raw = (os.environ.get("FTM_RATE_DB") or "").strip()
    if raw:
        return Path(raw)
    state = (os.environ.get("SIFTSTACK_STATE_DIR") or "").strip()
    base = Path(state) if state else Path(__file__).resolve().parent.parent
    return base / "ftm_rate.db"


def _int(name: str, default: int) -> int:
    try:
        value = int(float(os.environ.get(name) or default))
    except ValueError:
        return default
    return value if value > 0 else default


def per_min(name: str = "datasift") -> int:
    d = DEFAULTS.get(name) or DEFAULTS["datasift"]
    return _int(d["env"] + "_PER_MIN", d["per_min"])


def per_hour(name: str = "datasift") -> int:
    d = DEFAULTS.get(name) or DEFAULTS["datasift"]
    return _int(d["env"] + "_PER_HOUR", d["per_hour"])


def _keys(t: float) -> tuple[str, str]:
    g = time.gmtime(t)
    return time.strftime("m:%Y-%m-%dT%H:%M", g), time.strftime("h:%Y-%m-%dT%H", g)


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=20, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS rate (app TEXT NOT NULL, win TEXT NOT NULL, n INTEGER NOT NULL, "
              "PRIMARY KEY (app, win))")
    c.execute("CREATE TABLE IF NOT EXISTS note (day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL, "
              "PRIMARY KEY (day, kind))")
    return c


def _take(name: str, t: float, priority: bool) -> tuple[float, str]:
    """Try to count one request at time t. (0, "") when counted, else (seconds to wait, which window)."""
    mkey, hkey = _keys(t)
    cap_m, cap_h = per_min(name), per_hour(name)
    if not priority:
        cap_m = max(1, int(cap_m * (1 - RESERVE)))
    c = _connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            have = dict(c.execute("SELECT win, n FROM rate WHERE app=? AND win IN (?,?)", (name, mkey, hkey)).fetchall())
            if have.get(hkey, 0) >= cap_h:
                c.execute("COMMIT")
                return 3600 - (t % 3600) + 0.5, "hour"
            if have.get(mkey, 0) >= cap_m:
                c.execute("COMMIT")
                return 60 - (t % 60) + 0.2, "minute"
            for k in (mkey, hkey):
                c.execute("INSERT INTO rate (app, win, n) VALUES (?,?,1) "
                          "ON CONFLICT(app, win) DO UPDATE SET n=n+1", (name, k))
            if int(t) % 97 == 0:               # now and then, drop windows more than two days old
                old_m, old_h = _keys(t - 2 * 86400)
                c.execute("DELETE FROM rate WHERE app=? AND ((win LIKE 'm:%' AND win<?) OR (win LIKE 'h:%' AND win<?))",
                          (name, old_m, old_h))
            c.execute("COMMIT")
            return 0.0, ""
        except Exception:
            c.execute("ROLLBACK")
            raise
    finally:
        c.close()


def note(kind: str, t: float | None = None) -> None:
    """Count one refusal or wait for the day, so the health report can say how often it happened
    (plan 0.14: each app reports its own refusals). Never raises."""
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if t is None else t))
    try:
        c = _connect()
        try:
            c.execute("INSERT INTO note (day, kind, n) VALUES (?,?,1) "
                      "ON CONFLICT(day, kind) DO UPDATE SET n=n+1", (day, kind))
        finally:
            c.close()
    except Exception:  # noqa: BLE001  (a counter that cannot be written must not stop an upload)
        pass


def acquire(name: str = "datasift", priority: bool = True, clock=time.time, sleep=time.sleep,
            max_wait: float = 180.0) -> float:
    """Count one request against this box's share, waiting for the next minute when this one is
    full. Returns the seconds waited. Raises BudgetExhausted when the hour is used up.

    Does nothing at all, and returns 0.0, unless FTM_RATE_BUDGET is on.

    A counter file that cannot be opened (a full or missing volume) never blocks the pipeline: the
    call falls back to an even gap between requests, which keeps one process inside its share and
    is the best that can be done without the shared count."""
    if not enabled():
        return 0.0
    waited = 0.0
    while True:
        t = clock()
        try:
            wait, which = _take(name, t, priority)
        except (sqlite3.Error, OSError) as exc:
            if not _warned["db"]:
                _warned["db"] = True
                log.warning("rate budget file unavailable (%s); pacing by an even gap instead", exc)
            gap = 60.0 / max(1, per_min(name))
            sleep(gap)
            return waited + gap
        if not wait:
            return waited
        note(name + ":" + which, t)
        if which == "hour":
            raise BudgetExhausted("this box has used its %d %s requests for the hour; it can go on at the top "
                                  "of the hour" % (per_hour(name), name))
        if waited + wait > max_wait:
            raise BudgetExhausted("waited %.0f seconds for a free slot in %d %s requests a minute"
                                  % (waited, per_min(name), name))
        sleep(wait)
        waited += wait


def usage(name: str = "datasift", t: float | None = None) -> dict:
    """What this box has used in the current minute and hour, and today's waits and refusals.
    Readable whether or not the budget is on (an off budget reads all zeros)."""
    t = time.time() if t is None else t
    mkey, hkey = _keys(t)
    day = time.strftime("%Y-%m-%d", time.gmtime(t))
    out = {"bucket": name, "enabled": enabled(), "minute": 0, "minute_cap": per_min(name), "hour": 0,
           "hour_cap": per_hour(name), "minute_waits_today": 0, "hour_refusals_today": 0, "server_429_today": 0}
    try:
        if not db_path().exists():
            return out
        c = _connect()
        try:
            have = dict(c.execute("SELECT win, n FROM rate WHERE app=? AND win IN (?,?)", (name, mkey, hkey)).fetchall())
            notes = dict(c.execute("SELECT kind, n FROM note WHERE day=?", (day,)).fetchall())
        finally:
            c.close()
    except (sqlite3.Error, OSError):
        return out
    out.update(minute=have.get(mkey, 0), hour=have.get(hkey, 0),
               minute_waits_today=notes.get(name + ":minute", 0),
               hour_refusals_today=notes.get(name + ":hour", 0),
               server_429_today=notes.get(name + ":429", 0))
    return out


def summary_lines() -> list[str]:
    """One plain line per bucket for the health report's full view."""
    lines = []
    for name in DEFAULTS:
        u = usage(name)
        if not u["enabled"]:
            lines.append("%s: budget off (FTM_RATE_BUDGET is not set)" % name)
            continue
        lines.append("%s: %d of %d this minute, %d of %d this hour, %d wait(s), %d hourly refusal(s), "
                     "%d server refusal(s) today"
                     % (name, u["minute"], u["minute_cap"], u["hour"], u["hour_cap"], u["minute_waits_today"],
                        u["hour_refusals_today"], u["server_429_today"]))
    return lines


# ── selftest ──────────────────────────────────────────────────────────


def selftest() -> int:
    """Assertions on a throwaway counter file. No network, nothing written outside a temp folder."""
    import tempfile

    fails = 0

    def check(cond: bool, said: str) -> None:
        nonlocal fails
        print("  [%s] %s" % ("pass" if cond else "FAIL", said))
        fails += 0 if cond else 1

    keep = {k: os.environ.get(k) for k in ("FTM_RATE_BUDGET", "FTM_RATE_DB", "FTM_RATE_PER_MIN", "FTM_RATE_PER_HOUR")}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["FTM_RATE_DB"] = os.path.join(tmp, "rate.db")
            os.environ.pop("FTM_RATE_BUDGET", None)
            slept: list[float] = []
            check(acquire(sleep=slept.append) == 0.0 and not slept, "with the switch off nothing is counted and nothing waits")
            check(not os.path.exists(os.environ["FTM_RATE_DB"]), "with the switch off no counter file is created")

            os.environ.update(FTM_RATE_BUDGET="1", FTM_RATE_PER_MIN="3", FTM_RATE_PER_HOUR="5")
            now = [1_800_000_000.0]            # a fixed clock, on a minute boundary
            now[0] -= now[0] % 3600

            def nap(seconds: float) -> None:
                slept.append(seconds)
                now[0] += seconds

            for _ in range(3):
                acquire(clock=lambda: now[0], sleep=nap)
            check(not slept, "three requests fit a three a minute share with no wait")
            acquire(clock=lambda: now[0], sleep=nap)
            check(len(slept) == 1 and 59 < slept[0] <= 61, "the fourth waits for the next minute")
            acquire(clock=lambda: now[0], sleep=nap)
            raised = False
            try:
                acquire(clock=lambda: now[0], sleep=nap)
            except BudgetExhausted:
                raised = True
            check(raised, "the sixth in an hour of five is refused, not slept on")
            u = usage(t=now[0])
            check(u["hour"] == 5 and u["hour_refusals_today"] == 1, "the refusal is counted for the day's report")
        finally:
            for k, v in keep.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    print("\n  %s" % ("all passed" if not fails else "%d FAILED" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    for line in summary_lines():
        print(line)
