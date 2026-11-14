"""The clock: fires every live workflow whose trigger is a schedule, at business-local time.

One thread inside the brain, because Fly runs one machine and the queue is one SQLite file. Each
tick finds, for every schedule trigger, the latest slot at or before now. A slot newer than the one
last fired, and still inside the grace window, enqueues one task. The fired slot is persisted, so a
restart or a redeploy neither double-fires nor bursts the morning's missed jobs at once: a slot
older than the grace window is recorded as skipped and logged, never silently run late.

Grammar (tokens in any order, case-insensitive):
    07:15                       once a day at 07:15
    07:15 Mon-Fri               weekdays only; days as Mon-Fri, Mon,Wed,Fri, weekdays, daily
    Mon 07:00 America/New_York  an IANA zone overrides the business zone
    1st 07:00                   monthly, on the 1st (2nd, 3rd, 15th ...)
    every 30 min 08:00-18:00 Mon-Fri   every 30 minutes inside a window
    every 15 min                every 15 minutes, all day
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import store

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
GRACE = timedelta(minutes=int(float(os.environ.get("AGENTS_SCHEDULE_GRACE_MIN") or 20)))
BUSINESS_TZ = os.environ.get("AGENTS_BUSINESS_TZ") or "America/New_York"


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    text: str
    tz: str
    times: tuple            # ((h, m), ...) fixed times; empty when every_min is set
    every_min: int          # 0 means fixed times
    window: tuple | None    # (start_min, end_min) inside the day, for every_min
    days: frozenset         # weekday numbers, Monday 0
    monthday: int           # 0 means any day of the month


def _days(tok: str) -> set[int] | None:
    t = tok.lower().strip(",")
    if t in ("daily", "everyday"):
        return set(range(7))
    if t == "weekdays":
        return set(range(5))
    if t == "weekends":
        return {5, 6}
    out: set[int] = set()
    for part in t.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a[:3] not in DAYS or b[:3] not in DAYS:
                return None
            i, j = DAYS.index(a[:3]), DAYS.index(b[:3])
            out.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part[:3] in DAYS and part.isalpha():
            out.add(DAYS.index(part[:3]))
        else:
            return None
    return out or None


def parse(text: str, default_tz: str = "") -> Spec:
    raw = " ".join(str(text or "").split())
    if not raw:
        raise ScheduleError("empty schedule")
    toks = raw.split(" ")
    tz = default_tz or BUSINESS_TZ
    times: list[tuple[int, int]] = []
    every = 0
    window = None
    days: set[int] = set()
    monthday = 0
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        if low == "every":
            if i + 2 >= len(toks) or not toks[i + 1].isdigit():
                raise ScheduleError("%r: write 'every N min'" % raw)
            every = int(toks[i + 1])
            if not toks[i + 2].lower().startswith("min"):
                raise ScheduleError("%r: only minutes are supported after 'every'" % raw)
            if every < 1 or every > 720:
                raise ScheduleError("%r: every %d minutes is out of range" % (raw, every))
            i += 3
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", t)
        if m:
            a = int(m.group(1)) * 60 + int(m.group(2))
            b = int(m.group(3)) * 60 + int(m.group(4))
            if not (0 <= a < b <= 24 * 60):
                raise ScheduleError("%r: window %s must run forward inside one day" % (raw, t))
            window = (a, b)
            i += 1
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if h > 23 or mi > 59:
                raise ScheduleError("%r: %s is not a time" % (raw, t))
            times.append((h, mi))
            i += 1
            continue
        m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)", low)
        if m:
            monthday = int(m.group(1))
            if not 1 <= monthday <= 28:
                raise ScheduleError("%r: monthly days run 1st to 28th (every month has them)" % raw)
            i += 1
            continue
        if "/" in t:
            try:
                ZoneInfo(t)
            except Exception:  # noqa: BLE001
                raise ScheduleError("%r: unknown time zone %s" % (raw, t))
            tz = t
            i += 1
            continue
        d = _days(t)
        if d is None:
            raise ScheduleError("%r: cannot read %r" % (raw, t))
        days |= d
        i += 1
    if every and times:
        raise ScheduleError("%r: use either fixed times or 'every N min', not both" % raw)
    if not every and not times:
        raise ScheduleError("%r: give a time like 07:15 or 'every 30 min'" % raw)
    if window and not every:
        raise ScheduleError("%r: a window like 08:00-18:00 needs 'every N min'" % raw)
    return Spec(text=raw, tz=tz, times=tuple(sorted(times)), every_min=every, window=window,
                days=frozenset(days or range(7)), monthday=monthday)


def _day_slots(spec: Spec, day) -> list[datetime]:
    """Every slot on one local calendar day, as aware datetimes in the spec's zone."""
    if day.weekday() not in spec.days or (spec.monthday and day.day != spec.monthday):
        return []
    z = ZoneInfo(spec.tz)
    if spec.every_min:
        a, b = spec.window or (0, 24 * 60)
        mins = range(a, b + (1 if spec.window else 0), spec.every_min)
        return [datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=z) for m in mins if m < 24 * 60]
    return [datetime(day.year, day.month, day.day, h, m, tzinfo=z) for h, m in spec.times]


