"""The lead clock (build plan 2.5) and the speed-to-lead clock (the clock half of 2.2).

A lead that nobody moves sits where it was dropped. On 2026-11-12 the account held 68 leads: 13
still read New Lead after a day, and the median wait from New Lead to the first call was 18.8
hours. Cards do not move on their own; a sequence fires only when a person drags one. This stage
is the clock those cards never had.

TIME IS MEASURED FROM THE LAST REAL CONVERSATION, never from time in a column. A real conversation
is an answered call of a minute or more, an inbound text, or a note a person wrote. The reason is
measured: on one caller's day the dialer logged 185 "answered" calls and 6 conversations of a
minute or more. A voicemail greeting answers. Counting any answer as contact would keep a ghosting
lead "hot" forever, and counting time in a column would demote a lead the manager spoke to
yesterday because its card had not been dragged. A card's time in its column is only the floor:
a lead is never quieter than the day a person last placed it.

The rules (decision D15, each one a setting under `limits`):

    New Lead    to No Contact  after 1 day unreached, once it has been dialed at least once
    No Contact  to Nurture     after 5 daily tries with no conversation
    Nurture     to Ghosting    after 6 months (2 for a lead tagged marginal), or 31 touches
    Hot         to Warm        after 14 quiet days
    Warm        to Cold        after 60 quiet days
    Cold        to Ghosting    after a year
    Offer Rejected             back to the lead manager after a week

What the stage will not do:
  * MOVE A CARD THAT HOLDS A FUTURE TASK A PERSON DATED. Every move clears the record's tasks, so
    "call her back on the 3rd" would be lost. A task a sequence made is not a person's date; a
    task nobody can explain is treated as one.
  * MOVE ON A GUESS. The log copy keeps 200 days. A limit longer than the log can prove waits, and
    the stage says how many cards are waiting, unless `trust_column_time` is set.
  * MOVE ANYTHING WHILE THE LOG IS BEHIND. "No conversation in the log" means nothing when the log
    has not been read up to today.
  * SET A RECORD TO NEW LEAD. That status starts the whole intake again.

A move over the API may or may not fire the board's own sequences (`sequences_fire`: true, false,
or unknown, the default). Unknown moves the card and looks again on the next run: if the task the
sequence would have created is not there, the stage sets the status and creates the task itself,
and keeps count of how often that was needed, which is the evidence for settling the setting.

The first live run is staged for a person to release, like the attempt keeper's: it corrects
months of unmoved cards at once.

The speed-to-lead job (`job: speed`) is the same clock at five-minute grain: from the status
change to the first outbound call, with the assignee told at 5 minutes and the owner at 15.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity, budget, lanes, ledger
from . import leadroutes as routes
from .stages import Ctx, Summary, addr, assignee_of, tag_titles

log = logging.getLogger(__name__)

ANSWERED = ("owner.call.answered", "owner.call.received")
INBOUND_TEXT = "owner.sms.received"
OUTBOUND_TEXT = "owner.sms.sent"

LIMITS = {
    "new_lead_hours": 24.0,
    "new_lead_needs_dial": True,
    "no_contact_tries": 5,
    "nurture_strong_days": 180,
    "nurture_marginal_days": 60,
    "nurture_touches": 31,
    "hot_quiet_days": 14,
    "warm_quiet_days": 60,
    "cold_quiet_days": 365,
    "offer_rejected_days": 7,
}
NEXT = {"new_lead": "no_contact", "no_contact": "nurture", "nurture": "ghosting", "hot": "warm", "warm": "cold",
        "cold": "ghosting", "offer_rejected": "send_back"}
QUIET_KEY = {"hot": "hot_quiet_days", "warm": "warm_quiet_days", "cold": "cold_quiet_days"}
LABEL = {"new_lead": "New Lead", "no_contact": "No Contact", "nurture": "Nurture", "hot": "Hot", "warm": "Warm",
         "cold": "Cold", "ghosting": "Ghosting", "offer_rejected": "Offer Rejected",
         "send_back": "the lead manager", "needs_review": "Needs Review"}
# The log copy drops rows older than activity.KEEP_DAYS, so no limit can be proven past this.
PROVABLE_MAX = activity.KEEP_DAYS - 10
TEXT_PROOF_DAYS = 45
EVIDENCE_KV = "autopilot:leadclock:fired"


def _approved_key(stage_id: str) -> str:
    return "autopilot:leadclock:approved:%s" % stage_id


def limits(stage) -> dict:
    out = dict(LIMITS)
    for k, v in (stage.get("limits") or {}).items():
        if k in out and v is not None:
            out[k] = v
    return out


def fire_mode(stage) -> str:
    """true, false or unknown. YAML reads a bare true or false as a boolean, so both spellings count."""
    v = stage.get("sequences_fire", "unknown")
    if v is True:
        return "true"
    if v is False:
        return "false"
    v = str(v).strip().lower()
    return v if v in ("true", "false") else "unknown"


# ---------------------------------------------------------------------- the shared log copy
def sync_logs(ctx: Ctx, groups=("state", "calls", "texts", "board", "notes")) -> bool:
    """Bring the log groups a lead stage reads up to now. False means at least one is behind, and
    the caller must not read silence in the log as silence from the seller."""
    ok = True
    for g in groups:
        if g not in activity.GROUPS:
            continue
        try:
            s = activity.sync(ctx.crm, g, now=ctx.now, floor_days=int(ctx.stage.get("floor_days", activity.FLOOR_DAYS)),
                              max_calls=int(ctx.stage.get("max_log_calls", 200)))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read the %s log (%s); check the brain's DataSift key" % (g, str(exc)[:100]))
            ok = False
            continue
        if not s["complete"]:
            ctx.out.problem("the %s log is still catching up (read through %s); the next run continues" % (
                g, s["through"] or "nothing yet"))
            ok = False
    return ok


def log_floor() -> datetime | None:
    """The oldest call the log copy holds: how far back "no conversation" can be proven."""
    marks = ",".join("?" * len(activity.CALLS))
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT MIN(at) AS at FROM ap_log WHERE event_type IN (%s)" % marks, list(activity.CALLS)).fetchone()
    return datetime.fromisoformat(r["at"]) if r and r["at"] else None


def last_conversation(owner_uuid: str, record_uuid: str = "", since: datetime | None = None, seconds: int = 60,
                      notes: bool = True) -> tuple[datetime | None, str]:
    """(when, how) of the last REAL conversation with an owner: an answered call of `seconds` or
    more (either direction), an inbound text, or a note a person wrote. How is call, text or note.

    Why not "the last answered call": a voicemail greeting answers. On 2026-11-12 one caller's day
    held 185 answers and 6 calls of a minute or more. Why a note counts: a walk-in, a call from a
    personal phone and a talk at the door never reach the call log, and the lead manager's note is
    the only trace. Notes and tasks a machine wrote (a sequence, an import, this autopilot's own
    key) are not conversations. Calls and texts are filed under the OWNER, so a talk about the
    owner's other house counts too: it is the same person."""
    best, how = None, ""
    if owner_uuid:
        for e in activity.events(ANSWERED, resource=owner_uuid, since=since):
            if int(e["data"].get("duration") or 0) >= seconds and (best is None or e["when"] > best):
                best, how = e["when"], "call"
        for e in activity.events(INBOUND_TEXT, resource=owner_uuid, since=since):
            if e["data"].get("direction", "inbound") != "outbound" and (best is None or e["when"] > best):
                best, how = e["when"], "text"
    if notes:
        for res in (record_uuid, owner_uuid):
            if not res:
                continue
            for e in activity.events(routes.NOTE_EVENTS, resource=res, since=since):
                if e.get("author") == "user" and str(e.get("source") or "") not in routes.MACHINE_SOURCES \
                        and (best is None or e["when"] > best):
                    best, how = e["when"], "note"
    return best, how


