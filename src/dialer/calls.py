"""One call, from the check before it is dialed to the disposition after it ends.

    prepare()        the rules check and the call row. Nothing reaches Twilio unless this said yes.
    voice_twiml()    Twilio asks what the caller's browser leg should do: join this call's conference.
    dial_lead()      the homeowner is dialed into that conference (run just after the TwiML is sent).
    on_status()      the homeowner's leg: ringing, answered, and how it ended.
    on_conference()  the conference's own events (it started, someone left, it ended).
    on_amd()         what answering-machine detection heard, when it is on.
    on_recording()   the recording is ready.
    hangup(), vm_drop(), hold(), transfer(), monitor()   the caller's controls.
    dispose()        the caller says how it went; the write-back is queued from that.
    inbound*()       a homeowner calling one of our numbers.

A callback that arrives late or twice never rewinds a call: status moves forward only (prepared,
connecting, ringing, answered, ended), and every callback is kept in call_events as Twilio sent it, minus
nothing we would need and with nothing a person typed (Twilio sends none).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, dispositions, events, phone, rules, sift, store, twilio_voice

log = logging.getLogger(__name__)

PREPARED_TTL_S = 120
FINAL = ("completed", "busy", "failed", "no-answer", "canceled")
FORWARD = {"prepared": 0, "connecting": 1, "ringing": 2, "answered": 3, "ended": 4, "failed": 4, "blocked": 4}
KEEP_PARAMS = ("CallSid", "CallStatus", "CallDuration", "Duration", "SipResponseCode", "ErrorCode", "AnsweredBy",
               "ConferenceSid", "FriendlyName", "StatusCallbackEvent", "ParticipantLabel", "RecordingSid",
               "RecordingStatus", "RecordingDuration", "RecordingChannels", "DialCallStatus", "DialCallDuration",
               "DialCallSid", "MachineDetectionDuration", "Direction", "Timestamp", "SequenceNumber")


def _keep(params: dict) -> dict:
    return {k: params.get(k) for k in KEEP_PARAMS if params.get(k) not in (None, "")}


def _caller_name(identity: str) -> str:
    c = store.get_caller(identity) or {}
    return c.get("name") or identity or "a caller"


def _public(call: dict) -> dict:
    """What the phone page is told about a call."""
    import json
    detail = {}
    try:
        detail = json.loads(call.get("detail") or "{}")
    except ValueError:
        pass
    warnings = []
    try:
        warnings = json.loads(call.get("warnings") or "[]")
    except ValueError:
        pass
    return {
        "id": call["id"], "status": call["status"], "kind": call["kind"], "direction": call["direction"],
        "number": call.get("number") or "", "pretty": phone.pretty(call.get("number")),
        "from": call.get("from_number") or "", "record": detail, "record_uuid": call.get("record_uuid") or "",
        "answered_at": call.get("answered_at"), "ended_at": call.get("ended_at"), "seconds": call.get("seconds"),
        "end_status": call.get("end_status") or "", "amd": call.get("amd") or "", "disposition": call.get("disposition") or "",
        "suggest": dispositions.suggest(call.get("end_status") or "", call.get("sip_code") or "", bool(call.get("answered_at")),
                                        int(call.get("seconds") or 0), call.get("amd") or "", bool(call.get("vm_dropped"))),
        "recorded": bool(call.get("record_allowed")), "vm_dropped": bool(call.get("vm_dropped")),
        "warnings": warnings, "session_id": call.get("session_id"), "item_id": call.get("item_id"),
        "voicemail": bool(call.get("voicemail_sid")),
    }


def publish(call: dict, kind: str = "call") -> None:
    events.publish(call.get("identity") or "", kind, _public(call))


def busy_owner(owner_uuid: str, identity: str) -> str:
    """The first name of another caller on a live call with this owner, or ''."""
    if not owner_uuid:
        return ""
    for c in store.active_calls():
        if c.get("owner_uuid") == owner_uuid and c.get("identity") != identity:
            return _caller_name(c.get("identity") or "")
    return ""


def pick_from(caller: dict, number: str) -> str:
    """The number this call goes out from: the caller's own, or with local presence on, a teammate's line
    in the homeowner's area code when there is one."""
    mine = caller.get("caller_id") or ""
    if config.S.local_presence:
        ac = phone.digits(number)[:3]
        for other in store.callers():
            cid = other.get("caller_id") or ""
            if cid.startswith(ac):
                return cid
    return mine