def last_slot(spec: Spec, now: datetime) -> datetime | None:
    """The latest slot at or before now (looks back far enough for a monthly schedule)."""
    local = now.astimezone(ZoneInfo(spec.tz))
    for back in range(0, 33):
        day = (local - timedelta(days=back)).date()
        slots = [s for s in _day_slots(spec, day) if s <= now]
        if slots:
            return max(slots)
    return None


def next_slots(spec: Spec, now: datetime, n: int = 5) -> list[datetime]:
    out: list[datetime] = []
    local = now.astimezone(ZoneInfo(spec.tz))
    for ahead in range(0, 70):
        day = (local + timedelta(days=ahead)).date()
        out += [s for s in _day_slots(spec, day) if s > now]
        if len(out) >= n:
            break
    return sorted(out)[:n]


def schedules(roster) -> list[tuple]:
    """(workflow, owner, spec) for every live workflow with a schedule trigger. A bad schedule is
    logged and skipped: one typo must not stop the clock for every other workflow."""
    out = []
    for wf in roster.workflows.values():
        if not wf.live:
            continue
        for val in wf.trigger_values("schedule"):
            try:
                out.append((wf, wf.owner, parse(val)))
            except ScheduleError as exc:
                log.error("workflow %s: %s", wf.name, exc)
    return out


def tick(roster, now: datetime | None = None, enqueue=None) -> list[dict]:
    """Fire whatever is due. Returns one entry per decision, for the log and the tests."""
    now = now or datetime.now(timezone.utc)
    enqueue = enqueue or store.enqueue
    fired = []
    for wf, owner, spec in schedules(roster):
        slot = last_slot(spec, now)
        if slot is None:
            continue
        key = "schedule:%s:%s" % (wf.name, spec.text)
        last = store.kv_get(key)
        stamp = slot.astimezone(timezone.utc).isoformat(timespec="seconds")
        if last and last >= stamp:
            continue
        store.kv_set(key, stamp)
        if now - slot > GRACE:
            log.warning("schedule %s: slot %s missed by more than %s, not run late", wf.name, stamp, GRACE)
            fired.append({"workflow": wf.name, "slot": stamp, "action": "skipped_late"})
            continue
        if store.open_task(wf.name):
            log.info("schedule %s: the last run is still queued or running, slot %s skipped", wf.name, stamp)
            fired.append({"workflow": wf.name, "slot": stamp, "action": "skipped_busy"})
            continue
        task_id, _ = enqueue(owner, wf.name, subject="%s (scheduled %s)" % (wf.name.replace("_", " "), spec.text),
                             source="schedule", payload={"slot": stamp, "schedule": spec.text})
        fired.append({"workflow": wf.name, "slot": stamp, "action": "queued", "task": task_id})
        log.info("schedule %s: queued task %s for slot %s", wf.name, task_id, stamp)
    return fired


def loop(stop: threading.Event, load_roster, every_s: float = 30.0) -> None:
    store.init()
    while not stop.is_set():
        try:
            roster = load_roster()
            if roster:
                tick(roster)
        except Exception as exc:  # noqa: BLE001  (the clock must keep ticking)
            log.error("scheduler tick failed: %s", exc)
        stop.wait(every_s)


def start_thread(load_roster) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop, load_roster), name="agents-scheduler", daemon=True)
    t.start()
    return t, stop
