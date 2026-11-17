"""KPI sources: every tool's work read into one schema, and each act counted once (Ty, 2026-11-16).

Ty asked for "an evergreen infrastructure where any tool stack can be used either through just a cloud
login or directly through an API, obviously if that's available", and then to keep it "as core and as
simple for any universal users to run as we possibly can... no Lambda, AWS". So everything here runs
inside the brain: its own SQLite file, its own web routes, no outside host and no queue service.

An ADAPTER reads one kind of source and reports EVENTS in one schema:

    kind      call, text, phone_status, mail, stage, spend
    source    the connection that reported it ("datasift", "kixie")
    tool      the tool that did the work: a Kixie call DataSift logged has source datasift and tool kixie

A source that only gives totals (an agency PDF, an in-app report) reports TALLIES. A tally is never added to
the events: it is shown on its own, under the source's own word for it, and marked covered when an event
source already counts the same tool, metric and period.

THERE IS NO CONVERSATION KIND, and no tally may be called conversations, contacts or connects. A
conversation is always derived from calls and texts by the catalog's own definition (team/kpi.yaml,
`conversation`; `conversations()` below). A vendor's "contacts" counts every voicemail greeting, and pouring
it into ours is how a report reads thirty times too rosy (2026-11-12: 185 of 234 dials were answered and 6
were conversations). A source's own figure is kept as `as_reported`, with its own word as the definition.

collect(since, until, kinds) reads every connected source and returns each act once:

  * Within one source an act is (source, source_id): a call's start, answer and end share one id, so the
    later reports fill the same act. A filled field never goes back to empty; the outcome, status, talk
    time and answer take the latest value.
  * Across sources, stages join on (record, stage) and phone statuses on (number, status): a Smarter
    Contact label or a Kixie disposition that DataSift also records as a phone status is one fact told twice.
  * Calls and texts join on the tool's own id when both sources carry it, else on a fingerprint: the same
    tool and direction, the same other number, within 120 seconds for a call and 30 for a text, and the same
    person when both name one in the same form. One to one, nearest in time first.
  * Per tool, kind and day, two sources are added together only when they AGREE: acts both reported, over
    the smaller source's count, at least 0.9 (the smaller count is the divisor because one source is often a
    subset of the other: DataSift holds only calls to numbers on a record). Otherwise the larger source
    counts alone and one issue says so, because a union across a broken join counts every call twice, and a
    clock or number-format bug looks exactly like that.
  * Field by field the tool's own source wins (talk time, the answer, its wording, its user; an email beats
    a first name) and DataSift wins the record and owner.
  * OUR OWN WRITE-BACKS ARE NEVER A PERSON'S WORK. A log row our key wrote (author self) carries no person,
    and one that echoes a fact a connected source already reports (the agency workflow setting New Lead for
    the agency's lead, found through the ledger) is dropped, so that lead counts once, as the agency's.

How a source is reached is one of five modes (MODES), and nothing here ever holds a password.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields as dc_fields, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from ..autopilot.gate import digits

log = logging.getLogger(__name__)

TZ = "America/New_York"
KINDS = ("call", "text", "phone_status", "mail", "stage", "spend")
STAGES = ("lead", "appointment", "offer", "contract", "closed")
GRAINS = ("event", "export")
SCOPES = ("all", "on_record")
LATENCIES = ("push", "poll", "daily", "monthly")
NOT_A_KIND = ("a conversation is derived from calls and texts by the catalog's definition (team/kpi.yaml), never "
              "reported by a source")

# How a connection is reached (the adapter design, section b4). Nothing here ever holds a password.
MODES = {
    "api_key": "a key the owner pastes from the tool's own settings; it stops when it is revoked or rotated",
    "oauth": "a refresh token from the tool's consent screen; it stops when it is revoked or lapses",
    "webhook": "the tool posts to the brain at /hooks/kpi/<secret>/<connection>; it stops when the tool stops "
               "sending",
    "cloud_login": "a browser session the owner captures on their own computer, so only the session file is ever "
                   "uploaded, never a password. Off for a tool until the owner switches it on after reading that "
                   "tool's automation clause, because several tools' terms forbid it",
    "file": "an export the owner posts or schedules; it stops when the sender stops",
}
OWN = "own"   # the brain's own tables (the log copy, the ledger): nothing to connect and nothing to hold

# The fields an adapter may say it fills (Provides.fields); kind, at, source and source_id are always there.
OPTIONAL_FIELDS = frozenset({"tool_id", "person_ref", "direction", "phone", "line", "record", "owner", "answered",
                             "seconds", "outcome", "stage", "status", "amount", "campaign"})

TALLY_METRICS = ("dials", "answered", "correct_numbers", "texts_sent", "replies", "mail_pieces", "leads",
                 "appointments", "offers", "contracts", "closed", "spend", "as_reported")
# A source's own words for a figure that is not ours. Each is kept as as_reported, never as one of our metrics.
BORROWED = {"conversation", "conversations", "contact", "contacts", "connect", "connects", "connected",
            "contact rate", "connect rate", "conversation rate", "live conversations", "right party contacts", "rpc",
            "talk time", "transfers"}
# What event each metric counts, for marking a tally covered by an event source.
METRIC_TEST = {
    "dials": lambda a: a.kind == "call" and a.direction == "out",
    "answered": lambda a: a.kind == "call" and a.direction == "out" and a.answered is True,
    "texts_sent": lambda a: a.kind == "text" and a.direction == "out" and a.status != "failed",
    "replies": lambda a: a.kind == "text" and a.direction == "in",
    "mail_pieces": lambda a: a.kind == "mail",
    "correct_numbers": lambda a: a.kind == "phone_status" and a.outcome in ("correct", "correct_dnc"),
    "leads": lambda a: a.kind == "stage" and a.stage == "lead",
    "appointments": lambda a: a.kind == "stage" and a.stage == "appointment",
    "offers": lambda a: a.kind == "stage" and a.stage == "offer",
    "contracts": lambda a: a.kind == "stage" and a.stage == "contract",
    "closed": lambda a: a.kind == "stage" and a.stage == "closed",
    "spend": lambda a: a.kind == "spend",
}
METRIC_KIND = {"dials": "call", "answered": "call", "texts_sent": "text", "replies": "text", "mail_pieces": "mail",
               "correct_numbers": "phone_status", "leads": "stage", "appointments": "stage", "offers": "stage",
               "contracts": "stage", "closed": "stage", "spend": "spend"}

TOOL_NAMES = {"kixie": "Kixie", "smrtphone": "smrtPhone", "smrtdialer": "smrtDialer", "datasift": "DataSift",
              "agency": "the agency", "zapier": "Zapier", "aircall": "Aircall", "calltools": "CallTools",
              "smartercontact": "Smarter Contact", "trestle": "Trestle", "smartskip": "SmartSkip",
              "tracerfy": "Tracerfy", "claude": "Claude", "siftmail": "SiftMail", "batchdialer": "BatchDialer",
              "readymode": "ReadyMode", "phoneburner": "PhoneBurner", "twilio": "Twilio"}
ADAPTER_WORDS = {"webhook": "webhook", "file_import": "file", "datasift_log": "log", "ledger_spend": "ledger"}

OUT_WORDS = {"out", "outbound", "outgoing", "sent", "send", "dial", "dialed", "made"}
IN_WORDS = {"in", "inbound", "incoming", "received", "receive", "reply"}
ANSWER_WORDS = {"answered", "completed", "complete", "connected", "voicemail", "machine", "human", "talked", "yes",
                "y", "true", "1"}
NO_ANSWER_WORDS = {"missed", "no answer", "noanswer", "no-answer", "no_answer", "unanswered", "busy", "failed",
                   "canceled", "cancelled", "abandoned", "rejected", "declined", "no", "n", "false", "0"}
PHONE_WORDS = {"verified": "correct", "right number": "correct", "correct number": "correct", "good number": "correct",
               "correct dnc": "correct_dnc", "wrong number": "wrong", "wrong person": "wrong", "wrong dnc": "wrong_dnc",
               "do not call": "dnc", "disconnected": "dead", "bad number": "dead", "invalid": "dead",
               "not in service": "dead", "no answer": "no_answer"}

ID_RX = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PERIOD_RX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$")
SECRET_KEY_RX = re.compile(r"password|passwd|passcode|\bpwd\b", re.I)


# ---------------------------------------------------------------------- settings and words
def setting(name: str, default):
    """One setting from the environment (Fly secrets on the box), typed like its default."""
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def words(v) -> str:
    """Lower case, letters and digits, single spaces: how a header, a disposition or a status is compared."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).split())


