"""The goal sheet (build plan 6.4): the revenue goal worked back to dials, each number marked.

A dial count means nothing until it is set against what the business needs. The sheet starts from
the revenue goal and works back the way the teaching's KPI sheet does:

    revenue goal a year
      -> deals a month        (the average deal)
      -> contracts a month    (the share of signed contracts that close)
      -> leads a month        (leads a deal, the cautious end of the range)
      -> dials a month        (dials a lead, the cautious end)
      -> conversations        (the caller floors: conversations for every day's dials)

Then it reads what actually happened and marks each number BEHIND, AT RISK or ON TRACK. Every figure
in that chain comes from the KPI catalog, and the sheet ends by saying which of them are still
recommended or placeholders, so a mark against a stand-in goal is never mistaken for a verdict.

WINDOWS, said on every line, because a number without its window cannot be argued with:
  * dials, conversations and leads are judged on the last 30 days;
  * contracts and closed deals on the last 90 days (a month holds too few to judge);
  * each caller on the last 90 days, never on a week: a caller's line always says how many calling
    days are behind it. When the capture or the log does not reach that far back yet, the need is
    scaled to the days it does hold and the line says so.

A QUARTERLY OWNER REVIEW sits on top, on the first sheet of each quarter: days to a first deal,
net margin and market share against the catalog. Each of the three needs a number only the owner
has (the day marketing began, what a deal paid, the county's investor deal count); a missing one is
named, never guessed.

It reads only: the capture's stored days, the shared log copy, and a capped number of record reads
to credit a new lead to the caller whose conversation produced it.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .. import store
from . import capture, dealsource, kpi
from .stages import Ctx

log = logging.getLogger(__name__)

KV_REVIEW = "autopilot:goals:review"
NOT_CALLERS = ("unknown", kpi.UNASSIGNED)
USES = ("goal.revenue_per_year", "goal.average_deal", "goal.at_risk_share", "goal.activity_window_days",
        "goal.deal_window_days", "funnel.contracts_that_close", "funnel.leads_per_deal", "funnel.dials_per_lead",
        "funnel.dials_per_correct_number", "caller.dials_per_day", "caller.conversations_per_day",
        "caller.opportunities_per_day", "caller.judge_days")
REVIEW_USES = ("owner.days_to_first_deal", "owner.net_margin", "owner.market_share_ceiling")


def needs(cat: kpi.Catalog) -> dict:
    """What one month has to hold for the year's goal to be met, worked back step by step."""
    deals = cat.value("goal.revenue_per_year") / 12.0 / cat.value("goal.average_deal")
    contracts = deals / cat.share("funnel.contracts_that_close")
    leads = deals * cat.high("funnel.leads_per_deal")
    dials = leads * cat.high("funnel.dials_per_lead")
    conversations = dials * cat.low("caller.conversations_per_day") / cat.value("caller.dials_per_day")
    return {"deals": deals, "contracts": contracts, "leads": leads, "dials": dials, "conversations": conversations}


def _span(window_days: int, today: date, first: date | None) -> int:
    """How many of the window's days the data actually covers."""
    if first is None:
        return 0
    return max(0, min(window_days, (today - first).days))


def _n(x: float) -> str:
    """A count for a sentence: whole when it is whole or large, one decimal when it is small."""
    return kpi.num(round(x)) if x >= 10 or abs(x - round(x)) < 0.05 else kpi.num(round(x, 1))


