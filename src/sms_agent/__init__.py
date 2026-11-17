"""Two-way SMS agent for SiftStack.

Sends outreach, reads replies in real time, classifies them, writes the result
back to DataSift (phone status CORRECT / WRONG / DEAD, opt-outs, lead status),
and hands positive responses to a human in Slack.

Architecture in one paragraph: DataSift webhooks cannot see an inbound text
(the ten sequence triggers are all CRM state changes), so the inbound leg runs
on smrtPhone's `smsIncoming` webhook and the CRM leg runs on DataSift's. Both
post to the same receiver, which persists and returns 200 immediately; a worker
does the slow work and is the only thing that can put a message on a phone.

    receiver.py  verify -> persist -> 200
    worker.py    drain events, then drain the outbox (all guards live here)
    engine.py    webhook -> classification -> CRM writes -> escalation -> draft
    classify.py  deterministic rules first, model second
    respond.py   playbook-driven drafting, hard-validated before it can queue
    sender_pool  sticky per-conversation sender, caps, recipient-local quiet hours
    crm.py       reisift writes (phone status, tags, status, notes, assignment)
    store.py     SQLite: the durable event log and conversation state
"""

__all__ = [
    "classify",
    "config",
    "crm",
    "engine",
    "escalate",
    "knowledge",
    "sender_pool",
    "respond",
    "smrtphone",
    "store",
    "worker",
]
