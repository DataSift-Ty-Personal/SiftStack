"""Routes the research lane needs that crm.py does not carry (build plan 3.2, 3.5, 3.8, 3.10).

crm.py is the autopilot's one door to DataSift and several builds share it, so a route only this
track uses lives here instead. Each helper takes the crm object and goes through `crm.call`, which
is what counts the request against the account's budget and honors a 429.

Every route is in DataSift's published reference (output/datasift_api/specs/datasift.yaml, read
2026-11-13):

    boards         GET   /api/internal/siftline/board/
    card create    POST  /api/internal/siftline/board/column/{column}/card/          {"prop": record uuid}
    card list      GET   /api/internal/siftline/board/column/{column}/card/
    card move      PATCH /api/internal/siftline/board/column/{column}/card/{card}/   {"column": column uuid}
    card timeline  GET   /api/internal/siftline/board/column/{column}/card/{card}/timeline/
    task create    POST  /api/internal/task/
    task read      GET   /api/internal/task/{uuid}/        `completed` holds the time it was done
    lists          POST  /api/internal/property/{uuid}/add-lists/ and remove-lists/  {"lists": "<one title>"}

Three things the reference decides for us:

  * THERE IS NO ROUTE THAT LISTS A BOARD'S COLUMNS. A column is therefore named by its uuid in the
    playbook. The timeline of a card does return every column of its board with its title, so the
    first card a stage creates is read back through it, and a uuid that belongs to a column with
    another title is said out loud instead of filling the wrong column quietly.
  * A TASK TAKES EXACTLY ONE ASSIGNEE KEY. The account blueprint learned this on task presets
    (2026-09-10): two keys, or an empty list, is refused. A task here names one user or one role.
  * A LIST CHANGE TAKES ONE TITLE AS A STRING. An array is accepted and does nothing (measured on
    the lien release work, 2026-11-11).

None of these writes has been proven from this app on this account yet: the card move and the task
are open items in the one-record proofs (build plan 0.11). Every caller reaches them through
ctx.write and ships in plan mode.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, ledger
from .crm import rows_of

log = logging.getLogger(__name__)

UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CARD_FACT = "research_card"      # the record's research card: {"card", "column", "at", "source"}
CARD_PAGE = 100


def is_uuid(v) -> bool:
    return bool(UUID_RX.match(str(v or "").strip()))


# ---------------------------------------------------------------------- boards, columns, cards
def board_uuid(crm, title: str) -> str:
    """The board with this title, or ''. Titles are matched the way the app shows them: case and
    outer spaces do not matter."""
    want = str(title or "").strip().lower()
    if not want:
        return ""
    for b in rows_of(crm.call("/api/internal/siftline/board/?limit=999&offset=0")):
        if str(b.get("title") or "").strip().lower() == want:
            return str(b.get("uuid") or "")
    return ""


def column_ref(setting, default_title: str = "") -> tuple[str, str, str]:
    """(column uuid, column title, problem) from a playbook setting.

    The setting is a uuid, or a mapping {title, uuid}. A bare title cannot be turned into a uuid on
    documented routes, so it comes back with an empty uuid and the reason in words; the caller then
    skips the card and keeps the rest of its work."""
    title, uuid = default_title, ""
    if isinstance(setting, dict):
        title = str(setting.get("title") or default_title)
        uuid = str(setting.get("uuid") or "").strip()
    elif setting:
        text = str(setting).strip()
        if is_uuid(text):
            uuid = text
        else:
            title = text
    if uuid and not is_uuid(uuid):
        return "", title, "the %s column is set to %r, which is not a column uuid" % (title or "card", uuid)
    if not uuid:
        return "", title, ("the %s column is named by title only: DataSift's published API has no route that lists a "
                           "board's columns, so give the column's uuid in the playbook" % (title or "card"))
    return uuid, title, ""


def create_card(crm, column_uuid: str, record_uuid: str) -> dict:
    """One card for a record in a column. The body is the documented one: the record goes in `prop`."""
    return crm.call("/api/internal/siftline/board/column/%s/card/" % column_uuid, "POST", {"prop": record_uuid}) or {}


def move_card(crm, column_uuid: str, card_uuid: str, to_column_uuid: str) -> dict:
    """Move a card. The path names the column the card is in now; the body names where it goes."""
    return crm.call("/api/internal/siftline/board/column/%s/card/%s/" % (column_uuid, card_uuid), "PATCH",
                    {"column": to_column_uuid}) or {}


def card_timeline(crm, column_uuid: str, card_uuid: str) -> dict:
    """column uuid -> {title, order, time, current} for every column of the card's board."""
    r = crm.call("/api/internal/siftline/board/column/%s/card/%s/timeline/" % (column_uuid, card_uuid))
    return r if isinstance(r, dict) else {}


def column_title(timeline: dict, column_uuid: str) -> str:
    """The title of one column as the board itself gives it, or ''."""
    row = (timeline or {}).get(column_uuid) or {}
    return str(row.get("title") or "") if isinstance(row, dict) else ""


def card_property(card: dict) -> str:
    """The record a listed card belongs to. The reference marks `prop` as write-only, so the key the
    listing carries is not promised: every spelling it could use is read."""
    for key in ("prop_uuid", "prop", "property", "property_uuid"):
        v = (card or {}).get(key)
        if isinstance(v, dict):
            v = v.get("uuid")
        if v:
            return str(v)
    return ""


