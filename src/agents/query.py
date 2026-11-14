"""Read-only questions over the account and the brain's own tables, for every employee.

Why this exists: the owner's most common Claude Code request is an ad hoc question over the account
("how many Probate records have no phone", "which stages ran today and what did they find", "how
many single-family sales in 37917 in the last 6 months"). The team could run its workflows and look
up ONE record, so in Slack those questions had no answer at all. These four tools give every
employee a bounded, read-only way to ask them:

  * records_query   the DataSift records search (POST /api/internal/property/ with the GET
                    override, documented as the list endpoint's query) through the budgeted
                    autopilot client: a count and at most 25 compact rows, filtered by a saved
                    preset and/or filter keys proven to change the count;
  * siftmap_count   a count from SiftMap's documented search for ONE area (a ZIP, a county, a box):
                    count only, one request, never a detail read, because Acceptable use calls
                    detail pulls in volume without adding the records scraping;
  * activity_query  the brain's own copy of the account log (ap_log): calls, texts, status changes,
                    assignments, tags and lists added, by day, caller or type. No request at all;
  * team_query      the brain's own tables: each stage's last run, tasks and failures, staged
                    batches, spend. No request at all.

The rules each one keeps, and the failure each rule prevents:
  * Only filter keys proven to change the count are sent (PROVEN_KEYS). The records search and
    SiftMap both IGNORE a key they do not know, so a misspelled or guessed key widens the answer
    instead of failing, and a count that silently dropped half the question reads exactly like a
    real one. A question that needs a filter the search cannot express is refused with one
    sentence that says so (HINTS), never answered wider.
  * A saved preset's own filters pass through untouched (the app wrote them), with its relative
    date windows converted the way the stages convert them (autopilot/windows.py).
  * Every request goes through the budgeted client and counts against the app's shared share of
    the account's 120 a minute, and one tool call makes at most REQUESTS_PER_CALL of them.
  * Nothing here writes. The record client a question sees (_Reads) has a search and a GET and
    nothing else, so no input can turn a question into a change.
  * No phone number, and no do-not-call flag tied to a number, ever leaves this module: rows are
    built field by field from a whitelist, and the numbers in the log copy stay in the database.
"""
from __future__ import annotations

import copy
import difflib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import store
from .autopilot import activity, windows
from .autopilot.crm import rows_of, search_shape

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

CRM = None      # the selftest injects a fake; otherwise the budgeted autopilot client on the Open API key
MAP = None      # the selftest injects a fake SiftMap search; otherwise siftmap_standalone on the key, budgeted
NOW = None      # the selftest pins the clock
POLICY = None   # the selftest injects a playbook; otherwise team/autopilot.yaml

TZ = "America/New_York"         # the business zone when the playbook names none (_zone reads the playbook's)
ROWS_MAX = 25                   # example rows one answer may carry
ROWS_DEFAULT = 10
REQUESTS_PER_CALL = 8           # DataSift requests one tool call may make, whatever it was asked
LIST_MAX = 20                   # titles in one filter
GROUP_LINES = 40                # lines in one grouped answer
CONVERSATION_S = 60             # an answered call this long is a conversation (the daily capture's rule)
BOX_MAX_DEG = 5                 # a box wider or taller than this is a pasting mistake, not an area
CREATED_FLOOR = "2000-01-01"    # a created range with no start begins here (the search is sent both ends)
SOLD_FLOOR = "1900-01-01"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_HELP = "YYYY-MM-DD, or a count back from today such as 7-days, 2-weeks, 3-months, today or yesterday"

# The records search keys this module sends, each proven by a count that moved (or read live by the
# stages that rely on it). Anything else the search would quietly ignore.
#   property_type             clean or incomplete, the app's two record tabs (idle_records reads both)
#   any_lists, all_lists      the stages; the preset tool's live watch counts (all_lists: FTM on both
#                             Obituary and Probate read 137)
#   any_tags, all_tags        the stages
#   any_property_status       Phase 0: works as a positive filter; must_not holds it too
#   created, last_sold        the sold guard: [from, to], and a same-day pair matches nothing
#   assigned_to               the autopilot: JSON null is unassigned, any other value must be a uuid
#   owners                    crm.owner_records: an owner with 5 properties returns exactly 5
#   phone, skiptraced         the account's own preset grammar (0 or 1), the published preset example
#   predictivecall_attempts,  the dialer's and the text counters, [lo, hi], as the Priority 1 presets
#   sms_attempts              use them; an open end is null
#   must_not                  nested INSIDE must (a sibling 400s), proven by count 1,198 to 1,126
PROVEN_KEYS = ("property_type", "any_lists", "all_lists", "any_tags", "all_tags", "any_property_status",
               "created", "last_sold", "assigned_to", "owners", "phone", "skiptraced", "predictivecall_attempts",
               "sms_attempts", "must_not")
KEY_WORDS = {"any_lists": "lists", "all_lists": "lists", "any_tags": "tags", "all_tags": "tags",
             "any_property_status": "status", "created": "the date added", "last_sold": "the last sale date",
             "assigned_to": "assignee", "owners": "owner", "phone": "phone", "skiptraced": "skip trace",
             "predictivecall_attempts": "the call counter", "sms_attempts": "the text counter"}

WORKING_STATUSES = ["prospecting", "Deep Prospecting", "Exhausted", "Return Mail"]
STATUS_WORDS = {"dnc": "DNC", "opt_out": "Opt Out", "new_lead": "New Lead", "follow_up": "Follow Up"}

DIAL, ANSWERED = activity.DIAL, "owner.call.answered"
INBOUND = ("owner.call.received", "owner.call.missed")


class Refused(ValueError):
    """A question this module will not answer as asked. The message is one plain sentence the
    employee can repeat, and it names what to ask instead when there is something."""


class _Nothing(Exception):
    """The ask and the preset cannot both hold for any record (two statuses, two ranges that never
    meet): the answer is zero, known without a search."""


# ------------------------------------------------------------------------------- inputs
STR, STRS, BOOL, INT = "string", "strings", "boolean", "integer"


def _enum(*values) -> tuple:
    return ("enum", tuple(values))


def _enums(*values) -> tuple:
    return ("enums", tuple(values))


GROUPS = ("leads", "finished", "working")
INVESTOR_TYPES = ("flip", "rental", "wholesale", "wholetail", "pending")
WHAT = ("calls", "texts", "status_changes", "assignments", "tags", "lists", "events")
BY = ("total", "day", "caller", "status", "source", "assignee", "tag", "list", "type")
BY_FOR = {"calls": ("caller", "day", "total"), "texts": ("day", "total"),
          "status_changes": ("status", "day", "source", "total"), "assignments": ("assignee", "day", "total"),
          "tags": ("tag", "day", "total"), "lists": ("list", "day", "total"), "events": ("type", "day", "total")}

