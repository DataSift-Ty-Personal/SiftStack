"""Prospector handoff.

Posts to the Slack (or Discord) channel the prospectors watch. Self-contained
rather than importing src/slack_notifier.py so the receiver can be deployed on
its own without the rest of the repo.

A note on the ceiling here: an incoming webhook can only post a message. If you
want the prospector to hit "I've got this" / "not a lead" / "wrong number" from
the channel and have that write back to the CRM, that needs a real Slack app
with interactive components, not a webhook URL. This module is deliberately
shaped so that upgrade is a swap of `_post`, not a rewrite.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from . import config, store

log = logging.getLogger(__name__)

# The only alert kinds allowed to reach the channel. A live seller, and the
# daily campaign summary that was explicitly asked for. Everything else is
# bookkeeping and belongs in the digest.
ALWAYS_POST = {"handoff", "campaign"}

RECORD_URL = "https://app.reisift.io/records/properties/{uuid}/details"


def _is_discord(url: str) -> bool:
    return "discord.com" in (url or "")


def _post(text: str, blocks: Optional[list] = None) -> bool:
    url = config.SLACK_WEBHOOK_URL
    if not url:
        log.warning("no SMS_AGENT_SLACK_WEBHOOK configured; escalation not delivered")
        return False
    payload: dict = {"text": text}
    if blocks and not _is_discord(url):
        payload["blocks"] = blocks
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        log.warning("escalation post failed: %s", exc)
        return False
    if resp.status_code >= 300:
        log.warning("escalation post failed: HTTP %s %s", resp.status_code, resp.text[:160])
        return False
    return True


def _fmt_phone(p: str) -> str:
    d = store.clean_phone(p)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def hot_lead(
    phone: str,
    inbound: str,
    intent: str,
    context: Optional[dict] = None,
    thread: Optional[list[dict]] = None,
    record_uuid: str = "",
    note: str = "",
) -> bool:
    """The one that matters: a positive response needs a human on it now."""
    ctx = context or {}
    who = ctx.get("owner_first") or "Unknown owner"
    where = ", ".join(x for x in (ctx.get("street"), ctx.get("city"), ctx.get("state")) if x)

    mention = f"<@{config.HANDOFF_SLACK_ID}> " if config.HANDOFF_SLACK_ID else ""
    lines = [
        f"{mention}*{config.HANDOFF_NAME}: yours. Call within 5 minutes.*",
        f"*Positive SMS reply - {who}*",
        f"{_fmt_phone(phone)}" + (f"  {where}" if where else ""),
        f"> {inbound.strip()[:400]}",
    ]
    detail = []
    for key, label in (
        ("beds", "bd"),
        ("baths", "ba"),
        ("sqft", "sqft"),
        ("year_built", "built"),
    ):
        if ctx.get(key):
            detail.append(f"{ctx[key]} {label}")
    if ctx.get("vacant"):
        detail.append("vacant")
    if detail:
        lines.append(", ".join(detail))
    lines.append(f"Read: *{intent}*" + (f" - {note}" if note else ""))
    if record_uuid:
        lines.append(RECORD_URL.format(uuid=record_uuid))

    text = "\n".join(lines)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    if thread:
        convo = "\n".join(
            f"{'them' if m.get('direction') == 'in' else 'us'}: {m.get('body', '')}"
            for m in thread[-6:]
        )
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"```{convo[:2500]}```"}]}
        )
    return _post(text, blocks)


def draft_for_approval(
    phone: str,
    inbound: str,
    proposed: str,
    confidence: float,
    reason: str = "",
    record_uuid: str = "",
    blocked: Optional[list[str]] = None,
) -> bool:
    """Phase 3: every reply goes here before anything is sent."""
    lines = [
        f"*Draft reply - {_fmt_phone(phone)}* (confidence {confidence:.0%})",
        f"> them: {inbound.strip()[:300]}",
        f"> us:   {proposed.strip()[:300]}" if proposed else "> us:   (nothing proposed)",
    ]
    if reason:
        lines.append(f"_{reason}_")
    if blocked:
        lines.append(f":warning: blocked by validator: {', '.join(blocked)}")
    if record_uuid:
        lines.append(RECORD_URL.format(uuid=record_uuid))
    lines.append(f"Approve with: `python src/sms_agent/cli.py approve {store.clean_phone(phone)}`")
    text = "\n".join(lines)
    return _post(text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def alert(title: str, detail: str = "", record_uuid: str = "", kind: str = "ops") -> bool:
    """Something a human should see that is not itself a lead.

    The channel is for interested parties (Ty, 2026-08-11). Opt-out bookkeeping
    was posting there and burying the only messages that matter: a prospector
    who scrolls past four housekeeping notes stops reading the fifth, and the
    fifth is the seller. Anything not on ALWAYS_POST is logged and left for the
    digest instead of being posted.

    `kind="campaign"` and `kind="handoff"` are the exceptions: one is the daily
    "here is what went out" that was explicitly asked for, the other is a live
    seller.
    """
    if config.SLACK_INTERESTED_ONLY and kind not in ALWAYS_POST:
        log.info("slack suppressed (%s): %s | %s", kind, title, detail[:200])
        store.set_meta(f"last_notice_{kind}", f"{title} :: {detail[:300]}")
        return True

    text = f"*{title}*" + (f"\n{detail}" if detail else "")
    if record_uuid:
        text += f"\n{RECORD_URL.format(uuid=record_uuid)}"
    return _post(text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def unknown_number(phone: str, inbound: str) -> bool:
    """Somebody texted a pool number and we have no record for them.

    Worth surfacing rather than dropping: it is usually a reply from a second
    line on a record we already know, or a referral.
    """
    return alert(
        f"Inbound from an unmapped number - {_fmt_phone(phone)}",
        f"> {inbound.strip()[:400]}\n_No record mapped to this number; nothing was sent._",
    )
