"""The calling agency's file rules, in one place: which numbers on a record are callable, which owners are held
out of the file, and the record fields the file reads. agency_call_list.py builds the file with these rules; the
October one-off builder (knox_blount_top15k.py) reads the same ones. Nothing here touches a network.
"""

from __future__ import annotations

import re

# A number comes off only for what the CRM already knows: the agency runs its own DNC and litigator scrub.
BAD_PHONE_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
LITIGATOR_TAG = "DNC"          # d4d_call_list's convention: a prior litigator hit tags the phone DNC
LIVE_LEAD_STATUSES = {"new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead",
                      "Hot Lead", "Ghosting Lead", "follow_up"}
MAX_PHONES = 10
NAVY, LINE, SOFT = "0A1130", "D9DEE8", "F4F6FA"   # the workbook's header text, rule and fill


def slim(d: dict) -> dict:
    """The fields the agency file reads off a full DataSift record (GET /api/internal/property/<uuid>/)."""
    ow = d.get("owner") or {}
    a = d.get("address") or {}
    m = ow.get("address") or {}
    sec = d.get("secondary_owners") or []
    st = d.get("status")
    return {"uuid": d.get("uuid"), "dataflik_id": str(d.get("dataflik_id") or ""),
            "status": st.get("title") if isinstance(st, dict) else st,
            "street": a.get("street"), "city": a.get("city"), "state": a.get("state"),
            "zip": (a.get("postal_code") or "")[:5], "county": a.get("county"),
            "vacant": a.get("vacant"),
            "owner_uuid": ow.get("uuid"), "first": ow.get("first_name"),
            "last": ow.get("last_name"), "company": ow.get("company"),
            "deceased": ow.get("deceased"), "dnc": ow.get("dnc"),
            "opt_out": ow.get("opt_out"), "skiptraced": ow.get("skiptraced"),
            "m_street": m.get("street"), "m_city": m.get("city"),
            "m_state": m.get("state"), "m_zip": (m.get("postal_code") or "")[:5],
            "phones": ow.get("phones") or [], "emails": ow.get("emails") or [],
            "owner2": " ".join(x for x in ((sec[0].get("first_name") or ""),
                                           (sec[0].get("last_name") or "")) if x).strip()
            if sec and isinstance(sec[0], dict) else "",
            "value": d.get("estimate_value"), "equity": d.get("equity_percent"),
            "year": d.get("year"), "beds": d.get("bedrooms"), "baths": d.get("bathrooms"),
            "sqft": d.get("sqft")}


def clean_phones(r: dict) -> tuple[list, dict]:
    """Callable numbers as (ten digits, type), mobiles first, DataSift's order otherwise; and why each other
    number came off."""
    why: dict[str, int] = {}
    keep, seen = [], set()
    for p in r["phones"]:
        tags = [t.get("title") if isinstance(t, dict) else t for t in p.get("tags") or []]
        digits = "".join(c for c in str(p.get("number") or "") if c.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        reason = ""
        if len(digits) != 10 or digits[0] in "01":
            reason = "invalid number"
        elif (p.get("status") or "").upper() in BAD_PHONE_STATUSES:
            reason = "dispositioned %s" % p["status"].upper()
        elif LITIGATOR_TAG in tags:
            reason = "prior litigator flag"
        elif p.get("is_connected") is False:
            reason = "disconnected"
        elif digits in seen:
            reason = "duplicate"
        if reason:
            why[reason] = why.get(reason, 0) + 1
            continue
        seen.add(digits)
        keep.append((digits, (p.get("type") or "").title()))
    keep.sort(key=lambda x: x[1] != "Mobile")
    return keep, why


def display_name(s) -> str:
    """'SMITH' and 'smith' -> 'Smith'; a name already in mixed case is left as written."""
    s = (s or "").strip()
    return s.title() if s.isupper() or s.islower() else s


ENTITY = re.compile(
    r"\b(llc|llp|lp|inc|incorporated|corp|co|company|trust|tr|trustee|revocable|"
    r"irrevocable|living|est|estate|church|partners|partnership|properties|holdings|"
    r"investments?|enterprises?|enteprise|homes|rentals?|construction|development|"
    r"realty|mortgage|fund|series|acquis\w*|acquistion|restoration|habitat|solutions|"
    r"supply|management|manafement|group)\b", re.I)
_PLACEISH = re.compile(r"(-|wood|lawn|towne?|ville|view|side|land|field|ridge|grove|"
                       r"hill|park|creek|valley)$", re.I)


def church_as_person(first, last) -> bool:
    """A church filed in the person fields ('West-Towne' / 'Church'); a person named Church is not one."""
    return ((last or "").strip().lower() in ("church", "chapel", "ministries")
            and bool(_PLACEISH.search((first or "").strip()) or "-" in (first or "")))


_SUFFIX = {"street": "st", "avenue": "ave", "drive": "dr", "road": "rd", "lane": "ln",
           "court": "ct", "circle": "cir", "boulevard": "blvd", "place": "pl",
           "highway": "hwy", "parkway": "pkwy", "trail": "trl", "terrace": "ter",
           "north": "n", "south": "s", "east": "e", "west": "w"}
_UNIT = {"apt", "unit", "ste", "suite", "lot", "bldg", "#"}


def addr_key(s) -> str:
    """'100 Example Dr # B' and '100 Example Drive' -> one key."""
    out = []
    for t in "".join(c if c.isalnum() or c == "#" else " " for c in (s or "").lower()).split():
        if t in _UNIT or t.startswith("#"):
            break
        out.append(_SUFFIX.get(t, t))
    return " ".join(out)


def num(v, places: int = 0):
    """352000.00 -> 352000, 1.0 -> 1, 80.57 stays 80.57; None -> ''."""
    if v in (None, ""):
        return ""
    try:
        f = round(float(v), places)
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else f
