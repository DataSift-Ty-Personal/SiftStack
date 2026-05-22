"""Jefferson County, KY PVA (Property Valuation Administrator) lookup.

Source: https://jeffersonpva.ky.gov/property-search/

Authenticated HTTP-only scraper (requests + BeautifulSoup) for the Jefferson
County PVA. Requires a paid subscription login stored in env vars
``PVA_EMAIL`` / ``PVA_PASSWORD``. After login, a single ``PHPSESSID`` cookie
carries the session; every subsequent request is plain GET.

Public interface (mirrors tax_enricher so enrichment_pipeline can dispatch
by county):

  * ``probate_property_lookup(notices)`` — for KY probate records without an
    address, search by decedent name (LAST FIRST format) and populate the
    property address + parcel_id + assessed_value if a confident match is
    found.
  * ``lookup_parcel_addresses(notices)`` — for KY records with a parcel_id
    already populated, fetch the official PVA mailing address and owner
    string. Mirrors tax_enricher.lookup_parcel_addresses for Knox.

Session-conflict behavior: the user's PVA plan allows 1 concurrent session.
If a prior session is still alive on the server when we log in, the login
page re-renders with an "Active Sessions" table plus an End-Session form.
We auto-evict and retry login transparently.

Runs should be scheduled at off-hours (e.g. 4am ET) because the scraper's
login will kick the user out of any active browser session.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
import requests
from bs4 import BeautifulSoup

import config
from config import REQUEST_DELAY_MAX, REQUEST_DELAY_MIN
from kentucky_name_resolver import (
    CandidatePerson,
    NameVariant,
    disambiguate,
    generate_variants,
    score_match,
)

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── URLs + behavior knobs ─────────────────────────────────────────────
PVA_BASE_URL = "https://jeffersonpva.ky.gov"
PVA_LOGIN_URL = f"{PVA_BASE_URL}/login/"
PVA_LOGOUT_URL = f"{PVA_BASE_URL}/logout/"
PVA_LISTINGS_URL = f"{PVA_BASE_URL}/property-search/property-listings/"
PVA_DETAIL_URL = f"{PVA_BASE_URL}/property-search/property-details/"

REQUEST_TIMEOUT = 20  # seconds — PVA pages can be slow under load

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Minimum name-match score (0..1) to accept a property as the decedent's.
# Jefferson owner strings are often joint ("SMITH JOHN & SMITH JANE"), so
# substring matching of decedent's last+first tokens is the primary signal.
_MIN_MATCH_SCORE = 0.5

# Cap result pages visited per name search — Jefferson's owner-name search
# can return thousands of rows for common surnames (e.g. "SMITH"). First
# page is the strongest match by relevance.
_MAX_PAGES_PER_SEARCH = 3


# ── Data model for a listing row ──────────────────────────────────────
@dataclass
class PvaRow:
    """One row from the listings page. ``lrsn`` is the stable parcel key."""
    address: str
    owner: str
    parcel_id: str
    lrsn: str
    legal: str = ""


# ── Session management ────────────────────────────────────────────────


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT})
    return s


def _is_login_page(html: str) -> bool:
    """Check if the response is the (re-)login page."""
    return 'name="vsm_username"' in html and 'name="vsm_password"' in html


def _has_session_limit(html: str) -> bool:
    """Check if the login page is showing the session-limit table."""
    return 'class="end-session"' in html or "table-active-sessions" in html


def _extract_session_ids(html: str) -> list[str]:
    """Pull session_id hidden-input values out of the end-session form."""
    return re.findall(
        r'<form[^>]*class="end-session"[^>]*>[^<]*<input[^>]*name="session_id"[^>]*value="(\d+)"',
        html,
    )


def _evict_session(session: requests.Session, session_id: str) -> None:
    """POST session_id to /login/ to kill the prior session."""
    logger.info("  [PVA] Evicting existing session id=%s", session_id)
    session.post(
        PVA_LOGIN_URL,
        data={"session_id": session_id},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )


def _login(session: requests.Session) -> bool:
    """Log the session in. Returns True on success.

    Handles the 1-concurrent-session limit by evicting any existing session
    and re-submitting the login form.
    """
    if not config.PVA_EMAIL or not config.PVA_PASSWORD:
        logger.warning("  [PVA] PVA_EMAIL / PVA_PASSWORD not set — cannot authenticate")
        return False

    creds = {
        "vsm_username": config.PVA_EMAIL,
        "vsm_password": config.PVA_PASSWORD,
        "submit_login_form": "Log In",
    }

    for attempt in (1, 2):
        resp = session.post(
            PVA_LOGIN_URL,
            data=creds,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        if not _is_login_page(resp.text):
            logger.info("  [PVA] Authenticated as %s (attempt %d)", config.PVA_EMAIL, attempt)
            return True

        if _has_session_limit(resp.text):
            session_ids = _extract_session_ids(resp.text)
            if not session_ids:
                logger.warning("  [PVA] Session-limit page had no session_id to evict")
                return False
            for sid in session_ids:
                _evict_session(session, sid)
            # Loop and re-submit credentials
            continue

        logger.warning("  [PVA] Login failed — credentials may be wrong")
        return False

    logger.warning("  [PVA] Login failed after session-conflict retry")
    return False


def _logout(session: requests.Session) -> None:
    """Best-effort logout so we release the 1-session slot for the user."""
    try:
        session.get(PVA_LOGOUT_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        pass


# ── HTTP helpers ──────────────────────────────────────────────────────


def _polite_delay() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _get(session: requests.Session, url: str, params: dict | None = None) -> str | None:
    """GET a URL, return HTML string or None on error."""
    try:
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        logger.warning("  [PVA] GET %s failed: %s", url, e)
        return None


# ── Owner search ──────────────────────────────────────────────────────


def _parse_listing_page(html: str) -> list[PvaRow]:
    """Parse a property-listings HTML page into PvaRow objects.

    The listing is a real <table> with 5 <td> cells per <tr>:
      [0] thumbnail, [1] address, [2] owner, [3] legal description, [4] parcel ID.
    Each cell wraps its content in an <a href="...property-details/?lrsn=..."> anchor.
    TD[1] also contains a hidden ``<span class="mini-owner">`` visible only on
    mobile — strip those before extracting text so the address isn't polluted.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: dict[str, PvaRow] = {}

    for tr in soup.select("tr"):
        # Must contain a property-details link to be a result row
        first_link = tr.find("a", href=re.compile(r"property-details"))
        if not first_link:
            continue
        lrsn_m = re.search(r"lrsn=(\d+)", first_link.get("href", ""))
        if not lrsn_m:
            continue
        lrsn = lrsn_m.group(1)
        if lrsn in rows:
            continue

        # Drop the hidden mobile-only owner span before extracting cell text
        for span in tr.select("span.mini-owner, span.visible-xs"):
            span.decompose()

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue

        def cell_text(td) -> str:
            # Prefer the <a> text if present (the label); fall back to full td text
            a = td.find("a")
            txt = (a.get_text(" ", strip=True) if a else td.get_text(" ", strip=True))
            return re.sub(r"\s+", " ", txt).strip()

        rows[lrsn] = PvaRow(
            address=cell_text(tds[1]),
            owner=cell_text(tds[2]),
            legal=cell_text(tds[3]),
            parcel_id=cell_text(tds[4]),
            lrsn=lrsn,
        )

    return list(rows.values())


