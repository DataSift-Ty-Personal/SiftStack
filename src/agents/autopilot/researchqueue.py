"""The ranked research queue, and the mark a queued record carries (build plan 3.1 and 3.2).

WHY A RANK. Research was fed first come: three handoff stages, each sending one or two records a
day from its own preset, in whatever order the search returned them. A foreclosure with its sale
nine days out waited behind an obituary that will read the same next month. The queue reads every
feeding preset, ranks what it finds and hands the top of the list to research:

    1  a sale date ahead, soonest first
    2  probate, by county tax owed
    3  condemned
    4  an obituary with no probate filing after six months
    5  an obituary with a second sign of distress
    6  every number tried, or every channel finished, with no answer
    7  mail that came back
    8  a vacant mailing address
    9  no number after every trace (first to market, then Priority 1)
   10  a trust as owner
   then every other obituary once it is three months old, and anything else a preset sent.

A record a person asked for (they set the Deep Prospecting status by hand) goes first.

WHAT IS NOT QUEUED, each counted with its reason: a lead (the lead manager has it), a company (it
needs the entity path, not a name search), a house that sold, is listed or whose sale date passed,
an obituary inside its first three months, and a dead owner on a Strong or Bulk list with nothing
else going on (parked, unless the investor score, the equity or a second signal ranks it in).

THE HOUSE IS CHECKED TWICE. The first count of the research backlog (2026-11-12) called 267
foreclosures in scope and 240 of them had a sale date already passed, because nothing had opened
the records. The rules that fixed it live in deep_prospect_batch (facts_of, detail_gate, run_gate)
and are imported here, not copied. They run when a record is ranked and again on a fresh read right
before it is handed over. The queue never holds more than a few open research tasks (max_open), so
"handed" and "run" are minutes apart and the second check is the check before the money is spent.

WHY A BUDGET AND NOT A COUNT. A handoff costs money on a live box (measured $1.21 a record on the
low-cost engine, 2026-11-12). The queue prices each handoff at `rate_usd`, stops at the day's
research budget, and says how many are waiting and what the backlog would cost at that rate.
Research has its own daily budget, apart from data spend and mail, so the dollars are counted from
the brain's research spend and the day's handoffs, never through the data cap or the per-record
trace cap (a $1.21 handoff would fail a $1.00 trace cap on every record).

THE MARK (3.2). A queued record gets the tag Research Queued and ONE card in the research column,
and stays in its lane. A status would take the record out of its lane's presets, which is how a hot
auction left the hot list the moment it was sent to research. The Deep Prospecting status is written
only when a dead owner already holds the record out of every cold channel (the gate's own test), and
never on a lead. The card's id is kept in the ledger and a repeat run reuses it. When the research
task finishes, the tag comes off and the card moves on.

This module never marks an owner deceased, never swaps an owner and never changes a lead's status.
Those belong to the research write-back.

BLOUNT COUNTY has no obituary index. Nothing here looks a name up: the rank reads only what is on
the record, its custom fields and the autopilot's own facts, so a Blount record ranks on the same
marks as a Knox one. What Blount lacks is the death screen's index hit, so a Blount death reaches
this queue through the notice words, the Obituary list or a caller's tag, and later than in Knox.
"""
from __future__ import annotations

import importlib
import json
import logging
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
from . import gate, lanes, ledger, researchroutes, windows
from . import stages as stages_mod
from .stages import GONE_STATUSES, Ctx, addr, lane_entry, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

SIG_FACT = "research_signals"     # what the rank reads off a record, remembered between runs
MARK_FACT = "research_mark"       # what the queue wrote on a record it handed over (live runs only)
WAIT_FACT = "research_wait"       # why research may not take a record yet (the handoff stage's own note)
KV_LAST = "autopilot:research_queue:last"
WORKING = {"", "prospecting"}     # the statuses a queue mark may replace with the hold status

# The rank order, as base scores. Each class keeps 99 points for the order inside it.
BASE = {"asked": 1100, "hot_auction": 1000, "probate": 900, "condemned": 800, "obituary_no_probate": 700,
        "obituary_second_signal": 600, "exhausted": 500, "returned_mail": 400, "vacant_mailing": 300,
        "no_number": 200, "trust": 100, "obituary_due": 60, "death_linked": 20, "other": 5}
CLASS_WORDS = {"asked": "asked for by a person", "hot_auction": "sale ahead", "probate": "probate",
               "condemned": "condemned", "obituary_no_probate": "obituary, no probate",
               "obituary_second_signal": "obituary with a second signal", "exhausted": "no answer anywhere",
               "returned_mail": "returned mail", "vacant_mailing": "vacant mailing address", "no_number": "no number",
               "trust": "trust owner", "obituary_due": "obituary at three months", "death_linked": "possible death",
               "other": "other"}
HINTABLE = ("exhausted", "returned_mail", "vacant_mailing", "no_number")
LANE_BONUS = {"hot_auction": 40, "ftm": 30, "obituary": 20, "top1000": 15}
# The backlog runner's reasons, in the words the digest uses.
HELD_WORDS = {"auction date passed": "the sale date passed",
              "no sale date on record": "a foreclosure with no sale date on the record",
              "sold since the notice or death": "sold since the notice or the death",
              "listed or pending on the MLS": "listed or pending on the MLS",
              "dead status": "a finished status"}
# The three checks the plan asks for beside an obituary with no probate (3.7).
CHECK_WORDS = ("foreclosure", "notice of default", "vacant", "tax delinquent", "tax sale", "behind on taxes")

DEFAULTS = {
    "workflow": "deep_prospect", "agent": "research",
    "rate_usd": 1.21,               # what one handoff is priced at (measured 2026-11-12)
    "daily_usd": None,              # the queue's own day budget; never above the brain's research cap
    "max_open": 5,                  # research tasks open at once
    "max_per_run": 5,
    "max_reads": 100,               # records opened in one run to rank them (two requests each)
    "max_scan": 3000,               # rows read from one preset, tag, list or status
    "refresh_days": 7,              # how long a record's rank inputs are trusted; a new source re-reads at once
    "queued_tag": "Research Queued",
    "hold_status": "Deep Prospecting",
    "card_board": "",               # default: the brain's AGENTS_DP_BOARD
    "card_column": None,            # the research column: a uuid, or {title, uuid}
    "card_done_column": None,       # where the card goes when research is done; unset leaves it
    "status_makes_card": True,      # the account's own sequence adds the card when the status is set
    "leads": "skip",                # skip | research (a lead's status is never changed either way)
    "skip_tags": ["Deep Prospected", "Heir Identified", "Heirs Resolved", "Wrong House", "Estate Sold",
                  "Probate Filed"],
    "sale_date_fields": ["Foreclosure Date"],
    "sale_record_fields": ["auction_date", "tax_auction_date"],     # read on courthouse records only
    "courthouse_tags": ["Courthouse Data"],
    "tax_fields": ["Tax delinquency amount"],
    "obituary_date_fields": ["Date of Death"],
    "probate_date_fields": ["Probate Open Date"],
    "condemned_marks": ["Condemned", "Code Violation", "code_violation"],
    "distress_marks": {"foreclosure": "foreclosure", "notice of default": "notice of default",
                       "tax delinquent": "tax delinquent", "tax_delinquent": "tax delinquent",
                       "tax sale": "tax sale", "tax_sale": "tax sale", "liens": "a lien", "vacant": "vacant",
                       "condemned": "condemned", "code violation": "code violation", "bankruptcy": "bankruptcy",
                       "divorce": "divorce", "eviction": "eviction"},
    "return_tags": ["Return Mail", "Returned Mail"],
    "vacant_mail_tags": [],
    "obituary_min_days": 90,        # research opens three months after the obituary (decision D24)
    "no_probate_days": 180,
    "park_lanes": ["agency"],       # the agency track: a dead owner there waits for a second signal
    "park_score": 80,               # an investor score this high ranks a parked record in
    "park_equity_pct": 50,
    "done_calls": None,             # default: the lane's round end, else 5
    "done_mail": None,              # default: the lane's mail pieces, else 12
    "hints": {},                    # preset title -> exhausted | returned_mail | vacant_mailing | no_number
    "statuses": ["Deep Prospecting"],       # a person set this by hand: those records go first
    "scope_tags": [], "scope_lists": [], "scope_statuses": [],
    "redo_after_days": 0,           # 0: a researched record is never researched again by the queue
    "max_attempts": 2, "retry_days": 1,
    "max_finish": 25,
}

