"""Send transport selection.

smrtPhone exposes two ways to put a text on a phone, and they are not
equivalent:

  api      POST /sms/send with the X-Auth-smrtPhone header. Text only, which is
           all a reply ever is. Honors the per-conversation sticky FROM number,
           so the whole pool is usable. Fast, no browser.
  session  Drives the web app's Compose Message modal with a saved Playwright
           session. Works when the API token is not usable on the account.
           Sends from the account's DEFAULT caller ID only, so the sticky-sender
           and per-number cap logic degrade to a single number.

The MMS/image path is the one that genuinely has no API. Replies carry no image,
so the API is the preferred transport for this agent and the session is the
fallback.

`auto` tries the API and falls back to the session on a transport-level failure,
never on a rejection: a 400 means the request was wrong and repeating it through
a browser would be wrong too.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import config, smrtphone

log = logging.getLogger(__name__)


def _session():
    from . import session_sender

    return session_sender


def available() -> dict:
    """What each transport reports about itself. Surfaced by `doctor`."""
    api_ok, api_detail = smrtphone.check_auth()
    try:
        sess_ok, sess_detail = _session().check_session()
    except Exception as exc:  # noqa: BLE001 - playwright may be absent entirely
        sess_ok, sess_detail = False, str(exc)[:160]
    return {
        "configured": config.TRANSPORT,
        "api": {"ok": api_ok, "detail": api_detail},
        "session": {"ok": sess_ok, "detail": sess_detail},
    }


def send(to: str, body: str, from_number: str = ""):
    """Send one message on the configured transport."""
    mode = config.TRANSPORT

    if mode == "session":
        return _session().send_sms(to, body, from_number)

    result = smrtphone.send_sms(to, body, from_number)
    if result.ok or mode == "api":
        return result

    # Only fall back on a transport failure. A 4xx is a rejected request, and a
    # browser would submit the same rejected content.
    if 400 <= (result.status_code or 0) < 500 and result.status_code != 429:
        return result

    log.warning("API send failed (%s); falling back to the browser session", result.error)
    fallback = _session().send_sms(to, body, from_number)
    if not fallback.ok:
        fallback.error = f"api: {result.error} | session: {fallback.error}"
    return fallback


def supports_number_pool() -> bool:
    """Whether the active transport can honor a per-conversation FROM number.

    The session path cannot: the compose modal uses the account default caller
    ID. Sticky senders and per-number caps are meaningless there, and saying so
    is better than pretending the pool is being used.
    """
    if config.TRANSPORT == "session":
        return False
    if config.TRANSPORT == "api":
        return True
    ok, _ = smrtphone.check_auth()
    return ok


def shutdown() -> None:
    try:
        _session().shutdown()
    except Exception:  # noqa: BLE001
        pass
