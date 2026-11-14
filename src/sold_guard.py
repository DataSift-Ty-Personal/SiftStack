"""Sold guard: a record is Sold only when the house sold AFTER it became a lead.

What went wrong (measured 2026-11-12 on ty+2). Two SiftMap presets ("Knox - Recently Sold",
"Blount - Recently Sold") ran every night in in-account mode with a FIXED floor,
`extra_last_sale_date_min: 2023-01-01`, and tagged every match `recently sold`; two sequences then
set the status to Sold. A fixed floor never moves, so "recently" had grown to 46 months: any owner
who bought since January 2023 was marked Sold the night after the record arrived. On a fresh lead
that "sale" is the current owner's own purchase. On top of that, one backlog pull on 2026-06-25
created about 24,600 records for houses that had already sold; every later lead at one of those
addresses attached to a record that was already Sold.

The rule here is the one the SiftMap filter cannot express: compare the last sale with the day
the record became a lead.

    lead day   the day the record was created, or, for a record born with the sold tag, the day
               it first joined one of our lead lists
    floor      the earliest dated distress on the record (notice month, lien filing quarter,
               obituary date; a probate notice reaches back a year), never later than the lead day
    last sale before the floor          the owner's own purchase: tag off, status back to what it was
    last sale on or after the lead day  a real sale: tag on, status Sold
    in between                          cannot be told apart (it may have sold at its own auction
                                        before we uploaded the notice): left alone and counted
    dated within 120 days before the lead day and NOT on the record at birth
                                        learned late, so the owner we hold may be the seller:
                                        left alone and counted

The deed decides when there is one (2026-11-14). The dates above cannot tell an owner's own
purchase from a sale. The owner found five records the last preset run tagged, all uploaded by us
the day before, and on every one the newest county deed names the owner we hold as the BUYER: a
lien bought inside its filing quarter kept the tag (a live lead hidden from every preset), and one
record came back only because DataSift's last_sold was two months stale. So each record judged
here has SiftMap's newest deed read (a bounded number a night, cached), and who bought decides:

    the buyer is the owner we hold      the owner's own purchase, never Sold: tag off, status back
    someone else, on or after the lead  a real sale (also after the filing, or a sale learned late
                                        whose seller is our owner): tag on, status Sold
    a shared surname only, no buyer, someone else before the lead, no real price, or an owner a
    machine copied in after the sale    left alone and listed for a person (for_a_person.csv)

Without a deed the dates decide, as above. A nightly search of SiftMap for the houses in the
account that sold in the last 45 days catches a sale within weeks; DataSift's own last_sold takes
about two months, and since the presets went off nothing else looked sooner.

    python src/sold_guard.py audit                    # read-only counts
    python src/sold_guard.py presets [--commit]       # show / switch off the nightly SiftMap tagging
    python src/sold_guard.py repair [--limit N] [--commit] [--include-distress]
    python src/sold_guard.py mark [--commit]          # real sales the tag missed
    python src/sold_guard.py detect [--commit]        # the nightly sale search, judged on the deed
    python src/sold_guard.py daily [--commit]         # repair + detect + mark, for the scheduler
    python src/sold_guard.py undo [--commit]          # put back every change in the log, newest first
    python src/sold_guard.py reentry [--commit] [--force]     # January: sales older than 3 years come back
    python src/sold_guard.py monthly-audit [--month YYYY-MM]  # last month's sold leads and how far we got

Dry by default. Every write is appended to output/sold_guard/changes.jsonl first-read-then-write,
so `undo` can restore the exact prior tag and status.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

OUT = Path(os.environ.get("SIFTSTACK_STATE_DIR") or os.environ.get("SIFTSTACK_OUTPUT_DIR") or ROOT / "output") / "sold_guard"   # the change log must outlive a deploy
TAG = "recently sold"
# Lists a record only joins because WE pulled or uploaded it as a lead.
# Lists a record only joins because WE put it there as a lead. An UPLOAD list is written by our
# courthouse uploader, which also corrects the owner, so a later join there re-dates the lead. A
# PULL list (by id, by preset) leaves the owner alone, so it only counts for a record that was
# born in the sold backlog. Not "Dispo - Flip Buyers": an older acquisition record that joined it
# after its owner sold to an investor is a real sale for the acquisition side.
# SOLD_GUARD_EXTRA_UPLOAD_LISTS names the lists a NEW courthouse feed uploads to (comma separated),
# so a lead from that feed that lands on an old Sold record is revived like any other. Empty by
# default: a list is added here the day its feed is switched on, not before, because every name
# in this tuple widens which Sold records the daily pass is willing to put back.
_EXTRA_UPLOAD_LISTS = tuple(x.strip() for x in (os.environ.get("SOLD_GUARD_EXTRA_UPLOAD_LISTS") or "").split(",")
                            if x.strip())
UPLOAD_LISTS = ("Foreclosure", "Probate", "Obituary", "Liens", "Condemned", "Pre-Foreclosure",
                "Trustee Deed") + _EXTRA_UPLOAD_LISTS
PULL_LISTS = ("Dispo - VIP Buyers", "D4D Top 3000", "Knox Blount - Top 15K DPD", "Dream 100",
              "Land - Builder Lots", "Land - Entitlement Tracts")
OUR_LISTS = UPLOAD_LISTS + PULL_LISTS
# Lists DataSift attaches by itself when the CURRENT owner shows distress (opt in: --include-distress).
DISTRESS_LISTS = ("Tax Delinquent", "Vacant", "Pre-Foreclosures - Notice of Default", "Pre-Foreclosures - Lis Pendens",
                  "Pre-Foreclosures - Notice of Foreclosure", "Pre-Foreclosures - Final Judgement", "Bankruptcy",
                  "Tired Landlord", "Pre-Probate/Deceased", "Estate Sales", "Estate and Heirs", "Judgments", "Divorce")
AUTOMATION = {"sequence", "bulk"}                 # a status change from either was not a person's call
MIN_SALE_USD = 10000.0                            # below this a transfer is a family deed, not a sale
DATA_LAG_DAYS = 120                               # how long a closed sale can take to reach the record
MARK_LOOKBACK_DAYS = 400
# The two owner events are the app's own "Owner" activity group; they show whether a machine copied
# a new owner onto the record after a sale (see owner_replaced). One read carries all five.
LOG_EVENTS = ("property.created,property.status.updated,property.lists.added,"
              "property.owner.replaced,owner.updated")


def _setting(name: str, default: int) -> int:
    """A whole-number setting. A value that does not read as one keeps the default instead of
    stopping the first-to-market run at import."""
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# The deed. SiftMap detail reads are what acceptable use watches: these are houses already in the
# account, read in a bounded slice a night and cached, never a sweep.
DEED_READS = _setting("SOLD_GUARD_DEED_READS", 150)         # SiftMap deed reads a run, all passes together
DETECT_RESERVE = _setting("SOLD_GUARD_DETECT_RESERVE", 50)  # of those, kept back for the nightly sale search
DETECT_DAYS = _setting("SOLD_GUARD_DETECT_DAYS", 45)        # how far back the nightly sale search looks
DEED_TTL_DAYS = _setting("SOLD_GUARD_DEED_TTL_DAYS", 7)     # a deed read is reused this long
DEED_MATCH_DAYS = 45            # a last_sold this close after the deed's date is the same sale
SIFTMAP_GAP_S = 0.75            # 80 a minute against SiftMap's own counter of 120 a minute
SEARCH_PAGE = 250
DETECT_MAX_ROWS = 5000          # per county: a slice this big means the date floor is not biting
COUNTIES = tuple(tuple(x.strip() for x in c.split(":"))
                 for c in (os.environ.get("SOLD_GUARD_COUNTIES") or "47093:Knox:TN,47009:Blount:TN").split(",")
                 if c.count(":") == 2)
# Sources that copy the data provider's current owner onto a record. After a sale such a copy makes
# the CRM owner the BUYER, so a deed naming the owner proves nothing. Our uploader (internal-api)
# and a person (web) choose the owner on purpose and do not count.
OWNER_COPY_SOURCES = {"bulk", "auto-update", "data-enriching"}
SOURCE_LABEL = {"bulk": "a bulk add", "auto-update": "DataSift's own refresh", "data-enriching": "data enrichment"}


# ------------------------------------------------------------------- the rule --

def _day(value) -> str:
    return str(value)[:10] if value else ""


def born_with_tag(events: list[dict]) -> bool:
    for e in events:
        if e.get("event_type") == "property.created":
            pl = e.get("payload") or {}
            tags = pl.get("tags") or (pl.get("property") or {}).get("tags") or []
            return TAG in [str(t).lower() for t in tags]
    return False


def lead_day(events: list[dict], lead_lists) -> str:
    """The day the record became the lead it is today, or '' when it never was one (a sold house
    nobody pulled). A later courthouse upload re-dates it: the notice is about whoever owns it now."""
    def joins(names):
        want = {x.lower() for x in names}
        return [_day(e.get("timestamp")) for e in events if e.get("event_type") == "property.lists.added"
                and want & {str(x).lower() for x in ((e.get("payload") or {}).get("lists") or [])}]
    created = min((_day(e.get("timestamp")) for e in events if e.get("event_type") == "property.created"), default="")
    if born_with_tag(events):
        return max(joins(lead_lists), default="")
    uploads = [x for x in lead_lists if x in UPLOAD_LISTS or x in DISTRESS_LISTS]
    return max([created] + joins(uploads)) if created else max(joins(uploads), default="")


MONTH_RX = re.compile(r"^(20\d\d)-(\d\d)$")              # the notice month tag on a courthouse record
FILED_RX = re.compile(r"^filed_(20\d\d)-q([1-4])$")        # the county filing quarter on a lien record
PROBATE_LOOKBACK_DAYS = 365                               # an estate can sell the house before the notice runs


def evidence_floor(rec: dict, lead: str) -> str:
    """The earliest dated sign of distress on the record, never later than the lead day. We often
    upload a notice weeks after it ran, and a house can sell at its own auction in between: only a
    sale OLDER than this line is safely the current owner's own purchase."""
    tags = [t.lower() for t in tag_names(rec)]
    days = [lead] if lead else []
    months = sorted(t for t in tags if MONTH_RX.match(t))
    if months:
        first = date(int(months[0][:4]), int(months[0][5:7]), 1)
        if "probate" in tags:
            first -= timedelta(days=PROBATE_LOOKBACK_DAYS)
        days.append(first.isoformat())
    for t in tags:
        m = FILED_RX.match(t)
        if m:
            days.append("%s-%02d-01" % (m.group(1), (int(m.group(2)) - 1) * 3 + 1))
    obit = _day(rec.get("last_obituary_date"))
    if obit:
        days.append(obit)
    return min(days, default="")


