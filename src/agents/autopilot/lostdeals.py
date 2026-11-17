"""The monthly lost-deal audit (Ty, 2026-11-15): "every house we had that sold to an investor, who
bought it, and what we did or missed."

The course teaches it as a person's job: pull the sold records every month and work out why each
was missed, including who bought (C10.37), and read each one's activity log for the follow-up that
let it slip (lesson 2-06 at 2:28). Nobody did it on a clock, so a lost deal left nothing behind but a
sale date. The outcome study of 2026-11-12 shows what the audit finds: 15 houses sold to investors
while their notices sat in our account, and on all 15 the call counter read zero.

WHICH SALES. Every house in the account whose last sale falls in the audited month (the prior
calendar month by default) and whose record existed before the day it sold. The records search
honors both a `last_sold` and a `created` window, so the month is asked one sale day at a time with
the record's created date held before that day. A record created after its own sale (the June
backlog pull, a SiftMap add of a house that had already sold) never was ours to lose, and it is
never asked for. DataSift refreshes `last_sold` about two months after a closing, so each run also
looks again at the months before the audited one (`catch_up_months`); a sale that reached DataSift
late is reported as a late arrival the first time it is seen. Every sale is judged once and
remembered in the autopilot's own facts (plan-mode knowledge: nothing in the account changes). A
sale whose deed had not reached SiftMap yet is looked at again after `recheck_days`.

WHO BOUGHT, from the deed for that sale on SiftMap (sale_history, newest first; the house is in the
account, so a capped number of detail reads is allowed). An investor is
  * a company that is not a lender, a government body, a church or a family trust: the same test
    the deal-odds label is built on (deal_odds_train.investor_entity), so the audit and the ranking
    cannot disagree about who an investor is;
  * a cash buyer who did not move in (SiftMap's mailing address for the buyer is not the house); or
  * a known repeat buyer: SiftMap shows the buyer owning `portfolio_min` or more properties, or the
    same buyer bought two or more of the houses this audit has ever judged.
Not a lost deal: a transfer under `nominal_floor`, or a below-value sale between two people who
share a name (family); a lender taking the house back; and an investor reselling a house it bought
within `resale_months`. That earlier purchase was the deal, and it is named instead, because the
outcome study's worst trap was classifying on the last sale: an investor's retail exit read as the
owner selling at retail.

WHAT WE DID, from the record and the log copy: the lists and tags it carried (first to market or
not), the status it held on the day it sold, its counters (dials, texts, mail pieces), the dates the
log copy holds, whether it was ever a lead, and who it was assigned to. The miss is one of four, in
plain words: never worked, worked but never reached, reached but not followed up, a lead we lost.
Everything is held to BEFORE the sale, because the sold guard takes about two months to mark a
quiet sale and the house stays in the queues meanwhile: the touches the log copy holds after the
sale come off the counters, the status is the one held on the sale day (a lead made with the new
owner is not a lead we lost), and "reached" needs a dated conversation or a dated correct number.
The log copy reaches back only so far (45 days on a new box, 200 at most), so an older touch shows
only in the record's counters; the report says where the copy starts. When the record's owner now
IS the buyer (an owner swap after the sale), the calls to the seller may sit under the old owner,
and the counters are the evidence. A house on a buyer list (`buyer_list_prefixes`, the dispo
program) is an investor we sell deals to, and its sale is that investor's own exit: it is left out.

Reads, bounded: one search per sale day, then one record read and one SiftMap read per sale, up to
`max_records` and `siftmap_cap` a run, the audited month first. There is no write in this module:
the CSV in the run's files folder and the remembered facts are the whole output.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import config, store
from . import activity, budget, cadence, dealsource, kpi, lanes, ledger
from .stages import Ctx, addr, status_of
from .suppression import is_buyer

log = logging.getLogger(__name__)

FACT = "lostdeal"                 # ap_facts key "lostdeal:<sale day>", one per sale of one house
CREATED_FLOOR = "2000-01-01"
DEFAULTS = {"months_back": 1, "catch_up_months": 2, "max_records": 150, "siftmap_cap": 150, "day_cap": 500,
            "nominal_floor": 10000, "family_share": 0.5, "portfolio_min": 3, "resale_months": 24, "match_days": 45,
            "recheck_days": 7, "sold_statuses": ["sold"], "gap_lines": 3, "sync": True, "buyer_list_prefixes": ["Dispo"],
            "ftm_marks": ["FTM", "Courthouse Data", "Foreclosure", "Probate", "Liens", "Condemned", "Code Violation",
                          "Eviction", "Divorce", "Tax Sale"]}
INT_KEYS = ("months_back", "catch_up_months", "max_records", "siftmap_cap", "day_cap", "portfolio_min", "resale_months",
            "match_days", "recheck_days", "gap_lines")
MISSES = ("lead_lost", "reached", "worked", "never")
MISS_WORDS = {"lead_lost": "a lead we lost", "reached": "reached but not followed up", "worked": "worked but never reached",
              "never": "never worked"}
MISS_HEADS = {"lead_lost": "Lead lost", "reached": "Reached, not followed up", "worked": "Worked, never reached",
              "never": "Never worked"}
# What a sale that is not a lost deal to an investor was, as the report names it.
NOT_LOST = (("unknown", "deed not in SiftMap yet"), ("family", "family or no-price transfer"),
            ("resale", "an investor reselling (its purchase was the deal)"), ("lender", "taken back by a lender"),
            ("other", "bought by a government body, church, school or trust"), ("unclear", "cash buyer who may live there"),
            ("retail", "sold to a homebuyer"))
LENDER_RX = re.compile(r"\b(BANK|MORTGAGE|LENDING|LOANS?|FINANCIAL|CREDIT UNION|FEDERAL|NATIONAL ASSOCIATION|FANNIE|"
                       r"FREDDIE|SECRETARY|HUD|VETERANS)\b")
CORRECT = ("CORRECT", "CORRECT_DNC")
# Words that do not tell one buyer from another: the legal form, and the stock words a company name is built from.
LEGAL = frozenset("LLC L C INC CORP CORPORATION CO COMPANY LP LLP LTD PLLC PC THE AND OF".split())
GENERIC = frozenset(("HOMES HOME PROPERTIES PROPERTY INVESTMENTS INVESTMENT INVESTORS GROUP CAPITAL HOLDINGS HOLDING REAL "
                     "ESTATE REALTY TRUST LIVING REVOCABLE FAMILY PARTNERS VENTURES MANAGEMENT DEVELOPMENT SOLUTIONS "
                     "ENTERPRISES RENTALS RENTAL ACQUISITIONS FUND ASSETS BUILDERS CONSTRUCTION").split())


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with a lost-deal stage's block, for the playbook checker."""
    out = []
    for k in INT_KEYS:
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            out.append("stage %s: %s must be a whole number" % (st.id, k))
    if st.get("months_back") is not None and st.get("months_back") == 0:
        out.append("stage %s: months_back must be 1 or more (the month being audited has to be over)" % st.id)
    for k in ("nominal_floor", "family_share"):
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a positive number" % (st.id, k))
    for k in ("sold_statuses", "ftm_marks", "buyer_list_prefixes"):
        v = st.get(k)
        if v is not None and not isinstance(v, (list, tuple)):
            out.append("stage %s: %s must be a list" % (st.id, k))
    return out


