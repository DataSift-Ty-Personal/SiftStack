"""How work moves between employees and back to the operator.

The rules are Tyler's bootcamp handoff protocol, enforced here rather than trusted to a prompt:

  * a handoff stays on the same thread (the memory spine of one job) and Slack thread,
  * every handoff adds a hop and a chain past AGENTS_MAX_HOPS is refused,
  * nobody hands work to themselves,
  * the handoff carries what the sender already knows and what it needs back.

Asking the operator is the other direction: the employee's task goes to 'waiting' with its Claude
session id, and the operator's reply in that Slack thread queues a resume that continues the same
session. Nothing else wakes a waiting task.
"""
from __future__ import annotations

from . import config, store


class HandoffRefused(ValueError):
    pass


def handoff(task: dict, roster, to: str, need: str, known: str = "") -> int:
    """Queue `need` for teammate `to` on this task's thread; returns the new task id."""
    target = roster.find(to)
    if not target:
        raise HandoffRefused("nobody on the team is called %r (team: %s)" % (
            to, ", ".join(e.display_name for e in roster.employees.values())))
    if target.id == task["agent"]:
        raise HandoffRefused("an employee cannot hand work to itself")
    hop = int(task.get("hop") or 0) + 1
    if hop > config.MAX_HOPS:
        raise HandoffRefused("this thread has already been handed off %d times (limit %d); "
                             "answer with what the team has" % (hop - 1, config.MAX_HOPS))
    if not (need or "").strip():
        raise HandoffRefused("say what you need back from %s" % target.display_name)
    sender = roster.employees.get(task["agent"])
    payload = {"text": need.strip(), "known": (known or "").strip(),
               "from": sender.display_name if sender else task["agent"]}
    tid, _ = store.enqueue(target.id, "ask", task.get("record_uuid") or "", subject=need.strip()[:120],
                           payload=payload, thread_id=task["thread_id"], parent_id=task["id"], hop=hop,
                           source="agent:%s" % task["agent"], mode="agent",
                           slack_channel=task.get("slack_channel") or "", slack_thread=task.get("slack_thread") or "")
    store.log_event("bus", "handoff", {"from": task["agent"], "to": target.id, "task": tid, "parent": task["id"],
                                       "thread": task["thread_id"], "hop": hop})
    return tid


def resume(waiting: dict, answer: str, user: str = "", not_before: str = "") -> int:
    """The operator answered a waiting employee in its thread: queue the continuation, same thread,
    same Claude session, and close the waiting task so a second reply cannot resume it twice.
    `not_before` holds the continuation back while more files are still arriving on the thread."""
    payload = {"text": (answer or "").strip(), "question": waiting.get("question") or "", "user": user,
               "original": store.payload_of(waiting)}
    tid, _ = store.enqueue(waiting["agent"], "resume", waiting.get("record_uuid") or "",
                           subject=(waiting.get("subject") or "")[:120], payload=payload,
                           thread_id=waiting["thread_id"], parent_id=waiting["id"], hop=int(waiting.get("hop") or 0),
                           source="slack", mode="agent", slack_channel=waiting.get("slack_channel") or "",
                           slack_thread=waiting.get("slack_thread") or "", session_id=waiting.get("session_id") or "",
                           not_before=not_before)
    store.mark(waiting["id"], "answered")
    return tid


def thread_summary(thread_id: str, roster, exclude: int = 0, limit: int = 12) -> list[dict]:
    """What has happened on this thread, compact enough to hand an employee every turn."""
    out = []
    for t in store.thread(thread_id)[-limit - 1:]:
        if t["id"] == exclude:
            continue
        emp = roster.employees.get(t["agent"])
        res = store.result_of(t)
        out.append({"task": t["id"], "who": emp.display_name if emp else t["agent"], "type": t["workflow"],
                    "status": t["status"], "asked": (store.payload_of(t).get("text") or "")[:300],
                    "result": (res.get("text") or res.get("summary") or "")[:500],
                    "question": (t.get("question") or "")[:300], "error": (t.get("error") or "")[:200]})
    return out[-limit:]
