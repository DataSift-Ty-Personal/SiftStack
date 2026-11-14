"""Enformion Business Search V2 client, and the rules for who counts as an entity's principal.

Verified live 2026-07-22 (158 Old State dispo build):
- Endpoint: POST https://devapi.enformion.com/BusinessV2Search with header
  ``galaxy-search-type: BusinessV2``. The v1 ``BusinessSearch`` type returns
  "Access denied" on this account; ``AddressSearch`` is not licensed at all.
- Response: ``businessV2Records[]``, each carrying ``usCorpFilings[]`` (SOS
  corp records: officers with title REGISTERED AGENT etc.) and/or
  ``newBusinessFilings[]`` (contacts + mail/business addresses).
- Registered agents are often commercial fronts (Northwest Registered Agent,
  Registered Agents Inc, US Corp Agents): when no human surfaces, fall back to
  the SiftMap reverse-address unmask (see buyer_sweep.reverse_address).
- Billed per MATCH: a search that returns no record costs nothing.

The principal rules below (moved here from dispo_buyers on 2026-11-13; dispo_buyers imports them
back, so its phases and tests are unchanged) decide which of the names a source hands back is a
person who runs the company. They live beside the client because both principal finders need
them, the free reverse-address check in buyer_sweep and the paid search here, and the team's
entity_principals workflow should not have to load the whole buyer registry to get them.
`norm_name` and `ORG_RX` are exact copies of the ones in enterprise_prospects, for the same
reason; a test pins the copies to the originals so the two cannot drift apart.
"""

from __future__ import annotations

import logging
import re

import requests

import config as cfg

logger = logging.getLogger(__name__)

BUSINESS_V2_URL = "https://devapi.enformion.com/BusinessV2Search"
TIMEOUT = 60
UNIT_USD = 0.10          # billed per match on the DataSift affiliate rate; a miss is free

# Commercial registered-agent fronts that are NOT principals.
AGENT_FRONTS = (
    "REGISTERED AGENT", "NORTHWEST", "CORPORATION AGENTS", "CORPORATE DIRECT",
    "REGISTERED AGENTS INC", "CT CORPORATION", "COGENCY", "INCORP",
)


# ---------------------------------------------------------------- names ----
# Copies of enterprise_prospects.norm_name and ORG_RX (pinned by a test).

ORG_RX = re.compile(
    r"\b(LLC|LLP|LP|INC|CORP|TRUST|PROPERTIES|HOLDINGS|HOMES|CAPITAL|INVEST|"
    r"VENTURES|GROUP|REALTY|SOLUTIONS|PARTNERS|ENTERPRISES|ASSET|BANK|COMPANY|"
    r"ASSOCIATION|FUND|MANAGEMENT|SERVICES|SVC|SVCS|POSTAL|CHURCH|MINISTRIES|"
    r"CITY|COUNTY|STATE|AUTHORITY|DISTRICT|FOUNDATION)\b")


