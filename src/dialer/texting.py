"""The texting app, where Smarter Contact lives.

The texting app owns Smarter Contact (its login, its threads, its labels, its do-not-contact list), so the
dialer never talks to Smarter Contact itself. Three things cross between the two apps, each with a bearer
token in a header (a path secret would be printed by the access log):

  * GET  <texting>/hooks/dialer/thread?phone=   the text thread with the number being called, shown beside
    the record (what we texted, what they answered, whether the thread is paused).
  * POST <texting>/hooks/dialer/call            a call's outcome. The texting app applies its own half of the
    same disposition row: a conversation pauses its AI thread (the takeover it used to infer by polling
    smrtPhone's log), a stop closes the thread and queues the contact for Smarter Contact's do-not-contact,
    a no closes it and cancels any queued text, a hand raise pauses it for the person who now has it.
  * POST <dialer>/hooks/handraise              the texting app pushes a hand raise here the moment it posts
    one to Slack, so it sits at the top of every caller's phone page with a call button.

None of this blocks a call: the thread is fetched after the record is on screen, and the outcome is a
queued job that retries until the texting app takes it.
"""
from __future__ import annotations

import time

from . import config

_CACHE: dict = {}


def configured() -> bool:
    return bool(config.S.texting_url and config.S.texting_token)


def _headers() -> dict:
    return {"authorization": "Bearer " + config.S.texting_token, "accept": "application/json"}


def thread(number: str) -> dict:
    """{state, messages: [{direction, body, author, created_at}]} for a number, or {} when the texting app
    is not set up or has no thread with it."""
    if not configured() or not number:
        return {}
    hit = _CACHE.get(number)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    import requests
    try:
        r = requests.get(config.S.texting_url + "/hooks/dialer/thread", params={"phone": number}, headers=_headers(),
                         timeout=6)
    except Exception:  # noqa: BLE001 - a thread we cannot read is shown as none, never an error on the call
        return {}
    if r.status_code != 200:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    _CACHE[number] = (time.time(), data)
    return data


def call_outcome(call: dict) -> dict:
    """Tell the texting app how a call went (a queued job; raises so the job retries)."""
    from . import dispositions, store
    import requests
    row = dispositions.TABLE.get(call.get("disposition") or "")
    caller = (store.get_caller(call.get("identity") or "") or {}).get("name") or ""
    body = {
        "event": "dialerCall", "call_id": call["id"], "phone": call.get("number"),
        "record_uuid": call.get("record_uuid") or "", "owner_uuid": call.get("owner_uuid") or "",
        "direction": call.get("direction"), "answered": bool(call.get("answered_at")),
        "seconds": int(call.get("seconds") or 0), "caller": caller,
        "disposition": call.get("disposition") or "", "sms_kind": row.sms if row else "",
        "at": call.get("ended_at") or call.get("created_at"),
    }
    r = requests.post(config.S.texting_url + "/hooks/dialer/call", json=body, headers=_headers(), timeout=15)
    if r.status_code == 404:
        raise RuntimeError("the texting app does not take dialer calls yet (SMS_AGENT_DIALER_TOKEN unset, or an older build)")
    if r.status_code >= 400:
        raise RuntimeError("the texting app answered %s" % r.status_code)
    _CACHE.pop(call.get("number") or "", None)
    try:
        return r.json()
    except ValueError:
        return {"ok": True}
