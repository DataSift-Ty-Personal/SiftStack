"""MLS offer engine: every active listing in a county, a cash offer and a creative offer on each.

The method (Richard Taylor, Hold My Hand Wholesale): pull every active MLS listing, review all of them,
send the listing agent a cash offer and, where the seller's existing loan allows it, a subject-to offer
priced near the seller's number, then work the agents' replies. This module does the pull, the review and
the offer math, and writes the call list. It SENDS NOTHING and writes nothing to any CRM.

Where the data comes from (verified 2026-11-15 on Knox, 1,318 active single-family listings):
  * The SiftMap property index (read-only OpenSearch MCP, `opensearch_mcp.OpenSearchMCP`) carries the
    listing itself: `mls_data.status / listing_price / listed_since / agent_name / agent_phone /
    agent_office_name`, plus the seller's loans: `mortgage_data.open_mortgages[]` with an ESTIMATED
    balance, rate (the market rate in the recording month), payment, loan type and recording date.
    Agent name 1,316 of 1,318, office 1,318, agent phone 867, a loan estimate 802.
  * `mls-history` is the listing's event log: every price change, every expiry ("Fail") and relist, with
    the agent on each row. Price cuts, relists and agent changes come from here.
  * Zillow (OpenWebNinja property details, optional, paid) adds the listing remarks, the broker's phone,
    the agent's license number, Zillow's own days on market and bank-owned / new-build flags. The East
    Tennessee feed carries NO agent email, so email is the one contact gap.

TWO SOURCES feed the same offer math:
  * api (the default, and the only one a DataSift user has): the published SiftMap search and detail on
    the account's own Open API key, inside DataSift's acceptable use. See mls_offers_api.py.
        python src/mls_offers.py --county "Knox, TN" --phase all                 # screen, price, read what is in the account
        python src/mls_offers.py --county "Knox, TN" --phase all --add --commit  # also add the shortlist first
        python src/mls_offers.py --zips 37914,37917 --phase all --zillow --limit 50
  * cluster (staff only): the read-only OpenSearch MCP into SiftMap's own index.
        python src/mls_offers.py --source cluster --market knox --phase all

Cluster phases (state in output/mls_offers/<market>/):
    python src/mls_offers.py --source cluster --phase pull     # listings from the SiftMap index
    python src/mls_offers.py --source cluster --phase history  # mls-history: cuts, relists, days on market
    python src/mls_offers.py --phase zillow --limit 50         # remarks + broker phone on the top rows (paid)
    python src/mls_offers.py --phase offers                    # screen, score, cash + creative offers
    python src/mls_offers.py --phase export                    # workbook + agent call list + outreach drafts
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
# Output goes to output/ in a SiftStack checkout. An installed skill writes to ~/SiftStack instead: its own
# folder is replaced on every plugin update and can be read-only. MLS_OFFERS_OUT overrides both.
_DATA_HOME = ROOT / "output" if (ROOT / "src").is_dir() else Path.home() / "SiftStack"
OUT = Path(os.environ.get("MLS_OFFERS_OUT") or (_DATA_HOME / "mls_offers"))
logger = logging.getLogger("mls_offers")

MARKETS = {"knox": ["47093"], "blount": ["47009"], "knox_blount": ["47093", "47009"]}
DAY_MS = 86_400_000

LISTING_FIELDS = [
    "dataflik_id", "property_address", "property_city", "property_zip", "state", "property_value", "rent_value",
    "bedroom_count", "bath_count", "living_area_sqft", "year_built", "first_name", "last_name",
    "is_corporate_owned", "is_trust_owned", "owner_occupied", "mailing_address_state", "equity_est",
    "equity_percentage_est", "tax_amount", "has_postal_address", "is_assumable_loan", "is_adjustable_loan",
    "mls_data", "mortgage_data", "distressor_data", "fips", "location",
    "investor_off_market_score", "investor_on_market_score", "realtor_score",
    "full_name", "full_name_2", "is_owner_investor", "owner_age",   # a company's name is ONLY in full_name
]
# Owner type comes from the NAME. The index's is_corporate_owned flag marks married couples ("Barlow Dwight D &
# Carolina M") and family living trusts as corporate, and is_trust_owned misses some trusts, so the flags are
# only a fallback when there is no name. Checked in order: a bank or government owner (an REO), a company
# (an iBuyer "Property Trust" is a company), an estate, a trust, else a person.
OWNER_BANK = re.compile(r"\b(bank|financial|mortgage|lending|loan servic\w*|federal|secretary of|veterans affairs|"
                        r"hud|housing and urban|fannie|freddie|city of|county of|state of|united states)\b", re.I)
OWNER_COMPANY = re.compile(r"\b(llc|l l c|inc|incorporated|corp|corporation|company|co|lp|llp|ltd|holdings?|"
                           r"propert(y|ies) (trust|group|holdings)|properties|investments?|group|partners|partnership|"
                           r"enterprises?|realty|homes|builders?|builds|construction|development|developers|flips?|"
                           r"capital|ventures?|services|solutions|rentals?|opendoor|offerpad|acquisitions?|fund|"
                           r"funding|reo|residential credit)\b", re.I)
SHORT_LIENS = {"credit line", "construction", "commercial"}   # paid off at a sale, never taken over
OWNER_ESTATE = re.compile(r"\b(estate of|estate)\b", re.I)
OWNER_TRUST = re.compile(r"\b(trust|trustee|trustees|tr)\b", re.I)


def owner_type(row: dict) -> str:
    name = (row.get("owner") or "").strip()
    if name:
        if OWNER_BANK.search(name):
            return "Bank or government"
        if OWNER_COMPANY.search(name):
            return "Company"
        if OWNER_ESTATE.search(name):
            return "Estate"
        if OWNER_TRUST.search(name):
            return "Trust"
        return "Individual"
    if row.get("trust"):
        return "Trust"
    return "Company" if row.get("corporate") else "Unknown"
# Investor purchases near a listing are what our end buyer will pay for it. The label is on the LAST sale:
# Pending / Rental / Wholetail / Wholesale mean an investor bought and still holds (the price is an as-is
# investor buy, the current owner is the investor); Flip means the last sale was the renovated exit (an ARV).
ACQ_TYPES = ("Pending", "Rental", "Wholetail", "Wholesale", "Investor")   # "Investor": the API source's union
EVIDENCE_FIELDS = [
    "dataflik_id", "location", "last_sale_price", "last_sale_date", "last_sale_investor_transaction_type",
    "living_area_sqft", "bedroom_count", "year_built", "property_value", "is_cash_buyer", "first_name",
    "last_name", "property_address", "property_zip",
]
RADII_MI = (0.5, 1.0, 2.0)

# Distress flags that say the seller has a reason to take less, and how much each one counts.
MOTIVE_FLAGS = {
    "is_probate_properties": ("probate", 10), "is_pre_probate": ("pre-probate", 8),
    "is_inheritance_property": ("inherited", 8), "is_heirs_property": ("heirs", 8),
    "is_obituary_property": ("obituary", 6), "is_estate_sales": ("estate sale", 6),
    "is_divorce_property": ("divorce", 8), "is_notice_of_default": ("notice of default", 10),
    "is_lis_pendens": ("lis pendens", 8), "is_notice_of_foreclosure": ("foreclosure notice", 10),
    "is_bankruptcy_property": ("bankruptcy", 6), "is_recent_deliquent": ("tax delinquent", 6),
    "is_vacant_property": ("vacant", 8), "is_zombie_property": ("zombie", 8),
    "is_tired_landlord": ("tired landlord", 5), "is_oos_owners": ("out-of-state owner", 4),
    "is_absentee_owners": ("absentee owner", 2), "is_federal_lien": ("federal lien", 4),
    "is_state_tax": ("state tax lien", 4), "is_judgment_liens": ("judgment lien", 3),
    "is_eviction": ("eviction", 4), "is_senior_homeowners": ("senior owner", 2),
}
BUILDER_WORDS = re.compile(r"\b(homes|builders?|construction|development|developers|communities|custom)\b", re.I)
REMARK_WORDS = {
    "as is": 6, "as-is": 6, "investor": 6, "cash only": 8, "cash offers": 4, "tlc": 6, "fixer": 8,
    "handyman": 8, "needs work": 8, "needs some work": 6, "bring your": 4, "bring all offers": 6,
    "motivated": 6, "estate": 4, "sold as is": 6, "priced to sell": 4, "opportunity": 2, "potential": 2,
    "assumable": 6, "owner financing": 6, "seller financing": 6, "creative": 4, "rehab": 6, "flip": 4,
}


@dataclass
class Policy:
    """Offer rules. Every number is a lever on the command line or in policy.json; nothing is hidden."""
    # Defaults follow Richard Taylor's stated numbers (research 2026-11-15), tuned to Ty's buy box.
    max_list: int = 700_000            # Ty's buy box ceiling; creative lanes run up to here
    min_list: int = 50_000             # he skips listings under about $55-60K (land-value houses)
    cash_max_list: int = 300_000       # he keeps cash deals near $200K and under; Knox runs higher
    new_build_years: int = 2           # year_built within this many years of today = new construction, skipped
    # Cash lane: the lower of his 70%-of-list rule and his ARV x 60% - rehab - fee math (AVM stands in for ARV).
    cash_pct_list: float = 0.70
    cash_pct_list_motivated: float = 0.65  # when the listing scores as motivated
    cash_rule_pct: float = 0.60
    fee: int = 15_000                  # Ty's flat assignment fee (offer sheet default)
    cash_min_pct_list: float = 0.50    # under this share of list a cash offer is the lowball he warns burns the agent:
                                       # it goes to the price watch instead, with the list price that would make it work
    creative_min_coc: float = 0.10     # a creative offer has to return this cash on cash at its own price to be sent
    repair_psf: float = 15.0           # placeholder repairs per sq ft until someone reads the photos
    motivated_score: int = 45          # score at or above which the motivated cash share applies
    # The off-market AI score (investor_off_market_score) ranks too. On active Knox listings it spreads out
    # (median 28, top tenth 68+) while the on-market score sits at 80+ on 64% of them, so only the off-market
    # score can separate listings. Nothing below ai_floor, ai_weight points at 90 and above.
    ai_weight: int = 15
    ai_floor: int = 30
    # Subject-to lane. Ty, 2026-11-15: offer it on EVERY good-rate loan, because today's rate is so high
    # (Knox loans recorded in 2026 median 6.2%). His low-equity case is the easy one; with more equity the
    # seller gets the cash below and carries the rest as a note. We pay the agent's commission on top.
    subto_max_rate: float = 5.0        # the existing loan must be at or under this rate to be worth keeping
    subto_price_pct: float = 1.00      # contract price as a share of list
    subto_max_cash: int = 12_000       # his "$10-12K to the seller"; a bigger gap turns into a seller carry
    subto_max_carry_pct: float = 1.00  # carry cap as a share of price (1.0 = no cap; his own rule was about 0.20)
    carry_rate: float = 0.0            # the seller's note on the equity: 0% (his seller-finance terms)
    carry_years: int = 30              # paid monthly over this many years; the payment counts in our monthly cost
    subto_gate_cash_flow: bool = False # Ty wants subject-to on all good rates: flag weak cash flow, do not drop it
    subto_min_balance: int = 40_000    # a loan smaller than this is cheaper to pay off than to take over
    listing_commission: float = 0.025  # paid by us on top in the creative lanes, as he does
    buyer_closing_pct: float = 0.02
    insurance_pct: float = 0.0065      # annual premium as a share of value (TN average)
    rent_haircut: float = 0.20         # his cash-on-cash math takes 80% of rent
    target_coc: float = 0.20           # his rental buyers want 20% cash on cash
    # Seller-finance lane (houses the index flags free and clear, never just "no loan on record").
    sf_price_pct: float = 1.00
    sf_down_pct: float = 0.10
    sf_rate: float = 0.04
    sf_years: int = 30


# --------------------------------------------------------------------------------------------- helpers

def ms_to_date(ms) -> Optional[date]:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date() if ms else None
    except (TypeError, ValueError, OSError):
        return None


def phone_digits(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else ""


def fmt_phone(p: str) -> str:
    d = phone_digits(p)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else ""


def round_down(x: float, step: int = 1000) -> int:
    return int(math.floor(max(0.0, x) / step) * step)


def parse_point(wkt) -> tuple[Optional[float], Optional[float]]:
    """The index answers `location` as WKT, "POINT (lon lat)"."""
    m = re.match(r"\s*POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", str(wkt or ""), re.I)
    return (float(m.group(2)), float(m.group(1))) if m else (None, None)


def miles(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 7917.6 * math.asin(math.sqrt(a))


def median(xs: list) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def norm_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = re.sub(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|LTD|LP|LLP|PLLC|THE|TRUST|TR)\b", " ", s)
    return " ".join(s.split())


def first_name(full: str) -> str:
    toks = [t for t in re.split(r"\s+", (full or "").strip()) if t]
    for t in toks:
        if len(t.rstrip(".")) > 1:
            return t.title()
    return ""


def load_json(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------------------------- pull

def active_query(fips: list[str]) -> dict:
    return {"bool": {"filter": [
        {"terms": {"fips": fips}},
        {"term": {"property_type_group": "single_family_residence"}},
        {"term": {"mls_data.status": "Active"}},
    ]}}


def slim_listing(src: dict) -> dict:
    """One index document reduced to what the screen and the offer use."""
    mls = src.get("mls_data") or {}
    md = src.get("mortgage_data") or {}
    loans = []
    for m in md.get("open_mortgages") or []:
        loans.append({
            "type": m.get("loan_type") or "", "rate": m.get("interest_rate_est"),
            "rate_type": m.get("interest_rate_type") or "", "balance": m.get("remaining_balance_est") or 0,
            "payment": m.get("monthly_payment_est") or 0, "recorded": str(ms_to_date(m.get("recording_date")) or ""),
            "private": bool(m.get("is_private_lender")),
        })
    flags = [k for k, v in (src.get("distressor_data") or {}).items() if isinstance(v, dict) and v.get("is_active")]
    lat, lon = parse_point(src.get("location"))
    return {
        "lat": lat, "lon": lon, "ai_off": src.get("investor_off_market_score"),
        "ai_on": src.get("investor_on_market_score"), "realtor": src.get("realtor_score"),
        "id": src.get("dataflik_id"), "fips": src.get("fips"), "address": src.get("property_address") or "",
        "city": (src.get("property_city") or "").title(), "zip": str(src.get("property_zip") or "")[:5],
        "state": src.get("state") or "TN", "value": src.get("property_value"), "rent": src.get("rent_value"),
        "beds": src.get("bedroom_count"), "baths": src.get("bath_count"), "sqft": src.get("living_area_sqft"),
        "year": src.get("year_built"),
        # A person's name from first/last (full_name repeats surnames: "Samir Haddix Haddix"); a company has only full_name.
        "owner": (" ".join(x for x in (src.get("first_name"), src.get("last_name")) if x) or src.get("full_name") or "").strip(),
        "owner_2": src.get("full_name_2") or "", "owner_investor": bool(src.get("is_owner_investor")),
        "owner_age": src.get("owner_age"),
        "corporate": bool(src.get("is_corporate_owned")), "trust": bool(src.get("is_trust_owned")),
        "owner_occupied": src.get("owner_occupied") == "Yes", "mail_state": src.get("mailing_address_state") or "",
        "tax": src.get("tax_amount"), "has_address": src.get("has_postal_address") is not False,
        "assumable": bool(src.get("is_assumable_loan")), "adjustable": bool(src.get("is_adjustable_loan")),
        "list_price": mls.get("listing_price"), "listed_since": str(ms_to_date(mls.get("listed_since")) or ""),
        "agent": mls.get("agent_name") or "", "agent_phone": fmt_phone(mls.get("agent_phone")),
        "office": mls.get("agent_office_name") or "", "last_sold": str(ms_to_date(mls.get("last_sold_date")) or ""),
        "loans": loans, "total_balance": md.get("total_remaining_balance_est") or 0,
        "total_payment": md.get("total_monthly_payment_est") or 0, "flags": sorted(flags),
    }


def phase_pull(market: str, fips: list[str], client=None) -> list[dict]:
    from opensearch_mcp import OpenSearchMCP
    os_ = client or OpenSearchMCP()
    try:
        q = active_query(fips)
        total = os_.count(q)
        rows = [slim_listing(s) for s in os_.page(q, [{"dataflik_id": "asc"}], LISTING_FIELDS, total + 100)]
    finally:
        if client is None:
            os_.close()
    if len(rows) != total:
        raise SystemExit(f"pull read {len(rows)} of {total} listings; refusing a partial pull")
    save_json(OUT / market / "listings.json", {"pulled": str(date.today()), "fips": fips, "rows": rows})
    logger.info("pulled %d active single-family listings for %s", len(rows), market)
    return rows


# --------------------------------------------------------------------------------------------- history

def summarize_history(rows: list[dict], list_price: Optional[int] = None, today: Optional[date] = None) -> dict:
    """What the listing's event log says about the seller. A marketing run is every row after the last
    sale. Within it: the first price, the cuts (a lower price than the last one seen, repeats ignored), the
    expiries ("Fail") that were followed by a relist, pendings that fell back to active, agent changes."""
    today = today or date.today()
    ev = sorted(rows, key=lambda r: (r.get("recording_date") or 0, r.get("listing_update_id") or ""))
    last_sold = max((i for i, r in enumerate(ev) if r.get("status") == "Sold"), default=-1)
    run = ev[last_sold + 1:]
    if not run:
        return {"events": 0, "cuts": 0, "cut_pct": 0.0, "relists": 0, "fell_through": 0, "agents": 0,
                "original_price": list_price, "run_start": "", "dom": None, "last_cut": "", "days_since_cut": None}
    starts = [ms_to_date(r.get("listing_date")) for r in run if r.get("listing_date")]
    run_start = min(d for d in starts if d) if any(starts) else ms_to_date(run[0].get("recording_date"))
    prices = [(ms_to_date(r.get("recording_date")), r.get("sale_price")) for r in run if r.get("sale_price")]
    original = prices[0][1] if prices else list_price
    cuts, last_cut, prev = 0, None, None
    for d, p in prices:
        if prev is not None and p < prev:
            cuts += 1
            last_cut = d
        prev = p
    current = list_price or (prices[-1][1] if prices else None)
    cut_pct = round(1 - current / original, 4) if original and current and current < original else 0.0
    relists = sum(1 for a, b in zip(run, run[1:]) if a.get("status") == "Fail" and b.get("status") == "Active")
    fell = sum(1 for a, b in zip(run, run[1:]) if a.get("status") == "Pending" and b.get("status") == "Active")
    agents = {(r.get("agent_name") or "").strip().lower() for r in run if r.get("agent_name")}
    return {
        "events": len(run), "cuts": cuts, "cut_pct": cut_pct, "relists": relists, "fell_through": fell,
        "agents": len(agents), "original_price": original, "run_start": str(run_start or ""),
        "dom": (today - run_start).days if run_start else None, "last_cut": str(last_cut or ""),
        "days_since_cut": (today - last_cut).days if last_cut else None,
    }


def phase_history(market: str, client=None, batch: int = 100) -> dict:
    from opensearch_mcp import OpenSearchMCP
    data = load_json(OUT / market / "listings.json")
    if not data:
        raise SystemExit("run --phase pull first")
    by_id: dict[int, list] = {}
    ids = [r["id"] for r in data["rows"]]
    floor = int((time.time() - 6 * 365 * 86400) * 1000)
    os_ = client or OpenSearchMCP()
    try:
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            q = {"bool": {"filter": [{"terms": {"dataflik_id": chunk}},
                                     {"range": {"recording_date": {"gte": floor, "format": "epoch_millis"}}}]}}
            for r in os_.page(q, [{"dataflik_id": "asc"}, {"listing_update_id": "asc"}], ["*"], 200_000, "mls-history"):
                by_id.setdefault(int(r["dataflik_id"]), []).append(r)
            logger.info("history %d/%d listings", min(i + batch, len(ids)), len(ids))
    finally:
        if client is None:
            os_.close()
    summary = {str(r["id"]): summarize_history(by_id.get(int(r["id"]), []), r.get("list_price")) for r in data["rows"]}
    save_json(OUT / market / "history.json", {"read": str(date.today()), "events": sum(map(len, by_id.values())),
                                              "summary": summary})
    logger.info("history: %d events on %d listings", sum(map(len, by_id.values())), len(by_id))
    return summary


# --------------------------------------------------------------------------------------------- buyers

def evidence_query(fips: list[str], months: int) -> dict:
    return {"bool": {
        "filter": [{"terms": {"fips": fips}}, {"term": {"property_type_group": "single_family_residence"}},
                   {"range": {"last_sale_date": {"gte": f"now-{int(months * 30.4)}d"}}},
                   {"terms": {"last_sale_investor_transaction_type": list(ACQ_TYPES) + ["Flip"]}},
                   {"range": {"last_sale_price": {"gte": 20_000}}}],
        "must_not": [{"term": {"is_last_sale_intrafamily": True}}]}}


def slim_evidence(src: dict) -> Optional[dict]:
    """One investor sale, or None when it cannot price a house: no location or size, or a deed priced at
    twice the parcel's value or more (a portfolio or takedown deed that carried other parcels)."""
    lat, lon = parse_point(src.get("location"))
    price, sqft, value = src.get("last_sale_price"), src.get("living_area_sqft"), src.get("property_value")
    if lat is None or not price or not sqft or sqft < 400:
        return None
    if value and price >= 2 * value:
        return None
    psf = price / sqft
    if psf < 10 or psf > 600:
        return None
    return {"id": src.get("dataflik_id"), "lat": lat, "lon": lon, "price": int(price), "psf": round(psf, 1),
            "date": str(ms_to_date(src.get("last_sale_date")) or ""), "type": src.get("last_sale_investor_transaction_type"),
            "sqft": sqft, "beds": src.get("bedroom_count"), "year": src.get("year_built"), "value": value,
            "cash": bool(src.get("is_cash_buyer")),
            "buyer": " ".join(x for x in (src.get("first_name"), src.get("last_name")) if x).strip(),
            "address": src.get("property_address") or "", "zip": str(src.get("property_zip") or "")[:5]}


