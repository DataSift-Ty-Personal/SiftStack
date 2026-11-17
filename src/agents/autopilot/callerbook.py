"""The weekly caller book (build plan 4.2, decision D5).

A caller who is handed new records every day never finishes a round on anything: Monday's records
are on attempt two when Tuesday's arrive, and by Friday the queue is a pile nobody can read. The
teaching is 50 to 100 new records a week, 500 to 750 a month, loaded ONCE, and a caller's open
records kept under about 2,000. A queue far past that is mostly records nobody has started.

So on Monday at 7:30 each caller gets a book of new Priority 1 records (75 by default), taken from
the presets in the order the playbook lists them, and nothing more is added until the next Monday.

  * ONCE A WEEK. The first live run of the week hands the book out and marks the week. A run later
    that week adds nothing, whatever the feeders have added since (`top_up: true` lets a later run
    fill a book that came up short, and never starts a second one).
  * INSIDE THE CAP. Nobody is taken past `caller_cap` open records (2,000). A caller at the cap
    gets nothing this week and is named, because the fix is theirs or the owner's, not the stage's.
  * THE BEST CALLER PICKS FIRST. When the playbook names one (`best_caller`), that caller's book is
    filled from the first `best_rungs` presets, the top of the ladder, before anybody else's.
  * THE GATE ON EVERY RECORD. A record is read and passed through the suppression gate before it
    is assigned: a lead, a possible death, a Do Not Market record and an owner already in a higher
    lane are never put in a book.
  * First-to-market records are not part of the book. They cannot wait for Monday and arrive
    within 30 minutes through the assign stage.

The Dream 100 is the other weekly hand-out. When the playbook names its caller, the records on the
Dream 100 list go to that one person, and five tasks (Monday to Friday, 20 houses a day by default)
lay the week out as a board they work down.

A stage in plan mode lists the book and writes nothing; a live run writes each assignment with the
caller it replaced, so one command puts the week back.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import store
from . import laneroutes, ledger
from .stages import (DEAD_STATUSES, GONE_STATUSES, WORKLOAD_EXCLUDE, Ctx, addr, assignee_of, resolve_callers, stage_rows,
                     status_of)

log = logging.getLogger(__name__)


def week_key(stage_id: str, week: str) -> str:
    return "autopilot:book:%s:%s" % (stage_id, week)


def run_callerbook(ctx: Ctx) -> None:
    st = ctx.stage
    week = laneroutes.week_of(ctx.now, ctx.policy.tz)
    size = int(st.get("book_size", 75))
    cap = int(st.get("caller_cap", 2000))
    people = resolve_callers(ctx)
    if not people:
        ctx.out.problem("no active caller to hand a book to")
        ctx.out.head = "No book went out: there is no active caller."
        return
    handed = store.kv_get(week_key(st.id, week))
    if handed and not st.get("top_up", False):
        ctx.out.head = "This week's book went out on %s. Nothing is added until next Monday." % handed[:10]
        return
    rows = stage_rows(ctx, extra={"assigned_to": None})
    ctx.out.found = len(rows)
    load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
            for p in people}
    had = Counter(e["detail"].get("to_uuid") for e in ledger.events(action="book", stage=st.id)
                  if e["detail"].get("week") == week)
    room: dict[str, int] = {}
    for p in people:
        left_in_cap = cap - load[p["uuid"]]
        if left_in_cap <= 0:
            ctx.out.problem("%s holds %d open records, at or over the cap of %d, and gets no new records this week" % (
                p["name"], load[p["uuid"]], cap))
        room[p["uuid"]] = max(0, min(size - had.get(p["uuid"], 0), left_in_cap))
    best_name = str(st.get("best_caller") or "").strip().lower()
    best = next((p for p in people if p["name"].lower() == best_name), None)
    if best_name and not best:
        ctx.out.problem("best_caller %s is not one of the active callers, so nobody picks first" % st.get("best_caller"))
    top = set(st.presets[: int(st.get("best_rungs", 6))]) if best else set()
    max_reads = int(st.get("max_reads", max(200, 3 * size * len(people))))
    state = {"reads": 0, "checked": False, "stop": False}
    given: Counter = Counter()
    taken: set[str] = set()

    def hand(person: dict, title: str, row: dict) -> bool:
        """Read one record, pass it through the gate and put it in this caller's book."""
        u = row["uuid"]
        if state["reads"] >= max_reads:
            state["stop"] = True
            return False
        state["reads"] += 1
        rec = ctx.crm.record(u)
        taken.add(u)
        if status_of(rec) in DEAD_STATUSES or assignee_of(rec):
            return False
        if not ctx.passes(rec, "assign", row=row):
            return False
        ctx.change(u, "assign", assignee_of(rec), person["uuid"])
        ctx.write("book", ctx.crm.assign, u, person["uuid"])
        if ctx.live:
            if not state["checked"]:
                state["checked"] = True
                if assignee_of(ctx.crm.record(u)) != person["uuid"]:
                    ctx.out.problem("the assignment did not stick on %s; no more of the book was handed out" % addr(row))
                    state["stop"] = True
                    return False
            ledger.event(ctx.run_id, st.id, u, "book", {"to": person["name"], "to_uuid": person["uuid"], "preset": title,
                                                        "week": week}, now=ctx.now, tz=ctx.policy.tz)
        room[person["uuid"]] -= 1
        given[person["name"]] += 1
        ctx.out.line("%s -> %s (%s)" % (addr(row), person["name"], title))
        return True

    if best:
        for title, row in rows:
            if state["stop"] or room[best["uuid"]] <= 0:
                break
            if title in top:
                hand(best, title, row)
    turn = 0
    for title, row in rows:
        open_people = [p for p in people if room[p["uuid"]] > 0]
        if state["stop"] or not open_people:
            break                     # every book is full: stop reading, the rest wait for Monday
        if row["uuid"] in taken:
            continue
        if hand(open_people[turn % len(open_people)], title, row):
            turn += 1
    if state["reads"] >= max_reads and any(room.values()):
        ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
    total = sum(given.values())
    # The Dream 100 is laid out once a week too: a top-up run must not lay the week out a second time.
    dream = _dream100(ctx, week) if st.get("dream100_caller") and not handed else 0
    if ctx.live and (total or dream):
        store.kv_set(week_key(st.id, week), ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).isoformat(timespec="minutes"))
    ctx.out.acted = total + dream
    left = max(0, len(rows) - total)
    if total:
        ctx.out.head = "%s this week's book: %s. %d unassigned record(s) wait for next Monday." % (
            "Handed out" if ctx.live else "Would hand out", ", ".join("%s %d" % kv for kv in given.most_common()), left)
    else:
        ctx.out.head = "No book went out: %s.%s" % (
            "nothing unassigned is waiting in the presets" if not rows else "no caller has room and no record passed the gate",
            (" The Dream 100 %s (%d records)." % ("was laid out" if ctx.live else "would be laid out", dream)) if dream else "")
    short = [p["name"] for p in people if room[p["uuid"]] > 0]
    if total and short and not state["stop"]:
        ctx.out.line("Short of a full book of %d: %s (the presets ran out)." % (size, ", ".join(short)))


