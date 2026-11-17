"""County tax status on the records the team works, re-read until paid (research corpus, missed
workflows item 2; the course's C2.23, C1.14, L1.66, C12.22, C1.22, L14.130, C11.41).

The county figure was written once, at upload, and on courthouse notices only (plan 1.11). After
that nothing read the county again: a probate ranked by the tax it owes (C12.22) was ranked on what
an upload or the data vendor wrote months before, a lead manager made an offer without knowing the
county had been paid off or was owed another year, and a tax sale record that had paid still sat in
the hot lane. The course is plain about it: the county will not hand out a delinquent list, so each
property is looked up (L1.66); a lookup by address "can show a record is all paid" (C2.23); back
taxes stay Unknown until checked (L14.130).

Each run (weekly):
  1. the records in scope are listed by search rows, never read in full: the lead statuses and the
     hot auction tags first (an offer or a sale is close), then the lists (Tax Sale, Probate, Tax
     Delinquent, Obituary) and the Top 1000 tag;
  2. the ones that are due are read in full, a capped number a run, never checked first and then the
     oldest check. A reading that shows tax owed is due again in 28 days, a clean one in 91, a lead or
     a hot auction every week; and a reading taken before March 1 is due once March 1 passes, because
     that is the day the prior year's bill turns delinquent in Tennessee;
  3. each is looked up on the county Trustee's own bills, by street (Knox and Blount run the same
     vendor; other counties are listed for a person);
  4. live: the delinquent amount goes into Tax delinquency amount, the earliest delinquent year into
     the record's own Tax Delinquent Year, and a dated note says what the county shows. A record that
     showed tax owed and is now current gets 0, the tag Taxes Paid and a note saying so: that is news
     a caller can use. A record tagged Taxes Paid that owes again loses the tag. Every change keeps
     the value it replaced, so a run can be put back.

Rules, each one a way this has gone wrong before:
  * `amount_due` is NOT a delinquency. A parcel row's amount due is every unpaid bill, the current
    year's included, which is not late until the end of February; read as a delinquency it marked
    1,274 of 1,451 lien parcels tax delinquent one November. Only bills that are delinquent AND
    unpaid count, read from the account's own bills; the parcel row's amount is never read.
  * Total Delinquency is liens PLUS taxes (13,766.72 = 12,908.72 lien + 858.00 tax on one record).
    It is never written here and never read as the tax figure: the lien records own it, and the tax
    figure written into it would erase the lien half.
  * The county search is literal about the suffix and fuzzy in what it ranks first ("100 Example
    Street" finds nothing, "100 EXAMPLE ST" finds the parcel, and the first result is often another
    house), so the street is asked the way the county writes it and a parcel counts only when its
    own address is the same house. 3 of 51 records once carried another house's bill.
  * Two accounts at one house that both answer with bills is no answer. A business account can sit
    at the same street as the house (its bill route answers 404 and is passed over), and a condo or a
    street name used twice in one county puts two houses behind one search; which bill belongs to
    this record cannot be told, so it is listed for a person instead of guessed.
  * An account whose bills cannot be read gets nothing written, only a count: a guess reads exactly
    like a fact, and a blank means "not checked", never "owes nothing".
  * COUNTY tax only. The City of Knoxville, Maryville and Alcoa bill separately; every note says so.
  * People only (the owner's rule, 2026-11-14): a company or trust owner is not read. A person's
    estate is the person (estates_are_people): probates are this stage's main reason to exist, and
    the shared test reads "Estate of Pat Example" written in the company field as a company. An
    owner missing a first or a last name is a person whose record is incomplete, not a company.
  * Plan and live keep separate memories. A plan run reads the county and says what it would write;
    if it shared the live memory, the first live run would skip every record a plan run had looked
    at and write nothing for weeks.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import budget, lanes, ledger
from .auctiontasks import COUNTY_ROLLS, ROLL_AGENT, ROLL_TIMEOUT_S, same_house, street_queries
from .stages import GONE_STATUSES, Ctx, status_of, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

MEMORY = "taxwatch"                 # per record, live runs only: the last county reading a live run acted on
MEMORY_PLAN = "taxwatch_plan"       # per record, plan runs only
TOTAL_LABEL = "Total Delinquency"   # liens plus taxes: never written here, never read as the tax figure
COUNTY_LABEL = "County"             # the upload's own county field, read only when the address names none
YEAR_FIELDS = ("tax_delinquent_year",)   # record fields the year may go to (the documented property PATCH)
PERSON_FILE = "for_a_person.csv"
PERSON_COLS = ["record", "street", "city", "county", "why"]
CENT = 0.005
COUNTY_PAUSE_S = 0.5                # between county requests: a public site asked a burst by one client blocks it
# A weekly slot can fire a few seconds earlier than last week's, which would read a 7-day recheck as 6 days
# and 23 hours and skip the lead for a week. Half a day of slack keeps the cadence the schedule sets.
GRACE = timedelta(hours=12)
SYSTEMIC_MIN = 10                   # county answers in a run before "most of them failed" is said out loud
TN = {"tn", "tennessee"}
GONE = {s.lower() for s in GONE_STATUSES}
KNOX_ACCOUNT = re.compile(r"^(\d{3}[A-Z]{0,2})(\d{2,5})$")
# A person's estate, written in the owner's name or company field. "Real Estate" is a business word.
ESTATE_RX = re.compile(r"\b(?:estate\s+of|est\s+of|heirs\s+of|unknown\s+heirs)\b|(?<!real )\bestate\b", re.I)

DEFAULTS = {
    "statuses": list(lanes.LEAD_STATUS_VALUES),        # leads: before the offer (C1.22)
    "urgent_tags": ["Auction Critical", "Auction Soon"],
    "scope_lists": ["Tax Sale", "Probate", "Tax Delinquent", "Obituary"],   # a paid tax sale is wasted dials now
    "scope_tags": ["Top 1000"],
    "hold_tags": ["Do Not Market", "recently sold", "Estate Sold", "Wrong House"],
    "amount_field": "Tax delinquency amount",
    "year_field": "tax_delinquent_year",
    "paid_tag": "Taxes Paid",
    "recheck_days": 91,             # a clean reading, no parcel, a held record, another county
    "recheck_owing_days": 28,       # a reading that owes, or bills that could not be read
    "recheck_urgent_days": 7,       # a lead or a hot auction
    "delinquent_from": "03-01",     # the day the prior year's bill turns delinquent (Tennessee)
    "max_records": 300,             # records read in full a run
    "max_scan": 5000,               # search rows listed per list, tag or status set
    "max_minutes": 40,              # the run stops reading after this long: one worker runs every stage
    "county_pause_s": COUNTY_PAUSE_S,
    "note": True,
    "alert_leads": True,
    "zero_on_blank": False,         # a clean reading on a blank field writes 0 instead of leaving it blank
    "individuals_only": True,
    "estates_are_people": True,
}
LIST_KEYS = ("statuses", "urgent_tags", "scope_lists", "scope_tags", "hold_tags")
INT_KEYS = ("recheck_days", "recheck_owing_days", "recheck_urgent_days", "max_records", "max_scan")
BOOL_KEYS = ("note", "alert_leads", "zero_on_blank", "individuals_only", "estates_are_people")

# The run's own clock for its time limit. Tests swap it; ctx.now is the business clock and does not move.
_clock = time.monotonic


class NoRoll(LookupError):
    """No county tax roll is read here for the record's county."""


