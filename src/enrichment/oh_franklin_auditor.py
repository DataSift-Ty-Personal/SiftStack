"""Franklin County, Ohio Auditor property lookup client.

Used by the SiftStack probate pipeline: Franklin County probate court filings
include the decedent's name and personal representative, but not the property
address. This module searches the Franklin County Auditor's public Tyler
Technologies CAMA portal by owner name and returns candidate parcels with
full property address, city, zip, and (when available) appraised value.

Public surface:
    async def search_by_owner_name(name, last_name=None, first_name=None) -> list[dict]

Endpoint strategy
-----------------
The county exposes a "mobile API" at audr-api.franklincountyohio.gov, but
during build 1.0.30 development the ``ByOwner`` endpoint consistently
returned ``TotalCount: 0`` for every name (county-wide system maintenance
notice was active). We therefore use the public web search at
``property.franklincountyauditor.com``, which has a GET shortcut:

    /_web/search/commonsearch.aspx?mode=owner&searchimmediate=1&param1=<NAME>

This avoids ASP.NET ViewState/EventValidation POSTs entirely. The result
page is a Tyler CAMA HTML table (``table#searchResults``) with one row per
parcel, columns: Parcel ID, Address, Owner 1, Owner 2.

For each candidate match (Jaccard >= MATCH_THRESHOLD on tokenized names),
we fetch the parcel detail page directly via the auditor's permalink
redirector, which routes to a Datalet keyed by parcel pin (no
session-state dependency):

    https://audr-apps.franklincountyohio.gov/redir/Link/Parcel/<11-digit-pin>

The 11-digit pin is the parcel ID with dashes stripped
("010-071334-00" -> "01007133400").

Name matching
-------------
Tokenize on whitespace + punctuation, lowercase, drop suffixes
(JR/SR/II/III/IV/TRUSTEE/ETAL/ET AL/&). Compute Jaccard token overlap
between query name variants and each candidate's "Owner 1 + Owner 2" text.
Accept matches with score >= 0.4. Try multiple query variants:
    - original ("PATRICIA CRIDGE")
    - LAST FIRST ("CRIDGE PATRICIA")
    - LAST, FIRST ("CRIDGE, PATRICIA")
    - first + last only (drop middle if any)
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    # Allow `python -m enrichment.oh_franklin_auditor` from src/
    from config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX  # type: ignore
except Exception:  # pragma: no cover - fallback for standalone execution
    REQUEST_DELAY_MIN = 2.0
    REQUEST_DELAY_MAX = 3.0


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://property.franklincountyauditor.com"
SEARCH_PATH = "/_web/search/commonsearch.aspx"
PERMALINK_BASE = "https://audr-apps.franklincountyohio.gov/redir/Link/Parcel/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MATCH_THRESHOLD = 0.4
MAX_RESULTS = 10
HTTP_RETRIES = 3
HTTP_TIMEOUT = 30  # seconds

# Tokens dropped before Jaccard scoring — title/suffix noise that should not
# affect identity matching.
NOISE_TOKENS = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "trustee", "trustees", "tr", "trust",
    "etal", "et", "al",
    "and", "&",
    "estate", "est",
    "deceased", "dec", "dcd",
    "the", "of",
    "co", "ttee",
}

# Non-token characters used to split names (commas, dots, etc.)
_TOKEN_SPLIT = re.compile(r"[\s,.\-/]+")


# ---------------------------------------------------------------------------
# Name tokenization & Jaccard scoring
# ---------------------------------------------------------------------------


def _tokenize(name: str) -> set[str]:
    """Lowercase + strip punctuation + drop noise tokens. Returns a set."""
    if not name:
        return set()
    pieces = _TOKEN_SPLIT.split(name.strip().lower())
    return {p for p in pieces if p and p not in NOISE_TOKENS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


def _score_name(query_variants: list[str], candidate_owner: str) -> float:
    """Return the best Jaccard score across all query variants vs candidate."""
    cand = _tokenize(candidate_owner)
    if not cand:
        return 0.0
    best = 0.0
    for q in query_variants:
        s = _jaccard(_tokenize(q), cand)
        if s > best:
            best = s
    return best


def _build_name_variants(
    name: str,
    last_name: Optional[str],
    first_name: Optional[str],
) -> list[str]:
    """Build the list of name strings to try as Auditor search queries.

    Tyler CAMA "owner" search expects ``LASTNAME FIRSTNAME`` style. We try
    several variants so a "FIRST LAST" input still hits.
    """
    variants: list[str] = []
    seen: set[str] = set()

    def _add(v: Optional[str]) -> None:
        if not v:
            return
        v = re.sub(r"\s+", " ", v.strip())
        if not v:
            return
        key = v.upper()
        if key in seen:
            return
        seen.add(key)
        variants.append(v)

    # Use explicit first/last when given.
    if last_name and first_name:
        _add(f"{last_name} {first_name}")
        _add(f"{last_name}, {first_name}")
        _add(f"{first_name} {last_name}")

    if not name:
        return variants

    # Original (caller-supplied) form first — Auditor accepts arbitrary
    # token order for its search index.
    _add(name)

    # Tokenize the original to derive structural variants. Strip noise
    # tokens (e.g. "JR", "TRUSTEE") so we only permute real name pieces.
    raw = [t for t in _TOKEN_SPLIT.split(name.strip()) if t]
    clean = [t for t in raw if t.lower() not in NOISE_TOKENS]
    if len(clean) >= 2:
        # Assume input is "FIRST [MIDDLE...] LAST" -> derive "LAST FIRST"
        first = clean[0]
        last = clean[-1]
        _add(f"{last} {first}")
        _add(f"{last}, {first}")
        # Drop the middle name(s) and try plain "FIRST LAST"
        if len(clean) > 2:
            _add(f"{first} {last}")
        # Also try "LAST FIRST MIDDLE" and "LAST, FIRST MIDDLE"
        if len(clean) > 2:
            middle = " ".join(clean[1:-1])
            _add(f"{last} {first} {middle}")

    return variants


# ---------------------------------------------------------------------------
# HTTP layer (synchronous requests.Session, wrapped in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _polite_sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _http_get(session: requests.Session, url: str, params: Optional[dict] = None) -> Optional[str]:
    """GET with retries + exponential backoff. Returns text or None on failure."""
    delay = 2.0
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            logger.warning("Franklin Auditor GET %s failed (attempt %d/%d): %s",
                           url, attempt, HTTP_RETRIES, exc)
            if attempt == HTTP_RETRIES:
                return None
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            # Auditor pages are UTF-8; some Tyler CAMA installs serve cp1252.
            # Honor what the server declares, but fall back gracefully.
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text

        if 500 <= resp.status_code < 600 and attempt < HTTP_RETRIES:
            logger.warning("Franklin Auditor GET %s returned %d (attempt %d/%d)",
                           url, resp.status_code, attempt, HTTP_RETRIES)
            time.sleep(delay)
            delay *= 2
            continue

        logger.warning("Franklin Auditor GET %s returned %d", url, resp.status_code)
        return None
    return None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _parcel_id_to_pin(parcel_id: str) -> str:
    """Convert a Franklin parcel ID like '010-071334-00' to its 11-digit pin '01007133400'."""
    return re.sub(r"\D+", "", parcel_id or "")


def _parse_search_results(html: str) -> list[dict]:
    """Parse a Tyler CAMA owner-search results page.

    Returns a list of dicts with keys:
        parcel_id, address, owner1, owner2, owner_combined
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="searchResults")
    if not table:
        return []

    out: list[dict] = []
    for row in table.find_all("tr"):
        # Skip header / separator rows.
        if row.find("th"):
            continue
        if "SearchResults" not in (row.get("class") or []) and not row.get("onclick"):
            continue

        cells = row.find_all("td")
        # Layout (from observed HTML):
        #   td0 = checkbox, td1 = (hidden parid input), td2 = Parcel ID,
        #   td3 = Address, td4 = Owner1, td5 = Owner2
        if len(cells) < 5:
            continue

        def _txt(cell) -> str:
            return cell.get_text(strip=True)

        parcel_id = _txt(cells[2])
        address = _txt(cells[3])
        owner1 = _txt(cells[4])
        owner2 = _txt(cells[5]) if len(cells) > 5 else ""

        if not parcel_id or not address:
            continue

        owner_combined = (owner1 + " " + owner2).strip()
        out.append(
            {
                "parcel_id": parcel_id,
                "address": address,
                "owner1": owner1,
                "owner2": owner2,
                "owner_combined": owner_combined,
            }
        )
    return out


