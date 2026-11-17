"""Browser-session sender: drives the smrtPhone web app to send a text.

The fallback for when the public API is not usable on an account. Mechanics are
lifted from `src/mms_sender.py`, whose text leg is already proven live: the
Compose Message modal sends a plain SMS successfully. Only the IMAGE leg needed
the conversation iframe, and replies carry no image, so this stays on the modal.

Hard-won details this reuses rather than rediscovers:
  * Grant microphone permission at the CONTEXT level. The dialer's "Allow
    microphone" prompt appears a few minutes into a run and COVERS the entire
    compose UI, which silently timed out 21 consecutive sends on the first
    production run.
  * The inbox buttons are icon-only with no text or aria labels. The compose
    button is the middle of the Call / Message / Video trio at the top left and
    has to be targeted by POSITION.
  * The SPA is flaky under automation. Use domcontentloaded plus a fixed wait,
    never networkidle, and retry.

Limitation worth stating plainly: the compose modal sends from the account's
default caller ID. Per-conversation sticky sender numbers are an API-path
feature; on the session path every message leaves from the default number.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config, store

log = logging.getLogger(__name__)

BASE = "https://phone.smrt.studio"
INBOX_URL = f"{BASE}/inboxV2/"
STATE_FILE = Path(config.SMRTPHONE_STATE_FILE)

_lock = threading.Lock()
_session: Optional["_Session"] = None


@dataclass
class SendResult:
    ok: bool
    sms_id: str = ""
    error: str = ""
    status_code: int = 0


class _Session:
    """A long-lived browser so the worker does not pay startup per message."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=config.SESSION_HEADLESS)
        self.context = self.browser.new_context(
            storage_state=str(STATE_FILE),
            permissions=["microphone"],
            viewport={"width": 1440, "height": 900},
        )
        self.context.grant_permissions(["microphone"], origin=BASE)
        self.page = self.context.new_page()
        self.page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(4000)
        self.opened_at = time.time()

    def close(self) -> None:
        for closer in (self.context.close, self.browser.close, self._pw.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass


def _session_alive(sess: "_Session") -> bool:
    try:
        return "smrt" in sess.page.url
    except Exception:  # noqa: BLE001 - a dead page raises on attribute access
        return False


def _get_session() -> "_Session":
    global _session
    if _session is not None and _session_alive(_session):
        return _session
    if _session is not None:
        _session.close()
    _session = _Session()
    return _session


def shutdown() -> None:
    global _session
    with _lock:
        if _session is not None:
            _session.close()
            _session = None


def _dismiss_blocking_modals(page) -> None:
    """Clear the permission and announcement dialogs that cover the compose UI."""
    for label in ("I understand", "Cancel", "Got it", "Dismiss", "Close"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001 - absence is the normal case
            pass


def _open_compose(page) -> None:
    """Open the Compose Message modal.

    The button is icon-only with no accessible name, so it is targeted by its
    screen position: the middle of the Call / Message / Video trio at the top
    left. If the modal is already open this is a no-op.
    """
    if page.locator("textarea.messageTextArea").count():
        return
    page.mouse.click(80, 63)
    page.wait_for_timeout(1200)
    if not page.locator("textarea.messageTextArea").count():
        # Second chance: some builds expose a titled control.
        for name in ("Message", "New message", "Compose"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count():
                    btn.first.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:  # noqa: BLE001
                continue
    page.wait_for_selector("textarea.messageTextArea", timeout=15000)


def send_sms(to: str, body: str, from_number: str = "", timeout: int = 60) -> SendResult:
    """Send one text through the web app. Serialized: one browser, one at a time."""
    if not STATE_FILE.exists():
        return SendResult(
            False,
            error=f"no smrtPhone session at {STATE_FILE}; run _api/smrtphone_login.py to capture one",
        )
    digits = store.clean_phone(to)
    if len(digits) != 10:
        return SendResult(False, error=f"cannot dial {to!r}")

    with _lock:
        try:
            sess = _get_session()
            page = sess.page
            page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2500)
            _dismiss_blocking_modals(page)
            _open_compose(page)

            recipient = page.locator("input.prompt:not([placeholder*='user'])").first
            recipient.click()
            recipient.fill("")
            recipient.type(digits, delay=60)
            page.wait_for_timeout(1500)

            # Typing a number surfaces a contacts dropdown. Click the entry that
            # matches, else commit the raw number with Enter.
            picked = False
            try:
                option = page.locator(f"[class*='result']:has-text('{digits[-4:]}')").first
                if option.count() and option.is_visible():
                    option.click(timeout=3000)
                    picked = True
            except Exception:  # noqa: BLE001
                pass
            if not picked:
                recipient.press("Enter")
            page.wait_for_timeout(800)

            area = page.locator("textarea.messageTextArea").first
            area.click()
            area.fill(body)
            page.wait_for_timeout(400)

            send_btn = page.get_by_role("button", name="Send Message")
            if not send_btn.count():
                send_btn = page.locator("button:has-text('Send Message')")
            if not send_btn.count():
                return SendResult(False, error="Send Message button not found in compose modal")
            send_btn.first.click(timeout=10000)

            # Confirm. A click that silently did nothing is the failure mode the
            # first live run shipped with, so absence of the toast is a failure.
            try:
                page.wait_for_selector("text=Message sent successfully", timeout=15000)
            except Exception:  # noqa: BLE001
                remaining = ""
                try:
                    remaining = area.input_value()
                except Exception:  # noqa: BLE001
                    pass
                if remaining.strip() == body.strip():
                    return SendResult(False, error="send did not fire; message still in the box")
                log.warning("no success toast for %s; box cleared, treating as sent", digits)
            return SendResult(True, sms_id="")
        except Exception as exc:  # noqa: BLE001 - browser automation fails in many ways
            log.warning("session send to %s failed: %s", digits, exc)
            shutdown()  # a wedged page poisons every later send
            return SendResult(False, error=f"session send failed: {exc}"[:300])


def check_session() -> tuple[bool, str]:
    """Is a usable smrtPhone browser session on disk. Used by `doctor`."""
    if not STATE_FILE.exists():
        return False, f"no session file at {STATE_FILE} (run _api/smrtphone_login.py)"
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed"
    age_days = (time.time() - STATE_FILE.stat().st_mtime) / 86400
    return True, f"session file present, {age_days:.0f} day(s) old (expires without warning)"
