"""The tools an employee can use, as plain async handlers with JSON schemas.

The runner wraps these into the Claude Agent SDK's in-process MCP server; nothing here imports the
SDK, so the selftest drives the handlers directly. Four kinds:

  * team tools     handoff, ask_operator, thread_history (every employee),
  * shared tools   record_lookup (free, read-only, every employee), spend_report (what the team
                   paid for, from the brain's own ledger: every employee), and the four query
                   tools in query.py (records_query, siftmap_count, activity_query, team_query:
                   read-only counts over the account and the brain's own tables, every employee,
                   AGENTS_QUERY_TOOLS=0 removes them),
  * workflow tools one per workflow the employee OWNS in roster.yaml. A teammate's workflow is
                   never exposed: that work is a handoff, which is how ownership is enforced.

A workflow tool never runs a long job inside the employee's turn. It queues a direct task on the
same thread and returns at once; the worker runs the job and posts the result in the thread. A
workflow that is not wired yet answers with how it runs by hand today, so the employee says so
instead of acting as if it ran.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import bus, router, store
from .team import SPEND_LABEL

CRM = None      # injected by the selftest; otherwise crm_key_client.from_env() on first use


def _crm():
    if CRM is not None:
        return CRM
    import crm_key_client
    return crm_key_client.from_env()


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], Awaitable[dict]]
    read_only: bool = False


@dataclass
class RunContext:
    task: dict
    roster: Any
    employee: Any
    asked: str = ""
    handoffs: list = field(default_factory=list)
    tickets: list = field(default_factory=list)


def _text(s: str, error: bool = False) -> dict:
    out: dict = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ------------------------------------------------------------------------------- record lookup

def _title(v) -> str:
    if isinstance(v, dict):
        return str(v.get("title") or v.get("name") or v.get("label") or "")
    return str(v or "")


def compact_record(rec: dict) -> dict:
    """The record as an employee needs it: no phone numbers or emails (they stay on the record,
    out of model context and out of channels), just how many there are."""
    a = rec.get("address") or {}
    o = rec.get("owner") or {}
    phones = o.get("phones") or rec.get("phones") or []
    keep = ("estimate_value", "equity_percent", "last_sold", "last_sale_price", "rental_value", "bedrooms",
            "bathrooms", "sqft", "year", "lot_size", "structure_type", "investor_score", "realtor_score")
    out = {"uuid": rec.get("uuid", ""),
           "address": ", ".join(x for x in (a.get("street"), a.get("city"), a.get("state"), a.get("postal_code")) if x),
           "county": a.get("county", ""), "vacant": a.get("vacant"),
           "owner": " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x) or o.get("company", ""),
           "owner_is_company": bool(o.get("company")) and not (o.get("first_name") or o.get("last_name")),
           "status": _title(rec.get("status")),
           "lists": [_title(x) for x in rec.get("lists") or []][:20],
           "tags": [_title(x) for x in rec.get("tags") or []][:30],
           "phones_on_owner": len(phones)}
    out.update({k: rec.get(k) for k in keep if rec.get(k) not in (None, "")})
    return out


def find_record(query: str) -> tuple[dict | None, str]:
    """(record, "") or (None, why). Accepts a uuid or a street line; never guesses between matches."""
    q = (query or "").strip()
    if not q:
        return None, "Give me a record uuid or the property's street line."
    crm = _crm()
    if router.UUID_RE.match(q):
        rec = crm.get_record(q)
        return (rec, "") if rec else (None, "No record with uuid %s." % q)
    hits = crm.search_by_address(q, limit=5) or []
    if not hits:
        return None, "No record in this account matches %r." % q
    if len(hits) > 1:
        lines = ["%s  %s" % (h.get("uuid", ""), compact_record(h)["address"]) for h in hits]
        return None, "Several records match %r; ask which one, or use its uuid:\n%s" % (q, "\n".join(lines))
    uuid = hits[0].get("uuid", "")
    return (crm.get_record(uuid) or hits[0]), ""


def record_lookup_tool(ctx: RunContext) -> Tool:
    async def handler(args: dict) -> dict:
        try:
            rec, why = await asyncio.to_thread(find_record, args.get("record", ""))
        except Exception as exc:  # noqa: BLE001  (the CRM client raises its own error types)
            return _text("The DataSift lookup failed: %s" % str(exc)[:300], error=True)
        if not rec:
            return _text(why, error=True)
        return _text(json.dumps(compact_record(rec), default=str))
    return Tool("record_lookup", "Look up one DataSift record by uuid or street line: owner, status, lists, tags, "
                "value, equity, last sale, beds, baths, sqft. Free and read-only.",
                _obj({"record": {"type": "string", "description": "A record uuid, or the street line (123 Main St)"}},
                     ["record"]), handler, read_only=True)


# ------------------------------------------------------------------------------- team tools

def team_tools(ctx: RunContext) -> list[Tool]:
    others = [e.display_name for e in ctx.roster.employees.values() if e.id != ctx.employee.id]

    async def handoff(args: dict) -> dict:
        try:
            tid = bus.handoff(ctx.task, ctx.roster, args.get("to", ""), args.get("need", ""), args.get("known", ""))
        except bus.HandoffRefused as exc:
            return _text("Handoff refused: %s" % exc, error=True)
        who = ctx.roster.find(args.get("to", "")).display_name
        ctx.handoffs.append({"to": who, "task": tid})
        return _text("Handed to %s as task %d on this thread. %s posts the answer here." % (who, tid, who))

    async def ask_operator(args: dict) -> dict:
        q = (args.get("question") or "").strip()
        if not q:
            return _text("Ask one short question.", error=True)
        if ctx.asked:
            return _text("You already asked the operator a question this turn. End your turn now.", error=True)
        ctx.asked = q
        return _text("Your question goes into the thread when you end this turn. End your turn now; "
                     "the operator's reply wakes you up here.")

    async def thread_history(args: dict) -> dict:
        rows = bus.thread_summary(ctx.task["thread_id"], ctx.roster, exclude=ctx.task["id"])
        return _text(json.dumps(rows, default=str) if rows else "Nothing else has happened on this thread yet.")

    return [
        Tool("handoff", "Hand work you do not own to the teammate who owns it, on this same thread. Say what "
             "you need back and what you already know. Use it instead of doing a teammate's job.",
             _obj({"to": {"type": "string", "enum": others, "description": "The teammate who owns the work"},
                   "need": {"type": "string", "description": "Exactly what you need back"},
                   "known": {"type": "string", "description": "What you already know, so they never re-ask it"}},
                  ["to", "need"]), handoff),
        Tool("ask_operator", "Ask the operator one short question when a required input is missing or an action "
             "needs their yes. Then end your turn; their reply in the thread resumes you.",
             _obj({"question": {"type": "string", "description": "One short question"}}, ["question"]), ask_operator),
        Tool("thread_history", "What the team has already done on this thread: who did what and what they found. "
             "Read it before you start.", _obj({}, []), thread_history, read_only=True),
    ]


def spend_report_tool(ctx: RunContext) -> Tool:
    """What something cost, read from the ledger the caps are checked against, never estimated."""
    from datetime import date, timedelta

    from . import config

    async def handler(args: dict) -> dict:
        scope = str(args.get("scope") or "thread").strip().lower()
        if scope == "thread":
            rows = store.spend_detail(thread_id=ctx.task.get("thread_id") or "")
        elif scope == "today":
            rows = store.spend_detail(since_day=store.today())
        elif scope == "week":
            rows = store.spend_detail(since_day=(date.today() - timedelta(days=6)).isoformat())
        else:
            return _text("scope is thread, today or week.", error=True)
        by: dict = {}
        for r in rows:
            k = str(r.get("what") or "unlabelled")
            v = by.setdefault(k, {"what": k, "usd": 0.0, "times": 0})
            v["usd"] += float(r.get("usd") or 0)
            v["times"] += 1
        items = sorted(by.values(), key=lambda x: -x["usd"])
        for v in items:
            v["usd"] = round(v["usd"], 4)
        return _text(json.dumps({
            "scope": scope, "total_usd": round(sum(float(r.get("usd") or 0) for r in rows), 4),
            "tasks": len({r.get("task_id") for r in rows}), "items": items,
            "note": "Read from the brain's ledger. Your own turn right now is added when it ends. Caps: "
                    "$%.2f a turn ($%.2f with files), $%.0f a run, $%.0f a day." % (
                        config.AGENT_BUDGET_USD, max(config.AGENT_BUDGET_USD, config.FILE_TURN_USD),
                        config.RUN_CAP_USD, config.DAILY_CAP_USD)}, default=str))

    return Tool("spend_report", "What the team has spent, read from the brain's own ledger: each Claude turn by model "
                "and every paid vendor call (skip trace, phone scores, Zillow, research). scope: thread (this job, "
                "the default), today, or week. Use it whenever the operator asks what something cost.",
                _obj({"scope": {"type": "string", "enum": ["thread", "today", "week"],
                                "description": "thread (this job), today, or week"}}, []), handler, read_only=True)


# ------------------------------------------------------------------------------- query tools

def _query_handler(name: str):
    """One query tool's handler. The query runs in a thread (the record search waits on the shared
    request budget, which must not stall the SDK's event loop) and a refusal comes back as one plain
    sentence the employee can repeat, never as a crash of the turn."""
    from . import query

    async def handler(args: dict) -> dict:
        try:
            res = await asyncio.to_thread(query.answer, name, dict(args or {}))
        except query.Refused as exc:
            return _text(str(exc), error=True)
        except Exception as exc:  # noqa: BLE001  (query.answer turns errors into Refused; this is the backstop)
            return _text("The %s query failed: %s" % (name, str(exc)[:300]), error=True)
        return _text(json.dumps(res, default=str, separators=(",", ":")))
    return handler


def query_tools(ctx: RunContext) -> list[Tool]:
    """The read-only questions every employee can ask in Slack (query.py): counts over the records
    search and SiftMap, and over the brain's own log copy and tables."""
    from . import query
    if not query.enabled():
        return []
    return [Tool(t["name"], t["description"], t["schema"], _query_handler(t["name"]), read_only=True)
            for t in query.tools()]


