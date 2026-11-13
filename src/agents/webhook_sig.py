"""DataSift outbound webhook signatures (Ed25519), as published at developers.datasift.ai/guides/webhooks.

Every delivery carries three headers:
    X-Sift-Webhook-Signature   "v1,<base64 signature>"
    X-Sift-Webhook-Timestamp   unix seconds when it was signed
    X-Sift-Webhook-Key-Id      which public key verifies it
The signature covers the exact bytes  f"{timestamp}." + raw_request_body  (never the re-serialised
JSON: key order and whitespace change and the signature stops matching). Public keys come from
GET https://apiv2.reisift.io/webhook/signing-keys/ with no Authorization header; they are cached
and refetched when a key id we do not hold arrives (a rotation lists both keys, active and
additional, and both are valid). A delivery older than MAX_AGE_S is refused, so a captured one
cannot be replayed; the window is generous because DataSift retries up to three times.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request

log = logging.getLogger(__name__)

KEYS_URL = "https://apiv2.reisift.io/webhook/signing-keys/"
MAX_AGE_S = int(float(os.environ.get("AGENTS_WEBHOOK_MAX_AGE_S") or 1800))
_CACHE: dict = {"keys": {}, "at": 0.0}


def _fetch_keys() -> dict:
    req = urllib.request.Request(KEYS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode() or "{}")
    return {k.get("key_id"): k.get("public_key") for k in data.get("keys") or []
            if k.get("key_id") and k.get("public_key") and str(k.get("algorithm", "ed25519")).lower() == "ed25519"}


def public_key(key_id: str, fetch=_fetch_keys) -> str:
    keys = _CACHE["keys"]
    if key_id not in keys or time.time() - _CACHE["at"] > 24 * 3600:
        try:
            _CACHE["keys"], _CACHE["at"] = fetch(), time.time()
        except Exception as exc:  # noqa: BLE001
            log.error("could not fetch DataSift webhook signing keys: %s", exc)
    return _CACHE["keys"].get(key_id, "")


def verify(headers, raw: bytes, now: float | None = None, fetch=_fetch_keys) -> tuple[bool, str]:
    """(valid, reason). `headers` is any case-insensitive mapping (Starlette's) or a plain dict."""
    def h(name: str) -> str:
        for k in (name, name.lower()):
            v = headers.get(k) if hasattr(headers, "get") else None
            if v:
                return str(v)
        return ""

    sig_h, ts_h, kid = h("X-Sift-Webhook-Signature"), h("X-Sift-Webhook-Timestamp"), h("X-Sift-Webhook-Key-Id")
    if not sig_h:
        return False, "unsigned"
    version, _, sig = sig_h.partition(",")
    if version.strip() != "v1" or not sig:
        return False, "unknown signature version %r" % version
    try:
        ts = int(ts_h)
    except ValueError:
        return False, "no timestamp"
    if abs((now or time.time()) - ts) > MAX_AGE_S:
        return False, "stale delivery (signed %ss ago)" % int((now or time.time()) - ts)
    pem = public_key(kid, fetch)
    if not pem:
        return False, "unknown key id %r" % kid
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        load_pem_public_key(pem.encode()).verify(base64.b64decode(sig), ("%d." % ts).encode() + raw)
        return True, ""
    except InvalidSignature:
        return False, "signature does not match"
    except Exception as exc:  # noqa: BLE001
        return False, "could not verify: %s" % str(exc)[:120]


def mode() -> str:
    """require: refuse anything that does not verify; log: verify, record, accept; off: skip."""
    m = (os.environ.get("AGENTS_WEBHOOK_SIGNATURES") or "require").strip().lower()
    return m if m in ("require", "log", "off") else "require"
