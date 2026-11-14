"""Every lead has a next action (build plan 2.7): the daily check on the lead half of the business.

A lead with no open task is a lead nobody will call. The prospecting side has presets and counters
to keep records moving; a lead has only its next task, and when a card is dragged or a task is
completed without a follow-up, the lead goes quiet without any error. On 2026-11-12 the account
held 44 leads with no task at all.

Each morning this stage reads every record in a lead status and checks the four things a worked
lead always has:

  * an open task (the search row carries the task counts, so this costs nothing extra);
  * an assignee;
  * a number a person confirmed (a phone marked CORRECT);
  * a status the lead manager set on purpose: a record still reading "new_lead" after a day has
    not been called back.

Two more things ride on the same reads:

  * SPEED TO LEAD (plan 2.2), measured, not guessed: for every record that became a new lead in the
    last week, the minutes from that status change to the first outbound dial to its owner.
  * NEW DISTRESS ON A LEAD (plan 2.12): a lead whose record gained a list since yesterday (a lien,
    a notice, an obituary) is named, because that is the day to call. A new Obituary list on a lead
    also means the confirmed number may now reach someone else.

The check itself reads only: it hands the lead manager the list, grouped by what is missing, and
stays silent when nothing is. Two things act, and both live in leadtasks.py and go through
Ctx.write, so in plan mode they are only described:

  * the missing task is created by the lead-tasks stage that runs just before this one, so what is
    still listed here is what could not be fixed by a machine;
  * a lead whose record GAINED A LIST is handed to leadtasks.new_distress on the spot: a task for
    the lead manager that day and, for a new obituary, a mark on the confirmed number and a place
    in the research queue. The lead's status is never changed. What the lead looked like is
    remembered only once that was done, so a failed write is looked at again the next morning.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from . import activity, budget, gate, lanes, leadtasks, ledger
from . import leadroutes as routes
from .stages import Ctx, addr, assignee_of

log = logging.getLogger(__name__)


def _list_titles(rec: dict) -> list[str]:
    """The record's lists by title. The detail route has returned both plain titles and objects."""
    out = set()
    for x in rec.get("lists") or []:
        t = (x.get("title") or x.get("name")) if isinstance(x, dict) else x
        if t:
            out.add(str(t))
    return sorted(out)


