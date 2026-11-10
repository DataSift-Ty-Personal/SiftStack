"""Which agent owns an incoming webhook, and which record it is about.

The workflow comes from the URL (`/hooks/datasift/<secret>/<workflow>`): each DataSift sequence
posts to its own path, so routing never depends on what DataSift puts in the body. The record
does come from the body, whose shape DataSift does not document, so extraction is defensive and
every miss is logged and posted rather than dropped.
"""
from __future__ import annotations

import re

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# workflow slug -> (agent, handler name)
WORKFLOWS = {
    "deep-prospect": ("research", "deep_prospect"),
}

_PROPERTY_KEYS = ("property_uuid", "record_uuid", "propertyUuid", "property_id")
_CONTAINERS = ("property", "record", "resource", "data", "object", "payload", "card", "event")


def _is_uuid(v) -> bool:
    return isinstance(v, str) and bool(UUID_RE.match(v.strip()))


def record_uuid(payload) -> str:
    """The property uuid in a DataSift webhook body, or "" when none can be found."""
    if not isinstance(payload, dict):
        return ""
    for k in _PROPERTY_KEYS:
        if _is_uuid(payload.get(k)):
            return payload[k].strip()
    prop = payload.get("property")
    if isinstance(prop, dict) and _is_uuid(prop.get("uuid")):
        return prop["uuid"].strip()
    if _is_uuid(prop):
        return prop.strip()
    # A bare property object: it has an address and its own uuid.
    if _is_uuid(payload.get("uuid")) and ("address" in payload or "owner" in payload):
        return payload["uuid"].strip()
    for k in _CONTAINERS:
        inner = payload.get(k)
        if isinstance(inner, dict):
            found = record_uuid(inner)
            if found:
                return found
    return ""


def route(workflow: str, payload) -> dict:
    if workflow not in WORKFLOWS:
        raise KeyError("unknown workflow %r" % workflow)
    agent, handler = WORKFLOWS[workflow]
    return {"agent": agent, "workflow": handler, "record_uuid": record_uuid(payload)}
