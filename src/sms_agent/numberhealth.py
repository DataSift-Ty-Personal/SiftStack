"""Number health: a rest list for a flagged sending number, and a canary before a campaign day
(build plan 4.7).

The phone number is the asset. A carrier filters a number into silence long before anyone
complains, and from the inside a filtered number looks exactly like a quiet audience. Two habits
came out of that:

  REST, DO NOT REPLACE. A number the carriers have flagged recovers in about five days if it
  stops sending. Swapping it for a new one throws away its registration and its history, and
  moves every owner it was texting to a number they have never seen, which breaks the rule that
  an owner keeps one texting number. So a flagged number goes on the rest list: it takes no new
  conversations and sends no outreach until its rest ends. A thread already on it stays on it
  and waits. An answer to somebody who just wrote is still sent: silence to a reply is worse
  for the owner than one more text is for the number.

  CANARY BEFORE A CAMPAIGN DAY. The selftest cannot know that four of five numbers were never
  registered for A2P 10DLC; that was found by sending one text from each and reading what came
  back. The canary stages one line-check text per sending number to OUR OWN handset. It goes
  through the same queue as every other text (so there is still exactly one place that can put
  a message on a phone), staged held, and the readout joins each one to the carrier's verdict.

Nothing here sends on its own. Resting only takes effect with SMS_AGENT_REST_FLAGGED on; the
canary only exists once a person stages it and releases it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, store
from .knowledge import lead_texts

log = logging.getLogger(__name__)

CANARY_INTENT = "canary"
# Carrier content flags on one number, inside the window, that mean "rest it".
REST_AT_SPAM_FLAGS = 2
# The plan's own line: a quarter of the pool flagged is ordinary, more is an alarm.
ALARM_SHARE = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS number_rest (
    number     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    rested_at  TEXT NOT NULL,
    until      TEXT NOT NULL,
    note       TEXT
);
"""


def _db():
    c = store._conn()
    c.executescript(SCHEMA)
    return c


def _key(number: str) -> str:
    return store.clean_phone(number)


# ---------------------------------------------------------------------- the rest list
def rest(number: str, reason: str, days: int | None = None, note: str = "", now: datetime | None = None) -> dict:
    """Put a sending number on the rest list. Resting it again extends the rest, never shortens it."""
    key = _key(number)
    if len(key) != 10:
        raise ValueError("not a phone number: %r" % number)
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(days=int(days if days is not None else config.REST_DAYS))
    _db()
    with store.tx() as c:
        row = c.execute("SELECT until FROM number_rest WHERE number=?", (key,)).fetchone()
        if row and row["until"] > until.isoformat(timespec="seconds"):
            until = datetime.fromisoformat(row["until"])
        c.execute(
            "INSERT INTO number_rest(number, reason, rested_at, until, note) VALUES (?,?,?,?,?)"
            " ON CONFLICT(number) DO UPDATE SET reason=excluded.reason, until=excluded.until, note=excluded.note",
            (key, reason, now.isoformat(timespec="seconds"), until.isoformat(timespec="seconds"), note or None))
    return {"number": key, "reason": reason, "until": until.isoformat(timespec="seconds")}


def unrest(number: str) -> bool:
    _db()
    with store.tx() as c:
        return c.execute("DELETE FROM number_rest WHERE number=?", (_key(number),)).rowcount > 0


def resting(now: datetime | None = None) -> dict[str, dict]:
    """number (10 digits) -> its rest, for every number still resting."""
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {r["number"]: dict(r) for r in _db().execute("SELECT * FROM number_rest WHERE until>?", (at,))}


def is_resting(number: str, now: datetime | None = None) -> bool:
    """Is this sending number resting? False whenever the switch is off: the list is then a
    record of what somebody noted, with no effect on what sends."""
    if not config.REST_FLAGGED:
        return False
    key = _key(number)
    if not key:
        return False
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return _db().execute("SELECT 1 FROM number_rest WHERE number=? AND until>? LIMIT 1", (key, at)).fetchone() is not None