def tool_key(t) -> str:
    """kixie, smrtphone, smartercontact: one spelling per tool, so "Smarter Contact" and "smarter-contact" meet."""
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())[:40]


def tool_name(t) -> str:
    k = tool_key(t)
    return TOOL_NAMES.get(k) or (str(t).strip() if str(t or "").strip() else "Untagged")


def direction_of(v) -> str:
    w = words(v)
    first = w.split(" ")[0] if w else ""
    if first in OUT_WORDS:
        return "out"
    if first in IN_WORDS:
        return "in"
    return ""


def answered_of(v) -> bool | None:
    """True, False, or None when the value cannot say (never read as no)."""
    if isinstance(v, bool):
        return v
    raw = str(v if v is not None else "").strip().lower()
    if not raw:
        return None
    if raw in NO_ANSWER_WORDS or words(raw) in NO_ANSWER_WORDS:
        return False
    if raw in ANSWER_WORDS or words(raw) in ANSWER_WORDS:
        return True
    return None


def phone_status_of(v) -> str:
    """correct, correct_dnc, wrong, wrong_dnc, dnc, dead or the source's own word, so one status from two tools meets."""
    w = words(v)
    if not w:
        return ""
    return PHONE_WORDS.get(w) or w.replace(" ", "_")


def seconds_of(v) -> int | None:
    """Talk time in seconds from 75, "75", "1:15", "00:01:15" or "1m 15s". None when unknown, never 0 by default:
    a call with no length is "length unknown", not a zero-second call."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v >= 0 else None
    t = str(v).strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return int(float(t))
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", t)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.fullmatch(r"(?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?\s*(?:(\d+)\s*s\w*)?", t)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    return None


def money_of(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace("$", "").replace(",", "").strip()
    try:
        return float(t) if t else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- time
def zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or TZ)
    except Exception:  # noqa: BLE001  (a misspelled zone in a setting reads as the business zone, never a crash)
        return ZoneInfo(TZ)


def local_day(when: datetime, tz: str | None = None) -> str:
    return when.astimezone(zone(tz)).date().isoformat()


def fmt_day(day) -> str:
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return "%s %d" % (d.strftime("%b"), d.day)


def fmt_when(when: datetime, tz: str | None = None) -> str:
    t = when.astimezone(zone(tz))
    return "%s at %d:%02d %s" % (fmt_day(t.date()), t.hour % 12 or 12, t.minute, "AM" if t.hour < 12 else "PM")


_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?\s*(z|[+-]\d{2}:?\d{2})?$",
                  re.I)
_US = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?:[ ,t]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?)?$", re.I)
_NAMED = re.compile(r"^([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?)?$", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _hour(h: str | None, ampm: str | None) -> int:
    hour = int(h or 0)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return hour


def _offset(text: str | None):
    if not text:
        return None
    if text.lower() == "z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits_ = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits_[:2]), minutes=int(digits_[2:4] or 0)))


def _made(y, mo, d, hh, mm, ss, tzinfo, zone_, timed: bool):
    """(the moment, exact). A day with no time is placed at local noon and marked inexact, so a zone shift never
    moves it across midnight and the fingerprint never matches it to the second."""
    if not timed:
        return datetime(y, mo, d, 12, 0, tzinfo=zone_).astimezone(timezone.utc), False
    return datetime(y, mo, d, hh, mm, ss, tzinfo=tzinfo or zone_).astimezone(timezone.utc), True


def parse_when(value, tz: str | None = None) -> tuple[datetime | None, bool]:
    """(the moment in UTC, whether it carried a time of day) from the shapes tools write: ISO with or without a
    zone, 11/16/2026 2:02 PM, Nov 16, 2026, an Excel cell, epoch seconds or milliseconds. A value with no zone is
    read in `tz`: each adapter declares its own clock, so a dialer's local times never move a call a day."""
    z = zone(tz)
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if (value.hour, value.minute, value.second) == (0, 0, 0):
                return _made(value.year, value.month, value.day, 0, 0, 0, None, z, False)
            value = value.replace(tzinfo=z)
        return value.astimezone(timezone.utc), True
    if isinstance(value, date):
        return _made(value.year, value.month, value.day, 0, 0, 0, None, z, False)
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}(\.\d+)?", str(value).strip()):
        n = float(value)
        if n > 1e11:
            n /= 1000.0
        if 1e9 <= n < 1e11:
            return datetime.fromtimestamp(n, timezone.utc), True
        return None, False
    t = " ".join(str(value).strip().split())
    try:
        m = _ISO.match(t)
        if m:
            y, mo, d, hh, mm, ss, off = m.groups()
            return _made(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0), _offset(off), z, hh is not None)
        m = _US.match(t)
        if m:
            mo, d, y, hh, mm, ss, ampm = m.groups()
            y = int(y) + (2000 if int(y) < 100 else 0)
            return _made(y, int(mo), int(d), _hour(hh, ampm), int(mm or 0), int(ss or 0), None, z, hh is not None)
        m = _NAMED.match(t)
        if m:
            name, d, y, hh, mm, ss, ampm = m.groups()
            k = next((i for i, x in enumerate(_MONTHS) if name.lower().startswith(x)), None)
            if k is not None:
                return _made(int(y), k + 1, int(d), _hour(hh, ampm), int(mm or 0), int(ss or 0), None, z, hh is not None)
    except ValueError:
        return None, False
    return None, False


def utc(when: datetime) -> datetime:
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise ValueError("a moment here must carry its zone")
    return when.astimezone(timezone.utc)