TRUST_RX = re.compile(r"\b(trust|trustee|trustees|ttee|revocable|irrevocable)\b", re.I)
COMPANY_RX = re.compile(r"\b(llc|l\.l\.c|inc|corp|corporation|company|lp|llp|ltd|pllc|bank|church|ministries|"
                        r"association|holdings|properties|investments|partners|partnership|ventures|enterprises|"
                        r"foundation|authority)\b|\b(city|county|state) of\b", re.I)
ESTATE_RX = re.compile(r"\b(estate of|heirs of|est of|deceased)\b", re.I)


# ---------------------------------------------------------------------- small helpers
def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _low(items) -> set[str]:
    out = set()
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.add(str(n).strip().lower())
    return out


def iso_day(v) -> str:
    """A date in any of the shapes a record carries it, as YYYY-MM-DD, or ''."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return ""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def money(v) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return 0.0


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _field(custom: dict | None, labels) -> str:
    """The first of these custom fields that holds a value; labels match in any case."""
    low = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    for label in _list(labels):
        v = low.get(str(label).strip().lower())
        if v not in (None, ""):
            return str(v)
    return ""


def options(st) -> dict:
    """The stage's settings over the defaults above."""
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k) if st is not None else None
        o[k] = v if got is None else got
    for k in ("skip_tags", "sale_date_fields", "sale_record_fields", "courthouse_tags", "tax_fields", "obituary_date_fields",
              "probate_date_fields", "condemned_marks", "return_tags", "vacant_mail_tags", "park_lanes", "statuses",
              "scope_tags", "scope_lists", "scope_statuses"):
        o[k] = [str(x) for x in _list(o[k])]
    o["distress_marks"] = {str(k).strip().lower(): str(v) for k, v in dict(o["distress_marks"] or {}).items()}
    o["hints"] = {str(k): str(v) for k, v in dict(o["hints"] or {}).items()}
    o["leads"] = str(o["leads"]).strip().lower()
    return o


def batch_rules():
    """The backlog runner's module, whose rules decide what is still worth researching. Imported
    by name at run time: it is plain Python with no network at import, and a box that cannot load
    it must refuse to hand anything over, not hand over records nobody checked."""
    try:
        return importlib.import_module("deep_prospect_batch")
    except Exception as exc:  # noqa: BLE001
        log.error("deep_prospect_batch did not load: %s", exc)
        return None


# ---------------------------------------------------------------------- what the rank reads
def owner_kind(owner: dict) -> str:
    """person, trust, company or unnamed. A trust is researched (last in the rank); a company needs
    the entity path, because a name search on an LLC finds nobody."""
    ow = owner or {}
    first = " ".join(str(ow.get("first_name") or "").split())
    last = " ".join(str(ow.get("last_name") or "").split())
    company = " ".join(str(ow.get("company") or "").split())
    name = " ".join(x for x in (first, last, company) if x)
    if not name:
        return "unnamed"
    if TRUST_RX.search(name):
        return "trust"
    if COMPANY_RX.search(name) or (company and not (first and last)):
        return "company"
    if not (first and last):
        return "unnamed"
    return "person"


def with_sales(rec: dict, sales) -> dict:
    """The record with every sale date the autopilot knows written the way the backlog runner
    reads one. Its rule takes the date from the note line the uploader used to write. Records
    uploaded since 2026-08-14 carry the date in the Foreclosure Date field, with no such line, so
    the dates are handed to the same rule as note lines and the rule itself is left as it is."""
    out = dict(rec or {})
    notes = str(out.get("notes") or "")
    extra = []
    for d in sorted(set(sales or [])):
        try:
            y, m, dd = (int(x) for x in d.split("-"))
        except ValueError:
            continue
        line = "Auction: %d/%d/%d" % (m, dd, y)
        if line not in notes:
            extra.append(line)
    if extra:
        out["notes"] = (notes + "\n" + "\n".join(extra)).strip()
    return out


def sale_dates(rec: dict, custom: dict | None, known: dict | None, o: dict, dpb=None) -> list[str]:
    """Every sale date on file for the record that can be trusted: the custom field our own uploader
    writes, the note lines it used to write, what the auction stage already read, and, on a
    courthouse record only, the record's own auction fields.

    The built-in date fields are trusted on courthouse records and nowhere else: bulk rows carry
    phantom auction dates from the data vendor, which is why the auction stage reads these fields
    on courthouse records only. Believed, a phantom date ahead would put a bulk record at the top
    of the queue, and a phantom date behind would drop an obituary record as "sale passed"."""
    out = set()
    low = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    for label in o["sale_date_fields"]:
        out.add(iso_day(low.get(label.strip().lower())))
    if _low((rec or {}).get("tags")) & _low(o.get("courthouse_tags") or DEFAULTS["courthouse_tags"]):
        for key in o["sale_record_fields"]:
            out.add(iso_day((rec or {}).get(key)))
    for v in (known or {}).values():
        out.add(iso_day(v))
    if dpb is not None:
        out.update(dpb.facts_of(rec or {}).get("auctions") or [])
    return sorted(out - {""})


def house_check(rec: dict, sales, today: date, dpb, redo: bool = False) -> str:
    """Why this house is no longer worth researching, in the backlog runner's own words, or ''.

    The rule is the runner's (run_gate), called as it is. One case is handled around it. The runner
    works three lists that each carry a notice or a death date, and tests "sold since" against that
    date. The queue also ranks records that carry neither (a Priority 1 record with no number). For
    those the runner falls back to "sold in the last year", which on a record with no notice is
    usually the owner's own purchase: the sold guard measured exactly that mistake on 2026-11-12.
    So where there is no date to be "since", the sale test is left to the gate, which reads the sold
    tag and status the sold guard keeps true, and the rest of the runner's checks still run."""
    view = with_sales(rec, sales)
    held = dpb.run_gate(view, "", today, redo=redo)
    if held == "sold since the notice or death":
        f = dpb.facts_of(view)
        if not dpb.ref_day(dpb.title_of(f), f):
            held = dpb.run_gate(dict(view, last_sold=None), "", today, redo=redo)
    return held


