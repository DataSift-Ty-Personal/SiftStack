"""The SiftStack dialer (src/dialer), seen from the texting app.

The dialer is a Twilio softphone in its own app. Smarter Contact lives here, so three things cross between
the two, each behind a bearer token in a header (SMS_AGENT_DIALER_TOKEN for what the dialer sends in, the
dialer's own token for what this app pushes out):

  * GET /hooks/dialer/thread?phone=   the thread with a number, for the caller's screen.
  * POST /hooks/dialer/call           a call's outcome, kept in the event log like every webhook and
    handled by the worker (engine.process, source "dialer").
  * a hand raise this app posts to Slack is also pushed to the dialer, so it waits at the top of every
    caller's phone page with a call button (push_handraise).

WHAT A CALL CHANGES HERE, and why each piece:
  * A conversation (answered and at least SMS_AGENT_CALL_TAKEOVER_SECONDS long, or any outcome that means
    somebody talked) is a person taking the thread, the same as call_takeover.py infers from smrtPhone's
    log: the AI thread pauses, queued texts are cancelled, the other lines on the record pause.
  * The outcome is one of the texting table's own kinds (dispositions.py), and only THIS app's columns of
    that row are applied: the local do-not-text reason, the thread's state, the cancelled queue, the Smarter
    Contact label and its do-not-contact. The DataSift columns (phone status, record status, tags, lists)
    were written by the dialer, once; writing them twice from two apps is how two copies drift.
  * Never a text back. The table's wrong-number apology is for a texted wrong number; a person on the
    phone already said what they said.
"""
from __future__ import annotations

import hashlib
import logging
import threading

from . import config, store

log = logging.getLogger(__name__)

TALK_KINDS = {"plain_no", "not_now", "hand_raise", "stop_all", "listed", "relative", "bereavement"}


def thread_for(phone: str) -> dict:
    p = store.clean_phone(phone)
    conv = store.get_conversation(p) or {}
    return {
        "conversation": {"state": conv.get("state") or "", "record_uuid": conv.get("record_uuid") or "",
                         "paused_reason": conv.get("paused_reason") or "", "last_inbound": conv.get("last_inbound") or "",
                         "last_outbound": conv.get("last_outbound") or ""} if conv else {},
        "suppressed": store.is_suppressed(p) or "",
        "messages": store.thread(p, 30),
    }


def _takeover(phone: str, record_uuid: str, reason: str) -> list[str]:
    from . import crm, engine
    actions = []
    conv = store.get_conversation(phone)
    mapped = store.lookup_phone(phone)
    if not conv and not mapped:
        return actions
    if (conv or {}).get("state") not in (None, "", "active"):
        return actions
    store.ensure_conversation(phone)
    store.pause_conversation(phone, reason)
    n = store.cancel_queued(phone, "a person called them")
    m = store.sc_cancel(phone, "a person called them")
    actions.append("thread paused (%s); %d queued text(s) and %d Smarter Contact action(s) cancelled" % (reason, n, m))
    record = (conv or {}).get("record_uuid") or (mapped or {}).get("record_uuid") or record_uuid
    try:
        sib = engine._pause_siblings(phone, record, reason)
        if sib:
            actions.append("%d other line(s) on the record paused" % len(sib))
    except Exception as exc:  # noqa: BLE001 - the main thread is paused either way
        log.warning("pausing sibling lines failed: %s", exc)
    if record and config.PHASE >= 2:
        crm.add_tags(record, [config.TAG_AI_PAUSED])
    return actions


def _kind(phone: str, kind: str, call_id: str) -> list[str]:
    from . import dispositions, sc_client
    row = dispositions.TABLE.get(kind)
    if row is None:
        return ["no texting kind %r" % kind] if kind else []
    actions = []
    if row.suppress:
        store.suppress(phone, row.suppress)
        actions.append("number suppressed here (%s)" % row.suppress)
    if row.thread and store.get_conversation(phone):
        store.update_conversation(phone, state=row.thread, paused_reason="dialer: " + kind)
        actions.append("thread %s" % row.thread)
    if row.revoke in ("stop", "no", "handoff", "pause") or row.suppress or row.thread:
        n = store.cancel_queued(phone, "the call said " + kind.replace("_", " "))
        m = store.sc_cancel(phone, "the call said " + kind.replace("_", " "))
        if n or m:
            actions.append("%d queued text(s) and %d Smarter Contact action(s) cancelled" % (n, m))
    title = dispositions.sc_label(kind)
    wants_sc = (title and config.SC_AUTO_LABEL) or row.sc_dnc
    if wants_sc and sc_client.configured():
        try:
            contact = sc_client.contact_id_for(phone)
        except Exception as exc:  # noqa: BLE001 - a lookup that fails retries with the event
            raise RuntimeError("Smarter Contact lookup failed: %s" % str(exc)[:120]) from exc
        if contact:
            if title and config.SC_AUTO_LABEL:
                rid = store.sc_queue(phone, contact, "label", title, "DIALER:" + kind)
                actions.append("Smarter Contact label %r queued (#%s)" % (title, rid))
            if row.sc_dnc:
                rid = store.sc_queue(phone, contact, "dnc", "DNC", "DIALER:" + kind)
                actions.append("Smarter Contact do-not-contact queued (#%s)" % rid)
        else:
            actions.append("no Smarter Contact contact with this number")
    return actions


def process(payload: dict) -> dict:
    """One call from the dialer (the worker runs it; an exception leaves the event to be retried)."""
    phone = store.clean_phone(payload.get("phone"))
    if len(phone) != 10:
        return {"action": "ignored", "reason": "no phone"}
    kind = str(payload.get("sms_kind") or "")
    seconds = int(payload.get("seconds") or 0)
    talked = bool(payload.get("answered")) and (seconds >= config.CALL_TAKEOVER_MIN_SECONDS or kind in TALK_KINDS)
    actions = []
    if talked:
        caller = str(payload.get("caller") or "a caller")[:40]
        actions += _takeover(phone, str(payload.get("record_uuid") or ""), "human called (%s, %ss, dialer)" % (caller, seconds))
    actions += _kind(phone, kind, str(payload.get("call_id") or ""))
    return {"action": "dialer_call", "kind": kind, "talked": talked, "actions": actions}


def push_handraise(phone: str, record_uuid: str, body: str, program: str = "") -> bool:
    """Tell the dialer about a hand raise, best effort: never raises, never blocks the post to Slack."""
    if not (config.DIALER_URL and config.DIALER_HANDRAISE_TOKEN):
        return False
    if program and program not in ("seller",):
        return False
    p = store.clean_phone(phone)
    ident = hashlib.sha256(("%s|%s" % (p, (body or "").strip())).encode("utf-8")).hexdigest()[:24]

    def go():
        import requests
        try:
            requests.post(config.DIALER_URL.rstrip("/") + "/hooks/handraise", timeout=8,
                          headers={"authorization": "Bearer " + config.DIALER_HANDRAISE_TOKEN},
                          json={"phone": p, "record_uuid": record_uuid or "", "body": (body or "")[:600],
                                "program": program or "seller", "id": ident})
        except Exception as exc:  # noqa: BLE001
            log.info("the dialer did not take the hand raise: %s", str(exc)[:120])
    threading.Thread(target=go, daemon=True, name="dialer-handraise").start()
    return True
