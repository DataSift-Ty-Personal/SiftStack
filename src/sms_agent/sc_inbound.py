"""Smarter Contact replies: classify, write DataSift, hand hot leads to Adriana.

Smarter Contact's ConnectSI webhook (`message.received.v1`) delivers every
inbound text the moment it lands, which is the same signal smrtPhone's
smsIncoming gives the agent. So a reply to the empathy campaign runs through the
SAME classifier and the SAME CRM and Slack actions as a smrtPhone reply.

The one hard difference: the conversation lives in Smarter Contact, so nothing
here EVER goes through smrtPhone. A smrtPhone number answering a Smarter Contact
thread is a stranger texting the owner from a second number. Replies leave
through Smarter Contact itself, on the thread's own number, from the separate
`sc_outbox` queue, and only when SMARTERCONTACT_AUTO_REPLY is on (Ty,
2026-10-27): fixed copy in Adriana's voice, never a price, a company or a
promise. An interested owner gets no reply at all; Adriana takes over.

Contract (docs.smartercontact.com/webhooks.yaml, read 2026-10-21):
  body  {id: evt_..., event, created_at, email, test?, data: {account_id,
         message_id, contact_id, from_number (E.164 or null), to_number,
         content}}  -- no name, no address, no labels
  auth  X-SC-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">,
        keyed with the account's whsec_ secret as-is. `t` is fixed at first
        queueing and retries run ~90 minutes, so the age window must be hours,
        not minutes.
  retry up to 9 attempts on non-2xx, at least once: dedupe on `id`.
"""
from __future__ import annotations

import hashlib
import json
import hmac
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from . import classify, config, crm, engine, escalate, respond, sc_client, sender_pool, store
from .knowledge import sc_replies, touches

log = logging.getLogger(__name__)

SOURCE = "smartercontact"
EVENT = "message.received.v1"
REPLY_IN = "Smarter Contact"
# One conversation, opened full screen with its call button (verified 2026-10-28).
CONVERSATION_URL = "https://app.smartercontact.com/messenger-full/{contact_id}"
RETRIES = 3
HEALTH_EVERY = timedelta(hours=3)


def signature_ok(header: str, raw: bytes, secret: str, now: Optional[float] = None,
                 max_age: Optional[int] = None) -> bool:
    """True when X-SC-Signature proves this body came from our account.

    Fails closed: no secret configured, a malformed header, a stale timestamp or
    a mismatch all return False.
    """
    if not secret or not header:
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    stamp, sig = parts.get("t", ""), parts.get("v1", "")
    if not stamp.isdigit() or not sig:
        return False
    age = abs((time.time() if now is None else now) - int(stamp))
    if age > (config.SC_SIGNATURE_MAX_AGE if max_age is None else max_age):
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def bearer_ok(header: str, token: str) -> bool:
    """True when the Authorization header carries our Smarter Contact token."""
    if not token or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), token)


def normalize(payload: dict) -> dict:
    """Any Smarter Contact inbound body, as the message.received.v1 envelope.

    The webhook Smarter Contact's team sets up by hand (SMS_MESSAGE_RECEIVED)
    may send the legacy flat body (`message.received.v0`: senderPhoneNumber,
    recipientPhoneNumber, senderId, content, createdAt, direction), which has
    no event name and NO event id. The receiver dedupes on the id, so without
    one every reply after the first would be dropped as a duplicate; the id
    here is built from sender, timestamp and content instead. A body in
    neither shape is returned unchanged and is stored, not processed, so the
    real format is visible on the first delivery.
    """
    if payload.get("event") or "senderPhoneNumber" not in payload:
        return payload
    if str(payload.get("direction") or "incoming").lower() != "incoming":
        return {**payload, "event": "message.sent.v0"}
    key = "|".join(str(payload.get(k) or "") for k in ("senderPhoneNumber", "createdAt", "content"))
    return {
        "id": "v0:" + hashlib.sha256(key.encode()).hexdigest()[:32],
        "event": EVENT,
        "created_at": payload.get("createdAt"),
        "email": payload.get("email"),
        "test": bool(payload.get("test")),
        "legacy_format": True,
        "data": {"message_id": "", "contact_id": payload.get("senderId"),
                 "from_number": payload.get("senderPhoneNumber"),
                 "to_number": payload.get("recipientPhoneNumber"),
                 "content": payload.get("content")},
    }


def _sc_thread(phone: str, contact_id: str) -> tuple[str, list[dict], str]:
    """(contact_id, the Smarter Contact thread oldest first, error or '').

    The campaign texts were sent by Smarter Contact, not by us, so the local
    store has never seen them. Without the real thread the classifier reads a
    bare "yes" with no question in front of it.
    """
    if not sc_client.configured():
        return contact_id, [], ""
    try:
        contact_id = contact_id or sc_client.contact_id_for(phone)
        return contact_id, (sc_client.thread(contact_id) if contact_id else []), ""
    except sc_client.SCError as exc:
        log.warning("smartercontact thread read failed for %s: %s", phone, exc)
        return contact_id, [], str(exc)


def _history(sc_thread: list[dict], body: str, local: list[dict]) -> list[dict]:
    rows = list(sc_thread)
    # The newest inbound is the message being classified; it is passed on its own.
    if rows and rows[-1]["dir"] == "in" and rows[-1]["body"].strip() == body:
        rows = rows[:-1]
    if not rows:
        return local
    return [{"direction": r["dir"] or "out", "body": r["body"]} for r in rows]


