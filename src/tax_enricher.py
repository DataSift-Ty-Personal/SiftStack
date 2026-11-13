"""Enrich notices with tax delinquency data from county property tax APIs.

Knox County:   https://knox-tn.mygovonline.com/api/v2
Blount County: https://blount-tn.mygovonline.com/api/v2

Both Trustees run the same vendor, so one contract covers both (verified live
2026-11-11): `/parcels/<query>` searches by street, owner or parcel and
`/due/PPT/<account>` returns that account's bills. Blount sat as a placeholder
here until then, so no Blount record ever carried a tax figure.

COUNTY tax only. City of Knoxville, Maryville and Alcoa bill their own property
tax separately; a parcel inside a city can owe there and read clean here.
"""

import logging
import random
import re
import time
from urllib.parse import quote

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

KNOX_API_BASE = "https://knox-tn.mygovonline.com/api/v2"
BLOUNT_API_BASE = "https://blount-tn.mygovonline.com/api/v2"
TAX_API_BY_COUNTY = {"knox": KNOX_API_BASE, "blount": BLOUNT_API_BASE}
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15


# Business entity pattern — imported from shared config
import config as _cfg
_BUSINESS_RE = _cfg.BUSINESS_RE


def detect_deceased_indicator(owner_name: str) -> str:
    """Detect deceased-owner indicators from a county tax API owner name.

    Returns one of: "life_estate", "personal_rep", "care_of", "et_al",
    "trustee", or "" (no indicator detected).

    Priority order reflects confidence level (highest first).
    """
    if not owner_name or not owner_name.strip():
        return ""

    upper = owner_name.upper()

    # 1. Personal Representative — strongest signal (definite estate)
    if "PERSONAL REPRESENTATIVE" in upper or "PERSONAL REP" in upper:
        return "personal_rep"

    # 2. Life Estate — very strong signal (elderly/deceased holder)
    if "LIFE EST" in upper:
        return "life_estate"

    # 3. Care-of (%) — strong signal for deceased/incapacitated
    if "%" in owner_name:
        return "care_of"

    # 4. Et Al — moderate signal (multiple parties, often heirs)
    if re.search(r"\bET\s+AL\b", upper):
        return "et_al"

    # 5. Trustee — weakest signal; skip business entities
    if re.search(r"\bTRUSTEE\b", upper):
        if not _BUSINESS_RE.search(upper):
            return "trustee"

    return ""


def _normalize_parcel_id(raw: str) -> str:
    """Normalize parcel ID to the format used by the tax API (with dash).

    KGIS returns e.g. '048DE017' but the tax API uses '048DE-017'.
    TPAD returns e.g. '048DE-017' already.
    """
    raw = raw.strip()
    if not raw:
        return ""
    # Already has a dash — return as-is
    if "-" in raw:
        return raw
    # Insert dash before the last 3 digits (common Knox parcel format)
    m = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return raw


def _knox_lookup_by_parcel(parcel_id: str, base: str = KNOX_API_BASE) -> dict | None:
    """Fetch tax bills for one county tax account.

    Returns dict with 'delinquent_amount', 'delinquent_years',
    'latitude', 'longitude', or None on failure. The Knox account is the
    parcel id with a dash; a Blount account is its own short number, so it is
    passed through untouched.
    """
    normalized = _normalize_parcel_id(parcel_id) if base == KNOX_API_BASE else str(parcel_id or "").strip()
    if not normalized:
        return None

    url = f"{base}/due/PPT/{quote(normalized)}?detail_level=public"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            logger.debug("Tax API: no data for parcel '%s'", normalized)
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Tax API request failed for parcel '%s': %s", normalized, e)
        return None

    try:
        data = resp.json()
        ppt = data.get("due", {}).get("PPT", {})
        bills = ppt.get("bills", [])
        result = _parse_delinquency(bills)

        # Extract lat/lon from parcels array (already in the response)
        parcels = ppt.get("parcels", [])
        if parcels:
            p = parcels[0]
            if p.get("latitude") is not None:
                result["latitude"] = str(p["latitude"])
            if p.get("longitude") is not None:
                result["longitude"] = str(p["longitude"])

        return result
    except (ValueError, KeyError) as e:
        logger.debug("Tax API parse error for parcel '%s': %s", normalized, e)
        return None