def texts_reach_the_log(now: datetime) -> bool:
    """Has any inbound text reached the log copy lately? Calls always do. Texts depend on the texting
    tool: one of ours writes replies to DataSift and one did not when this was written, so a stage
    that leans on "no reply by text" checks that such a thing can be seen at all."""
    return bool(activity.events(INBOUND_TEXT, since=now - timedelta(days=TEXT_PROOF_DAYS), limit=1))


def touch_days(owner_uuid: str, since: datetime, tz: str) -> int:
    """Days on which the owner was dialed or texted since a moment. A touch is a day, as an attempt
    is: three dials in one afternoon are one touch."""
    zone = ZoneInfo(tz)
    days = set(activity.dial_days(owner_uuid, since=since, tz=tz))
    for e in activity.events(OUTBOUND_TEXT, resource=owner_uuid, since=since):
        days.add(e["when"].astimezone(zone).date().isoformat())
    return len(days)


# ---------------------------------------------------------------------- the rules, as one function
def _quiet(now: datetime, entered: datetime, conversation: datetime | None, reach: datetime | None):
    """(days quiet as best known, the days of that the log proves, the moment the quiet began)."""
    since = max(entered, conversation) if conversation else entered
    days = (now - since).total_seconds() / 86400.0
    if conversation is not None or (reach is not None and entered >= reach):
        proven = days            # the whole stretch lies inside the log copy
    elif reach is None:
        proven = 0.0
    else:
        proven = min(days, (now - reach).total_seconds() / 86400.0)
    return days, proven, since


