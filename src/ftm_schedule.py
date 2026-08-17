"""Long-running scheduler that fires the FTM run at a fixed local time.

This is the cloud process. It sleeps until the next scheduled slot, runs
ftm_runner in-process, records the outcome, and goes back to sleep.

    python src/ftm_schedule.py                 # run forever on the schedule
    python src/ftm_schedule.py --once          # run now, then exit
    python src/ftm_schedule.py --next          # print the next 5 fire times

WHY A PROCESS AND NOT `fly machine run --schedule`:
Fly's machine schedules are coarse (hourly / daily / weekly) and Fly picks the
minute. A notice pull wants a specific local hour, because the counties publish
in the morning and the whole point of first-to-market is being early. This also
keeps the run inside one machine with one volume, so run state, the seen-ID
cache and the session cookie all persist across runs and redeploys.

TIME IS BUSINESS-LOCAL, NOT UTC. Knox and Blount operate on US Eastern and the
container clock is UTC, so a naive schedule drifts by an hour twice a year and
fires in the middle of the night half the time.

Configuration (all env vars):
  FTM_SCHEDULE          comma-separated HH:MM in business time (default "06:30")
  FTM_SCHEDULE_DAYS     comma-separated weekday names or "all" (default "all")
  BUSINESS_TIMEZONE     default "America/New_York"
  FTM_ARGS              extra ftm_runner args, e.g. "--commit --stages notices"
  FTM_RUN_ON_BOOT       "1" to fire once at startup (useful for a first deploy)
  FTM_JITTER_SECONDS    random 0..N delay before each run (default 0)
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import shlex
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import ftm_runner  # noqa: E402

logger = logging.getLogger("ftm_schedule")

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.BUSINESS_TIMEZONE)
    except Exception:
        logger.warning("Unknown BUSINESS_TIMEZONE %r; falling back to UTC",
                       config.BUSINESS_TIMEZONE)
        return ZoneInfo("UTC")


def _slots() -> list[tuple[int, int]]:
    """Parse FTM_SCHEDULE into (hour, minute) pairs."""
    raw = os.getenv("FTM_SCHEDULE", "06:30")
    out: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            h, m = chunk.split(":")
            hh, mm = int(h), int(m)
        except ValueError:
            logger.error("Bad FTM_SCHEDULE entry %r (want HH:MM), ignoring", chunk)
            continue
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            logger.error("Out-of-range FTM_SCHEDULE entry %r, ignoring", chunk)
            continue
        out.append((hh, mm))
    if not out:
        logger.warning("No usable FTM_SCHEDULE entries; defaulting to 06:30")
        out = [(6, 30)]
    return sorted(set(out))


def _active_days() -> set[int]:
    """Parse FTM_SCHEDULE_DAYS into weekday numbers (Mon=0)."""
    raw = os.getenv("FTM_SCHEDULE_DAYS", "all").strip().lower()
    if raw in ("", "all", "daily", "*"):
        return set(range(7))
    days: set[int] = set()
    for chunk in raw.split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        for i, wd in enumerate(_WEEKDAYS):
            if wd.startswith(name):
                days.add(i)
                break
        else:
            logger.error("Unknown FTM_SCHEDULE_DAYS entry %r, ignoring", chunk)
    if not days:
        logger.warning("No usable FTM_SCHEDULE_DAYS; running every day")
        return set(range(7))
    return days


def next_fire(after: datetime | None = None) -> datetime:
    """Next scheduled datetime strictly after `after`, in business time."""
    tz = _tz()
    now = (after or datetime.now(tz)).astimezone(tz)
    slots = _slots()
    days = _active_days()
    # 8 days of lookahead covers any weekday subset.
    for day_offset in range(0, 9):
        day = (now + timedelta(days=day_offset)).date()
        if day.weekday() not in days:
            continue
        for hh, mm in slots:
            candidate = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if candidate > now:
                return candidate
    # Unreachable with a non-empty day set, but never spin.
    return now + timedelta(days=1)


def _runner_args() -> list[str]:
    return shlex.split(os.getenv("FTM_ARGS", "--commit"))


def run_once() -> int:
    """Fire one FTM run. Never raises: the scheduler must survive a bad run."""
    argv = _runner_args()
    logger.info("Firing FTM run: ftm_runner %s", " ".join(argv))
    started = time.time()
    try:
        code = ftm_runner.main(argv)
    except SystemExit as exc:            # argparse inside the runner
        code = int(exc.code or 0)
    except Exception:
        logger.exception("FTM run raised, scheduler continuing")
        code = 1
    logger.info("FTM run finished in %.0fs with exit code %d",
                time.time() - started, code)
    return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="FTM scheduler")
    p.add_argument("--once", action="store_true", help="Run immediately, then exit")
    p.add_argument("--next", action="store_true", help="Print the next 5 fire times and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    tz = _tz()
    logger.info(
        "Scheduler up. tz=%s slots=%s days=%s args=%r state=%s",
        config.BUSINESS_TIMEZONE,
        ",".join(f"{h:02d}:{m:02d}" for h, m in _slots()),
        ",".join(_WEEKDAYS[d][:3] for d in sorted(_active_days())),
        os.getenv("FTM_ARGS", "--commit"),
        config.STATE_DIR,
    )

    if args.next:
        when = datetime.now(tz)
        for _ in range(5):
            when = next_fire(when)
            print(f"  {when.isoformat(timespec='minutes')}  ({when.tzname()})")
        return 0

    if args.once:
        return run_once()

    if os.getenv("FTM_RUN_ON_BOOT", "").strip() in ("1", "true", "yes", "on"):
        logger.info("FTM_RUN_ON_BOOT set, firing immediately")
        run_once()

    jitter_max = int(os.getenv("FTM_JITTER_SECONDS", "0") or 0)

    while True:
        target = next_fire()
        wait = (target - datetime.now(tz)).total_seconds()
        logger.info("Next run at %s (in %.1f h)",
                    target.isoformat(timespec="minutes"), wait / 3600)
        # Sleep in chunks so a restart or clock change is picked up promptly and
        # a long sleep cannot swallow a DST shift.
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (target - datetime.now(tz)).total_seconds()

        if jitter_max > 0:
            jitter = random.uniform(0, jitter_max)
            logger.info("Jitter: waiting a further %.0fs", jitter)
            time.sleep(jitter)

        run_once()

        # Guard against firing twice inside the same minute if the run was
        # faster than the slot resolution.
        time.sleep(61)


if __name__ == "__main__":
    raise SystemExit(main())