SPECS: dict[str, dict] = {
    "records_query": {
        "description": (
            "Count records in this DataSift account, with up to 25 example rows (street, city, ZIP, status, how many "
            "lists and tags; never a phone number). Filter by a saved preset's exact title and/or lists, tags, "
            "statuses, the date added or last sold, the assignee, the owner of a record, has a phone, skip traced, "
            "the call or text counter; exclude lists, tags or statuses. It has no ZIP, value or equity filter. "
            "Free, read-only, a few requests. Example, Probate records with no phone: lists=[Probate], "
            "has_phone=false."),
        "inputs": {
            "preset": (STR, "A saved records preset by its exact title (case does not matter); its filters are the "
                            "base and anything else narrows it"),
            "lists": (STRS, "On any of these lists (titles)"),
            "all_lists": (STRS, "On every one of these lists"),
            "tags": (STRS, "Carries any of these tags (titles)"),
            "all_tags": (STRS, "Carries every one of these tags"),
            "statuses": (STRS, "In any of these statuses, as the app names them (New Lead, Not Interested, Dead Lead)"),
            "status_group": (_enum(*GROUPS), "Any lead status, any finished one (sold, dead, not interested, DNC), "
                                             "or a working one (prospecting, Deep Prospecting, Exhausted)"),
            "not_lists": (STRS, "On none of these lists"),
            "not_tags": (STRS, "Carries none of these tags"),
            "not_statuses": (STRS, "In none of these statuses"),
            "not_status_group": (_enum(*GROUPS), "In no status of this group"),
            "created_from": (STR, "Added to the account on or after: " + DATE_HELP),
            "created_to": (STR, "Added on or before (same forms)"),
            "sold_from": (STR, "Last sale on or after (same forms)"),
            "sold_to": (STR, "Last sale on or before (same forms)"),
            "assigned": (STR, "unassigned, a user uuid, or a teammate's first name"),
            "owner_of": (STR, "A record uuid: count every record that record's owner holds"),
            "has_phone": (BOOL, "true: the owner has a phone; false: no phone (DataSift's own phone filter)"),
            "skip_traced": (BOOL, "true: skip traced; false: never skip traced"),
            "call_attempts": (STR, "The dialer's call counter: 0, a range like 1-3, or 5+"),
            "text_attempts": (STR, "The text counter: 0, a range like 1-3, or 5+"),
            "include_incomplete": (BOOL, "Also count DataSift's incomplete records (the app's second tab)"),
            "rows": (INT, "Example rows to return, 0 to 25 (default 10)"),
        },
        "required": [],
    },
    "siftmap_count": {
        "description": (
            "Count properties in SiftMap's nationwide data (in this account or not) for ONE area: a ZIP, a county "
            "with its state, or a box lat_min,lat_max,lon_min,lon_max. Filters: single family, vacant land, the "
            "last sale's date range, the last sale's investor type (flip, rental, wholesale, wholetail, pending), "
            "leave out family transfers, one per owner, in or not in this account. Count only, one request, never "
            "a detail read. Example, single-family sales in 37917 in 6 months: zip=37917, single_family=true, "
            "sold_from=6-months."),
        "inputs": {
            "zip": (STR, "A 5-digit ZIP"),
            "county": (STR, "A county name without the word County, with state"),
            "state": (STR, "Two-letter state, with county"),
            "box": (STR, "lat_min,lat_max,lon_min,lon_max"),
            "single_family": (BOOL, "Single-family houses only"),
            "land": (BOOL, "Vacant residential land only"),
            "sold_from": (STR, "Last sale on or after: " + DATE_HELP),
            "sold_to": (STR, "Last sale on or before (same forms)"),
            "investor_types": (_enums(*INVESTOR_TYPES), "The last sale was one of these investor transactions"),
            "exclude_family_transfers": (BOOL, "Leave out a last sale between family members"),
            "one_per_owner": (BOOL, "Count one property per owner"),
            "in_account": (_enum("in", "not_in"), "in: only properties already in this account; not_in: only new ones"),
        },
        "required": [],
    },
    "activity_query": {
        "description": (
            "Counts from the brain's own copy of this account's activity log, with no DataSift request: calls "
            "(dials, answered, conversations of a minute or more, talk minutes, owners dialed), texts, status "
            "changes, assignments, and tags or lists added, by day, by caller or by type, over business days. "
            "Example, this week's dials by caller: what=calls, since=7-days, by=caller."),
        "inputs": {
            "what": (_enum(*WHAT), "Which activity to count"),
            "since": (STR, "First business day: " + DATE_HELP + " (default today)"),
            "until": (STR, "Last business day, same forms (default today)"),
            "by": (_enum(*BY), "How to group: calls by caller, day or total; texts by day or total; status changes "
                               "by status, day or source; assignments by assignee; tags by tag; lists by list; "
                               "events by type"),
            "caller": (STR, "Calls by one caller, by first name"),
            "status": (STR, "Status changes to this status only"),
            "tag": (STR, "Tags added: this tag only"),
            "list": (STR, "Lists added: this list only"),
        },
        "required": ["what"],
    },
    "team_query": {
        "description": (
            "What the team and the autopilot did, from the brain's own tables, with no DataSift request: each "
            "stage's last run and what it found (stages, or one stage), tasks by status and the recent failures, "
            "batches staged for a person to release, and spend by workflow and by stage. Example, which stages "
            "ran today and what they found: about=stages."),
        "inputs": {
            "about": (_enum("stages", "stage", "tasks", "batches", "spend"), "What to report"),
            "stage": (STR, "A stage id from the playbook, with about=stage"),
            "since": (STR, "Tasks and spend from this business day: " + DATE_HELP + " (default today)"),
            "only": (_enum("today", "problems", "all"), "Stages: those that ran today (default), those whose last "
                                                         "run had a problem, or all"),
            "status": (STR, "Tasks in this status (queued, running, waiting, done, failed), or batches in this "
                            "status (staged by default, any for all)"),
        },
        "required": ["about"],
    },
}
ORDER = ("records_query", "siftmap_count", "activity_query", "team_query")

# Inputs a model reaches for that a tool does not have, answered with what to ask instead.
_AREA = ("The records search has no ZIP, city or county filter this tool can trust. siftmap_count with "
         "in_account=in counts this account's properties in one ZIP or county.")
HINTS = {
    "records_query": {
        "zip": _AREA, "zipcode": _AREA, "zip_code": _AREA, "postal_code": _AREA, "city": _AREA, "county": _AREA,
        "state": _AREA,
        "status": "Use statuses, a list of statuses as the app names them.",
        "list": "Use lists, a list of list titles.", "tag": "Use tags, a list of tag titles.",
        "phone": "Use has_phone (true or false).", "phones": "Use has_phone (true or false).",
        "value": "Value and equity are not filters this tool sends; use a saved preset that has them.",
        "equity": "Value and equity are not filters this tool sends; use a saved preset that has them.",
        "deceased": "The search's deceased filter also drops every record whose mark is blank, so it is not "
                    "offered here; use a saved preset that has it.",
        "owner": "Use owner_of with a record uuid: it counts every record that record's owner holds.",
        "limit": "Use rows (0 to 25).", "offset": "This tool shows at most the first 25 rows.",
    },
    "siftmap_count": {
        "city": "Use a zip, a county with its state, or a box.", "address": "Use a zip, a county with its state, or a box.",
        "radius": "Use a box: lat_min,lat_max,lon_min,lon_max.",
        "value": "A value filter is not one this tool sends; ask for a count without it.",
        "zips": "One ZIP at a time; ask again for the next one.",
    },
    "activity_query": {
        "number": "Phone numbers are never shown; ask for counts by day or by caller.",
        "phone": "Phone numbers are never shown; ask for counts by day or by caller.",
        "from": "Use since and until.", "to": "Use since and until.",
    },
    "team_query": {},
}


def schema(name: str) -> dict:
    """The JSON schema an employee's tool carries, built from the same spec the inputs are checked
    against, so the two cannot drift."""
    props = {}
    for key, (kind, desc) in SPECS[name]["inputs"].items():
        if kind == STR:
            p: dict = {"type": "string"}
        elif kind == STRS:
            p = {"type": "array", "items": {"type": "string"}}
        elif kind == BOOL:
            p = {"type": "boolean"}
        elif kind == INT:
            p = {"type": "integer"}
        elif kind[0] == "enum":
            p = {"type": "string", "enum": list(kind[1])}
        else:
            p = {"type": "array", "items": {"type": "string", "enum": list(kind[1])}}
        p["description"] = desc
        props[key] = p
    return {"type": "object", "properties": props, "required": list(SPECS[name]["required"]),
            "additionalProperties": False}


def tools() -> list[dict]:
    """name, description and schema of each tool, in the order the employees see them."""
    return [{"name": n, "description": SPECS[n]["description"], "schema": schema(n)} for n in ORDER]


def enabled() -> bool:
    """AGENTS_QUERY_TOOLS=0 takes the four tools off every employee (for an install that wants the
    request budget kept for the stages)."""
    return (os.environ.get("AGENTS_QUERY_TOOLS") or "1").strip().lower() not in ("0", "off", "no", "false")


def _word(v: str) -> str:
    return re.sub(r"[\s-]+", "_", str(v).strip().lower())