def registry_index(path: Path = ROOT / "output" / "dispo_buyers") -> dict:
    """Our dispo buyer registry, keyed two ways so an investor sale near a listing can be tied to a buyer we
    know: by the address they bought (exact) and by normalized name (aliases included). `reach` holds the
    buyer keys with a phone on file. An install without the registry (or its address helper) gets an empty
    index: the offers still price, they just name no reachable buyer."""
    empty = {"by_addr": {}, "by_name": {}, "reach": set(), "buyers": 0}
    try:
        from mls_listings import address_key
    except Exception:  # noqa: BLE001 - a skill install carries no mls_listings or config
        return empty
    reg = load_json(path / "registry.json", []) or []
    if not reg:
        return empty
    phones = load_json(path / "phones.json", {}) or {}
    by_addr, by_name = {}, {}
    for b in reg:
        k = b.get("buyer_key")
        for n in [b.get("name")] + list(b.get("aliases") or []):
            if norm_name(n):
                by_name.setdefault(norm_name(n), k)
        for buy in b.get("buys") or []:
            a = buy.get("address") or ""
            z = re.search(r"(\d{5})\s*$", a)
            ak = address_key(a.split(",")[0], z.group(1) if z else "")
            if ak:
                by_addr[ak] = k
    reach = {k for k, v in phones.items() if (v or {}).get("phones")}
    return {"by_addr": by_addr, "by_name": by_name, "reach": reach, "buyers": len(reg)}