def _speed_to_lead(ctx: Ctx, owners: dict, window: timedelta) -> None:
    """Minutes from each new-lead status change in the window to the first outbound dial to that
    owner. A lead that has not been dialed at all is counted on its own: it is the one that matters."""
    waits, never, from_call = [], 0, 0
    for e in activity.events("property.status.updated", since=ctx.now - window):
        if str(e["data"].get("to") or "").lower() != "new_lead":
            continue
        u = e["resource"]
        owner = owners.get(u)
        if owner is None:
            rec = ctx.crm.record(u)
            owner = (rec.get("owner") or {}).get("uuid") or ""
            owners[u] = owner
        if not owner:
            continue
        calls = [d for d in activity.events(activity.CALLS, resource=owner, since=e["when"] - timedelta(minutes=20))
                 if d["event_type"] != "owner.call.missed"]
        if any(d["when"] <= e["when"] for d in calls):
            from_call += 1            # the status was set on, or right after, a call: there was no wait to measure
            continue
        dials = [d for d in calls if d["data"].get("direction") != "inbound"]
        if dials:
            waits.append((dials[0]["when"] - e["when"]).total_seconds() / 60.0)
        else:
            never += 1
    if from_call:
        ctx.out.tally["new leads set on a call (no wait to measure)"] = from_call
    if not waits and not never:
        return
    days = int(window.total_seconds() // 86400)
    if waits:
        waits.sort()
        median = waits[len(waits) // 2]
        ctx.out.line("Speed to lead, last %d days: median %s to the first call, slowest %s, over %d new lead(s)" % (
            days, _dur(median), _dur(waits[-1]), len(waits)))
        ctx.out.tally["speed to lead, median minutes"] = round(median)
    if never:
        ctx.out.problem("%d new lead(s) in the last %d days have no outbound call in the log yet" % (never, days))


def _dur(minutes: float) -> str:
    if minutes < 90:
        return "%d min" % round(minutes)
    if minutes < 48 * 60:
        return "%.1f hours" % (minutes / 60.0)
    return "%.1f days" % (minutes / 1440.0)


def run_leadcheck(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    statuses = [str(s) for s in (st.get("statuses") or lanes.LEAD_STATUS_VALUES)]
    skip_task = {str(s).lower() for s in (st.get("no_task_needed") or ["under_contract", "transactions"])}
    # Checked row by row as well: a search that stopped filtering would hand every record in the
    # account to the list-gained step below.
    rows = routes.status_rows(ctx, statuses, cap=int(st.get("max_scan", 2000)))
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No record is in a lead status."
        return
    stale_after = timedelta(hours=float(st.get("new_lead_hours", 24)))
    try:
        log_ok = True
        for group in ("state", "calls"):
            sync = activity.sync(ctx.crm, group, now=ctx.now, floor_days=int(st.get("floor_days", 14)),
                                 max_calls=int(st.get("max_log_calls", 400)))
            log_ok = log_ok and sync["complete"]
    except Exception as exc:  # noqa: BLE001
        log_ok = False
        ctx.out.problem("could not read the change log (%s); how long a lead has been new was not checked" % str(exc)[:100])
    owners: dict[str, str] = {}
    gained: list[str] = []
    missing: dict[str, list] = defaultdict(list)
    by_status: dict[str, int] = defaultdict(int)
    max_reads = int(st.get("max_records", 300))
    for i, row in enumerate(rows):
        status = lanes.status_of(row)
        by_status[status] += 1
        tasks = row.get("tasks") or {}
        street = addr(row)
        if status not in skip_task and int(tasks.get("incomplete") or 0) == 0:
            missing["no open task"].append(street)
        if i >= max_reads:
            ctx.out.tally["leads not opened (read cap)"] += 1
            continue
        rec = ctx.crm.record(row["uuid"])
        owners[row["uuid"]] = (rec.get("owner") or {}).get("uuid") or ""
        now_lists = _list_titles(rec)
        before = ledger.fact(row["uuid"], "lead_lists")
        handled = True
        if before is not None:
            new_lists = [x for x in now_lists if x not in before]
            if new_lists:
                gained.append("%s gained %s" % (street, ", ".join(new_lists)))
                try:
                    handled = leadtasks.new_distress(ctx, rec, row, new_lists)
                except budget.BudgetExhausted:
                    raise
                except Exception as exc:  # noqa: BLE001  (one lead failing must not stop the morning check)
                    handled = False
                    ctx.out.problem("could not act on %s gaining a list (%s); it is looked at again tomorrow" % (
                        street, str(exc)[:100]))
        if handled:
            ledger.set_fact(row["uuid"], "lead_lists", now_lists)      # what the lead looked like today: knowledge, kept in plan mode too
        if not assignee_of(rec):
            missing["no assignee"].append(street)
        phones = (rec.get("owner") or {}).get("phones") or []
        if not any(str(p.get("status") or "").upper() == "CORRECT" for p in phones):
            missing["no confirmed number"].append(street)
        if status == "new_lead" and log_ok:
            became = [e for e in activity.events("property.status.updated", resource=row["uuid"])
                      if str(e["data"].get("to") or "").lower() == "new_lead"]
            # No change in the log means the record has read New Lead for longer than the log reaches.
            if not became or ctx.now - became[-1]["when"] > stale_after:
                missing["still New Lead after a day"].append(street)
        if gate.deceased_signals(rec, getattr(ctx.policy, "gate", None) or gate.GateConfig()):
            missing["the owner may have died: check before the next call"].append(street)
    if gained:
        ctx.out.tally["leads whose record gained a list since the last check"] = len(gained)
        ctx.out.problem("%d lead(s) gained a list since the last check: call them today (%s)" % (len(gained), "; ".join(gained[:3])))
    try:
        leadtasks.flush_research(ctx)      # a lead an earlier day's research cap left waiting
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not queue a waiting lead for research (%s)" % str(exc)[:100])
    if log_ok:
        _speed_to_lead(ctx, owners, timedelta(days=float(st.get("speed_days", 7))))
    total = sum(len(v) for v in missing.values())
    ctx.out.acted = total + len(gained)
    for what, streets in sorted(missing.items(), key=lambda kv: -len(kv[1])):
        ctx.out.tally["leads with %s" % what] = len(streets)
        ctx.out.problem("%d lead(s) with %s (for example %s)" % (len(streets), what, ", ".join(streets[:2])))
    mix = ", ".join("%s %d" % (k or "none", n) for k, n in sorted(by_status.items(), key=lambda kv: -kv[1])[:6])
    if total:
        ctx.out.head = "%d lead(s) (%s). %d thing(s) are missing across them; the list is below." % (len(rows), mix, total)
    else:
        ctx.out.head = "%d lead(s) (%s). Every one has an open task, an assignee and a confirmed number." % (len(rows), mix)
    assert cfg is not None