def _leave_followups(contact_id: str) -> list[str]:
    """Pull a contact who just replied out of every follow-up not yet sent.

    Smarter Contact fixes a follow-up's recipients when it is created, so a
    reply that lands after that would otherwise still get the next touch: a
    second automated text to someone already in a conversation, or already
    told no.
    """
    if not (contact_id and sc_client.configured()):
        return []
    try:
        pulled = 0
        for camp in sc_client.pending_followups():
            # Before it starts, the recipients ARE its group; once sending, the campaign.
            in_group = False
            for g in camp["groups"]:
                try:
                    in_group = sc_client.remove_from_group(g, [contact_id]) or in_group
                except sc_client.SCError:
                    pass   # a follow-up's hidden snapshot group answers "Group not found"
            pulled += bool(sc_client.remove_from_campaign(contact_id, camp["id"]) or in_group)
    except sc_client.SCError as exc:
        log.warning("could not clear %s from pending follow-ups: %s", contact_id, exc)
        return [f"pending follow-ups NOT cleared: {str(exc)[:100]}"]
    return [f"removed from {pulled} pending follow-up(s)"] if pulled else []


def _p1_record(contact_id: str) -> str:
    if not (contact_id and sc_client.configured()):
        return ""
    try:
        return sc_client.contact_record_uuid(contact_id)
    except sc_client.SCError as exc:
        log.warning("smartercontact contact record read failed for %s: %s", contact_id, exc)
        return ""


def _status_of(record_uuid: str) -> str:
    status = (crm.get_record(record_uuid) or {}).get("status")
    if isinstance(status, dict):
        status = status.get("slug") or status.get("title")
    return str(status or "").strip().lower()


def _person_in_thread(phone: str, sc_thread: list[dict]) -> bool:
    """Has someone typed in this thread by hand? Our own sends do not count."""
    ours = {r["body"].strip() for r in store.sc_replies(phone)}
    return any(r["dir"] == "out" and r["manual"] and r["body"].strip() not in ours for r in sc_thread)


