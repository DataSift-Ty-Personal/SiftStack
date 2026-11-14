"""Coaching picks (build plan 6.7): the two best and two worst calls per caller, chosen from the log.

The teaching's review rhythm is small and strict: each week the two best and the two worst calls of
every caller, one thing to fix from each; every day during a new hire's first week; and the notes go
to the owner first, who touches them up before a caller sees them. What was missing was the
choosing. The coaching workflow pulled recordings from one dialer's own call list, so a call made
through the other dialer was never reviewed, and nobody picked which four calls were worth the time.

This stage does the choosing and nothing else. It reads the shared log copy (a call counts
whichever dialer made it, and the recording link comes with it), picks, keeps the picks, and queues
one review pack for the call coaching workflow. It does not listen, transcribe or grade: that is
the workflow's job and the workflow's spend.

HOW A CALL IS PICKED, from what a log can say without hearing the call (its length, and what
happened to the owner afterwards):

    best    a conversation after which the owner became a lead; then one that confirmed the number
            as the owner's; then the longest conversations
    worst   a conversation after which the owner said no (Not Interested, do-not-call) or the number
            was marked wrong; then the shortest conversations

Only a conversation as the catalog defines it (an answered call of a minute or more) with a
recording can be picked: a voicemail greeting is not a call to coach. Each pick carries its reason
in plain words, so the reviewer knows why it is in the pack.

WHEN. Once a week, on `weekly_on`, over the last seven days. Every calling day for a new hire, over
that day, until the catalog's new-hire days are up. A new hire is a caller named under `new_hires`
with a start day, or one whose first captured day falls inside those days when the daily capture
itself reaches back well before it (a young capture makes everyone look new, so it proves nothing).

A pack is picked once: the picks are kept under their period, and a second run that day finds them
and stays quiet. A weekly day that was missed is caught up on the next run. Queueing is the only thing that needs a live box. A plan run keeps the picks and
says what it would queue; the first live run of the same period queues them.

The recording link is never put in a channel message: it opens without a login.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from .. import store
from . import activity, capture, gate, kpi, lanes, ledger
from .stages import Ctx

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_coach_picks (
    period TEXT, caller TEXT, call_id TEXT, kind TEXT, at TEXT, seconds INTEGER, recording TEXT,
    why TEXT, queued INTEGER DEFAULT 0, picked_at TEXT, PRIMARY KEY (period, caller, call_id));
"""
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NO_STATUSES = {"not_interested", "dnc", "opt_out", "dead lead"}
USES = ("coaching.best_calls", "coaching.worst_calls", "coaching.new_hire_days")


# ---------------------------------------------------------------------- the picks on record
def picks(period: str = "", unqueued: bool = False) -> list[dict]:
    kpi.ensure("ap_coach_picks", SCHEMA)
    q, args = "SELECT * FROM ap_coach_picks WHERE 1=1", []
    if period:
        q += " AND period=?"
        args.append(period)
    if unqueued:
        q += " AND queued=0"
    with store._LOCK, store._db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY period, caller, kind, at", args).fetchall()]


def _keep(period: str, caller: str, chosen: list[dict]) -> None:
    kpi.ensure("ap_coach_picks", SCHEMA)
    with store._LOCK, store._db() as c:
        for p in chosen:
            c.execute("INSERT OR IGNORE INTO ap_coach_picks (period, caller, call_id, kind, at, seconds, recording, why, "
                      "queued, picked_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
                      (period, caller, p["call"], p["kind"], p["when"].isoformat(timespec="seconds"), p["seconds"],
                       p["recording"], p["why"], store.now()))


def _mark_queued(period: str) -> None:
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_coach_picks SET queued=1 WHERE period=?", (period,))


def last_weekly() -> date | None:
    """The day the last weekly pack was picked, or None when there has never been one."""
    kpi.ensure("ap_coach_picks", SCHEMA)
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT MAX(period) AS p FROM ap_coach_picks WHERE period LIKE 'week:%'").fetchone()
    try:
        return date.fromisoformat(str(row["p"])[5:]) if row and row["p"] else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- what happened after a call
def outcome(ev: dict, hours: float) -> tuple[str, str]:
    """("lead" | "confirmed" | "no" | "", why in plain words) for one conversation, from what the log
    shows in the hours after it. The owner's records come from the nightly owner-lane map, so this
    costs no read; an owner the map does not hold is judged on the phone marks alone."""
    start, end = ev["when"] - timedelta(minutes=1), ev["when"] + timedelta(hours=hours)
    held = lanes.owner_lane(ev["owner"]) or {}
    lead = no = False
    for rec in (held.get("records") or {}):
        for e in activity.events("property.status.updated", resource=rec, since=start, until=end):
            to = str(e["data"].get("to") or "").strip().lower()
            frm = str(e["data"].get("from") or "").strip().lower()
            if to in lanes.LEAD_STATUSES and frm not in lanes.LEAD_STATUSES:
                lead = True
            elif to in NO_STATUSES:
                no = True
    marks = [str(e["data"].get("to") or "").upper()
             for e in activity.events("owner.phone.status.updated", resource=ev["owner"], since=start, until=end)
             if not ev["number"] or e["data"].get("number") == ev["number"]]
    if lead:
        return "lead", "the owner became a lead within the day"
    if no:
        return "no", "the owner said no within the day"
    if any(m in gate.BAD_PHONE for m in marks):
        return "no", "the number was marked wrong or do-not-call after it"
    if "CORRECT" in marks:
        return "confirmed", "it confirmed the number as the owner's"
    return "", ""


