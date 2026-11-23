"""Knox tax sale results: the former owners who can still redeem, and the winning bidders as buyers.

Research item 7 (2026-11-15; rules C12.25, C12.33, C11.18, C1.31). knox_tax_sale.py archives each
document the Trustee posts about a tax sale, the results included, and stops there: "the results
document (the Report of Sale) is archived and announced but not parsed". The results are the list.
Each row names a parcel, its address, the defendants (the former owner), the winning bidder, the
bid, the excess proceeds, and the years delinquent and redemption period for county and city.

TWO LISTS FROM ONE DOCUMENT.
  1. The FORMER OWNER can still redeem: a tax sale does not end their claim. In Tennessee the right
     to redeem vests in every interested person when the court enters the order confirming the
     sale (Tenn. Code Ann. 67-5-2701(a)(1)). An owner who can still get the house back, and who is
     usually owed the excess proceeds, is a motivated owner. The course treats the redemption as an
     advanced play (C12.25) that is never too late to check (C12.33), on a research pipeline of its
     own (C11.18). So a former owner inside the window is tagged Tax Sale Redemption, which is how a
     record reaches the research queue (its scope_tags), with a note that says until when.
  2. The WINNING BIDDER is a verified cash buyer. A deed sweep rarely sees them (it is zip scoped and
     a tax sale buyer often has no deed yet): the 2026 sale's 95 bidders matched none of the buyers
     two zip-scoped deed sweeps had found, apart from the county itself. They go to a file in the
     buyer registry's own folder, in the registry's own shape, for the registry to fold in
     (merge_buyers below).

THE PERIOD (Tennessee law, read 2026-11-15; the period on each row comes first):
  * The Trustee's report prints the period the court applied to each parcel, county and city
    ("90 days", "180 days", "12 Months"). That is used as printed. When a row prints two, the
    SHORTER wins by default: the period follows the longest run of delinquency, and an owner told
    "you can still redeem" after the right ended is told something false (period_rule).
  * A row that prints none falls back to the statute, by the years delinquent and the date of the
    order: before July 1, 2026, five years or less is one year, more than five and less than eight
    is 180 days, eight or more is 90 days (67-5-2701(a)(1)(B), the tiers the University of
    Tennessee's County Technical Assistance Service publishes, and the tiers the 2026 report itself
    prints). For an order entered on or after July 1, 2026, three years or less is one year and more
    than three is 90 days (Public Chapter 971 of 2026, HB 1932 and SB 1983, law on May 18, 2026; read
    from the bill trackers because the enrolled text would not open, so the scale is a setting).
  * A court may set 30 days on a vacant property ((a)(1)(D)); the report prints that one too.
  * Never more than one year after the order, and the window starts at the ORDER, not the sale.
    The order (the decree confirming the sale) is a scanned image, so its date is a setting
    (confirmed). Without it the window is counted from the sale date, which ends it a few weeks
    early: an owner is never told the right is open after it has closed. When the Trustee posts the
    decree and its sale has no date under `confirmed`, the run asks a person to read the decree and
    set it (Tax Sale 25: sold Jun 2, 2026, the decree ENTERED Jun 30, 2026, read off its first page).

BUYING THE RIGHT. The research note that "an investor can buy their interest and redeem it" runs
into 67-5-2701(b)(2): a court denies a motion to redeem by a transferee who is speculating or
profiteering, presumed when the right was bought for less than the purchase price minus the
redemption amount. Every note this stage writes says to check with a lawyer before offering one.
Nothing here texts, calls or mails anyone: it tags, notes and queues research.

HOW THE TABLE IS READ. pdfminer's default layout reads a table column by column, which cannot be
put back into rows. Asked for one line per printed row (a wide char_margin) it gives the row the
way the 2026 parse saw it, address and owner run together, so the row is read from both ends: the
file number and tax id on the left; the redemption groups, then the three money columns on the
right; the instrument number and bidder number in the middle; and the address cut from the owner
at the first street type ("Avenue A" and "Drive E" are real Knoxville streets, so a lone letter
after the type stays with the street). Every reading is checked against the printed totals: rows
that do not add up to the totals line are a loud problem, because a partial parse looks exactly
like a sale with fewer parcels. "Add up" allows the rounding of the printed cents (half a cent a
row): the real Tax Sale 25 results read all 195 rows, bid and excess to the cent, and the minimum
bid column 4 cents off its printed total, which a to-the-cent test called broken. A text file
beside the PDF (the same name, .txt) is read first, so a person can fix a bad reading by hand.

Records that are not in the account are listed for a person, never created. People only: a company
or a trust is listed, not tagged. A lead belongs to the lead manager and is listed. Every record
passes the suppression gate on the research channel before it is tagged, and the record's owner
must be the former owner on the report (a SiftMap refresh can already show the bidder as owner).
When a window closes, the tag this stage put on comes off. Every write goes through ctx.write and
records its before-value for undo; what the stage learns (the parse, the window on a record) is
kept in plan mode too, because the research queue ranks on it.

THE ARCHIVE is the one knox_tax_sale.capture writes (tax_sale_docs.json and a folder of PDFs named
by content hash). With `fetch: true` this box captures it itself at the start of every run, with
that same code: the Trustee's page is a static site (Amazon S3 behind CloudFront) that answered a
plain request from a workstation on 2026-11-20, every PDF included. The same day the first-to-market
box's datacenter address got a 403 from knoxcounty.org (where two of the menu's forms live; those
are no longer fetched), so the playbook sets `fetch_via: residential`: the plain request first, then
Scrapfly's residential addresses for a request that was refused, as the first-to-market box fetches,
and the run says how many went that way. A refusal Scrapfly cannot clear is a loud problem, with the
last way round it (an archive captured on a workstation with `python src/knox_tax_sale.py
--capture-only`, copied to archive_dir). The page also announces each sale's date, which the results
PDF does not print, so the date the page gave a document's sale number is the sale date when no
setting and no text says otherwise. Capturing is this box's own knowledge, so a plan run captures too.

THE PERSON'S LIST. Every run writes every open window to one file, nearest deadline first, with the
record link and what happens to it (tagged for research, listed for a person and why, held by the
gate and why). The research queue takes the tagged ones (its scope_tags), and the gate holds a
tagged owner out of every outreach channel (gate.research_tags) until a person takes the tag off,
which is the lawyer's okay in practice.
"""
from __future__ import annotations

import calendar
import csv
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import lanes, ledger
from .stages import addr, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

TAG = "Tax Sale Redemption"
FACT = "tax_redemption"           # knowledge: the open window on a record (kept in plan mode too)
MARK = "taxsale_mark"             # what this stage put on a record (live runs only)
BUYERS_FILE = "tax_sale_buyers.json"
DOCS = "tax_sale_docs.json"       # knox_tax_sale.DOCS: the archive's index, keyed by content hash
ARCHIVE = "tax_sale_archive"      # the archive's folder of PDFs
MIN_ROWS = 5                      # a results table with fewer rows than this was not really read
COVERAGE = 0.95                   # rows must account for this share of the tax ids in the text
LIST_FILE = "Tax_Sale_Redemption_Open_Windows.csv"
FETCH_TIMEOUT_S = 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DECREE_RX = re.compile(r"decree|confirm", re.I)

# Tests swap these for fakes. In production: readings(), county_parcel() and the getter below.
READ = None
lookup_parcel = None
FETCH = None

