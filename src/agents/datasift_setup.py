"""Wire DataSift to the agents.

THE SUPPORTED PATH IS THE APP (developers.datasift.ai, 2026-11-01 changelog and Acceptable use):
creating or changing a webhook is an app-only operation, and sequences are not part of the Open
API at all. So by default this prints the steps for the person (Settings > Integrations > Webhooks,
then Sequence Builder) with this brain's exact URL, and reports whether DataSift's own
`webhook.test` delivery has reached the brain, which is the proof the setup worked.

--staff-api keeps the old route for OUR accounts only: it creates the webhook and repairs the
sequence "Send to Deep Prospecting" through endpoints the reference no longer lists. ty+2 already
had that sequence, inactive, pointing at a deleted column, so it is repaired in place rather than
duplicated (plans cap sequences: Essentials 3, Professional 8). The webhook action shape comes from
the app's own source: {"action": "send-webhook", "payload": {"webhook_uuid": ..., "label": "send.webhook"}}.
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


def test_receipt(workflow: str = "deep-prospect") -> tuple[str, str]:
    """(when DataSift's Test delivery last reached the brain, where that was read). The receipt is
    written by the RUNNING brain, so its /health is asked first: on Fly the local database is not
    the one the webhook landed in. The local store answers only for a brain run on this machine."""
    import requests
    try:
        r = requests.get(config.PUBLIC_URL.rstrip("/") + "/health", timeout=15)
        if r.ok:
            return (r.json().get("datasift_webhook_tests") or {}).get(workflow, ""), "the running brain"
    except Exception:  # noqa: BLE001
        pass
    from . import store
    store.init()
    return store.kv_get("datasift:webhook_test:%s" % workflow), "the local database (brain not reachable)"


def app_checklist(workflow: str = "deep-prospect") -> dict:
    """The supported setup: what a person does in the DataSift app, and whether it has worked."""
    if not (config.PUBLIC_URL and config.WEBHOOK_SECRET):
        raise SystemExit("Set AGENTS_PUBLIC_URL and AGENTS_WEBHOOK_SECRET first.")
    got, where = test_receipt(workflow)
    return {"mode": "app", "webhook_url": hook_url(workflow), "verified": bool(got),
            "test_received": got or "not yet", "checked": where, "steps": [
        "DataSift > Settings > Integrations > Webhooks > Add Webhook. Name: %s. URL: the webhook_url above. Save." % WEBHOOK_NAME,
        "Select Test on that webhook. This brain records the test delivery; re-run this command to see it arrive.",
        "Sequence Builder: open (or create) the sequence %r: trigger Property Status Change, condition status "
        "updated to %r; actions: create a card on the %r board, then Send a webhook > %s. Make it active." % (
            SEQUENCE_TITLE, config.DP_STATUS, config.DP_BOARD, WEBHOOK_NAME),
        "Creating webhooks needs the Super Admin or Sensei role; anyone with Sequence Builder can use one.",
    ]}


def plan_and_apply(commit: bool = False) -> dict:
    """The --staff-api path (our accounts only): routes the published reference no longer lists."""
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


def safe_plan(commit: bool, staff_api: bool = False) -> dict:
    if not staff_api:
        return app_checklist()
    try:
        return plan_and_apply(commit)
    except StandaloneCRMError as exc:
        return {"commit": commit, "error": str(exc)}
