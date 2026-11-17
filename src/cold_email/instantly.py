"""Thin Instantly API v2 client (https://developer.instantly.ai).

Key: INSTANTLY_API_KEY from the environment, else from the repo .env. Bearer auth.
Limits are per workspace (100/s, 6,000/min); the emails list is 20/min. A 429 is retried
with backoff, a read that drops its connection is retried, a write never is.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

BASE = "https://api.instantly.ai/api/v2"
ROOT = Path(__file__).resolve().parents[2]


class InstantlyError(RuntimeError):
    def __init__(self, code: int, method: str, path: str, body: str):
        super().__init__(f"HTTP {code} on {method} {path}: {body[:400]}")
        self.code, self.body = code, body


def api_key() -> str:
    k = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not k:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("INSTANTLY_API_KEY="):
                    k = line.split("=", 1)[1].strip()
    if not k:
        raise RuntimeError("INSTANTLY_API_KEY is not set (env or .env)")
    return k


class Instantly:
    def __init__(self, key: str | None = None, timeout: int = 45):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {key or api_key()}"
        self.timeout = timeout
        self.calls = 0

    def call(self, method: str, path: str, *, params=None, json=None, tries: int = 5):
        url = BASE + path
        for attempt in range(tries):
            self.calls += 1
            try:
                r = self.s.request(method, url, params=params, json=json, timeout=self.timeout)
            except requests.RequestException:
                if method == "GET" and attempt < tries - 1:
                    time.sleep(2 + attempt * 3)
                    continue
                raise
            if r.status_code == 429 and attempt < tries - 1:
                time.sleep(min(60, 2 ** attempt * 3))
                continue
            if r.status_code >= 400:
                raise InstantlyError(r.status_code, method, path, r.text)
            return r.json() if r.content else {}
        raise RuntimeError("unreachable")

    def pages(self, path: str, params: dict | None = None, cap: int = 100_000):
        """Every item of a cursor-paged GET listing."""
        q = dict(params or {}, limit=100)
        n = 0
        while True:
            d = self.call("GET", path, params=q)
            items = d.get("items") or []
            yield from items
            n += len(items)
            cur = d.get("next_starting_after")
            if not cur or not items or n >= cap:
                return
            q["starting_after"] = cur

    # accounts
    def accounts(self) -> list[dict]:
        return list(self.pages("/accounts", {"include_tags": "true"}))

    def account(self, email: str) -> dict:
        return self.call("GET", f"/accounts/{email}")

    def patch_account(self, email: str, body: dict) -> dict:
        return self.call("PATCH", f"/accounts/{email}", json=body)

    def warmup_analytics(self, emails: list[str]) -> dict:
        out = {"email_date_data": {}, "aggregate_data": {}}
        for i in range(0, len(emails), 100):
            d = self.call("POST", "/accounts/warmup-analytics", json={"emails": emails[i:i + 100]})
            for k in out:
                out[k].update(d.get(k) or {})
        return out

    # campaigns
    def campaigns(self) -> list[dict]:
        return list(self.pages("/campaigns"))

    def create_campaign(self, body: dict) -> dict:
        return self.call("POST", "/campaigns", json=body)

    def activate(self, campaign_id: str) -> dict:
        return self.call("POST", f"/campaigns/{campaign_id}/activate")

    def pause(self, campaign_id: str) -> dict:
        return self.call("POST", f"/campaigns/{campaign_id}/pause")

    # leads
    def add_leads(self, leads: list[dict], *, campaign_id: str | None = None, list_id: str | None = None,
                  idempotency_key: str | None = None, **flags) -> dict:
        assert bool(campaign_id) != bool(list_id), "a campaign or a list, not both"
        body = {"leads": leads, **flags}
        body["campaign_id" if campaign_id else "list_id"] = campaign_id or list_id
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return self.call("POST", "/leads/add", json=body)

    # unibox
    def emails(self, **params) -> list[dict]:
        return list(self.pages("/emails", params))

    def reply(self, reply_to_uuid: str, eaccount: str, subject: str, text: str) -> dict:
        return self.call("POST", "/emails/reply", json={
            "reply_to_uuid": reply_to_uuid, "eaccount": eaccount, "subject": subject,
            "body": {"text": text, "html": text.replace("\n", "<br/>")}})

    # block list
    def block(self, values: list[str]) -> dict:
        out = {"valid_count": 0, "invalid_count": 0}
        for i in range(0, len(values), 1000):
            d = self.call("POST", "/block-lists-entries/bulk-create", json={"bl_values": values[i:i + 1000]})
            out["valid_count"] += d.get("valid_count") or 0
            out["invalid_count"] += d.get("invalid_count") or 0
        return out