# ---------------------------------------------------------------------- days and months
def _day(v) -> str:
    s = str(v or "")[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return ""


def _month_start(d: date, back: int) -> date:
    d = d.replace(day=1)
    for _ in range(int(back)):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def window(now: datetime, tz: str, months_back: int, catch_up: int) -> tuple[date, date, date]:
    """(first day of the audited month, first day of the month after it, first day of the catch-up)."""
    today = now.astimezone(kpi.zone(tz)).date()
    first = _month_start(today, months_back)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt, _month_start(first, catch_up)


def _days(first: date, end: date) -> list[date]:
    out, d = [], first
    while d < end:
        out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------- names
def _upper(name) -> str:
    return re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())


def _words(name) -> list[str]:
    return [t for t in _upper(name).split() if t not in LEGAL]


def buyer_key(name) -> str:
    """One spelling for one buyer: "Example Homes, L.L.C." and "EXAMPLE HOMES LLC" are the same company."""
    return " ".join(sorted(set(_words(name))))


def _distinct(name) -> set[str]:
    return {t for t in _words(name) if len(t) >= 3 and t not in GENERIC}


def same_party(a, b) -> bool:
    """Whether two names on two documents can be the same buyer: they share a distinctive word."""
    return bool(_distinct(a) & _distinct(b))


def _investor_test():
    """deal_odds_train's company tests and its not-an-investor words. It is the definition the deal-odds
    label was fitted on; imported, never copied, so the two cannot drift apart."""
    import deal_odds_train
    return deal_odds_train.investor_entity, deal_odds_train.company, deal_odds_train.NOT_INVESTOR_RX


def _owner_is(rec: dict, buyer: str) -> bool:
    """Whether the record's owner now IS the buyer (the owner was replaced after the sale). The sold
    guard's own matcher reads first names, initials and co-owners the way a deed writes them."""
    if not buyer:
        return False
    try:
        import sold_guard
        return sold_guard.name_match(sold_guard.owner_names(rec), buyer) == "owner"
    except Exception:  # noqa: BLE001  (an install without that module still has the plain test)
        o = rec.get("owner") or {}
        return same_party(" ".join(str(o.get(k) or "") for k in ("first_name", "last_name", "company")), buyer)


def _street_key(text) -> tuple:
    """(house number, first word of the street) from an address line, or () for a PO box or no number."""
    toks = re.findall(r"[A-Za-z0-9]+", str(text or "").split(",")[0].upper())
    if not toks or not toks[0].isdigit():
        return ()
    words = [t for t in toks[1:] if not t.isdigit()]
    return (toks[0], words[0] if words else "")