# ---------------------------------------------------------------------- before the call
def prepare(identity: str, number: str, record_uuid: str = "", *, kind: str = "manual", session_id: int | None = None,
            item_id: int | None = None, confirm: bool = False, row: dict | None = None,
            now: datetime | None = None) -> dict:
    """Check a call and, when it may go, create its row. Returns {ok, call} or {ok: False, stop|ask}."""
    caller = store.get_caller(identity)
    if not caller or not caller.get("active"):
        return {"ok": False, "stop": ["you are not signed in as an active caller"]}
    n = phone.digits(number)
    if not caller.get("caller_id"):
        return {"ok": False, "stop": ["no outbound number is set for you yet (cli set-number)"]}
    rec, warn_lookup = None, []
    if record_uuid:
        try:
            rec = sift.client().record(record_uuid)
        except Exception as exc:  # noqa: BLE001 - the call may still go, with the caller told
            warn_lookup.append("the DataSift record could not be read (%s)" % str(exc)[:80])
    elif n:
        try:
            hits = sift.client().find_by_phone(n)
        except Exception as exc:  # noqa: BLE001
            hits = []
            warn_lookup.append("DataSift could not be searched for this number (%s)" % str(exc)[:80])
        if hits:
            rec, row = hits[0]["record"], row or hits[0]["row"]
            if len({h["record"].get("uuid") for h in hits}) > 1:
                warn_lookup.append("this number is on %d records; the first is shown" % len(hits))
    owner_uuid = ((rec or {}).get("owner") or {}).get("uuid") or ""
    chk = rules.manual(rec, n, row=row, now=now, busy_owner_by=busy_owner(owner_uuid, identity))
    ask = warn_lookup + chk.ask
    if not chk.ok:
        cid = store.new_call(identity=identity, kind=kind, number=n, record_uuid=(rec or {}).get("uuid") or record_uuid,
                             owner_uuid=owner_uuid, status="blocked", warnings=chk.stop + ask)
        store.add_event(cid, "system", "blocked", {"stop": chk.stop})
        return {"ok": False, "stop": chk.stop, "ask": ask, "call_id": cid}
    if chk.callback and kind == "manual":
        kind = "callback"
    if ask and not confirm and kind != "queue":
        return {"ok": False, "ask": ask, "need_confirm": True}
    summ = sift.summary(rec, row) if rec else {"uuid": "", "owner": "Not in DataSift", "phones": [], "link": ""}
    cid = store.new_call(identity=identity, kind=kind, session_id=session_id, item_id=item_id, number=n,
                         from_number=pick_from(caller, n), record_uuid=summ.get("uuid") or "", owner_uuid=owner_uuid,
                         status="prepared", record_allowed=1 if chk.record_ok else 0, warnings=ask + chk.notes,
                         detail=summ)
    store.add_event(cid, "caller", "prepared", {"confirmed": bool(ask), "kind": kind})
    call = store.get_call(cid)
    return {"ok": True, "call": _public(call)}