# ---------------------------------------------------------------------- the schema
@dataclass(eq=False)
class Event:
    """One fact as one source reported it. Several events can describe one act (a call the dialer and DataSift
    both saw); collect() joins them before anything is counted. Adapters never set the person, the program or
    the scope: those come after identity resolution, so a mapping fix re-labels history without a re-read."""
    kind: str                     # one of KINDS
    at: datetime                  # when it happened; stored in UTC
    source: str                   # the connection that reported it
    source_id: str                # that source's id for the act (call id, message id, row hash)
    tool: str = ""                # the tool that did the work
    tool_id: str = ""             # the tool's own id for the act, when this source carries it
    person_ref: str = ""          # how the source names the person: email, user id, first name
    direction: str = ""           # out or in (calls and texts)
    phone: str = ""               # the other party, 10 digits; never our own line
    line: str = ""                # our own number, 10 digits
    record: str = ""              # DataSift property uuid, when known
    owner: str = ""               # DataSift owner uuid, when known
    answered: bool | None = None  # None means the source cannot say, never no
    seconds: int | None = None    # talk time; None means unknown, never 0
    outcome: str = ""             # disposition or phone status, as the source wrote it, lower case
    stage: str = ""               # stage events: one of STAGES
    status: str = ""              # texts and mail: sent, delivered, failed, returned
    amount: float | None = None   # dollars: spend
    campaign: str = ""
    grain: str = "event"          # event (live or API) or export (a row of a file)
    raw_ref: str = ""             # id of the kept raw payload, for audit; never the payload itself
    exact: bool = True            # False when the source gave a day without a time
    author: str = ""              # self when our own key wrote the row; such an event is never a person's work
    via: str = ""                 # the workflow or stage whose write this row is, found through the ledger
    sources: tuple = ()           # set on an act: every source that reported it
    members: tuple = ()           # set on an act: every source:source_id folded into it

    def __post_init__(self):
        k = str(self.kind or "").strip().lower()
        if "convers" in k:
            raise ValueError(NOT_A_KIND)
        if k not in KINDS:
            raise ValueError("kind %r is not one of %s" % (self.kind, ", ".join(KINDS)))
        self.kind = k
        self.at = utc(self.at)
        self.source, self.source_id = str(self.source or "").strip(), str(self.source_id or "").strip()
        if not self.source or not self.source_id:
            raise ValueError("an event needs its source and that source's id for the act")
        self.tool, self.tool_id = tool_key(self.tool), str(self.tool_id or "").strip()[:120]
        self.direction = direction_of(self.direction)
        self.phone, self.line = digits(self.phone), digits(self.line)
        st = str(self.stage or "").strip().lower()
        if k == "stage" and st not in STAGES:
            raise ValueError("a stage event needs a stage, one of %s" % ", ".join(STAGES))
        self.stage = st if k == "stage" else ""
        if self.grain not in GRAINS:
            raise ValueError("grain is event or export")
        self.answered = None if self.answered is None else bool(self.answered)
        if self.seconds is not None:
            self.seconds = int(self.seconds) if int(self.seconds) >= 0 else None
        self.amount = None if self.amount is None else float(self.amount)
        self.outcome = " ".join(str(self.outcome or "").lower().split())[:80]
        if k == "phone_status":
            self.outcome = phone_status_of(self.outcome)
        self.status = " ".join(str(self.status or "").lower().split())[:40]
        self.record, self.owner = str(self.record or "").strip().lower(), str(self.owner or "").strip().lower()
        self.author = str(self.author or "").strip()
        self.person_ref = "" if self.author == "self" else str(self.person_ref or "").strip()[:120]
        self.campaign, self.via = str(self.campaign or "").strip()[:80], str(self.via or "").strip()
        self.exact = bool(self.exact)
        self.sources = tuple(self.sources) or (self.source,)
        self.members = tuple(self.members) or ("%s:%s" % (self.source, self.source_id),)

    # Read either way: e.kind or e["kind"], so a caller written against dicts keeps working.
    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in dc_fields(self)}
        d["at"] = self.at.isoformat(timespec="seconds")
        d["sources"], d["members"] = list(self.sources), list(self.members)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        known = {f.name for f in dc_fields(cls)}
        v = {k: x for k, x in dict(d).items() if k in known}
        v["at"] = datetime.fromisoformat(str(v["at"]))
        v["sources"], v["members"] = tuple(v.get("sources") or ()), tuple(v.get("members") or ())
        return cls(**v)


@dataclass(eq=False)
class Tally:
    """A total from a source that only gives totals. Never added to the events; shown on its own, with the
    source's own word for it, and marked covered when an event source counts the same cell."""
    metric: str                   # one of TALLY_METRICS
    period: str                   # a business-local day YYYY-MM-DD or a month YYYY-MM
    value: float
    source: str
    tool: str = ""
    person_ref: str = ""
    definition: str = ""          # the source's own word for the figure ("contacts", "dials")
    covered: bool = False         # set by collect(): an event source counts this tool, metric and period

    def __post_init__(self):
        m = str(self.metric or "").strip().lower()
        if m in BORROWED or "convers" in m:
            raise ValueError("%r is the source's own word, not one of ours: keep it as as_reported with %r as its "
                             "definition" % (self.metric, self.metric))
        if m not in TALLY_METRICS:
            raise ValueError("metric %r is not one of %s" % (self.metric, ", ".join(TALLY_METRICS)))
        self.metric = m
        self.definition = " ".join(str(self.definition or "").split())[:60]
        if m == "as_reported" and not self.definition:
            raise ValueError("a figure kept as reported needs the source's own word for it (definition)")
        self.period = str(self.period or "").strip()
        if not PERIOD_RX.match(self.period):
            raise ValueError("period is a day written YYYY-MM-DD or a month written YYYY-MM")
        self.value = float(self.value)
        self.source, self.tool = str(self.source or "").strip(), tool_key(self.tool)
        if not self.source:
            raise ValueError("a tally needs its source")
        self.person_ref = str(self.person_ref or "").strip()[:120]

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dc_fields(self)}


@dataclass(frozen=True)
class Provides:
    """What an adapter can report, declared so a planner can pick the primary source per tool and kind without a
    guess: events over exports over tallies, a named person over none, push or poll over daily or monthly, free
    over metered, an official API, OAuth or webhook over a file over a cloud login."""
    kind: str                       # one of KINDS
    mode: str                       # one of MODES, or OWN for the brain's own tables
    fields: frozenset = frozenset()  # optional Event fields it fills
    per_person: bool = False        # can it name who did the work
    scope: str = "all"              # all, or on_record (only acts the CRM matched to an owner)
    latency: str = "poll"           # push, poll, daily, monthly
    backfill_days: int | None = None  # how far back a first connect reaches; 0 = from now on; None = everything it holds
    metered: bool = False           # every read costs money

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("Provides kind %r is not one of %s" % (self.kind, ", ".join(KINDS)))
        if self.mode not in MODES and self.mode != OWN:
            raise ValueError("Provides mode %r is not one of %s or %s" % (self.mode, ", ".join(MODES), OWN))
        if self.scope not in SCOPES:
            raise ValueError("Provides scope %r is not one of %s" % (self.scope, ", ".join(SCOPES)))
        if self.latency not in LATENCIES:
            raise ValueError("Provides latency %r is not one of %s" % (self.latency, ", ".join(LATENCIES)))
        extra = sorted(set(self.fields) - OPTIONAL_FIELDS)
        if extra:
            raise ValueError("Provides names fields an event does not have: %s" % ", ".join(extra))


