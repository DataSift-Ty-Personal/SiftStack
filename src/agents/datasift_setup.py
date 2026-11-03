"""Wire DataSift to the agents, over the Open API key alone. Dry by default; --commit applies.

For the Research agent's deep prospect it makes sure that:
  1. a webhook named "SiftStack agents: deep prospect" points at this brain's URL,
  2. the sequence "Send to Deep Prospecting" (status changed to Deep Prospecting) creates the card
     on the Deep Prospecting board in a column that exists AND fires that webhook, and is active.

ty+2 already had that sequence, inactive, pointing at a deleted column, so it is repaired in place
rather than duplicated: plans cap sequences (Essentials 3, Professional 8). Everything is read
back after the write. The webhook action shape comes from the DataSift app's own source:
{"action": "send-webhook", "payload": {"webhook_uuid": ..., "label": "send.webhook"}}.
"""
from __future__ import annotations

import json

from sms_agent.crm_standalone import StandaloneCRMError

import crm_key_client

from . import config

SEQUENCE_TITLE = "Send to Deep Prospecting"
WEBHOOK_NAME = "SiftStack agents: deep prospect"


def _list(crm, path: str) -> list:
    r = crm._request(path, params={"limit": 999})
    return r.get("results", r) if isinstance(r, dict) else r


def _id(obj: dict) -> str:
    return str(obj.get("uuid") or obj.get("id") or "")


def hook_url(workflow: str = "deep-prospect") -> str:
    return "%s/hooks/datasift/%s/%s" % (config.PUBLIC_URL, config.WEBHOOK_SECRET, workflow)


def plan_and_apply(commit: bool = False) -> dict:
    if not (config.PUBLIC_URL and config.WEBHOOK_SECRET):
        raise SystemExit("Set AGENTS_PUBLIC_URL and AGENTS_WEBHOOK_SECRET first.")
    crm = crm_key_client.from_env()
    report = {"commit": commit, "steps": []}
    say = report["steps"].append

    board = next((b for b in _list(crm, "/api/internal/siftline/board/")
                  if b["title"].strip().lower() == config.DP_BOARD.lower()), None)
    if not board:
        raise SystemExit("No SiftLine board named %r in this account." % config.DP_BOARD)
    cols = _list(crm, "/api/internal/siftline/board/%s/column/" % board["uuid"])
    col = next((c for c in cols if c["title"].strip().lower() == config.DP_COLUMN.lower()), None) or (cols or [None])[0]
    if not col:
        raise SystemExit("The %r board has no columns." % config.DP_BOARD)
    say("board %r, card column %r" % (board["title"], col["title"]))

    url = hook_url()
    hooks = _list(crm, "/api/internal/integration/webhook/")
    hook = next((h for h in hooks if h.get("name") == WEBHOOK_NAME), None)
    if hook and hook.get("url") == url:
        say("webhook exists and points here")
    elif hook:
        say("webhook exists, URL updated")
        if commit:
            crm._request("/api/internal/integration/webhook/%s/" % _id(hook), method="PATCH", body={"url": url})
    else:
        say("webhook created")
        if commit:
            hook = crm._request("/api/internal/integration/webhook/", method="POST",
                                body={"name": WEBHOOK_NAME, "url": url})
    hook_uuid = _id(hook or {}) or "DRY-RUN-WEBHOOK"

    actions = [
        {"action": "create-siftline-card",
         "payload": {"label": "property.card.create", "values": {"board": board["uuid"], "column": col["uuid"]}}},
        {"action": "send-webhook", "payload": {"webhook_uuid": hook_uuid, "label": "send.webhook"}},
    ]
    conditions = [{"condition": "from_to", "payload": {"field": "status", "label": "property.status.updated",
                                                       "resource": False, "updated_to": config.DP_STATUS}}]
    seqs = _list(crm, "/api/internal/sequence/")
    seq = next((s for s in seqs if (s.get("title") or "").strip() == SEQUENCE_TITLE), None)
    if seq:
        say("sequence %r repaired: card column fixed, webhook added, active" % SEQUENCE_TITLE)
        if commit:
            crm._request("/api/internal/sequence/%s/" % seq["uuid"], method="PATCH",
                         body={"actions": actions, "is_active": True})
    else:
        say("sequence %r created" % SEQUENCE_TITLE)
        if commit:
            seq = crm._request("/api/internal/sequence/", method="POST", body={
                "title": SEQUENCE_TITLE, "trigger": "property.status.updated", "conditions": conditions,
                "actions": actions, "is_active": True})

    if commit:
        back = crm._request("/api/internal/sequence/%s/" % seq["uuid"])
        got = json.dumps(back.get("actions"))
        report["verified"] = bool(back.get("is_active")) and hook_uuid in got and col["uuid"] in got
        say("read back: active=%s, webhook action=%s, card column=%s" % (
            back.get("is_active"), hook_uuid in got, col["uuid"] in got))
    report["webhook_url"] = url.replace(config.WEBHOOK_SECRET, "<secret>")
    return report


def safe_plan(commit: bool) -> dict:
    try:
        return plan_and_apply(commit)
    except StandaloneCRMError as exc:
        return {"commit": commit, "error": str(exc)}