# ---------------------------------------------------------------------- Twilio asks about the browser leg
def voice_twiml(params: dict) -> tuple[str, Any]:
    """(TwiML, an optional follow-up to run after the response is sent)."""
    frm = str(params.get("From") or params.get("Caller") or "")
    identity = frm[7:] if frm.startswith("client:") else ""
    if params.get("AccountSid") and config.S.account_sid and params.get("AccountSid") != config.S.account_sid:
        return twilio_voice.twiml_say_hangup("This call is not for this account."), None
    caller = store.get_caller(identity) if identity else None
    if not caller or not caller.get("active"):
        return twilio_voice.twiml_say_hangup("This phone is not signed in."), None

    if params.get("action") == "record_vm":
        cid = store.new_call(identity=identity, kind="vm_record", status="answered", agent_sid=params.get("CallSid") or "")
        store.add_event(cid, "twilio", "vm_record_start", _keep(params))
        return twilio_voice.twiml_record_vm(identity), None

    if params.get("monitor"):
        call = store.get_call(str(params.get("monitor")))
        mode = str(params.get("mode") or "listen")
        if caller.get("role") != "manager" or not call or call.get("status") not in ("connecting", "ringing", "answered"):
            return twilio_voice.twiml_say_hangup("That call is not live, or you may not join it."), None
        if mode not in ("listen", "whisper", "barge"):
            mode = "listen"
        mid = store.new_call(identity=identity, kind="monitor", status="answered", agent_sid=params.get("CallSid") or "",
                             conference_name=call["conference_name"], record_uuid=call.get("record_uuid") or "")
        store.add_event(call["id"], "caller", "monitor", {"by": identity, "mode": mode, "leg": mid})
        events.publish(call.get("identity") or "", "monitor", {"call_id": call["id"], "by": caller.get("name"), "mode": mode})
        return twilio_voice.twiml_monitor(call, mode), None

    call = store.get_call(str(params.get("call_id") or ""))
    if not call or call.get("identity") != identity:
        return twilio_voice.twiml_say_hangup("This call was not prepared on your phone."), None
    made = store.parse(call.get("created_at"))
    if call.get("status") != "prepared" or not made or datetime.now(timezone.utc) - made > timedelta(seconds=PREPARED_TTL_S):
        return twilio_voice.twiml_say_hangup("This call expired. Please dial again."), None
    store.update_call(call["id"], agent_sid=params.get("CallSid") or "", status="connecting", started_at=store.now())
    store.add_event(call["id"], "twilio", "agent_connected", _keep(params))
    call = store.get_call(call["id"])
    publish(call)
    return twilio_voice.twiml_agent(call), (lambda: dial_lead(call["id"]))


def dial_lead(call_id: str) -> None:
    call = store.get_call(call_id)
    if not call or call.get("status") != "connecting":
        return
    try:
        res = twilio_voice.voice().dial_lead(call, phone.e164(call["from_number"]), phone.e164(call["number"]),
                                             bool(call.get("record_allowed")), config.S.amd, config.S.ring_seconds)
    except Exception as exc:  # noqa: BLE001 - the caller must hear about it, not sit in silence
        log.warning("dialing the homeowner failed on %s: %s", call_id, exc)
        store.update_call(call_id, status="failed", end_status="failed", ended_at=store.now(),
                          warnings=["Twilio refused the call: %s" % str(exc)[:160]])
        store.add_event(call_id, "system", "dial_failed", {"error": str(exc)[:300]})
        if call.get("agent_sid"):
            try:
                twilio_voice.voice().end(call["agent_sid"])
            except Exception:  # noqa: BLE001
                pass
        publish(store.get_call(call_id))
        return
    store.update_call(call_id, lead_sid=res.get("call_sid") or "", conference_sid=res.get("conference_sid") or "")
    store.add_event(call_id, "system", "lead_dialed", res)
    if call.get("item_id"):
        from . import queue
        queue.mark_dialed(call)


# ---------------------------------------------------------------------- callbacks
def _advance(call_id: str, to: str, **fields) -> bool:
    """Move a call's status forward only."""
    call = store.get_call(call_id)
    if not call:
        return False
    if FORWARD.get(to, 0) < FORWARD.get(call["status"], 0):
        if fields:
            store.update_call(call_id, **fields)
        return False
    store.update_call(call_id, status=to, **fields)
    return True