def handle(payload: dict) -> dict:
    if payload.get("event") != EVENT:
        # message.delivery_status.v1 and the deprecated v0 are logged by the
        # receiver and need nothing here.
        return {"action": "ignored", "reason": f"no handler for {payload.get('event')!r}"}
    if payload.get("test"):
        return {"action": "ignored", "reason": "Smarter Contact test delivery"}

    data = payload.get("data") or {}
    phone = store.clean_phone(data.get("from_number"))
    body = (data.get("content") or "").strip()
    message_id = str(data.get("message_id") or payload.get("id") or "")
    if not phone:
        return {"action": "ignored", "reason": "no from number"}

    state_before = (store.get_conversation(phone) or {}).get("state") or ""
    # No from_number on the conversation: an SC number must never become the
    # sticky sender the smrtPhone outbox would send from.
    conv = store.ensure_conversation(phone)
    store.add_message(phone, "in", body, from_number=data.get("to_number") or "",
                      sms_id=message_id, author="owner")

    contact_id, sc_thread, sc_error = _sc_thread(phone, str(data.get("contact_id") or ""))
    mapped = store.lookup_phone(phone) or {}
    record_uuid = mapped.get("record_uuid") or conv.get("record_uuid") or ""
    # The record this contact was pushed from outranks the phone map, which
    # holds whichever record an older campaign saw first.
    p1_record = _p1_record(contact_id)
    if p1_record and p1_record != record_uuid:
        record_uuid, mapped = p1_record, {}
        store.update_conversation(phone, record_uuid=record_uuid)
    thread = store.thread(phone)
    result = classify.classify(body, _history(sc_thread, body, thread[:-1]))
    store.stamp_intent(phone, message_id, result.intent, result.confidence)
    log.info("smartercontact %s [%s %.2f via %s] %s", phone, result.intent,
             result.confidence, result.source, body[:80])

    outcome = {"phone": phone, "record_uuid": record_uuid, "channel": SOURCE,
               "contact_id": contact_id, "classification": result.to_dict(),
               "actions": []}
    if sc_error:
        outcome["actions"].append(f"Smarter Contact thread unreadable: {sc_error[:120]}")
    # Anyone who answers is done with the burst, whatever they said.
    outcome["actions"] += _leave_followups(contact_id)

    # Priority 1 contacts came from DataSift, so the phone resolves to the record.
    matches: list[dict] = []
    if not record_uuid:
        matches = crm.find_records_by_phone(phone)
        if matches:
            record_uuid = matches[0]["record_uuid"]
            store.update_conversation(phone, record_uuid=record_uuid)
            outcome["record_uuid"] = record_uuid
            outcome["actions"].append(
                f"resolved from the CRM by phone ({len(matches)} record(s) carry this number)")

    intent = result.intent
    if not record_uuid:
        if intent in engine.TERMINAL_INTENTS:
            outcome["actions"] += engine._suppress_unknown(phone, intent)
            outcome["actions"] += _label(phone, contact_id, intent) + _sc_dnc(phone, contact_id, intent)
            outcome["action"] = intent.lower()
            return outcome
        outcome["action"] = "unmapped"
        return outcome

    # reply_in rides on the stored context so the debounced Slack post (built
    # later by the worker from this context) tells Adriana where the thread is.
    context = mapped.get("context") or crm.deal_context(record_uuid) or {}
    context = dict(context, reply_in=REPLY_IN)
    if contact_id:
        context["reply_link"] = CONVERSATION_URL.format(contact_id=contact_id)
    store.map_phone(phone, record_uuid=record_uuid, context=context)

    # Decided BEFORE the handlers run, because they close and pause threads.
    why_not = _why_no_reply(phone, contact_id, state_before, sc_thread, sc_error)
    if why_not == PERSON_REPLYING:
        store.pause_conversation(phone, f"a person replied in {REPLY_IN}")
    # A model read under the floor is never guessed at: a setter would explain
    # and ask, so it gets the clarify answer instead of a person (Ty, 2026-10-30).
    # A failed model call ("fallback") counts too; it used to mean silence.
    unsure = result.source != "rules" and result.confidence < config.SC_MIN_CONFIDENCE
    # A question about who we are still gets answered after a no (Ty, 2026-10-30),
    # with the answer that signs off rather than asking about selling again.
    closing = (why_not == "thread was already closed" and not unsure
               and (intent == "ASKING_WHO" or sc_replies.asks_how(body)))
    if closing:
        why_not = ""

    if intent == "OPT_OUT":
        # Smarter Contact suppresses STOP itself; this keeps DataSift and the
        # smrtPhone do-not-text list in step without anyone exporting.
        outcome["actions"] += engine._do_opt_out(phone, record_uuid, matches, body)
        outcome["actions"] += _label(phone, contact_id, intent) + _sc_dnc(phone, contact_id, intent)
        store.sc_cancel(phone, "opted out")
        outcome["action"] = "opted_out"
    elif intent != "ESCALATE" and BARE_NO.match(body):
        # A bare "No" is Not Interested however sure the model is: no apology,
        # no DNC, and the phone stays unverified, because it can as well mean
        # "not my number" (Ty, 2026-11-03; 85 read as wrong numbers at 0.72-0.85
        # got the apology and a DNC between Oct 30 and Nov 2).
        outcome["actions"] += _not_interested(phone, record_uuid, contact_id, body, context,
                                              reason="bare no, may be a wrong number", verified=False)
        store.sc_cancel(phone, "bare no")
        outcome["action"] = "not_interested"
    elif intent == "WRONG_NUMBER":
        outcome["actions"] += engine._do_wrong_number(phone, record_uuid, body, context, matches)
        # The apology queues FIRST and the DNC after it: Smarter Contact refuses
        # any text to a contact on its DNC, apology included (10 lost 2026-10-29).
        outcome["actions"] += _reply(phone, contact_id, intent, body, context,
                                     why_not or (f"unsure of the read ({result.confidence:.2f})" if unsure else ""))
        outcome["actions"] += _label(phone, contact_id, intent) + _sc_dnc(phone, contact_id, intent)
        outcome["action"] = "wrong_number"
    elif intent == "ESCALATE":
        outcome["actions"] += engine._do_sensitive(phone, record_uuid, body, result, matches)
        store.sc_cancel(phone, "sensitive reply")
        outcome["actions"] += _sc_dnc(phone, contact_id, intent)
        outcome["action"] = "escalated"
    elif intent == "INTERESTED" and not _person_in_thread(phone, sc_thread) \
            and (not_worth := _not_worth_pushing(record_uuid, body)):
        outcome["actions"] += _drop_lead(phone, record_uuid, contact_id, body, context, not_worth)
        outcome["action"] = "not_interested"
    elif intent == "INTERESTED":
        outcome["actions"] += engine._do_hot_lead(phone, record_uuid, body, result, context, thread)
        store.pause_conversation(phone, f"handed to {config.HANDOFF_NAME} ({REPLY_IN})")
        store.sc_cancel(phone, f"handed to {config.HANDOFF_NAME}")
        outcome["action"] = "handoff"
    elif intent == "NOT_INTERESTED":
        outcome["actions"] += _not_interested(phone, record_uuid, contact_id, body, context, why_not)
        outcome["action"] = "not_interested"
    elif intent in engine.NEEDS_HUMAN_REPLY_INTENTS:
        # Asking again gets the next answer in line (who, then how we got the
        # number, then clarify); a person only once every one has gone out.
        acts = _reply(phone, contact_id, intent, body, context, why_not, closing=closing, unsure=unsure)
        if _queued(acts):
            outcome["actions"] += acts
            if not unsure:
                res = crm.set_phone_status(record_uuid, phone, "CORRECT")
                outcome["actions"].append(f"phone status -> CORRECT: {engine._report(res)}")
            outcome["action"] = "answered"
        else:
            outcome["actions"] += acts + engine._do_needs_answer(phone, record_uuid, body, result, inbox=REPLY_IN,
                                                                 context=context, unsure=unsure)
            outcome["action"] = "needs_human_reply"
    else:
        if intent == "OTHER" and sc_replies.owner_confirmed(body):
            # "Yes, it's mine" is the most verified a number gets (Ty, 2026-10-28).
            res = crm.set_phone_status(record_uuid, phone, "CORRECT")
            outcome["actions"].append(f"owner confirmed; phone CORRECT: {engine._report(res)}")
        acts = _reply(phone, contact_id, intent, body, context, why_not, closing=closing, unsure=unsure)
        outcome["actions"] += acts
        if unsure and not _queued(acts):
            # No answer left to give and no disposition: a person sees it
            # rather than nobody ("you should really update you calls").
            outcome["actions"] += engine._do_needs_answer(phone, record_uuid, body, result,
                                                          inbox=REPLY_IN, context=context, unsure=True)
        outcome["action"] = "answered" if _queued(acts) else "classified"
    if state_before == "opted_out":
        # A later "not for sale" from someone who already said STOP must not
        # downgrade the thread to closed (seen live 2026-10-28, two minutes apart).
        store.update_conversation(phone, state="opted_out")
    return outcome


