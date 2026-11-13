"""Appointments (build plan 2.10): every walkthrough has a post, a confirm call and an outcome.

A walkthrough used to live in somebody's head or on a personal calendar. Nothing told the team it
was booked, nobody confirmed it the day before, and when the seller was not home nothing said so:
the card simply sat. An appointment is now a task with a start, an end and an outcome (the task
routes carry all three), created in DataSift by whoever booked it, or by Roma when told in Slack.
This stage watches those tasks and does the three things around each one:

  * WHEN IT IS BOOKED: one line in the channel (where, when, how many days out; the teaching is to
    set it for the same day when that is possible), and the comp workflow is queued, so the numbers
    exist before anyone drives out.
  * THE DAY BEFORE: a task to call and confirm, for whoever holds the appointment. A walkthrough
    booked for today gets the confirm task today. A moved appointment gets a new one.
  * AFTERWARDS: when the end time has passed and no outcome is recorded, a task to record it. Once
    an outcome is there (attended, no show, cancelled; offer made, accepted, rejected) the next
    move becomes a dated task for the lead manager, so a no-show is called back the same day and
    an offer has a follow-up date.

Each step is done once: the ledger remembers it, keyed on the appointment and its start time. An
appointment is found in the shared log copy (the task-created event says it is one) and read
through the task route only when something about it can have changed.

The Appointment Booked column is checked once a day: a card there whose record has no appointment
is counted, because the Make Offer sequence lands every card in that column today and the column
should mean a walkthrough is booked.

`book` is the booking itself, for Roma: it creates the appointment task and nothing else. The
stage picks it up on its next run like any other.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from . import activity, budget, gate, ledger
from . import leadclock
from . import leadroutes as routes
from .stages import Ctx, addr, assignee_of

log = logging.getLogger(__name__)

# What each outcome makes the next move. A key is the outcome status, or "attended:<result>".
OUTCOME_NEXT = {
    "no_show": {"task": "No show at the walkthrough: call to rebook", "due_days": 0},
    "cancelled": {"task": "Walkthrough cancelled: call to rebook", "due_days": 1},
    "rescheduled": {},
    "attended:offer_made": {"task": "Follow up on the offer", "due_days": 2},
    "attended:offer_accepted": {"task": "Offer accepted: send the contract and open title", "due_days": 0},
    "attended:offer_rejected": {"task": "Offer rejected at the walkthrough: decide the next step", "due_days": 1},
    "attended:needs_follow_up": {"task": "Follow up after the walkthrough", "due_days": 2},
    "attended:not_interested": {"task": "Seller said no at the walkthrough: move the card", "due_days": 1},
    "attended:no_result": {"task": "Walkthrough done: record the result and make the offer", "due_days": 0},
}


def _steps(u: str, task: str) -> dict:
    """step -> the start times it was done for, from the ledger (so only live runs count)."""
    out: dict[str, set] = {}
    for e in ledger.events(u, "appointment"):
        d = e["detail"]
        if d.get("appt") == task and d.get("step"):
            out.setdefault(str(d["step"]), set()).add(str(d.get("start") or ""))
    return out


def known(ctx: Ctx, look: timedelta) -> dict:
    """appointment task id -> record id, for every appointment made in the look-back window: the ones
    the log copy shows being created, and the ones booked through `book`."""
    out = {}
    for e in activity.events("task.created", since=ctx.now - look):
        if e["data"].get("kind") == "appointment" and e["data"].get("task") and e["resource"]:
            out[e["data"]["task"]] = e["resource"]
    for e in ledger.events(action="appointment", since=ctx.now - look):
        if e["detail"].get("step") == "booked" and e["detail"].get("appt") and e["record_uuid"]:
            out[e["detail"]["appt"]] = e["record_uuid"]
    for task in list(out):
        if activity.events("task.deleted", resource=task) or \
                any(x["data"].get("task") == task for x in activity.events("task.deleted", resource=out[task])):
            out.pop(task)
    return out


def _make_task(ctx: Ctx, rec: dict, title: str, days: int, who: str, appt: str, step: str, start: str, notes: str = "") -> bool:
    """One task around an appointment, behind the gate, remembered so it is made once."""
    u = rec["uuid"]
    if not ctx.passes(rec, "task"):
        return False
    due = routes.end_of_day(routes.business_day(ctx.now, ctx.policy.tz, days), ctx.policy.tz)
    ctx.change(u, "task_create", None, {"title": title})
    r = ctx.write("appointment task", routes.create_task, ctx.crm, u, title[:250], due, who, notes)
    if ctx.live:
        ledger.event(ctx.run_id, ctx.stage.id, u, "appointment", {"appt": appt, "step": step, "start": start,
                                                                   "task": routes.task_id(r)}, now=ctx.now, tz=ctx.policy.tz)
    return True


def _mark(ctx: Ctx, u: str, appt: str, step: str, start: str) -> None:
    if ctx.live:
        ledger.event(ctx.run_id, ctx.stage.id, u, "appointment", {"appt": appt, "step": step, "start": start},
                     now=ctx.now, tz=ctx.policy.tz)


def column_check(ctx: Ctx, open_now: set) -> None:
    """Once a day: cards in Appointment Booked whose record has no appointment ahead of it."""
    st = ctx.stage
    day = ledger.day(ctx.now, ctx.policy.tz)
    key = "autopilot:appointments:column_check:%s" % st.id
    if not st.get("check_column", True) or store.kv_get(key) == day:
        return
    ph = routes.phases(st)
    problems_before = list(ctx.out.problems)
    board = routes.stage_board(ctx, "offers", ph)
    col = board.find(ph["appointment"]["column"]) if board.columns else ""
    if not col:
        # Not knowing the offers board is not this stage's problem to raise every half hour.
        ctx.out.problems[:] = problems_before
        ctx.out.tally["the Appointment Booked column could not be read (its id is not known)"] = 1
        store.kv_set(key, day)
        return
    where, _unknown = routes.cards_by_record(ctx.crm, [col], cap=int(st.get("max_cards", 300)))
    empty = [u for u in where if u not in open_now]
    store.kv_set(key, day)
    ctx.out.tally["cards in %s" % board.name(col)] = len(where)
    if empty:
        ctx.out.problem("%d card(s) sit in %s with no appointment on the record. The Make Offer sequence still lands "
                        "cards there: change its landing column in Sequence Builder so the column means a booked walkthrough"
                        % (len(empty), board.name(col)))


def run_appointments(ctx: Ctx) -> None:
    st = ctx.stage
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    leadclock.sync_logs(ctx, ("board", "state"))
    look = timedelta(days=float(st.get("lookback_days", 45)))
    grace = timedelta(hours=float(st.get("outcome_grace_hours", 24)))
    table = dict(OUTCOME_NEXT)
    table.update({str(k): (v or {}) for k, v in (st.get("outcomes") or {}).items()})
    workflow, agent = str(st.get("comps_workflow", "quick_screen") or ""), str(st.get("comps_agent") or "sales")
    appts = known(ctx, look)
    ctx.out.found = len(appts)
    reads = {"n": 0, "max": int(st.get("max_reads", 40))}
    recs: dict[str, dict] = {}
    ahead = waiting = acted = 0
    overdue: list[str] = []
    open_now: set = set()

    def record(u: str) -> dict | None:
        if u not in recs:
            if reads["n"] >= reads["max"]:
                return None
            reads["n"] += 1
            recs[u] = ctx.crm.record(u)
        return recs[u]

    for appt, u in sorted(appts.items()):
        fact = ledger.fact(u, "appt:%s" % appt) or {}
        if fact.get("closed"):
            continue
        start = routes.parse_dt(fact.get("start"))
        read_at = routes.parse_dt(fact.get("read"))
        # Read the task again when it has never been read, when it is close (inside two days either
        # side of its start), or once a day otherwise: a reschedule or an outcome shows up only there.
        near = start is not None and abs((start - ctx.now).total_seconds()) < 2 * 86400
        if start is None or read_at is None or near or ctx.now - read_at > timedelta(hours=20):
            if reads["n"] >= reads["max"]:
                ctx.out.tally["appointments not read this run (read cap)"] += 1
                continue
            reads["n"] += 1
            try:
                t = routes.get_task(ctx.crm, appt)
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001  (deleted in the app: nothing left to watch)
                ledger.set_fact(u, "appt:%s" % appt, dict(fact, closed="the appointment is gone"))
                continue
            start = routes.parse_dt(t.get("start_time") or t.get("due_date"))
            if start is None or str(t.get("event_type") or "appointment") != "appointment":
                ledger.set_fact(u, "appt:%s" % appt, dict(fact, closed="not an appointment"))
                continue
            end = routes.parse_dt(t.get("end_time")) or start + timedelta(hours=1)
            holder = t.get("assigned_to_user") if isinstance(t.get("assigned_to_user"), dict) else {}
            fact = dict(fact, start=routes.iso_z(start), end=routes.iso_z(end), outcome=str(t.get("outcome_status") or ""),
                        result=str(t.get("outcome_result") or ""), holder=str(holder.get("uuid") or ""),
                        read=routes.iso_z(ctx.now), street=fact.get("street") or str(t.get("address") or ""))
            ledger.set_fact(u, "appt:%s" % appt, fact)      # what the appointment looks like: knowledge, kept in plan mode too
        end = routes.parse_dt(fact.get("end")) or start + timedelta(hours=1)
        stamp = fact["start"]
        done = _steps(u, appt)
        street = fact.get("street") or ""
        when = routes.nice_time(start, tz)

        if "post" not in done:
            rec = record(u)
            if rec is None:
                ctx.out.tally["appointments not read this run (read cap)"] += 1
                continue
            street = addr(rec)
            fact = dict(fact, street=street)
            ledger.set_fact(u, "appt:%s" % appt, fact)
            out_days = (start.astimezone(zone).date() - ctx.now.astimezone(zone).date()).days
            how = "today" if out_days == 0 else ("in %d day(s)" % out_days if out_days > 0 else "%d day(s) ago" % -out_days)
            ctx.out.line("Walkthrough booked: %s, %s (%s)" % (street, when, how))
            ctx.out.tally["walkthroughs booked"] += 1
            if out_days == 0:
                ctx.out.tally["set for the same day"] += 1
            if workflow and start > ctx.now:
                enqueue = ctx.enqueue or store.enqueue
                ctx.write("comps", enqueue, agent, workflow, u, street, source="autopilot:%s" % st.id)
                ctx.out.tally["comps queued before the walkthrough"] += 1
            _mark(ctx, u, appt, "post", stamp)
            acted += 1

        outcome = fact.get("outcome") or ""
        if outcome == "rescheduled" and start > ctx.now:
            outcome = ""          # moved to a time still ahead: it is an appointment again, not a result
        if outcome:
            key = outcome if outcome != "attended" else "attended:%s" % (fact.get("result") or "no_result")
            step = "outcome:%s" % key
            if step not in done:
                nxt = table.get(key) or table.get(outcome) or {}
                if nxt.get("task"):
                    rec = record(u)
                    if rec is None:
                        ctx.out.tally["appointments not read this run (read cap)"] += 1
                        continue
                    if _make_task(ctx, rec, "%s (%s)" % (nxt["task"], street or addr(rec)), int(nxt.get("due_days", 1)),
                                  routes.task_owner(st, rec), appt, step, stamp, "The walkthrough on %s: %s." % (
                                      when, key.replace("attended:", "attended, ").replace("_", " "))):
                        acted += 1
                        ctx.out.line("%s: %s. Next: %s" % (street, key.replace("attended:", "attended, ").replace("_", " "),
                                                           nxt["task"]))
                else:
                    _mark(ctx, u, appt, step, stamp)
            ledger.set_fact(u, "appt:%s" % appt, dict(fact, closed="outcome recorded") if ctx.live else fact)
            continue

        if start > ctx.now:
            ahead += 1
            open_now.add(u)
            if stamp not in done.get("confirm", set()):
                rec = record(u)
                if rec is None:
                    ctx.out.tally["appointments not read this run (read cap)"] += 1
                    continue
                today = ctx.now.astimezone(zone).date()
                gap = max(0, (start.astimezone(zone).date() - today).days - 1)
                moved = bool(done.get("confirm"))
                who = fact.get("holder") or assignee_of(rec)
                title = "Confirm the walkthrough: %s, %s" % (street or addr(rec), when)
                due = routes.end_of_day(ctx.now + timedelta(days=gap), tz)
                if ctx.passes(rec, "task"):
                    ctx.change(u, "task_create", None, {"title": title})
                    r = ctx.write("confirm task", routes.create_task, ctx.crm, u, title[:250], due, who,
                                  "Call the seller the day before and confirm the time.")
                    if ctx.live:
                        ledger.event(ctx.run_id, st.id, u, "appointment", {"appt": appt, "step": "confirm", "start": stamp,
                                                                            "task": routes.task_id(r)}, now=ctx.now, tz=tz)
                    acted += 1
                    ctx.out.line("%s: %sconfirm call due %s" % (street or addr(rec), "moved to %s, " % when if moved else "",
                                                                routes.nice_day(ctx.now + timedelta(days=gap), tz)))
            continue

        if end < ctx.now:
            waiting += 1
            if ctx.now - end > timedelta(days=float(st.get("give_up_days", 14))):
                ledger.set_fact(u, "appt:%s" % appt, dict(fact, closed="no outcome was ever recorded"))
                continue
            if stamp not in done.get("ask", set()):
                rec = record(u)
                if rec is None:
                    ctx.out.tally["appointments not read this run (read cap)"] += 1
                    continue
                if _make_task(ctx, rec, "Record how the walkthrough went: %s" % (street or addr(rec)), 0,
                              fact.get("holder") or assignee_of(rec), appt, "ask", stamp,
                              "Open the appointment and set its outcome: attended, no show or cancelled, and the result."):
                    acted += 1
            if ctx.now - end > grace:
                overdue.append(street or appt[:8])
        else:
            open_now.add(u)       # under way right now
    if overdue:
        ctx.out.problem("%d walkthrough(s) ended more than %d hours ago with no outcome recorded: %s. Set the outcome "
                        "on the appointment" % (len(overdue), int(grace.total_seconds() // 3600), ", ".join(overdue[:3])))
    column_check(ctx, open_now)
    ctx.out.acted = acted
    ctx.out.head = "%d walkthrough(s) ahead, %d waiting on an outcome." % (ahead, waiting)
    if not appts:
        ctx.out.head = "No walkthrough is on the books."


# ---------------------------------------------------------------------- booking (for Roma)
def local_time(text, tz: str) -> datetime | None:
    """A day and time as a person gives it. With no zone on it, it is business-local time: "2pm
    Tuesday" said in Knoxville is 2pm Eastern, and reading it as UTC books the visit for 9am."""
    s = str(text or "").strip().replace(" ", "T")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(tz))


