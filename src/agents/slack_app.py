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


_DASH_SPACED = re.compile(r"\s+[" + chr(0x2014) + chr(0x2013) + r"]\s+")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def clean_text(text: str) -> str:
    """Last line of defense for the house rules: no em or en dashes in anything the team says (a
    spaced dash becomes a comma, a bare en dash in a range a hyphen), and Markdown **bold** becomes
    Slack's *bold*, which Slack would otherwise print with the asterisks showing."""
    t = _DASH_SPACED.sub(", ", text or "")
    t = t.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
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
    if data.get("error") == "not_in_channel" and join(channel):
        data = _api("chat.postMessage", body)       # the app was not a member yet: joined, sent once more
    return str(data.get("ts") or "") if data.get("ok") else ""


def home_channel(employee=None, roster=None) -> str:
    """Where a post that belongs to no thread goes. One channel for the whole team when
    AGENTS_SLACK_CHANNEL is set; otherwise the employee's own channel, then the team's."""
    if config.SLACK_CHANNEL:
        return config.SLACK_CHANNEL
    if employee is not None and getattr(employee, "slack_channel", ""):
        return employee.slack_channel
    return roster.team_channel if roster is not None else ""


def join(channel: str) -> bool:
    """Join a public channel by id, so nobody has to invite the app before it can post or hear.
    A private channel refuses this (the app has to be invited there), and that is logged once."""
    if not (config.SLACK_BOT_TOKEN and channel):
        return False
    data = _api("conversations.join", {"channel": channel})
    if not data.get("ok"):
        log.warning("slack: could not join %s (%s). In a private channel, invite the app: /invite @<app name>",
                    channel, data.get("error"))
    return bool(data.get("ok"))


def notice(text: str, roster=None) -> str:
    """A message for the operator that belongs to no task (a webhook the brain could not read).
    With the app it goes to the team's home channel; without it, to the post-only webhook."""
    if config.SLACK_BOT_TOKEN:
        return post(home_channel(None, roster), text)
    slack.post(clean_text(text))
    return ""


def react(channel: str, ts: str, emoji: str = "eyes") -> None:
    """A reaction on the operator's message says "picked up" without adding a message to read."""
    if config.SLACK_BOT_TOKEN and channel and ts:
        _api("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji})


_SCOPES: dict = {"at": 0.0, "set": None}
SCOPES_TTL_S = 600


def scopes() -> set | None:
    """The bot token's granted scopes, read off the X-OAuth-Scopes header Slack puts on every Web
    API answer (auth.test is the cheapest call). None when unknown; cached ten minutes."""
    if not config.SLACK_BOT_TOKEN:
        return None
    if _SCOPES["set"] is not None and time.time() - _SCOPES["at"] < SCOPES_TTL_S:
        return _SCOPES["set"]
    try:
        r = _http.post("%s/auth.test" % API, timeout=15, headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
        raw = str((getattr(r, "headers", None) or {}).get("x-oauth-scopes") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("slack auth.test unreachable: %s", exc)
        return None
    got = {s.strip() for s in raw.split(",") if s.strip()} or None
    _SCOPES.update(at=time.time(), set=got)
    return got


def forget_scopes() -> None:
    """A download came back as a sign-in page: read the scopes again next time instead of trusting
    the cache (the operator may have just reinstalled the app)."""
    _SCOPES.update(at=0.0, set=None)


def can_read_files() -> bool:
    """False only when Slack says the token lacks files:read. Unknown counts as yes: the download
    itself then shows a sign-in page instead of the file, and that is caught there."""
    got = scopes()
    return got is None or "files:read" in got


def fetch(url: str):
    """The bytes of a private Slack file (files.slack.com), with the bot token. May raise."""
    return _http.get(url, timeout=60, headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})


def file_info(file_id: str) -> dict:
    """The full file object for a file Slack sent only as a stub; {} when it cannot be read."""
    if not (config.SLACK_BOT_TOKEN and file_id):
        return {}
    try:
        r = _http.get("%s/files.info" % API, params={"file": file_id}, timeout=15,
                      headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("slack files.info unreachable: %s", exc)
        return {}
    if not data.get("ok"):
        log.warning("slack files.info: %s", data.get("error"))
        return {}
    return data.get("file") or {}


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
