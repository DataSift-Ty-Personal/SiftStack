"""The brain's own ledger as a KPI source: what it paid for, and the mail pieces it handed to a printer.

Two tables hold the brain's spend, and one dollar can sit in both:
  * ap_events.usd (autopilot/ledger.py): every paid action of a live stage, with its record and its stage (a
    trace, a score, a mail piece, a transcription, a property check);
  * the spend table (store.add_spend): Claude turns, the voice rewrite, a workflow's own vendor calls, AND a copy of
    every paid stage action, written by ctx.settle as "autopilot <stage>: <what>" for the brain's daily cap.
Every paid settle is paired with a ledger event carrying the same dollars (checked when this was built: the native
trace, the trace waterfall, scoring, mail, heir and obituary letters, the call screen, lead notes, the property
check and new-hire grading), so the spend rows that start "autopilot " are skipped here. Read both and every dollar
the autopilot spent would count twice.

Mail: each ap_events row with the action "mail" is one piece the autopilot handed to a printer (a SiftMail launch, a
vendor send, or a CSV a person uploaded). The ledger holds live work only, so a planned piece is never a mailed one.
Its cost is the spend event of the same row, never a second figure.

Both are the brain's own memory: nothing to connect, nothing to hold, and no request to anyone.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import store
from . import OWN, Adapter, Batch, Event, Provides, tool_key

VENDORS = ("smartskip", "tracerfy", "trestle", "enformion", "serpapi", "serper", "firecrawl", "scrapfly", "openrouter",
           "zillow", "openwebninja", "claude")
TOOL_BY_ACTION = {"score": "trestle", "native_trace": "datasift", "native_refresh": "datasift"}


def _when(text) -> datetime | None:
    """Ledger times are ISO with their own offset (a stage's clock may not be UTC), so they are parsed, never
    compared as strings."""
    try:
        t = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def tool_of_action(action: str, detail: dict, stage: str) -> str:
    if action == "trace":
        return str(detail.get("source") or "trace")
    if action == "mail":
        vendor = str(detail.get("vendor") or "")
        return vendor if vendor and vendor != "csv" else "mail"
    return TOOL_BY_ACTION.get(action) or stage or action


def tool_of_spend(what: str) -> str:
    w = str(what or "").lower()
    if w.startswith("claude") or "voice" in w:
        return "claude"
    hit = next((v for v in VENDORS if v in w), "")
    return hit or tool_key(w.split(":", 1)[0]) or "unknown"


class LedgerSpend(Adapter):
    id = "ledger_spend"
    tool = ""
    provides = (
        Provides("spend", OWN, frozenset({"amount", "record", "campaign", "outcome"}), latency="poll", backfill_days=None),
        Provides("mail", OWN, frozenset({"record", "status", "campaign", "outcome"}), scope="on_record", latency="poll",
                 backfill_days=None),
    )

    def pull(self, conn, since, until, kinds, now=None) -> Batch:
        b = Batch()
        # A day of padding on either side, then the exact window in Python: the stored times carry mixed offsets.
        lo = (since - timedelta(days=1)).isoformat(timespec="seconds")
        hi = (until + timedelta(days=1)).isoformat(timespec="seconds")
        with store._LOCK, store._db() as c:
            ledger_rows = c.execute("SELECT id, at, stage, record_uuid, action, detail, usd FROM ap_events "
                                    "WHERE at>=? AND at<? AND (usd>0 OR action='mail') ORDER BY id", (lo, hi)).fetchall()
            spend_rows = c.execute("SELECT s.id, s.at, s.usd, s.what, t.workflow, t.record_uuid FROM spend s "
                                   "LEFT JOIN tasks t ON t.id = s.task_id WHERE s.at>=? AND s.at<? AND s.usd>0 "
                                   "ORDER BY s.id", (lo, hi)).fetchall()
        newest = None
        for r in ledger_rows:
            when = _when(r["at"])
            if when is None or not (since <= when < until):
                continue
            newest = max(newest, when) if newest else when
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            action, stage = str(r["action"] or ""), str(r["stage"] or "")
            if "spend" in kinds and float(r["usd"] or 0) > 0:
                b.events.append(Event(kind="spend", at=when, source=conn["id"], source_id="ap:%s" % r["id"],
                                      tool=tool_of_action(action, detail, stage), amount=round(float(r["usd"]), 4),
                                      record=r["record_uuid"] or "", campaign=stage, outcome=action,
                                      raw_ref="ap_events:%s" % r["id"]))
            if "mail" in kinds and action == "mail":
                b.events.append(Event(kind="mail", at=when, source=conn["id"], source_id="mail:%s" % r["id"],
                                      tool=tool_of_action(action, detail, stage), record=r["record_uuid"] or "",
                                      status="sent", campaign=str(detail.get("series") or stage),
                                      outcome=str(detail.get("piece") or ""), raw_ref="ap_events:%s" % r["id"]))
        if "spend" in kinds:
            for r in spend_rows:
                what = str(r["what"] or "")
                if what.lower().startswith("autopilot "):
                    continue            # the same dollars are the ledger event above
                when = _when(r["at"])
                if when is None or not (since <= when < until):
                    continue
                newest = max(newest, when) if newest else when
                b.events.append(Event(kind="spend", at=when, source=conn["id"], source_id="spend:%s" % r["id"],
                                      tool=tool_of_spend(what), amount=round(float(r["usd"]), 4),
                                      record=r["record_uuid"] or "", campaign=str(r["workflow"] or ""),
                                      outcome=what, raw_ref="spend:%s" % r["id"]))
        b.last_at = newest
        return b


ADAPTER = LedgerSpend()
