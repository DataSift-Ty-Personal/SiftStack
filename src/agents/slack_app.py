"""Two-way Slack for the team: verify what Slack sends in, post as each employee in threads.

With a bot token (a real Slack app, created from team.slack_manifest) every employee speaks under
its own name and icon, in the thread it was asked in, and the brain hears replies. Without one the
team falls back to the post-only incoming webhook (slack.post): it still reports, but it cannot
thread, cannot speak as an employee, and nobody can talk to it.

Nothing here raises. A Slack outage must not fail the work it reports on.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time

import requests

from . import config, slack

log = logging.getLogger(__name__)

API = "https://slack.com/api"
_http = requests                  # swapped for a fake in the selftest
_CHANNEL_NAMES: dict[str, str] = {}


def mode() -> str:
    if config.SLACK_BOT_TOKEN:
        return "bot"
    return "webhook" if config.SLACK_WEBHOOK else "none"


def verify(headers: dict, raw: bytes, now: float | None = None) -> bool:
    """Slack's v0 signature: HMAC-SHA256 of "v0:<timestamp>:<body>" with the signing secret, and a
    timestamp inside five minutes so a captured request cannot be replayed."""
    secret = config.SLACK_SIGNING_SECRET
    ts, sig = headers.get("x-slack-request-timestamp", ""), headers.get("x-slack-signature", "")
    if not (secret and ts and sig):
        return False
    try:
        if abs((now or time.time()) - int(ts)) > 300:
            return False
    except ValueError:
        return False
    base = b"v0:" + ts.encode() + b":" + raw
    want = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


_DASH_SPACED = re.compile(r"\s+[—–]\s+")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def clean_text(text: str) -> str:
    """Last line of defense for the house rules: no em or en dashes in anything the team says (a
    spaced dash becomes a comma, a bare en dash in a range a hyphen), and Markdown **bold** becomes
    Slack's *bold*, which Slack would otherwise print with the asterisks showing."""
    t = _DASH_SPACED.sub(", ", text or "")
    t = t.replace("—", ", ").replace("–", "-")
    return _MD_BOLD.sub(r"*\1*", t)


def _api(method: str, payload: dict) -> dict:
    try:
        r = _http.post("%s/%s" % (API, method), json=payload, timeout=15,
                       headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN,
                                "Content-Type": "application/json; charset=utf-8"})
        data = r.json() if hasattr(r, "json") else {}
    except Exception as exc:  # noqa: BLE001  (requests, JSON, anything: never raise)
        log.warning("slack %s unreachable: %s", method, exc)
        return {"ok": False, "error": str(exc)}
    if not data.get("ok"):
        log.warning("slack %s: %s", method, data.get("error"))
    return data


def post(channel: str, text: str, *, thread_ts: str = "", employee=None) -> str:
    """Post one message; returns its ts ("" when it could not thread or failed). `employee` is a
    team.Employee: the message goes out under that employee's name and icon."""
    text = clean_text(text)
    if not config.SLACK_BOT_TOKEN:
        who = "*%s:* " % employee.display_name if employee else ""
        slack.post(who + text)
        return ""
    body = {"channel": channel, "text": text[:3900], "unfurl_links": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if employee:
        body["username"] = employee.display_name
        if employee.icon_emoji:
            body["icon_emoji"] = employee.icon_emoji
    data = _api("chat.postMessage", body)
    return str(data.get("ts") or "") if data.get("ok") else ""


def react(channel: str, ts: str, emoji: str = "eyes") -> None:
    """A reaction on the operator's message says "picked up" without adding a message to read."""
    if config.SLACK_BOT_TOKEN and channel and ts:
        _api("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji})


def channel_name(channel_id: str) -> str:
    """Slack events carry channel IDs (C0123...), never names, so routing by channel needs the
    name. Cached for the life of the process; "" for a DM or when the lookup fails."""
    if not channel_id or channel_id.startswith("D"):
        return ""
    if channel_id in _CHANNEL_NAMES:
        return _CHANNEL_NAMES[channel_id]
    name = ""
    if config.SLACK_BOT_TOKEN:
        try:
            r = _http.get("%s/conversations.info" % API, params={"channel": channel_id}, timeout=15,
                          headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
            data = r.json()
            if data.get("ok"):
                name = str((data.get("channel") or {}).get("name") or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("slack conversations.info unreachable: %s", exc)
    if name:
        _CHANNEL_NAMES[channel_id] = name
    return name
