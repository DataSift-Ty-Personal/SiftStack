"""Resolve Nebraska property addresses by owner name (Douglas + Sarpy county GIS).

Several emailed distress lists give an owner name but NO street address:
  - Sarpy foreclosure NOD (only owner + subdivision/lot)
  - Douglas tax-sale (DSL) (only owner + tax cert #)
Probate records (owner = PR, decedent named) have the same gap.

This module searches a county's public parcel service by owner name, scores the
candidates by token overlap against our owner name, and writes the best match's
street address / city / zip / parcel_id back onto the NoticeData.

The parcel data source is a county ArcGIS REST parcel layer (a plain HTTP JSON
`/query` endpoint — no browser, no auth). Endpoints + field names are configured
in `NE_PARCEL_SERVICES`; when unconfigured the lookup no-ops (logs a warning) so
the pipeline degrades gracefully.
"""

import logging
import random
import re
import time
from urllib.parse import quote

import requests

import config

logger = logging.getLogger(__name__)

# Per-county public ArcGIS REST parcel layers (verified live — plain JSON, no token).
# `url` is the layer base; the code appends `/query`. `owner_sep` is the character
# separating LAST from FIRST in the owner field (Douglas space, Sarpy slash).
# Situs address is either separate fields (Douglas) or one combined field (Sarpy).
NE_PARCEL_SERVICES: dict[str, dict] = {
    "douglas": {
        # DOGIS ArcGIS Server 11.5 (dcgis.org). gis.douglascounty-ne.gov is WAF-blocked.
        "url": "https://dcgis.org/server/rest/services/vector/Parcels_public/FeatureServer/0",
        "owner_field": "OWNER_NAME",   # "LAST FIRST MIDDLE" (UPPER, space-separated)
        "owner_sep": " ",
        "parcel_field": "PIN",
        "addr_field": "PROPERTY_A",     # situs street
        "city_field": "PROP_CITY",
        "zip_field": "PROP_ZIP",
        # Fallback situs street from components when PROPERTY_A is blank.
        "addr_component_fields": ["HOUSE", "STREET_DIR", "STREET_NAM", "STREET_TYP", "APARTMENT"],
    },
    "sarpy": {
        # Sarpy AGOL-hosted parcels (CORS-friendly, no token).
        "url": "https://services.arcgis.com/OiG7dbwhQEWoy77N/arcgis/rest/services/Sarpy_Parcels_WFL1/FeatureServer/0",
        "owner_field": "OWNERNME1",     # "LAST/FIRST MIDDLE" (UPPER, SLASH-separated)
        "owner_sep": "/",
        "parcel_field": "PARCELID",
        # SITEADDRESS is one combined "STREET  CITY STATE [ZIP]" string (double-space).
        "situs_combined_field": "SITEADDRESS",
        # Mailing fields, used only as a city/zip fallback if the combined parse fails.
        "city_field": "PSTLCITY",
        "zip_field": "PSTLZIP5",
    },
}

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SiftStack/1.0; +https://datasift.ai)"
}
_REQUEST_TIMEOUT = 30

# Suffixes / role markers to drop before building a search term.
_STRIP_TOKENS = {
    "TR", "TRUSTEE", "TRUSTEES", "TTEE", "JR", "SR", "II", "III", "IV",
    "ET", "AL", "ETAL",
}


# ── Name → search terms ────────────────────────────────────────────────


def _tokens(name: str) -> list[str]:
    """Uppercase alphanumeric tokens of a name, role/suffix words removed."""
    raw = re.sub(r"[^A-Za-z0-9\- ]", " ", name or "").upper()
    out = []
    for tok in raw.replace("-", " ").split():
        if tok in _STRIP_TOKENS:
            continue
        out.append(tok)
    return out


def _is_entity(name: str) -> bool:
    return bool(config.BUSINESS_RE.search(name or ""))


def _primary_owner(name: str) -> str:
    """Return just the first owner from a joint 'A & B' / 'A AND B' owner string.

    Co-owner tokens otherwise pollute both the search term and the score.
    """
    return re.split(r"\s*&\s*|\s+AND\s+", name or "", maxsplit=1, flags=re.IGNORECASE)[0].strip()