DEFAULTS = {
    "fetch": False,                 # read the Trustee's page on this box at the start of every run (knox_tax_sale.capture)
    "page": "",                     # default: KNOX_TAX_SALE_PAGE, else the Trustee's tax sale page
    "fetch_via": "direct",          # direct | residential (Scrapfly, only when the plain request is refused)
    "archive_dir": "",              # default: the box's data folder, knox (the archive's own layout)
    "county": "Knox",
    "tag": TAG,
    "note": True,                   # a note on the record with the window, the money and the lawyer line
    "period_rule": "shorter",       # shorter | longer | county, when a row prints two periods
    "confirmed": {},                # sale date -> the date the court confirmed it (the window starts there)
    "sale_dates": {},               # PDF file name -> sale date, when the document's own date cannot be read
    "scales": [
        {"from": "2026-07-01", "steps": [[3, "12 months"]], "over": "90 days"},      # Public Chapter 971 (2026)
        {"from": "1900-01-01", "steps": [[5, "12 months"], [7, "180 days"]], "over": "90 days"},
    ],
    "min_days_left": 5,             # closer than this, research cannot finish: listed for a person instead
    "max_records": 40,              # report rows looked up in the account in one run
    "recheck_days": 7,              # how long a lookup or a plan verdict is trusted
    "roll_check": True,             # a row whose address finds no record is tried at the county roll's address
    "max_roll_lookups": 20,
    "buyers": True,
    "registry_dir": "",             # default: the buyer registry's own folder (AGENTS_BUYER_REGISTRY_DIR)
    "government_bidders": ["Knox County", "City of Knoxville", "State of Tennessee"],
}

UNITS = {"day": "days", "days": "days", "month": "months", "months": "months", "mo": "months", "mos": "months",
         "year": "years", "years": "years", "yr": "years", "yrs": "years"}
SUFFIX = {"ST", "STREET", "AVE", "AV", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE", "CT", "COURT", "CIR",
          "CIRCLE", "BLVD", "BOULEVARD", "PL", "PLACE", "TER", "TERR", "TERRACE", "PIKE", "PK", "HWY", "HIGHWAY",
          "PKWY", "PARKWAY", "WAY", "TRL", "TRAIL", "LOOP", "CV", "COVE", "XING", "SQ", "BND", "HOLW", "RDG", "PT",
          "VW", "ALY", "CRES", "EXT", "GRV", "HTS", "MNR", "PLZ", "PSGE", "RUN", "VLY", "WALK"}
DIRECTIONS = {"NE", "NW", "SE", "SW"}
UNIT_WORDS = {"APT", "UNIT", "STE", "SUITE", "LOT", "BLDG"}
PIN_RX = re.compile(r"^\d{3}[A-Z]{0,2}-\d{3,5}(?:\.\d{1,2})?$")
PIN_ANY = re.compile(r"(?<![0-9A-Z])\d{3}[A-Z]{0,2}-\d{3,5}(?:\.\d{1,2})?(?![0-9])")
HEAD_WORDS = re.compile(r"\b(file|tax id|parcel|defendants?|bidders?|redemption|page|report of sale|exhibit|trustee|"
                        r"chancery|docket|winning|minimum|excess|instrument|property address|totals?)\b", re.I)
GOV_RX = re.compile(r"^(?:THE )?(?:CITY|TOWN|STATE|COUNTY) OF\b|^[A-Z]+ COUNTY(?: TN| TENNESSEE| GOVERNMENT| TRUSTEE)?$")


# ---------------------------------------------------------------------- settings and places
def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def iso_day(v) -> str:
    """A date in any shape a setting or a record carries it (YAML reads a bare 2026-06-30 as a date)."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def nice(d: date | str) -> str:
    """Jun 2, 2026: a deadline months out needs its year."""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return "%s %d, %d" % (d.strftime("%b"), d.day, d.year)


def options(st) -> dict:
    """The stage's settings over the defaults."""
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k) if st is not None else None
        o[k] = v if got is None else got
    o["confirmed"] = {iso_day(k): iso_day(v) for k, v in dict(o["confirmed"] or {}).items() if iso_day(k)}
    o["sale_dates"] = {str(k).strip().lower(): iso_day(v) for k, v in dict(o["sale_dates"] or {}).items()}
    o["government_bidders"] = [str(x) for x in _list(o["government_bidders"])]
    o["period_rule"] = str(o["period_rule"] or "shorter").strip().lower()
    o["fetch_via"] = str(o["fetch_via"] or "direct").strip().lower()
    o["page"] = str(o["page"] or "").strip()
    return o


def state_dir() -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "taxsale"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_dir(o: dict) -> Path:
    return Path(o["archive_dir"]) if o.get("archive_dir") else Path(config.DATA_DIR) / "knox"


def registry_dir(o: dict) -> Path:
    """The buyer registry's folder, the same setting the buyer_registry workflow reads."""
    if o.get("registry_dir"):
        return Path(o["registry_dir"])
    return Path(os.environ.get("AGENTS_BUYER_REGISTRY_DIR") or (Path(config.DATA_DIR) / "dispo_buyers"))


def block_problems(st) -> list[str]:
    """A typo in the playbook block is refused when the playbook loads, never read as a default."""
    p, sid = [], st.id
    if str(st.get("period_rule", "shorter")).strip().lower() not in ("shorter", "longer", "county"):
        p.append("stage %s: period_rule must be shorter, longer or county" % sid)
    for key in ("confirmed", "sale_dates"):
        v = st.get(key)
        if v is not None and not isinstance(v, dict):
            p.append("stage %s: %s must map %s to a date" % (sid, key, "a sale date" if key == "confirmed" else "a file name"))
    for k, v in dict(st.get("confirmed") or {}).items() if isinstance(st.get("confirmed"), dict) else []:
        if not iso_day(k) or not iso_day(v):
            p.append("stage %s: confirmed needs a sale date and the order's date, both YYYY-MM-DD (%r: %r)" % (sid, k, v))
    for k, v in dict(st.get("sale_dates") or {}).items() if isinstance(st.get("sale_dates"), dict) else []:
        if not iso_day(v):
            p.append("stage %s: sale_dates[%r] must be a date, YYYY-MM-DD" % (sid, k))
    scales = st.get("scales")
    if scales is not None:
        if not isinstance(scales, list) or not scales:
            p.append("stage %s: scales must be a list of {from, steps, over}" % sid)
        else:
            for i, s in enumerate(scales, 1):
                ok = isinstance(s, dict) and iso_day(s.get("from")) and period(s.get("over"))
                steps = s.get("steps") if isinstance(s, dict) else None
                ok = ok and isinstance(steps, list) and all(
                    isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], int) and period(x[1]) for x in steps)
                if not ok:
                    p.append("stage %s: scale %d needs a from date, steps like [[5, \"12 months\"]] and an over period" % (sid, i))
    for key in ("min_days_left", "recheck_days", "max_roll_lookups"):
        v = st.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            p.append("stage %s: %s must be a whole number, 0 or more" % (sid, key))
    v = st.get("max_records")
    if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
        p.append("stage %s: max_records must be a whole number, 1 or more" % sid)
    if st.get("tag") is not None and not str(st.get("tag") or "").strip():
        p.append("stage %s: tag must name the tag the research queue reads" % sid)
    if st.get("fetch") is not None and not isinstance(st.get("fetch"), bool):
        p.append("stage %s: fetch must be true or false" % sid)
    if st.get("fetch_via") is not None and str(st.get("fetch_via")).strip().lower() not in ("direct", "residential"):
        p.append("stage %s: fetch_via must be direct or residential" % sid)
    page = st.get("page")
    if page and not re.match(r"^https?://\S+$", str(page).strip()):
        p.append("stage %s: page must be the Trustee's page address, http(s)://..." % sid)
    return p


# ---------------------------------------------------------------------- the period
def period(text) -> tuple[str, int] | None:
    """("days", 90) or ("months", 12) from "90 days", "12 Months", "1 year"; None for anything else."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*([A-Za-z]+)\s*", str(text or ""))
    if not m:
        return None
    unit, n = UNITS.get(m.group(2).lower()), int(m.group(1))
    if not unit or n <= 0:
        return None
    return ("months", n * 12) if unit == "years" else (unit, n)


def period_words(p: tuple[str, int]) -> str:
    unit, n = p
    return "%d %s" % (n, unit if n != 1 else unit[:-1])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def end_of(start: date, p: tuple[str, int]) -> date:
    """The last day the right is open: the period from the order, and never more than one year after it."""
    unit, n = p
    end = start + timedelta(days=n) if unit == "days" else add_months(start, n)
    return min(end, add_months(start, 12))


def statute_period(years: int, order_day: date, scales) -> tuple[str, int] | None:
    """The period the statute sets for this many years delinquent, on the scale in force on the day of
    the order (the newest scale whose `from` is on or before it)."""
    pick = None
    for s in sorted([s for s in _list(scales) if isinstance(s, dict)], key=lambda s: iso_day(s.get("from")), reverse=True):
        if iso_day(s.get("from")) and iso_day(s.get("from")) <= order_day.isoformat():
            pick = s
            break
    if pick is None:
        return None
    for limit, words in pick.get("steps") or []:
        if years <= int(limit):
            return period(words)
    return period(pick.get("over"))


def row_period(row: dict, order_day: date, o: dict) -> tuple[tuple[str, int] | None, str]:
    """(the period, where it came from: report or statute). The row's own print comes first."""
    county, city = period(row.get("county_period")), period(row.get("city_period"))
    printed = [p for p in (county, city) if p]
    if o["period_rule"] == "county" and county:
        printed = [county]
    if printed:
        pick = (max if o["period_rule"] == "longer" else min)(printed, key=lambda p: end_of(order_day, p))
        return pick, "report"
    years = max(int(row.get("county_years") or 0), int(row.get("city_years") or 0))
    if years <= 0:
        return None, ""
    p = statute_period(years, order_day, o["scales"])
    return p, ("statute" if p else "")