@dataclass
class Batch:
    """What one read of one source gave."""
    events: list = field(default_factory=list)
    tallies: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    complete: bool = True                 # False when the source is behind or a cap stopped the read
    through: datetime | None = None       # the source is complete up to here
    last_at: datetime | None = None       # the newest thing it ever reported, for the silence check


class Adapter:
    """One kind of source. A new tool is one module with one of these, its fixtures, and one registry line."""
    id = ""
    tool = ""                                  # whose work it reports; "" when it reports many tools
    provides: tuple = ()
    normal_gap_hours: float | None = None      # silence longer than this on a business day is a failure
    terms_note = ""                            # the tool's automation clause, shown before a cloud login goes on

    def modes(self) -> set:
        return {p.mode for p in self.provides}

    def kinds(self, conn: dict | None = None) -> set:
        return {p.kind for p in self.provides}

    def settings_problems(self, settings: dict) -> list:
        return []

    def check(self, conn: dict) -> list:
        """A read-only probe: sends nothing, writes nothing. A list of problems, empty when the source is fine."""
        return []

    def pull(self, conn: dict, since: datetime, until: datetime, kinds: tuple, now: datetime | None = None) -> Batch:
        return Batch()

    def people(self, conn: dict) -> list:
        return []


# ---------------------------------------------------------------------- the tables
DDL = """
CREATE TABLE IF NOT EXISTS kpisrc_conn (
    id TEXT PRIMARY KEY, adapter TEXT, tool TEXT, enabled INTEGER, settings TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS kpisrc_event (
    source TEXT, source_id TEXT, kind TEXT, at TEXT, data TEXT, updated TEXT, PRIMARY KEY (source, source_id));
CREATE INDEX IF NOT EXISTS kpisrc_event_at ON kpisrc_event(source, at);
CREATE TABLE IF NOT EXISTS kpisrc_tally (
    source TEXT, metric TEXT, period TEXT, person_ref TEXT, definition TEXT, tool TEXT, value REAL, updated TEXT,
    PRIMARY KEY (source, metric, period, person_ref, definition));
CREATE TABLE IF NOT EXISTS kpisrc_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source TEXT, digest TEXT, headers TEXT, body TEXT,
    parsed TEXT DEFAULT '', problem TEXT DEFAULT '', UNIQUE (source, digest));
CREATE TABLE IF NOT EXISTS kpisrc_file (
    source TEXT, digest TEXT, name TEXT, at TEXT, events INTEGER, tallies INTEGER, PRIMARY KEY (source, digest));
"""
_READY: set = set()


def ensure() -> None:
    """The tables this package keeps, made the first time they are used on this database file. They hold what a
    source reported and how each is connected, never a change to the account."""
    key = str(config.DB_PATH)
    if key in _READY:
        return
    with store._LOCK, store._db() as c:
        c.executescript(DDL)
    _READY.add(key)


FIRST_WINS = ("tool", "tool_id", "person_ref", "direction", "phone", "line", "record", "owner", "stage", "amount",
              "campaign", "raw_ref", "via")
LATEST_WINS = ("outcome", "status", "seconds", "answered")


def _empty(v) -> bool:
    return v is None or v == ""


def fold(a: Event, b: Event) -> Event:
    """Two reports of one act by ONE source (a call's answer after its dial; Kixie's End Call after its Start
    Call). A filled field never goes back to empty; the outcome, status, talk time and answer take the latest."""
    first, later = (a, b) if a.at <= b.at else (b, a)
    out = {}
    for name in FIRST_WINS:
        v = getattr(first, name)
        out[name] = getattr(later, name) if _empty(v) else v
    for name in LATEST_WINS:
        v = getattr(later, name)
        out[name] = getattr(first, name) if _empty(v) else v
    if first.exact or not later.exact:
        out["at"] = first.at
    else:
        out["at"] = later.at
    out["exact"] = first.exact or later.exact
    out["grain"] = "event" if "event" in (a.grain, b.grain) else "export"
    out["author"] = next((x.author for x in (first, later) if x.author and x.author != "self"), "") or \
        ("self" if "self" in (a.author, b.author) else "")
    out["members"] = tuple(dict.fromkeys(first.members + later.members))
    if out["author"] != "self" and _empty(out["person_ref"]):
        out["person_ref"] = next((x.person_ref for x in (first, later) if x.person_ref), "")
    return replace(first, **out)


def save_events(events) -> int:
    """Keep pushed or imported events (webhooks, files). The same act reported again folds into its row, so a
    retry or a re-sent file adds nothing. Returns how many acts were new."""
    ensure()
    new = 0
    with store._LOCK, store._db() as c:
        for e in sorted(events, key=lambda x: x.at):
            row = c.execute("SELECT data FROM kpisrc_event WHERE source=? AND source_id=?",
                            (e.source, e.source_id)).fetchone()
            if row:
                e = fold(Event.from_dict(json.loads(row["data"])), e)
            else:
                new += 1
            c.execute("INSERT INTO kpisrc_event (source, source_id, kind, at, data, updated) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(source, source_id) DO UPDATE SET kind=excluded.kind, at=excluded.at, "
                      "data=excluded.data, updated=excluded.updated",
                      (e.source, e.source_id, e.kind, e.at.isoformat(timespec="seconds"),
                       json.dumps(e.to_dict(), default=str), store.now()))
    return new


def stored_events(source: str, since: datetime | None = None, until: datetime | None = None) -> list[Event]:
    ensure()
    q, args = "SELECT data FROM kpisrc_event WHERE source=?", [source]
    if since:
        q += " AND at>=?"
        args.append(utc(since).isoformat(timespec="seconds"))
    if until:
        q += " AND at<?"
        args.append(utc(until).isoformat(timespec="seconds"))
    with store._LOCK, store._db() as c:
        rows = c.execute(q + " ORDER BY at", args).fetchall()
    out = []
    for r in rows:
        try:
            out.append(Event.from_dict(json.loads(r["data"])))
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("a stored KPI event of %s could not be read: %s", source, exc)
    return out


def save_tallies(tallies) -> int:
    """Keep totals. A re-sent report for the same period REPLACES its figure, never adds to it."""
    ensure()
    with store._LOCK, store._db() as c:
        for t in tallies:
            c.execute("INSERT INTO kpisrc_tally (source, metric, period, person_ref, definition, tool, value, updated) "
                      "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source, metric, period, person_ref, definition) DO UPDATE SET "
                      "tool=excluded.tool, value=excluded.value, updated=excluded.updated",
                      (t.source, t.metric, t.period, t.person_ref, t.definition, t.tool, t.value, store.now()))
    return len(tallies)


