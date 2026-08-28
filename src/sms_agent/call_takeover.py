"""A rep who CALLS the seller has taken over the thread just as surely as one
who texts, and until now the agent could not see it.

Texting is not how this team mostly works. The dialer is, and a connected call
produces no SMS event of any kind, so a homeowner could have a five minute
conversation with Adriana and still get an automated follow up text that
evening. That is the same failure as two people texting at once, arriving
through a door nothing was watching.

smrtPhone has no webhook for a completed call, so this polls the call log the
same way `reconcile` polls the SMS log, over the same web session. The endpoint
and its column list are already proven by the call coaching pipeline
(`src/call_coaching/pull_calls.py`); this reuses that contract rather than
re-deriving it.

    python src/sms_agent/cli.py call-takeover --hours 24 --dry-run
    python src/sms_agent/cli.py call-takeover --hours 24

WHAT COUNTS AS A TAKEOVER, and why each gate is there:

  * At least CALL_TAKEOVER_MIN_SECONDS of connected call. A 12 second ring out
    is a voicemail, not a conversation. This is the same 60 second floor the
    coaching pipeline uses, for the same reason: below it you are grading
    (or here, reacting to) something that never happened.
  * The other party is a number we know. We never pause a thread we do not have.
  * Both directions count. A rep dialing out and a rep answering a callback are
    both a human in the conversation.

The known false positive is a genuine 60 second wrong number conversation,
which pauses a thread that did not need pausing. That is the deliberate
direction: `cli.py resume <phone>` costs one command, and a seller hearing from
two of us costs the lead.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from . import config, store

log = logging.getLogger(__name__)

BASE = "https://phone.smrt.studio"
LOG_PATH = "/logs/calls/filtered"

# Mirrors src/call_coaching/pull_calls.py. The DataTables endpoint needs the
# column list echoed back at it or it returns nothing useful.
COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status",
           "disposition", "from_num", "to_num", "price", "duration",
           "podio_id", "recording_sid", "sid", "call_agent_id"]


def _session():
    from . import numbers_sync

    return numbers_sync._session()


def _clean(value) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"<[^>]+>", "", text).strip()


def _cell(row: dict, key: str, *inner: str) -> str:
    """Log cells are sometimes a dict, sometimes an HTML fragment.

    Verified live 2026-08-28, because guessing these shapes is how the age
    filter silently stopped filtering:

        created_at  {"date": "2026-08-28 15:30:27.000000", "timezone": ...}
        from_num    {"fromNum": "+1865...", "userName": "Adriana - 11", ...}
        to_num      {"toNum": "+1865...", "contactName": "Marticia Queen"}
        user        {"id": 63932, "name": "Adriana Mondragon"}

    Running `_clean` over the dict yields its repr, which parses as nothing and
    fails open, so every row looked recent enough to act on.
    """
    val = row.get(key)
    if isinstance(val, dict):
        for k in inner:
            if val.get(k):
                return _clean(val[k])
        return ""
    return _clean(val)


# smrtPhone's own CRM association for the call, which is better evidence than
# a phone number match: it is the link the dialer itself followed.
_OWNER_LINK = re.compile(r"/records/owners/([0-9a-f-]{36})", re.I)
_PROPERTY_LINK = re.compile(r"/records/properties/([0-9a-f-]{36})", re.I)


def _crm_ids(row: dict) -> tuple[str, str]:
    """(record_uuid, owner_uuid) from the log's `podio_id` link, if it has one."""
    link = str(row.get("podio_id") or "")
    prop = _PROPERTY_LINK.search(link)
    owner = _OWNER_LINK.search(link)
    return (prop.group(1) if prop else "", owner.group(1) if owner else "")


