"""Twilio: the browser token, the TwiML for each leg, the REST calls, and the webhook signature.

Everything the dialer asks of Twilio goes through `Voice`, so the selftest replaces one object and every
call path runs against a fake. The open-source pieces doing the work are Twilio's own: twilio-python (MIT)
here and the Voice JavaScript SDK (Apache-2.0) in the browser.

The call model is a conference per call:
  * the caller's browser connects through the TwiML App; /twilio/voice answers with <Dial><Conference>
    named for the call (dlr_<call id>), joining as the "agent";
  * the server dials the homeowner into the same conference with the Conference Participants API, with
    early media on so the caller hears it ring, the homeowner's leg recorded in two channels when the law
    allows, and the participant marked to end the conference when they hang up;
  * a voicemail drop moves the homeowner's leg out of the conference onto <Play> then <Hangup>, which ends
    the conference and frees the caller for the next dial while the message plays on;
  * hold, warm transfer and a manager's listen or whisper are participant updates and additions.

REST auth uses the API key (TWILIO_API_KEY_SID and its secret), which can be revoked on its own; the
Auth Token is kept only to check webhook signatures (and fetch recordings, which Twilio protects with it).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)

STATUS_EVENTS = ["initiated", "ringing", "answered", "completed"]
CONF_EVENTS = "start end join leave hold"


def url(path: str, **query) -> str:
    """An absolute callback URL on this app (Twilio signs the exact URL it calls, query included)."""
    from urllib.parse import urlencode
    q = {k: v for k, v in query.items() if v not in (None, "")}
    return config.S.public_url + path + ("?" + urlencode(q) if q else "")


# Twilio retries a callback once, and only when it cannot connect. A deploy restarts the machine and Fly
# answers 502 for those seconds, so every callback URL asks for up to three retries on a connect failure,
# a read timeout or a 5xx (Twilio's connection overrides ride in the fragment, which it strips before the
# request, so the signature is computed on the URL without it).
RETRY = "#rc=3&rp=ct,rt,5xx"


def cb(path: str, **query) -> str:
    return url(path, **query) + RETRY


# ---------------------------------------------------------------------- TwiML
def _vr():
    from twilio.twiml.voice_response import VoiceResponse
    return VoiceResponse()


def twiml_agent(call: dict) -> str:
    """The caller's browser leg: join this call's conference, end it when the caller leaves."""
    from twilio.twiml.voice_response import Dial
    r, d = _vr(), Dial()
    d.conference(call["conference_name"], start_conference_on_enter=True, end_conference_on_exit=True, beep=False,
                 wait_url="", participant_label="agent",
                 status_callback=cb("/twilio/conference", call_id=call["id"]), status_callback_event=CONF_EVENTS)
    r.append(d)
    return str(r)


def twiml_monitor(call: dict, mode: str) -> str:
    """A manager's leg: listen (muted), whisper (only the caller hears them) or barge (everyone hears)."""
    from twilio.twiml.voice_response import Dial
    r, d = _vr(), Dial()
    kw = dict(start_conference_on_enter=False, end_conference_on_exit=False, beep=False, wait_url="",
              participant_label="monitor")
    if mode == "listen":
        kw["muted"] = True
    elif mode == "whisper" and call.get("agent_sid"):
        kw["coach"] = call["agent_sid"]
    d.conference(call["conference_name"], **kw)
    r.append(d)
    return str(r)


def twiml_record_vm(identity: str) -> str:
    """The caller records their own voicemail drop by phone (a browser recording is webm, which <Play>
    cannot play; a Twilio recording is mp3)."""
    r = _vr()
    r.say("Record your voicemail message after the tone. Press the pound key when you are done.")
    r.record(max_length=90, finish_on_key="#", play_beep=True, trim="trim-silence",
             recording_status_callback=cb("/twilio/vm_recorded", identity=identity),
             recording_status_callback_event="completed")
    r.say("Thank you. Your voicemail message is saved.")
    return str(r)