class Unreadable(ValueError):
    """The house is on the roll, but its bills could not be read."""


class Ambiguous(Unreadable):
    """Two or more accounts at one house answer with bills: which is this record's cannot be told."""


# ---------------------------------------------------------------------- settings
def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def settings(st) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    for k in LIST_KEYS:
        out[k] = [str(x).strip() for x in _list(out[k]) if str(x).strip()]
    for k in ("amount_field", "year_field", "paid_tag", "delinquent_from"):
        out[k] = str(out[k] or "").strip()
    return out


def _month_day(v) -> tuple | None:
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", str(v or "").strip())
    if not m:
        return None
    try:
        datetime(2000, int(m.group(1)), int(m.group(2)))      # a leap year, so 02-29 is allowed
    except ValueError:
        return None
    return int(m.group(1)), int(m.group(2))


def block_problems(st) -> list[str]:
    """What is wrong with a tax watch block, in the playbook checker's words, so a typo is refused when
    the playbook loads instead of read as a default on the first live night."""
    out = []
    for k in LIST_KEYS:
        v = st.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v)):
            out.append("stage %s: %s lists titles, as the account spells them" % (st.id, k))
    af = st.get("amount_field")
    if af is not None:
        if not isinstance(af, str) or not af.strip():
            out.append("stage %s: amount_field names the custom field the tax figure goes to" % st.id)
        elif af.strip().lower() == TOTAL_LABEL.lower():
            out.append("stage %s: amount_field may not be Total Delinquency (it is liens plus taxes, kept by the lien records)"
                       % st.id)
    yf = st.get("year_field")
    if yf is not None and (not isinstance(yf, str) or (yf.strip() and yf.strip() not in YEAR_FIELDS)):
        out.append("stage %s: year_field is %s, or empty to leave the year alone" % (st.id, " or ".join(YEAR_FIELDS)))
    pt = st.get("paid_tag")
    if pt is not None and (not isinstance(pt, str) or not pt.strip()):
        out.append("stage %s: paid_tag names the tag a record that has paid gets" % st.id)
    for k in INT_KEYS:
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            out.append("stage %s: %s must be a whole number, 1 or more" % (st.id, k))
    for k, low in (("max_minutes", 1), ("county_pause_s", 0)):
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < low):
            out.append("stage %s: %s must be a number, %d or more" % (st.id, k, low))
    df = st.get("delinquent_from")
    if df is not None and _month_day(df) is None:
        out.append("stage %s: delinquent_from is a month and a day, MM-DD (03-01 in Tennessee)" % st.id)
    for k in BOOL_KEYS:
        v = st.get(k)
        if v is not None and not isinstance(v, bool):
            out.append("stage %s: %s is true or false" % (st.id, k))
    return out