def signals_of(rec: dict, custom: dict | None, o: dict, *, today: date, gate_cfg=None, lanes_cfg=None,
               known_sales: dict | None = None, dpb=None) -> dict:
    """Everything the rank needs from one record, as plain values. Reads the record, its custom
    fields and the autopilot's own facts. It makes no call of any kind."""
    gate_cfg = gate_cfg or gate.GateConfig()
    lanes_cfg = lanes_cfg or lanes.Lanes()
    rec = rec or {}
    ow = rec.get("owner") or {}
    tags = _low(rec.get("tags"))
    lists = _low(rec.get("lists"))
    marks = tags | lists
    wide = marks | _low(rec.get("system_lists"))
    status = lanes.status_of(rec)
    lane = lanes.lane_of(rec, lanes_cfg)
    dead = gate.deceased_signals(rec, gate_cfg, custom)
    name = " ".join(str(ow.get(k) or "") for k in ("first_name", "last_name", "company"))
    if ESTATE_RX.search(name):
        dead = dead + ["owner name"]
    obit_marks = _low(gate_cfg.deceased_lists) | _low((lanes_cfg.markers.get("obituary") or {}).get("tags")) \
        | _low((lanes_cfg.markers.get("obituary") or {}).get("lists"))
    sales = sale_dates(rec, custom, known_sales, o, dpb)
    held = house_check(rec, sales, today, dpb) if dpb is not None else ""
    tax = money(_field(custom, o["tax_fields"])) or money(rec.get("tax_delinquent_value"))
    phones = ow.get("phones") or []
    all_bad = bool(phones) and all(str(p.get("status") or "UNKNOWN").upper() in gate.BAD_PHONE for p in phones)
    tl = lanes_cfg.timeline(lane) or {}
    done_calls = int(o["done_calls"] if o["done_calls"] is not None else tl.get("round_ends_at", 5))
    done_mail = int(o["done_mail"] if o["done_mail"] is not None else tl.get("mail_pieces", 12))
    distress = []
    for mark, word in o["distress_marks"].items():
        if mark in wide and word not in distress:
            distress.append(word)
    if tax > 0 and "tax delinquent" not in distress:
        distress.append("behind on taxes")
    if (rec.get("address") or {}).get("vacant") and "vacant" not in distress:
        distress.append("vacant")
    skip_tag = next((t for t in o["skip_tags"] if t.strip().lower() in tags), "")
    return {
        "status": status, "lane": lane, "lead": status in lanes.LEAD_STATUSES,
        "owner_kind": owner_kind(ow),
        "dead": dead, "dead_hard": any(not d.endswith(" list") for d in dead),
        "probate": bool(marks & _low(gate_cfg.probate_marks)),
        "obituary": bool(marks & obit_marks),
        "obit_date": iso_day(rec.get("last_obituary_date")) or iso_day(_field(custom, o["obituary_date_fields"])),
        "probate_open": iso_day(rec.get("probate_open_date")) or iso_day(_field(custom, o["probate_date_fields"])),
        "sales": sales, "held": held, "tax_owed": round(tax, 2),
        "condemned": bool(wide & _low(o["condemned_marks"])),
        "distress": distress,
        "exhausted": status == "exhausted" or all_bad,
        "channels_done": int(rec.get("predictivecall_attempts") or 0) >= done_calls
        and int(rec.get("directmail_attempts") or 0) >= done_mail,
        "returned_mail": status == "return mail" or bool(tags & _low(o["return_tags"])),
        "vacant_mailing": bool((ow.get("address") or {}).get("vacant")) or bool(tags & _low(o["vacant_mail_tags"])),
        "no_number": not phones and bool(ow.get("skiptraced") or rec.get("skiptraced")),
        "investor_score": _num(rec.get("investor_score")), "equity_pct": _num(rec.get("equity_percent")),
        "skip_tag": skip_tag,
    }


# ---------------------------------------------------------------------- the rank
@dataclass
class Rank:
    score: float
    cls: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.score > 0


def _months(days: int) -> int:
    return max(0, days) // 30


def _and(words: list[str]) -> str:
    words = [w for w in words if w]
    return ", ".join(words[:-1]) + (" and " if len(words) > 1 else "") + words[-1] if words else ""


