"""The cadence tuner (build plan 6.6): are we following up too slowly? Read it off the houses that sold.

A cold lead that sells 30 days after our last conversation was on a 45-day follow-up: the next call
was always going to arrive after the closing. Nothing errors when that happens. The lead just turns
up Sold, and the interval that lost it stays where it was. The teaching's fix is to look, every
month, at each house that sold and compare the day we last SPOKE to the owner with the day it sold,
then shorten the interval that keeps losing, 15 days at a time. That study was run once by hand on
2026-11-12; this is the same study on a clock.

For every record whose sale date falls in the window it works out:

    the last conversation with the owner before the sale (a conversation as the catalog defines it:
    an answered call of a minute or more, or an inbound text; a dial nobody answered is not one)
    the days from that conversation to the sale
    the group the record was in when it was lost: a cold, warm or hot lead, a Not Interested owner
    (by the wait its list gets), another kind of lead, or never a lead at all

A group whose sales mostly landed INSIDE its own interval gets a proposal: one step shorter. A cold
interval is never proposed at or under the warm one, and a Not Interested wait never under the
shortest wait the catalog holds. Warm and hot leads get no proposal at all: the teaching's reading
of a warm lead that sold is that it was graded wrong, not that it was called too seldom.

IT PROPOSES. IT NEVER EDITS. The intervals live in the KPI catalog and in DataSift's own task
presets, and both are the owner's to change. This module has no write in it: not to the account, not
to the playbook, not to the catalog. The proposal and every sale behind it are kept as a file, so a
person can check the evidence before moving a number.

Reads, bounded: one search for the sold records in the window (the rows carry the owner), then one
record read for each sold record whose owner has a conversation in the log, up to `max_records`.
A sale already studied is remembered and not opened again. It belongs on the night list.

What it cannot see: who bought. The teaching speaks of leads "selling to investors"; the record's
sale date does not say who the buyer was, so every sale after a conversation is counted.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import activity, budget, kpi, lanes, ledger
from .stages import Ctx, addr

log = logging.getLogger(__name__)

FACT = "cadence_sale"
USES = ("cadence.cold_days", "cadence.warm_days", "cadence.hot_days", "cadence.not_interested_auction_days",
        "cadence.not_interested_priority_days", "cadence.not_interested_other_days", "cadence.step_days",
        "cadence.min_sales_to_propose")
# group -> (how the report names it, the catalog figure that holds its interval, or "" for none)
GROUPS = {
    "cold": ("Cold leads", "cadence.cold_days"),
    "warm": ("Warm leads", "cadence.warm_days"),
    "hot": ("Hot leads", "cadence.hot_days"),
    "not_interested_auction": ("Not Interested owners on a foreclosure list", "cadence.not_interested_auction_days"),
    "not_interested_priority": ("Not Interested owners on probate and priority lists", "cadence.not_interested_priority_days"),
    "not_interested_other": ("Not Interested owners on other lists", "cadence.not_interested_other_days"),
    "other_lead": ("Leads in another phase", ""),
    "never_a_lead": ("Owners who were never a lead", ""),
}
ORDER = tuple(GROUPS)


def _marks(rec: dict) -> set[str]:
    out = set()
    for key in ("tags", "lists"):
        for t in rec.get(key) or []:
            n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
            if n:
                out.add(str(n).strip().lower())
    return out


def group_of(status_before: str, rec: dict, st, lanes_cfg=None) -> str:
    """The follow-up group a record was in when it was lost, from the last status it held before
    the sale and, for a Not Interested owner, the list that sets its wait."""
    s = (status_before or "").strip().lower()
    if s == "cold lead":
        return "cold"
    if s == "warm lead":
        return "warm"
    if s == "hot lead":
        return "hot"
    if s == "not_interested":
        marks = _marks(rec)
        lane = lanes.lane_of(dict(rec, status=None), lanes_cfg)
        if lane == "hot_auction" or marks & {str(x).lower() for x in (st.get("auction_marks") or ["Foreclosure", "Auction Soon"])}:
            return "not_interested_auction"
        if lane in ("priority1", "strong") or marks & {str(x).lower() for x in (st.get("priority_marks") or ["Probate"])}:
            return "not_interested_priority"
        return "not_interested_other"
    if s in lanes.LEAD_STATUSES:
        return "other_lead"
    return "never_a_lead"


def status_before(record_uuid: str, sale_end: datetime, sold: set[str]) -> str:
    """The last status the record held before the sale that is not the sale itself."""
    last = ""
    for e in activity.events("property.status.updated", resource=record_uuid, until=sale_end):
        to = str(e["data"].get("to") or "").strip().lower()
        if to and to not in sold:
            last = to
    return last


def _sale_day(rec: dict) -> date | None:
    try:
        return date.fromisoformat(str(rec.get("last_sold") or "")[:10])
    except ValueError:
        return None


def study(ctx: Ctx, cat: kpi.Catalog, since: date, today: date) -> tuple[list[dict], dict]:
    """Every sold record in the window that we had spoken to, each with its gap, and the counts of
    the ones that were set aside."""
    st = ctx.stage
    tz = ctx.policy.tz
    sold = [str(s) for s in (st.get("sold_statuses") or ["sold"])]
    sold_low = {s.lower() for s in sold}
    rows = ctx.crm.rows({"any_property_status": sold, "last_sold": [since.isoformat(), (today + timedelta(days=1)).isoformat()]},
                        cap=int(st.get("max_scan", 2000)))
    counts = {"sold": len(rows), "no_conversation": 0, "no_sale_date": 0, "outside": 0, "left": 0, "read": 0}
    cap = int(st.get("max_records", 150))
    out = []
    spent = False
    for row in rows:
        u, owner = row.get("uuid") or "", (row.get("owner") or {}).get("uuid") or ""
        if not u:
            continue
        known = ledger.fact(u, FACT)
        if known:
            if known.get("sold", "") >= since.isoformat():
                out.append(known)
            else:
                counts["outside"] += 1
            continue
        talks = kpi.conversation_events(cat, owner=owner) if owner else []
        if not talks:
            counts["no_conversation"] += 1
            continue
        if counts["read"] >= cap or spent:
            counts["left"] += 1
            continue
        counts["read"] += 1
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted as exc:
            # The report is monthly: a spent budget must not cost the month. Stop opening records
            # and report on the sales already read; the rest are counted and wait for the next run.
            ctx.out.problem("stopped opening sold records: %s" % exc)
            spent = True
            counts["read"] -= 1
            counts["left"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a sold record (%s); it is tried again next month" % str(exc)[:100])
            continue
        day = _sale_day(rec)
        if day is None:
            counts["no_sale_date"] += 1
            continue
        if not since <= day <= today:
            counts["outside"] += 1
            continue
        sale_end = kpi.day_start(day, tz) + timedelta(days=1)
        before = [t for t in talks if t["when"] < sale_end]
        if not before:
            counts["no_conversation"] += 1     # we spoke only after it had sold: that is the new owner
            continue
        last = before[-1]
        last_day = date.fromisoformat(kpi.local_day(last["when"], tz))
        sale = {"record": u, "street": addr(rec), "sold": day.isoformat(), "last_conversation": last_day.isoformat(),
                "by": last["kind"], "gap": (day - last_day).days,
                "group": group_of(status_before(u, sale_end, sold_low), rec, st, getattr(ctx.policy, "lanes", None)),
                "lane": lanes.lane_of(dict(rec, status=None), getattr(ctx.policy, "lanes", None))}
        ledger.set_fact(u, FACT, sale)          # what was learned about this sale: kept, so it is opened once
        out.append(sale)
    return out, counts


def propose(cat: kpi.Catalog, group: str, gaps: list[int]) -> dict:
    """What the sales in one group say about its interval:
    {"interval", "sold", "inside", "median", "new", "why"}; "new" is None when nothing is proposed."""
    label, key = GROUPS[group]
    out = {"group": group, "label": label, "sold": len(gaps), "median": statistics.median(gaps) if gaps else None,
           "interval": None, "inside": None, "new": None, "why": ""}
    if not key:
        out["why"] = "no follow-up interval is set for this group"
        return out
    interval = cat.value(key)
    out["interval"] = interval
    out["inside"] = sum(1 for g in gaps if g < interval)
    need = int(cat.value("cadence.min_sales_to_propose"))
    if len(gaps) < need:
        out["why"] = "too few sales to judge (the tuner needs %d)" % need
        return out
    if out["inside"] * 2 <= len(gaps):
        out["why"] = "most of these sold after a follow-up was already due, so the interval is not what lost them"
        return out
    if group in ("warm", "hot"):
        out["why"] = "a %s lead should not be lost at all: check how these were graded" % group
        return out
    new = interval - cat.value("cadence.step_days")
    if group == "cold":
        ok = new > cat.value("cadence.warm_days")
    else:
        ok = new >= cat.value("cadence.not_interested_auction_days")
    if not ok:
        out["why"] = "the interval is already as short as the tuner will propose"
        return out
    out["new"] = new
    out["why"] = "more than half sold before the next follow-up was due"
    return out


def run_cadence(ctx: Ctx) -> None:
    st = ctx.stage
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    tz = ctx.policy.tz
    kpi.sync_log(ctx, ("calls", "state", "texts"))
    today = date.fromisoformat(kpi.local_day(ctx.now, tz))
    days = int(st.get("days", 120))
    since = today - timedelta(days=days)
    sales, counts = study(ctx, cat, since, today)
    ctx.out.found = counts["sold"]
    reach = kpi.log_reach(("owner.call.answered", "owner.call.made"))
    ctx.out.line("Sold in the last %d days: %d record(s). %d had a conversation with us before the sale, %d did not%s%s." % (
        days, counts["sold"], len(sales), counts["no_conversation"],
        (", and %d were left for the next run" % counts["left"]) if counts["left"] else "",
        (" (the call log reaches back to %s)" % kpi.fmt_day(kpi.local_day(reach, tz))) if reach else " (the call log is empty)"))
    by: dict[str, list] = defaultdict(list)
    for s in sales:
        by[s["group"]].append(s["gap"])
    results = [propose(cat, g, by[g]) for g in ORDER if by.get(g)]
    proposals = [r for r in results if r["new"] is not None]
    for r in results:
        if r["interval"] is None:
            ctx.out.line("%s: %d sold after a conversation, median %d days later." % (r["label"], r["sold"], round(r["median"])))
        else:
            ctx.out.line("%s (%s between follow-ups): %d sold, %d of them inside the interval, median %d days after the "
                         "last conversation. %s." % (r["label"], cat.text(GROUPS[r["group"]][1]), r["sold"], r["inside"],
                                                     round(r["median"]),
                                                     ("Proposal: every %d days instead of %d" % (r["new"], r["interval"]))
                                                     if r["new"] is not None else "No change: %s" % r["why"]))
    for r in proposals:
        ctx.out.tally["proposal: %s from %d to %d days" % (r["label"].lower(), r["interval"], r["new"])] = r["sold"]
    auction = sum(1 for s in sales if s["lane"] == "hot_auction" or s["group"] == "not_interested_auction")
    if auction:
        ctx.out.line("%d of these were foreclosure records, where a notice lands a median %s before its sale: a shorter "
                     "interval matters most there." % (auction, cat.text("cycle.foreclosure_notice_to_sale_days")))
    for label, n in (("sold record(s) not opened this run (read cap)", counts["left"]),
                     ("sold record(s) with no sale date on the record", counts["no_sale_date"])):
        if n:
            ctx.out.tally[label] = n
    ctx.out.line("Nothing was changed. These are proposals: the intervals in team/kpi.yaml and in DataSift's task "
                 "presets are the owner's to edit.")
    ctx.out.line(cat.footnote(USES))
    ctx.out.acted = len(proposals)
    path = kpi.save_report("cadence", ctx.now, {"day": today.isoformat(), "window_days": days, "counts": counts,
                                                "groups": results, "sales": sales})
    if proposals:
        ctx.out.head = "Cadence check: %d proposal(s), nothing changed: %s. From %d sale(s) we had spoken to; the evidence is in %s." % (
            len(proposals), "; ".join("%s every %d days instead of %d" % (r["label"].lower(), r["new"], r["interval"])
                                      for r in proposals), len(sales), path.name)
    else:
        ctx.out.head = "Cadence check: no change proposed. %d sale(s) we had spoken to, out of %d sold in %d days." % (
            len(sales), counts["sold"], days)