def _bool(v, key: str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    raise Refused("%s takes true or false, not %r." % (key, v))


def clean(name: str, args) -> dict:
    """The inputs as the query reads them, or Refused naming the one that is wrong. An input the
    tool does not have is refused rather than dropped: dropping it would answer a wider question
    than the one asked."""
    spec = SPECS[name]["inputs"]
    if not isinstance(args, dict):
        raise Refused("%s takes named inputs." % name)
    out: dict = {}
    for key, v in args.items():
        if key not in spec:
            hint = HINTS.get(name, {}).get(str(key).strip().lower())
            raise Refused(hint or "%s has no input %r; it takes: %s." % (name, key, ", ".join(spec)))
        if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, tuple)) and not v):
            continue
        kind = spec[key][0]
        if kind == STR:
            if isinstance(v, (list, tuple)):
                if len(v) != 1:
                    raise Refused("%s takes one value, not a list." % key)
                v = v[0]
            if isinstance(v, (dict, bool)):
                raise Refused("%s takes text, not %r." % (key, v))
            out[key] = str(v).strip()[:300]
        elif kind == STRS:
            items = [v] if isinstance(v, (str, int, float)) else v
            if not isinstance(items, (list, tuple)):
                raise Refused("%s takes a list of titles." % key)
            vals = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
            if len(vals) > LIST_MAX:
                raise Refused("%s takes at most %d titles." % (key, LIST_MAX))
            if vals:
                out[key] = vals
        elif kind == BOOL:
            out[key] = _bool(v, key)
        elif kind == INT:
            try:
                if isinstance(v, bool):
                    raise ValueError(v)
                out[key] = int(float(str(v).strip()))
            except ValueError:
                raise Refused("%s takes a whole number, not %r." % (key, v)) from None
        elif kind[0] == "enum":
            w = _word(v)
            if w not in kind[1]:
                raise Refused("%s is one of: %s (not %r)." % (key, ", ".join(kind[1]), v))
            out[key] = w
        else:
            items = [x for x in re.split(r"[,;]", v)] if isinstance(v, str) else list(v)
            items = [x for x in items if str(x).strip()]
            vals = []
            for x in items:
                w = _word(x)
                if w not in kind[1]:
                    raise Refused("%s takes these: %s (not %r)." % (key, ", ".join(kind[1]), x))
                vals.append(w)
            out[key] = list(dict.fromkeys(vals))
    for key in SPECS[name]["required"]:
        if key not in out:
            raise Refused("%s needs %s: %s." % (name, key, spec[key][1]))
    return out


# ------------------------------------------------------------------------------- clock and words
def _now() -> datetime:
    return NOW if NOW is not None else datetime.now(timezone.utc)


def _zone() -> ZoneInfo:
    """The playbook's business zone, read at most once a minute (America/New_York when the playbook
    names none): every day an answer counts is cut on it, the way the stages cut theirs."""
    hit = _INDEX.get("zone")
    if not hit or time.time() - hit[0] > 60:
        name = str(getattr(_policy(), "tz", "") or TZ)
        try:
            zone = ZoneInfo(name)
        except Exception:  # noqa: BLE001  (an unknown zone name falls back to the default)
            zone = ZoneInfo(TZ)
        hit = _INDEX["zone"] = (time.time(), zone)
    return hit[1]


def _today() -> date:
    return _now().astimezone(_zone()).date()


def _local(dt: datetime) -> datetime:
    return dt.astimezone(_zone())


def _day_of(dt: datetime) -> str:
    return _local(dt).date().isoformat()


def _bounds(since: date, until: date) -> tuple[datetime, datetime]:
    """Business-local days as the UTC moments the log is stored in: since 00:00 to the day after until."""
    z = _zone()
    start = datetime.combine(since, dtime.min, tzinfo=z).astimezone(timezone.utc)
    end = datetime.combine(until + timedelta(days=1), dtime.min, tzinfo=z).astimezone(timezone.utc)
    return start, end


def _date(value, field: str, today: date) -> date:
    s = str(value).strip().lower()
    try:
        if DATE_RE.match(s):
            return date.fromisoformat(s)
        return date.fromisoformat(windows.token_date(s, today))
    except ValueError:
        raise Refused("%s %r is not a date: use %s." % (field, value, DATE_HELP)) from None


def _counter(value, field: str) -> list:
    s = str(value).strip().replace(" ", "")
    m = re.fullmatch(r"(\d+)(?:-(\d+)|(\+))?", s)
    if not m:
        raise Refused("%s is a number, a range like 1-3, or 5+ (not %r)." % (field, value))
    lo = int(m.group(1))
    if m.group(3):
        return [lo, None]
    hi = int(m.group(2)) if m.group(2) else lo
    if hi < lo:
        raise Refused("%s runs low to high, so %r reads backwards." % (field, value))
    return [lo, hi]