def rank(sig: dict, today: date, o: dict | None = None) -> Rank:
    """The one place a record's turn is decided: a score (higher goes first) and the reason in
    words a person reading the digest understands. A score of zero means the record is not queued,
    and the reason says why. It reads only the signals: no request, no paid call."""
    o = options(None) if o is None else o
    sig = sig or {}
    if sig.get("skip_tag"):
        return Rank(0, "done", "already settled (tag %s)" % sig["skip_tag"])
    if sig.get("lead") and o["leads"] != "research":
        return Rank(0, "lead", "a lead: left to the lead manager")
    kind = sig.get("owner_kind") or "person"
    if kind == "company":
        return Rank(0, "company", "a company owns it: it needs the entity path, not a name search")
    if kind == "unnamed":
        return Rank(0, "unnamed", "no owner name on the record to research")
    day = today.isoformat()
    sales = sorted(d for d in (sig.get("sales") or []) if d)
    if sales and sales[-1] <= day:
        return Rank(0, "gone", HELD_WORDS["auction date passed"])
    held = sig.get("held") or ""
    if held and held != "auction date passed":
        return Rank(0, "gone", HELD_WORDS.get(held, held))

    lane = sig.get("lane") or lanes.DEFAULT_LANE
    inside = min(59.0, LANE_BONUS.get(lane, 0) + (sig.get("investor_score") or 0) * 0.19)
    via = [str(v) for v in (sig.get("via") or [])]
    hinted = {o["hints"][t] for t in via if o["hints"].get(t) in HINTABLE}
    distress = [str(w) for w in (sig.get("distress") or [])]
    dead = [str(w) for w in (sig.get("dead") or [])]

    if sig.get("asked"):
        return Rank(BASE["asked"] + inside, "asked", "a person set %s" % o["hold_status"])
    ahead = [d for d in sales if d > day]
    if ahead:
        d0 = date.fromisoformat(ahead[0])
        days = (d0 - today).days
        return Rank(BASE["hot_auction"] + max(0, 99 - days), "hot_auction",
                    "sale in %d day%s (%s)" % (days, "" if days == 1 else "s", d0.strftime("%b %d").replace(" 0", " ")))
    if sig.get("probate"):
        tax = float(sig.get("tax_owed") or 0)
        why = "probate, owes ${:,.0f} in county tax".format(tax) if tax > 0 else "probate"
        return Rank(BASE["probate"] + min(99.0, tax / 100.0), "probate", why)
    if sig.get("condemned"):
        others = [w for w in distress if w not in ("condemned", "code violation")]
        return Rank(BASE["condemned"] + min(99.0, 10.0 * len(others) + inside / 2),
                    "condemned", "condemned" + ((" and " + _and(others[:2])) if others else ""))
    obituary, age = bool(sig.get("obituary")), None
    if obituary:
        obit = sig.get("obit_date") or ""
        if not obit:
            return Rank(0, "hold", "an obituary with no date on the record: left until the date is known")
        age = (today - date.fromisoformat(obit)).days
        if age < int(o["obituary_min_days"]):
            return Rank(0, "hold", "obituary %d days ago: research opens at %d days" % (max(0, age), int(o["obituary_min_days"])))
        ago = "obituary %d months ago" % _months(age)
        if age >= int(o["no_probate_days"]):
            checks = [w for w in distress if w in CHECK_WORDS]
            return Rank(BASE["obituary_no_probate"] + min(99.0, 25.0 * len(checks) + min(24, _months(age) - 6)),
                        "obituary_no_probate", ago + ", no probate filed" + (("; also " + _and(checks[:3])) if checks else ""))
        if distress:
            return Rank(BASE["obituary_second_signal"] + min(99.0, 20.0 * len(distress) + inside / 2),
                        "obituary_second_signal", ago + " and " + _and(distress[:2]))
    if sig.get("exhausted") or sig.get("channels_done") or "exhausted" in hinted:
        why = "calls and mail finished with no answer" if (sig.get("channels_done") and not sig.get("exhausted")) \
            else "every number tried, no answer"
        return Rank(BASE["exhausted"] + inside, "exhausted", why)
    if sig.get("returned_mail") or "returned_mail" in hinted:
        return Rank(BASE["returned_mail"] + inside, "returned_mail", "mail came back")
    if sig.get("vacant_mailing") or "vacant_mailing" in hinted:
        return Rank(BASE["vacant_mailing"] + inside, "vacant_mailing", "the mailing address is vacant")
    if sig.get("no_number") or "no_number" in hinted:
        why = {"ftm": "first-to-market record with no number after every trace",
               "hot_auction": "first-to-market record with no number after every trace",
               "top1000": "a Top 1,000 owner with no number after every trace"}.get(lane, "no number after every trace")
        return Rank(BASE["no_number"] + inside, "no_number", why)
    if kind == "trust":
        return Rank(BASE["trust"] + inside, "trust", "owned by a trust")
    if obituary and age is not None:
        return Rank(BASE["obituary_due"] + min(39.0, float(_months(age))), "obituary_due",
                    "obituary %d months ago: due for research" % _months(age))
    if dead:
        if lane in o["park_lanes"]:
            score, equity = sig.get("investor_score"), sig.get("equity_pct")
            where = "the agency track" if lane == lanes.DEFAULT_LANE else "the %s lane" % lane.replace("_", " ")
            if distress:
                by = "and %s" % _and(distress[:2])
            elif score is not None and score >= float(o["park_score"]):
                by = "ranked in by an investor score of %d" % round(score)
            elif equity is not None and equity >= float(o["park_equity_pct"]):
                by = "ranked in by %d%% equity" % round(equity)
            else:
                return Rank(0, "parked", "a dead owner in %s with no second signal: parked" % where)
            return Rank(BASE["death_linked"] + min(39.0, inside), "death_linked", "a dead owner in %s, %s" % (where, by))
        return Rank(BASE["death_linked"] + min(39.0, inside), "death_linked", "the owner may have died (%s)" % dead[0])
    return Rank(BASE["other"] + min(14.0, inside / 4), "other", ("sent by %s" % via[0]) if via else "in the research presets")


# ---------------------------------------------------------------------- research already done or under way
def research_index(workflow: str = "deep_prospect", record_uuid: str = "") -> dict:
    """record uuid -> what the ledger and the brain's own queue know about research on it. One
    read of each table per run, so a backlog of a thousand records costs two queries, not two
    thousand. With a record named, only that record's rows are read."""
    out: dict[str, dict] = {}

    def slot(u: str) -> dict:
        return out.setdefault(u, {"handoffs": 0, "last_handoff": "", "open": False, "done": False, "failed": False,
                                  "task": 0, "finished": "", "tasks": 0})

    for e in ledger.events(record_uuid, action="handoff"):
        if e["detail"].get("to") == workflow and e["record_uuid"]:
            s = slot(e["record_uuid"])
            s["handoffs"] += 1
            s["last_handoff"] = e["at"]
    with store._LOCK, store._db() as c:
        if record_uuid:
            rows = c.execute("SELECT id, record_uuid, status, created, finished FROM tasks WHERE workflow=? "
                             "AND record_uuid=? ORDER BY id", (workflow, record_uuid)).fetchall()
        else:
            rows = c.execute("SELECT id, record_uuid, status, created, finished FROM tasks WHERE workflow=? "
                             "AND record_uuid<>'' ORDER BY id", (workflow,)).fetchall()
    for r in rows:
        s = slot(r["record_uuid"])
        s["tasks"] += 1
        s["task"] = r["id"]
        st = str(r["status"] or "")
        s["open"] = s["open"] or st in ("queued", "running", "waiting")
        s["done"] = s["done"] or st == "done"
        s["failed"] = st == "failed"          # the LAST task decides: a later good run clears an old failure
        s["finished"] = str(r["finished"] or r["created"] or "")
    return out


def research_state(record_uuid: str, workflow: str = "deep_prospect") -> dict:
    """The same answer for one record (other stages ask this: is the research done yet?). It reads
    that record's rows only; a stage that asks about every record in a list reads the index once."""
    empty = {"handoffs": 0, "last_handoff": "", "open": False, "done": False, "failed": False, "task": 0,
             "finished": "", "tasks": 0}
    if not record_uuid:
        return empty
    return research_index(workflow, record_uuid).get(record_uuid) or empty


def last_result(record_uuid: str, workflow: str = "deep_prospect") -> dict | None:
    """What the newest finished research task on this record returned, or None. The worker stores a
    deep prospect's result on its task: who signs, the call order, where the run folder is."""
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT id, finished, result FROM tasks WHERE workflow=? AND record_uuid=? AND status='done' "
                        "ORDER BY id DESC LIMIT 1", (workflow, record_uuid)).fetchone()
    if not row:
        return None
    try:
        res = json.loads(row["result"] or "{}")
    except ValueError:
        res = {}
    if not isinstance(res, dict):
        return None
    return dict(res, _task=row["id"], _finished=str(row["finished"] or ""))


def facts_by_key(key: str) -> dict:
    """record uuid -> what the autopilot remembers under one key, in ONE read. The queue looks at
    every waiting record on every run; asking the facts table once per record would be thousands
    of small reads to rank a backlog that has not changed."""
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (key,)).fetchall():
            try:
                out[r["record_uuid"]] = json.loads(r["value"])
            except ValueError:
                out[r["record_uuid"]] = r["value"]
    return out


def _age_days(stamp: str, now: datetime) -> float:
    try:
        at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 1e9
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() / 86400.0