def _street_queries(address: str) -> list[str]:
    """The street line as written, then the way the county writes it.

    The county search is literal about the suffix: "100 Example Street" finds
    nothing while "100 EXAMPLE ST" finds the parcel. Measured 2026-11-11 on 51
    Knox foreclosure addresses: 22 matched as written, 50 once the suffix and
    any direction were abbreviated. Only the TRAILING token is treated as a
    suffix, so a street named "Ridge Road" is searched as "RIDGE RD", never
    "RDG RD". The last form drops the suffix for a notice that got it wrong.
    """
    from address_fallback import _DIRECTION, _SUFFIX, _UNIT_RE

    line = (address or "").split(",")[0]
    line = _UNIT_RE.sub("", line.strip())
    parts = re.sub(r"[^A-Za-z0-9\s]", " ", line).split()
    queries = [(address or "").strip()]
    if len(parts) >= 2:
        body = [_DIRECTION.get(t.lower(), t) for t in parts[1:]]
        suffixed = body[:-1] + [_SUFFIX.get(body[-1].lower(), body[-1])]
        queries.append(" ".join([parts[0]] + suffixed).upper())
        if body[-1].lower() in _SUFFIX and len(body) >= 2:
            queries.append(" ".join([parts[0]] + body[:-1]).upper())
        # Some rolls print the house number LAST ("EXAMPLE DR 100").
        queries.append(" ".join(suffixed + [parts[0]]).upper())
    seen, out = set(), []
    for q in queries:
        if q and q.upper() not in seen:
            seen.add(q.upper())
            out.append(q)
    return out


def _roll_street_key(parcel_address: str) -> tuple[str, frozenset]:
    """street_key for an address as a county roll prints it.

    Tolerates the house number printed last ("EXAMPLE DR 100"), which Blount
    does on part of its roll.
    """
    from address_fallback import street_key

    key = street_key(parcel_address)
    if key[0]:
        return key
    parts = (parcel_address or "").split()
    if len(parts) >= 2 and parts[-1].isdigit():
        return street_key(" ".join(parts[-1:] + parts[:-1]))
    return key