# ---------------------------------------------------------------------- small readers
def _num(v) -> float | None:
    """A money value as the account or the county gives it ("858.00", 858.0, "$1,234.56"), or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch in ".-")
    try:
        return float(s) if s not in ("", ".", "-") else None
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _figure(n: float) -> str:
    """A figure the way a money field takes it: "1234.56", "858", "0"."""
    s = "%.2f" % n
    return s.rstrip("0").rstrip(".") if "." in s else s


def _money(n: float) -> str:
    """$1,234.56, or $858 for a whole figure."""
    return "$" + (format(n, ",.0f") if abs(n - round(n)) < CENT else format(n, ",.2f"))


def _and(words: list) -> str:
    """"2024", "2023 and 2024", "2022, 2023 and 2024"."""
    words = [str(w) for w in words if str(w)]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _day(now: datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).strftime("%b %d, %Y").replace(" 0", " ")


def _parse(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _short(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__)[:80]


def county_key(county: str) -> str:
    return str(county or "").strip().lower().replace(" county", "").strip()


def county_name(county: str) -> str:
    k = county_key(county)
    return ("%s County" % k.title()) if k else "the county"


def _label(custom: dict | None, label: str):
    """The value of a custom field, the label matched in any case, or None."""
    want = str(label or "").strip().lower()
    for k, v in (custom or {}).items():
        if str(k).strip().lower() == want:
            return v
    return None


# ---------------------------------------------------------------------- the county roll
def _fetch(url: str, pause: float = COUNTY_PAUSE_S):
    """One GET to a county roll: its JSON, or None for a 404. OSError when the site does not answer or
    answers with something that is not JSON (a maintenance page), which stops the county for the run."""
    if pause:
        time.sleep(pause)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ROLL_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=ROLL_TIMEOUT_S) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise OSError("the county answered HTTP %s" % exc.code)
    try:
        data = json.loads(body or "{}")
    except ValueError:
        raise OSError("the county answered with something that is not JSON")
    return data if isinstance(data, dict) else {}


def _account(raw, base: str) -> str:
    """The account the bill route wants. A Knox account is the parcel id with a dash ("048DE099" is asked
    as "048DE-099", the way tax_enricher asks it); a Blount account is its own short number."""
    a = str(raw if raw is not None else "").strip()
    if a and base == COUNTY_ROLLS.get("knox") and "-" not in a:
        m = KNOX_ACCOUNT.match(a.upper())
        if m:
            return "%s-%s" % (m.group(1), m.group(2))
    return a


def _bills(due) -> list | None:
    """The bills list out of a bill route answer, or None when the answer carries none."""
    d = due.get("due") if isinstance(due, dict) else None
    ppt = d.get("PPT") if isinstance(d, dict) else None
    bills = ppt.get("bills") if isinstance(ppt, dict) else None
    return bills if isinstance(bills, list) else None


def delinquent(bills: list) -> dict:
    """{"amount", "years"}: only bills that are delinquent AND unpaid with something still due. The
    current year's bill is unpaid for most owners from October to February and is not late, so it never
    counts; a bill that does not say it is unpaid is taken as paid."""
    amount, years = 0.0, set()
    for b in bills or []:
        if not isinstance(b, dict) or not b.get("delinquent") or b.get("paid", True):
            continue
        due = _num(b.get("due")) or 0.0
        if due <= 0:
            continue
        amount += due
        y = str(b.get("taxYear") or "").strip()
        if y:
            years.add(y)
    return {"amount": round(amount, 2), "years": sorted(years)}


def county_tax(street: str, county: str, fetch=None, pause: float = COUNTY_PAUSE_S) -> dict | None:
    """Delinquent county tax at exactly this street: {"amount", "years", "account", "address"}, or None
    when the roll has no parcel at that house. NoRoll for a county with no roll read here, Unreadable when
    the house is found but no account there answers with bills, Ambiguous when two or more do, OSError
    when the roll does not answer (worth trying again later). The parcel row's amount_due is never read.

    The same contract as the encumbrance stage's client, so one ctx.sources["county_tax"] serves both."""
    fetch = fetch or partial(_fetch, pause=pause)
    base = COUNTY_ROLLS.get(county_key(county))
    if not base:
        raise NoRoll("no county tax roll is read for %s" % county_name(county))
    line = (street or "").split(",")[0].strip()
    if not line[:1].isdigit():
        return None                      # no house number, so no result could be checked against it
    found_house, tried = False, {}
    for q in street_queries(line):
        data = fetch("%s/parcels/%s?detail_level=public&start=0&length=10" % (base, urllib.parse.quote(q)))
        parcels = (data or {}).get("parcels") if isinstance(data, dict) else None
        hits = [p for p in (parcels if isinstance(parcels, list) else [])
                if isinstance(p, dict) and same_house(line, str(p.get("parcel_address") or ""))]
        if not hits:
            continue
        found_house = True
        answered: dict = {}
        for p in hits:
            account = _account(p.get("account_number"), base)
            if not account or account in answered:
                continue
            if account not in tried:
                tried[account] = _bills(fetch("%s/due/PPT/%s?detail_level=public" % (base, urllib.parse.quote(account))))
            if tried[account] is not None:
                answered[account] = (tried[account], str(p.get("parcel_address") or ""))
        if len(answered) > 1:
            raise Ambiguous("%d county accounts at that house answer with bills" % len(answered))
        if answered:
            account, (bills, roll_address) = next(iter(answered.items()))
            out = delinquent(bills)
            out.update(account=account, address=roll_address)
            return out
    if found_house:
        raise Unreadable("the county has the house, but no account there answered with bills")
    return None