def moved_in(mail, street) -> bool | None:
    """Whether the buyer's mailing address is the house itself. None when either side cannot be read."""
    m, s = _street_key(mail), _street_key(street)
    if not m or not s:
        return None
    return m == s


def _money(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "")) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------- the deed
def deed_for(detail: dict, sale_day: str, match_days: int = 45, floor: float = 10000) -> dict:
    """The deed for this sale on SiftMap: among the deeds dated within `match_days` of the record's
    last sale (DataSift's own date and the county's can be days apart), a priced one before a nominal
    one, then the nearest. A small deed a few days before an auction deed is a side transfer, not the
    sale. {} when SiftMap holds no deed near the sale."""
    rows = [s for s in (detail or {}).get("sale_history") or [] if isinstance(s, dict) and _day(s.get("sale_date"))]
    if not rows or not _day(sale_day):
        return {}
    want = date.fromisoformat(_day(sale_day))

    def gap(s) -> int:
        return abs((date.fromisoformat(_day(s["sale_date"])) - want).days)

    near = [s for s in rows if gap(s) <= int(match_days)]
    if not near:
        return {}
    best = min(near, key=lambda s: (_money(s.get("sale_price")) < float(floor), gap(s)))
    newest = max(rows, key=lambda s: _day(s.get("sale_date")))
    bday = _day(best["sale_date"])
    earlier = [{"date": _day(s.get("sale_date")), "price": _money(s.get("sale_price")),
                "buyer": str(s.get("buyer_name") or "").strip()} for s in rows if _day(s.get("sale_date")) < bday]
    cash = best.get("is_cash_sale")
    return {"date": bday, "price": _money(best.get("sale_price")), "buyer": str(best.get("buyer_name") or "").strip(),
            "seller": str(best.get("seller_name") or "").strip(), "cash": cash if isinstance(cash, bool) else None,
            "newest": _day(newest.get("sale_date")) == bday, "earlier": earlier}


def resale_of(deed: dict, months: int) -> dict | None:
    """The purchase this sale's seller made within `months` before it, when there is one: then the
    sale is an investor's exit, and that purchase was the deal."""
    seller = buyer_key(deed.get("seller"))
    if not seller or not deed.get("date"):
        return None
    sold = date.fromisoformat(deed["date"])
    for e in sorted(deed.get("earlier") or [], key=lambda x: x["date"], reverse=True):
        if buyer_key(e.get("buyer")) == seller and (sold - date.fromisoformat(e["date"])).days <= int(months) * 31:
            return e
    return None


# ---------------------------------------------------------------------- one sale
def _names(ctx: Ctx) -> dict:
    try:
        return {u.get("uuid"): (u.get("first_name") or u.get("name") or "") for u in ctx.crm.users() or []}
    except Exception:  # noqa: BLE001  (the audit still says what happened without the caller's name)
        return {}


def _marks(rec: dict) -> list[str]:
    out = []
    for key in ("tags", "lists"):
        for t in rec.get(key) or []:
            n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
            if n:
                out.append(str(n).strip())
    return out


def _lists(rec: dict) -> list[str]:
    return [str((t.get("title") or t.get("name")) if isinstance(t, dict) else t) for t in rec.get("lists") or []
            if ((t.get("title") or t.get("name")) if isinstance(t, dict) else t)]


def status_at_sale(record_uuid: str, sale_end: datetime, sold_low: set, now_status: str) -> str:
    """The status the record held on the day it sold. The last one set before the sale; failing that,
    what the first change after the sale changed FROM; failing that, today's status. Today's status
    alone would read a lead made after the sale (a call to the new owner) as a lead we lost."""
    before = cadence.status_before(record_uuid, sale_end, sold_low)
    if before:
        return before
    for e in activity.events("property.status.updated", resource=record_uuid, since=sale_end):
        frm = str(e["data"].get("from") or "").strip().lower()
        return "" if frm in sold_low else frm
    return "" if now_status in sold_low else now_status


def held_as_sold(record_uuid: str, sale_start: datetime, sold_low: set, days: int = 30) -> bool:
    """Whether the record sat in a sold status for `days` or more before this sale: a house held only
    as a sold record (the June backlog pull, an earlier sale) was never in a queue to be worked. A
    Sold mark set near the sale is about the sale itself, so it does not count."""
    last = None
    for e in activity.events("property.status.updated", resource=record_uuid, until=sale_start):
        last = e
    if last is None:
        return False
    return str(last["data"].get("to") or "").strip().lower() in sold_low and last["when"] <= sale_start - timedelta(days=days)


def before_sale(counter: int, before: list, after: list, covered: bool) -> int:
    """Touches made before the sale. The record's counter is a lifetime total, and a house that sold
    quietly can stay in a dial queue for weeks, so the touches the log copy holds after the sale are
    taken off it when the copy reaches back to the sale. Never fewer than the log itself shows."""
    c = max(0, int(counter or 0) - len(after)) if covered else int(counter or 0)
    return max(c, len(before))


