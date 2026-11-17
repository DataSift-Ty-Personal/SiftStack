"""Lead-management routes (build plan 2.5, 2.6, 2.7, 2.10, 2.14): the board, its columns, a card's
timeline, a card move, tasks and appointments, each through a documented route.

The lead half of the account lives on two boards (leads, then offers) and in tasks. Three things
about those routes shaped this module, and each one fails quietly when it is forgotten:

  * COLUMNS ARE NOT LISTED ANYWHERE in the published API. The one documented place a board's
    columns appear is a card's timeline, which returns every column of that card's board keyed by
    column id, with the seconds the card spent in each. So the columns are learned from one card
    and remembered, and a card can only be addressed as (column id, card id). When no column id can
    be learned (an empty board, a log that names columns by title only), the stage says exactly
    which setting to fill and moves nothing.
  * A CARD ROW MAY NOT SAY WHICH RECORD IT BELONGS TO. The published card shape has no readable
    property field, so a card is matched to its record through the shared log copy (every card
    event is filed under its property). A card nobody can name is counted, never guessed.
  * EVERY MOVE CLEARS THE TASKS. The board's sequences delete all of a record's tasks when its card
    lands in a new column. A move over the API may or may not fire those sequences (unproven until
    one staging record is moved), so the phase table below also holds what each sequence would
    have done, and a stage can do it itself.

Nothing here decides anything. The stages decide; this module reads and writes, and every write is
called through Ctx.write by the stage that owns it.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity, budget, ledger
from .crm import rows_of

log = logging.getLogger(__name__)

# A note a person wrote on a record counts as a real conversation (build plan 2.5), and the shared
# log copy keeps only the event types its groups name. Notes were not among them, so the group is
# registered here, where the first lead stage imports it. setdefault: if activity.py grows its own
# "notes" group, that definition wins. The second name is the owner board's event as best known; a
# name the log never uses costs nothing in the filter.
NOTE_EVENTS = ("property.message.added", "owner.message.added")
activity.GROUPS.setdefault("notes", NOTE_EVENTS)

# Who wrote a log row. A note or a task from one of these was written by a machine, not by a person
# on the phone, so it is never read as a conversation or as a date a person chose.
MACHINE_SOURCES = {"sequence", "bulk", "auto-update", "data-enriching", "internal-api", "system", "import"}

_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# The phases a lead moves through, each with the column that holds it, the status the account's own
# sequence sets when a card lands there, and the task preset that sequence creates. These are the
# stock DataSift lead boards; a stage overrides any of it under `columns:` and `follow_on:`.
#   board: "lead" is the lead management board, "offers" the acquisitions board.
#   then:  the phase the sequence moves the card on to (Send Back lands the card in Needs Review).
PHASES = {
    "new_lead":       {"board": "lead", "column": "New Lead", "status": "new_lead", "preset": "Call New Lead"},
    "no_contact":     {"board": "lead", "column": "No Contact New Lead", "status": "No Contact New Lead",
                       "preset": "No Contact New Lead"},
    "nurture":        {"board": "lead", "column": "Nurture New Lead", "status": "", "preset": "Nurture New Lead"},
    "cold":           {"board": "lead", "column": "COLD Leads", "status": "Cold Lead", "preset": "COLD Follow-Up"},
    "warm":           {"board": "lead", "column": "WARM Leads", "status": "Warm Lead", "preset": "WARM Follow-Up"},
    "hot":            {"board": "lead", "column": "HOT Leads", "status": "Hot Lead", "preset": "HOT Follow-Up"},
    "ghosting":       {"board": "lead", "column": "Ghosting Lead", "status": "Ghosting Lead", "preset": ""},
    "not_interested": {"board": "lead", "column": "Not Interested Anymore", "status": "not_interested", "preset": ""},
    "dead":           {"board": "lead", "column": "Dead Lead", "status": "Dead Lead", "preset": ""},
    "needs_review":   {"board": "lead", "column": "Needs Review", "status": "", "preset": "Send Back to LM"},
    "appointment":    {"board": "offers", "column": "Appointment Booked", "status": "", "preset": ""},
    "offer_rejected": {"board": "offers", "column": "Offer Rejected", "status": "", "preset": ""},
    "send_back":      {"board": "offers", "column": "Send Back to Lead Management", "status": "",
                       "preset": "Send Back to LM", "then": "needs_review"},
}
BOARD_TITLES = {"lead": "Lead Management", "offers": "Acquisitions"}

# Change kinds the lead stages record that undo.py does not know yet. undo_step below puts each back.
# card_create is the First to Market board's (ftmpipeline): the card it placed is taken off again.
UNDOABLE = ("card_move", "card_remove", "card_create", "task_create", "task_delete", "add_lists", "remove_lists")


# ---------------------------------------------------------------------- small helpers
def is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID.match(v.strip()))


def parse_dt(v) -> datetime | None:
    """A timestamp in any of the shapes the routes and the log use, as an aware UTC datetime."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v or "").strip().replace(" ", "T")
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for cand in (s, s[:19]):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def end_of_day(now: datetime, tz: str, days: int = 0) -> str:
    """23:59:59.999 of the business-local day `days` from now, in UTC. create-by-preset REQUIRES this
    field (a create without it answers 400), and a task due "today" is due at the end of the
    business day, not at UTC midnight, which is 7pm Eastern."""
    local = now.astimezone(ZoneInfo(tz)) + timedelta(days=days)
    eod = local.replace(hour=23, minute=59, second=59, microsecond=0)
    return eod.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "999Z"