PERSON_REPLYING = "a person is already replying here"


def _queued(acts: list[str]) -> bool:
    return bool(acts) and acts[0].startswith("queued")


def _why_no_reply(phone: str, contact_id: str, state_before: str, sc_thread: list[dict],
                  sc_error: str) -> str:
    """'' when an auto reply may be queued, else the reason it may not."""
    if not config.SC_AUTO_REPLY:
        return "auto reply is off"
    if not contact_id:
        return "no Smarter Contact contact id"
    if sc_error:
        return "Smarter Contact thread unreadable"
    if state_before in ("paused", "opted_out", "closed"):
        return f"thread was already {state_before}"
    if any(not r["dir"] for r in sc_thread):
        return "message direction unreadable"
    if _person_in_thread(phone, sc_thread):
        return PERSON_REPLYING
    if len(store.sc_replies(phone)) >= config.SC_MAX_AUTO_REPLIES:
        return f"{config.SC_MAX_AUTO_REPLIES} auto replies already on this thread"
    return ""


def _not_interested(phone: str, record_uuid: str, contact_id: str, body: str, context: dict,
                    why_not: str = "", reason: str = "", verified: bool = True) -> list[str]:
    """A no is a verified number, and that is all (Ty, 2026-10-28).

    No follow-up question and no sign-off: the phone goes CORRECT in Sift, the
    thread closes as a soft or firm no for the later rework, and the Smarter
    Contact contact is labeled. Nothing is texted back.
    """
    label = "firm no" if sc_replies.is_firm_no(body) else "soft no"
    note = f"{label} by text in {REPLY_IN}: \"{body[:150]}\"" + (f" ({reason})" if reason else "")
    acts = [engine._close_not_interested(phone, record_uuid, note, verified=verified)]
    return acts + [_set_ni_status(record_uuid)] + _label(phone, contact_id, "NOT_INTERESTED")


# Only the whole message: "No" alone, never "no, but call me next year".
BARE_NO = re.compile(r"^\s*(?:no+|nope|nah|naw|negative|no\s+(?:sir|ma'?am))\s*[.!]*\s*$", re.I)

# Money in a reply: "650000", "155k", "$1.2 million", "2 million", "$500,000.00".
_MONEY = re.compile(r"(?<![\d.,])(\$\s*)?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
                    r"(million|mill|mil|thousand|grand|k|m)?\b", re.I)
_SCALE = {"million": 1e6, "mill": 1e6, "mil": 1e6, "m": 1e6, "thousand": 1e3, "grand": 1e3, "k": 1e3}


def asking_price(text: str) -> int:
    """The largest dollar figure named in a reply, 0 if none. A bare number
    under 10,000 is not money ("10 acres", "Fri 2"), and a run of 9 or more
    digits is a phone number."""
    best = 0.0
    for m in _MONEY.finditer(text or ""):
        dollar, raw, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        digits = raw.replace(",", "")
        if "," not in raw and "." not in raw and len(digits) >= 9:
            continue
        n = float(digits) * _SCALE.get(unit, 1)
        if unit or dollar or n >= 10_000:
            best = max(best, n)
    if re.search(r"\b(?:a|one)\s+mil(?:l?ion)?\b", text or "", re.I):
        best = max(best, 1e6)
    if re.search(r"\bcouple\s+(?:of\s+)?mil(?:l?ion)?\b", text or "", re.I):
        best = max(best, 2e6)
    return int(best)


def _not_worth_pushing(record_uuid: str, body: str) -> str:
    """Why an interested reply is not worth Adriana's call, '' when it is.

    Listed on the MLS, or a price above SC_PRICE_CEILING times Sift's value
    (Ty, 2026-10-30). No price, or no value to compare against, is pushed."""
    rec = crm.get_record(record_uuid) or {}
    if str(rec.get("mls") or "").strip().lower() in config.SC_LISTED_MLS:
        return f"listed on the MLS ({rec.get('mls')})"
    listed = listing_status(rec)
    if listed:
        return f"active on Zillow ({listed})"
    ask, value = asking_price(body), float(rec.get("estimate_value") or 0)
    if ask and value and ask > value * config.SC_PRICE_CEILING:
        return f"asked ${ask:,} against a Sift value of ${value:,.0f} ({ask / value:.1f}x)"
    return ""


ZILLOW_URL = "https://api.openwebninja.com/realtime-zillow-data/property-details-address"
# Agent listings only: a for-sale-by-owner is not on the MLS and is worth the call.
_ON_MARKET = {"forsale", "pending", "comingsoon", "undercontract"}