def stored_tallies(source: str) -> list[Tally]:
    ensure()
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT * FROM kpisrc_tally WHERE source=? ORDER BY period, metric", (source,)).fetchall()
    return [Tally(metric=r["metric"], period=r["period"], value=r["value"], source=r["source"], tool=r["tool"] or "",
                  person_ref=r["person_ref"] or "", definition=r["definition"] or "") for r in rows]


def forget(source: str) -> None:
    """Drop everything one source ever reported (a wrong import). Its connection stays as it is."""
    ensure()
    with store._LOCK, store._db() as c:
        for table in ("kpisrc_event", "kpisrc_tally", "kpisrc_raw", "kpisrc_file"):
            c.execute("DELETE FROM %s WHERE source=?" % table, (source,))


def prune(now: datetime | None = None) -> None:
    """Raw deliveries are kept AGENTS_KPI_RAW_DAYS (14) for audit and re-reading; events about a year."""
    ensure()
    now = now or datetime.now(timezone.utc)
    raw_cut = (now - timedelta(days=max(1, setting("AGENTS_KPI_RAW_DAYS", 14)))).isoformat(timespec="seconds")
    ev_cut = (now - timedelta(days=max(30, setting("AGENTS_KPI_EVENT_DAYS", 400)))).isoformat(timespec="seconds")
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM kpisrc_raw WHERE at<?", (raw_cut,))
        c.execute("DELETE FROM kpisrc_event WHERE at<?", (ev_cut,))


# ---------------------------------------------------------------------- connections
# Always on, because they need nothing from the owner: the brain's own log copy, its own ledger, and the agency
# files the agency results workflow already keeps. A row in kpisrc_conn with the same id switches one off.
BUILTIN = {
    "datasift": ("datasift_log", "", {}),
    "ledger": ("ledger_spend", "", {}),
    "agency": ("file_import", "agency", {"preset": "agency", "echoes": ["agency_results"]}),
}
BUILTIN_LABELS = {"datasift": "the DataSift log", "ledger": "the ledger", "agency": "the agency's files"}


def label(conn) -> str:
    """How a source is named in a sentence: "the Kixie webhook", "the DataSift log"."""
    c = conn if isinstance(conn, dict) else (connection(str(conn)) or {"id": str(conn), "adapter": "", "tool": ""})
    if c["id"] in BUILTIN_LABELS and c.get("builtin"):
        return BUILTIN_LABELS[c["id"]]
    who = tool_name(c.get("tool")) if c.get("tool") else c["id"]
    return "the %s %s" % (who, ADAPTER_WORDS.get(c.get("adapter") or "", "source"))


def _row(r) -> dict:
    try:
        s = json.loads(r["settings"] or "{}")
    except ValueError:
        s = {}
    return {"id": r["id"], "adapter": r["adapter"], "tool": r["tool"] or "", "enabled": bool(r["enabled"]),
            "settings": s if isinstance(s, dict) else {}, "created": r["created"] or "", "builtin": r["id"] in BUILTIN}


def connections(include_off: bool = False) -> list[dict]:
    """Every connection: {id, adapter, tool, enabled, settings, created, builtin}, built-ins first."""
    ensure()
    with store._LOCK, store._db() as c:
        rows = {r["id"]: _row(r) for r in c.execute("SELECT * FROM kpisrc_conn ORDER BY id").fetchall()}
    out = []
    for cid, (adapter, tool, settings) in BUILTIN.items():
        have = rows.pop(cid, None)
        if have is None:
            have = {"id": cid, "adapter": adapter, "tool": tool, "enabled": True, "settings": dict(settings),
                    "created": "", "builtin": True}
        else:
            have["settings"] = {**settings, **have["settings"]}
        out.append(have)
    out += list(rows.values())
    return out if include_off else [c for c in out if c["enabled"]]


def connection(cid: str) -> dict | None:
    return next((c for c in connections(include_off=True) if c["id"] == cid), None)


def _secret_keys(v, path="") -> list[str]:
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            p = "%s.%s" % (path, k) if path else str(k)
            if SECRET_KEY_RX.search(str(k)):
                out.append(p)
            out += _secret_keys(x, p)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            out += _secret_keys(x, "%s[%d]" % (path, i))
    return out


def connect(cid: str, adapter: str, tool: str = "", settings: dict | None = None, enabled: bool = True,
            now: datetime | None = None) -> list[str]:
    """Add or change a connection. Returns the problems (nothing was saved), an empty list when it was saved, or,
    for a cloud login, the one line saying it was saved switched OFF: it stays off until the owner has read the
    tool's automation clause (settings terms_seen). No connection ever holds a password (Ty, 2026-11-03: customers
    connect with keys and sessions, never with a password)."""
    from . import registry
    ensure()
    settings = dict(settings or {})
    problems = []
    if not ID_RX.match(str(cid or "")):
        problems.append("a connection's name is lower case letters, digits and underscores, 2 to 40 of them "
                        "(%r is not)" % cid)
    if cid in BUILTIN and adapter != BUILTIN[cid][0]:
        problems.append("%s is the built-in %s; give this connection another name" % (cid, BUILTIN_LABELS[cid]))
    if adapter not in registry.ADAPTERS:
        problems.append("there is no %r adapter (adapters are %s)" % (adapter, ", ".join(sorted(registry.ADAPTERS))))
    bad = _secret_keys(settings)
    if bad:
        problems.append("a connection never holds a password (%s); use the tool's key, its OAuth consent, a webhook "
                        "or a session the owner captures on their own computer" % ", ".join(bad))
    try:
        json.dumps(settings)
    except (TypeError, ValueError):
        problems.append("the settings must be plain values a file can hold")
    if problems:
        return problems
    try:
        ad = registry.adapter(adapter)
    except Exception as exc:  # noqa: BLE001
        return ["the %s adapter could not be loaded (%s)" % (adapter, str(exc)[:120])]
    problems += ad.settings_problems(settings)
    if problems:
        return problems
    said = []
    if "cloud_login" in ad.modes() and enabled and not settings.get("terms_seen"):
        enabled = False
        said.append("%s is saved switched off: a cloud login reads the tool's pages, and %s Switch it on with "
                    "terms_seen set once the owner has read that." % (cid, ad.terms_note or "its terms may forbid it."))
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO kpisrc_conn (id, adapter, tool, enabled, settings, created, updated) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET adapter=excluded.adapter, tool=excluded.tool, enabled=excluded.enabled, "
                  "settings=excluded.settings, updated=excluded.updated",
                  (cid, adapter, tool_key(tool), 1 if enabled else 0, json.dumps(settings), stamp, stamp))
    return said


def disconnect(cid: str, now: datetime | None = None) -> list[str]:
    """Switch a connection off. What it already reported is kept (forget() drops that)."""
    c = connection(cid)
    if not c:
        return ["there is no connection named %s" % cid]
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with store._LOCK, store._db() as db:
        db.execute("INSERT INTO kpisrc_conn (id, adapter, tool, enabled, settings, created, updated) VALUES (?,?,?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET enabled=0, updated=excluded.updated",
                   (cid, c["adapter"], c["tool"], 0, json.dumps({k: v for k, v in c["settings"].items()
                                                                if not c["builtin"] or k not in BUILTIN[cid][2]}),
                    c["created"] or stamp, stamp))
    return []


