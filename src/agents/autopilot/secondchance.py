"""Second chances (build plan 4.4, decision D23): a no is not forever.

The teaching puts 20 to 30% of deals in two piles everybody else walks away from: the owners who
said no and the owners who never answered. A sale date gets closer, a tenant moves out, a probate
drags on, and the owner who said no in September takes the call in November. The account already
holds those records, and nothing was calling them back.

This stage gives every Not Interested record a task to call again, on a clock set by the list the
record came from:

    30 days   a sale date ahead (a foreclosure, a tax sale)
    45 days   probate, Priority 1 and Priority 2
    90 days   code violation, tax delinquent, Priority 3 and everything else

The rules, each one from a way this goes wrong:

  * ONLY THE CORRECT NUMBER. The owner said no on one number, and that number is the only one
    anybody has confirmed is theirs. The task names that number (by its last four digits) and no
    other. A record with no number marked Correct gets no task: dialing its other numbers is cold
    calling strangers under a warm label.
  * TIMED FROM THE LAST COMPLETED RE-CALL. The first task is due one interval after the record
    became Not Interested. Each later one is due one interval after the caller COMPLETED the one
    before, so a caller who is a week behind is not handed a second task for the same owner. A
    task still open is never stacked under another.
  * IT KEEPS COMING until the owner sells, dies or asks to be left alone. The gate decides that,
    on every run. This stage may lift exactly one rule, Not Interested; nothing lifts Do Not
    Market, an opt-out, a sale, a possible death or a lead.
  * IT CREATES A TASK AND NOTHING ELSE. It does not call, text or mail anyone, it does not change
    the record's status and it does not move the record to another caller.

What it remembers is the autopilot's own (one fact per record: the open task, the last completed
re-call, the record's class), so a plan run can work out the whole schedule without writing a
thing to the account.

The quarterly postcard for these records is a mail piece, so it belongs to the mail lane: a mail
stage over a Not Interested preset with gate_allow: [not_interested]. The records that never
answered at all are the quarterly rehash file, which the dial export builds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, laneroutes, ledger
from .sources import digits
from .stages import Ctx, _list, addr, assignee_of, marks_of, mask, resolve_callers, stage_rows

log = logging.getLogger(__name__)

# (class, days before the next call, the tags or lists that put a record in it). The teaching's
# by-list table (decision D23). A record on two lists follows the shorter clock.
DEFAULT_CLASSES = (
    ("auction", 30, ("foreclosure", "tax sale", "trustee sale", "auction soon", "auction critical")),
    ("probate", 45, ("probate",)),
    ("priority 1", 45, ("priority 1",)),
    ("priority 2", 45, ("priority 2",)),
    ("code", 90, ("code violation", "condemned")),
    ("tax", 90, ("tax delinquent",)),
    ("priority 3", 90, ("priority 3",)),
)
OTHER = ("everything else", 90)
STATE = "second_chance"
CLASS_TTL = timedelta(days=30)        # how long a record's class is trusted before it is read again


def _when(text) -> datetime | None:
    """A timestamp from DataSift or from the autopilot's own notes, as an aware UTC time."""
    try:
        at = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def classes_of(stage) -> list[tuple[str, int, set]]:
    """The by-list table with the playbook's changes: `intervals` maps a class to its days (and may
    add a class), `marks` maps a class to the tags and lists that mean it."""
    days = {str(k).strip().lower(): int(v) for k, v in (stage.get("intervals") or {}).items()}
    marks = {str(k).strip().lower(): {str(x).strip().lower() for x in _list(v)} for k, v in (stage.get("marks") or {}).items()}
    out = [(name, days.get(name, d), marks.get(name, set(m))) for name, d, m in DEFAULT_CLASSES]
    known = {c[0] for c in out}
    for name, d in days.items():
        if name not in known and name not in ("other", OTHER[0]):
            out.append((name, d, marks.get(name, {name})))
    return out


def interval_for(stage, names: set) -> tuple[str, int]:
    """(class, days) for a record carrying these tags and lists: of the classes it matches, the one
    with the shortest clock. A record that matches none waits the longest."""
    hits = [(d, i, name) for i, (name, d, m) in enumerate(classes_of(stage)) if m & names]
    if hits:
        d, _, name = min(hits)
        return name, d
    days = {str(k).strip().lower(): int(v) for k, v in (stage.get("intervals") or {}).items()}
    return OTHER[0], int(days.get("other", days.get(OTHER[0], OTHER[1])))


