"""The CRM reads deep prospecting needs, over the Open API key alone.

dream100_dp reaches for the Deal Room `_api/clients` CRMClient and dossier helpers, which live on
a Windows desktop. The agents box on Fly has neither, and customer installs never will, so this
is the stand-in: the same method names and return shapes, built on `crm_standalone`, one
environment variable (REISIFT_API_KEY), no password and no JWT.

Verified 2026-11-03: the key reaches every `/api/internal/` read and write the pipeline uses.
"""
from __future__ import annotations

import os
from typing import Optional

from sms_agent.crm_standalone import StandaloneCRM


class KeyCRM(StandaloneCRM):
    """StandaloneCRM plus the reads the deep prospecting records phase calls."""

    def search_by_address(self, address_prefix: str, *, limit: int = 10, offset: int = 0) -> list[dict]:
        body = {"limit": limit, "offset": offset, "ordering": "-list_count",
                "query": {"must": {"property_type": "clean", "search": f"address_prefix:{address_prefix}"}}}
        resp = self._request("/api/internal/property/", method="POST", body=body, method_override="GET")
        return resp.get("results") or resp.get("data") or []

    def get_messages(self, uuid: str, *, limit: int = 999) -> list[dict]:
        resp = self._request(f"/api/internal/property/{uuid}/message/",
                             params={"limit": limit, "offset": 0, "ordering": "-created"})
        return resp.get("results", [])

    def get_custom_fields(self, uuid: str) -> list[dict]:
        resp = self._request(f"/api/internal/property/{uuid}/custom-field/", params={"limit": 1000, "offset": 0})
        return resp.get("results", [])

    def get_cards(self, uuid: str) -> list[dict]:
        resp = self._request(f"/api/internal/siftline/property/{uuid}/card/", params={"limit": 100, "offset": 0})
        return resp.get("results", [])


def from_env() -> KeyCRM:
    return KeyCRM((os.environ.get("REISIFT_API_KEY") or "").strip())


def resolve_actor(event: dict) -> str:
    """Who performed an activity-log event (port of dossier.resolve_actor)."""
    aei = event.get("author_extra_info") or {}
    if aei.get("first_name") or aei.get("last_name"):
        return f"{aei.get('first_name', '')} {aei.get('last_name', '')}".strip()
    payload = event.get("payload") or {}
    if event.get("source") == "smrtphone" or "sms" in payload or "call" in payload:
        sms, call = payload.get("sms") or {}, payload.get("call") or {}
        for who, how in ((sms.get("user") or {}, "SMS"), (call.get("user") or {}, "call")):
            if who.get("first_name") or who.get("last_name"):
                return f"{who.get('first_name', '')} {who.get('last_name', '')}".strip() + f" ({how})"
        if (call.get("external_user") or {}).get("name"):
            return f"{call['external_user']['name']} (call)"
        dest = sms.get("destination_number") or call.get("destination_number")
        if dest:
            return f"system (inbound to {dest})"
    return event.get("author") or "system"


def summarize_activity_log(events: list[dict]) -> dict:
    """Counts by category plus the newest event (port of dossier.summarize_activity_log)."""
    out = {"total_events": len(events), "calls": 0, "sms": 0, "status_changes": 0,
           "messages": 0, "exports": 0, "card_moves": 0}
    for e in events:
        et = (e.get("event_type") or "").lower()
        if et.startswith("owner.call"):
            out["calls"] += 1
        elif et.startswith("owner.sms"):
            out["sms"] += 1
        elif et == "property.status.updated":
            out["status_changes"] += 1
        elif et == "property.message.added":
            out["messages"] += 1
        elif et == "property.exported":
            out["exports"] += 1
        elif et == "card.moved":
            out["card_moves"] += 1
    top: Optional[dict] = events[0] if events else None
    out["last_event_type"] = (top or {}).get("event_type") or ""
    out["last_event_date"] = ((top or {}).get("timestamp") or "")[:19]
    out["last_actor"] = (top or {}).get("actor") or (top or {}).get("author") or ""
    return out