def tie_buyer(e: dict, idx: dict) -> Optional[str]:
    from mls_listings import address_key
    ak = address_key(e.get("address"), e.get("zip"))
    return (idx["by_addr"].get(ak) if ak else None) or idx["by_name"].get(norm_name(e.get("buyer")))


def phase_buyers(market: str, fips: list[str], months: int = 24, client=None) -> list[dict]:
    from opensearch_mcp import OpenSearchMCP
    os_ = client or OpenSearchMCP()
    try:
        q = evidence_query(fips, months)
        total = os_.count(q)
        raw = list(os_.page(q, [{"dataflik_id": "asc"}], EVIDENCE_FIELDS, total + 100))
    finally:
        if client is None:
            os_.close()
    if len(raw) != total:
        raise SystemExit(f"buyers read {len(raw)} of {total} investor sales; refusing a partial pull")
    idx = registry_index()
    ev = []
    for s in raw:
        e = slim_evidence(s)
        if e:
            e["buyer_key"] = tie_buyer(e, idx) if e["type"] in ACQ_TYPES else None
            ev.append(e)
    tied = sum(1 for e in ev if e["buyer_key"])
    save_json(OUT / market / "buyer_evidence.json", {"read": str(date.today()), "months": months, "sales": total,
                                                     "kept": len(ev), "tied": tied, "rows": ev})
    logger.info("buyers: %d investor sales in %d months, %d usable, %d tied to the dispo registry",
                total, months, len(ev), tied)
    return ev


def buyer_market(row: dict, ev: list[dict], reach: set, size_lo: float = 0.7, size_hi: float = 1.35) -> dict:
    """What investors paid for houses like this one, nearby. Radius widens 0.5 -> 1 -> 2 miles until 3
    similar-size sales show up. The as-is price prefers DISCOUNTED buys (paid at or under 90% of today's
    value), because a turnkey rental bought at full price says nothing about what an ugly house is worth."""
    lat, lon, sqft = row.get("lat"), row.get("lon"), row.get("sqft") or 0
    out = {"inv_n": 0, "inv_radius": None, "inv_psf": None, "investor_price": None, "arv_n": 0, "arv_psf": None,
           "arv": None, "buyers_near": 0, "reachable_near": 0, "top_buyers": "", "inv_basis": "no investor sales nearby"}
    if lat is None:
        out["inv_basis"] = "no location"
        return out
    box = [e for e in ev if abs(e["lat"] - lat) < 0.03 and abs(e["lon"] - lon) < 0.04]
    dist = [(miles(lat, lon, e["lat"], e["lon"]), e) for e in box]

    def similar(e):
        return not sqft or size_lo <= e["sqft"] / sqft <= size_hi

    for r in RADII_MI:
        acq = [e for d, e in dist if d <= r and e["type"] in ACQ_TYPES and similar(e)]
        if len(acq) >= 3:
            disc = [e for e in acq if e.get("value") and e["price"] <= 0.9 * e["value"]]
            use = disc if len(disc) >= 3 else acq
            psf = median([e["psf"] for e in use])
            out.update(inv_n=len(acq), inv_radius=r, inv_psf=round(psf, 1),
                       investor_price=round(psf * sqft) if sqft else round(median([e["price"] for e in use])),
                       inv_basis=(f"{len(use)} {'discounted ' if use is disc else ''}investor buys within {r:g} mi, "
                                  f"median ${psf:,.0f}/sq ft"))
            break
    for r in RADII_MI:
        flips = [e for d, e in dist if d <= r and e["type"] == "Flip" and similar(e)]
        if len(flips) >= 3:
            psf = median([e["psf"] for e in flips])
            out.update(arv_n=len(flips), arv_psf=round(psf, 1), arv=round(psf * sqft) if sqft else None)
            break
    r = out["inv_radius"] or 1.0
    near = [e for d, e in dist if d <= r and e["type"] in ACQ_TYPES]
    names = {}
    for e in near:
        key = e.get("buyer_key") or norm_name(e.get("buyer")) or None
        if key:
            names.setdefault(key, [e.get("buyer") or key, 0, key in reach])
            names[key][1] += 1
    out["buyers_near"] = len(names)
    out["reachable_near"] = sum(1 for v in names.values() if v[2])
    top = sorted(names.values(), key=lambda v: (-v[2], -v[1]))[:4]
    out["top_buyers"] = "; ".join(f"{v[0].title()}{' (phone)' if v[2] else ''}" for v in top)
    return out


# --------------------------------------------------------------------------------------------- mortgage model

MH_FIELDS = ["dataflik_id", "first_loan_amount", "first_loan_type", "first_loan_recording_date", "first_loan_due_date",
             "first_lender_name", "second_loan_amount", "second_loan_type", "second_loan_recording_date",
             "second_loan_due_date", "second_lender_name", "is_deleted"]


def recorded_loans(mh_rows: list[dict]) -> list[dict]:
    """Every loan slot on every mortgage-history row, flattened: original amount, recording and due dates."""
    out = []
    for r in mh_rows:
        if r.get("is_deleted"):
            continue
        for slot in ("first", "second"):
            amt, rec = r.get(f"{slot}_loan_amount"), ms_to_date(r.get(f"{slot}_loan_recording_date"))
            if amt and rec:
                out.append({"amount": amt, "recorded": rec, "due": ms_to_date(r.get(f"{slot}_loan_due_date")),
                            "lender": r.get(f"{slot}_lender_name") or ""})
    return out


def join_loan(open_loan: dict, recorded: list[dict], used: Optional[set] = None, days: int = 4) -> Optional[dict]:
    """The recorded loan behind one open loan on the index: same recording date, then the amount closest to
    the index's balance (a first and a second loan often record the same day), each recorded loan used once.
    A recorded amount below 90% of the index balance or above 3x it is the wrong loan and is not used."""
    try:
        d = date.fromisoformat(open_loan.get("recorded") or "")
    except ValueError:
        return None
    used = used if used is not None else set()
    bal = open_loan.get("balance") or 0
    hits = [r for r in recorded if id(r) not in used and abs((r["recorded"] - d).days) <= days]
    if bal:
        hits = [r for r in hits if 0.9 * bal <= r["amount"] <= 3 * bal]
    if not hits:
        return None
    best = min(hits, key=lambda r: (abs((r["recorded"] - d).days), abs(r["amount"] - bal)))
    used.add(id(best))
    return best


def phase_mortgage(market: str, fips: list[str], client=None, workers: int = 6) -> dict:
    """Model every open loan on the listings: HMDA match where one exists, otherwise Freddie Mac in the lock
    week plus a spread calibrated on the HMDA matches themselves. Writes mortgage_model.json."""
    from concurrent.futures import ThreadPoolExecutor
    from opensearch_mcp import OpenSearchMCP
    import mortgage_model as mm

    data = load_json(OUT / market / "listings.json")
    if not data:
        raise SystemExit("run --phase pull first")
    with_loans = [r for r in data["rows"] if r.get("loans")]
    mm.fetch_pmms()
    for c in fips:
        for y in mm.HMDA_YEARS:
            mm.fetch_hmda(c, y)
    model = mm.Model(fips, mm.Rates(mm.fetch_pmms()))

    mh_p = OUT / market / "mortgage_history.json"
    mh_doc = load_json(mh_p) or {}
    ids = [r["id"] for r in with_loans]
    if mh_doc.get("read") == str(date.today()) and set(map(str, ids)) <= set(mh_doc.get("rows", {})):
        by_id = {int(k): v for k, v in mh_doc["rows"].items()}
    else:
        by_id: dict[int, list] = {i: [] for i in ids}
        os_ = client or OpenSearchMCP()
        try:
            for i in range(0, len(ids), 100):
                q = {"terms": {"dataflik_id": ids[i:i + 100]}}
                for h in os_.page(q, [{"dataflik_id": "asc"}, {"transaction_id": "asc"}], MH_FIELDS, 50_000,
                                  "mortgage-history"):
                    by_id.setdefault(int(h["dataflik_id"]), []).append(h)
        finally:
            if client is None:
                os_.close()
        save_json(mh_p, {"read": str(date.today()), "rows": {str(k): v for k, v in by_id.items()}})

    tcache_p = mm.CACHE / "tracts.json"
    tcache = load_json(tcache_p, {}) or {}
    pts = [(r["lat"], r["lon"]) for r in with_loans if r.get("lat") is not None]
    todo = [p for p in pts if f"{p[0]:.6f},{p[1]:.6f}" not in tcache]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, _ in enumerate(ex.map(lambda p: mm.tract_ids(p[0], p[1], tcache), todo), 1):
            if n % 100 == 0:
                save_json(tcache_p, tcache)
                logger.info("tracts %d/%d", n, len(todo))
    save_json(tcache_p, tcache)

    jobs = []   # (listing id, open loan, recorded loan, tracts)
    for r in with_loans:
        rec = recorded_loans(by_id.get(int(r["id"]), []))
        tr = tcache.get(f"{r['lat']:.6f},{r['lon']:.6f}", {}) if r.get("lat") is not None else {}
        used: set = set()
        for l in sorted(r["loans"], key=lambda x: -(x.get("balance") or 0)):   # the big loan claims its record first
            jobs.append((r["id"], l, join_loan(l, rec, used), tr))

    # Pass 1: exact HMDA matches calibrate the spread for everything else.
    matches = []
    for _, l, rl, tr in jobs:
        if not rl or (l.get("type") or "").lower() in mm.UNMODELED:
            continue
        m1 = model.loan(rl["amount"], rl["recorded"], rl["due"], l["type"], tr)
        if m1 and m1["rate_source"] == "HMDA match":
            matches.append({"recorded": rl["recorded"], "typ": mm.TYPE_CODE.get(l["type"].lower(), 1),
                            "term": m1["term_months"], "rate": m1["rate"], "index_rate": l.get("rate")})
    check = model.choose_fallback(matches)
    spreads = {str(k): v for k, v in model.dated.items()}

    out = {}
    stats = {"loans": len(jobs), "joined": 0, "modeled": 0, "hmda_match": 0, "hmda_close": 0, "fallback": 0}
    for lid, l, rl, tr in jobs:
        rec = {"recorded": l.get("recorded"), "type": l.get("type"), "index_rate": l.get("rate"),
               "index_balance": l.get("balance"), "index_payment": l.get("payment")}
        if rl:
            stats["joined"] += 1
            m2 = model.loan(rl["amount"], rl["recorded"], rl["due"], l.get("type"), tr, index_rate=l.get("rate"))
            if m2:
                stats["modeled"] += 1
                src = m2["rate_source"]
                stats["hmda_match" if src == "HMDA match" else "hmda_close" if src.startswith("HMDA match") else "fallback"] += 1
                rec.update(m2, lender=rl["lender"], due=str(rl["due"] or ""))
        out.setdefault(str(lid), []).append(rec)
    d, r30 = model.rates.latest()
    doc = {"built": str(date.today()), "market_rate": {"date": str(d), "rate30": r30}, "hmda_loans": model.hmda_loans,
           "stats": stats, "calibrated_spreads": spreads, "check": check, "loans": out}
    save_json(OUT / market / "mortgage_model.json", doc)
    logger.info("mortgage: %s; check %s", stats, check)
    return doc


