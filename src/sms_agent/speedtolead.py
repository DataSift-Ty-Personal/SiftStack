"""The text that says a call is coming (build plan 2.2).

An owner who raises a hand should hear from a person within five minutes. Measured over one week
in November, the median wait from New Lead to the first call was 18.8 hours. The call is the
lead manager's; what this app can do is tell the owner, about three minutes after they raised
their hand, that the call is on its way, so the lead is still warm when it comes. A lead that
arrives at night gets the text when the window opens in the morning.

It is one text, fixed copy, no question, and it goes through the same queue as every other
outbound: the two send windows, the suppression table, the gate and the thread's own sending
number all apply. A reply to a Smarter Contact campaign is answered through Smarter Contact, on
the number that campaign used, never from a smrtPhone number the owner has not seen.

What stops it, all of which are checked again when it is about to leave:
  * a person texted or called first (the thread is no longer paused "handed to", it is taken);
  * the owner wrote again after it was staged (the conversation moved on);
  * the gate holds the record or the number;
  * it is more than an hour late (or past noon, for the morning text). "I will call you in a
    few minutes" is a promise with a time in it, and sent late it is simply not true.

Three settings (SMS_AGENT_SPEED_TO_LEAD): off, the default, sends nothing, which is what the
agent has done since 2026-08-11; held stages the text for a person to release; auto sends it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, gate_rules, gatekeeper, respond, sender_pool, store
from .knowledge import lead_texts, sc_replies, touches

log = logging.getLogger(__name__)

INTENT = "SPEED_TO_LEAD"
SC_INBOX = "Smarter Contact"
# How long past its send time the text is still worth sending. After that it is dropped: the
# lead still gets the call, just not a promise that has already been broken or kept.
GOOD_FOR = 60 * 60
TOO_LATE = "too late: the promise of a call in a few minutes is no longer true"


def _skip(why: str) -> dict:
    return {"staged": False, "skipped": why}


def _pick_phone(record_uuid: str, rec: dict) -> str:
    """The number to text when the caller did not name one (a lead that came in by phone or by a
    web form): a number on this record that has texted us, most recent first, else one a person
    already confirmed. Never a guess off the list."""
    rows = store._conn().execute(
        "SELECT phone FROM conversations WHERE record_uuid=? AND last_inbound IS NOT NULL"
        " ORDER BY last_inbound DESC", (record_uuid,)).fetchall()
    for r in rows:
        if r["phone"] and not store.is_suppressed(r["phone"]):
            return r["phone"]
    for n in gate_rules.usable_numbers(rec, "text", gatekeeper.settings(), None, gatekeeper.litigators()):
        if n["status"] == "CORRECT" and not store.is_suppressed(n["number"]):
            return n["number"]
    return ""


def _until_key(phone: str) -> str:
    return "lead_text_until:" + store.clean_phone(phone)


def good_until(at: datetime, morning: bool) -> datetime:
    """The last moment this text is still true: an hour after its send time, and for the
    morning text never past noon on the business clock."""
    until = at + timedelta(seconds=GOOD_FOR)
    if morning:
        local = at.astimezone(ZoneInfo(config.BUSINESS_TZ))
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        until = min(until, noon.astimezone(timezone.utc))
    return until


def expired(phone: str, now: datetime | None = None) -> bool:
    """Is the call-is-coming text staged for this number past the moment it was good for?
    False when nothing was recorded, so a row from before this rule is never dropped by it."""
    until = store.get_meta(_until_key(phone))
    if not until:
        return False
    try:
        return (now or datetime.now(timezone.utc)) > datetime.fromisoformat(until)
    except ValueError:
        return False


def _contact_id(context: dict, phone: str) -> str:
    link = str((context or {}).get("reply_link") or "")
    if "/messenger-full/" in link:
        return link.rsplit("/messenger-full/", 1)[1].strip("/")
    from . import sc_client

    if not sc_client.configured():
        return ""
    try:
        return sc_client.contact_id_for(phone)
    except sc_client.SCError as exc:
        log.warning("speed to lead: no Smarter Contact contact for the number ending %s: %s", phone[-4:], exc)
        return ""


def stage(record_uuid: str, phone: str = "", source: str = "", now: datetime | None = None,
          first_seen: str = "", require_lead: bool = False) -> dict:
    """Stage the call-is-coming text for one lead. Returns what was staged, or why not.

    `first_seen` is when the hand was raised; the text is timed from it, so a burst of three
    texts that took a minute to settle does not push the promise a minute later.
    `require_lead` is for a lead this app only heard about from a DataSift sequence: the record
    must actually read as a lead, so a stray webhook can never text a cold record.
    """
    mode = config.SPEED_TO_LEAD
    if mode == "off":
        return _skip("switched off")
    if not record_uuid:
        return _skip("no record")
    rec, custom = gatekeeper.read(record_uuid, fresh=True)
    if not rec:
        return _skip("the record could not be read")
    if require_lead and gate_rules.status_of(rec) not in gate_rules.LEAD_STATUSES:
        return _skip("the record is not a lead (status %s)" % (gate_rules.status_of(rec) or "blank"))
    phone = store.clean_phone(phone) or _pick_phone(record_uuid, rec)
    if len(phone) != 10:
        return _skip("no number this owner has texted from or confirmed")
    if store.is_suppressed(phone):
        return _skip("the number is suppressed (%s)" % store.is_suppressed(phone))

    # An answer to someone who just texted is held only by the hard rules (a litigator, an
    # opt-out, a death, a sold house). A lead we only heard about gets the whole gate, with the
    # one allowance that it IS a lead.
    held = gatekeeper.reasons(rec, phone=phone, allow=("lead",), program="seller",
                              hard_only=not require_lead, custom=custom)
    if held:
        return _skip("the gate holds it: %s" % held[0])

    mapped = store.lookup_phone(phone) or {}
    context = mapped.get("context") or {}
    conv = store.get_conversation(phone) or {}
    if conv.get("state") in ("opted_out", "closed"):
        return _skip("the thread is %s" % conv.get("state"))
    if conv.get("state") == "paused" and not str(conv.get("paused_reason") or "").startswith("handed to"):
        return _skip("a person already has this thread (%s)" % (conv.get("paused_reason") or "paused"))

    owner = " ".join(x for x in (context.get("owner_first"), context.get("owner_last")) if x)
    first = "" if (context.get("owner_company") or touches.is_entity(owner)) else touches.clean_first(
        store.addressed_first_name(phone) or context.get("owner_first") or "")
    caller = (config.LEAD_CALLER_NAME or "").strip()
    signer = (context.get("assigned_name") or config.HANDOFF_NAME or "").strip()
    same_person = bool(caller) and caller.lower() == signer.lower()

    now = now or datetime.now(timezone.utc)
    raised = now
    if first_seen:
        try:
            raised = datetime.fromisoformat(first_seen)
        except ValueError:
            raised = now
    at = max(now, raised + timedelta(seconds=max(0, int(config.SPEED_TO_LEAD_DELAY))))
    morning = False
    if not sender_pool.within_quiet_hours(phone, at):
        at = sender_pool.next_send_window(phone, at)
        morning = at.astimezone(ZoneInfo(config.BUSINESS_TZ)).hour < 12

    message = lead_texts.render_call(f"{record_uuid}|{phone}", first=first, caller=caller,
                                     same_person=same_person, morning=morning)
    if not message:
        return _skip("nobody is named to make the call (SMS_AGENT_LEAD_CALLER_NAME)")
    ok, problems = respond.validate(message, max_questions=0)
    in_sc = context.get("reply_in") == SC_INBOX
    if in_sc:
        problems = list(problems) + sc_replies.problems(message)
    if problems or not ok:
        # Never send copy that fails a check. Fixed copy that fails means a name went wrong.
        log.warning("speed to lead text blocked for the number ending %s: %s", phone[-4:], problems)
        return _skip("the text failed its checks: %s" % "; ".join(problems))

    status = "queued" if mode == "auto" else "held"
    when = at.isoformat(timespec="seconds")
    until = good_until(at, morning).isoformat(timespec="seconds")
    if in_sc:
        contact_id = _contact_id(context, phone)
        if not contact_id:
            return _skip("no Smarter Contact contact for this number")
        if not store.mark_notified(phone, "speed_to_lead_" + now.date().isoformat()):
            return _skip("already staged for this number today")
        store.set_meta(_until_key(phone), until)
        row = store.sc_queue(phone, contact_id, "reply", message, INTENT, not_before=when, status=status)
        system = "smartercontact"
    else:
        from_number = conv.get("from_number") or sender_pool.assign(phone, context.get("assigned_name", ""))
        if not from_number:
            return _skip("no sending number available")
        if not store.mark_notified(phone, "speed_to_lead_" + now.date().isoformat()):
            return _skip("already staged for this number today")
        store.set_meta(_until_key(phone), until)
        store.ensure_conversation(phone, from_number=from_number, record_uuid=record_uuid)
        row = store.queue_message(phone, message, from_number=from_number, not_before=when, status=status,
                                  intent=INTENT, confidence=1.0)
        system = "smrtphone"
    log.info("speed to lead text %s for the number ending %s via %s at %s (%s)",
             status, phone[-4:], system, when, source or "hand raise")
    return {"staged": True, "row": row, "system": system, "status": status, "at": when, "good_until": until,
            "phone": phone, "message": message, "morning": morning}


def from_webhook(payload: dict, record_uuid: str) -> dict | None:
    """A DataSift sequence webhook that means "this record just became a New Lead".

    Only a delivery from a sequence named in SMS_AGENT_SPEED_TO_LEAD_SEQUENCES counts, and the
    record must read as a lead when it is opened. None for anything else, DataSift's own test
    delivery included.
    """
    event = str(payload.get("event_type") or payload.get("event") or "")
    if event == "webhook.test":
        return None
    sequence = payload.get("sequence") if isinstance(payload.get("sequence"), dict) else {}
    name = str(sequence.get("name") or "").strip()
    if not name or name.lower() not in config.SPEED_TO_LEAD_SEQUENCES:
        return None
    return stage(record_uuid, source="DataSift sequence %s" % name, require_lead=True)


def release(phone: str) -> int:
    """Release the staged call-is-coming text for one number, on whichever system holds it.
    One that is already too late is cancelled instead, and 0 comes back."""
    p = store.clean_phone(phone)
    if expired(p):
        with store.tx() as c:
            c.execute("UPDATE outbox SET status='cancelled', error=? WHERE phone=? AND status='held' AND intent=?",
                      (TOO_LATE, p, INTENT))
            c.execute("UPDATE sc_outbox SET status='cancelled', detail=?, done_at=? WHERE phone=? AND kind='reply'"
                      " AND status='held' AND intent=?", (TOO_LATE, store.now(), p, INTENT))
        return 0
    with store.tx() as c:
        n = c.execute("UPDATE outbox SET status='queued' WHERE phone=? AND status='held' AND intent=?",
                      (p, INTENT)).rowcount
        n += c.execute("UPDATE sc_outbox SET status='queued' WHERE phone=? AND kind='reply' AND status='held'"
                       " AND intent=?", (p, INTENT)).rowcount
    return n
