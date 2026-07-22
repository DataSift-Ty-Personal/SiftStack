"""NJ sheriff sales scraper — salesweb.civilview.com.

Covers Essex (countyId=2), Middlesex (countyId=73), Union (countyId=15).
Somerset uses a different site and is out of scope for this module.

Plain HTTP + regex parsing. No auth, no CAPTCHA, no Playwright. Each
county returns all active sales on a single page (no pagination). Data
comes from the listing page directly — detail pages require session
state and carry no REI-relevant fields not already in the listing.

Columns per row (Essex/Union — 6 cells):
  [0] View Details link  — /Sales/SaleDetails?PropertyId=XXX
  [1] Sheriff #          — e.g. "F-24001837" or "CH-25001605"
  [2] Sales Date         — "M/D/YYYY"
  [3] Plaintiff          — lender
  [4] Defendant          — property owner (target contact)
  [5] Address            — "{street} [MAILING ADDRESS OF {alt}] CITY NJ ZIP"
Middlesex (7 cells) inserts a Status column at [2] ("Scheduled -
Foreclosure", "Purchased - 3rd Party", ...) and shifts Sales Date →
[3], Plaintiff → [4], Defendant → [5], Address → [6]. So the tail
(-1..-4) is layout-stable but Sheriff # must be read as cells[1].

NOTE: the PropertyId in the detail link is EPHEMERAL — the backend
rebuilds its snapshot every few minutes and reallocates every id (see
nj_sheriff_detail module docstring). Never persist or dedup on it.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

import requests

from config import (
    NJ_CIVILVIEW_BASE,
    NJ_CIVILVIEW_COUNTIES,
    NJ_SHERIFF_STATE_FILE,
    save_state,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Drop sheriff-sale records whose auction date is older than this many
# days. Defaults to 90 because most NJ counties hold a redo auction within
# ~60 days of the original date — anything older has either resold,
# settled, or is otherwise stale. Override via env var for backfills /
# historical runs. The 2026-05-21 weekly run shipped 32 stale records
# (~20%, oldest from Nov 2024) to enrichment before this filter existed.
SHERIFF_SALE_MAX_AGE_DAYS = int(os.getenv("SHERIFF_SALE_MAX_AGE_DAYS", "90"))

# Last filter pass's drop count — read by modal_app.py to surface in the
# weekly Slack summary. Module-level state is fine here because each
# Modal run is a fresh container; tests should reset between cases.
LAST_STALE_DROPPED: int = 0


def _filter_stale_auctions(notices: list[NoticeData]) -> list[NoticeData]:
    """Drop sheriff_sale records whose auction_date is older than the cutoff.

    Updates module-level LAST_STALE_DROPPED so the orchestrator can
    surface the count in the run summary. Records without an
    auction_date are kept (we don't know they're stale — let enrichment
    handle them).
    """
    global LAST_STALE_DROPPED
    cutoff = datetime.now().date() - timedelta(days=SHERIFF_SALE_MAX_AGE_DAYS)
    kept: list[NoticeData] = []
    dropped = 0
    for n in notices:
        if not n.auction_date:
            kept.append(n)
            continue
        try:
            auction_dt = datetime.strptime(n.auction_date, "%Y-%m-%d").date()
        except ValueError:
            kept.append(n)
            continue
        if auction_dt < cutoff:
            dropped += 1
            continue
        kept.append(n)
    LAST_STALE_DROPPED = dropped
    if dropped:
        logger.info(
            "Filtered %d stale sheriff sales (auction_date older than %d days, before %s)",
            dropped, SHERIFF_SALE_MAX_AGE_DAYS, cutoff.isoformat(),
        )
    return kept

# NJ caps PLAINTIFF adjournments at 2. Court / bankruptcy / judge
# adjournments don't count against that limit, so we filter the status
# history by reason rather than total count. Detail enrichment
# (nj_sheriff_detail.parse_detail_html) stamps status_history_json with
# {"date","status","reason"} entries; we read the "reason" field here.
_MAX_PLAINTIFF_ADJOURNMENTS = 2


def _count_plaintiff_adjournments(status_history_json: str) -> int | None:
    """Count adjournments where status/reason mentions "plaintiff".

    CivilView's row format varies by county:
      - Essex/Middlesex: "Adjourned - Plaintiff" → reason="Plaintiff"
      - Union:           "Plaintiff Adjournment" → status="Plaintiff Adjournment"
    We search the combined status+reason text for both "plaintiff" and
    "adjourn" so non-adjournment rows mentioning plaintiff (rare) don't
    get counted.

    Returns None when the history JSON is missing / malformed — caller
    treats that as "unknown" (priority_tier = UNKNOWN). Returns 0 when
    history exists but no plaintiff adjournments are recorded — that's
    the common case for a freshly-scheduled sale.
    """
    if not status_history_json:
        return None
    try:
        history = json.loads(status_history_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(history, list):
        return None
    count = 0
    for h in history:
        if not isinstance(h, dict):
            continue
        combined = (
            (h.get("status", "") or "") + " " + (h.get("reason", "") or "")
        ).lower()
        if "plaintiff" in combined and "adjourn" in combined:
            count += 1
    return count


def _classify_priority(adjournments_remaining: int | None,
                       days_until_auction: int | None) -> str:
    """Implement the tier ladder (CLAUDE.md / nj_sheriff_sales docstring).

    Returns one of: HOT / WARM / URGENT_NO_OPTIONS / LONG_RUNWAY / PAST_DUE / UNKNOWN.
    First match wins; order matches the operator spec verbatim so the
    edge cases (e.g. adj==0 + past auction → URGENT, not PAST_DUE) stay
    consistent with how the team thinks about the leads.
    """
    if adjournments_remaining is None or days_until_auction is None:
        return "UNKNOWN"
    if adjournments_remaining >= 1 and 0 < days_until_auction <= 30:
        return "HOT"
    if adjournments_remaining >= 1 and days_until_auction > 30:
        return "WARM"
    if adjournments_remaining == 0 and days_until_auction <= 14:
        return "URGENT_NO_OPTIONS"
    if adjournments_remaining == 0 and 15 <= days_until_auction <= 30:
        return "URGENT_NO_OPTIONS"
    if adjournments_remaining == 0 and days_until_auction > 30:
        return "LONG_RUNWAY"
    if days_until_auction <= 0:
        return "PAST_DUE"
    return "UNKNOWN"


def apply_priority_tiers(notices: list[NoticeData],
                         today: "datetime | None" = None) -> None:
    """Stamp priority_tier + adjournments_remaining + days_until_auction
    on each sheriff_sale record in place.

    Runs AFTER detail enrichment so status_history_json is populated for
    CivilView records. adjournments_remaining is computed from PLAINTIFF
    adjournments only (per NJ's 2-cap rule) — court/bankruptcy adjournments
    are unlimited and don't burn options. Somerset records (no detail
    page) leave status_history_json blank and tier as UNKNOWN.

    days_until_auction is signed: negative for past auctions (lets
    PAST_DUE catch them correctly). days==0 means today's auction —
    still URGENT_NO_OPTIONS / HOT, not PAST_DUE, because the sale
    hasn't started yet in calendar terms.
    """
    now = (today or datetime.now()).date()
    for n in notices:
        if (n.notice_type or "").lower() != "sheriff_sale":
            continue

        # adjournments_remaining: count plaintiff-only adjournments from
        # status history JSON (populated by detail enrichment). Missing
        # history → None → UNKNOWN tier downstream.
        plaintiff_used = _count_plaintiff_adjournments(n.status_history_json)
        if plaintiff_used is None:
            rem = None
        else:
            rem = max(0, _MAX_PLAINTIFF_ADJOURNMENTS - plaintiff_used)
        n.adjournments_remaining = "" if rem is None else str(rem)

        # days_until_auction — signed, so negative = past.
        days: int | None
        if not n.auction_date:
            days = None
        else:
            try:
                auction_dt = datetime.strptime(n.auction_date, "%Y-%m-%d").date()
                days = (auction_dt - now).days
            except ValueError:
                days = None
        n.days_until_auction = "" if days is None else str(days)

        n.priority_tier = _classify_priority(rem, days)


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
        # Sheriff # is NOT tail-stable: Middlesex inserts its extra
        # Status column at index 2 (between Sheriff# and Sales Date), so
        # cells[-5] there reads the STATUS ("Purchased - 3rd Party").
        # That poisoned the "Sheriff# ..." dedup key — extract_id fell
        # back to the EPHEMERAL PropertyId and re-shipped all of
        # Middlesex every week — and broke the sheriff#-keyed detail
        # matching. It is stable from the LEFT: always cells[1], right
        # after the View Details link.
        sheriff_num = _clean(cells[1])

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


async def scrape_civilview_notices(
    counties: list[str] | None = None,
    enrich_details: bool = True,
) -> list[NoticeData]:
    """Async wrapper around scrape_all for modal_app.py fan-out.

    CivilView listing pages are plain blocking HTTP (requests), so the
    scrape itself runs in a worker thread. Detail-page enrichment uses
    Playwright (the /SaleDetails endpoint needs a real browser session)
    and runs after — pass `enrich_details=False` to skip when testing.

    Detail enrichment may drop records whose case_disposition resolves
    to Sold / Redeemed / Cancelled (per nj_sheriff_detail spec).
    """
    import asyncio
    notices = await asyncio.to_thread(scrape_all, counties)
    if enrich_details and notices:
        from nj_sheriff_detail import enrich_sheriff_records
        notices = await enrich_sheriff_records(notices)
    # Priority tiering runs AFTER detail enrichment so it can read the
    # adjournment_count + auction_date fields that enrichment populates.
    apply_priority_tiers(notices)
    return notices


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

    # Drop stale auctions (older than SHERIFF_SALE_MAX_AGE_DAYS) before
    # they hit enrichment — these properties already sold or settled and
    # waste Smarty / Zillow credits.
    pre_filter_count = len(all_notices)
    all_notices = _filter_stale_auctions(all_notices)

    # Persist last run timestamp (not used for dedup — listings are refreshed daily
    # by the site itself, and existing data_formatter.deduplicate() handles duplicate
    # addresses across counties and prior runs).
    save_state(NJ_SHERIFF_STATE_FILE, {
        "last_run": datetime.now().isoformat(),
        "total_records": len(all_notices),
        "stale_dropped": pre_filter_count - len(all_notices),
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