def audit_one(ctx: Ctx, cat, rec: dict, sale_day: str, detail: dict, users: dict, st: dict) -> dict:
    """Everything the audit records about one sale: who bought, on what terms, and what we did first."""
    tz = ctx.policy.tz
    u = rec.get("uuid") or ""
    owner = rec.get("owner") or {}
    ow = owner.get("uuid") or ""
    sale_end = kpi.day_start(sale_day, tz) + timedelta(days=1)
    investor_entity, company, not_investor_rx = _investor_test()
    deed = deed_for(detail, sale_day, int(st["match_days"]), float(st["nominal_floor"]))
    buyer, seller = deed.get("buyer", ""), deed.get("seller", "")
    oi = (detail or {}).get("owner_info") or {}
    # SiftMap's owner panel speaks for this buyer only when this deed is the newest and names them.
    speaks = bool(deed) and deed["newest"] and same_party(oi.get("owner_name"), buyer)
    street = (rec.get("address") or {}).get("street") if isinstance(rec.get("address"), dict) else rec.get("address")
    moved = moved_in(oi.get("owner_mail_address"), street) if speaks else None
    try:
        portfolio = int(oi.get("total_properties") or 0) if speaks else None
    except (TypeError, ValueError):
        portfolio = None
    price, value = deed.get("price") or 0.0, _money(rec.get("estimate_value"))
    floor = float(st["nominal_floor"])
    shared = bool(buyer and seller) and not company(buyer) and not company(seller) and same_party(buyer, seller)
    family = bool(deed) and (price < floor or (shared and (not value or price < float(st["family_share"]) * value)))
    resale = resale_of(deed, int(st["resale_months"])) if deed else None
    owner_is_buyer = _owner_is(rec, buyer)
    from deep_prospect_batch import ENTITY_RX, is_entity
    # The shared test calls an owner with no full name an entity too; the line says "a company owned
    # it", so it also needs a company field or a company word in the name.
    person = " ".join(str(owner.get(k) or "") for k in ("first_name", "last_name")).strip()
    held_by_company = bool(is_entity(owner)) and bool((owner.get("company") or "").strip() or ENTITY_RX.search(person))
    owner_company = (bool(seller) and bool(company(seller))) if owner_is_buyer else held_by_company

    # ---- what we did before the day it sold
    sold_low = {str(s).strip().lower() for s in st["sold_statuses"]}
    before = status_at_sale(u, sale_end, sold_low, status_of(rec))
    born_sold = held_as_sold(u, kpi.day_start(sale_day, tz), sold_low)
    calls = [e for e in activity.events(activity.DIAL, resource=ow) if e["data"].get("direction") != "inbound"] if ow else []
    sent = activity.events(("owner.sms.sent",), resource=ow) if ow else []
    pieces = [datetime.fromisoformat(e["at"]) for e in ledger.events(u, "mail") if e.get("at")]
    dials = [e["when"] for e in calls if e["when"] < sale_end]
    texts = [e["when"] for e in sent if e["when"] < sale_end]
    mailed = [m for m in pieces if m < sale_end]
    reach = kpi.log_reach(activity.CALLS)
    covered = reach is not None and reach <= sale_end
    counts = {"dials": before_sale(rec.get("predictivecall_attempts"), dials, [e for e in calls if e["when"] >= sale_end], covered),
              "texts": before_sale(rec.get("sms_attempts"), texts, [e for e in sent if e["when"] >= sale_end], covered),
              "mail": before_sale(rec.get("directmail_attempts"), mailed, [m for m in pieces if m >= sale_end], covered)}
    # Reached means a dated conversation or a number marked correct before the sale. An undated mark on
    # the record can come from a call after the sale, which would blame the follow-up for nothing.
    talks = kpi.conversation_events(cat, until=sale_end, owner=ow) if ow else []
    marked = any(str(e["data"].get("to") or "").upper() in CORRECT
                 for e in activity.events("owner.phone.status.updated", resource=ow, until=sale_end)) if ow else False
    ever_lead = bool(dealsource.lead_moments(u, until=sale_end)) or before in lanes.LEAD_STATUSES
    reached = bool(talks) or marked
    worked = any(counts.values())
    miss = "lead_lost" if ever_lead else "reached" if reached else "worked" if worked else "never"
    touched = dials + texts + mailed
    lane = lanes.lane_of(dict(rec, status=None), getattr(ctx.policy, "lanes", None))
    want = {m.lower() for m in st["ftm_marks"]}
    marks = _marks(rec)
    titles = _lists(rec)
    low = {t.lower(): t for t in titles}
    primary = next((low[o.lower()] for o in dealsource.LIST_ORDER if o.lower() in low), sorted(titles)[0] if titles else "")
    assigned = rec.get("assigned_to")
    assigned = (assigned.get("uuid") if isinstance(assigned, dict) else assigned) or ""
    return {"record": u, "sold": sale_day, "street": addr(rec), "audited": kpi.local_day(ctx.now, tz),
            "deed": "found" if deed else "none", "deed_day": deed.get("date", ""), "buyer": buyer, "buyer_key": buyer_key(buyer),
            "seller": seller, "price": price, "value": value, "cash": deed.get("cash"), "moved_in": moved,
            "portfolio": portfolio, "company": bool(buyer) and bool(investor_entity(buyer)),
            "not_investor": bool(buyer) and bool(not_investor_rx.search(_upper(buyer))),
            "lender": bool(buyer) and bool(LENDER_RX.search(_upper(buyer))), "family": family,
            "resale": resale, "owner_is_buyer": owner_is_buyer, "owner_company": owner_company,
            "lane": lanes.name(lane), "ftm": lane in ("ftm", "hot_auction") or any(m.lower() in want for m in marks),
            "list": primary, "status_before": before, "born_sold": born_sold, "ever_lead": ever_lead,
            "talk_day": kpi.local_day(talks[-1]["when"], tz) if talks else "",
            "touch_day": kpi.local_day(max(touched), tz) if touched else "",
            "dials": counts["dials"], "texts": counts["texts"], "mail": counts["mail"],
            "assigned_to": users.get(assigned, ""), "miss": miss,
            "dataflik_id": bool(rec.get("dataflik_id"))}