def judge(phase: str, now: datetime, lim: dict, *, entered: datetime, conversation: datetime | None = None,
          became: datetime | None = None, dial_days: int = 0, touches: int = 0, marginal: bool = False,
          reach: datetime | None = None, trust_column_time: bool = False) -> tuple[str, str]:
    """(the phase the card should move to, why), or ("", note). A note is empty when the card is
    simply not due; "not dialed" and a note starting "wait" are worth telling a person.

    entered       when the card entered its column (the floor: a lead is never quieter than that)
    conversation  the last real conversation with the owner, if the log copy holds one
    became        when the record became a new lead, for the New Lead rule
    dial_days     days with an outbound dial since the card entered (No Contact), or since `became`
    touches       days with a dial or a text since the quiet began (Nurture)
    reach         the oldest call the log copy holds: nothing older can be proven
    """
    if phase == "new_lead":
        start = became or entered
        if conversation and conversation >= start:
            return "", ""
        waited = (now - start).total_seconds()
        if waited < float(lim["new_lead_hours"]) * 3600:
            return "", ""
        if lim.get("new_lead_needs_dial", True) and dial_days < 1:
            return "", "not dialed"
        return "no_contact", "unreached %s after becoming a lead" % routes.span(waited)
    if phase == "no_contact":
        if conversation and conversation >= entered:
            return "", ""
        if dial_days >= int(lim["no_contact_tries"]):
            return "nurture", "%d days of calls and never reached" % dial_days
        return "", ""
    if phase == "offer_rejected":
        days = (now - entered).total_seconds() / 86400.0
        if days >= float(lim["offer_rejected_days"]):
            return "send_back", "offer rejected %d days ago" % int(days)
        return "", ""
    if phase in ("nurture", "hot", "warm", "cold"):
        days, proven, _since = _quiet(now, entered, conversation, reach)
        if phase == "nurture":
            if touches >= int(lim["nurture_touches"]):
                return "ghosting", "%d touches with no conversation" % touches
            need = float(lim["nurture_marginal_days"] if marginal else lim["nurture_strong_days"])
        else:
            need = float(lim[QUIET_KEY[phase]])
        if days < need:
            return "", ""
        if proven >= min(need, PROVABLE_MAX) or trust_column_time:
            return NEXT[phase], "quiet %d days" % int(days)
        return "", "wait: quiet %d days by the card's time in its column, and the log proves %d" % (int(days), int(proven))
    return "", ""


# ---------------------------------------------------------------------- reading the board
def entered_at(ctx: Ctx, u: str, card: str, column: str, board: routes.Board, reads: dict) -> datetime | None:
    """When the card entered the column it is in now: the log copy's card event when it names this
    column, else the card's own timeline (one read, remembered while the card stays put)."""
    e = routes.last_card_event(u, board)
    if e is not None and routes.same_column(e["data"].get("column"), column, board):
        return e["when"]
    known = ledger.fact(u, "card_time")
    if isinstance(known, dict) and known.get("card") == card and known.get("column") == column and known.get("entered"):
        return datetime.fromisoformat(known["entered"])
    if reads["n"] >= reads["max"]:
        return None
    reads["n"] += 1
    try:
        tl = routes.timeline(ctx.crm, column, card)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read a card's timeline (%s)" % str(exc)[:100])
        return None
    secs = (tl.get(column) or {}).get("seconds")
    if secs is None:
        return None
    when = ctx.now - timedelta(seconds=float(secs))
    ledger.set_fact(u, "card_time", {"card": card, "column": column, "entered": when.isoformat(timespec="seconds")})
    return when


def person_dated(ctx: Ctx, u: str, card: str, future_cards: set | None, reads: dict) -> str:
    """Why this card must not be moved because of a task a person dated ('' when it may be).

    The board says whether the card holds a task due in the future; the log copy says who made each
    open task. A sequence-made task is read once more before the move, because a person can re-date
    it in the app and no log row says so."""
    now = ctx.now
    ours = routes.our_tasks(u)
    future = [t for t in routes.open_tasks(u) if t["due"] and t["due"] > now]
    for t in future:
        if not routes.made_by_machine(t, ours):
            what = "an appointment" if t["kind"] == "appointment" else "a task a person dated"
            return "%s for %s" % (what, routes.nice_day(t["due"], ctx.policy.tz))
    for t in future:
        if reads["n"] >= reads["max"]:
            return "a future task that could not be re-read this run"
        reads["n"] += 1
        try:
            cur = routes.get_task(ctx.crm, t["task"])
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001  (deleted since the log row: nothing left to lose)
            continue
        due = routes.parse_dt(cur.get("due_date"))
        if cur.get("completed") or not due:
            continue
        if abs((due - t["due"]).total_seconds()) > 90 and due > now:
            return "a task a person moved to %s" % routes.nice_day(due, ctx.policy.tz)
    if future_cards is not None and card in future_cards and not future:
        return "a future task the log cannot explain"
    return ""