def on_status(call_id: str, leg: str, params: dict) -> None:
    call = store.get_call(call_id)
    if not call:
        return
    st = str(params.get("CallStatus") or "").lower()
    store.add_event(call_id, "twilio", "status:%s:%s" % (leg, st), _keep(params))
    if leg != "lead":
        events.publish(call.get("identity") or "", "leg", {"call_id": call_id, "leg": leg, "status": st})
        return
    if not call.get("lead_sid") and params.get("CallSid"):
        store.update_call(call_id, lead_sid=params["CallSid"])
    if st == "ringing":
        _advance(call_id, "ringing")
    elif st in ("in-progress", "answered"):
        if _advance(call_id, "answered", answered_at=call.get("answered_at") or store.now()):
            store.close_handraises(call["number"], "called", call.get("identity") or "")
    elif st in FINAL:
        secs = params.get("CallDuration") or params.get("Duration") or 0
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            secs = 0
        fields = dict(ended_at=store.now(), end_status=st, seconds=secs if call.get("answered_at") else 0,
                      sip_code=str(params.get("SipResponseCode") or ""))
        if st == "completed" and not call.get("answered_at") and secs > 0:
            fields["answered_at"] = store.now()
            fields["seconds"] = secs
        _advance(call_id, "ended", **fields)
        call = store.get_call(call_id)
        # The homeowner's leg does not end the conference by itself (a voicemail drop or a transfer needs
        # it to stay up), so the call's end is decided here: the conference ends and every leg still in it
        # (the caller, a closer, a manager) hangs up.
        _end_room(call)
        store.add_job(call_id, "auto_dispose", {}, delay_s=900)
    publish(store.get_call(call_id))


def on_conference(call_id: str, params: dict) -> None:
    call = store.get_call(call_id)
    if not call:
        return
    ev = str(params.get("StatusCallbackEvent") or "")
    store.add_event(call_id, "twilio", "conference:" + ev, _keep(params))
    if params.get("ConferenceSid") and not call.get("conference_sid"):
        store.update_call(call_id, conference_sid=params["ConferenceSid"])
    if ev == "conference-end":
        call = store.get_call(call_id)
        # The caller hung up while it was still ringing: stop the homeowner's phone too.
        if call.get("lead_sid") and call["status"] in ("connecting", "ringing"):
            try:
                twilio_voice.voice().end(call["lead_sid"])
            except Exception as exc:  # noqa: BLE001
                log.info("stopping the homeowner leg on %s: %s", call_id, exc)
        if call["status"] in ("connecting", "ringing", "answered"):
            store.add_job(call_id, "reconcile", {}, delay_s=20)
    publish(store.get_call(call_id), "conference")


def on_amd(call_id: str, params: dict) -> None:
    call = store.get_call(call_id)
    if not call:
        return
    by = str(params.get("AnsweredBy") or "unknown")
    store.update_call(call_id, amd=by)
    store.add_event(call_id, "twilio", "amd:" + by, _keep(params))
    if config.S.amd == "auto_drop" and by.startswith("machine_end"):
        vm_drop(call_id, call.get("identity") or "", auto=True)
    publish(store.get_call(call_id))


def on_recording(call_id: str, params: dict) -> None:
    call = store.get_call(call_id)
    if not call:
        return
    store.add_event(call_id, "twilio", "recording:" + str(params.get("RecordingStatus") or ""), _keep(params))
    if str(params.get("RecordingStatus") or "") == "absent":
        # No audio was captured (the line never connected long enough): there is no link to wait for.
        store.update_call(call_id, record_allowed=0)
        return
    if str(params.get("RecordingStatus") or "completed") == "completed" and params.get("RecordingSid"):
        try:
            secs = int(params.get("RecordingDuration") or 0)
        except (TypeError, ValueError):
            secs = 0
        store.update_call(call_id, recording_sid=params["RecordingSid"], recording_seconds=secs)


def _end_room(call: dict) -> None:
    """End the call's conference (and with it every leg in it); the caller's leg alone when the conference
    is not known yet."""
    v = twilio_voice.voice()
    try:
        if call.get("conference_sid"):
            v.end_conference(call["conference_sid"])
            return
    except Exception as exc:  # noqa: BLE001 - a conference that already ended
        log.info("ending the conference on %s: %s", call.get("id"), str(exc)[:80])
    if call.get("agent_sid"):
        try:
            v.end(call["agent_sid"])
        except Exception as exc:  # noqa: BLE001
            log.info("ending the agent leg on %s: %s", call.get("id"), str(exc)[:80])