def owner_search_terms(owner_name: str, sep: str = " ") -> list[str]:
    """Candidate LIKE terms for a parcel-service owner search, most specific first.

    Our owner_name arrives as 'First [Middle] Last' (or an entity name). County
    parcel layers store owners 'LAST<sep>FIRST' (Douglas sep=' ', Sarpy sep='/').
    We search the precise 'LAST<sep>FIRST' substring first, then fall back to the
    bare surname; scoring disambiguates the surname-only results.
    """
    name = (owner_name or "").strip()
    if not name:
        return []

    if _is_entity(name):
        toks = _tokens(name)
        # Drop the trailing entity marker for the search term (LLC/INC/etc.)
        core = [t for t in toks if not config.BUSINESS_RE.fullmatch(t)]
        terms = []
        if len(core) >= 2:
            terms.append(" ".join(core[:2]))  # e.g. "INCEPTION REALTY"
        if core:
            terms.append(core[0])
        return list(dict.fromkeys(terms))  # de-dup, keep order

    toks = _tokens(name)
    if not toks:
        return []
    last = toks[-1]
    first = toks[0]
    terms = [f"{last}{sep}{first}", last]  # "LAST<sep>FIRST" then just "LAST"
    return list(dict.fromkeys(terms))


# ── Match scoring ──────────────────────────────────────────────────────


def score_owner_match(query_owner: str, result_owner: str) -> float:
    """Token-overlap score in [0,1] of a candidate owner vs. our owner name.

    Requires the surname token to be present (else 0.0), then scores by the
    fraction of our name's tokens found in the candidate. Single-letter middle
    initials are ignored so 'David W Rasmussen' vs 'RASMUSSEN DAVID' still scores 1.0.
    """
    q = [t for t in _tokens(query_owner) if len(t) > 1]
    r = set(_tokens(result_owner))
    if not q or not r:
        return 0.0

    if _is_entity(query_owner):
        # Entity: overlap of the core words (already excludes LLC/INC via _tokens? no)
        q_core = [t for t in q if not config.BUSINESS_RE.fullmatch(t)]
        if not q_core:
            q_core = q
        inter = [t for t in q_core if t in r]
        return len(inter) / len(q_core)

    surname = _tokens(query_owner)[-1] if _tokens(query_owner) else ""
    if surname and surname not in r:
        return 0.0
    inter = [t for t in q if t in r]
    return len(inter) / len(q)


def select_best_parcel(
    query_owner: str,
    candidates: list[dict],
    min_score: float = 0.4,
) -> tuple[dict, float, int] | None:
    """Pick the best-scoring parcel candidate (>= min_score).

    Each candidate dict must have keys: owner, address (others optional).
    Returns (candidate, score, n_tied) or None, where n_tied is the number of
    DISTINCT-address candidates sharing the top score (>1 = ambiguous, e.g. a
    common name matched several different properties). Rejects addressless candidates.
    """
    scored = []
    for c in candidates:
        if not (c.get("address") or "").strip():
            continue
        s = score_owner_match(query_owner, c.get("owner", ""))
        if s >= min_score:
            scored.append((c, s))
    if not scored:
        return None
    scored.sort(key=lambda cs: cs[1], reverse=True)
    top = scored[0][1]
    tied_addrs = {
        (c.get("address") or "").strip().lower()
        for c, s in scored if abs(s - top) < 1e-9
    }
    return scored[0][0], top, len(tied_addrs)


# ── Parcel service query (ArcGIS REST) ─────────────────────────────────