def fetch_calls(pages: int = 2, per_page: int = 200) -> list[dict]:
    """Recent calls, newest first."""
    session = _session()
    rows: list[dict] = []
    for page in range(pages):
        form = {
            "draw": "1", "start": str(page * per_page), "length": str(per_page),
            "search[value]": "", "search[regex]": "false",
            "order[0][column]": "3", "order[0][dir]": "desc",
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
            raise RuntimeError(f"HTTP {resp.status_code} from the call log")
        try:
            page_rows = resp.json().get("data") or []
        except ValueError:
            raise RuntimeError("session expired; re-run _api/smrtphone_login.py") from None
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


def _counterparty(row: dict) -> str:
    """The seller's number, whichever end of the call they were on.

    Reading `direction` would work, but picking the side that is not one of our
    own sending numbers is robust to a relabelled or newly bought DID.
    """
    ours = set(config.numbers())
    a = store.clean_phone(_cell(row, "from_num", "fromNum", "number"))
    b = store.clean_phone(_cell(row, "to_num", "toNum", "number"))
    if a and a not in ours:
        return a
    if b and b not in ours:
        return b
    # Both look like ours (or neither parsed). Fall back to direction.
    return b if (row.get("direction") or "").lower() == "outbound" else a


def _seconds(row: dict) -> int:
    raw = _clean(row.get("duration"))
    if raw.isdigit():
        return int(raw)
    # Occasionally rendered mm:ss.
    parts = [p for p in re.split(r"[:]", raw) if p.isdigit()]
    if not parts:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def run(pages: int = 2, hours: int = 24, apply: bool = True) -> dict:
    """One call-log sweep. Returns what it found and what it paused."""
    from . import crm, engine

    try:
        rows = fetch_calls(pages=pages)
    except Exception as exc:  # noqa: BLE001 - an expired session is the usual cause
        log.warning("call takeover could not read the call log: %s", exc)
        return {"error": str(exc)[:200]}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    scanned = too_short = unknown = already = paused = 0
    results: list[dict] = []

    for row in rows:
        call_id = _clean(row.get("id"))
        if not call_id:
            continue

        created = _cell(row, "created_at", "date")
        if created:
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00").replace(" ", "T")[:25])
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < cutoff:
                    continue
            except ValueError:
                pass

        scanned += 1
        secs = _seconds(row)
        if secs < config.CALL_TAKEOVER_MIN_SECONDS:
            too_short += 1
            continue

        phone = _counterparty(row)
        if not phone:
            unknown += 1
            continue

        conv = store.get_conversation(phone)
        mapped = store.lookup_phone(phone)
        link_record, link_owner = _crm_ids(row)

        # The CRM link is a better identifier than a phone number, but it is
        # NOT an admission ticket. The dialer works a far larger universe than
        # the texter does, so accepting a call just because it carries a link
        # would mint paused conversations for thousands of people we have never
        # texted. It only counts when it resolves to a record we do text.
        linked = bool(link_record and store.phones_for_record(link_record))
        if not conv and not mapped and not linked:
            unknown += 1
            continue

        state = (conv or {}).get("state") or ""
        if state and state != "active":
            already += 1
            continue

        caller = _cell(row, "user", "name") or "a caller"
        reason = f"human called ({caller}, {secs}s)"

        if not apply:
            paused += 1
            results.append({"phone": phone, "action": "would pause", "reason": reason})
            continue

        key = f"smrtphone:callTakeover:{call_id}"
        event_id = store.record_event("smrtphone", "callTakeover", key, {
            "event": "callTakeover", "callId": call_id, "phone": phone,
            "duration": secs, "user": caller, "created_at": created,
            "disposition": _clean(row.get("disposition")),
        })
        if event_id is None:
            already += 1
            continue

        try:
            store.ensure_conversation(phone)
            store.pause_conversation(phone, reason)
            cancelled = store.cancel_queued(phone, "human called them")
            record_uuid = ((conv or {}).get("record_uuid")
                           or (mapped or {}).get("record_uuid")
                           or link_record or "")
            siblings = engine._pause_siblings(phone, record_uuid, reason)
            if record_uuid and config.PHASE >= 2:
                crm.add_tags(record_uuid, [config.TAG_AI_PAUSED])
            store.finish_event(event_id, "paused")
            paused += 1
            results.append({"phone": phone, "action": "paused", "reason": reason,
                            "cancelled": cancelled, "siblings": siblings})
            log.info("call takeover on %s by %s (%ss); cancelled %s, paused %s sibling line(s)",
                     phone, caller, secs, cancelled, len(siblings))
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            store.finish_event(event_id, "error", str(exc)[:300])

    return {
        "rows_scanned": scanned,
        "too_short": too_short,
        "not_our_thread": unknown,
        "already_inactive": already,
        "paused": paused,
        "results": results,
    }
