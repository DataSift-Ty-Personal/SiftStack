"""Scrape court cases from Nebraska JUSTICE (probate, divorce, foreclosure).

Nebraska JUSTICE is the statewide Tyler court-records system. The subscriber
"full access" search (nebraska.gov/justice/) is a plain CGI form behind HTTP
Basic Auth — so this uses authenticated `requests` (no browser needed).

Key facts (see memory ne-justice-portal):
  - A BLANK party_name search returns the full case list for a county + case
    type (+ subtype) + year. The result LIST is FREE; the $2 charge applies only
    to opening a case Detail page — which this scraper NEVER does.
  - There is NO filing-date field. Recency comes from the case-number sequence
    (higher sequence = newer filing). We fetch newest-first by walking the tail
    of the ascending-sorted result set, and dedup against seen case numbers.
  - Results paginate 25 party-rows per page via a `start` offset; one case spans
    several party-rows, so rows are grouped by case number.

Supported record types (verified 2026-07-07):
  probate      PR  / County   (no subtype)   → decedent (caption) + PR contact
  divorce      CI  / District / DSSMARR      → petitioner + respondent
  foreclosure  CI  / District / FRMORTGE     → foreclosed owner (defendant)
Eviction is NOT supported: Nebraska county-civil FED cases aren't subtype-coded
in the index (REALFED/REALLLT return 0), so they can't be isolated from the
~38k county-civil cases. Use the courthouse-photo pipeline for evictions.

Output: list[NoticeData]. Property address is filled downstream by
ne_property_lookup (probate searches the decedent; divorce/foreclosure the owner).
"""

import logging
import random
import re
import time

import requests
import urllib3
from bs4 import BeautifulSoup

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

JUSTICE_URL = "https://www.nebraska.gov/justice/name.cgi"
PAGE_SIZE = 25

# County → JUSTICE county_num code (verified from the live form).
COUNTY_CODES = {"douglas": "01", "sarpy": "59"}

# Record type → search parameters + how to interpret the parties.
#   court:   "C" County Court, "D" District Court
#   kind:    "estate"  → decedent from caption (deceased-only), contact = PR party
#            "plaintiff_owner" → owner = petitioner/plaintiff party
#            "defendant_owner" → owner = defendant party (property being foreclosed)
RECORD_TYPES = {
    "probate":     {"case_type": "PR", "court": "C", "subtype": "",        "kind": "estate"},
    "divorce":     {"case_type": "CI", "court": "D", "subtype": "DSSMARR", "kind": "plaintiff_owner"},
    "foreclosure": {"case_type": "CI", "court": "D", "subtype": "FRMORTGE", "kind": "defendant_owner"},
}

# Party-type codes (JUSTICE) by role.
_PR_TYPES = ("PR", "PRP", "PET", "PERS", "COP", "APP")   # probate personal rep / petitioner
_PLAINTIFF_TYPES = ("PLF", "PET", "PLA")                 # petitioner / plaintiff
_DEFENDANT_TYPES = ("DEF", "RSP", "RES")                 # respondent / defendant


def _session() -> requests.Session:
    s = requests.Session()
    s.auth = (config.NEBRASKA_JUSTICE_USERNAME, config.NEBRASKA_JUSTICE_PASSWORD)
    s.verify = False  # gov cert valid; some local machines lack the issuer chain
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; SiftStack/1.0)"
    return s


def _base_payload(county_code: str, spec: dict, year2: str) -> dict:
    return {
        "party_name": "",
        "indiv_entity_type": "",
        "county_num": county_code,
        "case_type": spec["case_type"],
        "court_type": spec["court"],
        "year": year2,
        "subtype": spec["subtype"],
        "judge": "",
        "attorney_name": "",
        "sort": "casenum",
        "order": "asc",
        "client_data": "",
    }


def _fetch_page(session, base: dict, start: int, initial: bool) -> str:
    payload = dict(base)
    payload["submit_hidden"] = "search" if initial else "1"
    payload["start"] = "0" if initial else str(start)
    resp = session.post(JUSTICE_URL, data=payload, timeout=60)
    resp.raise_for_status()
    return resp.text


def _result_count(html: str) -> int:
    m = re.search(r"([\d,]+)\s+Results", html)
    return int(m.group(1).replace(",", "")) if m else 0