# ---------------------------------------------------------------------- the caller's controls
def _mine(call_id: str, identity: str) -> dict | None:
    call = store.get_call(call_id)
    if not call:
        return None
    if call.get("identity") == identity:
        return call
    me = store.get_caller(identity) or {}
    return call if me.get("role") == "manager" else None


def hangup(call_id: str, identity: str) -> dict:
    call = _mine(call_id, identity)
    if not call:
        return {"ok": False, "error": "not your call"}
    if call.get("lead_sid"):
        try:
            twilio_voice.voice().end(call["lead_sid"])
        except Exception as exc:  # noqa: BLE001 - a leg that already ended
            log.info("hangup %s: %s", call["lead_sid"][-6:], exc)
    _end_room(call)
    store.add_event(call_id, "caller", "hangup", {})
    return {"ok": True}


def vm_url(caller: dict) -> str:
    if not caller.get("vm_token") or not caller.get("vm_file"):
        return ""
    return twilio_voice.url("/media/vm/%s.mp3" % caller["vm_token"])


def vm_drop(call_id: str, identity: str, auto: bool = False) -> dict:
    """Leave the caller's recorded voicemail and move on. The homeowner's leg leaves the conference to play
    the recording and hang up; that ends the conference, so the caller is free while it plays."""
    call = _mine(call_id, identity)
    if not call:
        return {"ok": False, "error": "not your call"}
    if config.S.vm_drop != "on":
        return {"ok": False, "error": "voicemail drop is off on this dialer: leave the voicemail yourself"}
    rec = None
    if call.get("record_uuid"):
        try:
            rec = sift.client().record(call["record_uuid"])
        except Exception:  # noqa: BLE001 - the state rule falls back to the number's own state
            rec = None
    if rules.state_rule(call.get("number") or "", rec).get("no_prerecorded"):
        return {"ok": False, "error": "a recorded voicemail is not allowed to this state: leave it yourself"}
    caller = store.get_caller(call.get("identity") or identity) or {}
    media = vm_url(caller)
    if not media:
        return {"ok": False, "error": "record your voicemail message first (Settings, Record voicemail)"}
    if not call.get("lead_sid") or call["status"] != "answered":
        return {"ok": False, "error": "a voicemail can be dropped once the line picks up"}
    try:
        twilio_voice.voice().redirect(call["lead_sid"], twilio_voice.twiml_play_hangup(media))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Twilio refused the drop: %s" % str(exc)[:120]}
    store.update_call(call_id, vm_dropped=1)
    store.add_event(call_id, "caller" if not auto else "system", "vm_drop", {"auto": auto})
    # The homeowner's leg left the conference to play the message; the caller is freed for the next dial.
    _end_room(store.get_call(call_id))
    publish(store.get_call(call_id))
    return {"ok": True}


def hold(call_id: str, identity: str, on: bool) -> dict:
    call = _mine(call_id, identity)
    if not call or not call.get("conference_sid") or not call.get("lead_sid"):
        return {"ok": False, "error": "the call is not connected"}
    try:
        twilio_voice.voice().hold(call["conference_sid"], call["lead_sid"], on)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}
    store.add_event(call_id, "caller", "hold" if on else "unhold", {})
    events.publish(call.get("identity") or "", "hold", {"call_id": call_id, "on": bool(on)})
    return {"ok": True, "on": bool(on)}


