"""Worker: drains the event log, then drains the outbox.

Two loops, deliberately separate:

  * Event processing turns a stored webhook into CRM writes / escalations /
    drafts. Slow (LLM + CRM round trips) but never blocks the receiver.
  * Outbox draining is the ONLY place a message is actually sent. Quiet hours,
    per-number caps, pacing, and suppression are all enforced here, so there is
    exactly one code path that can put a text on a stranger's phone.

    python src/sms_agent/cli.py work            # one pass
    python src/sms_agent/cli.py work --loop     # continuous
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone

from . import config, engine, sender_pool, store, transport

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ events

def process_event(row: dict) -> dict:
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    try:
        result = engine.process(row["source"], payload)
    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the queue
        log.exception("event %s failed", row["id"])
        store.finish_event(row["id"], "error", str(exc)[:500])
        return {"action": "error", "error": str(exc)}
    store.finish_event(row["id"], json.dumps(result)[:2000])
    log.info("event %s (%s) -> %s", row["id"], row["event_type"], result.get("action"))
    return result


def drain_events(limit: int = 50) -> int:
    rows = store.pending_events(limit)
    for row in rows:
        process_event(dict(row))
    return len(rows)


# ------------------------------------------------------------------ outbox

def drain_outbox(limit: int = 25) -> dict:
    """Send what is due. Every guard lives here and nowhere else."""
    sent = held = skipped = failed = 0

    for row in store.due_outbox(limit):
        phone = row["phone"]

        reason = store.is_suppressed(phone)
        if reason:
            store.mark_outbox(row["id"], "cancelled", f"suppressed: {reason}")
            skipped += 1
            continue

        conv = store.get_conversation(phone) or {}
        if conv.get("state") not in ("active", None, ""):
            store.mark_outbox(row["id"], "cancelled", f"conversation {conv.get('state')}")
            skipped += 1
            continue

        if not sender_pool.within_quiet_hours(phone):
            nb = sender_pool.next_send_window(phone).isoformat(timespec="seconds")
            with store.tx() as c:
                c.execute("UPDATE outbox SET not_before=? WHERE id=?", (nb, row["id"]))
            held += 1
            continue

        # The CONVERSATION owns the sending number, not the queued row.
        #
        # The row's number is chosen when the message is staged. Two touches for
        # the same person staged in one batch are both chosen before either has
        # sent, so the pool hands out two different numbers and the thread
        # switches mid-conversation. That happened live: touch 3 went from
        # ...0296 and touch 4 from ...0270 an hour later, to one owner. Two
        # numbers texting one person about one house is precisely the pattern a
        # sticky sender exists to avoid.
        #
        # Reading it here instead means the first send fixes the number for the
        # whole thread, whatever was stamped on the row.
        from_number = (
            (conv or {}).get("from_number")
            or row["from_number"]
            or sender_pool.assign(
                phone,
                (store.lookup_phone(phone) or {}).get("context", {}).get("assigned_name", ""),
            )
        )
        if not from_number:
            store.mark_outbox(row["id"], "failed", "no sending number available")
            failed += 1
            continue

        ok, why = sender_pool.available(from_number)
        if not ok:
            log.info("holding outbox %s: %s (%s)", row["id"], why, from_number)
            held += 1
            continue

        if config.DRY_RUN:
            log.info("DRY_RUN send %s -> %s: %s", from_number, phone, row["body"][:100])
            store.mark_outbox(row["id"], "sent", "dry run")
            store.add_message(phone, "out", row["body"], from_number, author="ai",
                              intent=row["intent"], confidence=row["confidence"])
            store.record_send(from_number, phone)
            sent += 1
            continue

        result = transport.send(phone, row["body"], from_number)
        if result.ok:
            store.mark_outbox(row["id"], "sent")
            store.add_message(phone, "out", row["body"], from_number, sms_id=result.sms_id,
                              author="ai", intent=row["intent"], confidence=row["confidence"])
            store.record_send(from_number, phone)
            store.update_conversation(phone, from_number=from_number)
            sent += 1
            log.info("sent %s -> %s (%s)", from_number, phone, result.sms_id or "no id")

            # Count it in Sift, so the record itself shows how many touches it
            # has had. Deliberately AFTER the send and wrapped: a counter is
            # bookkeeping and must never be able to fail a message that already
            # left, nor stop the queue behind it.
            record_uuid = (conv or {}).get("record_uuid") or ""
            if record_uuid:
                from . import crm

                bumped = crm.bump_sms_attempts(record_uuid)
                if bumped.get("error"):
                    log.warning("sms_attempts not incremented on %s: %s",
                                record_uuid[:8], bumped["error"])
        else:
            # smrtPhone refusing on its own Do Not Text list is the one send
            # error that is an answer rather than a fault. We cannot read that
            # list through the API, so a rejected send is the only way we ever
            # learn a number is on it. Mirror it locally, or every future
            # campaign queues the same number and burns the same rejection.
            if "do not text" in (result.error or "").lower():
                store.suppress(phone, "smrtphone do-not-text list")
                store.cancel_queued(phone, "on the do-not-text list")
                store.mark_outbox(row["id"], "cancelled", result.error)
                skipped += 1
                log.info("suppressed %s: on smrtPhone's do-not-text list", phone)
                continue

            store.mark_outbox(row["id"], "queued" if row["attempts"] < 3 else "failed", result.error)
            failed += 1
            log.warning("send failed %s -> %s: %s", from_number, phone, result.error)

        # Human-ish spacing between sends so a burst does not look like a burst.
        time.sleep(random.uniform(1.5, 4.0))

    return {"sent": sent, "held": held, "skipped": skipped, "failed": failed}


# -------------------------------------------------------------------- loop

_last_reconcile = 0.0


def flush_escalations() -> int:
    """Post handoffs whose burst has settled.

    One notification per conversation carrying the WHOLE burst, rather than one
    per inbound text repeating the same transcript.
    """
    from . import crm, escalate

    posted = 0
    for row in store.due_escalations():
        phone = row["phone"]
        record_uuid = row.get("record_uuid") or ""
        thread = store.thread(phone)
        inbound = [m["body"] for m in thread if m.get("direction") == "in"]
        context = (store.lookup_phone(phone) or {}).get("context") or {}
        if not context and record_uuid:
            context = crm.deal_context(record_uuid)

        ok = escalate.hot_lead(
            phone,
            "\n".join(inbound[-4:]) or "(no message body)",
            row.get("intent") or "INTERESTED",
            context,
            thread,
            record_uuid,
            note="",
        )
        if ok:
            store.mark_escalation_posted(phone)
            posted += 1
            log.info("handed off %s to %s", phone, config.HANDOFF_NAME)
        else:
            log.warning("handoff post failed for %s; will retry next pass", phone)
    return posted


_last_repin = 0.0
REPIN_INTERVAL = 300

_campaign_thread = None
_campaign_result: dict = {}


def _start_campaign_thread() -> None:
    """Kick off the daily build if it is not already running.

    One at a time, always. `scheduler.run` is itself guarded by a once-a-day
    marker, but a second thread starting mid-build would read that marker before
    the first thread had claimed the day and queue the batch twice.
    """
    global _campaign_thread

    if _campaign_thread is not None and _campaign_thread.is_alive():
        return

    def _run() -> None:
        from . import scheduler

        try:
            out = scheduler.run()
            if out.get("released"):
                log.info("campaign started: %s texts", out["released"])
                _campaign_result.update(out)
            elif out.get("skipped") not in (None, "already ran today"):
                log.info("campaign skipped: %s", out["skipped"])
        except Exception:  # noqa: BLE001 - a bad build must never stop replies
            log.exception("campaign scheduler failed")

    _campaign_thread = threading.Thread(target=_run, name="campaign", daemon=True)
    _campaign_thread.start()


def run_once(with_reconcile: bool = True) -> dict:
    from . import scheduler

    scheduler.heartbeat()

    # The morning build runs on its own thread.
    #
    # Vetting a candidate costs a CRM read and reisift throttles hard, so a full
    # day's batch takes roughly twenty minutes to assemble. Inline, that is
    # twenty minutes where no reply is classified and nothing in the outbox
    # drains: the busiest part of the morning, spent building rather than
    # sending. It only runs once a day, so a thread is cheap, and the build
    # writes through the same store as everything else (SQLite in WAL, one
    # writer at a time) so the two loops do not corrupt each other.
    _start_campaign_thread()

    events = drain_events()
    handoffs = flush_escalations()
    outbox = drain_outbox()
    result = {"events": events, "handoffs": handoffs, **outbox}
    if _campaign_result.get("released"):
        result["campaign_released"] = _campaign_result.pop("released")

    # A hot lead the prospector cannot open is a hot lead we did not hand off.
    global _last_repin
    if config.HANDOFF_ASSIGNEE_UUID and time.time() - _last_repin >= REPIN_INTERVAL:
        _last_repin = time.time()
        try:
            from . import crm

            repin = crm.reassert_handoff_assignment(config.HANDOFF_ASSIGNEE_UUID)
            if repin.get("reassigned"):
                result["repinned"] = repin["reassigned"]
        except Exception:  # noqa: BLE001 - a bookkeeping sweep must not stop sends
            log.exception("handoff re-pin sweep failed")

    # Backstop poll. smrtPhone gives no delivery guarantee, so a dropped
    # webhook is a lost reply with nothing to notice it. This catches those.
    global _last_reconcile
    if with_reconcile and config.RECONCILE_INTERVAL:
        if time.time() - _last_reconcile >= config.RECONCILE_INTERVAL:
            _last_reconcile = time.time()
            try:
                from . import reconcile

                rec = reconcile.run(pages=1)
                if rec.get("replayed"):
                    log.info("reconcile recovered %s missed inbound", rec["replayed"])
                    result["reconciled"] = rec["replayed"]
                elif rec.get("error"):
                    log.warning("reconcile failed: %s", rec["error"])
            except Exception:  # noqa: BLE001 - never let the backstop stop the loop
                log.exception("reconcile pass failed")
    return result


def run_forever(interval: int = 20) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    store.init()
    log.info(
        "worker up | phase=%s dry_run=%s pool=%s", config.PHASE, config.DRY_RUN, len(sender_pool.pool())
    )
    while True:
        try:
            result = run_once()
            if any(result.values()):
                log.info("pass: %s", result)
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("worker pass failed")
        time.sleep(interval)