def _parse_rows(html: str, court: str, county_code: str, case_type: str, year2: str) -> list[dict]:
    """Extract (seq, party_type, party_name, caption) rows from a result page."""
    case_re = re.compile(rf"{court} {county_code} {case_type} {year2} (\d{{7}})")
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        case_cell = re.sub(r"\s+", " ", tds[1].get_text(" ", strip=True))
        m = case_re.search(case_cell)
        if not m:
            continue
        party = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True))
        # "LAST,FIRST M (TYPE) [Dob: ...]" — name before first "(", type in ().
        pm = re.match(r"^(.*?)\s*\(\s*(\w+)\s*\)", party)
        pname, ptype = (pm.group(1).strip().rstrip(","), pm.group(2).upper()) if pm else (party, "")
        caption = re.sub(r"\s+", " ", tds[2].get_text(" ", strip=True))
        rows.append({"seq": int(m.group(1)), "ptype": ptype, "pname": pname, "caption": caption})
    return rows


def _clean_person(raw: str) -> str:
    """'DUSATKO,JOANNE,M' → 'Joanne M Dusatko'; entities left as-is."""
    raw = (raw or "").strip().rstrip(",")
    if not raw:
        return ""
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) >= 2:
            last, first, *rest = parts
            raw = " ".join([first] + rest + [last])
    def cap(w):
        if w.isupper() and len(w) <= 3:
            return w  # keep short acronyms (LLC, INC, II)
        return "-".join(part.capitalize() for part in w.split("-"))
    return " ".join(cap(w) for w in raw.split())


def _caption_kind(caption: str) -> str:
    c = (caption or "").lower()
    if "deceased" in c or "estate of" in c:
        return "deceased"
    if "protected person" in c or "conservator" in c:
        return "protected"
    if "ward" in c or "minor" in c or "guardian" in c:
        return "ward"
    return "other"


def _decedent_from_caption(caption: str) -> str:
    c = re.sub(r"^\s*(?:Protected Person|Estate of)\s+", "", caption, flags=re.I)
    m = re.match(r"^(.*?),?\s*(Deceased|Ward|Incapacitated|Estate|Minor|Conservator)\b", c, re.I)
    return (m.group(1) if m else c).strip()


def _first_party(parties: list[dict], types: tuple, skip_placeholder: bool = False) -> str:
    """First party matching one of `types`. skip_placeholder drops Doe/unknown
    occupants (foreclosures name 'Jane Doe'/'John Doe'/'Unknown' as defendants)."""
    def ok(name: str) -> bool:
        # Foreclosure defendant lists include Doe occupants + government lienholders
        # (US of America, State, county treasurer) — skip to reach the real owner.
        return not (skip_placeholder and re.search(
            r"\b(?:doe|unknown|occupant|all persons|united states|state of|"
            r"county|treasurer|department|internal revenue|city of)\b", name, re.I))
    for t in types:
        hit = next((p for p in parties if p["ptype"] == t and ok(p["pname"])), None)
        if hit:
            return hit["pname"]
    return ""


def _match_defendant(parties: list[dict], caption: str) -> str:
    """Pick the defendant that matches the caption's post-'v.' party (the real
    property owner in a foreclosure), by surname — the DEF row order is unreliable
    and littered with gov/Doe co-defendants."""
    m = re.search(r"\sv\.?\s+(.+)$", caption)
    hint = (m.group(1) if m else "").lower()
    if not hint:
        return ""
    for p in parties:
        if p["ptype"] not in _DEFENDANT_TYPES:
            continue
        plast = p["pname"].split(",")[0].strip().lower()
        if plast and plast in hint:
            return p["pname"]
    return ""


def _case_number(court: str, county_code: str, case_type: str, year2: str, seq: int) -> str:
    return f"{court}{county_code}{case_type}{year2}{seq:07d}"


