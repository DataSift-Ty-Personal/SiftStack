"""Thin ZeroBounce v2 client (https://www.zerobounce.net/docs/).

Key: ZEROBOUNCE_API_KEY from the environment, else from the repo .env. One credit per address
checked; "unknown" results and duplicates are free. `verify_plus` is sent on every call so the
account setting never decides it silently: Verify+ is free, sends a probe from ZeroBounce's own
servers, and is the only way to catch disabled Yahoo, AOL and Verizon mailboxes that a standard
check reports as valid.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

BASE = "https://api.zerobounce.net/v2"
ROOT = Path(__file__).resolve().parents[2]
BATCH_MAX = 200


def api_key() -> str:
    k = os.environ.get("ZEROBOUNCE_API_KEY", "").strip()
    if not k and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("ZEROBOUNCE_API_KEY="):
                k = line.split("=", 1)[1].strip()
    if not k:
        raise RuntimeError("ZEROBOUNCE_API_KEY is not set (env or .env)")
    return k


class ZeroBounceError(RuntimeError):
    pass


class ZeroBounce:
    def __init__(self, key: str | None = None):
        self.key = key or api_key()
        self.s = requests.Session()

    def credits(self) -> int:
        r = self.s.get(f"{BASE}/getcredits", params={"api_key": self.key}, timeout=30)
        r.raise_for_status()
        n = int(r.json().get("Credits", -1))
        if n < 0:
            raise ZeroBounceError("credit read refused (bad key?)")
        return n

    def validate(self, email: str, *, verify_plus: bool = True, timeout: int = 60) -> dict:
        r = self.s.get(f"{BASE}/validate", timeout=timeout + 30, params={
            "api_key": self.key, "email": email, "ip_address": "", "timeout": timeout,
            "verify_plus": "true" if verify_plus else "false"})
        r.raise_for_status()
        return r.json()

    def validate_batch(self, emails: list[str], *, verify_plus: bool = True, timeout: int = 120) -> list[dict]:
        """Up to 200 addresses per call (the endpoint allows 5 calls a minute)."""
        out = []
        for i in range(0, len(emails), BATCH_MAX):
            body = {"api_key": self.key, "timeout": timeout, "verify_plus": verify_plus,
                    "email_batch": [{"email_address": e, "ip_address": None} for e in emails[i:i + BATCH_MAX]]}
            for attempt in range(5):
                r = self.s.post(f"{BASE}/validatebatch", json=body, timeout=timeout + 60)
                if r.status_code == 429 and attempt < 4:
                    time.sleep(15 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            d = r.json()
            if d.get("errors"):
                raise ZeroBounceError(f"batch errors: {str(d['errors'])[:300]}")
            out += d.get("email_batch") or []
        return out