def _length(seconds: int) -> str:
    return "%d min" % max(1, round(seconds / 60.0))


def choose(calls: list[dict], best_n: int, worst_n: int, hours: float, span: str) -> list[dict]:
    """The best and worst of one caller's conversations. A call is never picked as both, and with
    fewer conversations than the pack holds the best are filled first."""
    judged = []
    for ev in calls:
        what, why = outcome(ev, hours)
        judged.append(dict(ev, outcome=what, reason=why))
    rank = {"lead": 2, "confirmed": 1, "": 0, "no": -1}
    best = sorted([c for c in judged if c["outcome"] != "no"], key=lambda c: (-rank[c["outcome"]], -c["seconds"], c["when"]))[:best_n]
    taken = {c["call"] for c in best}
    rest = [c for c in judged if c["call"] not in taken and c["outcome"] != "lead"]
    worst = sorted(rest, key=lambda c: (0 if c["outcome"] == "no" else 1, c["seconds"], c["when"]))[:worst_n]
    out = []
    for c in best:
        out.append(dict(c, kind="best", why=c["reason"] or "one of the longest conversations of the %s" % span))
    for c in worst:
        out.append(dict(c, kind="worst", why=c["reason"] or "one of the shortest conversations of the %s" % span))
    return out


# ---------------------------------------------------------------------- who is new
def new_hires(ctx: Ctx, cat: kpi.Catalog, today: date) -> set[str]:
    """Callers still in their first days. Named in the playbook with a start day, or seen for the
    first time in a capture that reaches back far enough to know they were not there before."""
    days = int(cat.value("coaching.new_hire_days"))
    out = set()
    for name, start in (ctx.stage.get("new_hires") or {}).items():
        try:
            if 0 <= (today - date.fromisoformat(str(start))).days < days:
                out.add(str(name))
        except ValueError:
            ctx.out.problem("new_hires: %s has start day %r, which is not a day written YYYY-MM-DD" % (name, start))
    # The capture keeps one small row per caller per day, so a caller's first day is read from it
    # without walking months of call events.
    first: dict[str, str] = {}
    for r in capture.rows():
        if r["caller"] not in ("unknown", kpi.UNASSIGNED) and r["dials"] and r["caller"] not in first:
            first[r["caller"]] = r["day"]
    if not first:
        return out
    began = date.fromisoformat(min(first.values()))
    look_back = int(ctx.stage.get("new_hire_lookback_days", 30))
    for who, day in first.items():
        start = date.fromisoformat(day)
        if 0 <= (today - start).days < days and (start - began).days >= look_back:
            out.add(who)
    return out


# ---------------------------------------------------------------------- the queue
def wired(workflow: str) -> bool:
    """Whether the coaching workflow has a handler. Unwired, a queued task would become a model turn
    that can only answer that the work is done by hand, so nothing is queued and the stage says so."""
    try:
        from .. import workflows
        return workflows.handler(workflow) is not None
    except Exception:  # noqa: BLE001
        return False


def queue(ctx: Ctx, period: str, label: str) -> int:
    """Queue one review pack holding every pick of the period that is not queued yet. Returns the
    number of calls in it (0 when nothing was queued)."""
    st = ctx.stage
    todo = picks(period, unqueued=True)
    if not todo:
        return 0
    workflow, agent = str(st.get("workflow") or "call_coaching"), str(st.get("agent") or "ops")
    if not (wired(workflow) or st.get("queue_unwired")):
        ctx.out.tally["picks kept and not queued: the %s workflow has no handler yet" % workflow.replace("_", " ")] = len(todo)
        return 0
    calls = [{"caller": p["caller"], "kind": p["kind"], "call": p["call_id"], "at": p["at"], "seconds": p["seconds"],
              "recording": p["recording"], "why": p["why"]} for p in todo]
    payload = {"details": "Review pack for %s: the best and worst calls of each caller, picked from the call log. "
                          "Coach one thing to fix or one thing to reinforce from each. The notes go to the owner first." % label,
               "inputs": {"period": period, "notes_to": "owner", "calls": calls}}
    enqueue = ctx.enqueue or store.enqueue
    ctx.write("queue call coaching", enqueue, agent, workflow, "", "Call review, %s" % label, force=True,
              payload=payload, source="autopilot:%s" % st.id)
    if ctx.live:
        _mark_queued(period)
        ledger.event(ctx.run_id, st.id, "", "coach_queue", {"period": period, "calls": len(calls)}, now=ctx.now, tz=ctx.policy.tz)
    return len(calls)