def transfer(call_id: str, identity: str, to_identity: str = "", to_number: str = "") -> dict:
    """Warm transfer: the closer (a teammate's phone page, or any number) is added to the call. The caller
    stays until they hang up; after that the call carries on without them."""
    call = _mine(call_id, identity)
    if not call or call["status"] != "answered" or not call.get("conference_sid"):
        return {"ok": False, "error": "only a connected call can be transferred"}
    if to_identity:
        target = store.get_caller(to_identity)
        if not target or not target.get("active"):
            return {"ok": False, "error": "that teammate is not set up on the dialer"}
        to, label = "client:%s" % target["identity"], "transfer"
    else:
        n = phone.digits(to_number)
        if not n or not phone.valid(n):
            return {"ok": False, "error": "that is not a number to transfer to"}
        to, label = phone.e164(n), "transfer"
    v = twilio_voice.voice()
    try:
        v.update_participant(call["conference_sid"], call["agent_sid"], end_conference_on_exit=False)
        res = v.add_participant(call["conference_sid"], phone.e164(call["from_number"]), to, label,
                                end_on_exit=True, call_id=call_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Twilio refused the transfer: %s" % str(exc)[:120]}
    store.add_event(call_id, "caller", "transfer", {"to": to_identity or phone.last4(to_number), "leg": res.get("call_sid")})
    if to_identity:
        events.publish(to_identity, "transfer_offer", {**_public(call), "from_caller": _caller_name(identity)})
    return {"ok": True}


def monitor_allowed(call_id: str, identity: str) -> dict:
    me = store.get_caller(identity) or {}
    call = store.get_call(call_id)
    if me.get("role") != "manager":
        return {"ok": False, "error": "only a manager can listen in"}
    if not call or call["status"] not in ("connecting", "ringing", "answered"):
        return {"ok": False, "error": "that call is not live"}
    return {"ok": True}


# ---------------------------------------------------------------------- after the call
def dispose(call_id: str, identity: str, key: str, note: str = "", callback_at: str = "") -> dict:
    call = _mine(call_id, identity)
    if not call:
        return {"ok": False, "error": "not your call"}
    row = dispositions.TABLE.get(key)
    if not row:
        return {"ok": False, "error": "unknown outcome %r" % key}
    if call.get("disposition"):
        return {"ok": False, "error": "this call already has an outcome (%s)" % call["disposition"]}
    if call["status"] in ("prepared", "connecting", "ringing", "answered", "blocked"):
        # The talk time, the recording's link and what the homeowner said last are only known at the end.
        # The page hangs up first when an outcome is picked mid-call.
        return {"ok": False, "error": "the call has not ended yet"}
    note = " ".join((note or "").split())[:1500]
    if row.needs_note and not note:
        return {"ok": False, "error": "say who answered (a name and how they are related)"}
    when = ""
    if row.task:
        d = store.parse(callback_at)
        if not d or d < datetime.now(timezone.utc):
            return {"ok": False, "error": "pick a call-back time that is still ahead"}
        when = d.isoformat(timespec="seconds")
    if row.contact and not call.get("answered_at") and call.get("direction") == "outbound":
        return {"ok": False, "error": "nobody answered this call, so it cannot be %r" % row.label}
    store.update_call(call_id, disposition=key, note=note, callback_at=when, disposed_at=store.now(),
                      disposed_by=identity, writeback_state="queued")
    store.add_event(call_id, "caller", "disposition", {"key": key, "typed_note": bool(note)})
    from . import writeback
    writeback.plan(store.get_call(call_id))
    nxt = {}
    if call.get("item_id"):
        from . import queue
        nxt = queue.after_call(store.get_call(call_id))
    if row.contact or key in ("relative", "owner_deceased"):
        store.close_handraises(call["number"], "called", identity)
    call = store.get_call(call_id)
    publish(call)
    return {"ok": True, "call": _public(call), "next": nxt}


# ---------------------------------------------------------------------- inbound
def inbound_twiml(params: dict) -> str:
    frm = phone.digits(params.get("From"))
    to = phone.digits(params.get("To"))
    csid = str(params.get("CallSid") or "")
    last = store.last_outbound(frm) if frm else None
    target = None
    if last and last.get("identity"):
        target = store.get_caller(last["identity"])
    if not target or not target.get("active"):
        target = store.caller_by_number(to)
    record_ok, _ = rules.may_record(frm)
    rec_uuid = (last or {}).get("record_uuid") or ""
    detail = {}
    if rec_uuid:
        try:
            detail = sift.summary(sift.client().record(rec_uuid))
        except Exception:  # noqa: BLE001 - an inbound call is answered whether DataSift answers or not
            detail = {}
    cid = store.new_call(identity=(target or {}).get("identity") or "", direction="inbound", kind="inbound", number=frm,
                         from_number=to, record_uuid=rec_uuid, owner_uuid=(last or {}).get("owner_uuid") or "",
                         status="ringing", lead_sid=csid, record_allowed=1 if record_ok else 0, started_at=store.now(),
                         detail=detail)
    store.add_event(cid, "twilio", "inbound", _keep(params))
    online = False
    if target:
        seen = store.parse(target.get("seen_at"))
        online = bool(seen and datetime.now(timezone.utc) - seen < timedelta(minutes=70))
    if target and online:
        publish(store.get_call(cid), "incoming")
        return twilio_voice.twiml_inbound(target["identity"], cid, 20)
    return twilio_voice.twiml_voicemail(cid, config.S.inbound_greeting)


def inbound_done(call_id: str, params: dict) -> str:
    call = store.get_call(call_id)
    st = str(params.get("DialCallStatus") or "").lower()
    if call:
        store.add_event(call_id, "twilio", "inbound_done:" + st, _keep(params))
    if st == "completed":
        try:
            secs = int(params.get("DialCallDuration") or 0)
        except (TypeError, ValueError):
            secs = 0
        if call:
            _advance(call_id, "ended", answered_at=call.get("answered_at") or store.now(), ended_at=store.now(),
                     end_status="completed", seconds=secs)
            publish(store.get_call(call_id))
        return twilio_voice.twiml_hangup()
    if call:
        store.update_call(call_id, end_status="missed")
    return twilio_voice.twiml_voicemail(call_id, config.S.inbound_greeting)


def on_voicemail(call_id: str, params: dict) -> None:
    call = store.get_call(call_id)
    if not call:
        return
    store.add_event(call_id, "twilio", "voicemail", _keep(params))
    try:
        secs = int(params.get("RecordingDuration") or 0)
    except (TypeError, ValueError):
        secs = 0
    _advance(call_id, "ended", ended_at=store.now(), end_status="voicemail", seconds=0,
             voicemail_sid=params.get("RecordingSid") or "", recording_seconds=secs)
    call = store.get_call(call_id)
    publish(call, "voicemail")
    if config.S.slack_webhook:
        store.add_job(call_id, "slack", {"kind": "voicemail"})


def answered_inbound(call_id: str, identity: str) -> None:
    """The caller's page took an incoming call (the Device reports it; Twilio's own callback comes at the end)."""
    call = store.get_call(call_id)
    if call and call.get("direction") == "inbound" and call.get("identity") in (identity, ""):
        _advance(call_id, "answered", answered_at=store.now(), identity=identity)
        publish(store.get_call(call_id))


def on_vm_recorded(identity: str, params: dict) -> None:
    """A caller's voicemail drop finished recording on Twilio: fetch it onto the volume."""
    if params.get("RecordingSid"):
        store.add_job("", "fetch_vm", {"identity": identity, "sid": params["RecordingSid"]})


def reconcile(call_id: str) -> None:
    """A call whose final callback never came (the box restarted mid-call): ask Twilio how it ended."""
    call = store.get_call(call_id)
    if not call or call["status"] in ("ended", "failed", "blocked"):
        return
    sid = call.get("lead_sid")
    if not sid:
        made = store.parse(call.get("created_at"))
        if made and datetime.now(timezone.utc) - made > timedelta(minutes=3):
            _advance(call_id, "ended", ended_at=store.now(), end_status="never_connected", seconds=0)
            publish(store.get_call(call_id))
        return
    got = twilio_voice.voice().fetch_call(sid)
    st = (got.get("status") or "").lower()
    if st in FINAL:
        on_status(call_id, "lead", {"CallSid": sid, "CallStatus": st, "CallDuration": got.get("duration") or 0})