def caller_lines(ctx: Ctx, cat: kpi.Catalog, today: date, credit: dict) -> tuple[list[str], list[dict]]:
    """One line per caller over the judging window, against the caller floors."""
    tz = ctx.policy.tz
    days = int(cat.value("caller.judge_days"))
    since, until = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    risk = cat.share("goal.at_risk_share")
    per: dict[str, dict] = defaultdict(lambda: {"days": 0, "dials": 0, "conversations": 0})
    unnamed = 0
    for r in kpi.caller_days(cat, since, until, tz):
        if r["caller"] in NOT_CALLERS:
            unnamed += r["dials"]
            continue
        if not r["dials"]:
            continue
        p = per[r["caller"]]
        p["days"] += 1
        p["dials"] += r["dials"]
        p["conversations"] += r["conversations"]
    lines, rows = [], []
    for who, p in sorted(per.items(), key=lambda kv: -kv[1]["dials"]):
        d, c = p["dials"] / p["days"], p["conversations"] / float(p["days"])
        o = credit.get(who, 0) / float(p["days"])
        marks = (kpi.mark(d, cat.value("caller.dials_per_day"), risk), kpi.mark(c, cat.low("caller.conversations_per_day"), risk),
                 kpi.mark(o, cat.low("caller.opportunities_per_day"), risk))
        rows.append({"caller": who, "calling_days": p["days"], "dials_per_day": round(d, 1), "conversations_per_day": round(c, 1),
                     "opportunities_per_day": round(o, 2), "marks": marks})
        lines.append("%s, last %d days (%d calling day(s) captured): %s dials a day, %s; %s conversations a day, %s; "
                     "%s opportunities a day, %s. Floors: %s, %s and %s." % (
                         who, days, p["days"], _n(d), marks[0], _n(c), marks[1], _n(o), marks[2],
                         kpi.num(cat.value("caller.dials_per_day")), kpi.num(cat.low("caller.conversations_per_day")),
                         kpi.num(cat.low("caller.opportunities_per_day"))))
    if unnamed:
        lines.append("%s dial(s) in those days carry no caller's name (two callers dialed the same day): they are in "
                     "the totals and in nobody's average." % kpi.num(unnamed))
    return lines, rows


def credit_leads(ctx: Ctx, cat: kpi.Catalog, since: datetime, until: datetime) -> tuple[dict, int]:
    """caller -> the new leads in the window whose conversation was theirs, and how many leads no
    caller could be named for (a text reply, a lead older than the log, or the read cap)."""
    reads = {"left": int(ctx.stage.get("max_record_reads", 40))}
    out: Counter = Counter()
    unplaced = 0
    for lead in kpi.new_leads(since=since, until=until):
        owner = dealsource.owner_of(ctx, lead["record"], reads)
        who = dealsource.origin(cat, owner, lead["when"])["caller"] if owner else ""
        if who:
            out[who] += 1
        else:
            unplaced += 1
    if reads.get("budget"):
        ctx.out.problem("the hour's request budget ran out while crediting leads to callers, so some are credited to nobody this week")
    return dict(out), unplaced


def owner_review(ctx: Ctx, cat: kpi.Catalog, today: date) -> tuple[list[str], dict]:
    """The quarterly review: days to a first deal, net margin, market share. Each line is a number
    against the catalog, or the name of the input that is missing."""
    tz = ctx.policy.tz
    lines, data = ["Quarterly owner review."], {}
    closed = [str(s) for s in (ctx.stage.get("closed_statuses") or ["closed"])]
    recorded = dealsource.deals()
    first_days = [d["closed_day"] for d in recorded] + [kpi.local_day(e["when"], tz) for e in kpi.status_changes(closed)]
    first_deal = min(first_days) if first_days else ""
    started = cat.input("marketing_started")
    want = cat.text("owner.days_to_first_deal")
    if not started:
        lines.append("Days to a first deal: not read. Type the day marketing began under inputs.marketing_started in "
                     "team/kpi.yaml (the catalog says %s)." % want)
    elif first_deal:
        n = (date.fromisoformat(first_deal) - date.fromisoformat(started)).days
        data["days_to_first_deal"] = n
        lines.append("Days to a first deal: %d (the catalog says %s)." % (n, want))
    else:
        n = (today - date.fromisoformat(started)).days
        data["days_without_a_deal"] = n
        lines.append("Days to a first deal: none has closed yet, %d days after marketing began (the catalog says %s)." % (n, want))

    quarter_start = today - timedelta(days=91)
    in_quarter = [d for d in recorded if d["closed_day"] >= quarter_start.isoformat()]
    fees = [float((d.get("detail") or {}).get("fee") or 0) for d in in_quarter]
    if in_quarter and all(f > 0 for f in fees):
        paid = dealsource.spend(quarter_start, today)
        costs = sum(paid.values()) + 3 * sum(cat.monthly_costs().values())
        margin = 100.0 * (sum(fees) - costs) / sum(fees)
        data["net_margin"] = round(margin, 1)
        unseen = [n for n in ("dialer", "texting", "salaries", "subscriptions") if n not in cat.monthly_costs()]
        lines.append("Net margin, last 91 days: %d%%, %s in deals against %s of cost (the catalog says %s).%s" % (
            round(margin), kpi.money(sum(fees)), kpi.money(costs), cat.text("owner.net_margin"),
            (" It reads high: %s are not typed in under inputs.monthly_costs." % ", ".join(unseen)) if unseen else ""))
    elif not in_quarter:
        lines.append("Net margin: no deal is recorded in the last 91 days, so there is none to read (the catalog says %s)." %
                     cat.text("owner.net_margin"))
    else:
        lines.append("Net margin: not read. What a deal paid is not recorded at closing (set fee_field on the deal-source "
                     "stage); the catalog says %s." % cat.text("owner.net_margin"))

    market = cat.input("market_investor_deals_6mo")
    half_year = [d for d in recorded if d["closed_day"] >= (today - timedelta(days=183)).isoformat()]
    if market:
        share = 100.0 * len(half_year) / float(market)
        data["market_share"] = round(share, 2)
        lines.append("Market share: %d of %s investor deals in six months, %s%% (the ceiling is %s)." % (
            len(half_year), kpi.num(market), kpi.num(round(share, 1)), cat.text("owner.market_share_ceiling")))
    else:
        lines.append("Market share: not read. Type the county's six-month investor deal count under "
                     "inputs.market_investor_deals_6mo (the ceiling is %s)." % cat.text("owner.market_share_ceiling"))
    return lines, data