def listing_status(rec: dict) -> str:
    """'For Sale by Agent, $750,000, 58 days' when Zillow has the house on the
    market, '' when it does not or cannot be read (a lookup failure pushes the
    lead rather than dropping it)."""
    a = rec.get("address") or {}
    if not (config.ZILLOW_API_KEY and a.get("street")):
        return ""
    where = f"{a['street']}, {a.get('city') or ''}, {a.get('state') or 'TN'} {a.get('postal_code') or ''}".strip()
    try:
        resp = requests.get(ZILLOW_URL, headers={"x-api-key": config.ZILLOW_API_KEY},
                            params={"address": where}, timeout=30)
        d = ((resp.json() or {}).get("data") or {}) if resp.ok else {}
    except (requests.RequestException, ValueError) as exc:
        log.warning("zillow listing check failed for %s: %s", where, exc)
        return ""
    status = str(d.get("keystoneHomeStatus") or d.get("homeStatus") or "").replace("_", "").lower()
    sub = d.get("listingSubType") or {}
    if status not in _ON_MARKET or sub.get("isFSBO"):
        return ""
    price = d.get("price")
    return ", ".join(x for x in ("pending" if sub.get("isPending") or status == "pending" else "for sale by agent",
                                 f"${price:,.0f}" if isinstance(price, (int, float)) else "",
                                 f"{d['daysOnZillow']} days" if d.get("daysOnZillow") is not None else "") if x)


def _drop_lead(phone: str, record_uuid: str, contact_id: str, body: str, context: dict,
               why: str) -> list[str]:
    """Disposition an interested reply as Not Interested instead of pushing it.

    A burst still in its debounce never posts. When the "yes" was already
    posted and the number arrives after it, the channel gets one line so
    Adriana does not spend the call."""
    acts = []
    if store.escalation_posted(phone):
        where = context.get("street") or escalate._fmt_phone(phone)
        ok = escalate.alert(f"{config.HANDOFF_NAME}: skip {where}",
                            f"> {body[:200]}\n_Not worth the call: {why}. Marked Not Interested._",
                            record_uuid, kind="handoff")
        acts.append("posted: skip this lead" if ok else "skip notice FAILED")
    store.clear_escalation(phone)
    acts += _not_interested(phone, record_uuid, contact_id, body, context, reason=f"not pushed: {why}")
    store.sc_cancel(phone, "not worth pushing")
    return [f"not pushed: {why}"] + acts


def _set_ni_status(record_uuid: str) -> str:
    """Not Interested on the record in Sift, the disposition the Smarter Contact
    label maps to. That integration only fires on a manual Export to CRM, so
    without this the status never moved (14 of 17 on launch day read blank)."""
    current = _status_of(record_uuid)
    if current == config.SC_NI_STATUS:
        return f"status already {current}"
    if current not in config.SC_NI_OVERWRITE:
        crm.post_note(record_uuid, f"Texted 'not interested' in {REPLY_IN}; status left as {current} "
                                   "because someone is working this record.")
        return f"status left as {current} (record is being worked)"
    return f"status -> {config.SC_NI_STATUS}: {engine._report(crm.set_status(record_uuid, config.SC_NI_STATUS))}"


# Replies whose number Smarter Contact itself must never text again: they told
# us to stop, or we told them we would take the number off our list.
SC_DNC_INTENTS = {"OPT_OUT", "WRONG_NUMBER"}


def _sc_dnc(phone: str, contact_id: str, intent: str) -> list[str]:
    """Queue the contact for Smarter Contact's DNC. Not behind the label switch:
    this is the do-not-text guarantee, not a disposition."""
    # A hostile reply is an ESCALATE that the agent suppresses; anyone the agent
    # will not text, Smarter Contact must not text either.
    if not (intent in SC_DNC_INTENTS or store.is_suppressed(phone)):
        return []
    if not (contact_id and sc_client.configured()):
        return []
    row = store.sc_queue(phone, contact_id, "dnc", "DNC", intent, not_before=_after_last_reply(phone))
    return [f"Smarter Contact DNC queued (#{row})"]


def _after_last_reply(phone: str) -> str:
    """Two minutes after this thread's last queued reply ('' = now)."""
    waiting = [r["not_before"] for r in store.sc_replies(phone) if r["status"] == "queued" and r["not_before"]]
    if not waiting:
        return ""
    return (datetime.fromisoformat(max(waiting)) + timedelta(minutes=2)).isoformat(timespec="seconds")


def _label(phone: str, contact_id: str, intent: str) -> list[str]:
    title = sc_replies.LABELS.get(intent)
    if not (title and config.SC_AUTO_LABEL):
        return []
    if not contact_id:
        return [f"label {title!r} not set: no Smarter Contact contact id"]
    row = store.sc_queue(phone, contact_id, "label", title, intent)
    return [f"label {title!r} queued (#{row})"]