_VALUE_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")


def _parse_datalet(html: str) -> dict:
    """Pull city, zip, and (best effort) appraised value from a Datalet page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"city": "", "zip": "", "assessed_value": ""}

    # City + Zip live in the "Tax Status" / "Owner Mailing" datalet blocks
    # as DataletSideHeading -> DataletData rows.
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip(":").lower()
        value = cells[1].get_text(strip=True)
        if not label or not value:
            continue
        if label == "city/village" and not out["city"]:
            # Pass the auditor's city/village label through verbatim.
            # Tyler stores values like "COLUMBUS CITY", "GROVE CITY",
            # "WHITEHALL", "WORTHINGTON CITY". The trailing " CITY" /
            # " VILLAGE" is part of the auditor's tax-district label and
            # NOT reliably strippable — "Grove City" is a real
            # municipality, so a naive ".rstrip(' CITY')" would mangle
            # it. Downstream Smarty address standardization in the
            # SiftStack enrichment pipeline normalizes municipality
            # names, so we leave the raw value here.
            out["city"] = value.strip()
        elif label == "zip code" and not out["zip"]:
            m = re.search(r"\d{5}(?:-\d{4})?", value)
            if m:
                out["zip"] = m.group(0)

    # Appraised value lives in a "datalet_div_*" block whose first table
    # is a DataletTitleColor cell containing the year-prefixed label
    # "<YYYY> Auditor's Appraised Value". The id attribute has an embedded
    # apostrophe that breaks BS4 attribute parsing, so we locate the block
    # by its title cell instead and read the data table directly underneath.
    for div in soup.find_all("div"):
        did = div.get("id") or ""
        if not did.startswith("datalet_div"):
            continue
        title_cell = div.find("td", class_="DataletTitleColor")
        if not title_cell:
            continue
        if "Appraised Value" not in title_cell.get_text():
            continue
        # The data table is the second table inside this div; we just
        # iterate every table inside the block and pick the first row
        # whose first cell is "Total".
        for tbl in div.find_all("table"):
            for row in tbl.find_all("tr"):
                tds = [td.get_text(strip=True) for td in row.find_all("td")]
                if not tds:
                    continue
                if tds[0].lower() == "total":
                    # Prefer the rightmost numeric column ("Total" column).
                    for cell in reversed(tds[1:]):
                        if _VALUE_RE.match(cell):
                            out["assessed_value"] = cell
                            break
                    break
            if out["assessed_value"]:
                break
        if out["assessed_value"]:
            break

    return out


# ---------------------------------------------------------------------------
# Synchronous core (wrapped by the async public API)
# ---------------------------------------------------------------------------


def _search_one_variant(session: requests.Session, query: str) -> list[dict]:
    """Run a single owner search query against the Auditor portal."""
    text = _http_get(
        session,
        BASE_URL + SEARCH_PATH,
        params={
            "mode": "owner",
            "searchimmediate": "1",
            "param1": query,
        },
    )
    if not text:
        return []
    return _parse_search_results(text)


def _enrich_match(session: requests.Session, match: dict) -> None:
    """Fetch the parcel detail page via the auditor's permalink and merge
    city/zip/assessed_value into ``match``.

    Mutates ``match`` in place. Silently leaves fields blank on failure.
    The permalink redirects through audr-apps.franklincountyohio.gov to a
    Datalet URL keyed by parcel pin (no session-state dependency).
    """
    pin = _parcel_id_to_pin(match.get("parcel_id", ""))
    if not pin:
        return

    _polite_sleep()
    text = _http_get(session, PERMALINK_BASE + pin)
    if not text:
        return
    parsed = _parse_datalet(text)
    match.update(parsed)


# ---------------------------------------------------------------------------
# Parcel-by-ID lookup (used by RealAuction foreclosure scraper to fill in
# defendant names when RealAuction itself only exposes address + parcel)
# ---------------------------------------------------------------------------


def _parse_owner_block(html: str) -> dict:
    """Extract owner name + mailing address from the OWNERS3 datalet block.

    The Franklin auditor renders an "Owner" datalet block (div with
    name="OWNERS3") containing rows like:

        Owner                    -> <a>BLANTON KATHERINE</a>
        Owner Mailing /          -> 6623 LOCKBOURNE RD
        Contact Address          -> LOCKBOURNE OH 43137
        Site (Property) Address  -> 6623 LOCKBOURNE RD

    Returns a dict with keys: ``owner_name``, ``mail_street``, ``mail_city``,
    ``mail_state``, ``mail_zip``, ``site_address``. Blank strings on miss.
    """
    out = {
        "owner_name": "",
        "mail_street": "",
        "mail_city": "",
        "mail_state": "",
        "mail_zip": "",
        "site_address": "",
    }
    soup = BeautifulSoup(html, "html.parser")
    block = soup.find("div", attrs={"name": "OWNERS3"})
    if not block:
        return out

    pending_label: Optional[str] = None
    for row in block.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip(":").strip()
        value = cells[1].get_text(" ", strip=True)
        if not label or label in ("\xa0", ""):
            label = pending_label or ""
        # "Owner Mailing /" + "Contact Address" come on adjacent rows
        if label.lower().startswith("owner") and "mailing" not in label.lower() and not out["owner_name"]:
            if value and value != "\xa0":
                out["owner_name"] = value
        elif "owner mailing" in label.lower() and not out["mail_street"]:
            out["mail_street"] = value
            pending_label = "contact address"
        elif label.lower() == "contact address" and not out["mail_city"]:
            # "LOCKBOURNE OH 43137" — split into city, state, zip
            m = re.match(r"^(.*?)\s+([A-Z]{2})\s+(\d{5})(?:-(\d{4}))?$", value.strip())
            if m:
                out["mail_city"] = m.group(1).strip().title()
                out["mail_state"] = m.group(2)
                out["mail_zip"] = m.group(3) + (f"-{m.group(4)}" if m.group(4) else "")
        elif "site" in label.lower() and "property" in label.lower() and not out["site_address"]:
            out["site_address"] = value
        pending_label = label
    return out


def _lookup_parcel_sync(parcel_id: str) -> Optional[dict]:
    """Synchronous core for fetch_parcel_owner."""
    pin = _parcel_id_to_pin(parcel_id)
    if not pin:
        return None
    session = _new_session()
    text = _http_get(session, PERMALINK_BASE + pin)
    if not text:
        return None
    owner = _parse_owner_block(text)
    detail = _parse_datalet(text)
    return {**owner, **{k: v for k, v in detail.items() if v}}


async def fetch_parcel_owner(parcel_id: str) -> Optional[dict]:
    """Look up Franklin parcel by ID and return owner + mailing address.

    Used by the RealAuction foreclosure scraper to fill in defendant names
    that RealAuction's public calendar doesn't expose. Returns ``None`` if
    the parcel ID is empty or the auditor lookup fails. Otherwise returns:

        {
            "owner_name": "BLANTON KATHERINE",
            "mail_street": "6623 LOCKBOURNE RD",
            "mail_city": "Lockbourne",
            "mail_state": "OH",
            "mail_zip": "43137",
            "site_address": "6623 LOCKBOURNE RD",
            "city": "...",          # property city from datalet
            "zip": "...",           # property zip from datalet
            "assessed_value": "...",
        }
    """
    if not parcel_id:
        return None
    return await asyncio.to_thread(_lookup_parcel_sync, parcel_id)


def _search_sync(name: str, last_name: Optional[str], first_name: Optional[str]) -> list[dict]:
    variants = _build_name_variants(name, last_name, first_name)
    if not variants:
        return []

    session = _new_session()

    # Aggregate matches across all query variants, deduped by parcel_id.
    # Track the single best score per parcel so we don't double-count when
    # two variants both happen to return the same parcel.
    by_parcel: dict[str, dict] = {}

    for i, q in enumerate(variants):
        if i > 0:
            _polite_sleep()
        try:
            rows = _search_one_variant(session, q)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Franklin Auditor search variant %r failed: %s", q, exc)
            continue

        for row in rows:
            score = _score_name(variants, row.get("owner_combined", ""))
            if score < MATCH_THRESHOLD:
                continue
            pid = row["parcel_id"]
            existing = by_parcel.get(pid)
            if existing is None or score > existing.get("match_score", 0.0):
                row = dict(row)
                row["match_score"] = round(score, 4)
                by_parcel[pid] = row

        # Short-circuit: if we already have plenty of strong matches from
        # the first (most-specific) variant, don't keep hammering the site.
        if len(by_parcel) >= MAX_RESULTS and any(
            r["match_score"] >= 0.8 for r in by_parcel.values()
        ):
            break

    if not by_parcel:
        return []

    matches = sorted(
        by_parcel.values(), key=lambda r: r["match_score"], reverse=True
    )[:MAX_RESULTS]

    # Enrich each kept match with city/zip/value from its detail page.
    for m in matches:
        try:
            _enrich_match(session, m)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Franklin Auditor datalet enrich failed for %s: %s",
                           m.get("parcel_id"), exc)

    # Shape the output exactly per the spec.
    out: list[dict] = []
    for m in matches:
        out.append(
            {
                "parcel_id": m.get("parcel_id", ""),
                "owner_name": m.get("owner_combined", "") or m.get("owner1", ""),
                "address": m.get("address", ""),
                "city": m.get("city", ""),
                "state": "OH",
                "zip": m.get("zip", ""),
                "assessed_value": m.get("assessed_value", ""),
                "match_score": m.get("match_score", 0.0),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def search_by_owner_name(
    name: str,
    last_name: Optional[str] = None,
    first_name: Optional[str] = None,
) -> list[dict]:
    """Search Franklin County Auditor for parcels owned by ``name``.

    Args:
        name: Free-form owner name (e.g. "PATRICIA CRIDGE" or "CRIDGE, PATRICIA").
            Optional if both ``first_name`` and ``last_name`` are provided.
        last_name: Optional explicit last name.
        first_name: Optional explicit first name.

    Returns:
        List of dicts (up to ~10), each with:
            parcel_id (str)
            owner_name (str — Auditor record's owner field, may include Owner2)
            address (str — street address only)
            city (str)
            state (str = "OH")
            zip (str)
            assessed_value (str — "" if not found on the summary page)
            match_score (float, 0.0..1.0 — Jaccard token overlap)
        Sorted by match_score descending. Empty list on no matches or on
        persistent transient failure (errors are logged, not raised).
    """
    return await asyncio.to_thread(_search_sync, name, last_name, first_name)


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Search Franklin County, OH Auditor for parcels by owner name."
    )
    parser.add_argument(
        "name", help="Owner name to search (e.g. 'PATRICIA CRIDGE')"
    )
    parser.add_argument("--last", default=None, help="Optional explicit last name")
    parser.add_argument("--first", default=None, help="Optional explicit first name")
    args = parser.parse_args()

    results = asyncio.run(
        search_by_owner_name(args.name, last_name=args.last, first_name=args.first)
    )
    print(json.dumps(results, indent=2))
    print(f"\n{len(results)} matches")
