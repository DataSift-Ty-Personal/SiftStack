"""Which agent owns an incoming webhook, and which record it is about.

The workflow comes from the URL (`/hooks/datasift/<secret>/<workflow>`): each DataSift sequence
posts to its own path, so routing never depends on what DataSift puts in the body. The record
comes from the body, which DataSift now publishes (developers.datasift.ai/guides/webhooks:
`property.uuid` on a `sequence.action.triggered` event). Extraction stays defensive for any
other shape, and every miss is logged and posted rather than dropped.
"""
from __future__ import annotations

import re

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# workflow slug -> (agent, handler name). The roster's datasift:<slug> triggers extend this; the
# deep prospect route is also kept here so a roster that fails to load cannot take it down.
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


def routes(roster=None) -> dict:
    """Live routes only: a planned workflow's webhook must 404 rather than queue a task no
    handler can run."""
    out = dict(WORKFLOWS)
    if roster is not None:
        out.update({slug: owner_wf for slug, owner_wf in roster.datasift_routes().items()
                    if roster.workflows[owner_wf[1]].live})
    return out


def route(workflow: str, payload, roster=None) -> dict:
    table = routes(roster)
    if workflow not in table:
        raise KeyError("unknown workflow %r" % workflow)
    agent, handler = table[workflow]
    return {"agent": agent, "workflow": handler, "record_uuid": record_uuid(payload)}
