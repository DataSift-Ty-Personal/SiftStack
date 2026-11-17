"""Mail counted from a date: the auction campaign and the probate campaign (Ty, 2026-11-16).

Mail ran as one calendar (mail.py): a piece a month, each preset keyed on the record's own mail
counter. Ty split it into three campaigns, because two kinds of record run on a clock a court sets
(output/siftmail/SPEC.md, sections 6 and 7):

  AUCTION   a foreclosure notice lands a median 34 days before the sale (25 to 47 for the middle
            half). A piece a month is one letter, maybe two, before the house is sold. So the pieces
            count BACK from the sale date: the first the day the record comes in, then one every 5
            to 7 days, every 3 to 4 days in the last ten, 4 to 6 pieces in all, each a different
            format, and nothing that lands later than 2 days before the sale. A moved sale date
            re-times the pieces left. A sale that passes with no new date ends the countdown and
            hands the record to the monthly campaign (58% of borrowers still own the house 60 days
            after the sale date, so it must not go quiet).
  PROBATE   an estate is decided in its first months (Tennessee's creditor window is four). So the
            pieces count FORWARD from the day the estate opened: the first the week it opens, two
            more about two weeks apart, monthly through month 12, then about every 75 days through
            month 24, never two closer than 14 days. Only a house confirmed as the decedent's: a
            letter about an estate at the executor's own home or a stranger's house is worse than
            no letter.
  BULK      the Top 1,000 and the evergreen lists stay on mail.py's monthly lane.

TIMED BY THE DAY A PIECE LANDS, not the day it is staged. SiftMail prints only on a weekday at least
two business days out (three after 22:00 UTC, except Friday and Saturday: the app's
getNextAvailableSendDateDM), then the mail takes about three days, so a piece staged today lands
about a week later. Every rule above is about the mailbox, so each decision is made on the in-home
day: the later of the send date plus delivery_days and today plus lead_calendar_days.

THE SETTINGS are the SPEC's playbook blocks (section 7), under this kind:
  anchor            sale_date (the countdown) or filed_date (the probate table)
  scope             lists, tags (all required) and not_tags; or presets, as any stage
  require           decedent_house: the probate house check below
  clock             sale date: anchor_field, gap_days, final_window_days, final_gap_days,
                    latest_in_home_days_before, lead_calendar_days, delivery_days, max_pieces, fill
                    {first, middle, last}, postponed, after_sale {grace_days, hand_to, start_at_step}
                    filed date: anchor_field, min_gap_days, steps [{piece, at: on_entry |
                    after_days}], after_month_12 {every_days, rotate, until_month}
  pieces            the designs, each {id, piece, product_id, envelope, design}, and a fallback id
  min_batch         the fewest records one SiftMail campaign may hold (20, DataSift's bulk floor)
A piece's place in `pieces` is the step mail.deliver groups a campaign by, and the ledger remembers
what went out by that place: add a piece at the end, never in the middle.

THE COUNTDOWN. Slots are in-home days: from the last piece, gap_days later while that day is more
than final_window_days before the sale, else final_gap_days later (never before the window opens),
and none after the cutoff. The first slot takes the first piece the day the record comes in. The
last slots take the closing pieces; the middle ones go in order and drop from the end when time
is short; when there are fewer pieces than slots, the earliest slots stay empty so the last ten
days keep their cadence. A postponed sale re-times what is left; when nothing is left, the closing
pair runs once more before the new date, never the same piece twice in a row.

THE PROBATE TABLE. The first step goes the week the record comes in, each later step after_days
after the one before (14, 14, then 30), through month 12; after that one piece every_days apart,
the rotation in turn, to month 24. A record that comes in after month 12 starts on the rotation.
Never two pieces closer than min_gap_days, counting every mail piece the autopilot sent the record
from any stage, and the record's own last mailed date.

ONE CAMPAIGN PER RECORD (SPEC 7). The auction owns a record with a sale ahead or one passed in the
last grace_days; the probate campaign owns a confirmed probate record; a sale date comes first.
The owner is written as a fact (CLAIM_FACT) with the day it lapses. claimed_by() is what the monthly
mail stage asks so it leaves the record alone, and it honors a claim only from a stage that is live
on this box, so a campaign still in plan mode never silences the monthly lane. After the sale,
handoff_step() tells the monthly stage which step to start the record at.

THE LANE. These two campaigns are the mail clock for the records they own (SPEC 7), so the lane
timeline's monthly rule (the first piece on day 8, then 30 days apart) is not asked here: that rule
is the bulk campaign's, and asked here it would hold a probate's first letter past the week the estate
opens. The gate still runs when a piece is picked and again at release (an owner held in a higher lane
on another record is held here too), and a lane worked outside the building, the agency's, gets nothing.

THE HOUSE (probate). In order: a not_tags mark (Wrong House, Estate Sold) means no; a tag a person
uses to confirm a house means yes; then the 2026-11-11 address audit (the decedent on the title, a
name variant or an inherited house are confirmed; a flag the probate stage has not settled holds);
then a record not in the audit and opened on or after guard_since, whose house could only have
come through the owner-of-record guard at upload. Anything else waits for a person.

THE FLOOR. A SiftMail campaign is a bulk job; DataSift's guidance is 20 records or more, and the
auction sees about 7 new records a week. A piece whose group is under min_batch is not staged: it
waits, and the run says how many and for how long. One piece's records are never split into jobs
under the floor. Lowering min_batch is Ty's call (SPEC 10.7).

A PIECE THAT CANNOT BE ORDERED (the Snap Pack, product 18, has no price row in this account and is
blocked in DataSift's builder) is refused with a plain problem; its fallback piece goes instead when
it names one, otherwise its slot goes to the next piece.

STAGED, NEVER SENT. Live, the pieces become a batch of kind mail, the shape mail.release already
releases; a person releases it. At release mail.deliver must ask at_release() below: a person may
release days later, so the send date is set again by SiftMail's rule and an auction piece that would
now land after the cutoff is dropped, never sent late. Plan mode writes nothing to the account and
says what it would stage today, and what falls due over the next seven days with its cost.
"""
from __future__ import annotations

import csv
import difflib
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
from . import auctiontasks, gate, laneroutes, lanes, ledger, mail, probate, researchqueue
from . import policy as policy_mod
from .stages import GONE_STATUSES, Ctx, addr, marks_of, merge_must, preset_rows, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KIND = "mailcadence"
ANCHORS = ("sale_date", "filed_date")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CLAIM_FACT = "mail_campaign"          # the date campaign timing the record's mail, and the day that lapses
HANDOFF_FACT = "mail_handoff"         # an auction record handed to the monthly campaign, and at which step
FILED_FACT = "mailcadence:filed"      # a probate record's open date, where it was read, and its marks
WAIT_FACT = "mailcadence:wait"        # since when a record's piece has waited on the SiftMail floor
SALE_FACT = "mailcadence:sale"        # the sale date a record was last timed against (a later one is a postponement)

# prod-account's SiftMail price table, read 2026-11-16 (SPEC section 1): business plan rows by mail type, which
# is the ENVELOPE code for a letter and the product id for a card or self-mailer. A piece whose mail
# type has no row cannot be ordered. The Snap Pack (product 18) has none: it is blocked in DataSift's
# builder. When DataSift adds the row, list it under mail_type_prices in the stage's block.
MAIL_TYPE_PRICES = {1: 1.10, 3: 1.22, 5: 1.23, 7: 1.23, 11: 1.18, 13: 0.67, 14: 0.74, 15: 1.01, 16: 1.89, 17: 1.89}

AUCTION_CLOCK = {"anchor_field": "Foreclosure Date", "first": "on_entry", "gap_days": 6, "final_window_days": 10,
                 "final_gap_days": 3, "latest_in_home_days_before": 2, "lead_calendar_days": 7, "delivery_days": 3,
                 "max_pieces": 6, "fill": {}, "postponed": "retime",
                 "after_sale": {"grace_days": 14, "hand_to": "", "start_at_step": 1},
                 "refresh_days": 1, "date_fields": ["auction_date", "tax_auction_date"]}
