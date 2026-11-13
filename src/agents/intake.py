"""What a Slack message means for the team. Called by the web process; never runs a model.

In order:
  1. A reply in a thread where an employee is waiting on the operator resumes that employee.
  2. A greeting ("good morning", "hello world", "meet the team") starts the meet-the-team chain.
  3. A name at the start ("Roma, quick screen 123 Main St") or the channel (#roma-sales) picks
     the employee, and the ask is queued on the Slack thread's own job thread.
  4. An unaddressed reply in a thread goes to whoever on the team spoke there last.
  5. Anything else is ignored in a shared channel (people talk to each other there too), and
     answered with a short "which of us?" only when the bot was mentioned or messaged directly.

The operator's message gets an :eyes: reaction when it is picked up, rather than another message.
"""
from __future__ import annotations

from . import bus, hello, slack_app, store, team


def slack_event(event: dict, roster) -> dict:
    channel, ts = event.get("channel") or "", event.get("ts") or ""
    thread, text, user = event.get("thread_ts") or "", event.get("text") or "", event.get("user") or ""
    if not (channel and ts):
        return {"ignored": "no channel or ts"}
    # A mention arrives twice (app_mention and message): one ask, not two.
    if not store.seen_once("msg:%s:%s" % (channel, ts)):
        return {"duplicate": True}

    if thread:
        waiting = store.waiting_on(channel, thread)
        if waiting:
            tid = bus.resume(waiting, team.clean(text), user)
            slack_app.react(channel, ts)
            return {"resumed": waiting["id"], "task": tid}

    name = slack_app.channel_name(channel)
    route = team.route_message(roster, name, text)
    on_thread = store.on_slack_thread(channel, thread) if thread else []
    if route["kind"] == "which" and on_thread:
        last = next((t["agent"] for t in reversed(on_thread) if t["agent"] in roster.employees), "")
        if last:
            route = {"kind": "ask", "agent": last, "text": team.clean(text)}

    if route["kind"] == "hello":
        return {"hello": hello.start(roster, channel, thread or ts, team.clean(text))}
    if route["kind"] == "which":
        direct = event.get("type") == "app_mention" or event.get("channel_type") == "im"
        if direct:
            slack_app.post(channel, roster.which_text(), thread_ts=thread or ts)
        return {"which": direct}

    tid, _ = store.enqueue(route["agent"], "ask", "", subject=route["text"][:120],
                           payload={"text": route["text"], "user": user, "channel_name": name},
                           thread_id=on_thread[-1]["thread_id"] if on_thread else "", mode="agent", source="slack",
                           slack_channel=channel, slack_thread=thread or ts)
    slack_app.react(channel, ts)
    return {"queued": tid, "agent": route["agent"]}
