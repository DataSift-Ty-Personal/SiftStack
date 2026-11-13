"""The worker: takes one task at a time off the queue, runs it, reports to Slack.

One worker, deliberately. The deep prospect chain patches module state in dream100_dp, and the
queue is one SQLite file, so tasks run in order. A failure is always posted: a task that dies
quietly reads exactly like a record nobody moved.

Three kinds of task:
  * hello    the meet-the-team chain (hello.py), no model,
  * direct   a wired workflow (HANDLERS), queued by a DataSift webhook, a schedule, or an
             employee's workflow tool,
  * agent    an employee turn (runner.py): a Slack ask, a teammate's handoff, or a resume after
             the operator answered a question.
Every result is posted in the task's Slack thread under the owning employee's name. A task that
started without a thread (a webhook) adopts the thread of its first post.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from . import config, hello, runner, slack_app, store, team
from .research import deep_prospect

log = logging.getLogger(__name__)

HANDLERS = {
    "deep_prospect": (deep_prospect.run, deep_prospect.slack_text),
}


def _roster():
    try:
        return team.load()
    except Exception as exc:  # noqa: BLE001  (a broken roster must not stop wired workflows)
        log.error("roster failed to load: %s", exc)
        return None


def _post(task: dict, text: str, emp, roster) -> str:
    channel, thread = task.get("slack_channel") or "", task.get("slack_thread") or ""
    if not channel and config.SLACK_BOT_TOKEN:
        channel = slack_app.home_channel(emp, roster)
    ts = slack_app.post(channel, text, thread_ts=thread, employee=emp)
    if ts and not thread:
        store.set_slack(task["id"], channel, ts)
    return ts


def _employee(roster, agent: str):
    return roster.employees.get(agent) if roster else None


def _fail(task: dict, roster, err: str) -> None:
    store.finish(task["id"], "failed", error=err)
    emp = _employee(roster, task["agent"])
    what = task["workflow"].replace("_", " ")
    _post(task, ":x: *%s failed* on %s (task %s)\n%s" % (what, task.get("subject") or task.get("record_uuid") or
                                                          "this request", task["id"], err), emp, roster)


def handler_for(workflow: str):
    """(run, render) for a wired workflow: the fixed HANDLERS, then any autopilot playbook stage."""
    if workflow in HANDLERS:
        return HANDLERS[workflow]
    try:
        from .autopilot import sweep
        if sweep.is_stage(workflow):
            return sweep.task_handler, sweep.slack_text
    except Exception as exc:  # noqa: BLE001
        log.error("autopilot lookup failed for %s: %s", workflow, exc)
    return None


def _direct(task: dict, roster) -> dict | None:
    run, render = handler_for(task["workflow"])
    try:
        result = run(task)
    except BaseException as exc:  # noqa: BLE001  (dream100_dp gates raise SystemExit)
        if isinstance(exc, KeyboardInterrupt):
            raise
        log.error("task %s failed: %s\n%s", task["id"], exc, traceback.format_exc())
        _fail(task, roster, "%s: %s" % (type(exc).__name__, str(exc)[:500]))
        return None
    store.finish(task["id"], "done", result=result)
    text = render(task, result)
    if text:  # a quiet scheduled run renders nothing and posts nothing
        _post(task, text, _employee(roster, task["agent"]), roster)
    return result


def _hello(task: dict, roster) -> dict | None:
    if not roster:
        _fail(task, roster, "the roster did not load, so the team cannot introduce itself")
        return None
    result = hello.run(task, roster)
    store.finish(task["id"], "done", result=result)
    return result


def _agent(task: dict, roster) -> dict | None:
    if not roster or task["agent"] not in roster.employees:
        _fail(task, roster, "no employee %r on the roster" % task["agent"])
        return None
    emp = roster.employees[task["agent"]]
    try:
        turn, ctx = runner.run(task, roster)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        log.error("task %s failed: %s\n%s", task["id"], exc, traceback.format_exc())
        _fail(task, roster, "%s: %s" % (type(exc).__name__, str(exc)[:500]))
        return None
    result = {"text": turn.text, "session_id": turn.session_id, "cost_usd": round(turn.cost_usd, 4),
              "turns": turn.turns, "handoffs": ctx.handoffs, "tickets": ctx.tickets, "stub": turn.stub}
    if ctx.asked:
        store.wait_for_operator(task["id"], ctx.asked, turn.session_id, result)
        _post(task, ctx.asked, emp, roster)
        return result
    if turn.error and not (turn.text or ctx.handoffs or ctx.tickets):
        _fail(task, roster, "%s could not finish: %s" % (emp.display_name, turn.error))
        return None
    text = turn.text or "; ".join("Handed to %s (task %s)." % (h["to"], h["task"]) for h in ctx.handoffs)
    if turn.error:
        text += "\n_(stopped early: %s)_" % turn.error
    store.finish(task["id"], "done", result=result, error=turn.error)
    if text.strip():
        _post(task, text, emp, roster)
    return result


def run_task(task: dict) -> dict | None:
    roster = _roster()
    if task.get("mode") == "hello" or task["workflow"] == "introduce_self":
        return _hello(task, roster)
    if (task.get("mode") or "direct") == "direct" and handler_for(task["workflow"]):
        return _direct(task, roster)
    return _agent(task, roster)


def loop(stop: threading.Event, idle_s: float = 3.0) -> None:
    store.init()
    while not stop.is_set():
        task = store.claim_next()
        if not task:
            stop.wait(idle_s)
            continue
        log.info("task %s: %s %s on %s", task["id"], task["agent"], task["workflow"],
                 task.get("record_uuid") or task.get("subject") or "-")
        run_task(task)
        time.sleep(0.2)


def start_thread() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop,), name="agents-worker", daemon=True)
    t.start()
    return t, stop
