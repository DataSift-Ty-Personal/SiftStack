"""The daily follow-up: one text per person per day, next in their sequence.

Everybody in the prospector's assigned book walks the same four touches, one a
day, until the sequence is done: identity check, resend, soft ask, goodbye.

Which touch someone gets comes from THEIR OWN text history, not from where the
record sits in the calling cadence. The first build mapped each call-attempt
stage to a fixed touch, which quietly capped almost everyone at touch 1: a
record parked in Ready to Call never advances a stage on its own, so it never
earned touch 2 and the campaign looked exhausted after a single day.

Two things this has to get right, and both are about not annoying people:

  * **Never resend a touch someone already had.** Adriana and Tinaa have been
    sending these same four touches BY HAND out of the smrtPhone inbox, and
    1,600+ numbers already have one. Sending touch 1 to somebody who got it
    last week from a different number is the fastest way to look like a spam
    farm to a human, which matters more than looking like one to a carrier.
  * **One person, one text per run**, even when they own property in two
    different cadence stages.

Prior sends are read from BOTH our own outbox history and the smrtPhone SMS
log, because the manual program is invisible to our database.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from . import config, crm, respond, seed, sender_pool, store
from .knowledge import touches

log = logging.getLogger(__name__)

# Who is in the daily follow-up: records that are ASSIGNED and in prospecting
# (Ty, 2026-08-12). A text belongs to somebody who is going to call, so the
# cohort is the prospector's own book, not the whole database.
#
# Order is priority, because the daily cap cuts the tail. The four Hottest
# call-attempt stages come first as the best leads, then the rest of the
# assigned prospecting book. Everyone is deduped by phone across all of it, so
# appearing in two sources is one text, not two.
#
# All four call stages, not just Ready to Call: a record sitting on attempt 2 is
# still owed the rest of its text sequence, and the text is what warms the next
# dial. Where they are in the CALL cadence decides nothing about which text they
# get; that comes from their own text history in next_touch().
FAMILIES = ["Hottest"]
STAGES = [
    "02 Ready to Call",
    "03 Call Attempt 1",
    "04 Call Attempt 2",
    "05 Call Attempt 3",
]
PROSPECTING_PRESETS = [f"{config.HANDOFF_NAME} - Actively Prospecting"]

STAGE_TOUCHES = (
    [(f"{fam} - {stage}", 0) for fam in FAMILIES for stage in STAGES]
    + [(p, 0) for p in PROSPECTING_PRESETS]
)


def _fingerprints() -> dict[int, list[str]]:
    """A distinctive literal snippet from each variant, per touch.

    Merge fields are stripped, so what remains is the fixed wording that
    identifies which touch a historical message came from.
    """
    out: dict[int, list[str]] = {}
    for touch, (pool, noname) in enumerate(touches.POOLS, start=1):
        marks = []
        for template in list(pool) + list(noname):
            # Longest literal run between merge fields, lowercased.
            literals = [p.strip() for p in re.split(r"\{[a-z]+\}", template)]
            best = max(literals, key=len)
            if len(best) >= 18:
                marks.append(re.sub(r"\s+", " ", best.lower())[:60])
        out[touch] = marks
    return out


_FP = _fingerprints()


def identify_touch(body: str) -> Optional[int]:
    """Which touch (1-4) an already-sent message came from, if any."""
    text = re.sub(r"\s+", " ", (body or "").lower())
    if not text:
        return None
    for touch, marks in _FP.items():
        for mark in marks:
            if mark and mark in text:
                return touch
    return None


def prior_touches(sms_log_rows: Optional[list] = None) -> dict[str, dict]:
    """phone -> {"touches": {n}, "last": iso date}, ours AND smrtPhone's log.

    The date matters as much as the set. Progression is per PERSON on a clock,
    so the question at build time is not only "which touches has this number
    had" but "was the last one long enough ago to send the next".
    """
    history: dict[str, dict] = {}

    def note(phone: str, touch: int, when: str) -> None:
        if not phone or not touch:
            return
        entry = history.setdefault(phone, {"touches": set(), "last": ""})
        entry["touches"].add(touch)
        if when and when > entry["last"]:
            entry["last"] = when

    for row in store._conn().execute(
        "SELECT phone, body, created_at FROM messages WHERE direction='out'"
    ):
        note(row["phone"], identify_touch(row["body"]), str(row["created_at"] or ""))

    from . import reconcile

    for row in sms_log_rows or []:
        if (row.get("direction") or "").lower() != "outbound":
            continue
        note(
            store.clean_phone(row.get("toNum")),
            identify_touch(reconcile._clean(row.get("content"))),
            str(row.get("date") or row.get("createdAt") or ""),
        )
    return history


def next_touch(entry: Optional[dict], min_days: int, today: date) -> tuple[Optional[int], str]:
    """Which touch this person is due, and why not if they are not.

    Every owner walks the same four touches in order (Ty, 2026-08-11): the
    identity check, the resend, the soft ask, the goodbye. Traction comes from
    completing that sequence from ONE number, not from a single well-aimed text.

    The first build tied each touch to a CRM call-attempt stage, which quietly
    capped most people at touch 1: a record sitting in Ready to Call never
    advances a stage on its own, so it never earned touch 2 and the campaign
    looked exhausted after a day. Progression is now the person's own history.
    """
    touches_had = (entry or {}).get("touches") or set()
    if not touches_had:
        return 1, ""

    done = max(touches_had)
    if done >= 4:
        return None, "completed all four touches"

    last = (entry or {}).get("last") or ""
    if last:
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00")).date()
        except ValueError:
            when = None
        if when and (today - when).days < min_days:
            waited = (today - when).days
            return None, f"touch {done} was {waited}d ago, waiting {min_days}d"
    return done + 1, ""


@dataclass
class Plan:
    candidates: list = field(default_factory=list)
    per_stage: dict = field(default_factory=dict)
    per_touch: dict = field(default_factory=dict)
    skipped_already_touched: int = 0
    skipped_duplicate_person: int = 0
    skipped_waiting: int = 0
    skipped_completed: int = 0
    hit_cap: bool = False


def build(sender_fallback: str = "", log_pages: int = 6,
          min_days: Optional[int] = None, today: Optional[date] = None,
          limit: int = 0) -> Plan:
    """Assemble one run across every cadence stage. Sends nothing.

    Every eligible person is advanced to the NEXT touch they have not had,
    spaced by whole days. A stage is only a source of people now, not the thing
    that decides which text they get.
    """
    from . import reconcile

    min_days = config.TOUCH_GAP_DAYS if min_days is None else min_days
    today = today or datetime.now(timezone.utc).date()

    try:
        sms_rows = reconcile.fetch_log(pages=log_pages)
        log.info("read %s rows of smrtPhone SMS history", len(sms_rows))
    except Exception as exc:  # noqa: BLE001 - fall back to our own history only
        log.warning("could not read the SMS log (%s); using our history only", exc)
        sms_rows = []

    history = prior_touches(sms_rows)
    plan = Plan()
    seen_phone: set = set()

    for title, _stage_touch in STAGE_TOUCHES:
        rows, matched = seed.from_preset(title)
        if not matched:
            plan.per_stage[title] = {"error": "preset not found"}
            continue

        stage_ready = 0
        for row in rows:
            phone = store.clean_phone(row.get("phone"))
            if phone in seen_phone:
                plan.skipped_duplicate_person += 1
                continue

            touch, why = next_touch(history.get(phone), min_days, today)
            if touch is None:
                if "completed" in why:
                    plan.skipped_completed += 1
                else:
                    plan.skipped_waiting += 1
                continue

            built = seed.build([row], touch=touch, sender_fallback=sender_fallback)
            cand = built[0] if built else None
            if not cand or cand.status != "ready":
                continue

            seen_phone.add(phone)
            cand.touch = touch  # type: ignore[attr-defined]
            plan.per_touch[touch] = plan.per_touch.get(touch, 0) + 1
            plan.candidates.append(cand)
            stage_ready += 1

            # Stop as soon as the day is full. Vetting a candidate costs a CRM
            # read, and the scheduler runs inside the worker pass, so building
            # the whole book every morning would block reply processing for
            # minutes to then discard most of it at the cap. Sources are in
            # priority order, so stopping early keeps the best leads.
            if limit and len(plan.candidates) >= limit:
                plan.per_stage[title] = {
                    "mobile_records": len(rows), "ready": stage_ready, "stopped_at_cap": True,
                }
                plan.hit_cap = True
                return plan

        plan.per_stage[title] = {"mobile_records": len(rows), "ready": stage_ready}
    return plan


def summary(plan: Plan) -> str:
    lines = [f"{'stage':32} {'mobile':>7} {'to send':>8}"]
    lines.append("-" * 50)
    for title, info in plan.per_stage.items():
        if "error" in info:
            lines.append(f"{title:32} {info['error']}")
            continue
        lines.append(f"{title:32} {info['mobile_records']:>7} {info['ready']:>8}")
    lines.append("-" * 50)
    lines.append(f"{'TOTAL':32} {'':>7} {len(plan.candidates):>8}")
    lines.append("")
    by_touch = ", ".join(f"touch {t}: {n}" for t, n in sorted(plan.per_touch.items()))
    lines.append(f"sending                         : {by_touch or 'nothing'}")
    lines.append(f"waiting out the {config.TOUCH_GAP_DAYS}-day gap        : {plan.skipped_waiting}")
    lines.append(f"finished all four touches       : {plan.skipped_completed}")
    lines.append(f"same person in 2 stages         : {plan.skipped_duplicate_person}")
    cap = sender_pool.capacity_today()
    lines.append(f"pool capacity today             : {cap['remaining']} of {cap['capacity']}")
    return "\n".join(lines)