def verdict(s: dict, repeats: Counter, st: dict) -> tuple[str, str]:
    """(kind, why) for one judged sale; kind is investor, retail, unclear, lender, other, family, resale or
    unknown. The repeat-buyer rule is applied here and not at the sale, so a buyer seen a second time
    turns its first purchase into an investor purchase too."""
    if s.get("deed") != "found":
        return "unknown", "the deed has not reached SiftMap yet" if s.get("dataflik_id", True) else "the record has no SiftMap id"
    if not s.get("buyer"):
        return "unknown", "the deed names no buyer"
    if s.get("family"):
        return "family", "a family or no-price transfer"
    if s.get("resale"):
        r = s["resale"]
        return "resale", "resold by %s, who bought it %s%s" % (
            s.get("seller") or "the seller", kpi.fmt_day(r["date"]), (" for %s" % kpi.money(r["price"])) if r.get("price") else "")
    if s.get("lender"):
        return "lender", "taken back by a lender"
    if s.get("not_investor"):
        return "other", "a government body, church, school or trust"
    if s.get("company"):
        return "investor", "a company"
    if s.get("cash") and s.get("moved_in") is False:
        return "investor", "paid cash and did not move in"
    if s.get("portfolio") and int(s["portfolio"]) >= int(st["portfolio_min"]):
        return "investor", "owns %d properties" % int(s["portfolio"])
    n = repeats.get(s.get("buyer_key") or "", 0)
    if n >= 2:
        return "investor", "bought %d of our houses" % n
    if s.get("cash") and s.get("moved_in") is None:
        return "unclear", "paid cash; whether they moved in is not known"
    return "retail", "a homebuyer"


# ---------------------------------------------------------------------- what is remembered
def remembered(since_day: str = "", until_day: str = "") -> list[dict]:
    """Every sale the audit has judged, oldest first; `until_day` is not included."""
    out = []
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, key, value FROM ap_facts WHERE key LIKE ?", (FACT + ":%",)).fetchall()
    for r in rows:
        try:
            v = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict) or v.get("skip"):
            continue
        sold = str(v.get("sold") or "")
        if (since_day and sold < since_day) or (until_day and sold >= until_day):
            continue
        out.append(v)
    return sorted(out, key=lambda v: (v.get("sold") or "", v.get("record") or ""))