def patch_record(crm, uuid: str, fields: dict):
    """PATCH /api/internal/property/{uuid}/, the documented partialUpdateProperty route, whose body is the
    published Property schema (tax_delinquent_year is a nullable whole number there). The CRM client has
    no method for it, so this stage carries its own, through crm.call so it is paced and budgeted."""
    return crm.call("/api/internal/property/%s/" % uuid, "PATCH", dict(fields))


# ---------------------------------------------------------------------- who is in scope, and when
def person_hold(owner: dict, st: dict) -> str:
    """Why an owner is not a person for this stage, or "". The shared test is
    deep_prospect_batch.is_entity, with two cases it would hold kept: a person's estate (see the module
    notes) and an owner missing a first or a last name with no company at all (a person whose record is
    incomplete; the nightly completion stage fills the name in)."""
    import deep_prospect_batch as dpb
    o = owner or {}
    if not dpb.is_entity(o):
        return ""
    name = " ".join(str(o.get(k) or "") for k in ("first_name", "last_name", "company")).strip()
    company_like = bool(str(o.get("company") or "").strip()) or bool(dpb.ENTITY_RX.search(name))
    if not company_like:
        return ""
    if st["estates_are_people"] and ESTATE_RX.search(name) and not dpb.ENTITY_RX.search(name):
        return ""
    return "owned by a company or trust"


def row_skip(row: dict, st: dict) -> str:
    """Why a search row needs no read at all, or "". Free, so it is decided again every run."""
    a = row.get("address")
    if isinstance(a, dict) and "street" in a and not str(a.get("street") or "").strip():
        return "no street on the record"
    o = row.get("owner") or {}
    # A row that carries no owner names at all says nothing about the owner: decided on the full record.
    if st["individuals_only"] and any(k in o for k in ("first_name", "last_name", "company")):
        return person_hold(o, st)
    return ""


def record_hold(rec: dict, st: dict) -> str:
    """Why a full record is not looked up, or ""."""
    if status_of(rec) in GONE:
        return "a finished status"
    have = {t.strip().lower() for t in tag_titles(rec)}
    for t in st["hold_tags"]:
        if t.strip().lower() in have:
            return "tagged %s" % t
    if st["individuals_only"]:
        return person_hold(rec.get("owner") or {}, st)
    return ""


def _turn(now: datetime, st: dict, tz: str) -> datetime | None:
    md = _month_day(st["delinquent_from"])
    if not md:
        return None
    local = now.astimezone(ZoneInfo(tz))
    return datetime(local.year, md[0], md[1], tzinfo=ZoneInfo(tz))


def interval_days(mem: dict, urgent: bool, st: dict) -> int:
    days = int(st["recheck_owing_days"]) if mem.get("status") in ("owes", "unreadable") else int(st["recheck_days"])
    return min(days, int(st["recheck_urgent_days"])) if urgent else days


def is_due(mem, urgent: bool, st: dict, now: datetime, tz: str) -> bool:
    """Never checked, past its interval, or checked before the day the prior year's bill turned delinquent."""
    if not isinstance(mem, dict) or not mem.get("at"):
        return True
    at = _parse(mem["at"])
    if at is None:
        return True
    if now - at >= timedelta(days=interval_days(mem, urgent, st)) - GRACE:
        return True
    turn = _turn(now, st, tz)
    return bool(turn and at < turn <= now)


def order_key(mem, c: dict) -> tuple:
    """A lead or a hot auction first, then never checked, then the oldest check, then the source's order."""
    at = (mem or {}).get("at") if isinstance(mem, dict) else ""
    return (0 if c["urgent"] else 1, 0 if not at else 1, at or "", c["rank"])


def scope(ctx: Ctx, st: dict) -> dict:
    """record uuid -> {"row", "rank", "urgent", "via"} for everything the stage's statuses, tags and lists
    hold, from search rows only. A record on several keeps its most urgent, first-ranked source."""
    out: dict[str, dict] = {}
    cap = int(st["max_scan"])
    sources = []
    if st["statuses"]:
        sources.append(("status", "the lead statuses", True))
    sources += [("tag", t, True) for t in st["urgent_tags"]]
    sources += [("list", n, False) for n in st["scope_lists"]]
    sources += [("tag", t, False) for t in st["scope_tags"]]
    for rank, (kind, title, urgent) in enumerate(sources):
        if kind == "status":
            must = {"any_property_status": list(st["statuses"])}
            label = title
        else:
            uid = ctx.crm.tag_uuid(title) if kind == "tag" else ctx.crm.list_uuid(title)
            if not uid:
                ctx.out.problem("there is no %s titled %r in this account, so nothing is read from it" % (kind, title))
                continue
            must = {("any_tags" if kind == "tag" else "any_lists"): [uid],
                    "must_not": {"any_property_status": list(GONE_STATUSES)}}
            label = "the %s %s" % (title, kind)
        rows = ctx.crm.rows(must, cap=cap)
        if len(rows) >= cap:
            ctx.out.line("Only the first %s records of %s were listed this run." % (format(cap, ","), label))
        for r in rows:
            u = r.get("uuid")
            if not u:
                continue
            c = out.get(u)
            if c is None:
                out[u] = {"row": r, "rank": rank, "urgent": urgent, "via": label}
            else:
                c["urgent"] = c["urgent"] or urgent
                c["rank"] = min(c["rank"], rank)
    return out


