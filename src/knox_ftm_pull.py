"""Knox County first-to-market multi-source pull -> DataSift upload CSV.

Collects the FTM lists that carry a property address, enriches each against
SiftMap, applies the buy box, and writes a CSV in the exact DATASIFT_COLUMNS
shape used by the rest of the pipeline.

Sources (tax sale deliberately excluded per Ty, 2026-07-26):
  condemnation  BBB certification of blight + Public Officer repair/demolition
                orders + boarding approvals (monthly, no public back-archive)
  probate       Knox Probate Court estate cases (CaseLook), last 12 months
  public_sale   tnpublicnotice "Public sales" (partition/receiver/judicial/lien)
  order         tnpublicnotice "Orders" (service by publication)

Buy box: single family only, AVM between --value-min and --value-max.

    python src/knox_ftm_pull.py --out output/knox_ftm_pull.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_formatter import DATASIFT_COLUMNS  # noqa: E402

EXTRA_COLUMNS = [
    "Outstanding Lien Amount",   # active liens only
    "Liens Active vs Total",
    "Lien Type",
    "Total Delinquency",      # lien total + county tax owed
    "Est Equity",             # AVM minus mortgage on THIS property
    "Debt vs Equity Pct",     # total delinquency as a share of equity
    "Upside Down",            # yes = debt swallows the equity, not workable
    "Upside Down Reason",
]
OUT_COLUMNS = DATASIFT_COLUMNS + EXTRA_COLUMNS

_API = r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api"
if os.path.isdir(_API):
    sys.path.insert(0, _API)
    sys.path.insert(0, os.path.join(_API, "clients"))
os.environ.setdefault("REISIFT_ACCOUNT", "datasift-apikey")

SFR = "single_family_residence"


def _iso(d: str) -> str:
    """M/D/YYYY (how the ROD prints it) -> YYYY-MM-DD. Pass ISO through."""
    d = (d or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", d)
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else ""


def _quarter(iso: str) -> str:
    if not iso or len(iso) < 7:
        return ""
    y, mth = iso[:4], int(iso[5:7])
    return "%s-Q%d" % (y, (mth - 1) // 3 + 1)

# notice_type -> DataSift list name, mirroring datasift_formatter's routing
LIST_NAME = {
    "code_violation": "Condemned",
    "probate": "Probate",
    "public_sale": "Public Sale",
    "order": "Court Order",
    "eviction": "Eviction",
    "trustee_deed": "Trustee Deed",
    "lien": "Liens",
}


# ---------------------------------------------------------------- collection

_MONEY = r"\$([\d,]+(?:\.\d\d)?)"


def _condemnation_money(note: str) -> dict:
    """Pull the dollar figures the BBB/Public Officer agenda states in prose.

    These records never hit the county tax API path, so without this their tax
    and lien amounts sit in Notes only and every structured field reads blank
    (spotted on 3240 Wilson Ave, whose note says "1 bill $254.00, county tax
    $271.36 (2025)"). Agenda wording varies: "2 liens = $422.00",
    "City tax: $3,884.46 (2022)", "county $2,113.47 (2022)", "1 bill = $254.00".
    """
    note = note or ""
    tax, years, liens = 0.0, [], 0.0
    for pat in (r"[Cc]ity tax[:\s]*" + _MONEY, r"[Cc]ounty(?: tax)?[:\s]*" + _MONEY):
        for m in re.finditer(pat + r"\s*\((\d{4})\)", note):
            tax += float(m.group(1).replace(",", ""))
            years.append(int(m.group(2)))
    # abatement liens and billed charges both become a claim on the parcel
    for m in re.finditer(r"\d+\s+liens?\s*=?\s*" + _MONEY, note):
        liens += float(m.group(1).replace(",", ""))
    for m in re.finditer(r"\d+\s+bills?\s*=?\s*" + _MONEY, note):
        liens += float(m.group(1).replace(",", ""))
    return {"tax_due": round(tax, 2) or None,
            "tax_years": sorted(set(years)),
            "lien_total": round(liens, 2) or None}


def load_condemnations(scratch: str) -> list[dict]:
    """BBB + Public Officer docket. Hand-keyed from the two agenda PDFs."""
    path = os.path.join(scratch, "condemnations.json")
    if not os.path.exists(path):
        return []
    out = []
    for r in json.load(open(path, encoding="utf-8")):
        out.append({
            "source": "condemnation",
            "notice_type": "code_violation",
            "address": r["address"],
            "owner_raw": r.get("owner_raw", ""),
            "notes": r.get("notes", ""),
            "case_number": r.get("record", ""),
            "event_date": r.get("hearing", ""),
            "filed_date": r.get("hearing", ""),   # BBB/Public Officer hearing date
            "source_url": "https://www.knoxvilletn.gov/government/boards_commissions/better_building_board",
        })
        out[-1].update(_condemnation_money(r.get("notes", "")))
    return out


def load_probate(scratch: str) -> list[dict]:
    """Knox County TN probate. Currently ALWAYS EMPTY, and that is correct.

    knoxpjcourt.com is Knox County OHIO (Mount Vernon), not Tennessee. Knox
    County TN probate runs through Chancery Court under the Clerk & Master,
    which publishes NO online case index: on-site inspection or a mailed
    records request only. The only online route to TN probate is the published
    notice to creditors on tnpublicnotice.com. Do not point this loader at an
    out-of-state court.
    """
    path = os.path.join(scratch, "probate_pe.json")
    if not os.path.exists(path):
        return []
    out = []
    for r in json.load(open(path, encoding="utf-8")):
        out.append({
            "source": "probate",
            "notice_type": "probate",
            "address": r.get("property_address", ""),   # usually blank, resolved later
            "owner_raw": r.get("personal_representative", "") or "",
            "decedent": r.get("decedent_name", "") or r.get("style", ""),
            "case_number": r.get("case_number", ""),
            "event_date": r.get("file_date", ""),
            "notes": r.get("style", ""),
            "source_url": "https://knoxpjcourt.com/recordSearch.php",
        })
    return out


# Already covered by the production foreclosure scraper, so including these
# would just duplicate the existing feed (61% of the raw pull).
_DUP_RE = re.compile(r"trustee|foreclos", re.I)
# Not real property: vehicle auctions, self-storage and marina lien sales.
_JUNK_RE = re.compile(r"vehicle|storage|\bVIN\b|impound|\btow\b|marina|boat|vessel", re.I)
# Public-sector noise that carries a street address but is not a seller lead.
_NOISE_RE = re.compile(
    r"advertisement for bids|sealed bids|invites proposals|specifications and contract|"
    r"annual meeting|notice of public meeting|department of transportation", re.I)
# The auction venue and other institutional addresses the snippet leads with.
_VENUE_RE = re.compile(r"chancery|court|circuit|clerk|400 Main|Main Street|Main St\b|Dameron", re.I)


def load_tnpn(scratch: str) -> list[dict]:
    """tnpublicnotice "Public sales" + "Orders", deduped to real property leads.

    Reality check from the live 12-month pull: of 1,997 Knox notices, 1,223 were
    trustee/foreclosure (already scraped), 219 were vehicle or storage lien
    sales, and most of the rest were construction bids and utility notices. The
    grid snippet is truncated, so the address it exposes is usually the AUCTION
    VENUE (400 Main Street, the City County Building), not the subject property.
    What survives is the Chancery non-resident / unknown-heir notice, which does
    name the property, and which runs 4 consecutive weeks so it needs dedup.
    """
    path = os.path.join(scratch, "tnpn_public_sales_orders.json")
    if not os.path.exists(path):
        return []

    by_addr: dict[str, dict] = {}
    for r in json.load(open(path, encoding="utf-8")):
        blob = "%s %s" % (r.get("title") or "", r.get("snippet") or "")
        if _DUP_RE.search(blob) or _JUNK_RE.search(blob) or _NOISE_RE.search(blob):
            continue
        addr = r.get("property_address") or extract_address(r.get("body") or r.get("snippet") or "")
        if not addr or _VENUE_RE.search(addr) or not addr[0].isdigit():
            continue

        key = " ".join(addr.upper().split())
        cat = (r.get("category") or "").lower()
        rec = by_addr.get(key)
        if rec is None:
            by_addr[key] = {
                "source": "tnpublicnotice",
                "notice_type": "public_sale" if "sale" in cat else "order",
                "address": addr,
                "owner_raw": "",
                "case_number": r.get("notice_id", ""),
                "event_date": r.get("publish_date", ""),
                "filed_date": r.get("publish_date", ""),   # legal publication date
                "notes": (r.get("title") or "")[:200],
                "source_url": r.get("detail_url", ""),
                "_runs": 1,
            }
        else:
            rec["_runs"] += 1
            rec["event_date"] = max(rec["event_date"], r.get("publish_date", ""))

    out = []
    for rec in by_addr.values():
        rec["notes"] = "%s | published %d times, latest %s" % (
            rec["notes"], rec.pop("_runs"), rec["event_date"])
        out.append(rec)
    return out


def load_landlords(scratch: str) -> list[dict]:
    """Eviction-docket landlords resolved to parcels via the open county tax API.

    Two exclusions, both to keep noise out of the upload: filers holding more
    than 15 parcels are apartment operators or public housing rather than tired
    landlords, and a single-token name (just a surname) matches too many
    unrelated owners to trust.
    """
    path = os.path.join(scratch, "landlord_parcels.json")
    if not os.path.exists(path):
        return []

    # The resolved file aggregates by landlord and drops the docket date, so map
    # it back from the raw docket: the filing date is the timeframe that matters.
    docket_by_landlord: dict[str, str] = {}
    ev = os.path.join(scratch, "evictions.json")
    if os.path.exists(ev):
        for e in json.load(open(ev, encoding="utf-8")):
            k = re.sub(r"[^A-Z ]", " ", (e.get("plaintiff_landlord") or "").upper())
            k = " ".join(k.split())
            d = e.get("docket_date", "")
            if k and d and (k not in docket_by_landlord or d < docket_by_landlord[k]):
                docket_by_landlord[k] = d

    out = []
    for L in json.load(open(path, encoding="utf-8")):
        if L.get("institutional") or not L.get("n_parcels"):
            continue
        if len(L["landlord"].split()) < 2:
            continue
        unpaid = L.get("unpaid_bill_years") or []
        for p in L.get("parcels", []):
            if not p.get("address"):
                continue
            out.append({
                "source": "eviction_landlord",
                "notice_type": "eviction",
                "address": p["address"],
                "owner_raw": p.get("owner") or L["landlord"],
                "case_number": "",
                "event_date": "",
                "notes": ("Landlord %s filed %d detainer action(s) on the Knox General Sessions "
                          "docket. Holds %d parcels. County tax account %s, amount due %s.%s"
                          % (L["landlord"], L["filings"], L["n_parcels"],
                             p.get("account"), p.get("amount_due"),
                             (" Unpaid tax years: %s." % ", ".join(str(y) for y in unpaid)) if unpaid else "")),
                "source_url": "https://www.knoxcounty.org/civil/dockets.php",
                "filed_date": L.get("docket_date")
                               or docket_by_landlord.get(L["landlord"], ""),
                "tax_due": p.get("amount_due"),
                "tax_years": unpaid,
            })
    return out


def load_rod(scratch: str) -> list[dict]:
    """Register of Deeds instruments already resolved to a parcel address.

    Only some document types carry a parcel. Trustee's deeds (TRS) carry
    'PrpId:' on 100% of rows and resolve cleanly. Substitute trustee
    appointments (APPT) are deliberately NOT here: the index names only the
    lender and the substitute trustee, never the borrower, so they cannot be
    turned into a lead without buying the document image. Tax liens and general
    liens are indexed against the person, not the parcel, and need the separate
    name-to-parcel path.
    """
    path = os.path.join(scratch, "rod_resolved.json")
    if not os.path.exists(path):
        return []
    out = []
    for r in json.load(open(path, encoding="utf-8")):
        a = r.get("assessor") or {}
        if not a.get("_found") or not a.get("address"):
            continue
        unpaid = a.get("unpaid_years") or []
        note = ("Register of Deeds %s instrument %s recorded %s. Grantor %s. Grantee %s. "
                "Consideration %s. Parcel %s."
                % (r.get("doc_type"), r.get("instrument"), r.get("recorded") or "n/a",
                   r.get("grantor") or "n/a", r.get("grantee") or "n/a",
                   r.get("consideration") or "n/a", r.get("parcel_id")))
        if unpaid:
            note += " Unpaid county tax years: %s." % ", ".join(str(y) for y in unpaid)
        out.append({
            "source": "register_of_deeds",
            "notice_type": "trustee_deed" if r.get("doc_type") == "TRS" else r.get("doc_type", "").lower(),
            "address": a["address"],
            "owner_raw": a.get("owner") or r.get("grantee") or "",
            "case_number": r.get("instrument", ""),
            "event_date": r.get("recorded", ""),
            "filed_date": _iso(r.get("recorded", "")),   # county recording date
            "tax_due": a.get("amount_due"),
            "tax_years": unpaid,
            "notes": note,
            "source_url": "https://paxsub.knoxrod.org/views/search",
        })
    return out


def _release_index(scratch: str) -> tuple[set, set]:
    """Instruments and party names that appear on a RELEASE document.

    Without this the lien list carries satisfied debt as if it were live: 8% of
    lead debtors had EVERY lien already released. Instrument-level matching is
    the precise signal (a release names the original instrument); the name set
    is a weaker corroborating flag, since one release does not clear a person's
    other liens.
    """
    for path in (os.path.join(scratch, "lien_releases.json"),
                 os.path.join("output", "lien_releases.json")):
        if os.path.exists(path):
            d = json.load(open(path, encoding="utf-8"))
            return set(d.get("released_instruments") or []), set(d.get("released_names") or [])
    return set(), set()


def _lien_amounts(scratch: str) -> dict:
    """instrument -> recorded lien amount (the Consideration column).

    This is the number that qualifies a lien lead and it was missing entirely:
    11,511 of 12,867 general liens and 373 of 377 federal tax liens carry a
    dollar figure. State tax liens carry none, so those stay blank rather than
    being guessed at.
    """
    for path in (os.path.join(scratch, "lien_amounts.json"),
                 os.path.join("output", "lien_amounts.json")):
        if os.path.exists(path):
            return json.load(open(path, encoding="utf-8"))
    return {}


def _band(v: float) -> str:
    """Coarse band so the amount is filterable in DataSift, which has no
    numeric lien column."""
    for hi, label in ((5000, "lien_under_5k"), (15000, "lien_5k_15k"),
                      (50000, "lien_15k_50k"), (100000, "lien_50k_100k")):
        if v < hi:
            return label
    return "lien_over_100k"


def load_liens(scratch: str) -> list[dict]:
    """Lien debtors already resolved to parcels by knox_lien_resolve.py.

    Liens carry no parcel id (indexed against the person), so these come from
    the name-to-parcel join. Institutional holders are excluded upstream.
    """
    for path in (os.path.join(scratch, "knox_lien_parcels.json"),
                 os.path.join("output", "knox_lien_parcels.json")):
        if os.path.exists(path):
            break
    else:
        return []

    released_instr, released_names = _release_index(scratch)
    amounts = _lien_amounts(scratch)
    out = []
    dropped_cleared = 0
    for r in json.load(open(path, encoding="utf-8")):
        if r.get("institutional") or not r.get("n_parcels"):
            continue
        instruments = r.get("instruments") or []
        active = [i for i in instruments if i not in released_instr]
        if instruments and not active:
            dropped_cleared += 1      # every lien satisfied: not a lead
            continue
        unpaid = r.get("tax_unpaid_years") or []
        # Only ACTIVE liens count toward the outstanding total.
        active_amts = [amounts[i] for i in active if i in amounts]
        total_active = sum(active_amts)
        for p in r.get("parcels", []):
            if not p.get("address"):
                continue
            note = ("Lien debtor %s: %d lien instrument(s) recorded (%s). "
                    "%d of %d still active. Outstanding lien total $%s "
                    "(across %d priced instrument(s)). Creditors: %s. "
                    "County tax account %s, amount due %s.%s"
                    % (r["name"], r.get("n_liens", 0), "/".join(r.get("types") or []),
                       len(active), len(instruments) or r.get("n_liens", 0),
                       format(total_active, ",.2f"), len(active_amts),
                       ", ".join(r.get("creditors") or [])[:120] or "n/a",
                       p.get("account"), p.get("amount_due"),
                       (" Unpaid county tax years: %s." % ", ".join(str(y) for y in unpaid))
                       if unpaid else ""))
            out.append({
                "source": "register_of_deeds_lien",
                "notice_type": "lien",
                "address": p["address"],
                "owner_raw": p.get("owner") or r["name"],
                "case_number": (r.get("instruments") or [""])[0],
                "event_date": r.get("filed_last", ""),
                "filed_date": r.get("filed_last", ""),   # most recent lien recording
                "lien_total": total_active,
                "lien_n_priced": len(active_amts),
                "lien_active": len(active),
                "lien_recorded": len(instruments) or r.get("n_liens", 0),
                "lien_types": r.get("types") or [],
                "tax_due": p.get("amount_due"),
                "tax_years": unpaid,
                "notes": note,
                "source_url": "https://paxsub.knoxrod.org/views/search",
            })
    return out


_ADDR_RE = re.compile(
    r"\b(\d{1,6}\s+[A-Za-z0-9.'\- ]{2,40}?\s+"
    r"(?:Rd|Road|Dr|Drive|Ln|Lane|Ave|Avenue|Cir|Circle|St|Street|Pike|Pk|Way|Ct|Court|"
    r"Blvd|Pl|Place|Trl|Trail|Loop|Hwy|Highway|Ter|Terrace|Pkwy)\.?)\b",
    re.IGNORECASE)


def extract_address(text: str) -> str:
    """Best-effort street address out of free notice text."""
    if not text:
        return ""
    m = _ADDR_RE.search(text)
    return " ".join(m.group(1).split()) if m else ""


# ---------------------------------------------------------------- enrichment

_UNIT_RE = re.compile(r"\s*(#|\bAPT\b|\bUNIT\b|\bSTE\b|\bP-)\s*\S*$", re.IGNORECASE)


_STREET_ABBR = {
    "ROAD": "Rd", "LANE": "Ln", "AVENUE": "Ave", "DRIVE": "Dr", "STREET": "St",
    "COURT": "Ct", "PLACE": "Pl", "BOULEVARD": "Blvd", "HIGHWAY": "Hwy",
    "CIRCLE": "Cir", "TERRACE": "Ter", "PARKWAY": "Pkwy", "TRAIL": "Trl",
}
_ORDINALS = {
    "FIRST": "1st", "SECOND": "2nd", "THIRD": "3rd", "FOURTH": "4th",
    "FIFTH": "5th", "SIXTH": "6th", "SEVENTH": "7th", "EIGHTH": "8th",
    "NINTH": "9th", "TENTH": "10th", "ELEVENTH": "11th", "TWELFTH": "12th",
}


def normalize_address(a: str) -> str:
    """Coerce an address into the shape SiftMap's autocomplete will actually match.

    Verified against live misses: "1423 W. FIFTH AVENUE" returns nothing but
    "1423 W 5th Ave" resolves, and "307 HAMILTON ROAD" fails where "307 Hamilton
    Rd" succeeds. So spelled-out street types and spelled-out ordinals both have
    to be converted, and unit suffixes and an already-appended state/zip removed.
    """
    a = " ".join((a or "").split())
    a = re.sub(r",?\s*,?\s*TN\s*\d{5}(?:-\d{4})?$", "", a, flags=re.IGNORECASE)
    a = _UNIT_RE.sub("", a.rstrip(" ,"))
    a = a.replace(".", " ")

    words = []
    for w in a.split():
        up = w.upper()
        if up in _STREET_ABBR:
            words.append(_STREET_ABBR[up])
        elif up in _ORDINALS:
            words.append(_ORDINALS[up])
        elif up.isupper() and len(up) > 2 and up.isalpha():
            words.append(w.capitalize())   # ALL CAPS street names miss
        else:
            words.append(w)
    return " ".join(words).strip(" ,")


def enrich(records: list[dict], city: str = "Knoxville", state: str = "TN",
           sleep: float = 0.2) -> list[dict]:
    from clients.siftmap_api import SiftMapClient
    c = SiftMapClient()
    resolved = 0
    for i, r in enumerate(records):
        if not r.get("address"):
            continue
        q = "%s, %s, %s" % (normalize_address(r["address"]), city, state)
        try:
            hits = c.autocomplete(q, limit=3)
            if not hits:
                continue
            r["sm_title"] = hits[0].get("title")
            pid = hits[0].get("id")
            if not pid:
                continue
            d = c.get_detail(pid)
            r["sm"] = d
            resolved += 1
        except Exception as e:
            r["sm_error"] = "%s: %s" % (type(e).__name__, e)
        if (i + 1) % 25 == 0:
            print("  enriched %d/%d (resolved %d)" % (i + 1, len(records), resolved), flush=True)
        time.sleep(sleep)
    print("SiftMap resolved %d of %d" % (resolved, len(records)))
    return records


def in_buybox(d: dict, vmin: float, vmax: float) -> tuple[bool, str]:
    if not d:
        return False, "no SiftMap match"
    if d.get("property_type") != SFR:
        return False, "not single family (%s)" % (d.get("property_type") or "unknown")
    v = d.get("total_market_value")
    if v is None:
        return False, "no AVM"
    if not (vmin <= v <= vmax):
        return False, "AVM %s outside %s-%s" % (v, int(vmin), int(vmax))
    return True, ""


# ---------------------------------------------------------------- formatting

ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|CORP|CO|COMPANY|TRUST|TRUSTEE|PROPERTIES|PROPERTY|HOLDINGS|"
    r"PARTNERS|LP|L\.P|ASSOCIATION|CHURCH|BANK|FUND|GROUP|ENTERPRISES|INVESTMENTS|"
    r"THEATRE|THEATER|MINISTRIES|FOUNDATION)\b", re.IGNORECASE)


def is_entity(raw: str) -> bool:
    return bool(ENTITY_RE.search(raw or ""))


def split_name(raw: str) -> tuple[str, str]:
    """SiftMap returns owner_name as 'First [Middle...] Last'.

    An entity is never split: DataSift's contact logic wants the whole business
    name in the last-name column, not 'Carpetbag' / 'Theatre Inc'.
    """
    raw = " ".join((raw or "").replace(",", " ").split())
    if not raw:
        return "", ""
    if is_entity(raw):
        return "", raw
    parts = raw.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def split_mailing(raw: str) -> tuple[str, str, str, str]:
    """'215 E Glenwood Ave, Knoxville, TN 37917' -> street, city, state, zip."""
    raw = " ".join((raw or "").split())
    if not raw:
        return "", "", "", ""
    m = re.match(r"^(.*?),\s*([^,]+?),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 2:
        return parts[0], parts[1], "", ""
    return raw, "", "", ""


def to_datasift(r: dict, run_date: str) -> dict:
    d = r.get("sm") or {}
    oi = d.get("owner_info") or {}
    addr = d.get("address") if isinstance(d.get("address"), dict) else {}

    owner_name = oi.get("owner_name") or r.get("owner_raw") or ""
    first, last = split_name(owner_name)
    m_street, m_city, m_state, m_zip = split_mailing(oi.get("owner_mail_address") or "")

    filed = _iso(r.get("filed_date", ""))
    title = r.get("sm_title") or ""
    zip_code = ""
    m = re.search(r"\b(\d{5})\b", title)
    if m:
        zip_code = m.group(1)

    row = {k: "" for k in OUT_COLUMNS}
    row.update({
        "Property Street Address": r.get("address", ""),
        "Property City": "Knoxville",
        "Property State": "TN",
        "Property ZIP Code": zip_code,
        "Owner First Name": first,
        "Owner Last Name": last,
        "Mailing Street Address": m_street,
        "Mailing City": m_city,
        "Mailing State": m_state,
        "Mailing ZIP Code": m_zip,
        "Tags": ", ".join(t for t in [
            "Courthouse Data", r.get("notice_type", ""), "Knox",
            r.get("source", ""),
            ("filed_" + _quarter(filed)) if filed else "filed_unknown",
            _band(r["lien_total"]) if r.get("lien_total") else "",
            "pulled_" + run_date] if t),
        "Lists": LIST_NAME.get(r.get("notice_type", ""), ""),
        "Notes": build_notes(r),
        "Estimated Value": d.get("total_market_value") or "",
        "Last Sale Date": d.get("last_sale_date") or "",
        "Last Sale Price": d.get("last_sale_price") or "",
        "Parcel ID": d.get("apn") or "",
        "Structure Type": d.get("property_type") or "",
        "Year Built": d.get("years_built") or "",
        "Living SqFt": d.get("living_square_feet") or "",
        "Bedrooms": d.get("bedrooms") or "",
        "Bathrooms": d.get("bathrooms") or "",
        "Lot (Acres)": d.get("lot_acress") or "",
        "Tax Deliquent Value": _tax_due(r),                # DataSift's own typo
        "Tax Delinquent Year": _tax_year(r),
        "Notice Type": r.get("notice_type", ""),
        "County": "Knox",
        "Date Added": filed or run_date,
        "Source URL": r.get("source_url", ""),
    })
    row.update(_equity_block(r))
    if r.get("lien_total"):
        row["Outstanding Lien Amount"] = round(float(r["lien_total"]), 2)
    if r.get("lien_active") is not None and r.get("lien_recorded"):
        row["Liens Active vs Total"] = "%s of %s" % (r["lien_active"], r["lien_recorded"])
    if r.get("lien_types"):
        row["Lien Type"] = r["lien_types"][0]
    oi_pct = (d.get("owner_info") or {}).get("total_equity_avg")
    if oi_pct is not None:
        row["Equity Percentage"] = oi_pct
    if r.get("notice_type") == "trustee_deed" and filed:
        row["Foreclosure Date"] = filed
    if r.get("notice_type") == "probate":
        row["Probate Open Date"] = r.get("event_date", "")
        row["Personal Representative"] = r.get("owner_raw", "")
        row["Decedent Name"] = r.get("decedent", "")
    return row


def _mortgage_on_parcel(d: dict) -> float | None:
    """Mortgage attributable to THIS parcel.

    owner_info.total_mortgage is PORTFOLIO level, so it only equals this
    property when the owner holds one. For multi-parcel owners fall back to this
    parcel's own mortgage_history; if neither is usable, return None rather than
    inventing an equity figure.
    """
    oi = d.get("owner_info") or {}
    try:
        if int(oi.get("total_properties") or 0) == 1 and oi.get("total_mortgage") is not None:
            return float(oi["total_mortgage"])
    except (TypeError, ValueError):
        pass
    amts = [m.get("amount") for m in (d.get("mortgage_history") or []) if m.get("amount")]
    if amts:
        try:
            return float(max(amts))      # most recent/largest recorded loan
        except (TypeError, ValueError):
            pass
    # No mortgage of record: treat as free and clear rather than unknown (Ty).
    # Not perfect, but a missing mortgage row overwhelmingly means no lien
    # against title, and leaving equity blank made those records unjudgeable
    # for the upside-down test.
    return 0.0


def _equity_block(r: dict) -> dict:
    """Total delinquency, estimated equity, and the upside-down verdict."""
    d = r.get("sm") or {}
    lien = float(r.get("lien_total") or 0)
    tax = float(_tax_due(r) or 0)
    total_delinq = lien + tax

    avm = d.get("total_market_value")
    mort = _mortgage_on_parcel(d)
    equity = None
    if avm is not None and mort is not None:
        equity = float(avm) - mort

    presets = d.get("presets") or {}
    assumed_free_clear = (mort == 0.0
                          and not (d.get("owner_info") or {}).get("total_mortgage")
                          and not (d.get("mortgage_history") or []))
    reasons = []
    if presets.get("negative_equity"):
        reasons.append("SiftMap negative equity")
    if equity is not None and total_delinq > 0 and total_delinq >= equity:
        reasons.append("delinquency $%s >= equity $%s"
                       % (format(total_delinq, ",.0f"), format(equity, ",.0f")))
    if equity is not None and equity <= 0:
        reasons.append("no equity")

    pct = ""
    if equity and equity > 0 and total_delinq > 0:
        pct = round(100 * total_delinq / equity, 1)

    return {
        "_assumed_free_clear": assumed_free_clear,
        "Total Delinquency": round(total_delinq, 2) if total_delinq else "",
        "Est Equity": round(equity) if equity is not None else "",
        "Debt vs Equity Pct": pct,
        "Upside Down": "yes" if reasons else "",
        "Upside Down Reason": "; ".join(reasons),
    }


def _tax_due(r: dict):
    """Amount owed on THIS parcel. Authoritative: it comes back per parcel."""
    v = r.get("tax_due")
    try:
        return v if v not in (None, "") and float(v) > 0 else ""
    except (TypeError, ValueError):
        return ""


def _tax_year(r: dict):
    """Earliest unpaid year, but ONLY when this parcel itself owes money.

    The county tax API returns `bills` for the OWNER, not per parcel, so an
    owner holding several parcels would otherwise stamp another parcel's
    delinquency onto this one. Gating on a positive per-parcel amount keeps the
    year attributable. Same over-attribution class as the name-match guards.
    """
    if not _tax_due(r):
        return ""
    yrs = sorted(y for y in (r.get("tax_years") or []) if y)
    return yrs[0] if yrs else ""


def build_notes(r: dict) -> str:
    bits = []
    f = _iso(r.get("filed_date", ""))
    if f:
        bits.append("Filed/recorded at county %s (%s)" % (f, _quarter(f)))
    if r.get("case_number"):
        bits.append("Case %s" % r["case_number"])
    if r.get("event_date"):
        bits.append("Filed/heard %s" % r["event_date"])
    if r.get("notes"):
        bits.append(r["notes"])
    return " | ".join(bits)[:900]


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output/knox_ftm_pull.csv")
    ap.add_argument("--rejects", default="output/knox_ftm_pull_rejected.csv")
    ap.add_argument("--value-min", type=float, default=1.0)
    ap.add_argument("--value-max", type=float, default=700000.0)
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "."))
    ap.add_argument("--no-enrich", action="store_true")
    a = ap.parse_args()

    run_date = date.today().isoformat()
    records = (load_condemnations(a.scratch) + load_probate(a.scratch)
               + load_tnpn(a.scratch) + load_landlords(a.scratch)
               + load_rod(a.scratch) + load_liens(a.scratch))
    if not records:
        print("no source records found in", a.scratch)
        return 1

    from collections import Counter
    print("collected:", dict(Counter(r["source"] for r in records)))
    with_addr = [r for r in records if r.get("address")]
    print("with a street address: %d of %d" % (len(with_addr), len(records)))

    if not a.no_enrich:
        enrich(with_addr)

    kept, rejected = [], []
    for r in records:
        if not r.get("address"):
            rejected.append((r, "no property address in the record"))
            continue
        ok, why = in_buybox(r.get("sm"), a.value_min, a.value_max)
        (kept if ok else rejected).append((r, why) if not ok else r)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        w.writeheader()
        upside = []
        for r in kept:
            row = to_datasift(r, run_date)
            # Ty: do not upload the ones where debt swallows the equity, they
            # are not workable. Written to their own file, never silently lost.
            if row.get("Upside Down") == "yes":
                upside.append(row)
                continue
            w.writerow(row)

    if upside:
        up_path = a.out.replace(".csv", "_upside_down.csv")
        with open(up_path, "w", newline="", encoding="utf-8") as f:
            w2 = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
            w2.writeheader()
            w2.writerows(upside)
        print("UPSIDE DOWN %d -> %s (excluded from the upload)" % (len(upside), up_path))

    with open(a.rejects, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "notice_type", "address", "case_number", "reason"])
        for r, why in rejected:
            w.writerow([r.get("source"), r.get("notice_type"), r.get("address"),
                        r.get("case_number"), why])

    print("\nKEPT   %d -> %s" % (len(kept), a.out))
    print("DROPPED %d -> %s" % (len(rejected), a.rejects))
    print("drop reasons:", dict(Counter(w for _, w in rejected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