def find_due(ctx: Ctx, ph: dict, lim: dict) -> tuple[list[dict], dict]:
    """Every card past its limit that may be moved, and the boards they sit on. Reads the columns
    that have a rule, never the whole account."""
    st = ctx.stage
    tz = ctx.policy.tz
    conv = int(st.get("conversation_seconds", 60))
    trust = bool(st.get("trust_column_time", False))
    marginal_tags = {str(t).lower() for t in (st.get("marginal_tags") or ["Marginal Lead"])}
    boards: dict[str, routes.Board] = {}
    lead = routes.stage_board(ctx, "lead", ph)
    if lead.columns:
        boards["lead"] = lead
    if st.get("rejected_offers", True):
        # The offers board holds one rule. Not knowing its columns must not read as the clock being
        # broken, so it is said in a line, not as a problem.
        said = list(ctx.out.problems)
        offers = routes.stage_board(ctx, "offers", ph)
        if offers.columns:
            boards["offers"] = offers
        elif len(ctx.out.problems) > len(said):
            ctx.out.problems[:] = said
            ctx.out.line("Rejected offers were not timed: the %s board's columns are not known (set seed_columns)" % offers.title)
    if not boards:
        return [], boards
    statuses = [str(s) for s in (st.get("statuses") or lanes.LEAD_STATUS_VALUES)]
    in_lane = {s.strip().lower() for s in statuses}
    rows = {r["uuid"]: r for r in routes.status_rows(ctx, statuses, cap=int(st.get("max_scan", 2000)))}
    index = routes.card_index()
    reach = log_floor()
    reads = {"n": 0, "max": int(st.get("max_reads", 120))}
    due: list[dict] = []
    not_dialed: list[str] = []
    for role, board in boards.items():
        cols = {p: u for p, u in board.phase_columns(ph, role).items() if p in NEXT}
        missing = [ph[p]["column"] for p in NEXT if ph[p]["board"] == role and p not in cols]
        if missing:
            ctx.out.problem("the %s board has no column named %s; name it under `columns` on the stage" % (
                board.title, ", ".join(missing)))
        where, unknown = routes.cards_by_record(ctx.crm, list(cols.values()), cap=int(st.get("max_cards", 500)), index=index)
        if unknown:
            ctx.out.tally["cards whose record the log cannot name (left alone)"] += unknown
        by_col = {u: p for p, u in cols.items()}
        for u, w in where.items():
            ctx.out.found += 1
            row = rows.get(u)
            if row is None:
                ctx.out.tally["cards whose record is not in a lead status (left alone)"] += 1
                continue
            phase = by_col[w["column"]]
            owner = (row.get("owner") or {}).get("uuid") or ""
            entered = entered_at(ctx, u, w["card"], w["column"], board, reads)
            if entered is None:
                ctx.out.tally["cards not timed this run (read cap, next run)"] += 1
                continue
            talked, _how = last_conversation(owner, u, seconds=conv)
            became = None
            if phase == "new_lead":
                ev = [e for e in activity.events("property.status.updated", resource=u)
                      if str(e["data"].get("to") or "").lower() == "new_lead"]
                became = ev[-1]["when"] if ev else None
            start = became or entered
            dials = len(activity.dial_days(owner, since=start, tz=tz)) if owner and phase in ("new_lead", "no_contact") else 0
            touches = 0
            if phase == "nurture" and owner:
                touches = touch_days(owner, max(entered, talked) if talked else entered, tz)
            kw = dict(entered=entered, conversation=talked, became=became, dial_days=dials, touches=touches,
                      reach=reach, trust_column_time=trust)
            to, why = judge(phase, ctx.now, lim, **kw)
            rec = None
            if not to and phase == "nurture" and judge(phase, ctx.now, lim, marginal=True, **kw)[0]:
                # Past the two-month limit and short of the six-month one: only a lead tagged
                # marginal moves, and the tag is on the full record.
                if reads["n"] >= reads["max"]:
                    ctx.out.tally["cards not checked this run (read cap, next run)"] += 1
                    continue
                reads["n"] += 1
                rec = ctx.crm.record(u)
                if {t.lower() for t in tag_titles(rec)} & marginal_tags:
                    to, why = judge(phase, ctx.now, lim, marginal=True, **kw)
                    why += " (a marginal lead)"
            if why == "not dialed":
                not_dialed.append(addr(row))
                continue
            if why.startswith("wait"):
                ctx.out.tally["past their limit by the card's own time, waiting on the log to prove it"] += 1
                continue
            if not to:
                continue
            to_role = ph[to]["board"]
            to_col = (boards[to_role].find(ph[to]["column"]) if to_role in boards else "")
            if not to_col:
                ctx.out.problem("%s is due to move to %s, and the %s board has no such column" % (
                    addr(row), ph[to]["column"], routes.board_title(st, to_role)))
                continue
            due.append({"record_uuid": u, "owner_uuid": owner, "street": addr(row), "card": w["card"], "role": role,
                        "from": phase, "to": to, "from_col": w["column"], "to_col": to_col, "why": why,
                        "status": routes.stored_status(row), "_row": row, "_rec": rec})
    if not_dialed:
        ctx.out.problem("%d new lead(s) have waited a day and were never dialed: call them (%s)" % (
            len(not_dialed), ", ".join(not_dialed[:3])))
    if not due:
        return [], boards
    # Only now, and only for the columns that hold a card due to move: which cards carry a future task.
    future: dict[str, set] = {}
    if st.get("future_task_filter", True):
        for col in {c["from_col"] for c in due}:
            future[col] = {str(c.get("uuid") or "") for c in routes.cards(ctx.crm, col, cap=int(st.get("max_cards", 500)),
                                                                          due_after=ctx.now)}
    keep = []
    for c in due:
        if c["_rec"] is None:
            if reads["n"] >= reads["max"]:
                ctx.out.tally["due to move, not checked this run (read cap, next run)"] += 1
                continue
            reads["n"] += 1
            c["_rec"] = ctx.crm.record(c["record_uuid"])
        if lanes.status_of(c["_rec"]) not in in_lane:
            # The search index trails the record: its status left the lead pipeline a minute ago.
            ctx.out.tally["cards whose record is not in a lead status (left alone)"] += 1
            continue
        c["status"] = routes.stored_status(c["_rec"])      # the record's own, not the search row's: it is the value undo puts back
        # A move creates a call task (the column's sequence does), so the gate's task rule applies.
        if not ctx.passes(c["_rec"], "task", row=c["_row"]):
            continue
        held = person_dated(ctx, c["record_uuid"], c["card"], future.get(c["from_col"]), reads)
        if held:
            group = ("a task a person re-dated" if " moved to " in held else held.split(" for ")[0])
            ctx.out.tally["left where they are: %s" % group] += 1
            ctx.out.line("%s: past its limit and not moved, it holds %s" % (c["street"], held))
            continue
        keep.append(c)
    return keep, boards