# ---------------------------------------------------------------------- what to write
def decide(shown_raw, amount: float, years: list, prev, rec_year, tags_low: set, st: dict) -> dict:
    """What the record should be made to say, from what it shows now and what the county shows. Pure.

    `shown_raw` is the record's Tax delinquency amount (never Total Delinquency); `prev` the last LIVE
    reading, which says whether the record owed when its field cannot (a field a person cleared, a
    write that failed half way). Events: owes (newly), changed (another figure), owes_again (it carried
    Taxes Paid), unchanged, paid, current."""
    shown = _num(shown_raw)
    prev = prev if isinstance(prev, dict) else {}
    prev_owed = max(_num(prev.get("amount")) or 0.0, _num(prev.get("owed")) or 0.0)
    has_paid = bool(st["paid_tag"]) and st["paid_tag"].strip().lower() in tags_low
    year_now = _int(rec_year)
    first = min((int(y) for y in years if str(y).isdigit()), default=None)
    p = {"event": "", "field": None, "year": None, "clear_year": False, "add_tags": [], "remove_tags": [],
         "showed": shown if (shown or 0) > CENT else (prev_owed if prev_owed > CENT else None),
         "owed": max(shown or 0.0, prev_owed)}
    if amount > CENT:
        if shown is None or abs(shown - amount) > CENT:
            p["field"] = _figure(amount)
            p["event"] = "changed" if (shown or 0) > CENT else ("owes_again" if has_paid else "owes")
        else:
            p["event"] = "owes_again" if has_paid else "unchanged"
        if st["year_field"] and first and year_now != first:
            p["year"] = first
        if has_paid:
            p["remove_tags"] = [st["paid_tag"]]
        return p
    if (shown or 0) > CENT or prev_owed > CENT:
        p["event"] = "paid"
        if shown is None or abs(shown) > CENT:
            p["field"] = "0"
        if st["year_field"] and year_now is not None:
            p["clear_year"] = True
        if st["paid_tag"] and not has_paid:
            p["add_tags"] = [st["paid_tag"]]
        return p
    p["event"] = "current"
    if st["zero_on_blank"] and shown is None:
        p["field"] = "0"
    return p


def wants_note(p: dict) -> bool:
    """A note only for news: a new or changed figure, a record that owes again, a record that paid."""
    return p["event"] == "paid" or (p["event"] in ("owes", "changed", "owes_again")
                                    and (p["field"] is not None or bool(p["remove_tags"])))


def note_text(p: dict, amount: float, years: list, county: str, day: str) -> str:
    who = "the %s Trustee" % county_name(county)
    tail = " County tax only: city tax is billed separately."
    if p["event"] == "paid":
        was = (" The record showed %s." % _money(p["showed"])) if p["showed"] else ""
        return "Taxes paid. As of %s %s shows no delinquent county tax at this house.%s%s" % (day, who, was, tail)
    yrs = (" for %s" % _and(years)) if years else ""
    was = ""
    if p["event"] == "changed" and p["showed"]:
        was = " The record showed %s." % _money(p["showed"])
    elif p["event"] == "owes_again":
        was = " It had shown as paid before."
    return "County tax checked %s: %s shows %s delinquent%s.%s%s" % (day, who, _money(amount), yrs, was, tail)


def alert_text(p: dict, street: str, amount: float, years: list, county: str) -> str:
    """One line for the lead manager. A street address is fine in an alert; a phone number never is. No date
    in it: an alert posts once per exact text, and in plan mode the same news is found again every week."""
    who = "the %s Trustee" % county_name(county)
    if p["event"] == "paid":
        was = (" The record showed %s." % _money(p["showed"])) if p["showed"] else ""
        return "%s (a lead): %s shows no delinquent county tax now.%s" % (street, who, was)
    yrs = (" for %s" % _and(years)) if years else ""
    return "%s (a lead): %s shows %s of delinquent county tax%s. Count it in the offer." % (street, who, _money(amount), yrs)


# ---------------------------------------------------------------------- the run
def _dir() -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "taxwatch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _custom(ctx: Ctx, u: str, state: dict):
    try:
        return ctx.crm.custom_fields(u)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (one record failing must not stop the run)
        ctx.out.problem("could not read the custom fields on a record in scope (%s); it is tried again next run" % _short(exc))
        state["res"]["could not be read"] += 1
        return None


def _field_uuid(ctx: Ctx, st: dict, state: dict) -> str:
    """The tax figure's own field, looked up once a run. Refused when it resolves to Total Delinquency."""
    if state.get("field_uuid") is None:
        fid = ctx.crm.custom_field_uuid(st["amount_field"]) or ""
        total = ctx.crm.custom_field_uuid(TOTAL_LABEL) or ""
        if fid and fid == total:
            ctx.out.problem("the tax figure's field is Total Delinquency, which is liens plus taxes; nothing was written to it")
            fid = ""
        elif not fid:
            ctx.out.problem("there is no %r custom field in this account, so the tax figures went into the notes only"
                            % st["amount_field"])
        state["field_uuid"] = fid
    return state["field_uuid"]


