"""Self-contained reisift client for deployments that have no Deal Room checkout.

`crm.py` prefers the shared `_api/clients` CRMClient, which is the richer and
better-tested path. But that lives on a local Windows disk, and the whole point
of putting the receiver on an always-on box is that it does not depend on a
laptop. This module is the fallback: everything the SMS agent actually needs,
over plain `requests`, configured by one environment variable.

Auth is the no-expiry Open API key: `authorization: Api-Key <key>`. That is the
right credential here precisely because it never expires, so a cloud worker
cannot die at 2am on a stale JWT the way the buyer sweep silently did.

Rate limiting is real: `/api/internal/` throttles hard and the 429 body carries
an "available in N seconds" hint. We parse it rather than guessing.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

BASE = "https://apiv2.reisift.io"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_RETRY_HINT = re.compile(r"available in (\d+)\s*second", re.I)


class StandaloneCRMError(RuntimeError):
    pass


class StandaloneCRM:
    """Minimal reisift client. Method names mirror the shared CRMClient."""

    def __init__(self, api_key: str, base: str = BASE, min_interval: float = 0.5) -> None:
        if not api_key:
            raise StandaloneCRMError("no reisift API key")
        self._key = api_key
        self._base = base.rstrip("/")
        self._min_interval = min_interval
        self._last_call = 0.0
        self._session = requests.Session()

    def _headers(self, content_type: Optional[str] = "application/json") -> dict:
        headers = {
            "authorization": f"Api-Key {self._key}",
            "accept": "application/json, text/plain, */*",
            "origin": "https://app.reisift.io",
            "referer": "https://app.reisift.io/",
            "x-reisift-ui-version": "2022.02.01.7",
            "user-agent": UA,
        }
        if content_type:
            headers["content-type"] = content_type
        return headers

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last_call = time.monotonic()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        method_override: Optional[str] = None,
        timeout: float = 30.0,
    ) -> dict:
        url = self._base + path
        for attempt in range(4):
            self._throttle()
            headers = self._headers()
            if method_override:
                # The property search is a GET whose body is too large for a
                # query string, so reisift accepts it as POST-as-GET.
                headers["x-http-method-override"] = method_override
            try:
                resp = self._session.request(
                    method, url, headers=headers, json=body, params=params, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt == 3:
                    raise StandaloneCRMError(f"network: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                # The server tells us how long to wait. Believe it.
                hint = _RETRY_HINT.search(resp.text or "")
                wait = int(hint.group(1)) + 1 if hint else 5 * (attempt + 1)
                log.info("reisift 429; sleeping %ss", wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise StandaloneCRMError(
                    f"HTTP {resp.status_code} on {method} {path}: {resp.text[:250]}"
                )
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        raise StandaloneCRMError(f"gave up after retries on {method} {path}")

    # ------------------------------------------------------------- reads

    def get_record(self, uuid: str) -> dict:
        return self._request(f"/api/internal/property/{uuid}/")

    def get_activity_log(
        self,
        uuid: str,
        *,
        event_types: Optional[list[str]] = None,
        limit: int = 100,
        max_pages: int = 5,
    ) -> list:
        """Paginated activity log, optionally filtered by event type.

        The one that matters here is `owner.sms.received`: smrtPhone already
        syncs inbound replies into the CRM, threaded to the record by phone. So
        the reply history exists before a single webhook is wired, which is how
        the classifier gets tested against real homeowner replies without
        sending anything.
        """
        events: list = []
        offset = 0
        for _ in range(max_pages):
            params = {"limit": limit, "offset": offset}
            if event_types:
                params["any_event_type"] = ",".join(event_types)
            resp = self._request(f"/api/internal/property/{uuid}/logs/", params=params)
            page = resp.get("results") or resp.get("data") or []
            count = resp.get("count", len(page))
            events.extend(page)
            if len(events) >= count or not page:
                break
            offset += limit
        return events

    # ------------------------------------------------------------ writes

    @staticmethod
    def _tag_names(rec: dict) -> list[str]:
        return [t.get("name") if isinstance(t, dict) else str(t) for t in (rec.get("tags") or [])]

    def add_tags(self, uuid: str, tags: list[str], *, dry_run: bool = True) -> dict:
        """Per-record tag add.

        Must be a read-modify-write PATCH of the FULL tag set. Two verified
        traps: PATCH {tags_add:[...]} is silently ignored, and
        POST /property/add-tags/ applies the tag ACCOUNT-WIDE while ignoring its
        own `properties` filter.
        """
        if dry_run:
            return {"_dry_run": True, "uuid": uuid, "add": tags}
        current = self._tag_names(self.get_record(uuid))
        merged = current + [t for t in tags if t not in current]
        return self._request(
            f"/api/internal/property/{uuid}/", method="PATCH", body={"tags": merged}
        )

    def update_status(self, uuid: str, status: str, *, dry_run: bool = True) -> dict:
        if dry_run:
            return {"_dry_run": True, "uuid": uuid, "status": status}
        return self._request(
            f"/api/internal/property/{uuid}/", method="PATCH", body={"status": status}
        )

    def add_message(self, uuid: str, message: str, *, pinned: bool = False, dry_run: bool = True) -> dict:
        if dry_run:
            return {"_dry_run": True, "uuid": uuid, "message": message[:120]}
        return self._request(
            f"/api/internal/property/{uuid}/message/",
            method="POST",
            body={"message": message, "pinned": pinned},
        )


def from_env() -> Optional[StandaloneCRM]:
    """Build a client from REISIFT_API_KEY, or None when it is not configured."""
    from . import config  # also loads .env

    key = (config.REISIFT_API_KEY or "").strip()
    if not key:
        return None
    try:
        return StandaloneCRM(key)
    except StandaloneCRMError as exc:
        log.warning("standalone CRM unavailable: %s", exc)
        return None


def check() -> tuple[bool, str]:
    """Probe the key without writing anything."""
    client = from_env()
    if client is None:
        return False, "REISIFT_API_KEY not set"
    try:
        client._request("/api/internal/property/", params={"limit": 1})
    except StandaloneCRMError as exc:
        return False, str(exc)[:200]
    return True, "Api-Key accepted"
