"""The attempt keeper (build plan 0.5): the call counter tells the truth.

The call lane is a row of presets keyed on one number, the record's call counter: Ready to Call
holds counter 0, Call Attempt 1 holds 1, and so on. Whatever dials through DataSift's own dialer
raises that counter. Calls made through Kixie do not. Measured on 2026-11-12: of 130 records sitting
in "FTM - 02 Ready to Call", 18 had already been dialed, nearly all through Kixie, and every one
still read zero. A dialed record that never leaves Ready to Call gets dialed again tomorrow as if
for the first time, and never reaches the mail lane or research.

This stage counts the attempts from the account's own call log and writes the counter.

  * AN ATTEMPT IS A DAY, not a dial. One owner dialed on three numbers in one afternoon has had one
    attempt. The count is the number of business-local days with at least one outbound dial.
  * ONLY SINCE THE RECORD ENTERED ITS LANE. The log is per OWNER, so it also holds the calls made
    about that owner's other house under another list last spring. Counting those would push a
    brand-new foreclosure straight to exhausted.
  * A PERSON LOWERING THE COUNTER IS A RESET. The count starts again from that moment.
  * THE COUNTER ONLY GOES UP. When DataSift's own dialer already counted the day, there is nothing
    to write.
  * THE FIRST LIVE RUN IS STAGED. It lists every change it would make and moves nothing until a
    person releases that list, because the first run corrects weeks of history at once.

The dialer's own outcomes matter too: Kixie writes some phone statuses back by itself (the log shows
`owner.phone.status.updated` from it), and the stage reports how many it saw, which is the evidence
for whether a disposition export still has to be imported by hand.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .. import store
from . import activity, ftmpipeline, ledger
from .stages import Ctx, Summary, addr, lane_entry, stage_rows

log = logging.getLogger(__name__)

RESET = "property.predictivecall.attempts.decreased"


def _approved_key(stage_id: str) -> str:
    return "autopilot:attempts:approved:%s" % stage_id


def counters_of(stage) -> dict:
    """preset title -> the call counter a record in it holds. By default the presets are listed in
    lane order: the first holds 0, the next 1, and so on."""
    given = stage.get("counters")
    if isinstance(given, dict) and given:
        return {str(k): int(v) for k, v in given.items()}
    return {title: i for i, title in enumerate(stage.presets)}


def _nice_days(days: list[str]) -> str:
    out = [date.fromisoformat(d).strftime("%b %d").replace(" 0", " ") for d in days]
    return ", ".join(out[:-1]) + (" and " if len(out) > 1 else "") + out[-1] if out else ""


def plan_changes(ctx: Ctx) -> list[dict]:
    """Every record in the stage's presets whose counter is behind its real call days."""
    st = ctx.stage
    lane = str(st.get("lane") or "")
    floor_days = int(st.get("floor_days", 45))
    counters = counters_of(st)
    floor = ctx.now - timedelta(days=floor_days)
    dialed = activity.owners_dialed(floor)
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    out = []
    for title, row in rows:
        owner_uuid = (row.get("owner") or {}).get("uuid") or ""
        if not owner_uuid:
            ctx.out.tally["rows with no owner id"] += 1
            continue
        if owner_uuid not in dialed:
            continue
        ctx.out.tally["owners dialed in the last %d days" % floor_days] += 1
        u = row["uuid"]
        have = int(counters.get(title, 0))
        since = max(lane_entry(ctx, row, lane), floor)
        resets = activity.events(RESET, resource=u, since=since)
        if resets:
            since = resets[-1]["when"]
            ctx.out.tally["counter lowered by a person (counted again from that day)"] += 1
        days = activity.dial_days(owner_uuid, since=since, tz=ctx.policy.tz)
        want = len(days)
        board = ftmpipeline.counter_for(ctx, u, lane)
        if board is not None:
            # On the live First to Market board a day counts once it is a full attempt (its call and,
            # when the day needs one, its text), so the counter is what the board counted and every
            # preset keyed on it shows what the board shows (ftmpipeline).
            want = board
        if want <= have:
            continue
        numbers = set()
        for d in days.values():
            numbers |= d
        out.append({"record_uuid": u, "owner_uuid": owner_uuid, "street": addr(row), "preset": title, "have": have,
                    "want": want, "days": sorted(days), "numbers": len(numbers - {""}),
                    "board": (ftmpipeline.position(u) or {}).get("column", "") if board is not None else ""})
    return out


