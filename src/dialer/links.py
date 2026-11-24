"""Signed links: a recording on a DataSift note, a voicemail on the phone page.

A link is the path plus an HMAC of it under the app's secret, so it works with no sign-in (a teammate
reading the record in DataSift can play the recording) and cannot be guessed or edited into another call's.
It does not expire: a note in the CRM is read months later. Rotating DIALER_SECRET retires every link.
"""
from __future__ import annotations

import hashlib
import hmac

from . import config


def sig(kind: str, ident: str) -> str:
    mac = hmac.new(config.secret_key(), ("%s:%s" % (kind, ident)).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]


def ok(kind: str, ident: str, given: str) -> bool:
    return bool(given) and hmac.compare_digest(sig(kind, ident), given or "")


def recording_link(call_id: str) -> str:
    return "%s/rec/%s/%s" % (config.S.public_url, call_id, sig("rec", call_id))


def voicemail_link(call_id: str) -> str:
    return "%s/vm/%s/%s" % (config.S.public_url, call_id, sig("vm", call_id))