# ---------------------------------------------------------------------- reading one row
def money_of(tok: str) -> float | None:
    """A money figure as printed ($12,500.00, 3,210.55, -$0.01, ($0.01)); None for a plain number,
    so a years-delinquent column is never read as dollars."""
    t = (tok or "").strip()
    if not any(c in t for c in "$.,"):
        return None
    neg = t.startswith("-") or t.startswith("(") or "-$" in t
    num = re.sub(r"[^\d.]", "", t)
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", num):
        return None
    if "," in t and not re.search(r"\d{1,3}(?:,\d{3})+", t):
        return None
    v = float(num)
    return -v if neg else v


def _instrument(tok: str) -> bool:
    t = (tok or "").upper()
    return len(t) >= 10 and re.fullmatch(r"[0-9A-Z-]+", t) is not None and sum(c.isdigit() for c in t) >= 6


def _pin_at(toks: list[str], i: int) -> tuple[str, int]:
    """(tax id, words it took) at toks[i]; the id may be printed with spaces ("082 LB-012")."""
    for n in (1, 2, 3, 4):
        if i + n > len(toks):
            break
        cand = "".join(toks[i:i + n]).upper()
        if PIN_RX.match(cand):
            return cand, n
    return "", 0


def pin_key(pin: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(pin or "").upper())


def split_address(words: list[str]) -> tuple[str, str, str]:
    """(street, owner, how) from the run-together address and defendants.

    The street ends at the FIRST street type after the house number. A street whose name is itself a
    street type ("Court St") keeps the type that follows; a lone letter or a two-letter direction after
    the type stays with the street, because Knoxville has streets named Avenue A and Drive E; a unit
    (#D3, Apt 4) stays too. With no street type anywhere, the street is the number and one word
    ("guessed"): the record match then has to agree on that word, so a wrong guess finds nothing."""
    up = [w.upper().strip(",") for w in words]
    first = 1 if up and re.fullmatch(r"\d+[A-Z]?", up[0]) else 0
    cut = next((i for i in range(first, len(up)) if up[i] in SUFFIX), None)
    if cut is None:
        if first and len(up) > 2:
            return " ".join(words[:2]), " ".join(words[2:]), "guessed"
        return "", " ".join(words), "none"
    j = cut
    if cut == first and j + 1 < len(up) and up[j + 1] in SUFFIX:
        j += 1                                  # "COURT ST": the first word was the street's name
    k = j + 1
    if k < len(up) and (up[k] in DIRECTIONS or (len(up[k]) == 1 and up[k].isalpha())):
        k += 1
    if k < len(up) and up[k].startswith("#"):
        k += 1
    elif k + 1 < len(up) and up[k] in UNIT_WORDS:
        k += 2
    return " ".join(words[:k]), " ".join(words[k:]), "street type"


def parse_line(line: str) -> dict | None:
    """One printed row as a dict, or None when the line is not a row."""
    toks = (line or "").split()
    if len(toks) < 9 or not re.fullmatch(r"\d{1,6}", toks[0]):
        return None
    pin, used = _pin_at(toks, 1)
    if not pin:
        return None
    j, groups = len(toks), []
    while len(groups) < 2 and j > 0:
        per = None
        if j >= 2 and toks[j - 1].lower() in UNITS and toks[j - 2].isdigit():
            per = "%s %s" % (toks[j - 2], toks[j - 1].lower())
            j -= 2
        if j >= 1 and re.fullmatch(r"\d{1,3}", toks[j - 1]):
            groups.append((int(toks[j - 1]), per))
            j -= 1
            continue
        if per is not None:
            return None                         # a period with no years before it: not this layout
        break
    if not groups:
        return None
    groups.reverse()                            # county first, then city
    money = []
    while len(money) < 3 and j > 0:
        v = money_of(toks[j - 1])
        if v is None:
            break
        money.append(v)
        j -= 1
    if len(money) != 3:
        return None
    excess, minimum, bid = money
    mid = toks[1 + used:j]
    at = next((i for i in range(1, len(mid) - 1) if _instrument(mid[i]) and re.fullmatch(r"\d{1,5}", mid[i + 1])), None)
    if at is None:
        return None
    street, owner, how = split_address(mid[:at])
    county = groups[0]
    city = groups[1] if len(groups) > 1 else (0, None)
    return {"file": toks[0], "pin": pin, "pin_key": pin_key(pin), "address": street, "split": how, "owner": owner,
            "instrument": mid[at].upper(), "bidder_no": mid[at + 1], "bidder": " ".join(mid[at + 2:]),
            "bid": bid, "minimum": minimum, "excess": excess,
            "county_years": county[0], "county_period": county[1] or "",
            "city_years": city[0], "city_period": city[1] or "", "wrapped": ""}


def parse_text(text: str) -> dict:
    """Every row in one document's text, the totals line, and how many tax ids the text holds."""
    rows, totals, prev_row = [], None, False
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        r = parse_line(line)
        if r:
            rows.append(r)
            prev_row = True
            continue
        toks = line.split()
        found = [m for m in (money_of(t) for t in toks) if m is not None]
        if re.search(r"\btotals?\b", line, re.I) and found:
            totals = found
            prev_row = False
            continue
        # A wrapped cell's second line ("ETAL", "& DOE JOHN") prints on its own line under the row.
        if prev_row and rows and not found and len(toks) <= 8 and not HEAD_WORDS.search(line) \
                and not any(ch.isdigit() for ch in line):
            rows[-1]["wrapped"] = (rows[-1]["wrapped"] + " " + line).strip()
            continue
        prev_row = False
    pins = {pin_key(p) for p in PIN_ANY.findall((text or "").upper())}
    return {"rows": rows, "totals": totals, "pins": len(pins)}