PROBATE_CLOCK = {"anchor_field": "Probate Open Date", "min_gap_days": 14, "lead_calendar_days": 7, "delivery_days": 3,
                 "steps": [], "after_month_12": {"every_days": 75, "rotate": [], "until_month": 24}}
TOP = {"anchor": "", "scope": {}, "require": "", "clock": {}, "pieces": [], "min_batch": 20, "daily_pieces": 50,
       "max_reads": 150, "max_scan": 3000, "min_gap_days": None, "weekly_batch_day": "", "audit_csv": "",
       "audit_confirms": ["ok", "inherited", "name_variant"], "guard_since": "2026-11-11",
       "house_confirmed_tags": [], "mail_type_prices": {}}
AUDIT_KEYS = ("ok", "name_variant", "inherited", "own_home", "unrelated", "sold")
VERDICT_WORDS = {"PR_OWN_HOME": "the representative's own home", "DIFFERENT_OWNER": "a different owner on the county roll",
                 "NOT_ON_ROLL": "no parcel on the county roll at that street", "SURNAME_ONLY": "the county owner shares only the surname",
                 "NO_DECEDENT": "no decedent name to test", "LOOKUP_FAILED": "the county lookup failed"}
# What else a stage block may carry: keys the engine and mail.py read. Anything else close to a known
# key is refused as a typo when the playbook loads, so it is never read as a default.
KNOWN = set(TOP) | {"presets", "prices", "vendor", "internal_routes", "send", "sender", "undocumented_counter",
                    "mailed_tag", "last_mailed_field", "lane", "lanes", "pipeline", "covers", "gate_allow", "read_fields",
                    "daily_usd", "max_records", "summary"}
PIECE_KNOWN = {"id", "piece", "product_id", "envelope", "design", "design_noname", "fallback", "copy",
               "external_identifier", "external_identifier_noname", "street_view"}
POLICY_KEYS = {"kind", "owner", "mode", "summary", "schedule", "presets"}


# ---------------------------------------------------------------------- small helpers
def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _as_dict(v) -> dict:
    return dict(v) if isinstance(v, dict) else {}


def _low(items) -> set:
    return {str(x).strip().lower() for x in items or [] if str(x).strip()}


def _day(v) -> date | None:
    s = researchqueue.iso_day(v)
    return date.fromisoformat(s) if s else None


