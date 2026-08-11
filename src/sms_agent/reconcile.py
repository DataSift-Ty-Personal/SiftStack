"""Catch inbound replies the webhook never delivered.

smrtPhone documents no retry semantics and purges its own webhook logs after 30
days. If a delivery is dropped, a homeowner's reply simply never reaches us and
there is nothing to notice it. That is a silent lost lead, which is the failure
mode this whole system exists to prevent.

So we poll the SMS log as a backstop. Every pass pulls recent messages, drops
anything already in the event table, and pushes the rest through the exact same
engine path a webhook would have taken. Same dedupe key, so a webhook that
arrives late is still only processed once.

Two useful things come out of the log that the webhook does not carry:

  * `podioId` is the reisift link for the message. That is smrtPhone's own
    record association, which is more trustworthy than searching by phone.
  * outbound rows show texts sent by hand from the inbox, which is how the
    agent learns a human is already in the thread.

    python src/sms_agent/cli.py reconcile          # one pass
    python src/sms_agent/cli.py reconcile --hours 24
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from . import store

log = logging.getLogger(__name__)

# How far back a matching body counts as the same text rather than a repeat.
# Comfortably wider than the poll interval, so a webhook and the backstop can
# never both claim one message.
DEDUPE_WINDOW_MINUTES = 90

BASE = "https://phone.smrt.studio"
LOG_PATH = "/logs/sms/filtered"
COLUMNS = ["id", "created_at", "fromNum", "toNum", "content", "direction", "price", "podioId"]
_OWNER_LINK = re.compile(r"/records/owners/([0-9a-f-]{36})", re.I)
_PROPERTY_LINK = re.compile(r"/records/properties/([0-9a-f-]{36})", re.I)


def _session():
    from . import numbers_sync

    return numbers_sync._session()


def _clean(value) -> str:
    """Log cells are HTML fragments; the content column is entity-encoded."""
    text = html.unescape(str(value or ""))
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_log(pages: int = 2, per_page: int = 200) -> list[dict]:
    """Recent SMS log rows, newest first."""
    session = _session()
    rows: list[dict] = []
    for page in range(pages):
        form = {
            "draw": "1", "start": str(page * per_page), "length": str(per_page),
            "search[value]": "", "search[regex]": "false",
            "order[0][column]": "0", "order[0][dir]": "desc",
        }
        for i, col in enumerate(COLUMNS):
            form[f"columns[{i}][data]"] = col
            form[f"columns[{i}][name]"] = col
            form[f"columns[{i}][searchable]"] = "true"
            form[f"columns[{i}][orderable]"] = "true"
            form[f"columns[{i}][search][value]"] = ""
            form[f"columns[{i}][search][regex]"] = "false"
        resp = session.post(BASE + LOG_PATH, data=form, timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from the SMS log")
        try:
            page_rows = resp.json().get("data") or []
        except ValueError:
            raise RuntimeError("session expired; re-run _api/smrtphone_login.py") from None
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


def record_uuid_from(row: dict) -> str:
    """smrtPhone's own CRM association for the message, if it carries one.

    The log's `podioId` cell is the "go to item" link. It usually points at an
    OWNER rather than a property, so a property link is preferred when present.
    """
    link = str(row.get("podioId") or "")
    prop = _PROPERTY_LINK.search(link)
    return prop.group(1) if prop else ""


def owner_uuid_from(row: dict) -> str:
    match = _OWNER_LINK.search(str(row.get("podioId") or ""))
    return match.group(1) if match else ""


def run(pages: int = 2, apply: bool = True) -> dict:
    """One reconciliation pass. Returns what it found and what it replayed."""
    from . import engine

    try:
        rows = fetch_log(pages=pages)
    except Exception as exc:  # noqa: BLE001 - an expired session is the usual cause
        log.warning("reconcile could not read the SMS log: %s", exc)
        return {"error": str(exc)[:200]}

    seen_inbound = replayed = already = outbound_seen = 0
    seen_content: set = set()
    results: list[dict] = []

    for row in rows:
        direction = (row.get("direction") or "").lower()
        sms_id = str(row.get("id") or "")
        if not sms_id:
            continue

        if direction == "outbound":
            outbound_seen += 1
            continue

        seen_inbound += 1
        payload = {
            "event": "smsIncoming",
            "smsId": sms_id,
            "from": store.clean_phone(row.get("fromNum")),
            "to": _clean(row.get("toNum")),
            "message": _clean(row.get("content")),
            "source": "reconcile",
        }
        # The id key alone is NOT enough. smrtPhone identifies the same text two
        # different ways: the webhook posts a uuid ("b6df1664-...") and this log
        # returns an internal integer ("129829667"). The keys never collided, so
        # the backstop replayed every reply the webhook had already delivered:
        # doubled classification spend, doubled CRM writes, and threads that
        # read as though the seller said everything twice.
        #
        # So content is the real identity. A genuine repeat inside the window is
        # swallowed, which costs nothing (the same text yields the same
        # disposition), while a genuinely new message differs in body or falls
        # outside the window and still gets through.
        key = f"smrtphone:smsIncoming:{sms_id}"

        # Two guards, because they cover different windows. The stored-message
        # check catches anything the webhook already handled. The in-pass set
        # catches the same text appearing twice in THIS batch, which the stored
        # check cannot see: events are written here but only turn into messages
        # later, when the worker processes them, so nothing is on disk yet.
        content_key = (payload["from"], payload["message"].strip().lower())
        if content_key in seen_content:
            already += 1
            continue
        if store.recent_inbound_exists(payload["from"], payload["message"], DEDUPE_WINDOW_MINUTES):
            already += 1
            continue
        seen_content.add(content_key)

        if not apply:
            # A preview must NOT consume the dedupe key, or the real run that
            # follows would treat everything as already-seen and skip it.
            if store.event_exists(key):
                already += 1
            else:
                replayed += 1
                results.append({"phone": payload["from"], "action": "would replay"})
            continue

        event_id = store.record_event("smrtphone", "smsIncoming", key, payload)
        if event_id is None:
            already += 1
            continue

        try:
            outcome = engine.process("smrtphone", payload)
            store.finish_event(event_id, str(outcome.get("action"))[:200])
            results.append({"phone": payload["from"], "action": outcome.get("action")})
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            store.finish_event(event_id, "error", str(exc)[:300])
        replayed += 1
        log.info("reconcile replayed missed inbound from %s", payload["from"])

    return {
        "rows_scanned": len(rows),
        "inbound_seen": seen_inbound,
        "outbound_seen": outbound_seen,
        "already_had": already,
        "replayed": replayed,
        "results": results,
    }
