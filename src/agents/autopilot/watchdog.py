"""The sequence watchdog (build plan 0.12): did the sequences actually fire?

DataSift sequences fail quietly. On 2026-11-12 the "Recently Sold to Sold Status" sequence turned
out to have missed about half of the newest tags, and nobody knew, because a sequence that does not
fire leaves no error anywhere. This stage is the check: for every trigger the log recorded in the
last day, was the result the sequence should have produced there?

The playbook lists the expectations, one per sequence:

    expect:
      - name: Recently Sold to Sold Status
        when: {event: property.tags.added, tag: recently sold}
        then: {status: sold}
      - name: Call New Lead
        when: {event: property.status.updated, to: new_lead}
        then: {assigned: true, task: true, card: true}

`when` matches a logged event (a tag added, a status reached). `then` is checked in the same log:
a later status change, a card created, a task created, an assignment, all for that record. A
status that the log does not show is read off the record itself before it is called a miss,
because a record that already had the status logs no change.

It reads only, so plan and live do the same thing, and it speaks only when something was missed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import activity
from .stages import Ctx, addr, status_of

log = logging.getLogger(__name__)


def _matches(e: dict, when: dict) -> bool:
    if e["event_type"] != when.get("event"):
        return False
    d = e["data"]
    if when.get("tag") and str(when["tag"]).lower() not in {str(t).lower() for t in d.get("tags") or []}:
        return False
    if when.get("to") and str(when["to"]).lower() != str(d.get("to") or "").lower():
        return False
    if when.get("column") and str(when["column"]).lower() != str(d.get("column") or "").lower():
        return False
    if when.get("by") and str(when["by"]).lower() != str(e.get("source") or "").lower():
        return False
    return True


def _produced(ctx: Ctx, e: dict, then: dict, until: datetime, state: dict) -> list[str]:
    """What the trigger did NOT produce, as plain words. Empty means the sequence fired."""
    u, after = e["resource"], e["when"] - timedelta(seconds=5)
    missing = []
    if then.get("status"):
        want = str(then["status"]).lower()
        hit = [x for x in activity.events("property.status.updated", resource=u, since=after, until=until)
               if str(x["data"].get("to") or "").lower() == want]
        if not hit:
            rec = state.setdefault("recs", {}).get(u)
            if rec is None:
                rec = ctx.crm.record(u)
                state["recs"][u] = rec
            if status_of(rec) != want:
                missing.append("status %s" % then["status"])
    if then.get("card") and not activity.events("card.created", resource=u, since=after, until=until) \
            and not activity.events("card.moved", resource=u, since=after, until=until):
        missing.append("a card")
    if then.get("task") and not activity.events("task.created", resource=u, since=after, until=until):
        missing.append("a task")
    if then.get("assigned") and not activity.events("property.assigned", resource=u, since=after, until=until):
        rec = state.setdefault("recs", {}).get(u)
        if rec is None:
            rec = ctx.crm.record(u)
            state["recs"][u] = rec
        if not rec.get("assigned_to"):
            missing.append("an assignee")
    return missing


def run_watchdog(ctx: Ctx) -> None:
    st = ctx.stage
    window = timedelta(hours=float(st.get("window_hours", 24)))
    settle = timedelta(minutes=float(st.get("settle_minutes", 30)))
    since, until = ctx.now - window - settle, ctx.now - settle
    for group in ("state", "board"):
        sync = activity.sync(ctx.crm, group, now=ctx.now, floor_days=int(st.get("floor_days", 3)),
                             max_calls=int(st.get("max_log_calls", 400)))
        if not sync["complete"]:
            ctx.out.problem("the %s log is still catching up (read through %s); this check may be short" % (
                group, sync["through"] or "nothing yet"))
    state: dict = {}
    total = missed_all = 0
    max_reads = int(st.get("max_record_reads", 60))
    for rule in st.get("expect") or []:
        name = str(rule.get("name") or rule["when"].get("event"))
        triggers = [e for e in activity.events(rule["when"].get("event") or "", since=since, until=until)
                    if e["resource"] and _matches(e, rule["when"])]
        # One trigger per record is enough: a tag added twice in a day is one expectation.
        by_record = {}
        for e in triggers:
            by_record.setdefault(e["resource"], e)
        misses = []
        for u, e in by_record.items():
            if len(state.get("recs", {})) >= max_reads:
                ctx.out.tally["triggers not checked (read cap)"] += 1
                continue
            miss = _produced(ctx, e, rule["then"], ctx.now, state)
            if miss:
                misses.append((u, miss))
        total += len(by_record)
        missed_all += len(misses)
        ctx.out.tally["%s: fired" % name] = len(by_record) - len(misses)
        if misses:
            ctx.out.tally["%s: missed" % name] = len(misses)
            what = sorted({m for _, ms in misses for m in ms})
            ctx.out.problem("%s missed %d of %d trigger(s) in the last day: no %s" % (
                name, len(misses), len(by_record), " or ".join(what)))
            for u, ms in misses[:3]:
                rec = state.get("recs", {}).get(u) or {}
                ctx.out.line("%s: %s did not produce %s" % (addr(rec) if rec else u[:8], name, " or ".join(ms)))
    ctx.out.found = total
    ctx.out.acted = missed_all
    if not total:
        ctx.out.head = "No sequence trigger in the last day to check."
    elif missed_all:
        ctx.out.head = "%d of %d sequence trigger(s) in the last day did not produce their result." % (missed_all, total)
    else:
        ctx.out.head = "All %d sequence trigger(s) in the last day produced their result." % total