# ---------------------------------------------------------------------- the sequence's own work
def _delete_if_there(crm, task_uuid: str) -> bool:
    """Delete a task that the log copy still shows as open. Somebody may have completed or deleted
    it in the minutes since the log was read; that is the outcome wanted, not an error."""
    try:
        routes.delete_task(crm, task_uuid)
        return True
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001
        return False


def follow_on(ctx: Ctx, u: str, to: str, ph: dict, boards: dict, card: str = "", column: str = "",
              status_now: str | None = "?") -> list[str]:
    """What the board's sequence does when a card lands in a column, done here because the API move
    did not fire it: the old column's sequence-made tasks go, the status is set, the column's task
    is created, and a card sent back to the lead manager lands in Needs Review. Returns what was
    done, in words. Never sets New Lead."""
    st = ctx.stage
    row = ph[to]
    did = []
    if st.get("clear_old_tasks", True):
        ours = routes.our_tasks(u)
        for t in routes.open_tasks(u):
            if t["kind"] != "appointment" and routes.made_by_machine(t, ours):
                ctx.change(u, "task_delete", {"task": t["task"], "title": t["title"],
                                              "due": routes.iso_z(t["due"]) if t["due"] else ""}, None)
                if ctx.write("clear the old column's task", _delete_if_there, ctx.crm, t["task"]) is not False:
                    did.append("cleared %s" % (t["title"] or "a task"))
    want = row.get("status") or ""
    if want and want.lower() == "new_lead":
        ctx.out.problem("refused to set New Lead on a record that is already a lead; check `follow_on` on the stage")
        want = ""
    if want:
        if status_now == "?":                     # not handed in: read it, in the account's own spelling
            status_now = routes.stored_status(ctx.crm.record(u))
        if (status_now or "").strip().lower() != want.lower():
            ctx.change(u, "status", status_now, want)
            ctx.write("status", ctx.crm.set_status, u, want)
            did.append("status %s" % want)
    title = row.get("preset") or ""
    if title:
        pid = routes.preset_id(ctx.crm, title)
        if not pid:
            ctx.out.problem("there is no task preset titled %r; the lead in %s has no task (set `follow_on` on the stage)" % (
                title, row.get("column")))
        else:
            ctx.change(u, "task_create", None, {"preset": title})
            r = ctx.write("create the column's task", routes.create_task_by_preset, ctx.crm, pid, u, ctx.now, ctx.policy.tz)
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "lead_task", {"preset": title, "task": routes.task_id(r), "why": "follow on"},
                             now=ctx.now, tz=ctx.policy.tz)
            did.append("task %s" % title)
    then = row.get("then") or ""
    if then and card and column:
        role = ph[then]["board"]
        dest = boards[role].find(ph[then]["column"]) if role in boards else ""
        if dest:
            cross = role != row.get("board")
            board_uuid = routes.ensure_board_id(ctx.crm, boards[role]) if cross else ""
            home = routes.ensure_board_id(ctx.crm, boards[row["board"]]) if cross and row.get("board") in boards else ""
            ctx.change(u, "card_move", {"card": card, "column": column, "board": home}, {"card": card, "column": dest})
            ctx.write("move card on", routes.move_card, ctx.crm, column, card, dest, board_uuid)
            did.append("card to %s" % ph[then]["column"])
        else:
            ctx.out.problem("could not land a card in %s: that column was not found" % ph[then]["column"])
    return did


def _pending() -> list[tuple[str, dict]]:
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key='clock_move'").fetchall()
    out = []
    for r in rows:
        try:
            v = json.loads(r["value"])
        except ValueError:
            continue
        if isinstance(v, dict) and v.get("to") and not v.get("done"):
            out.append((r["record_uuid"], v))
    return out


def _fired(u: str, to: str, at: datetime, ph: dict) -> bool:
    """Did the column's sequence do its work after the move? Read from the log copy."""
    since = at - timedelta(seconds=5)
    row = ph[to]
    if row.get("preset"):
        return any(e.get("source") == "sequence" or e["data"].get("by")
                   for e in activity.events("task.created", resource=u, since=since))
    if row.get("status"):
        return any(str(e["data"].get("to") or "").lower() == row["status"].lower()
                   for e in activity.events("property.status.updated", resource=u, since=since))
    return True


