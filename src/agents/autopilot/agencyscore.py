"""The agency scorecard, once a month: what came back from the calling agency against what it was sent, with
the same numbers for the in-house lanes over the same month beside it.

WHY. The agency track hands an outside calling agency a few thousand owners a month and pays a fee for it,
and nothing measured what the fee bought, or whether a removal the agency was given ever reached our gate.
The course's benchmark (C13.18) is plain about the test that matters: an agency's leads should close at
least one deal a month, and when they do not it reads as a sales problem, not an agency one.

What it reads, all of it the brain's own (it makes no DataSift write and contacts nobody, so plan mode and
live mode are the same report):
  * the returns workflows/agency_results.py kept under DATA_DIR/agency_returns/: per-record files and the
    agency's summary PDFs, each filed under the month the agency called in;
  * what was sent: the agency export's batches marked sent, and the day each record was sent (the export's
    own memory, the fact agency_sent);
  * the shared copy of the account log, counted with the KPI catalog's own definitions (kpi.py), so this
    report and the goal sheet cannot disagree about what a conversation or a lead is. Records and numbers
    the agency's file touched are left out of the in-house side.

What it says:
  * the agency's leads, not interested, wrong and disconnected numbers and removals, and what was written;
  * the cost per lead when the monthly fee is set (blank until the owner sets it), the in-house figure when
    its cost is set too;
  * how the agency's leads of the last few months went: appointments, walks, contracts, closings, and the
    C13.18 line when enough of them produced no contract this month;
  * when only the PDF came back, or nothing did, that no removal the agency was given can be held by the
    gate, and what to ask the agency for (a setting).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity, kpi, ledger

FACT_SENT = "agency_sent"     # agencyexport.FACT_SENT
# max_log_calls (the log refresh's request cap) is read from the block by kpi.sync_log itself.
DEFAULTS = {"month": "last", "monthly_fee": 0, "inhouse_monthly_cost": 0, "ask": "", "lead_months": 3,
            "sales_test_min_leads": 10, "contract_statuses": ["under_contract"], "closed_statuses": ["closed"],
            "appointment_kind": "appointment", "sync": True}
DNC_FAMILY = {"DNC", "CORRECT_DNC", "WRONG_DNC"}


def options(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None and v != "":
            out[k] = v
    return out


def _money(v) -> float | None:
    if v in (None, ""):
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def block_problems(st) -> list[str]:
    """A typo in the stage's block is refused when the playbook loads, never read as a default."""
    out = []
    m = st.get("month")
    if m is not None and m != "" and not (m in ("last", "this") or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(m))):
        out.append("stage %s: month is last, this or a month written YYYY-MM" % st.id)
    for key in ("monthly_fee", "inhouse_monthly_cost"):
        v = _money(st.get(key))
        if v is None or v < 0:
            out.append("stage %s: %s is a dollar amount a month (0 or empty when not set)" % (st.id, key))
    for key, low, high in (("lead_months", 1, 12), ("sales_test_min_leads", 1, 10000), ("max_log_calls", 0, 5000)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high):
            out.append("stage %s: %s must be a whole number from %d to %d" % (st.id, key, low, high))
    for key in ("contract_statuses", "closed_statuses"):
        v = st.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v)):
            out.append("stage %s: %s must be a list of statuses" % (st.id, key))
    v = st.get("ask")
    if v is not None and not isinstance(v, str):
        out.append("stage %s: ask is the sentence of what to ask the agency for" % st.id)
    return out