def _known(record_uuid: str, day: date) -> dict | None:
    """The sale the audit already judged on this house within two days of `day`. The search's own idea
    of a day and the record's can differ by one (time zones), and a sale must be judged once."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT value FROM ap_facts WHERE record_uuid=? AND key LIKE ?", (record_uuid, FACT + ":%")).fetchall()
    for r in rows:
        try:
            v = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        sold = _day((v or {}).get("sold"))
        if sold and abs((date.fromisoformat(sold) - day).days) <= 2:
            return v
    return None


def _stale(known: dict, now: datetime, tz: str, days: int) -> bool:
    """A sale whose deed was not on SiftMap yet is looked at again after `days`; anything else stands."""
    if known.get("skip") or known.get("deed") == "found":
        return False
    seen = _day(known.get("audited"))
    if not seen:
        return True
    return (date.fromisoformat(kpi.local_day(now, tz)) - date.fromisoformat(seen)).days >= int(days)


def _repeats(sales: list[dict]) -> Counter:
    """buyer -> how many different houses of ours that buyer bought, across every audit kept. A lender
    taking back three foreclosures, a city buying lots or an investor's resale is not a repeat buyer
    to call about the next deal, so only a real purchase counts."""
    seen: dict[str, set] = {}
    for s in sales:
        k = s.get("buyer_key") or ""
        if (k and s.get("deed") == "found" and not s.get("family") and not s.get("resale") and not s.get("lender")
                and not s.get("not_investor")):
            seen.setdefault(k, set()).add(s.get("record"))
    return Counter({k: len(v) for k, v in seen.items()})


# ---------------------------------------------------------------------- the run's file
def files_dir(ctx: Ctx) -> Path:
    """RUNS_DIR/<stage>-<task id>/files, which the brain serves by a signed link (Summary.run_dir)."""
    d = Path(config.RUNS_DIR) / ("%s-%s" % (ctx.stage.id, ctx.task_id or "cli")) / "files"
    d.mkdir(parents=True, exist_ok=True)
    ctx.out.run_dir = str(d.parent)
    return d


def _link(ctx: Ctx, path: Path) -> str:
    if ctx.task_id:
        from .. import slack
        url = slack.file_url(int(ctx.task_id), path.name)
        if url:
            return url
    return str(path)


COLUMNS = ["sold", "street", "buyer", "kind", "why", "price", "value", "cash", "moved_in", "seller", "owner_was_company",
           "record_names_buyer", "first_to_market", "list", "lane", "status_before", "ever_a_lead", "last_conversation",
           "last_touch", "dials", "texts", "mail", "assigned_to", "what_we_did", "record"]


def _yes(v) -> str:
    return "" if v is None else ("yes" if v else "no")


def write_csv(path: Path, sales: list[dict], repeats: Counter, st: dict) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for s in sales:
            kind, why = verdict(s, repeats, st)
            w.writerow([s.get("sold"), s.get("street"), s.get("buyer"), kind, why, round(s.get("price") or 0),
                        round(s.get("value") or 0), _yes(s.get("cash")), _yes(s.get("moved_in")), s.get("seller"),
                        _yes(s.get("owner_company")), _yes(s.get("owner_is_buyer")), _yes(s.get("ftm")), s.get("list"),
                        s.get("lane"), s.get("status_before"), _yes(s.get("ever_lead")), s.get("talk_day"),
                        s.get("touch_day"), s.get("dials"), s.get("texts"), s.get("mail"), s.get("assigned_to"),
                        MISS_WORDS.get(s.get("miss"), ""), s.get("record")])


# ---------------------------------------------------------------------- the report
def _and(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _status_word(s: str) -> str:
    return s.replace("_", " ").strip().capitalize() if s else ""


def _days_between(a: str, b: str) -> int | None:
    if not _day(a) or not _day(b):
        return None
    return (date.fromisoformat(_day(b)) - date.fromisoformat(_day(a))).days


def gap_line(s: dict, why: str) -> str:
    """One lost deal in a line a person reads: the house, what it carried, what we did, who bought."""
    where = "%s%s" % (s.get("street") or "a house", (", %s list" % s["list"]) if s.get("list") else ", no list")
    if s.get("assigned_to"):
        where += ", %s" % s["assigned_to"]
    sold = kpi.fmt_day(s["sold"])
    terms = " for %s" % kpi.money(s["price"]) if s.get("price") else ""
    if s.get("price") and s.get("value"):
        terms += ", %d%% of its %s value" % (round(100.0 * s["price"] / s["value"]), kpi.money(s["value"]))
    to = "sold %s to %s (%s)%s" % (sold, s.get("buyer") or "a buyer", why, terms)
    miss = s.get("miss")
    if miss == "lead_lost":
        did = "%s" % (_status_word(s.get("status_before")) or "A lead")
        gap = _days_between(s.get("talk_day"), s["sold"])
        if s.get("talk_day"):
            did += "; last conversation %s, %d days before it %s" % (kpi.fmt_day(s["talk_day"]), gap or 0, to)
        else:
            did += "; it %s" % to
    elif miss == "reached":
        if s.get("talk_day"):
            did = "Last conversation %s, %d days before it %s" % (kpi.fmt_day(s["talk_day"]),
                                                                 _days_between(s["talk_day"], s["sold"]) or 0, to)
        else:
            did = "A number on the record is marked correct, never a lead; it %s" % to
    elif miss == "worked":
        bits = _and(["%d dial%s" % (s["dials"], "" if s["dials"] == 1 else "s") if s.get("dials") else "",
                     "%d text%s" % (s["texts"], "" if s["texts"] == 1 else "s") if s.get("texts") else "",
                     "%d mail piece%s" % (s["mail"], "" if s["mail"] == 1 else "s") if s.get("mail") else ""])
        last = ", the last %s" % kpi.fmt_day(s["touch_day"]) if s.get("touch_day") else ""
        did = "%s%s, nobody reached; it %s" % (bits or "Touched", last, to)
    else:
        did = ("Held as a sold record, never marketed; it %s" % to) if s.get("born_sold") else "Nothing done; it %s" % to
    extra = ""
    if s.get("owner_company"):
        extra += " A company owned it."
    if s.get("owner_is_buyer"):
        extra += " The record now names the buyer, so calls to the seller may not show in the log."
    return "%s: %s. %s.%s" % (MISS_HEADS.get(miss, "Lost"), where, did, extra)


def report(ctx: Ctx, st: dict, first: date, nxt: date, start: date, new: list[dict], counts: dict, path: Path | None) -> None:
    tz = ctx.policy.tz
    every = remembered()
    repeats = _repeats(every)
    month = [s for s in every if first.isoformat() <= s["sold"] < nxt.isoformat()]
    month_name = first.strftime("%B %Y")
    judged = [(s,) + verdict(s, repeats, st) for s in month]
    sales = [x for x in judged if x[1] != "family"]
    inv = [x for x in judged if x[1] == "investor"]
    by_miss = Counter(x[0]["miss"] for x in inv)
    ctx.out.found = len(sales)
    ctx.out.acted = len(new)
    # The channel puts the stage's name in front ("Lost deals: ..."), so the head starts at the month.
    houses = "1 house in our account sold" if len(sales) == 1 else "%d houses in our account sold" % len(sales)
    if not sales:
        fam = len(judged) - len(sales)
        ctx.out.head = "In %s, no market sale of a house in our account is in DataSift yet%s." % (
            month_name, (" (%d family or no-price transfer%s)" % (fam, "" if fam == 1 else "s")) if fam else "")
    elif not inv:
        ctx.out.head = "In %s, %s, none to an investor we can name." % (month_name, houses)
    else:
        ctx.out.head = "In %s, %s, %d of them to %s: %s." % (
            month_name, houses, len(inv), "an investor" if len(inv) == 1 else "investors",
            _and(["%d %s" % (by_miss[m], MISS_WORDS[m]) for m in MISSES if by_miss[m]]))
    # The channel shows the head and the first six lines (voice.report_text), so the order is the
    # reading order: the biggest gaps, the repeat buyers, the file, then the detail.
    order = sorted(inv, key=lambda x: (MISSES.index(x[0]["miss"]), -(x[0].get("price") or 0)))
    for s, kind, why in order[:int(st["gap_lines"])]:
        ctx.out.line(gap_line(s, why))
    tops = []
    for k, n in repeats.most_common():
        if n < 2 or len(tops) >= 3:
            break
        names = [s.get("buyer") for s in every if s.get("buyer_key") == k and s.get("buyer")]
        here = sum(1 for x in judged if x[0].get("buyer_key") == k)
        tops.append("%s %d of our houses%s" % (names[-1] if names else k, n, (" (%d in %s)" % (here, first.strftime("%B"))) if here else ""))
    if tops:
        ctx.out.line("Repeat buyers, possible dispo buyers: %s." % "; ".join(tops))
    more = len(order) - int(st["gap_lines"])
    if path is not None:
        ctx.out.line("Every sale is in %s: %s.%s Nothing was changed in DataSift." % (
            path.name, _link(ctx, path), (" %d more investor sale%s %s there." % (more, "" if more == 1 else "s",
                                                                                "is" if more == 1 else "are")) if more > 0 else ""))
    if inv:
        ftm = sum(1 for x in inv if x[0].get("ftm"))
        ctx.out.line("First to market: %d of the %d investor sales were courthouse records." % (ftm, len(inv)))
    late = [s for s in new if s["sold"] < first.isoformat()]
    if late:
        late_inv = [s for s in late if verdict(s, repeats, st)[0] == "investor"]
        months = sorted({date.fromisoformat(s["sold"]).strftime("%B") for s in late},
                        key=lambda m: datetime.strptime(m, "%B").month)
        ctx.out.line("Late arrivals: %d sale(s) from %s reached DataSift since the last audit, %d to an investor%s." % (
            len(late), _and(months), len(late_inv), (" (%s)" % "; ".join(s["street"] for s in late_inv[:3])) if late_inv else ""))
    rest = Counter(x[1] for x in judged if x[1] != "investor")
    words = dict(NOT_LOST)
    if rest:
        ctx.out.line("Not lost to an investor: %s." % _and(["%d %s" % (rest[k], words[k]) for k, _ in NOT_LOST if rest.get(k)]))
    reads = "Read this run: %d sale(s)" % len(new)
    if counts.get("waiting"):
        reads += "; %d wait for the next run (%d record reads and %d SiftMap reads a run)" % (
            counts["waiting"], int(st["max_records"]), int(st["siftmap_cap"]))
    reach = kpi.log_reach(activity.CALLS)
    reads += ". Calls and texts before %s show only in a record's counters: the log copy starts there." % (
        kpi.fmt_day(kpi.local_day(reach, tz))) if reach else ". The call log copy is empty, so every touch is read from the counters."
    ctx.out.line(reads)
    for label in ("added after the sale", "outside the window", "a buyer's own house"):
        if counts.get(label):
            ctx.out.tally["sales left out: %s" % label] = counts[label]


# ---------------------------------------------------------------------- the stage
def _siftmap(ctx: Ctx):
    sm = (ctx.sources or {}).get("siftmap")
    if sm is None:
        from siftmap_standalone import SiftMapClient
        sm = SiftMapClient()
        ctx.sources["siftmap"] = sm
    return sm


def run_lostdeals(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    try:
        _investor_test()
    except Exception as exc:  # noqa: BLE001  (the audit cannot say who an investor is without it)
        ctx.out.problem("could not load the investor test (deal_odds_train), so the audit did not run (%s)" % str(exc)[:100])
        ctx.out.head = "Not run: the investor test could not be loaded."
        return
    if st["sync"]:
        kpi.sync_log(ctx, ("state", "calls", "texts"))
    tz = ctx.policy.tz
    first, nxt, start = window(ctx.now, tz, int(st["months_back"]), int(st["catch_up_months"]))
    counts: Counter = Counter()
    cands, seen = [], set()
    try:
        for d in _days(first, nxt) + _days(start, first):
            rows = ctx.crm.rows({"last_sold": [d.isoformat(), (d + timedelta(days=1)).isoformat()],
                                 "created": [CREATED_FLOOR, d.isoformat()]}, cap=int(st["day_cap"]))
            if len(rows) >= int(st["day_cap"]):
                counts["days at the search cap"] += 1
            for r in rows:
                u = r.get("uuid") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                known = _known(u, d)
                if known and not _stale(known, ctx.now, tz, int(st["recheck_days"])):
                    continue
                cands.append((0 if d >= first else 1, -int(r.get("list_count") or 0), d, u))
    except budget.BudgetExhausted as exc:
        ctx.out.problem("stopped searching the month's sales: %s. The rest are searched next run" % exc)
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    users = _names(ctx)
    reads = sm_reads = 0
    new: list[dict] = []
    stopped = False
    for _, _, d, u in cands:
        # Nearly every record carries a SiftMap id, so a spent SiftMap allowance stops the record reads
        # too: a record read whose deed cannot be read this run would only be read again next run.
        if stopped or reads >= int(st["max_records"]) or sm_reads >= int(st["siftmap_cap"]):
            counts["waiting"] += 1
            continue
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted as exc:
            ctx.out.problem("stopped reading the sold houses: %s. The rest are read next run" % exc)
            stopped = True
            counts["waiting"] += 1
            continue
        except Exception:  # noqa: BLE001  (counted below, one line for all of them, and tried again next run)
            counts["read errors"] += 1
            continue
        reads += 1
        if not rec.get("uuid"):
            continue
        sale_day = _day(rec.get("last_sold")) or d.isoformat()
        key = "%s:%s" % (FACT, sale_day)
        # A sale left out is remembered under the day it was searched on, so the next run's search
        # finds it known and does not open the record again.
        if not start.isoformat() <= sale_day < nxt.isoformat():
            counts["outside the window"] += 1
            ledger.set_fact(u, "%s:%s" % (FACT, d.isoformat()), {"skip": "outside the window", "sold": d.isoformat(),
                                                                 "record_sold": sale_day})
            continue
        made = _day(rec.get("created"))
        if made and made >= sale_day:
            counts["added after the sale"] += 1
            ledger.set_fact(u, "%s:%s" % (FACT, d.isoformat()), {"skip": "added after the sale", "sold": d.isoformat()})
            continue
        # A house on a buyer list (the dispo program) belongs to an investor we sell deals to; its sale
        # is that investor's own exit, not a seller we lost.
        if is_buyer(rec, st["buyer_list_prefixes"]):
            counts["a buyer's own house"] += 1
            ledger.set_fact(u, "%s:%s" % (FACT, d.isoformat()), {"skip": "a buyer's own house", "sold": d.isoformat()})
            continue
        detail: dict = {}
        if rec.get("dataflik_id"):
            sm_reads += 1
            try:
                detail = _siftmap(ctx).get_detail(rec["dataflik_id"]) or {}
            except Exception:  # noqa: BLE001  (counted and tried again next run)
                counts["siftmap errors"] += 1
                continue
        fact = audit_one(ctx, cat, rec, sale_day, detail, users, st)
        ledger.set_fact(u, key, fact)      # what the audit learned: kept in plan mode too, so a sale is judged once
        new.append(fact)
    if counts.get("read errors"):
        ctx.out.problem("could not read some sold houses (%d this run); they are tried again next run" % counts["read errors"])
    if counts.get("siftmap errors"):
        ctx.out.problem("SiftMap did not answer for some sold houses (%d this run); they are tried again next run"
                        % counts["siftmap errors"])
    if counts.get("days at the search cap"):
        ctx.out.problem("a sale day reached the search cap of %d rows, so some sales that day were not read; raise day_cap"
                        % int(st["day_cap"]))
    in_window = remembered(start.isoformat(), nxt.isoformat())
    repeats = _repeats(remembered())
    path = None
    if in_window:
        path = files_dir(ctx) / ("lost_deals_%s.csv" % first.strftime("%Y-%m"))
        write_csv(path, in_window, repeats, st)
        kpi.save_report("lost_deals_%s" % first.strftime("%Y-%m"), ctx.now, {
            "month": first.isoformat(), "window_from": start.isoformat(), "judged_this_run": len(new),
            "counts": dict(counts), "sales": [dict(s, kind=verdict(s, repeats, st)[0], why=verdict(s, repeats, st)[1])
                                              for s in in_window]})
    report(ctx, st, first, nxt, start, new, counts, path)
