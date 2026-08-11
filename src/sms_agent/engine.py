"""The brain: turn a webhook into CRM writes, an escalation, and maybe a reply.

Autonomy ladder (SMS_AGENT_PHASE):
  1  classify inbound and write phone status / opt-outs. No replies, no Slack.
  2  + escalate positive replies to the prospector channel and flip the CRM.
  3  + draft replies, held for approval in Slack. Still nothing auto-sent.
  4  + auto-send the highest-confidence intents only; everything else drafts.

The ladder exists because the phone number is the asset. Phase 2 already gives
a working handoff with zero AI-authored text going out.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import classify, config, crm, escalate, respond, sender_pool, smrtphone, store
from .knowledge import touches

log = logging.getLogger(__name__)

# Only these may ever be auto-sent, and only above the confidence floor.
# Everything else drafts for a human regardless of how sure the model is.
AUTO_SEND_INTENTS = {"ASKING_WHO", "NOT_INTERESTED"}

# Intents that end the thread. No reply, no further outbound, ever.
TERMINAL_INTENTS = {"OPT_OUT", "WRONG_NUMBER"}

# A real person asked something and is waiting. Not a hot lead, but it cannot
# be left in silence while the agent is below the phase that can answer.
NEEDS_HUMAN_REPLY_INTENTS = {"ASKING_WHO"}


def _all_records_for(phone: str, record_uuid: str, matches: Optional[list]) -> list[str]:
    """Every record uuid carrying this phone. Falls back to a CRM lookup."""
    uuids = [m["record_uuid"] for m in (matches or []) if m.get("record_uuid")]
    if not uuids:
        uuids = [m["record_uuid"] for m in crm.find_records_by_phone(phone)]
    if record_uuid and record_uuid not in uuids:
        uuids.insert(0, record_uuid)
    return uuids


def _suppress_unknown(phone: str, intent: str) -> list[str]:
    """Honour a terminal intent from a number we cannot attach to any record.

    We cannot write a phone status without a record, but we can guarantee we
    never text them again, and get them onto smrtPhone's do-not-text list so no
    other tool does either.
    """
    reason = "opt_out" if intent == "OPT_OUT" else "wrong_number"
    store.suppress(phone, reason)
    store.update_conversation(
        phone, state="opted_out" if intent == "OPT_OUT" else "closed", paused_reason=reason
    )
    cancelled = store.cancel_queued(phone, reason)
    acts = [f"suppressed as {reason} (no record found; cancelled {cancelled} queued)"]
    if intent == "OPT_OUT":
        ok, detail = smrtphone.add_to_dnt(phone)
        acts.append(f"smrtPhone DNT: {'added' if ok else 'FAILED - ' + detail}")
        if not ok:
            escalate.alert(
                "Opt-out could not be written to smrtPhone DNT",
                f"{phone} is suppressed on our side but ADD IT TO THE DNT LIST MANUALLY. {detail}",
            )
    return acts


def _report(result: dict) -> str:
    """Describe a CRM write honestly.

    A dry run must never read as 'ok'. Half the failure modes in this codebase
    have been a call that returned success while changing nothing.
    """
    if not isinstance(result, dict):
        return "ok"
    if result.get("error"):
        return f"FAILED - {result['error']}"
    if result.get("_dry_run"):
        return "dry run (nothing written)"
    if result.get("skipped"):
        return f"skipped: {result['skipped']}"
    return "ok"


# ---------------------------------------------------------------- inbound

def handle_inbound(payload: dict) -> dict:
    """smsIncoming. The main path."""
    phone = store.clean_phone(payload.get("from"))
    to_number = payload.get("to") or ""
    body = (payload.get("message") or "").strip()
    sms_id = str(payload.get("smsId") or "")

    if not phone:
        return {"action": "ignored", "reason": "no from number"}

    conv = store.ensure_conversation(phone, from_number=to_number)
    store.add_message(phone, "in", body, from_number=to_number, sms_id=sms_id, author="owner")

    mapped = store.lookup_phone(phone) or {}
    record_uuid = mapped.get("record_uuid") or conv.get("record_uuid") or ""
    if record_uuid and not conv.get("record_uuid"):
        store.update_conversation(phone, record_uuid=record_uuid)

    thread = store.thread(phone)
    result = classify.classify(body, thread[:-1])
    log.info("inbound %s [%s %.2f via %s] %s", phone, result.intent, result.confidence,
             result.source, body[:80])
    store.stamp_intent(phone, sms_id, result.intent, result.confidence)

    outcome = {
        "phone": phone,
        "record_uuid": record_uuid,
        "classification": result.to_dict(),
        "actions": [],
    }

    # Not in the local map: ask the CRM. A reply routinely arrives from a line
    # we never texted, and giving up here meant real wrong-numbers and opt-outs
    # went undispositioned while filling Slack with noise.
    matches: list[dict] = []
    if not record_uuid:
        matches = crm.find_records_by_phone(phone)
        if matches:
            record_uuid = matches[0]["record_uuid"]
            store.update_conversation(phone, record_uuid=record_uuid)
            store.map_phone(phone, record_uuid=record_uuid)
            outcome["record_uuid"] = record_uuid
            outcome["actions"].append(
                f"resolved from the CRM by phone ({len(matches)} record(s) carry this number)"
            )

    if not record_uuid:
        # Genuinely unknown. Terminal intents still get honoured, because a
        # person who says "leave me alone" must never be texted again whether
        # or not we can name them.
        if result.intent in TERMINAL_INTENTS:
            outcome["actions"] += _suppress_unknown(phone, result.intent)
            outcome["action"] = result.intent.lower()
            return outcome
        # Everything else is logged, not escalated. Slack is for handing a
        # conversation to a person, not a feed of every stray inbound.
        log.info("unmapped inbound from %s: %s", phone, body[:120])
        outcome["action"] = "unmapped"
        return outcome

    context = mapped.get("context") or {}
    if not context:
        context = crm.deal_context(record_uuid)
        if context:
            store.map_phone(phone, record_uuid=record_uuid, context=context)

    # ---- terminal intents: write, suppress, stop talking ----
    if result.intent == "OPT_OUT":
        outcome["actions"] += _do_opt_out(phone, record_uuid, matches, body)
        outcome["action"] = "opted_out"
        return outcome

    if result.intent == "WRONG_NUMBER":
        outcome["actions"] += _do_wrong_number(phone, record_uuid, body, context, matches)
        outcome["action"] = "wrong_number"
        return outcome

    if result.intent == "ESCALATE":
        store.pause_conversation(phone, f"sensitive: {result.rationale}")
        store.cancel_queued(phone, "sensitive content")
        if config.PHASE >= 2:
            crm.add_tags(record_uuid, [config.TAG_AI_PAUSED, config.TAG_ESCALATED])
            if "ESCALATE" in config.ESCALATE_INTENTS:
                escalate.alert(
                    "Sensitive SMS reply - needs a person now",
                    f"> {body[:400]}\n_{result.rationale}_",
                    record_uuid,
                )
                outcome["actions"].append("escalated:sensitive")
            else:
                outcome["actions"].append("sensitive: tagged and paused, not posted to Slack")
        outcome["action"] = "escalated"
        return outcome

    # ---- live conversation ----
    # A positive reply always reaches the prospector (Ty, 2026-08-11): flag it,
    # never try to hold the conversation with a well-worded auto-reply.
    #
    # This used to fire only while the thread was still ours, to avoid re-paging
    # someone who had already taken it over. But that dropped exactly the wrong
    # message: a seller warms up ACROSS turns, so the clearest buying signal
    # usually lands after a human has already answered once. queue_escalation
    # posts once per conversation, so the original spam concern is handled there
    # rather than by staying silent.
    if result.intent == "INTERESTED":
        outcome["actions"] += _do_hot_lead(phone, record_uuid, body, result, context, thread)

    # Turn cap: a thread that has gone six rounds without a handoff is not
    # going to be rescued by a seventh.
    if int(conv.get("ai_turns") or 0) >= config.MAX_AI_TURNS:
        store.pause_conversation(phone, "turn cap reached")
        if config.PHASE >= 2:
            escalate.alert(
                f"SMS thread hit the {config.MAX_AI_TURNS}-turn cap",
                f"> {body[:300]}\n_Handing to a human._",
                record_uuid,
            )
            crm.add_tags(record_uuid, [config.TAG_AI_PAUSED])
        outcome["actions"].append("paused:turn_cap")
        outcome["action"] = "turn_cap"
        return outcome

    if conv.get("state") != "active":
        outcome["action"] = "conversation_" + str(conv.get("state"))
        outcome["actions"].append(f"no reply: conversation is {conv.get('state')}")
        return outcome

    if result.intent == "INTERESTED":
        # Handed to a person. The agent does not keep talking to a live lead,
        # at any phase: a hold-text from a bot is worse than a fast human call.
        store.pause_conversation(phone, f"handed to {config.HANDOFF_NAME}")
        outcome["actions"].append(f"paused: handed to {config.HANDOFF_NAME}, no reply drafted")
        outcome["action"] = "handoff"
        return outcome

    if config.PHASE < 3:
        # Below phase 3 the agent writes nothing back, so a live person who
        # asked us a direct question gets silence unless someone is told.
        # "How can I help you" sat active and unanswered for half an hour
        # (7038897165, 2026-08-11): the worst outcome available, since they
        # replied and we simply did not answer.
        #
        # This is deliberately NOT the hot-lead path. It makes no claim about
        # selling, writes no escalation tags, and says plainly what it is, so
        # the channel keeps its meaning: a handoff post means a live seller.
        if result.intent == "ASKING_WHO" and config.ANSWER_WHO:
            outcome["actions"] += _do_answer_who(phone, record_uuid, context)
            outcome["action"] = "answered_who"
            return outcome
        if result.intent in NEEDS_HUMAN_REPLY_INTENTS:
            outcome["actions"] += _do_needs_answer(phone, record_uuid, body, result)
            outcome["action"] = "needs_human_reply"
            return outcome
        outcome["action"] = "classified"
        return outcome

    outcome["actions"] += _do_reply(phone, record_uuid, body, result, context)
    outcome["action"] = "replied"
    return outcome


def _do_opt_out(phone: str, record_uuid: str, matches: Optional[list] = None,
                body: str = "") -> list[str]:
    """Honor an opt-out completely: local, remote, CRM, and anything queued.

    An angry reply is often BOTH. "I don't own it ... leave me alone" is a
    wrong number and a do-not-contact in one sentence. Opt-out wins for
    suppression, and the phone is ALSO marked WRONG so the data is clean and no
    caller dials it either.
    """
    acts = []
    store.suppress(phone, "opt_out")
    store.update_conversation(phone, state="opted_out", paused_reason="opt-out")
    cancelled = store.cancel_queued(phone, "opt-out")
    acts.append(f"suppressed locally (cancelled {cancelled} queued)")

    ok, detail = smrtphone.add_to_dnt(phone)
    acts.append(f"smrtPhone DNT: {'added' if ok else 'FAILED - ' + detail}")

    # Tag every record carrying this number, not just the one we texted. Skip
    # trace attaches one line to several owners; leaving the others untagged
    # means the number gets dialled again tomorrow by someone else.
    targets = _all_records_for(phone, record_uuid, matches)
    for uuid in targets:
        acts.append(
            f"CRM tag {config.TAG_OPT_OUT} on {uuid[:8]}: "
            f"{_report(crm.add_tags(uuid, [config.TAG_OPT_OUT]))}"
        )
        crm.post_note(uuid, f"SMS opt-out received from {phone}. Suppressed from all texting.")
    if len(targets) > 1:
        acts.append(f"applied across {len(targets)} records sharing this number")

    # The opt-out goes onto the PHONE in Sift, which is the durable fix (Ty,
    # 2026-08-11). smrtPhone's DNT list has no documented write route and every
    # attempt 404s, so suppressing only in our database left the number textable
    # from any other tool. Sift is where every campaign is built, so a phone
    # dispositioned DNC there is excluded at the source, for us and for callers,
    # forever, without depending on a vendor endpoint we cannot reach.
    wrong = bool(body and classify.classify_rules_wrong_number(body))
    for uuid in targets:
        status = crm.dnc_status(uuid, phone, wrong_number=wrong)
        acts.append(
            f"phone -> {status} on {uuid[:8]}: {_report(crm.set_phone_status(uuid, phone, status))}"
        )

    if not ok and config.PHASE >= 2:
        # Sift now carries the suppression, so this is a note rather than the
        # emergency it used to be. It still gets said out loud, because a number
        # left off smrtPhone's DNT can still be texted by hand from its inbox.
        escalate.alert(
            "Opt-out recorded in Sift, not on smrtPhone's DNT",
            f"{phone} is suppressed here and dispositioned DNC in Sift, so no campaign "
            f"will text it again. smrtPhone's own DNT list could not be written "
            f"(no API route), so add it there by hand if you want its inbox blocked too.\n{detail}",
            record_uuid,
        )
    return acts


def _do_wrong_number(phone: str, record_uuid: str, body: str, context: dict,
                     matches: Optional[list] = None) -> list[str]:
    acts = []
    # A wrong number is wrong on every record it appears on.
    for uuid in _all_records_for(phone, record_uuid, matches):
        res = crm.set_phone_status(uuid, phone, "WRONG")
        acts.append(f"phone status WRONG on {uuid[:8]}: {_report(res)}")
        crm.post_note(uuid, f"{phone} replied wrong number: \"{body[:180]}\". Phone set to WRONG.")
    store.suppress(phone, "wrong_number")
    store.update_conversation(phone, state="closed", paused_reason="wrong number")
    store.cancel_queued(phone, "wrong number")
    acts.append("suppressed + queued cancelled")
    return acts


def _do_hot_lead(
    phone: str, record_uuid: str, body: str, result, context: dict, thread: list[dict]
) -> list[str]:
    """A positive reply is a handoff, not a conversation to continue.

    The CRM write happens immediately so the lead exists in her queue whether or
    not Slack is watched. The NOTIFICATION is debounced: a seller routinely
    sends one thought across two or three texts, and posting each separately
    buries the lead under repeats of its own transcript.
    """
    acts = []
    if config.PHASE < 2:
        return ["hot lead detected (phase 1: no escalation)"]

    # We post to Slack directly rather than relying on a CRM sequence to notice,
    # because the agent deliberately does NOT move the lead's status (below).
    if "INTERESTED" in config.ESCALATE_INTENTS:
        if store.escalation_expired(phone, config.ESCALATION_QUIET_DAYS):
            store.clear_escalation(phone)
        state = store.queue_escalation(
            phone, record_uuid, result.intent, config.ESCALATION_DEBOUNCE_SECONDS
        )
        acts.append({
            "new": f"handoff queued, posting in ~{config.ESCALATION_DEBOUNCE_SECONDS}s",
            "extended": "burst still arriving; notification held open",
            "already_posted": "already handed off; no repeat post",
        }[state])
    else:
        acts.append("escalation disabled by SMS_AGENT_ESCALATE_INTENTS")

    # The agent does NOT touch lead status (Ty, 2026-08-11). A text saying "I'll
    # sell, or keep" is interest, not a qualified lead, and only the person who
    # makes the call can decide that. Moving it also had a mechanical cost: the
    # status change fired a DataSift sequence that reassigned the record away
    # from the prospector we had just paged, and it dropped the record out of
    # every Hottest preset (all of which exclude new_lead), so the hottest leads
    # were the ones that silently vanished from the cadence. The tag and the
    # assignment carry the handoff instead; Adriana advances the status herself.
    crm.add_tags(record_uuid, [config.TAG_ESCALATED, config.TAG_AI_HANDLED])
    if config.HANDOFF_ASSIGNEE_UUID:
        acts.append(
            f"assigned to {config.HANDOFF_NAME}: "
            f"{_report(crm.assign(record_uuid, config.HANDOFF_ASSIGNEE_UUID))}"
        )
    crm.post_note(record_uuid, f"Positive SMS reply from {phone}: \"{body[:250]}\"", pinned=True)

    # Set CORRECT: they engaged about the property, so the number is good.
    acts.append(f"phone status -> CORRECT: {_report(crm.set_phone_status(record_uuid, phone, 'CORRECT'))}")
    return acts


def _do_answer_who(phone: str, record_uuid: str, context: dict) -> list[str]:
    """Answer "who is this?" from the fixed pool.

    Deliberately a TEMPLATE, not a model draft. Identity is the one thing the
    model has already been caught inventing (it called itself "Alex" once), and
    it is also the thing with legal weight, so it is not left to a generator.
    Fixed copy means the answer is reviewable, identical every time, and cannot
    name a company by accident.

    It still goes through the outbox, so quiet hours, the sticky sender, the
    per-number cap and suppression all apply exactly as they do to outreach.
    """
    acts = []
    first = touches.clean_first(context.get("owner_first") or "")
    if touches.is_entity(context.get("owner_full") or ""):
        first = ""
    place = context.get("county") or ""
    place = f"{place} County" if place and "county" not in place.lower() else (
        place or context.get("city") or ""
    )
    sender = context.get("assigned_name") or config.HANDOFF_NAME
    conv = store.get_conversation(phone) or {}

    message = touches.render_who(
        seed=f"{record_uuid}|{phone}",
        first=first,
        addr=context.get("street") or "your place",
        place=place,
        sender=sender,
    )
    ok, problems = respond.validate(message, max_questions=1)
    if not ok:
        # Never send unvalidated copy. Fall back to telling a human.
        log.warning("who-reply failed validation (%s); handing to a person", problems)
        return _do_needs_answer(phone, record_uuid, "(asked who we are)", None)

    row_id = store.queue_message(
        phone, message, from_number=conv.get("from_number") or "",
        intent="ASKING_WHO", confidence=1.0,
    )
    store.bump_ai_turns(phone)
    acts.append(f"queued who-reply #{row_id}")
    if record_uuid:
        acts.append(
            f"phone status -> CORRECT: {_report(crm.set_phone_status(record_uuid, phone, 'CORRECT'))}"
        )
    return acts


def _do_needs_answer(phone: str, record_uuid: str, body: str, result) -> list[str]:
    """Tell a human that someone is waiting on an answer.

    Posted once per conversation, because a person asking twice is still one
    question. The phone is marked CORRECT: whoever replied is reachable, which
    is true whatever they went on to say.
    """
    acts = []
    if config.PHASE < 2:
        return ["question detected (phase 1: no notification)"]

    if store.mark_notified(phone, "needs_reply"):
        ok = escalate.alert(
            f"{config.HANDOFF_NAME}: someone replied with a question",
            f"> {body[:300]}\n\n_Live thread, no auto-reply is sent at this phase. "
            f"Answer from the smrtPhone inbox._",
            record_uuid,
        )
        acts.append("posted: awaiting a human answer" if ok else "notify FAILED")
    else:
        acts.append("already flagged as awaiting an answer")

    if record_uuid:
        acts.append(
            f"phone status -> CORRECT: {_report(crm.set_phone_status(record_uuid, phone, 'CORRECT'))}"
        )
    return acts


def _do_reply(phone: str, record_uuid: str, body: str, result, context: dict) -> list[str]:
    acts = []
    thread = store.thread(phone)
    reply = respond.draft(thread, context, result.intent, result.rationale)

    if not reply.message:
        acts.append(f"no reply drafted: {reply.reason}")
        return acts

    if not reply.ok:
        # Validator blocked it. That is a signal about the conversation, not
        # just about the draft, so a human sees it either way.
        escalate.draft_for_approval(
            phone, body, reply.message, reply.confidence, reply.reason, record_uuid, reply.blocked
        )
        acts.append(f"blocked by validator ({', '.join(reply.blocked)}); sent to Slack")
        return acts

    auto = (
        config.PHASE >= 4
        and not reply.handoff
        and result.intent in AUTO_SEND_INTENTS
        and reply.confidence >= config.CONFIDENCE_FLOOR
        and result.confidence >= 0.6
    )

    from_number = sender_pool.assign(phone, (context or {}).get('assigned_name', ''))
    if not from_number:
        escalate.draft_for_approval(
            phone, body, reply.message, reply.confidence, "no sending number available", record_uuid
        )
        return ["no sending number in the pool; drafted to Slack instead"]

    not_before = ""
    if not sender_pool.within_quiet_hours(phone):
        not_before = sender_pool.next_send_window(phone).isoformat(timespec="seconds")
        acts.append(f"outside recipient quiet hours; queued for {not_before}")

    status = "queued" if auto else "held"
    row_id = store.queue_message(
        phone,
        reply.message,
        from_number=from_number,
        not_before=not_before,
        status=status,
        intent=result.intent,
        confidence=reply.confidence,
    )
    store.update_conversation(phone, from_number=from_number)

    if auto:
        store.bump_ai_turns(phone)
        acts.append(f"queued outbox #{row_id} from {from_number} (conf {reply.confidence:.2f})")
    else:
        escalate.draft_for_approval(
            phone, body, reply.message, reply.confidence, reply.reason, record_uuid
        )
        acts.append(f"held outbox #{row_id} for approval (conf {reply.confidence:.2f})")

    if reply.handoff:
        store.pause_conversation(phone, "model requested handoff")
        crm.add_tags(record_uuid, [config.TAG_AI_PAUSED])
        acts.append("paused: model requested handoff")
    elif result.intent == "NOT_INTERESTED":
        acts.append(_close_not_interested(phone, record_uuid, reply.reason))
    return acts


def _close_not_interested(phone: str, record_uuid: str, reason: str) -> str:
    """Close the thread out, recording whether it was a soft or a hard no.

    The distinction is the whole point. A hard no is done. A soft no is a lead
    with a later date on it, and the proven playbook works those again rather
    than discarding them, so the digest surfaces soft nos once they age.

    Either way the conversation closes after the single permitted follow-up:
    never rebut twice. This list is worth more than any one conversation.
    """
    hard = any(
        marker in (reason or "").lower()
        for marker in ("hard no", "firm no", "final", "adamant", "hostile", "annoyed")
    )
    label = "hard no" if hard else "soft no"
    store.update_conversation(phone, state="closed", paused_reason=label)
    crm.add_tags(record_uuid, [config.TAG_AI_HANDLED])
    crm.post_note(record_uuid, f"SMS: {label} from {phone}. {reason}"[:400])

    # A no is still a verified number. Someone who answers about their own house
    # has proved the line reaches the owner, which is worth more than the single
    # conversation it just ended: this is the number the next campaign dials.
    crm.set_phone_status(record_uuid, phone, "CORRECT")
    return f"closed as a {label}, phone marked CORRECT"


# --------------------------------------------------------------- outbound

def handle_outbound(payload: dict) -> dict:
    """smsOutgoing. Its real job is detecting that a human took over.

    The moment a person replies from the smrtPhone inbox, the agent has to go
    silent. Two of us texting the same seller is the worst failure this system
    can produce, and it is entirely preventable: an outgoing message we did not
    author means a human typed it.
    """
    phone = store.clean_phone(payload.get("to"))
    body = (payload.get("message") or "").strip()
    if not phone or not body:
        return {"action": "ignored"}

    if _authored_by_us(phone, body) or (payload.get("source") or "").lower() == "api":
        return {"action": "ours"}

    store.add_message(
        phone, "out", body, from_number=payload.get("from") or "",
        sms_id=str(payload.get("smsId") or ""), author="human",
    )
    store.ensure_conversation(phone)
    store.pause_conversation(phone, f"human took over ({payload.get('userName') or 'unknown user'})")
    cancelled = store.cancel_queued(phone, "human took over")

    conv = store.get_conversation(phone) or {}
    if conv.get("record_uuid") and config.PHASE >= 2:
        crm.add_tags(conv["record_uuid"], [config.TAG_AI_PAUSED])
    log.info("human takeover on %s; cancelled %s queued", phone, cancelled)
    return {"action": "human_takeover", "cancelled": cancelled}


def _authored_by_us(phone: str, body: str) -> bool:
    normalized = " ".join(body.lower().split())
    for m in store.thread(phone, limit=12):
        if m.get("direction") != "out":
            continue
        if " ".join(str(m.get("body", "")).lower().split()) == normalized:
            return True
    return False


# --------------------------------------------------------------- delivery

def handle_delivery(payload: dict) -> dict:
    """smsDeliveryCallback.

    New capability versus the batch classifier: a dead number never replies, so
    reply-only classification can never find one. The carrier tells us here.
    """
    status = (payload.get("status") or "").lower()
    reason = payload.get("failure_reason") or ""
    sms_id = str(payload.get("smsId") or "")
    if status in ("delivered", "sent", "queued", "accepted"):
        return {"action": "ok", "status": status}

    phone = store.clean_phone(payload.get("to"))
    if not phone:
        # The callback often carries only the smsId. Resolve through our log.
        phone = store.phone_for_sms_id(sms_id)
    if not phone:
        return {"action": "unresolved", "sms_id": sms_id}

    dead_signals = ("unknown", "no longer exist", "invalid", "not in service", "unallocated")
    if any(s in reason.lower() for s in dead_signals):
        store.suppress(phone, "dead")
        store.cancel_queued(phone, "dead number")
        conv = store.get_conversation(phone) or {}
        # A number can be dead without ever having had a conversation (the very
        # first seed bounced), so fall back to the phone map.
        record_uuid = conv.get("record_uuid") or (store.lookup_phone(phone) or {}).get("record_uuid")
        acts = ["suppressed as dead"]
        if record_uuid:
            acts.append(f"phone status DEAD: {_report(crm.set_phone_status(record_uuid, phone, 'DEAD'))}")
        else:
            acts.append("no record mapped; CRM not updated")
        return {"action": "dead", "phone": phone, "reason": reason, "actions": acts}

    return {"action": "failed", "phone": phone, "status": status, "reason": reason}


def handle_dnt(payload: dict) -> dict:
    """addNumberToDNT - somebody was added to do-not-text anywhere in smrtPhone."""
    phone = store.clean_phone(payload.get("phone"))
    if not phone:
        return {"action": "ignored"}
    store.suppress(phone, "opt_out")
    store.update_conversation(phone, state="opted_out", paused_reason="added to DNT")
    cancelled = store.cancel_queued(phone, "DNT")
    return {"action": "suppressed", "phone": phone, "cancelled": cancelled}


# --------------------------------------------------------------- DataSift

def handle_datasift(payload: dict) -> dict:
    """DataSift sequence webhook.

    The payload shape is not documented and is confirmed by Phase 0 logging.
    Until then this resolves defensively: pull whatever looks like a record
    uuid and a phone, and record the event. Nothing here writes.
    """
    record_uuid = ""
    phone = ""
    for key in ("uuid", "property_uuid", "record_uuid", "id"):
        val = payload.get(key)
        if isinstance(val, str) and len(val) >= 32:
            record_uuid = val
            break
    prop = payload.get("property") if isinstance(payload.get("property"), dict) else {}
    record_uuid = record_uuid or prop.get("uuid") or ""

    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    phones = owner.get("phones") or payload.get("phones") or []
    if isinstance(phones, list) and phones:
        first = phones[0]
        phone = store.clean_phone(first.get("number") if isinstance(first, dict) else first)

    if record_uuid and phone:
        store.map_phone(phone, record_uuid=record_uuid)
        return {"action": "mapped", "record_uuid": record_uuid, "phone": phone}
    return {"action": "logged", "record_uuid": record_uuid, "phone": phone,
            "note": "payload shape not recognized; see events table"}


# ---------------------------------------------------------------- dispatch

HANDLERS = {
    "smsIncoming": handle_inbound,
    "smsOutgoing": handle_outbound,
    "smsDeliveryCallback": handle_delivery,
    "addNumberToDNT": handle_dnt,
    "addNumberToDNC": handle_dnt,
}


def process(source: str, payload: dict) -> dict:
    if source == "datasift":
        return handle_datasift(payload)
    handler = HANDLERS.get(payload.get("event") or "")
    if not handler:
        return {"action": "ignored", "reason": f"no handler for event {payload.get('event')!r}"}
    return handler(payload)