def settle(ctx: Ctx, ph: dict, boards: dict) -> None:
    """Look again at the cards moved on an earlier run (sequences_fire unknown): where the sequence
    did not do its work, do it now, and count both outcomes."""
    wait = timedelta(minutes=float(ctx.stage.get("settle_minutes", 30)))
    tally = routes._json(store.kv_get(EVIDENCE_KV))
    fired = int(tally.get("fired") or 0)
    missed = int(tally.get("missed") or 0)
    for u, v in _pending():
        at = routes.parse_dt(v.get("at"))
        if at is None or ctx.now - at < wait:
            continue
        if _fired(u, v["to"], at, ph):
            fired += 1
            ledger.set_fact(u, "clock_move", dict(v, done="the sequence fired"))
            continue
        did = follow_on(ctx, u, v["to"], ph, boards, card=v.get("card") or "", column=v.get("column") or "")
        ctx.out.tally["moves whose sequence did not fire"] += 1
        ctx.out.line("%s an earlier move for %s: %s" % ("finished" if ctx.live else "would finish", v.get("street") or u[:8],
                                                        ", ".join(did) or "nothing left to do"))
        if ctx.live:
            missed += 1
            ledger.set_fact(u, "clock_move", dict(v, done="done by the stage"))
    if fired + missed:
        store.kv_set(EVIDENCE_KV, json.dumps({"fired": fired, "missed": missed}))
        ctx.out.tally["API moves that fired the board's sequence so far"] = fired
        ctx.out.tally["API moves that did not, so far"] = missed


# ---------------------------------------------------------------------- moving
def apply_move(ctx: Ctx, c: dict, ph: dict, boards: dict, state: dict) -> str:
    st = ctx.stage
    u = c["record_uuid"]
    cross = home = ""
    if ph[c["to"]]["board"] != c["role"]:
        cross = routes.ensure_board_id(ctx.crm, boards[ph[c["to"]]["board"]])
        home = routes.ensure_board_id(ctx.crm, boards[c["role"]])
    ctx.change(u, "card_move", {"card": c["card"], "column": c["from_col"], "phase": c["from"], "board": home},
               {"card": c["card"], "column": c["to_col"], "phase": c["to"]})
    ctx.write("move card", routes.move_card, ctx.crm, c["from_col"], c["card"], c["to_col"], cross)
    if not ctx.live:
        return "moved"
    if not state.get("verified"):
        state["verified"] = True
        if not routes.card_in_column(ctx.crm, c["to_col"], c["card"]):
            state["broken"] = True
            ctx.out.problem("the card for %s did not move (it is not in %s after the write); nothing more was moved. "
                            "Move one card by hand and check the column ids" % (c["street"], ph[c["to"]]["column"]))
            return "failed"
    ledger.event(ctx.run_id, st.id, u, "card_move", {"from": c["from"], "to": c["to"], "why": c["why"]},
                 now=ctx.now, tz=ctx.policy.tz)
    mode = fire_mode(st)
    if mode == "false":
        follow_on(ctx, u, c["to"], ph, boards, card=c["card"], column=c["to_col"], status_now=c.get("status"))
    elif mode == "unknown":
        ledger.set_fact(u, "clock_move", {"to": c["to"], "from": c["from"], "at": ctx.now.isoformat(timespec="seconds"),
                                          "card": c["card"], "column": c["to_col"], "street": c["street"]})
    return "moved"


def _mix(cands: list[dict]) -> str:
    by = Counter("%s to %s" % (LABEL.get(c["from"], c["from"]), LABEL.get(c["to"], c["to"])) for c in cands)
    return ", ".join("%d %s" % (n, k) for k, n in by.most_common())