def rounding_cents(n_rows: int) -> int:
    """How far a column of printed cents may sit from its printed total: each row is rounded to the cent
    (half a cent either way) and so is the total. The real Tax Sale 25 minimum bid column summed to 4
    cents over its printed total on 195 rows that were all read. A missing or misread row is off by
    dollars, never by this."""
    return max(1, n_rows // 2 + 1)


def check_totals(rows: list[dict], totals: list | None) -> str:
    """confirmed (the rows add up to the printed totals, within the rounding of the printed cents),
    broken (they do not), unconfirmed (no totals line), or none (no rows). The excess total is the
    last figure printed on the totals line; the bid and minimum totals precede it when they are
    printed."""
    if not rows:
        return "none"
    if not totals:
        return "unconfirmed"
    sums = [sum(int(round(r[k] * 100)) for r in rows) for k in ("bid", "minimum", "excess")]
    tot = [int(round(t * 100)) for t in totals][-3:]
    slack = rounding_cents(len(rows))
    return "confirmed" if all(abs(a - b) <= slack for a, b in zip(sums[3 - len(tot):], tot)) else "broken"


# ---------------------------------------------------------------------- the archive
def reader_available() -> bool:
    try:
        import pdfminer.high_level  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def readings(pdf: Path) -> list[tuple[str, str]]:
    """Every reading of one PDF worth trying, best first: a text file a person left beside it, then
    pdfminer with one line per printed row, then knox_tax_sale's own reading (pdfminer's default)."""
    out: list[tuple[str, str]] = []
    side = Path(pdf).with_suffix(".txt")
    if side.is_file():
        out.append(("text file", side.read_text(encoding="utf-8", errors="replace")))
    if not Path(pdf).is_file() or not reader_available():
        return out
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams
    try:
        out.append(("rows", extract_text(str(pdf), laparams=LAParams(char_margin=200.0, line_margin=0.0, word_margin=0.1)) or ""))
    except Exception as exc:  # noqa: BLE001  (a damaged file must not stop the other documents)
        log.warning("tax sale results: the row reading failed on %s (%s)", pdf, exc)
    try:
        import knox_tax_sale
        out.append(("default", knox_tax_sale._pdf_text(str(pdf))))
    except Exception as exc:  # noqa: BLE001
        log.warning("tax sale results: the default reading failed on %s (%s)", pdf, exc)
    return out


def load_docs(arc: Path) -> dict | None:
    """The archive's index, or None when this box has no archive at all."""
    path = Path(arc) / DOCS
    if not path.is_file():
        return None
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _file_name(doc: dict) -> str:
    return urllib.parse.unquote(str(doc.get("url") or "").rsplit("/", 1)[-1].split("?")[0])


def _pdf_path(doc: dict, arc: Path, sha: str) -> Path:
    """The archive's path is absolute on the box that captured it; a copy on this box sits under the
    archive folder by its content hash."""
    p = Path(str(doc.get("path") or ""))
    if str(doc.get("path") or "") and p.is_file():
        return p
    return Path(arc) / ARCHIVE / ("%s.pdf" % sha)


def _rank(got: dict) -> tuple:
    return ({"confirmed": 3, "unconfirmed": 2, "broken": 1, "none": 0}[got["check"]], len(got["rows"]))


def _expects_table(doc: dict) -> bool:
    blob = ("%s %s" % (doc.get("label") or "", _file_name(doc))).lower()
    return bool(re.search(r"result|report\s*of\s*sale|reportofsale", blob)) and not re.search(r"decree|confirm", blob)


def parse_doc(sha: str, doc: dict, arc: Path, now: datetime) -> dict:
    """One results document read into rows, with the reading that added up best."""
    base = {"sha": sha, "label": str(doc.get("label") or ""), "url": str(doc.get("url") or ""), "file": _file_name(doc),
            "first_seen": str(doc.get("first_seen") or ""), "parsed_at": now.isoformat(timespec="seconds"),
            "expects_table": _expects_table(doc), "page_sale": str(doc.get("sale_date_page") or "")}
    path = _pdf_path(doc, arc, sha)
    texts = (READ or readings)(path)
    if not texts:
        why = ("the archived file is not on this box" if not path.is_file() else
               "no PDF reader is installed on this box (pdfminer.six)")
        return dict(base, status="unreadable", why=why, rows=[], totals=None, check="none", reading="", pins=0, doc_sale="")
    best, best_text = None, ""
    for name, text in texts:
        got = parse_text(text)
        got["reading"], got["check"] = name, check_totals(got["rows"], got["totals"])
        if best is None or _rank(got) > _rank(best):
            best, best_text = got, text
    rows, pins = best["rows"], best["pins"]
    if len(rows) < MIN_ROWS and pins < MIN_ROWS:
        status = "no_table"
    elif best["check"] == "broken" or (pins and len({r["pin_key"] for r in rows}) < COVERAGE * pins):
        status = "broken"
    else:
        status = "table"
    doc_sale = ""
    try:
        import knox_tax_sale
        doc_sale = knox_tax_sale.sale_date(best_text, base["label"])
    except Exception as exc:  # noqa: BLE001  (without the reader the date comes from the settings)
        log.warning("tax sale results: the sale date reader did not load (%s)", exc)
    return dict(base, status=status, why="", rows=rows, totals=best["totals"], check=best["check"], reading=best["reading"],
                pins=pins, doc_sale=doc_sale)


def rejudge(s: dict) -> None:
    """A document read on an earlier run, judged again by today's rules (the totals check, the coverage)
    without opening the PDF: the parse is kept, the verdict on it is not."""
    if s.get("status") not in ("table", "broken"):
        return
    rows, pins = s.get("rows") or [], int(s.get("pins") or 0)
    s["check"] = check_totals(rows, s.get("totals"))
    s["status"] = "broken" if s["check"] == "broken" or (pins and len({r.get("pin_key") for r in rows}) < COVERAGE * pins) \
        else "table"


def load_sale(sha: str) -> dict | None:
    f = state_dir() / ("sale_%s.json" % sha)
    try:
        got = json.loads(f.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def save_sale(s: dict) -> None:
    f = state_dir() / ("sale_%s.json" % s["sha"])
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, default=str), encoding="utf-8")
    tmp.replace(f)


def sale_day(s: dict, o: dict) -> str:
    """The sale date: the setting for this file (or its label) first, then the document's own, then the
    date the Trustee's page announced for the document's sale number (knox_tax_sale.capture keeps it).
    A results document comes after its sale, so a page date later than the day the document was first
    archived belongs to some other sale and is not used."""
    for key in (s.get("file") or "", s.get("label") or ""):
        got = o["sale_dates"].get(key.strip().lower())
        if got:
            return got
    own = iso_day(s.get("doc_sale"))
    if own:
        return own
    page, seen = iso_day(s.get("page_sale")), iso_day(s.get("first_seen"))
    return page if page and (not seen or page <= seen) else ""


def sale_number(s: dict) -> str:
    """The sale's number the document's link or file name gives ("Tax Sale 25"), or ''."""
    try:
        import knox_tax_sale
        return knox_tax_sale.sale_number(s.get("label") or "", s.get("url") or s.get("file") or "")
    except Exception:  # noqa: BLE001  (without the module a decree is tied to its sale by its own date only)
        return ""


def decrees(sales: list[dict], o: dict) -> list[tuple[str, dict]]:
    """(sale date, document) for every decree confirming a sale in the archive. A scanned decree has no
    text, so its sale is its own setting or page date, else the sale of the results table that names the
    same sale number."""
    by_number: dict[str, str] = {}
    for s in sales:
        n, day = sale_number(s), sale_day(s, o)
        if n and day and s.get("status") in ("table", "broken"):
            by_number.setdefault(n, day)
    out = []
    for s in sales:
        if DECREE_RX.search("%s %s" % (s.get("label") or "", s.get("file") or "")):
            out.append((sale_day(s, o) or by_number.get(sale_number(s), ""), s))
    return out


# ---------------------------------------------------------------------- reading the Trustee's page
def direct_get(url: str, timeout: int = FETCH_TIMEOUT_S) -> bytes:
    """One plain request, the way the Trustee's site answered a workstation on 2026-11-20. A refusal
    raises with its status, and the run says so."""
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.content


def getter(o: dict, refused: list | None = None):
    """How this box reads the page: the test's fake, the plain request, or the first-to-market box's own
    fetch (knox_condemnations.http_get: the plain request first, then Scrapfly's residential addresses
    when SCRAPFLY_KEY is set and the plain request was refused, about 25 credits a fetch). Every address
    the plain request was refused at is added to `refused`, so the run can say what went the paid way."""
    if FETCH is not None:
        return FETCH
    if o["fetch_via"] != "residential":
        return direct_get
    from knox_condemnations import http_get

    def get(url: str, timeout: int = FETCH_TIMEOUT_S) -> bytes:
        def direct(u):
            try:
                return direct_get(u, timeout)
            except Exception:
                if refused is not None:
                    refused.append(u)
                raise
        return http_get(url, timeout, direct=direct)
    return get