def _said_no(ctx: Ctx, uuid: str, statuses: set) -> datetime | None:
    """The day the record became Not Interested, from the record's OWN log. The shared copy of the
    account log reaches back about six weeks; most of the owners who said no said it long before
    that, and the record's own log still holds the change."""
    newest = None
    for e in ctx.crm.record_logs(uuid, ["property.status.updated"]):
        change = ((e.get("payload") or {}).get("property") or {}).get("status")
        to = change[-1] if isinstance(change, list) and change else change
        at = activity.parse_ts(e.get("timestamp") or "")
        if at and str(to or "").strip().lower() in statuses and (newest is None or at > newest):
            newest = at
    return newest


def _since(ctx: Ctx, uuid: str, state: dict, statuses: set, budget: dict) -> datetime:
    """What the next call is timed from: the last re-call a caller completed, else the day the
    record became Not Interested (the shared log, then the record's own log, read once and
    remembered), else the day this stage first saw it. The last is a floor: it can make a call
    late, never early."""
    last = _when(state.get("last_recall")) or _when(state.get("said_no"))
    if last:
        return last
    said = [e for e in activity.events("property.status.updated", resource=uuid)
            if str(e["data"].get("to") or "").strip().lower() in statuses]
    if said:
        return said[-1]["when"]
    if not state.get("log_read") and budget["reads"] < budget["max"]:
        budget["reads"] += 1
        try:
            found = _said_no(ctx, uuid, statuses)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a record's own log (%s); it is timed from the day this stage first saw it" % str(exc)[:100])
            return ledger.first_seen(uuid, ctx.stage.id, ctx.now)
        state["log_read"] = True
        if found:
            state["said_no"] = found.isoformat(timespec="seconds")
        ledger.set_fact(uuid, STATE, state)       # what the record's log shows: knowledge, kept in plan mode too
        if found:
            return found
    return ledger.first_seen(uuid, ctx.stage.id, ctx.now)


def correct_numbers(rec: dict) -> list[dict]:
    """The numbers a person or a dialer marked Correct, and nothing else. CORRECT_DNC is the
    owner's number and a request not to be called on it, so it is not one of these. (A litigator's
    number never gets this far: the gate holds the whole record.)"""
    out = []
    for p in ((rec.get("owner") or {}).get("phones") or []):
        n = digits(p.get("number"))
        if n and str(p.get("status") or "").upper() == "CORRECT":
            out.append({"number": n, "type": str(p.get("type") or "").upper()})
    return out


