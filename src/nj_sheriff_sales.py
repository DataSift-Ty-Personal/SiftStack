"""NJ sheriff sales scraper — salesweb.civilview.com.

Covers Essex (countyId=2), Middlesex (countyId=73), Union (countyId=15).
Somerset uses a different site and is out of scope for this module.

Plain HTTP + regex parsing. No auth, no CAPTCHA, no Playwright. Each
county returns all active sales on a single page (no pagination). Data
comes from the listing page directly — detail pages require session
state and carry no REI-relevant fields not already in the listing.

Columns per row:
  [0] View Details link  — /Sales/SaleDetails?PropertyId=XXX
  [1] Sheriff #          — e.g. "F-24001837" or "CH-25001605"
  [2] Sales Date         — "M/D/YYYY"
  [3] Plaintiff          — lender
  [4] Defendant          — property owner (target contact)
  [5] Address            — "{street} [MAILING ADDRESS OF {alt}] CITY NJ ZIP"
"""

import logging
import re
from datetime import datetime

import requests

from config import (
    NJ_CIVILVIEW_BASE,
    NJ_CIVILVIEW_COUNTIES,
    NJ_SHERIFF_STATE_FILE,
    save_state,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30

# Match any <tr> containing a SaleDetails link (6 or 7 cells — Middlesex
# has an extra Status column).
_ROW_RE = re.compile(
    r'<tr[^>]*>((?:(?!</tr>).)*SaleDetails\?PropertyId=\d+(?:(?!</tr>).)*)</tr>',
    re.DOTALL,
)
_CELL_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_DETAIL_URL_RE = re.compile(r'href="([^"]*SaleDetails\?PropertyId=\d+)"')

# Street suffix tokens used to locate the street/city boundary. A parcel
# address usually ends its street component with one of these, and the
# city begins immediately after.
_STREET_SUFFIXES = {
    "STREET", "ST", "AVENUE", "AVE", "ROAD", "RD", "PLACE", "PL",
    "LANE", "LN", "DRIVE", "DR", "BOULEVARD", "BLVD", "COURT", "CT",
    "CIRCLE", "CIR", "PARKWAY", "PKWY", "TERRACE", "TER", "WAY",
    "TRAIL", "TRL", "SQUARE", "SQ", "HIGHWAY", "HWY", "TURNPIKE",
    "TPKE", "PLAZA", "CROSSING", "POINT", "PT", "LOOP", "PATH", "ROW",
    "RUN", "VIEW", "EXTENSION", "EXT", "ROUTE", "RTE", "PIKE",
    "BROADWAY", "CORNER", "CORNERS",
    # Careful — these words also appear in NJ municipality names and can
    # split incorrectly (e.g. "Old Bridge", "Short Hills"). Added only
    # when disambiguation is safe for current data:
    # NOT included: BRIDGE, HEIGHTS, HILLS, GARDENS
}

# Unit/apt descriptors that sometimes appear between the street and the
# city — we peel these back into the street so the city field stays clean.
_UNIT_KEYWORDS = {
    "UNIT", "APT", "APARTMENT", "SUITE", "STE", "BLDG", "BUILDING",
    "FLOOR", "FLR", "PARKING", "LOT", "REAR", "FRONT",
}

_ZIP_TAIL_RE = re.compile(r'\s+(\d{5})(?:-\d{4})?\s*$')
_NJ_TAIL_RE = re.compile(r'\s+NJ\s*$', re.IGNORECASE)

# "MAILING ADDRESS OF ..." (or "MAILING ADDRESS: ...") between property
# address and city.
_MAILING_SPLIT_RE = re.compile(r'\s+MAILING\s+ADDRESS\s*(?:OF|:)\s+', re.IGNORECASE)


def _clean(text: str) -> str:
    """Strip HTML tags, collapse whitespace, unescape entities."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&amp;', '&').replace('&#39;', "'").replace('&quot;', '"')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _split_at_street_suffix(text: str) -> tuple[str, str]:
    """Find the last street-suffix token in `text` and split there.

    Peels any trailing unit/apt descriptors (UNIT C-5, APT 12, #76,
    PARKING SPACE #..., REAR) back into the street so the city field
    stays clean.

    Returns (street, city). If no suffix token is found, returns
    (text, "").
    """
    tokens = text.split()
    last_suffix_idx = -1
    for i, tok in enumerate(tokens):
        bare = tok.rstrip(",.").upper()
        if bare in _STREET_SUFFIXES:
            last_suffix_idx = i
    if last_suffix_idx == -1:
        return text, ""

    street_end = last_suffix_idx
    # Consume unit descriptors after the street suffix — they belong to
    # the street, not the city. A unit value is short and alphanumeric
    # (e.g. "C-5", "1A", "194") — not a plain word like "BELLEVILLE".
    i = street_end + 1
    while i < len(tokens):
        tok = tokens[i]
        bare = tok.rstrip(",.").upper()
        is_unit_kw = bare in _UNIT_KEYWORDS
        is_hash_unit = tok.startswith("#")
        if is_unit_kw or is_hash_unit:
            street_end = i
            # After a unit keyword, consume the next token IF it looks
            # like a unit value (contains a digit or is <=4 chars). Stop
            # if the next token is a plain word (likely the city).
            if is_unit_kw and i + 1 < len(tokens):
                nxt = tokens[i + 1]
                looks_like_value = (
                    any(c.isdigit() for c in nxt) or len(nxt) <= 4 or nxt.startswith("#")
                )
                if looks_like_value:
                    street_end = i + 1
                    i += 1
        else:
            break
        i += 1

    street = " ".join(tokens[: street_end + 1]).rstrip(",")
    city = " ".join(tokens[street_end + 1 :]).strip()
    return street, city


def _parse_address(raw: str) -> tuple[str, str, str]:
    """Split 'NNN STREET NAME CITY NJ ZIP' into (street, city, zip).

    Handles the "MAILING ADDRESS OF ..." variant by using the property
    address for the street and the trailing city/zip from the mailing alt.
    """
    raw = raw.strip()
    if not raw:
        return "", "", ""

    # Split mailing-address variant. `before` carries the property street;
    # `after` carries the (duplicated) street + city + NJ + ZIP.
    parts = _MAILING_SPLIT_RE.split(raw, maxsplit=1)
    if len(parts) == 2:
        before = parts[0].strip()
        after = parts[1].strip()
        street = before  # prefer property address over mailing variant
        body = after
    else:
        street = None
        body = raw

    # Pull zip off the tail.
    zip_code = ""
    m = _ZIP_TAIL_RE.search(body)
    if m:
        zip_code = m.group(1)
        body = body[: m.start()].rstrip()

    # Strip trailing " NJ".
    body = _NJ_TAIL_RE.sub("", body).rstrip()

    if street is None:
        # No mailing variant: split body at its last street suffix.
        street, city = _split_at_street_suffix(body)
    else:
        # Mailing variant: body still contains {alt_street CITY}. Find its
        # split, keep only the CITY portion — discard the alt_street.
        _, city = _split_at_street_suffix(body)

    city = city.strip().title()
    return street, city, zip_code


def _row_to_notice(url: str, sheriff_num: str, sale_date: str,
                   plaintiff: str, defendant: str, address: str,
                   county: str) -> NoticeData | None:
    """Convert one parsed listing row into NoticeData."""
    street, city, zip_code = _parse_address(address)
    if not street:
        return None

    try:
        auction_dt = datetime.strptime(sale_date, "%m/%d/%Y")
        auction_iso = auction_dt.strftime("%Y-%m-%d")
    except ValueError:
        auction_iso = ""

    full_url = url if url.startswith("http") else f"{NJ_CIVILVIEW_BASE}{url}"

    # Notes string carries the Sheriff# + Plaintiff for downstream use.
    raw_text = f"Sheriff# {sheriff_num} | Plaintiff: {plaintiff}"

    return NoticeData(
        date_added=datetime.now().strftime("%Y-%m-%d"),
        auction_date=auction_iso,
        address=street,
        city=city,
        state="NJ",
        zip=zip_code,
        owner_name=defendant,
        notice_type="sheriff_sale",
        county=county,
        source_url=full_url,
        raw_text=raw_text,
    )


def scrape_county(county: str) -> list[NoticeData]:
    """Fetch and parse all active sheriff sales for one county."""
    if county not in NJ_CIVILVIEW_COUNTIES:
        raise ValueError(f"Unsupported county: {county}. Supported: {list(NJ_CIVILVIEW_COUNTIES)}")

    county_id = NJ_CIVILVIEW_COUNTIES[county]
    url = f"{NJ_CIVILVIEW_BASE}/Sales/SalesSearch?countyId={county_id}"
    logger.info("Fetching sheriff sales: %s (countyId=%d)", county, county_id)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    notices: list[NoticeData] = []
    for row_match in _ROW_RE.finditer(resp.text):
        inner = row_match.group(1)
        cells = _CELL_RE.findall(inner)
        if len(cells) < 6:
            continue

        url_match = _DETAIL_URL_RE.search(inner)
        if not url_match:
            continue
        detail_url = url_match.group(1)

        # Read from the right — column order is stable at the tail
        # regardless of whether a Status column is present (Middlesex
        # has 7 cells; Essex/Union have 6).
        address = _clean(cells[-1])
        defendant = _clean(cells[-2])
        plaintiff = _clean(cells[-3])
        sale_date = _clean(cells[-4])
        sheriff_num = _clean(cells[-5])

        notice = _row_to_notice(
            url=detail_url,
            sheriff_num=sheriff_num,
            sale_date=sale_date,
            plaintiff=plaintiff,
            defendant=defendant,
            address=address,
            county=county,
        )
        if notice:
            notices.append(notice)

    logger.info("%s: parsed %d sales", county, len(notices))
    return notices


async def scrape_civilview_notices(counties: list[str] | None = None) -> list[NoticeData]:
    """Async wrapper around scrape_all for modal_app.py fan-out.

    CivilView is plain blocking HTTP (requests), so we push it to a thread
    so nj_weekly_all can gather() all scrapers in parallel without blocking
    the event loop.
    """
    import asyncio
    return await asyncio.to_thread(scrape_all, counties)


def scrape_all(counties: list[str] | None = None) -> list[NoticeData]:
    """Scrape sheriff sales across configured counties.

    Args:
        counties: List of county names to scrape. None = all supported.
    """
    targets = counties or list(NJ_CIVILVIEW_COUNTIES.keys())
    all_notices: list[NoticeData] = []
    for county in targets:
        try:
            all_notices.extend(scrape_county(county))
        except Exception as e:
            logger.error("Failed to scrape %s: %s", county, e)

    # Persist last run timestamp (not used for dedup — listings are refreshed daily
    # by the site itself, and existing data_formatter.deduplicate() handles duplicate
    # addresses across counties and prior runs).
    save_state(NJ_SHERIFF_STATE_FILE, {
        "last_run": datetime.now().isoformat(),
        "total_records": len(all_notices),
        "counties": targets,
    })

    logger.info("Sheriff sales: %d total notices across %d counties",
                len(all_notices), len(targets))
    return all_notices


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counties_arg = None
    for arg in sys.argv[1:]:
        if arg.startswith("--counties="):
            counties_arg = [c.strip() for c in arg.split("=", 1)[1].split(",")]

    notices = scrape_all(counties=counties_arg)
    print(f"\nFetched {len(notices)} total notices")
    for n in notices[:10]:
        print(f"  [{n.county}] {n.auction_date} | {n.address}, {n.city} {n.zip} | "
              f"{n.owner_name[:50]}")