def fetch_documents(ctx, o: dict, arc: Path, today: date) -> dict:
    """Read the Trustee's tax sale page and archive every document it links that is new or due a recheck,
    with knox_tax_sale.capture (the archive's own code). Every failure is a problem said with its reason:
    a page that cannot be read otherwise looks exactly like "no sale was held"."""
    refused: list = []
    try:
        import knox_tax_sale
        Path(arc).mkdir(parents=True, exist_ok=True)
        got = knox_tax_sale.capture(str(arc), http_get=getter(o, refused), today=today.isoformat(), page=o["page"] or None)
    except Exception as exc:  # noqa: BLE001  (the run still reads whatever archive the box already holds)
        got = {"new": [], "docs": {}, "problems": ["the capture stopped (%s: %s)" % (type(exc).__name__, str(exc)[:100])]}
    for p in got.get("problems") or []:
        ctx.out.problem("Knox tax sale documents: %s" % p)
    if refused:
        # A refusal the residential way could not clear is a problem above; this line only counts them.
        ctx.out.line("The Trustee's site refused this box's plain request %d time%s; with SCRAPFLY_KEY set, %s tried "
                     "again from a residential address (Scrapfly credits)." % (
                         len(refused), "" if len(refused) == 1 else "s", "it was" if len(refused) == 1 else "each was"))
    if any("page could not be read" in p for p in got.get("problems") or []):
        ctx.out.line("If the Trustee's site refuses this box, fetch_via: residential sends a refused request through "
                     "Scrapfly (SCRAPFLY_KEY on the box); otherwise capture on a workstation with python "
                     "src/knox_tax_sale.py --capture-only --scratch <folder> and copy that folder to archive_dir.")
    new = got.get("new") or []
    if new:
        ctx.out.line("%d new document%s on the Knox Trustee's tax sale page: %s" % (
            len(new), "" if len(new) == 1 else "s", "; ".join(str(d.get("label") or "")[:60] for d in new[:4])))
    return got


# ---------------------------------------------------------------------- the record
# Street words the shared matcher does not shorten (it knows Street, Avenue, Road and the like),
# in the USPS form DataSift stores. The report prints "Pk" for Pike, which otherwise never matches
# a record stored as "Example Pike". Both sides go through the same map, so a name word that is
# also a type ("Ridge View") reads the same on both.
STREET_FORMS = {"PK": "PIKE", "PKE": "PIKE", "AV": "AVE", "TERR": "TER", "TRAIL": "TRL", "COVE": "CV", "CROSSING": "XING",
                "SQUARE": "SQ", "BEND": "BND", "HOLLOW": "HOLW", "RIDGE": "RDG", "POINT": "PT", "VIEW": "VW", "ALLEY": "ALY",
                "CRESCENT": "CRES", "EXTENSION": "EXT", "GROVE": "GRV", "HEIGHTS": "HTS", "MANOR": "MNR", "PLAZA": "PLZ",
                "VALLEY": "VLY"}


def match_form(street: str) -> str:
    return " ".join(STREET_FORMS.get(t, t) for t in re.sub(r"[^A-Z0-9 ]", " ", str(street or "").upper()).split())


def _county_key(c) -> str:
    return re.sub(r"\s+county$", "", str(c or "").strip().lower())


def find_record(crm, street: str, county: str = "", zip5: str = "") -> tuple[str, str]:
    """(record uuid, "") or ("", why). The documented records search on the house number and the
    first street word (DataSift standardizes the street on ingest, so the raw line misses), then a
    candidate counts only with the same house number and street, the same ZIP when both carry one,
    and the same county when the row carries one. Two candidates is no match, never a guess."""
    from .sources import same_street
    toks = (street or "").split()
    if len(toks) < 2 or not toks[0].isdigit():
        return "", "no house number on the report"
    _, rows = crm.search({"property_type": "clean", "search": "address_prefix:%s %s" % (toks[0], toks[1])}, limit=25)
    hits = []
    for row in rows or []:
        a = row.get("address") or {}
        z = str(a.get("zip5") or a.get("postal_code") or "")[:5]
        c = _county_key(a.get("county"))
        if not same_street(match_form(street), match_form(a.get("street"))):
            continue
        if zip5 and z and z != zip5:
            continue
        if county and c and c != _county_key(county):
            continue
        if row.get("uuid") and row["uuid"] not in hits:
            hits.append(row["uuid"])
    if not hits:
        return "", "not in the account"
    if len(hits) > 1:
        return "", "two or more records match the address"
    return hits[0], ""


def county_parcel(pin: str, county: str = "knox") -> dict | None:
    """{"address", "owner"} for exactly this parcel on the county's tax roll, or None. The roll's
    search is fuzzy and ranked, so a result counts only when its own id is the one asked for. The
    same client the auction stage uses for its deed check (Knox and Blount run one vendor). Never
    run against the live roll from this module while it was built (no network), so the first live
    lookups are the ones to watch."""
    from .auctiontasks import COUNTY_ROLLS, ROLL_AGENT, ROLL_TIMEOUT_S
    base = COUNTY_ROLLS.get(_county_key(county))
    if not base:
        raise LookupError("no county roll is wired for %s" % (county or "this county"))
    url = "%s/parcels/%s?detail_level=public&start=0&length=10" % (base, urllib.parse.quote(pin))
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ROLL_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=ROLL_TIMEOUT_S) as r:
            parcels = (json.loads(r.read().decode() or "{}") or {}).get("parcels") or []
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise OSError("the county roll answered HTTP %s" % exc.code)
    want = pin_key(pin)
    for p in parcels:
        if any(pin_key(p.get(k)) == want for k in ("parcel_id", "parcel_number", "account_number", "pin") if p.get(k)):
            return {"address": str(p.get("parcel_address") or ""), "owner": str(p.get("owner") or "")}
    return None


def former_owner(owner: dict, report_owner: str) -> bool:
    """Is the record's owner the defendant the report names? The auction stage's rule: surname and
    first name, the report printing LAST FIRST MIDDLE, heirs and co-owners in the same cell."""
    from .auctiontasks import owner_matches
    ow = owner or {}
    return owner_matches(str(ow.get("first_name") or ""), str(ow.get("last_name") or ""), str(ow.get("company") or ""),
                         report_owner)


def is_company(owner: dict) -> bool:
    """People only (Ty, 2026-11-14): the shared test, which also counts a trust."""
    from deep_prospect_batch import is_entity
    return is_entity(owner or {})


# ---------------------------------------------------------------------- the research queue's side
def window_of(record_uuid: str, today: date) -> dict | None:
    """The open redemption window on a record, or None (none on file, or it has closed). The research
    queue ranks on this, so a closed window can never put a record at the top."""
    info = ledger.fact(record_uuid, FACT)
    if not isinstance(info, dict) or not iso_day(info.get("until")):
        return None
    return info if date.fromisoformat(iso_day(info["until"])) >= today else None


def reason(info: dict, today: date) -> str:
    """The queue's reason in the digest's words."""
    until = date.fromisoformat(iso_day(info["until"]))
    left = (until - today).days
    return "tax sale on %s, the owner can redeem until %s (%d day%s)" % (
        nice(iso_day(info.get("sale")) or until), nice(until), left, "" if left == 1 else "s")


def research_reads_tag(pol, tag: str) -> bool:
    """Does any research queue in the playbook read this tag? A tag nobody reads queues nothing."""
    for s in (getattr(pol, "stages", None) or {}).values():
        if getattr(s, "kind", "") == "researchqueue" and getattr(s, "mode", "off") != "off":
            if tag.strip().lower() in {str(t).strip().lower() for t in _list(s.get("scope_tags"))}:
                return True
    return False


# ---------------------------------------------------------------------- the buyers
def _registry_rules():
    from dispo_buyers import _is_entity, local_tier, norm_name
    return norm_name, _is_entity, local_tier


def government(name: str, o: dict, norm) -> bool:
    """The county striking a parcel to itself is not a buyer (the 2026 sale's only overlap with the
    registry was Knox County)."""
    n = norm(name)
    return bool(GOV_RX.search(n)) or any(n == norm(g) or n.startswith(norm(g) + " ") for g in o["government_bidders"])


