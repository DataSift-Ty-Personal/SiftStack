"""The full-attempt assistant (build plan 1.3).

An attempt is a day, and a FULL attempt is a day on which every top-tier number the owner has was
dialed. Dialing one of three numbers and moving on counts as an attempt in the call lane all the
same, and the two numbers nobody tried are usually where the owner is. So at 4:30 each calling day
this stage looks at everyone who was dialed today and lists the owners still one or two numbers
short, while there is time left to finish them.

For each owner dialed today it reads the owner's record, takes the numbers a call may use (the
gate's rule: Dial First and Dial Second, or a number a person confirmed, never one marked wrong,
dead or do-not-call) and subtracts the numbers the log shows were dialed today. A number on the
do-not-call registry is not listed as a number to try while calls to those are held (decision
D32), and a dial to one is counted, so the caller hears about it the same afternoon.

THE AFTERNOON TEXT (`afternoon_text: true`). On the call days after the opening texts (days 3 to
5 on the first-to-market timeline, read from the lanes table), a number that was dialed today and
not reached gets one short text in the thread the owner already has: "I tried calling you today".
Every part of that sentence has to be true, so the text goes only to a number the log shows was
dialed today, with no answered call of a minute or more, that is already in the owner's text
thread. The owner's one text a day and five a week are asked of limits.room() first.

This stage does not send. In plan mode it lists who would get the text. In live mode it stages
the texts as a batch for a person to release before 5:30pm; a batch released later than that is
refused, because "today" in the message would no longer be the day it is read. The send itself is a
one-off message in an existing Smarter Contact thread, which waits on one proof (proofs.py,
thread_text) that such a message lands in that thread and not in a new one. Until that proof is
done the setting stays off in the playbook.

It shows the last four digits of a number, never the whole of it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import activity, gate, scrub
from .stages import Ctx, addr, lane_entry

log = logging.getLogger(__name__)


def run_fullattempt(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    zone = ZoneInfo(ctx.policy.tz)
    lane = str(st.get("lane") or "")
    sync = activity.sync(ctx.crm, "calls", now=ctx.now, floor_days=int(st.get("floor_days", 3)),
                         max_calls=int(st.get("max_log_calls", 400)))
    if not sync["complete"]:
        ctx.out.problem("the call log is still catching up (read through %s); today's list may be short" % (
            sync["through"] or "nothing yet"))
    start = ctx.now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    if st.get("afternoon_text", False):
        # Yesterday's afternoon batch that nobody released is closed and said, dials today or not.
        from . import textlane
        textlane.expire_stale(ctx, start.date().isoformat())
    dialed: dict[str, set] = {}
    for e in activity.events(activity.DIAL, since=start, until=ctx.now + timedelta(seconds=1)):
        if e["data"].get("direction") != "inbound" and e["resource"]:
            dialed.setdefault(e["resource"], set()).add(e["data"].get("number") or "")
    ctx.out.found = len(dialed)
    if not dialed:
        ctx.out.head = "Nobody has been dialed yet today."
        return
    hold_calls = scrub.settings(ctx.policy).calls == "hold"
    short_max = int(st.get("short_by", 2))
    full = short = more = held = on_registry = 0
    worked = []
    for i, (owner, numbers) in enumerate(dialed.items()):
        if i >= int(st.get("max_owners", 150)):
            ctx.out.tally["owners not checked (cap)"] += 1
            continue
        try:
            rows = ctx.crm.owner_records(owner, cap=10)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a dialed owner's records (%s)" % str(exc)[:100])
            continue
        scrub.note_rows(rows, lane, ctx.now)
        row = next((r for r in rows if gate.check_row(r, "call", cfg=cfg).ok), None)
        if row is None:
            held += 1
            continue
        rec = ctx.crm.record(row["uuid"])
        worked.append((owner, row, rec))
        # The gate drops the number its row flags from calls whatever the playbook says. While calls
        # to registry numbers are held that is the rule; once the playbook allows them the row is
        # kept from the gate, and the registry rule (which reads the setting) decides alone.
        usable, off = scrub.split(gate.usable_numbers(rec, "call", cfg, row if hold_calls else None), "call",
                                  ctx.policy, row, lane, ctx.now)
        if off:
            ctx.out.tally["numbers left off the list: on the do-not-call registry"] += len(off)
        if hold_calls and scrub.flagged_among(numbers):
            on_registry += 1
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
    if on_registry:
        ctx.out.tally["dialed on a number the do-not-call registry flags"] = on_registry
        ctx.out.problem("%d owner(s) were dialed today on a number the do-not-call registry flags; those numbers are held "
                        "from calls until the attorney answers" % on_registry)
    ctx.out.head = "%d owner(s) dialed today: %d got a full attempt, %d are one or two numbers short%s." % (
        len(dialed), full, short, (", %d have three or more untried" % more) if more else "")
    if st.get("afternoon_text", False):
        afternoon_texts(ctx, worked, dialed, start)


def _clock_today(text: str, start: datetime) -> datetime:
    h, m = (int(x) for x in str(text).split(":"))
    return start + timedelta(hours=h, minutes=m)


def afternoon_texts(ctx: Ctx, worked: list, dialed: dict, start: datetime) -> None:
    """The second half of plan 1.3: stage one short text to each number that was dialed today and
    not reached, for the owners on the call days after the opening texts. Nothing is sent here."""
    from . import ftmpipeline, lanes, ledger, limits, textlane
    from .stages import stage_lanes

    st = ctx.stage
    tally = ctx.out.tally
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    # Each lane the stage works keeps its own call days and markers. The stage used to read one lane
    # only, so a dialed Top 1,000 owner was counted as "not in the lane" and never got the text.
    plans = {}
    for ln in stage_lanes(st):
        tl = lanes_cfg.timeline(ln)
        if not tl or tl.get("outside"):
            continue
        # The call days that come after the opening texts: days 3 to 5 on the first-to-market timeline.
        last_opening = max(tl.get("opening_texts") or [0])
        marks = lanes_cfg.markers.get(ln) or {}
        plans[ln] = {"days": [d for d in (tl.get("call_days") or []) if d > last_opening],
                     "marks": {str(t).lower() for t in (marks.get("tags") or []) + (marks.get("lists") or [])}}
    if not plans:
        ctx.out.problem("the afternoon text reads its days from a lane's timeline; set `lane` in the stage block (for example ftm)")
        return
    callers = [c for c in (getattr(ctx.policy, "callers", None) or []) if isinstance(c, str)]
    sender = str(st.get("sender") or (callers[0] if len(callers) == 1 else "")).strip()
    if not sender:
        ctx.out.problem("the afternoon text is signed by the caller who dialed; name that person as `sender` in the stage block")
        return
    copy_bad = textlane.audit()
    if copy_bad:
        ctx.out.problem("the text copy fails its own audit (%d problem(s), the first: %s); no afternoon text was staged" % (
            len(copy_bad), copy_bad[0]))
        return
    lim = limits.settings(ctx.policy)
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    today = ctx.now.astimezone(zone).date()
    try:
        send_by = _clock_today(st.get("text_by", "17:30"), start)
    except ValueError:
        ctx.out.problem("text_by %r must read like 17:30; no afternoon text was staged" % st.get("text_by"))
        return
    send_at = ctx.now + timedelta(minutes=float(st.get("lead_minutes", 10)))
    if send_at >= send_by:
        tally["afternoon texts not staged: it is past %s" % send_by.strftime("%I:%M%p").lstrip("0").lower()] += 1
        return
    reached_s = int(st.get("reached_seconds", 60))
    reached: dict[str, set] = {}
    for e in activity.events("owner.call.answered", since=start, until=ctx.now + timedelta(seconds=1)):
        if e["data"].get("direction") != "inbound" and int(e["data"].get("duration") or 0) >= reached_s:
            reached.setdefault(e["resource"], set()).add(e["data"].get("number") or "")
    words = " or ".join(ln.replace("_", " ") for ln in plans)
    stop_on_correct = bool(st.get("stop_on_correct", True))
    text_usd = float(st.get("text_usd", 0.02))
    room_left = limits.capacity_left(ctx.now, lim, tz)
    staged = ledger.staged_record_uuids(st.id)
    items, usd, messages = [], 0.0, 0
    for owner, row, rec in worked:
        u = rec.get("uuid") or ""
        missed = {n for n in dialed.get(owner, set()) if n} - reached.get(owner, set())
        if not missed:
            tally["reached by phone today: no text"] += 1
            continue
        if u in staged:
            tally["afternoon text already staged, waiting on release"] += 1
            continue
        names = {str(t.get("title") or t.get("name") or "").lower() if isinstance(t, dict) else str(t).lower()
                 for t in (rec.get("tags") or []) + (rec.get("lists") or [])}
        lane = next((ln for ln, pl in plans.items() if names & pl["marks"]), "")
        if not lane:
            tally["dialed today, not in the %s lane: no text" % words] += 1
            continue
        days = plans[lane]["days"]
        # On the live First to Market board the day is the card's column, not the calendar.
        slot = ftmpipeline.afternoon_day(ctx, u, lane, today)
        if slot is not None and slot.get("hold"):
            tally["no afternoon text: %s" % slot["hold"]] += 1
            continue
        if slot is not None:
            entered = date.fromisoformat(slot["since"]) if slot.get("since") else today
            day = int(slot["day"])
        else:
            entered = lane_entry(ctx, rec, lane).astimezone(zone).date()
            day = lanes.business_days(entered, today)
        if day not in days:
            tally["not on an afternoon-text day (days %s of the lane)" % ", ".join(str(d) for d in days)] += 1
            continue
        v = ctx.verdict(rec, "text", row=row, lane=lane)
        if not v.ok:
            tally["no text, held back: %s" % v.reason()] += 1
            continue
        ow = rec.get("owner") or {}
        if stop_on_correct and any(str(p.get("status") or "").upper() == "CORRECT" for p in (ow.get("phones") or [])):
            tally["the owner already answered: a person has the thread"] += 1
            continue
        limits.observe(owner, rec, ctx.now, tz, lim)
        keep, removed = scrub.split(v.numbers, "text", ctx.policy, row, lane, ctx.now)
        if removed:
            tally["numbers left out of the text: on the do-not-call registry"] += len(removed)
        threads = limits.number_threads(owner, since_day=entered.isoformat())
        if not threads:
            tally["no text thread yet (the opening texts have not gone out): no afternoon text"] += 1
            continue
        nums = [n for n in keep if n["number"] in missed and n["number"] in threads
                and limits.inside_hours(lim.text_hours, send_at, tz, n["number"], "text")[0]
                and not limits.number_busy(n["number"], today.isoformat(), owner)]
        if not nums:
            tally["no dialed number that a text may use in the owner's thread"] += 1
            continue
        system = threads[nums[0]["number"]]["system"]
        r = limits.room(owner, "text", send_at, policy=ctx.policy, lim=lim, tz=tz, system=system, campaign=False)
        if not r.ok:
            tally[textlane.ROOM_WORDS.get(r.code, "no room: %s" % r.why)] += 1
            continue
        if messages + len(nums) > room_left:
            tally[textlane.ROOM_WORDS["capacity"]] += 1
            continue
        first = textlane.greeting_name(ow)
        street = textlane._street(rec)
        text = textlane.render_afternoon(u, first, street, sender, turn=day)
        if len(text) > textlane.SEGMENT:
            tally["no text: the street is too long for one message"] += 1
            continue
        price = round(len(nums) * text_usd, 4)
        _, last, values = textlane.contact_values(rec)
        numbers = [{"number": n["number"], "tier": n.get("tier") or "", "status": n.get("status") or "", "tz": "",
                    "send_at": limits._iso(send_at), "group": "", "cohort": threads[n["number"]]["thread"].split(":", 1)[-1],
                    "thread": threads[n["number"]]["thread"], "new_contact": False} for n in nums]
        items.append({"record_uuid": u, "owner_uuid": owner, "street": street, "day": today.isoformat(), "lane": lane, "lane_day": day,
                      "series": "afternoon", "step": day, "touch": "%s:%s:afternoon:%d:%s" % (st.id, owner, day, today.isoformat()),
                      "system": system, "campaign": False, "audience": "named" if first else "noname", "template_set": "",
                      "template_touch": 0, "templates": [], "first_name": first, "last_name": last, "values": values,
                      "numbers": numbers, "price": price, "sms_before": int(rec.get("sms_attempts") or 0), "text": text,
                      "send_by": limits._iso(send_by), "sender": sender})
        usd += price
        messages += len(numbers)
        ctx.out.line("%s: a text to %d number(s) dialed and not reached (ending %s)" % (
            street or u[:8], len(numbers), ", ".join(n["number"][-4:] for n in numbers)))
    if not items:
        return
    ctx.out.acted += len(items)
    by = send_by.strftime("%I:%M%p").lstrip("0").lower()
    if not ctx.live:
        ctx.reserve(usd)
        ctx.out.head += " Would stage %d afternoon text(s) to %d owner(s) dialed and not reached, to go out by %s." % (
            messages, len(items), by)
        return
    bid = textlane.stage_batch(ctx, items, usd)
    ctx.out.batch = bid
    ctx.out.head += (" Staged batch %d: %d afternoon text(s) to %d owner(s) dialed and not reached. Nothing is sent until a person "
                     "releases it before %s: python src/agents/cli.py autopilot release %d --commit" % (bid, messages, len(items), by, bid))