def search_by_owner(
    session: requests.Session, owner_name: str, max_pages: int = _MAX_PAGES_PER_SEARCH,
) -> list[PvaRow]:
    """Run an owner-name search. Returns rows across all pages (up to limit)."""
    all_rows: list[PvaRow] = []
    seen_lrsns: set[str] = set()

    for page in range(1, max_pages + 1):
        params = {
            "psfldOwner": owner_name,
            "propertySearchFormButton": "Search",
            "searchType": "OwnerSearch",
        }
        if page > 1:
            params["searchPage"] = str(page)

        _polite_delay()
        html = _get(session, PVA_LISTINGS_URL, params=params)
        if not html:
            break

        rows = _parse_listing_page(html)
        new_rows = [r for r in rows if r.lrsn not in seen_lrsns]
        if not new_rows:
            break

        for r in new_rows:
            seen_lrsns.add(r.lrsn)
        all_rows.extend(new_rows)

        # Stop when page returned fewer than the typical page size (20);
        # indicates we're on the final page.
        if len(rows) < 20:
            break

    return all_rows


# Street suffix normalization — applied ONLY to the trailing word of the
# address, since the same words ("RIDGE", "POINT", "COVE", "WAY") often
# appear inside street NAMES rather than as suffixes (e.g. "RIVA RIDGE PT"
# where RIDGE is part of the name and PT is the suffix). Limiting to the
# last word keeps the normalizer from corrupting street names.
_SUFFIX_MAP = {
    "LANE":      "LN",
    "DRIVE":     "DR",
    "AVENUE":    "AVE",
    "ROAD":      "RD",
    "STREET":    "ST",
    "BOULEVARD": "BLVD",
    "COURT":     "CT",
    "PLACE":     "PL",
    "CIRCLE":    "CIR",
    "TERRACE":   "TER",
    "PARKWAY":   "PKWY",
    "HIGHWAY":   "HWY",
    "TRAIL":     "TRL",
    "POINT":     "PT",
    "RIDGE":     "RDG",
    "COVE":      "CV",
    # Already-short forms retained as-is so a row that has them doesn't get
    # un-abbreviated. Identity entries kept for clarity.
    "LN": "LN", "DR": "DR", "AVE": "AVE", "RD": "RD", "ST": "ST",
    "BLVD": "BLVD", "CT": "CT", "PL": "PL", "CIR": "CIR", "TER": "TER",
    "PKWY": "PKWY", "HWY": "HWY", "TRL": "TRL", "PT": "PT", "RDG": "RDG",
    "CV": "CV", "WAY": "WAY",
}