def run_secondchance(ctx: Ctx) -> None:
    st = ctx.stage
    zone = ZoneInfo(ctx.policy.tz)
    statuses = {str(s).strip().lower() for s in (_list(st.get("statuses")) or ["not_interested"])}
    if st.presets:
        rows = stage_rows(ctx)
    else:
        stored = [str(s) for s in (_list(st.get("statuses")) or ["not_interested"])]
        rows = [("", r) for r in ctx.crm.rows({"any_property_status": stored}, cap=int(st.get("max_scan", 2000)))]
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No record is Not Interested."
        return
    try:
        sync = activity.sync(ctx.crm, "state", now=ctx.now, floor_days=int(st.get("floor_days", 45)),
                             max_calls=int(st.get("max_log_calls", 400)))
        if not sync["complete"]:
            ctx.out.problem("the change log is still catching up (read through %s); a record whose no is older than "
                            "the log is timed from the day this stage first saw it" % (sync["through"] or "nothing yet"))
    except Exception as exc:  # noqa: BLE001  (the stage still works from its own first-seen dates)
        ctx.out.problem("could not read the change log (%s); records are timed from the day this stage first saw them"
                        % str(exc)[:100])
    people = resolve_callers(ctx)
    shortest = min([d for _, d, _ in classes_of(st)] + [interval_for(st, set())[1]])
    max_reads = int(st.get("max_reads", 150))
    max_new = int(st.get("max_per_run", 50))
    made = 0
    budget = {"reads": 0, "max": max_reads}      # every request this run may spend on reading
    load: dict[str, int] = {}
    by_class: dict[str, int] = {}
    stamp = ctx.now.isoformat(timespec="seconds")
    recheck = timedelta(hours=float(st.get("task_check_hours", 20)))
    facts = {row["uuid"]: (ledger.fact(row["uuid"], STATE) or {}) for _, row in rows}
    # Least recently looked at first. With hundreds of open tasks and a read cap, the cap then works
    # through every record in turn instead of re-reading the same tasks at the top of the list.
    rows = sorted(rows, key=lambda tr: str(facts[tr[1]["uuid"]].get("looked") or ""))
    for _, row in rows:
        u = row["uuid"]
        state = facts[u]
        if state.get("task"):
            # A task is out. Until the caller completes it there is nothing to time the next one from,
            # and an open task read since the last run is not read again.
            looked = _when(state.get("looked"))
            if looked and ctx.now - looked < recheck:
                ctx.out.tally["the last re-call task is still open"] += 1
                continue
            if budget["reads"] >= max_reads:
                ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
                continue
            budget["reads"] += 1
            try:
                task = laneroutes.get_task(ctx.crm, state["task"])
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not read a re-call task (%s); that record waits a run" % str(exc)[:100])
                continue
            done = laneroutes.completed_at(task)
            if task is not None and not done:
                ledger.set_fact(u, STATE, dict(state, looked=stamp))      # the autopilot's own note
                ctx.out.tally["the last re-call task is still open"] += 1
                continue
            if task is None:
                # Somebody deleted the task. Nobody made the call, so the clock starts again today
                # and the owner is not handed straight back to the caller who removed it.
                ctx.out.tally["the last re-call task was deleted, timed again from today"] += 1
                done = ctx.now.isoformat(timespec="seconds")
            else:
                ctx.out.tally["re-calls completed since the last run"] += 1
            state = dict(state, task="", last_recall=(_when(done) or ctx.now).isoformat(timespec="seconds"))
            ledger.set_fact(u, STATE, state)      # what the task shows: the autopilot's own note, kept in plan mode too
        since = _since(ctx, u, state, statuses, budget)
        age = (ctx.now - since).days
        if age < shortest:
            ctx.out.tally["not due yet"] += 1
            continue
        known_at = _when(state.get("class_at"))
        if state.get("class") and known_at and ctx.now - known_at < CLASS_TTL and age < int(state.get("days") or 0):
            ctx.out.tally["not due yet"] += 1
            continue
        if made >= max_new:
            ctx.out.tally["due, left for the next run (%d tasks a run)" % max_new] += 1
            continue
        if budget["reads"] >= max_reads:
            ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
            continue
        budget["reads"] += 1
        rec = ctx.crm.record(u)
        name, days = interval_for(st, marks_of(rec))
        state = dict(state, **{"class": name, "days": days, "class_at": stamp, "looked": stamp})
        ledger.set_fact(u, STATE, state)          # the record's class: knowledge, kept in plan mode too
        if age < days:
            ctx.out.tally["not due yet"] += 1
            continue
        # The one rule this stage lifts. Everything else the gate holds still holds, and a held
        # record is counted under its reason.
        if not ctx.passes(rec, "task", row=row, allow={"not_interested"}):
            continue
        numbers = correct_numbers(rec)
        if not numbers:
            ctx.out.tally["no number is marked Correct, so there is nobody confirmed to call"] += 1
            continue
        who = laneroutes.task_person(assignee_of(rec), people, load)
        if not who:
            ctx.out.problem("no active caller to give a re-call task to, so nothing was created")
            break
        today = ctx.now.astimezone(zone).date()
        last4 = " and ".join(mask(n["number"]) for n in numbers[:2])
        title = str(st.get("title") or "Second chance call: {street}").format(street=addr(rec))
        notes = ("Said no on %s (%d days ago). Call only the number %s, the one marked Correct. "
                 "The next call is due %d days after you complete this task." % (
                     laneroutes.nice_day(since.astimezone(zone).date()), age, last4, days))
        res = ctx.write("re-call task", laneroutes.create_task, ctx.crm, title,
                        laneroutes.end_of_day(laneroutes.business_day(today), ctx.policy.tz), notes, who["uuid"], u)
        if ctx.live:
            task_id = str((res or {}).get("uuid") or "")
            if not task_id:
                ctx.out.problem("DataSift returned no task id for %s; no more tasks were created this run" % addr(rec))
                break
            ledger.change(ctx.run_id, st.id, u, "task", None, task_id)
            ledger.set_fact(u, STATE, dict(state, task=task_id, created=ctx.now.isoformat(timespec="seconds"),
                                           n=int(state.get("n") or 0) + 1))
            ledger.event(ctx.run_id, st.id, u, "second_chance", {"task": task_id, "to": who["name"], "class": name,
                                                                 "days": days}, now=ctx.now, tz=ctx.policy.tz)
        load[who["uuid"]] = load.get(who["uuid"], 0) + 1
        by_class[name] = by_class.get(name, 0) + 1
        made += 1
        ctx.out.line("%s: said no %d days ago (%s, every %d days), call the number %s -> %s" % (
            addr(rec), age, name, days, last4, who["name"]))
    ctx.out.acted = made
    mix = ", ".join("%s %d" % kv for kv in sorted(by_class.items(), key=lambda kv: -kv[1]))
    ctx.out.head = "%d Not Interested record(s): %s %d re-call task(s)%s; %d not due yet." % (
        len(rows), "created" if ctx.live else "would create", made, (" (%s)" % mix) if mix else "",
        ctx.out.tally.get("not due yet", 0))
