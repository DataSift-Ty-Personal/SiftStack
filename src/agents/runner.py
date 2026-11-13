"""One employee turn, on the Claude Agent SDK.

The employee runs with its workspace as cwd (persona + TEAM.md + the skills it carries, built by
team.materialize) and exactly the tools it owns. Four layers keep an unattended employee inside
its job, because the SDK's allowed_tools only pre-approves and does not restrict:

  1. tools=[...]            only safe built-ins exist (Read, Glob, Grep, Skill, plus WebSearch and
                            WebFetch where granted); Bash, Write and Edit are not in context at all,
  2. allowed_tools=[...]    the employee's own MCP tools and those built-ins are pre-approved,
  3. permission_mode        "dontAsk": anything not pre-approved is denied, never prompted,
  4. a PreToolUse hook      re-checks the allowlist in our code and keeps file reads inside the
                            workspace, so a prompt injection cannot read the box's environment.

Spend: every turn runs under max_budget_usd, the lesser of the per-turn cap and what is left of the
day's cap, and its cost lands in the same ledger the paid workflows use.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from . import bus, config, store, team
from . import tools as team_tools

log = logging.getLogger(__name__)

SERVER = "team"
BASE_BUILTINS = ["Read", "Glob", "Grep", "Skill"]
NEVER = ["Bash", "Write", "Edit", "NotebookEdit"]
_SDK = None


def sdk():
    """The Claude Agent SDK module, imported on first real use (the selftest injects a fake)."""
    global _SDK
    if _SDK is None:
        import claude_agent_sdk
        _SDK = claude_agent_sdk
    return _SDK


@dataclass
class Turn:
    text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    stub: bool = False


def builtins_for(emp) -> list[str]:
    return BASE_BUILTINS + [b for b in emp.builtins if b not in BASE_BUILTINS]


def mcp_name(tool_name: str) -> str:
    return "mcp__%s__%s" % (SERVER, tool_name)


def _inside(root: Path, p: str) -> bool:
    try:
        path = Path(p)
        path = path if path.is_absolute() else root / path
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def make_guard(allowed: set, ws: Path):
    async def guard(input_data, tool_use_id, context):
        data = input_data or {}
        name, args = data.get("tool_name", ""), data.get("tool_input") or {}
        why = ""
        if name not in allowed:
            why = "%s is not on this employee's tool list" % name
        elif name in ("Read", "Glob", "Grep"):
            p = args.get("file_path") or args.get("path") or ""
            if (p and not _inside(ws, p)) or ".." in str(args.get("pattern") or ""):
                why = "reading outside your workspace is not allowed"
        if why:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": why}}
        return {}
    return guard


def budget_now() -> float:
    left_today = config.DAILY_CAP_USD - store.spent(day=store.today())
    return max(0.0, min(config.AGENT_BUDGET_USD, left_today))


def prompt_for(task: dict, roster, emp) -> str:
    p = store.payload_of(task)
    src = task.get("source") or ""
    if task["workflow"] == "resume":
        lines = ["The operator answered your question.", "You asked: %s" % p.get("question", ""),
                 "They said: %s" % p.get("text", ""), "", "Continue the work."]
    elif src.startswith("agent:"):
        lines = ["%s handed you this on the thread:" % p.get("from", "A teammate"), p.get("text", "")]
        if p.get("known"):
            lines += ["", "What they already know:", p["known"]]
    else:
        where = ("#" + p["channel_name"]) if p.get("channel_name") else "a direct message"
        lines = ["The operator wrote in %s:" % where, p.get("text") or task.get("subject") or ""]
    hist = bus.thread_summary(task["thread_id"], roster, exclude=task["id"], limit=8)
    if hist:
        lines += ["", "Earlier on this thread (oldest first):"]
        for h in hist:
            said = h["result"] or h["question"] or h["asked"] or h["error"]
            lines.append("- task %s, %s, %s (%s): %s" % (h["task"], h["who"], h["type"], h["status"], said[:240]))
    lines += ["", "Mode: %s. Caps: $%.0f per run, $%.0f per day; $%.2f spent today before this turn "
              "(this turn's own Claude cost is added after it ends)." % (
        config.MODE, config.RUN_CAP_USD, config.DAILY_CAP_USD, store.spent(day=store.today())),
        "Your reply is posted in the Slack thread as written: three short lines at most."]
    return "\n".join(lines)


def options(sdk_mod, ctx, tool_list: list, ws: Path, model: str, budget: float, resume: str = ""):
    sdk_tools = [sdk_mod.tool(t.name, t.description, t.schema)(t.handler) for t in tool_list]
    server = sdk_mod.create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=sdk_tools)
    builtins = builtins_for(ctx.employee)
    allowed = [mcp_name(t.name) for t in tool_list] + builtins
    prompt_text = team.system_prompt(ctx.roster, ctx.employee.id)
    system = prompt_text if len(prompt_text) < 20000 else {"type": "file", "path": str(ws / "EMPLOYEE.md")}
    kw = dict(cwd=str(ws), setting_sources=["project"], system_prompt=system, tools=builtins,
              allowed_tools=allowed, disallowed_tools=list(NEVER), permission_mode="dontAsk",
              mcp_servers={SERVER: server}, strict_mcp_config=True, model=model,
              max_turns=config.AGENT_MAX_TURNS, max_budget_usd=budget,
              hooks={"PreToolUse": [sdk_mod.HookMatcher(matcher=None, hooks=[make_guard(set(allowed), ws)])]},
              env={"CLAUDE_AGENT_SDK_CLIENT_APP": "siftstack-agents", "ENABLE_TOOL_SEARCH": "false"})
    if ctx.employee.skills:
        kw["skills"] = "all"
    if ctx.employee.effort:
        kw["effort"] = ctx.employee.effort
    if resume:
        kw["resume"] = resume
    return sdk_mod.ClaudeAgentOptions(**kw)


async def _turn(sdk_mod, prompt: str, opts) -> Turn:
    result = None
    try:
        async for message in sdk_mod.query(prompt=prompt, options=opts):
            if isinstance(message, sdk_mod.ResultMessage):
                result = message
    except Exception as exc:  # noqa: BLE001  (a single-shot query raises after an error result)
        if result is None:
            return Turn(error="%s: %s" % (type(exc).__name__, str(exc)[:400]))
        log.warning("agent sdk raised after its result: %s", exc)
    if result is None:
        return Turn(error="the employee ended without a result")
    turn = Turn(text=(result.result or "").strip(), session_id=result.session_id or "",
                cost_usd=float(result.total_cost_usd or 0), turns=int(result.num_turns or 0))
    if result.is_error or (result.subtype or "") != "success":
        errs = "; ".join(getattr(result, "errors", None) or [])
        turn.error = (result.subtype or "error") + (": " + errs if errs else "")
    return turn


def stub_turn(ctx, tool_list) -> Turn:
    mine = [t.name for t in tool_list if t.name not in ("handoff", "ask_operator", "thread_history")]
    return Turn(stub=True, text="%s here. Claude is switched off on this install (AGENTS_USE_LLM=0), so I can't "
                "work this through yet. When it's on, these are my tools: %s." % (
                    ctx.employee.display_name, ", ".join(mine)))


def run(task: dict, roster) -> tuple[Turn, "team_tools.RunContext"]:
    emp = roster.employees[task["agent"]]
    ctx = team_tools.RunContext(task=task, roster=roster, employee=emp)
    tool_list = team_tools.build(ctx)
    if not config.USE_LLM:
        return stub_turn(ctx, tool_list), ctx
    budget = budget_now()
    if budget < 0.05:
        return Turn(error="today's spend cap ($%.0f) is used up, so nothing ran" % config.DAILY_CAP_USD), ctx
    ws = team.materialize(roster, emp.id)
    resume = (task.get("session_id") or "") if task["workflow"] == "resume" else ""
    opts = options(sdk(), ctx, tool_list, ws, emp.model, budget, resume=resume)
    turn = asyncio.run(_turn(sdk(), prompt_for(task, roster, emp), opts))
    if turn.cost_usd:
        store.add_spend(task["id"], turn.cost_usd, "claude %s" % emp.model)
    return turn, ctx
