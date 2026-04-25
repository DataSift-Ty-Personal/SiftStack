"""Greene County, Ohio Auditor property lookup client.

Used by the SiftStack probate pipeline: Greene County Probate Court filings
include the decedent's name and personal representative, but not the property
address. This module searches the Greene County Auditor's public parcel data
by owner name and returns candidate parcels with full property address, city,
zip, and (when available) assessed value.

Public surface:
    async def search_by_owner_name(name, last_name=None, first_name=None) -> list[dict]

Endpoint strategy
-----------------
The Auditor's primary HTML site at ``auditor.greenecountyohio.gov`` (ISSG
PTSTheme) sits behind an Azure Application Gateway WAF that issues a
JavaScript challenge on every request — completely impractical to defeat
with plain ``requests``. Fortunately the same parcel database is exposed
unauthenticated via the county's public ArcGIS REST service powering the
GIMS / "Public Access System (PAS)" map application:

    https://gis.greenecountyohio.gov/webgis2/rest/services/PAS/TaxParcels/MapServer/1

This is a fully queryable Esri Feature Layer (capabilities: Map,Query,Data),
no Azure WAF, no auth, no CAPTCHA, no rate-limit headers observed. We hit
the standard Esri ``/query`` operation with a SQL ``WHERE`` clause against
``Owner_Name`` and parse the JSON response directly — much cleaner than
scraping the WAF-protected HTML site.

Layer fields used (from layer metadata):
    Owner_Name, Property_Address, Property_City_St_Zip,
    Address_City, Address_State, Address_ZipCode,
    Parcel_Id, Parcel_Number, Assessed_Total, Tax_Year

``Property_City_St_Zip`` is occasionally blank for vacant land / utility
parcels; we fall back to the discrete Address_City / Address_State /
Address_ZipCode columns in that case.

Name matching
-------------
Tokenize on whitespace + punctuation, lowercase, drop suffixes
(JR/SR/II/III/IV/TRUSTEE/ETAL/ET AL/&). Compute Jaccard token overlap
between query name variants and each candidate's Owner_Name. Accept matches
with score >= MATCH_THRESHOLD (0.4). Try multiple query variants:
    - original ("JOHN SMITH")
    - LAST FIRST ("SMITH JOHN")          <- Greene's storage order
    - LAST FIRST MIDDLE ("SMITH JOHN A")
    - first + last only (drop middle)

Greene County (~170K population) is small; expect lower match counts than
Franklin or Montgomery.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Optional

import requests

try:
    # Allow `python -m enrichment.oh_greene_auditor` from src/
    from config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX  # type: ignore
except Exception:  # pragma: no cover - fallback for standalone execution
    REQUEST_DELAY_MIN = 2.0
    REQUEST_DELAY_MAX = 3.0


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Esri Feature Layer URL — TaxParcels layer 1 of the PAS service.
QUERY_URL = (
    "https://gis.greenecountyohio.gov/webgis2/rest/services/"
    "PAS/TaxParcels/MapServer/1/query"
)

# Fields we ask the server to return — keep this list small so the response
# stays under the default 1MB transfer limit even for big "SMITH" searches.
OUT_FIELDS = ",".join(
    [
        "Parcel_Id",
        "Parcel_Number",
        "Owner_Name",
        "Property_Address",
        "Property_City_St_Zip",
        "Address_City",
        "Address_State",
        "Address_ZipCode",
        "Assessed_Total",
        "Tax_Year",
    ]
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MATCH_THRESHOLD = 0.4
MAX_RESULTS = 10
# Cap each ArcGIS query at 50 features — plenty of headroom for ranking and
# avoids dragging back several megabytes for very common surnames.
SERVER_RECORD_CAP = 50
HTTP_RETRIES = 3
HTTP_TIMEOUT = 30  # seconds

# Tokens dropped before Jaccard scoring — title/suffix noise that should not
# affect identity matching. Aligned with the Franklin/Montgomery clients.
NOISE_TOKENS = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "trustee", "trustees", "tr", "trust",
    "etal", "et", "al",
    "and", "&",
    "estate", "est",
    "deceased", "dec", "dcd",
    "the", "of",
    "co", "ttee",
    "llc", "inc", "corp",
}

# Non-token characters used to split names (commas, dots, ampersands, etc.)
_TOKEN_SPLIT = re.compile(r"[\s,.\-/&]+")


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

    Greene's Owner_Name field stores ``LAST FIRST [MIDDLE]`` (e.g.
    "SMITH JOHN A"), with no comma. We try several variants so a "FIRST LAST"
    input still hits.
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
        _add(f"{first_name} {last_name}")

    if not name:
        return variants

    # Original (caller-supplied) form first.
    _add(name)

    # Tokenize the original to derive structural variants. Strip noise
    # tokens (e.g. "JR", "TRUSTEE") so we only permute real name pieces.
    raw = [t for t in _TOKEN_SPLIT.split(name.strip()) if t]
    clean = [t for t in raw if t.lower() not in NOISE_TOKENS]
    if len(clean) >= 2:
        first = clean[0]
        last = clean[-1]
        # Greene stores LAST FIRST — that's the highest-yield variant.
        _add(f"{last} {first}")
        # Drop middle and try plain "FIRST LAST".
        if len(clean) > 2:
            _add(f"{first} {last}")
            # Also try "LAST FIRST MIDDLE" exactly.
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
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://gis.greenecountyohio.gov/GIMS/",
        }
    )
    return s


def _polite_sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _http_get_json(
    session: requests.Session,
    url: str,
    params: dict,
) -> Optional[dict]:
    """GET a JSON endpoint with retries + exponential backoff.

    ArcGIS REST endpoints occasionally return HTTP 200 with an embedded
    ``error`` envelope (e.g. ``{"error": {"code": 400, ...}}``) — we treat
    those as failures too.
    """
    delay = 2.0
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning(
                "Greene Auditor GET %s failed (attempt %d/%d): %s",
                url, attempt, HTTP_RETRIES, exc,
            )
            if attempt == HTTP_RETRIES:
                return None
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            # The ArcGIS server serves UTF-8 JSON; cp1252 fallback is here
            # only because some Greene-stack endpoints are cp1252 elsewhere.
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            try:
                data = resp.json()
            except ValueError:
                logger.warning(
                    "Greene Auditor GET %s returned non-JSON body (len=%d)",
                    url, len(resp.text),
                )
                return None
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                logger.warning(
                    "Greene Auditor query error: %s",
                    err.get("message") or err,
                )
                # Don't retry semantic errors (bad WHERE clause, etc.) — they
                # won't get better.
                return None
            return data

        if 500 <= resp.status_code < 600 and attempt < HTTP_RETRIES:
            logger.warning(
                "Greene Auditor GET %s returned %d (attempt %d/%d)",
                url, resp.status_code, attempt, HTTP_RETRIES,
            )
            time.sleep(delay)
            delay *= 2
            continue

        logger.warning("Greene Auditor GET %s returned %d", url, resp.status_code)
        return None
    return None


# ---------------------------------------------------------------------------
# ArcGIS query helpers
# ---------------------------------------------------------------------------


# Single-quote escape for SQL-style WHERE clauses — ArcGIS REST follows the
# same convention as ANSI SQL: double the quote.
def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _build_where(query: str) -> str:
    """Build a case-insensitive ``Owner_Name LIKE %...%`` WHERE clause.

    For multi-token queries we AND each token so the server pre-filters to
    rows that actually contain all requested name pieces. This keeps the
    result set small even for very common surnames.
    """
    tokens = [t for t in _TOKEN_SPLIT.split(query.strip()) if t]
    # Drop noise tokens before sending to the server.
    tokens = [t for t in tokens if t.lower() not in NOISE_TOKENS]
    if not tokens:
        return "1=0"  # explicit no-op
    clauses = []
    for tok in tokens:
        esc = _sql_escape(tok.upper())
        clauses.append(f"UPPER(Owner_Name) LIKE '%{esc}%'")
    return " AND ".join(clauses)


def _query_owner(session: requests.Session, query: str) -> list[dict]:
    """Run a single owner search query against the ArcGIS Feature Layer."""
    where = _build_where(query)
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": str(SERVER_RECORD_CAP),
        "orderByFields": "Owner_Name ASC",
    }
    data = _http_get_json(session, QUERY_URL, params)
    if not data:
        return []
    feats = data.get("features") or []
    out: list[dict] = []
    for feat in feats:
        attrs = feat.get("attributes") or {}
        owner = (attrs.get("Owner_Name") or "").strip()
        addr = (attrs.get("Property_Address") or "").strip()
        if not owner or not addr:
            continue
        out.append(attrs)
    return out


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


_CSZ_RE = re.compile(
    r"^\s*(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def _resolve_city_state_zip(attrs: dict) -> tuple[str, str, str]:
    """Extract (city, state, zip) preferring the combined column when present."""
    csz = (attrs.get("Property_City_St_Zip") or "").strip()
    if csz:
        m = _CSZ_RE.match(csz)
        if m:
            return m.group("city").strip(), m.group("state"), m.group("zip")

    city = (attrs.get("Address_City") or "").strip()
    state = (attrs.get("Address_State") or "").strip() or "OH"
    zipc = (attrs.get("Address_ZipCode") or "").strip()
    return city, state, zipc


def _format_value(val) -> str:
    """Render Assessed_Total as a plain integer string, or '' if missing."""
    if val is None:
        return ""
    try:
        # ArcGIS returns floats; the field is dollars so strip the .0.
        f = float(val)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return str(int(round(f)))


# ---------------------------------------------------------------------------
# Synchronous core (wrapped by the async public API)
# ---------------------------------------------------------------------------


def _search_sync(
    name: str,
    last_name: Optional[str],
    first_name: Optional[str],
) -> list[dict]:
    variants = _build_name_variants(name, last_name, first_name)
    if not variants:
        return []

    session = _new_session()

    # Aggregate matches across all query variants, deduped by parcel id, with
    # the best-scoring variant winning the tie.
    by_parcel: dict[str, dict] = {}

    for i, q in enumerate(variants):
        if i > 0:
            _polite_sleep()
        try:
            rows = _query_owner(session, q)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Greene Auditor query variant %r failed: %s", q, exc)
            continue

        for attrs in rows:
            score = _score_name(variants, attrs.get("Owner_Name", ""))
            if score < MATCH_THRESHOLD:
                continue
            pid = (
                attrs.get("Parcel_Id")
                or attrs.get("Parcel_Number")
                or ""
            ).strip()
            if not pid:
                continue
            existing = by_parcel.get(pid)
            if existing is None or score > existing["_score"]:
                by_parcel[pid] = {"_score": score, "_attrs": attrs}

        # Short-circuit: if the first (most-specific) variant already gave us
        # plenty of strong matches, don't keep hammering the service.
        if len(by_parcel) >= MAX_RESULTS and any(
            r["_score"] >= 0.8 for r in by_parcel.values()
        ):
            break

    if not by_parcel:
        return []

    ranked = sorted(
        by_parcel.values(), key=lambda r: r["_score"], reverse=True
    )[:MAX_RESULTS]

    out: list[dict] = []
    for r in ranked:
        attrs = r["_attrs"]
        city, state, zipc = _resolve_city_state_zip(attrs)
        parcel_id = (
            (attrs.get("Parcel_Number") or attrs.get("Parcel_Id") or "").strip()
        )
        out.append(
            {
                "parcel_id": parcel_id,
                "owner_name": (attrs.get("Owner_Name") or "").strip(),
                "address": (attrs.get("Property_Address") or "").strip(),
                "city": city,
                "state": state or "OH",
                "zip": zipc,
                "assessed_value": _format_value(attrs.get("Assessed_Total")),
                "match_score": round(r["_score"], 4),
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
    """Search Greene County, OH Auditor for parcels owned by ``name``.

    Args:
        name: Free-form owner name (e.g. "JOHN SMITH" or "SMITH, JOHN").
            Optional if both ``first_name`` and ``last_name`` are provided.
        last_name: Optional explicit last name.
        first_name: Optional explicit first name.

    Returns:
        List of dicts (up to ~10), each with:
            parcel_id (str — formatted parcel number, e.g. "L35-0002-0002-0-0004-00")
            owner_name (str — Auditor record's Owner_Name field)
            address (str — street address only)
            city (str)
            state (str = "OH")
            zip (str)
            assessed_value (str — integer dollars, "" if 0/missing)
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
        description="Search Greene County, OH Auditor for parcels by owner name."
    )
    parser.add_argument(
        "name", help="Owner name to search (e.g. 'JOHN SMITH')"
    )
    parser.add_argument("--last", default=None, help="Optional explicit last name")
    parser.add_argument("--first", default=None, help="Optional explicit first name")
    args = parser.parse_args()

    results = asyncio.run(
        search_by_owner_name(args.name, last_name=args.last, first_name=args.first)
    )
    print(json.dumps(results, indent=2))
    print(f"\n{len(results)} matches")