def _tag_ok(ctx: Ctx, st: dict, state: dict) -> bool:
    if state.get("tag_ok") is None:
        state["tag_ok"] = bool(ctx.crm.tag_uuid(st["paid_tag"]))
        if not state["tag_ok"]:
            ctx.out.problem("there is no %r tag in this account; create it before this stage goes live (until then a record "
                            "that paid gets the note only)" % st["paid_tag"])
    return state["tag_ok"]


def _year_took(ctx: Ctx, u: str, field: str, want) -> bool:
    """The first year a live run writes is read back: the route is documented, but this field has not been
    written through it before. A year that did not take stops the year writes for the rest of the run."""
    try:
        got = (ctx.crm.record(u) or {}).get(field)
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001  (an unreadable read-back is not proof the write took)
        got = "unread"
    ok = (_int(got) == want) if want is not None else got in (None, "")
    if not ok:
        ctx.out.problem("the record's Tax Delinquent Year did not take the county's year, so year writes stopped for this run")
    return ok


def _apply(ctx: Ctx, st: dict, u: str, rec: dict, custom: dict, p: dict, amount: float, years: list,
           county: str, state: dict) -> tuple[bool, list]:
    """Make the record say what the county says. Each write keeps the value it replaces (ctx.change, before
    the write), and the note goes last: a write that fails stops the record before its note, and the next
    run, finding the rest already true, writes only what is missing."""
    did = []
    try:
        if p["field"] is not None:
            fid = _field_uuid(ctx, st, state)
            if fid:
                before = _label(custom, st["amount_field"])
                ctx.change(u, "custom_field", {"field_uuid": fid, "value": "" if before is None else str(before)},
                           {"field_uuid": fid, "value": p["field"]})
                ctx.write("tax figure", ctx.crm.set_custom_field, u, fid, p["field"])
                did.append("figure")
        field = st["year_field"]
        if field and (p["year"] is not None or p["clear_year"]) and state.get("year_ok") is not False:
            new = p["year"]
            ctx.change(u, "record_fields", {field: rec.get(field)}, {field: new})
            ctx.write("tax year", patch_record, ctx.crm, u, {field: new})
            did.append("year")
            if ctx.live and state.get("year_ok") is None:
                state["year_ok"] = _year_took(ctx, u, field, new)
        if p["remove_tags"]:
            ctx.change(u, "remove_tags", list(p["remove_tags"]), [])
            ctx.write("tag", ctx.crm.remove_tags, u, list(p["remove_tags"]))
            did.append("untag")
        if p["add_tags"] and _tag_ok(ctx, st, state):
            ctx.change(u, "add_tags", [], list(p["add_tags"]))
            ctx.write("tag", ctx.crm.add_tags, u, list(p["add_tags"]), rec)
            did.append("tag")
        if st["note"] and wants_note(p):
            ctx.write("note", ctx.crm.message, u, note_text(p, amount, years, county, _day(ctx.now, ctx.policy.tz)))
            did.append("note")
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (one refused write must not stop the run; it is said and retried)
        ctx.out.problem("DataSift refused a tax write on a record (%s); it is tried again next run" % _short(exc))
        return False, did
    return True, did


def _person(state: dict, u: str, a: dict, county: str, why: str) -> None:
    state["person"].append({"record": u, "street": str(a.get("street") or ""), "city": str(a.get("city") or ""),
                            "county": county or "", "why": why})


def _write_person_file(rows: list, resolved: set) -> str:
    """The records a person has to look up by hand (another county, two accounts at one house), merged with
    the earlier runs' and without the ones the county has since answered."""
    f = _dir() / PERSON_FILE
    old = []
    if f.exists():
        try:
            with f.open(encoding="utf-8", newline="") as fh:
                old = list(csv.DictReader(fh))
        except (OSError, csv.Error):
            old = []
    seen, out = set(), []
    for r in rows + old:
        u = str(r.get("record") or "")
        if not u or u in seen or u in resolved:
            continue
        seen.add(u)
        out.append({k: str(r.get(k) or "") for k in PERSON_COLS})
    if out or f.exists():
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=PERSON_COLS)
            w.writeheader()
            w.writerows(out)
    return str(f)


def _remember(key: str, u: str, value: dict) -> None:
    ledger.set_fact(u, key, value)


