"""Every lead has a next action (build plan 2.7), new distress on a lead (2.12) and the nurture
bucket (2.13).

A prospecting record is kept moving by presets and counters. A lead has only its next task. When a
card is dragged or a task is completed with no follow-up, the lead goes quiet with no error
anywhere: on 2026-11-12 the account held 44 leads with no open task. The morning check
(leadcheck.py) names those leads. This module is the half that acts:

  * `job: missing` (daily). A lead with no open task gets the task its column's own sequence would
    have created, from the same task preset. The column is read from the card (the log copy), not
    only from the status, because the Nurture column sets no status of its own: a nurture lead
    still reads No Contact, and the daily No Contact task is the wrong one for it. A ghosting lead
    with a drip step ahead of it already has its next action and gets no task. A lead whose owner
    may have died gets no call task either: the gate holds it and the check names it.
  * `job: weekly` (Wednesdays). The slower questions, read only: does each card's column match the
    record's status, is there a lead with no card or a card with no lead, was a conversation left
    without a note, and do the account's own check presets still filter on statuses it uses.
  * `job: nurture`. An owner on a confirmed number who said "not now" sits in Follow Up. Nothing
    works that status, so every such record gets one dated call task 90 days out; the cold drip
    covers the months between.
  * `new_distress`, called by the morning check when a lead's record gained a list overnight (a
    lien, a notice, an obituary). The lead manager gets a task that day. A new obituary also marks
    the confirmed number as one to verify, because it may now reach a widow or a child, and queues
    the record for research. The lead status is never changed: the lead is still a lead.

The first task a live run creates is read back before the second is created. A route that answers
without a task behind it must stop the run at one record, not at forty.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import store
from . import activity, budget, drips, gate, lanes, ledger, windows
from . import leadclock
from . import leadroutes as routes
from .stages import Ctx, addr, phone_payload, phone_tier, tag_titles

log = logging.getLogger(__name__)

NO_TASK_NEEDED = ["under_contract", "transactions", "push_deal", "refer_lead"]
KNOWN_STATUSES = lanes.LEAD_STATUS_VALUES + lanes.GONE_STATUS_VALUES + ["prospecting", "Deep Prospecting", "Exhausted",
                                                                        "Return Mail"]
QC_PRESETS = ["Board - No Tasks (no next action)", "Board - Overdue Tasks", "Board - 2+ Open Tasks",
              "Board - Missing Correct Phone"]


def phase_of(u: str, status: str, ph: dict, lead_board: str) -> str:
    """The phase a lead is in: its card's column when the log copy shows the card on the lead board,
    else the phase its status belongs to."""
    e = routes.last_card_event(u)
    if e is not None:
        board = str(e["data"].get("board") or "").strip().lower()
        if not board or board == lead_board.strip().lower():
            p = routes.title_phase(e["data"].get("column") or "", ph, "lead")
            if p:
                return p
    return routes.status_phase(status, ph)


def just_made(u: str, why: str, now) -> bool:
    """Did this autopilot give the record a task for this reason in the last 20 hours? The count of
    open tasks comes off the search index, which can trail a write. Without this, a second run in
    the same morning would see the old zero and create the task twice."""
    return any(e["detail"].get("why") == why for e in ledger.events(u, "lead_task", since=now - timedelta(hours=20)))


def create_from_preset(ctx: Ctx, rec: dict, title: str, why: str, state: dict) -> str:
    """One task from a task preset on a lead. Returns created, failed, or no preset."""
    st = ctx.stage
    u = rec["uuid"]
    pid = routes.preset_id(ctx.crm, title)
    if not pid:
        ctx.out.problem("there is no task preset titled %r in this account; set `task_for` on the stage" % title)
        return "no preset"
    who = routes.task_owner(st, rec) if str(st.get("task_assignee") or "preset") == "record" else ""
    ctx.change(u, "task_create", None, {"preset": title})
    r = ctx.write("create task", routes.create_task_by_preset, ctx.crm, pid, u, ctx.now, ctx.policy.tz, who)
    if not ctx.live:
        return "created"
    tid = routes.task_id(r)
    if not state.get("verified"):
        state["verified"] = True
        ok = False
        if tid:
            try:
                back = routes.get_task(ctx.crm, tid)
                ok = str(back.get("assigned_to_property") or u) == u and not back.get("completed")
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            state["broken"] = True
            ctx.out.problem("the task route answered and no task could be read back on %s; nothing more was created. "
                            "Create one task from a preset by hand and compare" % addr(rec))
            return "failed"
    ledger.event(ctx.run_id, st.id, u, "lead_task", {"preset": title, "task": tid, "why": why}, now=ctx.now, tz=ctx.policy.tz)
    return "created"


# ---------------------------------------------------------------------- daily: the missing task
def run_missing(ctx: Ctx) -> None:
    st = ctx.stage
    ph = routes.phases(st)
    lead_board = routes.board_title(st, "lead")
    statuses = [str(s) for s in (st.get("statuses") or lanes.LEAD_STATUS_VALUES)]
    skip = {str(s).lower() for s in (st.get("no_task_needed") or NO_TASK_NEEDED)}
    task_for = {str(k).strip().lower(): str(v) for k, v in (st.get("task_for") or {}).items()}
    nurture_statuses = {str(s).lower() for s in (st.get("nurture_statuses") or ["follow_up"])}
    has_nurture = any(s.kind == "leadtasks" and str(s.get("job")) == "nurture" and s.mode != "off"
                      for s in ctx.policy.stages.values())
    leadclock.sync_logs(ctx, ("board", "state"))       # the card's column and the drip's start come from the log copy
    rows = routes.status_rows(ctx, statuses, cap=int(st.get("max_scan", 2000)))
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No record is in a lead status."
        return
    listed: dict[str, list[str]] = defaultdict(list)
    max_reads, max_create = int(st.get("max_reads", 80)), int(st.get("max_create", 50))
    reads = made = 0
    state: dict = {}
    for row in rows:
        status = lanes.status_of(row)
        if status in skip or int((row.get("tasks") or {}).get("incomplete") or 0) > 0:
            continue
        u, street = row["uuid"], addr(row)
        ctx.out.tally["leads with no open task"] += 1
        phase = phase_of(u, status, ph, lead_board)
        if phase == "ghosting":
            if drips.scheduled(ctx.policy, u, ctx.now):
                ctx.out.tally["ghosting leads whose next action is a drip step"] += 1
            else:
                listed["a ghosting lead with no drip step ahead: decide nurture or dead"].append(street)
            continue
        if status in nurture_statuses and status not in task_for:
            if not has_nurture:
                listed["Follow Up with no dated task (the nurture stage is off)"].append(street)
            continue
        title = task_for.get(status) or (ph[phase]["preset"] if phase else "")
        if not title:
            listed["status %s, which has no task preset mapped" % (status or "none")].append(street)
            continue
        if state.get("broken"):
            continue
        if just_made(u, "no open task", ctx.now):
            ctx.out.tally["given a task earlier today (the open-task count has not caught up)"] += 1
            continue
        if made >= max_create:
            ctx.out.tally["left for the next run (cap %d a run)" % max_create] += 1
            continue
        if reads >= max_reads:
            ctx.out.tally["not read this run (read cap)"] += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        if lanes.status_of(rec) != status:
            # The search index trails the record. A lead whose status just changed had its card moved,
            # and the new column's own sequence gives it its task.
            ctx.out.tally["status changed since the search (left for the next run)"] += 1
            continue
        if not ctx.passes(rec, "task", row=row):
            continue
        res = create_from_preset(ctx, rec, title, "no open task", state)
        if res == "created":
            made += 1
            ctx.out.line("%s: %s, task %s" % (street, ("%s column" % ph[phase]["column"]) if phase else ("status %s" % status), title))
    for what, streets in sorted(listed.items(), key=lambda kv: -len(kv[1])):
        ctx.out.problem("%d lead(s) with no task and none created: %s (%s)" % (len(streets), what, ", ".join(streets[:3])))
    proof = str(st.get("proof_preset") or QC_PRESETS[0])
    try:
        must, _notes = windows.resolve(ctx.crm.preset_must(proof), ctx.now)
        ctx.out.tally["the board's own no-task preset reads"] = ctx.crm.count(must)
    except KeyError:
        pass
    ctx.out.acted = made
    none = ctx.out.tally.get("leads with no open task", 0)
    if not none:
        ctx.out.head = "Every one of %d lead(s) has an open task." % len(rows)
    else:
        ctx.out.head = "%d of %d lead(s) had no open task. %s %d from the column's task preset." % (
            none, len(rows), "Created" if ctx.live else "Would create", made)


# ---------------------------------------------------------------------- weekly: the deeper pass
def _status_fix(value: str) -> str:
    """The stored spelling of a status a preset names, when the account has one that differs only
    by case, spaces and underscores ("New Lead" is stored as "new_lead")."""
    flat = value.strip().lower().replace(" ", "_")
    for s in KNOWN_STATUSES:
        if s.strip().lower().replace(" ", "_") == flat:
            return s
    return ""


def preset_health(ctx: Ctx, titles: list[str]) -> None:
    """A check preset that filters on a status the account does not store reads short for ever, and
    nobody sees a number that is too small. "3. Leads Missing Tasks" read 1 while 44 leads had no
    task, because it names "New Lead" and the account stores "new_lead"."""
    known = set(KNOWN_STATUSES)
    for title in titles:
        try:
            must = ctx.crm.preset_must(title)
        except KeyError:
            continue
        named = [str(s) for s in (must.get("any_property_status") or [])]
        wrong = [s for s in named if s not in known]
        if wrong:
            fixes = ["%s (stored as %s)" % (s, _status_fix(s)) if _status_fix(s) else s for s in wrong]
            ctx.out.problem("the preset %r filters on a status this account does not use: %s. It reads short; change it in "
                            "DataSift or with the preset change list" % (title, ", ".join(fixes)))


def _note_since(u: str, owner: str, since) -> bool:
    """Did a person write a note on this record (or on its owner's board) since a moment?"""
    for res in (u, owner):
        if res and any(e.get("author") == "user" and str(e.get("source") or "") not in routes.MACHINE_SOURCES
                       for e in activity.events(routes.NOTE_EVENTS, resource=res, since=since)):
            return True
    return False


def run_weekly(ctx: Ctx) -> None:
    st = ctx.stage
    ph = routes.phases(st)
    tz = ctx.policy.tz
    conv = int(st.get("conversation_seconds", 60))
    synced = leadclock.sync_logs(ctx)
    statuses = [str(s) for s in (st.get("statuses") or lanes.LEAD_STATUS_VALUES)]
    rows = {r["uuid"]: r for r in routes.status_rows(ctx, statuses, cap=int(st.get("max_scan", 2000)))}
    ctx.out.found = len(rows)
    found: dict[str, list[str]] = defaultdict(list)
    board = routes.stage_board(ctx, "lead", ph)
    if board.columns:
        where, unknown = routes.cards_by_record(ctx.crm, list(board.columns), cap=int(st.get("max_cards", 500)))
        if unknown:
            ctx.out.tally["cards whose record the log cannot name"] = unknown
        closed = {"dead", "not_interested"}
        for u, w in where.items():
            phase = board.phase_of(w["column"], ph, "lead")
            row = rows.get(u)
            if row is None:
                if phase and phase not in closed:
                    ctx.out.tally["cards in a working column whose record is not a lead"] += 1
                continue
            want = (ph[phase]["status"] if phase else "").lower()
            if want and lanes.status_of(row) != want:
                found["a card in one column and a status from another"].append("%s (%s card, status %s)" % (
                    addr(row), board.name(w["column"]), lanes.status_of(row) or "none"))
        on_lead = {p for p, r in ph.items() if r["board"] == "lead"}
        for u, row in rows.items():
            if u not in where and routes.status_phase(lanes.status_of(row), ph) in on_lead:
                found["no card on the %s board" % board.title].append(addr(row))
    if synced:
        any_note = bool(activity.events(routes.NOTE_EVENTS, limit=1))
        if not any_note:
            ctx.out.tally["no note has reached the log copy yet (the note check was skipped)"] = 1
        now = ctx.now
        for u, row in rows.items():
            owner = (row.get("owner") or {}).get("uuid") or ""
            talked, how = leadclock.last_conversation(owner, u, seconds=conv, notes=False)
            if any_note and talked:
                if not _note_since(u, owner, talked - timedelta(minutes=5)):
                    found["a conversation with no note after it"].append("%s (a %s on %s)" % (
                        addr(row), how, routes.nice_day(talked, tz)))
            mine = routes.open_tasks(u)
            late = [t for t in mine if t["due"] and t["due"] < now]
            if late:
                found["a task past its date"].append("%s (%s, due %s)" % (
                    addr(row), late[0]["title"] or "a task", routes.nice_day(late[0]["due"], tz)))
            if mine and not any(t["due"] for t in mine):
                # A next action is a DATED task. A task with no date never comes due and never
                # shows as overdue, so it hides a lead as well as having no task at all.
                found["an open task with no date on it"].append("%s (%s)" % (addr(row), mine[0]["title"] or "a task"))
    for row in rows.values():
        if int((row.get("tasks") or {}).get("incomplete") or 0) >= 2:
            found["two or more open tasks"].append(addr(row))
    preset_health(ctx, [str(t) for t in (st.get("check_presets") or ["3. Leads Missing Tasks"])])
    for title in [str(t) for t in (st.get("count_presets") or QC_PRESETS)]:
        try:
            must, _notes = windows.resolve(ctx.crm.preset_must(title), ctx.now)
            ctx.out.tally["preset %s" % title] = ctx.crm.count(must)
        except KeyError:
            continue
    total = sum(len(v) for v in found.values())
    # As lines, not problems: a post shows six lines and only three problems, and this report has
    # six kinds of finding. The preset line above is the one structural problem.
    for what, items in sorted(found.items(), key=lambda kv: -len(kv[1])):
        ctx.out.tally["leads with %s" % what] = len(items)
        ctx.out.line("%d lead(s) with %s (%s)" % (len(items), what, "; ".join(items[:2])))
    ctx.out.acted = total
    if total:
        ctx.out.head = "Weekly check of %d lead(s): %d thing(s) to put right, listed below." % (len(rows), total)
    else:
        ctx.out.head = "Weekly check of %d lead(s): every card matches its status, and nothing is overdue." % len(rows)


# ---------------------------------------------------------------------- the nurture bucket
def run_nurture(ctx: Ctx) -> None:
    st = ctx.stage
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    statuses = [str(s) for s in (st.get("statuses") or ["follow_up"])]
    days = int(st.get("due_days", 90))
    title = str(st.get("title") or "Call back: said not now")
    need_number = bool(st.get("require_confirmed", True))
    leadclock.sync_logs(ctx, ("state",))
    rows = routes.status_rows(ctx, statuses, cap=int(st.get("max_scan", 2000)))
    ctx.out.found = len(rows)
    max_reads, max_create = int(st.get("max_reads", 80)), int(st.get("max_create", 50))
    reads = made = have = 0
    no_number: list[str] = []
    state: dict = {}
    for row in rows:
        if int((row.get("tasks") or {}).get("incomplete") or 0) > 0:
            have += 1
            continue
        u = row["uuid"]
        if just_made(u, "nurture", ctx.now):
            have += 1
            continue
        if made >= max_create or reads >= max_reads or state.get("broken"):
            ctx.out.tally["left for the next run"] += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        if lanes.status_of(rec) != lanes.status_of(row):
            ctx.out.tally["status changed since the search (left alone)"] += 1      # no longer in the bucket
            continue
        if not ctx.passes(rec, "task", row=row):
            continue
        phones = (rec.get("owner") or {}).get("phones") or []
        if need_number and not any(str(p.get("status") or "").upper() == "CORRECT" for p in phones):
            no_number.append(addr(rec))
            continue
        said = [e for e in activity.events("property.status.updated", resource=u)
                if str(e["data"].get("to") or "").strip().lower() == lanes.status_of(row)]
        since = said[-1]["when"] if said else ctx.now
        due = max(since + timedelta(days=days), ctx.now + timedelta(days=1))
        ahead = max(1, (due.astimezone(zone).date() - ctx.now.astimezone(zone).date()).days)
        ctx.change(u, "task_create", None, {"title": title})
        r = ctx.write("nurture task", routes.create_task, ctx.crm, u, title, routes.end_of_day(ctx.now, tz, ahead),
                      routes.task_owner(st, rec), "Said not now on %s. One call, then decide: back to the board or dead." % (
                          routes.nice_day(since, tz)))
        if ctx.live:
            tid = routes.task_id(r)
            if not state.get("verified"):
                state["verified"] = True
                if not tid:
                    state["broken"] = True
                    ctx.out.problem("the task route answered with no task id on %s; nothing more was created" % addr(rec))
                    continue
            ledger.event(ctx.run_id, st.id, u, "lead_task", {"title": title, "task": tid, "why": "nurture"},
                         now=ctx.now, tz=tz)
        made += 1
        ctx.out.line("%s: call back on %s" % (addr(rec), routes.nice_day(due, tz)))
    if no_number:
        ctx.out.problem("%d Follow Up record(s) have no confirmed number, so no call task was set: confirm one or move "
                        "them (%s)" % (len(no_number), ", ".join(no_number[:3])))
    ctx.out.acted = made
    ctx.out.head = "%d record(s) in Follow Up: %d already had a dated task, %s %d a call task %d days out." % (
        len(rows), have, "gave" if ctx.live else "would give", made, days)


# ---------------------------------------------------------------------- a lead's record gained a list
def new_distress(ctx: Ctx, rec: dict, row: dict | None, new_lists: list[str]) -> bool:
    """A lead's record gained a list since the last check. The lead manager gets a task due today.
    A new obituary also marks every confirmed number as one to verify and queues the record for
    research. The lead's status is not touched. Returns False only when a read cap or a failed
    write means the same change should be looked at again tomorrow."""
    st = ctx.stage
    tz = ctx.policy.tz
    u = rec.get("uuid") or ""
    if not u or not new_lists:
        return True
    if lanes.status_of(rec) not in lanes.LEAD_STATUSES:
        # Thousands of prospecting records gain a list every week. This is for a LEAD that did, and
        # nothing here may run on a record that only looked like one in a search result.
        ctx.out.tally["gained a list and is not a lead (left alone)"] += 1
        return True
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    obit = [x for x in new_lists if x.strip().lower() in {d.lower() for d in cfg.deceased_lists}]
    # The task tells the lead manager to look before the next call, so a possible death must not
    # hold it: that warning is the point. Do Not Market, an opt-out, a litigator and a sale still do.
    # Being a lead is NOT lifted here: the stage has to name leads in its own gate_allow, and a
    # stage that does not is only allowed to name the change, never to act on it.
    if not ctx.passes(rec, "task", row=row, allow={"deceased"}):
        return True
    if obit:
        title = "An obituary landed on this lead: find out who died before the next call"
    else:
        title = "New on this lead: %s. Call today" % ", ".join(new_lists)
    # Each step below is safe to meet twice: if a write fails half way, the morning check hands the
    # same change back tomorrow, and the lead manager must not get a second copy of the task.
    recent = ledger.events(u, "lead_task", since=ctx.now - timedelta(days=2))
    if not any(e["detail"].get("why") == "gained a list" and sorted(e["detail"].get("lists") or []) == sorted(new_lists)
               for e in recent):
        ctx.change(u, "task_create", None, {"title": title})
        r = ctx.write("distress task", routes.create_task, ctx.crm, u, title[:250], routes.end_of_day(ctx.now, tz),
                      routes.task_owner(st, rec), "The record gained %s since yesterday's check." % ", ".join(new_lists))
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, "lead_task", {"why": "gained a list", "lists": new_lists,
                                                             "task": routes.task_id(r)}, now=ctx.now, tz=tz)
        ctx.out.tally["tasks for a lead that gained a list"] += 1
        ctx.out.line("%s: gained %s, a task for the lead manager due today%s" % (
            addr(rec), ", ".join(new_lists), "; its confirmed number is marked to verify and it goes to research" if obit else ""))
    if not obit:
        return True
    ow = rec.get("owner") or {}
    mark = str(st.get("stale_tag") or "Verify Number")
    confirmed = [p for p in (ow.get("phones") or []) if str(p.get("status") or "").upper() == "CORRECT"
                 and mark not in tag_titles(p)]
    if confirmed and ow.get("uuid"):
        ctx.change(u, "phones", [phone_payload(p, phone_tier(p)) for p in confirmed], [p.get("number") for p in confirmed])
        ctx.write("mark number", ctx.crm.upsert_phones, ow["uuid"], [phone_payload(p, phone_tier(p), [mark]) for p in confirmed])
        ctx.out.tally["confirmed numbers marked to verify (a new obituary)"] += len(confirmed)
    if not any(e["detail"].get("why") == "obituary note" for e in recent):
        ctx.write("obituary note", ctx.crm.message, u,
                  "An obituary was added to this record on %s. The number marked Correct may now reach someone else: "
                  "confirm who answers before talking about the house." % routes.nice_day(ctx.now, tz))
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, "lead_task", {"why": "obituary note"}, now=ctx.now, tz=tz)
    if queue_research(ctx, rec, row) == "waiting" and ctx.live:
        ledger.set_fact(u, "research_wanted", {"why": "a new obituary on a lead", "at": ctx.now.isoformat(timespec="seconds")})
    return True


