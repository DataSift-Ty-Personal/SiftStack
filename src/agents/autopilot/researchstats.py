"""Research results, measured (build plan 3.11).

Research is the most expensive thing done to a single record, and nothing ever checked whether it
worked. A file was written, numbers were added, the record went back to a caller, and that was the
last anyone asked. The teaching makes a claim that can be tested: research should reach a decision
maker on 50% to 70% of records, against 5% to 7% for a plain skip trace, and close about one deal in
ten contacts. If our own number is far under that, the money is going into files nobody dials.

So each researched record is read once, a set number of days after its research (the catalog's
`research.check_after_days`), from the shared log:

    reached     was there a conversation with the owner's record in those days, or a number
                confirmed as right? Heirs' numbers are written onto the same owner, so a call to a
                daughter's number counts here.
    by which    the number that reached them: one research added (it carries research's own tags:
                a relationship, the source that found it) or one that was already on the record
    lead        did the record enter a lead status after the research
    deal        did it close

The window is always the same length, measured from the day the researched tag landed, so a record
checked late is judged on the same days as one checked on time.

Then the table: reached out of checked, against the catalog, and deals out of contacts. Under the
floor, with enough records to mean something, it is a problem line that says what to check first.

Reads, bounded: one record read for each record that has come due, up to `max_records` a run. A
record is checked once and remembered. Nothing is written to the account.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .. import store
from . import activity, budget, dealsource, gate, kpi, lanes
from .stages import Ctx, addr

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_research_checks (
    record_uuid TEXT PRIMARY KEY, researched_at TEXT, checked_at TEXT, reached INTEGER, by_number TEXT,
    lead INTEGER, deal INTEGER, detail TEXT);
"""
ADDED = "a number research added"
OLD = "a number already on the record"
GONE = "a number no longer on the record"
USES = ("research.check_after_days", "research.reached", "research.contacts_per_deal", "research.min_records_to_judge")


def researched(tags, since: datetime | None = None, until: datetime | None = None) -> dict:
    """record -> the moment a researched tag first landed on it, from the stored log."""
    want = {str(t).lower() for t in tags or []}
    out: dict[str, datetime] = {}
    for e in activity.events("property.tags.added", since=since, until=until):
        if e["resource"] and e["resource"] not in out and want & {str(t).lower() for t in e["data"].get("tags") or []}:
            out[e["resource"]] = e["when"]
    return out


def checks() -> list[dict]:
    kpi.ensure("ap_research_checks", SCHEMA)
    out = []
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT * FROM ap_research_checks ORDER BY researched_at, record_uuid").fetchall():
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except ValueError:
                d["detail"] = {}
            out.append(d)
    return out


def _save(row: dict) -> None:
    kpi.ensure("ap_research_checks", SCHEMA)
    with store._LOCK, store._db() as c:
        c.execute("INSERT OR REPLACE INTO ap_research_checks (record_uuid, researched_at, checked_at, reached, by_number, "
                  "lead, deal, detail) VALUES (?,?,?,?,?,?,?,?)",
                  (row["record_uuid"], row["researched_at"], row["checked_at"], int(row["reached"]), row["by_number"],
                   int(row["lead"]), int(row["deal"]), json.dumps(row.get("detail") or {}, default=str)))


def number_kind(rec: dict, number: str, ignore) -> tuple[str, list[str]]:
    """Whether the number that reached someone is one research added. Research tags its numbers
    with who they belong to and the source that found them; a number carrying only a dial tier was
    on the record before."""
    skip = {t.lower() for t in gate.TIER_NAMES} | {str(x).lower() for x in ignore or []}
    for p in ((rec.get("owner") or {}).get("phones") or []):
        if gate.digits(p.get("number")) != number:
            continue
        tags = [str((t.get("title") or t.get("name")) if isinstance(t, dict) else t) for t in p.get("tags") or []]
        own = [t for t in tags if t and t.lower() not in skip]
        return (ADDED if own else OLD), own
    return GONE, []


def check_one(ctx: Ctx, cat: kpi.Catalog, rec: dict, researched_at: datetime, days: int) -> dict:
    """One researched record, read over the fixed window after its research."""
    st = ctx.stage
    u = rec.get("uuid") or ""
    owner = (rec.get("owner") or {}).get("uuid") or ""
    end = researched_at + timedelta(days=days)
    talks = kpi.conversation_events(cat, since=researched_at, until=end, owner=owner) if owner else []
    marks = [e for e in activity.events("owner.phone.status.updated", resource=owner, since=researched_at, until=end)
             if str(e["data"].get("to") or "").upper() in ("CORRECT", "CORRECT_DNC")] if owner else []
    number, how = "", ""
    if talks:
        number, how = talks[0]["number"], talks[0]["kind"]
    elif marks:
        number, how = marks[0]["data"].get("number") or "", "a number marked correct"
    reached = bool(talks or marks)
    kind, tags = number_kind(rec, number, st.get("ignore_number_tags") or ["dispo", "Litigator", "DNC"]) if reached else ("", [])
    lead = any(t >= researched_at for t in dealsource.lead_moments(u, until=ctx.now))
    closed = {str(s).lower() for s in (st.get("closed_statuses") or ["closed"])}
    deal = any(str(e["data"].get("to") or "").strip().lower() in closed
               for e in activity.events("property.status.updated", resource=u, since=researched_at, until=ctx.now))
    return {"record_uuid": u, "researched_at": researched_at.isoformat(timespec="seconds"),
            "checked_at": ctx.now.isoformat(timespec="seconds"), "reached": reached, "by_number": kind, "lead": lead, "deal": deal,
            "detail": {"street": addr(rec), "how": how, "number_ends": number[-4:], "number_tags": tags,
                       "lane": lanes.lane_of(dict(rec, status=None), getattr(ctx.policy, "lanes", None))}}