def birth_sold(events: list[dict]) -> str:
    """The last sale the record carried the moment it was created ('' when it carried none)."""
    for e in events:
        if e.get("event_type") == "property.created":
            return _day(((e.get("payload") or {}).get("property") or {}).get("last_sold"))
    return ""


def verdict(lead: str, last_sold: str, floor: str = "", born_sold: str = "") -> str:
    if not lead:
        return "not a lead"
    if not last_sold:
        return "no sale date"
    if last_sold >= lead:
        return "sold since"
    if floor and floor < lead and last_sold >= floor:
        return "sold between the filing and our pull"
    # Deeds reach the data weeks after closing. A sale dated shortly before the record arrived and
    # NOT on it at birth was learned later, so the owner we hold may be the seller: do not revive.
    recent = (date.fromisoformat(lead) - timedelta(days=DATA_LAG_DAYS)).isoformat()
    if last_sold != born_sold and last_sold >= recent:
        return "sold just before we pulled it"
    return "own purchase"


def sold_flip(events: list[dict]) -> dict | None:
    """The newest status change that landed on Sold: what it replaced and who made it."""
    flips = []
    for e in events:
        if e.get("event_type") != "property.status.updated":
            continue
        ch = ((e.get("payload") or {}).get("property") or {}).get("status")
        if isinstance(ch, list) and len(ch) == 2 and str(ch[1] or "").lower() == "sold":
            flips.append({"at": str(e.get("timestamp") or ""), "from": ch[0], "source": str(e.get("source") or "")})
    return max(flips, key=lambda f: f["at"]) if flips else None


# ------------------------------------------------------------------ names --
#
# A deed and a CRM record write one person several ways: "SMITH JOHN A & MARY B", "John A Smith",
# "JOHN A SMITH REVOCABLE LIVING TRUST". The test is loose on form and strict on substance: the
# owner's surname AND first name (or its initial) must both be there. A surname alone is the trap
# in the fifth record the owner found: a relative buying the family house shares the name and is
# not the owner we hold.

_NOISE = frozenset((
    "JR SR II III IV MR MRS MS DR AKA FKA NKA THE OF TRUSTEE TRUSTEES TR TRS TTEE TTEES TRUST "
    "REVOCABLE REV LIVING IRREVOCABLE FAMILY ESTATE EST DECEASED DEC HEIRS LIFE JTWROS JT TEN TENANTS "
    "COMMON ENTIRETY HW HUSBAND WIFE SURVIVOR SURVIVORSHIP AS FOR DATED DTD AGREEMENT DECLARATION "
    "ETAL ETUX ETVIR").split())
_LEGAL = frozenset("LLC INC CORP CORPORATION CO COMPANY LP LLP LTD PLLC PC LC".split())
# Words too common in company names to tie two companies together on their own.
_GENERIC = frozenset((
    "HOMES HOME PROPERTIES PROPERTY INVESTMENTS INVESTMENT INVESTORS HOLDINGS HOLDING GROUP REALTY "
    "REAL CAPITAL PARTNERS VENTURES ENTERPRISES MANAGEMENT SERVICES FUND BUILDERS CONSTRUCTION "
    "DEVELOPMENT SOLUTIONS").split())


def _tokens(text) -> list[str]:
    """Upper-case words with the punctuation gone and "et al", "et ux", "et vir" dropped."""
    raw = "".join(c if c.isalnum() else " " for c in str(text or "").upper()).split()
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "ET" and i + 1 < len(raw) and raw[i + 1] in ("AL", "UX", "VIR"):
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return out


def _parties(text) -> list[list[str]]:
    """The people or companies one deed name holds, split on &, ;, /, + and the word AND."""
    segs, cur = [], []
    for ch in str(text or ""):
        if ch in "&;/+":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segs.append("".join(cur))
    out = []
    for seg in segs:
        part = []
        for t in _tokens(seg):
            if t == "AND":
                if part:
                    out.append(part)
                part = []
            elif t not in _NOISE and t not in _LEGAL:
                part.append(t)
        if part:
            out.append(part)
    return out


def owner_names(rec: dict) -> list[dict]:
    """The owners the record holds: {"first": [...], "last": [...]} for a person, {"company": [...]}
    for a company. Secondary owners count: a deed to the household names either spouse."""
    out = []

    def add(first, last, company):
        f = [t for t in _tokens(first) if t not in _NOISE]
        sur = [t for t in _tokens(last) if t not in _NOISE and t not in _LEGAL]
        co = [t for t in _tokens(company) if t not in _NOISE and t not in _LEGAL and len(t) > 1]
        if sur:
            out.append({"first": f, "last": sur})
        if co:
            out.append({"company": co})

    o = rec.get("owner") or {}
    if isinstance(o, dict):
        add(o.get("first_name"), o.get("last_name"), o.get("company"))
    for s in rec.get("secondary_owners") or []:
        if isinstance(s, dict) and (s.get("first_name") or s.get("last_name") or s.get("company")):
            add(s.get("first_name"), s.get("last_name"), s.get("company"))
            continue
        words = [t for t in _tokens(s.get("full_name") if isinstance(s, dict) else s)
                 if t not in _NOISE and t not in _LEGAL]
        if len(words) >= 2:
            out.append({"first": words[:-1], "last": words[-1:]})
    return out


def _first_matches(first: list[str], given: list[str]) -> bool:
    """The record's first name against one deed party's given names: equal anywhere, or by its
    initial or a short form in the first place (J for John, Chris for Christopher). Only the first
    place, so a wife's middle initial J never stands in for her husband John."""
    f, g = first[0], given[0]
    if f in given:
        return True
    if len(g) == 1 or len(f) == 1:
        return g[0] == f[0]
    return min(len(f), len(g)) >= 3 and (f.startswith(g) or g.startswith(f))


def _one_edit(a: str, b: str) -> bool:
    """Whether two words are at most one letter apart (one added, dropped or changed)."""
    if abs(len(a) - len(b)) > 1:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] <= 1


def _surname_near(last: list[str], parties: list[list[str]]) -> bool:
    """A surname that is on the deed only in part, run together, split apart or one letter off
    ("OBRIEN" for O'Brien, a dropped double letter). Calling that a stranger would mark a live lead
    Sold on a spelling, so it is left for a person instead."""
    seen = set()
    for p in parties:
        seen.update(p)
        seen.update(p[i] + p[i + 1] for i in range(len(p) - 1))
    forms = set(last) | {"".join(last)}
    if any(t in seen for t in forms if len(t) >= 3):
        return True
    return any(_one_edit(t, w) for t in forms if len(t) >= 5 for w in seen)


def name_match(owners: list[dict], name) -> str:
    """How a name on a deed relates to the owners we hold: "owner", "surname" (a person shares only
    the surname, or something close to it), "partial" (a company shares a distinctive word, not its
    name), "stranger", "nobody" (the deed names no one) or "no owner" (no name to test). The two
    costly mistakes point opposite ways: a false "owner" revives a house that sold, a false
    "stranger" marks a live lead Sold. Anything between the two goes to a person."""
    parties = _parties(name)
    if not parties:
        return "nobody"
    if not owners:
        return "no owner"
    words = {t for p in parties for t in p}
    shared = ""
    for o in owners:
        if o.get("company"):
            core = o["company"]
            if all(t in words for t in core):
                return "owner"
            if any(t in words for t in core if len(t) > 2 and t not in _GENERIC):
                shared = shared or "partial"
            continue
        last, first = o["last"], o["first"]
        if not all(t in words for t in last):
            if _surname_near(last, parties):
                shared = shared or "surname"
            continue
        if not first:                       # a trust or an estate held under one name: the name is the test
            if any(set(p) == set(last) for p in parties):
                return "owner"
            shared = "surname"
            continue
        for p in parties:
            given = [t for t in p if t not in last]
            # A party without the surname borrows it ("SMITH JOHN & MARY", "JOHN & MARY SMITH"), but
            # only when it is a bare first name: "DOE MARY" is a person of her own.
            if len(given) == len(p) and sum(1 for t in p if len(t) > 1) > 1:
                continue
            if given and _first_matches(first, given):
                return "owner"
        shared = "surname"
    return shared or "stranger"


# ------------------------------------------------------------------- deed --

def _iso_day(value) -> str:
    """YYYY-MM-DD, or '' for anything that is not a calendar day: every date here is compared as
    text, and a day in another shape would sort as nonsense without a word of warning."""
    day = _day(value)
    try:
        return date.fromisoformat(day).isoformat() if day else ""
    except ValueError:
        return ""