def _flat(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def status_word(s) -> str:
    """A stored status as the app shows it: new_lead is New Lead, Dead Lead stays Dead Lead."""
    if not s:
        return "no status"
    s = str(s)
    if s in STATUS_WORDS:
        return STATUS_WORDS[s]
    return s.replace("_", " ").title() if s == s.lower() else s


def families() -> dict:
    from .autopilot import lanes
    return {"leads": list(lanes.LEAD_STATUS_VALUES), "finished": list(lanes.GONE_STATUS_VALUES),
            "working": list(WORKING_STATUSES)}


def known_statuses() -> list[str]:
    """Every status this account is known to store, in the stored spelling the search needs (the
    same three families the lanes and the preset tool use). The published global-status route names
    only the lead pipeline's, so it is the second place to look, never the first."""
    f = families()
    return list(dict.fromkeys(f["leads"] + f["finished"] + f["working"]))


def _close(key: str, titles: dict) -> str:
    near = difflib.get_close_matches(key, list(titles), n=3, cutoff=0.6)
    names = []
    for k in near:
        v = titles[k]
        names.append(v[0][0] if isinstance(v, list) else v[0])
    return ("; close titles: %s" % ", ".join(names)) if names else ""


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


# ------------------------------------------------------------------------------- clients
class _Reads:
    """The record client as a question sees it: a search and a GET, each one counted, and nothing
    that writes. A question that would need more than `cap` requests stops and says so."""

    def __init__(self, crm, cap: int = REQUESTS_PER_CALL):
        self._crm, self.cap, self.n = crm, int(cap), 0

    def _take(self) -> None:
        if self.n >= self.cap:
            raise Refused("That question needed more than %d DataSift requests, so it stopped; ask a narrower one."
                          % self.cap)
        self.n += 1

    def search(self, must: dict, limit: int) -> tuple[int, list]:
        self._take()
        return self._crm.search(must, limit=int(limit), offset=0)

    def get(self, path: str):
        self._take()
        return self._crm.call(path)


def _crm():
    if CRM is not None:
        return CRM
    from .autopilot.crm import CRM as Client
    return Client()


class _SiftMap:
    """SiftMap's documented search on the Open API key, each request counted against the app's shared
    budget, the same counter every DataSift read uses."""

    def __init__(self):
        from siftmap_standalone import SiftMapClient
        key = (os.environ.get("REISIFT_API_KEY") or "").strip()
        if not key:
            raise Refused("REISIFT_API_KEY is not set, and SiftMap needs the account's Open API key.")
        self.client = SiftMapClient(key, min_interval=0.5)

    def search(self, body: dict) -> dict:
        from .autopilot import budget
        budget.acquire()
        return self.client.search(body)


def _map():
    return MAP if MAP is not None else _SiftMap()


def _policy():
    if POLICY is not None:
        return POLICY
    from .autopilot import policy
    try:
        return policy.load()
    except Exception as exc:  # noqa: BLE001  (a playbook that does not load must not stop a question)
        log.warning("the playbook did not load for a team query: %s", exc)
        return None


# ------------------------------------------------------------------------------- title indexes
# The list, tag and preset titles are read once and kept for AGENTS_QUERY_INDEX_TTL_S (600), so a
# turn that asks three questions reads each index once. A title that misses is read again once when
# the copy is over a minute old, so a list made a moment ago is still found.
_INDEX: dict = {}


def reset() -> None:
    _INDEX.clear()


def _index(kind: str, load, fresh: bool = False):
    hit = _INDEX.get(kind)
    if hit and not fresh and time.time() - hit[0] < _setting("AGENTS_QUERY_INDEX_TTL_S", 600):
        return hit[1]
    data = load()
    _INDEX[kind] = (time.time(), data)
    return data


def _stale(kind: str) -> bool:
    hit = _INDEX.get(kind)
    return not hit or time.time() - hit[0] > 60


def _lists(reads: _Reads, fresh: bool = False) -> dict:
    """lower title -> (title, uuid). List titles are unique ignoring case and surrounding spaces."""
    def load():
        out = {}
        for r in rows_of(reads.get("/api/internal/list/?offset=0&limit=1000")):
            t = str(r.get("title") or "").strip() if isinstance(r, dict) else ""
            if t and r.get("uuid"):
                out.setdefault(t.lower(), (t, r["uuid"]))
        return out
    return _index("lists", load, fresh)


def _tags(reads: _Reads, fresh: bool = False) -> dict:
    """lower title -> [(title, uuid), ...]. Tag case is significant: an account can hold both
    Foreclosure and foreclosure, so the lower-case key can hold two tags."""
    def load():
        out: dict = {}
        for r in rows_of(reads.get("/api/internal/tag/?offset=0&limit=10000&ordering=title")):
            t = str(r.get("title") or "").strip() if isinstance(r, dict) else ""
            if t and r.get("uuid"):
                out.setdefault(t.lower(), []).append((t, r["uuid"]))
        return out
    return _index("tags", load, fresh)


def _presets(reads: _Reads, fresh: bool = False) -> dict:
    """lower title -> [(title, must), ...] for every saved records preset, from ONE request: the
    documented flat listing carries each preset's filters, where the folder walk the stages use
    costs a request per folder (about 21 on ty+2)."""
    def load():
        out: dict = {}
        for p in rows_of(reads.get("/api/internal/filter-preset/?limit=999")):
            if not isinstance(p, dict) or str(p.get("type") or "properties") != "properties":
                continue
            t = str(p.get("title") or "").strip()
            f = p.get("filters") if isinstance(p.get("filters"), dict) else {}
            if t:
                out.setdefault(t.lower(), []).append((t, f.get("must") if isinstance(f.get("must"), dict) else {}))
        return out
    return _index("presets", load, fresh)


def _published_statuses(reads: _Reads) -> list[str]:
    def load():
        out = []
        for row in rows_of(reads.get("/api/internal/global-status/")):
            for s in (row.get("statuses") or []) if isinstance(row, dict) else []:
                if isinstance(s, str) and s.strip():
                    out.append(s.strip())
        return out
    return _index("statuses", load)


def _list_id(reads: _Reads, title: str) -> tuple[str, str]:
    key = title.strip().lower()
    idx = _lists(reads)
    if key not in idx and _stale("lists"):
        idx = _lists(reads, fresh=True)
    if key not in idx:
        raise Refused("No list in this account is titled %r%s." % (title, _close(key, idx)))
    return idx[key]


def _tag_id(reads: _Reads, title: str) -> tuple[str, str]:
    key = title.strip().lower()
    idx = _tags(reads)
    if key not in idx and _stale("tags"):
        idx = _tags(reads, fresh=True)
    cands = idx.get(key) or []
    exact = [c for c in cands if c[0] == title.strip()]
    if exact:
        return exact[0]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise Refused("Two tags differ only in case (%s); give the exact one." % " and ".join(repr(c[0]) for c in cands))
    raise Refused("No tag in this account is titled %r%s." % (title, _close(key, idx)))


def _status_value(reads: _Reads, word: str) -> str:
    want = _flat(word)
    for s in known_statuses():
        if _flat(s) == want:
            return s
    for s in _published_statuses(reads):
        if _flat(s) == want:
            return s
    raise Refused("%r is not a status this account uses. Known: %s." % (
        word, ", ".join(status_word(s) for s in known_statuses())))


# ------------------------------------------------------------------------------- teammates
def _user_setting() -> dict:
    """AGENTS_QUERY_USERS: "Adriana=<uuid>, Marcus=<uuid>". Listing the account's users is not on the
    Open API key, so a teammate is named by uuid, here or from the call log."""
    out = {}
    for part in re.split(r"[,;\n]", os.environ.get("AGENTS_QUERY_USERS") or ""):
        if "=" in part:
            name, uid = (x.strip() for x in part.split("=", 1))
            if name and UUID_RE.match(uid):
                out.setdefault(name.lower(), {})[uid] = name
    return out


def _log_users() -> dict:
    """lower first name -> {user uuid: name}, read from the answered calls in the brain's log copy:
    the dialer writes the caller's DataSift user (first name and uuid) on every answer."""
    out: dict = {}
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT data FROM ap_log WHERE event_type IN (?, ?) ORDER BY at DESC LIMIT 20000",
                         (ANSWERED, DIAL)).fetchall()
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        name, uid = str(d.get("caller") or "").strip(), str(d.get("caller_uuid") or "").strip()
        if name and UUID_RE.match(uid):
            out.setdefault(name.lower(), {})[uid] = name
    return out


def _people() -> dict:
    found = _log_users()
    for k, v in _user_setting().items():
        found[k] = v            # a name the operator set wins over one read from the log
    return found


def names_by_uuid() -> dict:
    out = {}
    for byname in _people().values():
        for uid, name in byname.items():
            out.setdefault(uid, name)
    return out


def _assignee(text: str) -> tuple:
    s = text.strip()
    if s.lower() in ("unassigned", "nobody", "no one", "none"):
        return None, "unassigned"
    if UUID_RE.match(s):
        return s, names_by_uuid().get(s, s)
    hits = _people().get(s.lower()) or {}
    if len(hits) == 1:
        uid, name = next(iter(hits.items()))
        return uid, name
    if hits:
        raise Refused("Two DataSift users named %s appear in the call log; give the user uuid." % s)
    raise Refused("I can filter by a teammate only by user uuid, or by a first name set in AGENTS_QUERY_USERS or "
                  "seen on a call in the log; the account's user list is not on the Open API key.")


# ------------------------------------------------------------------------------- records_query
def _end(a, b, pick):
    vals = [x for x in (a, b) if x is not None]
    return pick(vals) if vals else None


def _and(key: str, have, want):
    """One filter key the preset and the ask both set, as the single value that means both."""
    if key in ("all_lists", "all_tags"):
        return list(dict.fromkeys(list(have) + list(want)))
    if key in ("any_property_status", "owners"):
        both = [x for x in have if x in set(want)]
        if not both:
            raise _Nothing(key)
        return both
    if key in ("created", "last_sold", "predictivecall_attempts", "sms_attempts"):
        if not (isinstance(have, list) and len(have) == 2):
            raise Refused("The preset sets %s in a form this tool cannot narrow; ask without it, or without the "
                          "preset." % KEY_WORDS[key])
        lo, hi = _end(have[0], want[0], max), _end(have[1], want[1], min)
        dates = key in ("created", "last_sold")
        if lo is not None and hi is not None and (lo >= hi if dates else lo > hi):
            raise _Nothing(key)
        return [lo, hi]
    if key == "assigned_to" and isinstance(have, list):
        # A preset may hold several assignees; the ask names one of them or none of them.
        if want in have:
            return want
        raise _Nothing(key)
    if (int(bool(have)) if key in ("phone", "skiptraced") else have) != \
            (int(bool(want)) if key in ("phone", "skiptraced") else want):
        raise _Nothing(key)
    return have


def _merge(base: dict, ask: dict, neg: dict, title: str) -> dict:
    """The preset's filters AND the ask's. Exclusions add up. Two any-of lists cannot be one filter
    unless one side names a single title, which then becomes an every-of filter."""
    m = copy.deepcopy(base or {})
    for key, want in ask.items():
        if key not in m:
            m[key] = want
            continue
        have = m[key]
        if key in ("any_lists", "any_tags"):
            every = "all_" + key[4:]
            if len(want) == 1:
                m[every] = list(dict.fromkeys(list(m.get(every) or []) + list(want)))
            elif isinstance(have, list) and len(have) == 1:
                m[every] = list(dict.fromkeys(list(m.get(every) or []) + list(have)))
                m[key] = want
            else:
                raise Refused("The preset %s already takes any of several %s, and the search cannot hold a second "
                              "any-of list; ask with one, or without the preset." % (title, KEY_WORDS[key]))
            continue
        m[key] = _and(key, have, want)
    if neg:
        mn = dict(m["must_not"]) if isinstance(m.get("must_not"), dict) else {}
        for key, vals in neg.items():
            mn[key] = list(dict.fromkeys(list(mn.get(key) or []) + list(vals)))
        m["must_not"] = mn
    return m


