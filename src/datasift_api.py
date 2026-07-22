"""REISift / DataSift Open API client — REST alternative to Playwright.

Covers the daily-run HOT PATH only (Phase 1 of the API migration):
    upload properties → ensure list/tag → assign → skip-trace → verify by count

Everything the Open API does NOT expose (sequences/drip campaigns, SiftMap
"Enrich Property Info") stays on the Playwright layer in datasift_uploader.py.
This module is designed to sit BEHIND the same call sites so main.py does not
care which backend runs; when the API key is unset, callers fall back to
Playwright.

Auth: Authorization: Api-Key <key>   (plans above Professional)
Base: https://apiv2.reisift.io       (SiftMap: https://map.reisift.io, same key)

Full API reference lives in-repo at docs/datasift-api/ (README.md indexes it;
12-endpoint-index.md lists every operation; 13-use-case-playbooks.md has the
end-to-end recipes).

Behavior below is aligned to the DataSift developer docs (observed working
behavior), notably:
  * bulk-create takes an ARRAY of the property payload.
  * list responses page under `data` OR `results` depending on resource;
    _page_items() handles both.
  * tags/lists on the property create payload are comma-strings, not arrays.
  * THE OPERATING RULE: verify the data, never the exit code. A 200/201 means
    accepted, not applied. Every write helper here has a matching *_count read
    so callers can confirm the change landed.

Endpoint auth reality (mapped live 2026-07-21 — the key does NOT work
everywhere the docs imply):
  * WORKS with the Open API key: GET user/property/list/tag/activity/status,
    POST list/, POST tag/, POST property/ (single create), property add-tags,
    DELETE property/list/tag.
  * DOES NOT honor the key: POST property/bulk-create/ (401) and the top-level
    v1 POST /property/ surface (403). Hence bulk_create_properties() loops
    single creates instead of hitting bulk-create.
  * UNTESTED (do not fire blind — real cost / side effects): skip-trace.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://apiv2.reisift.io"
SIFTMAP_BASE = "https://map.reisift.io"

DEFAULT_TIMEOUT = 30  # seconds


class DataSiftAPIError(RuntimeError):
    """Raised when the REISift API returns a non-2xx response."""


class DataSiftAPI:
    """Thin synchronous REST client for the REISift Open API hot path.

    Call sites in the async pipeline can wrap invocations in
    ``asyncio.to_thread(...)``; the REST calls themselves are blocking.
    """

    def __init__(self, api_key: str, base_url: str = API_BASE, timeout: int = DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError("api_key is required (DATASIFT_API_KEY)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Api-Key {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # ── low-level ─────────────────────────────────────────────────────
    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        resp = self._session.request(method, url, **kwargs)
        if not resp.ok:
            # Docs: always log the response body on non-2xx; validation
            # messages name the offending field.
            raise DataSiftAPIError(
                f"{method} {url} → {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _page_items(body: Any) -> list[dict]:
        """Extract the row list from a page, tolerating `data` or `results`."""
        if not isinstance(body, dict):
            return []
        return body.get("data") or body.get("results") or []

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow next-link pagination, returning all rows across pages."""
        out: list[dict] = []
        next_url: str | None = path
        first = True
        while next_url:
            body = self._request("GET", next_url, params=params if first else None)
            first = False
            out.extend(self._page_items(body))
            next_url = body.get("next") if isinstance(body, dict) else None
        return out

    # ── verification (safe to call blind) ─────────────────────────────
    def verify(self) -> dict:
        """Confirm the key authenticates and the plan exposes the API.

        Hits GET /api/internal/user/ (read-only). Returns the user payload;
        raises DataSiftAPIError on 401 (bad/revoked key) or 403 (plan lacks
        API access).
        """
        return self._request("GET", "/api/internal/user/")

    def count_properties(self, params: dict | None = None) -> int:
        """Total account property count (cheap — one record + the count).

        The core verification primitive: snapshot before/after a write and
        compare the delta.
        """
        body = self._request("GET", "/api/internal/property/",
                             params={**(params or {}), "limit": 1})
        return int(body.get("count", 0)) if isinstance(body, dict) else 0

    # ── lists & tags ──────────────────────────────────────────────────
    def list_lists(self) -> list[dict]:
        return self._paginate("/api/internal/list/", params={"limit": 200})

    def ensure_list(self, title: str) -> dict:
        """Return the list named `title`, creating it if absent (idempotent).

        Names use plain ASCII hyphens — the platform strips em dashes from
        titles, so the name you write is the name you read back.
        """
        for lst in self.list_lists():
            if lst.get("title", "").strip().lower() == title.strip().lower():
                return lst
        return self._request("POST", "/api/internal/list/", json={"title": title})

    def list_properties_count(self, list_uuid: str) -> int:
        body = self._request("GET", f"/api/internal/list/{list_uuid}/properties-count/")
        return int(body.get("properties_count", 0)) if isinstance(body, dict) else 0

    def list_tags(self) -> list[dict]:
        return self._paginate("/api/internal/tag/", params={"limit": 200})

    def ensure_tag(self, title: str) -> dict:
        for tag in self.list_tags():
            if tag.get("title", "").strip().lower() == title.strip().lower():
                return tag
        return self._request("POST", "/api/internal/tag/", json={"title": title})

    def tag_properties_count(self, tag_uuid: str) -> int:
        body = self._request("GET", f"/api/internal/tag/{tag_uuid}/properties-count/")
        return int(body.get("properties_count", 0)) if isinstance(body, dict) else 0

    def add_tags_to_property(self, property_uuid: str, tags: list[str]) -> Any:
        # Confirmed live 2026-07-21: payload is {"tags": [...]}, returns
        # {"new_tags": [...]}. Key accepted on this endpoint.
        return self._request(
            "POST", f"/api/internal/property/{property_uuid}/add-tags/",
            json={"tags": tags},
        )

    def add_lists_to_property(self, property_uuid: str, lists: list[str]) -> Any:
        # Mirrors add-tags; {"lists": [...]} by symmetry (add-tags confirmed).
        return self._request(
            "POST", f"/api/internal/property/{property_uuid}/add-lists/",
            json={"lists": lists},
        )

    # ── upload ────────────────────────────────────────────────────────
    def property_exists(self, *, reapi_id: str | None = None, sift_id: str | None = None) -> Any:
        """Dedupe check before create. Docs: match on reapi_id or sift_id."""
        payload = {k: v for k, v in {"reapi_id": reapi_id, "sift_id": sift_id}.items() if v}
        return self._request("POST", "/api/internal/property/exists/", json=payload)

    def create_property(self, record: dict) -> dict:
        """Create ONE property; returns the created record (with uuid).

        Confirmed live 2026-07-21: the payload shape is
            {"address": {street, city, state, postal_code, country},
             "owner":   {first_name, last_name, "address": {...}, phones, emails},
             "lists": "Src 2026-07", "tags": "FTM", "notes": "..."}
        `lists`/`tags` are comma-STRINGS on input; the response returns them as
        arrays. owner.address is REQUIRED (400 without it). A bad/synthetic
        address yields type="incomplete".
        """
        return self._request("POST", "/api/internal/property/", json=record)

    def bulk_create_properties(self, properties: list[dict],
                               dedupe: bool = False) -> dict:
        """Create many properties.

        IMPORTANT: the native POST /property/bulk-create/ endpoint does NOT
        honor the Open API key (returns 401 "credentials were not provided" —
        it is app-session-only). Confirmed live 2026-07-21. So this loops
        single create_property() calls, which the key DOES accept. Fine for the
        daily OH volumes (tens of records); do not use for 10k+ imports.

        Returns {"created": [records], "errors": [{"index", "error"}]}.
        Verify the data, never the exit code — the caller should compare the
        target list's properties-count afterward.
        """
        created, errors = [], []
        for i, rec in enumerate(properties):
            try:
                created.append(self.create_property(rec))
            except DataSiftAPIError as e:
                errors.append({"index": i, "error": str(e)})
        return {"created": created, "errors": errors}

    # ── custom fields ─────────────────────────────────────────────────
    def list_custom_fields(self) -> list[dict]:
        """All account custom-field definitions (title + id).

        We resolve titles → ids against what ALREADY exists (Mike's manual
        uploads created "Notice Type", "County", "Date Added", "Decision
        Maker", etc.). We never blind-create definitions in the live account —
        a mistyped field name orphans values and the docs warn against casual
        deletes.
        """
        return self._paginate("/api/internal/custom-fields/", params={"limit": 200})

    def custom_field_title_map(self) -> dict[str, str]:
        """{lower-cased title: field id} for the account's custom fields."""
        out: dict[str, str] = {}
        for f in self.list_custom_fields():
            title = (f.get("title") or "").strip()
            fid = f.get("uuid") or f.get("id")
            if title and fid:
                out[title.lower()] = str(fid)
        return out

    def update_custom_field_values(self, property_uuid: str,
                                   field_values: dict[str, str]) -> Any:
        """Write custom-field values on a record.

        `field_values` maps field ID → value. Payload shape mirrors the
        documented update pattern; the exact envelope is confirmed on first
        live run (docs say "read one record's values first, mirror the shape").
        Non-fatal by design at the call site — a record still lands clean +
        tagged + listed + noted even if this write is rejected.
        """
        payload = {"values": [{"custom_field": fid, "value": val}
                              for fid, val in field_values.items()]}
        return self._request(
            "PATCH",
            f"/api/internal/property/{property_uuid}/custom-field/update-values/",
            json=payload,
        )

    def read_custom_field_values(self, property_uuid: str) -> Any:
        return self._request(
            "GET", f"/api/internal/property/{property_uuid}/custom-field/")

    def add_notes_to_property(self, property_uuid: str, notes: str) -> Any:
        return self._request(
            "POST", f"/api/internal/property/{property_uuid}/add-notes/",
            json={"notes": notes},
        )

    # ── skip trace ────────────────────────────────────────────────────
    def skip_trace(self, payload: dict) -> Any:
        """Submit a batch of property records for skip tracing (async).

        VERIFY LIVE for the exact request shape (property uuids vs. filter).
        Monitor progress with skiptrace_stats() rather than polling each record;
        confirm results by re-reading records for `skiptraced: true`.
        """
        return self._request("POST", "/api/internal/property/skip-trace/", json=payload)

    def skiptrace_stats(self) -> dict:
        return self._request("GET", "/api/internal/activity/skiptrace/stats/")

    # ── activity polling ──────────────────────────────────────────────
    def list_activities(self, activity_type: str = "upload", limit: int = 20) -> list[dict]:
        """activity_type: 'upload' | 'skip_trace'."""
        body = self._request(
            "GET", "/api/internal/activity/",
            params={"type": activity_type, "limit": limit, "ordering": "-created"},
        )
        return self._page_items(body)

    def wait_for_activity(
        self, activity_uuid: str, activity_type: str = "upload",
        poll_secs: int = 15, timeout_secs: int = 1800,
    ) -> dict:
        """Poll until the activity with `activity_uuid` reports done, or timeout."""
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            for act in self.list_activities(activity_type, limit=50):
                if act.get("uuid") == activity_uuid:
                    status = str(act.get("status", "")).lower()
                    if status in {"complete", "completed", "failed"}:
                        return act
            time.sleep(poll_secs)
        raise DataSiftAPIError(f"activity {activity_uuid} did not finish in {timeout_secs}s")


def from_config() -> "DataSiftAPI | None":
    """Build a client from the configured API key, or None if unset.

    Reads config.DATASIFT_API_KEY (which already falls back to the legacy
    REISIFT_OPEN_API_KEY alias). Lets call sites do:
        api = from_config()
        if api: ...REST...   else: ...Playwright fallback...
    """
    key = ""
    try:
        import config
        key = getattr(config, "DATASIFT_API_KEY", "")
    except Exception:
        import os
        key = os.getenv("DATASIFT_API_KEY", "") or os.getenv("REISIFT_OPEN_API_KEY", "")
    return DataSiftAPI(key) if key else None


if __name__ == "__main__":
    # Manual smoke test:  PYTHONPATH=src python -m datasift_api
    logging.basicConfig(level=logging.INFO)
    client = from_config()
    if not client:
        print("DATASIFT_API_KEY not set — nothing to verify.")
    else:
        user = client.verify()
        print("Auth OK. User:", user.get("email"), "| plan:", user.get("plan_name"))
        print("Account property count:", client.count_properties())