def twiml_inbound(identity: str, call_id: str, ring_seconds: int) -> str:
    """A homeowner calling one of our numbers: ring the caller who owns it, then voicemail."""
    from twilio.twiml.voice_response import Dial
    r = _vr()
    rec = bool(call_id and _record_inbound(call_id))
    d = Dial(timeout=ring_seconds, answer_on_bridge=True, action=url("/twilio/inbound_done", call_id=call_id),
             record="record-from-answer-dual" if rec else None,
             recording_status_callback=cb("/twilio/recording", call_id=call_id) if rec else None)
    c = d.client()
    c.identity(identity)
    c.parameter(name="call_id", value=call_id)
    r.append(d)
    return str(r)


def _record_inbound(call_id: str) -> bool:
    from . import store
    c = store.get_call(call_id) or {}
    return bool(c.get("record_allowed"))


def twiml_voicemail(call_id: str, greeting: str) -> str:
    r = _vr()
    r.say(greeting)
    r.record(max_length=180, play_beep=True, trim="trim-silence", timeout=6,
             recording_status_callback=cb("/twilio/voicemail", call_id=call_id),
             recording_status_callback_event="completed")
    r.hangup()
    return str(r)


def twiml_play_hangup(media_url: str) -> str:
    r = _vr()
    r.play(media_url)
    r.hangup()
    return str(r)


def twiml_say_hangup(text: str) -> str:
    r = _vr()
    r.say(text)
    r.hangup()
    return str(r)


def twiml_hangup() -> str:
    r = _vr()
    r.hangup()
    return str(r)


# ---------------------------------------------------------------------- tokens
def token(identity: str, ttl: int = 3600) -> str:
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant
    s = config.S
    t = AccessToken(s.account_sid, s.api_key_sid, s.api_key_secret, identity=identity, ttl=ttl)
    t.add_grant(VoiceGrant(outgoing_application_sid=s.twiml_app_sid, incoming_allow=True))
    tok = t.to_jwt()
    return tok.decode("utf-8") if isinstance(tok, bytes) else tok


# ---------------------------------------------------------------------- signatures
def valid_signature(full_url: str, params: dict, signature: str, settings_header: str = "") -> bool:
    """Twilio signs the full URL it requested plus every form field. Behind Fly's proxy the URL the app
    sees is not the one Twilio called, so the caller passes the public URL. A SharedKey signature (the
    X-Twilio-Webhooks-Signature-Settings header names HMAC_SHA256) is checked with TWILIO_WEBHOOK_KEY."""
    from twilio.request_validator import RequestValidator, get_signature_algorithm
    s = config.S
    if not signature:
        return False
    algorithm = get_signature_algorithm(settings_header)
    key = s.webhook_key if (settings_header and s.webhook_key) else s.auth_token
    if not key:
        return False
    try:
        return bool(RequestValidator(key).validate(full_url, params, signature, algorithm))
    except Exception as exc:  # noqa: BLE001 - an unsupported algorithm fails closed
        log.warning("signature check failed: %s", exc)
        return False