def _dream100(ctx: Ctx, week: str) -> int:
    """The Dream 100 to one caller, laid out Monday to Friday. Returns how many records it placed."""
    st = ctx.stage
    people = resolve_callers(ctx, [st.get("dream100_caller")])
    if not people:
        return 0
    who = people[0]
    title = str(st.get("dream100_list") or "Dream 100")
    per_day = max(1, int(st.get("dream100_per_day", 20)))
    list_uuid = ctx.crm.list_uuid(title)
    if not list_uuid:
        ctx.out.problem("there is no list named %s in this account, so the Dream 100 was not handed out" % title)
        return 0
    rows = ctx.crm.rows({"any_lists": [list_uuid], "must_not": {"any_property_status": GONE_STATUSES}},
                        cap=int(st.get("dream100_max", 150)))
    streets = []
    for row in rows:
        rec = ctx.crm.record(row["uuid"])
        if not ctx.passes(rec, "assign", row=row):
            continue
        if assignee_of(rec) != who["uuid"]:
            ctx.change(row["uuid"], "assign", assignee_of(rec), who["uuid"])
            ctx.write("dream 100", ctx.crm.assign, row["uuid"], who["uuid"])
            if ctx.live:
                ledger.event(ctx.run_id, st.id, row["uuid"], "book", {"to": who["name"], "to_uuid": "", "preset": title,
                                                                      "week": week, "dream100": True}, now=ctx.now, tz=ctx.policy.tz)
        streets.append(addr(rec))
    if not streets:
        return 0
    local = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    monday = local - timedelta(days=local.weekday())
    # The weekdays this week still has. A book that goes out on a Wednesday is laid over three days.
    left = [monday + timedelta(days=i) for i in range(5) if monday + timedelta(days=i) >= local]
    days = 0
    for i, day in enumerate(left):
        chunk = streets[i * per_day:(i + 1) * per_day]
        if not chunk:
            break
        ctx.write("dream 100 day", laneroutes.create_task, ctx.crm, "Dream 100: %s" % day.strftime("%A"),
                  laneroutes.end_of_day(day, ctx.policy.tz), "Today's houses, in order:\n" + "\n".join(chunk), who["uuid"])
        days += 1
    over = max(0, len(streets) - len(left) * per_day)
    ctx.out.tally["Dream 100 records with %s" % who["name"]] = len(streets)
    ctx.out.line("Dream 100: %d record(s) %s %s, on %d daily task(s) of up to %d%s." % (
        len(streets), "with" if ctx.live else "would go to", who["name"], days, per_day,
        (", %d more than this week holds" % over) if over else ""))
    return len(streets)
