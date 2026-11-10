"""The worker: takes one task at a time off the queue, runs its agent, reports to Slack.

One worker, deliberately. The deep prospect chain patches module state in dream100_dp, and the
queue is one SQLite file, so tasks run in order. A failure is always posted: a task that dies
quietly reads exactly like a record nobody moved.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from . import slack, store
from .research import deep_prospect

log = logging.getLogger(__name__)

HANDLERS = {
    "deep_prospect": (deep_prospect.run, deep_prospect.slack_text),
}


def run_task(task: dict) -> dict | None:
    run, render = HANDLERS[task["workflow"]]
    try:
        result = run(task)
    except BaseException as exc:  # noqa: BLE001  (dream100_dp gates raise SystemExit)
        if isinstance(exc, KeyboardInterrupt):
            raise
        err = "%s: %s" % (type(exc).__name__, str(exc)[:500])
        log.error("task %s failed: %s\n%s", task["id"], err, traceback.format_exc())
        store.finish(task["id"], "failed", error=err)
        slack.post(":x: *%s failed* on %s (task %s)\n%s" % (
            task["workflow"].replace("_", " "), task.get("subject") or task["record_uuid"], task["id"], err))
        return None
    store.finish(task["id"], "done", result=result)
    slack.post(render(task, result))
    return result


def loop(stop: threading.Event, idle_s: float = 3.0) -> None:
    store.init()
    while not stop.is_set():
        task = store.claim_next()
        if not task:
            stop.wait(idle_s)
            continue
        log.info("task %s: %s on %s", task["id"], task["workflow"], task["record_uuid"])
        run_task(task)
        time.sleep(0.2)


def start_thread() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop,), name="agents-worker", daemon=True)
    t.start()
    return t, stop
