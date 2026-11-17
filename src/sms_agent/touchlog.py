"""The day's sent texts, for the touch audit (build plan 0.23).

"No wrong touch" is only a promise until something counts the wrong ones. Each morning the brain
takes yesterday's calls and mail pieces and asks the gate about each. Texts were the gap: they
are not in DataSift's log (smrtPhone and Smarter Contact keep their own), so the audit could not
see them at all. This is the texting app's side of that: one function that says what went out,
read from this app's own store with no network.

What it can see, and says so in its answer:
  * every text this app sent through smrtPhone (the queue's own ledger);
  * every text a PERSON sent from the smrtPhone inbox that the webhook reported;
  * every auto reply this app sent through Smarter Contact.
What it cannot: the campaign texts Smarter Contact sends itself. Those never pass through here;
they are in Smarter Contact's own delivery report.

A row carries what the audit needs to ask the gate and nothing more: the record (and the owner,
where known), the last four digits of the number, the moment, and which system. Never the body,
never the whole number: this feed leaves the app, and the audit does not need either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, store

NOT_SEEN = "campaign texts sent by Smarter Contact itself are not in this app's store"


def day_bounds(day: str = "", tz: str = "") -> tuple[str, str, str]:
    """(the day, its start, its end) as UTC timestamps, for one business-local day. Yesterday
    when no day is given: the audit runs the morning after."""
    zone = ZoneInfo(tz or config.BUSINESS_TZ)
    if day:
        start = datetime.fromisoformat(day).replace(tzinfo=zone)
    else:
        start = (datetime.now(zone) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (start.date().isoformat(), start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _who(phone: str) -> tuple[str, str]:
    """(record id, owner id) for a number, from the store. Either may be empty."""
    mapped = store.lookup_phone(phone) or {}
    conv = store.get_conversation(phone) or {}
    return (conv.get("record_uuid") or mapped.get("record_uuid") or ""), (mapped.get("owner_uuid") or "")


def sent_texts(day: str = "", tz: str = "") -> list[dict]:
    """Every text this app knows went out on one business-local day, oldest first.

    Each row: at (UTC), system (smrtphone or smartercontact), author (agent or person), kind
    (what sort of text it was), last4, record_uuid, owner_uuid, from_last4.
    """
    _, start, end = day_bounds(day, tz)
    c = store._conn()
    out: list[dict] = []

    def add(at: str, system: str, author: str, kind: str, phone: str, from_number: str = "") -> None:
        record, owner = _who(phone)
        out.append({"at": at, "system": system, "author": author, "kind": kind or "",
                    "last4": store.clean_phone(phone)[-4:], "record_uuid": record, "owner_uuid": owner,
                    "from_last4": store.clean_phone(from_number)[-4:] if from_number else ""})

    # Ours, through smrtPhone. A dry run marks the row sent without sending, so it is not a touch.
    for r in c.execute(
            "SELECT phone, from_number, intent, sent_at FROM outbox WHERE status='sent'"
            " AND COALESCE(error,'')<>'dry run' AND sent_at>=? AND sent_at<? ORDER BY sent_at", (start, end)):
        add(r["sent_at"], "smrtphone", "agent", r["intent"], r["phone"], r["from_number"] or "")
    # A person, from the smrtPhone inbox.
    for r in c.execute(
            "SELECT phone, from_number, created_at FROM messages WHERE direction='out' AND author='human'"
            " AND created_at>=? AND created_at<? ORDER BY created_at", (start, end)):
        add(r["created_at"], "smrtphone", "person", "inbox", r["phone"], r["from_number"] or "")
    # Ours, through Smarter Contact.
    for r in c.execute(
            "SELECT phone, intent, done_at FROM sc_outbox WHERE kind='reply' AND status='sent'"
            " AND done_at>=? AND done_at<? ORDER BY done_at", (start, end)):
        add(r["done_at"], "smartercontact", "agent", r["intent"], r["phone"])
    out.sort(key=lambda x: x["at"])
    return out


def feed(day: str = "", tz: str = "") -> dict:
    """The answer the audit reads: the day, its texts, and what this feed cannot see."""
    day, start, end = day_bounds(day, tz)
    rows = sent_texts(day, tz)
    by_system: dict[str, int] = {}
    for r in rows:
        key = "%s by %s" % (r["system"], r["author"])
        by_system[key] = by_system.get(key, 0) + 1
    return {"day": day, "from": start, "to": end, "count": len(rows), "by_system": by_system,
            "not_seen": NOT_SEEN, "texts": rows}