def norm_name(raw: str) -> str:
    """Canonical buyer-name join key used by every phase."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (raw or "").upper())
    s = re.sub(r"\bL\s+L\s+C\b", "LLC", s)
    s = re.sub(r"\bL\s+L\s+P\b", "LLP", s)
    s = re.sub(r"\bL\s+P\b", "LP", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ------------------------------------------------------- principal rules ----

AGENT_FRONT_RX = re.compile(
    r"SERVICE OF PROCESS|CORPORATION SYSTEM|REGISTERED AGENT|NORTHWEST|"
    r"CORPORATION AGENTS|CORPORATE DIRECT|COGENCY|INCORP|NATIONAL ASSOC",
    re.I)


# A professional firm wearing a person-shaped name. The stock AGENT_FRONTS list
# only knows the national commercial agents (CT Corporation, Northwest), so it
# waves through the LOCAL law firms that actually appear on Knox filings.
# Measured live: "DRYER AND ASSOCIATES P C" and "CHAMBLISS BAHNER & STOPHEL"
# both came back as the "principal" of a real buying LLC.
#
# An ampersand ALONE is not the tell. Deed and filing names are full of married
# couples ("Oneal Brian A & Ethel L", "Ciminieri Louis & Colleen"), and a first
# pass that dropped every "&" threw away 7 real co-owning couples to catch 5
# firms. `clean_owner_name` already splits a couple down to one person, so the
# only thing worth rejecting is an explicit professional marker.
FIRM_RX = re.compile(
    r"\bAND ASSOCIATES\b|\bASSOCIATES\b|\bP\.?\s?C\.?\s*$|\bPLLC\b|\bLLP\b|"
    r"\bLAW\b|\bATTORNEY|\bLEGAL\b|\bFIRM\b|\bCPA\b|\bACCOUNTAN|"
    r"&\s*CO\b|\bLIMITED PARTNERSHIP\b|\bHOSPITAL\b|\bCLINIC\b|\bOFFICE\b",
    re.I)

# Titles that mean the person actually runs the company, as opposed to merely
# receiving its mail.
OWNER_TITLES = ("MANAGER", "MGR", "MEMBER", "ORGANIZER", "OFFICER", "PRESIDENT",
                "CEO", "SECRETARY", "TREASURER", "PARTNER", "DIRECTOR", "OWNER",
                "INCORPORATOR", "CONTACT")

# Legal status and credential tokens that a name parser can mistake for a
# surname. "Morales Family Trust" resolved to first=Morales last=Tr.
STATUS_TOKENS = {
    "TR", "TRUSTEE", "TTEE", "TRUST", "EST", "ESTATE", "LIFE", "ETAL", "ET",
    "AL", "UX", "JR", "SR", "II", "III", "IV", "V", "MD", "DC", "PC", "DDS",
    "DVM", "ESQ", "CPA", "REV", "LLC", "INC", "CORP", "CO", "LP", "LLP",
}


# Placeholder strings that arrive where a name should be. SiftMap returned the
# literal "UNKNOWN" as the owner of one LLC's mailing address, and it sailed
# through every entity and firm check because it is neither. Left alone it
# reaches a text as "Hi UNKNOWN".
PLACEHOLDER_NAMES = {
    "UNKNOWN", "UNKNOWN OWNER", "NONE", "N A", "NA", "TBD", "OWNER",
    "CURRENT OWNER", "OCCUPANT", "CURRENT RESIDENT", "RESIDENT", "TAXPAYER",
    "NOT AVAILABLE", "NO OWNER", "VACANT", "ESTATE OF",
}


def _is_human(name: str) -> bool:
    n = norm_name(name or "")
    if not n or n in PLACEHOLDER_NAMES:
        return False
    # A person has a first name and a surname. One bare token is a placeholder,
    # a company fragment, or a surname with no one attached to it.
    if len(n.split()) < 2:
        return False
    if ORG_RX.search(n) or AGENT_FRONT_RX.search(name or ""):
        return False
    return not FIRM_RX.search(name or "")


# BUSINESSV2 CANNOT BE CONSTRAINED GEOGRAPHICALLY, AND THAT IS THE WHOLE
# PROBLEM WITH IT. Verified live 2026-08-28: searching "SMITHBILT LLC" with no
# anchor, with Addresses[{AddressLine2: "Knoxville, TN"}], and with
# Addresses[{State: "TN"}] all return byte-identical results. It fuzzy-matches
# company names nationally, so a Knoxville LLC resolves to officers of a
# same-named company in another state. Measured on 12 cohort entities, only 2
# had ANY Tennessee officer, which means most of what the first pass bought was
# a stranger who shares a company name.
#
# The officer's own address is the only geographic control available, and it is
# only available because extract_officers() returns it. Filtering to the
# buyer's own state is therefore mandatory, not a refinement.
def _officer_in_state(officer: dict, state: str) -> bool:
    """Is this officer physically in the buyer's state.

    Tokenized rather than regexed on purpose. The first version used word
    boundaries and the escape did not survive being written to the file: the
    pattern compiled with literal backspace characters, matched nothing, and
    reported 0 of 153 entities verified as though it were a finding about the
    data. Splitting the address into alphanumeric tokens has no escaping to
    get wrong.
    """
    addr = (officer.get("address") or "").upper()
    if not addr:
        return False
    st = (state or "TN").upper()
    tokens = "".join(c if c.isalnum() else " " for c in addr).split()
    if st in tokens:
        return True
    return st == "TN" and "TENNESSEE" in tokens


def rank_officers(entity: str, officers: list, state: str) -> list:
    """State-verified officers first, then the ones who actually run the place.

    Ty's rule (2026-08-28): prefer an ownership title, fall back to any officer
    rather than giving up on the entity. But an officer in the wrong state is
    not a fallback, it is a different person, so those are dropped entirely.
    """
    keep = [o for o in officers if _officer_in_state(o, state)]

    def rank(o):
        conf = principal_confidence(entity, o.get("name") or "", o.get("title") or "")
        title = (o.get("title") or "").upper()
        owner_title = any(k in title for k in OWNER_TITLES)
        return (0 if owner_title else 1, 0 if conf == "high" else 1)
    return sorted(keep, key=rank)


def principal_confidence(entity: str, person: str, title: str) -> str:
    """How much to trust a BusinessV2 officer as the person to actually text.

    THE REGISTERED AGENT IS OFTEN THE COMPANY'S LAWYER, NOT ITS OWNER. On the
    live run 135 of 193 resolved officers carried the title AGENT or REGISTERED
    AGENT, and texting a firm's attorney a deal blast is the litigation bait
    this program is built to avoid.

    But an agent title is not automatically wrong: plenty of small operators are
    their own registered agent. The tell is the SURNAME. "Turner Homes LLC ->
    Michael L Turner" and "Smithbilt LLC -> Smith Kenneth" are obviously the
    owner; "New Season Properties LLC -> Dryer and Associates" is obviously not.

    So: an ownership title is trusted outright, an agent title is trusted only
    when a name token also appears in the company name, and everything else is
    low confidence and held back from the text list.
    """
    t = (title or "").upper()
    if any(k in t for k in OWNER_TITLES):
        return "high"
    ent_tokens = {w for w in norm_name(entity).split() if len(w) > 2}
    per_tokens = {w for w in norm_name(person).split() if len(w) > 2}
    if ent_tokens & per_tokens:
        return "high"
    return "low"


# ------------------------------------------------------ names and addresses ----
# Kept here rather than imported from enformion_ftm: that module imports notice_parser, which
# imports Playwright at module scope, and the agents brain has no browser. A test pins
# person_first_last to enformion_ftm.clean_owner_name, so a fix to one that is not made in the
# other fails loudly.
NAME_SPLIT_MARKERS = (" and ", " & ", " aka ", " a/k/a ", " c/o ", " et al", " et ux", " or ")
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def person_first_last(raw: str) -> tuple:
    """One clean (First, Last) from a possibly messy owner string: the primary person only,
    suffixes, middle initials and punctuation dropped. Same rules as enformion_ftm.clean_owner_name."""
    s = " " + (raw or "").strip().lower() + " "
    cut = len(s)
    for m in NAME_SPLIT_MARKERS:
        i = s.find(m)
        if i != -1:
            cut = min(cut, i)
    s = re.sub(r"[.,`()/]", " ", s[:cut])
    toks = [t for t in s.split() if t and t not in NAME_SUFFIXES and len(t) > 1]
    if len(toks) < 2:
        toks = [t for t in s.split() if t and t not in NAME_SUFFIXES]
    if len(toks) < 2:
        return "", ""
    return toks[0].title(), toks[-1].title()


def first_last_for(person: str, company: str = "") -> tuple:
    """(First, Last) for tracing a principal, or ("", "") when the name cannot be traced.

    NAME ORDER. County filings and corporate records write "SMITH KENNETH HAYWARD", not "Kenneth
    Smith". When the person came from a company there is a free oracle: whichever of their tokens
    also appears in the COMPANY name is the surname (Smith Homes LLC -> Smith Kenneth means Kenneth
    Smith). Measured: 45 of these on the live registry, each a lookup that could not have matched.

    A legal-status token is not a surname ("Morales Family Trust" read as first=Morales last=Tr),
    and a bare initial is not a first name ("E Bourgeois" buys a lookup that matches nobody)."""
    first, last = person_first_last(person)
    if not first or not last:
        return "", ""
    ent = {w for w in norm_name(company).split() if len(w) > 2}
    if ent and first.upper() in ent and last.upper() not in ent:
        first, last = last, first
    if first.upper() in STATUS_TOKENS or last.upper() in STATUS_TOKENS or len(first.strip(".")) < 2:
        return "", ""
    return first, last


def split_mail(mail: str) -> tuple:
    """"1007 Andover View Ln, Knoxville, TN 37919" -> its four parts.

    SiftMap hands the mailing address back as ONE string, but SmartSkip's
    contract is `mailingAddress` = the STREET, with mailingCity / mailingState /
    mailingZip as separate optional fields (its own synonym list calls the
    field "mailingstreet"). Passing the whole line as the street matches worse,
    and on a 1,900-row batch that is real money spent on a degraded input.
    """
    raw = (mail or "").strip().rstrip(",")
    if not raw:
        return "", "", "", ""
    # TWO ADDRESS FORMATS REACH THIS FUNCTION. SiftMap writes
    # "1007 Andover View Ln, Knoxville, TN 37919" but Enformion officer records
    # write "4300 Hiawatha; Knoxville, TN 37919" with a SEMICOLON before the
    # city. Splitting on commas alone glued the city onto the street and left
    # Mailing City empty, and SmartSkip returned nothing for all 33 rows of a
    # paid batch as a result.
    # The semicolon is the street/city delimiter, so it takes precedence over
    # commas rather than being flattened into one. "2099 Thunderhead, Ste 204;
    # Knoxville, TN 37922" has a comma INSIDE the street, and treating both
    # separators alike made the city read "Ste 204".
    head = ""
    if ";" in raw:
        head, raw = raw.split(";", 1)
        head, raw = head.strip().rstrip(","), raw.strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if head:
        # Everything before the semicolon is the street; what follows is
        # city, state and zip.
        parts = [head] + parts
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 2 else (parts[1] if len(parts) == 2 else "")
    tail = parts[-1] if len(parts) > 1 else ""
    m = re.match(r"^([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?$", tail)
    if m:
        state, zip_code = m.group(1).upper(), m.group(2)
        if len(parts) < 3:
            city = ""
    else:
        # No trailing "ST 12345": fall back to whatever we can see.
        state, zip_code = "", ""
        m2 = re.search(r"\b(\d{5})(?:-\d{4})?\b", tail)
        if m2:
            zip_code = m2.group(1)
        m3 = re.search(r"\b([A-Za-z]{2})\b", tail)
        if m3 and not zip_code:
            state = m3.group(1).upper()
    return street, city, state, zip_code


# ----------------------------------------------------------------- client ----

def is_configured() -> bool:
    return bool(cfg.ENFORMION_AP_NAME and cfg.ENFORMION_AP_PASSWORD)


def business_search(name: str, city_state: str = "", *, results: int = 3) -> list[dict]:
    """One BusinessV2 search. Returns businessV2Records (possibly empty)."""
    if not is_configured():
        return []
    body: dict = {"BusinessName": name, "Page": 1, "ResultsPerPage": results}
    if city_state:
        body["Addresses"] = [{"AddressLine2": city_state}]
    try:
        resp = requests.post(BUSINESS_V2_URL, json=body, timeout=TIMEOUT, headers={
            "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
            "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
            "galaxy-search-type": "BusinessV2",
            "Content-Type": "application/json",
        })
    except requests.RequestException as e:
        logger.warning("BusinessV2 request failed for %s: %s", name, e)
        return []
    if resp.status_code != 200:
        logger.warning("BusinessV2 HTTP %s for %s: %s", resp.status_code, name, resp.text[:200])
        return []
    try:
        return resp.json().get("businessV2Records") or []
    except ValueError:
        return []


def extract_officers(records: list[dict], entity_name: str) -> list[dict]:
    """Pull human officers/contacts from corp + new-business filings.

    Filters out entity self-references and commercial registered-agent fronts.
    Returns [{name, title, address}] deduped by name.
    """
    token = entity_name.split()[0].upper() if (entity_name or "").split() else ""
    seen: set[str] = set()
    officers: list[dict] = []
    for rec in records:
        filings = (rec.get("usCorpFilings") or []) + (rec.get("newBusinessFilings") or [])
        for filing in filings:
            fname = (filing.get("name") or filing.get("company") or "").upper()
            if not token or token not in fname:
                continue
            for off in (filing.get("officers") or []) + (filing.get("contacts") or []):
                n = off.get("name") or {}
                full = (n.get("nameRaw") or n.get("fullName") or "").strip()
                upper = full.upper()
                if (not full or upper in seen or "LLC" in upper or "INC" in upper
                        or any(f in upper for f in AGENT_FRONTS)):
                    continue
                seen.add(upper)
                officers.append({
                    "name": full,
                    "title": off.get("title") or off.get("officerTitleDesc") or "",
                    "address": (off.get("address") or {}).get("fullAddress") or "",
                })
    return officers


def find_principals(entity_name: str, city_state: str = "") -> list[dict]:
    """Convenience: search + extract in one call."""
    return extract_officers(business_search(entity_name, city_state), entity_name)


def search_officers(entity_name: str, city_state: str = "", *, search=None) -> tuple[list[dict], bool]:
    """(officers, billed). The search is billed per match, so a caller that keeps a budget needs to
    know whether this one cost anything: it did when any record came back, whatever the officers
    turned out to be. `search` stands in for business_search in tests."""
    records = (search or business_search)(entity_name, city_state)
    return extract_officers(records, entity_name), bool(records)