QUEUED = "queued for research (a new obituary on a lead)"


def queue_research(ctx: Ctx, rec: dict, row: dict | None) -> str:
    """Hand a lead's record to research, inside the stage's own day cap. The research workflow
    spends inside its own caps and writes its findings to the record; it is given no instruction
    about the status, and this stage sets none. Returns queued, waiting, held or already."""
    st = ctx.stage
    u = rec.get("uuid") or ""
    if ledger.handed_off(u, "deep_prospect"):
        return "already"
    per_day = int(st.get("research_per_day", 2))
    today = ledger.day(ctx.now, ctx.policy.tz)
    # A live run's handoffs are in the ledger the moment they are made; a plan run writes none, so
    # there the run's own count stands in. Adding the two would count every live handoff twice.
    sent = sum(1 for e in ledger.events(action="handoff", stage=st.id) if e["day"] == today)
    if (sent if ctx.live else ctx.out.tally.get(QUEUED, 0)) >= per_day:
        ctx.out.tally["research waiting (cap %d a day)" % per_day] += 1
        return "waiting"
    if not ctx.passes(rec, "research", row=row):
        return "held"
    enqueue = ctx.enqueue or store.enqueue
    tid = ctx.write("handoff", enqueue, "research", "deep_prospect", u, addr(rec), source="autopilot:%s" % st.id)
    if ctx.live:
        ledger.event(ctx.run_id, st.id, u, "handoff", {"to": "deep_prospect", "why": "a new obituary on a lead",
                                                       "task": tid[0] if tid else 0}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally[QUEUED] += 1
    return "queued"


def flush_research(ctx: Ctx, max_reads: int = 5) -> None:
    """Queue the records an earlier day's cap left waiting. Called once a run by the morning check."""
    with store._LOCK, store._db() as c:
        waiting = [r["record_uuid"] for r in c.execute("SELECT record_uuid FROM ap_facts WHERE key='research_wanted' "
                                                       "AND value<>'null'").fetchall()]
    for u in waiting[:max_reads]:
        rec = ctx.crm.record(u)
        if queue_research(ctx, rec, None) != "waiting" and ctx.live:
            ledger.set_fact(u, "research_wanted", None)


def run_leadtasks(ctx: Ctx) -> None:
    job = str(ctx.stage.get("job") or "missing").strip().lower()
    if job == "weekly":
        run_weekly(ctx)
    elif job == "nurture":
        run_nurture(ctx)
    else:
        run_missing(ctx)