def _quarter(d: date) -> str:
    return "%d-Q%d" % (d.year, (d.month - 1) // 3 + 1)


def run_goals(ctx: Ctx) -> None:
    st = ctx.stage
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    tz = ctx.policy.tz
    kpi.sync_log(ctx, ("calls", "state", "texts"))
    today = date.fromisoformat(kpi.local_day(ctx.now, tz))
    now_start = kpi.day_start(today, tz)
    need = needs(cat)
    risk = cat.share("goal.at_risk_share")
    act_days, deal_days = int(cat.value("goal.activity_window_days")), int(cat.value("goal.deal_window_days"))

    captured = capture.rows()
    first_cap = date.fromisoformat(min(r["day"] for r in captured)) if captured else None
    state_reach = kpi.log_reach("property.status.updated")
    first_state = date.fromisoformat(kpi.local_day(state_reach, tz)) if state_reach else None

    # --- activity: the capture's days
    span_a = _span(act_days, today, first_cap)
    rows = kpi.caller_days(cat, (today - timedelta(days=act_days)).isoformat(), (today - timedelta(days=1)).isoformat(), tz)
    got_dials = sum(r["dials"] for r in rows)
    got_conv = sum(r["conversations"] for r in rows)
    span_l = _span(act_days, today, first_state)
    got_leads = len(kpi.new_leads(since=now_start - timedelta(days=act_days), until=now_start))
    # --- deals: the status log
    span_d = _span(deal_days, today, first_state)
    since_d = now_start - timedelta(days=deal_days)
    got_contracts = len(kpi.status_changes(st.get("contract_statuses") or ["under_contract"], since=since_d, until=now_start))
    got_deals = len(kpi.status_changes(st.get("closed_statuses") or ["closed"], since=since_d, until=now_start))

    def judge(got: float, month_need: float, span: int) -> tuple[float, str]:
        needed = month_need * span / 30.0
        return needed, (kpi.mark(got, needed, risk) if span else "not judged")

    marks = {}
    n_dials, marks["dials"] = judge(got_dials, need["dials"], span_a)
    n_conv, marks["conversations"] = judge(got_conv, need["conversations"], span_a)
    n_leads, marks["leads"] = judge(got_leads, need["leads"], span_l)
    n_contracts, marks["contracts"] = judge(got_contracts, need["contracts"], span_d)
    n_deals, marks["deals"] = judge(got_deals, need["deals"], span_d)

    def window(span: int, full: int) -> str:
        return "Last %d days" % full if span >= full else "The %d day(s) on record (of the %d the sheet judges)" % (span, full)

    lines = []
    review_data = {}
    reviewed = False
    mode = str(st.get("owner_review") or "quarterly")
    if mode == "always" or (mode == "quarterly" and store.kv_get(KV_REVIEW) != _quarter(today)):
        review, review_data = owner_review(ctx, cat, today)
        lines += review
        reviewed = True
        # Only a run the queue started marks the quarter as reviewed: a look from the command line
        # must not use up the one review the channel gets.
        if ctx.task_id:
            store.kv_set(KV_REVIEW, _quarter(today))
    if span_a:
        lines.append("%s: %s dials (%s needed, %s), %s conversations (%s needed, %s), %s new lead(s) (%s needed, %s)." % (
            window(min(span_a, span_l) if span_l else span_a, act_days), kpi.num(got_dials), _n(n_dials), marks["dials"],
            kpi.num(got_conv), _n(n_conv), marks["conversations"], kpi.num(got_leads), _n(n_leads), marks["leads"]))
    else:
        lines.append("No calling day is captured yet, so dials and conversations are not judged.")
    if span_d:
        lines.append("%s: %d contract(s) signed (%s needed, %s), %d deal(s) closed (%s needed, %s)." % (
            window(span_d, deal_days), got_contracts, _n(n_contracts), marks["contracts"], got_deals, _n(n_deals), marks["deals"]))
    else:
        lines.append("The status log holds nothing yet, so leads, contracts and deals are not judged.")
    lines.append(cat.footnote(USES + (REVIEW_USES if reviewed else ())))

    judge_days = int(cat.value("caller.judge_days"))
    credit, unplaced = credit_leads(ctx, cat, now_start - timedelta(days=judge_days), now_start)
    c_lines, c_rows = caller_lines(ctx, cat, today, credit)
    lines += c_lines
    if unplaced:
        lines.append("%d new lead(s) in that window are credited to nobody: the log holds no call of theirs before the "
                     "status changed (a text reply reads this way)." % unplaced)

    since_j = now_start - timedelta(days=judge_days)
    correct = kpi.correct_numbers(cat, since=since_j, until=now_start)
    dials_j = kpi.dials(since=since_j, until=now_start)
    want = cat.value("funnel.dials_per_correct_number")
    dpc = None
    if correct["total"]:
        dpc = dials_j / float(correct["total"])
        lines.append("Dials per correct number, last %d days: %s (%s dials, %d numbers confirmed: %d on a call, %d by a "
                     "text or by hand); the catalog says %s, %s." % (
                         judge_days, kpi.num(round(dpc, 1)), kpi.num(dials_j), correct["total"], correct["by_call"],
                         correct["other"], kpi.num(want), kpi.mark(dpc, want, risk, lower_is_better=True)))
    lines.append("Worked back from %s at %s: %s deal(s), %s contract(s), %s lead(s) at %s a deal, %s dials at %s a lead, and "
                 "%s conversations a month." % (
                     cat.text("goal.revenue_per_year"), cat.text("goal.average_deal"), _n(need["deals"]), _n(need["contracts"]),
                     _n(need["leads"]), kpi.num(cat.high("funnel.leads_per_deal")), _n(need["dials"]),
                     kpi.num(cat.high("funnel.dials_per_lead")), _n(need["conversations"])))
    for ln in lines:
        ctx.out.line(ln)

    judged = [m for m in marks.values() if m != "not judged"]
    count = Counter(judged)
    ctx.out.found = len(judged)
    ctx.out.acted = count["behind"] + count["at risk"]
    ctx.out.tally.update({"behind": count["behind"], "at risk": count["at risk"], "on track": count["on track"]})
    goal = cat.fig("goal.revenue_per_year")
    ctx.out.head = "Goal sheet for %s: %d behind, %d at risk, %d on track against %s%s." % (
        kpi.fmt_day(today), count["behind"], count["at risk"], count["on track"], goal.text(),
        " (%s)" % goal.tag() if goal.tag() else "")
    kpi.save_report("goal_sheet", ctx.now, {
        "day": today.isoformat(), "needs_a_month": {k: round(v, 2) for k, v in need.items()},
        "windows": {"activity_days": act_days, "deal_days": deal_days, "caller_days": judge_days,
                    "activity_days_on_record": span_a, "deal_days_on_record": span_d},
        "actual": {"dials": got_dials, "conversations": got_conv, "leads": got_leads, "contracts": got_contracts, "deals": got_deals},
        "needed_so_far": {"dials": round(n_dials, 1), "conversations": round(n_conv, 1), "leads": round(n_leads, 1),
                          "contracts": round(n_contracts, 2), "deals": round(n_deals, 2)},
        "marks": marks, "callers": c_rows, "leads_credited": credit, "leads_credited_to_nobody": unplaced,
        "dials_per_correct_number": round(dpc, 1) if dpc else None, "owner_review": review_data, "lines": lines})