def compact_row(r: dict) -> dict:
    """A search row as an employee may see it. Built field by field: the row also carries the owner's
    name, one phone number and that number's do-not-call flag, none of which may leave."""
    a = r.get("address") or {}
    return {"uuid": r.get("uuid") or "", "street": a.get("street") or "", "city": a.get("city") or "",
            "zip": str(a.get("zip5") or a.get("postal_code") or "")[:5], "status": status_word(r.get("status")),
            "lists": r.get("list_count"), "tags": r.get("tag_count"), "has_phones": r.get("has_phones")}


def _preset(reads: _Reads, asked: str, today: date) -> tuple[str, dict, list[str]]:
    key = asked.strip().lower()
    idx = _presets(reads)
    if key not in idx and _stale("presets"):
        idx = _presets(reads, fresh=True)
    cands = idx.get(key) or []
    exact = [c for c in cands if c[0] == asked.strip()]
    if not cands:
        raise Refused("No saved records preset is titled %r%s." % (asked, _close(key, idx)))
    if not exact and len(cands) > 1:
        raise Refused("Two presets differ only in case (%s); give the exact title." % " and ".join(repr(c[0]) for c in cands))
    title, must = (exact or cands)[0]
    shaped = search_shape(must or {})
    resolved, notes = windows.resolve(shaped, today)
    out = []
    for n in notes:
        if "dropped" in n:
            out.append("The preset's window %s could not be read, so it was left out and this count is wider than "
                       "the app shows." % n.split(" dropped")[0])
    if str(resolved.get("show_properties") or "") == "one_per_owner":
        out.append("The preset shows one property per owner, and the count follows that setting.")
    return title, resolved, out


def records_query(args: dict) -> dict:
    a = clean("records_query", args)
    today = _today()
    rows_wanted = a.get("rows", ROWS_DEFAULT)
    limits: list[str] = []
    if rows_wanted > ROWS_MAX or rows_wanted < 0:
        limits.append("rows is 0 to %d, so %d were asked for and %d are shown at most." % (
            ROWS_MAX, rows_wanted, ROWS_MAX))
        rows_wanted = max(0, min(ROWS_MAX, rows_wanted))
    applied: dict = {}
    ask: dict = {}
    neg: dict = {}

    # Everything that needs no request is checked first, so a wrong date or counter costs nothing.
    for field, key, floor in (("created", "created", CREATED_FLOOR), ("sold", "last_sold", SOLD_FLOOR)):
        lo, hi = a.get(field + "_from"), a.get(field + "_to")
        if lo is None and hi is None:
            continue
        d_lo = _date(lo, field + "_from", today) if lo is not None else None
        d_hi = _date(hi, field + "_to", today) if hi is not None else today
        if d_lo and d_lo > d_hi:
            raise Refused("%s_from is after %s_to." % (field, field))
        # The search's range ends before its second date (a same-day pair matches nothing), so the
        # last asked day is sent as the day after it.
        ask[key] = [d_lo.isoformat() if d_lo else floor, (d_hi + timedelta(days=1)).isoformat()]
        applied[key] = [d_lo.isoformat() if d_lo else "any", d_hi.isoformat()]
    for field, key in (("call_attempts", "predictivecall_attempts"), ("text_attempts", "sms_attempts")):
        if field in a:
            ask[key] = _counter(a[field], field)
            applied[field] = a[field]
    for field, key in (("has_phone", "phone"), ("skip_traced", "skiptraced")):
        if field in a:
            ask[key] = 1 if a[field] else 0
            applied[field] = a[field]
    if "assigned" in a:
        uid, who = _assignee(a["assigned"])
        ask["assigned_to"] = uid
        applied["assigned"] = who

    reads = _Reads(_crm())
    try:
        for field, key, find in (("lists", "any_lists", _list_id), ("all_lists", "all_lists", _list_id),
                                 ("tags", "any_tags", _tag_id), ("all_tags", "all_tags", _tag_id),
                                 ("not_lists", "any_lists", _list_id), ("not_tags", "any_tags", _tag_id)):
            if field not in a:
                continue
            found = [find(reads, t) for t in a[field]]
            (neg if field.startswith("not_") else ask)[key] = [u for _, u in found]
            applied[field] = [t for t, _ in found]
        fam = families()
        for field, group, target in (("statuses", "status_group", ask), ("not_statuses", "not_status_group", neg)):
            vals = [_status_value(reads, w) for w in a.get(field) or []] + list(fam.get(a.get(group) or "", []))
            if vals:
                target["any_property_status"] = list(dict.fromkeys(vals))
            if field in a:
                applied[field] = [status_word(v) for v in vals[:len(a[field])]]
            if group in a:
                applied[group] = a[group]
        if a.get("owner_of"):
            u = a["owner_of"]
            if not UUID_RE.match(u):
                raise Refused("owner_of takes a record uuid; records_query rows and record_lookup show it.")
            try:
                rec = reads.get("/api/internal/property/%s/" % u) or {}
            except Refused:
                raise
            except Exception as exc:  # noqa: BLE001  (a 404 is a plain answer, anything else is reported)
                if getattr(exc, "code", None) == 404:
                    raise Refused("No record in this account has uuid %s." % u) from None
                raise
            owner = (rec.get("owner") or {}).get("uuid") or ""
            if not owner:
                raise Refused("Record %s has no owner on it." % u)
            ask["owners"] = [owner]
            applied["owner_of"] = ((rec.get("address") or {}).get("street") or u)

        base, title = {}, ""
        if a.get("preset"):
            title, base, notes = _preset(reads, a["preset"], today)
            applied["preset"] = title
            limits += notes
        try:
            must = _merge(base, ask, neg, title)
        except _Nothing as nothing:
            limits.append("The preset %s already limits %s in a way no record can also meet as asked, so the answer "
                          "is 0 with no search." % (title, KEY_WORDS.get(str(nothing), str(nothing))))
            return _records_out(today, 0, [], applied, "nothing searched", limits, reads.n, rows_wanted)

        types = ["clean", "incomplete"] if a.get("include_incomplete") else ["clean"]
        if "property_type" in must:
            types = [None]
            if a.get("include_incomplete"):
                limits.append("The preset sets its own record type, so include_incomplete was not applied.")
        total, rows = 0, []
        for ptype in types:
            m = dict(must) if ptype is None else dict(must, property_type=ptype)
            n, got = reads.search(m, limit=max(1, rows_wanted))
            total += int(n or 0)
            rows += list(got or [])
        counted = ("the record type the preset sets" if types == [None] else
                   "clean and incomplete records" if len(types) == 2 else
                   "clean records (incomplete records, the app's second tab, only with include_incomplete)")
        return _records_out(today, total, rows, applied, counted, limits, reads.n, rows_wanted)
    finally:
        log.debug("records_query made %d requests", reads.n)


def _records_out(today, total, rows, applied, counted, limits, requests, rows_wanted) -> dict:
    shown = [compact_row(r) for r in rows if isinstance(r, dict)][:rows_wanted]
    notes = list(limits)
    if shown:
        notes.append("The rows are the first ones the search returns, an example, not a ranking; a row's list "
                     "count includes DataSift's own classification lists.")
    if total > len(shown) and rows_wanted:
        notes.append("Showing %d of %d." % (len(shown), total))
    return {"tool": "records_query", "today": today.isoformat(), "count": total, "counted": counted,
            "applied": applied, "rows": shown, "limits": notes, "requests": requests}


