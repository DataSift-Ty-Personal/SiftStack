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
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import store

log = logging.getLogger(__name__)

# Retained for callers and tests that still pass it; `store.recent_inbound_exists`
# no longer bounds its search by time. A 90 minute window put an edge exactly
# where this poller lands and produced 300 duplicated inbound groups in
# production, each answered twice. See that function for the full story.
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


WATERMARK_KEY = "reconcile_outbound_high_id"


def _above_watermark(sms_id: str) -> bool:
    """Is this outbound row newer than anything we have already considered?

    Ids in this log are increasing integers, which is the only ordering the
    rows carry (created_at comes back null). Anything at or below the mark has
    already been through here, or predates the feature entirely.
    """
    if not sms_id.isdigit():
        return True
    return int(sms_id) > int(store.get_meta(WATERMARK_KEY) or 0)


def _raise_watermark(rows: list[dict]) -> None:
    """Move the mark to the newest outbound id in this batch.

    Called after the sweep, so a crash mid-pass re-reads rather than skips.
    """
    ids = [int(r["id"]) for r in rows
           if str(r.get("id") or "").isdigit()
           and (r.get("direction") or "").lower() == "outbound"]
    if not ids:
        return
    high = max(ids)
    if high > int(store.get_meta(WATERMARK_KEY) or 0):
        store.set_meta(WATERMARK_KEY, str(high))


def _replay_outbound(row: dict, seen: set, apply: bool) -> str:
    """Recover a human takeover the `smsOutgoing` webhook never delivered.

    Returns "replayed", "ours" or "skipped".

    This is the half of the backstop that was promised in the docstring above
    and never written: the loop simply skipped outbound rows. smrtPhone
    documents no retry semantics, so a dropped `smsOutgoing` meant a rep could
    take over a thread and the agent would never learn it.

    THE LEDGER CHECK HERE IS DELIBERATELY UNBOUNDED, unlike the tight window
    `engine._provably_ours` uses on the live webhook path. That asymmetry is the
    whole safety of this function. A pass scans hours of history, so a ten
    minute window would classify every one of our own older sends as a human
    takeover and the campaign would silently pause itself on the first sweep.
    An `outbox` row we marked 'sent' is a record we wrote, not an inference, so
    it needs no recency bound.

    The cost is that a rep hand sending copy identical to ours is swallowed here.
    The webhook path still catches that case in real time, and the alternative
    is mass false takeovers on every pass.
    """
    from . import engine

    sms_id = str(row.get("id") or "")
    phone = store.clean_phone(_clean(row.get("toNum")))
    body = _clean(row.get("content"))
    from_number = _clean(row.get("fromNum"))
    if not phone or not body:
        return "skipped"

    # NOTE: this log returns `created_at: null` on every row (verified live
    # 2026-08-28), so there is no timestamp to filter on. Recency comes from the
    # id, which is an increasing integer, via the high water mark below.
    #
    # A dropped webhook is a recent event. Without this bound the first pass
    # after any deploy re-reads weeks of history and pauses threads over texts
    # everyone has long since moved past: measured at 33 on real data, and 408
    # before the cold-thread escape. The mark is set on the first ever pass so
    # that pass acts on nothing, which is the correct behaviour for history.
    if not _above_watermark(sms_id):
        return "skipped"

    key_content = (phone, body.strip().lower())
    if key_content in seen:
        return "skipped"

    if store.we_sent_body(phone, body, from_number=from_number):
        seen.add(key_content)
        return "ours"

    if store.recent_outbound_exists(phone, body):
        return "skipped"

    conv = store.get_conversation(phone) or {}
    if (conv.get("state") or "active") != "active":
        return "skipped"

    # THE COLD-THREAD ESCAPE, and without it this function is a wrecking ball.
    #
    # A missing ledger row does not prove a human typed the message. It also
    # happens whenever a send leaves from somewhere other than this process,
    # and that is not hypothetical: a second worker on a workstation sent 608
    # texts whose outbox rows lived in a different database entirely. A first
    # cut of this sweep flagged 408 of 481 outbound rows as takeovers on real
    # data, which would have paused most of the campaign in one pass.
    #
    # So it uses the same tie breaker as the live webhook path. A message into
    # a thread that has never replied is outreach, whoever queued it. A message
    # into a thread that is already talking is a person stepping in.
    if not store.has_inbound_before(phone, store.now()):
        return "ours"

    seen.add(key_content)
    payload = {
        "event": "smsOutgoing",
        "smsId": sms_id,
        "from": from_number,
        "to": phone,
        "message": body,
        "source": "reconcile",
        "userName": _clean(row.get("user")) or "",
    }
    if not apply:
        return "replayed"

    key = f"smrtphone:smsOutgoing:{sms_id}"
    event_id = store.record_event("smrtphone", "smsOutgoing", key, payload)
    if event_id is None:
        return "skipped"
    try:
        outcome = engine.process("smrtphone", payload)
        store.finish_event(event_id, str(outcome.get("action"))[:200])
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
        store.finish_event(event_id, "error", str(exc)[:300])
        return "skipped"
    if outcome.get("action") != "human_takeover":
        return "ours"
    log.info("reconcile recovered a missed takeover on %s", phone)
    return "replayed"


def run(pages: int = 2, apply: bool = True) -> dict:
    """One reconciliation pass. Returns what it found and what it replayed."""
    from . import engine

    try:
        rows = fetch_log(pages=pages)
    except Exception as exc:  # noqa: BLE001 - an expired session is the usual cause
        log.warning("reconcile could not read the SMS log: %s", exc)
        return {"error": str(exc)[:200]}

    # First ever pass: adopt the log's current position and act on no outbound
    # at all. Everything already in the log predates this feature, so replaying
    # it would pause threads over texts everyone moved past days ago. Inbound
    # replay is unaffected, since a missed reply is worth recovering whenever
    # we notice it.
    priming = not store.get_meta(WATERMARK_KEY)
    if priming:
        if apply:
            _raise_watermark(rows)
            log.info("reconcile: outbound backstop primed at the current log position; "
                     "history will not be replayed")
        else:
            log.info("reconcile: outbound backstop is unprimed; the first real pass "
                     "will adopt the log position and replay nothing")

    seen_inbound = replayed = already = outbound_seen = 0
    outbound_replayed = ours_skipped = 0
    seen_content: set = set()
    seen_content_out: set = set()
    results: list[dict] = []

    for row in rows:
        direction = (row.get("direction") or "").lower()
        sms_id = str(row.get("id") or "")
        if not sms_id:
            continue

        if direction == "outbound":
            outbound_seen += 1
            if priming:
                already += 1
                continue
            outcome = _replay_outbound(row, seen_content_out, apply)
            if outcome == "replayed":
                outbound_replayed += 1
                results.append({"phone": store.clean_phone(row.get("toNum")),
                                "action": "human_takeover (recovered)"})
            elif outcome == "ours":
                ours_skipped += 1
            else:
                already += 1
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

    if apply:
        _raise_watermark(rows)

    return {
        "rows_scanned": len(rows),
        "inbound_seen": seen_inbound,
        "outbound_seen": outbound_seen,
        "outbound_replayed": outbound_replayed,
        "ours_skipped": ours_skipped,
        "already_had": already,
        "replayed": replayed,
        "results": results,
    }