# ---------------------------------------------------------------------- the window
def month_window(month: str, now: datetime, tz: str) -> tuple[str, datetime, datetime]:
    """(YYYY-MM, its first moment, the next month's first moment), business-local."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone).date()
    if month == "this":
        first = local.replace(day=1)
    elif month == "last" or not month:
        first = (local.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        y, m = (int(x) for x in str(month).split("-"))
        first = date(y, m, 1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (first.strftime("%Y-%m"), datetime(first.year, first.month, 1, tzinfo=zone),
            datetime(nxt.year, nxt.month, 1, tzinfo=zone))


def month_name(label: str) -> str:
    try:
        return date.fromisoformat(label + "-01").strftime("%B %Y")
    except ValueError:
        return label


def months_back(label: str, n: int) -> set[str]:
    """This month and the n - 1 before it, as YYYY-MM."""
    y, m = (int(x) for x in label.split("-"))
    out = set()
    for _ in range(max(1, n)):
        out.add("%04d-%02d" % (y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


# ---------------------------------------------------------------------- what was sent
def sent_in(start: datetime, end: datetime, tz: str) -> dict:
    """Owners in the agency files released in the window (the export's batches), and records whose remembered
    send day falls in it (a file sent before the export stage existed is remembered by record only)."""
    owners = files = 0
    for b in ledger.batches(status="sent"):
        if b.get("kind") != "agency":
            continue
        full = ledger.batch(int(b["id"])) or {}
        try:
            when = datetime.fromisoformat(str(full.get("released") or ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if start <= when < end:
            owners += int((full.get("result") or {}).get("owners") or len(full.get("items") or []))
            files += 1
    lo, hi = start.astimezone(ZoneInfo(tz)).date().isoformat(), end.astimezone(ZoneInfo(tz)).date().isoformat()
    houses = 0
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT value FROM ap_facts WHERE key=?", (FACT_SENT,)).fetchall():
            d = str(r["value"] or "").strip('"')[:10]
            if d and lo <= d < hi:
                houses += 1
    return {"owners": owners, "files": files, "houses": houses}


# ---------------------------------------------------------------------- what came back
def agency_side(returns: list[dict]) -> dict:
    """The month's files read as one (a corrected file posted again is the same calls, not new ones)."""
    from ..workflows import agency_results as ar
    merged = ar.merge_returns(returns)
    recs = merged["records"]
    return {"counts": dict(ar.tally(merged)), "states": dict(Counter(e["state"] for e in recs.values())),
            "rows": merged["rows"], "records": len(recs), "person": ar.person_count(merged)[0]}


def _lead_day(ret: dict, e: dict, tz: str) -> datetime:
    d = e.get("written") or ret.get("day") or ""
    try:
        day = date.fromisoformat(str(d)[:10])
    except ValueError:
        day = datetime.fromisoformat(str(ret.get("received"))).date()
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz))


def leads_after(returns: list[dict], o: dict, start: datetime, end: datetime, tz: str) -> dict:
    """How the agency's leads went once they came in: appointments set, walked (an appointment task done),
    contracts and closings, each from the brain's copy of the account log."""
    kind = str(o["appointment_kind"]).lower()
    contract = {s.lower() for s in o["contract_statuses"]}
    closed = {s.lower() for s in o["closed_statuses"]}
    leads = {}
    for ret in returns:
        for u, e in (ret.get("records") or {}).items():
            if e.get("top") == "lead" and u not in leads:
                leads[u] = _lead_day(ret, e, tz)
    out = Counter()
    for u, since in leads.items():
        appts = {ev["data"].get("task") for ev in activity.events("task.created", resource=u, since=since)
                 if str(ev["data"].get("kind") or "").lower() == kind}
        if appts:
            out["appointments"] += 1
        if appts and any(ev["data"].get("task") in appts for ev in activity.events("task.completed", resource=u, since=since)):
            out["walked"] += 1
        moves = [ev for ev in activity.events("property.status.updated", resource=u, since=since)]
        to = {str(ev["data"].get("to") or "").lower() for ev in moves}
        if to & contract:
            out["contracts"] += 1
        if to & closed:
            out["closed"] += 1
        if any(start <= ev["when"] < end and str(ev["data"].get("to") or "").lower() in contract | closed for ev in moves):
            out["this_month"] += 1
    out["leads"] = len(leads)
    return dict(out)


def pdf_side(returns: list[dict]) -> list[str]:
    from ..workflows.agency_results import show
    lines = []
    for ret in returns:
        nums = ret.get("numbers") or []
        if nums:
            lines.append("The agency's own numbers, from its PDF: %s." % "; ".join("%s %s" % (x["label"], show(x))
                                                                               for x in nums[:8]))
            lines += ret.get("notes") or []
            if ret.get("connect"):
                lines.append("A connect count includes voicemail and any answered line; it is not live conversations.")
        else:
            lines.append("The agency's PDF came back, but its text could not be read in order; a person reads it.")
    return lines