def table(rows: list[dict]) -> dict:
    reached = [r for r in rows if r["reached"]]
    return {"checked": len(rows), "reached": len(reached),
            "by_added": sum(1 for r in reached if r["by_number"] == ADDED),
            "by_old": sum(1 for r in reached if r["by_number"] == OLD),
            "by_gone": sum(1 for r in reached if r["by_number"] == GONE),
            "leads": sum(1 for r in rows if r["lead"]), "deals": sum(1 for r in rows if r["deal"])}


def run_researchstats(ctx: Ctx) -> None:
    st = ctx.stage
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    kpi.ensure("ap_research_checks", SCHEMA)
    kpi.sync_log(ctx, ("state", "calls", "texts"))
    days = int(cat.value("research.check_after_days"))
    tags = st.get("researched_tags") or ["Deep Prospected"]
    look_back = int(st.get("look_back_days", 120))
    done = {r["record_uuid"] for r in checks()}
    due = researched(tags, since=ctx.now - timedelta(days=look_back), until=ctx.now - timedelta(days=days))
    waiting = researched(tags, since=ctx.now - timedelta(days=days), until=ctx.now)
    todo = sorted(((when, u) for u, when in due.items() if u not in done))
    cap = int(st.get("max_records", 60))
    new = []
    for when, u in todo[:cap]:
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted as exc:
            ctx.out.problem("stopped opening researched records: %s. The rest are read next run" % exc)
            break
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a researched record (%s); it is tried again next run" % str(exc)[:100])
            continue
        if not rec.get("uuid"):
            continue
        row = check_one(ctx, cat, rec, when, days)
        _save(row)                        # what the check found: kept in plan mode too, so a record is read once
        new.append(row)
    if len(todo) > cap:
        ctx.out.tally["researched records left for the next run (%d a run)" % cap] = len(todo) - cap
    rows = checks()
    t, t_new = table(rows), table(new)
    ctx.out.found = len(todo)
    ctx.out.acted = len(new)
    ctx.out.tally.update({"checked so far": t["checked"], "checked this run": t_new["checked"],
                          "researched and still inside their %d days" % days: len([u for u in waiting if u not in done])})
    if not rows:
        ctx.out.head = "No researched record has reached its %d days yet (%d are inside them)." % (days, len(waiting))
        ctx.out.line("A record is read %d days after the tag %s lands on it; the first results follow then." % (days, " or ".join(tags)))
        return
    rate = 100.0 * t["reached"] / t["checked"]
    low, high = cat.low("research.reached"), cat.high("research.reached")
    enough = t["checked"] >= cat.value("research.min_records_to_judge")
    if not enough:
        verdict = "too few records to judge against the catalog's %s" % cat.text("research.reached")
    elif rate < low:
        verdict = "under the catalog's %s" % cat.text("research.reached")
    elif rate <= high:
        verdict = "inside the catalog's %s" % cat.text("research.reached")
    else:
        verdict = "above the catalog's %s" % cat.text("research.reached")
    ctx.out.line("Researched records read %d days on: %d so far, %d of them new this run." % (days, t["checked"], t_new["checked"]))
    ctx.out.line("A decision maker was reached on %d of %d (%d%%): %s." % (t["reached"], t["checked"], round(rate), verdict))
    if t["reached"]:
        ctx.out.line("Reached by %s: %d. By %s: %d.%s" % (ADDED, t["by_added"], OLD, t["by_old"],
                                                         (" By %s: %d." % (GONE, t["by_gone"])) if t["by_gone"] else ""))
        per = t["reached"] / float(t["deals"]) if t["deals"] else None
        ctx.out.line("Became a lead: %d. Closed: %d%s (the catalog says %s)." % (
            t["leads"], t["deals"], (", one deal in %s contact(s)" % kpi.num(round(per, 1))) if per else " so far",
            cat.text("research.contacts_per_deal")))
    else:
        ctx.out.line("Became a lead: %d. Closed: %d." % (t["leads"], t["deals"]))
    ctx.out.line(cat.footnote(USES))
    if enough and rate < low:
        ctx.out.problem("research reached a decision maker on %d%% of %d records, under the %s%% the teaching expects. "
                        "Check first that callers are working the numbers research added, in its call order." % (
                            round(rate), t["checked"], kpi.num(low)))
    kpi.save_report("research_results", ctx.now, {"day": kpi.local_day(ctx.now, ctx.policy.tz), "window_days": days,
                                                  "all": t, "this_run": t_new, "records": rows})
    ctx.out.head = "Research results: a decision maker reached on %d of %d researched record(s) (%d%%), %d lead(s), %d deal(s)." % (
        t["reached"], t["checked"], round(rate), t["leads"], t["deals"])