def _reply(phone: str, contact_id: str, intent: str, body: str, context: dict, why_not: str,
           closing: bool = False, unsure: bool = False) -> list[str]:
    if why_not:
        return [] if why_not == "auto reply is off" else [f"no auto reply: {why_not}"]
    first = _first(context)
    sent = store.sc_replies(phone)
    template = sc_replies.pick(intent, body, 1 if closing else 0,
                               seed=f"{phone}|{intent}|{len(sent)}", has_first=bool(first),
                               used=frozenset(sc_replies.pool_of(r["body"]) for r in sent) - {""},
                               unsure=unsure, closing=closing)
    if not template and unsure:
        return ["no auto reply: unsure of the read, and every clarifying answer already went out"]
    if not template:
        return []
    message = sc_replies.render(template, first, context.get("street") or "",
                                context.get("city") or "", config.HANDOFF_NAME)
    problems = sc_replies.problems(message)
    # The street we texted is exempt from the zip check, literally and only it:
    # "12105 Sample Mill Rd" read as a zip and blocked the answer (2026-10-30).
    ok, blocked = respond.validate(message, max_questions=1, allowed_address=context.get("street") or "")
    if problems or not ok:
        # Never send copy that fails a check. A person sees the thread instead.
        problems = problems + list(blocked)
        log.warning("smartercontact reply blocked for %s: %s", phone, problems)
        return [f"auto reply blocked ({', '.join(problems)}); left for a person"]
    delay = random.randint(config.SC_REPLY_DELAY_MIN,
                           max(config.SC_REPLY_DELAY_MIN, config.SC_REPLY_DELAY_MAX))
    at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    if not sender_pool.within_quiet_hours(phone, at):
        at = sender_pool.next_send_window(phone, at)
    row = store.sc_queue(phone, contact_id, "reply", message, intent,
                         not_before=at.isoformat(timespec="seconds"))
    return [f"queued Smarter Contact reply #{row} for {at.isoformat(timespec='minutes')}"]


def _first(context: dict) -> str:
    owner = " ".join(x for x in (context.get("owner_first"), context.get("owner_last")) if x)
    if context.get("owner_company") or touches.is_entity(owner):
        return ""
    return touches.clean_first(context.get("owner_first") or "")


# ------------------------------------------------------------------ sending

def drain(limit: int = 20) -> dict:
    """Send what is due in the Smarter Contact queue. Called from the worker tick."""
    done = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    for row in store.sc_due(limit=limit):
        try:
            sender = {"label": _send_label, "dnc": _send_dnc}.get(row["kind"], _send_reply)
            outcome = sender(row)
        except Exception as exc:  # never let one row stop the queue
            log.exception("smartercontact row %s crashed", row["id"])
            store.sc_finish(row["id"], "failed", f"crash: {exc}")
            outcome = "failed"
        done[outcome] = done.get(outcome, 0) + 1
    return done


def _retry_or_fail(row: dict, exc: Exception, what: str) -> str:
    if row["attempts"] + 1 < RETRIES:
        at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        store.sc_finish(row["id"], "", f"{what} failed, retrying: {exc}", retry_at=at)
        return "retry"
    store.sc_finish(row["id"], "failed", f"{what} failed: {exc}")
    _health(f"Smarter Contact {what} failed", f"{store.clean_phone(row['phone'])}: {str(exc)[:300]}")
    return "failed"


def _send_label(row: dict) -> str:
    if config.DRY_RUN:
        store.sc_finish(row["id"], "skipped", "dry run")
        return "skipped"
    try:
        sc_client.add_labels([row["contact_id"]], row["body"].split("|"))
    except sc_client.SCError as exc:
        return _retry_or_fail(row, exc, "label")
    store.sc_finish(row["id"], "sent", f"labeled {row['body']}")
    return "sent"


def _send_dnc(row: dict) -> str:
    later = _after_last_reply(row["phone"])
    if later:   # an apology is still waiting; suppressing now would block it
        store.sc_reschedule(row["id"], later)
        return "retry"
    if config.DRY_RUN:
        store.sc_finish(row["id"], "skipped", "dry run")
        return "skipped"
    try:
        source = sc_client.add_contact_dnc(row["contact_id"])
    except sc_client.SCError as exc:
        return _retry_or_fail(row, exc, "DNC")
    store.sc_finish(row["id"], "sent", f"Smarter Contact suppression: {source}")
    return "sent"


def _send_reply(row: dict) -> str:
    phone = row["phone"]

    def skip(why: str) -> str:
        store.sc_finish(row["id"], "cancelled", why)
        return "skipped"

    if not config.SC_AUTO_REPLY:
        return skip("auto reply was switched off before it went out")
    state = (store.get_conversation(phone) or {}).get("state")
    if state in ("paused", "opted_out"):
        return skip(f"thread {state} after this was queued")
    supp = store.is_suppressed(phone)
    if supp and not (supp == "wrong_number" and row["intent"] == "WRONG_NUMBER"):
        return skip(f"suppressed: {supp}")
    if not sender_pool.within_quiet_hours(phone):
        store.sc_reschedule(row["id"], sender_pool.next_send_window(phone).isoformat(timespec="seconds"))
        return "retry"
    if config.DRY_RUN:
        store.sc_finish(row["id"], "skipped", "dry run")
        return "skipped"
    try:
        # The newest message must still be theirs. If anything was sent after it
        # (a person in the inbox, a campaign touch, our own earlier reply), this
        # answer is stale and two of us would be talking at once.
        latest = (sc_client.thread(row["contact_id"], limit=5) or [{}])[-1]
        if latest.get("dir") != "in":
            ours = {r["body"].strip() for r in store.sc_replies(phone)}
            if latest.get("dir") == "out" and latest.get("manual") \
                    and (latest.get("body") or "").strip() not in ours:
                store.pause_conversation(phone, f"a person replied in {REPLY_IN}")
                return skip("a person already replied in Smarter Contact")
            return skip(f"newest message is not theirs ({latest.get('raw_direction') or 'empty thread'})")
        msg_id = sc_client.send_message(row["contact_id"], row["body"])
    except sc_client.SCError as exc:
        if "dnc list" in str(exc).lower():
            # Not an outage: the contact is suppressed, which is the point.
            return skip("contact is on the Smarter Contact DNC list")
        return _retry_or_fail(row, exc, "reply")
    store.sc_finish(row["id"], "sent", f"message {msg_id}")
    store.add_message(phone, "out", row["body"], sms_id=msg_id, author="agent")
    store.bump_ai_turns(phone)
    log.info("smartercontact reply sent to %s (%s)", phone, row["intent"])
    return "sent"