# ---------------------------------------------------------------------- the stage
def _clock(when: datetime, tz: str) -> str:
    local = when.astimezone(kpi.zone(tz))
    return "%s %s" % (local.strftime("%a"), local.strftime("%I:%M%p").lstrip("0").lower())


def run_coachpick(ctx: Ctx) -> None:
    st = ctx.stage
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    tz = ctx.policy.tz
    kpi.sync_log(ctx, ("calls", "state"))
    local_now = ctx.now.astimezone(kpi.zone(tz))
    today = local_now.date()
    weekly_on = str(st.get("weekly_on") or "Fri").lower()[:3]
    if weekly_on not in DAYS:
        ctx.out.problem("weekly_on %r is not a day (use Mon to Sun)" % st.get("weekly_on"))
        weekly_on = "fri"
    # The weekly pack is due on its day. A week whose day was missed (the box was down, a holiday)
    # is caught up on the next run, so one missed evening does not cost a whole week's review.
    earlier = last_weekly()
    is_weekly = today.weekday() == DAYS.index(weekly_on) or (earlier is not None and (today - earlier).days > 7)
    rookies = new_hires(ctx, cat, today)
    best_n, worst_n = int(cat.value("coaching.best_calls")), int(cat.value("coaching.worst_calls"))
    hours = float(st.get("outcome_hours", 24))

    week = kpi.conversation_events(cat, since=ctx.now - timedelta(days=7), until=ctx.now)
    by_caller: dict[str, list] = defaultdict(list)
    no_recording = no_name = 0
    for ev in week:
        if ev["kind"] != "call":
            continue
        if not ev["caller"]:
            no_name += 1
        elif not ev["recording"]:
            no_recording += 1
        else:
            by_caller[ev["caller"]].append(ev)
    if no_recording:
        ctx.out.tally["conversations with no recording in the log (cannot be reviewed)"] = no_recording
    if no_name:
        ctx.out.tally["conversations the log ties to no caller"] = no_name
    ctx.out.found = sum(len(v) for v in by_caller.values())

    day_start = kpi.day_start(today, tz)
    periods: dict[str, str] = {}           # period -> how the pack is named
    picked = callers = 0
    for who, calls in sorted(by_caller.items()):
        if who in rookies:
            period, label, span = "day:%s:%s" % (today.isoformat(), who), "%s on %s (a new hire: reviewed every day)" % (who, kpi.fmt_day(today)), "day"
            calls = [c for c in calls if c["when"] >= day_start]
        elif is_weekly:
            period, label, span = "week:%s" % today.isoformat(), "the week ending %s" % kpi.fmt_day(today), "week"
        else:
            continue
        mine = [p for p in picks(period) if p["caller"] == who]
        if not mine:
            chosen = choose(calls, best_n, worst_n, hours, span)
            if not chosen:
                continue
            _keep(period, who, chosen)
            picked += len(chosen)
            callers += 1
            parts = []
            for kind in ("best", "worst"):
                got = ["%s (%s, %s)" % (_clock(c["when"], tz), _length(c["seconds"]), c["why"]) for c in chosen if c["kind"] == kind]
                if got:
                    parts.append("%s %s" % (kind, " and ".join(got)))
            ctx.out.line("%s: %s." % (who, "; ".join(parts)))
            if len(calls) < best_n + worst_n:
                ctx.out.tally["callers with fewer conversations than a full pack"] += 1
        periods[period] = label
    queued = 0
    for period, label in periods.items():
        queued += queue(ctx, period, label)
    ctx.out.acted = picked
    if picked or queued:
        ctx.out.line("Picked by what the log shows (how long the call ran and what happened to the owner afterwards), "
                     "not by a grade. The notes go to the owner first.")
        ctx.out.line(cat.footnote(USES))
    verb = "queued" if ctx.live else "would queue"
    if picked:
        ctx.out.head = "Call review: %d call(s) picked for %d caller(s)%s." % (
            picked, callers, (", %s %d for call coaching" % (verb, queued)) if queued else ", kept for call coaching")
    elif queued:
        ctx.out.head = "Call review: %s %d call(s) picked earlier for call coaching." % (verb, queued)
        ctx.out.acted = queued if ctx.live else 0
    elif not is_weekly and not rookies:
        ctx.out.head = "No pick is due today: the weekly pack is picked on %s and nobody is in a first week." % weekly_on.capitalize()
    elif not by_caller:
        ctx.out.head = "Nothing to pick: no conversation of a minute or more with a recording in the last 7 days."
    else:
        ctx.out.head = "The picks for this period are already made."