def overlay_model(row: dict, modeled: list[dict]) -> dict:
    """The listing with each open loan replaced by its modeled numbers (the index's own estimate kept beside
    it). A loan the model amortizes to zero is dropped: the index still lists it, but it is paid off."""
    by_rec = {(m.get("recorded"), m.get("type")): m for m in modeled or []}
    loans, notes = [], []
    for l in row.get("loans") or []:
        m = by_rec.get((l.get("recorded"), l.get("type")))
        if m and m.get("rate") is not None:
            if m.get("paid_off"):
                notes.append(f"{l.get('type')} from {(l.get('recorded') or '')[:4]} amortizes to zero")
                continue
            loans.append({**l, "rate": m["rate"], "balance": m["balance"], "payment": m["pi"] + m["mi"], "pi": m["pi"],
                          "mi": m["mi"], "mi_note": m.get("mi_note", ""), "rate_source": m["rate_source"],
                          "term": m["term_months"], "original": m["original"], "index_rate": l.get("rate"),
                          "index_balance": l.get("balance"), "index_payment": l.get("payment")})
        else:
            loans.append({**l, "rate_source": "index estimate"})
    total_bal = sum(l.get("balance") or 0 for l in loans)
    total_pay = sum(l.get("payment") or 0 for l in loans)
    return {**row, "loans": loans, "total_balance": total_bal, "total_payment": total_pay, "model_notes": notes}


# --------------------------------------------------------------------------------------------- zillow (optional)

def zillow_details(row: dict, api_key: str) -> dict:
    import requests
    addr = f"{row['address']}, {row['city']}, {row['state']} {row['zip']}"
    for attempt in range(3):
        r = requests.get("https://api.openwebninja.com/realtime-zillow-data/property-details-address",
                         params={"address": addr}, headers={"x-api-key": api_key}, timeout=60)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        d = r.json().get("data") or {}
        a = d.get("attributionInfo") or {}
        lic = next((x.get("memberStateLicense") for x in a.get("listingAgents") or [] if x.get("memberStateLicense")), "")
        sub = d.get("listingSubType") or {}
        return {
            "status": d.get("homeStatus") or "", "true_status": a.get("trueStatus") or "",
            "days_on_zillow": d.get("daysOnZillow"), "remarks": (d.get("description") or "")[:2000],
            "agent": a.get("agentName") or "", "agent_phone": fmt_phone(a.get("agentPhoneNumber")),
            "agent_email": a.get("agentEmail") or "", "agent_license": a.get("agentLicenseNumber") or lic or "",
            "broker": a.get("brokerName") or "", "broker_phone": fmt_phone(a.get("brokerPhoneNumber")),
            "price": d.get("price"), "zestimate": d.get("zestimate"), "rent": d.get("rentZestimate"),
            "url": d.get("hdpUrl") or "",
            "bank_owned": bool(sub.get("isBankOwned")), "foreclosure": bool(sub.get("isForeclosure")),
            "new_home": bool(sub.get("isNewHome")), "auction": bool(sub.get("isForAuction")),
        }
    return {"error": "retries exhausted"}


ZILLOW_READS = {"last": 0}      # paid Zillow calls the last phase_zillow made


def phase_zillow(market: str, limit: int) -> dict:
    """Details for the top `limit` offer rows not already cached. Paid, so it reads the ranked offers."""
    key = (os.environ.get("OPENWEBNINJA_API_KEY") or "").strip()
    if not key:
        try:
            import config
            key = getattr(config, "OPENWEBNINJA_API_KEY", "") or ""
        except Exception:  # noqa: BLE001 - a skill install carries no config module
            key = ""
    if not key:
        raise RuntimeError("OPENWEBNINJA_API_KEY is not set; the Zillow check needs it (it is optional, skip --zillow)")
    offers = load_json(OUT / market / "offers.json")
    if not offers:
        raise RuntimeError("run --phase offers first (zillow reads the ranked list)")
    cache_p = OUT / market / "zillow.json"
    cache = load_json(cache_p, {})
    todo = [o for o in offers["rows"] if str(o["id"]) not in cache][:limit]
    ZILLOW_READS["last"] = 0
    for n, o in enumerate(todo, 1):
        ZILLOW_READS["last"] = n           # paid calls this run, for a caller that books the spend
        cache[str(o["id"])] = {"read": str(date.today()), **zillow_details(o, key)}
        if n % 10 == 0:
            save_json(cache_p, cache)
            logger.info("zillow %d/%d", n, len(todo))
    save_json(cache_p, cache)
    logger.info("zillow: %d read this run, %d cached", len(todo), len(cache))
    return cache


# --------------------------------------------------------------------------------------------- screen + offers

def remark_hits(text: str) -> list[str]:
    t = (text or "").lower()
    return [w for w in REMARK_WORDS if w in t]


def motivation(row: dict, hist: dict, z: Optional[dict] = None, pol: Optional[Policy] = None) -> tuple[int, list[str]]:
    """0-100. Time on market and price cuts carry most of it, because those are the seller telling the
    market the price is wrong; distress flags, remarks and the off-market AI score add to it."""
    pol = pol or Policy()
    pts, why = 0.0, []
    ai = row.get("ai_off")
    if ai is not None and pol.ai_weight and ai > pol.ai_floor:
        pts += min(pol.ai_weight, (ai - pol.ai_floor) * pol.ai_weight / max(1, 90 - pol.ai_floor))
        if ai >= 70:
            why.append(f"AI off-market score {ai:.0f}")
    dom = hist.get("dom")
    if dom is not None:
        pts += min(30, dom / 6)                       # 180 days = full 30
        if dom >= 90:
            why.append(f"{dom} days on market")
    if hist.get("cuts"):
        pts += min(15, 5 * hist["cuts"])
        why.append(f"{hist['cuts']} price cut{'s' if hist['cuts'] != 1 else ''}")
    if hist.get("cut_pct"):
        pts += min(15, hist["cut_pct"] * 100)
        if hist["cut_pct"] >= 0.05:
            why.append(f"down {hist['cut_pct']:.0%} from {money(hist.get('original_price'))}")
    if hist.get("relists"):
        pts += min(10, 5 * hist["relists"])
        why.append("expired and relisted")
    if hist.get("fell_through"):
        pts += 8
        why.append("back on market after a pending")
    if hist.get("agents", 0) >= 2:
        pts += 4
        why.append("changed agents")
    flag_pts = 0
    for f in row.get("flags") or []:
        if f in MOTIVE_FLAGS:
            label, p = MOTIVE_FLAGS[f]
            flag_pts += p
            if p >= 4:
                why.append(label)
    pts += min(20, flag_pts)
    if z and not z.get("error"):
        hits = remark_hits(z.get("remarks"))
        pts += min(10, sum(REMARK_WORDS[h] for h in hits))
        if hits:
            why.append("remarks: " + ", ".join(hits[:4]))
        if z.get("bank_owned") or z.get("foreclosure"):
            pts += 6
            why.append("bank owned" if z.get("bank_owned") else "foreclosure listing")
    return int(round(min(100, pts))), why


def money(x) -> str:
    return f"${x:,.0f}" if isinstance(x, (int, float)) and x else ""


def screen(row: dict, pol: Policy, z: Optional[dict] = None, today: Optional[date] = None) -> Optional[str]:
    """Why a listing gets no offer, or None. Each reason is counted on the Excluded sheet."""
    today = today or date.today()
    lp = row.get("list_price") or 0
    if not row.get("has_address") or not re.match(r"^[1-9]", row.get("address") or ""):   # house number 0 = no postal address
        return "no street address"
    if not lp:
        return "no list price"
    if lp > pol.max_list:
        return f"list over {money(pol.max_list)}"
    if lp < pol.min_list:
        return f"list under {money(pol.min_list)}"
    if (row.get("year") or 0) >= today.year - pol.new_build_years + 1 or (z and z.get("new_home")):
        return "new construction"
    if row.get("corporate") and BUILDER_WORDS.search(row.get("owner") or "") and (row.get("year") or 0) >= today.year - 5:
        return "builder spec home"
    if z and not z.get("error") and z.get("status") and z["status"] != "FOR_SALE":
        return f"no longer active on Zillow ({z['status'].lower().replace('_', ' ')})"
    return None


def cash_offer(row: dict, score: int, pol: Policy, bm: Optional[dict] = None) -> dict:
    """What our end buyer pays for a house like this nearby, minus our fee, never above his 70%-of-list
    anchor (65% when motivated). With no investor sales nearby it falls back to his formula on the AVM:
    value x 60% - repairs - fee. Above the cash cap there is no cash offer: only a creative lane can fit.
    `spread_at_list_rule` says whether his plain 70%-of-list offer would even leave our fee."""
    lp = row["list_price"]
    repairs = (row.get("sqft") or 0) * pol.repair_psf
    blank = {"cash_offer": None, "cash_pct_of_list": None, "repair_placeholder": round(repairs), "spread_at_list_rule": None,
             "arv_rule": None, "over_arv_rule": False}
    if lp > pol.cash_max_list:
        return {**blank, "cash_basis": f"list over the {money(pol.cash_max_list)} cash cap"}
    share = pol.cash_pct_list_motivated if score >= pol.motivated_score else pol.cash_pct_list
    by_list = lp * share
    buyer_price = (bm or {}).get("investor_price")
    arv = (bm or {}).get("arv")
    arv_rule = round(arv * pol.cash_rule_pct - repairs - pol.fee) if arv else None
    if buyer_price:
        by_buyers = buyer_price - pol.fee
        offer = round_down(min(by_list, by_buyers))
        basis = (f"buyers pay about {money(buyer_price)} ({bm['inv_basis']}) less our {money(pol.fee)} fee"
                 if by_buyers < by_list else f"{share:.0%} of list (buyers pay about {money(buyer_price)})")
        spread = round(buyer_price - by_list)
    else:
        value = row.get("value") or 0
        by_math = value * pol.cash_rule_pct - repairs - pol.fee if value else None
        offer = round_down(min(by_list, by_math) if by_math is not None else by_list)
        basis = ("no investor sales nearby: value x 60% - repairs - fee" if by_math is not None and by_math < by_list
                 else f"{share:.0%} of list (no investor sales nearby)")
        spread = None
    if offer <= 0:
        return {**blank, "cash_basis": "no room after repairs and fee", "arv_rule": arv_rule}
    if offer < lp * pol.cash_min_pct_list:
        return {**blank, "arv_rule": arv_rule, "spread_at_list_rule": spread, "watch_offer": offer,
                "watch_trigger": round_down(offer / pol.cash_min_pct_list),
                "cash_basis": (f"a cash offer here is {offer / lp:.0%} of list ({basis}); "
                               f"send it if the list drops to {money(round_down(offer / pol.cash_min_pct_list))}")}
    return {"cash_offer": offer, "cash_pct_of_list": round(offer / lp, 3) if lp else None, "cash_basis": basis,
            "repair_placeholder": round(repairs), "spread_at_list_rule": spread, "arv_rule": arv_rule,
            "over_arv_rule": bool(arv_rule is not None and offer > arv_rule)}


def amort_payment(principal: float, rate: float, years: int) -> float:
    r, n = rate / 12, years * 12
    return principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)


