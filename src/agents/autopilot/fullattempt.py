"""The full-attempt list (build plan 1.3, the read-only half).

An attempt is a day, and a FULL attempt is a day on which every top-tier number the owner has was
dialed. Dialing one of three numbers and moving on counts as an attempt in the call lane all the
same, and the two numbers nobody tried are usually where the owner is. So at 4:30 each calling day
this stage looks at everyone who was dialed today and lists the owners still one or two numbers
short, while there is time left to finish them.

For each owner dialed today it reads the owner's record, takes the numbers a call may use (the
gate's rule: Dial First and Dial Second, or a number a person confirmed, never one marked wrong,
dead or do-not-call) and subtracts the numbers the log shows were dialed today.

It reads only, and shows the last four digits of a number, never the whole of it. The second half
of plan 1.3, a short text to each number that was dialed and not reached, sends through Smarter
Contact and waits on one proof that a one-off text lands in the owner's existing thread.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from . import activity, gate
from .stages import Ctx, addr

log = logging.getLogger(__name__)


def run_fullattempt(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    zone = ZoneInfo(ctx.policy.tz)
    sync = activity.sync(ctx.crm, "calls", now=ctx.now, floor_days=int(st.get("floor_days", 3)),
                         max_calls=int(st.get("max_log_calls", 400)))
    if not sync["complete"]:
        ctx.out.problem("the call log is still catching up (read through %s); today's list may be short" % (sync["through"] or "nothing yet"))
    start = ctx.now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    dialed: dict[str, set] = {}
    for e in activity.events(activity.DIAL, since=start, until=ctx.now + timedelta(seconds=1)):
        if e["data"].get("direction") != "inbound" and e["resource"]:
            dialed.setdefault(e["resource"], set()).add(e["data"].get("number") or "")
    ctx.out.found = len(dialed)
    if not dialed:
        ctx.out.head = "Nobody has been dialed yet today."
        return
    short_max = int(st.get("short_by", 2))
    full = short = more = held = 0
    for i, (owner, numbers) in enumerate(dialed.items()):
        if i >= int(st.get("max_owners", 150)):
            ctx.out.tally["owners not checked (cap)"] += 1
            continue
        try:
            rows = ctx.crm.owner_records(owner, cap=10)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a dialed owner's records (%s)" % str(exc)[:100])
            continue
        row = next((r for r in rows if gate.check_row(r, "call", cfg=cfg).ok), None)
        if row is None:
            held += 1
            continue
        rec = ctx.crm.record(row["uuid"])
        usable = gate.usable_numbers(rec, "call", cfg, row)
        left = [n for n in usable if n["number"] not in numbers]
        if not usable or not left:
            full += 1
        elif len(left) <= short_max:
            short += 1
            ctx.out.line("%s: %d number(s) not tried today (ending %s)" % (
                addr(rec), len(left), ", ".join(n["number"][-4:] for n in left)))
        else:
            more += 1
    ctx.out.acted = short
    ctx.out.tally.update({"got a full attempt": full, "one or two numbers short": short,
                          "three or more numbers short": more, "dialed although the record is held": held})
    ctx.out.head = "%d owner(s) dialed today: %d got a full attempt, %d are one or two numbers short%s." % (
        len(dialed), full, short, (", %d have three or more untried" % more) if more else "")
