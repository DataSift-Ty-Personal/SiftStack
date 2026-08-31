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
import math
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
# EVERY SOURCE GETS A RESERVED SHARE OF THE DAY, which is the difference
# between this list and the one it replaces.
#
# The old loop walked sources in order and RETURNED at the cap, so anything
# after the first productive source was never even queried. That was survivable
# while every source was a Hottest stage. It is not survivable now: FTM is the
# largest and freshest book we have (604 records, 549 never contacted), and
# appended to a sequential list it would be starved by Adriana's 1,476 every
# single morning.
#
# Shares are of the daily cap. Pass one fills each source to its share; pass two
# hands the unspent remainder back out in priority order, so a thin source never
# wastes the day's capacity.
#
# `deep` means resolve the best phone from the FULL record rather than trusting
# the search row's representative one. Measured on FTM: 97% of records hold a
# Dial First/Second number, but 52% of the time it is not the one the search row
# returns. Only FTM pays that extra read.
@dataclass(frozen=True)
class Source:
    title: str
    share: float
    deep: bool = False


SOURCES = [
    Source("Hottest - 02 Ready to Call", 0.12),
    Source("Hottest - 03 Call Attempt 1", 0.01),
    Source("Hottest - 04 Call Attempt 2", 0.01),
    Source("Hottest - 05 Call Attempt 3", 0.01),
    Source(f"{config.HANDOFF_NAME} - Actively Prospecting", 0.25),
    Source("FTM - 02 Ready to Call", 0.55, deep=True),
    Source("FTM - 03 Call Attempt 1", 0.05, deep=True),
]

# Kept so anything still importing it keeps working; the shares live above.
STAGE_TOUCHES = [(s.title, 0) for s in SOURCES]


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
    skipped_duplicate_person: int = 0
    skipped_waiting: int = 0
    skipped_completed: int = 0
    hit_cap: bool = False
    # Why vetted candidates were held, bucketed. These used to be thrown away
    # the moment seed.build returned, which is why nobody could say why a
    # 1,577 record cohort produced 17 sends.
    holds: dict = field(default_factory=dict)
    unreached: list = field(default_factory=list)
    missing_presets: list = field(default_factory=list)


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

    # Fetch every source's cohort up front. This is search paging only, no
    # per-record reads, so it is cheap; the expensive part is vetting, and that
    # stays bounded by the quotas below.
    fetched: dict[str, list] = {}
    dnc_numbers: set = set()
    for src in SOURCES:
        stats: dict = {}
        rows, matched = seed.from_preset(src.title, keep_unresolved=src.deep, stats=stats)
        dnc_numbers |= stats.pop("_dnc_numbers", set())
        if not matched:
            # A source that does not resolve is a silent zero, and these are
            # referenced BY NAME, so a rename in DataSift breaks the day with
            # no other symptom. The scheduler turns this into an alert.
            plan.per_stage[src.title] = {"error": "preset not found"}
            plan.missing_presets.append(src.title)
            continue
        fetched[src.title] = rows
        plan.per_stage[src.title] = {
            "cohort": stats.get("cohort", len(rows)),
            "rows": len(rows),
            "ready": 0,
            "reached": False,
            "quota": 0,
            "drops": {k: v for k, v in stats.items() if k not in ("cohort", "kept")},
            "holds": {},
        }

    def take(src: Source, quota: int) -> int:
        """Vet this source's rows until `quota` candidates are ready."""
        if quota <= 0 or src.title not in fetched:
            return 0
        stage = plan.per_stage[src.title]
        stage["reached"] = True
        stage["quota"] += quota
        taken = 0
        rows = fetched[src.title]
        while rows and taken < quota:
            if limit and len(plan.candidates) >= limit:
                plan.hit_cap = True
                break
            row = rows.pop(0)
            phone = store.clean_phone(row.get("phone"))
            if phone and phone in seen_phone:
                plan.skipped_duplicate_person += 1
                continue

            # Deep sources: the search row's phone is often not the record's
            # best. Resolve now, at the moment we would actually use the row,
            # so an unused row never costs a record fetch.
            if row.get("_needs_best_phone"):
                resolved, why = seed.resolve_best_phone(row, dnc_numbers)
                if not resolved:
                    stage["holds"][why] = stage["holds"].get(why, 0) + 1
                    plan.holds[why] = plan.holds.get(why, 0) + 1
                    continue
                row = resolved
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
                # Every hold reason used to be discarded right here.
                for reason in (cand.reasons if cand else ["seed produced nothing"]):
                    key = seed.reason_key(reason)
                    stage["holds"][key] = stage["holds"].get(key, 0) + 1
                    plan.holds[key] = plan.holds.get(key, 0) + 1
                continue

            seen_phone.add(phone)
            cand.touch = touch  # type: ignore[attr-defined]
            plan.per_touch[touch] = plan.per_touch.get(touch, 0) + 1
            plan.candidates.append(cand)
            stage["ready"] += 1
            taken += 1
        return taken

    cap = limit or 0
    if cap:
        # Pass one: each source fills its own reserved share, so a big source
        # cannot eat a small one's allocation.
        for src in SOURCES:
            take(src, math.ceil(cap * src.share))
        # Pass two: hand the remainder back out in priority order.
        for src in SOURCES:
            if len(plan.candidates) >= cap:
                break
            take(src, cap - len(plan.candidates))
    else:
        for src in SOURCES:
            take(src, len(fetched.get(src.title, [])))

    plan.unreached = [s.title for s in SOURCES
                      if s.title in fetched and fetched[s.title]
                      and not plan.per_stage[s.title]["ready"]]
    return plan


