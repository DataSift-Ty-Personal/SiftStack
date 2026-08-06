"""Philadelphia, PA distress-notice scrapers — six independent portal sources.

Source map
----------
1  li_violations        L&I Code Violations        Carto SQL API (httpx GET, no auth)
2  bid4assets_mortgage  Sheriff Mortgage Sales      Scrapfly SDK (asp=True, render_js=True)
3  bid4assets_tax       Sheriff Tax Sales           Scrapfly SDK (same parser, different URL)
4  fjd_lis_pendens      Lis Pendens / Pre-FC        Playwright + Bright Data proxy + CapSolver v3
5  fjd_evictions        Evictions                   FJD Municipal Court CLAIMS (Playwright, public access)
6  inquirer_probate     Probate / Estate Notices    Playwright + OPA cross-ref

Run order for validation: 1 → 4 → 6 → 2 → then 3 and 5.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import capsolver
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PwTimeout,
    async_playwright,
)
from playwright_stealth import Stealth as _Stealth
from scrapfly import ScrapflyClient, ScrapeConfig

_stealth = _Stealth()
from config import (
    BID4ASSETS_HCAPTCHA_SITEKEY,
    CAPTCHA_API_KEY,
    OUTPUT_DIR,
    CAPSOLVER_API_KEY,
    PHILLY_BID4ASSETS_MORTGAGE_URL,
    PHILLY_BID4ASSETS_TAX_URL,
    PHILLY_CARTO_API_URL,
    PHILLY_CARTO_LOOKBACK_DAYS,
    PHILLY_CARTO_VIOLATIONS_TABLE,
    PHILLY_FJD_CIVIL_LOOKBACK_DAYS,
    PHILLY_FJD_CIVIL_URL,
    PHILLY_FJD_CLAIMS_LOOKBACK_DAYS,
    PHILLY_FJD_CLAIMS_URL,
    PHILLY_INQUIRER_ESTATE_URL,
    PHILLY_INQUIRER_LOOKBACK_DAYS,
    PHILLY_OPA_TABLE,
    PHILLY_PROXY_URL,
    PHILLY_SOURCES,
    SCRAPFLY_API_KEY,
    PhillySource,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# ── Shared helpers ─────────────────────────────────────────────────────


def _philly_notice(source: PhillySource, **kwargs) -> NoticeData:
    """Construct a NoticeData pre-filled with Philadelphia defaults."""
    n = NoticeData(
        county=source.county,
        state=source.state,
        notice_type=source.notice_type,
        city="Philadelphia",
    )
    for k, v in kwargs.items():
        if hasattr(n, k):
            setattr(n, k, v)
    return n


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _normalize_addr(s: str) -> str:
    """Uppercase + collapse whitespace for address comparison."""
    return re.sub(r"\s+", " ", (s or "").upper().strip())


async def _stealth_page(context: BrowserContext) -> Page:
    """Open a new page with stealth mode enabled (required for Bid4Assets)."""
    page = await context.new_page()
    await _stealth.apply_stealth_async(page)
    return page


async def _delay() -> None:
    import random
    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


# ── OPA enrichment utilities ───────────────────────────────────────────

_VACANT_LAND_KW = ("VACANT", "LAND", "EMPTY")

# mailing_city_state in OPA is "CITY ST" (e.g. "PHILADELPHIA PA")
# We need both city and state in separate OPA queries, so also pull mailing_city
# Confirmed OPA columns (live inspection 2026-04-26): parcel_number, market_value,
# category_code_description, year_built, owner_1, mailing_street, mailing_city_state,
# mailing_zip
_OPA_SELECT = (
    "parcel_number, market_value, category_code_description, year_built, "
    "owner_1, mailing_street, mailing_city_state, mailing_zip, "
    "location, zip_code"   # property address — used when notice.address is blank
)


def _is_residential(category: str) -> bool:
    """Return False if OPA category indicates vacant land/lot/empty parcel."""
    cat = (category or "").upper()
    return not any(kw in cat for kw in _VACANT_LAND_KW)


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _set_meta(notice: NoticeData, **kv) -> None:
    """Merge key-value pairs into heir_map_json (our structured metadata store)."""
    try:
        meta = json.loads(notice.heir_map_json) if notice.heir_map_json else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    meta.update(kv)
    notice.heir_map_json = json.dumps(meta)


def _get_meta(notice: NoticeData, key: str, default=None):
    try:
        return (json.loads(notice.heir_map_json) if notice.heir_map_json else {}).get(key, default)
    except (json.JSONDecodeError, TypeError):
        return default


async def _opa_fetch_batch(parcel_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch OPA property data keyed by parcel_number. Batches of 100."""
    results: dict[str, dict] = {}
    if not parcel_ids:
        return results

    async with httpx.AsyncClient(timeout=30.0) as client:
        for batch in _chunks(list(parcel_ids), 100):
            ids_str = "','".join(b.replace("'", "''") for b in batch)
            query = (
                f"SELECT {_OPA_SELECT} "
                f"FROM opa_properties_public "
                f"WHERE parcel_number IN ('{ids_str}')"
            )
            try:
                resp = await client.get(
                    PHILLY_CARTO_API_URL, params={"q": query, "format": "json"}
                )
                resp.raise_for_status()
                for row in resp.json().get("rows", []):
                    pid = str(row.get("parcel_number", ""))
                    if pid:
                        results[pid] = row
            except Exception:
                logger.warning("OPA batch fetch error for batch size %d", len(batch), exc_info=True)

    return results


def _apply_opa_row(notice: NoticeData, row: dict) -> None:
    """Write OPA columns into NoticeData fields."""
    notice.estimated_value = str(row.get("market_value") or "")
    notice.property_type = str(row.get("category_code_description") or "")
    notice.year_built = str(row.get("year_built") or "")
    notice.tax_owner_name = _title(str(row.get("owner_1") or ""))

    # Property address from OPA — backfills sources that don't carry a raw
    # property address (e.g. inquirer_probate only knows the executor address).
    # OPA.location is the situs address ("1433 N VOGDES ST"); zip_code is the
    # property ZIP. Only write when the notice has no address yet.
    if not notice.address:
        opa_loc = _title(str(row.get("location") or ""))
        if opa_loc:
            notice.address = opa_loc
            notice.city  = notice.city or "Philadelphia"
            notice.state = notice.state or "PA"
    if not notice.zip:
        opa_zip = str(row.get("zip_code") or "")[:5]
        if opa_zip:
            notice.zip = opa_zip

    # Mailing address from OPA
    mst = _title(str(row.get("mailing_street") or ""))
    if mst:
        notice.owner_street = mst
    city_state = str(row.get("mailing_city_state") or "")
    parts = city_state.rsplit(" ", 1)
    if len(parts) == 2:
        notice.owner_city = _title(parts[0])
        notice.owner_state = parts[1].upper()
    mzip = str(row.get("mailing_zip") or "")[:5]
    if mzip:
        notice.owner_zip = mzip


async def enrich_and_filter(
    notices: list[NoticeData],
    id_field: str = "parcel_id",
) -> tuple[list[NoticeData], int]:
    """OPA-enrich all notices and apply Filter 2 (drop VACANT/LAND/EMPTY).

    Returns (kept_records, dropped_count).
    Records without OPA match are tagged opa_match=false but KEPT.
    """
    if not notices:
        return [], 0

    # Collect all unique parcel IDs
    ids = list({getattr(n, id_field) for n in notices if getattr(n, id_field)})
    opa_map = await _opa_fetch_batch(ids)

    kept: list[NoticeData] = []
    dropped = 0

    for n in notices:
        pid = getattr(n, id_field, "")
        row = opa_map.get(pid)
        if row:
            _apply_opa_row(n, row)
            _set_meta(n, opa_match=True)
            if not _is_residential(n.property_type):
                dropped += 1
                continue
        else:
            _set_meta(n, opa_match=False)
        kept.append(n)

    logger.info("OPA enrichment: %d kept, %d dropped (vacant/land)", len(kept), dropped)
    return kept, dropped


# ── Bid4Assets inline JSON extraction ─────────────────────────────────


