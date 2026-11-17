"""Replay real homeowner replies through the classifier. Sends nothing.

smrtPhone already syncs inbound SMS into the CRM as `owner.sms.received`
activity events, threaded to the record by phone. That means real replies from
real Knox and Blount homeowners exist right now, before a single webhook is
wired.

This pulls them and runs them through the live classifier, which is the only
honest way to answer three questions before going live:

  1. Does the classifier read actual replies correctly, or only the tidy
     examples I wrote for it?
  2. Where should the confidence floor sit? Guessing 0.80 is a guess.
  3. Which numbers should already be suppressed and are not?

Read-only by default. `--apply` writes the phone-status and suppression
consequences for the terminal intents only, and even then respects DRY_RUN.

    python src/sms_agent/cli.py backfill --queue output/mms_send_queue.csv
    python src/sms_agent/cli.py backfill --queue ... --apply --commit
"""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import classify, config, crm, store

log = logging.getLogger(__name__)

SMS_EVENTS = ["owner.sms.received", "owner.sms.sent"]


@dataclass
class Reply:
    record_uuid: str
    phone: str
    texted_phone: str
    body: str
    timestamp: str
    intent: str = ""
    confidence: float = 0.0
    source: str = ""
    rationale: str = ""

    @property
    def from_the_number_we_texted(self) -> bool:
        return self.phone == self.texted_phone


@dataclass
class Result:
    replies: list[Reply] = field(default_factory=list)
    records_checked: int = 0
    records_with_replies: int = 0
    outbound_seen: int = 0
    errors: list[str] = field(default_factory=list)


def _extract_sms(event: dict) -> dict:
    """Find the {sms:{...}} payload regardless of which key wraps it."""
    if isinstance(event.get("sms"), dict):
        return event["sms"]
    for key in ("payload", "event_data", "data", "meta", "extra", "details", "body"):
        value = event.get(key)
        if isinstance(value, dict):
            if isinstance(value.get("sms"), dict):
                return value["sms"]
            if "message" in value or "origin_number" in value:
                return value
    for value in event.values():
        if isinstance(value, dict) and isinstance(value.get("sms"), dict):
            return value["sms"]
    return {}


def load_targets(queue_csv: Path) -> list[tuple[str, str]]:
    """(record_uuid, phone we texted) from a send queue or recipients CSV."""
    phone_cols = ("to_number", "best_dial_phone", "phone", "number")
    out = []
    with queue_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            uuid = (row.get("record_uuid") or row.get("uuid") or "").strip()
            phone = next((row[c] for c in phone_cols if row.get(c)), "")
            if uuid:
                out.append((uuid, store.clean_phone(phone)))
    return out


def collect(targets: list[tuple[str, str]], limit: int = 0) -> Result:
    result = Result()
    client = crm.client()
    if client is None:
        result.errors.append(crm.unavailable_reason())
        return result

    for uuid, texted in targets[: limit or len(targets)]:
        result.records_checked += 1
        try:
            events = client.get_activity_log(uuid, event_types=SMS_EVENTS, limit=100, max_pages=3)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the sweep
            result.errors.append(f"{uuid[:8]}: {str(exc)[:120]}")
            continue

        found = 0
        for event in events:
            etype = (event.get("event_type") or "").lower()
            if etype == "owner.sms.sent":
                result.outbound_seen += 1
                continue
            if etype != "owner.sms.received":
                continue
            sms = _extract_sms(event)
            body = (sms.get("message") or "").strip()
            if not body:
                continue
            found += 1
            result.replies.append(
                Reply(
                    record_uuid=uuid,
                    phone=store.clean_phone(sms.get("origin_number")),
                    texted_phone=texted,
                    body=body,
                    timestamp=str(event.get("timestamp") or "")[:19],
                )
            )
        if found:
            result.records_with_replies += 1
    return result


def classify_all(result: Result) -> Result:
    """Run the live classifier over every reply. This is the point of the exercise."""
    for reply in result.replies:
        verdict = classify.classify(reply.body)
        reply.intent = verdict.intent
        reply.confidence = verdict.confidence
        reply.source = verdict.source
        reply.rationale = verdict.rationale
    return result


