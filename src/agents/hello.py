"""Meet the team: Tyler's Hello World, kept as the free smoke test of a whole install.

"good morning" in Slack (or POST /team/hello/<secret> at the end of a deploy) wakes the first
employee, who introduces itself in the thread and hands the mic to the next, until everyone on
the roster has spoken. No model is called, so it costs nothing, and it proves the parts a real job
depends on: Slack in, the queue, the worker, handoffs on one thread with hops counted, and each
employee posting under its own name.
"""
from __future__ import annotations

from . import slack_app, store


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def start(roster, channel: str, thread_ts: str, note: str = "", source: str = "slack",
          must_land: bool = False) -> int:
    """Start the chain; returns the first task's id. A hello that starts outside a thread (a deploy,
    the first boot) threads under its own opening message. `must_land` starts nothing when that
    opening message could not be posted, so a caller can try again later instead of queueing four
    introductions nobody will see."""
    names = [e.display_name for e in roster.employees.values()]
    ts = slack_app.post(channel, ":wave: *Hello World!* Waking %s. Each of us introduces ourselves in this thread."
                        % _join(names), thread_ts=thread_ts)
    if must_land and not ts:
        return 0
    tid, _ = store.enqueue(roster.order()[0], "introduce_self", payload={"note": note, "so_far": []},
                           mode="hello", slack_channel=channel, slack_thread=thread_ts or ts, source=source)
    return tid


def run(task: dict, roster) -> dict:
    emp = roster.employees[task["agent"]]
    so_far = list(store.payload_of(task).get("so_far") or []) + [emp.display_name]
    nxt = roster.next_after(emp.id)
    text = "%s\n\n_On this thread so far: %s_" % (emp.introduction, ", ".join(so_far))
    if nxt:
        nxt_name = roster.employees[nxt].display_name
        store.enqueue(nxt, "introduce_self", payload={"so_far": so_far}, thread_id=task["thread_id"],
                      parent_id=task["id"], hop=int(task.get("hop") or 0) + 1, mode="hello",
                      source="agent:%s" % emp.id, slack_channel=task.get("slack_channel") or "",
                      slack_thread=task.get("slack_thread") or "")
        if nxt_name not in emp.introduction:
            text += "\n\nHanding the mic to %s." % nxt_name
    else:
        sales = roster.employees.get("sales") or emp
        text += ("\n\n*Meet the team: complete.* All %d of us are online. Ask any of us by name, for example: "
                 "%s, quick screen 123 Main St" % (len(so_far), sales.display_name))
    channel = task.get("slack_channel") or slack_app.home_channel(None, roster)
    slack_app.post(channel, text, thread_ts=task.get("slack_thread") or "", employee=emp)
    return {"text": text, "next": nxt or ""}
