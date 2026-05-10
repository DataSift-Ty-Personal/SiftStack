"""NJ tax records lookup via taxrecords-nj.com (Vital Communications).

Covers Middlesex (1201), Somerset (1801), Union (2001) — three of our
four target counties via a single HTTP POST per query. Essex (0701) is
on a different vendor (taxdatahub.com) with reCAPTCHA v3 + JS rendering;
handled separately if needed (deferred — address waterfall falls through
cleanly to Tier 3 when Essex is missed).

Used as Tier 1 / Tier 2 of the DM address waterfall in obituary_enricher
to find a verified-living heir's mailing address from the county
assessment roll. Free, fast, authoritative.

Performance: each lookup is one POST to inf.cgi using the special "ALL"
district code (e.g. 1200 for Middlesex), which searches every
municipality in the county in one shot. Typical wall time 1-2 seconds.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://taxrecords-nj.com/pub/cgi/inf.cgi"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20

# County codes (the `select_cc` param). Only counties whose tax records
# are exposed by Vital Communications via taxrecords-nj.com are listed —
# Essex (0701) uses taxdatahub.com instead.
COUNTY_CODES = {
    "Middlesex": "1201",
    "Somerset": "1801",
    "Union": "2001",
}

# Special "ALL" district codes. Submit with district=COUNTY_ALL to search
# every municipality in the county in a single request.
COUNTY_ALL_DISTRICT = {
    "Middlesex": "1200",
    "Somerset": "1800",
    "Union": "2000",
}


@dataclass
class Parcel:
    """One row from a taxrecords-nj.com search result."""
    county: str = ""
    district_code: str = ""
    block: str = ""
    lot: str = ""
    qualifier: str = ""
    property_location: str = ""
    class_code: str = ""
    owner_name: str = ""
    mailing_street: str = ""
    mailing_city_state: str = ""
    sqft: str = ""
    year_built: str = ""
    last_sale_price: str = ""
    last_sale_date: str = ""
    detail_url: str = ""

    @property
    def parcel_id(self) -> str:
        """Combined block-lot-qualifier identifier."""
        parts = [self.block, self.lot]
        if self.qualifier and self.qualifier.strip() not in ("", "&nbsp;"):
            parts.append(self.qualifier)
        return "-".join(p for p in parts if p)

    @property
    def mailing_full(self) -> str:
        """Full mailing address (street + city/state) as one line."""
        parts = [self.mailing_street, self.mailing_city_state]
        return ", ".join(p for p in parts if p)


# ── HTML parsers ──────────────────────────────────────────────────────────────

# Each result row begins with `<tr><td nowrap align=center>` (the "More Info"
# link cell) and ends at the next `</tr>`. The detail link href contains the
# l02 parcel composite key.
_ROW_RE = re.compile(
    r'<tr><td nowrap align=center>\s*<a href=(m4\.cgi[^>]*)>'
    r'.*?</tr>',
    re.DOTALL,
)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)


def _strip_tags(text: str) -> str:
    """Strip HTML tags + collapse whitespace + decode entities."""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = (
        text.replace('&nbsp;', ' ')
            .replace('&amp;', '&')
            .replace('&#39;', "'")
            .replace('&quot;', '"')
    )
    return text


def _split_lines(cell_text: str) -> list[str]:
    """Split a multi-line table cell into clean lines (whitespace-stripped)."""
    return [ln.strip() for ln in cell_text.splitlines() if ln.strip()]


def _parse_results(html: str, county: str) -> list[Parcel]:
    """Parse multi-line list HTML into Parcel objects.

    Multi-line format (out_type=2) per row — confirmed via header inspection
    of inf.cgi output 2026-05-10:
      td 0: "More Info" link (with parcel id in href)
      td 1: District code (4-digit, e.g. "1216" for Perth Amboy)
      td 2: Block / Lot / Qualifier (br-separated)
      td 3: Property location / Class
      td 4: Owner name / Mailing street / City State Zip
      td 5: SF / Year Built
      td 6: Last Sale Price / Date
    """
    parcels: list[Parcel] = []
    for row_match in _ROW_RE.finditer(html):
        row = row_match.group(0)
        href = row_match.group(1)
        cells = _TD_RE.findall(row)
        if len(cells) < 7:
            continue

        district_from_data = _strip_tags(cells[1]).strip()
        bl_lines = _split_lines(_strip_tags(cells[2]))
        loc_lines = _split_lines(_strip_tags(cells[3]))
        owner_lines = _split_lines(_strip_tags(cells[4]))
        sf_lines = _split_lines(_strip_tags(cells[5]))
        sale_lines = _split_lines(_strip_tags(cells[6]))

        block = bl_lines[0] if len(bl_lines) > 0 else ""
        lot = bl_lines[1] if len(bl_lines) > 1 else ""
        qualifier = bl_lines[2] if len(bl_lines) > 2 else ""

        property_location = loc_lines[0] if len(loc_lines) > 0 else ""
        class_code = ""
        for ln in loc_lines:
            m = re.match(r'Class:\s*(\S+)', ln)
            if m:
                class_code = m.group(1)
                break

        owner_name = owner_lines[0] if len(owner_lines) > 0 else ""
        mailing_street = owner_lines[1] if len(owner_lines) > 1 else ""
        mailing_city_state = owner_lines[2] if len(owner_lines) > 2 else ""

        sqft = sf_lines[0] if len(sf_lines) > 0 else ""
        year_built = sf_lines[1] if len(sf_lines) > 1 else ""

        last_sale_price = sale_lines[0] if len(sale_lines) > 0 else ""
        last_sale_date = sale_lines[1] if len(sale_lines) > 1 else ""

        # Parse district code from the More Info link's `district=` param
        district_match = re.search(r'district=(\d+)', href)
        district_code = district_match.group(1) if district_match else ""

        parcels.append(Parcel(
            county=county,
            district_code=district_code,
            block=block,
            lot=lot,
            qualifier=qualifier,
            property_location=property_location,
            class_code=class_code,
            owner_name=owner_name,
            mailing_street=mailing_street,
            mailing_city_state=mailing_city_state,
            sqft=sqft,
            year_built=year_built,
            last_sale_price=last_sale_price,
            last_sale_date=last_sale_date,
            detail_url=f"https://taxrecords-nj.com/pub/cgi/{href}",
        ))

    return parcels


# ── Public lookup API ─────────────────────────────────────────────────────────


def _post_search(
    county: str,
    *,
    owner: str = "",
    property_location: str = "",
    block: str = "",
    lot: str = "",
    qual: str = "",
    page_size: int = 100,
) -> list[Parcel]:
    """Submit one search to inf.cgi for a county-wide query and parse results."""
    if county not in COUNTY_CODES:
        return []

    data = {
        "ms_user": "ctb",
        "passwd": "",
        "srch_type": "1",
        "select_cc": COUNTY_CODES[county],
        "district": COUNTY_ALL_DISTRICT[county],
        "adv": "1",
        "out_type": "2",  # multi-line list — gives mailing address
        "ms_ln": str(page_size),
        "pageno": "1",
        "owner": owner,
        "p_loc": property_location,
        "block": block,
        "lot": lot,
        "qual": qual,
    }
    try:
        resp = requests.post(
            SEARCH_URL, data=data,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("taxrecords-nj search failed (%s): %s", county, e)
        return []
    return _parse_results(resp.text, county)


def lookup_by_owner_name(
    name: str,
    county: str,
    page_size: int = 100,
) -> list[Parcel]:
    """Find parcels in `county` whose owner field contains `name`.

    Uses a substring match — Vital Communications normalizes to LAST FIRST,
    so passing "JOHN SMITH" or just "SMITH" will match equally. To be safe
    pass the last name OR the full normalized "LAST, FIRST" form.

    Returns up to page_size results. Empty list when county isn't supported
    (Essex falls into this) or the search returned nothing.
    """
    name = (name or "").strip()
    if not name or county not in COUNTY_CODES:
        return []
    parcels = _post_search(county, owner=name, page_size=page_size)
    logger.info(
        "taxrecords-nj %s: '%s' -> %d parcels",
        county, name, len(parcels),
    )
    return parcels


def lookup_by_address(
    address: str,
    county: str,
    page_size: int = 50,
) -> list[Parcel]:
    """Find parcels in `county` whose property location matches `address`.

    Useful for confirming a probate decedent actually owned the property
    (cross-check against MOD-IV owner-of-record).
    """
    address = (address or "").strip()
    if not address or county not in COUNTY_CODES:
        return []
    parcels = _post_search(county, property_location=address, page_size=page_size)
    logger.info(
        "taxrecords-nj %s: addr '%s' -> %d parcels",
        county, address, len(parcels),
    )
    return parcels


def lookup_executor_family(
    executor_name: str,
    decedent_last_name: str,
    county: str,
    page_size: int = 100,
) -> list[Parcel]:
    """Find parcels owned by the executor where the decedent's last name
    also appears in the owner field — catches "executor inherited the
    property pre-death" or "executor + decedent co-owned".

    Filters the lookup_by_owner_name(executor) result.
    """
    executor_name = (executor_name or "").strip()
    decedent_last_name = (decedent_last_name or "").strip().upper()
    if not executor_name or not decedent_last_name:
        return []
    candidates = lookup_by_owner_name(executor_name, county, page_size=page_size)
    matched = [
        p for p in candidates
        if decedent_last_name in (p.owner_name or "").upper()
    ]
    logger.info(
        "taxrecords-nj %s: executor '%s' + decedent '%s' -> %d/%d matched",
        county, executor_name, decedent_last_name, len(matched), len(candidates),
    )
    return matched


# ── Adapter: Parcel -> dict shape used by obituary_enricher waterfall ─────────


def parcel_to_address_dict(parcel: Parcel, source: str = "taxrecords-nj") -> dict:
    """Convert a Parcel into the {street, city, state, zip, source} dict
    that obituary_enricher._lookup_dm_address tiers return.

    Splits "EDISON, NJ 08817" or "EDISON, NJ" into city/state/zip.
    """
    street = (parcel.mailing_street or "").strip()
    city_state = (parcel.mailing_city_state or "").strip()

    city, state, zip_code = "", "NJ", ""
    if city_state:
        # Common formats: "EDISON, NJ", "EDISON, NJ 08817", "EDISON NJ 08817"
        m = re.match(
            r'^([A-Z][A-Z .\-/&]+?)[,\s]+([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?\s*$',
            city_state.upper().strip(),
        )
        if m:
            city = m.group(1).strip().title()
            state = m.group(2)
            zip_code = m.group(3) or ""

    return {
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "source": source,
        "parcel_id": parcel.parcel_id,
        "owner_of_record": parcel.owner_name,
    }


# ── CLI quick-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 3:
        print("Usage: python nj_taxrecords.py <County> <owner_name>")
        print("       python nj_taxrecords.py Middlesex 'SMITH JOHN'")
        sys.exit(1)

    county = sys.argv[1]
    owner = sys.argv[2]

    results = lookup_by_owner_name(owner, county, page_size=20)
    if not results:
        print(f"No parcels found for '{owner}' in {county}.")
        sys.exit(0)

    print(f"\n{len(results)} parcel(s) for '{owner}' in {county}:\n")
    for i, p in enumerate(results, 1):
        print(f"  {i}. {p.owner_name}")
        print(f"     Property:  {p.property_location} (Block {p.block}, Lot {p.lot})")
        print(f"     Mailing:   {p.mailing_full}")
        if p.last_sale_price:
            print(f"     Last sale: {p.last_sale_price} on {p.last_sale_date}")
        print()