def newest_deed(detail) -> dict:
    """The newest deed on a SiftMap detail as {date, price, buyer, seller}; {} when it holds none.
    sale_history comes newest first, and the latest dated row is taken anyway."""
    sh = detail.get("sale_history") if isinstance(detail, dict) else None
    rows = [s for s in sh or [] if isinstance(s, dict)]
    if not rows:
        return {}
    dated = [s for s in rows if _iso_day(s.get("sale_date"))]
    s = max(dated, key=lambda x: _iso_day(x.get("sale_date"))) if dated else rows[0]
    return {"date": _iso_day(s.get("sale_date")), "price": s.get("sale_price"),
            "buyer": str(s.get("buyer_name") or "").strip(), "seller": str(s.get("seller_name") or "").strip()}


def deed_sale_day(deed, last_sold: str) -> str:
    """The day of the sale the deed speaks for, or '' when it does not describe the record's last
    sale. A last_sold OLDER than the deed gives way to it (DataSift's own refresh runs about two
    months behind the county); one newer by more than DEED_MATCH_DAYS is a sale the deed has not
    reached, so the dates decide that one."""
    day = _day((deed or {}).get("date"))
    if not day:
        return ""
    if not last_sold or day >= last_sold:
        return day
    try:
        gap = (date.fromisoformat(last_sold) - date.fromisoformat(day)).days
    except ValueError:
        return ""
    return last_sold if gap <= DEED_MATCH_DAYS else ""


_NAME_KEYS = ("first_name", "last_name", "company", "full_name", "name")


def _renames(payload) -> bool:
    """Whether an owner.updated payload changed a name. A name key with no before-and-after pair
    counts as a change: the cautious reading, since the record is then left for a person."""
    if not isinstance(payload, dict):
        return False
    for holder in [payload] + [v for v in payload.values() if isinstance(v, dict)]:
        for k in _NAME_KEYS:
            if k not in holder:
                continue
            v = holder[k]
            if isinstance(v, list) and len(v) == 2:
                if _tokens(v[0]) != _tokens(v[1]):
                    return True
                continue
            return True
    for k in ("fields", "updated_fields", "last_updated_fields"):
        named = payload.get(k)
        if isinstance(named, list) and any(str(x) in _NAME_KEYS for x in named):
            return True
    return False


def owner_replaced(events: list[dict], since: str, after: str = "") -> dict | None:
    """The first time, on or after `since` (the deed's day) and strictly after `after` (the lead
    day), a machine copied an owner onto the record: a property.owner.replaced, or an owner.updated
    that changed a name, from a source in OWNER_COPY_SOURCES. After a real sale such a copy installs
    the buyer, so the deed then names 'the owner' on a house that really sold. A copy made before
    the record became our lead overwrote nobody we were working: our uploader sets the owner on the
    lead day, and a record born in a pull is born with its owner (measured 2026-11-14: 25 of the 30
    first reports had the sale, and so possibly the copy, before the lead)."""
    hits = []
    for e in events:
        et, src = str(e.get("event_type") or ""), str(e.get("source") or "").lower()
        day = _day(e.get("timestamp"))
        if src not in OWNER_COPY_SOURCES or day < since or (after and day <= after):
            continue
        if et == "property.owner.replaced" or (et == "owner.updated" and _renames(e.get("payload"))):
            hits.append({"at": str(e.get("timestamp") or ""), "source": src, "event": et})
    return min(hits, key=lambda h: h["at"]) if hits else None