def _one(ctx: Ctx, st: dict, key: str, u: str, c: dict, state: dict) -> None:
    res, now_iso = state["res"], ctx.now.isoformat(timespec="seconds")
    try:
        rec = ctx.crm.record(u)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read a record in scope (%s); it is tried again next run" % _short(exc))
        res["could not be read"] += 1
        return
    state["reads"] += 1
    if not rec:
        res["could not be read"] += 1
        return
    hold = record_hold(rec, st)
    a = rec.get("address") if isinstance(rec.get("address"), dict) else {}
    street = str(a.get("street") or "").strip()
    if not hold and not street:
        hold = "no street on the record"
    if hold:
        res["held: " + hold] += 1
        _remember(key, u, {"at": now_iso, "status": "held", "why": hold})
        return
    custom = None
    county = str(a.get("county") or "").strip()
    if not county:
        custom = _custom(ctx, u, state)
        if custom is None:
            return
        county = str(_label(custom, COUNTY_LABEL) or "").strip()
    place = str(a.get("state") or "").strip().lower()
    if county_key(county) not in COUNTY_ROLLS or (place and place not in TN):
        if not county_key(county):
            why, group = "the record names no county", "no county named"
        elif county_key(county) not in COUNTY_ROLLS:
            why, group = "no county tax roll is read for %s" % county_name(county), county_name(county)
        else:
            why, group = "the record is not in Tennessee", "outside Tennessee"
        res["another county"] += 1
        state["counties"][group] += 1
        _person(state, u, a, county, why)
        _remember(key, u, {"at": now_iso, "status": "unwired", "why": why})
        return
    if state["county_down"]:
        res["waiting on the county site"] += 1
        return
    try:
        found = state["lookup"](street, county)
    except budget.BudgetExhausted:
        raise
    except Ambiguous as exc:
        res["two accounts at one house"] += 1
        _person(state, u, a, county, str(exc)[:120])
        _remember(key, u, {"at": now_iso, "status": "ambiguous", "why": str(exc)[:120]})
        return
    except LookupError as exc:
        res["another county"] += 1
        state["counties"][county_name(county)] += 1
        _person(state, u, a, county, str(exc)[:120])
        _remember(key, u, {"at": now_iso, "status": "unwired", "why": str(exc)[:120]})
        return
    except ValueError as exc:
        res["bills could not be read"] += 1
        _remember(key, u, {"at": now_iso, "status": "unreadable", "why": str(exc)[:120]})
        return
    except OSError as exc:
        state["county_down"] = _short(exc)
        ctx.out.problem("the %s tax site did not answer, so the rest of this run's records are checked next run"
                        % county_name(county))
        res["waiting on the county site"] += 1
        return
    except Exception as exc:  # noqa: BLE001  (one odd answer must not stop the run: counted, nothing written)
        res["bills could not be read"] += 1
        _remember(key, u, {"at": now_iso, "status": "unreadable", "why": type(exc).__name__})
        return
    if found is None:
        res["no parcel at that street"] += 1
        state["resolved"].add(u)
        _remember(key, u, {"at": now_iso, "status": "none"})
        return
    state["resolved"].add(u)
    amount = round(float(_num(found.get("amount")) or 0.0), 2)
    years = sorted({str(y).strip() for y in (found.get("years") or []) if str(y).strip()})
    if custom is None:
        custom = _custom(ctx, u, state)
        if custom is None:
            return
    prev = ledger.fact(u, MEMORY)          # the last LIVE reading, in plan mode too: it is what the writes left
    tags_low = {t.strip().lower() for t in tag_titles(rec)}
    p = decide(_label(custom, st["amount_field"]), amount, years, prev,
               rec.get(st["year_field"]) if st["year_field"] else None, tags_low, st)
    res["owes" if amount > CENT else "current"] += 1
    if amount > CENT:
        state["owe"].append((amount, street))
    if p["event"] == "paid":
        state["paid"].append(street)
    elif p["event"] == "owes_again":
        state["again"].append(street)
    if p["field"] is not None and str(_label(custom, TOTAL_LABEL) or "").strip():
        state["stale_total"] += 1
    ok, did = _apply(ctx, st, u, rec, custom, p, amount, years, county, state)
    for w in did:
        state["writes"][w] += 1
    if did:
        state["acted"] += 1
    if (st["alert_leads"] and status_of(rec) in lanes.LEAD_STATUSES
            and (p["event"] == "paid" or (p["event"] in ("owes", "changed", "owes_again") and p["field"] is not None))):
        ctx.out.alert(alert_text(p, street, amount, years, county))
    reading = {"at": now_iso, "status": "owes" if amount > CENT else "current", "amount": amount, "years": years,
               "account": str(found.get("account") or ""), "county": county_key(county)}
    if not ok:
        # Due again next run, still carrying what the record owed, so a paid record whose tag or note was
        # refused is still seen as paid then.
        reading.update(at="", owed=p["owed"])
    _remember(key, u, reading)


