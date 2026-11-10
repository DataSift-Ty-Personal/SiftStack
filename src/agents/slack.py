"""Slack output for the agents, plus signed links to the files they produce.

An incoming webhook cannot upload a file, so a property file is posted as a link to the brain's
own `/files` route, signed with the webhook secret so the URL cannot be guessed or walked.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import requests

from . import config

log = logging.getLogger(__name__)


def sign(task_id: int, name: str) -> str:
    key = (config.WEBHOOK_SECRET or "unset").encode()
    return hmac.new(key, f"{task_id}:{name}".encode(), hashlib.sha256).hexdigest()[:32]


def file_url(task_id: int, name: str) -> str:
    if not config.PUBLIC_URL:
        return ""
    return f"{config.PUBLIC_URL}/files/{task_id}/{sign(task_id, name)}/{name}"


def post(text: str) -> bool:
    """Post one message. Never raises: a Slack outage must not fail the work it reports on."""
    if not config.SLACK_WEBHOOK:
        log.info("slack (no webhook set): %s", text[:500])
        return False
    try:
        r = requests.post(config.SLACK_WEBHOOK, json={"text": text}, timeout=15)
        if r.status_code >= 300:
            log.warning("slack %s: %s", r.status_code, r.text[:200])
        return r.status_code < 300
    except requests.RequestException as exc:
        log.warning("slack unreachable: %s", exc)
        return False