def usable(numbers: list[str], now: datetime | None = None) -> list[str]:
    """The numbers a NEW conversation may start on. If every one is resting the whole list comes
    back: no numbers at all is a louder failure than a rested one sending, and the caller then
    holds the message anyway."""
    if not config.REST_FLAGGED:
        return list(numbers)
    fresh = [n for n in numbers if not is_resting(n, now)]
    return fresh or list(numbers)


def holds_outreach(from_number: str, intent: str) -> bool:
    """Should the worker hold this row because its number is resting? Outreach only."""
    return bool(str(intent or "").startswith("seed_touch") and is_resting(from_number))


# ---------------------------------------------------------------------- who is flagged
def flagged(days: int = 14, db_path: str | None = None) -> dict[str, dict]:
    """Sending numbers the carriers' own answers say are in trouble, and why.

    Read from the delivery callbacks this app already stores: a number whose texts were refused
    as spam, refused for registration, or whose reply rate sits under half its pool's.
    """
    from . import deliverability

    data = deliverability.collect(days=days, db_path=db_path)
    # Only numbers that are ours to rest. The event log remembers every number that ever sent,
    # including ones that left the pool months ago.
    ours = {_key(n) for n in config.numbers()}
    cold = set()
    for line in data["findings"]:
        if line.startswith("COLD NUMBERS"):
            cold |= {x.strip() for x in line.split(":", 1)[1].split(" reply at")[0].split(",")}
    out: dict[str, dict] = {}
    for number, n in data["numbers"].items():
        why = []
        spam = int(n["fail"].get("spam_flag", 0))
        if spam >= REST_AT_SPAM_FLAGS:
            why.append("%d texts refused as spam" % spam)
        if n["fail"].get("registration") or n.get("api_rejected"):
            why.append("not registered for this traffic")
        if number in cold:
            why.append("replies at under half its pool's rate")
        if why and _key(number) and (not ours or _key(number) in ours):
            out[_key(number)] = {"number": number, "pool": n["pool"], "sends": n["sends"], "why": why,
                                 "registration": bool(n["fail"].get("registration") or n.get("api_rejected"))}
    return out


def suggest(days: int = 14, db_path: str | None = None, now: datetime | None = None) -> dict:
    """What a person should do today: rest these, and these have finished their rest."""
    bad = flagged(days, db_path)
    on_rest = resting(now)
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    done = [dict(r) for r in _db().execute("SELECT * FROM number_rest WHERE until<=?", (at,))]
    return {"rest": [v for k, v in sorted(bad.items()) if k not in on_rest and not v["registration"]],
            "register": [v for k, v in sorted(bad.items()) if v["registration"]],
            "resting": [on_rest[k] for k in sorted(on_rest)], "rested": done}


# ---------------------------------------------------------------------- the canary
def canary_stage(to: str = "", pool: str = "", now: datetime | None = None) -> dict:
    """Stage one line-check text per sending number to our own handset. Held: nothing leaves
    until a person releases it (`canary_release`).

    Refuses without a handset of our own, and refuses a handset that is suppressed: the canary
    is the one text in this app that has no owner behind it, so the only thing that makes it
    safe is where it is going.
    """
    to = store.clean_phone(to or config.CANARY_TO)
    if len(to) != 10:
        return {"staged": 0, "refused": "no handset of our own to send to (SMS_AGENT_CANARY_TO)"}
    if store.is_suppressed(to):
        return {"staged": 0, "refused": "that handset is suppressed (%s)" % store.is_suppressed(to)}
    pools = config.number_pools()
    if pool:
        numbers = next((list(v) for k, v in pools.items() if k.strip().lower() == pool.strip().lower()), [])
    else:
        numbers = config.numbers()
    if not numbers:
        return {"staged": 0, "refused": "no sending numbers" + (" in pool %s" % pool if pool else "")}
    pending = {r["from_number"] for r in store._conn().execute(
        "SELECT from_number FROM outbox WHERE phone=? AND intent=? AND status IN ('held','queued')",
        (to, CANARY_INTENT))}
    store.ensure_conversation(to)
    staged = []
    for i, number in enumerate(numbers):
        if number in pending:
            continue
        store.queue_message(to, lead_texts.render_canary(number), from_number=number, status="held",
                            intent=CANARY_INTENT, confidence=1.0)
        staged.append(number)
    return {"staged": len(staged), "to_last4": to[-4:], "numbers": staged, "already_pending": len(pending)}


