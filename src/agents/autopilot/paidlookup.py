"""Paid lookups by a person (build plan 3.10): a short queue of records worth one more search.

Every automated source has a floor: DataSift's own trace, the paid skip traces, then research. A
record all of them missed is usually given up. A few are worth more than that: a house with a sale
date ahead, or one with heavy equity. Those go to a SHORT queue that a person works through the
paid people-search tools by hand, once a week.

  * WHO GETS IN. Every skip trace source the playbook names has been tried on the record (the
    ledger knows which), DataSift's own trace has run, research has run, and the record still
    holds no number a caller may dial. Then it has to be worth the money: a sale inside the hot
    window, or equity above a floor in dollars AND in share of value.
  * THE QUEUE IS A TAG AND A PRESET. The stage tags the record (Paid Lookup) and a preset in the
    account shows the tagged records: that preset is what the assistant opens. At most
    `max_queue` records carry the tag at once, the soonest sale first, then the most equity.
  * HOW A RECORD LEAVES. A number that was not on the record when it was tagged is a hit, and the
    tag comes off. The assistant marks a search that found nothing with the miss tag, and the
    stage takes the queue tag off for them. A record that sold, became a lead or no longer passes
    the gate comes off too. A record is queued once: a miss is not searched again next month.
  * ONE TASK A WEEK for the assistant, naming the preset and how many records wait. Until the
    playbook names that person (decision D37), no task is written and the stage says so.

The stage itself spends nothing: the paid search is a person's. It counts the queue and the hit
rate (hits out of hits plus misses), which is what says whether the searches are worth paying for.

A dead owner's record never gets in from here: the gate holds it for research, and research is
what names the person worth looking up.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .. import store
from . import gate, lanes, ledger, researchqueue, researchroutes
from .stages import Ctx, addr, resolve_callers, stage_rows, tag_titles

log = logging.getLogger(__name__)

ACTION = "paid_lookup"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULTS = {
    "tag": "Paid Lookup",
    "miss_tag": "Paid Lookup Miss",       # the assistant's mark for a search that found nothing
    "queue_preset": "Paid Lookup Queue",
    "sources": ["smartskip", "tracerfy"],  # the paid traces that must already have been tried
    "after_research": True,
    "researched_tags": ["Deep Prospected"],
    "research_workflow": "deep_prospect",
    "max_queue": 15,
    "max_reads": 80,
    "hot_days": 45,
    "hot_tags": ["Auction Soon", "Auction Critical"],
    "min_equity_pct": 60,
    "min_equity_usd": 100000,
    "assistant": None,                    # a first name or {name, uuid} (decision D37)
    "task_weekday": "mon",
    "sale_date_fields": ["Foreclosure Date"],
    "sale_record_fields": ["auction_date", "tax_auction_date"],
}


def options(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k)
        o[k] = v if got is None else got
    return o


def numbers_on(rec: dict) -> set[str]:
    return {gate.digits(p.get("number")) for p in ((rec.get("owner") or {}).get("phones") or [])} - {""}


def house_gone(rec: dict, custom: dict | None, o: dict, today: date, dpb=None) -> tuple[list[str], str]:
    """(the sale dates on file, why the house is no longer worth a paid search or ''). The same check
    the research queue runs, by the same rules: sold since the notice, listed, or a sale date that
    has passed. The Deep Prospected tag is not a reason here: these records are researched already."""
    sales = researchqueue.sale_dates(rec, custom, ledger.fact(rec.get("uuid") or "", "auction_dates"),
                                     {"sale_date_fields": [str(x) for x in o["sale_date_fields"]],
                                      "sale_record_fields": [str(x) for x in o["sale_record_fields"]]}, dpb)
    held = researchqueue.house_check(rec, sales, today, dpb, redo=True) if dpb is not None else ""
    return sales, researchqueue.HELD_WORDS.get(held, held)


def worth(rec: dict, custom: dict | None, o: dict, today: date, dpb=None) -> tuple[bool, str, tuple]:
    """(worth a paid search, why in words, the order it is worked in). A sale ahead comes first, by
    date; then equity, the most dollars first. A house that sold, is listed or whose sale date has
    passed is not worth one, whatever its equity reads."""
    tags = {t.lower() for t in tag_titles(rec)}
    sales, gone = house_gone(rec, custom, o, today, dpb)
    if gone:
        return False, "not worth a paid search: %s" % gone, (9, 0)
    ahead = [d for d in sales if d > today.isoformat()]
    if ahead:
        days = (date.fromisoformat(ahead[0]) - today).days
        if days <= int(o["hot_days"]):
            return True, "sale in %d day%s" % (days, "" if days == 1 else "s"), (0, days)
    if tags & {str(t).lower() for t in o["hot_tags"]}:
        return True, "a sale date is close", (0, int(o["hot_days"]))
    value = researchqueue.money(rec.get("estimate_value"))
    pct = researchqueue.money(rec.get("equity_percent"))
    equity = value * pct / 100.0
    if pct >= float(o["min_equity_pct"]) and equity >= float(o["min_equity_usd"]):
        return True, "about ${:,.0f} of equity ({:.0f}%)".format(equity, pct), (1, -equity)
    return False, "not worth a paid search (no sale close, equity under the floor)", (9, 0)


def every_source_missed(rec: dict, o: dict) -> tuple[bool, str]:
    """Has everything automatic been tried, with nothing a caller may dial to show for it?"""
    u = rec.get("uuid") or ""
    ow = rec.get("owner") or {}
    if not (ow.get("skiptraced") or rec.get("skiptraced")):
        return False, "DataSift's own trace has not run yet"
    tried = ledger.tried_sources(u)
    missing = [s for s in o["sources"] if s not in tried]
    if missing:
        return False, "not every paid trace has been tried yet"
    if o["after_research"]:
        tags = {t.lower() for t in tag_titles(rec)}
        done = bool(tags & {str(t).lower() for t in o["researched_tags"]}) \
            or researchqueue.research_state(u, str(o["research_workflow"]))["done"]
        if not done:
            return False, "research has not run yet"
    return True, ""


def hit_rate() -> tuple[int, int]:
    """(hits, searches that came back): a hit is a record that left the queue with a new number, a
    miss is one the assistant marked as found nothing."""
    hits = misses = 0
    for e in ledger.events(action=ACTION):
        why = e["detail"].get("why")
        hits += 1 if why == "hit" else 0
        misses += 1 if why == "miss" else 0
    return hits, hits + misses


def _weekly_task(ctx: Ctx, o: dict, waiting: int) -> None:
    st = ctx.stage
    local = ctx.now.astimezone(ZoneInfo(ctx.policy.tz))
    week = "%d-W%02d" % (local.isocalendar()[0], local.isocalendar()[1])
    key = "autopilot:paidlookup:week:%s" % st.id
    start = str(o["task_weekday"]).lower()[:3]
    if not waiting or store.kv_get(key) == week or local.weekday() < (DAYS.index(start) if start in DAYS else 0):
        return
    who = o["assistant"]
    people = resolve_callers(ctx, [who]) if who else []
    if not people:
        ctx.out.line("No assistant is named for the paid lookups yet (decision D37), so the weekly task is not written.")
        return
    title = "Paid lookups: %d record(s) in the preset %s" % (waiting, o["queue_preset"])
    notes = ("Run each record through the paid people-search tools. Add a number you find to the record. If nothing "
             "turns up, tag the record %s. The queue takes care of the rest." % o["miss_tag"])
    ctx.write("paid lookup task", researchroutes.create_task, ctx.crm, title, user_uuid=people[0]["uuid"],
              due=researchroutes.end_of_day(ctx.now, ctx.policy.tz, days=max(0, 4 - local.weekday())), notes=notes)
    ctx.out.line("%s this week's paid lookup task for %s (%d record(s))." % (
        "Created" if ctx.live else "Would create", people[0]["name"], waiting))
    ctx.out.tally["weekly paid lookup task"] += 1
    if ctx.live:
        store.kv_set(key, week)
        ledger.event(ctx.run_id, st.id, "", ACTION, {"task": True, "to": people[0]["name"], "week": week, "n": waiting},
                     now=ctx.now, tz=ctx.policy.tz)


def run_paidlookup(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    tag, miss = str(o["tag"]), str(o["miss_tag"])
    dpb = researchqueue.batch_rules()
    try:
        if str(o["queue_preset"]) not in ctx.crm.presets():
            ctx.out.problem("there is no preset titled %r in this account; create it (records tagged %s) so the "
                            "assistant has a queue to open" % (o["queue_preset"], tag))
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the presets (%s)" % str(exc)[:80])
    tag_id = ctx.crm.tag_uuid(tag)
    queued = ctx.crm.rows({"all_tags": [tag_id]}, cap=500) if tag_id else []
    reads = 0
    staying = 0
    came_off = 0
    # ---- the records already in the queue: who found a number, who was marked a miss, who left
    for row in queued:
        u = row["uuid"]
        if reads >= int(o["max_reads"]):
            staying += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        added = [e for e in ledger.events(u, ACTION) if e["detail"].get("add")]
        before = set(added[-1]["detail"].get("numbers") or []) if added else None
        tags = {t.lower() for t in tag_titles(rec)}
        why = ""
        if before is not None and numbers_on(rec) - before:
            why = "hit"
        elif miss.lower() in tags:
            why = "miss"
        elif lanes.status_of(rec) in lanes.LEAD_STATUSES:
            why = "a lead now"
        else:
            v = ctx.verdict(rec, "trace", row=row)
            if not v.ok:
                why = v.reason()
            else:
                # A sale date passes and a house sells while a record waits for the assistant.
                why = house_gone(rec, researchqueue.custom_of(ctx, u, max_age=timedelta(days=7)), o, today, dpb)[1]
        if not why:
            staying += 1
            continue
        ctx.change(u, "remove_tags", [tag], None)
        ctx.write("paid lookup done", ctx.crm.remove_tags, u, [tag])
        came_off += 1
        ctx.out.tally["came off the queue: %s" % {"hit": "a number was found", "miss": "the search found nothing"}.get(why, why)] += 1
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, ACTION, {"add": False, "why": why}, now=ctx.now, tz=ctx.policy.tz)

    # ---- who else is worth a paid search
    room = max(0, int(o["max_queue"]) - staying)
    in_queue = {r["uuid"] for r in queued}
    found = []
    rows = stage_rows(ctx) if st.presets else []
    if dpb is None:
        # The same refusal the research queue makes: a house nobody checked for a sale or a passed
        # sale date is not put in front of a person to spend money on.
        ctx.out.problem("the research rules (deep_prospect_batch) did not load on this box, so no record was added to "
                        "the paid lookup queue")
        rows = []
    ctx.out.found = len(rows)
    for _, row in rows:
        u = row["uuid"]
        if u in in_queue or any(e["detail"].get("add") is not None for e in ledger.events(u, ACTION)):
            continue                                   # in the queue now, or searched once already
        if not room:
            ctx.out.tally["not read: the queue is full (%d)" % int(o["max_queue"])] += 1
            continue
        if reads >= int(o["max_reads"]):
            ctx.out.tally["left for the next run (read cap)"] += 1
            continue
        if not gate.check_row(row, "trace", cfg=gate_cfg, allow=st.get("gate_allow") or []).ok:
            continue
        reads += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid") or not ctx.passes(rec, "trace", row=row):
            continue
        if gate.usable_numbers(rec, "call", gate_cfg, row):
            ctx.out.tally["has a number a caller may dial"] += 1
            continue
        ok, why = every_source_missed(rec, o)
        if not ok:
            ctx.out.tally["not yet: %s" % why] += 1
            continue
        custom = researchqueue.custom_of(ctx, u, max_age=timedelta(days=7))
        good, why, key = worth(rec, custom, o, today, dpb)
        if not good:
            ctx.out.tally[why] += 1
            continue
        found.append((key, rec, why))
    found.sort(key=lambda x: x[0])
    added = 0
    for _key, rec, why in found[:room]:
        u = rec["uuid"]
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("paid lookup", ctx.crm.add_tags, u, [tag], rec)
        added += 1
        ctx.out.line("%s: %s" % (addr(rec), why))
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, ACTION, {"add": True, "why": why, "numbers": sorted(numbers_on(rec))},
                         now=ctx.now, tz=ctx.policy.tz)
    if len(found) > room:
        ctx.out.tally["worth a search, waiting for room in the queue"] = len(found) - room
    waiting = staying + added
    _weekly_task(ctx, o, waiting)
    hits, tried = hit_rate()
    ctx.out.acted = added + came_off
    ctx.out.head = "Paid lookup queue: %d waiting (%s %d, %d came off)%s." % (
        waiting, "added" if ctx.live else "would add", added, came_off,
        ("; %d of %d searches found a number" % (hits, tried)) if tried else "")