# ------------------------------------------------------------------------------- siftmap_count
def _county(text: str, state: str) -> tuple[str, str]:
    t = text.strip()
    m = re.fullmatch(r"(.+?),\s*([A-Za-z]{2})", t)
    if m:
        t, state = m.group(1), state or m.group(2)
    t = re.sub(r"\s+(county|parish)$", "", t.strip(), flags=re.I).strip()
    if not t:
        raise Refused("county takes a county name, such as Knox.")
    st = (state or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", st):
        raise Refused("A county needs its two-letter state (state=TN), because county names repeat across states.")
    return t, st


def _box(text: str) -> tuple[list[dict], str]:
    """A box as the closed ring of {lat, lon} SiftMap's search takes (comp_package's shape)."""
    try:
        lat_min, lat_max, lon_min, lon_max = (float(x) for x in re.split(r"[,\s]+", text.strip()) if x)
    except ValueError:
        raise Refused("box is four numbers: lat_min,lat_max,lon_min,lon_max.") from None
    if not (-90 <= lat_min < lat_max <= 90 and -180 <= lon_min < lon_max <= 180):
        raise Refused("box is lat_min,lat_max,lon_min,lon_max, each low before high (for example "
                      "35.90,36.10,-84.00,-83.80).")
    # Two corners pasted as lat,lon,lat,lon or a longitude in a latitude's place can still read as a
    # valid box, just one a hemisphere tall. Nobody means that, so a box is a county or smaller.
    if lat_max - lat_min > BOX_MAX_DEG or lon_max - lon_min > BOX_MAX_DEG:
        raise Refused("That box spans more than %d degrees, which is a mistake or a whole state; the order is "
                      "lat_min,lat_max,lon_min,lon_max, and a county is the way to count something that big."
                      % BOX_MAX_DEG)
    ring = [{"lat": lat_max, "lon": lon_min}, {"lat": lat_max, "lon": lon_max}, {"lat": lat_min, "lon": lon_max},
            {"lat": lat_min, "lon": lon_min}, {"lat": lat_max, "lon": lon_min}]
    return ring, "the box %s to %s, %s to %s" % (lat_min, lat_max, lon_min, lon_max)


def siftmap_count(args: dict) -> dict:
    a = clean("siftmap_count", args)
    today = _today()
    areas = [k for k in ("zip", "county", "box") if a.get(k)]
    if not areas:
        raise Refused("Name one area: a zip, a county with its state, or a box. These counts are never nationwide.")
    if len(areas) > 1:
        raise Refused("Name one area at a time, not %s." % " and ".join(areas))
    if a.get("state") and not a.get("county"):
        raise Refused("state goes with county.")
    body: dict = {"size": 1, "result_index": 0}
    if a.get("zip"):
        z = re.sub(r"\D", "", a["zip"])[:9]
        if len(z) not in (5, 9):
            raise Refused("zip takes a 5-digit ZIP, not %r." % a["zip"])
        z = z[:5]
        body["addresses"] = [{"type": "zip", "search": z}]
        area = "ZIP %s" % z
    elif a.get("county"):
        name, st = _county(a["county"], a.get("state") or "")
        # The bare county name: "Knox" counts the county and "Knox County, TN" counts nothing.
        body["addresses"] = [{"type": "county", "search": name, "state": st}]
        area = "%s County, %s" % (name, st)
    else:
        body["polygon"], area = _box(a["box"])
    filters: dict = {}
    applied: dict = {}
    if a.get("single_family"):
        filters["type_single_family"] = True
        applied["single_family"] = True
    if a.get("land"):
        filters["type_land"] = True
        applied["land"] = True
    lo = _date(a["sold_from"], "sold_from", today) if a.get("sold_from") else None
    hi = _date(a["sold_to"], "sold_to", today) if a.get("sold_to") else None
    if lo and hi and lo > hi:
        raise Refused("sold_from is after sold_to.")
    if lo:
        filters["extra_last_sale_date_min"] = lo.isoformat()
        applied["sold_from"] = lo.isoformat()
    if hi:
        filters["extra_last_sale_date_max"] = hi.isoformat()
        applied["sold_to"] = hi.isoformat()
    if a.get("investor_types"):
        filters["extra_last_sale_investor_transaction_type"] = list(a["investor_types"])   # a list: a string 400s
        applied["investor_types"] = list(a["investor_types"])
    if a.get("exclude_family_transfers"):
        filters["extra_is_last_sale_interfamily"] = False
        applied["exclude_family_transfers"] = True
    if a.get("one_per_owner"):
        filters["extra_collapse_by_owner"] = True
        applied["one_per_owner"] = True
    if a.get("in_account"):
        filters["in_my_account_mode"] = a["in_account"]
        applied["in_account"] = a["in_account"]
    if filters:
        body["filters"] = filters
    r = _map().search(body) or {}
    total = r.get("total_results") if isinstance(r, dict) else None
    if not isinstance(total, int):
        raise Refused("SiftMap answered without a count, so there is nothing to report.")
    limits = ["Only filters proven to change the count are sent; SiftMap ignores a key it does not know."]
    if not a.get("in_account"):
        limits.append("SiftMap counts every property in the area, in this account or not.")
    if lo or hi:
        limits.append("A sale date filter reads each property's LAST recorded sale: a house sold twice counts once, "
                      "and deeds reach SiftMap weeks late.")
    if a.get("investor_types"):
        limits.append("The investor type labels the last sale: pending means an investor bought and still holds; "
                      "flip means the flip already sold to a homebuyer.")
    return {"tool": "siftmap_count", "today": today.isoformat(), "count": total, "area": area, "applied": applied,
            "limits": limits, "requests": 1}


# ------------------------------------------------------------------------------- activity_query
def _call_days(evs: list[dict]) -> tuple[dict, dict]:
    """(day -> caller -> numbers, day -> inbound calls), by the daily capture's rules
    (capture.day_rows): a dial is owner.call.made, an answer is the dialer's second event with the
    duration, an inbound call is not a dial, and a day's unnamed dials belong to its only named
    caller (the dialer names the caller on the answer, not on the dial)."""
    days: dict = {}
    inbound: dict = {}
    for e in evs:
        d, et = e["data"], e["event_type"]
        day = _day_of(e["when"])
        if et in INBOUND or d.get("direction") == "inbound":
            inbound[day] = inbound.get(day, 0) + 1
            continue
        if et not in (DIAL, ANSWERED):
            continue
        who = str(d.get("caller") or "").strip() or "unknown"
        r = days.setdefault(day, {}).setdefault(who, {"dials": 0, "answered": 0, "conversations": 0, "talk_s": 0,
                                                      "owners": set()})
        if et == DIAL:
            r["dials"] += 1
            r["owners"].add(e["resource"] or "")
        else:
            secs = int(d.get("duration") or 0)
            r["answered"] += 1
            r["talk_s"] += secs
            if secs >= CONVERSATION_S:
                r["conversations"] += 1
    for callers in days.values():
        named = [w for w in callers if w != "unknown"]
        if "unknown" in callers and len(named) == 1:
            u, n = callers.pop("unknown"), callers[named[0]]
            n["dials"] += u["dials"]
            n["owners"] |= u["owners"]
    return days, inbound


def _sum_calls(parts: list[dict]) -> dict:
    owners: set = set()
    out = {"dials": 0, "answered": 0, "conversations": 0, "talk_s": 0}
    for p in parts:
        for k in out:
            out[k] += p[k]
        owners |= p["owners"]
    return {"dials": out["dials"], "answered": out["answered"], "conversations": out["conversations"],
            "talk_minutes": round(out["talk_s"] / 60.0, 1), "owners": len(owners - {""})}


def _grouped(counts: dict, key: str, by_day: bool) -> tuple[list[dict], int]:
    items = sorted(counts.items()) if by_day else sorted(counts.items(), key=lambda kv: (-kv[1]["events"], kv[0]))
    out = [{key: k, "events": v["events"], "records": len(v["records"] - {""})} for k, v in items]
    return out[:GROUP_LINES], max(0, len(out) - GROUP_LINES)


def _log_limits(groups: list[str], types: list[str], start: datetime) -> list[str]:
    """How far the log copy reaches, said every time: a count from a copy that stopped syncing reads
    exactly like a quiet day, which is the failure the FTM watchdog was built for."""
    out, read, never = [], [], []
    for g in groups:
        thr = activity.synced_through(g)
        if thr is None:
            never.append(g)
        else:
            read.append("%s %s" % (g, _local(thr).strftime("%Y-%m-%d %H:%M")))
    if read:
        out.append("From the brain's copy of the account log, read through %s local time; anything later is not "
                   "counted yet." % ", ".join(read))
    if never:
        out.append("The brain has not read the %s log yet, so those counts are empty, not a real zero."
                   % " and ".join(never))
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT MIN(at) AS a FROM ap_log WHERE event_type IN (%s)" % ",".join("?" * len(types)),
                        list(types)).fetchone()
    oldest = row["a"] if row else None
    if oldest and datetime.fromisoformat(oldest) > start:
        out.append("The copy reaches back to %s; earlier days read as zero." % _day_of(datetime.fromisoformat(oldest)))
    return out


