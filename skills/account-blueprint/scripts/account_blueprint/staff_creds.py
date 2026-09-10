"""EXPORT-ONLY credential lookup against the Deal Room auth store.

Reads reisift_auth.json with a plain json.load, never imports the Deal Room
client, never writes the store (so it cannot race the ty+2 cron jobs). The
community apply path never touches this module: a user applies with their
own JWT, staff apply with --impersonate or a paste.
"""
from __future__ import annotations

import json
import os
import time

from .client import Client, decode_claims

STORE = os.environ.get(
    "REISIFT_AUTH_STORE",
    r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients\config\reisift_auth.json")
API_KEY_ACCOUNT = "datasift-apikey"
STAFF_ACCOUNT = "ty2-direct"


def _accounts() -> dict:
    with open(STORE, encoding="utf-8") as f:
        return (json.load(f).get("accounts") or {})


def api_key_client(name: str = API_KEY_ACCOUNT) -> tuple[Client, dict]:
    env = os.environ.get("REISIFT_API_KEY", "").strip()
    if env:
        return Client.from_api_key(env), {"email": "", "account_id": ""}
    acct = _accounts().get(name) or {}
    if not acct.get("api_key"):
        raise SystemExit("no api_key on account %r in %s (or set REISIFT_API_KEY)" % (name, STORE))
    return Client.from_api_key(acct["api_key"]), acct


def staff_client(name: str = STAFF_ACCOUNT, min_left_s: int = 600) -> Client | None:
    tok = os.environ.get("REISIFT_STAFF_JWT", "").strip()
    if not tok:
        try:
            tok = (_accounts().get(name) or {}).get("access_token") or ""
        except OSError:
            return None
    if not tok:
        return None
    claims = decode_claims(tok)
    if int(claims.get("exp", 0)) - time.time() < min_left_s:
        return None
    if "staff" not in (claims.get("feature_flags") or []):
        return None
    return Client.from_jwt(tok)


def stored_target_jwt(email: str) -> str:
    """A directly pasted, unexpired token for `email` in the store, or ""."""
    try:
        accts = _accounts()
    except OSError:
        return ""
    for acct in accts.values():
        tok = acct.get("access_token") or ""
        if not tok:
            continue
        try:
            claims = decode_claims(tok)
        except Exception:
            continue
        if ((claims.get("email") or "").lower() == email.lower()
                and int(claims.get("exp", 0)) > time.time() + 600):
            return tok
    return ""
