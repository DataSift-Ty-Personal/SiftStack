"""Daily readout: what the agent did, and what a human still owes it.

Two audiences in one message. The top half is the funnel, so you can see
whether texting is producing leads. The bottom half is the work queue: drafts
waiting on approval, threads a human took over, soft nos due for a follow-up,
and anything that failed. The queue matters more, because a draft nobody
approves is a conversation nobody is having.

    python src/sms_agent/cli.py digest          # print it
    python src/sms_agent/cli.py digest --post   # print and post to Slack
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config, escalate, sender_pool, store

# A soft no is not a dead lead, it is a later lead. The proven playbook treats
# it that way, so the digest surfaces them rather than letting them rot.
SOFT_NO_FOLLOWUP_DAYS = int(__import__("os").getenv("SMS_AGENT_FOLLOWUP_DAYS", "45"))


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def collect(days: int = 1) -> dict:
    c = store._conn()
    since = _since(days)

    def scalar(sql: str, *args) -> int:
        row = c.execute(sql, args).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    intents = {
        r["intent"]: r["n"]
        for r in c.execute(
            "SELECT intent, COUNT(*) AS n FROM messages"
            " WHERE direction='in' AND created_at>=? AND intent IS NOT NULL AND intent<>''"
            " GROUP BY intent ORDER BY n DESC",
            (since,),
        )
    }
    # Inbound intent is recorded on the reply row, so read it off conversations
    # when the message row predates that column being populated.
    inbound = scalar("SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at>=?", since)
    sent = scalar("SELECT COUNT(*) FROM outbox WHERE status='sent' AND sent_at>=?", since)

    holds = [
        dict(r)
        for r in c.execute(
            "SELECT phone, intent, confidence, body, created_at FROM outbox"
            " WHERE status='held' ORDER BY id DESC LIMIT 25"
        )
    ]
    paused = [
        dict(r)
        for r in c.execute(
            "SELECT phone, paused_reason, updated_at FROM conversations"
            " WHERE state='paused' ORDER BY updated_at DESC LIMIT 25"
        )
    ]
    failed = [
        dict(r)
        for r in c.execute(
            "SELECT phone, error, attempts FROM outbox WHERE status='failed' ORDER BY id DESC LIMIT 15"
        )
    ]
    stale_events = scalar(
        "SELECT COUNT(*) FROM events WHERE processed_at IS NULL AND received_at<?", _since(0)
    )
    followups = [
        dict(r)
        for r in c.execute(
            "SELECT phone, updated_at FROM conversations"
            " WHERE state='closed' AND paused_reason LIKE '%soft%' AND updated_at<=?"
            " ORDER BY updated_at LIMIT 20",
            (_since(SOFT_NO_FOLLOWUP_DAYS),),
        )
    ]

    return {
        "window_days": days,
        "inbound": inbound,
        "sent": sent,
        "intents": intents,
        "opt_outs": scalar(
            "SELECT COUNT(*) FROM suppression WHERE reason='opt_out' AND created_at>=?", since
        ),
        "wrong_numbers": scalar(
            "SELECT COUNT(*) FROM suppression WHERE reason='wrong_number' AND created_at>=?", since
        ),
        "dead": scalar(
            "SELECT COUNT(*) FROM suppression WHERE reason='dead' AND created_at>=?", since
        ),
        "holds": holds,
        "paused": paused,
        "failed": failed,
        "stale_events": stale_events,
        "followups_due": followups,
        "capacity": sender_pool.capacity_today(),
        "stats": store.stats(),
    }


def render(data: dict) -> str:
    cap = data["capacity"]
    lines = [
        f"*SMS agent, last {data['window_days']} day(s)*",
        f"Sent {data['sent']}, replies in {data['inbound']}",
    ]

    if data["intents"]:
        parts = [f"{k.lower().replace('_', ' ')} {v}" for k, v in data["intents"].items()]
        lines.append("Replies read as: " + ", ".join(parts))

    hygiene = []
    for key, label in (("opt_outs", "opt-out"), ("wrong_numbers", "wrong number"), ("dead", "dead")):
        if data[key]:
            hygiene.append(f"{data[key]} {label}{'s' if data[key] != 1 else ''}")
    if hygiene:
        lines.append("Suppressed: " + ", ".join(hygiene))

    lines.append(
        f"Pool: {cap['sent_today']} of {cap['capacity']} used today"
        f" across {cap['numbers']} number(s)"
    )

    # The queue. This is the half that needs a person.
    lines.append("")
    if data["holds"]:
        lines.append(f"*{len(data['holds'])} draft(s) waiting on approval*")
        for h in data["holds"][:5]:
            conf = f"{h['confidence']:.0%}" if h["confidence"] is not None else "n/a"
            lines.append(f"  {h['phone']} ({conf}): {str(h['body'])[:90]}")
        if len(data["holds"]) > 5:
            lines.append(f"  and {len(data['holds']) - 5} more")
    else:
        lines.append("No drafts waiting.")

    if data["paused"]:
        lines.append(f"*{len(data['paused'])} thread(s) with a human*")
        for p in data["paused"][:5]:
            lines.append(f"  {p['phone']}: {p['paused_reason']}")

    if data["followups_due"]:
        lines.append(
            f"*{len(data['followups_due'])} soft no(s) past {SOFT_NO_FOLLOWUP_DAYS} days,"
            " worth another touch*"
        )
        lines.append("  " + ", ".join(f["phone"] for f in data["followups_due"][:10]))

    if data["failed"]:
        lines.append(f"*{len(data['failed'])} send failure(s)*")
        for f in data["failed"][:5]:
            lines.append(f"  {f['phone']}: {str(f['error'])[:80]}")

    if data["stale_events"]:
        lines.append(
            f"*{data['stale_events']} webhook event(s) unprocessed for over a day.*"
            " The worker may not be running."
        )

    if config.DRY_RUN:
        lines.append("")
        lines.append("_Dry run is ON: nothing was actually sent or written._")
    return "\n".join(lines)


def run(days: int = 1, post: bool = False) -> str:
    text = render(collect(days))
    if post:
        escalate._post(text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
    return text