def _health(title: str, detail: str) -> None:
    """Tell the channel the Smarter Contact side is broken, at most every few hours."""
    last = store.get_meta("sc_health_posted")
    if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - HEALTH_EVERY:
        log.warning("%s (alert already posted recently): %s", title, detail)
        return
    store.set_meta("sc_health_posted", store.now())
    escalate.alert(f":warning: {title}",
                   f"{detail}\n_Auto replies and labels in {REPLY_IN} are failing. Replies still land "
                   f"in {REPLY_IN}, so work the inbox by hand until this is fixed._", kind="sc_health")


# ------------------------------------------------------------------ repair

def _logged(row) -> tuple[dict, dict]:
    """(payload, outcome) of a stored event. The worker cuts outcomes at 2,000
    characters, so a long one is not valid JSON; fall back to what the payload
    and the phone map can say, rather than skipping the reply silently."""
    payload = normalize(json.loads(row["payload"] or "{}"))
    try:
        outcome = json.loads(row["outcome"] or "{}")
    except json.JSONDecodeError:
        phone = store.clean_phone((payload.get("data") or {}).get("from_number"))
        outcome = {"phone": phone, "record_uuid": (store.lookup_phone(phone) or {}).get("record_uuid") or "",
                   "contact_id": (payload.get("data") or {}).get("contact_id") or "", "truncated": True}
    return payload, outcome

def recheck_opt_outs(commit: bool = False) -> list[dict]:
    """Re-read every stored Smarter Contact reply with the CURRENT opt-out rules.

    Run after an opt-out rule changes. A reply the old rules read as a soft no
    is an opt-out that was never honored, and nothing else will go back for it.
    Dry by default; `commit` applies the full opt-out disposition.
    """
    found = []
    rows = store._conn().execute(
        "SELECT payload, outcome FROM events WHERE source=? AND outcome IS NOT NULL ORDER BY id",
        (SOURCE,)).fetchall()
    for row in rows:
        payload, outcome = _logged(row)
        phone, record_uuid = outcome.get("phone") or "", outcome.get("record_uuid") or ""
        body = ((payload.get("data") or {}).get("content") or "").strip()
        ruled = classify.classify_rules(body)
        if not (phone and record_uuid and ruled and ruled.intent == "OPT_OUT"):
            continue
        bereaved = bool(classify._hit(body.lower(), classify.BEREAVEMENT_NOW))
        if outcome.get("action") == "opted_out" and not bereaved:
            continue
        item = {"phone": phone, "was": outcome.get("action"), "body": body[:80], "actions": []}
        if commit:
            if store.is_suppressed(phone) != "opt_out":
                item["actions"] += engine._do_opt_out(phone, record_uuid, None, body)
                item["actions"] += _label(phone, outcome.get("contact_id") or "", "OPT_OUT")
                store.sc_cancel(phone, "opted out")
            elif bereaved:
                crm.post_note(record_uuid, f'SMS reply from {phone} reports a death: "{body[:220]}". '
                                           "That number asked us to stop texting; work the estate another way.",
                              pinned=True)
                item["actions"].append("pinned the reported death on the record")
        found.append(item)
    return found