def run_taxwatch(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    if st["amount_field"].lower() == TOTAL_LABEL.lower():
        ctx.out.problem("the playbook points the tax figure at Total Delinquency, which is liens plus taxes; this stage did not run")
        ctx.out.head = "Not run: the tax figure may not be written to Total Delinquency."
        return
    pool = scope(ctx, st)
    ctx.out.found = len(pool)
    if not pool:
        ctx.out.head = "No record is in scope."
        return
    key = MEMORY if ctx.live else MEMORY_PLAN
    tz = ctx.policy.tz
    lookup = (ctx.sources or {}).get("county_tax") or partial(county_tax, pause=float(st["county_pause_s"]))
    state = {"res": Counter(), "reads": 0, "county_down": "", "lookup": lookup, "owe": [], "paid": [], "again": [],
             "writes": Counter(), "acted": 0, "person": [], "resolved": set(), "stale_total": 0,
             "counties": Counter(), "row_held": 0}
    due = []
    for u, c in pool.items():
        why = row_skip(c["row"], st)
        if why:
            state["res"]["held: " + why] += 1
            state["row_held"] += 1
            continue
        mem = ledger.fact(u, key)
        if is_due(mem, c["urgent"], st, ctx.now, tz):
            due.append((order_key(mem, c), u))
    due.sort()
    start, timed_out, done = _clock(), False, 0
    try:
        for _, u in due:
            # A record attempted counts toward the cap whether or not its read succeeded: each is a request.
            # Both counties run the same vendor, so a site that stopped answering ends the run's reading
            # rather than spending DataSift reads on records that can only wait.
            if done >= int(st["max_records"]) or state["county_down"]:
                break
            if (_clock() - start) / 60.0 >= float(st["max_minutes"]):
                timed_out = True
                break
            _one(ctx, st, key, u, pool[u], state)
            done += 1
    finally:
        _summarize(ctx, st, state, len(pool), len(due), done, timed_out)


def _summarize(ctx: Ctx, st: dict, state: dict, in_scope: int, due: int, done: int, timed_out: bool) -> None:
    res, w = state["res"], state["writes"]
    ctx.out.tally.update(res)
    ctx.out.acted = state["acted"]
    path = _write_person_file(state["person"], state["resolved"])
    checked = res["owes"] + res["current"]
    # A run that "succeeds" while the county's answer has changed under it is the failure this codebase keeps
    # meeting (a rejected street form, a new bill shape): said out loud once most answers fail.
    asked = checked + res["no parcel at that street"] + res["bills could not be read"] + res["two accounts at one house"]
    if asked >= SYSTEMIC_MIN and res["bills could not be read"] * 2 >= asked:
        ctx.out.problem("the county's bills could not be read for most records this run; its answer may have changed")
    if asked >= SYSTEMIC_MIN and res["no parcel at that street"] * 5 >= asked * 4:
        ctx.out.problem("the county found no parcel for most of the streets asked this run; the street matching may need a look")
    owe = sorted(state["owe"], reverse=True)
    total = sum(a for a, _ in owe)
    paid, again = state["paid"], state["again"]
    left = max(0, due - done)
    verb = "Updated" if ctx.live else "Would update"
    if not due and state["row_held"] >= in_scope:
        ctx.out.head = "None of the %s records in scope can be looked up (the reasons are below)." % format(in_scope, ",")
    elif not due:
        ctx.out.head = ("Nothing is due: every one of the %s records in scope that can be looked up was checked inside "
                        "its window." % format(in_scope, ","))
    elif checked:
        ctx.out.head = ("Checked %d of %s due records with the county Trustee: %d owe %s in delinquent county tax, %d paid "
                        "since the record showed them owing. %s %d record(s)." % (
                            checked, format(due, ","), len(owe), _money(total), len(paid), verb, state["acted"]))
    else:
        ctx.out.head = "Read %d of %s due records; none could be checked with the county (the reasons are below)." % (
            done, format(due, ","))
    ctx.out.line("In scope: %s records, %s due. Read this run: %d; left for later runs: %s." % (
        format(in_scope, ","), format(due, ","), done, format(left, ",")))
    if owe:
        ctx.out.line("Owe delinquent county tax: %d record(s), %s in all (largest %s at %s)." % (
            len(owe), _money(total), _money(owe[0][0]), owe[0][1]))
    if paid:
        ctx.out.line("Paid since the record showed them owing: %d (%s)." % (len(paid), _streets(paid)))
    if again:
        ctx.out.line("Owe again after showing paid: %d (%s)." % (len(again), _streets(again)))
    if w:
        ctx.out.line("%s: %d tax figure(s), %d delinquent year(s), %d %s tag(s) added, %d removed, %d note(s)." % (
            "Wrote" if ctx.live else "Would write", w["figure"], w["year"], w["tag"], st["paid_tag"], w["untag"], w["note"]))
    misses = [(res["no parcel at that street"], "no parcel at that street on the county roll"),
              (res["bills could not be read"], "bills could not be read"),
              (res["two accounts at one house"], "two or more accounts answering at one house")]
    said = "; ".join("%s: %d" % (what, n) for n, what in misses if n)
    if said:
        ctx.out.line(said[:1].upper() + said[1:] + ". Nothing was written for these.")
    if res["another county"]:
        top = sorted(state["counties"].items(), key=lambda kv: (-kv[1], kv[0]))[:4]   # stable: count, then name
        parts = ", ".join("%s %d" % (k, n) for k, n in top)
        ctx.out.line("Not on a county roll read here: %d (%s), listed for a person in %s." % (
            res["another county"], parts, Path(path).name))
    held = sorted(((k[len("held: "):], n) for k, n in res.items() if k.startswith("held: ")), key=lambda kv: (-kv[1], kv[0]))
    if held:
        ctx.out.line("Not looked up: %s." % "; ".join("%d %s" % (n, why) for why, n in held[:4]))
    if state["stale_total"]:
        ctx.out.line("Total Delinquency still holds the old tax on %d of the changed records; the lien records keep that "
                     "field and it was not touched." % state["stale_total"])
    if res["could not be read"]:
        ctx.out.line("Records that could not be read from DataSift: %d (tried again next run)." % res["could not be read"])
    if state["county_down"]:
        ctx.out.line("The county site did not answer (%s); the %s records still due are checked next run." % (
            state["county_down"], format(left + res["waiting on the county site"], ",")))
    if timed_out:
        ctx.out.line("Stopped at the %s minute limit; the rest are due next run." % _figure(float(st["max_minutes"])))


def _streets(streets: list) -> str:
    shown = ", ".join(streets[:3])
    return shown + (" and %d more" % (len(streets) - 3) if len(streets) > 3 else "")