def _normalize_street_address(addr: str) -> str:
    """Uppercase, collapse whitespace, abbreviate trailing suffix only.

    Returns the address with the LAST word abbreviated if it's a known
    suffix. Words earlier in the address are left alone to preserve
    street names like "RIVA RIDGE PT" or "POINT BLANK DR".
    """
    s = re.sub(r"\s+", " ", (addr or "").upper().strip())
    s = s.rstrip(",. ")
    if not s:
        return ""
    parts = s.split()
    last = parts[-1]
    if last in _SUFFIX_MAP:
        parts[-1] = _SUFFIX_MAP[last]
    return " ".join(parts)


_HOUSE_NUM_RE = re.compile(r"^\s*(\d+[A-Za-z]?)\b")


def _house_number(addr: str) -> str:
    """Extract the leading house number (e.g. '1005' or '5206A')."""
    m = _HOUSE_NUM_RE.match(addr or "")
    return m.group(1) if m else ""


def search_by_parcel(session: requests.Session, parcel_id: str) -> list[PvaRow]:
    """Run a parcel-ID search. Single result expected."""
    params = {
        "psfldParcelId": parcel_id,
        "propertySearchFormButton": "Search",
        "searchType": "ParcelSearch",
    }
    _polite_delay()
    html = _get(session, PVA_LISTINGS_URL, params=params)
    if not html:
        return []
    return _parse_listing_page(html)


def search_by_address(
    session: requests.Session, address: str, max_pages: int = _MAX_PAGES_PER_SEARCH,
) -> list[PvaRow]:
    """Run a street-name search (PVA's StreetSearch endpoint).

    PVA's address search **does not accept house numbers** in the query —
    submitting "5206 TWINKLE DR" returns 0 rows; submitting "TWINKLE DR"
    returns all 25 properties on that street, each with its full
    house+street address in the row. Strategy: strip the house number
    before querying, then the caller filters returned rows by matching
    the full address.

    Long streets (e.g. Sale Ave has 205 properties) span multiple result
    pages, so this paginates up to ``max_pages``. Default cap mirrors
    ``search_by_owner`` to bound the polite-delay budget; raise it for
    flyer/lookup tools that need to find a specific house number.

    Suffix abbreviation matters too: PVA stores suffixes in their
    abbreviated form (LN/DR/AVE/RD), so "CANNONS LANE" returns the same
    rows as "CANNONS LN" but full-form variants are preserved here for
    fault tolerance.
    """
    if not address or not address.strip():
        return []
    # Strip any leading house number — PVA can't match if it's present.
    street_only = re.sub(r"^\s*\d+\s+", "", address.strip())
    if not street_only:
        return []

    all_rows: list[PvaRow] = []
    seen_lrsns: set[str] = set()
    for page in range(1, max_pages + 1):
        params = {
            "psfldAddress": street_only,
            "propertySearchFormButton": "Search",
            "searchType": "StreetSearch",
        }
        if page > 1:
            params["searchPage"] = str(page)
        _polite_delay()
        html = _get(session, PVA_LISTINGS_URL, params=params)
        if not html:
            break
        page_rows = _parse_listing_page(html)
        new_rows = [r for r in page_rows if r.lrsn not in seen_lrsns]
        if not new_rows:
            break
        for r in new_rows:
            seen_lrsns.add(r.lrsn)
        all_rows.extend(new_rows)
        if len(page_rows) < 20:
            break

    return all_rows