def apply_terminal(result: Result, commit: bool = False) -> list[str]:
    """Write the consequences of the two terminal intents. Nothing else.

    Deliberately narrow. OPT_OUT and WRONG_NUMBER are the intents whose
    consequence is unambiguous and overdue. INTERESTED replies from months ago
    are not escalated, because paging a prospector about a conversation that
    went cold in June is noise, not a lead.
    """
    actions = []
    for reply in result.replies:
        if reply.intent not in ("OPT_OUT", "WRONG_NUMBER"):
            continue
        phone = reply.phone or reply.texted_phone
        if not phone:
            continue
        if store.is_suppressed(phone):
            continue
        if not commit:
            actions.append(f"WOULD suppress {phone} ({reply.intent})")
            continue

        if reply.intent == "OPT_OUT":
            store.suppress(phone, "opt_out")
            res = crm.add_tags(reply.record_uuid, [config.TAG_OPT_OUT])
            actions.append(f"{phone}: suppressed opt_out, tag {'error' if res.get('error') else 'ok'}")
        else:
            store.suppress(phone, "wrong_number")
            res = crm.set_phone_status(reply.record_uuid, phone, "WRONG")
            state = "dry run" if res.get("_dry_run") else ("error" if res.get("error") else "ok")
            actions.append(f"{phone}: suppressed wrong_number, phone status {state}")
    return actions


def report(result: Result) -> str:
    lines = [
        f"records checked      {result.records_checked}",
        f"with >=1 reply       {result.records_with_replies}",
        f"inbound replies      {len(result.replies)}",
        f"our outbound seen    {result.outbound_seen}  (confirms reisift/smrtPhone threading)",
    ]
    if not result.replies:
        lines.append("")
        lines.append("No inbound replies found. Either nobody replied, or the CRM token")
        lines.append("cannot read the activity log. Check the errors below before")
        lines.append("concluding the market is quiet.")
        if result.errors:
            lines.append("")
            for err in result.errors[:10]:
                lines.append(f"  error: {err}")
        return "\n".join(lines)

    counts = Counter(r.intent for r in result.replies)
    lines.append("")
    lines.append("classification:")
    for intent, n in counts.most_common():
        confs = [r.confidence for r in result.replies if r.intent == intent]
        avg = sum(confs) / len(confs) if confs else 0
        lines.append(f"  {intent:15} {n:4}   mean confidence {avg:.2f}")

    by_llm = [r for r in result.replies if r.source == "llm"]
    if by_llm:
        low = [r for r in by_llm if r.confidence < config.CONFIDENCE_FLOOR]
        lines.append("")
        lines.append(
            f"model-classified: {len(by_llm)}, of which {len(low)} fall under the"
            f" {config.CONFIDENCE_FLOOR:.2f} floor and would draft rather than auto-send"
        )

    other = [r for r in result.replies if r.phone and not r.from_the_number_we_texted]
    if other:
        lines.append(f"replies from a DIFFERENT number on the record: {len(other)}")

    lines.append("")
    lines.append("every reply:")
    for reply in sorted(result.replies, key=lambda r: r.timestamp):
        flag = "" if reply.from_the_number_we_texted else "  [different number]"
        lines.append(
            f"  {reply.timestamp}  {reply.phone or '?':10}  "
            f"{reply.intent:14} {reply.confidence:.2f} ({reply.source}){flag}"
        )
        lines.append(f"      {reply.body[:150]}")
        if reply.rationale:
            lines.append(f"      why: {reply.rationale[:130]}")
    return "\n".join(lines)


def save(result: Result, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records_checked": result.records_checked,
        "records_with_replies": result.records_with_replies,
        "outbound_seen": result.outbound_seen,
        "errors": result.errors,
        "replies": [
            {
                "record_uuid": r.record_uuid,
                "phone": r.phone,
                "texted_phone": r.texted_phone,
                "timestamp": r.timestamp,
                "body": r.body,
                "intent": r.intent,
                "confidence": r.confidence,
                "source": r.source,
                "rationale": r.rationale,
            }
            for r in result.replies
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