def creative_offer(row: dict, pol: Policy) -> dict:
    """Subject-to when the seller's loan is worth keeping, seller finance when the house is free and clear,
    nothing creative otherwise. Every figure rides on ESTIMATED balances and rates (the market rate in the
    month the loan recorded), so the agent conversation confirms the payoff before any number is binding."""
    lp = row["list_price"]
    value = row.get("value") or lp
    loans = [l for l in row.get("loans") or [] if (l.get("balance") or 0) > 0]
    term_loans = [l for l in loans if (l.get("type") or "").lower() not in SHORT_LIENS]
    heloc = sum(l["balance"] for l in loans if (l.get("type") or "").lower() in SHORT_LIENS)   # paid off at closing
    tax_m = (row.get("tax") or value * 0.006) / 12
    ins_m = value * pol.insurance_pct / 12
    rent = row.get("rent") or 0
    commission = lp * pol.listing_commission           # we pay it on top, as he does
    closing = lp * pol.buyer_closing_pct
    out = {"lane": "", "creative_price": None, "loan_balance": round(sum(l["balance"] for l in loans)) or 0,
           "loan_rate": None, "loan_types": ", ".join(sorted({l.get("type") or "?" for l in loans})),
           "piti": None, "cash_to_seller": None, "seller_carry": None, "cash_in": None, "cash_flow": None,
           "coc": None, "creative_note": ""}

    def finish(cash_seller: float, piti: float) -> None:
        out["piti"] = round(piti)
        out["cash_in"] = round(cash_seller + commission + closing)
        if rent:
            out["cash_flow"] = round(rent * (1 - pol.rent_haircut) - piti)
            out["coc"] = round(out["cash_flow"] * 12 / out["cash_in"], 3) if out["cash_in"] > 0 else None

    if not loans:
        if "is_free_and_clear" not in (row.get("flags") or []):
            out["creative_note"] = "No loan on record but not flagged free and clear; confirm before a seller-finance offer."
            return out
        price = round_down(lp * pol.sf_price_pct)
        down = round_down(price * pol.sf_down_pct)
        pi = amort_payment(price - down, pol.sf_rate, pol.sf_years)
        out.update(lane="Seller finance", creative_price=price, cash_to_seller=down,
                   creative_note=(f"Free and clear. {money(price)} with {money(down)} down, the rest paid to the seller "
                                  f"at {pol.sf_rate:.0%} over {pol.sf_years} years ({money(pi)} a month); "
                                  "we pay the commission."))
        finish(down, pi + tax_m + ins_m)
        return gate_cash_flow(out, pol)
    if not term_loans:
        out["creative_note"] = ("Only a credit line or construction loan of record: nothing to take over, "
                                "and a credit line's balance estimate is a limit, not a payoff.")
        return out
    bal = sum(l["balance"] for l in term_loans)
    rate = sum((l.get("rate") or 0) * l["balance"] for l in term_loans) / bal if bal else None
    pay = sum(l.get("payment") or 0 for l in term_loans)
    out["loan_rate"] = round(rate, 2) if rate else None
    if bal < pol.subto_min_balance:
        out["creative_note"] = f"Loan estimate {money(bal)} is small enough to pay off; cash lane only."
        return out
    if rate is None or rate > pol.subto_max_rate:
        out["creative_note"] = (f"Existing rate about {rate:.2f}%, above the {pol.subto_max_rate:.1f}% keep line."
                                if rate else "No rate estimate.")
        return out
    price = round_down(lp * pol.subto_price_pct)
    debt = bal + heloc
    if row.get("blanket") or debt > 1.25 * price:
        out["creative_note"] = (f"Owes about {money(debt)} against a {money(price)} price: a loan on more than this house "
                                "(a blanket or portfolio loan) cannot be taken over on one house. Confirm the payoff.")
        return out
    gap = price - debt                                  # what the seller nets: his "difference between owed and list"
    piti = pay + tax_m + ins_m
    loan_line = f"keep the {rate:.2f}% loan (about {money(bal)}, {money(pay)} a month)"
    heloc_line = (f" A credit line or construction loan of record ({money(heloc)}) has to be paid off at closing "
                  "or confirmed closed." if heloc else "")
    if gap <= 0:
        out.update(lane="Subject-to (no equity)", creative_price=price, cash_to_seller=0,
                   creative_note=(f"Owes about {money(debt)} against a {money(price)} price: {loan_line}, the seller walks "
                                  f"with no cash and we pay the commission. Ask about a short payoff.{heloc_line}"))
        finish(0, piti)
    elif gap <= pol.subto_max_cash:
        cash = round_down(gap, 500)
        out.update(lane="Subject-to", creative_price=price, cash_to_seller=cash,
                   creative_note=f"{loan_line[0].upper() + loan_line[1:]}, {money(cash)} to the seller, we pay the commission.{heloc_line}")
        finish(cash, piti)
    elif gap - pol.subto_max_cash <= price * pol.subto_max_carry_pct:
        cash = pol.subto_max_cash
        carry = round_down(gap - cash)
        carry_pay = amort_payment(carry, pol.carry_rate, pol.carry_years)
        out.update(lane="Subject-to + seller carry", creative_price=price, cash_to_seller=cash, seller_carry=carry,
                   carry_payment=round(carry_pay),
                   creative_note=(f"{loan_line[0].upper() + loan_line[1:]}, {money(cash)} to the seller at closing, the other "
                                  f"{money(carry)} of equity carried by the seller as a note at {pol.carry_rate:.0%} over "
                                  f"{pol.carry_years} years ({money(carry_pay)} a month), we pay the commission.{heloc_line}"))
        finish(cash, piti + carry_pay)
    else:
        out["creative_note"] = f"About {money(gap)} of equity over the loan: too much to carry, the cash lane fits better."
    return gate_cash_flow(out, pol)


def gate_cash_flow(out: dict, pol: Policy) -> dict:
    """A creative offer that does not cash flow at its own price is not worth sending. With no rent estimate
    it stays, marked, because the agent can tell us what it rents for."""
    if not out["lane"]:
        return out
    if out.get("coc") is None:
        out["creative_note"] += " No rent estimate: confirm the rent before relying on this."
        return out
    if out["lane"].startswith("Subject-to") and not pol.subto_gate_cash_flow:
        if out["coc"] <= pol.creative_min_coc:
            out["weak_cash_flow"] = True
            out["creative_note"] += (f" As a rental this returns {out['coc']:.0%} cash on cash "
                                     f"({money(out['cash_flow']) if (out['cash_flow'] or 0) > 0 else 'a loss of ' + money(-(out['cash_flow'] or 0))} a month): "
                                     "the exit is a wrap or a resale of the loan, not a rental.")
        return out
    if out["coc"] <= pol.creative_min_coc:
        return {**out, "lane": "", "dropped_lane": out["lane"],
                "creative_note": (f"{out['lane']} at {money(out['creative_price'])} does not cash flow "
                                  f"({money(out['cash_flow'])} a month on {money(out['cash_in'])} in).")}
    return out


def mortgage_summary(row: dict, pol: Optional[Policy] = None) -> dict:
    """The seller's mortgage on every row whatever the offer: what is left across all open loans, the
    balance-weighted rate on the term loans (a credit line only when that is all there is), the monthly
    principal and interest, mortgage insurance, the full housing payment (with taxes and insurance), where the
    rate came from, and one line per loan. Modeled loans (see mortgage_model) carry `pi`, `mi`, `rate_source`;
    a loan the model could not price keeps the index's estimate and says so."""
    pol = pol or Policy()
    loans = sorted((l for l in row.get("loans") or [] if (l.get("balance") or 0) > 0), key=lambda l: -l["balance"])
    if not loans:
        fc = "is_free_and_clear" in (row.get("flags") or [])
        note = "; ".join(row.get("model_notes") or [])
        return {"mtg_balance": 0 if fc else None, "mtg_rate": None, "mtg_payment": 0 if fc else None, "mtg_mi": None,
                "mtg_full": None, "mtg_source": "",
                "mtg_detail": ("free and clear" if fc else "no loan on record (not flagged free and clear)")
                + (f"; {note}" if note else "")}
    rated = [l for l in loans if l.get("rate")]
    is_short = lambda l: (l.get("type") or "").lower() in SHORT_LIENS
    term = [l for l in rated if not is_short(l)] or rated
    tb = sum(l["balance"] for l in term)
    rate = round(sum(l["rate"] * l["balance"] for l in term) / tb, 2) if tb else None
    # A construction loan's index "payment" amortizes it over its one-year term ($17,068 on one house): it is a
    # payoff at the sale, never a monthly payment someone takes over, so it stays out of P&I.
    # The same goes for a credit line: the index amortizes the LIMIT, and either one is paid off at the sale.
    pi = sum((l.get("pi") if l.get("pi") is not None else l.get("payment")) or 0 for l in loans if not is_short(l))
    mi = sum(l.get("mi") or 0 for l in loans)
    value = row.get("value") or row.get("list_price") or 0
    full = pi + mi + (row.get("tax") or value * 0.006) / 12 + value * pol.insurance_pct / 12
    sources = sorted({l.get("rate_source") or "index estimate" for l in term})

    def line(l):
        if not l.get("rate"):
            return f"{l.get('type') or 'Loan'} {money(l['balance'])}"
        s = f"{l.get('type') or 'Loan'} {l['rate']:.2f}% ({l.get('rate_source') or 'index estimate'}) {money(l['balance'])}"
        if l.get("original"):
            s += f" left of {money(l['original'])}"
        p = l.get("pi") if l.get("pi") is not None else l.get("payment")
        if p:
            s += f", {money(p)} P&I"
        if l.get("mi"):
            s += f" + {money(l['mi'])} {l.get('mi_note') or 'MI'}"
        if l.get("term"):
            s += f", {l['term'] // 12}-yr"
        if l.get("recorded"):
            s += f" from {l['recorded'][:4]}"
        if l.get("index_rate") is not None and l.get("index_rate") != l.get("rate"):
            s += f" (index said {l['index_rate']:.2f}%)"
        if (l.get("type") or "").lower() == "credit line":
            s += " (credit line: limit, not payoff)"
        elif (l.get("type") or "").lower() in ("construction", "commercial"):
            s += f" ({l['type'].lower()} loan: paid off at the sale, not in the monthly payment)"
        return s
    detail = "; ".join(line(l) for l in loans) + ("; " + "; ".join(row["model_notes"]) if row.get("model_notes") else "")
    lp = row.get("list_price") or 0
    heloc = sum(l["balance"] for l in loans if is_short(l))     # credit lines, construction, commercial
    owed = sum(l["balance"] for l in loans) - heloc
    if row.get("blanket"):
        detail += f"; the same loan sits on {row['blanket']} listed houses: a blanket loan, not this house's alone"
    elif lp and owed > 1.05 * lp:
        detail += "; owes more than the list price: confirm the payoff (a short sale, or a loan on more than this house)"
    return {"mtg_balance": round(sum(l["balance"] for l in loans)), "mtg_rate": rate, "mtg_payment": round(pi) or None,
            "mtg_mi": round(mi) or None, "mtg_full": round(full), "mtg_source": ", ".join(sources), "mtg_detail": detail,
            # What they owe against the asking price: the closer to 100%, the less a cash buyer can pay and the
            # more a subject-to is the only way the seller walks away clean. Credit lines are kept apart because
            # their balance estimate is a limit, not a payoff.
            "owed_pct": round(owed / lp, 3) if lp and owed else None,
            "owed_pct_all": round((owed + heloc) / lp, 3) if lp and heloc else None}