def eligible(info: dict | None, now: datetime, o: dict) -> tuple[bool, str, bool]:
    """(may be handed over, why not, is a repeat). A record is handed once. A failed run is tried
    again once, a day later, and then left for a person: a record that fails every day would
    otherwise be paid for every day."""
    if not info:
        return True, "", False
    if info["open"]:
        return False, "in research now", False
    tries = max(int(info["handoffs"]), int(info["tasks"]))
    last = max(str(info.get("last_handoff") or ""), str(info.get("finished") or ""))
    if info["failed"]:
        if tries >= int(o["max_attempts"]):
            return False, "research failed %d times: left for a person" % tries, False
        if _age_days(last, now) < float(o["retry_days"]):
            return False, "research failed: tried again after a day", False
        return True, "", True
    if info["done"] or info["handoffs"]:
        if float(o["redo_after_days"]) > 0 and _age_days(last, now) >= float(o["redo_after_days"]):
            return True, "", True
        return False, "already researched", False
    return True, "", False


def open_tasks(workflow: str = "deep_prospect") -> int:
    with store._LOCK, store._db() as c:
        return int(c.execute("SELECT COUNT(*) FROM tasks WHERE workflow=? AND status IN ('queued','running')",
                             (workflow,)).fetchone()[0])


def measured_rate(workflow: str = "deep_prospect", last: int = 30) -> tuple[float, int]:
    """(dollars a record, records) over the newest finished research tasks that spent anything."""
    with store._LOCK, store._db() as c:
        rows = c.execute(
            "SELECT s.task_id, SUM(s.usd) AS usd FROM spend s JOIN tasks t ON t.id = s.task_id "
            "WHERE t.workflow=? AND t.status='done' GROUP BY s.task_id ORDER BY s.task_id DESC LIMIT ?",
            (workflow, int(last))).fetchall()
    vals = [float(r["usd"] or 0) for r in rows if float(r["usd"] or 0) > 0]
    return (round(sum(vals) / len(vals), 2), len(vals)) if vals else (0.0, 0)