# ---------------------------------------------------------------------- joining reports into acts
def _form(ref: str) -> str:
    return "email" if "@" in ref else "name"


def _same_person(refs_a: set, refs_b: set) -> bool:
    """The same person when both name one in the same form; an email and a first name cannot be compared, so
    they never stop a match (a wrong refusal double counts, and the agreement check would catch it anyway)."""
    for a in refs_a:
        for b in refs_b:
            if _form(a) != _form(b):
                continue
            if _form(a) == "email":
                if a.lower() != b.lower():
                    return False
            elif a.lower().split(" ")[0] != b.lower().split(" ")[0]:
                return False
    return True


def _rank(m: Event, tool: str, conns: dict) -> tuple:
    """Field precedence: the tool's own source, then DataSift, then anyone else; a live event before an export."""
    c = conns.get(m.source) or {}
    own = bool(tool) and tool_key(c.get("tool")) == tool
    ds = c.get("adapter") == "datasift_log"
    return (0 if own else (1 if ds else 2), 0 if m.grain == "event" else 1)


def build(members: list, conns: dict) -> Event:
    """One act from every report of it, field by field (the adapter design, b8): talk time, the answer, the wording
    and the person from the tool's own source, then DataSift; the record and owner from DataSift. Our own key never
    supplies a person."""
    members = sorted(members, key=lambda m: m.at)
    if len(members) == 1:
        return members[0]
    named = [m for m in members if m.author != "self" and m.tool] or [m for m in members if m.tool]
    tool = named[0].tool if named else ""
    order = sorted(members, key=lambda m: (_rank(m, tool, conns), m.at))

    def first(name):
        return next((getattr(m, name) for m in order if not _empty(getattr(m, name))), None)

    people = [m for m in order if m.author != "self" and m.person_ref]
    people.sort(key=lambda m: (_rank(m, tool, conns)[0], 0 if "@" in m.person_ref else 1, m.at))
    ds_first = sorted(members, key=lambda m: (0 if (conns.get(m.source) or {}).get("adapter") == "datasift_log" else 1,
                                              m.at))
    owns = [m for m in members if tool and tool_key((conns.get(m.source) or {}).get("tool")) == tool]
    timed = [m for m in owns if m.exact] or [m for m in members if m.exact] or members
    head = order[0]
    v = {name: first(name) for name in ("tool_id", "direction", "phone", "line", "outcome", "status", "seconds",
                                        "answered", "stage", "amount", "campaign", "raw_ref", "via")}
    for name in ("record", "owner"):
        v[name] = next((getattr(m, name) for m in ds_first if getattr(m, name)), "")
    authors = [m.author for m in order if m.author and m.author != "self"]
    v.update(tool=tool, at=min(m.at for m in timed), exact=any(m.exact for m in members),
             grain="event" if any(m.grain == "event" for m in members) else "export",
             author=authors[0] if authors else ("self" if all(m.author == "self" for m in members) else ""),
             person_ref=people[0].person_ref if people else "",
             sources=tuple(sorted({m.source for m in members})),
             members=tuple(dict.fromkeys(x for m in members for x in m.members)))
    return replace(head, **{k: ("" if x is None and k not in ("seconds", "answered", "amount") else x)
                            for k, x in v.items()})


def _natural(events: list, key) -> list[list]:
    """Group by a natural key; an event with no key stands alone."""
    groups: dict = defaultdict(list)
    alone = []
    for e in events:
        k = key(e)
        if k is None:
            alone.append([e])
        else:
            groups[k].append(e)
    return list(groups.values()) + alone


def _phone_groups(events: list) -> list[list]:
    """(number, status) once. An owner splits it only when two reports name different owners (one number on a
    husband's record and on his wife's); a report with no owner, like the agency's, joins the first."""
    acts: dict = defaultdict(list)       # (phone, status) -> [[members], ...]
    alone = []
    for e in sorted(events, key=lambda x: x.at):
        if not e.phone:
            alone.append([e])
            continue
        slots = acts[(e.phone, e.outcome)]
        if not e.owner:
            if slots:
                slots[0].append(e)
            else:
                slots.append([e])
            continue
        hit = next((s for s in slots if any(m.owner == e.owner for m in s)), None) or \
            next((s for s in slots if not any(m.owner for m in s)), None)
        if hit is None:
            slots.append([e])
        else:
            hit.append(e)
    return [s for slots in acts.values() for s in slots] + alone


def _fingerprint(atoms: list[list], kind: str, tz: str, window_s: int) -> list[list]:
    """Join atoms (each one source's act, or acts already joined on the tool's own id) that are the same call or
    text: same tool and direction, same other number, within the window (or the same local day when either gave
    only a day), the same person when both name one in the same form. One to one: two reports from one source
    never fold into one act. Nearest in time first."""
    parent = list(range(len(atoms)))
    srcs = [{m.source for m in a} for a in atoms]

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def head(a):
        return min((m for m in a if m.exact), key=lambda m: m.at, default=min(a, key=lambda m: m.at))

    buckets: dict = defaultdict(list)
    for i, a in enumerate(atoms):
        h = head(a)
        if h.phone and h.direction:
            buckets[(h.direction, h.phone)].append(i)
    pairs = []
    for idx in buckets.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                if srcs[i] & srcs[j]:
                    continue
                ti = {m.tool for m in atoms[i] if m.tool}
                tj = {m.tool for m in atoms[j] if m.tool}
                if ti and tj and not (ti & tj):
                    continue
                if not _same_person({m.person_ref for m in atoms[i] if m.person_ref},
                                    {m.person_ref for m in atoms[j] if m.person_ref}):
                    continue
                hi, hj = head(atoms[i]), head(atoms[j])
                gap = abs((hi.at - hj.at).total_seconds())
                if hi.exact and hj.exact:
                    if gap > window_s:
                        continue
                elif local_day(hi.at, tz) != local_day(hj.at, tz):
                    continue
                pairs.append((gap, i, j))
    pairs.sort()
    joined_srcs = {i: set(s) for i, s in enumerate(srcs)}
    for _gap, i, j in pairs:
        ri, rj = root(i), root(j)
        if ri == rj or joined_srcs[ri] & joined_srcs[rj]:
            continue
        parent[rj] = ri
        joined_srcs[ri] |= joined_srcs.pop(rj)
    groups: dict = defaultdict(list)
    for i, a in enumerate(atoms):
        groups[root(i)].extend(a)
    return list(groups.values())