def activity_query(args: dict) -> dict:
    a = clean("activity_query", args)
    today = _today()
    what = a["what"]
    by = a.get("by") or BY_FOR[what][0]
    if by not in BY_FOR[what]:
        raise Refused("%s can be grouped by %s, not by %s." % (what, ", ".join(BY_FOR[what]), by))
    for k, only in (("caller", "calls"), ("status", "status_changes"), ("tag", "tags"), ("list", "lists")):
        if a.get(k) and what != only:
            raise Refused("%s goes with what=%s." % (k, only))
    since = _date(a.get("since") or "today", "since", today)
    until = _date(a.get("until") or "today", "until", today)
    if until < since:
        raise Refused("until is before since.")
    if (until - since).days > activity.KEEP_DAYS:
        raise Refused("The brain keeps %d days of the log; ask for a shorter range." % activity.KEEP_DAYS)
    start, end = _bounds(since, until)
    out: dict = {"tool": "activity_query", "today": today.isoformat(), "what": what, "by": by,
                 "since": since.isoformat(), "until": until.isoformat()}
    limits: list[str] = []

    if what == "calls":
        types = [DIAL, ANSWERED, *INBOUND]
        days, inbound = _call_days(activity.events(types, since=start, until=end))
        want = (a.get("caller") or "").strip().lower()
        parts = [(day, who, r) for day, callers in days.items() for who, r in callers.items()
                 if not want or who.lower() == want]
        if by == "caller":
            names = sorted({who for _, who, _ in parts})
            rows = [dict(caller=w, **_sum_calls([r for _, who, r in parts if who == w])) for w in names]
            rows.sort(key=lambda x: (-x["dials"], x["caller"]))
        elif by == "day":
            rows = [dict(day=d, **_sum_calls([r for day, _, r in parts if day == d]),
                         inbound=0 if want else inbound.get(d, 0))
                    for d in sorted({day for day, _, _ in parts} | (set() if want else set(inbound)))]
        else:
            rows = [dict(_sum_calls([r for _, _, r in parts]), inbound=0 if want else sum(inbound.values()))]
        out["rows"] = rows[:GROUP_LINES]
        if want and not parts:
            limits.append("No calls by %s in this range." % a["caller"])
        limits.append("A conversation is an answered call of %d seconds or more; an answer alone is not one, because "
                      "a voicemail greeting answers too." % CONVERSATION_S)
        limits.append("A dial carries no caller name, so a day's unnamed dials go to its only caller; on a day two "
                      "people called they stay under unknown.")
        limits += _log_limits(["calls"], types, start)
    elif what == "texts":
        types = list(activity.TEXTS)
        kinds = {"owner.sms.sent": "sent", "owner.sms.received": "received", "owner.sms.failed": "failed",
                 "owner.sms.delivered": "delivered"}
        per: dict = {}
        for e in activity.events(types, since=start, until=end):
            k = kinds.get(e["event_type"])
            if not k:
                continue
            key = _day_of(e["when"]) if by == "day" else "total"
            per.setdefault(key, {"sent": 0, "received": 0, "failed": 0, "delivered": 0})[k] += 1
        if by == "day":
            out["rows"] = [dict(day=k, **v) for k, v in sorted(per.items())]
        else:
            out["rows"] = [per.get("total") or {"sent": 0, "received": 0, "failed": 0, "delivered": 0}]
        limits.append("Texts sent through Smarter Contact or smrtPhone are not in DataSift's log (only their failures "
                      "are), so these counts are only what that log holds.")
        limits += _log_limits(["texts"], types, start)
    else:
        if what == "events":
            types = sorted({t for g in activity.GROUPS.values() for t in g})
            groups = list(activity.GROUPS)
        else:
            types = {"status_changes": ["property.status.updated"], "assignments": ["property.assigned"],
                     "tags": ["property.tags.added"], "lists": ["property.lists.added"]}[what]
            groups = ["state"]
        names = names_by_uuid() if what == "assignments" else {}
        want_status = _flat(a["status"]) if a.get("status") else ""
        want_title = str(a.get("tag") or a.get("list") or "").strip().lower()
        counts: dict = {}
        total, records = 0, set()
        for e in activity.events(types, since=start, until=end):
            d = e["data"]
            # What this event counts under when grouped by its own content (status, assignee, tag...).
            if what == "status_changes":
                if want_status and _flat(d.get("to")) != want_status:
                    continue
                own = [status_word(d.get("to"))] if by == "status" else [str(e.get("source") or "unknown")]
            elif what == "assignments":
                to = str(d.get("to") or "")
                own = [names.get(to, "user %s" % to[:8]) if to else "unassigned"]
            elif what in ("tags", "lists"):
                own = [str(t).strip() for t in (d.get(what) or []) if str(t).strip()]
                if want_title:
                    own = [t for t in own if t.lower() == want_title]
                if not own:
                    continue
            else:
                own = [e["event_type"]]
            total += 1
            records.add(e["resource"] or "")
            if by == "total":
                continue
            for k in ([_day_of(e["when"])] if by == "day" else own):
                c = counts.setdefault(k, {"events": 0, "records": set()})
                c["events"] += 1
                c["records"].add(e["resource"] or "")
        if by == "total":
            out["rows"] = [{"events": total, "records": len(records - {""})}]
        else:
            out["rows"], more = _grouped(counts, by, by == "day")
            if more:
                limits.append("%d more lines are not shown." % more)
        out["events"], out["records"] = total, len(records - {""})
        if what == "assignments":
            limits.append("A teammate is named from the call log or AGENTS_QUERY_USERS; anyone else shows as a user id.")
        if what == "status_changes":
            limits.append("Source says what made the change: sequence, bulk, a person in the app, or a tool.")
        limits += _log_limits(groups, types, start)
    out["limits"] = limits
    return out


# ------------------------------------------------------------------------------- team_query
def _who(agent: str) -> str:
    try:
        from . import team
        emp = team.load().employees.get(agent)
        return emp.display_name if emp else agent
    except Exception:  # noqa: BLE001  (a roster that will not load still lets the tables be read)
        return agent


def _stamp(text) -> str:
    try:
        return _local(datetime.fromisoformat(str(text))).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(text or "")[:16]


def _last_runs() -> dict:
    out = {}
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT key, value FROM kv WHERE key LIKE 'autopilot:last:%'").fetchall()
    for r in rows:
        try:
            v = json.loads(r["value"] or "{}")
        except ValueError:
            v = {}
        if isinstance(v, dict) and v:
            out[r["key"][len("autopilot:last:"):]] = v
    return out


def _stage_row(sid: str, st, last: dict, today: date) -> dict:
    row = {"stage": sid, "mode": getattr(st, "mode", "unknown") if st else "not in the playbook",
           "kind": getattr(st, "kind", "") if st else "", "owner": _who(getattr(st, "owner", "")) if st else ""}
    if not last:
        row["last_run"] = "never run here"
        return row
    row["last_run"] = _stamp(last.get("at"))
    row["found"] = last.get("found", 0)
    row["did" if last.get("live") else "would_do"] = last.get("acted", 0)
    if last.get("usd"):
        row["usd"] = last["usd"]
    if last.get("would_usd"):
        row["would_usd"] = last["would_usd"]
    row["problems"] = len(last.get("problems") or [])
    head = str(last.get("head") or "")
    row["head"] = head if len(head) <= 180 else head[:177].rstrip() + "..."
    return row


def _ran_on(last: dict, day: date) -> bool:
    try:
        return _local(datetime.fromisoformat(str(last.get("at")))).date() == day
    except (TypeError, ValueError):
        return False