def _extract_b4a_json(html: str) -> list[dict]:
    """Extract the full auction list embedded as an inline JSON array.

    Bid4Assets embeds all records in a single JS array (confirmed 395 items
    for the Philadelphia mortgage foreclosure listing on 2026-04-26).
    Pattern: [...{"AuctionID":...}...]
    """
    start = html.find('[{"AuctionID"')
    if start == -1:
        return []

    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(html[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return []
    return []


# ── Name normalization for probate OPA matching ────────────────────────

_SUFFIX_RE = re.compile(r"\b(JR|SR|II|III|IV|V|ESQ|PHD|MD)\b\.?", re.I)
_PUNCT_RE = re.compile(r"[',.]")


def _normalize_name(raw: str) -> str:
    """Uppercase, strip suffixes and punctuation, remove single-letter words."""
    s = _SUFFIX_RE.sub(" ", raw.upper())
    s = _PUNCT_RE.sub(" ", s)
    parts = [p for p in s.split() if len(p) > 1]
    return " ".join(parts)


def _name_variants(name: str) -> list[str]:
    """Return normalized name + reversed-word variant for matching."""
    base = _normalize_name(name)
    parts = base.split()
    variants = [base]
    if len(parts) >= 2:
        variants.append(" ".join(reversed(parts)))
    return variants


def _best_name_score(query: str, opa_name: str) -> float:
    """Highest SequenceMatcher ratio across all name-ordering variants."""
    opa_norm = _normalize_name(opa_name)
    return max(
        SequenceMatcher(None, v, opa_norm).ratio()
        for v in _name_variants(query)
    )


# ── SOURCE 1 — L&I Code Violations (Carto SQL API) ────────────────────


_SOURCE_LI = next(s for s in PHILLY_SOURCES if s.source_id == "li_violations")

def _build_li_query(lookback_days: int) -> str:
    """Build the Carto SQL query for ALL recent L&I violations (no absentee filter).

    Change 3: absentee filter removed — pass all violations through, tag owner_status.
    Adds violation_count_for_parcel via window function (all-time count per OPA parcel).
    Primary date: casecreateddate (NOT violationdate per spec).

    Confirmed column names (live Carto schema 2026-04-26):
      violations: address, zip, casecreateddate, violationcodetitle, violationcode,
                  violationstatus, opa_owner, opa_account_num, geocode_x, geocode_y
      opa_properties_public: parcel_number, mailing_street, mailing_city_state, mailing_zip
    """
    return (
        f"SELECT "
        f"  v.cartodb_id, "
        f"  v.casecreateddate, "
        f"  v.address, "
        f"  v.zip, "
        f"  v.violationcodetitle AS violationdescription, "
        f"  v.violationcode AS violationcodesection, "
        f"  v.violationstatus, "
        f"  v.opa_owner AS ownername, "
        f"  v.opa_account_num AS parcelid_num, "
        f"  v.geocode_x, "
        f"  v.geocode_y, "
        f"  o.mailing_street AS owneraddress, "
        f"  o.mailing_city_state AS ownercitystate, "
        f"  o.mailing_zip AS ownerzip, "
        # All-time violation count for this parcel (window function)
        f"  COUNT(*) OVER (PARTITION BY v.opa_account_num) AS violation_count_for_parcel "
        f"FROM {PHILLY_CARTO_VIOLATIONS_TABLE} v "
        f"LEFT JOIN opa_properties_public o "
        f"  ON v.opa_account_num = o.parcel_number "
        f"WHERE v.casecreateddate >= NOW() - INTERVAL '{lookback_days} days' "
        f"ORDER BY v.casecreateddate DESC"
    )


async def scrape_li_violations(
    lookback_days: int = PHILLY_CARTO_LOOKBACK_DAYS,
) -> list[NoticeData]:
    """Fetch ALL L&I code violations via the Carto SQL API (no absentee filter).

    owner_status tag (OWNER_OCCUPIED / ABSENTEE / UNKNOWN) computed from
    mailing_street vs property address comparison.
    violation_count_for_parcel added via Carto window function.
    """
    query = _build_li_query(lookback_days)
    params = {"q": query, "format": "json"}

    logger.info("[li_violations] Querying Carto API (last %d days, no absentee filter)", lookback_days)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(PHILLY_CARTO_API_URL, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    rows: list[dict] = data.get("rows", [])
    logger.info("[li_violations] %d rows returned", len(rows))

    notices: list[NoticeData] = []
    for row in rows:
        raw_date = (row.get("casecreateddate") or "")[:10]

        # Compute owner_status
        prop_addr = _normalize_addr(row.get("address") or "")
        mail_addr = _normalize_addr(row.get("owneraddress") or "")
        if not mail_addr:
            owner_status = "UNKNOWN"
        elif prop_addr and mail_addr == prop_addr:
            owner_status = "OWNER_OCCUPIED"
        else:
            owner_status = "ABSENTEE"

        # Mailing address components
        city_state = row.get("ownercitystate") or ""
        cs_parts = city_state.rsplit(" ", 1)
        owner_city = _title(cs_parts[0]) if cs_parts else ""
        owner_state = cs_parts[1].upper() if len(cs_parts) > 1 else ""

        vcount = int(row.get("violation_count_for_parcel") or 0)

        n = _philly_notice(
            _SOURCE_LI,
            date_added=raw_date or _today(),
            address=_title(row.get("address") or ""),
            zip=(row.get("zip") or "")[:5],
            owner_name=_title(row.get("ownername") or ""),
            owner_street=_title(row.get("owneraddress") or ""),
            owner_city=owner_city,
            owner_state=owner_state,
            owner_zip=(row.get("ownerzip") or "")[:5],
            parcel_id=str(row.get("parcelid_num") or ""),
            latitude=str(row.get("geocode_y") or ""),
            longitude=str(row.get("geocode_x") or ""),
            raw_text=(
                f"Violation: {row.get('violationdescription','')} "
                f"[{row.get('violationcodesection','')}] "
                f"Status: {row.get('violationstatus','')} "
                f"Case created: {raw_date}"
            ),
            source_url=f"https://atlas.phila.gov/#{_normalize_addr(row.get('address',''))}",
        )
        _set_meta(n, owner_status=owner_status, violation_count_for_parcel=vcount)
        notices.append(n)

    logger.info("[li_violations] Built %d notices", len(notices))
    return notices


def _title(s: str) -> str:
    """Title-case a string that may be ALL-CAPS (common in city records)."""
    if not s:
        return s
    return s.title() if s.isupper() or s.islower() else s


# ── SOURCE: OPA Tax Delinquencies ─────────────────────────────────────

_SOURCE_TAX_DELINQUENT = next(
    (s for s in PHILLY_SOURCES if s.source_id == "opa_tax_delinquent"), None
)


# Seen-state for the tax-delinquency DELTA. The Carto dataset is a rolling
# snapshot (~19.6K parcels match the filters as of 2026-07), so uploading the
# full list every run would flood the niche funnel with tier-2 bulk data.
# First-to-market value is only the NEWLY delinquent parcels, so each run
# uploads the delta against this parcel set. Path is CWD-relative like
# phone_validator's scored_phones.json; the GHA workflow persists it via
# actions/cache between runs.
_OPA_SEEN_PATH = Path("data/cache/opa_delinquent_seen.json")


def _load_opa_seen() -> set[str]:
    try:
        return set(json.loads(_OPA_SEEN_PATH.read_text()))
    except (OSError, ValueError):
        return set()


def _save_opa_seen(seen: set[str]) -> None:
    _OPA_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OPA_SEEN_PATH.write_text(json.dumps(sorted(seen)))


async def scrape_opa_tax_delinquent(lookback_days: int = 7) -> list[NoticeData]:
    """Fetch NEWLY delinquent parcels from OpenDataPhilly Carto (delta mode).

    Filters: num_years_owed >= 2 AND total_due >= 5000. The dataset is a
    rolling snapshot, so this returns only parcels not present in the
    seen-state file from prior runs. On the very first run (no state file)
    it seeds the state with the full snapshot and returns nothing — the
    existing ~19.6K backlog is tier-2 stacking data already available in
    SiftMap, not first-to-market.
    lookback_days param accepted for API compatibility but unused.
    """
    if _SOURCE_TAX_DELINQUENT is None:
        logger.error("[opa_tax_delinquent] Source not found in PHILLY_SOURCES")
        return []

    query = (
        "SELECT opa_number, owner, total_due, principal_due, oldest_year_owed, "
        "num_years_owed, street_address, zip_code, mailing_address, mailing_city, "
        "mailing_state, mailing_zip "
        "FROM real_estate_tax_delinquencies "
        "WHERE num_years_owed >= 2 AND total_due >= 5000 "
        "ORDER BY total_due DESC "
        "LIMIT 50000"
    )
    params = {"q": query, "format": "json"}

    logger.info(
        "[opa_tax_delinquent] Querying Carto API (>=2 years, >=$5K balance)"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(PHILLY_CARTO_API_URL, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    rows: list[dict] = data.get("rows", [])
    today = _today()
    notices: list[NoticeData] = []

    seen = _load_opa_seen()
    snapshot = {str(r.get("opa_number") or "") for r in rows} - {""}
    if not seen:
        _save_opa_seen(snapshot)
        logger.info(
            "[opa_tax_delinquent] First run — seeded seen-state with %d parcels, "
            "returning 0 records (backlog is tier-2 data; deltas start next run)",
            len(snapshot),
        )
        return []

    new_rows = [r for r in rows if str(r.get("opa_number") or "") not in seen]
    _save_opa_seen(seen | snapshot)
    logger.info(
        "[opa_tax_delinquent] %d newly delinquent parcels (of %d in snapshot)",
        len(new_rows), len(rows),
    )

    for row in new_rows:
        zip_raw = str(row.get("zip_code") or "")[:5]
        n = _philly_notice(
            _SOURCE_TAX_DELINQUENT,
            date_added=today,
            address=_title(str(row.get("street_address") or "")),
            zip=zip_raw,
            owner_name=_title(str(row.get("owner") or "")),
            parcel_id=str(row.get("opa_number") or ""),
            tax_delinquent_amount=str(row.get("total_due") or ""),
            tax_delinquent_years=str(row.get("num_years_owed") or ""),
            owner_street=_title(str(row.get("mailing_address") or "")),
            owner_city=_title(str(row.get("mailing_city") or "")),
            owner_state=str(row.get("mailing_state") or ""),
            owner_zip=str(row.get("mailing_zip") or "")[:5],
        )
        notices.append(n)

    logger.info(
        "[opa_tax_delinquent] %d records (>=2 years, >=$5K balance)", len(notices)
    )
    return notices


async def _enrich_tax_delinquency(notices: list[NoticeData]) -> int:
    """Overlay tax delinquency data from Carto onto notices that have a parcel_id.

    Only runs on records where parcel_id is set and tax_delinquent_amount is empty.
    Returns count of records enriched.
    """
    candidates = [
        n for n in notices
        if (n.parcel_id or "").strip() and not (n.tax_delinquent_amount or "").strip()
    ]
    if not candidates:
        return 0

    ids = list({n.parcel_id.strip() for n in candidates})
    enriched = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for batch in _chunks(ids, 200):
            ids_str = ",".join(b.replace("'", "''") for b in batch)
            query = (
                f"SELECT opa_number, total_due, num_years_owed "
                f"FROM real_estate_tax_delinquencies "
                f"WHERE opa_number IN ({ids_str})"
            )
            try:
                resp = await client.get(
                    PHILLY_CARTO_API_URL, params={"q": query, "format": "json"}
                )
                resp.raise_for_status()
                rows = resp.json().get("rows", [])
                delinq_map = {str(r["opa_number"]): r for r in rows if r.get("opa_number")}

                for n in candidates:
                    pid = n.parcel_id.strip()
                    if pid in delinq_map:
                        row = delinq_map[pid]
                        n.tax_delinquent_amount = str(row.get("total_due") or "")
                        n.tax_delinquent_years = str(row.get("num_years_owed") or "")
                        enriched += 1
            except Exception:
                logger.warning(
                    "[_enrich_tax_delinquency] Batch error for %d ids", len(batch),
                    exc_info=True,
                )

    if enriched:
        logger.info("[_enrich_tax_delinquency] Enriched %d records with tax delinquency data", enriched)
    return enriched


# ── SOURCE: L&I Imminently Dangerous ─────────────────────────────────

_SOURCE_IMMINENTLY_DANGEROUS = next(
    (s for s in PHILLY_SOURCES if s.source_id == "li_imminently_dangerous"), None
)


async def scrape_li_imminently_dangerous(lookback_days: int = 7) -> list[NoticeData]:
    """Fetch Imminently Dangerous structures from the live L&I violations table.

    Queries the eCLIPSE-era `violations` table (caseprioritydesc), NOT the
    legacy `li_violations` table — that dataset froze at the March 2020
    eCLIPSE migration (max caseaddeddate 2020-03-12), so the old query only
    ever returned pre-2020 cases. Live-table volume check (2026-07-24):
    1,042 ID rows in the trailing year, ~72/month — a small daily delta.

    Filters: caseprioritydesc = 'IMMINENTLY DANGEROUS', casestatus != 'CLOSED',
    casecreateddate within lookback_days.
    """
    if _SOURCE_IMMINENTLY_DANGEROUS is None:
        logger.error("[li_imminently_dangerous] Source not found in PHILLY_SOURCES")
        return []

    query = (
        "SELECT v.address, v.zip, v.casecreateddate, "
        "v.violationcodetitle AS violationdescription, "
        "v.violationstatus AS status, v.casestatus, "
        "v.opa_owner AS ownername, "
        "v.opa_account_num AS parcelid_num, "
        "v.geocode_x, v.geocode_y, "
        "o.mailing_street AS owneraddress, "
        "o.mailing_city_state AS ownercitystate, "
        "o.mailing_zip AS ownerzip "
        f"FROM {PHILLY_CARTO_VIOLATIONS_TABLE} v "
        "LEFT JOIN opa_properties_public o "
        "  ON v.opa_account_num = o.parcel_number "
        "WHERE v.caseprioritydesc = 'IMMINENTLY DANGEROUS' "
        "  AND v.casestatus != 'CLOSED' "
        f"  AND v.casecreateddate >= NOW() - INTERVAL '{lookback_days} days' "
        "ORDER BY v.casecreateddate DESC"
    )
    params = {"q": query, "format": "json"}

    logger.info(
        "[li_imminently_dangerous] Querying Carto API (last %d days, open cases)",
        lookback_days,
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(PHILLY_CARTO_API_URL, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    rows: list[dict] = data.get("rows", [])
    logger.info("[li_imminently_dangerous] %d rows returned", len(rows))

    notices: list[NoticeData] = []
    for row in rows:
        # li_violations uses caseaddeddate (confirmed from live schema 2026-04-30)
        raw_date = (row.get("caseaddeddate") or "")[:10]

        prop_addr = _normalize_addr(row.get("address") or "")
        mail_addr = _normalize_addr(row.get("owneraddress") or "")
        if not mail_addr:
            owner_status = "UNKNOWN"
        elif prop_addr and mail_addr == prop_addr:
            owner_status = "OWNER_OCCUPIED"
        else:
            owner_status = "ABSENTEE"

        city_state = row.get("ownercitystate") or ""
        cs_parts = city_state.rsplit(" ", 1)
        owner_city = _title(cs_parts[0]) if cs_parts else ""
        owner_state = cs_parts[1].upper() if len(cs_parts) > 1 else ""

        n = _philly_notice(
            _SOURCE_IMMINENTLY_DANGEROUS,
            date_added=raw_date or _today(),
            address=_title(row.get("address") or ""),
            zip=(row.get("zip") or "")[:5],
            owner_name=_title(row.get("ownername") or ""),
            owner_street=_title(row.get("owneraddress") or ""),
            owner_city=owner_city,
            owner_state=owner_state,
            owner_zip=(row.get("ownerzip") or "")[:5],
            parcel_id=str(row.get("parcelid_num") or ""),
            latitude=str(row.get("geocode_y") or ""),
            longitude=str(row.get("geocode_x") or ""),
            raw_text=(
                f"IMMINENTLY DANGEROUS: {row.get('violationdescription', '')} "
                f"Status: {row.get('status', '')} "
                f"Case status: {row.get('casestatus', '')} "
                f"Case added: {raw_date}"
            ),
            source_url=f"https://atlas.phila.gov/#{_normalize_addr(row.get('address', ''))}",
        )
        _set_meta(n, owner_status=owner_status, imminently_dangerous=True)
        notices.append(n)

    logger.info("[li_imminently_dangerous] Built %d notices", len(notices))

    # OPA enrichment — same as li_violations
    if notices:
        kept, dropped = await enrich_and_filter(notices, id_field="parcel_id")
        logger.info(
            "[li_imminently_dangerous] After OPA filter: %d kept, %d dropped (vacant/land)",
            len(kept), dropped,
        )
        return kept

    return notices


async def _enrich_expired_permits(notices: list[NoticeData]) -> int:
    """Overlay expired permit flag onto notices that have a parcel_id.

    Queries permits Carto table for EXPIRED status by opa_account_num.
    Sets notice.expired_permit = 'yes' for matches.
    Returns count of records enriched.
    """
    candidates = [
        n for n in notices
        if (n.parcel_id or "").strip() and not (n.expired_permit or "").strip()
    ]
    if not candidates:
        return 0

    ids = list({n.parcel_id.strip() for n in candidates})
    enriched = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for batch in _chunks(ids, 200):
            ids_str = "','".join(b.replace("'", "''") for b in batch)
            query = (
                f"SELECT DISTINCT opa_account_num "
                f"FROM permits "
                f"WHERE status ILIKE 'EXPIRED%' "
                f"AND opa_account_num IN ('{ids_str}')"
            )
            try:
                resp = await client.get(
                    PHILLY_CARTO_API_URL, params={"q": query, "format": "json"}
                )
                resp.raise_for_status()
                rows = resp.json().get("rows", [])
                expired_set = {str(r["opa_account_num"]) for r in rows if r.get("opa_account_num")}

                for n in candidates:
                    if n.parcel_id.strip() in expired_set:
                        n.expired_permit = "yes"
                        enriched += 1
            except Exception:
                logger.warning(
                    "[_enrich_expired_permits] Batch error for %d ids", len(batch),
                    exc_info=True,
                )

    if enriched:
        logger.info("[_enrich_expired_permits] Flagged %d records with expired permits", enriched)
    return enriched


# ── Bid4Assets shared helpers (Sources 2 & 3) ─────────────────────────

# CSS selectors for the Bid4Assets auction listing pages.
# Philadelphia sheriff auction items appear inside a UL with class "auctions-list".
# Each LI contains the property card. Selectors verified against:
#   bid4assets.com/philaforeclosures  and  bid4assets.com/philataxsales
_B4A_ITEM_SEL = "ul.auctions-list li.auction-item, div.auction-item"
_B4A_ADDRESS_SEL = "h3.auction-title, .auction-address, h2.item-title"
_B4A_BRT_SEL = ".auction-description, .parcel-id, .item-description"
_B4A_BID_SEL = ".current-bid, .minimum-bid, .starting-bid"
_B4A_DATE_SEL = ".auction-end, .end-date, time[datetime]"
_B4A_LINK_SEL = "a.auction-link, a.item-link, a[href*='/auction/']"
_B4A_NEXT_SEL = "a.pagination-next, a[rel='next'], .pagination a:last-child"
_B4A_HCAPTCHA_SEL = ".h-captcha[data-sitekey], [data-sitekey]"


# FJD Civil reCAPTCHA v3 sitekey (confirmed from live DOM 2026-04-26)
_FJD_RECAPTCHA_V3_SITEKEY = "6Lcp47cUAAAAAHa3U6EeoEjCD3K60BTflkgWoRxj"


async def _solve_recaptcha_v3(
    page: Page,
    page_url: str,
    sitekey: str,
    action: str = "submit",
    proxy_url: str = "",
) -> bool:
    """Solve reCAPTCHA v3 via CapSolver, inject into FJD's hash_code field.

    Uses ReCaptchaV3Task (with proxy) so CapSolver routes the solve through
    the Bright Data residential IP — the token is tied to that IP, which must
    match the browser's egress IP for Google's server-side verification.

    Verbose logging: logs raw token prefix, CapSolver task response, and
    wires a Playwright response listener to capture FJD's siteverify result.
    """
    if not CAPSOLVER_API_KEY:
        logger.warning("[capsolver] CAPSOLVER_API_KEY not set — cannot solve v3")
        return False

    # Always use ProxyLess — Bright Data proxy (port 33335) is unreachable from
    # CapSolver's infrastructure (error 1001 on every attempt).  ProxyLess works
    # reliably; CapSolver generates higher-score tokens than 2Captcha proxyless.
    task_type = "ReCaptchaV3TaskProxyLess"
    logger.info(
        "[capsolver] Solving v3 | url=%s | action=%s | task_type=%s",
        page_url, action, task_type,
    )

    # Register a response listener BEFORE submission so we catch the FJD
    # server's redirect and can log what personcase_details_idx returns.
    fjd_responses: list[dict] = []

    async def _capture_fjd_response(resp) -> None:
        if "personcase" in resp.url:
            body = ""
            try:
                body = await resp.text()
            except Exception:
                pass
            fjd_responses.append({
                "url": resp.url,
                "status": resp.status,
                "body_snippet": body[:300],
            })

    page.on("response", _capture_fjd_response)

    try:
        capsolver.api_key = CAPSOLVER_API_KEY
        task: dict = {
            "type": task_type,
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "pageAction": action,
            "minScore": 0.7,
        }

        # CapSolver.solve() is synchronous — run in thread to avoid blocking
        loop = asyncio.get_event_loop()
        solution = await loop.run_in_executor(None, capsolver.solve, task)

        token = solution.get("gRecaptchaResponse", "")
        logger.info(
            "[capsolver] Solution received | token=%.50s... | len=%d | full_solution=%s",
            token, len(token), solution,
        )
    except Exception:
        logger.exception("[capsolver] Solve failed")
        page.remove_listener("response", _capture_fjd_response)
        return False

    if not token:
        logger.warning("[capsolver] Empty token returned")
        page.remove_listener("response", _capture_fjd_response)
        return False

    # Inject into hash_code (confirmed FJD field name from live DOM 2026-04-26)
    await page.evaluate(
        """(tok) => {
            document.querySelectorAll('[name="hash_code"]').forEach(el => { el.value = tok; });
        }""",
        token,
    )
    logger.info("[capsolver] Token injected into hash_code (%d chars)", len(token))

    # Store listener removal for caller to trigger after form submit
    page._fjd_response_log = fjd_responses  # type: ignore[attr-defined]
    page._fjd_response_listener = _capture_fjd_response  # type: ignore[attr-defined]
    return True


async def _solve_hcaptcha(page: Page, page_url: str) -> bool:
    """Solve hCaptcha via CapSolver and inject the token into the page.

    NOTE: Bid4Assets now uses Scrapfly (no Playwright) so this function is
    not called in normal operation. Kept for future Playwright-based sources
    that may encounter hCaptcha.
    """
    if not CAPSOLVER_API_KEY:
        logger.error("[hcaptcha] CAPSOLVER_API_KEY not set — skipping solve")
        return False

    widget = await page.query_selector(_B4A_HCAPTCHA_SEL)
    if not widget:
        logger.debug("[hcaptcha] No hCaptcha widget found — none required")
        return True

    sitekey = await page.evaluate("el => el.getAttribute('data-sitekey')", widget) or BID4ASSETS_HCAPTCHA_SITEKEY
    if not sitekey:
        logger.error("[hcaptcha] Cannot determine sitekey")
        return False

    logger.info("[hcaptcha] Solving hCaptcha via CapSolver for %s (sitekey=%.12s…)", page_url, sitekey)
    try:
        capsolver.api_key = CAPSOLVER_API_KEY
        loop = asyncio.get_event_loop()
        solution = await loop.run_in_executor(None, capsolver.solve, {
            "type": "HCaptchaTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        })
        token = solution.get("gRecaptchaResponse", "")
    except Exception:
        logger.exception("[hcaptcha] CapSolver request failed")
        return False

    if not token:
        logger.warning("[hcaptcha] CapSolver returned empty token")
        return False

    await page.evaluate(
        """(tok) => {
            ['h-captcha-response', 'g-recaptcha-response'].forEach(name => {
                const el = document.querySelector(`[name="${name}"]`);
                if (el) { el.value = tok; el.style.display = 'block'; }
            });
            if (window.hcaptcha) { try { window.hcaptcha.execute(); } catch(e) {} }
        }""",
        token,
    )
    await asyncio.sleep(1)
    logger.info("[hcaptcha] Token injected")
    return True


def _parse_b4a_date(raw: str) -> str:
    """Parse Bid4Assets date strings to YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback: strip time component if present ("May 5, 2026 01:00 PM")
    raw_short = re.sub(r"\s+\d{1,2}:\d{2}.*$", "", raw).strip()
    if raw_short != raw:
        return _parse_b4a_date(raw_short)
    return ""


def _extract_brt(text: str) -> str:
    """Pull a BRT# (8-digit Philadelphia parcel ID) from text."""
    m = re.search(r"\b(\d{8,9})\b", text)
    return m.group(1) if m else ""


async def _scrape_b4a_page(
    page: Page,
    source: PhillySource,
    notices: list[NoticeData],
) -> bool:
    """Parse one Bid4Assets listing page into NoticeData records.

    Returns True if a next-page link exists.
    """
    try:
        await page.wait_for_selector(_B4A_ITEM_SEL, timeout=20_000)
    except PwTimeout:
        logger.warning("[%s] No auction items found on page", source.source_id)
        return False

    items = await page.query_selector_all(_B4A_ITEM_SEL)
    logger.info("[%s] %d items on this page", source.source_id, len(items))

    for item in items:
        try:
            # Address
            addr_el = await item.query_selector(_B4A_ADDRESS_SEL)
            raw_addr = (await addr_el.inner_text()).strip() if addr_el else ""

            # BRT# / parcel ID
            desc_el = await item.query_selector(_B4A_BRT_SEL)
            desc_text = (await desc_el.inner_text()).strip() if desc_el else ""
            brt = _extract_brt(desc_text) or _extract_brt(raw_addr)

            # Minimum bid
            bid_el = await item.query_selector(_B4A_BID_SEL)
            bid_text = (await bid_el.inner_text()).strip() if bid_el else ""

            # Auction / sale date
            date_el = await item.query_selector(_B4A_DATE_SEL)
            raw_date = ""
            if date_el:
                raw_date = (
                    await date_el.get_attribute("datetime")
                    or await date_el.inner_text()
                )
            auction_dt = _parse_b4a_date(raw_date)

            # Detail page link (for source_url only — we do NOT follow it)
            link_el = await item.query_selector(_B4A_LINK_SEL)
            detail_href = await link_el.get_attribute("href") if link_el else ""
            source_url = (
                f"https://www.bid4assets.com{detail_href}"
                if detail_href and not detail_href.startswith("http")
                else detail_href
            )

            # Parse street + zip from raw_addr (format: "1234 Main St, Philadelphia PA 19103")
            addr_parts = _split_philly_address(raw_addr)

            n = _philly_notice(
                source,
                date_added=_today(),
                address=addr_parts["street"],
                zip=addr_parts["zip"],
                auction_date=auction_dt,
                parcel_id=brt,
                raw_text=f"{raw_addr} | {desc_text} | Min bid: {bid_text}",
                source_url=source_url or page.url,
            )
            notices.append(n)

        except Exception:
            logger.debug("[%s] Error parsing item", source.source_id, exc_info=True)

    # Check for pagination
    next_btn = await page.query_selector(_B4A_NEXT_SEL)
    if not next_btn:
        return False
    disabled = await next_btn.get_attribute("disabled") or await next_btn.get_attribute("aria-disabled")
    return not disabled


def _split_philly_address(raw: str) -> dict[str, str]:
    """Split 'STREET, Philadelphia PA ZIP' into components."""
    # Philadelphia ZIP codes: 191xx
    zip_m = re.search(r"\b(191\d{2})\b", raw)
    zip_code = zip_m.group(1) if zip_m else ""
    # Street is everything up to the first comma
    street = raw.split(",")[0].strip()
    return {"street": street, "zip": zip_code}


def _parse_b4a_soup(soup: BeautifulSoup, source: PhillySource, page_url: str) -> list[NoticeData]:
    """Parse a Bid4Assets listing page rendered by Scrapfly.

    Confirmed HTML structure (live Scrapfly render 2026-04-26):
      .property-auction > .panel-body > tables[2] = Kendo grid
      Columns (0-indexed TDs): ID | Book/Writ | OPA# | Address | Current Bid | Close Time | Status
      Sale date is in the panel heading: "Real Property List for <date>"
    """
    # Extract sale date from panel heading
    heading_el = soup.select_one(".auction-folders a, .panel-title a, h4.panel-title")
    heading_text = heading_el.get_text(strip=True) if heading_el else ""
    auction_dt = ""
    import re as _re
    sale_date_m = _re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4}", heading_text, _re.IGNORECASE,
    )
    if sale_date_m:
        auction_dt = _parse_b4a_date(sale_date_m.group(0))
    logger.info("[%s] Sale date from heading: '%s' → %s", source.source_id, heading_text[:60], auction_dt)

    # The Kendo grid is the THIRD table in .panel-body (index 2)
    panel_body = soup.select_one(".property-auction .panel-body")
    if not panel_body:
        top_classes = sorted({c for el in soup.find_all(class_=True) for c in el.get("class", [])
                               if "auction" in c.lower() or "property" in c.lower()})
        logger.warning("[%s] No .property-auction .panel-body — classes: %s", source.source_id, top_classes[:15])
        return []

    tables = panel_body.find_all("table")
    logger.info("[%s] Tables in panel-body: %d", source.source_id, len(tables))
    if len(tables) < 3:
        logger.warning("[%s] Expected ≥3 tables, got %d", source.source_id, len(tables))
        return []

    grid = tables[2]  # Kendo data grid
    rows = grid.find_all("tr")
    logger.info("[%s] Grid rows: %d", source.source_id, len(rows))

    notices: list[NoticeData] = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue  # header row or empty
        try:
            # td[0] = Auction ID (link)
            auction_id_el = cells[0].find("a")
            detail_href = auction_id_el.get("href", "") if auction_id_el else ""
            detail_url = (
                f"https://www.bid4assets.com{detail_href}"
                if detail_href and not detail_href.startswith("http")
                else detail_href
            ) or page_url

            # td[2] = OPA account number (Philadelphia parcel ID)
            opa = cells[2].get_text(strip=True)

            # td[3] = Address (link text: "1700 GERMANTOWN AVENUE PHILADELPHIA PA 19122")
            addr_el = cells[3].find("a") or cells[3]
            raw_addr = addr_el.get_text(strip=True)
            addr_parts = _split_philly_address(raw_addr)

            # td[4] = Current bid
            bid_text = cells[4].get_text(strip=True)

            # td[6] = Status (Postponed / Preview / Active)
            status = cells[6].get_text(strip=True) if len(cells) > 6 else ""

            n = _philly_notice(
                source,
                date_added=_today(),
                address=_title(addr_parts["street"]),
                zip=addr_parts["zip"],
                auction_date=auction_dt,
                parcel_id=opa,
                raw_text=f"{raw_addr} | OPA:{opa} | Bid:{bid_text} | Status:{status}",
                source_url=detail_url,
            )
            notices.append(n)
        except Exception:
            logger.debug("[%s] Row parse error", source.source_id, exc_info=True)

    return notices


async def _scrape_bid4assets_scrapfly(source: PhillySource, listing_url: str) -> list[NoticeData]:
    """Scrape a Bid4Assets sheriff auction listing via Scrapfly SDK.

    All 395+ records are embedded as a single inline JSON array in the JS —
    confirmed 2026-04-26. One Scrapfly request gets everything.
    No pagination needed. Filter 1 (Philly County only) applied here.
    Filter 2 (houses only) applied later in enrich_and_filter().
    """
    if not SCRAPFLY_API_KEY:
        logger.error("[%s] SCRAPFLY_API_KEY not set — cannot scrape Bid4Assets", source.source_id)
        return []

    logger.info("[%s] Scrapfly fetch: %s", source.source_id, listing_url)
    with ScrapflyClient(key=SCRAPFLY_API_KEY) as client:
        try:
            result = await client.async_scrape(ScrapeConfig(
                url=listing_url,
                asp=True,
                render_js=True,
                country="us",
                retry=True,
                rendering_wait=4000,
            ))
            html = result.content
            logger.info("[%s] html_len=%d", source.source_id, len(html))
        except Exception:
            logger.exception("[%s] Scrapfly request failed", source.source_id)
            return []

    records = _extract_b4a_json(html)
    logger.info("[%s] Inline JSON: %d total records", source.source_id, len(records))

    if not records:
        logger.warning("[%s] No inline JSON found — check if Bid4Assets changed its embed pattern", source.source_id)
        return []

    notices: list[NoticeData] = []
    seen_ids: set[int] = set()

    for rec in records:
        aid = rec.get("AuctionID")
        if aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)

        # Filter 1: Philadelphia County only
        county = rec.get("County") or ""
        if county and county.upper() != "PHILADELPHIA":
            continue

        raw_addr = rec.get("Address") or ""
        addr_parts = _split_philly_address(raw_addr)

        close_dt = (rec.get("ActualCloseTime") or "")[:10]  # YYYY-MM-DD
        open_dt = (rec.get("BidOpenTime") or "")[:10]
        sale_dt = (rec.get("SaleDate") or "")[:10]
        auction_dt = _parse_b4a_date(sale_dt) or _parse_b4a_date(close_dt)

        status = rec.get("AuctionStatusString") or ""
        min_bid = rec.get("MinimumBid") or 0
        book_writ = rec.get("CourtCase") or rec.get("SheriffNumber") or ""
        attorney = rec.get("Attorney") or ""
        plaintiff = rec.get("Plaintiff") or ""
        defendant = rec.get("Defendant") or ""
        sale_type = rec.get("SaleType") or source.notice_type
        opa = str(rec.get("Apn") or "")
        detail_url = f"https://www.bid4assets.com/auction/{aid}" if aid else listing_url

        # Owner = DEFENDANT (the homeowner being foreclosed). The plaintiff is
        # the foreclosing lender / the City — never the contact. Records built
        # with owner_name=plaintiff (pre-2026-08) leaked bank names into the
        # owner fields whenever OPA had no row to override them, and sent the
        # obituary enricher searching for deceased Wells Fargos.
        #
        # Live check 2026-08-06: the inline JSON's Defendant field is EMPTY on
        # all 393 mortgage records, so in practice owner_name is blank here and
        # the OPA assessed owner (tax_owner_name, applied in enrich_and_filter)
        # is the effective name source — which the formatter prefers anyway.
        # Kept as defendant in case Bid4Assets starts populating it; records
        # with no OPA name either are dropped by the pipeline's nameless gate.
        n = _philly_notice(
            source,
            date_added=_today(),
            address=_title(addr_parts["street"]),
            zip=addr_parts["zip"],
            auction_date=auction_dt,
            parcel_id=opa,
            owner_name=_title(defendant),
            raw_text=(
                f"{raw_addr} | OPA:{opa} | Book/Writ:{book_writ} | "
                f"Attorney:{attorney} | Min bid:${min_bid:,.0f} | "
                f"Status:{status} | Plaintiff:{plaintiff} | Defendant:{defendant}"
            ),
            source_url=detail_url,
        )
        _set_meta(
            n,
            b4a_auction_id=aid,
            b4a_status=status,
            min_bid=min_bid,
            bidding_close_dt=close_dt,
            bidding_open_dt=open_dt,
            book_writ=book_writ,
            attorney=attorney,
            sale_type=sale_type,
            plaintiff=plaintiff,
            defendant=defendant,
        )
        notices.append(n)

    logger.info("[%s] After Filter 1 (Philly only): %d records", source.source_id, len(notices))
    return notices


# ── SOURCE 2 — Sheriff Mortgage Foreclosure Sales ─────────────────────

_SOURCE_MORTGAGE = next(s for s in PHILLY_SOURCES if s.source_id == "bid4assets_mortgage")


async def scrape_bid4assets_mortgage() -> list[NoticeData]:
    """Scrape Philadelphia sheriff mortgage-foreclosure sale listings via Scrapfly."""
    return await _scrape_bid4assets_scrapfly(_SOURCE_MORTGAGE, PHILLY_BID4ASSETS_MORTGAGE_URL)


# ── SOURCE 3 — Sheriff Tax Sales ──────────────────────────────────────

_SOURCE_TAX = next(s for s in PHILLY_SOURCES if s.source_id == "bid4assets_tax")


async def scrape_bid4assets_tax() -> list[NoticeData]:
    """Scrape Philadelphia sheriff tax-sale listings via Scrapfly."""
    return await _scrape_bid4assets_scrapfly(_SOURCE_TAX, PHILLY_BID4ASSETS_TAX_URL)


# ── SOURCE 4 — Lis Pendens / Pre-Foreclosure (FJD Civil) ─────────────

_SOURCE_LP = next(s for s in PHILLY_SOURCES if s.source_id == "fjd_lis_pendens")

# FJD personcase search confirmed selectors (live DOM inspection 2026-04-26):
#   Main index: zk_fjd_public_qry_00.zp_main_idx  (navigation menu)
#   Person search: zk_fjd_public_qry_01.zp_personcase_setup_idx
#   Form: last_name (required), first_name, begin_date, end_date (date inputs)
#   reCAPTCHA v3 (invisible, sitekey 6Lcp47cUAAAAAHa3U6EeoEjCD3K60BTflkgWoRxj)
#   Stealth mode typically passes v3 without a CAPTCHA solve step.
#
# Strategy: search by company name for known major Philadelphia mortgage servicers
# that file the bulk of foreclosure complaints. Results page:
#   zk_fjd_public_qry_01.zp_personcase_results_idx
# Each row has: Docket | Name | Role | Case type | Filed date | Status

_FJD_PERSON_LINK_SEL = "a[href*='personcase_setup']"
_FJD_LAST_NAME_SEL = "input[name='last_name']"
_FJD_BEGIN_DATE_SEL = "input[name='begin_date']"
_FJD_END_DATE_SEL = "input[name='end_date']"
_FJD_SUBMIT_SEL = "input[type='submit'][value='Submit']"
_FJD_RESULTS_URL_FRAG = "personcase_details"  # confirmed form action: zp_personcase_details_idx
_FJD_RESULTS_ROW_SEL = "table tbody tr, table tr:not(:first-child)"
_FJD_NO_RESULTS_TEXT = "No records"

# Updated servicer list per Change 4 (2026-04-26).  Includes Mr. Cooper (was
# Nationstar), Wilmington Savings Fund Society, MidFirst Bank, M&T Bank, PNC,
# Carrington Mortgage, and U.S. Bank Trust variations.
_PHILLY_FC_SERVICERS = [
    "WELLS FARGO",
    "U.S. BANK",
    "US BANK",
    "BANK OF AMERICA",
    "SPECIALIZED LOAN",
    "WILMINGTON SAVINGS",
    "NEWREZ",
    "MIDFIRST BANK",
    "M&T BANK",
    "PNC",
    "CARRINGTON MORTGAGE",
    "PHH MORTGAGE",
    "MR. COOPER",
    "NATIONSTAR",          # historical alias for Mr. Cooper
    "SELENE FINANCE",
    "ROCKET MORTGAGE",
    "PENNYMAC",
    "LAKEVIEW LOAN",
    "FREEDOM MORTGAGE",
]

# Default lookback changed from 7 → 30 days (Change 4)
_FJD_DEFAULT_LOOKBACK = 30


async def scrape_fjd_lis_pendens(
    lookback_days: int = _FJD_DEFAULT_LOOKBACK,
) -> list[NoticeData]:
    """Scrape recent MORTGAGE FORECLOSURE filings from FJD Civil eFiling via Scrapfly.

    Replaced Playwright+CapSolver with Scrapfly ASP + JS rendering.
    Scrapfly's managed-browser infrastructure produces reCAPTCHA v3 tokens
    that score high enough for FJD's server-side verification — headless
    Chromium (0.1-0.3) and CapSolver ProxyLess are both rejected by FJD.

    Flow per servicer:
      1. Scrapfly loads the setup form page (asp=True, render_js=True).
      2. A JS hook fills last_name / begin_date / end_date and clicks Submit.
      3. The form's own grecaptcha.execute() runs → injects token → form.submit().
      4. Scrapfly follows the navigation; rendering_wait captures the results page.
      5. HTML is parsed with BeautifulSoup — same row/caption parsers as before.
    """
    if not SCRAPFLY_API_KEY:
        logger.warning("[fjd_lis_pendens] SCRAPFLY_API_KEY not set — skipping")
        return []

    # FJD date fields are <input type="date"> — programmatic .value assignment
    # REQUIRES ISO YYYY-MM-DD. The old MM/DD/YYYY strings were silently
    # rejected by the browser (value stayed empty), so every search posted
    # blank dates and bounced back to the setup page — previously
    # misdiagnosed as a reCAPTCHA v3 rejection.
    date_from = _cutoff(lookback_days)
    date_to = datetime.now().strftime("%Y-%m-%d")
    notices: list[NoticeData] = []
    seen_dockets: set[str] = set()

    # One sticky Scrapfly session for the whole run: the person-search URL
    # carries uid/o tokens minted on the index load, so cookies + proxy IP
    # must stay consistent across requests.
    session_name = f"fjd_lp_{datetime.now():%Y%m%d%H%M%S}"

    logger.info("[fjd_lis_pendens] Loading FJD index via Scrapfly: %s", PHILLY_FJD_CIVIL_URL)

    with ScrapflyClient(key=SCRAPFLY_API_KEY) as client:
        # ── Step 1: get the person-search session URL (uid/o params) ──
        try:
            idx_result = await client.async_scrape(ScrapeConfig(
                url=PHILLY_FJD_CIVIL_URL,
                asp=True,
                render_js=True,
                country="us",
                rendering_wait=3000,
                session=session_name,
            ))
        except Exception:
            logger.exception("[fjd_lis_pendens] Scrapfly: could not load FJD index")
            return []

        idx_soup = BeautifulSoup(idx_result.content, "html.parser")
        link_tag = idx_soup.find("a", href=re.compile(r"personcase_setup", re.IGNORECASE))
        if not link_tag:
            logger.error("[fjd_lis_pendens] Person-search link not found on FJD index page")
            return []
        href = str(link_tag.get("href", ""))
        if not href.startswith("http"):
            href = "https://fjdefile.phila.gov/efsfjd/" + href
        person_form_url = href
        logger.info("[fjd_lis_pendens] Person search URL: %s", person_form_url)

        # ── Step 2: one Scrapfly request per servicer ──
        for servicer in _PHILLY_FC_SERVICERS:
            logger.info(
                "[fjd_lis_pendens] Searching '%s' (%s – %s)", servicer, date_from, date_to,
            )

            # Escape servicer name for inline JS string literal
            safe_name = servicer.replace("\\", "\\\\").replace("'", "\\'")

            # Set fields by direct .value assignment — the date fields are
            # <input type="date"> and only accept ISO YYYY-MM-DD (scenario
            # "fill" types like a user, unreliable for date controls).
            set_fields_js = (
                "(function(){"
                f"  document.querySelector('[name=last_name]').value='{safe_name}';"
                f"  document.querySelector('[name=begin_date]').value='{date_from}';"
                f"  document.querySelector('[name=end_date]').value='{date_to}';"
                "})()"
            )

            # js_scenario, NOT plain js=: clicking Submit fires the page's
            # validate() → grecaptcha.execute() (async) → form.submit(), and
            # only a scenario's wait_for_navigation makes Scrapfly follow
            # that POST before capturing content. The old plain-js hook
            # captured the pre-submit setup page every time, which looked
            # like a reCAPTCHA rejection.
            scenario = [
                {"wait_for_selector": {"selector": "input[name='last_name']", "timeout": 15000}},
                {"wait": 3000},  # let the reCAPTCHA v3 script finish loading
                {"execute": {"script": set_fields_js}},
                {"click": {"selector": "input[type='submit']"}},
                {"wait_for_navigation": {"timeout": 10000}},  # Scrapfly max 10s
                {"wait": 2500},
            ]

            try:
                result = await client.async_scrape(ScrapeConfig(
                    url=person_form_url,
                    asp=True,
                    render_js=True,
                    country="us",
                    retry=True,
                    rendering_wait=3000,
                    js_scenario=scenario,
                    session=session_name,
                ))
            except Exception:
                logger.warning(
                    "[fjd_lis_pendens] Scrapfly error for '%s'", servicer, exc_info=True,
                )
                continue

            html = result.content
            cost = getattr(result, "context", {}).get("cost", "?")

            soup = BeautifulSoup(html, "html.parser")

            # Detect bounce-back: still on setup page if the search form is present
            if soup.find("input", {"name": "last_name"}):
                logger.warning(
                    "[fjd_lis_pendens] '%s' → setup page returned (reCAPTCHA rejected). "
                    "Scrapfly cost: %s",
                    servicer, cost,
                )
                continue

            body_text = soup.get_text(" ", strip=True)
            if _FJD_NO_RESULTS_TEXT in body_text:
                logger.info("[fjd_lis_pendens] '%s' → 0 rows (cost: %s)", servicer, cost)
                continue

            # Parse result rows (same logic as Playwright version)
            data_rows = [r for r in soup.select("table tr") if r.find("td")]
            logger.info(
                "[fjd_lis_pendens] '%s' → %d rows (cost: %s)", servicer, len(data_rows), cost,
            )

            for row in data_rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 3:
                        continue
                    texts = [c.get_text(strip=True) for c in cells]
                    docket, caption, filed_date = _parse_fjd_row(texts)

                    row_text = " ".join(texts).upper()
                    if "MORTGAGE" not in row_text and "FORECLOSURE" not in row_text:
                        continue

                    if docket and docket in seen_dockets:
                        continue
                    if docket:
                        seen_dockets.add(docket)

                    plaintiff, defendant, addr_hint = _parse_fjd_caption(caption)
                    n = _philly_notice(
                        _SOURCE_LP,
                        date_added=filed_date or _today(),
                        address=addr_hint,
                        owner_name=defendant,
                        raw_text=f"Docket: {docket} | {caption} | Filed: {filed_date}",
                        source_url=PHILLY_FJD_CIVIL_URL,
                    )
                    notices.append(n)
                except Exception:
                    logger.debug("[fjd_lis_pendens] Row parse error", exc_info=True)

    logger.info("[fjd_lis_pendens] Total unique records: %d", len(notices))
    return notices


def _parse_fjd_row(texts: list[str]) -> tuple[str, str, str]:
    """Extract docket number, case caption, and filing date from a result row."""
    docket = ""
    caption = ""
    filed = ""
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
    docket_re = re.compile(r"\d{4}-\d{5,}")

    for t in texts:
        t = t.strip()
        if date_re.match(t):
            filed = t
        elif docket_re.search(t) and not docket:
            docket = t
        elif " v. " in t or " VS " in t.upper() or len(t) > 20:
            caption = t

    return docket, caption, filed


def _parse_fjd_caption(caption: str) -> tuple[str, str, str]:
    """Split 'PLAINTIFF v. DEFENDANT' and try to find an address hint."""
    caption = caption.strip()
    plaintiff = ""
    defendant = ""
    addr_hint = ""

    sep_m = re.search(r"\s+v[s]?\.\s+", caption, re.IGNORECASE)
    if sep_m:
        plaintiff = caption[: sep_m.start()].strip().title()
        remainder = caption[sep_m.end():].strip()
        # Address sometimes appears in parens: "DOE, JOHN (123 Main St)"
        paren_m = re.search(r"\((.+?)\)", remainder)
        if paren_m:
            addr_hint = paren_m.group(1).strip()
            defendant = remainder[: paren_m.start()].strip().title()
        else:
            defendant = remainder.title()
    else:
        defendant = caption.title()

    return plaintiff, defendant, addr_hint


# ── SOURCE 5 — Evictions (FJD Municipal Court CLAIMS) ─────────────────

_SOURCE_EV = next(s for s in PHILLY_SOURCES if s.source_id == "fjd_evictions")

# ── CLAIMS portal constants (live-mapped 2026-04-27) ─────────────────
# Login: image CAPTCHA at /captcha.png, field captchaText, button "I Accept"
# Search: POST to /phmuni/cms/search.do
#   searchType=P (Plaintiff), caseType=2 (LT), startDate/endDate MM/DD/YYYY,
#   searchFor=<letter>, startRow=<1-based offset>, isPost=Y
# Results: <tbody tr> with 4 cells [Case#, MatchingParty, Plaintiffs, Defendants]
# Case# format: LT-YY-MM-DD-NNNN  →  filing date embedded in tokens 1-3
# Pagination: resubmit form with startRow incremented by _CLAIMS_PAGE_SIZE
_CLAIMS_SEARCH_URL = "https://fjdclaims.phila.gov/phmuni/cms/search.do"
_CLAIMS_LT_TYPE    = "2"   # LT - Landlord/Tenant
_CLAIMS_PAGE_SIZE  = 100   # rows per page (confirmed empirically)
# Search letter strategy: a-z covers all plaintiff names; dedup by case number
# handles overlap between letters. Numeric names caught by 0-9 sweep.
_CLAIMS_LETTERS = list("abcdefghijklmnopqrstuvwxyz0123456789")
_CLAIMS_CASE_RE = re.compile(r"^LT-(\d{2})-(\d{2})-(\d{2})-\d+$", re.IGNORECASE)

_2CAP_IN  = "https://2captcha.com/in.php"
_2CAP_RES = "https://2captcha.com/res.php"


async def _solve_claims_image_captcha(page: Page, api_key: str) -> str:
    """Screenshot the rendered captcha <img> element and solve via 2Captcha.

    Uses element screenshot (not ctx.request.get) so we solve exactly the image
    the browser is displaying.  Diagnostic confirmed ctx.request.get returns a
    DIFFERENT image than the browser renders for per-case CAPTCHAs → all fails.
    Screenshot approach: 3/3 success on first attempt.
    """
    img_el = await page.query_selector("img[src*='captcha']")
    if not img_el:
        raise RuntimeError("[fjd_evictions] CAPTCHA img element not found on page")
    img_bytes = await img_el.screenshot()
    b64 = base64.b64encode(img_bytes).decode()

    async with httpx.AsyncClient(timeout=30) as c:
        r = (await c.post(
            _2CAP_IN, data={"key": api_key, "method": "base64", "body": b64, "json": 1}
        )).json()
        if r.get("status") != 1:
            raise RuntimeError(f"2Captcha submit failed: {r}")
        task_id = r["request"]
        logger.debug("[fjd_evictions] 2Captcha task %s", task_id)

        for _ in range(30):
            await asyncio.sleep(3)
            r2 = (await c.get(
                f"{_2CAP_RES}?key={api_key}&action=get&id={task_id}&json=1"
            )).json()
            if r2.get("status") == 1:
                return r2["request"]
            if r2.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2Captcha poll error: {r2}")

    raise TimeoutError("2Captcha: no solution after 90s")


async def _claims_login(page: Page, ctx: BrowserContext) -> tuple[bool, bool]:  # noqa: ARG001
    """Log in to CLAIMS as a public user via image CAPTCHA (up to 5 attempts).

    Returns (logged_in, network_error) so the caller can distinguish a transient
    server outage (network_error=True) from a persistent CAPTCHA failure.
    """
    if not CAPTCHA_API_KEY:
        logger.warning("[fjd_evictions] CAPTCHA_API_KEY not set — cannot login to CLAIMS")
        return False, False

    net_failures = 0
    for attempt in range(1, 6):
        try:
            await page.goto(PHILLY_FJD_CLAIMS_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1)
            # Reveal the public-login form (JS toggle, no navigation)
            await page.click("input[value='Login As Public User']")
            await asyncio.sleep(1)

            solution = await _solve_claims_image_captcha(page, CAPTCHA_API_KEY)
            logger.info("[fjd_evictions] CAPTCHA solution %r (attempt %d)", solution, attempt)

            await page.fill("input[name='captchaText']", solution)
            await page.click("input[name='submitAction'][value='I Accept']")
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)

            if "login.do" not in page.url:
                logger.info("[fjd_evictions] Logged in: %s", page.url)
                return True, False
            logger.warning("[fjd_evictions] Wrong CAPTCHA (attempt %d), retrying", attempt)
        except Exception as exc:
            if any(p in str(exc) for p in _NET_ERR_PATTERNS):
                net_failures += 1
            logger.warning("[fjd_evictions] Login attempt %d failed", attempt, exc_info=True)

    # Network error if majority of attempts were connection-level failures
    return False, net_failures >= 3


def _parse_claims_address(raw: str) -> tuple[str, str, str, str]:
    """Split a CLAIMS property address string into (street, city, state, zip).

    Format: 'street [unit], CITY, ST XXXXX'
    Examples:
      '1050 N. Hancock St., N442, Philadelphia, PA 19123'
      '1010 N. Hancock Street, M103, AKA 1050 N. Hancock Street, Philadelphia, PA 19123'
      '1122 Frankford Avenue # 216, PHILADELPHIA, PA 19125'
    """
    m = re.search(r"^(.*),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})\s*$", raw.strip())
    if m:
        return (
            m.group(1).strip(),
            m.group(2).strip().title(),
            m.group(3),
            m.group(4),
        )
    # Fallback: return raw as street with Philadelphia defaults
    return raw.strip(), "Philadelphia", "PA", ""


async def _opa_parcel_id_by_address(street: str, zip_code: str) -> str:
    """Look up an OPA parcel_number from a free-form street address + ZIP.

    Queries OPA.location (situs address, e.g. '1122 FRANKFORD AVE') using the
    house number and first distinctive street-name token.  Returns the first
    matching parcel_number, or '' on no match / error.
    """
    # Extract house number
    hn_m = re.match(r"^(\d+)", street.strip())
    if not hn_m:
        return ""
    house_num = hn_m.group(1)

    # First significant street-name word (skip directionals and short tokens)
    _DIR = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NO", "SO"}
    keyword = ""
    for tok in street.upper().split()[1:]:
        clean = re.sub(r"[^A-Z]", "", tok)
        if len(clean) >= 4 and clean not in _DIR:
            keyword = clean[:6]
            break
    if not keyword:
        return ""

    safe_zip = re.sub(r"[^0-9]", "", zip_code)[:5]
    query = (
        f"SELECT parcel_number FROM opa_properties_public "
        f"WHERE UPPER(location) LIKE '{house_num} %{keyword}%' "
        f"AND zip_code = '{safe_zip}' LIMIT 3"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                PHILLY_CARTO_API_URL, params={"q": query, "format": "json"}
            )
            resp.raise_for_status()
            rows = resp.json().get("rows", [])
            if rows:
                return str(rows[0].get("parcel_number", ""))
    except Exception:
        logger.debug("[fjd_evictions] OPA address lookup error", exc_info=True)
    return ""


async def _fetch_case_property_address(
    page: Page,
    case_url: str,
    api_key: str,
) -> str:
    """Navigate to a CLAIMS case docket URL, solve the per-case CAPTCHA, and
    return the raw 'Property Address' string from the Additional Information
    table (e.g. '1122 Frankford Avenue # 216, PHILADELPHIA, PA 19125').

    Returns '' if CAPTCHA fails after 2 attempts or address not found.
    Each detail page requires its own image CAPTCHA solve (confirmed live).
    Uses element screenshot (not ctx.request.get) to avoid image mismatch.
    Click target is input[id='continueButton'] — NOT the first submit button
    which is 'Get New Image' and would just refresh the CAPTCHA.
    """
    await page.goto(case_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(1)

    # Solve per-case CAPTCHA (2 retries max)
    for attempt in range(1, 3):
        if "caseCaptcha" not in page.url:
            break
        try:
            sol = await _solve_claims_image_captcha(page, api_key)
            cap_field = await page.query_selector("input[name='captchaText']")
            if cap_field:
                await cap_field.fill(sol)
            # Must click "Continue", not "Get New Image" (first submit)
            cont = await page.query_selector(
                "input[id='continueButton'], input[value='Continue']"
            )
            if cont:
                await cont.click()
            else:
                # Fallback: click any submit that isn't "Get New Image" or "Cancel"
                for btn in await page.query_selector_all("input[type='submit']"):
                    val = (await btn.get_attribute("value") or "").strip()
                    if val not in ("Get New Image", "Cancel"):
                        await btn.click()
                        break
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)
        except Exception:
            logger.debug("[fjd_evictions] Case CAPTCHA attempt %d failed", attempt, exc_info=True)

    if "caseCaptcha" in page.url:
        return ""

    # Extract property address from page text
    # Pattern in innerText: "Property Address\tRevenue Case ID\n{address}"
    try:
        text = await page.evaluate("document.body.innerText")
        addr_m = re.search(
            r"Property Address[\s\t]+Revenue Case ID[\s\t]*\n([^\n]+)", text
        )
        if addr_m:
            return addr_m.group(1).strip()
    except Exception:
        logger.debug("[fjd_evictions] Address text parse error", exc_info=True)
    return ""


async def _claims_submit_search(
    page: Page,
    letter: str,
    date_from: str,
    date_to: str,
    start_row: int,
) -> None:
    """Navigate to the CLAIMS search form and submit for one letter + startRow."""
    await page.goto(_CLAIMS_SEARCH_URL, wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_selector("input[name='submit2']", timeout=10_000)
    await asyncio.sleep(1)

    # Set all form fields via JS (caseType select is hidden until searchType changes)
    safe_letter = letter.replace("'", "\\'")
    await page.evaluate(
        "document.querySelector(\"select[name='searchType']\").value='P';"
        "var ct=document.querySelector(\"select[name='caseType']\");"
        "ct.value='2';ct.dispatchEvent(new Event('change',{bubbles:true}));"
        f"document.querySelector(\"input[name='startDate']\").value='{date_from}';"
        f"document.querySelector(\"input[name='endDate']\").value='{date_to}';"
        f"document.querySelector(\"input[name='searchFor']\").value='{safe_letter}';"
        f"var sr=document.querySelector(\"input[name='startRow']\");"
        f"if(sr)sr.value='{start_row}';"
    )
    await asyncio.sleep(0.5)
    btn = await page.query_selector("input[name='submit2']")
    await btn.click()
    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    await asyncio.sleep(2)


# Network-error threshold before aborting detail page phase
_CLAIMS_NET_ERR_THRESHOLD = 3
# Patterns that indicate connectivity loss (not a CAPTCHA or parse failure)
_NET_ERR_PATTERNS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_NETWORK_CHANGED",
)

# Retries when the CLAIMS server itself is unreachable (transient outage)
_CLAIMS_OUTAGE_RETRIES  = 3
_CLAIMS_OUTAGE_WAIT_S   = 300   # 5 minutes between outage retries


def _save_evictions_checkpoint(
    path: Path,
    all_cases: list[tuple],
    addr_data: dict[str, tuple],
    date_from: str,
    date_to: str,
) -> None:
    """Persist unprocessed cases + already-fetched addresses to a JSON checkpoint.

    'all_cases' is the full letter-sweep result so plaintiff counts remain
    accurate on resume.  'addr_data' contains addresses fetched so far so
    the resume run skips those detail pages.
    """
    unprocessed = [c for c in all_cases if c[0] not in addr_data]
    data = {
        "created_at": datetime.now().isoformat(),
        "date_from": date_from,
        "date_to": date_to,
        "total_cases": len(all_cases),
        "processed_count": len(addr_data),
        "pending_count": len(unprocessed),
        # Full case list preserved so plaintiff eviction_count is accurate on resume
        "all_cases": [list(c) for c in all_cases],
        # Addresses already fetched — resume skips these
        "addr_cache": {k: list(v) for k, v in addr_data.items()},
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(
        "[fjd_evictions] Checkpoint saved → %s  (%d pending / %d total)",
        path, len(unprocessed), len(all_cases),
    )


async def scrape_fjd_evictions(
    context: BrowserContext,
    lookback_days: int = PHILLY_FJD_CLAIMS_LOOKBACK_DAYS,
    checkpoint_file: Path | None = None,
    max_detail_pages: int | None = None,
) -> list[NoticeData]:
    """Scrape LT (Landlord/Tenant) filings from Philadelphia Municipal Court CLAIMS.

    Login flow:
      Image CAPTCHA at /captcha.png → 2Captcha image-to-text → inject solution
      → click "I Accept" → land on /phmuni/index.jsp.

    Search strategy:
      POST to /phmuni/cms/search.do with searchType=P, caseType=2 (LT),
      date range, and searchFor=<letter> for each letter a-z + 0-9.
      Single-letter LIKE searches cover all plaintiff names; case-number
      deduplication handles overlaps between letters.

    Pagination:
      Results come 100/page. Resubmit the same form with startRow incremented
      by 100 until a page returns fewer than _CLAIMS_PAGE_SIZE data rows.

    Columns from search: Case#, MatchingParty, Plaintiffs, Defendants + case detail URL.
    Phase 2 fetches each case docket page (one per-case image CAPTCHA via 2Captcha
    screenshot method) and parses the 'Property Address' from the Additional Information
    table.  Filing date is parsed from the case number: LT-YY-MM-DD-NNNN.
    """
    if not CAPTCHA_API_KEY:
        logger.warning("[fjd_evictions] CAPTCHA_API_KEY not set — skipping")
        return []

    # CLAIMS uses a self-signed certificate — create a dedicated context
    browser = context.browser
    if browser is None:
        logger.error("[fjd_evictions] Cannot access browser from context")
        return []

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    claims_ctx = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    page = await claims_ctx.new_page()

    try:
        # ── Phase 1 Login ──────────────────────────────────────────────────
        _resume = checkpoint_file and Path(checkpoint_file).exists()

        if _resume:
            # Resume mode: skip login + letter sweep, load previous state.
            ckpt = json.loads(Path(checkpoint_file).read_text(encoding="utf-8"))
            date_from = ckpt["date_from"]
            date_to   = ckpt["date_to"]
            raw_rows  = [tuple(c) for c in ckpt["all_cases"]]
            addr_data: dict[str, tuple[str, str, str, str]] = {
                k: tuple(v) for k, v in ckpt["addr_cache"].items()
            }
            addr_success = len(addr_data)
            logger.info(
                "[fjd_evictions] Resuming from checkpoint %s — %d/%d addresses already fetched",
                checkpoint_file, addr_success, len(raw_rows),
            )
        else:
            # Outer retry loop for transient server outages (ERR_CONNECTION_RESET etc.)
            logged_in = False
            network_error = False
            for outage_attempt in range(1, _CLAIMS_OUTAGE_RETRIES + 2):
                logged_in, network_error = await _claims_login(page, claims_ctx)
                if logged_in:
                    break
                if network_error and outage_attempt <= _CLAIMS_OUTAGE_RETRIES:
                    logger.warning(
                        "[fjd_evictions] CLAIMS server unreachable "
                        "(outage retry %d/%d) — waiting %ds before retry",
                        outage_attempt, _CLAIMS_OUTAGE_RETRIES, _CLAIMS_OUTAGE_WAIT_S,
                    )
                    await asyncio.sleep(_CLAIMS_OUTAGE_WAIT_S)
                    await page.close()
                    page = await claims_ctx.new_page()
                else:
                    break  # CAPTCHA failure (server up but CAPTCHA wrong) — no point waiting

            if not logged_in:
                if network_error:
                    logger.error(
                        "[fjd_evictions] CLAIMS server unreachable after %d retries — "
                        "evictions skipped for this run",
                        _CLAIMS_OUTAGE_RETRIES,
                    )
                else:
                    logger.error("[fjd_evictions] Failed to log in to CLAIMS after 5 attempts")
                return []

            # ── Phase 2 Letter sweep ────────────────────────────────────────
            date_from = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
            date_to   = datetime.now().strftime("%m/%d/%Y")
            logger.info("[fjd_evictions] Searching LT cases %s → %s", date_from, date_to)

            seen_cases: set[str] = set()
            raw_rows = []
            addr_data = {}
            addr_success = 0

            _CLAIMS_BASE = "https://fjdclaims.phila.gov"

            for letter in _CLAIMS_LETTERS:
                start_row = 1
                while True:
                    try:
                        await _claims_submit_search(page, letter, date_from, date_to, start_row)
                    except Exception:
                        logger.warning("[fjd_evictions] Search error letter=%r start=%d",
                                       letter, start_row, exc_info=True)
                        break

                    tbody_rows = await page.query_selector_all("tbody tr")
                    page_count = 0
                    new_this_page = 0
                    for row in tbody_rows:
                        try:
                            cells = await row.query_selector_all("td")
                            if len(cells) != 4:
                                continue
                            txts = [(await c.inner_text()).strip() for c in cells]
                            case_num = txts[0]
                            m = _CLAIMS_CASE_RE.match(case_num)
                            if not m:
                                continue
                            page_count += 1
                            if case_num in seen_cases:
                                continue
                            seen_cases.add(case_num)
                            new_this_page += 1
                            filed = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
                            plaintiffs = txts[2]
                            defendants = txts[3]
                            a_el = await cells[0].query_selector("a[href*='caseDocket']")
                            case_url = ""
                            if a_el:
                                href = await a_el.get_attribute("href")
                                if href:
                                    case_url = _CLAIMS_BASE + href
                            raw_rows.append((case_num, filed, plaintiffs, defendants, case_url))
                        except Exception:
                            logger.debug("[fjd_evictions] Row parse error", exc_info=True)

                    logger.info(
                        "[fjd_evictions] letter=%r start=%d  page_rows=%d  new=%d  total_unique=%d",
                        letter, start_row, page_count, new_this_page, len(seen_cases),
                    )
                    await _delay()

                    if page_count < _CLAIMS_PAGE_SIZE or new_this_page == 0:
                        break
                    start_row += _CLAIMS_PAGE_SIZE

            logger.info("[fjd_evictions] Letter sweep done: %d unique cases", len(raw_rows))

        # ── Phase 3 Detail page fetch → property address ───────────────────
        # max_detail_pages caps how many case dockets we fetch (useful for
        # micro/dry-runs where we only need N sample records).
        detail_rows = raw_rows[:max_detail_pages] if max_detail_pages else raw_rows
        if max_detail_pages:
            logger.info("[fjd_evictions] Detail page cap: %d/%d cases", len(detail_rows), len(raw_rows))

        # Checkpoint path: auto-generated on first run, re-used on resume.
        ckpt_path = (
            Path(checkpoint_file) if checkpoint_file
            else OUTPUT_DIR / f"fjd_evictions_checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        consecutive_net_errors = 0

        for i, (case_num, _filed, _pl, _df, case_url) in enumerate(detail_rows, 1):
            # Skip cases already fetched (relevant during resume)
            if case_num in addr_data:
                continue
            if not case_url:
                logger.debug("[fjd_evictions] No detail URL for %s", case_num)
                continue
            try:
                raw_addr = await _fetch_case_property_address(page, case_url, CAPTCHA_API_KEY)
                if raw_addr:
                    street, city, state, zip_ = _parse_claims_address(raw_addr)
                    addr_data[case_num] = (street, city, state, zip_)
                    addr_success += 1
                else:
                    logger.warning("[fjd_evictions] [%d/%d] %s — CAPTCHA failed or no address",
                                   i, len(raw_rows), case_num)
                consecutive_net_errors = 0   # reset on any non-network outcome
                if i % 50 == 0:
                    logger.info("[fjd_evictions] Detail pages: %d/%d  addr_ok=%d",
                                i, len(raw_rows), addr_success)
                await asyncio.sleep(1)
            except Exception as exc:
                err = str(exc)
                if any(pat in err for pat in _NET_ERR_PATTERNS):
                    consecutive_net_errors += 1
                    logger.warning(
                        "[fjd_evictions] Network error %d/%d on %s",
                        consecutive_net_errors, _CLAIMS_NET_ERR_THRESHOLD, case_num,
                    )
                    if consecutive_net_errors >= _CLAIMS_NET_ERR_THRESHOLD:
                        _save_evictions_checkpoint(ckpt_path, raw_rows, addr_data, date_from, date_to)
                        logger.error(
                            "[fjd_evictions] %d consecutive network errors — aborting detail phase. "
                            "Re-run with: --resume-from %s",
                            _CLAIMS_NET_ERR_THRESHOLD, ckpt_path,
                        )
                        break
                else:
                    consecutive_net_errors = 0
                    logger.warning("[fjd_evictions] Detail page error for %s", case_num, exc_info=True)

        logger.info(
            "[fjd_evictions] Property address extraction: %d/%d (%.0f%%)",
            addr_success, len(detail_rows),
            100 * addr_success / len(detail_rows) if detail_rows else 0,
        )

        # ── Phase 4 OPA parcel lookup by address ───────────────────────────
        # Sets notice.parcel_id so the orchestrator's enrich_and_filter can
        # apply Filter 2 (drop vacant/land) on the matched OPA category.
        parcel_cache: dict[str, str] = {}  # street+zip → parcel_number (de-dupes lookups)
        for case_num, (street, _city, _state, zip_) in addr_data.items():
            key = f"{street}|{zip_}"
            if key not in parcel_cache:
                parcel_cache[key] = await _opa_parcel_id_by_address(street, zip_)

        # ── Phase 5 Build notices ──────────────────────────────────────────
        from collections import Counter
        plaintiff_counts: Counter = Counter(r[2].upper() for r in raw_rows if r[2])

        notices: list[NoticeData] = []
        for case_num, filed, plaintiffs, defendants, case_url in raw_rows:
            primary_plaintiff = plaintiffs.split("\n")[0].strip()
            street, city, state, zip_ = addr_data.get(case_num, ("", "Philadelphia", "PA", ""))
            addr_ok = bool(street)
            pid = parcel_cache.get(f"{street}|{zip_}", "") if addr_ok else ""

            n = _philly_notice(
                _SOURCE_EV,
                date_added=filed,
                address=street,
                city=city,
                state=state,
                zip=zip_,
                owner_name=_title(primary_plaintiff),   # landlord
                parcel_id=pid,
                raw_text=(
                    f"Case: {case_num} | Filed: {filed} | "
                    f"Plaintiffs: {plaintiffs} | Defendants: {defendants}"
                ),
                source_url=case_url or _CLAIMS_SEARCH_URL,
            )
            _set_meta(n,
                      eviction_count_for_owner=plaintiff_counts.get(plaintiffs.upper(), 1),
                      claims_case_num=case_num,
                      property_address_extracted=addr_ok)
            notices.append(n)

        logger.info("[fjd_evictions] Total unique records: %d", len(notices))
        return notices

    finally:
        await claims_ctx.close()


def _parse_claims_row(texts: list[str]) -> tuple[str, str, str, str, str]:
    """Extract case#, filing date, plaintiff, defendant, and address from a CLAIMS row."""
    case_num = filed = plaintiff = defendant = address = ""
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
    lt_re = re.compile(r"LT-\d{2}-\d{2}-\d{2,}-\d{4}", re.IGNORECASE)
    addr_re = re.compile(r"\d{1,5}\s+\w")  # starts with house number

    for t in texts:
        t = t.strip()
        if lt_re.search(t) and not case_num:
            case_num = t
        elif date_re.match(t) and not filed:
            filed = t
        elif addr_re.match(t) and not address:
            address = t
        elif not plaintiff:
            plaintiff = t
        elif not defendant:
            defendant = t

    return case_num, filed, plaintiff, defendant, address


# ── SOURCE 6 — Probate / Estate Notices (Inquirer Marketplace) ────────

_SOURCE_PB = next(s for s in PHILLY_SOURCES if s.source_id == "inquirer_probate")

# Inquirer Marketplace confirmed selectors (live DOM inspection 2026-04-26).
# Card structure:
#   <div class="ap_ads_waterfall ap_even ap_first">
#     <div class="ap_ad_wrap" data-id="...">
#       <a href="/pa/estate-notice/SLUG/ID">
#         <div class="list-panel-info">
#           <div class="post-summary-title"><p class="desktop">LASTNAME, FIRSTNAME-- Executor; ...</p></div>
#           <p class="post-copy desktop">Full notice text. Ref# 205329</p>
#           <div class="post-summary-date">Posted Online N days ago</div>
#         </div>
#       </a>
#     </div>
#   </div>
_INQ_CARD_SEL = "[class*='ap_ads_waterfall']"
_INQ_TITLE_SEL = ".post-summary-title p.desktop, .post-summary-title p"
_INQ_DATE_SEL = ".post-summary-date"
_INQ_BODY_SEL = "p.post-copy.desktop, p.post-copy"
_INQ_LINK_SEL = "a[href*='/estate-notice/']"
_INQ_NEXT_SEL  = ".ap_paginator_next_page a"
# ap_page_link is on the <li>, not the <a> — target the anchor inside each
# numbered <li>, excluding the previous/next nav items.
_INQ_PAGES_SEL = (
    ".ap_paginator li.ap_page_link:not(.ap_paginator_next_page)"
    ":not(.ap_paginator_previous_page) a[data-page]"
)
# Ref# is embedded in the body text as "Ref# NNNNN" — no dedicated element
_INQ_REF_RE = re.compile(r"Ref#\s*(\d+)", re.IGNORECASE)


_PHILLY_ZIP_RE = re.compile(r"\b191\d{2}\b")


async def scrape_inquirer_probate(
    context: BrowserContext,
    lookback_days: int = PHILLY_INQUIRER_LOOKBACK_DAYS,
) -> list[NoticeData]:
    """Scrape estate/probate notices from the Philadelphia Inquirer Marketplace.

    Change 5:
      Filter 1 (geo): Keep only notices containing a 191xx ZIP code — either the
        executor's address or any address in the notice body.
      OPA match: Fuzzy name lookup against OPA owner_1/owner_2 (≥0.85 threshold).
        Tag opa_match=True/False. DO NOT drop on mismatch — keep all geo-filtered
        records. Filter 2 (houses) applied later via enrich_and_filter().
    Logs match rate at end.
    """
    page = await context.new_page()
    notices: list[NoticeData] = []

    logger.info("[inquirer_probate] Loading %s", PHILLY_INQUIRER_ESTATE_URL)
    try:
        await page.goto(PHILLY_INQUIRER_ESTATE_URL, wait_until="domcontentloaded", timeout=30_000)
        await _delay()
    except PwTimeout:
        logger.error("[inquirer_probate] Timeout loading Inquirer Marketplace")
        await page.close()
        return []

    cutoff_dt = datetime.now() - timedelta(days=lookback_days)

    # Wait for cards, then give JS time to render the paginator.
    try:
        await page.wait_for_selector(_INQ_CARD_SEL, timeout=15_000)
    except PwTimeout:
        await page.close()
        return []
    try:
        await page.wait_for_selector(".ap_paginator", timeout=5_000)
    except PwTimeout:
        pass  # single-page result — no paginator rendered

    # Read max data-page attribute from numbered <li> anchors.
    # ap_page_link is on the <li>, not the <a>; next/prev <li>s are excluded
    # by the selector so only integers appear here.
    total_pages = 1
    page_links = await page.query_selector_all(_INQ_PAGES_SEL)
    for lnk in page_links:
        dp = await lnk.get_attribute("data-page")
        try:
            n = int(dp or "")
            if n > total_pages:
                total_pages = n
        except ValueError:
            pass
    logger.info("[inquirer_probate] Total pages detected: %d", total_pages)

    async def _extract_page_cards(pg_num: int) -> tuple[list[NoticeData], bool]:
        """Extract all notices from the current page. Returns (notices, stop_flag)."""
        page_notices: list[NoticeData] = []
        stop = False
        cards = await page.query_selector_all(_INQ_CARD_SEL)
        logger.info("[inquirer_probate] %d cards on page %d", len(cards), pg_num)
        for card in cards:
            try:
                title_el = await card.query_selector(_INQ_TITLE_SEL)
                raw_title = (await title_el.inner_text()).strip() if title_el else ""

                date_el = await card.query_selector(_INQ_DATE_SEL)
                raw_date = (await date_el.inner_text()).strip() if date_el else ""
                notice_date = _parse_inq_date(raw_date)

                if notice_date and _to_dt(notice_date) < cutoff_dt:
                    stop = True
                    break

                body_el = await card.query_selector(_INQ_BODY_SEL)
                raw_body = (await body_el.inner_text()).strip() if body_el else ""

                has_philly_zip = bool(
                    _PHILLY_ZIP_RE.search(raw_body) or _PHILLY_ZIP_RE.search(raw_title)
                )

                link_el = await card.query_selector(_INQ_LINK_SEL)
                detail_href = await link_el.get_attribute("href") if link_el else ""
                notice_url = (
                    f"https://marketplace.inquirer.com{detail_href}"
                    if detail_href else page.url
                )

                decedent, pr_name = _parse_estate_title(raw_title, raw_body)

                n = _philly_notice(
                    _SOURCE_PB,
                    date_added=notice_date or _today(),
                    owner_name=pr_name,
                    decedent_name=decedent,
                    raw_text=f"{raw_title}\n{raw_body}",
                    source_url=notice_url,
                )
                _set_meta(n, has_philly_zip=has_philly_zip)
                page_notices.append(n)
            except Exception:
                logger.debug("[inquirer_probate] Card parse error", exc_info=True)
        return page_notices, stop

    # Page 1 is already loaded — extract it directly.
    page_notices, stop = await _extract_page_cards(1)
    notices.extend(page_notices)

    # Pages 2..N: navigate via ?p=N (URL-based, more reliable than clicking).
    for pg_num in range(2, total_pages + 1):
        if stop:
            break
        url = f"{PHILLY_INQUIRER_ESTATE_URL}?p={pg_num}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await _delay()
            await page.wait_for_selector(_INQ_CARD_SEL, timeout=15_000)
        except PwTimeout:
            logger.warning("[inquirer_probate] Timeout on page %d — stopping", pg_num)
            break
        page_notices, stop = await _extract_page_cards(pg_num)
        notices.extend(page_notices)

    await page.close()
    logger.info("[inquirer_probate] Raw (within lookback): %d notices", len(notices))

    # OPA fuzzy name match — tags opa_match in heir_map_json
    await _probate_opa_name_match(notices)

    # Filter 1: keep if executor address has 191xx ZIP *or* decedent matched an OPA Philly property.
    # Executor addresses are often suburban law offices — OPA match is the primary geo signal.
    before = len(notices)
    notices = [
        n for n in notices
        if _get_meta(n, "has_philly_zip") or _get_meta(n, "opa_match")
    ]
    logger.info(
        "[inquirer_probate] After Filter 1 (191xx ZIP OR opa_match): %d/%d kept",
        len(notices), before,
    )
    return notices


async def _probate_opa_name_match(notices: list[NoticeData]) -> None:
    """Fuzzy-match each decedent against OPA owner_1/owner_2. Tags opa_match in heir_map_json.

    Algorithm:
      1. Extract last-name token from decedent_name (last word of "FIRST LAST" form)
         AND first token (catches "LAST FIRST" OPA storage)
      2. Query OPA using both anchors against owner_1 AND owner_2 columns
      3. Score candidates with SequenceMatcher across name-ordering variants
      4. opa_match=True if any score ≥ 0.75 (lowered from 0.85 — accounts for
         FIRST/LAST ordering differences that reduce ratio to ~0.75)
    """
    _THRESHOLD = 0.75
    matched = skipped = failed = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        for n in notices:
            if not n.decedent_name:
                _set_meta(n, opa_match=False, opa_match_reason="no_decedent_name")
                skipped += 1
                continue

            parts = _normalize_name(n.decedent_name).split()
            # Use both first and last tokens as anchors — OPA stores names as
            # "LASTNAME FIRSTNAME" so either token may be the useful anchor
            anchors = list(dict.fromkeys(
                t for t in [parts[-1] if parts else "", parts[0] if parts else ""]
                if t and len(t) >= 3
            ))
            if not anchors:
                _set_meta(n, opa_match=False, opa_match_reason="name_too_short")
                skipped += 1
                continue

            # Build OR clause for all anchors × both OPA columns (owner_1 + owner_2)
            clauses = []
            for anchor in anchors:
                safe = anchor.replace("'", "''")
                clauses += [
                    f"UPPER(owner_1) LIKE '%{safe}%'",
                    f"UPPER(owner_2) LIKE '%{safe}%'",
                ]
            where = " OR ".join(clauses)
            query = (
                f"SELECT owner_1, owner_2, parcel_number, "
                f"category_code_description, market_value "
                f"FROM {PHILLY_OPA_TABLE} "
                f"WHERE {where} "
                f"LIMIT 30"
            )
            logger.debug(
                "[inquirer_probate] OPA query for '%s' | anchors=%s | where=...%s...",
                n.decedent_name, anchors, where[:80],
            )
            try:
                resp = await client.get(
                    PHILLY_CARTO_API_URL,
                    params={"q": query, "format": "json"},
                    timeout=10.0,
                )
                resp.raise_for_status()
                rows = resp.json().get("rows", [])
            except Exception:
                _set_meta(n, opa_match=False, opa_match_reason="opa_query_error")
                failed += 1
                logger.debug("[inquirer_probate] OPA query error for '%s'", n.decedent_name, exc_info=True)
                continue

            best_score = 0.0
            best_parcel = ""
            best_category = ""
            for row in rows:
                for col in ("owner_1", "owner_2"):
                    opa_name = str(row.get(col) or "")
                    if not opa_name:
                        continue
                    score = _best_name_score(n.decedent_name, opa_name)
                    if score > best_score:
                        best_score = score
                        best_parcel = str(row.get("parcel_number") or "")
                        best_category = str(row.get("category_code_description") or "")

            is_match = best_score >= _THRESHOLD
            _set_meta(
                n,
                opa_match=is_match,
                opa_match_score=round(best_score, 3),
                opa_match_parcel=best_parcel,
                opa_category=best_category,
            )
            if is_match:
                n.parcel_id = best_parcel
                n.property_type = best_category
                matched += 1
            else:
                logger.debug(
                    "[inquirer_probate] MISS '%s' | anchors=%s | best_opa=%r | score=%.3f",
                    n.decedent_name, anchors, best_parcel or "(none)", best_score,
                )

    total = len(notices)
    logger.info(
        "[inquirer_probate] OPA name match (threshold=%.2f): %d/%d matched (%.0f%%), "
        "%d skipped (no name), %d errors",
        _THRESHOLD, matched, total,
        100 * matched / total if total else 0, skipped, failed,
    )


def _parse_inq_date(raw: str) -> str:
    """Parse Inquirer date strings to YYYY-MM-DD.

    Handles all relative formats the Inquirer Marketplace uses:
      "Posted Online 4 hours ago"  → today
      "Posted Online today"        → today
      "Posted Online 1 day ago"    → yesterday
      "Posted Online N days ago"   → N days back
    Absolute date strings also accepted as fallback.
    """
    raw = raw.strip()

    # Hours ago = posted today
    if re.search(r"\d+\s+hours?\s+ago", raw, re.IGNORECASE):
        return _today()
    # Minutes / seconds ago = posted today
    if re.search(r"\d+\s+(?:minutes?|seconds?)\s+ago", raw, re.IGNORECASE):
        return _today()
    # N days ago
    rel_m = re.search(r"(\d+)\s+days?\s+ago", raw, re.IGNORECASE)
    if rel_m:
        return (datetime.now() - timedelta(days=int(rel_m.group(1)))).strftime("%Y-%m-%d")
    if re.search(r"\btoday\b", raw, re.IGNORECASE):
        return _today()
    if re.search(r"\byesterday\b", raw, re.IGNORECASE):
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Absolute formats — use full string, not truncated
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""  # unparseable → treat as no date (don't stop pagination)


def _to_dt(date_str: str) -> datetime:
    """Parse YYYY-MM-DD to datetime. Returns datetime.max on invalid input (keeps records)."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return datetime.max  # unknown date → always within lookback window


# "Estate of John A. Doe" / "Notice to Creditors – Estate of Jane Doe"
_ESTATE_OF_RE = re.compile(
    r"Estate\s+of\s+([A-Z][A-Za-z\s.,'\-]+?)(?:\s*,|\s*$|\s+Deceased|\s+Dec\.)",
    re.IGNORECASE,
)
# "Personal Representative: John Smith" / "Executor: Jane Doe"
_PR_TITLE_RE = re.compile(
    r"(?:Personal\s+Representative|Executor|Executrix|Administrator|Administratrix)"
    r"\s*[:\-]\s*([A-Z][A-Za-z\s.,]+?)(?:\s*,|\s*$|\s+\d|\s+of\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_estate_title(title: str, body: str) -> tuple[str, str]:
    """Extract (decedent_name, pr_name) from notice title + body.

    Handles all Inquirer separator styles (confirmed 2026-04-27):
      "CHOLAJ, TERESA S.-- George Cholaj, Executor"         double-hyphen
      "BUTLER, RONNIE – Ronnie Butler, Executor"            en-dash
      "BOYLE, BRIAN JOSEPH (a/k/a ...) – Carrie Boyle"     a/k/a + en-dash
      "BLOOD, FRANK A., JR. (a/k/a C...) – ..."            suffix + a/k/a

    Also handles the traditional "Estate of NAME, Deceased" format.
    """
    decedent = ""
    pr_name = ""
    combined = f"{title}\n{body}"

    # When the card title is truncated (ends in "..." or "…"), fall through
    # to the body’s first line which always contains the full notice text.
    _title = title.strip()
    if _title.endswith("...") or _title.endswith("…"):
        # Collapse full body to a single line so multi-line notices like:
        # "BURRELL, DARCENIA V. ----\nJohnnie Mae Thurmond, Executrix, ..."
        # are matched by the dash regex in one pass. Stop before "Ref#" lines.
        body_single = " ".join(
            l.strip() for l in body.splitlines()
            if l.strip() and not l.strip().startswith("Ref#")
        )
        if body_single:
            _title = body_single

    # ── Inquirer dash format (double-hyphen OR en-dash/em-dash) ──────
    # Pattern: DECEDENT_PART [optional (a/k/a ...)] DASH PR_PART
    # Separators: --, –, —, or multiple dashes (----).
    # Unicode ranges: en-dash U+2013, em-dash U+2014
    dash_m = re.match(
        r"^([A-Z][A-Z\s.,’’-]+?)"       # decedent (ALL CAPS section)
        r"(?:\s*\([^)]*\))*"             # optional (a/k/a ...) groups
        r"\s*(?:--+|–|—|-{1,4})\s*"     # separator: --, –, —, ----
        r"(.+)",                          # PR section
        _title,
    )
    if dash_m:
        raw_dec = dash_m.group(1).strip().rstrip(". ,")
        if "," in raw_dec:
            parts = raw_dec.split(",", 1)
            decedent = f"{parts[1].strip().title()} {parts[0].strip().title()}"
        else:
            decedent = raw_dec.title()

        pr_raw = dash_m.group(2).strip()
        pr_end_m = re.search(
            r",\s*(?:Executor|Executrix|Administrator|Administratrix|Personal\s+Rep)",
            pr_raw, re.IGNORECASE,
        )
        if pr_end_m:
            pr_name = pr_raw[: pr_end_m.start()].strip().title()
        else:
            pr_name = pr_raw.split(";")[0].split(",")[0].strip().title()

        return decedent, pr_name

    # ── Traditional "Estate of NAME" format ─────────────────────────
    m = _ESTATE_OF_RE.search(combined)
    if m:
        decedent = m.group(1).strip().title()

    m2 = _PR_TITLE_RE.search(combined)
    if m2:
        pr_name = m2.group(1).strip().title()

    return decedent, pr_name


# _opa_filter_probate and _opa_owns_property removed (Change 5).
# Replaced by _probate_opa_name_match (tag-not-drop, fuzzy matching).


# ── Orchestrator ───────────────────────────────────────────────────────


async def run_philly_scrape(
    source_ids: list[str] | None = None,
    lookback_days: int = 30,
    checkpoint_file: Path | None = None,
    evictions_max_detail: int | None = None,
) -> dict[str, Any]:
    """Run Philadelphia scrapers; return results + filter diagnostics.

    Default lookback changed from 7 → 30 days (Change 4).

    Returns dict with:
      results[source_id]      → list[NoticeData] after Filter 1 + Filter 2
      filter2_dropped[source_id] → int (records dropped as vacant/land)
    """
    enabled = [
        s for s in PHILLY_SOURCES
        if s.enabled and (source_ids is None or s.source_id in (source_ids or []))
    ]
    if not enabled:
        logger.warning("No enabled Philly sources match the requested IDs")
        return {"results": {}, "filter2_dropped": {}}

    raw: dict[str, list[NoticeData]] = {}

    # httpx sources — pure async HTTP, no Playwright
    _httpx_sources = {
        "li_violations":        scrape_li_violations,
        "opa_tax_delinquent":   scrape_opa_tax_delinquent,
        "li_imminently_dangerous": scrape_li_imminently_dangerous,
    }
    for sid, fn in _httpx_sources.items():
        if any(s.source_id == sid for s in enabled):
            logger.info("Running source: %s (httpx)", sid)
            try:
                raw[sid] = await fn(lookback_days)
            except Exception:
                logger.exception("Unhandled error in source %s", sid)
                raw[sid] = []

    # SOURCES 2, 3, 4 — Scrapfly (no Playwright needed)
    _scrapfly_sources = {
        "bid4assets_mortgage": scrape_bid4assets_mortgage,
        "bid4assets_tax":      scrape_bid4assets_tax,
    }
    for sid, fn in _scrapfly_sources.items():
        if any(s.source_id == sid for s in enabled):
            logger.info("Running source: %s (Scrapfly)", sid)
            try:
                raw[sid] = await fn()
            except Exception:
                logger.exception("Unhandled error in source %s", sid)
                raw[sid] = []

    if any(s.source_id == "fjd_lis_pendens" for s in enabled):
        logger.info("Running source: fjd_lis_pendens (Scrapfly)")
        try:
            raw["fjd_lis_pendens"] = await scrape_fjd_lis_pendens(lookback_days)
        except Exception:
            logger.exception("Unhandled error in source fjd_lis_pendens")
            raw["fjd_lis_pendens"] = []

    # SOURCES 5, 6 — Playwright (fjd_evictions, inquirer_probate)
    # Exclude sources already handled above (httpx + Scrapfly)
    _non_playwright_sids = set(_httpx_sources.keys()) | {
        "bid4assets_mortgage", "bid4assets_tax", "fjd_lis_pendens"
    }
    playwright_sources = [
        s for s in enabled
        if s.source_id not in _non_playwright_sids
    ]
    if playwright_sources:
        _UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(headless=True)
            context: BrowserContext = await browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 800},
            )
            context.set_default_timeout(30_000)

            for source in playwright_sources:
                logger.info("Running source: %s", source.source_id)
                try:
                    if source.source_id == "fjd_evictions":
                        raw[source.source_id] = await scrape_fjd_evictions(
                            context, lookback_days,
                            checkpoint_file=checkpoint_file,
                            max_detail_pages=evictions_max_detail,
                        )
                    elif source.source_id == "inquirer_probate":
                        raw[source.source_id] = await scrape_inquirer_probate(context, lookback_days)
                except Exception:
                    logger.exception("Unhandled error in source %s", source.source_id)
                    raw[source.source_id] = []

            await browser.close()

    # Change 2: OPA enrichment + Filter 2 (drop VACANT/LAND/EMPTY) for ALL sources.
    # Sources that have a parcel_id get batch-enriched; others get tagged opa_match=False.
    # Probate uses parcel_id set by _probate_opa_name_match — fallback to empty string.
    results: dict[str, list[NoticeData]] = {}
    filter2_dropped: dict[str, int] = {}

    # Sources that already ran OPA enrichment + filter internally — skip double-enrich.
    # li_imminently_dangerous calls enrich_and_filter internally.
    # opa_tax_delinquent IS the OPA dataset — no cross-reference needed.
    _opa_already_done = {"li_imminently_dangerous", "opa_tax_delinquent"}

    for sid, notices in raw.items():
        if not notices:
            results[sid] = []
            filter2_dropped[sid] = 0
            continue

        if sid in _opa_already_done:
            results[sid] = notices
            filter2_dropped[sid] = 0
            logger.info("[%s] Filter 2: skipped (OPA handled internally)", sid)
            continue

        before = len(notices)
        kept, dropped = await enrich_and_filter(notices, id_field="parcel_id")
        results[sid] = kept
        filter2_dropped[sid] = dropped
        logger.info(
            "[%s] Filter 2: %d before → %d after (%d dropped as vacant/land)",
            sid, before, len(kept), dropped,
        )

    return {"results": results, "filter2_dropped": filter2_dropped}