def merge(events: list, conns: dict, tz: str | None = None, call_s: int | None = None,
          text_s: int | None = None) -> tuple[list, dict]:
    """Every report into acts. Returns (acts, {"echoes": {source: n}}). `conns` maps a connection id to its
    connection (the precedence and the echo rule read the adapter, the tool and the settings)."""
    tz = tz or setting("AGENTS_KPI_TZ", TZ)
    call_s = int(call_s if call_s is not None else setting("AGENTS_KPI_CALL_MATCH_S", 120))
    text_s = int(text_s if text_s is not None else setting("AGENTS_KPI_TEXT_MATCH_S", 30))
    folded: dict = {}
    for e in sorted(events, key=lambda x: x.at):
        k = (e.source, e.source_id)
        folded[k] = fold(folded[k], e) if k in folded else e
    claimed = {w: cid for cid, c in conns.items() for w in ((c.get("settings") or {}).get("echoes") or [])}
    kept, echoes = [], Counter()
    for e in folded.values():
        if e.author == "self" and e.via and e.via in claimed:
            echoes[claimed[e.via]] += 1
            continue
        kept.append(e)
    by_kind: dict = defaultdict(list)
    for e in kept:
        by_kind[e.kind].append(e)
    groups: list[list] = []
    groups += _natural(by_kind.pop("stage", []), lambda e: ("stage", e.record, e.stage) if e.record else None)
    groups += _phone_groups(by_kind.pop("phone_status", []))
    for kind, evs in by_kind.items():
        atoms = _natural(evs, lambda e: (e.kind, e.tool, e.tool_id) if (e.tool and e.tool_id) else None)
        if kind in ("call", "text"):
            atoms = _fingerprint(atoms, kind, tz, call_s if kind == "call" else text_s)
        groups += atoms
    acts = [build(g, conns) for g in groups]
    return sorted(acts, key=lambda a: (a.at, a.kind, a.source, a.source_id)), {"echoes": dict(echoes)}


def _primary_score(cid: str, tool: str, conns: dict) -> tuple:
    c = conns.get(cid) or {}
    own = bool(tool) and tool_key(c.get("tool")) == tool
    return (1 if own else 0, 1 if c.get("adapter") == "datasift_log" else 0)


def _created_day(c: dict, tz: str) -> str:
    try:
        return local_day(datetime.fromisoformat(str(c.get("created"))), tz) if c.get("created") else ""
    except ValueError:
        return ""


def reconcile(acts: list, conns: dict, tz: str | None = None, agree: float | None = None,
              silent_min: int | None = None) -> tuple[list, list]:
    """Per tool, kind and day for calls and texts: keep the union only when every pair of sources agrees (acts both
    reported over the smaller count, at least `agree`), else the larger source alone, with one issue. A source that
    should cover the day and sent nothing while another sent many is an issue too: silence is never a quiet day."""
    tz = tz or setting("AGENTS_KPI_TZ", TZ)
    agree = float(agree if agree is not None else setting("AGENTS_KPI_AGREE", 0.9))
    silent_min = int(silent_min if silent_min is not None else setting("AGENTS_KPI_SILENT_MIN", 10))
    groups: dict = defaultdict(list)
    out = []
    for a in acts:
        if a.kind in ("call", "text"):
            groups[(a.kind, a.tool, local_day(a.at, tz))].append(a)
        else:
            out.append(a)
    reported: dict = defaultdict(set)              # (kind, tool) -> sources that reported it on some day
    for (kind, tool, _day), group in groups.items():
        for a in group:
            reported[(kind, tool)] |= set(a.sources)
    issues = []
    for (kind, tool, day), group in sorted(groups.items()):
        counts: Counter = Counter()
        for a in group:
            for mem in a.members:
                counts[mem.split(":", 1)[0]] += 1
        what = "%s %ss on %s" % (tool_name(tool), kind, fmt_day(day))
        keep = group
        live = sorted(s for s, n in counts.items() if n > 0)
        if len(live) >= 2:
            worst = None
            for x in range(len(live)):
                for y in range(x + 1, len(live)):
                    i, j = live[x], live[y]
                    both = sum(1 for a in group if i in a.sources and j in a.sources)
                    small = min(counts[i], counts[j])
                    ratio = both / small if small else 1.0
                    if ratio < agree and (worst is None or ratio < worst[0]):
                        worst = (ratio, i, j, both, small)
            if worst:
                primary = max(live, key=lambda s: (counts[s], _primary_score(s, tool, conns), s))
                keep = [a for a in group if primary in a.sources]
                _r, i, j, both, small = worst
                issues.append({"source": "merge", "level": "problem", "tool": tool, "kind": kind, "day": day,
                               "counted_from": primary,
                               "text": "%s: %s and %s do not agree (%d of %d matched), so only %s's %d are counted."
                                       % (what, label(conns.get(i) or i), label(conns.get(j) or j), both, small,
                                          label(conns.get(primary) or primary), counts[primary])})
        biggest = max(counts.values() or [0])
        for cid, c in conns.items():
            if counts.get(cid, 0) or biggest < silent_min:
                continue
            if c.get("adapter") == "webhook":
                expected = bool(tool) and tool_key(c.get("tool")) == tool and kind in _kinds_of(c) and \
                    (not _created_day(c, tz) or _created_day(c, tz) <= day)
            elif c.get("adapter") == "datasift_log":
                expected = cid in reported[(kind, tool)]
            else:
                expected = False
            if expected:
                loud = max(counts, key=lambda s: counts[s])
                issues.append({"source": "merge", "level": "problem", "tool": tool, "kind": kind, "day": day,
                               "silent": cid,
                               "text": "%s: %s reported none while %s shows %d." % (
                                   what, label(c), label(conns.get(loud) or loud), counts[loud])})
        out += keep
    return sorted(out, key=lambda a: (a.at, a.kind, a.source, a.source_id)), issues


def _kinds_of(c: dict) -> set:
    from . import registry
    try:
        return set(registry.adapter(c["adapter"]).kinds(c))
    except Exception:  # noqa: BLE001
        return set()


def _covered(t: Tally, acts: list, tz: str) -> bool:
    test = METRIC_TEST.get(t.metric)
    if test is None:
        return False
    for a in acts:
        if t.tool and a.tool != t.tool:
            continue
        day = local_day(a.at, tz)
        if (day == t.period or day[:7] == t.period) and test(a):
            return True
    return False


def _tally_in(t: Tally, since: datetime, until: datetime, tz: str) -> bool:
    lo, hi = local_day(since, tz), local_day(until - timedelta(seconds=1), tz)
    if len(t.period) == 7:
        return lo[:7] <= t.period <= hi[:7]
    return lo <= t.period <= hi


def _kinds(kinds) -> tuple:
    if isinstance(kinds, str):
        kinds = (kinds,)
    out = []
    for k in kinds or KINDS:
        k = str(k or "").strip().lower()
        if "convers" in k:
            raise ValueError(NOT_A_KIND)
        if k not in KINDS:
            raise ValueError("kind %r is not one of %s" % (k, ", ".join(KINDS)))
        out.append(k)
    return tuple(dict.fromkeys(out))


def _business_now(now: datetime, tz: str) -> bool:
    t = now.astimezone(zone(tz))
    return t.weekday() < 5 and 10 <= t.hour < 20


def _quiet_for(last: datetime, now: datetime, tz: str) -> timedelta:
    """How long a source has been quiet in business time today: measured from its last report or from 9:00 this
    morning, whichever is later, so a weekend or a night never reads as a dead source on Monday morning."""
    t = now.astimezone(zone(tz))
    opening = t.replace(hour=9, minute=0, second=0, microsecond=0)
    return now - max(last, opening)


