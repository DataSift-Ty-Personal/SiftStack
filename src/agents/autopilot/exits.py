"""Clean exits (build plan 2.14): finished cards leave the board, and a dead lead is not thrown away.

The lead board is a work list, and a work list that keeps every lead that ever died stops being
read. Dead and Not Interested cards pile up in their columns for good, because nothing takes a
card off a board. Meanwhile a dead lead's record keeps its status for ever, and that status keeps
it out of every marketing preset, so an owner who said "not this year" is never asked again.
Once a month this stage does two things:

  * CARDS OUT. A card that has sat in the Dead Lead or Not Interested column for 30 days is removed
    from the board. The record's STATUS STAYS: the history, the lists and the notes are all still
    there, and the second-chance lane still knows a no from a dead end. A card whose record no
    longer holds that column's status is left alone and named: somebody changed their mind.
  * DEAD LEADS BACK TO MARKETING after 180 days (decision D27), when the house has not sold. The
    status is cleared, so the record falls back into whichever marketing lane its tags put it in,
    and a note says where it has been. Nobody follows it up by hand. A record that Do Not Market,
    an opt-out, a litigator, a death or a sale holds is never brought back: the gate is asked, and
    only the dead status itself is forgiven.

The time in a column comes from the shared log copy; a card older than the log reaches is timed
from its own timeline. How long a lead has been dead comes from the log too, and when the log does
not reach that far the count starts the day this stage first saw the record, which can only make
the return later.

`buyer_exit` is the third exit, for the reply agent: a reply that shows the sender is a buyer sets
the buyer status and adds the record to the dispo list. It lives here so the three exits share one
definition; the reply agent decides when to call it.
"""
from __future__ import annotations

import logging

from . import activity, gate, lanes, ledger
from . import leadclock
from . import leadroutes as routes
from .stages import Ctx, addr, status_of

log = logging.getLogger(__name__)

EXIT_PHASES = ["dead", "not_interested"]


def dead_since(ctx: Ctx, u: str, status: str):
    """When the record took its dead status: from the log copy, else the day this stage first saw it."""
    took = [e for e in activity.events("property.status.updated", resource=u)
            if str(e["data"].get("to") or "").strip().lower() == status]
    if took:
        return took[-1]["when"], True
    return ledger.first_seen(u, "exits:%s" % status, ctx.now), False


