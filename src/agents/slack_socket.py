"""Socket Mode: Slack pushes events over a WebSocket the brain opens with an app-level token.

The other way in is the Request URL (the brain's /slack/events, verified with the signing secret).
This one needs no public URL check and no signing secret: the connection is opened with the app's
own app-level token (xapp-..., scope connections:write), and Slack sends over it when Socket Mode
is switched on for the app. Either way in works, and with both set an event that arrives twice is
dropped by its id.

Each event arrives in an envelope that has to be acknowledged within three seconds, so the envelope
is acknowledged first and handled after. Slack closes a connection every few hours with a
`disconnect` frame; a new one is opened at once. Anything else that breaks the connection is
retried with a growing pause. Nothing here raises into the brain.

An open connection is not proof that Slack is sending: with Socket Mode switched OFF for the app the
connection still opens and simply stays silent. `brain.hearing_notice` covers that case.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger(__name__)

OPEN_URL = "https://slack.com/api/apps.connections.open"
RECV_TIMEOUT_S = 30.0
PAUSE_MIN_S, PAUSE_MAX_S = 2.0, 60.0
LINK_DISABLED_PAUSE_S = 300.0          # Socket Mode was switched off for the app: do not hammer Slack
_http = requests                       # swapped for a fake in the selftest

STATE = {"state": "off", "since": "", "error": "", "events": 0, "connects": 0}
_LOCK = threading.Lock()


def _set(**kw) -> None:
    with _LOCK:
        STATE.update(kw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    """What /health shows: off, connecting, connected or down, since when, and how many events."""
    with _LOCK:
        out = {"state": STATE["state"], "since": STATE["since"], "events": STATE["events"]}
        if STATE["error"]:
            out["error"] = STATE["error"]
        return out


def open_url() -> tuple[str, str]:
    """(a one-time wss URL, "") or ("", why not). The URL is good for one connection."""
    if not config.SLACK_APP_TOKEN:
        return "", "no app-level token"
    try:
        r = _http.post(OPEN_URL, headers={"Authorization": "Bearer %s" % config.SLACK_APP_TOKEN}, timeout=20)
        data = r.json()
    except Exception as exc:  # noqa: BLE001  (requests, JSON, anything: never raise)
        return "", "slack unreachable (%s)" % type(exc).__name__
    if not data.get("ok"):
        return "", str(data.get("error") or "refused")
    url = str(data.get("url") or "")
    return (url, "") if url.startswith("wss://") else ("", "no socket address in the answer")


def handle_frame(raw, send, handler) -> str:
    """One frame from Slack. Returns what it was: hello, disconnect:<reason>, event, other or bad.
    An envelope is acknowledged BEFORE its event is handled: Slack allows three seconds, and an
    employee's turn can take longer than that to queue."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return "bad"
    if not isinstance(msg, dict):
        return "bad"
    kind = msg.get("type")
    if kind == "hello":
        return "hello"
    if kind == "disconnect":
        return "disconnect:%s" % (msg.get("reason") or "")
    envelope = msg.get("envelope_id")
    if envelope:
        try:
            send(json.dumps({"envelope_id": envelope}))
        except Exception as exc:  # noqa: BLE001  (an ack that could not be sent: Slack will send it again)
            log.warning("slack socket: ack failed: %s", exc)
    if kind != "events_api":
        return "other"                 # a slash command or a button: acknowledged, nothing to do
    payload = msg.get("payload")
    if isinstance(payload, dict):
        with _LOCK:
            STATE["events"] += 1
        try:
            # Never treated as a retry: the first delivery may be the one whose ack was lost, and
            # the event's own id already keeps it from being handled twice.
            handler(payload, False)
        except Exception as exc:  # noqa: BLE001  (one bad event must not close the connection)
            log.exception("slack socket: an event could not be handled: %s", exc)
    return "event"


def _default_connect(url: str):
    from websockets.sync.client import connect   # imported late: the brain starts without it
    return connect(url, open_timeout=20, close_timeout=5)


def session(conn, handler, stop) -> str:
    """Read one connection until Slack ends it, it breaks, or the brain stops. Returns why it ended:
    stop, a disconnect reason (warning, refresh_requested, link_disabled), or closed."""
    while not stop.is_set():
        try:
            raw = conn.recv(timeout=RECV_TIMEOUT_S)
        except TimeoutError:
            continue                   # quiet is normal; the library keeps the connection alive
        what = handle_frame(raw, conn.send, handler)
        if what == "hello":
            _set(state="connected", since=_now(), error="")
        elif what.startswith("disconnect:"):
            return what.split(":", 1)[1] or "disconnect"
    return "stop"


def run(stop, handler, connect=None, sleep=None) -> None:
    """Keep one connection open until `stop` is set. `connect(url)` returns an object with
    recv(timeout=), send(text) and close(); `sleep(seconds)` returns True when the brain is stopping."""
    connect = connect or _default_connect
    sleep = sleep or stop.wait
    pause = PAUSE_MIN_S
    while not stop.is_set():
        _set(state="connecting")
        url, why = open_url()
        if not url:
            _set(state="down", error=why, since=_now())
            log.warning("slack socket: could not open a connection: %s", why)
            if sleep(pause):
                break
            pause = min(PAUSE_MAX_S, pause * 2)
            continue
        ended = "closed"
        try:
            conn = connect(url)
        except ImportError:
            _set(state="down", error="the websockets package is not installed", since=_now())
            log.error("slack socket: the websockets package is not installed; Socket Mode is off")
            return
        except Exception as exc:  # noqa: BLE001
            _set(state="down", error="connect failed (%s)" % type(exc).__name__, since=_now())
            log.warning("slack socket: connect failed: %s", exc)
            if sleep(pause):
                break
            pause = min(PAUSE_MAX_S, pause * 2)
            continue
        with _LOCK:
            STATE["connects"] += 1
        try:
            ended = session(conn, handler, stop)
            pause = PAUSE_MIN_S        # a connection that worked resets the pause
        except Exception as exc:  # noqa: BLE001  (a closed or broken connection: open another)
            ended = "closed (%s)" % type(exc).__name__
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if ended == "stop":
            break
        if ended == "link_disabled":
            # Socket Mode was switched off for the app while the brain was connected.
            _set(state="down", error="Socket Mode is switched off for the app", since=_now())
            log.warning("slack socket: Socket Mode is switched off for the app; trying again in %d s", LINK_DISABLED_PAUSE_S)
            if sleep(LINK_DISABLED_PAUSE_S):
                break
            continue
        if ended.startswith("closed"):
            _set(state="down", error=ended, since=_now())
            if sleep(pause):
                break
            pause = min(PAUSE_MAX_S, pause * 2)
        # warning / refresh_requested: Slack asked for a fresh connection; open one at once
    _set(state="off", since=_now())


def start_thread(handler):
    stop = threading.Event()
    t = threading.Thread(target=run, args=(stop, handler), name="slack-socket", daemon=True)
    t.start()
    return t, stop