def summary(plan: Plan) -> str:
    lines = [f"{'source':34} {'cohort':>7} {'rows':>6} {'quota':>6} {'send':>5}"]
    lines.append("-" * 62)
    for title, info in plan.per_stage.items():
        if "error" in info:
            lines.append(f"{title:34} {info['error']}")
            continue
        lines.append(f"{title:34} {info.get('cohort', 0):>7} {info.get('rows', 0):>6} "
                     f"{info.get('quota', 0):>6} {info.get('ready', 0):>5}"
                     + ("" if info.get("reached") else "   (not reached)"))
        # The drop from cohort to rows, which used to be invisible entirely.
        for reason, n in sorted(info.get("drops", {}).items(), key=lambda kv: -kv[1])[:4]:
            lines.append(f"     -{reason:44} {n:>5}")
        for reason, n in sorted(info.get("holds", {}).items(), key=lambda kv: -kv[1])[:4]:
            lines.append(f"     held: {reason:40} {n:>5}")
    lines.append("-" * 62)
    lines.append(f"{'TOTAL':34} {'':>7} {'':>6} {'':>6} {len(plan.candidates):>5}")
    lines.append("")
    by_touch = ", ".join(f"touch {t}: {n}" for t, n in sorted(plan.per_touch.items()))
    lines.append(f"sending                         : {by_touch or 'nothing'}")
    lines.append(f"waiting out the {config.TOUCH_GAP_DAYS}-day gap        : {plan.skipped_waiting}")
    lines.append(f"finished all four touches       : {plan.skipped_completed}")
    lines.append(f"same person in 2 stages         : {plan.skipped_duplicate_person}")
    if plan.holds:
        top = sorted(plan.holds.items(), key=lambda kv: -kv[1])[:6]
        lines.append("held overall                    : "
                     + ", ".join(f"{k} {n}" for k, n in top))
    if plan.missing_presets:
        lines.append("PRESETS NOT FOUND               : " + ", ".join(plan.missing_presets))
    cap = sender_pool.capacity_today()
    lines.append(f"pool capacity today             : {cap['remaining']} of {cap['capacity']}")
    return "\n".join(lines)