def team_query(args: dict) -> dict:
    a = clean("team_query", args)
    today = _today()
    about = a["about"]
    out: dict = {"tool": "team_query", "today": today.isoformat(), "about": about}
    limits: list[str] = []

    if about in ("stages", "stage"):
        pol = _policy()
        stages = dict(getattr(pol, "stages", None) or {})
        last = _last_runs()
        if pol is None:
            limits.append("The playbook did not load, so stage modes are unknown.")
        if about == "stage":
            sid = (a.get("stage") or "").strip()
            if not sid:
                raise Refused("about=stage needs stage, a stage id from the playbook.")
            known = sorted(set(stages) | set(last))
            if sid not in known:
                near = difflib.get_close_matches(sid, known, n=3, cutoff=0.5)
                raise Refused("No stage is called %r%s." % (sid, ("; close: %s" % ", ".join(near)) if near else ""))
            st, lr = stages.get(sid), last.get(sid) or {}
            row = _stage_row(sid, st, lr, today)
            if lr.get("head"):
                row["head"] = str(lr["head"])[:500]
            row["problem_lines"] = [str(p)[:240] for p in (lr.get("problems") or [])][:5]
            if st is not None:
                row["summary"] = getattr(st, "summary", "")
                row["schedule"] = getattr(st, "schedule", [])
            with store._LOCK, store._db() as c:
                acts = c.execute("SELECT action, COUNT(DISTINCT record_uuid) AS n, COALESCE(SUM(usd),0) AS usd "
                                 "FROM ap_events WHERE stage=? AND day=? GROUP BY action ORDER BY n DESC",
                                 (sid, today.isoformat())).fetchall()
                staged = c.execute("SELECT id, kind, created, usd, items FROM ap_batches WHERE stage=? AND "
                                   "status='staged' ORDER BY id", (sid,)).fetchall()
            row["today_actions"] = [{"action": r["action"], "records": r["n"], "usd": round(float(r["usd"]), 2)}
                                    for r in acts]
            row["staged_batches"] = [{"batch": r["id"], "kind": r["kind"], "records": len(json.loads(r["items"] or "[]")),
                                      "usd": r["usd"], "created": _stamp(r["created"])} for r in staged]
            out["stage"] = row
            limits.append("A stage in plan mode only says what it would do: would_do counts are not actions.")
        else:
            only = a.get("only") or "today"
            ids = list(stages) + sorted(set(last) - set(stages))
            rows = []
            for sid in ids:
                lr = last.get(sid) or {}
                if only == "today" and not _ran_on(lr, today):
                    continue
                if only == "problems" and not (lr.get("problems") or []):
                    continue
                rows.append(_stage_row(sid, stages.get(sid), lr, today))
            out["summary"] = {"stages": len(ids), "ran_today": sum(1 for s in ids if _ran_on(last.get(s) or {}, today)),
                              "with_problems": sum(1 for s in ids if (last.get(s) or {}).get("problems")),
                              "never_run": sum(1 for s in ids if not last.get(s)),
                              "off": sum(1 for s in ids if getattr(stages.get(s), "mode", "") == "off")}
            out["rows"] = rows[:60]
            if len(rows) > 60:
                limits.append("%d more stages are not shown." % (len(rows) - 60))
            limits.append("A stage in plan mode only says what it would do: would_do counts are not actions.")
            limits.append("Each stage keeps only its last run; team_query about=stage names one run's problems.")
    elif about == "tasks":
        since = _date(a.get("since") or "today", "since", today)
        start = _bounds(since, since)[0].isoformat(timespec="seconds")
        want = (a.get("status") or "").strip().lower()
        if want and want not in ("queued", "running", "waiting", "answered", "done", "failed"):
            raise Refused("A task's status is queued, running, waiting, answered, done or failed, not %r." % want)
        with store._LOCK, store._db() as c:
            by_status = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE created>=? GROUP BY status", (start,)).fetchall()}
            by_wf: dict = {}
            for r in c.execute("SELECT workflow, status, COUNT(*) AS n FROM tasks WHERE created>=? "
                               "GROUP BY workflow, status", (start,)).fetchall():
                by_wf.setdefault(r["workflow"], {})[r["status"]] = r["n"]
            open_now = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE status IN ('queued','running','waiting') "
                "GROUP BY status").fetchall()}
            failed = c.execute("SELECT id, workflow, agent, finished, error FROM tasks WHERE status='failed' AND "
                               "created>=? ORDER BY id DESC LIMIT 10", (start,)).fetchall()
            listed = c.execute("SELECT id, workflow, agent, created, subject FROM tasks WHERE status=? AND created>=? "
                               "ORDER BY id DESC LIMIT 15", (want, start)).fetchall() if want else []
        wf_rows = sorted(({"workflow": w, **s, "total": sum(s.values())} for w, s in by_wf.items()),
                         key=lambda x: -x["total"])
        out.update({"since": since.isoformat(), "by_status": by_status, "open_now": open_now,
                    "by_workflow": wf_rows[:20],
                    "failed": [{"task": r["id"], "workflow": r["workflow"], "who": _who(r["agent"]),
                                "at": _stamp(r["finished"]), "error": str(r["error"] or "")[:160]} for r in failed]})
        if want:
            out["listed"] = [{"task": r["id"], "workflow": r["workflow"], "who": _who(r["agent"]),
                              "at": _stamp(r["created"]), "subject": str(r["subject"] or "")[:80]} for r in listed]
        if len(wf_rows) > 20:
            limits.append("%d more workflows are not shown." % (len(wf_rows) - 20))
        limits.append("An employee's own turn is the workflow ask (or resume); a stage's run is its stage id.")
    elif about == "batches":
        want = (a.get("status") or "staged").strip().lower()
        with store._LOCK, store._db() as c:
            q = "SELECT id, created, stage, kind, status, usd, items FROM ap_batches"
            rows = c.execute(q + ("" if want == "any" else " WHERE status=?") + " ORDER BY id DESC LIMIT 30",
                             () if want == "any" else (want,)).fetchall()
            totals = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM ap_batches GROUP BY status").fetchall()}
        out["status"] = want
        out["rows"] = [{"batch": r["id"], "stage": r["stage"], "kind": r["kind"], "status": r["status"],
                        "records": len(json.loads(r["items"] or "[]")), "usd": r["usd"], "created": _stamp(r["created"])}
                       for r in rows]
        out["by_status"] = totals
        limits.append("A staged batch waits for a person: release batch N in Slack, or cli.py autopilot release.")
    else:
        since = _date(a.get("since") or "today", "since", today)
        rows = store.spend_detail(since_day=since.isoformat())
        by_wf: dict = {}
        by_what: dict = {}
        for r in rows:
            usd = float(r.get("usd") or 0)
            w = r.get("workflow") or "no task"
            by_wf[w] = by_wf.get(w, 0.0) + usd
            k = str(r.get("what") or "unlabelled")
            by_what[k] = by_what.get(k, 0.0) + usd
        with store._LOCK, store._db() as c:
            ap = c.execute("SELECT stage, COALESCE(SUM(usd),0) AS usd, COUNT(*) AS n FROM ap_events WHERE day>=? AND "
                           "usd>0 GROUP BY stage ORDER BY usd DESC", (since.isoformat(),)).fetchall()
        out.update({
            "since": since.isoformat(),
            "brain_ledger": {"usd": round(sum(by_wf.values()), 4),
                             "by_workflow": [{"workflow": k, "usd": round(v, 4)} for k, v in
                                             sorted(by_wf.items(), key=lambda kv: -kv[1])][:15],
                             "by_item": [{"what": k, "usd": round(v, 4)} for k, v in
                                         sorted(by_what.items(), key=lambda kv: -kv[1])][:10]},
            "autopilot_ledger": {"usd": round(sum(float(r["usd"]) for r in ap), 4),
                                 "by_stage": [{"stage": r["stage"], "usd": round(float(r["usd"]), 4), "paid_calls": r["n"]}
                                              for r in ap][:15]}})
        limits.append("The brain's ledger holds every Claude turn and every paid call a workflow made; the autopilot's "
                      "holds what its live stages paid. A stage in plan mode pays nothing (its would_usd is a quote).")
    out["limits"] = limits
    return out


# ------------------------------------------------------------------------------- entry
HANDLERS = {"records_query": records_query, "siftmap_count": siftmap_count, "activity_query": activity_query,
            "team_query": team_query}


def _plain(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "BudgetExhausted":
        return "The brain has used its share of DataSift requests for now (%s). Ask again later." % str(exc)[:160]
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        body = str(getattr(exc, "body", "") or "").strip()[:200]
        return "DataSift refused the request (HTTP %d): %s" % (code, body or "no detail")
    if name == "SiftMapError":
        return "SiftMap refused the count: %s" % str(exc)[:200]
    return "The query failed: %s: %s" % (name, str(exc)[:200])


def answer(name: str, args: dict | None) -> dict:
    """Run one query tool. Raises Refused with a sentence the employee can repeat; any other error
    (the hour's request budget used, DataSift refusing a search) becomes one too."""
    fn = HANDLERS.get(name)
    if fn is None:
        raise Refused("There is no query tool named %s." % name)
    try:
        return fn(dict(args or {}))
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001  (every failure is reported to the employee as one sentence)
        log.warning("%s failed: %s", name, exc)
        raise Refused(_plain(exc)) from exc
