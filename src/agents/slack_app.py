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
import os
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


_ICON_VERSIONS: dict = {}


def icon_url(path: str) -> str:
    """A public URL Slack can fetch for a picture under team/assets/ (the brain serves that folder at
    /assets/<file>). Versioned by the file's content, because Slack caches an icon by its URL, so a new
    picture under the same name still shows. Empty when the brain has no public address (AGENTS_PUBLIC_URL,
    else the Fly app's own name) or the file is not a served picture: the post then keeps its emoji."""
    from . import team
    if not path or not team._asset_ok(path):
        return ""
    fly = os.environ.get("FLY_APP_NAME", "")
    base = config.PUBLIC_URL or ("https://%s.fly.dev" % fly if fly else "")
    if not base:
        return ""
    p = config.ROOT / path
    if path not in _ICON_VERSIONS:
        _ICON_VERSIONS[path] = hashlib.sha256(p.read_bytes()).hexdigest()[:10]
    return "%s/assets/%s?v=%s" % (base, p.name, _ICON_VERSIONS[path])


def identity(employee=None, roster=None) -> dict:
    """The name and picture a post goes out under: an employee's own name and portrait, or for the
    app's own posts (relays, alerts) the roster's app icon. A picture that cannot be served falls back
    to the employee's emoji, and an app post to Slack's own app icon. Needs chat:write.customize."""
    if employee is not None:
        out = {"username": employee.display_name}
        url = icon_url(getattr(employee, "icon", ""))
        if url:
            out["icon_url"] = url
        elif getattr(employee, "icon_emoji", ""):
            out["icon_emoji"] = employee.icon_emoji
        return out
    if roster is None:
        try:
            from . import team
            roster = team.load()
        except Exception:  # noqa: BLE001  (no roster: the app's own icon)
            return {}
    url = icon_url(getattr(roster, "app_icon", ""))
    return {"icon_url": url} if url else {}


def post(channel: str, text: str, *, thread_ts: str = "", employee=None) -> str:
    """Post one message; returns its ts ("" when it could not thread or failed). `employee` is a
    team.Employee: the message goes out under that employee's name and portrait. Without one it goes
    out as the app, under the roster's app icon."""
    text = clean_text(text)
    if not config.SLACK_BOT_TOKEN:
        who = "*%s:* " % employee.display_name if employee else ""
        slack.post(who + text)
        return ""
    body = {"channel": channel, "text": text[:3900], "unfurl_links": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    body.update(identity(employee))
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


def channel_for(kind: str, employee=None, roster=None) -> str:
    """Where a post of this kind goes: "pipeline" (the team's daily view: the morning digest, what a
    live stage did, a lead that needs a call now), "issues" (anything broken, in plain words) or
    "chat" (talking with the team). A department ("sales", "marketing", "ops", 2026-11-19) goes to its
    own channel, else the ops channel, else the pipeline channel. A kind whose channel is not set goes
    to the team's home channel, so nothing is lost before the channels exist."""
    if kind in ("sales", "marketing") and getattr(config, "SLACK_%s_CHANNEL" % kind.upper(), ""):
        return getattr(config, "SLACK_%s_CHANNEL" % kind.upper())
    if kind in ("sales", "marketing", "ops") and getattr(config, "SLACK_OPS_CHANNEL", ""):
        return config.SLACK_OPS_CHANNEL
    boards = (getattr(config, "SLACK_LAYOUT", "") or "classic").strip().lower() == "boards"
    if kind == "pipeline" and boards and getattr(config, "SLACK_OPS_CHANNEL", ""):
        return config.SLACK_OPS_CHANNEL            # the boards layout retires the pipeline channel: a straggler lands in ops
    if kind in ("pipeline", "sales", "marketing", "ops") and config.SLACK_PIPELINE_CHANNEL:
        return config.SLACK_PIPELINE_CHANNEL
    if kind == "issues" and config.SLACK_ISSUES_CHANNEL:
        return config.SLACK_ISSUES_CHANNEL
    return home_channel(employee, roster)


# chat.update answers these when the message it was asked to edit is gone (deleted, or in a channel the
# app left): the caller posts a new one instead.
GONE = {"message_not_found", "cant_update_message", "edit_window_closed", "channel_not_found", "is_archived",
        "not_in_channel"}


def update(channel: str, ts: str, text: str) -> tuple[bool, str]:
    """Edit one of the app's own messages in place: (ok, Slack's error word). An edit sends nobody a
    notification, which is the point of a message the team keeps current through the day."""
    if not (config.SLACK_BOT_TOKEN and channel and ts):
        return False, "not_set_up"
    data = _api("chat.update", {"channel": channel, "ts": ts, "text": clean_text(text)[:3900]})
    return bool(data.get("ok")), str(data.get("error") or "")


def permalink(channel: str, ts: str) -> str:
    """A link to one message, or "" when Slack will not say."""
    if not (config.SLACK_BOT_TOKEN and channel and ts):
        return ""
    try:
        r = _http.get("%s/chat.getPermalink" % API, params={"channel": channel, "message_ts": ts}, timeout=15,
                      headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
        data = r.json() if hasattr(r, "json") else {}
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("permalink") or "") if data.get("ok") else ""


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


def notice(text: str, roster=None, kind: str = "issues") -> str:
    """A message for the operator that belongs to no task (a webhook the brain could not read goes to
    the issues channel; a word about hearing the team channel stays in it, kind="chat"). Without the
    app it goes to the post-only webhook."""
    if config.SLACK_BOT_TOKEN:
        return post(channel_for(kind, None, roster), text)
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


def channel_before(channel_id: str, ts: str, limit: int = 5) -> list[dict]:
    """The messages posted in a channel just before `ts`, oldest first: [{"who", "text"}]. An ask
    written at the top of a channel often points at the post above it ("give me the links to these");
    the employee sees its own thread only, so without this it answers that nothing was posted (Ty,
    2026-11-17). Empty when Slack cannot be read; never raises."""
    if not (config.SLACK_BOT_TOKEN and channel_id and ts):
        return []
    try:
        r = _http.get("%s/conversations.history" % API, timeout=15,
                      params={"channel": channel_id, "latest": ts, "limit": int(limit), "inclusive": "false"},
                      headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("slack conversations.history unreachable: %s", exc)
        return []
    if not data.get("ok"):
        return []
    out = []
    for m in reversed(data.get("messages") or []):            # Slack returns the newest first
        if m.get("subtype") in ("channel_join", "channel_leave", "channel_topic", "channel_purpose"):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        who = m.get("username") or (m.get("bot_profile") or {}).get("name") or ("the operator" if m.get("user") else "the app")
        out.append({"who": str(who), "text": text[:1500]})
    return out