def _knox_lookup_by_address(address: str, base: str = KNOX_API_BASE) -> dict | None:
    """Search a county's tax records by address, then fetch bills.

    Returns dict with 'delinquent_amount', 'delinquent_years', and
    'account_number', or None when no parcel AT THIS ADDRESS is found.

    The county search is fuzzy and relevance-ranked, so the first result is
    often a different house. This used to take it anyway, which writes one
    property's tax debt onto another (3 of 51 on the 2026-11-11 measurement
    would have). A parcel now counts only when its own address has the same
    house number and street as the one asked for.
    """
    from address_fallback import street_key

    if not address or not address.strip():
        return None
    want = street_key(address.split(",")[0])
    if not want[0]:
        return None   # no house number, so nothing to verify a result against

    for query in _street_queries(address):
        search_url = f"{base}/parcels/{quote(query)}?detail_level=public&start=0&length=10"
        try:
            resp = requests.get(search_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            parcels = resp.json().get("parcels", []) or []
        except (requests.RequestException, ValueError) as e:
            logger.debug("Tax API address search failed for '%s': %s", query, e)
            continue

        for parcel in parcels:
            account = parcel.get("account_number", "")
            if not account or _roll_street_key(parcel.get("parcel_address") or "") != want:
                continue
            # Small delay before the bill lookup
            time.sleep(random.uniform(0.5, 1.0))
            result = _knox_lookup_by_parcel(str(account), base)
            if result:
                result["account_number"] = account
                return result
        time.sleep(random.uniform(0.3, 0.6))

    logger.debug("Tax API: no parcel at '%s'", address)
    return None


def _parse_delinquency(bills: list[dict]) -> dict:
    """Parse bill list to extract delinquent amount and years count.

    A bill is delinquent if delinquent=True AND paid=False.
    """
    delinquent_amount = 0.0
    delinquent_years = 0
    year_list: list[str] = []

    for bill in bills:
        is_delinquent = bill.get("delinquent", False)
        is_paid = bill.get("paid", True)
        due = bill.get("due", 0) or 0

        if is_delinquent and not is_paid and due > 0:
            delinquent_amount += due
            delinquent_years += 1
            year = str(bill.get("taxYear") or "").strip()
            if year:
                year_list.append(year)

    return {
        "delinquent_amount": round(delinquent_amount, 2),
        "delinquent_years": delinquent_years,
        # WHICH years, oldest first. The current year's bill is unpaid for most
        # owners from October to February and is not delinquent, so it never
        # appears here.
        "delinquent_year_list": sorted(set(year_list)),
    }


def _knox_parcel_address(parcel_id: str) -> dict | None:
    """Look up official street address and owner for a Knox County parcel ID.

    Uses the parcels search endpoint which returns parcel_address and owner.
    Returns dict with 'address' and 'owner', or None if not found.
    """
    normalized = _normalize_parcel_id(parcel_id)
    if not normalized:
        return None

    url = f"{KNOX_API_BASE}/parcels/{quote(normalized)}?detail_level=public&start=0&length=1"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Parcel address lookup failed for '%s': %s", normalized, e)
        return None

    try:
        data = resp.json()
        parcels = data.get("parcels", [])
        if not parcels:
            return None
        p = parcels[0]
        addr = p.get("parcel_address", "").strip()
        owner = p.get("owner", "").strip()
        return {"address": addr or None, "owner": owner or None}
    except (ValueError, KeyError):
        return None


def lookup_parcel_addresses(notices: list[NoticeData]) -> None:
    """Replace OCR addresses with official county addresses from parcel IDs.

    For Knox County tax_sale notices that have a parcel_id, looks up the
    official parcel_address from the county API. This fixes garbled OCR
    addresses before Smarty standardization runs.

    Updates notices in-place.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "knox" and n.parcel_id.strip()
    ]

    if not candidates:
        logger.info("No Knox County notices with parcel IDs for address lookup")
        return

    logger.info("Looking up official addresses for %d parcels...", len(candidates))

    updated = 0
    failed = 0

    for i, notice in enumerate(candidates, 1):
        result = _knox_parcel_address(notice.parcel_id)

        if result:
            # Store raw tax API owner name and detect deceased indicators
            raw_owner = result.get("owner", "")
            if raw_owner:
                notice.tax_owner_name = raw_owner.strip()
                notice.deceased_indicator = detect_deceased_indicator(raw_owner)

            # Extract owner name if available and notice doesn't have one
            if raw_owner and not notice.owner_name.strip():
                notice.owner_name = raw_owner.title()

            api_addr = result.get("address")
            if api_addr:
                old_addr = notice.address
                # Title-case the API address (comes back ALL CAPS)
                new_addr = api_addr.title()
                if old_addr.lower().strip() != new_addr.lower().strip():
                    # Don't replace an address that has a house number with one
                    # that doesn't — the OCR version is more useful for Smarty
                    old_has_number = bool(re.match(r"\d", old_addr.strip()))
                    new_has_number = bool(re.match(r"\d", new_addr.strip()))
                    if old_has_number and not new_has_number:
                        logger.debug(
                            "  [%d/%d] %s: keeping OCR address '%s' (API has no house #: '%s')",
                            i, len(candidates), notice.parcel_id, old_addr, new_addr,
                        )
                    else:
                        notice.address = new_addr
                        updated += 1
                        logger.debug(
                            "  [%d/%d] %s: '%s' → '%s'",
                            i, len(candidates), notice.parcel_id, old_addr, new_addr,
                        )
                else:
                    logger.debug("  [%d/%d] %s: address matches", i, len(candidates), notice.parcel_id)
        else:
            failed += 1

        if i % 50 == 0:
            logger.info("Parcel address progress: %d/%d (updated=%d, failed=%d)",
                        i, len(candidates), updated, failed)

        # Rate limit
        time.sleep(random.uniform(0.5, 1.0))

    logger.info(
        "Parcel address lookup complete: %d updated, %d failed, %d unchanged",
        updated, failed, len(candidates) - updated - failed,
    )


def enrich_tax_delinquency(notices: list[NoticeData]) -> None:
    """Enrich notices with tax delinquency data from county APIs.

    Updates notices in-place, setting tax_delinquent_amount and
    tax_delinquent_years fields.

    Knox and Blount. A record the county has no parcel for at that address is
    counted as failed and left blank: blank means "not checked", never "owes
    nothing".
    """
    knox_notices = [
        n for n in notices
        if (n.county or "").lower() in TAX_API_BY_COUNTY
        and ((n.parcel_id or "").strip() or (n.address or "").strip())
    ]

    if not knox_notices:
        logger.info("No Knox or Blount notices with addresses/parcels to enrich")
        return

    logger.info("Enriching tax delinquency for %d Knox/Blount notices...", len(knox_notices))

    enriched = 0
    failed = 0
    skipped = 0

    for i, notice in enumerate(knox_notices, 1):
        result = None
        county = notice.county.lower()
        base = TAX_API_BY_COUNTY[county]
        parcel_id = (notice.parcel_id or "").strip()
        address = (notice.address or "").strip()

        # Prefer parcel_id lookup (faster, more precise). Knox only: a Knox
        # account IS the parcel id, a Blount account is a separate number.
        if county == "knox" and parcel_id:
            result = _knox_lookup_by_parcel(parcel_id)

        # Fallback to address search
        if result is None and address:
            result = _knox_lookup_by_address(address, base)
            # Store the discovered parcel_id if we didn't have one
            if county == "knox" and result and not parcel_id and result.get("account_number"):
                notice.parcel_id = result["account_number"]

        if result:
            notice.tax_delinquent_amount = str(result["delinquent_amount"]) if result["delinquent_amount"] > 0 else ""
            notice.tax_delinquent_years = str(result["delinquent_years"]) if result["delinquent_years"] > 0 else ""

            # Store lat/lon from the API (used for reverse geocoding later)
            if result.get("latitude") and not notice.latitude:
                notice.latitude = result["latitude"]
            if result.get("longitude") and not notice.longitude:
                notice.longitude = result["longitude"]

            enriched += 1
            if result["delinquent_years"] > 0:
                logger.debug(
                    "  [%d/%d] %s: $%.2f delinquent, %d years",
                    i, len(knox_notices), notice.address or notice.parcel_id,
                    result["delinquent_amount"], result["delinquent_years"],
                )
        else:
            failed += 1

        if i % 20 == 0:
            logger.info("Tax enrichment progress: %d/%d (enriched=%d, failed=%d)",
                        i, len(knox_notices), enriched, failed)

        # Rate limit
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    skipped = len(notices) - len(knox_notices)

    logger.info(
        "Tax delinquency enrichment complete: %d enriched, %d failed, %d skipped",
        enriched, failed, skipped,
    )


def _name_match_score(search_name: str, api_owner: str) -> float:
    """Score how well a search name matches an API owner name.

    Knox Tax API returns "LAST FIRST MIDDLE" format.
    Search name is "FIRST MIDDLE LAST" format.
    Returns 0.0-1.0 based on token overlap.
    """
    search_tokens = set(search_name.upper().split())
    api_tokens = set(api_owner.upper().split())
    # Remove common noise
    noise = {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    search_tokens -= noise
    api_tokens -= noise
    if not search_tokens or not api_tokens:
        return 0.0
    overlap = search_tokens & api_tokens
    return len(overlap) / max(len(search_tokens), len(api_tokens))


def _clean_name_for_search(name: str) -> list[str]:
    """Generate search variations for a name.

    Knox Tax API stores names as "LAST FIRST MIDDLE". We try multiple formats.
    Returns a list of search strings to try in order.
    """
    # Remove suffixes
    suffixes_re = re.compile(r"\b(JR|SR|II|III|IV|ESQ)\b\.?", re.IGNORECASE)
    clean = suffixes_re.sub("", name).strip()
    clean = re.sub(r"\s+", " ", clean)  # collapse whitespace
    clean = re.sub(r",\s*$", "", clean)  # trailing comma

    parts = clean.split()
    if not parts:
        return [name]

    searches = []

    # Original name as-is
    searches.append(name.strip())

    # Without suffixes
    if clean != name.strip():
        searches.append(clean)

    # "LAST FIRST" format (how Knox API stores names)
    if len(parts) >= 2:
        searches.append(f"{parts[-1]} {parts[0]}")

    # "LAST FIRST MIDDLE" if 3+ parts
    if len(parts) >= 3:
        searches.append(f"{parts[-1]} {' '.join(parts[:-1])}")

    # First + Last only (drop middle)
    if len(parts) >= 3:
        searches.append(f"{parts[0]} {parts[-1]}")

    return list(dict.fromkeys(searches))  # dedupe preserving order


def _knox_name_search(name: str, min_score: float = 0.4) -> list[tuple[float, dict]]:
    """Search Knox Tax API by name, return scored results above threshold."""
    search_url = (
        f"{KNOX_API_BASE}/parcels/{quote(name)}"
        f"?detail_level=public&start=0&length=10"
    )
    try:
        resp = requests.get(search_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except requests.RequestException:
        return []

    try:
        data = resp.json()
        parcels = data.get("parcels", [])
        scored = []
        for p in parcels:
            owner = p.get("owner", "")
            score = _name_match_score(name, owner)
            if score >= min_score:
                scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored
    except (ValueError, KeyError):
        return []


def _people_search_property(name: str, city: str = "Knoxville", state: str = "TN") -> str | None:
    """Search people search sites for a person's property address.

    Uses TruePeopleSearch/FastPeopleSearch via the obituary enricher's
    existing infrastructure. Returns address string or None.
    """
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page
        urls = _build_people_search_urls(name, city)
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page(url)
            if not text or len(text) < 100:
                continue
            # Look for address patterns near the name in the text
            # People search pages list current/past addresses
            import re as _re
            # Look for Knox County addresses (37xxx ZIP codes)
            addr_pattern = _re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Pike|Trl|Loop|Run|Ter|Pkwy))"
                r"[,.\s]+(?:Knoxville|Knox)",
                _re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                # Return the first (usually current) address
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  People search property lookup failed: %s", e)
    return None


def _usable_parcels(results: list[tuple[float, dict]], decedent: str) -> list[tuple[float, dict]]:
    """Keep only parcels that can stand in for the estate's property.

    Two requirements, each from a live failure (2026-11-11):

    A STREET ADDRESS. The county API also returns business and personal
    property accounts, whose parcel_address is null. One of those was applied
    to a notice as address=None, and every later step that calls .strip() on
    an address then crashed, taking a 103-notice run down with it.

    THE DECEDENT ON THE TITLE. A token-overlap score of 0.4 is a surname-only
    match on a two-word name, and the old executor tier accepted any parcel
    whose owner string contained the decedent's surname. When the executor is
    a child with the same surname, that is the executor's own house. Every
    executor-tier match in the September 2026 Knox run was one of those.
    """
    from property_lookup import _owner_matches_decedent

    kept = []
    for score, parcel in results:
        if not (parcel.get("parcel_address") or "").strip():
            continue
        if not _owner_matches_decedent(decedent, parcel.get("owner") or ""):
            continue
        kept.append((score, parcel))
    return kept


def _probate_property_lookup(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for probate records without addresses.

    Tier 1: Knox Tax API by decedent name (multiple search variations)
    Tier 2: Knox Tax API by executor name, for a parcel the decedent co-owns
    Tier 3: People search for decedent's last known address

    One record that raises is logged and skipped; it never stops the rest.
    """
    for notice in notices:
        try:
            _probate_property_lookup_one(notice)
        except Exception as e:
            logger.warning("  Property lookup failed for one probate record: %s", e)


def _probate_property_lookup_one(notice: NoticeData) -> None:
    if (notice.address or "").strip():
        return
    decedent = (notice.decedent_name or "").strip()
    if not decedent:
        return
    executor = (notice.owner_name or "").strip()
    logger.info("  Looking up property for decedent: %s", decedent)

    # ── Tier 1: Knox Tax API by decedent name variations ──
    best_match = None
    rejected = 0
    for search_name in _clean_name_for_search(decedent):
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        raw = _knox_name_search(search_name, min_score=0.4)
        results = _usable_parcels(raw, decedent)
        rejected += len(raw) - len(results)
        if results:
            if not best_match or results[0][0] > best_match[0]:
                best_match = results[0]
            if results[0][0] >= 0.6:
                break  # good enough, stop searching

    if best_match:
        score, parcel = best_match
        logger.info(
            "  Tier 1 (Tax API): %s (owner: %s, score: %.2f)",
            parcel.get("parcel_address", ""), parcel.get("owner", ""), score,
        )
        _apply_parcel_to_notice(notice, parcel)
        return

    # ── Tier 2: Knox Tax API by executor name ──
    # Finds a parcel the decedent holds jointly with the executor when the
    # decedent-name search missed it. The executor's own home is not the
    # estate's property, so the decedent must be on the title here too.
    if executor:
        for search_name in _clean_name_for_search(executor)[:3]:
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            raw = _knox_name_search(search_name, min_score=0.4)
            results = _usable_parcels(raw, decedent)
            rejected += len(raw) - len(results)
            if results:
                score, parcel = results[0]
                logger.info(
                    "  Tier 2 (Executor, decedent on title): %s (owner: %s, score: %.2f)",
                    parcel.get("parcel_address", ""), parcel.get("owner", ""), score,
                )
                _apply_parcel_to_notice(notice, parcel)
                return

    # ── Tier 3: People search for decedent's property address ──
    logger.info("  Tier 3: People search for %s", decedent)
    people_addr = _people_search_property(decedent, city="Knoxville")
    if people_addr:
        logger.info("  Tier 3 (People Search): %s", people_addr)
        notice.address = people_addr
        notice.city = "Knoxville"
        notice.state = "TN"
        return

    logger.warning(
        "  No property found for decedent: %s (all tiers exhausted, "
        "%d county results not owned by the decedent)", decedent, rejected,
    )


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict) -> None:
    """Apply parcel data from Knox Tax API to a notice."""
    notice.address = (parcel.get("parcel_address") or "").strip()
    notice.city = "Knoxville"
    notice.state = "TN"
    account = parcel.get("account_number", "")
    if account:
        notice.parcel_id = account
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