def canary_release() -> int:
    """Move the staged canary texts into the send queue. The deliberate go."""
    with store.tx() as c:
        return c.execute("UPDATE outbox SET status='queued' WHERE status='held' AND intent=?",
                         (CANARY_INTENT,)).rowcount


def canary_results(day: str = "") -> list[dict]:
    """Each sending number's canary for one day (UTC date, today by default), with the carrier's
    verdict where one has arrived."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    c = store._conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, from_number, status, error, sent_at, created_at FROM outbox"
        " WHERE intent=? AND substr(created_at, 1, 10)=? ORDER BY id", (CANARY_INTENT, day))]
    verdicts: dict[str, dict] = {}
    for r in c.execute("SELECT payload FROM events WHERE event_type='smsDeliveryCallback'"
                       " AND substr(received_at, 1, 10)>=?", (day,)):
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        sms_id, status = str(p.get("smsId") or ""), str(p.get("status") or "").lower()
        if sms_id and status in ("delivered", "undelivered", "failed"):
            verdicts[sms_id] = {"status": status, "reason": str(p.get("failure_reason") or "")}
    out = []
    for row in rows:
        sms = c.execute(
            "SELECT sms_id FROM messages WHERE direction='out' AND intent=? AND from_number=?"
            " AND substr(created_at, 1, 10)=? AND sms_id<>'' ORDER BY id DESC LIMIT 1",
            (CANARY_INTENT, row["from_number"], day)).fetchone()
        verdict = verdicts.get(sms["sms_id"]) if sms else None
        if row["status"] != "sent":
            result = {"held": "staged, not released", "queued": "waiting to send", "cancelled": "cancelled",
                      "failed": "send refused"}.get(row["status"], row["status"])
            detail = row["error"] or ""
        elif row["error"] == "dry run":
            result, detail = "dry run, nothing sent", ""
        elif not verdict:
            result, detail = "sent, no carrier verdict yet", ""
        else:
            result, detail = verdict["status"], verdict["reason"]
        out.append({"number": row["from_number"], "result": result, "detail": detail})
    return out


# ---------------------------------------------------------------------- the readout
def report(days: int = 14, db_path: str | None = None) -> str:
    """The number-health readout, in the digest's register."""
    pool = config.numbers()
    plan = suggest(days, db_path)
    bad = len(plan["rest"]) + len(plan["register"]) + len(plan["resting"])
    share = (bad / len(pool)) if pool else 0.0
    lines = ["*Sending numbers, last %d day(s)*" % days,
             "%d of %d flagged or resting (%d%%). %s" % (
                 bad, len(pool), round(share * 100),
                 "A quarter is ordinary." if share <= ALARM_SHARE else "More than a quarter: stop and look before today's sends.")]
    if plan["rest"]:
        lines.append("Rest these, about %d days, do not replace them:" % config.REST_DAYS)
        lines += ["  %s (%s): %s" % (v["number"], v["pool"], "; ".join(v["why"])) for v in plan["rest"]]
    if plan["register"]:
        lines.append("Not a rest problem. These need registering before they can send:")
        lines += ["  %s (%s)" % (v["number"], v["pool"]) for v in plan["register"]]
    if plan["resting"]:
        lines.append("Resting:")
        lines += ["  %s until %s (%s)" % (r["number"], r["until"][:10], r["reason"]) for r in plan["resting"]]
    if plan["rested"]:
        lines.append("Rest finished, back in the pool: " + ", ".join(r["number"] for r in plan["rested"]))
    canary = canary_results()
    if canary:
        lines.append("Today's line check:")
        lines += ["  %s: %s%s" % (r["number"], r["result"], (" (" + r["detail"][:80] + ")") if r["detail"] else "")
                  for r in canary]
    if not config.REST_FLAGGED:
        lines.append("_Resting is switched off (SMS_AGENT_REST_FLAGGED): the list above changes nothing that sends._")
    return "\n".join(lines)