# ------------------------------------------------------------------------------- workflow tools

def workflow_tool(ctx: RunContext, wf) -> Tool:
    props: dict = {k: {"type": "string", "description": str(v)} for k, v in (wf.inputs or {}).items()}
    props["details"] = {"type": "string", "description": "Anything else the job needs to know"}
    if wf.spend == "records":
        props["approved"] = {"type": "boolean", "description": "True only after the operator said yes in this thread"}
    required = [k for k in (wf.inputs or {})]
    tail = (" Queues the job; it posts its own result in this thread when it finishes." if wf.live else
            " Not wired into the team yet: calling it returns how it runs by hand today.")
    desc = "%s Spend: %s.%s" % (wf.summary, SPEND_LABEL[wf.spend], tail)

    async def handler(args: dict) -> dict:
        if not wf.live:
            return _text("%s is not wired into the team yet, so nothing ran. Today it runs by hand: %s" % (
                wf.name, wf.manual or "see the SiftStack docs"), error=True)
        if wf.spend == "outreach":
            return _text("Outreach is staged by a person and released by a person; the team never sends it.",
                         error=True)
        if wf.spend == "records" and not args.get("approved"):
            return _text("This spends the account's record allowance. Ask the operator first (with the count), "
                         "then call again with approved=true.", error=True)
        record_uuid, subject = "", ""
        if "record" in (wf.inputs or {}):
            try:
                rec, why = await asyncio.to_thread(find_record, args.get("record", ""))
            except Exception as exc:  # noqa: BLE001
                return _text("The DataSift lookup failed: %s" % str(exc)[:300], error=True)
            if not rec:
                return _text(why, error=True)
            record_uuid, subject = rec.get("uuid", ""), compact_record(rec)["address"]
        t = ctx.task
        tid, created = store.enqueue(
            wf.owner, wf.name, record_uuid, subject=subject or (args.get("details") or "")[:120],
            payload={"requested_by": ctx.employee.display_name, "details": args.get("details", ""),
                     # Every declared input rides along: the first version kept only the record, so a
                     # workflow asked for a ZIP or a month never saw it.
                     "inputs": {k: args.get(k) for k in (wf.inputs or {}) if args.get(k) not in (None, "")}},
            thread_id=t["thread_id"], parent_id=t["id"], hop=int(t.get("hop") or 0),
            source="agent:%s" % ctx.employee.id, mode="direct",
            slack_channel=t.get("slack_channel") or "", slack_thread=t.get("slack_thread") or "")
        ctx.tickets.append(tid)
        if not created:
            prior = store.task(tid) or {}
            return _text("Already %s as task %d inside the dedupe window, so it was not queued again." % (
                prior.get("status", "queued"), tid))
        return _text("Queued %s for %s as task %d. It posts its result in this thread when it finishes." % (
            wf.name, subject or "this request", tid))

    return Tool(wf.name, desc, _obj(props, required), handler)


def build(ctx: RunContext) -> list[Tool]:
    tools = team_tools(ctx)
    if "record_lookup" in ctx.roster.shared_tools:
        tools.append(record_lookup_tool(ctx))
    tools.append(spend_report_tool(ctx))
    tools += query_tools(ctx)
    tools += [workflow_tool(ctx, wf) for wf in ctx.roster.owned(ctx.employee.id)]
    return tools