# ── Detail page fetch ─────────────────────────────────────────────────


def _parse_area_table(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the property's area table (Main Unit / Basement / Attic / Garage).

    Jefferson PVA renders these in an HTML <table> with headers
    "Area Type | Gross Area | Finished Area" — separate from the <dl>
    pairs that everything else uses. Returns flat keys suitable for
    merging into the get_detail() dict, e.g.:

      'Area:Main Unit:Finished' = '616'
      'Area:Main Unit:Gross'    = '-'
      'Area:Basement:Finished'  = '0'
      'Area:Basement:Gross'     = '616'
    """
    out: dict[str, str] = {}
    for table in soup.find_all("table"):
        thead = table.find("thead")
        if not thead:
            continue
        headers = [th.get_text(" ", strip=True).lower() for th in thead.find_all("th")]
        if not (any("area type" in h for h in headers)
                and any("gross" in h for h in headers)
                and any("finished" in h for h in headers)):
            continue
        # Header indices include the row-label column (col 0). The <td> cells
        # in body rows skip that column (the row label is a <th>), so subtract 1.
        gross_col = next(
            (i - 1 for i, h in enumerate(headers) if "gross" in h), -1
        )
        fin_col = next(
            (i - 1 for i, h in enumerate(headers) if "finished" in h), -1
        )
        for tr in table.select("tbody tr"):
            label_th = tr.find("th")
            if not label_th:
                continue
            label = label_th.get_text(" ", strip=True)
            tds = tr.find_all("td")
            if 0 <= gross_col < len(tds):
                out[f"Area:{label}:Gross"] = tds[gross_col].get_text(" ", strip=True)
            if 0 <= fin_col < len(tds):
                out[f"Area:{label}:Finished"] = tds[fin_col].get_text(" ", strip=True)
        break
    return out


def get_detail(session: requests.Session, lrsn: str) -> dict[str, str]:
    """Fetch a property detail page and return all labeled fields.

    Labels come from <dl><dt>..</dt><dd>..</dd></dl> pairs. Square footage
    lives in a separate <table> (Main Unit / Basement / Attic / Garage rows)
    and gets merged in with ``Area:<row>:Gross`` / ``Area:<row>:Finished``
    keys. Duplicate labels (e.g. multiple Deed Book/Page entries in the
    sales history) are suffixed with an index.
    """
    _polite_delay()
    html = _get(session, PVA_DETAIL_URL, params={"lrsn": lrsn})
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    seen_labels: dict[str, int] = {}

    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(" ", strip=True)
            value = dd.get_text(" ", strip=True)
            if label in fields:
                seen_labels[label] = seen_labels.get(label, 0) + 1
                label = f"{label} [{seen_labels[label]}]"
            fields[label] = value

    fields.update(_parse_area_table(soup))
    return fields


# ── Name matching / scoring ───────────────────────────────────────────
# Primitives (SUFFIX_RE, name_tokens, _search_variations, score_match) live in
# the canonical kentucky_name_resolver module — imported at the top of this file.


# ── Apply result to notice ────────────────────────────────────────────

_MONEY_RE = re.compile(r"[^\d]")


def _parse_money(s: str) -> str:
    """'$399,990' -> '399990'. Empty on failure."""
    if not s:
        return ""
    return _MONEY_RE.sub("", s)


def _apply_to_notice(
    notice: "NoticeData", row: PvaRow, detail: dict[str, str],
    owner_status: str = "direct",
) -> None:
    """Populate a NoticeData from a PvaRow + detail-page dict.

    ``owner_status`` is one of:
      * "direct"      — decedent is named as the PVA owner
      * "estate"      — PVA shows "ESTATE OF <decedent>"
      * "heir_recent" — PVA shows a third party, but a deed shows the
                        decedent transferred to them in the last 24 months
    The equity estimator (Phase 2d) gates on ``property_owner_status`` so
    equity is only computed when current ownership is confirmed — per the
    product rule that equity is meaningless if the estate no longer holds
    the property.
    """
    # Prefer the PVA mailing address from the detail page (includes zip+4)
    mail = detail.get("Mailing Address", "").strip()
    if mail:
        # Format: "7802 RIVA RIDGE PT, LOUISVILLE, KY 40214-4177"
        m = re.match(r"(.+?),\s*(.+?),\s*(\w{2})\s*(\d{5}(?:-\d{4})?)", mail)
        if m:
            notice.address = m.group(1).title()
            notice.city = m.group(2).title()
            notice.state = m.group(3).upper()
            notice.zip = m.group(4).split("-")[0]
            notice.zip_plus4 = m.group(4)
        else:
            notice.address = mail
    else:
        notice.address = row.address.title() if row.address else ""
        notice.city = "Louisville"
        notice.state = "KY"

    if not notice.state:
        notice.state = "KY"

    if row.parcel_id:
        notice.parcel_id = row.parcel_id

    owner = detail.get("Owner") or row.owner
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner

    # Refine owner_status based on the actual PVA owner string. The matched
    # owner is the strongest evidence — it overrides the caller's hint.
    #   "ESTATE OF X"          → "estate"
    #   "X TRUST" / "TRUSTEE"  → "trust" (only if not already an estate)
    if owner:
        owner_upper = owner.upper()
        if "ESTATE OF" in owner_upper:
            owner_status = "estate"
        elif "TRUST" in owner_upper or "TRUSTEE" in owner_upper:
            owner_status = "trust"
    notice.property_owner_status = owner_status

    # Assessed value → estimated_value (equity estimator reads this field)
    assessed = _parse_money(detail.get("Assessed Value", ""))
    if assessed and not notice.estimated_value:
        notice.estimated_value = assessed

    year_built = detail.get("Year Built", "").strip()
    if year_built and not notice.year_built:
        notice.year_built = year_built


# ── Public entry points (match tax_enricher shape) ────────────────────


def probate_property_lookup(notices: list["NoticeData"]) -> None:
    """For KY probate records without an address, find the decedent's property.

    Mutates notices in place. Runs one login per call and reuses the session
    across all records. No-ops if credentials are missing or login fails.
    """
    candidates = [
        n for n in notices
        if n.notice_type == "probate"
        and not n.address.strip()
        and n.decedent_name.strip()
        and n.county.lower() == "jefferson"
    ]
    if not candidates:
        return

    logger.info("  [PVA] Starting probate lookup for %d decedent(s)", len(candidates))
    session = _make_session()
    if not _login(session):
        logger.warning("  [PVA] Could not authenticate; skipping all %d records", len(candidates))
        return

    try:
        for notice in candidates:
            _lookup_one(session, notice)
    finally:
        _logout(session)


# Tokens that mark a name as a non-individual entity. When the search
# target contains these, we use the verbatim string as the primary query
# rather than splitting into LAST/FIRST variations (which would garble
# names like "ROBERT G REAGAN TRUST").
_ENTITY_NAME_RE = re.compile(
    r"\b(?:TRUST|TRUSTEE|LLC|INC|CORP|LP|LTD|FOUNDATION|ESTATE)\b",
    re.IGNORECASE,
)


def _entity_search_variations(holder: str) -> list[str]:
    """Variations for a non-individual title holder (trust, LLC, estate)."""
    cleaned = re.sub(r"\s+", " ", holder).strip()
    if not cleaned:
        return []
    variations = [cleaned]

    # Trust-specific: try the "X TRUST" canonical form. PVA often stores
    # trusts as "REAGAN ROBERT G TRUST" — surname first.
    m = re.match(r"^(.+?)\s+TRUST\b", cleaned, re.IGNORECASE)
    if m:
        trust_subject = m.group(1).strip()
        if trust_subject and trust_subject.upper() != cleaned.upper():
            # Try just the subject (first half of "X Y TRUST")
            variations.append(f"{trust_subject} TRUST")
    return list(dict.fromkeys(v for v in variations if v.strip()))


def _lookup_one(session: requests.Session, notice: "NoticeData") -> None:
    """Search PVA for the property tied to this notice.

    Search-target priority:
      1. ``current_property_holder`` from Phase 2b (deed-chain analysis) —
         this is the highest-confidence target because it reflects who
         actually holds title now, not who used to.
      2. ``decedent_name`` (legacy fallback when deed chain wasn't run).

    For non-individual holders (trust, LLC, estate), use verbatim-string
    variations rather than the LAST/FIRST splitter that's tuned for
    person names.
    """
    # Pick the search target
    holder = (notice.current_property_holder or "").strip()
    holder_relationship = notice.current_holder_relationship.strip()
    if holder:
        primary_target = holder
        target_source = f"deed-chain ({holder_relationship or 'unknown'})"
    elif notice.decedent_name.strip():
        primary_target = notice.decedent_name
        target_source = "decedent name"
    else:
        return

    # Build search variants. Non-individual holders (trust, LLC, estate) use
    # the verbatim-string variation set; person names go through the resolver,
    # which widens the search to maiden/prior-married/non-Anglo surname forms
    # (NAME-01). Maiden/aka context comes from the obituary step (Plan 03);
    # getattr falls back to None so this still works if obituary didn't run.
    if _ENTITY_NAME_RE.search(primary_target):
        variations = _entity_search_variations(primary_target)
        variants = [
            NameVariant(value=v, fmt="ENTITY", source="primary", confidence=1.0)
            for v in variations
        ]
    else:
        variants = generate_variants(
            primary_target,
            maiden_name=getattr(notice, "decedent_obit_maiden_name", None) or None,
            prior_surnames=getattr(notice, "decedent_also_known_as", None) or None,
        )
    if not variants:
        return

    logger.info(
        "  [PVA] Target %r [%s] -> %d variants (sources: %s)",
        primary_target, target_source, len(variants),
        sorted({v.source for v in variants}),
    )

    best: tuple[float, PvaRow, str] | None = None  # (score, row, variation_used)

    # Loop variants HIGHEST-confidence-first (generate_variants returns them
    # ordered). Break early on a strong hit — same accept thresholds as before.
    for v in variants:
        query = v.value
        rows = search_by_owner(session, query)

        # Score each row against the VARIANT VALUE, not the original target.
        # A maiden/prior-titled property is owned under a different surname
        # (Jackson -> "GREATHOUSE DOROTHY"), so score_match(original, row)
        # is 0 (surnames differ) and the maiden case would never resolve.
        # The variant carries the correct surname, so scoring against it is
        # what realizes the maiden/prior-name payoff. The accept floor and
        # the 0.85 strong-hit break are unchanged.

        # Multi-parcel disambiguation guard (NAME-02): when a single variant
        # returns 2+ rows that each clear the accept floor, the same name maps
        # to multiple people/parcels. Run the corroboration guard rather than
        # picking arbitrarily; if it can't confidently choose, leave the lookup
        # unresolved (manual queue) — never auto-attach a wrong-person parcel.
        scoring_rows = [
            row for row in rows
            if score_match(query, row.owner) >= _MIN_MATCH_SCORE
        ]
        if len(scoring_rows) >= 2:
            candidates = [
                CandidatePerson(
                    name=row.owner,
                    addresses=[getattr(row, "address", "") or ""],
                )
                for row in scoring_rows
            ]
            known = [a for a in (notice.address.strip(),) if a]
            picked = disambiguate(
                query, candidates,
                known_addresses=known or None, min_score=0.6,
            )
            if picked is None:
                logger.info(
                    "  [PVA]   %d same-name parcels for %r; disambiguate "
                    "declined -> leaving unresolved (manual queue)",
                    len(scoring_rows), query,
                )
                continue
            for row in scoring_rows:
                if row.owner == picked.person.name:
                    best = (picked.score, row, query)
                    break
            if best and best[0] >= 0.85:
                break
            continue

        for row in rows:
            score = score_match(query, row.owner)
            if score >= _MIN_MATCH_SCORE and (not best or score > best[0]):
                best = (score, row, query)
        if best and best[0] >= 0.85:
            break

    if not best:
        # Name-search miss. Fallback chain (most-reliable first):
        #   (1) deed_discovered_parcel_id — exact PVA parcel-ID match. The
        #       12-char Jefferson PIDN is OCR'd in Phase 2b from the active
        #       mortgage's "Parcel/Map ID" field. Bulletproof when present.
        #   (2) deed_discovered_address  — street-search using the OCR'd
        #       address, filtered by exact house-number match. Lower
        #       confidence (depends on OCR quality), but catches cases
        #       where parcel ID OCR fails but street OCR succeeds.
        parcel_hint = (notice.deed_discovered_parcel_id or "").strip()
        if parcel_hint:
            logger.info(
                "  [PVA]   name-search miss; trying parcel-id fallback %r",
                parcel_hint,
            )
            rows = search_by_parcel(session, parcel_hint)
            if rows:
                row = rows[0]
                logger.info(
                    "  [PVA]   parcel-id match: %s (owner=%r, lrsn=%s)",
                    row.address, row.owner, row.lrsn,
                )
                detail = get_detail(session, row.lrsn)
                if holder_relationship == "trust":
                    initial_status = "trust"
                elif holder_relationship == "heir_recent":
                    initial_status = "heir_recent"
                else:
                    initial_status = "direct"
                _apply_to_notice(notice, row, detail, owner_status=initial_status)
                return
            logger.info("  [PVA]   parcel-id %r returned no rows", parcel_hint)

        addr_hint = (notice.deed_discovered_address or "").strip()
        if addr_hint:
            logger.info(
                "  [PVA]   name-search miss; trying address fallback %r",
                addr_hint,
            )
            rows = search_by_address(session, addr_hint)
            # Require house-number match (the OCR'd address has one; PVA
            # rows that are vacant land or the street's "header" entry
            # come back without a number — we want to skip those). PVA
            # stores suffixes abbreviated, so normalize both sides too.
            addr_norm = _normalize_street_address(addr_hint)
            target_house_num = _house_number(addr_norm)
            matched_row = None
            for row in rows:
                row_addr = _normalize_street_address(row.address)
                if not row_addr:
                    continue
                if target_house_num:
                    row_house_num = _house_number(row_addr)
                    if row_house_num != target_house_num:
                        continue
                # House numbers match (or both empty) — confirm street
                # name/suffix match too via substring after stripping the
                # house number from each.
                tgt_street = _HOUSE_NUM_RE.sub("", addr_norm).strip()
                row_street = _HOUSE_NUM_RE.sub("", row_addr).strip()
                if tgt_street and row_street and (
                    tgt_street == row_street
                    or tgt_street in row_street
                    or row_street in tgt_street
                ):
                    matched_row = row
                    break
            if matched_row is not None:
                row = matched_row
                logger.info(
                    "  [PVA]   address match: %s (owner=%r, lrsn=%s)",
                    row.address, row.owner, row.lrsn,
                )
                detail = get_detail(session, row.lrsn)
                # Owner status comes from the deed-chain hint when we had
                # one, otherwise default direct (decedent's mortgage was
                # found at this address — most likely they own).
                if holder_relationship == "trust":
                    initial_status = "trust"
                elif holder_relationship == "heir_recent":
                    initial_status = "heir_recent"
                else:
                    initial_status = "direct"
                _apply_to_notice(notice, row, detail, owner_status=initial_status)
                return
            logger.info(
                "  [PVA]   address fallback returned %d row(s) but none matched %r",
                len(rows), addr_hint,
            )
        logger.info("  [PVA]   no match for %r", primary_target)
        return

    score, row, variation_used = best
    logger.info(
        "  [PVA]   match: %s (owner=%r, score=%.2f, lrsn=%s, variation=%r)",
        row.address, row.owner, score, row.lrsn, variation_used,
    )
    detail = get_detail(session, row.lrsn)

    # Map deed-chain relationship into the property_owner_status value.
    # _apply_to_notice will upgrade to "estate" if the matched owner
    # string itself begins with "ESTATE OF" — that signal trumps our hint.
    if holder_relationship == "trust":
        initial_status = "trust"
    elif holder_relationship == "heir_recent":
        initial_status = "heir_recent"
    elif holder_relationship == "self":
        initial_status = "direct"
    elif variation_used.upper().startswith("ESTATE OF "):
        initial_status = "estate"
    else:
        initial_status = "direct"
    _apply_to_notice(notice, row, detail, owner_status=initial_status)


def heir_property_lookup(notices: list["NoticeData"]) -> None:
    """Second-pass PVA lookup for probate records where the decedent isn't
    the current owner, but a deed transferred property to an heir within
    the last 24 months.

    Phase 2b's deed scraper detects the transfer and populates
    ``heir_transferred_to`` and ``heir_transfer_date``. This function
    runs AFTER 2b and BEFORE 2d, searching PVA by the heir's name to
    pick up the property that's now in their name. Sets
    ``property_owner_status="heir_recent"`` on a match.

    Only runs when Phase 2a didn't already find a property (empty
    ``estimated_value``). Idempotent — won't overwrite existing matches.
    """
    candidates = [
        n for n in notices
        if n.notice_type == "probate"
        and n.county.lower() == "jefferson"
        and n.heir_transferred_to.strip()
        and not n.estimated_value.strip()  # no PVA match from Phase 2a
    ]
    if not candidates:
        return

    logger.info(
        "  [PVA] Heir lookup for %d record(s) (transfers within 24mo)",
        len(candidates),
    )
    session = _make_session()
    if not _login(session):
        logger.warning("  [PVA] heir lookup: auth failed — skipping all")
        return

    try:
        for notice in candidates:
            _heir_lookup_one(session, notice)
    finally:
        _logout(session)


def _heir_lookup_one(session: requests.Session, notice: "NoticeData") -> None:
    """PVA lookup using the heir's name from a recent transfer deed."""
    heir = notice.heir_transferred_to.strip()
    if not heir:
        return

    # JCD stores grantees in "LAST FIRST" already. generate_variants handles
    # both comma and natural-order and widens to maiden/prior surname forms
    # (NAME-01) so an heir who took title under a maiden/prior name is found.
    variants = generate_variants(heir)
    if not variants:
        return

    logger.info(
        "  [PVA] Heir %r (transfer %s) -> %d variants (sources: %s)",
        heir, notice.heir_transfer_date, len(variants),
        sorted({v.source for v in variants}),
    )

    best: tuple[float, PvaRow] | None = None
    for v in variants:
        query = v.value
        # Skip the "ESTATE OF" variations here — we're looking for the
        # heir as a living owner, not another estate.
        if query.upper().startswith("ESTATE OF "):
            continue
        rows = search_by_owner(session, query)
        for row in rows:
            # Score against the variant value (carries the correct surname
            # for maiden/prior forms), not the original heir string.
            score = score_match(query, row.owner)
            if score >= _MIN_MATCH_SCORE and (not best or score > best[0]):
                best = (score, row)
        if best and best[0] >= 0.85:
            break

    if not best:
        logger.info("  [PVA]   no heir match for %r", heir)
        return

    score, row = best
    logger.info(
        "  [PVA]   heir match: %s (owner=%r, score=%.2f, lrsn=%s)",
        row.address, row.owner, score, row.lrsn,
    )
    detail = get_detail(session, row.lrsn)
    _apply_to_notice(notice, row, detail, owner_status="heir_recent")


def lookup_parcel_addresses(notices: list["NoticeData"]) -> None:
    """For KY records with a parcel_id but no address, fetch PVA mailing address.

    Counterpart to tax_enricher.lookup_parcel_addresses (Knox). Runs one login
    per call, reuses session across all records.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "jefferson" and n.parcel_id.strip()
    ]
    if not candidates:
        return

    logger.info("  [PVA] Parcel lookup for %d parcel(s)", len(candidates))
    session = _make_session()
    if not _login(session):
        logger.warning("  [PVA] Could not authenticate; skipping all %d parcels", len(candidates))
        return

    try:
        for notice in candidates:
            rows = search_by_parcel(session, notice.parcel_id)
            if not rows:
                logger.info("  [PVA]   parcel %s: no match", notice.parcel_id)
                continue
            row = rows[0]
            detail = get_detail(session, row.lrsn)
            _apply_to_notice(notice, row, detail)
            logger.info("  [PVA]   parcel %s -> %s", notice.parcel_id, row.address)
    finally:
        _logout(session)