def query_parcels_by_owner(county: str, term: str) -> list[dict]:
    """Query a county parcel layer for owners matching `term` (a LIKE substring).

    Returns a list of normalized dicts: {owner, address, city, zip, parcel_id}.
    No-ops (returns []) when the county's service is not yet configured.
    """
    cfg = NE_PARCEL_SERVICES.get(county.lower()) or {}
    url = cfg.get("url")
    owner_field = cfg.get("owner_field")
    if not url or not owner_field:
        logger.debug("No parcel service configured for %s — skipping", county)
        return []

    term_esc = term.replace("'", "''")
    where = f"UPPER({owner_field}) LIKE '%{term_esc.upper()}%'"
    query_url = (
        f"{url}/query?where={quote(where)}"
        f"&outFields=*&returnGeometry=false&resultRecordCount=50&f=json"
    )
    try:
        resp = requests.get(query_url, headers=_HTTP_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Parcel query failed (%s, %r): %s", county, term, e)
        return []

    if "error" in data:
        logger.warning("Parcel service error (%s): %s", county, data.get("error"))
        return []

    results = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        address, city, zipc = _extract_situs(attrs, cfg)
        results.append(
            {
                "owner": str(attrs.get(owner_field, "") or "").strip(),
                "address": address,
                "city": city,
                "zip": zipc,
                "parcel_id": str(attrs.get(cfg.get("parcel_field", ""), "") or "").strip(),
            }
        )
    logger.debug("Parcel query %s %r → %d candidates", county, term, len(results))
    return results


def query_parcels_by_address(county: str, street: str) -> list[dict]:
    """Query a county parcel layer by SITUS address → {owner, address, city, zip, parcel_id}.

    Used to resolve the OWNER for records that have an address but no owner
    (e.g. Omaha code violations). No-ops when the county isn't configured.
    """
    cfg = NE_PARCEL_SERVICES.get(county.lower()) or {}
    url = cfg.get("url")
    addr_field = cfg.get("addr_field") or cfg.get("situs_combined_field")
    owner_field = cfg.get("owner_field")
    if not url or not addr_field or not owner_field:
        return []

    term_esc = (street or "").upper().replace("'", "''").strip()
    if not term_esc:
        return []
    where = f"UPPER({addr_field}) LIKE '%{term_esc}%'"
    query_url = (
        f"{url}/query?where={quote(where)}"
        f"&outFields=*&returnGeometry=false&resultRecordCount=25&f=json"
    )
    try:
        resp = requests.get(query_url, headers=_HTTP_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Parcel address query failed (%s, %r): %s", county, street, e)
        return []
    if "error" in data:
        return []
    results = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        address, city, zipc = _extract_situs(attrs, cfg)
        results.append({
            "owner": str(attrs.get(owner_field, "") or "").strip(),
            "address": address, "city": city, "zip": zipc,
            "parcel_id": str(attrs.get(cfg.get("parcel_field", ""), "") or "").strip(),
        })
    return results


def _format_owner(raw: str) -> str:
    """Format a parcel OWNER_NAME for display. 'BUEIDE ANDREA' → 'Andrea Bueide';
    entities (LLC/INC/…) keep word order and short acronyms."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    def cap(w):
        return w if (w.isupper() and len(w) <= 3) else "-".join(p.capitalize() for p in w.split("-"))
    if config.BUSINESS_RE.search(raw):
        return " ".join(cap(w) for w in raw.split())
    toks = raw.replace(",", " ").split()
    if len(toks) >= 2:  # county parcels store owners "LAST FIRST [MID]"
        toks = toks[1:] + toks[:1]
    return " ".join(cap(w) for w in toks)


def _addr_terms(street: str) -> list[str]:
    """Address LIKE variants: full, then without the street-type suffix."""
    s = re.sub(r"\s+", " ", (street or "").upper()).strip()
    if not s:
        return []
    terms = [s]
    s2 = re.sub(r"\s+(ST|STREET|AVE?|AVENUE|DR|DRIVE|BLVD|CIR|CIRCLE|CT|COURT|LN|LANE|"
                r"RD|ROAD|PL|PLACE|PKWY|WAY|TER|TERRACE|TRL|LOOP|PT|HWY)\.?$", "", s)
    if s2 != s and s2:
        terms.append(s2)
    return terms


def resolve_owners(notices: list) -> int:
    """Fill owner_name (+ parcel) for NE records that have an address but no owner.

    Queries the county parcel layer by situs address. Modifies notices in place;
    returns the count resolved. Targets e.g. Omaha code-violation records.
    """
    targets = [
        n for n in notices
        if n.county.lower() in _SUPPORTED
        and (n.address or "").strip()
        and not (n.owner_name or "").strip()
    ]
    if not targets:
        return 0
    logger.info("NE owner lookup: %d ownerless candidate(s)", len(targets))
    resolved = 0
    for i, n in enumerate(targets, 1):
        county = n.county.lower()
        best = None
        for term in _addr_terms(n.address):
            time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))
            cands = [c for c in query_parcels_by_address(county, term) if c.get("owner")]
            if not cands:
                continue
            want = re.sub(r"\s+", " ", n.address.upper()).strip()
            exact = next((c for c in cands if c["address"].upper() == want), None)
            best = exact or cands[0]
            if best:
                break
        if best:
            n.owner_name = _format_owner(best["owner"])
            if best.get("parcel_id") and not n.parcel_id:
                n.parcel_id = best["parcel_id"]
            if best.get("zip") and not n.zip:
                n.zip = best["zip"]
            n.missing_data_flags = _add_flag(n.missing_data_flags, "owner_from_parcel_lookup")
            logger.info("  [%d/%d] %s → owner %s", i, len(targets), n.address, n.owner_name)
            resolved += 1
        else:
            logger.info("  [%d/%d] %s → no owner match", i, len(targets), n.address)
    logger.info("NE owner lookup: resolved %d/%d", resolved, len(targets))
    return resolved


def _attr(attrs: dict, field: str) -> str:
    return str(attrs.get(field, "") or "").strip() if field else ""


def _extract_situs(attrs: dict, cfg: dict) -> tuple[str, str, str]:
    """Pull (street, city, zip) from a parcel record per the county's schema."""
    # Sarpy: one combined "STREET  CITY STATE [ZIP]" field.
    combined = cfg.get("situs_combined_field")
    if combined:
        street, city, zipc = _parse_combined_situs(_attr(attrs, combined))
        if not city:
            city = _attr(attrs, cfg.get("city_field", "")).title()
        if not zipc:
            zipc = _attr(attrs, cfg.get("zip_field", ""))
        return (street, city, zipc)

    # Douglas: separate situs fields, with a component-built fallback for street.
    street = _attr(attrs, cfg.get("addr_field", ""))
    if not street and cfg.get("addr_component_fields"):
        parts = [_attr(attrs, f) for f in cfg["addr_component_fields"]]
        street = " ".join(p for p in parts if p)
    city = _attr(attrs, cfg.get("city_field", "")).title()
    zipc = _attr(attrs, cfg.get("zip_field", ""))
    return (street.strip(), city, zipc)


def _parse_combined_situs(raw: str) -> tuple[str, str, str]:
    """Split 'STREET  CITY STATE [ZIP]' (double-space) → (street, city, zip)."""
    raw = (raw or "").strip()
    if not raw:
        return ("", "", "")
    parts = re.split(r"\s{2,}", raw, maxsplit=1)
    street = parts[0].strip()
    locality = parts[1].strip() if len(parts) > 1 else ""
    zipc = ""
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", locality)
    if m:
        zipc = m.group(1)
        locality = locality[: m.start()].strip()
    locality = re.sub(r"\bNE\b\.?\s*$", "", locality, flags=re.IGNORECASE).strip()
    return (street, locality.title(), zipc)


# ── Main entry point ───────────────────────────────────────────────────

_SUPPORTED = {"douglas", "sarpy"}


def resolve_addresses(notices: list, min_score: float = 0.4) -> int:
    """Fill street addresses for address-less NE notices via owner-name parcel search.

    Modifies notices in place. Returns the count resolved. Targets records that are
    NE (Douglas/Sarpy), have an owner_name, and lack a street address.
    """
    def _search_key(n) -> str:
        # For probate the property is under the DECEDENT, not the PR (owner_name).
        if n.notice_type == "probate" and (getattr(n, "decedent_name", "") or "").strip():
            return n.decedent_name
        return n.owner_name

    targets = [
        n for n in notices
        if n.county.lower() in _SUPPORTED
        and not (n.address or "").strip()
        and _search_key(n).strip()
    ]
    if not targets:
        return 0

    logger.info("NE property lookup: %d address-less candidate(s)", len(targets))
    resolved = 0
    for i, notice in enumerate(targets, 1):
        county = notice.county.lower()
        sep = (NE_PARCEL_SERVICES.get(county) or {}).get("owner_sep", " ")
        search_owner = _primary_owner(_search_key(notice))
        terms = owner_search_terms(search_owner, sep=sep)
        best = None
        for term in terms:
            time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))
            candidates = query_parcels_by_owner(county, term)
            match = select_best_parcel(search_owner, candidates, min_score=min_score)
            if match and (best is None or match[1] > best[1]):
                best = match
            # Confident enough only when a strong, UNambiguous match is found.
            if best and best[1] >= 0.75 and best[2] == 1:
                break

        if best:
            parcel, score, n_tied = best
            ambiguous = n_tied > 1
            notice.address = parcel["address"]
            notice.city = parcel.get("city") or notice.city
            notice.state = "NE"
            if parcel.get("zip"):
                notice.zip = parcel["zip"]
            if parcel.get("parcel_id") and not notice.parcel_id:
                notice.parcel_id = parcel["parcel_id"]
            # A perfect name score is still "low" if several properties tie (can't
            # tell which is the right person from name alone).
            if ambiguous or score < 0.55:
                conf = "low"
            elif score >= 0.75:
                conf = "high"
            else:
                conf = "medium"
            notice.missing_data_flags = _add_flag(
                notice.missing_data_flags, f"addr_from_owner_lookup:{conf}"
            )
            if ambiguous:
                notice.missing_data_flags = _add_flag(
                    notice.missing_data_flags, f"ambiguous_owner_match:{n_tied}"
                )
            logger.info(
                "  [%d/%d] %s → %s (score %.2f, %s%s)",
                i, len(targets), search_owner, notice.address, score, conf,
                f", {n_tied} tied" if ambiguous else "",
            )
            resolved += 1
        else:
            notice.missing_data_flags = _add_flag(notice.missing_data_flags, "no_address_match")
            logger.info("  [%d/%d] %s → no confident parcel match", i, len(targets), search_owner)

    logger.info("NE property lookup: resolved %d/%d", resolved, len(targets))
    return resolved


def _add_flag(existing: str, flag: str) -> str:
    flags = [f for f in (existing or "").split("|") if f]
    if flag not in flags:
        flags.append(flag)
    return "|".join(flags)