def _build_notice(seq, parties, record_type, spec, county, county_code, year2, date_added):
    caption = parties[0]["caption"] if parties else ""
    case_no = _case_number(spec["court"], county_code, spec["case_type"], year2, seq)
    kind = spec["kind"]
    decedent = owner = counter = ""

    if kind == "estate":
        decedent = _decedent_from_caption(caption)
        owner = _first_party(parties, _PR_TYPES) or next(
            (p["pname"] for p in parties if p["ptype"] not in ("DEC", "WRD")), "")
    elif kind == "plaintiff_owner":       # divorce: petitioner is the lead owner
        owner = _first_party(parties, _PLAINTIFF_TYPES)
        counter = _first_party(parties, _DEFENDANT_TYPES)
    elif kind == "defendant_owner":       # foreclosure: defendant is the owner
        owner = (_match_defendant(parties, caption)
                 or _first_party(parties, _DEFENDANT_TYPES, skip_placeholder=True))
        counter = _first_party(parties, _PLAINTIFF_TYPES)

    return NoticeData(
        address="",  # filled by ne_property_lookup (decedent for probate, owner otherwise)
        city="",
        state="NE",
        owner_name=_clean_person(owner),
        decedent_name=_clean_person(decedent),
        notice_type=record_type,
        county=county.title(),
        date_added=date_added,
        source_url=f"justice://{case_no}",
        raw_text=f"case={case_no} | caption={caption}"
                 + (f" | counterparty={_clean_person(counter)}" if counter else ""),
    )


def fetch_cases(
    county: str,
    record_type: str,
    year: int,
    date_added: str,
    max_cases: int = 40,
    seen_case_seqs: set[int] | None = None,
    include_guardianships: bool = False,
) -> list[NoticeData]:
    """Fetch the newest JUSTICE cases of a record type for a county.

    Walks the ascending-sorted result set from the tail backward (newest case
    numbers first), grouping party-rows into cases, until `max_cases` new cases
    are collected or an already-seen case number is reached.
    """
    county_key = county.lower()
    if county_key not in COUNTY_CODES:
        raise ValueError(f"Unsupported JUSTICE county: {county}")
    if record_type not in RECORD_TYPES:
        raise ValueError(f"Unsupported JUSTICE record type: {record_type}")
    if not config.NEBRASKA_JUSTICE_USERNAME or not config.NEBRASKA_JUSTICE_PASSWORD:
        raise RuntimeError("NEBRASKA_JUSTICE_USERNAME / PASSWORD not set")

    spec = RECORD_TYPES[record_type]
    county_code = COUNTY_CODES[county_key]
    year2 = f"{year % 100:02d}"
    seen = seen_case_seqs or set()

    session = _session()
    base = _base_payload(county_code, spec, year2)

    first_html = _fetch_page(session, base, 0, initial=True)
    total = _result_count(first_html)
    logger.info("JUSTICE %s %s %d: %d party-rows total", county, record_type, year, total)
    if total == 0:
        return []

    by_case: dict[int, list[dict]] = {}

    def _qualifies(seq: int) -> bool:
        if seq in seen:
            return False
        if spec["kind"] == "estate" and not include_guardianships:
            rows = by_case.get(seq) or []
            return bool(rows) and _caption_kind(rows[0]["caption"]) == "deceased"
        return True

    start = (max(total - 1, 0) // PAGE_SIZE) * PAGE_SIZE
    stop = False
    while start >= 0 and not stop:
        html = _fetch_page(session, base, start, initial=False)
        rows = _parse_rows(html, spec["court"], county_code, spec["case_type"], year2)
        for r in rows:
            by_case.setdefault(r["seq"], []).append(r)
        if any(r["seq"] in seen for r in rows):
            stop = True
        if len([s for s in by_case if _qualifies(s)]) >= max_cases:
            stop = True
        start -= PAGE_SIZE
        time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

    keep = sorted((s for s in by_case if _qualifies(s)), reverse=True)[:max_cases]
    notices = [
        _build_notice(seq, by_case[seq], record_type, spec, county, county_code, year2, date_added)
        for seq in keep
    ]
    logger.info("JUSTICE %s %s: %d cases kept, %d skipped (filtered/seen)",
                county, record_type, len(notices), len(by_case) - len(keep))
    return notices


# Back-compat: the original probate-only entry point.
def fetch_probate(county="Douglas", year=None, max_cases=40, seen_case_seqs=None,
                  date_added=None, include_guardianships=False):
    if year is None or date_added is None:
        raise ValueError("fetch_probate requires explicit year and date_added")
    return fetch_cases(county, "probate", year, date_added, max_cases,
                       seen_case_seqs, include_guardianships)