def _item(c: dict) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def run_moves(ctx: Ctx) -> None:
    st = ctx.stage
    ph = routes.phases(st)
    lim = limits(st)
    synced = sync_logs(ctx)
    if not synced:
        ctx.out.head = "The log is behind, so no card was judged or moved this run."
        return
    cands, boards = find_due(ctx, ph, lim)
    if boards:
        settle(ctx, ph, boards)
    if not texts_reach_the_log(ctx.now):
        ctx.out.tally["no inbound text in the log copy: a conversation by text does not count toward the clock yet"] = 1
    for c in cands:
        ctx.out.line("%s: %s to %s, %s" % (c["street"], LABEL.get(c["from"], c["from"]), LABEL.get(c["to"], c["to"]), c["why"]))
    if not boards:
        ctx.out.head = "The board's columns are not known yet, so no card was judged."
        return
    if not cands:
        ctx.out.head = "No card is past its limit (%d on the clock)." % ctx.out.found
        return
    max_moves = int(st.get("max_moves", 25))
    if not ctx.live:
        ctx.out.acted = len(cands)
        ctx.out.head = "Would move %d card(s): %s." % (len(cands), _mix(cands))
        return
    if st.get("first_run", "staged") == "staged" and not store.kv_get(_approved_key(st.id)):
        staged = ledger.staged_record_uuids(st.id)
        items = [_item(c) for c in cands if c["record_uuid"] not in staged]
        if items:
            bid = ledger.batch_create(st.id, "leadclock", items, 0)
            ctx.out.batch = bid
            ctx.out.acted = len(items)
            ctx.out.head = ("First run: %d card(s) are past their limit (%s). Nothing was moved. They are staged as batch %d "
                            "for a person to approve: python src/agents/cli.py autopilot release %d --commit"
                            % (len(items), _mix(items), bid, bid))
        else:
            ctx.out.head = "The first set of moves is still staged and waits on a person (%d card(s))." % len(staged)
        return
    state: dict = {}
    moved = []
    for c in cands:
        if state.get("broken"):
            break
        if len(moved) >= max_moves:
            ctx.out.tally["left for the next run (cap %d a run)" % max_moves] += 1
            continue
        if apply_move(ctx, c, ph, boards, state) == "moved":
            moved.append(c)
    ctx.out.acted = len(moved)
    if moved:
        ctx.out.head = "Moved %d card(s): %s." % (len(moved), _mix(moved))
    else:
        ctx.out.head = "No card was moved: the first move did not hold, so the run stopped there."
    if fire_mode(st) == "unknown" and moved:
        ctx.out.head += " The next run checks that each column's sequence did its work."


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, now: datetime | None = None) -> dict:
    """Approve the staged first set of moves. Every card is judged again from scratch first (a lead
    that was called since, or was given a dated task, stays put), and from then on the stage moves
    cards on its own. Dry unless commit."""
    from . import policy as policy_mod
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    if b["kind"] != "leadclock":
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    items = b["items"]
    if not commit:
        return {"batch": batch_id, "cards": len(items), "dry": True, "moves": _mix(items)}
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind="leadclock", owner=st.owner, mode=st.mode, live=True)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=True, out=out)
    res: Counter = Counter()
    ph = routes.phases(st)
    if not sync_logs(ctx):
        return {"batch": batch_id, "results": {}, "problems": out.problems + ["nothing was moved: the log is behind"]}
    cands, boards = find_due(ctx, ph, limits(st))
    by = {c["record_uuid"]: c for c in cands}
    state: dict = {}
    for it in items:
        c = by.get(it.get("record_uuid") or "")
        if state.get("broken"):
            res["not moved (stopped)"] += 1
        elif c is None or c["to"] != it.get("to"):
            res["no longer due"] += 1
        else:
            res[apply_move(ctx, c, ph, boards, state)] += 1
    ok = not state.get("broken")
    ledger.batch_set(batch_id, "sent" if ok else "failed", {"results": dict(res), "problems": out.problems})
    if ok:
        store.kv_set(_approved_key(st.id), store.now())
        # Later mornings may have staged more cards before anyone released the first set. The
        # stage moves cards on its own from here, so those lists are closed, not left waiting.
        for other in ledger.batches(status="staged", stage=st.id):
            if other["kind"] == "leadclock" and other["id"] != batch_id:
                ledger.batch_set(other["id"], "cancelled", {"why": "batch %d was approved; the stage now moves cards itself" % batch_id})
    return {"batch": batch_id, "run": ctx.run_id, "results": dict(res), "problems": out.problems}


# ---------------------------------------------------------------------- speed to lead (plan 2.2, the clock half)
def business_open(when: datetime, pol) -> datetime:
    """The moment somebody could first act on a lead that arrived at `when`: `when` itself inside
    business hours, else the next opening. A 5-minute ping at 11pm reaches nobody."""
    if pol.in_business_hours(when):
        return when
    zone = ZoneInfo(pol.tz)
    local = when.astimezone(zone)
    for add in range(0, 9):
        day = (local + timedelta(days=add)).replace(hour=0, minute=0, second=0, microsecond=0)
        opening = day + timedelta(minutes=pol.hours[0])
        if opening.weekday() in pol.days and opening > local:
            return opening.astimezone(timezone.utc)
    return when


def _lead_info(ctx: Ctx, u: str, reads: dict) -> dict | None:
    info = ledger.fact(u, "lead_owner", max_age=timedelta(days=7))
    if isinstance(info, dict) and info.get("owner"):
        return info
    if reads["n"] >= reads["max"]:
        return None
    reads["n"] += 1
    rec = ctx.crm.record(u)
    info = {"owner": (rec.get("owner") or {}).get("uuid") or "", "street": addr(rec), "assignee": assignee_of(rec)}
    ledger.set_fact(u, "lead_owner", info)        # who to look for in the call log: knowledge, kept in plan mode too
    return info


def first_call(owner_uuid: str, start: datetime, until: datetime | None = None) -> tuple[datetime | None, bool]:
    """(when the owner was first called after `start`, whether the status was set ON a call). A
    status set during or right after a call has no wait to measure."""
    calls = [d for d in activity.events(activity.CALLS, resource=owner_uuid, since=start - timedelta(minutes=20), until=until)
             if d["event_type"] != "owner.call.missed"]
    if any(d["when"] <= start for d in calls):
        return None, True
    for d in calls:
        if d["data"].get("direction") != "inbound" or d["event_type"] == "owner.call.received":
            return d["when"], False
    return None, False


def run_speed(ctx: Ctx) -> None:
    st = ctx.stage
    # This clock is measured in minutes, so it may use the part of each minute's request budget that
    # is held back for urgent work. The flag is put back: one client can serve several stages.
    was = getattr(ctx.crm, "priority", None)
    if was is not None:
        ctx.crm.priority = True
    try:
        _speed(ctx, st)
    finally:
        if was is not None:
            ctx.crm.priority = was