def phase_offers(market: str, pol: Policy, today: Optional[date] = None) -> dict:
    data = load_json(OUT / market / "listings.json")
    hist = (load_json(OUT / market / "history.json") or {}).get("summary") or {}
    zc = load_json(OUT / market / "zillow.json", {})
    mm_doc = load_json(OUT / market / "mortgage_model.json") or {}
    modeled = mm_doc.get("loans") or {}
    if not modeled:
        logger.warning("no mortgage_model.json: loans use the index's own estimates (run --phase mortgage)")
    ev_doc = load_json(OUT / market / "buyer_evidence.json") or {}
    ev = ev_doc.get("rows") or []
    reach = registry_index()["reach"] if ev else set()
    if not data:
        raise SystemExit("run --phase pull first")
    if not hist:
        logger.warning("no history.json: days on market and price cuts fall back to the listed-since date only")
    if not ev:
        logger.warning("no buyer_evidence.json: cash offers fall back to the AVM formula (run --phase buyers)")
    # A loan (same recording date and amount) on two or more listed houses is a blanket or portfolio loan.
    loan_key = lambda l: (l.get("recorded"), round((l.get("balance") or 0) / 1000)) if (l.get("balance") or 0) > 50_000 else None
    shared = {}
    for r in data["rows"]:
        for k in {loan_key(l) for l in r.get("loans") or []} - {None}:
            shared[k] = shared.get(k, 0) + 1
    rows, excluded = [], []
    for r in data["rows"]:
        n_blanket = max((shared.get(loan_key(l), 0) for l in r.get("loans") or []), default=0)
        if n_blanket >= 2:
            r = {**r, "blanket": n_blanket}
        if modeled.get(str(r["id"])):
            r = overlay_model(r, modeled[str(r["id"])])
        z = zc.get(str(r["id"]))
        if z and not z.get("error") and not r.get("rent") and z.get("rent"):
            r = {**r, "rent": z["rent"]}          # the API source has no rent estimate; Zillow's fills it
        h = hist.get(str(r["id"])) or summarize_history([], r.get("list_price"), today)
        if h.get("dom") is None and r.get("listed_since"):
            h["dom"] = ((today or date.today()) - date.fromisoformat(r["listed_since"])).days
        if h.get("dom") is None and r.get("dom") is not None:
            h["dom"] = r["dom"]                    # the API screen's days-on-market bucket, until a detail is read
        why = screen(r, pol, z, today)
        if why:
            excluded.append({**r, "reason": why, "owner_type": owner_type(r)})
            continue
        score, reasons = motivation(r, h, z, pol)
        bm = buyer_market(r, ev, reach) if ev else {}
        row = {**r, **h, **bm, **mortgage_summary(r, pol), "owner_type": owner_type(r), "score": score, "why": "; ".join(reasons),
               **cash_offer(r, score, pol, bm), **creative_offer(r, pol)}
        if r.get("detailed") is False:
            # API source: screened from search alone (not shortlisted, or not in the account), so there is no
            # agent, no listing history and no loan. Its preliminary cash number goes on the More listings sheet.
            excluded.append({**row, "reason": "screened only: no details read (not shortlisted, or not in your account)",
                             "reason_group": "screened only (no details read)"})
            continue
        if row["cash_offer"] is None and not row["lane"]:
            if row.get("watch_offer"):
                excluded.append({**row, "reason": row["cash_basis"], "reason_group": "price watch: cash under half of list"})
            else:
                excluded.append({**r, "reason": f"no offer fits ({row['cash_basis']}; {row['creative_note'] or 'no creative lane'})"[:220],
                                 "reason_group": "no offer fits", "owner_type": row["owner_type"]})
            continue
        if z and not z.get("error"):
            row.update(remarks=z.get("remarks", ""), broker=z.get("broker", ""), broker_phone=z.get("broker_phone", ""),
                       agent_email=z.get("agent_email", ""), agent_license=z.get("agent_license", ""),
                       zillow_url=z.get("url", ""), days_on_zillow=z.get("days_on_zillow"))
            if not row.get("agent_phone") and z.get("agent_phone"):
                row["agent_phone"] = z["agent_phone"]
        rows.append(row)
    # Workable offers first: a listing whose only offer is a subject-to that loses money as a rental sorts last.
    rows.sort(key=lambda x: (bool(not x.get("cash_offer") and x.get("weak_cash_flow")), -x["score"],
                             -(x.get("cut_pct") or 0), -(x.get("dom") or 0)))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    out = {"built": str(today or date.today()), "policy": asdict(pol), "listings": len(data["rows"]),
           "source": data.get("source") or "cluster", "rows": rows, "excluded": excluded}
    save_json(OUT / market / "offers.json", out)
    logger.info("offers: %d listings, %d with offers, %d excluded", len(data["rows"]), len(rows), len(excluded))
    return out


# --------------------------------------------------------------------------------------------- agents + outreach

def agent_key(row: dict) -> str:
    return phone_digits(row.get("agent_phone")) or (row.get("agent") or "").strip().lower() or "unknown"


def agents_roster(rows: list[dict]) -> list[dict]:
    """One row per listing agent: how many of the offer list they hold. An agent with several stale
    listings is a relationship, not a one-off: one good conversation reaches every one of them."""
    by: dict[str, dict] = {}
    for r in rows:
        k = agent_key(r)
        a = by.setdefault(k, {"agent": r.get("agent") or "", "phone": r.get("agent_phone") or "",
                              "email": r.get("agent_email") or "", "office": r.get("office") or "",
                              "broker_phone": r.get("broker_phone") or "", "listings": 0, "creative": 0,
                              "motivated": 0, "top_score": 0, "addresses": []})
        a["listings"] += 1
        a["creative"] += 1 if r.get("lane") else 0
        a["motivated"] += 1 if r["score"] >= 45 else 0
        a["top_score"] = max(a["top_score"], r["score"])
        a["addresses"].append(r["address"])
        for f in ("email", "broker_phone", "phone"):
            if not a[f] and r.get({"phone": "agent_phone", "email": "agent_email"}.get(f, f)):
                a[f] = r.get({"phone": "agent_phone", "email": "agent_email"}.get(f, f))
    out = sorted(by.values(), key=lambda a: (-a["motivated"], -a["listings"], -a["top_score"]))
    for a in out:
        a["addresses"] = "; ".join(a["addresses"][:12]) + (" ..." if len(a["addresses"]) > 12 else "")
    return out


def default_sender() -> str:
    """Who the drafts are from: MLS_OFFERS_SENDER, else nobody named ("I'm a local investor")."""
    return (os.environ.get("MLS_OFFERS_SENDER") or "").strip()


def offer_text(row: dict, sender: str = "") -> str:
    """The first message to the listing agent: both offers in one short text, no dash, one question.
    It says plainly what we are (a local investor) and never claims a lender pre-approval: his script does,
    and an agent who later learns the contract is being assigned has a fair complaint."""
    hi = first_name(row.get("agent")) or "there"
    street = row["address"]
    lane = row.get("lane") or ""
    cash = row.get("cash_offer")
    parts = []
    if cash:
        parts.append(f"I can do {money(cash)} cash, as-is, closing in 14 days")
    if lane.startswith("Subject-to"):
        if row.get("seller_carry"):
            parts.append(f"I can pay {money(row['creative_price'])} by keeping your seller's current loan in place, paying "
                         f"them {money(row['cash_to_seller'])} at closing plus {money(row['seller_carry'])} over time, and covering your commission")
        elif row.get("cash_to_seller"):
            parts.append(f"I can pay {money(row['creative_price'])} by keeping your seller's current loan in place, paying "
                         f"them {money(row['cash_to_seller'])} at closing, and covering your commission")
        else:
            parts.append("I can take over your seller's current loan payments so they walk away clean, and cover your commission")
    elif lane == "Seller finance":
        parts.append(f"I can pay {money(row['creative_price'])} with {money(row['cash_to_seller'])} down if your seller "
                     "would carry the rest, and cover your commission")
    body = parts[0] + (". Or " + parts[1] if len(parts) > 1 else "")
    ask = "Would your seller look at either one?" if len(parts) > 1 else "Would your seller consider it?"
    who = f"this is {sender}, a local investor" if sender else "I'm a local investor"
    return f"Hi {hi}, {who}. I'd like to make an offer on {street}. {body}. {ask}"


def phase_export(market: str, sender: str = "") -> Path:
    offers = load_json(OUT / market / "offers.json")
    if not offers:
        raise SystemExit("run --phase offers first")
    rows, excluded = offers["rows"], offers["excluded"]
    roster = agents_roster(rows)
    d = OUT / market
    stamp = offers["built"]
    with open(d / f"agent_outreach_{stamp}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "score", "address", "city", "zip", "owner_type", "list_price", "mortgage_est", "owed_pct_of_list",
                    "rate_est", "pi_per_month_est", "cash_offer", "creative_lane",
                    "creative_price", "agent", "agent_phone", "agent_email", "office", "first_text"])
        for r in rows:
            w.writerow([r["rank"], r["score"], r["address"], r["city"], r["zip"], r.get("owner_type") or "", r["list_price"],
                        "" if r.get("mtg_balance") is None else r["mtg_balance"], r.get("owed_pct") or "",
                        r.get("mtg_rate") or "",
                        "" if r.get("mtg_payment") is None else r["mtg_payment"], r.get("cash_offer") or "",
                        r.get("lane") or "", r.get("creative_price") or "", r.get("agent"), r.get("agent_phone"),
                        r.get("agent_email") or "", r.get("office"), offer_text(r, sender)])
    path = write_workbook(d / f"{market.title()}_MLS_Offers_{stamp}.xlsx", offers, roster, sender)
    logger.info("wrote %s and the outreach CSV", path)
    return path


# --------------------------------------------------------------------------------------------- workbook

NAVY, BLUE, GRAY = "0A1130", "316AFF", "F2F4F7"