def apply_one(ctx: Ctx, item: dict, state: dict) -> str:
    """Write one counter. Reads the record first: the preset a row came from can be minutes old."""
    u, want = item["record_uuid"], int(item["want"])
    rec = ctx.crm.record(u)
    if not rec.get("uuid"):
        return "unreadable"
    cur = int(rec.get("predictivecall_attempts") or 0)
    if cur >= want:
        return "already there"
    ctx.change(u, "call_attempts", cur, want)
    ctx.write("call counter", ctx.crm.call_attempts, u, want)
    if not ctx.live:
        return "moved"
    if not state.get("verified"):
        state["verified"] = True
        back = int(ctx.crm.record(u).get("predictivecall_attempts") or 0)
        if back != want:
            state["broken"] = True
            ctx.out.problem("the call counter did not stick on %s (wrote %d, read %d); nothing more was written" % (
                item.get("street") or u[:8], want, back))
            return "failed"
    ledger.event(ctx.run_id, ctx.stage.id, u, "attempts", {"from": cur, "to": want, "days": item.get("days")},
                 now=ctx.now, tz=ctx.policy.tz)
    return "moved"


def run_attempts(ctx: Ctx) -> None:
    st = ctx.stage
    floor_days = int(st.get("floor_days", 45))
    sync = activity.sync(ctx.crm, "calls", now=ctx.now, floor_days=floor_days, max_calls=int(st.get("max_log_calls", 400)))
    if not sync["complete"]:
        ctx.out.problem("the call log is still catching up (read through %s, %d pages this run); the counts below are "
                        "a floor and the next run continues" % (sync["through"] or "nothing yet", sync["calls"]))
    changes = plan_changes(ctx)
    wrote = activity.events("owner.phone.status.updated", since=ctx.now - timedelta(days=floor_days))
    if wrote:
        ctx.out.tally["phone statuses the dialer wrote back by itself"] = len(wrote)
    by = Counter("%d to %d" % (c["have"], c["want"]) for c in changes)
    for c in changes:
        if c.get("board"):
            ctx.out.line("%s: %d to %d (on the board in %s)" % (c["street"], c["have"], c["want"], c["board"]))
        else:
            ctx.out.line("%s: %d to %d (dialed %s)" % (c["street"], c["have"], c["want"], _nice_days(c["days"])))
    mix = ", ".join("%s on %d" % kv for kv in sorted(by.items()))
    if not changes:
        ctx.out.head = "Every call counter in the lane matches the call log."
        return
    if not ctx.live:
        ctx.out.acted = len(changes)
        ctx.out.head = "Would correct %d call counter(s) from the call log: %s." % (len(changes), mix)
        return
    if st.get("first_run", "staged") == "staged" and not store.kv_get(_approved_key(st.id)):
        staged = ledger.staged_record_uuids(st.id)
        items = [c for c in changes if c["record_uuid"] not in staged]
        if items:
            bid = ledger.batch_create(st.id, "attempts", items, 0)
            ctx.out.batch = bid
            ctx.out.acted = len(items)
            ctx.out.head = ("First run: %d call counter(s) are behind the call log (%s). Nothing was written. They are "
                            "staged as batch %d for a person to approve: python src/agents/cli.py autopilot release %d --commit"
                            % (len(items), mix, bid, bid))
        else:
            ctx.out.head = "The first correction is still staged and waits on a person (%d record(s))." % len(staged)
        return
    state: dict = {}
    moved = 0
    for c in changes[: int(st.get("max_per_run", 500))]:
        if state.get("broken"):
            break
        res = apply_one(ctx, c, state)
        ctx.out.tally[res] += 1
        moved += res == "moved"
    ctx.out.acted = moved
    ctx.out.head = "Corrected %d call counter(s) from the call log: %s." % (moved, mix)


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, now: datetime | None = None) -> dict:
    """Approve the staged first correction: write each counter (re-reading every record first), and
    from then on the stage writes on its own. Dry unless commit."""
    from . import policy as policy_mod
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    if b["kind"] != "attempts":
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    items = b["items"]
    if not commit:
        by = Counter("%d to %d" % (c["have"], c["want"]) for c in items)
        return {"batch": batch_id, "records": len(items), "dry": True,
                "changes": ", ".join("%s on %d" % kv for kv in sorted(by.items()))}
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind="attempts", owner=st.owner, mode=st.mode, live=True)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=True, out=out)
    state: dict = {}
    res = Counter()
    for c in items:
        if state.get("broken"):
            res["not written (stopped)"] += 1
            continue
        res[apply_one(ctx, c, state)] += 1
    ok = not state.get("broken")
    ledger.batch_set(batch_id, "sent" if ok else "failed", {"results": dict(res), "problems": out.problems})
    if ok:
        store.kv_set(_approved_key(st.id), store.now())
    return {"batch": batch_id, "run": ctx.run_id, "results": dict(res), "problems": out.problems}