def collect(since: datetime, until: datetime, kinds=KINDS, now: datetime | None = None, tz: str | None = None,
            only=None, agree: float | None = None) -> dict:
    """Every connected source between two moments, each act counted once.

    Returns {"events": [Event, ...], "tallies": [Tally, ...], "health": [dict, ...]}. Events are merged acts
    (each lists every source that reported it); tallies are never added to them. Health has one row per source
    ({"source", "label", "level": ok | problem, "events", "complete", "through", "problems", ...}) and one row per
    issue the merge found ({"source": "merge", "level", "text"}). A source that cannot be read is a problem in its
    row, never a crash: the others still count."""
    from . import registry
    kinds = _kinds(kinds)
    now = now or datetime.now(timezone.utc)
    tz = tz or setting("AGENTS_KPI_TZ", TZ)
    since, until = utc(since), utc(until)
    if until <= since:
        raise ValueError("until must come after since")
    ensure()
    prune(now)
    margin = timedelta(hours=max(0.0, float(setting("AGENTS_KPI_MARGIN_HOURS", 2.0))))
    conns = {c["id"]: c for c in connections() if only is None or c["id"] in set(only)}
    reports, tallies, health = [], [], []
    for cid, c in conns.items():
        row = {"source": cid, "label": label(c), "adapter": c["adapter"], "tool": c["tool"], "level": "ok",
               "events": 0, "tallies": 0, "complete": True, "through": "", "last_at": "", "problems": [], "notes": []}
        health.append(row)
        try:
            ad = registry.adapter(c["adapter"])
        except Exception as exc:  # noqa: BLE001
            row.update(level="problem", problems=["%s: its adapter could not be loaded (%s)" % (label(c), str(exc)[:100])])
            continue
        want = tuple(k for k in kinds if k in ad.kinds(c))
        if not want:
            continue
        try:
            # A margin on BOTH sides: a dial just before midnight is answered just after it, and the act (placed at
            # its own start) must be read whole by the window that owns it, and by no other.
            b = ad.pull(c, since - margin, until + margin, want, now=now)
        except Exception as exc:  # noqa: BLE001  (one source failing never sinks the others)
            log.exception("KPI source %s could not be read", cid)
            row.update(level="problem", complete=False,
                       problems=["%s could not be read (%s)" % (label(c), str(exc)[:120])])
            continue
        evs = []
        for e in b.events:
            if e.kind not in want:
                continue
            if e.source != cid:      # the source of an event is always its connection
                e = replace(e, source=cid, sources=(cid,), members=("%s:%s" % (cid, e.source_id),))
            evs.append(e)
        reports += evs
        tl = [t for t in b.tallies if (METRIC_KIND.get(t.metric) in want or t.metric == "as_reported")
              and _tally_in(t, since, until, tz)]
        for t in tl:
            t.source = cid
        tallies += tl
        problems = list(b.problems)
        gap = getattr(ad, "normal_gap_hours", None)
        if gap and _business_now(now, tz):
            if b.last_at and _quiet_for(b.last_at, now, tz) > timedelta(hours=float(gap)):
                problems.append("%s: nothing has arrived since %s." % (label(c), fmt_when(b.last_at, tz)))
            elif not b.last_at and c.get("created"):
                try:
                    made = datetime.fromisoformat(c["created"])
                except ValueError:
                    made = None
                if made and _quiet_for(made, now, tz) > timedelta(hours=float(gap)):
                    problems.append("%s: nothing has arrived since it was connected on %s." % (label(c), fmt_day(
                        local_day(made, tz))))
        row.update(events=sum(1 for e in evs if since <= e.at < until), tallies=len(tl), complete=b.complete,
                   through=b.through.isoformat(timespec="seconds") if b.through else "",
                   last_at=b.last_at.isoformat(timespec="seconds") if b.last_at else "",
                   problems=problems, notes=list(b.notes), level="problem" if problems else "ok")
    acts, info = merge(reports, conns, tz)
    acts = [a for a in acts if since <= a.at < until]
    acts, issues = reconcile(acts, conns, tz, agree)
    for t in tallies:
        t.covered = _covered(t, acts, tz)
    health += issues
    for cid, n in sorted((info.get("echoes") or {}).items()):
        health.append({"source": "merge", "level": "note",
                       "text": "%d row(s) our own key wrote repeat what %s already reports, so each is counted once."
                               % (n, label(conns.get(cid) or cid))})
    return {"events": acts, "tallies": tallies, "health": health}


# ---------------------------------------------------------------------- conversations, from the catalog only
def conversations(acts: list, cat, tz: str | None = None) -> dict:
    """The conversations among merged acts, by the catalog's definition and nothing else: an answered call of
    `min_seconds` or more either way (an inbound one when counts_inbound_call), or an inbound text when
    counts_inbound_text. A text or a call back counts once per owner a day, and not at all for an owner who had an
    outbound conversation that day (kpi.caller_days does the same). An answered call whose length no source gave is
    "length unknown", never a conversation, and is counted on its own so the reader sees how many there were.

    Returns {"items": [{"kind": call | call back | text, "at", "day", "party", "act"}], "length_unknown": n}."""
    tz = tz or setting("AGENTS_KPI_TZ", TZ)
    d = cat.definition("conversation")
    min_s = int(d["min_seconds"])
    items, extra, unknown = [], [], 0
    talked = set()
    for a in sorted(acts, key=lambda x: x.at):
        party = a.owner or a.record or a.phone
        day = local_day(a.at, tz)
        if a.kind == "call" and a.answered is True:
            if a.seconds is None:
                unknown += 1
                continue
            if a.seconds < min_s:
                continue
            if a.direction == "in":
                if d["counts_inbound_call"]:
                    extra.append(("call back", a, day, party))
            elif a.direction == "out":
                items.append({"kind": "call", "at": a.at, "day": day, "party": party, "act": a})
                talked.add((day, party))
        elif a.kind == "text" and a.direction == "in" and d["counts_inbound_text"]:
            extra.append(("text", a, day, party))
    seen = set()
    for kind, a, day, party in extra:
        if (day, party) in talked or (day, party) in seen:
            continue
        seen.add((day, party))
        items.append({"kind": kind, "at": a.at, "day": day, "party": party, "act": a})
    return {"items": sorted(items, key=lambda x: x["at"]), "length_unknown": unknown}


def public_health() -> dict:
    """Counts only, for the brain's health page (it answers anyone): sources switched on, deliveries kept, and
    deliveries not read yet. Never a number, a name or a street."""
    try:
        ensure()
        with store._LOCK, store._db() as c:
            raw = c.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN parsed='' THEN 1 ELSE 0 END) AS waiting "
                            "FROM kpisrc_raw").fetchone()
        on = connections()
        return {"sources_on": len(on), "webhooks_on": sum(1 for c in on if c["adapter"] == "webhook"),
                "deliveries_kept": int(raw["n"] or 0), "deliveries_waiting": int(raw["waiting"] or 0)}
    except Exception:  # noqa: BLE001  (a health page never fails on a side table)
        return {}