def write_workbook(path: Path, offers: dict, roster: list[dict], sender: str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    rows, excluded, pol = offers["rows"], offers["excluded"], offers["policy"]
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color=NAVY), PatternFill("solid", fgColor=GRAY)
    line = Border(bottom=Side(style="thin", color="D0D5DD"))

    def table(ws, headers, data, widths, money_cols=(), pct_cols=(), wrap_cols=()):
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill, c.border = head_font, head_fill, line
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for rec in data:
            ws.append(rec)
        for i, wdt in enumerate(widths, 1):
            col = get_column_letter(i)
            ws.column_dimensions[col].width = wdt
            for cell in ws[col][1:]:
                if i in money_cols:
                    cell.number_format = '"$"#,##0'
                elif i in pct_cols:
                    cell.number_format = "0%"
                if i in wrap_cols:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                else:
                    cell.alignment = Alignment(vertical="top")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Overview"
    lanes = {}
    for r in rows:
        lanes[r.get("lane") or "Cash only"] = lanes.get(r.get("lane") or "Cash only", 0) + 1
    reasons = {}
    for r in excluded:
        g = r.get("reason_group") or r["reason"]
        reasons[g] = reasons.get(g, 0) + 1
    motivated = sum(1 for r in rows if r["score"] >= pol["motivated_score"])
    with_phone = sum(1 for r in rows if r.get("agent_phone"))
    cash_rows = [r for r in rows if r.get("cash_offer")]
    by_buyers = sum(1 for r in cash_rows if r.get("investor_price"))
    reach = sum(1 for r in rows if r.get("reachable_near"))
    thin = sum(1 for r in cash_rows if r.get("spread_at_list_rule") is not None and r["spread_at_list_rule"] < pol["fee"])
    lines = [
        ("MLS offers", f"{len(rows):,} of {offers['listings']:,} active single-family listings get an offer ({offers['built']})"),
        ("Motivated", f"{motivated:,} score {pol['motivated_score']} or higher (long on market, price cuts, relists, distress)"),
        ("Agent phone", f"{with_phone:,} of {len(rows):,} offers have the listing agent's phone"),
        ("Agents", f"{len(roster):,} listing agents hold the offer list"),
        ("Buyer pricing", f"{by_buyers:,} of {len(cash_rows):,} cash offers are priced off what investors paid nearby"),
        ("70% of list", f"on {thin:,} of them a plain 70%-of-list offer would leave less than our {money(pol['fee'])} fee"),
        ("Buyers to call", f"{reach:,} offers have at least one buyer we can reach who bought nearby"),
        ("", ""),
        ("Owed / list", ""),
        *[(label, f"{sum(1 for r in rows if test(r)):,}") for label, test in (
            ("Owe 90% of list or more", lambda r: (r.get("owed_pct") or 0) >= 0.90),
            ("Owe 80% to 90%", lambda r: 0.80 <= (r.get("owed_pct") or 0) < 0.90),
            ("Owe 50% to 80%", lambda r: 0.50 <= (r.get("owed_pct") or 0) < 0.80),
            ("Owe under 50%", lambda r: 0 < (r.get("owed_pct") or 0) < 0.50),
            ("Free and clear", lambda r: r.get("mtg_balance") == 0),
            ("No loan data", lambda r: r.get("mtg_balance") is None))],
        ("", ""),
        ("Owner type", ""),
        *[(k, f"{v:,}") for k, v in sorted({t: sum(1 for r in rows if r.get("owner_type") == t)
                                             for t in {r.get("owner_type") for r in rows}}.items(), key=lambda kv: -kv[1])],
        ("", ""),
        ("Offer lanes", ""),
        *[(k, f"{v:,}") for k, v in sorted(lanes.items(), key=lambda kv: -kv[1])],
        ("", ""),
        ("Not offered", ""),
        *[(k, f"{v:,}") for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])],
        ("", ""),
        ("Cash offer", f"what investors paid for similar houses nearby, less our {money(pol['fee'])} fee, never above "
                       f"{pol['cash_pct_list']:.0%} of list ({pol['cash_pct_list_motivated']:.0%} when motivated); list "
                       f"{money(pol['cash_max_list'])} and under"),
        ("Subject-to", f"every loan at or under {pol['subto_max_rate']:.1f}%, price {pol['subto_price_pct']:.0%} of list, "
                       f"up to {money(pol['subto_max_cash'])} to the seller at closing, the rest of their equity carried "
                       f"as a {pol['carry_rate']:.0%} note over {pol['carry_years']} years (its payment counts in our cost), "
                       "we pay the commission"),
        ("Weak cash flow", f"{sum(1 for r in rows if r.get('weak_cash_flow')):,} subject-to offers return under "
                           f"{pol['creative_min_coc']:.0%} cash on cash as a rental: kept, flagged, the exit is a wrap or resale"),
        ("Seller finance", f"flagged free and clear: {pol['sf_price_pct']:.0%} of list, {pol['sf_down_pct']:.0%} down, "
                           f"{pol['sf_rate']:.0%} over {pol['sf_years']} years"),
        ("Sends", "Nothing. Every text in this file is a draft for a person to send."),
    ]
    for a, b in lines:
        ws.append([a, b])
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 18, 110
    for c in ws["A"]:
        c.font = Font(bold=True, color=NAVY)
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)

    M, P, R, W = '"$"#,##0', "0%", "0.00%", "wrap"
    rnd = lambda v: round(v) if v is not None else None
    lane_only = lambda k: (lambda r: r.get(k) if r.get("lane") else None)
    spec = [  # header, value, width, format
        ("Rank", lambda r: r["rank"], 6, None), ("Score", lambda r: r["score"], 6, None),
        ("AI off-market", lambda r: rnd(r.get("ai_off")), 8, None), ("AI on-market", lambda r: rnd(r.get("ai_on")), 8, None),
        ("Address", lambda r: r["address"], 24, None), ("City", lambda r: r["city"], 12, None), ("Zip", lambda r: r["zip"], 7, None),
        ("Owner", lambda r: r.get("owner"), 28, W), ("Owner type", lambda r: r.get("owner_type"), 12, None),
        ("Investor owner", lambda r: "yes" if r.get("owner_investor") else "", 8, None),
        ("List", lambda r: r["list_price"], 11, M), ("Original", lambda r: r.get("original_price"), 11, M),
        ("Cut", lambda r: r.get("cut_pct") or None, 6, P), ("Days", lambda r: r.get("dom"), 6, None),
        ("Cuts", lambda r: r.get("cuts"), 5, None), ("Relists", lambda r: r.get("relists"), 6, None),
        ("Value", lambda r: r.get("value"), 11, M), ("Rent", lambda r: r.get("rent"), 8, M),
        ("Mortgage left (modeled)", lambda r: r.get("mtg_balance"), 12, M),
        ("Owed / list", lambda r: r.get("owed_pct"), 8, P),
        ("Owed / list with credit lines", lambda r: r.get("owed_pct_all"), 9, P),
        ("Rate (modeled)", lambda r: (r["mtg_rate"] / 100) if r.get("mtg_rate") else None, 8, R),
        ("Rate source", lambda r: r.get("mtg_source"), 16, W),
        ("P&I per month", lambda r: r.get("mtg_payment"), 10, M), ("Mortgage insurance", lambda r: r.get("mtg_mi"), 10, M),
        ("Full payment (P&I, MI, taxes, insurance)", lambda r: r.get("mtg_full") if r.get("mtg_balance") else None, 12, M),
        ("Loans", lambda r: r.get("mtg_detail"), 48, W),
        ("Bd", lambda r: r.get("beds"), 4, None), ("Ba", lambda r: r.get("baths"), 4, None),
        ("SqFt", lambda r: r.get("sqft"), 7, None), ("Built", lambda r: r.get("year"), 6, None), ("Why", lambda r: r.get("why"), 40, W),
        ("Cash offer", lambda r: r.get("cash_offer"), 11, M), ("Cash % list", lambda r: r.get("cash_pct_of_list"), 8, P),
        ("Cash basis", lambda r: r.get("cash_basis"), 40, W), ("Buyers pay (nearby)", lambda r: r.get("investor_price"), 12, M),
        ("ARV (flip exits)", lambda r: r.get("arv"), 12, M), ("ARV x 60% check", lambda r: r.get("arv_rule"), 12, M),
        ("Buyers nearby", lambda r: r.get("buyers_near"), 8, None), ("Reachable buyers", lambda r: r.get("reachable_near"), 9, None),
        ("Top buyers nearby", lambda r: r.get("top_buyers"), 34, W), ("Lane", lambda r: r.get("lane") or "Cash only", 20, None),
        ("Creative price", lambda r: r.get("creative_price"), 12, M), ("Cash to seller", lambda r: r.get("cash_to_seller"), 11, M),
        ("Seller carry", lambda r: r.get("seller_carry"), 11, M), ("Carry per month", lambda r: r.get("carry_payment"), 9, M),
        ("Our monthly cost", lane_only("piti"), 9, M), ("Cash in", lane_only("cash_in"), 10, M),
        ("Cash on cash", lane_only("coc"), 8, P), ("Creative terms", lambda r: r.get("creative_note"), 50, W),
        ("Agent", lambda r: r.get("agent"), 20, None), ("Agent phone", lambda r: r.get("agent_phone"), 15, None),
        ("Office", lambda r: r.get("office"), 22, None), ("First text", lambda r: offer_text(r, sender), 70, W),
    ]
    def spec_sheet(title, cols, data_rows):
        ws_ = wb.create_sheet(title)
        table(ws_, [s[0] for s in cols], [[s[1](r) for s in cols] for r in data_rows], [s[2] for s in cols],
              wrap_cols=tuple(i for i, s in enumerate(cols, 1) if s[3] == W))
        for i, s in enumerate(cols, 1):
            if s[3] in (M, P, R):
                for c in ws_[get_column_letter(i)][1:]:
                    c.number_format = s[3]
        return ws_

    spec_sheet("Offers", spec, rows)

    # Subject-to, closest to owing the list price first: that is where the seller cannot sell for cash and
    # walk away with anything, so taking over the loan is the offer that works for them.
    st = [r for r in rows if (r.get("lane") or "").startswith("Subject-to")]
    st.sort(key=lambda r: (-(r.get("owed_pct") or 0), r.get("mtg_rate") or 99))
    spec_sheet("Subject-to", [
        ("Rank", lambda r: r["rank"], 6, None), ("Score", lambda r: r["score"], 6, None),
        ("Address", lambda r: r["address"], 24, None), ("City", lambda r: r["city"], 12, None),
        ("Owner type", lambda r: r.get("owner_type"), 12, None), ("List", lambda r: r["list_price"], 11, M),
        ("Mortgage left (modeled)", lambda r: r.get("mtg_balance"), 12, M), ("Owed / list", lambda r: r.get("owed_pct"), 8, P),
        ("Owed / list with credit lines", lambda r: r.get("owed_pct_all"), 9, P),
        ("Rate (modeled)", lambda r: (r["mtg_rate"] / 100) if r.get("mtg_rate") else None, 8, R),
        ("Rate source", lambda r: r.get("mtg_source"), 16, W), ("P&I per month", lambda r: r.get("mtg_payment"), 10, M),
        ("Mortgage insurance", lambda r: r.get("mtg_mi"), 10, M),
        ("Full payment (P&I, MI, taxes, insurance)", lambda r: r.get("mtg_full"), 12, M),
        ("Cash to seller", lambda r: r.get("cash_to_seller"), 11, M), ("Seller carry", lambda r: r.get("seller_carry"), 11, M),
        ("Carry per month", lambda r: r.get("carry_payment"), 9, M), ("Our monthly cost", lambda r: r.get("piti"), 9, M),
        ("Rent", lambda r: r.get("rent"), 8, M), ("Cash flow per month", lambda r: r.get("cash_flow"), 10, M),
        ("Cash in", lambda r: r.get("cash_in"), 10, M), ("Cash on cash", lambda r: r.get("coc"), 8, P),
        ("Clears 20%", lambda r: "yes" if (r.get("coc") or 0) >= pol["target_coc"] else "", 8, None),
        ("Terms", lambda r: r.get("creative_note"), 60, W), ("Agent", lambda r: r.get("agent"), 20, None),
        ("Agent phone", lambda r: r.get("agent_phone"), 15, None)], st)

    # Buyers: every investor who bought near an offer, so dispo starts before the contract does.
    tally: dict[str, list] = {}
    for r in rows:
        for name in [n.strip() for n in (r.get("top_buyers") or "").split(";") if n.strip()]:
            t = tally.setdefault(name, [name.replace(" (phone)", ""), 0, "yes" if "(phone)" in name else ""])
            t[1] += 1
    table(wb.create_sheet("Buyers"), ["Buyer", "Offers it bought near", "Phone on file"],
          [list(v) for v in sorted(tally.values(), key=lambda v: (-(v[2] == "yes"), -v[1]))], [40, 12, 10])

    table(wb.create_sheet("Agents"),
          ["Agent", "Phone", "Email", "Office", "Broker phone", "Listings", "Motivated", "Creative", "Top score", "Addresses"],
          [[a["agent"], a["phone"], a["email"], a["office"], a["broker_phone"], a["listings"], a["motivated"],
            a["creative"], a["top_score"], a["addresses"]] for a in roster],
          [22, 15, 24, 26, 15, 8, 9, 8, 8, 80], wrap_cols=(10,))

    watch = sorted((r for r in excluded if r.get("watch_offer")), key=lambda r: -(r.get("score") or 0))
    table(wb.create_sheet("Price watch"),
          ["Score", "Address", "City", "List", "Buyers pay (nearby)", "Cash offer now", "% of list",
           "Send when list is at or under", "Days", "Cuts", "Mortgage (modeled)", "Rate (modeled)", "P&I per month",
           "Owed / list", "Owner type", "Why", "Agent", "Agent phone"],
          [[r.get("score"), r["address"], r["city"], r.get("list_price"), r.get("investor_price"), r["watch_offer"],
            round(r["watch_offer"] / r["list_price"], 3) if r.get("list_price") else None, r["watch_trigger"],
            r.get("dom"), r.get("cuts"), r.get("mtg_balance"), (r["mtg_rate"] / 100) if r.get("mtg_rate") else None,
            r.get("mtg_payment"), r.get("owed_pct"), r.get("owner_type"), r.get("why"), r.get("agent"),
            r.get("agent_phone")] for r in watch],
          [6, 24, 12, 11, 12, 11, 7, 14, 6, 5, 12, 7, 10, 8, 12, 44, 20, 15], money_cols=(4, 5, 6, 8, 11, 13),
          pct_cols=(7, 14), wrap_cols=(16,))
    for c in wb["Price watch"]["L"][1:]:
        c.number_format = "0.00%"

    is_more = lambda r: (r.get("reason_group") or "").startswith("screened only")
    more = sorted((r for r in excluded if is_more(r)), key=lambda r: -(r.get("score") or 0))
    if more:
        table(wb.create_sheet("More listings"),
              ["Score", "Address", "City", "Zip", "List", "Value", "Equity", "Days on market (est)", "Why",
               "Buyers pay (nearby)", "Cash offer (preliminary)", "Cash % list", "In your account"],
              [[r.get("score"), r["address"], r["city"], r.get("zip"), r.get("list_price"), r.get("value"),
                (r["equity_pct"] / 100) if r.get("equity_pct") is not None else None, r.get("dom_bucket") or r.get("dom"),
                r.get("why"), r.get("investor_price"), r.get("cash_offer") or r.get("watch_offer"),
                r.get("cash_pct_of_list"), "yes" if r.get("saved") else ""] for r in more],
              [6, 24, 12, 7, 11, 11, 7, 10, 44, 12, 12, 8, 9], money_cols=(5, 6, 10, 11), pct_cols=(7, 12), wrap_cols=(9,))

    table(wb.create_sheet("Not offered"),
          ["Reason", "Address", "City", "List", "Built", "Owner", "Agent"],
          [[r["reason"], r["address"], r["city"], r.get("list_price"), r.get("year"), r.get("owner"), r.get("agent")]
           for r in excluded if not r.get("watch_offer") and not is_more(r)],
          [60, 26, 14, 11, 7, 30, 22], money_cols=(4,), wrap_cols=(1,))

    src = wb.create_sheet("Sources and caveats")
    if offers.get("source") == "api":
        source_notes = [
            "Listings come from SiftMap search on your DataSift Open API key (allowed in volume). Days on market "
            "on the More listings sheet is the search's window (within 30, 90 or 365 days) until a detail is read.",
            "Agents, listing history and loans come from the SiftMap property detail, read only for listings in your "
            "own DataSift account, as DataSift's acceptable use requires. A listing not in the account is screened "
            "from search alone and sits on More listings; add it (--add --commit) to read it.",
            "Loan rates, balances and payments are MODELED from the recorded loan: the actual rate from HMDA (the "
            "federal mortgage disclosure) where one loan matches the year, census tract, type, amount and term, "
            "otherwise Freddie Mac's rate in the lock week plus a spread measured on those matches; the balance is "
            "amortized from the original amount over the real term, plus FHA, USDA or private mortgage insurance. "
            "Confirm the payoff and the rate with the agent before any number is binding.",
            "What investors paid nearby comes from search split into last-sale price bands, so each sale is priced "
            "at its band's midpoint ($10,000 bands under $300,000, $25,000 above). The buyers themselves are not "
            "named by search.",
        ]
    else:
        source_notes = [
            "Listings, agents and loans come from the SiftMap property index (read-only). Loan balances, rates and "
            "payments are ESTIMATES: the rate is the market average in the month the loan recorded, the balance is "
            "amortized from the original amount. Confirm the payoff and the rate with the agent before any number is binding.",
        ]
    notes = source_notes + [
        "A 'Credit Line' loan is a home equity line. Its balance estimate is a limit, not a payoff, so it never "
        "qualifies a subject-to offer on its own.",
        "Days on market and price cuts come from the MLS event log and count the whole marketing run since the last "
        "sale, across expiries and relists. Zillow's own days count restarts at a relist, so it reads lower.",
        "The cash offer is what investors paid for similar houses (70% to 135% of the size) within half a mile "
        "(widening to 1 and 2 miles until 3 sales show up) over the last 24 months, less our fee. It prefers buys "
        "at or under 90% of today's value, because a turnkey rental bought at full price does not price an ugly "
        "house. Deeds at twice the parcel's value or more are portfolio deeds and are dropped. 'ARV x 60% check' "
        "is his formula on flip resales nearby; an offer above it is flagged for a condition read.",
        "Investor labels come from the deed (pending, rental, wholetail, wholesale = an investor bought and still "
        "holds; flip = the renovated resale). Where a dispo buyer registry is installed, buyers are tied to it by "
        "the address they bought, then by name, and 'Reachable' means a phone is on file in the registry.",
        "Repairs are a flat placeholder per square foot until someone reads the photos or walks the house.",
        "Subject-to: the loan stays in the seller's name and the lender can call it due on a transfer. Every "
        "subject-to and seller-carry offer needs a closing attorney who does them.",
        "Write every contract 'and/or assigns' and tell the agent up front that you may assign it. Check your "
        "state's current wholesaling rules with a closing attorney before marketing any contract.",
        "The drafts never claim a lender pre-approval: an agent who later learns the contract is being assigned "
        "has a fair complaint. The listing agent is the contact; never go around the agent to the homeowner.",
        "The agent's email is not in every MLS feed (it is absent in East Tennessee's). Phone is the primary channel.",
        "MLS data here is for identifying properties you may buy, which is the use the feed allows.",
    ]
    src.column_dimensions["A"].width = 140
    for n in notes:
        src.append([n])
        src.cell(src.max_row, 1).alignment = Alignment(wrap_text=True, vertical="top")
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    # Excel holds an exclusive lock on an open workbook: save beside it rather than lose the run.
    for n in range(0, 20):
        target = path if n == 0 else path.with_name(f"{path.stem} ({n + 1}){path.suffix}")
        try:
            tmp.replace(target)
            return target
        except PermissionError:
            continue
    raise SystemExit(f"could not write {path.name}: every name is locked (close it in Excel)")


