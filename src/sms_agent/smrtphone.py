"""smrtPhone API client: outbound SMS and the do-not-text list.

The conversation leg is pure API. Only the ORIGINAL auction-screenshot MMS
needs the browser path in src/mms_sender.py, because smrtPhone's public API is
text-only (no mediaUrl param, confirmed 2026-06). Replies are text, so nothing
here touches Playwright.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import config, store

log = logging.getLogger(__name__)

SEND_PATH = "/sms/send"
# Admin > DNT Numbers exists in the web app; the public API route for it is not
# documented. We attempt it, and fall back to local suppression so an opt-out
# is honored either way. See `add_to_dnt`.
DNT_PATHS = ("/dnt/add", "/api/dnt/add", "/numbers/dnt/add")


@dataclass
class SendResult:
    ok: bool
    sms_id: str = ""
    error: str = ""
    status_code: int = 0


def _headers() -> dict:
    return {
        "X-Auth-smrtPhone": config.SMRTPHONE_API_KEY,
        "Accept": "application/json",
    }


def send_sms(to: str, body: str, from_number: str, timeout: int = 20) -> SendResult:
    """POST /sms/send. Returns a result rather than raising, always."""
    if not config.SMRTPHONE_API_KEY:
        return SendResult(False, error="SMRTPHONE_API_KEY not set")
    if not from_number:
        return SendResult(False, error="no from_number assigned")

    to_e164 = _e164(to)
    url = config.SMRTPHONE_BASE.rstrip("/") + SEND_PATH
    payload = {"from": _e164(from_number), "to": to_e164, "message": body}

    last = ""
    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, headers=_headers(), timeout=timeout)
        except requests.RequestException as exc:
            last = f"network: {exc}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code in (200, 201, 202):
            sms_id = ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    sms_id = str(
                        data.get("smsId") or data.get("id") or data.get("sid") or ""
                    )
            except ValueError:
                pass
            return SendResult(True, sms_id=sms_id, status_code=resp.status_code)

        last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        # 4xx other than rate limiting is a request problem; retrying repeats it.
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            return SendResult(False, error=last, status_code=resp.status_code)
        time.sleep(2 ** attempt)

    return SendResult(False, error=last or "send failed after 3 attempts")


def add_to_dnt(phone: str) -> tuple[bool, str]:
    """Add a number to smrtPhone's native Do-Not-Text list.

    The route is NOT documented (only the addNumberToDNT webhook is), so this
    tries the plausible paths and reports honestly when none work. Local
    suppression in `store` is applied by the caller regardless, so an opt-out
    is always honored on our side even when the remote write fails.
    """
    if not config.SMRTPHONE_API_KEY:
        return False, "SMRTPHONE_API_KEY not set"
    e164 = _e164(phone)
    errors = []
    for path in DNT_PATHS:
        url = config.SMRTPHONE_BASE.rstrip("/") + path
        try:
            resp = requests.post(url, data={"phone": e164}, headers=_headers(), timeout=15)
        except requests.RequestException as exc:
            errors.append(f"{path}: {exc}")
            continue
        if resp.status_code in (200, 201, 202, 204):
            return True, path
        errors.append(f"{path}: HTTP {resp.status_code}")
    return False, "; ".join(errors)[:300]


def _e164(n: str) -> str:
    digits = store.clean_phone(n)
    return f"+1{digits}" if len(digits) == 10 else (n if str(n).startswith("+") else f"+{digits}")


def check_auth() -> tuple[bool, str]:
    """Credential probe used by `doctor`. Cannot send anything.

    Verified live 2026-08-10: POST /sms/send with NO parameters returns 400
    "Missing required parameter(s): from, to, message" on a valid key and 403
    Forbidden on an invalid one. That is the clean auth signal, and because no
    destination is supplied there is nothing to send.

    Do not probe GET /dialerConfigs: it answers 405 for GET and serves the web
    app's HTML for POST regardless of the key, so it proves nothing.
    """
    if not config.SMRTPHONE_API_KEY:
        return False, "SMRTPHONE_API_KEY not set"
    try:
        resp = requests.post(
            config.SMRTPHONE_BASE.rstrip("/") + SEND_PATH, headers=_headers(), timeout=15
        )
    except requests.RequestException as exc:
        return False, f"network: {exc}"
    if resp.status_code == 400 and "required parameter" in resp.text.lower():
        return True, "key accepted by /sms/send"
    if resp.status_code in (401, 403):
        return False, f"key rejected (HTTP {resp.status_code})"
    return False, f"unexpected HTTP {resp.status_code}: {resp.text[:140]}"