def book(crm, record_uuid: str, start, *, minutes: int = 60, assignee: str = "", booked_by: str = "",
         kind: str = "property_walkthrough", where: str = "in_person", notes: str = "", pol=None,
         now: datetime | None = None, commit: bool = False) -> dict:
    """Book a walkthrough on a record: the appointment task, nothing else. Dry unless `commit` is set
    AND the box is live. A person asked for this booking, so being a lead does not hold it, and a
    possible death does not either (somebody agreed to the visit); Do Not Market, an opt-out, a
    litigator and a sold house still refuse it, with the reason."""
    now = now or datetime.now(timezone.utc)
    tz = getattr(pol, "tz", None) or ledger.BUSINESS_TZ
    when = start if isinstance(start, datetime) else local_time(start, tz)
    if when is None:
        return {"ok": False, "say": "Give the day and time like 2026-11-20 14:00."}
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz))
    if when < now - timedelta(minutes=5):
        return {"ok": False, "say": "That time has already passed."}
    rec = crm.record(record_uuid)
    cfg = getattr(pol, "gate", None) or gate.GateConfig()
    v = gate.check(rec, "task", cfg=cfg, allow={"lead", "deceased", "unknown_status", "in_research", "exhausted", "listed"})
    if not v.ok:
        return {"ok": False, "say": "Not booked: %s." % v.reason()}
    street = addr(rec)
    zone = ZoneInfo(tz)
    out_days = (when.astimezone(zone).date() - now.astimezone(zone).date()).days
    plan = {"record": record_uuid, "street": street, "when": routes.nice_time(when, tz), "minutes": int(minutes),
            "days_out": out_days, "assignee": assignee or assignee_of(rec)}
    if not (commit and config.live()):
        return {"ok": True, "dry": True, "would_book": plan,
                "say": "Would book a walkthrough at %s for %s." % (street, plan["when"])}
    run_id = "book-%s" % uuid.uuid4().hex[:8]          # one id per booking, so undo takes back exactly this one
    ledger.change(run_id, "appointments", record_uuid, "task_create", None, {"title": "Walkthrough: %s" % street})
    r = routes.create_appointment(crm, record_uuid, "Walkthrough: %s" % street, when, when + timedelta(minutes=int(minutes)),
                                  assignee=plan["assignee"], address=street, kind=kind, where=where, notes=notes)
    tid = routes.task_id(r)
    ledger.event(run_id, "appointments", record_uuid, "appointment",
                 {"appt": tid, "task": tid, "step": "booked", "start": routes.iso_z(when), "by": booked_by}, now=now, tz=tz)
    return {"ok": bool(tid), "booked": plan, "task": tid,
            "say": "Booked: %s, %s%s." % (street, plan["when"], " (today)" if out_days == 0 else "")}


def book_run(task: dict, crm=None, pol=None, now: datetime | None = None) -> dict:
    """The booking as a team workflow (the run half of the workflows/__init__.py contract). The
    record is the task's record; the inputs are `when` (a day and time, business-local unless it
    carries a zone) and optionally `minutes`. On a box that is not live it says what it would book."""
    payload = store.payload_of(task)
    inputs = payload.get("inputs") or {}
    record = task.get("record_uuid") or ""
    if not record or not inputs.get("when"):
        return {"ok": False, "say": "Name the record and the day and time of the walkthrough."}
    try:
        minutes = int(float(inputs.get("minutes") or 60))
    except (TypeError, ValueError):
        minutes = 60
    if pol is None:
        try:
            from . import policy as policy_mod
            pol = policy_mod.load()
        except Exception:  # noqa: BLE001  (a playbook that will not load must not stop a booking: the gate's defaults apply)
            pol = None
    if crm is None:
        from .crm import CRM
        crm = CRM()
    return book(crm, record, str(inputs["when"]), minutes=minutes, pol=pol, now=now,
                booked_by=str(payload.get("requested_by") or ""), commit=True)


def book_render(task: dict, result: dict) -> str:
    return str((result or {}).get("say") or "")