# --------------------------------------------------------------------------------------------- cli

API_PHASES = ["screen", "buyers", "shortlist", "add", "detail", "mortgage", "zillow", "offers", "export", "all"]
CLUSTER_PHASES = ["pull", "history", "mortgage", "buyers", "zillow", "offers", "export", "all"]


def _load_env() -> None:
    """Read a .env next to the repo (or the current folder) when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            load_dotenv(p, override=False)


def main_api(a, pol: Policy) -> int:
    """The published-API run: one phase, or `all` (the whole run, mls_offers_api.run)."""
    global OUT
    import mls_offers_api as api
    try:
        market = api.resolve_market(a.county or "", a.fips or "", a.zips or "", a.name or "")
    except ValueError as exc:
        print(str(exc))
        return 2
    out = Path(a.out) if a.out else OUT / market["name"]
    sender = a.sender if a.sender is not None else default_sender()
    if a.phase == "all":
        res = api.run(market, out=out, shortlist=a.shortlist, add=a.add, commit=a.commit, zillow=a.zillow,
                      zillow_limit=a.limit if a.zillow else 0, months=a.months, policy=asdict(pol),
                      list_name=a.list_name, tag=a.tag, sender=sender, expect_email=a.expect_email or "")
        print(json.dumps({k: res[k] for k in ("market", "workbook", "csv", "counts")}, indent=1))
        for n in res["notes"]:
            print("NOTE:", n)
        if res.get("quote") and not (a.add and a.commit):
            q = res["quote"]
            print("Adding the %s shortlisted listings not in your account would use %s records (cost %s). "
                  "Re-run with --add --commit to add them." % (q.get("total"), q.get("billable"), q.get("cost")))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    OUT, key = out.parent, out.name
    notes: list = []
    client = None if a.phase in ("shortlist", "offers", "export", "mortgage") else api.make_client()
    if a.phase == "screen":
        api.phase_screen(market, pol, client, out, notes)
    elif a.phase == "buyers":
        api.phase_buyers(market, client, out, a.months)
    elif a.phase == "shortlist":
        api.phase_shortlist(pol, out, a.shortlist)
    elif a.phase == "add":
        res = api.phase_add(client, out, commit=bool(a.add and a.commit), list_name=a.list_name, tag=a.tag,
                            expect_email=a.expect_email or "", notes=notes)
        print(json.dumps({k: v for k, v in res.items() if k != "quote"}), json.dumps(res.get("quote"))[:600])
    elif a.phase == "detail":
        print(api.phase_detail(client, out, notes=notes))
        api.phase_rent(client, out, (load_json(out / "shortlist.json") or {}).get("ids") or [])
    elif a.phase == "mortgage":
        print(api.phase_mortgage(market, out).get("stats"))
    elif a.phase == "zillow":
        phase_zillow(key, a.limit)
    elif a.phase == "offers":
        phase_offers(key, pol)
    elif a.phase == "export":
        print(phase_export(key, sender))
    for n in notes:
        print("NOTE:", n)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # --source cluster and --market are staff-only (an internal read-only index), so the help leaves them out.
    ap.add_argument("--source", default="api", choices=["api", "cluster"], help=argparse.SUPPRESS)
    ap.add_argument("--phase", required=True, choices=sorted(set(API_PHASES) | set(CLUSTER_PHASES)),
                    metavar="{" + ",".join(API_PHASES) + "}",
                    help="one step of the run, or all of it (all)")
    ap.add_argument("--county", help='county and state, like "Knox, TN" (several separated by ";")')
    ap.add_argument("--fips", help="county FIPS codes, like 47093,47009")
    ap.add_argument("--zips", help="ZIP codes, like 37914,37917")
    ap.add_argument("--name", help="a short name for the market's output folder")
    ap.add_argument("--out", help="the folder every file is written to (default output/mls_offers/<market> in a SiftStack checkout, ~/SiftStack/mls_offers/<market> for an installed skill)")
    ap.add_argument("--shortlist", type=int, default=60, help="how many listings to read in full")
    ap.add_argument("--add", action="store_true", help="add the shortlisted listings not yet in your account")
    ap.add_argument("--commit", action="store_true", help="with --add, actually add (uses record allowance)")
    ap.add_argument("--zillow", action="store_true", help="all: check the offers on Zillow (paid, OPENWEBNINJA_API_KEY)")
    ap.add_argument("--list-name", default="MLS Offers", help="the DataSift list added listings go on")
    ap.add_argument("--tag", default="MLS Listing", help="the tag added listings get")
    ap.add_argument("--expect-email", help="refuse to add unless the key belongs to this user")
    ap.add_argument("--months", type=int, default=24, help="buyers: how far back investor purchases count")
    ap.add_argument("--market", default="knox", choices=sorted(MARKETS), help=argparse.SUPPRESS)
    ap.add_argument("--limit", type=int, default=50, help="zillow: how many top rows to read (paid)")
    ap.add_argument("--policy", help="JSON file of Policy overrides")
    ap.add_argument("--sender", default=None, help="who the drafts are from (default MLS_OFFERS_SENDER, else unnamed)")
    ap.add_argument("-v", "--verbose", action="store_true")
    for f in Policy.__dataclass_fields__.values():
        typ = (lambda s: s.strip().lower() in ("1", "true", "yes", "on")) if isinstance(f.default, bool) else type(f.default)
        ap.add_argument("--" + f.name.replace("_", "-"), type=typ, default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s")
    _load_env()
    pol = Policy(**(load_json(Path(a.policy)) if a.policy else {}))
    for f in Policy.__dataclass_fields__:
        v = getattr(a, f)
        if v is not None:
            setattr(pol, f, v)
    if a.source == "api":
        if a.phase not in API_PHASES:
            ap.error(f"--phase {a.phase} is a cluster phase; the api phases are {', '.join(API_PHASES)}")
        try:
            return main_api(a, pol)
        except RuntimeError as exc:      # no key, the wrong account, a failed download: a sentence, no trace
            print("Stopped: %s" % exc)
            return 1
    if a.phase not in CLUSTER_PHASES:
        ap.error(f"--phase {a.phase} is an api phase; add --source api")
    if a.sender is None:
        a.sender = default_sender()
    fips = MARKETS[a.market]
    if a.phase in ("pull", "all"):
        phase_pull(a.market, fips)
    if a.phase in ("history", "all"):
        phase_history(a.market)
    if a.phase in ("mortgage", "all"):
        phase_mortgage(a.market, fips)
    if a.phase in ("buyers", "all"):
        phase_buyers(a.market, fips, a.months)
    if a.phase == "zillow":
        phase_zillow(a.market, a.limit)
    if a.phase in ("offers", "all"):
        phase_offers(a.market, pol)
    if a.phase in ("export", "all"):
        print(phase_export(a.market, a.sender))
    return 0


if __name__ == "__main__":
    sys.exit(main())