def business_day(now: datetime, tz: str, days: int) -> datetime:
    """The local day `days` business days from now (0 is today, or the next weekday on a weekend)."""
    d = now.astimezone(ZoneInfo(tz))
    while d.weekday() >= 5:
        d += timedelta(days=1)
    left = int(days)
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def nice_day(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%b %d").replace(" 0", " ")


def nice_time(dt: datetime, tz: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    return ("%s %s" % (local.strftime("%a %b %d").replace(" 0", " "), local.strftime("%I:%M %p").lstrip("0")))


def span(seconds: float) -> str:
    """A length of time the way a person says it."""
    m = max(0.0, seconds) / 60.0
    if m < 90:
        return "%d min" % round(m)
    if m < 48 * 60:
        h = m / 60.0
        return ("%d hours" % round(h)) if abs(h - round(h)) < 0.05 else ("%.1f hours" % h)
    return "%d days" % round(m / 1440.0)


def phases(stage) -> dict:
    """The phase table with this stage's overrides: `columns: {phase: column title}` and
    `follow_on: {phase: {status, preset}}`."""
    out = copy.deepcopy(PHASES)
    for k, title in (stage.get("columns") or {}).items():
        if k in out and title:
            out[k]["column"] = str(title)
    for k, v in (stage.get("follow_on") or {}).items():
        if k in out and isinstance(v, dict):
            for f in ("status", "preset", "then"):
                if f in v:
                    out[k][f] = str(v[f] or "")
    return out


def board_title(stage, role: str) -> str:
    if role == "lead":
        return str(stage.get("board") or BOARD_TITLES["lead"])
    return str(stage.get("offers_board") or BOARD_TITLES["offers"])


def stored_status(x: dict) -> str | None:
    """A record's status exactly as the account stores it, or None when it has none. A status is
    written back in this spelling: the status route knows "Hot Lead" and has never heard of
    "hot lead", which is what every comparison in the engine lower-cases it to."""
    s = (x or {}).get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title")
    return str(s) if s else None


def status_rows(ctx, statuses, cap: int = 2000) -> list[dict]:
    """The search rows for these statuses, with every row's own status checked here as well.

    The records search drops a filter key it does not honor and answers with every record in the
    account, with no error (a plain `status` key is ignored exactly that way). A stage that trusted
    the rows would then give a lead's task to a prospecting record, or clear the status of a record
    that was never dead. So a row outside the statuses asked for is dropped here, and the stage
    says so: that line is what a filter the server stopped applying looks like."""
    asked = [str(s) for s in statuses if str(s).strip()]
    want = {s.strip().lower() for s in asked}
    rows = ctx.crm.rows({"any_property_status": asked}, cap=cap) if asked else []
    keep = [r for r in rows if (stored_status(r) or "").strip().lower() in want]
    if len(keep) != len(rows):
        ctx.out.problem("the records search returned %d record(s) outside the status(es) asked for, and they were left alone. "
                        "Check that the any_property_status filter is still applied (asked for: %s)" % (
                            len(rows) - len(keep), ", ".join(asked[:6])))
    return keep


def status_phase(status: str, ph: dict) -> str:
    """The phase a status belongs to ('' when the status is not one a board column sets)."""
    s = str(status or "").strip().lower()
    for key, row in ph.items():
        if row.get("status") and row["status"].lower() == s:
            return key
    return ""


def title_phase(title: str, ph: dict, role: str = "lead") -> str:
    """The phase a logged column TITLE belongs to. The longest configured name that the title is,
    or starts with, wins: "No Contact New Lead (Unqualified)" is No Contact, never New Lead, though
    it ends with those words."""
    t = str(title or "").strip().lower()
    best, size = "", 0
    for key, row in ph.items():
        name = str(row.get("column") or "").strip().lower()
        if row.get("board") != role or not name:
            continue
        if t == name:
            return key
        if t.startswith(name) and len(name) > size:
            best, size = key, len(name)
    return best


# ---------------------------------------------------------------------- boards and columns
def boards(crm) -> list[dict]:
    return rows_of(crm.call("/api/internal/siftline/board/?limit=100&offset=0"))


def board_id(crm, title: str) -> str:
    """The board's id by its title (any case), remembered: a board is renamed about never."""
    key = "autopilot:leads:board:%s" % title.strip().lower()
    v = store.kv_get(key)
    if v:
        return v
    for b in boards(crm):
        if str(b.get("title") or "").strip().lower() == title.strip().lower() and b.get("uuid"):
            store.kv_set(key, str(b["uuid"]))
            return str(b["uuid"])
    return ""


def match_title(titles: dict, name: str) -> str:
    """The key in `titles` (key -> title) whose title is `name`: exactly, else the one title that
    starts with it, else the one that contains it. "New Lead" must find "New Lead (Unqualified)"
    and must not find "No Contact New Lead (Unqualified)", which is why a prefix is tried before a
    contains, and why two prefix matches find nothing instead of the first."""
    want = str(name or "").strip().lower()
    if not want:
        return ""
    low = {k: str(t or "").strip().lower() for k, t in titles.items()}
    exact = [k for k, t in low.items() if t == want]
    if exact:
        return exact[0]
    for test in (lambda t: t.startswith(want), lambda t: want in t):
        hits = [k for k, t in low.items() if test(t)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return ""
    return ""


@dataclass
class Board:
    title: str
    uuid: str = ""
    columns: dict = field(default_factory=dict)      # column id -> {"title": ..., "order": ...}
    source: str = ""                                 # where the columns came from, for the report

    def find(self, name: str) -> str:
        """The column id for a column title (loosely matched), or ''."""
        return match_title({u: c.get("title") for u, c in self.columns.items()}, name)

    def name(self, column_uuid: str) -> str:
        return str((self.columns.get(column_uuid) or {}).get("title") or "")

    def phase_columns(self, ph: dict, role: str) -> dict:
        """phase -> column id, for the phases that live on this board and whose column exists."""
        out = {}
        for key, row in ph.items():
            if row.get("board") == role:
                u = self.find(row.get("column") or "")
                if u:
                    out[key] = u
        return out

    def phase_of(self, column_uuid: str, ph: dict, role: str) -> str:
        for key, u in self.phase_columns(ph, role).items():
            if u == column_uuid:
                return key
        return ""


def timeline(crm, column_uuid: str, card_uuid: str) -> dict:
    """column id -> {"title", "order", "seconds", "current"} for every column of the card's board."""
    r = crm.call("/api/internal/siftline/board/column/%s/card/%s/timeline/" % (column_uuid, card_uuid)) or {}
    out = {}
    for cu, v in (r.items() if isinstance(r, dict) else []):
        if isinstance(v, dict):
            out[str(cu)] = {"title": str(v.get("title") or ""), "order": v.get("order") or 0,
                            "seconds": float(v.get("time") or 0), "current": bool(v.get("current"))}
    return out


def _columns_key(title: str) -> str:
    return "autopilot:leads:columns:%s" % title.strip().lower()


def _json(text: str) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def remember_columns(title: str, board_uuid: str, cols: dict, now: datetime) -> None:
    store.kv_set(_columns_key(title), json.dumps({"at": now.isoformat(timespec="seconds"), "board": board_uuid,
                                                  "columns": cols}))


def seeds_from_log(crm, title: str, board_uuid: str, now: datetime, limit: int = 25) -> list[tuple[str, str]]:
    """(column id, card id) pairs from the newest card events in the account log, for learning a
    board's columns. The stored log copy keeps a column's title; the raw row may also carry its
    id, and when it does that is a free seed. A row that names another board is skipped."""
    must = {"timestamp": [iso_z(now - timedelta(days=60)), iso_z(now + timedelta(minutes=1))],
            "any_event_type": ["card.moved", "card.created"]}
    try:
        r = crm.logs(must, limit=limit)
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001  (a seed is a convenience; the caller reports what is missing)
        return []
    want = {title.strip().lower(), (board_uuid or "").lower()} - {""}
    out: list[tuple[str, str]] = []
    for e in (r.get("results") or []) if isinstance(r, dict) else []:
        p = e.get("payload") or {}
        c = p.get("card") if isinstance(p.get("card"), dict) else {}
        extra = p.get("extra_info") if isinstance(p.get("extra_info"), dict) else {}
        ec = extra.get("card") if isinstance(extra.get("card"), dict) else {}
        named = set()
        for v in (c.get("board"), ec.get("board"), c.get("board_uuid"), ec.get("board_uuid")):
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, dict):
                    x = x.get("uuid") or x.get("title")
                if x:
                    named.add(str(x).strip().lower())
        if named and not named & want:
            continue
        col = ""
        for v in (c.get("column"), c.get("column_uuid"), ec.get("column_uuid"), ec.get("column")):
            x = v[-1] if isinstance(v, list) and v else v
            if isinstance(x, dict):
                x = x.get("uuid")
            if is_id(x):
                col = x
                break
        card = str(e.get("resource") or c.get("uuid") or "")
        if col and card and (col, card) not in out:
            out.append((col, card))
    return out


def _columns_from_detail(crm, board_uuid: str) -> dict:
    """The board detail is documented; whether it carries the columns is not. Use them if it does."""
    try:
        d = crm.call("/api/internal/siftline/board/%s/" % board_uuid) or {}
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001
        return {}
    cols = d.get("columns") if isinstance(d, dict) else None
    out = {}
    if isinstance(cols, dict):
        cols = [dict(v, uuid=k) for k, v in cols.items() if isinstance(v, dict)]
    for i, c in enumerate(cols or []):
        if isinstance(c, dict) and c.get("uuid") and c.get("title"):
            out[str(c["uuid"])] = {"title": str(c["title"]), "order": c.get("order", i)}
    return out


def recent_cards(board: Board) -> list[str]:
    """Card ids the log copy has seen on a board, newest first."""
    want = {board.title.strip().lower(), (board.uuid or "").lower()} - {""}
    out: list[str] = []
    for e in reversed(activity.events(("card.created", "card.moved"))):
        named = str(e["data"].get("board") or "").strip().lower()
        card = e["data"].get("card") or ""
        if card and (not named or named in want) and card not in out:
            out.append(card)
    return out


def load_board(crm, title: str, *, column_ids: dict | None = None, seed_column: str = "", expect=(),
               now: datetime | None = None, ttl_days: float = 7.0, problems: list | None = None) -> Board:
    """The board with its columns, from the cheapest source that has them: the playbook, what was
    learned earlier, the board detail, then one card's timeline. `expect` names columns the right
    board must have, so a seed card from another board is not mistaken for this one. A board whose
    columns cannot be learned comes back with none and one line saying what to set."""
    now = now or datetime.now(timezone.utc)
    b = Board(title=title)

    def fail(text: str) -> None:
        if problems is not None and text not in problems:
            problems.append(text)

    given = {str(t): str(u) for t, u in (column_ids or {}).items() if u}
    cached = _json(store.kv_get(_columns_key(title)))
    b.uuid = str(cached.get("board") or "")
    if given:
        b.columns = {u: {"title": t, "order": i} for i, (t, u) in enumerate(given.items())}
        b.source = "the playbook"
        return b
    at = parse_dt(cached.get("at"))
    if cached.get("columns") and at and now - at <= timedelta(days=ttl_days):
        b.columns, b.source = cached["columns"], "remembered"
        return b
    try:
        b.uuid = board_id(crm, title)
        if not b.uuid:
            fail("there is no board titled %r in this account; set `board` (or `offers_board`) on the stage" % title)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        fail("could not list the boards (%s)" % str(exc)[:100])
        b.uuid = ""
    cols, how = {}, ""
    if b.uuid:
        cols, how = _columns_from_detail(crm, b.uuid), "the board"
    if not cols and (b.uuid or seed_column):
        seeds: list[tuple[str, str]] = []
        if seed_column:
            try:
                seeds += [(seed_column, str(c.get("uuid"))) for c in cards(crm, seed_column, cap=3) if c.get("uuid")][:1]
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                fail("could not read the seed column of the %s board (%s)" % (title, str(exc)[:100]))
        seeds += seeds_from_log(crm, title, b.uuid, now)[:3]
        # Last try. The log copy knows card ids even when it knows a column only by its title, and
        # whether the timeline route insists on the right column id in its path is not documented.
        # One read with the board's own id in that place either teaches every column or answers
        # 404 and costs nothing else.
        if b.uuid and not seed_column:
            seeds += [(b.uuid, card) for card in recent_cards(b)[:2]]
        for col, card in seeds[:6]:
            try:
                tl = timeline(crm, col, card)
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001  (the card moved or was removed since the log row: try the next)
                continue
            titles = {u: v["title"] for u, v in tl.items()}
            if not titles or (expect and not any(match_title(titles, n) for n in expect)):
                continue
            cols, how = {u: {"title": v["title"], "order": v["order"]} for u, v in tl.items()}, "a card's timeline"
            break
    if not cols:
        if cached.get("columns"):
            b.columns, b.source = cached["columns"], "remembered (could not be refreshed)"
            return b
        if b.uuid:
            fail("could not learn the columns of the %s board from the documented routes. Put the id of any one "
                 "column that holds a card under seed_columns on the stage, or list every column under column_ids" % title)
        return b
    b.columns, b.source = cols, how
    remember_columns(title, b.uuid, cols, now)
    return b


def stage_board(ctx, role: str, ph: dict) -> Board:
    """The board a stage works on, by role, with the stage's own settings applied."""
    st = ctx.stage
    title = board_title(st, role)
    expect = [row["column"] for row in ph.values() if row.get("board") == role and row.get("column")]
    return load_board(ctx.crm, title, column_ids=(st.get("column_ids") or {}).get(title),
                      seed_column=str((st.get("seed_columns") or {}).get(title) or ""), expect=expect, now=ctx.now,
                      ttl_days=float(st.get("columns_ttl_days", 7)), problems=ctx.out.problems)


# ---------------------------------------------------------------------- cards
def cards(crm, column_uuid: str, cap: int = 500, due_after: datetime | None = None) -> list[dict]:
    """The cards in one column. `due_after` keeps only the cards that hold a task due after that
    moment (the route's own filter)."""
    out, off, page = [], 0, 100
    extra = ("&task_due_date_after=" + urllib.parse.quote(iso_z(due_after))) if due_after else ""
    while off < cap:
        n = min(page, cap - off)
        r = crm.call("/api/internal/siftline/board/column/%s/card/?limit=%d&offset=%d%s" % (column_uuid, n, off, extra))
        rows = rows_of(r)
        out += rows
        if len(rows) < n:
            break
        off += n
    return out


def card_prop(card: dict) -> str:
    """The record a card row names, in any shape a response has been seen or documented to use."""
    for k in ("prop", "prop_uuid", "property", "property_uuid"):
        v = (card or {}).get(k)
        if isinstance(v, dict):
            v = v.get("uuid")
        if isinstance(v, str) and v:
            return v
    return ""


def card_index() -> dict:
    """card id -> record id, from every card event the shared log copy holds."""
    out = {}
    for e in activity.events(("card.created", "card.moved")):
        card = e["data"].get("card") or ""
        if card and e["resource"] and e["resource"] != card:
            out[card] = e["resource"]
    return out


def cards_by_record(crm, column_uuids: list[str], cap: int = 500, index: dict | None = None) -> tuple[dict, int]:
    """record id -> {"card", "column"} for every card in these columns, and how many cards could
    not be matched to a record. The listing is the truth about where a card is NOW; the log copy is
    only how a card is matched to its record when the row itself does not say."""
    index = card_index() if index is None else index
    out, unknown = {}, 0
    for col in column_uuids:
        for c in cards(crm, col, cap=cap):
            cu = str(c.get("uuid") or "")
            u = card_prop(c) or index.get(cu, "")
            if not cu or not u:
                unknown += 1
                continue
            out[u] = {"card": cu, "column": col, "updated": c.get("updated") or ""}
    return out, unknown


def last_card_event(record_uuid: str, board: Board | None = None) -> dict | None:
    """The newest card event the log copy holds for a record (on this board, when the row says)."""
    evs = activity.events(("card.created", "card.moved"), resource=record_uuid)
    if board is not None:
        want = {board.title.strip().lower(), (board.uuid or "").lower()} - {""}
        keep = [e for e in evs if not e["data"].get("board") or str(e["data"]["board"]).strip().lower() in want]
        evs = keep
    return evs[-1] if evs else None


def same_column(logged: str, column_uuid: str, board: Board) -> bool:
    """A logged column (a title, or an id) against a column id."""
    v = str(logged or "").strip().lower()
    return bool(v) and v in (column_uuid.lower(), board.name(column_uuid).strip().lower())


def move_card(crm, column_uuid: str, card_uuid: str, to_column: str, to_board: str = "") -> dict:
    """Move a card to another column. The board id rides along only for a move between boards."""
    body = {"column": to_column}
    if to_board:
        body["board"] = to_board
    return crm.call("/api/internal/siftline/board/column/%s/card/%s/" % (column_uuid, card_uuid), "PATCH", body) or {}


def card_in_column(crm, column_uuid: str, card_uuid: str) -> bool:
    """Read a card back at its new address. The route is nested, so a card that did not move
    answers 404 under the column it was sent to."""
    try:
        r = crm.call("/api/internal/siftline/board/column/%s/card/%s/" % (column_uuid, card_uuid))
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001
        return False
    return isinstance(r, dict) and str(r.get("uuid") or card_uuid) == card_uuid


def remove_card(crm, column_uuid: str, card_uuid: str) -> None:
    crm.call("/api/internal/siftline/board/column/%s/card/%s/" % (column_uuid, card_uuid), "DELETE")


def create_card(crm, column_uuid: str, record_uuid: str) -> dict:
    return crm.call("/api/internal/siftline/board/column/%s/card/" % column_uuid, "POST", {"prop": record_uuid}) or {}


# ---------------------------------------------------------------------- tasks
def task_presets(crm) -> dict:
    """lower-case title -> the task preset, read once per client."""
    have = getattr(crm, "_lead_task_presets", None)
    if have is None:
        have = {}
        for p in rows_of(crm.call("/api/internal/task-preset/?limit=999&offset=0")):
            if p.get("title") and p.get("uuid"):
                have.setdefault(str(p["title"]).strip().lower(), p)
        try:
            crm._lead_task_presets = have
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001  (a client that refuses attributes just reads the list again)
            pass
    return have


def preset_id(crm, title: str) -> str:
    return str((task_presets(crm).get(str(title or "").strip().lower()) or {}).get("uuid") or "")


def create_task_by_preset(crm, preset_uuid: str, record_uuid: str, now: datetime, tz: str, assignee: str = "") -> dict:
    """One task from a task preset, attached to the record. The preset carries its own due date
    rule and assignee; `assignee` overrides it only when the stage asks for that."""
    body = {"task_preset": preset_uuid, "end_of_day": end_of_day(now, tz), "assigned_to_property": record_uuid}
    if assignee:
        body["assigned_to_user"] = assignee
    return crm.call("/api/internal/task/create-by-preset/", "POST", body) or {}


def create_task(crm, record_uuid: str, title: str, due: str = "", assignee: str = "", notes: str = "",
                extra: dict | None = None) -> dict:
    """A plain task on a record. `due` is an ISO timestamp (see end_of_day)."""
    body = {"title": str(title)[:256], "all_day": True, "event_type": "task", "assigned_to_property": record_uuid}
    if due:
        body["due_date"] = due
    if assignee:
        body["assigned_to_users"] = [assignee]
    if notes:
        body["notes"] = notes[:2000]
    body.update(extra or {})
    return crm.call("/api/internal/task/", "POST", body) or {}


def create_appointment(crm, record_uuid: str, title: str, start: datetime, end: datetime, assignee: str = "",
                       address: str = "", kind: str = "property_walkthrough", where: str = "in_person",
                       notes: str = "") -> dict:
    """An appointment is a task with a start, an end and, later, an outcome."""
    extra = {"event_type": "appointment", "appointment_type": kind, "location_type": where, "all_day": False,
             "start_time": iso_z(start), "end_time": iso_z(end)}
    if address:
        extra["address"] = address[:250]
    return create_task(crm, record_uuid, title, due=iso_z(start), assignee=assignee, notes=notes, extra=extra)


def get_task(crm, task_uuid: str) -> dict:
    return crm.call("/api/internal/task/%s/" % task_uuid) or {}


def delete_task(crm, task_uuid: str) -> None:
    crm.call("/api/internal/task/%s/" % task_uuid, "DELETE")


def set_outcome(crm, task_uuid: str, status: str = "", result: str = "", notes: str = "") -> dict:
    body = {k: v for k, v in (("outcome_status", status), ("outcome_result", result), ("outcome_notes", notes)) if v}
    return crm.call("/api/internal/task/%s/set-outcome/" % task_uuid, "POST", body) or {}


def task_id(resp) -> str:
    return str((resp or {}).get("uuid") or "") if isinstance(resp, dict) else ""


def open_tasks(record_uuid: str) -> list[dict]:
    """A record's open tasks as far as the shared log copy shows them: created and neither completed
    nor deleted since. Each carries who made it, which is what decides whether a date on it is a
    date a person chose. The search row's own count is the truth about HOW MANY are open; this is
    the only place that says WHICH, and it reaches only as far back as the log copy does."""
    made: dict[str, dict] = {}
    closed: set[str] = set()
    for e in activity.events(("task.created", "task.completed", "task.deleted"), resource=record_uuid):
        t = e["data"].get("task") or ""
        if not t:
            continue
        if e["event_type"] == "task.created":
            made[t] = {"task": t, "title": e["data"].get("title") or "", "kind": e["data"].get("kind") or "task",
                       "due": parse_dt(e["data"].get("due")), "source": str(e.get("source") or ""),
                       "by": e["data"].get("by") or "", "author": str(e.get("author") or ""), "at": e["when"]}
        else:
            closed.add(t)
    # A completion whose row does not name the property is filed under the task itself.
    for t in list(made):
        if t not in closed and activity.events(("task.completed", "task.deleted"), resource=t):
            closed.add(t)
    return [v for t, v in made.items() if t not in closed]


def made_by_machine(task: dict, ours: set | None = None) -> bool:
    """True for a task a sequence made, or one this autopilot made. An appointment is always a
    person's: somebody agreed that day and time with a seller."""
    if task.get("kind") == "appointment":
        return False
    return task.get("source") == "sequence" or bool(task.get("by")) or task.get("task") in (ours or ())


def our_tasks(record_uuid: str) -> set:
    """The ids of the tasks this autopilot created on a record (from its own ledger)."""
    return {e["detail"]["task"] for e in ledger.events(record_uuid) if e["detail"].get("task")}


def ensure_board_id(crm, board: Board) -> str:
    """A board's id, looked up the first time a move between boards needs it."""
    if not board.uuid:
        board.uuid = board_id(crm, board.title)
    return board.uuid


# ---------------------------------------------------------------------- people
def names_from_log(since: datetime | None = None) -> dict:
    """user id -> first name, from the callers the call log names. The team list is not in the
    published API, and a ping that says "the assignee" instead of a name is a ping nobody answers."""
    out = {}
    for e in activity.events("owner.call.answered", since=since):
        d = e["data"]
        if d.get("caller_uuid") and d.get("caller"):
            out[str(d["caller_uuid"])] = str(d["caller"])
    return out


def person_name(stage, user_uuid: str, names: dict | None = None) -> str:
    people = stage.get("people") or {}
    return str(people.get(user_uuid) or (names or {}).get(user_uuid) or "")


def task_owner(stage, rec: dict) -> str:
    """Who a lead's task goes to (decision D13): the lead manager named on the stage by user id,
    else the record's own assignee. Leads belong to the lead manager; while the account's New Lead
    sequence still assigns somebody else, `lead_manager_id` puts the work where the decision says."""
    a = (rec or {}).get("assigned_to")
    return str(stage.get("lead_manager_id") or (a.get("uuid") if isinstance(a, dict) else a) or "")


# ---------------------------------------------------------------------- lists (documented, one title a call)
def add_lists(crm, record_uuid: str, titles: list[str]) -> None:
    for t in titles:
        crm.call("/api/internal/property/%s/add-lists/" % record_uuid, "POST", {"lists": t})


def remove_lists(crm, record_uuid: str, titles: list[str]) -> None:
    for t in titles:
        crm.call("/api/internal/property/%s/remove-lists/" % record_uuid, "POST", {"lists": t})


# ---------------------------------------------------------------------- putting a run back
def undo_step(crm, change: dict) -> str:
    """Put one recorded change back and say what was done. For the change kinds in UNDOABLE; undo.py
    calls this for them. A task that was created is deleted, a moved card goes back to its column
    (which, like any move, may fire that column's sequence), a removed card is created again."""
    what, before, after, u = change["what"], change.get("before"), change.get("after"), change.get("record_uuid") or ""
    if what == "card_move":
        move_card(crm, after["column"], after["card"], before["column"], before.get("board") or "")
        return "card moved back"
    if what == "card_remove":
        create_card(crm, before["column"], u)
        return "card created again"
    if what == "card_create":
        from .ftmpipeline import undo_create
        return undo_create(crm, change)
    if what == "task_create":
        ids = [e["detail"].get("task") for e in ledger.events(u) if e["run_id"] == change.get("run_id")
               and e["detail"].get("task")]
        if not ids:
            raise RuntimeError("the task's id was not recorded, so it cannot be deleted from here")
        for t in ids:
            delete_task(crm, t)
        return "%d task(s) deleted" % len(ids)
    if what == "task_delete":
        # The deleted task is gone for good; a plain task with the same title and date stands in.
        create_task(crm, u, (before or {}).get("title") or "Task", due=(before or {}).get("due") or "")
        return "task created again"
    if what == "add_lists":
        remove_lists(crm, u, list(after or []))
        return "list(s) removed"
    if what == "remove_lists":
        add_lists(crm, u, list(before or []))
        return "list(s) added back"
    raise RuntimeError("no undo is written for %r" % what)