def _money(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


PERSON_WHY = {"nobody": "the newest deed names no buyer",
              "no owner": "the record names no owner to check the deed against",
              "surname": "the buyer on the newest deed shares only the surname",
              "partial": "the buyer on the newest deed shares only part of the company name"}


GUARD_SOURCE = "internal-api"            # our key: the source the guard's own writes carry
RESTORABLE = AUTOMATION | {"sold guard"}


def guard_sold_flips(log: list[dict] | None = None) -> dict:
    """uuid -> the status the sold guard itself replaced when it set Sold, from its own change log.
    A later guard change that took the record off Sold drops it again."""
    out = {}
    for e in read_log() if log is None else log:
        u = e.get("uuid")
        if not u or e.get("error") or "status_to" not in e:
            continue
        if str(e.get("status_to") or "").lower() == "sold":
            if str(e.get("status_from") or "").lower() != "sold":
                out[u] = e.get("status_from") or None
        else:
            out.pop(u, None)
    return out


def sold_flip_of(rec: dict, events: list[dict], own_flips: dict | None = None) -> dict | None:
    """The newest flip to Sold, with the guard's own writes told apart from a person's. The guard
    sets Sold through our key, so its flips read as source internal-api, which the person-made-it
    rule treats as a person's call. Measured 2026-11-14: 53 of the 65 Sold records the first deed
    run could not touch had been set Sold by the guard itself on 2026-11-12, on the dates alone.
    When the change log says the guard made the flip, it is the guard's to undo, back to the status
    the log recorded; a person's Sold is still never undone."""
    flip = sold_flip(events)
    uuid = str(rec.get("uuid") or "")
    if own_flips and uuid in own_flips and (flip is None or flip["source"] == GUARD_SOURCE):
        return {"at": (flip or {}).get("at", ""), "from": own_flips[uuid], "source": "sold guard"}
    return flip


def needs_deed(rec: dict, events: list[dict], lead_lists, own_flips: dict | None = None) -> bool:
    """Whether a deed could change the answer. A record that was never a lead, and one a person
    marked Sold, are left alone whatever the deed says, so reading one there spends a read for
    nothing."""
    if not lead_day(events, lead_lists):
        return False
    if str(rec.get("status") or "").strip().lower() == "sold":
        flip = sold_flip_of(rec, events, own_flips)
        return flip is not None and flip["source"] in RESTORABLE
    return True


def decide(rec: dict, events: list[dict], lead_lists, deed: dict | None = None,
           own_flips: dict | None = None) -> dict:
    """What to do with one record. The repair pass hands in tagged records, the mark and detection
    passes untagged ones. When SiftMap's newest deed (`deed`, from newest_deed) speaks for the last
    sale, who bought decides; without one the dates decide, as they did before the deed was read.
    `deed` None means no deed was read; {} means SiftMap holds no sale for the house. `own_flips`
    (guard_sold_flips) lets the guard undo a Sold it set itself."""
    status = str(rec.get("status") or "").strip()
    tagged = TAG in [t.lower() for t in tag_names(rec)]
    lead, sold = lead_day(events, lead_lists), _day(rec.get("last_sold"))
    out = {"verdict": "", "lead_day": lead, "floor": evidence_floor(rec, lead), "last_sold": sold,
           "born_sold": birth_sold(events), "status": status, "action": "leave", "status_to": status,
           "tagged": tagged}
    flip = sold_flip_of(rec, events, own_flips)
    sale = deed_sale_day(deed, sold)
    if sale:
        return _by_deed(out, rec, events, deed, sale, flip)
    if deed is not None:
        out["deed"] = "older than the last sale" if deed else "none on file"
    return _by_dates(out, flip)


def _restore(out: dict, flip: dict | None) -> dict:
    """Put back the status the Sold flip replaced, unless a person made that flip."""
    if flip is None or flip["source"] not in RESTORABLE:
        out["action"], out["verdict"] = "leave", "marked sold by a person"
        return out
    out.update(action="restore", status_to=flip["from"])
    if flip["source"] == "sold guard":
        out["undoes"] = "a Sold the guard set itself"
    return out


def _by_dates(out: dict, flip: dict | None) -> dict:
    """The rule without a deed: where the last sale falls against the lead day and the floor. It
    acts only on a tagged record; an untagged one is marked on its deed or by should_mark."""
    v = verdict(out["lead_day"], out["last_sold"], out["floor"], out["born_sold"])
    out["verdict"] = v
    status = out["status"]
    if not out["tagged"]:
        return out
    if v == "sold since":
        if not status:                              # the sequence never fired on this one
            out.update(action="set sold", status_to="sold")
        return out
    if v != "own purchase":
        return out
    if status.lower() != "sold":
        out["action"] = "untag"
        return out
    return _restore(out, flip)


def _for_person(out: dict, why: str) -> dict:
    out.update(verdict=why, action="leave", person=why)
    return out


def _own(out: dict, flip: dict | None) -> dict:
    """The owner's own purchase: never Sold. Status back if automation set Sold, tag off if it is on."""
    if out["status"].lower() == "sold":
        return _restore(out, flip)
    if out["tagged"]:
        out["action"] = "untag"
    return out


def _by_deed(out: dict, rec: dict, events: list[dict], deed: dict, sale: str, flip: dict | None) -> dict:
    """The rule with a deed. Its date stands in for an older last_sold. Where that date leaves the
    answer open (on or after the lead, between the filing and the lead, or in the weeks just before
    the lead) the buyer decides, so the filing floor no longer decides alone. A deed older than all
    of that is the current owner's own purchase whoever it names: a probate deed names the
    decedent, not the personal representative we hold, and that is no reason to call a person."""
    lead = out["lead_day"]
    out.update(deed_day=deed.get("date") or "", sale_day=sale, buyer=deed.get("buyer") or "")
    zone = verdict(lead, sale, out["floor"], out["born_sold"])
    if zone == "not a lead":
        out["verdict"] = zone
        return out
    if zone == "own purchase":
        out["verdict"] = "own purchase: the newest deed is older than the filing"
        return _own(out, flip)
    owners = owner_names(rec)
    who = name_match(owners, deed.get("buyer"))
    out["match"] = who
    if who in PERSON_WHY:
        return _for_person(out, PERSON_WHY[who])
    if who == "owner":
        swap = owner_replaced(events, deed.get("date") or sale, after=lead)
        if swap:
            out["owner_copied"] = swap["at"][:10]
            return _for_person(out, "the owner was replaced by %s after the sale"
                               % SOURCE_LABEL.get(swap["source"], swap["source"]))
        out["verdict"] = "own purchase: the newest deed names the owner"
        return _own(out, flip)
    # Someone else bought it.
    if zone == "sold since":
        why = "sold since: the newest deed names someone else"
    elif zone == "sold between the filing and our pull":
        why = "sold after the filing: the newest deed names someone else"
    elif name_match(owners, deed.get("seller")) == "owner":
        why = "sold just before we pulled it: the newest deed names the owner as the seller"
    else:
        return _for_person(out, "the newest deed names someone else, dated just before the lead")
    price = _money(deed.get("price")) or (_money(rec.get("last_sale_price")) if sale == out["last_sold"] else 0.0)
    if price < MIN_SALE_USD:
        return _for_person(out, "the house changed hands with no real price")
    out["verdict"] = why
    if not out["tagged"]:
        out.update(action="mark", status_to="sold")
    elif not out["status"]:
        out.update(action="set sold", status_to="sold")
    return out


def should_mark(rec: dict, events: list[dict], lead_lists) -> str:
    """'' when an untagged record with a recent sale is a real sale since the lead; else why not."""
    lead, sold, born = lead_day(events, lead_lists), _day(rec.get("last_sold")), birth_sold(events)
    v = verdict(lead, sold, evidence_floor(rec, lead), born)
    # A sale dated just before the record arrived counts when the record was born carrying an
    # OLDER sale: the newer one was learned afterwards, so the owner we hold is the seller.
    late = v == "sold just before we pulled it" and bool(born) and born < sold
    if v != "sold since" and not late:
        return v
    try:
        price = float(rec.get("last_sale_price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price < MIN_SALE_USD:
        return "no real price (family transfer)"
    return ""


# ----------------------------------------------------------------------- CRM --

class Crm:
    def __init__(self, gap: float = 0.5):
        import datasift_api_upload as up
        # The Open API key is the published way in. Api() alone logs in with a password whenever
        # the box carries one, so a workstation run used a login token while the box used the key.
        key = str(up.env().get("REISIFT_API_KEY") or "").strip()
        self.up, self.api, self.gap = up, up.Api(api_key=key), gap

    def call(self, path, method="GET", body=None, headers=None):
        for attempt in range(6):
            try:
                time.sleep(self.gap)
                return self.api.call(path, method, body, headers=headers)
            except self.up.ApiError as exc:
                if getattr(exc, "code", 0) not in (429, 502, 503, 504):
                    raise
                m = re.search(r"available in (\d+)", str(exc))
                time.sleep(int(m.group(1)) + 2 if m else 10 * (attempt + 1))
        raise RuntimeError("gave up on " + path)

    def titles(self, path: str) -> dict:
        r = self.call(path)
        rows = r.get("results", r) if isinstance(r, dict) else r
        return {(x.get("title") or "").strip(): x["uuid"] for x in rows}

    def search(self, must: dict, limit: int = 1, offset: int = 0) -> dict:
        return self.call("/api/internal/property/", "POST",
                         {"limit": limit, "offset": offset, "ordering": "-created", "query": {"must": must}},
                         headers={"x-http-method-override": "GET"})

    def page(self, must: dict) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.search(must, 100, off)
            page = r.get("results") or []
            out += page
            off += 100
            if not page or off >= (r.get("count") or 0) or off >= 9900:
                return out

    def record(self, uuid: str) -> dict:
        return self.call("/api/internal/property/%s/" % uuid)

    def events(self, uuid: str) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.call("/api/internal/property/%s/logs/?limit=100&offset=%d&any_event_type=%s" % (uuid, off, LOG_EVENTS))
            page = r.get("results") or []
            out += page
            off += 100
            if not page or off >= (r.get("count") or 0) or off >= 500:
                return out

    def untag(self, uuid: str) -> None:
        self.call("/api/internal/property/%s/remove-tags/" % uuid, "POST", {"tags": TAG})

    def tag(self, uuid: str) -> None:
        self.call("/api/internal/property/%s/add-tags/" % uuid, "POST", {"tags": TAG})

    def set_status(self, uuid: str, status) -> None:
        # The status route takes a name OR null. A PATCH of the record refuses null
        # ("This field may not be null"), verified live 2026-11-12.
        self.call("/api/internal/property/%s/status/" % uuid, "POST", {"status": status or None})


def tag_names(rec: dict) -> list[str]:
    return [str((t.get("title") or t.get("name")) if isinstance(t, dict) else t) for t in rec.get("tags") or []]


class HeldBack(Exception):
    """This run's deed reads are spent (or SiftMap refused the key): the rest waits for the next run."""


def _siftmap():
    import datasift_api_upload as up
    from siftmap_standalone import SiftMapClient
    return SiftMapClient(api_key=up.env().get("REISIFT_API_KEY"), min_interval=SIFTMAP_GAP_S)


def _age_days(day, today: date) -> int:
    try:
        return (today - date.fromisoformat(str(day)[:10])).days
    except ValueError:
        return 10 ** 6


class Deeds:
    """SiftMap's newest deed per house: read at most `cap` times a run, every pass together, and
    reused for DEED_TTL_DAYS. `store` is the deed part of the verdict cache (dataflik id ->
    {"at", "deed"}). Acceptable use forbids detail pulls in volume without adding the records; these
    are houses already in the account, and the cap keeps each night a small slice."""

    def __init__(self, store: dict, client=None, cap: int | None = None, today: date | None = None):
        self.store, self._client = store, client
        self.cap = DEED_READS if cap is None else cap
        self.today = today or date.today()
        self.reads = self.reused = self.errors = 0
        self.dead = ""

    @property
    def client(self):
        if self._client is None:
            self._client = _siftmap()
        return self._client

    def left(self, reserve: int = 0) -> int:
        return max(0, self.cap - reserve - self.reads)

    def get(self, dataflik_id, reserve: int = 0) -> dict | None:
        """The newest deed ({} when SiftMap holds no sale), or None when the record carries no SiftMap
        id. Raises HeldBack once the run may read no more, keeping `reserve` reads for a later pass."""
        key = str(dataflik_id or "").strip()
        if not key:
            return None
        hit = self.store.get(key)
        if isinstance(hit, dict) and _age_days(hit.get("at"), self.today) < DEED_TTL_DAYS:
            self.reused += 1
            return hit.get("deed") or {}
        if self.dead:
            raise HeldBack(self.dead)
        if self.left(reserve) <= 0:
            raise HeldBack("the run's %d deed reads are spent" % self.cap)
        self.reads += 1
        try:
            deed = newest_deed(self.client.get_detail(key))
        except Exception as exc:
            self.errors += 1
            text = str(exc)
            if "401" in text or "403" in text or "not set" in text:
                self.dead = "SiftMap refused the key (%s)" % text[:80]
            raise
        self.store[key] = {"at": self.today.isoformat(), "deed": deed}
        return deed


# ---------------------------------------------------------------- change log --

def _log(entry: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "changes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def read_log() -> list[dict]:
    p = OUT / "changes.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def apply(crm: Crm, uuid: str, street: str, d: dict, commit: bool) -> bool:
    """Carry out one decision. The status goes first, so a refused write leaves the record exactly
    as it was; the change is logged with the prior state either way, so `undo` can put it back."""
    if d["action"] == "leave" or not commit:
        return False
    # A record restored without the tag on it (the nightly sale search finds untagged Sold records)
    # must not log a removal, or `undo` would put on a tag that was never there.
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "uuid": uuid, "street": street, "action": d["action"],
             "status_from": d["status"], "status_to": d["status_to"],
             "tag_removed": d["action"] in ("restore", "untag", "reenter") and d.get("tagged", True),
             "tag_added": d["action"] == "mark", "why": d["verdict"], "lead_day": d["lead_day"], "last_sold": d["last_sold"],
             "deed_day": d.get("deed_day") or ""}
    try:
        if (d["status_to"] or "") != (d["status"] or ""):
            crm.set_status(uuid, d["status_to"])
        if entry["tag_removed"]:
            crm.untag(uuid)
        if entry["tag_added"]:
            crm.tag(uuid)
    except Exception as exc:
        _log(dict(entry, error=str(exc)[:200]))
        raise
    _log(entry)
    return True


def verify(crm: Crm, uuid: str, d: dict) -> str:
    rec = crm.record(uuid)
    has = TAG in [t.lower() for t in tag_names(rec)]
    status = str(rec.get("status") or "")
    want_tag = d["action"] == "mark" or (d["action"] == "set sold")
    if has != want_tag:
        return "tag reads %s, wanted %s" % (has, want_tag)
    if status.lower() != str(d["status_to"] or "").lower():
        return "status reads %r, wanted %r" % (status, d["status_to"])
    return ""


# ------------------------------------------------------------------ commands --

def candidates(crm: Crm, lists: dict, tag_uuid: str, lead_lists) -> dict:
    """Tagged records that are, or have become, leads: born as one, or sitting on a lead list."""
    base = {"property_type": "clean", "any_tags": [tag_uuid]}
    out = {}
    for x in crm.page(dict(base, created=["2026-06-26", "2100-01-01"])):
        out[x["uuid"]] = x
    ours = [lists[t] for t in lead_lists if t in lists]
    for i in range(0, len(ours), 6):                 # small groups keep each result under the 10,000-row cap
        for x in crm.page(dict(base, any_lists=ours[i:i + 6])):
            out.setdefault(x["uuid"], x)
    return out


# Verdicts made before the deed was read (2026-11-14) are judged again: the repair and mark parts
# of an older file are dropped on load. Without that, a "leave" cached on the dates alone (the lien
# bought inside its filing quarter) would never be read again while its row stayed the same.
CACHE_VERSION = 2


def _cache() -> dict:
    p = OUT / "judged.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        d = {}
    stale = d.get("v") != CACHE_VERSION
    return {"v": CACHE_VERSION, "repair": {} if stale else d.get("repair") or {},
            "mark": {} if stale else d.get("mark") or {}, "detect": d.get("detect") or {},
            "deeds": d.get("deeds") or {}, "reentry": d.get("reentry") or {}}


def _save_cache(c: dict, today: date | None = None) -> None:
    """Write the cache, pruning old deeds and old weekly marks IN PLACE: the deed reader holds the
    very dict, and a replaced one would quietly stop saving every read made after the first save."""
    OUT.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    c["v"] = CACHE_VERSION
    for part, key, keep in (("deeds", "at", DEED_TTL_DAYS * 4), ("detect", "week", 14)):
        held = c.setdefault(part, {})
        for k in [k for k, v in held.items() if not isinstance(v, dict) or _age_days(v.get(key), today) > keep]:
            del held[k]
    (OUT / "judged.json").write_text(json.dumps(c), encoding="utf-8")


def _state(args) -> dict:
    """The verdict cache for this run, loaded once and shared by every pass of `daily`."""
    c = getattr(args, "cache", None)
    if c is None:
        c = _cache()
        args.cache = c
    return c


def _deed_reader(args) -> Deeds:
    d = getattr(args, "deeds", None)
    if d is None:
        d = Deeds(_state(args)["deeds"], client=getattr(args, "siftmap", None), today=getattr(args, "today", None))
        args.deeds = d
    return d


def _judged(args) -> set:
    """Records already judged this run, so a record found by two passes is read once."""
    s = getattr(args, "judged", None)
    if s is None:
        s = set()
        args.judged = s
    return s


def _own_flips(args) -> dict:
    """The Sold flips the guard made itself (guard_sold_flips), read from the change log once a run."""
    f = getattr(args, "own_flips", None)
    if f is None:
        f = guard_sold_flips()
        args.own_flips = f
    return f


# --------------------------------------------------------- for a person --

PERSON_COLS = ("street", "why", "status", "tagged", "lead_day", "deed_day", "buyer", "copied_on", "first_seen",
               "last_seen", "uuid")


def _person(args, uuid: str, street: str, d: dict) -> None:
    """Note a record the deed could not decide (or one a later judgment did decide)."""
    if getattr(args, "person_rows", None) is None:
        args.person_rows, args.person_done = {}, set()
    if d.get("person"):
        args.person_rows[uuid] = {"uuid": uuid, "street": street, "why": d["person"], "status": d.get("status") or "",
                                  "tagged": d.get("tagged"), "lead_day": d.get("lead_day") or "",
                                  "deed_day": d.get("deed_day") or "", "buyer": d.get("buyer") or "",
                                  "copied_on": d.get("owner_copied") or ""}
    else:
        args.person_done.add(uuid)


def _write_person(args) -> None:
    """Keep for_a_person.json and .csv: every record waiting on a person, kept until a later
    judgment decides it. Written in a dry run as well, since it changes nothing in the account."""
    import csv as _csv
    rows = getattr(args, "person_rows", None) or {}
    done = getattr(args, "person_done", None) or set()
    p = OUT / "for_a_person.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    today = (getattr(args, "today", None) or date.today()).isoformat()
    new = 0
    for u in done:
        cur.pop(u, None)
    for u, row in rows.items():
        first = (cur.get(u) or {}).get("first_seen")
        new += 0 if first else 1
        cur[u] = dict(row, first_seen=first or today, last_seen=today)
    OUT.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    with (OUT / "for_a_person.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=PERSON_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(cur.values(), key=lambda r: (str(r.get("why") or ""), str(r.get("street") or ""))))
    args.person_rows, args.person_done = {}, set()
    stats = getattr(args, "stats", None)
    if stats is None:
        stats = args.stats = {}
    stats["for_a_person"] = len(cur)
    stats["for_a_person_new"] = stats.get("for_a_person_new", 0) + new


def row_sig(row: dict) -> str:
    """What a search row shows of a record's state. A new list or tag, or a status change, moves it."""
    return "%s|%s|%s" % (row.get("list_count"), row.get("tag_count"), row.get("status") or "")


def repair(args) -> int:
    """Tagged leads: put back the ones whose 'sale' is the owner's own purchase. A record judged
    'leave' is not read again until something on it changes, so the daily pass stays small. Its
    deed reads stop DETECT_RESERVE short of the run's cap, so the nightly sale search always runs;
    once they are spent the pass stops and counts the rest, rather than reading records it cannot
    judge (the mail stage's lesson)."""
    crm = getattr(args, "crm", None) or Crm()
    lists, tags = crm.titles("/api/internal/list/?limit=1000"), crm.titles("/api/internal/tag/?limit=10000")
    lead_lists = OUR_LISTS + (DISTRESS_LISTS if args.include_distress else ())
    cand = candidates(crm, lists, tags[TAG], lead_lists)
    cache, judged, deeds = _state(args), _judged(args), _deed_reader(args)
    today = getattr(args, "today", None) or date.today()
    seen = {} if getattr(args, "fresh", False) else cache["repair"]
    todo = [u for u in cand if seen.get(u) != row_sig(cand[u])]
    unchanged = len(cand) - len(todo)
    todo = todo[:args.limit] if args.limit else todo
    print("%d tagged lead records, %d unchanged since they were last judged, %d to judge (%s)"
          % (len(cand), unchanged, len(todo), "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, checked, plan, held = Counter(), 0, Counter(), [], 0
    for i, uuid in enumerate(todo, 1):
        street = ((cand[uuid].get("address") or {}).get("street") or "")
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            if TAG not in [t.lower() for t in tag_names(rec)]:
                counts["tag already gone"] += 1
                continue
            own = _own_flips(args)
            deed = (deeds.get(rec.get("dataflik_id"), reserve=DETECT_RESERVE)
                    if needs_deed(rec, events, lead_lists, own) else None)
            d = decide(rec, events, lead_lists, deed=deed, own_flips=own)
            judged.add(uuid)
            _person(args, uuid, street, d)
            counts["%s: %s" % (d["action"], d["verdict"])] += 1
            plan.append(dict(d, uuid=uuid, street=street))
            if d["action"] == "leave" and args.commit:
                cache["repair"][uuid] = row_sig(cand[uuid])
            if apply(crm, uuid, street, d, args.commit):
                changed += 1
                if checked[d["action"]] < 3 or changed % 100 == 0:   # the first three of each kind, then one in a hundred
                    bad = verify(crm, uuid, d)
                    checked[d["action"]] += 1
                    if bad:
                        print("STOPPED: %s did not read back (%s)" % (street, bad), flush=True)
                        _save_cache(cache, today)
                        _write_person(args)
                        return 2
        except HeldBack as why:
            held = len(todo) - i + 1
            print("Repair: %s; %d record(s) wait for the next run." % (why, held), flush=True)
            break
        except Exception as exc:  # noqa: BLE001  (one unreadable record never stops the pass)
            counts["error: %s" % str(exc)[:50]] += 1
        if i % 100 == 0:
            _save_cache(cache, today)
            print("  %d of %d judged, %d changed" % (i, len(todo), changed), flush=True)
    _save_cache(cache, today)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "repair_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    _write_person(args)
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    print("Repair: %d judged, %d changed%s." % (len(todo) - held, changed, "" if args.commit else " (dry run: nothing written)"))
    args.stats = dict(getattr(args, "stats", None) or {}, restored=changed, repair_judged=len(todo) - held,
                      repair_held=held, repair_errors=sum(v for k, v in counts.items() if k.startswith("error")),
                      repair_would=sum(v for k, v in counts.items()
                                       if not k.startswith(("leave", "error", "tag already gone"))))
    return 0


def sale_weeks(today: date, days: int = MARK_LOOKBACK_DAYS) -> list[tuple[str, str]]:
    """Calendar weeks (Monday to Sunday) back to the lookback. Fixed weeks keep the cache keys stable."""
    monday = today - timedelta(days=today.weekday())
    out = []
    while monday >= today - timedelta(days=days):
        out.append((monday.isoformat(), (monday + timedelta(days=6)).isoformat()))
        monday -= timedelta(days=7)
    return out


def mark(args) -> int:
    """Real sales the tag missed: untagged records whose last sale is on or after their lead day.
    The search is cut by the week of the sale, so a record is read once per sale and never again.
    Each one is judged on its deed when SiftMap has one for the sale, else on the dates; it runs
    after the nightly sale search, so a house that search judged this week is not read twice."""
    crm = getattr(args, "crm", None) or Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cache, today = _state(args), getattr(args, "today", None) or date.today()
    judged, deeds = _judged(args), _deed_reader(args)
    week = (today - timedelta(days=today.weekday())).isoformat()
    todo = {}
    for a, b in sale_weeks(today):
        for x in crm.page({"property_type": "clean", "last_sold": [a, b], "must_not": {"any_tags": [tags[TAG]]}}):
            key = "%s|%s" % (x["uuid"], a)
            if str(x.get("status") or "").lower() != "sold" and key not in cache["mark"]:
                todo[key] = x
    keys = list(todo)[:args.limit] if args.limit else list(todo)
    print("%d untagged records with a sale in the last %d days not judged yet, %d this pass (%s)"
          % (len(todo), MARK_LOOKBACK_DAYS, len(keys), "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, plan, held = Counter(), 0, [], 0
    for i, key in enumerate(keys, 1):
        x = todo[key]
        uuid, street = x["uuid"], ((x.get("address") or {}).get("street") or "")
        if uuid in judged or (cache["detect"].get(uuid) or {}).get("week") == week:
            counts["judged by another pass"] += 1
            if args.commit and uuid not in judged:
                cache["mark"][key] = "judged by the sale search"
            continue
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            own = _own_flips(args)
            deed = deeds.get(rec.get("dataflik_id")) if needs_deed(rec, events, OUR_LISTS, own) else None
            if deed_sale_day(deed, _day(rec.get("last_sold"))):
                d = decide(rec, events, OUR_LISTS, deed=deed, own_flips=own)
                why = "" if d["action"] == "mark" else d["verdict"]
                _person(args, uuid, street, d)
            else:
                why = should_mark(rec, events, OUR_LISTS)
                d = {"action": "mark", "verdict": "sold since", "status": str(rec.get("status") or ""),
                     "status_to": "sold", "lead_day": lead_day(events, OUR_LISTS),
                     "last_sold": _day(rec.get("last_sold")), "tagged": False}
            judged.add(uuid)
            if why:
                counts["leave: " + why] += 1
            else:
                counts["mark sold"] += 1
                plan.append(dict(d, uuid=uuid, street=street, price=rec.get("last_sale_price"),
                                 born_sold=birth_sold(events)))
                changed += apply(crm, uuid, street, d, args.commit)
            if args.commit:
                cache["mark"][key] = why or "marked"
        except HeldBack as hb:
            held = len(keys) - i + 1
            print("Mark: %s; %d record(s) wait for the next run." % (hb, held), flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            counts["error: %s" % str(exc)[:50]] += 1
    _save_cache(cache, today)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mark_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    _write_person(args)
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    print("Mark: %d real sales%s." % (counts["mark sold"], "" if args.commit else " (dry run: nothing written)"))
    args.stats = dict(getattr(args, "stats", None) or {}, marked=changed, mark_judged=len(keys) - held,
                      mark_held=held, mark_would=counts["mark sold"],
                      mark_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


# ---------------------------------------------------- the nightly sale search --
#
# The two presets switched off on 2026-11-12 were the only thing that noticed a sale within weeks.
# Since then a sale reached the guard only when DataSift's own last_sold caught up, about two
# months after closing. This search asks SiftMap for the houses ALREADY IN the account whose last
# sale is inside the last DETECT_DAYS days, and judges each one on its deed once a week. Unlike the
# presets it hands out nothing by itself: the deed and the lead day decide.

WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]   # the search intersects addresses with the viewport


def _detect_filters(since: str = "") -> dict:
    f = {"in_my_account_mode": "in", "type_single_family": True, "extra_is_last_sale_interfamily": False}
    if since:
        f["extra_last_sale_date_min"] = since
    return f


def _county_address(fips: str, name: str, state: str) -> dict:
    """The county block the search wants. `search` must be the BARE county name: "Knox County, TN"
    returns nothing, which reads exactly like a quiet month."""
    title = "%s County, %s" % (name, state)
    return {"state": state, "title": title, "value": title, "county": name, "searchType": "county",
            "counties": [{"fips": fips, "county_name": name}], "search": name, "type": "county"}


def _search_body(fips: str, name: str, state: str, filters: dict, offset: int, size: int) -> dict:
    return {"result_index": offset, "size": size, "with_boundaries": False, "filters": filters,
            "addresses": [_county_address(fips, name, state)], "polygon": WORLD}


def prove_detect_filter(client, fips: str, name: str, state: str, since: str) -> tuple[bool, int, int]:
    """One count with the sale date floor and one without it. SiftMap ignores a filter key it does
    not know and answers as if it were not there, so the only proof the floor bites is a count that
    moves; without that proof the nightly slice would quietly be every house in the account."""
    def total(filters):
        r = client.search(_search_body(fips, name, state, filters, 0, 1)) or {}
        return int(r.get("total_results") or 0)
    with_floor, without = total(_detect_filters(since)), total(_detect_filters())
    return (without > 0 and with_floor < without), with_floor, without


def recent_sales(client, fips: str, name: str, state: str, since: str, total: int) -> tuple[dict, int]:
    """saved_uuid -> search row for every recent sale in the county, at most DETECT_MAX_ROWS rows. A
    row with no saved_uuid is not in the account (the in-account filter should keep those out): it
    is counted, never read. Offsets overlap rather than skip a row; the dict drops the repeat."""
    rows, outside, off = {}, 0, 0
    cap = min(int(total or 0), DETECT_MAX_ROWS)
    while off < cap:
        r = client.search(_search_body(fips, name, state, _detect_filters(since), off, SEARCH_PAGE)) or {}
        page = r.get("data") or []
        if not page:
            break
        for x in page:
            u = x.get("saved_uuid")
            if u:
                rows.setdefault(u, x)
            else:
                outside += 1
        off += SEARCH_PAGE
    return rows, outside


def detect(args) -> int:
    """The nightly sale search: every house in the account that SiftMap says sold in the last
    DETECT_DAYS days, not judged this week, goes through the deed rule. The deed reads it may spend
    are what the earlier passes left of the run's cap (at least DETECT_RESERVE)."""
    today = getattr(args, "today", None) or date.today()
    crm = getattr(args, "crm", None) or Crm()
    cache, judged, deeds = _state(args), _judged(args), _deed_reader(args)
    since = (today - timedelta(days=DETECT_DAYS)).isoformat()
    week = (today - timedelta(days=today.weekday())).isoformat()
    notes, rows, outside = [], {}, 0
    for fips, name, state in COUNTIES:
        try:
            ok, n_with, n_without = prove_detect_filter(deeds.client, fips, name, state, since)
            if not ok:
                notes.append("%s: the sale date filter did not change the count (%d with it, %d without), "
                             "so the search was skipped" % (name, n_with, n_without))
                continue
            got, skipped = recent_sales(deeds.client, fips, name, state, since, n_with)
        except Exception as exc:  # noqa: BLE001  (one county failing never stops the other)
            notes.append("%s: the sale search failed (%s)" % (name, str(exc)[:80]))
            continue
        if n_with > DETECT_MAX_ROWS:
            notes.append("%s: %d recent sales, only the first %d were read" % (name, n_with, DETECT_MAX_ROWS))
        if skipped and skipped * 2 > skipped + len(got):
            notes.append("%s: %d of %d rows are not in the account, so the in-account filter may be ignored"
                         % (name, skipped, skipped + len(got)))
        outside += skipped
        rows.update(got)
    seen = cache["detect"]
    todo = [u for u in rows if u not in judged and (seen.get(u) or {}).get("week") != week]
    todo = todo[:args.limit] if args.limit else todo
    print("%d house(s) in the account sold since %s, %d not judged this week (%s)"
          % (len(rows), since, len(todo), "LIVE" if args.commit else "dry"), flush=True)
    for n in notes:
        print("  " + n, flush=True)
    counts, restored, marked, checked, plan, held = Counter(), 0, 0, Counter(), [], 0
    for i, uuid in enumerate(todo, 1):
        row = rows[uuid]
        street = str(row.get("address") or "").split(",")[0]
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            street = ((rec.get("address") or {}).get("street") or street)
            own = _own_flips(args)
            deed = (deeds.get(rec.get("dataflik_id") or row.get("id"))
                    if needs_deed(rec, events, OUR_LISTS, own) else None)
            d = decide(rec, events, OUR_LISTS, deed=deed, own_flips=own)
            judged.add(uuid)
            _person(args, uuid, street, d)
            counts["%s: %s" % (d["action"], d["verdict"])] += 1
            plan.append(dict(d, uuid=uuid, street=street))
            if args.commit:
                seen[uuid] = {"week": week, "why": d["verdict"]}
            if apply(crm, uuid, street, d, args.commit):
                if d["action"] in ("restore", "untag"):
                    restored += 1
                else:
                    marked += 1
                if checked[d["action"]] < 3:
                    bad = verify(crm, uuid, d)
                    checked[d["action"]] += 1
                    if bad:
                        print("STOPPED: %s did not read back (%s)" % (street, bad), flush=True)
                        _save_cache(cache, today)
                        _write_person(args)
                        return 2
        except HeldBack as hb:
            held = len(todo) - i + 1
            print("Sale search: %s; %d house(s) wait for the next run." % (hb, held), flush=True)
            break
        except Exception as exc:  # noqa: BLE001  (one unreadable record never stops the pass)
            counts["error: %s" % str(exc)[:50]] += 1
        if i % 100 == 0:
            _save_cache(cache, today)
    _save_cache(cache, today)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "detect_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    _write_person(args)
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    print("Sale search: %d judged, %d revived, %d real sales%s." % (len(todo) - held, restored, marked,
                                                                    "" if args.commit else " (dry run: nothing written)"))
    args.stats = dict(getattr(args, "stats", None) or {}, detect_rows=len(rows), detect_outside=outside,
                      detect_judged=len(todo) - held, detect_held=held, detect_restored=restored,
                      detect_marked=marked, detect_note="; ".join(notes),
                      detect_would=sum(v for k, v in counts.items() if not k.startswith(("leave", "error"))),
                      detect_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


def daily(commit: bool = False, limit: int = 0) -> dict:
    """One pass for the scheduler: revive leads that landed on an old Sold record, catch this
    month's sales on the deed, mark the real sales DataSift's own last_sold shows. The three share
    one cap on deed reads; what it holds back waits for the next night and is counted, never
    dropped. The keys the runner reads (restored, marked, *_judged, *_errors) include the search."""
    args = argparse.Namespace(commit=commit, limit=limit, include_distress=False, fresh=False, stats={})
    rc = repair(args) or detect(args) or mark(args)
    st, deeds = args.stats, getattr(args, "deeds", None)
    out = dict(st, rc=rc,
               restored=st.get("restored", 0) + st.get("detect_restored", 0),
               marked=st.get("marked", 0) + st.get("detect_marked", 0),
               mark_judged=st.get("mark_judged", 0) + st.get("detect_judged", 0),
               mark_errors=st.get("mark_errors", 0) + st.get("detect_errors", 0),
               held_back=st.get("repair_held", 0) + st.get("detect_held", 0) + st.get("mark_held", 0),
               deed_reads=deeds.reads if deeds else 0, deed_problem=deeds.dead if deeds else "",
               for_a_person=st.get("for_a_person", 0), for_a_person_new=st.get("for_a_person_new", 0))
    print("Deeds: %d read (cap %d), %d reused from the last %d days. %d record(s) held back for the next run. "
          "%d waiting for a person (%s)." % (out["deed_reads"], deeds.cap if deeds else DEED_READS,
                                             deeds.reused if deeds else 0, DEED_TTL_DAYS, out["held_back"],
                                             out["for_a_person"], OUT / "for_a_person.csv"), flush=True)
    if out["deed_problem"]:
        print("Deed problem: " + out["deed_problem"], flush=True)
    return out


# ------------------------------------------------------- the yearly re-entry --
#
# The teaching keeps the sold window trailing about three years and resets it every January 1:
# a house that sold more than three years ago is no longer "recently sold", and the person who
# bought it then is today's owner and a prospect like any other. Until this existed nothing ever
# took a record back out of Sold, so the Sold pile only grew.

REENTRY_YEARS = int(os.environ.get("SOLD_REENTRY_YEARS") or 3)
REENTRY_LOOKBACK_MONTHS = int(os.environ.get("SOLD_REENTRY_LOOKBACK_MONTHS") or 24)
REENTRY_MAX_PER_RUN = int(os.environ.get("SOLD_REENTRY_MAX_PER_RUN") or 150)
OWNER_SLACK_DAYS = 45       # how far before the sale date an owner record may be dated and still be the buyer's


def reentry_cutoff(today: date, years: int = REENTRY_YEARS) -> str:
    """Sales before this day have left the window: January 1 of (this year minus `years`).

    On any day of 2027 with a three year window that is 2024-01-01, so the year leaving is 2023.
    A fixed day of the year on purpose: the window moves once, on January 1, and stays put."""
    return date(today.year - years, 1, 1).isoformat()


def reentry_due(today: date) -> bool:
    """The yearly pass runs in January (SOLD_REENTRY_MONTHS widens it for a pass that needs longer)."""
    try:
        months = max(1, int(os.environ.get("SOLD_REENTRY_MONTHS") or 1))
    except ValueError:
        months = 1
    return today.month <= months


def reentry_months(cutoff: str, months: int = REENTRY_LOOKBACK_MONTHS) -> list[tuple[str, str]]:
    """Calendar months ending the day before the cutoff, newest first. One search per month keeps
    every result under the records search's 10,000 row wall."""
    y, m, _ = (int(x) for x in cutoff.split("-"))
    out = []
    for _i in range(max(1, months)):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append((first.isoformat(), (nxt - timedelta(days=1)).isoformat()))
    return out


def reentry_decide(rec: dict, events: list[dict], cutoff: str) -> dict:
    """What to do with one Sold record whose sale may have left the window.

    It comes back only when ALL of these hold, and each "leave" is counted by its reason:
      * the status is Sold and the last sale is older than the cutoff
      * automation marked it Sold (a person's Sold is never undone: it may be our own deal)
      * the owner on the record is the BUYER. A record born with the sold tag came in through
        the sold pull, which replaces owners, so its owner is the buyer. Any other record must
        show an owner dated at or after the sale; otherwise the person on it is the seller we
        once marketed to, and re-entering it would call a stranger's house and ask for someone
        who left three years ago.
      * it is not on one of our courthouse lists. Those name the PREVIOUS owner's distress, and
        a returning record must not ride into the first-to-market lane on somebody else's notice.
    """
    status = str(rec.get("status") or "").strip()
    sold = _day(rec.get("last_sold"))
    out = {"verdict": "", "action": "leave", "status": status, "status_to": status, "last_sold": sold,
           "lead_day": "", "born_sold": birth_sold(events)}

    def leave(why: str) -> dict:
        out["verdict"] = why
        return out

    if status.lower() != "sold":
        return leave("not marked Sold")
    if not sold:
        return leave("no sale date")
    if sold >= cutoff:
        return leave("sold inside the window")
    born = born_with_tag(events)
    flip = sold_flip(events)
    if flip is not None and flip["source"] not in AUTOMATION:
        return leave("marked sold by a person")
    if flip is None and not born:
        return leave("no record of who marked it sold")
    if not born:
        owned = _day(rec.get("owned_since"))
        floor = (date.fromisoformat(sold) - timedelta(days=OWNER_SLACK_DAYS)).isoformat()
        if not owned or owned < floor:
            return leave("the owner on the record is from before the sale")
    lists = {str((x.get("title") or x.get("name")) if isinstance(x, dict) else x).strip().lower()
             for x in rec.get("lists") or []}
    ours = sorted(lists & {x.lower() for x in UPLOAD_LISTS})
    if ours:
        return leave("on a courthouse list from the previous owner")
    reset = (os.environ.get("SOLD_REENTRY_STATUS") or "").strip()
    out.update(verdict="sold before the window", action="reenter", status_to=reset)
    return out


def reentry(args) -> int:
    """The January pass: Sold records whose sale left the three year window come back as the new
    owner's record (status reset, sold tag off). Dry by default; every change is in the change
    log with its prior status, so `undo` puts it back."""
    today = getattr(args, "today", None) or date.today()
    args.stats = dict(getattr(args, "stats", None) or {}, reentered=0, reentry_judged=0, reentry_errors=0,
                      reentry_due=reentry_due(today), reentry_waiting=0)
    if not args.stats["reentry_due"] and not getattr(args, "force", False):
        print("Re-entry: not due. The window moves on January 1; pass --force to run it now.")
        return 0
    cutoff = reentry_cutoff(today)
    crm = getattr(args, "crm", None) or Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cache = _cache()
    seen = {} if getattr(args, "fresh", False) else cache["reentry"]
    cand: dict = {}
    for a, b in reentry_months(cutoff):
        for x in crm.page({"property_type": "clean", "any_tags": [tags[TAG]], "last_sold": [a, b]}):
            if str(x.get("status") or "").lower() == "sold":
                cand.setdefault(x["uuid"], x)
    todo = [u for u in cand if seen.get(u) != row_sig(cand[u])]
    limit = args.limit or REENTRY_MAX_PER_RUN
    waiting = max(0, len(todo) - limit)
    todo = todo[:limit]
    print("%d Sold record(s) with a sale before %s, %d to judge this pass, %d waiting (%s)"
          % (len(cand), cutoff, len(todo), waiting, "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, checked, plan = Counter(), 0, 0, []
    for i, uuid in enumerate(todo, 1):
        street = ((cand[uuid].get("address") or {}).get("street") or "")
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            d = reentry_decide(rec, events, cutoff)
            counts["%s: %s" % (d["action"], d["verdict"])] += 1
            plan.append(dict(d, uuid=uuid, street=street))
            if d["action"] == "leave" and args.commit:
                cache["reentry"][uuid] = row_sig(cand[uuid])
            if apply(crm, uuid, street, d, args.commit):
                changed += 1
                if checked < 3 or changed % 100 == 0:
                    bad = verify(crm, uuid, d)
                    checked += 1
                    if bad:
                        print("STOPPED: %s did not read back (%s)" % (street, bad), flush=True)
                        _save_cache(cache, today)
                        args.stats.update(reentered=changed, reentry_judged=i, reentry_waiting=waiting)
                        return 2
        except Exception as exc:  # noqa: BLE001  (one unreadable record never stops the pass)
            counts["error: %s" % str(exc)[:50]] += 1
        if i % 100 == 0:
            _save_cache(cache, today)
    _save_cache(cache, today)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reentry_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    would = sum(v for k, v in counts.items() if k.startswith("reenter"))
    print("Re-entry: %d judged, %d %s%s." % (len(todo), changed if args.commit else would,
                                           "put back into marketing" if args.commit else "would come back",
                                           "" if args.commit else " (dry run: nothing written)"))
    args.stats.update(reentered=changed, reentry_would=would, reentry_judged=len(todo), reentry_waiting=waiting,
                      reentry_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


# -------------------------------------------------------- the monthly audit --
#
# The by-hand sold audit: every month, look at what sold and ask why each one was missed. This
# reads last month's sales among the records we hold and says, for the ones that were OUR leads
# when they sold, how far we got: how long we had it, whether anyone called, texted or mailed,
# and what status it was sitting in. Read-only on the account; it writes two files beside the
# change log.

AUDIT_MAX = int(os.environ.get("SOLD_AUDIT_MAX") or 400)
ATTEMPT_FIELDS = (("predictivecall_attempts", "calls"), ("sms_attempts", "texts"),
                  ("directmail_attempts", "mail"), ("rvm_attempts", "voicemail drops"))


def audit_month(today: date, month: str = "") -> tuple[str, str, str]:
    """(YYYY-MM, first day, last day) for the month to audit: the one named, else last month."""
    if month:
        y, m = int(month[:4]), int(month[5:7])
    else:
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return "%04d-%02d" % (y, m), first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _attempt(rec: dict, key: str) -> int:
    for holder in (rec, rec.get("owner") or {}):
        try:
            if holder.get(key) not in (None, ""):
                return int(holder.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def audit_row(rec: dict, events: list[dict], lead_lists=None) -> dict:
    """One sold record, as the audit sees it. `was_lead` is the question that matters: was this
    ours to win when it sold?"""
    lead_lists = lead_lists or OUR_LISTS
    lead, sold, born = lead_day(events, lead_lists), _day(rec.get("last_sold")), birth_sold(events)
    v = verdict(lead, sold, evidence_floor(rec, lead), born)
    flip = sold_flip(events)
    lists = [str((x.get("title") or x.get("name")) if isinstance(x, dict) else x) for x in rec.get("lists") or []]
    ours = [x for x in lists if x in OUR_LISTS]
    attempts = {label: _attempt(rec, key) for key, label in ATTEMPT_FIELDS}
    days = None
    if lead and sold and sold >= lead:
        days = (date.fromisoformat(sold) - date.fromisoformat(lead)).days
    try:
        price = float(rec.get("last_sale_price") or 0) or None
    except (TypeError, ValueError):
        price = None
    return {"street": ((rec.get("address") or {}).get("street") or ""), "was_lead": v == "sold since",
            "verdict": v, "lead_day": lead, "sold": sold, "days_as_lead": days, "price": price,
            "lists": ", ".join(ours), "status_before": (flip or {}).get("from") or "",
            "marked_by": (flip or {}).get("source") or "", "assigned": bool(rec.get("assigned_to")),
            "untouched": not any(attempts.values()), **attempts}


def audit_summary(month: str, rows: list[dict]) -> dict:
    leads = [r for r in rows if r["was_lead"]]
    days = sorted(r["days_as_lead"] for r in leads if r["days_as_lead"] is not None)
    by_status = Counter((r["status_before"] or "no status") for r in leads)
    by_list = Counter(x.strip() for r in leads for x in r["lists"].split(",") if x.strip())
    return {"month": month, "sold_records": len(rows), "were_our_leads": len(leads),
            "never_touched": sum(1 for r in leads if r["untouched"]),
            "never_called": sum(1 for r in leads if not r["calls"]),
            "never_mailed": sum(1 for r in leads if not r["mail"]),
            "never_texted": sum(1 for r in leads if not r["texts"]),
            "unassigned": sum(1 for r in leads if not r["assigned"]),
            "median_days_as_lead": days[len(days) // 2] if days else None,
            "status_before_sold": dict(by_status.most_common(8)), "by_list": dict(by_list.most_common(8)),
            "not_leads": dict(Counter(r["verdict"] for r in rows if not r["was_lead"]).most_common())}


def audit_text(s: dict) -> str:
    """The summary as a few plain lines."""
    if not s["were_our_leads"]:
        return "%s: %d record(s) we hold sold, none while it was our lead." % (s["month"], s["sold_records"])
    lines = ["%s: %d of our leads sold (%d sold record(s) read)." % (s["month"], s["were_our_leads"], s["sold_records"]),
             "%d were never touched, %d never called, %d never texted, %d never mailed, %d had no caller assigned."
             % (s["never_touched"], s["never_called"], s["never_texted"], s["never_mailed"], s["unassigned"])]
    if s["median_days_as_lead"] is not None:
        lines.append("We held a lead a median %d days before it sold." % s["median_days_as_lead"])
    if s["by_list"]:
        lines.append("By list: " + ", ".join("%s %d" % kv for kv in s["by_list"].items()) + ".")
    return "\n".join(lines)


def monthly_audit(args) -> int:
    """Last month's sales among the records we hold, and how far we got on the ones that were
    ours. Runs once a month (the first run after the month ends); read-only on the account."""
    import csv as _csv

    today = getattr(args, "today", None) or date.today()
    month, first, last = audit_month(today, getattr(args, "month", "") or "")
    args.stats = dict(getattr(args, "stats", None) or {}, audit_month=month, audit_ran=False)
    OUT.mkdir(parents=True, exist_ok=True)
    out_json = OUT / ("audit_%s.json" % month)
    if out_json.exists() and not getattr(args, "force", False) and not getattr(args, "month", ""):
        print("Sold audit for %s is already written (%s). Pass --force to run it again." % (month, out_json))
        try:
            args.stats["audit_summary"] = json.loads(out_json.read_text(encoding="utf-8")).get("summary")
        except (OSError, ValueError):
            pass
        return 0
    crm = getattr(args, "crm", None) or Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cand: dict = {}
    day = date.fromisoformat(first)
    while day.isoformat() <= last:                     # by week, to stay under the 10,000 row wall
        end = min(day + timedelta(days=6), date.fromisoformat(last))
        for x in crm.page({"property_type": "clean", "any_tags": [tags[TAG]],
                           "last_sold": [day.isoformat(), end.isoformat()]}):
            cand.setdefault(x["uuid"], x)
        day = end + timedelta(days=1)
    limit = args.limit or AUDIT_MAX
    uuids = list(cand)[:limit]
    print("%d tagged record(s) sold in %s, %d read" % (len(cand), month, len(uuids)), flush=True)
    rows, errors = [], 0
    for uuid in uuids:
        try:
            rows.append(dict(audit_row(crm.record(uuid), crm.events(uuid)), uuid=uuid))
        except Exception:  # noqa: BLE001
            errors += 1
    summary = audit_summary(month, rows)
    summary["not_read"] = max(0, len(cand) - len(uuids))
    summary["errors"] = errors
    out_json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    cols = ["street", "was_lead", "verdict", "lead_day", "sold", "days_as_lead", "price", "lists", "status_before",
            "marked_by", "assigned", "untouched", "calls", "texts", "mail", "voicemail drops", "uuid"]
    with (OUT / ("audit_%s.csv" % month)).open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (not r["was_lead"], r["street"])))
    print(audit_text(summary))
    args.stats.update(audit_ran=True, audit_summary=summary)
    return 0


def presets(args) -> int:
    """The nightly SiftMap presets that hand out the sold tag. Off means auto-add off, nothing deleted."""
    import datasift_api_upload as up
    from siftmap_standalone import SiftMapClient
    sm = SiftMapClient(api_key=up.env().get("REISIFT_API_KEY"))
    rows, path = [], "/filters/"
    while path:
        r = sm._request(path)
        rows += r.get("results", r) if isinstance(r, dict) else r
        nxt = r.get("next") if isinstance(r, dict) else None
        path = nxt.replace("https://map.reisift.io", "") if nxt else None
    hit = [f for f in rows if TAG in [str(t).lower() for t in f.get("tags") or []]]
    for f in hit:
        floor = ((f.get("filter_data") or {}).get("filters") or {}).get("extra_last_sale_date_min")
        print("[%s] %-26s active=%s auto_add=%s sale floor=%s" % (f["id"], f.get("name"), f.get("is_active"),
                                                                 f.get("auto_add_enabled"), floor))
        if args.commit and f.get("auto_add_enabled"):
            _log({"at": datetime.now().isoformat(timespec="seconds"), "action": "preset auto-add off", "preset": f["id"],
                  "name": f.get("name")})
            sm._request("/filters/%s/" % f["id"], "PATCH", {"auto_add_enabled": False})
            back = sm._request("/filters/%s/" % f["id"])
            print("     auto-add now reads %s" % back.get("auto_add_enabled"))
            if back.get("auto_add_enabled"):
                return 2
    if not args.commit:
        print("(dry: pass --commit to switch auto-add off on the %d preset(s) still on)"
              % sum(1 for f in hit if f.get("auto_add_enabled")))
    return 0


def audit(_args) -> int:
    crm = Crm()
    lists, tags = crm.titles("/api/internal/list/?limit=1000"), crm.titles("/api/internal/tag/?limit=10000")
    base = {"property_type": "clean", "any_tags": [tags[TAG]]}
    n = crm.search(base).get("count") or 0
    born = crm.search(dict(base, created=["2000-01-01", "2026-06-25T23:59:59"])).get("count") or 0
    print("tagged %r: %d | born in the sold backlog pull: %d | created since as leads: %d" % (TAG, n, born, n - born))
    for t in OUR_LISTS + DISTRESS_LISTS:
        if t in lists:
            k = crm.search(dict(base, any_lists=[lists[t]])).get("count") or 0
            if k:
                print("  %-40s %5d tagged" % (t, k))
    # The nightly sale search: how big tonight's slice is, and the count that proves its date floor.
    since = (date.today() - timedelta(days=DETECT_DAYS)).isoformat()
    try:
        client = _siftmap()
        for fips, name, state in COUNTIES:
            ok, n_with, n_without = prove_detect_filter(client, fips, name, state, since)
            print("sale search, %s: %d house(s) in the account sold since %s, %d without the date floor (%s)"
                  % (name, n_with, since, n_without, "floor proven" if ok else "floor not proven, the search would skip it"))
    except Exception as exc:  # noqa: BLE001
        print("sale search: could not be read (%s)" % str(exc)[:120])
    return 0


def undo(args) -> int:
    crm, n = Crm(), 0
    for e in reversed(read_log()):
        if not e.get("uuid") or e.get("undone") or e.get("error"):
            continue
        print("%s  %s: tag %s, status %r -> %r" % (e["street"][:30], e["action"],
              "back on" if e.get("tag_removed") else ("off" if e.get("tag_added") else "unchanged"),
              e["status_to"], e["status_from"]))
        if args.commit:
            if e.get("tag_removed"):
                crm.tag(e["uuid"])
            if e.get("tag_added"):
                crm.untag(e["uuid"])
            if (e["status_to"] or "") != (e["status_from"] or ""):
                crm.set_status(e["uuid"], e["status_from"])
        n += 1
    print("%d change(s) %s" % (n, "put back" if args.commit else "would be put back (dry)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "presets", "repair", "mark", "detect", "daily", "undo", "reentry", "monthly-audit"):
        p = sub.add_parser(name)
        p.add_argument("--commit", action="store_true")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--include-distress", action="store_true",
                       help="also treat DataSift's own distress lists as a reason a record is a lead")
        p.add_argument("--fresh", action="store_true", help="judge every record again, ignoring earlier verdicts")
        if name in ("reentry", "monthly-audit"):
            p.add_argument("--force", action="store_true",
                           help="run even when it is not due (re-entry outside January, an audit already written)")
        if name == "monthly-audit":
            p.add_argument("--month", default="", help="YYYY-MM to audit (default: last month)")
    a = ap.parse_args(argv)
    if a.cmd == "daily":
        return daily(a.commit, a.limit)["rc"]
    return {"audit": audit, "presets": presets, "repair": repair, "mark": mark, "detect": detect, "undo": undo,
            "reentry": reentry, "monthly-audit": monthly_audit}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
