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

Files (walkthrough photos, a boundary file, a PDF) ride along with any of the above
(agents/attachments.py): they are recorded on the thread here and saved into the employee's
workspace when its turn starts. A message carrying files starts its turn FILE_SETTLE_S later, and a
further message on the same thread while that turn waits joins it, so a walkthrough posted ten
photos at a time is ONE turn that sees every photo. Files posted with no ask are held for the same
person's next ask in that channel.
"""
from __future__ import annotations

from . import attachments, bus, config, hello, release, slack_app, store, team

HEARD_KEY = "slack:last_event"      # when Slack last delivered anything, by either way in
# Message subtypes a person produces by writing: a post with files, and a thread reply also sent to
# the channel. Every other subtype (edits, deletes, joins, the bot's own posts) is not an ask.
HUMAN_SUBTYPES = {"file_share", "thread_broadcast"}


def handle_body(body: dict, retry: bool, roster_fn) -> dict:
    """One Slack event body, however it arrived: the Request URL (after its signature was checked)
    or the Socket Mode connection. `retry` is Slack saying the first delivery was not acknowledged
    in time; it was, so a retry over the Request URL is acknowledged and not run again."""
    store.kv_set(HEARD_KEY, store.now())
    if retry:
        return {"ok": True, "retry": True}
    event = body.get("event") or {}
    if body.get("event_id") and not store.seen_once("slack:%s" % body["event_id"]):
        return {"ok": True, "duplicate": True}
    if event.get("bot_id") or (event.get("subtype") and event["subtype"] not in HUMAN_SUBTYPES):
        return {"ok": True, "ignored": True}   # our own posts, edits, joins
    roster = roster_fn()
    if not roster:
        return {"ok": False, "error": "roster"}
    store.log_event("slack", event.get("type", ""), {"channel": event.get("channel"), "ts": event.get("ts"),
                                                     "thread_ts": event.get("thread_ts")})
    return {"ok": True, **slack_event(event, roster)}


def slack_event(event: dict, roster) -> dict:
    channel, ts = event.get("channel") or "", event.get("ts") or ""
    thread, text, user = event.get("thread_ts") or "", event.get("text") or "", event.get("user") or ""
    if not (channel and ts):
        return {"ignored": "no channel or ts"}
    # A mention arrives twice (app_mention and message): one ask, not two.
    if not store.seen_once("msg:%s:%s" % (channel, ts)):
        return {"duplicate": True}

    # A release is read by a pattern, before any employee sees the message: a model never releases.
    cmd = release.parse(team.clean(text))
    if cmd:
        return release.slack_command(cmd, user, channel, thread or ts)

    files = attachments.from_event(event)
    if files and thread:
        attachments.add(channel, thread, files)
        attachments.unreadable_notice(channel, thread, len(files))
    settle = store.later(config.FILE_SETTLE_S) if files else ""

    if thread:
        waiting = store.waiting_on(channel, thread)
        if waiting:
            tid = bus.resume(waiting, team.clean(text), user, not_before=settle)
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
        if files and not thread:
            # Photos first, the ask second: held for this person's next ask in this channel.
            return {"which": False, "held": attachments.hold(channel, user, files)}
        direct = event.get("type") == "app_mention" or event.get("channel_type") == "im"
        if direct:
            slack_app.post(channel, roster.which_text(), thread_ts=thread or ts)
        return {"which": direct}

    if not thread:
        # A new ask in the channel: its own files, plus any this person posted just before it.
        held = attachments.claim(channel, user)
        mine = files + [f for f in held if f.get("id") not in {g.get("id") for g in files}]
        if mine:
            attachments.add(channel, ts, mine)
            attachments.unreadable_notice(channel, ts, len(mine))

    # A turn on this thread that has not started yet takes the new message instead of a second turn.
    waiting_turn = store.queued_ask(channel, thread or ts, route["agent"])
    if waiting_turn and store.join_ask(waiting_turn["id"], route["text"], settle):
        slack_app.react(channel, ts)
        return {"joined": waiting_turn["id"], "agent": route["agent"]}

    tid, _ = store.enqueue(route["agent"], "ask", "", subject=(route["text"] or "files")[:120],
                           payload={"text": route["text"], "user": user, "channel_name": name},
                           thread_id=on_thread[-1]["thread_id"] if on_thread else "", mode="agent", source="slack",
                           slack_channel=channel, slack_thread=thread or ts, not_before=settle)
    slack_app.react(channel, ts)
    return {"queued": tid, "agent": route["agent"]}