def _speed(ctx: Ctx, st) -> None:
    if not sync_logs(ctx, ("state", "calls")):
        ctx.out.head = "The log is behind, so the speed-to-lead clock was not read this run."
        return
    window = timedelta(hours=float(st.get("window_hours", 24)))
    a_min, o_min = float(st.get("assignee_minutes", 5)), float(st.get("owner_minutes", 15))
    owner_label = str(st.get("owner_label") or "Owner")
    latest: dict[str, dict] = {}
    for e in activity.events("property.status.updated", since=ctx.now - window):
        if str(e["data"].get("to") or "").lower() == "new_lead" and e["resource"]:
            latest[e["resource"]] = e
    ctx.out.found = len(latest)
    names = routes.names_from_log(ctx.now - timedelta(days=60))
    reads = {"n": 0, "max": int(st.get("max_reads", 20))}
    open_now = ctx.policy.in_business_hours(ctx.now)
    waits, waiting, pings = [], 0, 0
    for u, e in latest.items():
        start = e["when"]
        info = _lead_info(ctx, u, reads)
        if info is None:
            ctx.out.tally["new leads not read this run (read cap)"] += 1
            continue
        if not info.get("owner"):
            continue
        called, on_call = first_call(info["owner"], start)
        if on_call:
            ctx.out.tally["new leads set on a call (no wait to measure)"] += 1
            continue
        if called:
            waits.append((called - start).total_seconds() / 60.0)
            continue
        if any(str(x["data"].get("to") or "").lower() != "new_lead"
               for x in activity.events("property.status.updated", resource=u, since=start + timedelta(seconds=1))):
            ctx.out.tally["new leads a person moved on without a logged call"] += 1
            continue
        waiting += 1
        if not open_now:
            continue
        waited = (ctx.now - business_open(start, ctx.policy)).total_seconds() / 60.0
        said = ledger.fact(u, "speed_pings") or {}
        if said.get("for") != start.isoformat(timespec="seconds"):
            said = {"for": start.isoformat(timespec="seconds")}
        # Decision D13: leads belong to the lead manager. Named on the stage, that is who is told;
        # otherwise the record's assignee is, by the name the call log knows.
        who = str(st.get("lead_manager") or "") or routes.person_name(st, info.get("assignee") or "", names)
        if waited >= o_min and not said.get("owner"):
            ctx.out.problem("%s: %s has waited %s for a first call and nobody has called. This new lead needs a call now." % (
                owner_label, info["street"], routes.span(waited * 60)))
            said["owner"] = ctx.now.isoformat(timespec="seconds")
            said.setdefault("assignee", said["owner"])
            pings += 1
        elif a_min <= waited < o_min and not said.get("assignee"):
            ctx.out.problem("%s: %s became a new lead %s ago and has not been called. Call now." % (
                who or "Whoever holds this lead", info["street"], routes.span(waited * 60)))
            said["assignee"] = ctx.now.isoformat(timespec="seconds")
            pings += 1
        ledger.set_fact(u, "speed_pings", said)   # what was already said, so a ping is said once: the stage's own memory
    ctx.out.acted = pings
    if waits:
        waits.sort()
        ctx.out.tally["speed to lead, median minutes"] = round(waits[len(waits) // 2])
        ctx.out.line("Called: %d new lead(s), median %s to the first call, slowest %s" % (
            len(waits), routes.span(waits[len(waits) // 2] * 60), routes.span(waits[-1] * 60)))
    if waiting:
        ctx.out.head = "%d new lead(s) are waiting on a first call." % waiting
    elif latest:
        ctx.out.head = "Every new lead in the last %d hours has been called." % int(window.total_seconds() // 3600)
    else:
        ctx.out.head = "No new lead in the last %d hours." % int(window.total_seconds() // 3600)


def speed_lines(now: datetime, tz: str, days: int = 1) -> list[str]:
    """Yesterday's speed to lead in one plain line, for the morning digest. Reads only the log copy
    and what the speed job already learned about each lead."""
    zone = ZoneInfo(tz)
    end = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    waits, never = [], 0
    for e in activity.events("property.status.updated", since=start, until=end):
        if str(e["data"].get("to") or "").lower() != "new_lead":
            continue
        info = ledger.fact(e["resource"], "lead_owner")
        if not isinstance(info, dict) or not info.get("owner"):
            continue
        called, on_call = first_call(info["owner"], e["when"])
        if on_call:
            continue
        if called:
            waits.append((called - e["when"]).total_seconds() / 60.0)
        else:
            never += 1
    if not waits and not never:
        return []
    waits.sort()
    text = "Speed to lead %s: " % start.strftime("%b %d").replace(" 0", " ")
    if waits:
        text += "median %s, slowest %s over %d new lead(s)" % (
            routes.span(waits[len(waits) // 2] * 60), routes.span(waits[-1] * 60), len(waits))
    if never:
        text += "%s%d never called" % ("; " if waits else "", never)
    return [text]


def run_leadclock(ctx: Ctx) -> None:
    if str(ctx.stage.get("job") or "moves").strip().lower() == "speed":
        run_speed(ctx)
    else:
        run_moves(ctx)