def _when(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _whole(v, low: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= low


def nice(d: date) -> str:
    return laneroutes.nice_day(d)


def weekday_nice(d: date) -> str:
    return "%s %s" % (d.strftime("%a"), laneroutes.nice_day(d))


def _entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


# ---------------------------------------------------------------------- the send date and the in-home day
def next_send_date(now: datetime) -> date:
    """The earliest day SiftMail prints a piece ordered at `now`. The app allows only Monday to Friday,
    two business days out at the earliest, three when it is ordered after 22:00 UTC (except on a
    Friday or Saturday, when the weekend already gives the printer its time). A campaign dated sooner
    is one the app would never let a person place."""
    u = now.astimezone(timezone.utc)
    left = 3 if u.hour >= 22 and u.weekday() not in (4, 5) else 2
    d = u.date()
    while left:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def in_home(now: datetime, tz: str, lead: int, delivery: int) -> date:
    """The day a piece staged at `now` should reach the mailbox: the send date SiftMail allows plus the
    days in the mail, and never sooner than `lead` calendar days after the staging day."""
    today = now.astimezone(ZoneInfo(tz)).date()
    return max(next_send_date(now) + timedelta(days=delivery), today + timedelta(days=lead))


# ---------------------------------------------------------------------- settings
def options(st) -> dict:
    o = {}
    for k, v in TOP.items():
        got = st.get(k)
        o[k] = v if got is None else got
    o["anchor"] = str(o["anchor"] or "").strip().lower()
    scope = _as_dict(o["scope"])
    o["lists"] = [str(x) for x in _list(scope.get("lists"))]
    o["tags"] = [str(x) for x in _list(scope.get("tags"))]
    o["not_tags"] = [str(x) for x in _list(scope.get("not_tags"))]
    base = AUCTION_CLOCK if o["anchor"] == "sale_date" else PROBATE_CLOCK
    clock = dict(base)
    clock.update(_as_dict(o["clock"]))
    for k in ("after_sale", "after_month_12"):
        if k in base:
            clock[k] = dict(base[k], **_as_dict(clock.get(k)))
    o["clock"] = clock
    if o["min_gap_days"] is None:
        o["min_gap_days"] = clock.get("min_gap_days") if o["anchor"] == "filed_date" else clock.get("final_gap_days")
    return o


def _lead(o: dict) -> tuple[int, int]:
    c = o["clock"]
    return int(c.get("lead_calendar_days", 7)), int(c.get("delivery_days", 3))


@dataclass
class Design:
    no: int            # its place in `pieces`: the step mail.deliver groups one SiftMail campaign by
    id: str
    kind: str          # the piece kind, which mail.py prices and labels
    raw: dict
    fallback: str = ""

    @property
    def label(self) -> str:
        return "%s (%s)" % (self.id, mail.PIECE_LABEL.get(self.kind, self.kind.replace("_", " ")))

    @property
    def mail_type(self):
        env = self.raw.get("envelope")
        return self.raw.get("product_id") if env is None else env


def designs_of(st) -> list[Design]:
    out = []
    for i, raw in enumerate(_list(st.get("pieces")), 1):
        if not isinstance(raw, dict):
            continue
        fb = raw.get("fallback")
        out.append(Design(no=i, id=str(raw.get("id") or "piece %d" % i).strip(), kind=str(raw.get("piece") or "").strip(),
                          raw=raw, fallback=fb.strip() if isinstance(fb, str) else ""))
    return out


def price_table(o: dict) -> dict:
    table = dict(MAIL_TYPE_PRICES)
    for k, v in _as_dict(o["mail_type_prices"]).items():
        try:
            table[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return table


def usable(designs: list[Design], o: dict, mailer, out=None) -> dict:
    """id -> the design that prints for it: itself, its fallback when it cannot be ordered, or nothing
    (left out of the map, so its slot goes to the next piece). Only SiftMail has the price table: an
    outside vendor's file goes to a mail house that prints what it is sent."""
    by_id = {d.id: d for d in designs}
    if not getattr(mailer, "batch", False):
        return dict(by_id)
    table = price_table(o)

    def orderable(d: Design) -> bool:
        t = d.mail_type
        if t is None:
            return True                  # no product at all: the design check says so, by name
        try:
            return int(t) in table
        except (TypeError, ValueError):
            return False

    res = {}
    for d in designs:
        if orderable(d):
            res[d.id] = d
            continue
        fb = by_id.get(d.fallback)
        if fb is not None and orderable(fb):
            res[d.id] = fb
            if out is not None:
                out.problem("Piece %s cannot be ordered: SiftMail has no price for mail type %s in this account (the Snap Pack, "
                            "product 18, is blocked in DataSift's builder). Its fallback %s goes in its place." % (d.id, d.mail_type, fb.id))
        elif out is not None:
            out.problem("Piece %s cannot be ordered: SiftMail has no price for mail type %s in this account (the Snap Pack, "
                        "product 18, is blocked in DataSift's builder). It is left out and its slot goes to the next piece; "
                        "once DataSift has the price row, add it under mail_type_prices." % (d.id, d.mail_type))
    return res


def _params(st) -> dict:
    p = getattr(st, "params", None)
    if isinstance(p, dict):
        return p
    return {k: v for k, v in (st if isinstance(st, dict) else {}).items() if k not in POLICY_KEYS}


def block_problems(st) -> list[str]:
    """What is wrong with a block, in the playbook checker's words: a typo must fail `autopilot check`
    instead of being read as a default (a mistyped min_gap_days would let probate pieces go a week apart)."""
    sid = getattr(st, "id", "") or "this stage"
    out: list[str] = []

    def say(text: str) -> None:
        out.append("stage %s: %s" % (sid, text))

    o = options(st)
    anchor = o["anchor"]
    if anchor not in ANCHORS:
        say("anchor must be sale_date (count back from the sale) or filed_date (count forward from the day the estate opened)")
    for k in _params(st):
        if k not in KNOWN:
            near = difflib.get_close_matches(k, sorted(KNOWN), n=1, cutoff=0.8)
            if near:
                say("%r is not a setting; did you mean %s?" % (k, near[0]))
    if str(st.get("send") or "staged") != "staged":
        say("send must be staged: this stage only stages its pieces, a person releases them")
    scope = st.get("scope")
    if scope is not None and not isinstance(scope, dict):
        say("scope must be a mapping of lists, tags and not_tags")
    for k in _as_dict(scope):
        if k not in ("lists", "tags", "not_tags"):
            say("scope.%s is not a setting (lists, tags, not_tags)" % k)
    presets = getattr(st, "presets", None)
    if presets is None:
        presets = _list(st.get("presets"))
    if not presets and not o["lists"]:
        say("name the records: scope.lists or presets")
    for key, low in (("min_batch", 1), ("daily_pieces", 0), ("max_reads", 1), ("max_scan", 1)):
        if not _whole(o[key], low):
            say("%s must be a whole number, %d or more" % (key, low))
    if not _whole(o["min_gap_days"], 0):
        say("min_gap_days must be a whole number of days")
    raw = st.get("pieces")
    if not isinstance(raw, list) or not raw:
        say("list the pieces, each with an id, a piece kind, a product_id and a design")
        raw = []
    designs = designs_of(st)
    ids = [d.id for d in designs]
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        say("piece ids must be unique; %s appears twice" % ", ".join(dup))
    prices = _as_dict(st.get("prices"))
    for i, pc in enumerate(raw, 1):
        if not isinstance(pc, dict):
            say("piece %d must be a mapping" % i)
            continue
        name = str(pc.get("id") or i)
        kind = str(pc.get("piece") or "")
        if kind not in policy_mod.PIECES and kind not in prices:
            say("piece %s: the piece kind %r has no price; use %s, or price it under prices" % (name, kind, ", ".join(policy_mod.PIECES)))
        for k in ("product_id", "envelope"):
            if pc.get(k) is not None and not _whole(pc.get(k), 0):
                say("piece %s: %s must be a whole number" % (name, k))
        fb = pc.get("fallback")
        if fb is not None and (not isinstance(fb, str) or fb not in ids or fb == pc.get("id")):
            say("piece %s: fallback must name another piece in the list by its id" % name)
        for k in pc:
            if k not in PIECE_KNOWN:
                say("piece %s: %r is not a piece setting (%s)" % (name, k, ", ".join(sorted(PIECE_KNOWN))))
    clock = st.get("clock")
    if clock is not None and not isinstance(clock, dict):
        say("clock must be a mapping")
    base = AUCTION_CLOCK if anchor == "sale_date" else PROBATE_CLOCK
    for k in _as_dict(clock):
        if anchor in ANCHORS and k not in base:
            say("clock.%s is not a %s setting (%s)" % (k, anchor, ", ".join(sorted(base))))
    c = o["clock"]
    if anchor == "sale_date":
        for key, low in (("gap_days", 1), ("final_window_days", 0), ("final_gap_days", 1), ("latest_in_home_days_before", 0),
                         ("lead_calendar_days", 0), ("delivery_days", 0), ("max_pieces", 1), ("refresh_days", 0)):
            if not _whole(c.get(key), low):
                say("clock.%s must be a whole number, %d or more" % (key, low))
        if _whole(c.get("gap_days"), 1) and _whole(c.get("final_gap_days"), 1) and c["gap_days"] < c["final_gap_days"]:
            say("clock.gap_days must be at least final_gap_days: the pieces come closer together as the sale nears")
        fill = c.get("fill")
        if fill is not None and not isinstance(fill, dict):
            say("clock.fill must be a mapping of first, middle and last")
        f = _as_dict(fill)
        named = ([f.get("first")] if f.get("first") else []) + _list(f.get("middle")) + _list(f.get("last"))
        for x in named:
            if str(x) not in ids:
                say("clock.fill names %s, which is not a piece id" % x)
        if str(c.get("postponed") or "retime") not in ("retime", "keep"):
            say("clock.postponed must be retime or keep")
        a = _as_dict(c.get("after_sale"))
        if not _whole(a.get("grace_days"), 0) or not _whole(a.get("start_at_step"), 1):
            say("clock.after_sale needs grace_days (0 or more) and start_at_step (1 or more)")
        if str(o["weekly_batch_day"] or ""):
            say("weekly_batch_day is for the probate table; a countdown cannot wait a week")
        if str(o["require"] or ""):
            say("require is for the probate table")
    elif anchor == "filed_date":
        for key, low in (("min_gap_days", 1), ("lead_calendar_days", 0), ("delivery_days", 0)):
            if not _whole(c.get(key), low):
                say("clock.%s must be a whole number, %d or more" % (key, low))
        steps = c.get("steps")
        if not isinstance(steps, list) or not steps:
            say("clock.steps must list the table: {piece, at: on_entry} first, then {piece, after_days}")
            steps = []
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict) or str(s.get("piece") or "") not in ids:
                say("clock.steps %d must name a piece id" % i)
                continue
            if i == 1 and str(s.get("at") or "") != "on_entry":
                say("clock.steps 1 must be at: on_entry (the first piece goes the week the estate opens)")
            if i > 1 and not _whole(s.get("after_days"), 1):
                say("clock.steps %d needs after_days, a whole number of days after the step before" % i)
        am = _as_dict(c.get("after_month_12"))
        if not _whole(am.get("every_days"), 1) or not _whole(am.get("until_month"), 12):
            say("clock.after_month_12 needs every_days (1 or more) and until_month (12 or more)")
        for x in _list(am.get("rotate")):
            if str(x) not in ids:
                say("clock.after_month_12.rotate names %s, which is not a piece id" % x)
        day = str(o["weekly_batch_day"] or "").strip().lower()[:3]
        if day and day not in DAYS[:5]:
            say("weekly_batch_day must be a weekday, Mon to Fri")
        if str(o["require"] or "") not in ("", "decedent_house"):
            say("require must be decedent_house")
        bad = [x for x in _list(o["audit_confirms"]) if str(x) not in AUDIT_KEYS]
        if bad:
            say("audit_confirms has %s (use %s)" % (bad, ", ".join(AUDIT_KEYS)))
        if not _day(o["guard_since"]):
            say("guard_since must be a date, YYYY-MM-DD")
    for k, v in _as_dict(o["mail_type_prices"]).items():
        try:
            int(k)
            float(v)
        except (TypeError, ValueError):
            say("mail_type_prices maps a mail type number to its price in dollars")
            break
    return out


# ---------------------------------------------------------------------- the countdown (sale date)
@dataclass
class Countdown:
    gap: int
    final_window: int
    final_gap: int
    latest: int
    max_pieces: int
    first: str
    middle: list
    last: list
    grace: int
    hand_to: str
    start_at_step: int
    retime: bool


def countdown_of(o: dict, designs: list[Design]) -> Countdown:
    c = o["clock"]
    f = _as_dict(c.get("fill"))
    ids = [d.id for d in designs]
    if f:
        first, middle, last = str(f.get("first") or ""), [str(x) for x in _list(f.get("middle"))], [str(x) for x in _list(f.get("last"))]
    else:
        first, middle, last = (ids[0] if ids else ""), ids[1:-2] if len(ids) > 2 else [], ids[-2:] if len(ids) > 2 else ids[1:]
    a = _as_dict(c.get("after_sale"))
    return Countdown(gap=int(c["gap_days"]), final_window=int(c["final_window_days"]), final_gap=int(c["final_gap_days"]),
                     latest=int(c["latest_in_home_days_before"]), max_pieces=int(c["max_pieces"]), first=first,
                     middle=middle, last=last, grace=int(a.get("grace_days", 14)), hand_to=str(a.get("hand_to") or ""),
                     start_at_step=int(a.get("start_at_step") or 1), retime=str(c.get("postponed") or "retime") == "retime")


@dataclass
class Decision:
    design: Design | None = None
    why: str = ""
    final: bool = False
    opens: int = 0


def next_slot(prev: date, sale: date, c: Countdown) -> date:
    """The in-home day after `prev`: gap days later while that day is more than final_window days before
    the sale; inside the window final_gap days later, and never before the window opens."""
    step = prev + timedelta(days=c.gap)
    if (sale - step).days > c.final_window:
        return step
    return max(prev + timedelta(days=c.final_gap), sale - timedelta(days=c.final_window))


def slots_from(start: date, sale: date, c: Countdown) -> list[date]:
    cutoff = sale - timedelta(days=c.latest)
    out, d = [], start
    while d <= cutoff and len(out) < 60:
        out.append(d)
        d = next_slot(d, sale, c)
    return out


def fill(first: list, middle: list, last: list, r: int) -> list:
    """The pieces for r slots, in order: the first piece, the middle ones in order (dropping from the
    end when time is short), and the closing pieces in the last slots (the very last kept when only
    one fits)."""
    if r <= 0:
        return []
    head = list(first[:1])
    room = r - len(head)
    n = min(max(room, 0), len(last))
    tail = list(last[len(last) - n:]) if n else []
    return head + list(middle[:max(0, room - n)]) + tail


def _opens(start: date, sale: date, c: Countdown, k: int) -> int:
    for i in range(1, 61):
        if len(slots_from(start + timedelta(days=i), sale, c)) <= k:
            return i
    return 0


def countdown_next(sent: list, sale: date, lands: date, c: Countdown, avail: dict, rerun_ok: bool = False) -> Decision:
    """The auction piece to stage now, if any. `sent` is [(printed piece id, in-home day)] in order;
    `lands` is the day a piece staged now reaches the mailbox."""
    cutoff = sale - timedelta(days=c.latest)
    if lands > cutoff:
        return Decision(why="too close to the sale for a piece to land in time", final=True)
    ids = [x for x, _ in sent]
    printed = set(ids)
    got = printed | {k for k, d in avail.items() if d.id in printed}

    def keep(xs):
        return [x for x in xs if x and x not in got and x in avail]

    first, middle, last = keep([c.first]), keep(c.middle), keep(c.last)
    pool = first + middle + last
    left = c.max_pieces - len(sent)
    if not pool or left <= 0:
        again = [x for x in c.last if x in avail]
        if ids and again and avail[again[0]].id == ids[-1]:
            again = again[1:]                     # never the same piece twice in a row
        if not (rerun_ok and again):
            return Decision(why="every piece of the countdown has gone", final=True)
        first, middle, last, pool, left = [], [], again, again, len(again)
    if sent:
        nxt = next_slot(sent[-1][1], sale, c)
        if lands < nxt:
            return Decision(why="waiting for the next slot", opens=(nxt - lands).days)
    r = len(slots_from(lands, sale, c))
    k = min(len(pool), left)
    if sent and r > k:
        # More slots than pieces: the earliest stay empty, so the last ten days keep their cadence.
        return Decision(why="waiting for a later slot: more slots than pieces left", opens=_opens(lands, sale, c, k))
    seq = fill(first, middle, last, min(r, k))
    if not seq:
        return Decision(why="every piece of the countdown has gone", final=True)
    return Decision(design=avail[seq[0]])


# ---------------------------------------------------------------------- the probate table (filed date)
@dataclass
class Table:
    min_gap: int
    steps: list            # [(piece id, after_days or None for on entry)]
    every: int
    rotate: list
    table_end: int         # day 365: the end of month 12
    end: int               # the last day of the campaign (month 24)
    until_month: int


def month_days(n: int) -> int:
    return int(round(n * 365.25 / 12))


def table_of(o: dict) -> Table:
    c = o["clock"]
    am = _as_dict(c.get("after_month_12"))
    steps = []
    for s in _list(c.get("steps")):
        if isinstance(s, dict) and s.get("piece"):
            after = None if str(s.get("at") or "") == "on_entry" or s.get("after_days") is None else int(s["after_days"])
            steps.append((str(s["piece"]), after))
    until = int(am.get("until_month", 24))
    return Table(min_gap=int(c.get("min_gap_days", 14)), steps=steps, every=int(am.get("every_days", 75)),
                 rotate=[str(x) for x in _list(am.get("rotate"))], table_end=month_days(12), end=month_days(until),
                 until_month=until)


def table_next(sent: list, opened: date, lands: date, t: Table, avail: dict) -> Decision:
    """The probate piece to stage now, if any, by its in-home day `lands`."""
    age = (lands - opened).days
    if age > t.end:
        return Decision(why="past month %d: the probate campaign has ended" % t.until_month, final=True)
    last_id = sent[-1][0] if sent else ""
    if sent and (lands - sent[-1][1]).days < t.min_gap:
        return Decision(why="waiting: pieces are at least %d days apart" % t.min_gap,
                        opens=t.min_gap - (lands - sent[-1][1]).days)
    in_table = [s for s in sent if (s[1] - opened).days <= t.table_end]
    rotated = len(sent) - len(in_table)
    if age <= t.table_end and not rotated:
        i = used = 0
        while i < len(t.steps):
            pid = t.steps[i][0]
            if pid not in avail:
                i += 1                          # cannot be ordered and has no fallback: the step is passed over
                continue
            if used < len(in_table):
                used += 1
                i += 1
                continue
            if avail[pid].id == last_id:
                i += 1                          # never the same piece twice in a row
                continue
            break
        if i < len(t.steps):
            pid, after = t.steps[i]
            target = lands if (not sent or after is None) else sent[-1][1] + timedelta(days=after)
            if lands < target:
                return Decision(why="waiting for the next step of the table", opens=(target - lands).days)
            return Decision(design=avail[pid])
    order = [x for x in t.rotate if x in avail]
    if not order:
        return Decision(why="every piece of the table has gone", final=True)
    if sent:
        target = sent[-1][1] + timedelta(days=t.every)
        if lands < target:
            return Decision(why="waiting: after month 12 one piece about every %d days" % t.every, opens=(target - lands).days)
    pick = order[rotated % len(order)]
    if avail[pick].id == last_id and len(order) > 1:
        pick = order[(rotated + 1) % len(order)]
    return Decision(design=avail[pick])


# ---------------------------------------------------------------------- what went out
def history(stage_id: str, record_uuid: str, designs: list[Design], o: dict, tz: str) -> tuple[list, date | None, datetime | None]:
    """(this campaign's pieces as (printed piece id, in-home day) in order, the last day ANY mail piece went
    to the record from any stage, when this campaign's last piece was released). Only live releases
    write these events (mail.deliver), so a plan never reads as mail that went out."""
    by_no = {d.no: d.id for d in designs}
    lead, delivery = _lead(o)
    zone = ZoneInfo(tz)
    mine, last_day, last_at = [], None, None
    for e in ledger.events(record_uuid, "mail"):
        at = _when(e.get("at"))
        if at is None:
            continue
        d = at.astimezone(zone).date()
        if last_day is None or d > last_day:
            last_day = d
        if e.get("stage") != stage_id:
            continue
        try:
            n = int((e.get("detail") or {}).get("step") or 0)
        except (TypeError, ValueError):
            n = 0
        if n in by_no:
            mine.append((by_no[n], in_home(at, tz, lead, delivery)))
            last_at = at
    return mine, last_day, last_at


# ---------------------------------------------------------------------- one campaign per record
def _stage_anchor(pol, stage_id: str) -> str:
    st = (getattr(pol, "stages", None) or {}).get(stage_id)
    return str(st.get("anchor") or "").strip().lower() if st is not None else ""


def claimed_by(record_uuid: str, pol=None, now: datetime | None = None, live_only: bool = True) -> str:
    """The date campaign that owns this record's mail today, or ''. The monthly mail stage asks this and
    leaves the record alone, so no record is in two mail campaigns at once (SPEC 7). With live_only a
    claim counts only from a stage that is live on this box: a campaign still in plan mode never
    silences the monthly lane."""
    f = ledger.fact(record_uuid, CLAIM_FACT)
    if not isinstance(f, dict) or not f.get("stage"):
        return ""
    tz = getattr(pol, "tz", None) or ledger.BUSINESS_TZ
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()
    until = _day(f.get("until"))
    if until is None or until < today:
        return ""
    stages_ = getattr(pol, "stages", None)
    if stages_ is None:
        return "" if live_only else str(f["stage"])
    st = stages_.get(f["stage"])
    if st is None or st.kind != KIND or st.mode == "off":
        return ""
    if live_only and not (st.mode == "live" and config.live()):
        return ""
    return str(f["stage"])


def handoff_step(record_uuid: str, stage_id: str) -> int:
    """The step the monthly mail stage `stage_id` starts this record at, when an auction countdown
    handed it over after the sale (SPEC 6.3: step 2), else 0."""
    f = ledger.fact(record_uuid, HANDOFF_FACT)
    if isinstance(f, dict) and f.get("hand_to") == stage_id:
        return int(f.get("start_at_step") or 1)
    return 0


def _set_claim(u: str, st, anchor: str, until: date, why: str) -> None:
    cur = ledger.fact(u, CLAIM_FACT)
    new = {"stage": st.id, "anchor": anchor, "until": until.isoformat(), "why": why}
    if not isinstance(cur, dict) or {k: cur.get(k) for k in new} != new:
        ledger.set_fact(u, CLAIM_FACT, new)


def _drop_claim(u: str, st) -> None:
    cur = ledger.fact(u, CLAIM_FACT)
    if isinstance(cur, dict) and cur.get("stage") == st.id:
        ledger.set_fact(u, CLAIM_FACT, {})


def _drop_stale_claims(st, seen: set) -> int:
    """Claims this stage holds on records it no longer reads (taken off the list, a status that ends
    them): the monthly lane gets them back today rather than when the claim would have lapsed."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (CLAIM_FACT,)).fetchall()
    dropped = 0
    for r in rows:
        if r["record_uuid"] in seen:
            continue
        try:
            v = json.loads(r["value"] or "{}")
        except ValueError:
            continue
        if isinstance(v, dict) and v.get("stage") == st.id:
            ledger.set_fact(r["record_uuid"], CLAIM_FACT, {})
            dropped += 1
    return dropped


# ---------------------------------------------------------------------- the probate house
_AUDIT: dict = {}


def audit_rows(o: dict) -> tuple[Path | None, dict]:
    """(the address audit file, uuid -> {verdict, key}). The probate stage finds the file (the box's
    data folder, then output/); the verdict column is read here as well, because the probate stage's
    reader keeps only the flagged rows, and the confirmed ones are what this campaign mails."""
    path = probate.find_audit({"audit_csv": str(o["audit_csv"] or "")})
    if path is None:
        return None, {}
    key = (str(path), Path(path).stat().st_mtime)
    if _AUDIT.get("key") != key:
        rows = {}
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                u = str(r.get("uuid") or "").strip()
                if u:
                    rows[u] = {"verdict": str(r.get("verdict") or "").strip().upper(), "key": probate.classify(r.get("suggested"))}
        _AUDIT.clear()
        _AUDIT.update(key=key, rows=rows)
    return path, _AUDIT["rows"]


def house_check(u: str, marks: set, day: str, path, audit: dict, o: dict) -> tuple[bool, str]:
    """(confirmed as the decedent's house, why), in the probate lane's own split (SPEC 6.4)."""
    bad = sorted(marks & _low(o["not_tags"]))
    if bad:
        return False, "tagged %s" % bad[0]
    good = sorted(marks & _low(o["house_confirmed_tags"]))
    if good:
        return True, "a person confirmed the house (tagged %s)" % good[0]
    row = audit.get(u)
    confirms = _low(o["audit_confirms"])
    if row:
        key, verdict = row.get("key") or "", row.get("verdict") or ""
        if key and key in confirms:
            return True, probate.AUDIT_WHY.get(key, key)
        if not key and verdict == "OK" and "ok" in confirms:
            return True, "the address audit found the decedent on the title"
        if any(e["detail"].get("part") == "audit" and e["detail"].get("action") == "keep" for e in ledger.events(u, probate.KIND)):
            return True, "the probate stage kept it after the address audit"
        if key:
            return False, "the address audit flagged it and the probate stage has not settled it (%s)" % probate.AUDIT_WHY.get(key, key)
        return False, "the address audit could not confirm it (%s)" % VERDICT_WORDS.get(verdict, verdict.lower().replace("_", " ") or "no verdict")
    guard = str(o["guard_since"] or "")
    if day and guard and day >= guard:
        return True, "opened on or after %s, so its house passed the owner-of-record guard at upload" % guard
    if path is None:
        return False, "opened before the owner guard, and there is no address audit on this box"
    return False, "opened before the owner guard, and not in the address audit"


def opened_day(rec: dict, custom: dict | None, field_name: str) -> tuple[str, str]:
    """(the day the estate opened, where it was read) or ('', ''). The record's Probate Open Date first
    (the uploader writes the notice's publication day there), then a custom field of that name, then
    the earliest notice month tag our uploader writes (its first day), then the Date Added field."""
    builtin = field_name.strip().lower().replace(" ", "_")
    d = researchqueue.iso_day(rec.get(builtin))
    if d:
        return d, "the %s" % field_name.strip().lower().replace("_", " ")
    low = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    d = researchqueue.iso_day(low.get(field_name.strip().lower()))
    if d:
        return d, "the %s field" % field_name
    months = sorted(t for t in tag_titles(rec) if probate.MONTH_TAG.match(t))
    if months:
        return months[0] + "-01", "the notice month tag %s" % months[0]
    d = researchqueue.iso_day(low.get("date added"))
    if d:
        return d, "the Date Added field"
    return "", ""


def probate_info(ctx: Ctx, u: str, state: dict, o: dict, recs: dict) -> dict | None:
    """{day, source, marks, at} for a probate record, remembered (knowledge, kept in plan mode too). It is
    read again after a week while reads are left, so a tag a person adds is seen within a week. None
    when it has never been read and the run's reads are used up."""
    f = ledger.fact(u, FILED_FACT)
    at = _when((f or {}).get("at")) if isinstance(f, dict) else None
    fresh = at is not None and (ctx.now - at) < timedelta(days=7)
    if isinstance(f, dict) and (fresh or state["reads"] >= state["max_reads"]):
        return f
    if state["reads"] >= state["max_reads"]:
        return None
    state["reads"] += 1
    rec = ctx.crm.record(u)
    if not rec.get("uuid"):
        return f if isinstance(f, dict) else None
    recs[u] = rec
    field_name = str(o["clock"].get("anchor_field") or "Probate Open Date")
    day, source = opened_day(rec, None, field_name)
    if not day:
        state["reads"] += 1
        day, source = opened_day(rec, researchqueue.custom_of(ctx, u, max_age=timedelta(days=7)), field_name)
    f = {"day": day, "source": source, "marks": sorted(marks_of(rec)), "at": ctx.now.isoformat(timespec="seconds")}
    ledger.set_fact(u, FILED_FACT, f)
    return f


# ---------------------------------------------------------------------- the records
def scope_rows(ctx: Ctx, o: dict) -> tuple[list[dict], bool]:
    """(the stage's records, whether every source was read). Its presets and its lists, carrying every
    scope tag, finished statuses and not_tags left out by the search; deduped, in the order read. A
    partial read must never be taken for the whole: claims on records it missed would be let go."""
    cap = int(o["max_scan"])
    complete = True
    must_not: dict = {"any_property_status": list(GONE_STATUSES)}
    extra: dict = {}
    if o["tags"]:
        ids = [ctx.crm.tag_uuid(t) for t in o["tags"]]
        missing = [t for t, i in zip(o["tags"], ids) if not i]
        if missing:
            ctx.out.problem("The tag %s does not exist in this account, so no record was read" % missing[0])
            return [], False
        extra["all_tags"] = ids
    if o["not_tags"]:
        ids = [i for i in (ctx.crm.tag_uuid(t) for t in o["not_tags"]) if i]
        if ids:
            must_not["any_tags"] = ids
    extra["must_not"] = must_not
    seen, out = set(), []
    before = len(ctx.out.problems)
    for _, row in preset_rows(ctx, ctx.stage.presets, extra, cap):
        if row.get("uuid") and row["uuid"] not in seen:
            seen.add(row["uuid"])
            out.append(row)
    complete = complete and len(ctx.out.problems) == before      # a preset that could not be read said so
    for name in o["lists"]:
        lid = ctx.crm.list_uuid(str(name))
        if not lid:
            ctx.out.problem("There is no %s list in this account, so none of its records were read" % name)
            complete = False
            continue
        must = merge_must({"any_lists": [lid]}, {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in extra.items()})
        rows = ctx.crm.rows(must, cap=cap)
        complete = complete and len(rows) < cap
        for row in rows:
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                out.append(row)
    return out, complete


@dataclass
class Entry:
    uuid: str
    row: dict
    anchor: date
    words: str
    sent: list
    last_day: date | None
    last_at: datetime | None = None
    rerun: bool = False
    held: bool = False


class _DateSettings:
    """The stage as auctiontasks.sale_dates reads it: the sale date's field and freshness come from the
    clock block, so the countdown reads the sale date the way the hot auction lane does."""

    def __init__(self, st, o: dict):
        self._st = st
        c = o["clock"]
        self._over = {"custom_date_fields": [str(c.get("anchor_field") or "Foreclosure Date")],
                      "date_fields": [str(x) for x in _list(c.get("date_fields"))] or ["auction_date", "tax_auction_date"],
                      "trust_builtin_foreclosure_date": False, "refresh_days": c.get("refresh_days", 1)}

    @property
    def id(self) -> str:
        return self._st.id

    def get(self, key, default=None):
        if key in self._over:
            return self._over[key]
        return self._st.get(key, default)


def _ordered(rows: list[dict], sale: bool, o: dict) -> list[dict]:
    """Records never read first (a new record gets its first piece the day it comes in), then what is
    known and fresh (it costs nothing), then what is stale."""
    if not sale:
        known = {r["uuid"] for r in rows if ledger.fact(r["uuid"], FILED_FACT) is not None}
        return [r for r in rows if r["uuid"] not in known] + [r for r in rows if r["uuid"] in known]
    refresh = timedelta(days=float(o["clock"].get("refresh_days", 1)))
    new, fresh, stale = [], [], []
    for r in rows:
        if ledger.fact(r["uuid"], "auction_dates") is None:
            new.append(r)
        elif ledger.fact(r["uuid"], "auction_dates", max_age=refresh) is not None:
            fresh.append(r)
        else:
            stale.append(r)
    return new + fresh + stale


def _note_sale(ctx: Ctx, u: str, day: date, row: dict) -> None:
    """Remember the sale date this record is timed against. A later one is a postponement: the record
    stays in and the pieces left count back from the new date."""
    key = "%s:%s" % (SALE_FACT, ctx.stage.id)
    f = ledger.fact(u, key)
    f = f if isinstance(f, dict) else {}
    if f.get("day") == day.isoformat():
        return
    prev = _day(f.get("day"))
    ledger.set_fact(u, key, {"day": day.isoformat(), "prev": prev.isoformat() if prev else "",
                             "moved_at": ctx.now.isoformat(timespec="seconds")})
    if prev and day > prev:
        ctx.out.tally["the sale date moved later: the pieces left count back from the new date"] += 1
        ctx.out.line("%s: the sale moved from %s to %s; the pieces left count back from the new date" % (addr(row), nice(prev), nice(day)))
    elif prev and day < prev:
        ctx.out.tally["the sale date moved earlier"] += 1
        ctx.out.line("%s: the sale moved up from %s to %s" % (addr(row), nice(prev), nice(day)))


def _rerun_ok(ctx: Ctx, u: str, anchor: date, sent: list, last_at: datetime | None, c: Countdown) -> bool:
    """May the closing pair run once more: the sale moved later after the last piece went out, and the
    pair has not run twice already."""
    if not c.retime or not sent or last_at is None:
        return False
    f = ledger.fact(u, "%s:%s" % (SALE_FACT, ctx.stage.id))
    f = f if isinstance(f, dict) else {}
    prev, moved = _day(f.get("prev")), _when(f.get("moved_at"))
    if not prev or anchor <= prev or moved is None or moved <= last_at:
        return False
    ids = [x for x, _ in sent]
    return not any(ids.count(x) >= 2 for x in c.last)


def _sale_anchor(ctx: Ctx, dctx, u: str, row: dict, state: dict, today: date, c: Countdown) -> tuple[date, str] | None:
    st = ctx.stage
    dates = auctiontasks.sale_dates(dctx, u, state)
    if dates is None:
        ctx.out.tally["sale date not read yet (read cap, next run)"] += 1
        return None
    upcoming, past = auctiontasks._split(dates, today)
    if upcoming:
        d = upcoming[0]
        _note_sale(ctx, u, d, row)
        cur = ledger.fact(u, CLAIM_FACT)
        if isinstance(cur, dict) and cur.get("stage") not in (None, "", st.id) and _stage_anchor(ctx.policy, cur["stage"]) == "filed_date":
            ctx.out.tally["taken from the probate campaign: a sale date comes first"] += 1
        _set_claim(u, st, "sale_date", d + timedelta(days=c.grace), "the countdown to the sale on %s" % d.isoformat())
        return d, "the sale on %s" % nice(d)
    if past:
        last = past[-1]
        if (today - last).days <= c.grace:
            _set_claim(u, st, "sale_date", last + timedelta(days=c.grace), "the sale on %s passed; held for a new date" % last.isoformat())
            ctx.out.tally["the sale date passed: held %d days for a new date" % c.grace] += 1
            return None
        if auctiontasks._redeeming(u, today):
            ctx.out.tally["the sale passed and a redemption window is open: this campaign does not mail redemptions"] += 1
        f = ledger.fact(u, HANDOFF_FACT)
        if not (isinstance(f, dict) and f.get("sale") == last.isoformat()):
            if ctx.live:
                ledger.set_fact(u, HANDOFF_FACT, {"hand_to": c.hand_to, "start_at_step": c.start_at_step, "sale": last.isoformat(),
                                                  "from": st.id, "at": ctx.now.isoformat(timespec="seconds")})
            ctx.out.tally["%s to the monthly campaign: the sale passed with no new date" % ("handed" if ctx.live else "would hand")] += 1
        _drop_claim(u, st)
        return None
    _drop_claim(u, st)
    ctx.out.tally["no sale date on file"] += 1
    return None


def _probate_anchor(ctx: Ctx, u: str, state: dict, o: dict, recs: dict, path, audit: dict, t: Table) -> tuple[date, str] | None:
    st = ctx.stage
    f = probate_info(ctx, u, state, o, recs)
    if f is None:
        ctx.out.tally["open date not read yet (read cap, next run)"] += 1
        return None
    if not f.get("day"):
        ctx.out.tally["no open date on the record"] += 1
        return None
    if str(o["require"] or "") == "decedent_house":
        ok, why = house_check(u, set(f.get("marks") or []), f["day"], path, audit, o)
        if not ok:
            ctx.out.tally["house not confirmed: %s" % why] += 1
            _drop_claim(u, st)
            return None
    other = claimed_by(u, ctx.policy, ctx.now, live_only=False)
    if other and other != st.id and _stage_anchor(ctx.policy, other) == "sale_date":
        ctx.out.tally["an auction countdown is timing it: a sale date comes first"] += 1
        return None
    opened = date.fromisoformat(f["day"])
    _set_claim(u, st, "filed_date", opened + timedelta(days=t.end), "the probate campaign")
    return opened, "opened %s (from %s)" % (nice(opened), f.get("source") or "the record")


def _batch_weekday(o: dict) -> int | None:
    d = str(o["weekly_batch_day"] or "").strip().lower()[:3]
    return DAYS.index(d) if d in DAYS[:5] else None


def decide(e: Entry, lands: date, today: date, o: dict, c: Countdown | None, t: Table | None, avail: dict) -> Decision:
    """The one piece due for a record on `today`, or why none. The spacing counts every mail piece the
    autopilot sent the record from any stage, so two campaigns never land on one mailbox in a week."""
    gap = int(o["min_gap_days"])
    if e.last_day and (today - e.last_day).days < gap:
        return Decision(why="waiting: the last mail piece went out too recently", opens=gap - (today - e.last_day).days)
    dec = countdown_next(e.sent, e.anchor, lands, c, avail, e.rerun) if c is not None else table_next(e.sent, e.anchor, lands, t, avail)
    wd = _batch_weekday(o)
    if dec.design is not None and c is None and wd is not None and today.weekday() != wd:
        return Decision(why="waiting for the weekly batch on %s" % DAYS[wd].title(), opens=(wd - today.weekday()) % 7)
    return dec


def _blocked(ctx: Ctx, designs: list[Design], sender: dict) -> dict:
    """id -> why SiftMail cannot place the piece yet (no design, no return address). An outside vendor
    exports a file, so nothing is blocked there."""
    if not getattr(ctx.mailer, "batch", False):
        return {}
    out = {}
    for d in designs:
        _, why = ctx.mailer.touch(d.raw, sender)
        if why:
            out[d.id] = "no return address" if "return address" in why else "no SiftMail design yet"
    return out


def _outside(ctx: Ctx, rec: dict) -> bool:
    """A record whose lane is worked outside the building (the agency lane): nothing goes out from here."""
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tl = cfg.timeline(lanes.lane_of(rec, cfg)) or {}
    return bool(tl.get("outside"))


def _mix(counts: Counter, by_id: dict) -> str:
    return ", ".join("%s x%d" % (by_id[k].label if k in by_id else k, n) for k, n in sorted(counts.items()))


def week_ahead(entries: list[Entry], ctx: Ctx, o: dict, c: Countdown | None, t: Table | None, avail: dict,
               price_of) -> tuple[Counter, set, float]:
    """What falls due over the next seven days, each piece assumed staged and released the day it is due:
    (pieces by printed id, the records, the dollars). It leaves out the floor and the caps, so it reads
    as what is due, not what will launch."""
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    lead, delivery = _lead(o)
    today = ctx.now.astimezone(zone).date()
    by, recs, usd = Counter(), set(), 0.0
    for e in entries:
        if e.held:
            continue
        sent, last_day = list(e.sent), e.last_day
        for k in range(7):
            d = today + timedelta(days=k)
            if d.weekday() >= 5:
                continue
            run_at = ctx.now + timedelta(days=k)
            dec = decide(replace(e, sent=sent, last_day=last_day), in_home(run_at, tz, lead, delivery), d, o, c, t, avail)
            if dec.design is None:
                continue
            by[dec.design.id] += 1
            recs.add(e.uuid)
            usd += price_of(dec.design)
            sent = sent + [(dec.design.id, in_home(run_at, tz, lead, delivery))]
            last_day = d
    return by, recs, round(usd, 2)


# ---------------------------------------------------------------------- the stage
def run_mailcadence(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    if o["anchor"] not in ANCHORS:
        ctx.out.problem("This stage needs anchor: sale_date or filed_date, so nothing was read")
        ctx.out.head = "Nothing was read: the stage has no anchor."
        return
    designs = designs_of(st)
    if not designs:
        ctx.out.problem("This stage lists no pieces, so nothing was read")
        ctx.out.head = "Nothing was read: the stage lists no pieces."
        return
    if ctx.mailer is None:
        ctx.mailer = mail.vendor_for(st)
    if getattr(ctx.mailer, "refused", ""):
        ctx.out.problem(ctx.mailer.refused)
    tz = ctx.policy.tz
    today = ctx.now.astimezone(ZoneInfo(tz)).date()
    sale = o["anchor"] == "sale_date"
    lead, delivery = _lead(o)
    lands = in_home(ctx.now, tz, lead, delivery)
    send = next_send_date(ctx.now)
    by_id = {d.id: d for d in designs}
    avail = usable(designs, o, ctx.mailer, ctx.out)
    c = countdown_of(o, designs) if sale else None
    t = None if sale else table_of(o)
    sender = mail.sender_block(st)
    blocked = _blocked(ctx, designs, sender)
    staged = ledger.staged_record_uuids(st.id)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    allow = set(_list(st.get("gate_allow")))
    state = {"reads": 0, "max_reads": int(o["max_reads"])}
    recs: dict = {}
    dctx = replace(ctx, stage=_DateSettings(st, o)) if sale else ctx
    path, audit = (None, {}) if sale else audit_rows(o)
    if not sale and str(o["require"] or "") == "decedent_house" and path is None:
        ctx.out.problem("There is no probate address audit on this box, so a probate opened before %s is mailed only once a "
                        "person tags its house as confirmed (house_confirmed_tags)" % o["guard_since"])
    rows, complete = scope_rows(ctx, o)
    ctx.out.found = len(rows)
    entries: list[Entry] = []
    due: list[tuple[Entry, Decision]] = []
    for row in _ordered(rows, sale, o):
        u = row["uuid"]
        if u in staged:
            ctx.out.tally["already staged, waiting on release"] += 1
            continue
        v = gate.check_row(row, "mail", cfg=gate_cfg, allow=allow)
        if not v.ok:
            ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        got = _sale_anchor(ctx, dctx, u, row, state, today, c) if sale else _probate_anchor(ctx, u, state, o, recs, path, audit, t)
        if got is None:
            continue
        sent, last_day, last_at = history(st.id, u, designs, o, tz)
        e = Entry(uuid=u, row=row, anchor=got[0], words=got[1], sent=sent, last_day=last_day, last_at=last_at)
        if sale:
            e.rerun = _rerun_ok(ctx, u, e.anchor, sent, last_at, c)
        entries.append(e)
        dec = decide(e, lands, today, o, c, t, avail)
        if dec.design is None:
            ctx.out.tally[dec.why] += 1
            continue
        due.append((e, dec))
    if complete:
        _drop_stale_claims(st, {r["uuid"] for r in rows})

    # The soonest sale first; on the table, the earliest step first, then the newest estate.
    due.sort(key=lambda x: (x[0].anchor, x[1].design.no) if sale else (len(x[0].sent), -x[0].anchor.toordinal()))
    picked: list[tuple[mail.Piece, Entry, Design]] = []
    usd, refused_from = 0.0, None
    cap_n = int(o["daily_pieces"])
    gap = int(o["min_gap_days"])
    for e, dec in due:
        d = dec.design
        if len(picked) >= cap_n:
            ctx.out.tally["left for tomorrow (daily cap %d)" % cap_n] += 1
            continue
        price = float(ctx.mailer.price(d.kind))
        if refused_from is not None and price >= refused_from:
            ctx.out.tally["left for tomorrow (the day's mail budget is used)"] += 1
            continue
        if ctx.live and d.id in blocked:
            ctx.out.tally["not staged: %s" % blocked[d.id]] += 1
            continue
        rec = recs.get(e.uuid)
        if rec is None:
            if state["reads"] >= state["max_reads"]:
                ctx.out.tally["due, not read this run (read cap %d)" % state["max_reads"]] += 1
                continue
            state["reads"] += 1
            rec = ctx.crm.record(e.uuid)
            recs[e.uuid] = rec
        if not rec.get("uuid"):
            ctx.out.tally["the record could not be read"] += 1
            e.held = True
            continue
        if not ctx.passes(rec, "mail", row=e.row):
            e.held = True
            continue
        if _entity(rec.get("owner") or {}):
            ctx.out.tally["owned by a company or trust: people only"] += 1
            e.held = True
            continue
        marks = marks_of(rec)
        if marks & _low(o["not_tags"]):
            ctx.out.tally["tagged %s" % sorted(marks & _low(o["not_tags"]))[0]] += 1
            e.held = True
            continue
        if not sale and str(o["require"] or "") == "decedent_house":
            ok, why = house_check(e.uuid, marks, e.anchor.isoformat(), path, audit, o)
            if not ok:
                ctx.out.tally["house not confirmed: %s" % why] += 1
                e.held = True
                continue
        if _outside(ctx, rec):
            ctx.out.tally["worked outside the building (the agency lane): nothing goes out from here"] += 1
            e.held = True
            continue
        mailed = _day(rec.get("last_direct_mailed"))
        if mailed and (send - mailed).days < gap:
            ctx.out.tally["mailed too recently (the record's last mailed date)"] += 1
            continue
        piece, why = mail.build_piece(rec, d.kind, "%s %s" % (st.id, d.id), d.no, st)
        if not piece:
            ctx.out.tally["not mailable: %s" % why] += 1
            e.held = True
            continue
        piece.price = price
        ok, why = ctx.budget(usd + price, piece.record_uuid, record_usd=0)
        if not ok:
            ctx.out.tally["left for tomorrow (%s)" % why] += 1
            refused_from = price if refused_from is None else min(refused_from, price)
            continue
        if d.raw.get("copy"):
            text, prob = mail.render_copy(str(d.raw["copy"]), piece, sender)
            if prob:
                ctx.out.problem("%s copy: %s; nothing staged for that piece" % (d.id, prob))
                continue
            piece.merge["text"] = text
        piece.merge.update({"send_date": send.isoformat(), "in_home": lands.isoformat(), "anchor": o["anchor"],
                            "anchor_day": e.anchor.isoformat(), "design": d.id, "stage": st.id})
        if mail.has_twin(d.raw):
            ctx.out.tally["greets the owner by first name" if piece.named else "greets without a name (no clean first name)"] += 1
        usd += price
        picked.append((piece, e, d))

    # The floor: a piece whose group is under min_batch waits, and the run says how many and how long.
    floor = max(1, int(o["min_batch"]))
    if getattr(ctx.mailer, "batch", False) and picked:
        groups = Counter(p.step for p, _, _ in picked)
        small = {n for n, k in groups.items() if k < floor}
        if small:
            longest, held_n = 0, 0
            for p, e, d in picked:
                if p.step not in small:
                    continue
                f = ledger.fact(e.uuid, WAIT_FACT)
                since = _day((f or {}).get("since")) if isinstance(f, dict) else None
                if since is None:
                    since = today
                    ledger.set_fact(e.uuid, WAIT_FACT, {"since": today.isoformat(), "design": d.id})
                longest = max(longest, (today - since).days)
                held_n += 1
            for n in sorted(small):
                d = next(x for x in designs if x.no == n)
                ctx.out.line("%s: %d record(s) due, under the floor of %d for one SiftMail campaign, so they wait" % (d.label, groups[n], floor))
            ctx.out.tally["due, waiting on the SiftMail floor (min_batch %d)" % floor] += held_n
            ctx.out.problem("Pieces are waiting on SiftMail's floor of %d records a campaign (min_batch): %d record(s) are due and "
                            "wait, the longest %d day(s) so far. One piece's records are never split into smaller jobs; whether "
                            "smaller campaigns are allowed is Ty's call (SPEC 10.7)." % (floor, held_n, longest))
            picked = [x for x in picked if x[0].step not in small]
            usd = round(sum(p.price for p, _, _ in picked), 2)
    # A record that clears the floor is no longer waiting: its timer starts again from nothing, in plan
    # mode too, so a later wait is never read as one that began weeks earlier.
    for _, e, _ in picked:
        if ledger.fact(e.uuid, WAIT_FACT):
            ledger.set_fact(e.uuid, WAIT_FACT, {})

    used = {d.id for _, _, d in picked} | {dec.design.id for _, dec in due}
    nodesign = [i for i in sorted(used) if blocked.get(i) == "no SiftMail design yet"]
    if nodesign:
        ctx.out.problem("Some pieces have no SiftMail design yet (%s): design each once in Mail Design, then set its design "
                        "uuid in the playbook (cli.py autopilot mail-designs lists them)" % ", ".join(nodesign))
    if any(v == "no return address" for v in blocked.values()):
        ctx.out.problem("No return address for SiftMail: set AUTOPILOT_MAIL_FROM_NAME, _STREET, _CITY, _STATE, _ZIP and _PHONE "
                        "on the box (or sender in the playbook)")

    def price_of(d: Design) -> float:
        return float(ctx.mailer.price(d.kind))

    wk_by, wk_recs, wk_usd = week_ahead(entries, ctx, o, c, t, avail, price_of)
    week = "Due in the next 7 days: %d piece(s) to %d record(s), about $%.2f%s." % (
        sum(wk_by.values()), len(wk_recs), wk_usd, (" (%s)" % _mix(wk_by, by_id)) if wk_by else "")
    for p, e, d in picked[:6]:
        ctx.out.line("%s: %s, %s, landing about %s" % (p.property_street or addr(e.row), d.label, e.words, nice(lands)))
    if not picked:
        ctx.out.head = "Nothing to stage today. %s" % week
        return
    mix = _mix(Counter(d.id for _, _, d in picked), by_id)
    if not ctx.live:
        ctx.reserve(usd)
        ctx.out.acted = len(picked)
        ctx.out.head = "Would stage %d piece(s) to mail %s, landing about %s, about $%.2f: %s. %s" % (
            len(picked), weekday_nice(send), nice(lands), usd, mix, week)
        return
    items = [p.as_dict() for p, _, _ in picked]
    bid = ledger.batch_create(st.id, "mail", items, usd)
    path_csv = ctx.mailer.export(bid, items, sender) if getattr(ctx.mailer, "export_only", False) else ""
    ctx.out.batch = bid
    ctx.out.acted = len(picked)
    ctx.out.head = "Staged batch %d: %d piece(s) to mail %s, about $%.2f (%s). Release: python src/agents/cli.py autopilot release %d%s. %s" % (
        bid, len(picked), weekday_nice(send), usd, mix, bid, ("  (CSV for the mail house: %s)" % path_csv) if path_csv else "", week)


# ---------------------------------------------------------------------- release
def at_release(ctx: Ctx, pieces: list, uploaded: bool = False) -> tuple[list, str]:
    """For mail.deliver, when a batch this stage staged is released: (the pieces that may still go, the
    send date). A person may release days after staging, so the send date is set again by SiftMail's
    rule from the release, and every piece is checked against what is true now: an auction piece that
    would land later than the cutoff before the sale, or after a sale that has passed, is dropped
    (never sent late); a probate piece whose house was marked since is dropped. A file already at the
    mail house cannot be pulled back, so there it is only said."""
    planned = [d for d in (_day((p.merge or {}).get("send_date")) for p in pieces) if d]
    send = max(planned + [next_send_date(ctx.now)])
    st = ctx.stage
    if getattr(st, "kind", "") != KIND:
        return list(pieces), send.isoformat()
    o = options(st)
    lead, delivery = _lead(o)
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    lands = max(send + timedelta(days=delivery), today + timedelta(days=lead))
    latest = int(o["clock"].get("latest_in_home_days_before", 2))
    dctx = replace(ctx, stage=_DateSettings(st, o))
    state = {"reads": 0, "max_reads": 2 * len(pieces) + 10}
    keep = []
    for p in pieces:
        m = p.merge or {}
        why = ""
        if m.get("anchor") == "sale_date":
            dates = auctiontasks.sale_dates(dctx, p.record_uuid, state)
            upcoming, _ = auctiontasks._split(dates or {}, today)
            if not upcoming:
                why = "the sale date has passed"
            elif lands > upcoming[0] - timedelta(days=latest):
                why = "it would now land later than %d days before the sale" % latest
        elif m.get("anchor") == "filed_date" and o["not_tags"]:
            hit = sorted(marks_of(ctx.crm.record(p.record_uuid)) & _low(o["not_tags"]))
            if hit:
                why = "the record is now tagged %s" % hit[0]
        if not why:
            keep.append(p)
            continue
        if uploaded:
            keep.append(p)
            ctx.out.problem("%s was in the uploaded file, but %s" % (p.property_street or "a record", why))
        else:
            ctx.out.tally["held back at release: %s" % why] += 1
            ctx.out.line("%s: held back at release, %s" % (p.property_street or "a record", why))
    return keep, send.isoformat()