def buyer_entries(sold: list[tuple[str, dict]], today: date, o: dict) -> list[dict]:
    """One registry entry per winning bidder, in the registry's own shape (dispo_buyers.phase_aggregate
    writes the same keys), with the parcels they bought. Every tax sale purchase is paid in cash."""
    norm, is_ent, tier_of = _registry_rules()
    groups: dict[str, list] = {}
    for sale, r in sold:
        name = " ".join(str(r.get("bidder") or "").split())
        if not name or government(name, o, norm):
            continue
        groups.setdefault(norm(name), []).append((sale, r, name))
    out = []
    for key, items in groups.items():
        raw = Counter(n for _, _, n in items).most_common(1)[0][0]
        bids = sorted(float(r["bid"]) for _, r, _ in items if float(r.get("bid") or 0) > 0)
        days = sorted(s for s, _, _ in items)
        last = days[-1]
        tier, why, _conf = tier_of(key, len(items))
        buys = [{"address": r.get("address") or "", "date": s, "price": r.get("bid"), "county": o["county"], "txn": "tax_sale",
                 "role": "tax_sale_bidder", "parcel": r.get("pin")} for s, r, _ in sorted(items, key=lambda x: x[0], reverse=True)]
        out.append({
            "buyer_key": key, "name": raw, "aliases": [], "is_entity": bool(is_ent(raw)), "mail": "",
            "counties": [o["county"]], "txn_types": ["tax_sale"], "n_buys": len(items), "n_held": 0, "n_exited": 0,
            "cash_n": len(items), "price_min": bids[0] if bids else None, "price_med": bids[len(bids) // 2] if bids else None,
            "price_max": bids[-1] if bids else None, "portfolio_n": None, "portfolio_value": None, "saved_uuid": None,
            "secondary": [], "tier": tier, "tier_reason": why, "buys": buys[:25],
            "parcels": sorted({str(r.get("pin")) for _, r, _ in items}),
            "active_365d": last >= (today - timedelta(days=365)).isoformat(),
            "active_182d": last >= (today - timedelta(days=182)).isoformat(),
            "n_in_window": sum(1 for d in days if d >= (today - timedelta(days=365)).isoformat()),
            "last_active_buy": last, "source": "knox_tax_sale", "tax_sale_buys": len(items),
            "tax_sale_spend": round(sum(bids), 2)})
    out.sort(key=lambda b: (-b["n_buys"], b["buyer_key"]))
    return out


def merge_buyers(registry: list, extra: list) -> list:
    """The registry with the tax sale bidders folded in by buyer_key, for dispo_buyers.phase_aggregate
    to call after its recency join. A new buyer is added as it is. A known buyer gains the purchases,
    the cash count, the county and the tax_sale type, and is active when the sale was recent; its
    price band is left alone, because a tax sale bid buys an encumbered parcel at auction and would
    pull a flipper's band below what they pay for a house. Folding the same file in twice changes
    nothing (the earlier tax sale counts are taken back out first)."""
    out = [dict(b) for b in registry or []]
    by = {b.get("buyer_key"): b for b in out}
    for e in extra or []:
        b = by.get(e.get("buyer_key"))
        if b is None:
            b = dict(e)
            out.append(b)
            by[b.get("buyer_key")] = b
            continue
        old = int(b.get("tax_sale_buys") or 0)
        n = int(e.get("n_buys") or 0)
        b["n_buys"] = int(b.get("n_buys") or 0) - old + n
        b["cash_n"] = int(b.get("cash_n") or 0) - old + n
        b["txn_types"] = sorted(set(b.get("txn_types") or []) | {"tax_sale"})
        b["counties"] = sorted(set(b.get("counties") or []) | set(e.get("counties") or []))
        theirs = [x for x in (b.get("buys") or []) if x.get("txn") != "tax_sale"]
        b["buys"] = sorted(theirs + list(e.get("buys") or []), key=lambda x: str(x.get("date") or ""), reverse=True)[:25]
        b["tax_sale_buys"], b["tax_sale_spend"], b["parcels"] = n, e.get("tax_sale_spend"), list(e.get("parcels") or [])
        if e.get("active_365d"):
            b["n_in_window"] = int(b.get("n_in_window") or 0) - int(b.get("tax_sale_in_window") or 0) + int(e.get("n_in_window") or 0)
            b["tax_sale_in_window"] = int(e.get("n_in_window") or 0)
            b["active_365d"] = True
            b["last_active_buy"] = max(str(b.get("last_active_buy") or ""), str(e.get("last_active_buy") or ""))
        if e.get("active_182d"):
            b["active_182d"] = True
    return out


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------- the note
def note_text(r: dict, sale: str, start: date, until: date, p: tuple, src: str, from_order: bool, money_ok: bool) -> str:
    parts = ["Knox County tax sale on %s, parcel %s." % (nice(sale), r["pin"])]
    if money_ok:
        parts.append("Sold for ${:,.0f}; excess proceeds on the Trustee's report ${:,.0f}.".format(float(r["bid"]), float(r["excess"])))
    frm = ("the order confirming the sale on %s" % nice(start) if from_order else
           "the sale date (the date the court confirmed the sale is not on file, so the real window ends a few weeks later)")
    how = "the period printed on the Trustee's report" if src == "report" else "the period the statute sets for the years delinquent"
    parts.append("The owner can redeem until %s: %s from %s, %s (Tenn. Code Ann. 67-5-2701)." % (
        nice(until), period_words(p), frm, how))
    parts.append("Buying the owner's redemption right must clear the profiteering rule in 67-5-2701(b)(2): "
                 "check with a lawyer before offering one.")
    return " ".join(parts)


# ---------------------------------------------------------------------- files for a person
LIST_COLUMNS = ["Redeem by", "Days left", "Address", "Former owner (report)", "Parcel", "Sale date", "Window counted from",
                "Period", "Winning bid", "Excess proceeds", "What happens", "DataSift record"]


def write_list(path: Path, open_windows: list[dict], fate: dict) -> Path:
    """Every open window in one file, nearest deadline first: the person's list. The money is shown only
    when the document's rows added up to its printed totals (the note's own rule), and a record link only
    where the row was matched to a record. Nothing here contacts anyone: the lawyer line is in each note."""
    from datasift_links import record_url
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(LIST_COLUMNS)
        for w in sorted(open_windows, key=lambda x: (x["until"], x["r"]["pin"])):
            r, s = w["r"], w["s"]
            what, uuid_ = fate.get((w["sale"], w["key"]), ("not looked at this run", ""))
            money = s.get("check") == "confirmed"
            frm = ("the order of %s" % nice(w["start"])) if w["order"] else "the sale date (the order's date is not set)"
            out.writerow([w["until"].isoformat(), w["left"], (r.get("address") or "").title(),
                          " ".join([(r.get("owner") or ""), (r.get("wrapped") or "")]).strip().title(), r["pin"], w["sale"],
                          frm, "%s (%s)" % (period_words(w["p"]), "printed on the report" if w["src"] == "report" else "the statute"),
                          ("%.2f" % float(r["bid"])) if money else "", ("%.2f" % float(r["excess"])) if money else "",
                          what, record_url(uuid_)])
    return Path(path)


def files_dir(ctx) -> Path:
    """RUNS_DIR/<stage>-<task id>/files, which the brain serves by a signed link (Summary.run_dir)."""
    d = Path(config.RUNS_DIR) / ("%s-%s" % (ctx.stage.id, ctx.task_id or "cli")) / "files"
    d.mkdir(parents=True, exist_ok=True)
    ctx.out.run_dir = str(d.parent)
    return d


def _link(ctx, path: Path) -> str:
    if ctx.task_id:
        from .. import slack
        url = slack.file_url(int(ctx.task_id), path.name)
        if url:
            return url
    return path.name


# ---------------------------------------------------------------------- the stage
def _stale(stamp: str, now: datetime, days: int) -> bool:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() > days * 86400
    except (TypeError, ValueError):
        return True


def run_taxsale(ctx) -> None:
    st = ctx.stage
    o = options(st)
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    stamp = ctx.now.isoformat(timespec="seconds")
    arc = archive_dir(o)
    got = fetch_documents(ctx, o, arc, today) if o["fetch"] else None
    docs = load_docs(arc)
    if docs is None:
        if got is not None:
            # The capture's problems say why; the head must not read like a quiet day.
            ctx.out.head = "No Knox tax sale results could be read: the Trustee's page was not captured on this box."
            return
        ctx.out.head = "No Knox tax sale results have reached this box yet."
        ctx.out.line("Set fetch: true to read the Trustee's page on this box, or copy an archive to archive_dir.")
        if ctx.live:
            ctx.out.problem("there is no Knox tax sale archive on this box, so no results were read (archive_dir)")
        return
    results = sorted(((sha, d) for sha, d in docs.items() if isinstance(d, dict) and d.get("kind") == "results"),
                     key=lambda kv: (str(kv[1].get("first_seen") or ""), kv[0]))
    if not results:
        if got is not None and got.get("problems"):
            ctx.out.head = "No Knox tax sale results were read this run: the Trustee's page was read, its documents were not."
        else:
            ctx.out.head = "No tax sale results have been posted yet; the Trustee posts them after each sale."
        return

    # ---- read every results document once (a reading that could not be made is tried again)
    sales, fresh = [], []
    for sha, d in results:
        s = load_sale(sha)
        if s is None or s.get("status") == "unreadable":
            s = parse_doc(sha, d, arc, ctx.now)
            save_sale(s)
            if s["status"] in ("table", "broken"):
                fresh.append(sha)
        else:
            rejudge(s)
        # The page's date for this document's sale can arrive after the document was read.
        s["page_sale"] = str(d.get("sale_date_page") or s.get("page_sale") or "")
        sales.append(s)
    tables = [s for s in sales if s.get("status") in ("table", "broken")]
    for s in sales:
        if s.get("status") == "unreadable":
            ctx.out.problem("the Knox tax sale results could not be read on this box: %s" % s.get("why"))
        elif s.get("status") == "broken":
            ctx.out.problem("the Knox tax sale results in %s did not add up to the printed totals, so some sales may be "
                            "missing (%d rows read)" % (s.get("file") or s.get("label"), len(s.get("rows") or [])))
    if not tables and any(s.get("expects_table") and s.get("status") == "no_table" for s in sales):
        ctx.out.problem("the Knox tax sale results hold no readable table (a scanned image, or the layout changed); "
                        "a text copy beside the PDF is read instead")
    for day, s in decrees(sales, o):
        if day and day not in o["confirmed"]:
            # The window runs from the order. The decree is a scanned image, so a person reads its date once.
            ctx.out.problem("the Trustee posted the decree confirming the Knox tax sale of %s (%s); read the date the "
                            "court entered it and set `confirmed` on the stage, so each window runs from the order"
                            % (nice(day), s.get("file") or s.get("label")))

    # ---- every sold parcel once: a later document for the same sale replaces an earlier one
    rows: dict = {}
    for s in tables:
        sale = sale_day(s, o)
        if not sale:
            ctx.out.problem("the sale date could not be read from the tax sale results %s; set `sale_dates` on the "
                            "stage by that file name" % (s.get("file") or s.get("label")))
            continue
        for r in s.get("rows") or []:
            rows[(sale, r["pin_key"])] = (s, r, sale)
    ctx.out.found = len(rows)

    tag = str(o["tag"] or TAG)
    tag_ok = bool(ctx.crm.tag_uuid(tag))
    wired = research_reads_tag(ctx.policy, tag)
    county = str(o["county"] or "")
    min_left = int(o["min_days_left"])
    work, from_sale, by_sale = [], set(), {}
    for (sale, key), (s, r, _) in rows.items():
        order = o["confirmed"].get(sale) or ""
        start = date.fromisoformat(order or sale)
        if not order:
            from_sale.add(sale)
        info = by_sale.setdefault(sale, {"sold": 0, "open": 0, "soonest": None, "last": None})
        info["sold"] += 1
        p, src = row_period(r, start, o)
        if not p:
            ctx.out.tally["no redemption period on the row"] += 1
            continue
        until = end_of(start, p)
        left = (until - today).days
        if left >= 0:
            info["open"] += 1
            info["soonest"] = min(info["soonest"] or until, until)
        info["last"] = max(info["last"] or until, until)
        work.append({"s": s, "r": r, "sale": sale, "start": start, "order": bool(order), "p": p, "src": src,
                     "until": until, "left": left, "key": key})
    work.sort(key=lambda w: (w["left"] < 0, w["left"]))     # the open windows that close soonest get the reads first

    reads = rolls = 0
    to_tag, untagged, listed, held = [], [], [], Counter()
    fate: dict = {}         # (sale, parcel) -> (what happens to an open window, the record it is on), for the list
    for w in work:
        s, r, key = w["s"], w["r"], w["key"]
        res = (s.setdefault("resolved", {})).get(key) or {}
        uuid_ = res.get("record") or ""
        wkey = (w["sale"], key)
        if w["left"] < 0:
            # Closed: the right has ended. Only a tag this stage put on comes off.
            mark = ledger.fact(uuid_, MARK) if uuid_ else None
            if isinstance(mark, dict) and not mark.get("removed"):
                ctx.change(uuid_, "remove_tags", [tag], [])
                ctx.write("tax sale window closed", ctx.crm.remove_tags, uuid_, [tag])
                if o["note"]:
                    ctx.write("tax sale window closed note", ctx.crm.message, uuid_,
                              "The tax sale redemption window on parcel %s closed on %s, so the %s tag came off." % (
                                  r["pin"], nice(w["until"]), tag))
                if ctx.live:
                    ledger.set_fact(uuid_, MARK, dict(mark, removed=stamp))
                    ledger.event(ctx.run_id, st.id, uuid_, "taxsale_untag", {"parcel": r["pin"], "until": str(w["until"])},
                                 now=ctx.now, tz=ctx.policy.tz)
                untagged.append(w)
            ctx.out.tally["redemption window closed"] += 1
            continue
        cached = str(res.get("verdict") or "")
        if cached in ("tagged", "had the tag") and uuid_:
            ctx.out.tally["already tagged"] += 1
            fate[wkey] = ("carries the %s tag: held from outreach, queued for research" % tag, uuid_)
            continue
        if cached and not _stale(res.get("verdict_at") or "", ctx.now, int(o["recheck_days"])) \
                and (cached in ("listed", "held") or (cached == "tag" and not ctx.live)):
            # A verdict from this week stands instead of opening the record again. A live run still opens
            # a record it is about to tag, because the gate runs when a record is picked up.
            if cached == "tag":
                to_tag.append(dict(w, uuid=uuid_))
                fate[wkey] = ("would be tagged %s for research" % tag, uuid_)
            elif cached == "listed":
                listed.append(dict(w, why=res.get("why") or ""))
                fate[wkey] = ("for a person: %s" % (res.get("why") or "listed"), uuid_)
            else:
                held[res.get("why") or "held back"] += 1
                fate[wkey] = (res.get("why") or "held back", uuid_)
            ctx.out.tally[{"tag": "would tag (checked earlier this week)", "listed": "listed for a person",
                           "held": "held back (checked earlier this week)"}[cached]] += 1
            continue
        if reads >= int(o["max_records"]):
            ctx.out.tally["left for the next run (%d records a run)" % int(o["max_records"])] += 1
            fate[wkey] = ("not looked up yet: the next run reads it", uuid_)
            continue
        reads += 1
        why = ""
        if not uuid_ or _stale(res.get("at") or "", ctx.now, int(o["recheck_days"])):
            uuid_, why = find_record(ctx.crm, r["address"], county)
            if not uuid_ and o["roll_check"] and rolls < int(o["max_roll_lookups"]):
                rolls += 1
                try:
                    found = (lookup_parcel or county_parcel)(r["pin"], county)
                except Exception as exc:  # noqa: BLE001  (a roll that is down today may answer tomorrow)
                    found = None
                    ctx.out.tally["county roll did not answer"] += 1
                    log.info("tax sale: county roll lookup failed for %s (%s)", r["pin"], exc)
                if found and found.get("address"):
                    from .sources import same_street
                    if not same_street(match_form(r["address"]), match_form(found["address"].split(",")[0])):
                        u2, why2 = find_record(ctx.crm, found["address"].split(",")[0], county)
                        if u2:
                            uuid_, why = u2, ""
                            ctx.out.tally["found at the county roll's address"] += 1
                        else:
                            why = why2 or why
            res = {"record": uuid_, "why": why, "at": stamp}
            s["resolved"][key] = res
        if not uuid_:
            res.update(verdict="listed", verdict_at=stamp, why=why or "not in the account")
            listed.append(dict(w, why=res["why"]))
            ctx.out.tally["listed: %s" % res["why"]] += 1
            fate[wkey] = ("for a person: %s" % res["why"], "")
            continue
        rec = ctx.crm.record(uuid_)
        verdict, why = "tag", ""
        owner = (rec or {}).get("owner") or {}
        rcounty = _county_key((rec.get("address") or {}).get("county")) if rec else ""
        if not (rec or {}).get("uuid"):
            verdict, why = "listed", "the record could not be read"
        elif county and rcounty and rcounty != _county_key(county):
            verdict, why = "listed", "the only match is in another county"
        elif is_company(owner):
            verdict, why = "listed", "a company or trust owns it (people only)"
        elif not former_owner(owner, "%s %s" % (r.get("owner") or "", r.get("wrapped") or "")):
            verdict, why = "listed", "the record's owner is not the former owner on the report"
        if verdict == "tag":
            # What the stage learned about the record, kept in plan mode too: the research queue ranks on it.
            ledger.set_fact(uuid_, FACT, {"until": w["until"].isoformat(), "sale": w["sale"], "start": w["start"].isoformat(),
                                          "from": "order" if w["order"] else "sale", "parcel": r["pin"],
                                          "period": period_words(w["p"]), "source": w["src"], "bid": r.get("bid"),
                                          "excess": r.get("excess"), "doc": s.get("file") or s.get("label")})
            if lanes.status_of(rec) in lanes.LEAD_STATUSES:
                verdict, why = "listed", "a lead: the lead manager has it"
            else:
                v = ctx.verdict(rec, "research")
                if not v.ok:
                    verdict, why = "held", "held back: %s" % v.reason()
                elif w["left"] < min_left:
                    verdict, why = "listed", "too soon for research (under %d days left)" % min_left
        res.update(verdict=verdict, verdict_at=stamp, why=why)
        if verdict == "listed":
            listed.append(dict(w, why=why, uuid=uuid_))
            ctx.out.tally["listed: %s" % why] += 1
            fate[wkey] = ("for a person: %s" % why, uuid_)
            continue
        if verdict == "held":
            held[why] += 1
            ctx.out.tally[why] += 1
            fate[wkey] = (why, uuid_)
            continue
        if tag.strip().lower() in {t.strip().lower() for t in tag_titles(rec)}:
            ctx.out.tally["already carries the %s tag" % tag] += 1
            res.update(verdict="tagged" if ledger.fact(uuid_, MARK) else "had the tag")
            fate[wkey] = ("carries the %s tag: held from outreach, queued for research" % tag, uuid_)
            continue
        if not tag_ok:
            ctx.out.tally["waiting for the %s tag to exist" % tag] += 1
            if ctx.live:
                ctx.out.problem("there is no %r tag in this account, so no former owner was tagged; create it first" % tag)
            fate[wkey] = ("waiting for the %s tag to exist" % tag, uuid_)
            continue
        ctx.change(uuid_, "add_tags", [], [tag])
        ctx.write("tax sale redemption tag", ctx.crm.add_tags, uuid_, [tag], rec)
        if o["note"]:
            ctx.write("tax sale note", ctx.crm.message, uuid_,
                      note_text(r, w["sale"], w["start"], w["until"], w["p"], w["src"], w["order"], s.get("check") == "confirmed"))
        if ctx.live:
            ledger.set_fact(uuid_, MARK, {"tag": tag, "at": stamp, "parcel": r["pin"], "until": w["until"].isoformat()})
            ledger.event(ctx.run_id, st.id, uuid_, "taxsale_tag", {"parcel": r["pin"], "until": w["until"].isoformat()},
                         now=ctx.now, tz=ctx.policy.tz)
            res["verdict"] = "tagged"
        to_tag.append(dict(w, uuid=uuid_, street=addr(rec)))
        fate[wkey] = (("tagged %s for research" if ctx.live else "would be tagged %s for research") % tag, uuid_)
    for s in tables:
        save_sale(s)

    # ---- the winning bidders, for the buyer registry
    buyers_line = ""
    if o["buyers"] and rows:
        try:
            entries = buyer_entries([(sale, r) for (_, r, sale) in rows.values()], today, o)
        except Exception as exc:  # noqa: BLE001  (the registry's own rules must load, or nothing is written)
            entries = None
            ctx.out.problem("the buyer registry's rules did not load on this box, so the winning bidders were not added (%s)"
                            % str(exc)[:80])
        if entries is not None:
            reg = registry_dir(o)
            try:
                known = {b.get("buyer_key") for b in json.loads((reg / "registry.json").read_text(encoding="utf-8"))}
            except (OSError, ValueError, AttributeError):
                known = set()
            new = sum(1 for e in entries if e["buyer_key"] not in known)
            path = reg / BUYERS_FILE
            try:
                same = json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(entries, default=str))
            except (OSError, ValueError):
                same = False
            if not same:
                ctx.write("buyer registry file", _write_json, path, entries)
            if entries and not (ctx.live and same):
                # A live run that changed nothing says nothing; a plan run always says what it would add.
                buyers_line = "%s %d winning bidder%s to the buyer registry (%d not in it yet)" % (
                    "added" if ctx.live else "would add", len(entries), "" if len(entries) == 1 else "s", new)

    # ---- what the team reads
    for sha in fresh:
        s = next(x for x in sales if x["sha"] == sha)
        sale = sale_day(s, o)
        info = by_sale.get(sale) if sale else None
        if info:
            ctx.out.alert("Knox tax sale results are in: %d parcels sold on %s; %d former owners can still redeem%s."
                          % (info["sold"], nice(sale), info["open"],
                             (", the soonest by %s" % nice(info["soonest"])) if info["soonest"] else ""))
    for sale, info in sorted(by_sale.items()):
        if info["open"]:
            ctx.out.line("Knox tax sale on %s: %d parcels sold, %d can still be redeemed, the soonest window closes %s."
                         % (nice(sale), info["sold"], info["open"], nice(info["soonest"])))
    for w in to_tag[:6]:
        ctx.out.line("%s: redeem by %s (%d days)" % (w.get("street") or (w["r"]["address"] or w["r"]["pin"]).title(),
                                                    nice(w["until"]), w["left"]))
    if from_sale:
        ctx.out.line("The windows are counted from the sale date: the date the court confirmed the sale is not in the "
                     "settings (confirmed), so each closes a few weeks before the real one.")
    if to_tag and not wired:
        ctx.out.line("The research queue does not read the %s tag yet (its scope_tags), so tagged owners wait for a person."
                     % tag)
        if ctx.live:
            ctx.out.problem("the research queue does not read the %s tag, so the former owners tagged here are not researched" % tag)
    if not tag_ok and to_tag:
        ctx.out.line("The %s tag does not exist in this account yet; create it before this stage goes live." % tag)
    open_ = [w for w in work if w["left"] >= 0]
    if open_:
        f = write_list(files_dir(ctx) / LIST_FILE, open_, fate)
        ctx.out.line("Every open window, nearest deadline first (%d need a person): %s" % (len(listed), _link(ctx, f)))

    opened = sum(i["open"] for i in by_sale.values())
    ctx.out.acted = len(to_tag) + len(untagged)
    if to_tag:
        parts = ["%s %d former owner%s who can still redeem their Knox tax sale house, for research" % (
            "Tagged" if ctx.live else "Would tag", len(to_tag), "" if len(to_tag) == 1 else "s")]
    elif opened:
        parts = ["%d former owner%s can still redeem their Knox tax sale house; none %s tagged this run" % (
            opened, "" if opened == 1 else "s", "was" if ctx.live else "would be")]
    else:
        last = max((i["last"] for i in by_sale.values() if i["last"]), default=None)
        parts = ["No former owner from the Knox tax sales can still redeem%s" % (
            (" (the last window closed %s)" % nice(last)) if last and last < today else "")]
    if untagged:
        parts.append("%s the tag off %d whose window closed" % ("took" if ctx.live else "would take", len(untagged)))
    if listed:
        parts.append("%d listed for a person" % len(listed))
    if held:
        parts.append("%d held back by the gate" % sum(held.values()))
    if buyers_line:
        parts.append(buyers_line)
    ctx.out.head = "; ".join(parts) + "."