# ---------------------------------------------------------------------- REST
class Voice:
    """The REST surface. One instance per process; the selftest swaps in its own."""

    def __init__(self, client: Any = None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client
            s = config.S
            self._client = Client(s.api_key_sid, s.api_key_secret, s.account_sid)
        return self._client

    def dial_lead(self, call: dict, from_number: str, to_e164: str, record: bool, amd: str,
                  ring_seconds: int) -> dict:
        """Dial the homeowner into the call's conference. Returns {call_sid, conference_sid}."""
        # end_conference_on_exit stays False on the homeowner: a voicemail drop moves their leg out of the
        # conference, and a transfer must outlive the caller. The server ends the conference itself when
        # the homeowner's leg reports it is over (calls.on_status).
        kw = dict(
            from_=from_number, to=to_e164, label="lead", early_media=True, beep="false",
            start_conference_on_enter=True, end_conference_on_exit=False, timeout=max(10, int(ring_seconds)),
            time_limit=4 * 3600,
            status_callback=cb("/twilio/status", call_id=call["id"], leg="lead"),
            status_callback_event=STATUS_EVENTS,
        )
        if record:
            kw.update(record=True, recording_channels="dual", recording_track="both",
                      recording_status_callback=cb("/twilio/recording", call_id=call["id"]),
                      recording_status_callback_event=["completed", "absent"])
        if amd in ("detect", "auto_drop"):
            kw.update(machine_detection="DetectMessageEnd" if amd == "auto_drop" else "Enable",
                      amd_status_callback=cb("/twilio/amd", call_id=call["id"]))
        p = self.client.conferences(call["conference_name"]).participants.create(**kw)
        return {"call_sid": getattr(p, "call_sid", ""), "conference_sid": getattr(p, "conference_sid", "")}

    def add_participant(self, conference: str, from_number: str, to: str, label: str, *, coaching: bool = False,
                        agent_sid: str = "", muted: bool = False, end_on_exit: bool = False, call_id: str = "") -> dict:
        kw = dict(from_=from_number, to=to, label=label, beep="false", early_media=True, muted=muted,
                  end_conference_on_exit=end_on_exit, timeout=30,
                  status_callback=cb("/twilio/status", call_id=call_id, leg=label), status_callback_event=STATUS_EVENTS)
        if coaching and agent_sid:
            kw.update(coaching=True, call_sid_to_coach=agent_sid)
        p = self.client.conferences(conference).participants.create(**kw)
        return {"call_sid": getattr(p, "call_sid", ""), "conference_sid": getattr(p, "conference_sid", "")}

    def update_participant(self, conference_sid: str, call_sid: str, **kw) -> None:
        self.client.conferences(conference_sid).participants(call_sid).update(**kw)

    def hold(self, conference_sid: str, call_sid: str, on: bool) -> None:
        self.update_participant(conference_sid, call_sid, hold=bool(on))

    def remove_participant(self, conference_sid: str, call_sid: str) -> None:
        self.client.conferences(conference_sid).participants(call_sid).delete()

    def redirect(self, call_sid: str, twiml: str) -> None:
        self.client.calls(call_sid).update(twiml=twiml)

    def end_conference(self, conference_sid: str) -> None:
        """End a call's conference: every leg still in it hangs up (the caller, a closer, a manager)."""
        self.client.conferences(conference_sid).update(status="completed")

    def end(self, call_sid: str) -> None:
        """Hang up a call that is in progress, or stop one still ringing."""
        try:
            self.client.calls(call_sid).update(status="completed")
        except Exception as exc:  # noqa: BLE001 - a call not yet answered refuses "completed"
            log.info("completing %s failed (%s); canceling instead", call_sid[-6:], str(exc)[:80])
            self.client.calls(call_sid).update(status="canceled")

    def fetch_call(self, call_sid: str) -> dict:
        c = self.client.calls(call_sid).fetch()
        return {"status": getattr(c, "status", ""), "duration": getattr(c, "duration", None),
                "start_time": str(getattr(c, "start_time", "") or ""), "end_time": str(getattr(c, "end_time", "") or ""),
                "answered_by": getattr(c, "answered_by", "") or ""}

    def recording_bytes(self, recording_sid: str, ext: str = "mp3") -> bytes:
        """A recording's audio. Twilio protects media with the account's credentials."""
        import requests
        s = config.S
        r = requests.get("https://api.twilio.com/2010-04-01/Accounts/%s/Recordings/%s.%s" % (s.account_sid, recording_sid, ext),
                         auth=(s.account_sid, s.auth_token), timeout=60)
        r.raise_for_status()
        return r.content

    def numbers(self) -> list[dict]:
        out = []
        for n in self.client.incoming_phone_numbers.list(limit=200):
            out.append({"sid": n.sid, "number": n.phone_number, "name": n.friendly_name,
                        "voice_url": n.voice_url, "voice_application_sid": getattr(n, "voice_application_sid", "")})
        return out


_VOICE: list = []


def voice() -> Voice:
    if not _VOICE:
        _VOICE.append(Voice())
    return _VOICE[0]


def set_voice(v: Optional[Voice]) -> None:
    _VOICE.clear()
    if v is not None:
        _VOICE.append(v)