def remove_cards(ctx: Ctx, ph: dict, board: routes.Board, reads: dict) -> tuple[int, dict]:
    """Take finished cards off the board. Returns how many, and where every card in those columns is."""
    st = ctx.stage
    days = float(st.get("card_days", 30))
    max_remove = int(st.get("max_remove", 40))
    cols = {}
    for p in [str(x) for x in (st.get("exit_phases") or EXIT_PHASES)]:
        if p not in ph:
            ctx.out.problem("exit_phases names %r, which is not a phase (phases: %s)" % (p, ", ".join(sorted(ph))))
            continue
        u = board.find(ph[p]["column"])
        if u:
            cols[u] = p
        else:
            ctx.out.problem("the %s board has no column named %s; name it under `columns` on the stage" % (
                board.title, ph[p]["column"]))
    if not cols:
        return 0, {}
    where, unknown = routes.cards_by_record(ctx.crm, list(cols), cap=int(st.get("max_cards", 500)))
    if unknown:
        ctx.out.tally["cards whose record the log cannot name (left alone)"] += unknown
    removed, state = 0, {}
    for u, w in where.items():
        ctx.out.found += 1
        phase = cols[w["column"]]
        entered = leadclock.entered_at(ctx, u, w["card"], w["column"], board, reads)
        if entered is None:
            ctx.out.tally["cards not timed this run (read cap, next run)"] += 1
            continue
        if (ctx.now - entered).total_seconds() < days * 86400:
            continue
        if state.get("broken"):
            continue
        if removed >= max_remove:
            ctx.out.tally["left for the next run (cap %d a run)" % max_remove] += 1
            continue
        if reads["n"] >= reads["max"]:
            ctx.out.tally["not read this run (read cap)"] += 1
            continue
        reads["n"] += 1
        rec = ctx.crm.record(u)
        want = ph[phase]["status"].lower()
        if not rec.get("uuid") or status_of(rec) != want:
            ctx.out.tally["cards left: the record no longer holds that column's status"] += 1
            ctx.out.line("%s: card in %s, status %s, left for a person" % (
                addr(rec) if rec else u[:8], board.name(w["column"]), status_of(rec) or "none"))
            continue
        ctx.change(u, "card_remove", {"card": w["card"], "column": w["column"], "column_title": board.name(w["column"])}, None)
        ctx.write("remove card", routes.remove_card, ctx.crm, w["column"], w["card"])
        if ctx.live:
            if not state.get("verified"):
                state["verified"] = True
                if routes.card_in_column(ctx.crm, w["column"], w["card"]):
                    state["broken"] = True
                    ctx.out.problem("the card for %s is still on the board after it was removed; nothing more was removed" % addr(rec))
                    continue
            ledger.event(ctx.run_id, st.id, u, "exit", {"what": "card removed", "column": board.name(w["column"]),
                                                        "days": int((ctx.now - entered).total_seconds() // 86400)},
                         now=ctx.now, tz=ctx.policy.tz)
        removed += 1
        ctx.out.line("%s: %d days in %s, card off the board (status kept)" % (
            addr(rec), int((ctx.now - entered).total_seconds() // 86400), board.name(w["column"])))
    return removed, where


def remarket(ctx: Ctx, ph: dict, reads: dict) -> int:
    """Dead leads whose wait is over go back to marketing. Returns how many."""
    st = ctx.stage
    tz = ctx.policy.tz
    days = float(st.get("remarket_days", 180))
    dead = ph["dead"]["status"]
    back = st.get("return_status") or None          # None clears the status: the record is a prospect again
    if back is not None and str(back).strip().lower() in lanes.LEAD_STATUSES:
        ctx.out.problem("return_status is %s, a lead status. A dead lead goes back to marketing, never back into the "
                        "lead pipeline by machine; nothing was sent back" % back)
        return 0
    max_n = int(st.get("max_remarket", 25))
    # Clearing a status is the one write here that could hurt a record that was never dead, so the
    # search is trusted for nothing: each row's status is checked, and the full record's again.
    rows = routes.status_rows(ctx, [dead], cap=int(st.get("max_scan", 2000)))
    ctx.out.tally["dead leads"] = len(rows)
    sent = unseen = 0
    for row in rows:
        u = row["uuid"]
        since, in_log = dead_since(ctx, u, dead.lower())
        if not in_log:
            unseen += 1
        if (ctx.now - since).total_seconds() < days * 86400:
            continue
        if sent >= max_n:
            ctx.out.tally["left for the next run (cap %d a run)" % max_n] += 1
            continue
        if reads["n"] >= reads["max"]:
            ctx.out.tally["not read this run (read cap)"] += 1
            continue
        reads["n"] += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid") or status_of(rec) != dead.lower():
            # The search index trails the record: somebody changed the status a minute ago.
            ctx.out.tally["left: the record is no longer dead (or could not be read)"] += 1
            continue
        # The gate is asked as if the record were about to be assigned. The dead status is the one
        # thing forgiven: it is what this step exists to lift. Everything else it reports holds.
        v = ctx.verdict(rec, "assign", row=row)
        why = [r for r in v.reasons if not (r[0] == "finished" and r[1] == dead.lower())]
        if why:
            ctx.out.tally["stay dead: %s" % _plain(why[0])] += 1
            continue
        sold = routes.parse_dt(rec.get("last_sold"))
        if sold and sold > since:
            ctx.out.tally["stay dead: the house sold since"] += 1
            continue
        ctx.change(u, "status", routes.stored_status(rec) or dead, back)     # the account's own spelling, so undo can put it back
        ctx.write("back to marketing", ctx.crm.set_status, u, back)
        ctx.write("note", ctx.crm.message, u, "Was a lead, marked dead on %s. Back in marketing after %d days with no sale. "
                                              "Nobody is following this one up by hand." % (routes.nice_day(since, tz), int(days)))
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, "exit", {"what": "back to marketing", "dead_since": since.isoformat(timespec="seconds")},
                         now=ctx.now, tz=tz)
        sent += 1
        ctx.out.line("%s: dead since %s, status cleared so it is marketed again" % (addr(rec), routes.nice_day(since, tz)))
    if unseen:
        ctx.out.tally["dead leads older than the log reaches (counted from the day first seen)"] = unseen
    return sent


def _plain(reason: tuple) -> str:
    code, detail = reason
    text = gate.TEXT.get(code, code)
    return text % detail if "%s" in text else text


def run_exits(ctx: Ctx) -> None:
    st = ctx.stage
    ph = routes.phases(st)
    if not leadclock.sync_logs(ctx, ("board", "state")):
        ctx.out.head = "The log is behind, so no exit was judged this run."
        return
    reads = {"n": 0, "max": int(st.get("max_reads", 120))}
    removed = 0
    board = routes.stage_board(ctx, "lead", ph)
    if board.columns:
        removed, _where = remove_cards(ctx, ph, board, reads)
    back = remarket(ctx, ph, reads) if st.get("remarket_dead", True) else 0
    ctx.out.acted = removed + back
    verb = ("Removed", "sent") if ctx.live else ("Would remove", "send")
    parts = ["%s %d finished card(s) from the board" % (verb[0], removed)]
    if st.get("remarket_dead", True):
        parts.append("%s %d dead lead(s) back to marketing" % (verb[1], back))
    ctx.out.head = " and ".join(parts) + "."
    if not board.columns:
        ctx.out.head = "The board's columns are not known yet, so no card was removed. " + ctx.out.head


def buyer_exit(crm, record_uuid: str, lists: list[str], *, status: str = "buyer", commit: bool = False) -> dict:
    """A reply showed the sender is a buyer, not a seller: set the buyer status and add the record to
    the dispo list(s). Dry unless commit. Returns what was (or would be) written and what was there
    before, so the caller can log it. A record that already holds a lead status is not touched: a
    seller who also buys is still our seller, and a person decides that one."""
    rec = crm.record(record_uuid)
    if not rec.get("uuid"):
        return {"ok": False, "say": "the record could not be read"}
    cur = status_of(rec)
    if cur in lanes.LEAD_STATUSES:
        return {"ok": False, "say": "already a lead (%s): a person decides whether this owner is a buyer" % cur}
    have = {str((t.get("title") if isinstance(t, dict) else t) or "").lower() for t in (rec.get("lists") or [])}
    add = [t for t in lists if t and t.lower() not in have]
    plan = {"record": record_uuid, "street": addr(rec), "add_lists": add,
            "status": {"before": routes.stored_status(rec), "after": status}}
    if not commit:
        return {"ok": True, "dry": True, "would": plan}
    if cur != status.lower():
        crm.set_status(record_uuid, status)
    if add:
        routes.add_lists(crm, record_uuid, add)
    return {"ok": True, "did": plan}