# ---------------------------------------------------------------------- in-house, the same month
def inhouse(start: datetime, end: datetime, skip_records: set, skip_numbers: set, cat) -> dict:
    """The in-house lanes over the same window, by the catalog's definitions. A record or a number the agency's
    file touched is left out, so the two sides never count one answer twice."""
    d = {"dials": kpi.dials(start, end), "conversations": None}
    if cat is not None:
        d["conversations"] = len(kpi.conversation_events(cat, since=start, until=end))
    d["leads"] = sum(1 for x in kpi.new_leads(start, end) if x["record"] not in skip_records)
    d["not_interested"] = sum(1 for x in kpi.status_changes(["not_interested"], start, end) if x["record"] not in skip_records)
    last: dict = {}
    for e in activity.events("owner.phone.status.updated", since=start, until=end):
        n = e["data"].get("number") or ""
        if n and n not in skip_numbers:
            last[(e["resource"], n)] = str(e["data"].get("to") or "").upper()
    c = Counter("dnc" if v in DNC_FAMILY else v.lower() for v in last.values())
    d.update(wrong=c.get("wrong", 0), dead=c.get("dead", 0), dnc=c.get("dnc", 0))
    return d


def _usd(v: float) -> str:
    return "$%s" % format(round(float(v), 2), ",.2f")


def _said(c: dict, keys) -> list[str]:
    from ..workflows.agency_results import SAID
    parts = []
    for k, one, many in SAID:
        if k in keys and c.get(k):
            parts.append("%s %s" % (format(int(c[k]), ","), one if c[k] == 1 else many))
    return parts


