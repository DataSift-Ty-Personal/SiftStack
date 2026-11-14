"""A no stops what is already in motion (build plan 0.17).

A suppression has to take effect at once, not at the next load. Before this, when somebody
texted STOP the app suppressed THAT number and cancelled what was queued to THAT number. But a
record carries several numbers, and the seeder maps them all, because on a measured campaign five
of nine replies came from a different number than the one that was texted. So the owner who said
stop from their cell still had tomorrow's touch queued to their landline, and the owner who said
"yes, call me" still had touch 2 on its way to their second line while a person was dialing them.

Two moments, two functions:

  on_event()    fired by the handler the instant an owner opts out, says no, raises a hand or a
                death is reported. It finds every other number of that owner in this app's own
                store (no network: a webhook handler must never block on a CRM round trip) and
                pulls what is queued or held to them, here and in Smarter Contact's campaigns.

  send_block()  the last look, right before a text leaves. It asks the same question again from
                what the store says NOW, because a number can be mapped to the owner after the
                stop, and a batch can be staged after it. In `full` mode it also reads the record
                as DataSift holds it at that moment and runs the gate, which is the only way this
                app learns the owner was marked deceased, became a lead or sold the house, since
                none of those is a text.

The check is by OWNER, not by phone. Siblings are found two ways: every number mapped to the same
record, and every number mapped to the same owner id (an owner with a rental and a home has two
records, and their numbers may have been mapped under either).

A wrong number is the one event that stays on the phone. The stranger who answered is not the
owner, so the owner's other numbers are untouched.

Both functions do nothing unless their switch is on (SMS_AGENT_OWNER_REVOKE, SMS_AGENT_SEND_RECHECK).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import config, store

log = logging.getLogger(__name__)

# The reason written on the owner's OTHER numbers. Its own word on purpose: the digest counts
# opt-outs by the reason "opt_out", and one person saying stop is one opt-out, not four.
OWNER_STOP = "owner stopped"

# Every text this module takes out of the queue says so the same way, so the digest can count
# them: "pulled: the owner stopped", "pulled: tagged Do Not Market".
PULLED = "pulled: "

# A suppression reason that begins with one of these is a person (or the law) saying stop, and
# holds for every number of that owner. "wrong_number" and "dead" are not here: they are facts
# about one line.
STOP_PREFIXES = tuple(x for x in (
    "opt_out", "asked to be left alone", "sensitive", config.LITIGATOR_SUPPRESSION_REASON,
    "smrtphone do-not-text list", OWNER_STOP) if x)     # never an empty prefix: it would match every reason

# A sibling paused for one of these means a person is on this owner right now: a handoff, a
# takeover by text or by call, a person typing in the Smarter Contact inbox, a sensitive reply,
# a crisis, a relative. A thread the agent paused on its own (the turn cap, a question asked
# twice) is about that one conversation and does not hold the owner's other numbers.
PERSON_PREFIXES = ("handed to", "human took over", "human called", "a person replied", "sensitive", "crisis",
                   "a relative", "model requested handoff", "owner's other number")

KINDS = ("stop", "no", "handoff", "pause")


def _last4(phone: str) -> str:
    return store.clean_phone(phone)[-4:]


def siblings(phone: str, record_uuids=()) -> list[str]:
    """Every other number this app knows for the same owner. Local only, no network."""
    p = store.clean_phone(phone)
    if not p:
        return []
    mapped = store.lookup_phone(p) or {}
    conv = store.get_conversation(p) or {}
    records = {u for u in list(record_uuids or []) + [mapped.get("record_uuid"), conv.get("record_uuid")] if u}
    owners = {mapped.get("owner_uuid")} - {None, ""}
    found: set[str] = set()
    for u in records:
        found |= set(store.phones_for_record(u))
    for sib in list(found):
        owner = (store.lookup_phone(sib) or {}).get("owner_uuid")
        if owner:
            owners.add(owner)
    for owner in owners:
        found |= set(store.phones_for_owner(owner))
    found.discard(p)
    return sorted(x for x in found if x)


def on_event(kind: str, phone: str, record_uuid: str = "", matches=None, why: str = "") -> list[str]:
    """Pull what is in motion for this owner's OTHER numbers. Returns what it did, in words.

    kind   stop     the owner told us to stop (an opt-out, a legal threat, "leave us alone")
           no       the owner said not interested, or not now
           handoff  the owner raised a hand and a person has it
           pause    something a person must look at first (a reported death, a crisis)
    """
    if not config.OWNER_REVOKE:
        return []
    if kind not in KINDS:
        raise ValueError("unknown revocation kind %r (kinds: %s)" % (kind, ", ".join(KINDS)))
    if kind == "no":
        # A buyer who passes on one deal stays on the buyer list, on every number. Closing their
        # other lines would drop them from the next blast, which is the opposite of the rule.
        from . import sender_pool

        if sender_pool.program_for((store.get_conversation(phone) or {}).get("from_number") or "") == "buyer":
            return []
    records = [record_uuid] + [m.get("record_uuid") for m in (matches or []) if isinstance(m, dict)]
    sibs = siblings(phone, records)
    if not sibs:
        return []
    tail = _last4(phone)
    cancelled = sc_cancelled = changed = 0
    for sib in sibs:
        conv = store.ensure_conversation(sib)
        state = conv.get("state") or "active"
        if kind == "stop":
            if not store.is_suppressed(sib):
                store.suppress(sib, OWNER_STOP)
            if state != "opted_out":
                store.update_conversation(sib, state="opted_out",
                                          paused_reason="owner stopped on the number ending %s" % tail)
                changed += 1
        elif kind == "no":
            if state in ("active", "paused"):
                store.update_conversation(sib, state="closed", paused_reason="owner said no on another number")
                changed += 1
        elif kind == "handoff":
            if state == "active":
                store.pause_conversation(sib, "handed to %s (same owner as the number ending %s)" % (
                    config.HANDOFF_NAME, tail))
                changed += 1
        elif state == "active":
            store.pause_conversation(sib, "owner's other number: %s" % (why or "a person is looking at a reply"))
            changed += 1
        label = {"stop": "the owner stopped", "no": "the owner said no", "handoff": "a person has this owner",
                 "pause": "a person is looking at this owner"}[kind]
        cancelled += store.cancel_queued(sib, PULLED + label)
        sc_cancelled += store.sc_cancel(sib, PULLED + label)
    acts = ["owner-wide: %d other number(s) of this owner %s; cancelled %d queued text(s)%s" % (
        len(sibs), {"stop": "stopped", "no": "closed", "handoff": "paused", "pause": "paused"}[kind], cancelled,
        (" and %d Smarter Contact reply(ies)" % sc_cancelled) if sc_cancelled else "")]
    acts += _smarter_contact(sibs, kind)
    log.info("revocation (%s) from %s: %s sibling(s), %s cancelled, %s state change(s)",
             kind, tail, len(sibs), cancelled, changed)
    return acts


def _smarter_contact(sibs: list[str], kind: str) -> list[str]:
    """Take the owner's other numbers out of Smarter Contact's campaigns.

    Smarter Contact fixes a campaign's recipients when it is built, so the owner's other
    contacts would still get the next touch. A stop puts each on Smarter Contact's own
    do-not-contact list (queued, so the drain sends it); anything else only pulls them out of
    campaigns that have not finished, because a new lead is still texted one to one.
    """
    from . import sc_client

    if not sc_client.configured():
        return []
    from . import sc_inbound

    pulled = dnc = 0
    try:
        for sib in sibs:
            contact_id = sc_client.contact_id_for(sib)
            if not contact_id:
                continue
            if kind == "stop":
                store.sc_queue(sib, contact_id, "dnc", "DNC", "OWNER_STOP")
                dnc += 1
            if sc_inbound._leave_followups(contact_id):
                pulled += 1
    except sc_client.SCError as exc:
        log.warning("owner-wide Smarter Contact pull failed: %s", exc)
        return ["Smarter Contact NOT checked for this owner's other numbers: %s" % str(exc)[:100]]
    if not (pulled or dnc):
        return []
    return ["Smarter Contact: %d other contact(s) of this owner pulled from pending campaigns%s" % (
        pulled, (", %d queued for its do-not-contact list" % dnc) if dnc else "")]


def owner_stopped(phone: str, program: str = "seller", intent: str = "") -> str:
    """Why a text to this number must not leave because of what happened on ANOTHER of the
    owner's numbers. "" when nothing did. Local only.

    A stop holds everyone. A no and a live handoff hold a seller's other numbers; they do not
    hold a buyer's, because a buyer who passed on the last deal, or whom a person took over on
    it, is exactly who the next deal goes to.
    """
    buyer = program in ("buyer", "dispo")
    for sib in siblings(phone):
        supp = store.is_suppressed(sib) or ""
        if supp.startswith(STOP_PREFIXES):
            return "the owner stopped on another number (%s)" % supp.split(":")[0]
        conv = store.get_conversation(sib) or {}
        state, reason = conv.get("state") or "", str(conv.get("paused_reason") or "")
        if state == "opted_out":
            return "the owner opted out on another number"
        if buyer:
            continue
        if state == "closed" and ("soft no" in reason or "hard no" in reason or reason.startswith("owner said no")):
            return "the owner said no on another number"
        if state == "paused" and reason.startswith(PERSON_PREFIXES):
            if intent == "SPEED_TO_LEAD" and reason.startswith("handed to"):
                continue        # the handoff is what this text is about
            return "a person is working this owner on another number"
    return ""


def send_block(row: dict, conv: dict | None = None, from_number: str = "") -> tuple[str, str]:
    """The last look before one queued text leaves. ("", "") to send, ("cancel", why) to drop it,
    ("hold", why) to try again on a later pass.

    A record that cannot be read is a hold, never a send: no answer from DataSift is not a yes.
    """
    mode = config.SEND_RECHECK
    if mode == "off":
        return "", ""
    intent = str(row.get("intent") or "")
    if intent == "canary":
        return "", ""           # our own handset: there is no owner to ask about
    if intent == "WRONG_NUMBER":
        # The one apology to a stranger. Nothing about the owner or the owner's record describes
        # the person who gets it, and the sender already applies the only rule that does (the
        # number's own suppression, which the apology is the one exception to).
        return "", ""
    from . import gatekeeper, ratebudget, sender_pool

    phone = row["phone"]
    program = sender_pool.program_for(from_number or row.get("from_number") or "")
    why = owner_stopped(phone, program, intent)
    if why:
        return "cancel", why
    outreach = intent.startswith("seed_touch")
    # A text WE start: a campaign touch, or the call-is-coming text. The rules about one phone
    # (the do-not-call registry flag, a phone status of wrong or do-not-call) apply to these.
    # An answer to a question somebody just texted us is not a solicitation, and is judged on
    # the record alone.
    ours = outreach or intent == "SPEED_TO_LEAD"
    if ours and gatekeeper.is_flagged(phone):
        # Decision D32: a number carrying the registry flag is never texted.
        return "cancel", "this number carries the do-not-call registry flag"
    if mode != "full":
        return "", ""

    mapped = store.lookup_phone(phone) or {}
    record_uuid = (conv or {}).get("record_uuid") or mapped.get("record_uuid") or ""
    if not record_uuid:
        return "", ""           # nothing to read: an unmapped number has no record to be stopped on
    allow = list((mapped.get("context") or {}).get("gate_allow") or [])
    with ratebudget.nowait():
        rec, custom = gatekeeper.read(record_uuid, fresh=outreach)
    if not rec:
        return "hold", "the record could not be re-read; nothing leaves on no answer"
    _remember_owner(rec, record_uuid)
    # Outreach meets the whole gate again. Everything else (an answer, the call-is-coming text)
    # was owed to somebody who wrote to us, so only the hard rules can still stop it: Do Not
    # Market, a litigator, an opt-out, a death, a sold house. A hand raise from an owner whose
    # record reads Not Interested is still a hand raise.
    found = gatekeeper.reasons(rec, phone=phone if ours else "", allow=allow, program=program,
                               hard_only=not outreach, custom=custom)
    if found:
        return "cancel", found[0]
    return "", ""


def _remember_owner(rec: dict, record_uuid: str) -> None:
    """Now that the record is in hand, map its numbers to their owner, so the next stop on any
    of them finds the rest without a network call."""
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    owner_uuid = owner.get("uuid") or ""
    if not owner_uuid:
        return
    for p in owner.get("phones") or []:
        number = store.clean_phone(p.get("number") if isinstance(p, dict) else p)
        if len(number) != 10:
            continue
        # Only the owner id is added to a number that is already mapped. Which RECORD a number
        # routes its replies to was decided when it was texted, and must not move because some
        # other house of the same owner happened to be read.
        known = (store.lookup_phone(number) or {}).get("record_uuid")
        store.map_phone(number, record_uuid="" if known else record_uuid, owner_uuid=owner_uuid)


def hold_until(minutes: int = 5) -> str:
    """When a held row is next worth trying."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