def catch_up(commit: bool = False, since: str = "") -> list[dict]:
    """Queue what the agent would have sent for replies that came in while it was off.

    The NEWEST reply per thread only, read with the current rules. Skips any
    thread a person has already answered, any thread that is no longer the
    owner's turn, and any thread that already has a queued or sent reply, so
    running it twice queues nothing new. Dry by default.
    """
    latest: dict[str, tuple[dict, dict]] = {}
    for row in store._conn().execute(
            "SELECT payload, outcome FROM events WHERE source=? AND outcome IS NOT NULL"
            " AND received_at>=? ORDER BY id", (SOURCE, since)).fetchall():
        payload, outcome = _logged(row)
        if outcome.get("phone"):
            latest[outcome["phone"]] = (payload, outcome)

    planned = []
    for phone, (payload, outcome) in latest.items():
        record_uuid, contact_id = outcome.get("record_uuid") or "", outcome.get("contact_id") or ""
        body = ((payload.get("data") or {}).get("content") or "").strip()
        cl = outcome.get("classification") or {}
        ruled = classify.classify_rules(body)
        if ruled and ruled.intent == "OPT_OUT":
            intent = "OPT_OUT"
        else:
            intent = cl.get("intent") or (ruled.intent if ruled else "")
        if intent not in ("OPT_OUT", "ESCALATE") and BARE_NO.match(body):
            intent = "NOT_INTERESTED"   # never the wrong-number apology (Ty, 2026-11-03)
        item = {"phone": phone, "intent": intent, "body": body[:60], "actions": [], "skip": ""}
        planned.append(item)
        if not (record_uuid and contact_id):
            item["skip"] = "no record or no Smarter Contact contact"
            continue

        title = sc_replies.LABELS.get(intent)
        # A label that FAILED (a login outage) does not count as labeled.
        labeled = store._conn().execute("SELECT 1 FROM sc_outbox WHERE phone=? AND kind='label'"
                                        " AND status IN ('queued','sent')", (phone,)).fetchone()
        if title and not labeled:
            item["actions"].append(f"label {title}")
            if commit:
                _label(phone, contact_id, intent)
        if intent == "OTHER" and sc_replies.owner_confirmed(body):
            item["actions"].append("phone CORRECT")
            if commit:
                crm.set_phone_status(record_uuid, phone, "CORRECT")

        state = (store.get_conversation(phone) or {}).get("state") or ""
        supp = store.is_suppressed(phone)
        if intent in sc_replies.NO_REPLY_INTENTS:
            item["skip"] = "no text for this kind of reply"
        elif store.sc_replies(phone):
            item["skip"] = "already has a reply"
        elif intent != "WRONG_NUMBER" and (state in ("paused", "opted_out", "closed") or supp):
            item["skip"] = f"thread {state or supp}"
        elif cl.get("source") == "llm" and float(cl.get("confidence") or 0) < config.SC_MIN_CONFIDENCE:
            item["skip"] = f"unsure of the read ({float(cl.get('confidence') or 0):.2f})"
        else:
            try:
                sc_thread = sc_client.thread(contact_id)
            except sc_client.SCError as exc:
                item["skip"] = f"thread unreadable: {exc}"[:120]
                continue
            if not sc_thread or sc_thread[-1]["dir"] != "in":
                item["skip"] = "newest message is not theirs (someone already answered)"
            elif _person_in_thread(phone, sc_thread):
                item["skip"] = "a person is replying here"
            elif not sc_replies.pick(intent, body, 0, seed=f"{phone}|{intent}|0", has_first=True):
                item["skip"] = "nothing to say to this one"
            else:
                context = (store.lookup_phone(phone) or {}).get("context") or {}
                if commit:
                    item["actions"] += _reply(phone, contact_id, intent, body, context, "")
                else:
                    item["actions"].append("reply")
    return planned


_PHONE_FOR = {"not_interested": "CORRECT", "wrong_number": "WRONG", "handoff": "CORRECT",
              "needs_human_reply": "CORRECT", "answered": "CORRECT"}


def resync_sift(commit: bool = False, since: str = "") -> list[dict]:
    """Make each replied contact's OWN Sift record carry its disposition.

    For replies handled before the agent targeted the record behind the
    contact, or before "not interested" set the Sift status. Writes only what
    differs, never turns a DNC phone back to CORRECT, and leaves a worked
    record's status alone. Dry by default.
    """
    latest: dict[str, dict] = {}
    for row in store._conn().execute(
            "SELECT payload, outcome FROM events WHERE source=? AND outcome IS NOT NULL"
            " AND received_at>=? ORDER BY id", (SOURCE, since)).fetchall():
        payload, outcome = _logged(row)
        if outcome.get("phone"):
            outcome["_body"] = ((payload.get("data") or {}).get("content") or "").strip()
            latest[outcome["phone"]] = outcome
    out = []
    for phone, o in latest.items():
        record = _p1_record(o.get("contact_id") or "")
        item = {"phone": phone, "action": o.get("action"), "record": record[:8], "changes": []}
        out.append(item)
        if not record:
            item["changes"].append("no Priority 1 record on the contact")
            continue
        body, action = o["_body"], o.get("action")
        ruled = classify.classify_rules(body)
        if (ruled and ruled.intent == "OPT_OUT") or action == "opted_out":
            want = crm.dnc_status(record, phone)
        elif action == "classified" and sc_replies.owner_confirmed(body):
            want = "CORRECT"
        else:
            want = _PHONE_FOR.get(action or "", "")
        _, obj = crm.find_phone_object(record, phone)
        have = ((obj or {}).get("status") or "").upper()
        if want and obj is None:
            item["changes"].append("phone not on the Priority 1 record")
        elif want and have != want and not ("DNC" in have and "DNC" not in want):
            item["changes"].append(f"phone {have or 'UNKNOWN'} -> {want}")
            if commit:
                crm.set_phone_status(record, phone, want)
        if "DNC" in want:
            rec = crm.get_record(record, fresh=True) or {}
            lists = [x.get("title") if isinstance(x, dict) else str(x) for x in rec.get("lists") or []]
            if config.LIST_DNC not in lists:
                item["changes"].append(f"list {config.LIST_DNC}")
                if commit:
                    crm.add_list(record, config.LIST_DNC)
                    crm.add_tags(record, [config.TAG_OPT_OUT])
        if action == "not_interested":
            current = _status_of(record)
            if current != config.SC_NI_STATUS:
                if current in config.SC_NI_OVERWRITE:
                    item["changes"].append(f"status {current or 'none'} -> {config.SC_NI_STATUS}")
                    if commit:
                        crm.set_status(record, config.SC_NI_STATUS)
                else:
                    item["changes"].append(f"status left as {current} (worked record)")
    return out
