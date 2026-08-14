"""The daily campaign run: build a batch each morning and let it send.

Until this existed, a day's texts went out because a person built the cohort,
re-laid the schedule and released it by hand. The box received replies and
dispositioned them perfectly overnight, and sent nothing. That is the gap
between a thing you operate and a thing that operates.

Three decisions worth stating, because each one is a guard:

  * **The window is ours, not theirs.** 9am to 6pm Eastern is when the team
    works, so a reply lands while somebody can act on it. The recipient's own
    8am-9pm quiet hours still apply underneath, and a send needs BOTH. A
    California owner is not texted at 6am because Knoxville is open.

  * **One build per day, spread across the whole window.** The batch is laid
    out end to end at 60-180s spacing rather than released in blocks, because
    a burst is what a carrier notices.

  * **It cannot run away.** A cap bounds the day, the touch-dedup means nobody
    is re-sent a message they already got, and every send still passes the
    per-number cap, the sticky sender, suppression and the dial-tier filter.
    If the cohort ever came back wrong, the blast radius is one day's cap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import campaign, config, escalate, seed, sender_pool, store

log = logging.getLogger(__name__)

try:  # stdlib on 3.9+, and the image has tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STATE_KIND = "campaign_run"


def _now_local() -> datetime:
    if ZoneInfo is None:
        return datetime.now(timezone.utc)
    return datetime.now(ZoneInfo(config.CAMPAIGN_TZ))


def within_window(now: datetime = None) -> tuple[bool, str]:
    """Is it a working moment for the team? (ok, why not)"""
    now = now or _now_local()
    days = {int(d) for d in config.CAMPAIGN_DAYS.split(",") if d.strip().isdigit()}
    if days and now.weekday() not in days:
        return False, f"{now:%A} is not a campaign day"
    if now.hour < config.CAMPAIGN_START_HOUR:
        return False, f"before {config.CAMPAIGN_START_HOUR}:00 {config.CAMPAIGN_TZ}"
    if now.hour >= config.CAMPAIGN_END_HOUR:
        return False, f"after {config.CAMPAIGN_END_HOUR}:00 {config.CAMPAIGN_TZ}"
    return True, ""


def already_ran_today() -> bool:
    today = _now_local().strftime("%Y-%m-%d")
    return not store.mark_notified(f"campaign-{today}", STATE_KIND)


def run(force: bool = False, dry_run: bool = False) -> dict:
    """Build and release one day's batch. Safe to call every worker pass."""
    if not config.CAMPAIGN_ENABLED and not force:
        return {"skipped": "campaign scheduler disabled"}

    ok, why = within_window()
    if not ok and not force:
        return {"skipped": why}

    if not dry_run and already_ran_today():
        return {"skipped": "already ran today"}

    cap = config.CAMPAIGN_DAILY_CAP or sender_pool.capacity_today()["remaining"]
    # Build only as far as the cap. The cohort is larger than a day's pool,
    # so vetting all of it every morning would spend minutes of CRM reads on
    # candidates the cap then throws away.
    plan = campaign.build(limit=cap)
    ready = plan.candidates[:cap]
    dropped_for_cap = len(plan.candidates) - len(ready)

    if not ready:
        # Say so out loud. A quiet morning and a broken cohort query look
        # identical from the outside, and only one of them is fine.
        escalate.alert(
            "SMS campaign: nothing to send today",
            f"Cohort produced no eligible records.\n"
            f"Waiting out the {config.TOUCH_GAP_DAYS}-day gap: {plan.skipped_waiting}. "
            f"Finished all four touches: {plan.skipped_completed}.",
            kind="campaign",
        )
        return {"queued": 0, "reason": "no eligible candidates"}

    if dry_run:
        return {
            "would_queue": len(ready),
            "dropped_for_cap": dropped_for_cap,
            "per_stage": plan.per_stage,
        }

    # Queued per touch (that is the seed API), then re-laid as ONE timeline.
    # Queuing four batches independently gave four schedules all starting at
    # "now", which collapsed to a 2-second gap: a burst wearing a schedule.
    queued = 0
    for touch in (1, 2, 3, 4):
        batch = [c for c in ready if getattr(c, "touch", None) == touch]
        if batch:
            queued += seed.queue(batch, touch).get("queued", 0)
    laid = seed.reschedule_held()
    released = 0
    for touch in (1, 2, 3, 4):
        released += seed.release(touch)
    result = {"queued": queued}

    summary = {
        "queued": result.get("queued", 0),
        "released": released,
        "dropped_for_cap": dropped_for_cap,
        "waiting_gap": plan.skipped_waiting,
        "completed": plan.skipped_completed,
        "duplicate_person": plan.skipped_duplicate_person,
        "window": f"{laid.get('first')} - {laid.get('last')} UTC",
        "avg_gap_seconds": laid.get("avg_gap_seconds"),
    }
    log.info("campaign run: %s", summary)

    stages = "\n".join(
        f"  {title}: touch {info.get('touch')}, {info.get('ready', 0)} sending"
        for title, info in plan.per_stage.items() if "error" not in info
    )
    escalate.alert(
        f"SMS campaign started: {released} texts today",
        f"{stages}\n\n"
        f"Spread over {laid.get('span_minutes')} minutes, "
        f"{laid.get('avg_gap_seconds')}s average gap, "
        f"{sender_pool.capacity_today()['numbers']} numbers.\n"
        f"{plan.skipped_waiting} waiting out the {config.TOUCH_GAP_DAYS}-day gap, "
        f"{plan.skipped_completed} have finished all four"
        + (f", {dropped_for_cap} held back by the daily cap." if dropped_for_cap else "."),
        kind="campaign",
    )
    return summary


# ----------------------------------------------------------- heartbeat

def heartbeat() -> dict:
    """Record that the worker is alive. Read by an external checker.

    Deliberately just a timestamp. Anything clever that runs INSIDE the worker
    cannot report that the worker has stopped, which is the failure this is for:
    the box went down when the Fly trial ended and nothing said a word.
    """
    store.set_meta("last_worker_pass", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {"ok": True}


def liveness() -> dict:
    """How long since the worker last did a pass."""
    raw = store.get_meta("last_worker_pass")
    if not raw:
        return {"alive": False, "reason": "no worker pass ever recorded"}
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return {"alive": False, "reason": f"unparseable timestamp {raw!r}"}
    age = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return {
        "alive": age <= config.HEARTBEAT_STALE_MINUTES,
        "last_pass": raw,
        "minutes_ago": round(age, 1),
        "stale_after_minutes": config.HEARTBEAT_STALE_MINUTES,
    }