def research_budget(ctx: Ctx, o: dict) -> dict:
    """What research may still commit today.

    Used = what research has already spent today (every deep prospect, whoever asked for it) plus
    what today's handoffs have committed and not spent yet. A handoff is money committed before its
    task has spent a cent, so each one from today (from any stage) counts at the rate until its task
    has finished; after that only what it really spent counts, and that is already in the first
    figure. The stage's own budget is never above the box's research cap: tasks handed past that
    cap would run with their paid steps skipped and come back as empty research."""
    rate = max(0.0, float(o["rate_usd"]))
    box_cap = float(config.RESEARCH_DAILY_CAP_USD)
    cap = box_cap if o["daily_usd"] is None else min(float(o["daily_usd"]), box_cap)
    day = ledger.day(ctx.now, ctx.policy.tz)
    # Two days back is every handoff that can carry today's business day, without reading the whole history.
    today = [e for e in ledger.events(action="handoff", since=ctx.now - timedelta(days=2))
             if e["day"] == day and e["detail"].get("to") == o["workflow"]]
    ids = [int(e["detail"].get("task") or 0) for e in today]
    status: dict[int, str] = {}
    if any(ids):
        with store._LOCK, store._db() as c:
            marks = ",".join("?" * len(ids))
            for r in c.execute("SELECT id, status FROM tasks WHERE id IN (%s)" % marks, ids).fetchall():
                status[int(r["id"])] = str(r["status"] or "")
    pending = 0.0
    for tid in ids:
        if status.get(tid) in ("done", "failed"):
            continue
        pending += max(0.0, rate - (store.spent(task_id=tid) if tid in status else 0.0))
    spent = store.spent(day=datetime.now(timezone.utc).isoformat()[:10], research=True)
    used = spent + pending
    left = max(0.0, cap - used)
    n = int((left + 1e-9) // rate) if rate > 0 else 10 ** 6
    return {"cap": cap, "box_cap": box_cap, "rate": rate, "used": round(used, 2), "left": round(left, 2), "n": n,
            "handed_today": len(today)}


# ---------------------------------------------------------------------- reading the candidates
def custom_of(ctx: Ctx, uuid: str, max_age: timedelta | None = None, fresh: bool = False) -> dict:
    """The record's custom fields, from the same remembered copy the gate keeps (so the gate does
    not read them a second time)."""
    if not fresh:
        c = ledger.fact(uuid, "custom_fields", max_age=max_age)
        if c is not None:
            return c
    try:
        c = ctx.crm.custom_fields(uuid)
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read a record's custom fields (%s); its sale date and tax owed were not ranked" % str(exc)[:80])
        return {}
    ledger.set_fact(uuid, "custom_fields", c)
    return c


def _preset_rows(ctx: Ctx, title: str, cap: int) -> list:
    try:
        must = ctx.crm.preset_must(title)
    except KeyError as exc:
        ctx.out.problem("%s (the queue skipped it)" % exc)
        return []
    must, notes = windows.resolve(must, ctx.now)
    for n in notes:
        if "dropped" in n:
            ctx.out.problem("%s: %s" % (title, n))
    return ctx.crm.rows(must, cap=cap)


def candidates(ctx: Ctx, o: dict) -> dict:
    """record uuid -> {"row", "via", "asked"} for everything a feeding source holds: the stage's
    presets (read live by title), any scope tags and lists, and the records a person put in the
    hold status by hand."""
    out: dict[str, dict] = {}
    cap = int(o["max_scan"])

    def add(row: dict, via: str, asked: bool = False) -> None:
        u = row.get("uuid")
        if not u:
            return
        c = out.setdefault(u, {"row": row, "via": [], "asked": False})
        if via not in c["via"]:
            c["via"].append(via)
        c["asked"] = c["asked"] or asked

    for title in ctx.stage.presets:
        for row in _preset_rows(ctx, title, cap):
            add(row, title)
    for tag in o["scope_tags"]:
        tid = ctx.crm.tag_uuid(tag)
        if not tid:
            ctx.out.problem("there is no %r tag in this account, so nothing is read from it" % tag)
            continue
        for row in ctx.crm.rows({"all_tags": [tid], "must_not": {"any_property_status": GONE_STATUSES}}, cap=cap):
            add(row, "tag %s" % tag)
    for name in o["scope_lists"]:
        lid = ctx.crm.list_uuid(name)
        if not lid:
            ctx.out.problem("there is no %r list in this account, so nothing is read from it" % name)
            continue
        for row in ctx.crm.rows({"any_lists": [lid], "must_not": {"any_property_status": GONE_STATUSES}}, cap=cap):
            add(row, "list %s" % name)
    for status in o["scope_statuses"]:
        for row in ctx.crm.rows({"any_property_status": [status]}, cap=cap):
            add(row, "status %s" % status)
    for status in o["statuses"]:
        for row in ctx.crm.rows({"any_property_status": [status]}, cap=cap):
            add(row, "status %s" % status, asked=True)
    if ctx.stage.get("return_signals"):
        # Returned mail is recorded three ways (the Return Mail status, a return tag, the do-not-mail
        # flag). The handoff stage already knows how to read all three, so that reading is used as
        # it is, not written a second time here.
        read = getattr(stages_mod, "_return_rows", None)
        if read is None:
            ctx.out.problem("return_signals is set, but this build has no reader for them; returned mail is read from the presets only")
        else:
            try:
                rows, how = read(ctx, [])
            except Exception as exc:  # noqa: BLE001
                rows, how = [], {}
                ctx.out.problem("could not read the return signals (%s); returned mail is read from the presets only" % str(exc)[:100])
            for label, row in rows:
                add(row, str(label))
                if row.get("uuid") in how:
                    out[row["uuid"]]["returned"] = True
    return out


# ---------------------------------------------------------------------- the mark, the card, the handoff
def _board_id(ctx: Ctx, o: dict, state: dict) -> str:
    if "board_id" not in state:
        try:
            state["board_id"] = researchroutes.board_uuid(ctx.crm, o["card_board"] or config.DP_BOARD)
        except Exception as exc:  # noqa: BLE001
            state["board_id"] = ""
            ctx.out.problem("could not read the boards (%s); a card made by the account's own sequence may not be recognised"
                            % str(exc)[:80])
    return state["board_id"]


def _column_index(ctx: Ctx, state: dict) -> dict:
    col = state["column"][0]
    if "index" not in state:
        try:
            state["index"] = researchroutes.cards_in_column(ctx.crm, col) if col else {}
        except Exception as exc:  # noqa: BLE001
            state["index"] = {}
            ctx.out.problem("could not list the cards in the research column (%s)" % str(exc)[:80])
    return state["index"]


def ensure_card(ctx: Ctx, o: dict, state: dict, rec: dict, status_written: bool) -> str:
    """One card for the record, in words: reused, made by the status, created, or why not. A card
    the ledger, the log or the column already holds is adopted, never doubled."""
    u = rec["uuid"]
    col, col_title, problem = state["column"]
    board = o["card_board"] or config.DP_BOARD
    found = researchroutes.find_card(u, board, _board_id(ctx, o, state) if col else "", None)
    if not found and col:
        found = researchroutes.find_card(u, board, state.get("board_id", ""), _column_index(ctx, state))
    if found:
        if found.get("source") != "ledger":
            researchroutes.remember_card(u, found["card"], found.get("column") or col, ctx.now, found["source"])
        return "reused"
    if status_written and o["status_makes_card"]:
        return "left to the status"          # the account's own sequence adds it; adopted on a later run
    if not col:
        if not state.get("column_said"):
            state["column_said"] = True
            ctx.out.problem("%s. Records are still tagged and handed to research; no card is added." % problem)
        return "no column"
    if state.get("card_stop"):
        return "stopped"
    ctx.change(u, "card", None, {"column": col})
    res = ctx.write("card", researchroutes.create_card, ctx.crm, col, u)
    if ctx.live:
        card = str((res or {}).get("uuid") or "")
        if not card:
            state["card_stop"] = True
            ctx.out.problem("DataSift answered the card for %s with no card id; no more cards were added this run" % addr(rec))
            return "failed"
        researchroutes.remember_card(u, card, col, ctx.now)
        if not state.get("card_checked") and col_title:
            state["card_checked"] = True
            try:
                title = researchroutes.column_title(researchroutes.card_timeline(ctx.crm, col, card), col)
            except Exception:  # noqa: BLE001  (a failed read-back is not a failed card)
                title = ""
            if title and title.strip().lower() != col_title.strip().lower():
                state["card_stop"] = True
                ctx.out.problem("the column the playbook names for research cards is titled %r on the board, not %r; "
                                "no more cards were added this run" % (title, col_title))
    return "created"


def dead_hold(rec: dict, custom: dict | None, gate_cfg) -> list[str]:
    """The gate's own test for a death that holds the record out of the cold lanes. Not a verdict
    on who died: only the reason the record is already out of every lane."""
    return gate.deceased_signals(rec, gate_cfg, custom)


def send(ctx: Ctx, o: dict, state: dict, rec: dict, custom: dict | None, r: Rank, via: list[str]) -> None:
    """Mark one record as queued and hand it to research: the tag, the status when a dead owner
    holds the record out, one card, then the task."""
    st = ctx.stage
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    u = rec["uuid"]
    tag = str(o["queued_tag"] or "")
    tagged = False
    if tag and tag.lower() not in {t.lower() for t in tag_titles(rec)}:
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("research queued", ctx.crm.add_tags, u, [tag], rec)
        tagged = True
    status = lanes.status_of(rec)
    working = WORKING | {str(s).strip().lower() for s in (gate_cfg.working_statuses or [])}
    status_written = False
    hold = str(o["hold_status"] or "")
    if hold and status != hold.lower() and status in working and status not in lanes.LEAD_STATUSES \
            and dead_hold(rec, custom, gate_cfg):
        ctx.change(u, "status", rec.get("status") if not isinstance(rec.get("status"), dict) else status, hold)
        ctx.write("research hold", ctx.crm.set_status, u, hold)
        status_written = True
        ctx.out.tally["status set to %s (a dead owner holds the record out)" % hold] += 1
    card = ensure_card(ctx, o, state, rec, status_written)
    ctx.out.tally["card %s" % card] += 1
    enqueue = ctx.enqueue or store.enqueue
    tid = ctx.write("handoff", enqueue, o["agent"], o["workflow"], u, addr(rec), source="autopilot:%s" % st.id)
    if ctx.live:
        task = tid[0] if tid else 0
        ledger.event(ctx.run_id, st.id, u, "handoff", {"to": o["workflow"], "task": task, "why": r.reason,
                                                      "class": r.cls, "score": round(r.score, 1), "via": via[:3]},
                     now=ctx.now, tz=ctx.policy.tz)
        # What this queue itself put on the record, kept across a repeat run: a retry finds the tag
        # already there, and must still know the tag is the queue's to take off when research ends.
        prev = ledger.fact(u, MARK_FACT)
        prev = prev if isinstance(prev, dict) else {}
        ledger.set_fact(u, MARK_FACT, {"at": ctx.now.isoformat(timespec="seconds"),
                                       "tag": tag if tagged else str(prev.get("tag") or ""),
                                       "status": hold if status_written else str(prev.get("status") or ""), "task": task})


def finish(ctx: Ctx, o: dict, state: dict) -> int:
    """Research is done on a record the queue marked: the queue tag comes off and the card moves to
    the done column. A failed run keeps its mark, so the record still reads as waiting."""
    done_col = ""
    if o["card_done_column"]:
        done_col, _, problem = researchroutes.column_ref(o["card_done_column"])
        if problem and not state.get("done_said"):
            state["done_said"] = True
            ctx.out.problem(problem)
    index = None
    closed = 0
    for u, mark in facts_by_key(MARK_FACT).items():
        if not isinstance(mark, dict) or mark.get("done"):
            continue
        if closed >= int(o["max_finish"]):
            ctx.out.tally["finished research still to close out (next run)"] += 1
            continue
        index = research_index(o["workflow"]) if index is None else index
        info = index.get(u) or {}
        if info.get("open") or not info.get("done") or info.get("failed"):
            continue
        tag = str(mark.get("tag") or "")
        moved = False
        try:
            if tag:
                ctx.change(u, "remove_tags", [tag], None)
                ctx.write("research done", ctx.crm.remove_tags, u, [tag])
            card = ledger.fact(u, researchroutes.CARD_FACT)
            if not (isinstance(card, dict) and card.get("card")):
                found = researchroutes.find_card(u, o["card_board"] or config.DP_BOARD, _board_id(ctx, o, state), None)
                card = researchroutes.remember_card(u, found["card"], found.get("column") or state["column"][0],
                                                    ctx.now, found["source"]) if found else None
            from_col = (card or {}).get("column") or state["column"][0]
            if done_col and card and card.get("card") and from_col and from_col != done_col:
                ctx.change(u, "card_move", from_col, done_col)
                ctx.write("card moved", researchroutes.move_card, ctx.crm, from_col, card["card"], done_col)
                moved = True
                if ctx.live:
                    researchroutes.remember_card(u, card["card"], done_col, ctx.now, "moved")
        except Exception as exc:  # noqa: BLE001  (one record's close-out failing must not stop the queue)
            ctx.out.problem("could not close out a finished research record (%s); it is tried again next run" % str(exc)[:120])
            continue
        closed += 1
        did = (["tag removed"] if tag else []) + (["card moved"] if moved else [])
        ctx.out.tally["research finished: %s" % (", ".join(did) or "nothing left to close")] += 1
        if ctx.live:
            ledger.set_fact(u, MARK_FACT, dict(mark, done=ctx.now.isoformat(timespec="seconds")))
    return closed


def lane_open(ctx: Ctx, rec: dict, sig: dict, r: Rank, today: date) -> tuple[bool, str]:
    """Does the record's lane allow research yet? The lane table decides when a channel opens, never
    a stage, so this only asks. A lane with no written timeline answers yes."""
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    lane = sig.get("lane") or lanes.DEFAULT_LANE
    if not lanes_cfg.timeline(lane):
        return True, ""
    zone = ZoneInfo(ctx.policy.tz)
    entered = lane_entry(ctx, rec, lane).astimezone(zone).date()
    facts = {"possible_death": bool(sig.get("dead")), "no_number": bool(sig.get("no_number")),
             "returned_mail": bool(sig.get("returned_mail")), "foreclosure": r.cls == "hot_auction",
             "research_class": r.cls}
    return lanes.channel_open(lanes_cfg, lane, "research", lanes.business_days(entered, today),
                              int(rec.get("predictivecall_attempts") or 0), facts, today.weekday())


def research_rules(ctx: Ctx, rec: dict, sig: dict, r: Rank, today: date) -> tuple[bool, str]:
    """May research take this record yet? Two rules written elsewhere are asked, in turn: the list
    rule (a lien is dialed and mailed until it is exhausted before a dollar of research is spent on
    it; a possible death is never held by a list), then the lane's own timeline."""
    by_list = getattr(stages_mod, "research_open", None)
    if by_list is not None:
        ok, why = by_list(ctx, rec, death=bool(sig.get("dead")))
        if not ok:
            return False, why
    return lane_open(ctx, rec, sig, r, today)


def _stamp(sig: dict, c: dict, now: datetime, today: date, o: dict) -> dict:
    """Mark freshly read signals with when they were read, which sources held the record then, and
    whether the answer can change (a company owner or a finished record is read again far less often)."""
    sig["at"] = now.isoformat(timespec="seconds")
    sig["seen_via"] = [str(v) for v in c.get("via") or []]
    sig["final"] = rank(sig, today, o).cls in ("done", "company", "unnamed")
    return sig


def _view(sig: dict, c: dict) -> dict:
    """The signals as this run ranks them: who sent the record, whether a person asked for it, and,
    on a repeat run, without the mark the first run left behind (the tag that says it was done)."""
    v = dict(sig, via=c["via"], asked=c["asked"])
    if c.get("returned"):
        v["returned_mail"] = True          # a return the record itself cannot show (the do-not-mail flag)
    if c.get("again"):
        if str(v.get("skip_tag") or "").strip().lower() == "deep prospected":
            v["skip_tag"] = ""
        if v.get("held") == "tagged Deep Prospected":
            v["held"] = ""
    return v


# ---------------------------------------------------------------------- the stage
def run_researchqueue(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    if o["leads"] not in ("skip", "research"):
        ctx.out.problem("leads must be skip or research; %r is read as skip" % o["leads"])
        o["leads"] = "skip"
    dpb = batch_rules()
    if dpb is None:
        ctx.out.problem("the research rules (deep_prospect_batch) did not load on this box, so nothing was handed over: "
                        "a record nobody checked for a sale or a passed auction is not researched")
        ctx.out.head = "The research queue did not run."
        return
    state: dict = {"column": researchroutes.column_ref(o["card_column"], config.DP_COLUMN)}
    closed = finish(ctx, o, state)

    cands = candidates(ctx, o)
    ctx.out.found = len(cands)
    index = research_index(o["workflow"])
    allow = _list(st.get("gate_allow"))
    marks = facts_by_key(MARK_FACT)
    waiting: dict[str, dict] = {}
    for u, c in cands.items():
        row = c["row"]
        if lanes.status_of(row) in lanes.LEAD_STATUSES and o["leads"] != "research":
            ctx.out.tally["not queued: a lead, left to the lead manager"] += 1
            continue
        v = gate.check_row(row, "research", cfg=gate_cfg, allow=allow)
        if not v.ok:
            ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        ok, why, again = eligible(index.get(u), ctx.now, o)
        if not ok:
            ctx.out.tally[why] += 1
            continue
        mark = marks.get(u) if isinstance(marks.get(u), dict) else {}
        # Asked for by a person: the record sits in the hold status and this queue did not put it there.
        c["asked"] = bool(c["asked"]) and not mark.get("status")
        c["again"] = again
        waiting[u] = c

    budget = research_budget(ctx, o)
    open_now = open_tasks(o["workflow"])
    room = max(0, min(budget["n"], int(o["max_open"]) - open_now, int(o["max_per_run"])))

    # Rank inputs are remembered, so a record is opened once and then about once a week. A run that
    # cannot hand anything over opens nothing: reading a thousand records to tell each one
    # "tomorrow" buys nothing.
    refresh = timedelta(days=float(o["refresh_days"]))
    sigs = facts_by_key(SIG_FACT)
    known = {u: (sigs.get(u) if isinstance(sigs.get(u), dict) else None) for u in waiting}
    fresh: dict[str, tuple[dict, dict]] = {}

    def new_source(u: str) -> bool:
        # A record that turns up somewhere new since it was read (a sale date tagged it Auction Soon)
        # has changed in a way that matters: it is opened again now, not when its week is up.
        seen = (known[u] or {}).get("seen_via")
        return seen is not None and bool(set(waiting[u]["via"]) - set(seen))

    def first(c: dict) -> int:
        # When there are more records than one run may open: the ones a person asked for, then the
        # ones a tag sent (a sale date ahead), then lists and statuses, then the big presets.
        if c["asked"]:
            return 0
        if any(str(v).startswith("tag ") for v in c["via"]):
            return 1
        return 2 if any(str(v).startswith(("list ", "status ")) for v in c["via"]) else 3

    if room > 0:
        order = sorted(waiting, key=lambda u: (0 if known[u] is None or new_source(u) else 1, first(waiting[u]),
                                               str((known[u] or {}).get("at") or "")))
        reads = 0
        for u in order:
            sig = known[u]
            if sig is not None and not new_source(u):
                keep = refresh * (10 if sig.get("final") else 1)
                if _age_days(str(sig.get("at") or ""), ctx.now) < keep.total_seconds() / 86400.0:
                    continue
            if reads >= int(o["max_reads"]):
                break
            reads += 1
            rec = ctx.crm.record(u)
            if not rec.get("uuid"):
                continue
            custom = custom_of(ctx, u, fresh=True)
            sig = signals_of(rec, custom, o, today=today, gate_cfg=gate_cfg, lanes_cfg=lanes_cfg,
                             known_sales=ledger.fact(u, "auction_dates"), dpb=dpb)
            _stamp(sig, waiting[u], ctx.now, today, o)
            ledger.set_fact(u, SIG_FACT, sig)         # knowledge about the record, kept in plan mode too
            known[u] = sig
            fresh[u] = (rec, custom)
        if reads:
            ctx.out.tally["records opened to rank them"] = reads

    ranked: list[tuple[Rank, str, datetime]] = []
    unranked = 0
    seen_at = facts_by_key("first_seen:%s" % st.id)
    for u, c in waiting.items():
        sig = known[u]
        if sig is None:
            unranked += 1
            continue
        r = rank(_view(sig, c), today, o)
        if not r.ok:
            ctx.out.tally["not queued: %s" % r.reason] += 1
            continue
        try:
            since = datetime.fromisoformat(seen_at[u])
        except (KeyError, TypeError, ValueError):
            since = ledger.first_seen(u, st.id, ctx.now)       # the first time it is seen waiting: the wait starts now
        ranked.append((r, u, since))
    if unranked:
        ctx.out.tally["not ranked yet (opened on a later run)"] = unranked
    ranked.sort(key=lambda x: (-x[0].score, x[2]))

    handed: list[tuple[Rank, str]] = []
    dropped: set[str] = set()         # ranked, then found on the fresh read to be no longer worth it
    for r, u, _seen in ranked:
        if len(handed) >= room:
            break
        c = waiting[u]
        if not c["asked"] and ledger.fact(u, WAIT_FACT, max_age=timedelta(hours=20)) is not None:
            ctx.out.tally["research waits (checked earlier today)"] += 1
            continue
        try:
            rec, custom = fresh[u] if u in fresh else (ctx.crm.record(u), None)
            if not rec.get("uuid"):
                dropped.add(u)
                continue
            if custom is None:
                custom = custom_of(ctx, u, fresh=True)
            # The second check, on a fresh read, right before the money is spent.
            sig = signals_of(rec, custom, o, today=today, gate_cfg=gate_cfg, lanes_cfg=lanes_cfg,
                             known_sales=ledger.fact(u, "auction_dates"), dpb=dpb)
            _stamp(sig, c, ctx.now, today, o)
            ledger.set_fact(u, SIG_FACT, sig)
            r2 = rank(_view(sig, c), today, o)
            if not r2.ok:
                ctx.out.tally["not queued: %s" % r2.reason] += 1
                dropped.add(u)
                continue
            if not ctx.passes(rec, "research", row=c["row"]):
                dropped.add(u)
                continue
            ok, why = (True, "") if r2.cls == "asked" else research_rules(ctx, rec, sig, r2, today)
            if not ok:
                # Still waiting: its list or its lane opens research later. The answer is kept for
                # the day, so the same record is not opened again on every run to hear it again.
                ledger.set_fact(u, WAIT_FACT, why)
                ctx.out.tally["research waits: %s" % why] += 1
                continue
            send(ctx, o, state, rec, custom, r2, c["via"])
        except Exception as exc:  # noqa: BLE001  (say which record and stop: the next one would fail the same way)
            ctx.out.problem("could not hand %s to research (%s); stopped, the rest wait for the next run" % (
                addr(c["row"]), str(exc)[:160]))
            break
        handed.append((r2, u))
        ctx.out.line("%s: %s" % (addr(rec), r2.reason))

    rate = budget["rate"]
    n = len(handed)
    handed_ids = {h[1] for h in handed}
    oldest = 0.0
    still = 0
    for r, u, seen in ranked:
        if u in handed_ids or u in dropped:
            continue
        still += 1
        ctx.out.tally["waiting: %s" % CLASS_WORDS.get(r.cls, r.cls)] += 1
        oldest = max(oldest, (ctx.now - seen).total_seconds() / 86400.0)
    waiting_n = still + unranked
    ctx.out.acted = n + closed
    if not ctx.live:
        ctx.out.would_usd += n * rate
    mix = Counter(CLASS_WORDS.get(r.cls, r.cls) for r, _ in handed)
    parts = []
    if n:
        parts.append("%s %d record(s) to research, about $%.2f: %s." % (
            "Handed" if ctx.live else "Would hand", n, n * rate, ", ".join("%d %s" % (k, w) for w, k in mix.most_common())))
    elif waiting_n:
        if budget["n"] <= 0:
            parts.append("The day's research budget is used ($%.2f of $%.2f), so nothing was handed over." % (
                budget["used"], budget["cap"]))
        elif int(o["max_open"]) - open_now <= 0:
            parts.append("%d research task(s) are still open, so nothing new was handed over." % open_now)
        else:
            parts.append("Nothing could be handed over this run.")
    if waiting_n:
        days = math.ceil(waiting_n * rate / budget["cap"]) if budget["cap"] > 0 else 0
        parts.append("%d waiting, about $%.2f at $%.2f a record%s%s." % (
            waiting_n, waiting_n * rate, rate,
            (", %d day(s) at $%.0f a day" % (max(1, days), budget["cap"])) if budget["cap"] > 0 else "",
            ("; the oldest has waited %d day(s)" % int(oldest)) if oldest >= 1 else ""))
    if not parts:
        parts.append("Nothing is waiting for research.")
    ctx.out.head = " ".join(parts)
    if o["daily_usd"] is not None and float(o["daily_usd"]) > budget["box_cap"]:
        ctx.out.line("The research budget on this box is $%.2f a day (AGENTS_RESEARCH_DAILY_CAP_USD), under the playbook's $%.2f."
                     % (budget["box_cap"], float(o["daily_usd"])))
    got, over = measured_rate(o["workflow"])
    if over >= 5:
        ctx.out.line("Measured cost: $%.2f a record over the last %d finished." % (got, over))
    store.kv_set(KV_LAST, json.dumps({"at": ctx.now.isoformat(timespec="seconds"), "live": ctx.live, "handed": n,
                                      "waiting": waiting_n, "waiting_usd": round(waiting_n * rate, 2),
                                      "oldest_days": int(oldest), "rate": rate, "used": budget["used"],
                                      "cap": budget["cap"]}))


def digest_line() -> str:
    """The queue in one line for the morning readout: how many wait, what that costs, how long the
    oldest has waited. Empty until the queue has run once."""
    try:
        last = json.loads(store.kv_get(KV_LAST) or "{}")
    except ValueError:
        last = {}
    if not last:
        return ""
    return "Research queue: %d waiting, about $%.2f, the oldest %d day(s); %d handed on the last run." % (
        int(last.get("waiting") or 0), float(last.get("waiting_usd") or 0), int(last.get("oldest_days") or 0),
        int(last.get("handed") or 0))