def cards_in_column(crm, column_uuid: str, cap: int = 500) -> dict:
    """record uuid -> card, for the cards sitting in one column. Read once per run and looked up by
    record, never once per record: the listing has no filter by property."""
    out: dict[str, dict] = {}
    offset = 0
    while offset < cap:
        n = min(CARD_PAGE, cap - offset)
        page = rows_of(crm.call("/api/internal/siftline/board/column/%s/card/?limit=%d&offset=%d" % (column_uuid, n, offset)))
        for c in page:
            prop = card_property(c)
            if prop and c.get("uuid"):
                out.setdefault(prop, dict(c, _column=column_uuid))
        if len(page) < n:
            break
        offset += n
    return out


def find_card(record_uuid: str, board_title: str = "", board_id: str = "", column_index: dict | None = None) -> dict | None:
    """The record's research card, from the three places that can know it, cheapest first.

    1. The ledger: a card this app created, or adopted on an earlier run.
    2. The shared log copy: a card event is filed under the record it belongs to, so a card the
       account's own sequence made (a person set the Deep Prospecting status by hand) is found
       without a request.
    3. The column listing the caller read once this run.

    Without this lookup a repeat run would add a second card for the same house, and the board
    would show one record twice."""
    f = ledger.fact(record_uuid, CARD_FACT)
    if isinstance(f, dict) and f.get("card"):
        return dict(f, source="ledger")
    boards = {str(board_title or "").strip().lower(), str(board_id or "").strip().lower()} - {""}
    for e in reversed(activity.events(("card.created", "card.moved"), resource=record_uuid)):
        d = e.get("data") or {}
        board = str(d.get("board") or "").strip().lower()
        if boards and board and board not in boards:
            continue                      # a card on another board (the lead board) is not this one
        if boards and not board:
            continue                      # the log row does not say which board: do not guess
        if d.get("card"):
            return {"card": str(d["card"]), "column": "", "column_title": str(d.get("column") or ""), "source": "log"}
    c = (column_index or {}).get(record_uuid)
    if c and c.get("uuid"):
        return {"card": str(c["uuid"]), "column": str(c.get("_column") or ""), "source": "column"}
    return None


def remember_card(record_uuid: str, card_uuid: str, column_uuid: str, now: datetime, source: str = "created") -> dict:
    f = {"card": card_uuid, "column": column_uuid, "at": now.isoformat(timespec="seconds"), "source": source}
    ledger.set_fact(record_uuid, CARD_FACT, f)
    return f


# ---------------------------------------------------------------------- tasks
def end_of_day(now: datetime, tz: str, days: int = 0) -> str:
    """The last moment of the business-local day, in UTC, the way the app writes an all-day due
    date (23:59:59.999 of the day in the user's zone)."""
    local = now.astimezone(ZoneInfo(tz)) + timedelta(days=days)
    end = local.replace(hour=23, minute=59, second=59, microsecond=999000)
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def task_body(title: str, *, property_uuid: str = "", user_uuid: str = "", role: str = "", due: str = "",
              notes: str = "", source: str = "autopilot") -> dict:
    """The documented body for one task. Exactly one assignee: a user or a role, never both and
    never neither. A key with no value is left out, never sent empty."""
    if bool(user_uuid) == bool(role):
        raise ValueError("a task takes exactly one assignee: a user or a role")
    body: dict = {"title": " ".join(str(title).split())[:256], "all_day": True, "event_type": "task"}
    if due:
        body["due_date"] = due
    if notes:
        body["notes"] = str(notes)[:4000]
    if property_uuid:
        body["assigned_to_property"] = property_uuid
    if user_uuid:
        body["assigned_to_users"] = [user_uuid]
    else:
        body["assigned_to_role"] = role
    if source:
        body["source"] = str(source)[:256]
    return body


def create_task(crm, title: str, *, property_uuid: str = "", user_uuid: str = "", role: str = "", due: str = "",
                notes: str = "") -> dict:
    r = crm.call("/api/internal/task/", "POST",
                 task_body(title, property_uuid=property_uuid, user_uuid=user_uuid, role=role, due=due, notes=notes))
    return r if isinstance(r, dict) else {}


def task_open(crm, task_uuid: str) -> bool | None:
    """Is this task still open? True, False (completed), or None when it no longer exists (a person
    deleted it). GET /api/internal/task/{uuid}/ carries `completed`, the time it was done."""
    try:
        r = crm.call("/api/internal/task/%s/" % task_uuid)
    except Exception as exc:  # noqa: BLE001  (only a 404 is an answer; anything else is a failed read)
        if getattr(exc, "code", None) == 404:
            return None
        raise
    return not (isinstance(r, dict) and r.get("completed"))


# ---------------------------------------------------------------------- lists
def add_list(crm, record_uuid: str, title: str) -> None:
    """Put a record on one list, by title. One title a call, as a string."""
    crm.call("/api/internal/property/%s/add-lists/" % record_uuid, "POST", {"lists": str(title)})


def remove_list(crm, record_uuid: str, title: str) -> None:
    """Take a record off one list, by title. Other lists on the record are not touched."""
    crm.call("/api/internal/property/%s/remove-lists/" % record_uuid, "POST", {"lists": str(title)})


def last4(number) -> str:
    """The last four digits of a number: what a task title or a Slack line shows, never the whole."""
    d = "".join(ch for ch in str(number or "") if ch.isdigit())
    return d[-4:] if len(d) >= 4 else ""