# ---------------------------------------------------------------------- the stage
def run_agencyscore(ctx) -> None:
    from ..workflows import agency_results as ar
    o = options(ctx.stage)
    tz = ctx.policy.tz
    label, start, end = month_window(str(o["month"]), ctx.now, tz)
    name = month_name(label)
    returns = ar.load_returns()
    mine = [r for r in returns if r.get("month") == label]
    rec_returns = [r for r in mine if r.get("kind") == "records"]
    pdf_returns = [r for r in mine if r.get("kind") == "pdf"]
    sent = sent_in(start, end, tz)
    if not mine and not sent["owners"] and not sent["houses"]:
        return                                   # nothing went out and nothing came back: no head, nothing posts
    ask = str(o.get("ask") or "").strip() or ar.ASK
    agency = agency_side(rec_returns)
    c = agency["counts"]
    ctx.out.found = agency["records"]
    sent_txt = (_n(sent["owners"], "owner") + " sent") if sent["owners"] else (
        (_n(sent["houses"], "house") + " sent") if sent["houses"] else "")
    fee = _money(o["monthly_fee"]) or 0.0
    if rec_returns:
        bits = _said(c, ("lead", "not_interested", "remove"))
        head = "Agency, %s: %s%s." % (name, _join(bits) or "no lead and no answer to write",
                                      (", from %s" % sent_txt) if sent_txt else "")
        if fee and c.get("lead"):
            head = head[:-1] + ", %s a lead." % _usd(fee / c["lead"])
    elif pdf_returns:
        head = ("Agency, %s: only its summary PDF came back, so no removal it was given can be held by our gate."
                % name)
    else:
        head = "Agency, %s: nothing came back yet%s." % (name, (" from the %s" % sent_txt) if sent_txt else "")
    ctx.out.head = head
    if rec_returns:
        ctx.out.line("Back from the agency (%s on %s): %s." % (_n(agency["rows"], "row"), _n(agency["records"], "record"),
                                                              ar.said_line(c)))
        st = agency["states"]
        ctx.out.line("Written to DataSift: %s; %s waiting; %s for a person." % (
            _n(st.get("written", 0), "record"), format(int(st.get("pending", 0)), ","), format(int(agency["person"]), ",")))
    elif not pdf_returns and (sent["owners"] or sent["houses"]):
        ctx.out.line("Sent in %s: %s." % (name, _n(sent["owners"], "owner") if sent["owners"] else _n(sent["houses"], "house")))
    window = [r for r in returns if r.get("kind") == "records" and r.get("month") in months_back(label, int(o["lead_months"]))]
    follow = leads_after(window, o, start, end, tz) if window else {}
    if follow.get("leads"):
        board = kpi.log_reach(activity.BOARD) is not None
        ctx.out.line("Agency leads from the last %s (%d): %s%s under contract, %d closed." % (
            _n(int(o["lead_months"]), "month"), follow["leads"],
            ("%d with an appointment, %d walked, " % (follow.get("appointments", 0), follow.get("walked", 0))) if board
            else "appointments not known (the brain's log copy has no tasks yet), ",
            follow.get("contracts", 0), follow.get("closed", 0)))
        if follow["leads"] >= int(o["sales_test_min_leads"]) and not follow.get("this_month"):
            ctx.out.line("No contract this month from %d agency leads: the course reads that as a sales problem, not an "
                         "agency one (C13.18)." % follow["leads"])
    for ln in pdf_side(pdf_returns):
        ctx.out.line(ln)
    skip_records = {u for r in returns for u in (r.get("records") or {})}
    skip_numbers = {n for r in returns for e in (r.get("records") or {}).values() for n in (e.get("phones") or {})}
    if o["sync"]:
        kpi.sync_log(ctx, ("calls", "state", "board"))
    try:
        cat = kpi.load()
    except kpi.KpiError:
        cat = None
    reach = kpi.log_reach(("owner.call.made", "property.status.updated"))
    if reach is None:
        ctx.out.line("In-house numbers: the brain's copy of the account log holds nothing yet.")
    elif reach >= end:
        ctx.out.line("In-house numbers: the brain's copy of the account log starts %s, after this month, so there is "
                     "nothing to compare." % reach.astimezone(ZoneInfo(tz)).date())
    else:
        ih = inhouse(start, end, skip_records, skip_numbers, cat)
        parts = [_n(ih["dials"], "dial")]
        if ih["conversations"] is not None:
            parts.append(_n(ih["conversations"], "conversation"))
        parts += [_n(ih["leads"], "lead"), "%s not interested" % format(ih["not_interested"], ","),
                  _n(ih["wrong"], "wrong number"), _n(ih["dead"], "disconnected number"),
                  "%s do not call" % format(ih["dnc"], ",")]
        tail = ""
        if reach > start + timedelta(days=1):
            tail = " (the brain's log copy starts %s, so this is part of the month)" % reach.astimezone(ZoneInfo(tz)).date()
        ctx.out.line("In-house, the same month: %s%s." % (_join(parts), tail))
        cost = _money(o["inhouse_monthly_cost"]) or 0.0
        if fee or cost:
            bits = []
            if fee:
                bits.append("agency %s" % (("%s (%s over %s)" % (_usd(fee / c["lead"]), _usd(fee), _n(c["lead"], "lead")))
                                           if c.get("lead") else "no lead for %s" % _usd(fee)))
            if cost:
                bits.append("in-house %s" % (("%s (%s over %s)" % (_usd(cost / ih["leads"]), _usd(cost), _n(ih["leads"], "lead")))
                                             if ih["leads"] else "no lead for %s" % _usd(cost)))
            ctx.out.line("Cost per lead: %s." % "; ".join(bits))
    if not fee:
        ctx.out.line("Set monthly_fee in this stage's block for the agency's cost per lead.")
    if not rec_returns:
        ctx.out.line("Ask the agency for %s." % ask)
    kpi.save_report("agency_scorecard", ctx.now, {"month": label, "sent": sent, "agency": agency, "follow": follow,
                                                   "returns": [r.get("id") for r in mine]})


def _n(n, one: str, many: str = "") -> str:
    n = int(n or 0)
    return "%s %s" % (format(n, ","), one if n == 1 else (many or one + "s"))


def _join(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]
