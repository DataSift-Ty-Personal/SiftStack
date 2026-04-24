"""Montgomery County, Ohio sheriff foreclosure sales scraper.

Pulls upcoming sheriff-sale listings from the county sheriff's ColdFusion
auction list (go.mcohio.org/applications/sheriffauction/sflistauction.cfm).
Covers BOTH mortgage foreclosures and treasurer tax sales (tagged with
distinct notice_type — see classification below).

Portal characteristics (researched against live site):
  * Tech stack: ColdFusion (*.cfm), server-rendered HTML, no JS required.
  * No login, no CAPTCHA.
  * Single form with a true date-range filter (idate1 / idate2 = sale
    date window, MM/DD/YYYY) and a SUMMARY/DETAIL toggle (iSUMDET).
    A single POST to SFLISTAUCTIONDO.cfm returns the full matching set —
    NO pagination, NO per-case detail pages. All fields are rendered
    inline in the results HTML.
  * DETAIL view exposes: sale date, status, case number, property address,
    plaintiff (bank), defendant (homeowner = our contact), attorney,
    "sold to" (if already sold), parcel ID (PID), ZIP, appraised amount,
    minimum bid, sale amount. SUMMARY drops plaintiff/defendant/attorney/PID
    so we always use DETAIL.
  * Response page has three labeled sections, each a <td BGCOLOR="YELLOW">
    banner followed by record rows. Records use status-coded bg colors:
      - FFCCFF (pink)     = Active (still scheduled)
      - SILVER            = Sold
      - lightblue         = Cancelled by court
      - lightyellow       = No Bid
    Sections:
      - MORTGAGE FORECLOSURE        → notice_type="foreclosure"
      - TREASURER'S TAX SALES       → notice_type="tax_sale"
      - TREASURER'S TAX LIEN SALES  → notice_type="tax_sale"
    Tax-sale records have "Treasurer of Montgomery County, Ohio" as
    plaintiff, which gives us a secondary signal for classification.
    Every section's records live in the SAME <table> as its banner; we
    walk TRs forward from the banner row until we hit the next banner.
  * Actual online bidding happens on an external Realauction portal
    (montgomery.sheriffsaleauction.ohio.gov); the county .cfm list is
    the authoritative public schedule that feeds us what we need.

NoticeData contract per CLAUDE.md domain rules:
  * owner_name = defendant (the homeowner being foreclosed on / tax-sold).
    ", et al." suffix stripped; "UNKNOWN HEIRS ..." left as-is so the
    enrichment pipeline can spot it for probate-style deep prospecting.
  * address / city / zip = property address.
  * auction_date = sheriff-sale date (YYYY-MM-DD).
  * date_added = sale date (the portal exposes no filing/publish date).
  * parcel_id = PID from the record.
  * raw_text = full rendered record plus a Section: header line so
    downstream classification can disambiguate mortgage vs tax sales
    regardless of the final notice_type.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup, Tag

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://go.mcohio.org/applications/sheriffauction"
SEARCH_FORM_URL = f"{BASE_URL}/sflistauction.cfm"
SEARCH_ACTION_URL = f"{BASE_URL}/SFLISTAUCTIONDO.cfm"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Default look-ahead for upcoming sheriff sales when no explicit `until`
# is supplied. Sheriff sales are scheduled 4-8 weeks out; 60 days captures
# the full published schedule comfortably.
DEFAULT_LOOKAHEAD_DAYS = 60

# Section banner text → notice_type mapping.
SECTION_NOTICE_TYPE = {
    "MORTGAGE FORECLOSURE": "foreclosure",
    "TREASURER'S TAX SALES": "tax_sale",
    "TREASURER'S TAX LIEN SALES": "tax_sale",
}

# Record-row bgcolor values (case-insensitive). Each maps to a status but
# status is also spelled out in the row itself — we just use this to
# identify which TRs are data rows vs chrome.
RECORD_ROW_BGCOLORS = {"SILVER", "FFCCFF", "LIGHTBLUE", "LIGHTYELLOW"}

# Regex helpers.
FIELD_LABEL_RE = re.compile(
    r"^\s*(Address|Plaintiff|Defendant|Attorney|Sold To|PID|Zip Code|Status|"
    r"Appr Amt|Min Bid|Sale Amt)\s*:",
    re.IGNORECASE,
)
SALE_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
CASE_NUMBER_RE = re.compile(r"\b20\d{2}\s+CV\s+\d{3,6}\b")
MONEY_RE = re.compile(r"\$\s*([0-9,]+(?:\.\d{2})?)")
ADDRESS_PARTS_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[A-Za-z .'\-]+),\s*Ohio\s*(?P<zip>\d{5}(?:-\d{4})?)?\s*$",
    re.IGNORECASE,
)


@dataclass
class _SaleRow:
    """Internal bundle for one sheriff-sale record parsed from the list."""
    section: str               # e.g. "MORTGAGE FORECLOSURE"
    notice_type: str           # "foreclosure" or "tax_sale"
    case_number: str           # e.g. "2024 CV 05152"
    sale_date: date | None
    status: str                # "SOLD", "CANCELLED", "ACTIVE", "NO BID", etc.
    address_raw: str           # as rendered, e.g. "609 COLERIDGE AVENUE, TROTWOOD, Ohio  45426"
    street: str
    city: str
    zip_code: str
    plaintiff: str
    defendant: str
    attorney: str
    sold_to: str
    parcel_id: str
    appraised_amount: str
    min_bid: str
    sale_amount: str
    source_url: str            # deep-link with anchor
    raw_text: str              # full rendered record text


class Scraper(NoticeScraper):
    """Montgomery County Sheriff — foreclosure + tax-sale auction scraper."""

    county = "Montgomery"
    notice_type = "foreclosure"   # default; tax-sale records override per-row
    source_name = "Montgomery County Sheriff Auction List"
    source_url = SEARCH_FORM_URL

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull sheriff-sale listings with sale dates on/after `since_date`.

        Default window: today → today+60 days. The portal requires an
        explicit MM/DD/YYYY date-range; we upper-bound at +60 days to
        fetch the full published schedule.
        """
        if since_date is None:
            since_date = date.today() - timedelta(days=7)
        until_date = max(since_date, date.today()) + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)
        return await asyncio.to_thread(self._scrape_sync, since_date, until_date)

    # ── Private sync implementation ────────────────────────────────────
    def _scrape_sync(self, since_date: date, until_date: date) -> list[NoticeData]:
        session = self._build_session()
        logger.info(
            "Montgomery foreclosure scrape: sale dates %s → %s",
            since_date.isoformat(), until_date.isoformat(),
        )

        soup = self._post_search(session, since_date, until_date)
        if soup is None:
            logger.warning("Search POST failed — returning no records.")
            return []

        rows = self._parse_sections(soup)
        logger.info("Parsed %d raw sale rows across all sections", len(rows))

        records: list[NoticeData] = []
        for row in rows:
            if row.sale_date is None:
                logger.debug("Row %s: unparseable sale date — skipping", row.case_number)
                continue
            if row.sale_date < since_date:
                # Shouldn't happen given the form filter, but guard anyway.
                continue
            record = self._to_notice_data(row)
            records.append(record)
            logger.info(
                "  %s  %s  %s  defendant=%s  status=%s",
                row.sale_date.isoformat(),
                row.notice_type,
                row.case_number,
                row.defendant or "(blank)",
                row.status or "(blank)",
            )

        # Oldest-first for downstream consistency with probate scraper.
        records.sort(key=lambda r: (r.auction_date, r.source_url))
        logger.info("Montgomery foreclosure scrape done — %d records", len(records))
        return records

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Warm cookies + populate the CF session. Non-fatal if it fails.
        try:
            s.get(SEARCH_FORM_URL, timeout=20)
        except requests.RequestException as e:
            logger.debug("Initial form GET failed (non-fatal): %s", e)
        return s

    def _sleep(self) -> None:
        """Rate-limit — 2-3s jittered delay between requests."""
        delay = random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
        time.sleep(delay)

    def _post_search(
        self,
        session: requests.Session,
        since_date: date,
        until_date: date,
    ) -> BeautifulSoup | None:
        """POST the auction-list form, DETAIL mode. Returns parsed soup or None."""
        data = {
            "idate1": since_date.strftime("%m/%d/%Y"),
            "idate2": until_date.strftime("%m/%d/%Y"),
            "iSUMDET": "DET",
        }
        for attempt in range(config.MAX_RETRIES):
            try:
                self._sleep()
                resp = session.post(
                    SEARCH_ACTION_URL,
                    data=data,
                    timeout=45,
                    headers={"Referer": SEARCH_FORM_URL},
                )
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    return BeautifulSoup(resp.text, "html.parser")
                logger.debug("POST search → %d (attempt %d)", resp.status_code, attempt + 1)
            except requests.RequestException as e:
                logger.debug("POST search raised %s (attempt %d)", e, attempt + 1)
        return None

    # ── Section + row parsing ──────────────────────────────────────────
    def _parse_sections(self, soup: BeautifulSoup) -> list[_SaleRow]:
        """Walk the response HTML and extract sale rows, tagged by section.

        The response has three labeled sections whose banners are
        <td BGCOLOR="YELLOW"> cells containing <em><strong>SECTION_NAME
        </strong></em>. Each banner lives in its OWN tiny table (not the
        same table as the data rows), and data rows appear LATER in
        document order as <tr bgcolor="{silver|ffccff|lightblue|lightyellow}">
        with 4 <td> cells. We assign each data row to the most recent
        preceding banner by walking forward from the banner until we
        reach the next banner's row.
        """
        rows: list[_SaleRow] = []

        banners = soup.find_all(
            lambda tag: tag.name == "td"
            and str(tag.get("bgcolor", "")).upper() == "YELLOW"
        )
        # Capture each banner's parent <tr> so we know where to stop walking.
        banner_boundaries: list[tuple[Tag, str, Tag | None]] = []
        for banner in banners:
            label_node = banner.find(["em", "strong"])
            label = label_node.get_text(" ", strip=True).upper() if label_node else ""
            label = re.sub(r"\s+", " ", label).strip()
            if label not in SECTION_NOTICE_TYPE:
                continue
            banner_boundaries.append((banner, label, banner.find_parent("tr")))

        for i, (banner, section_label, _banner_tr) in enumerate(banner_boundaries):
            stop_tr = banner_boundaries[i + 1][2] if i + 1 < len(banner_boundaries) else None
            section_notice_type = SECTION_NOTICE_TYPE[section_label]
            anchor = "#m" if section_notice_type == "foreclosure" else "#x"
            section_url = f"{SEARCH_FORM_URL}{anchor}"

            for tr in banner.find_all_next("tr"):
                if stop_tr is not None and tr is stop_tr:
                    break
                bg = str(tr.get("bgcolor", "")).upper()
                if bg not in RECORD_ROW_BGCOLORS:
                    continue
                cells = tr.find_all("td", recursive=False)
                if len(cells) < 4:
                    continue
                row = self._parse_row(cells, section_label, section_notice_type, section_url)
                if row is not None:
                    rows.append(row)

        return rows

    def _parse_row(
        self,
        cells: list[Tag],
        section_label: str,
        notice_type: str,
        source_url: str,
    ) -> _SaleRow | None:
        """Extract one _SaleRow from the 4 <td> cells of a silver row."""
        # Cell 0: sale date + status banner.
        c0 = cells[0].get_text("\n", strip=True)
        sale_date = self._parse_date(SALE_DATE_RE.search(c0).group(1)) if SALE_DATE_RE.search(c0) else None
        # Cell 1: case number.
        c1 = cells[1].get_text(" ", strip=True)
        case_m = CASE_NUMBER_RE.search(c1)
        case_number = case_m.group(0) if case_m else ""
        if not case_number:
            # No case number → not a real data row (probably a sub-header).
            return None

        # Cell 2: Address / Plaintiff / Defendant / Attorney / Sold To / PID.
        desc_map = self._parse_labeled_cell(cells[2])
        address_raw = desc_map.get("address", "")
        plaintiff = desc_map.get("plaintiff", "")
        defendant = desc_map.get("defendant", "")
        attorney = desc_map.get("attorney", "")
        sold_to = desc_map.get("sold to", "")
        parcel_id = desc_map.get("pid", "")

        # Cell 3: Zip Code / Status / Appr Amt / Min Bid / Sale Amt.
        amt_map = self._parse_labeled_cell(cells[3])
        zip_code = amt_map.get("zip code", "")
        status = (amt_map.get("status") or "").upper()
        appraised_amount = self._clean_money(amt_map.get("appr amt", ""))
        min_bid = self._clean_money(amt_map.get("min bid", ""))
        sale_amount = self._clean_money(amt_map.get("sale amt", ""))

        # Best-effort address split. Falls back to raw if regex doesn't match.
        street, city, zip_from_addr = self._split_address(address_raw)
        if not zip_code:
            zip_code = zip_from_addr

        # Full-row raw_text bundles the original field labels for downstream use.
        raw_text = (
            f"Section: {section_label}\n"
            f"Sale Date: {sale_date.isoformat() if sale_date else ''}\n"
            f"Case: {case_number}\n"
            f"Status: {status}\n"
            f"Address: {address_raw}\n"
            f"Plaintiff: {plaintiff}\n"
            f"Defendant: {defendant}\n"
            f"Attorney: {attorney}\n"
            f"Sold To: {sold_to}\n"
            f"PID: {parcel_id}\n"
            f"Zip Code: {zip_code}\n"
            f"Appr Amt: {appraised_amount}\n"
            f"Min Bid: {min_bid}\n"
            f"Sale Amt: {sale_amount}\n"
        )

        return _SaleRow(
            section=section_label,
            notice_type=notice_type,
            case_number=case_number,
            sale_date=sale_date,
            status=status,
            address_raw=address_raw,
            street=street,
            city=city,
            zip_code=zip_code,
            plaintiff=plaintiff,
            defendant=defendant,
            attorney=attorney,
            sold_to=sold_to,
            parcel_id=parcel_id,
            appraised_amount=appraised_amount,
            min_bid=min_bid,
            sale_amount=sale_amount,
            source_url=source_url,
            raw_text=raw_text,
        )

    def _parse_labeled_cell(self, cell: Tag) -> dict[str, str]:
        """Parse a cell like `Address: X  Plaintiff: Y  ...` into a dict.

        The cell's HTML interleaves literal `Label:` text with inline <font>
        spans holding the values; rendering as text with <br> → newlines
        gives us one logical line per field which we then key off the
        leading label.
        """
        # Rendering with "\n" honors <br> boundaries.
        text = cell.get_text("\n", strip=True)
        # Collapse tabs/multiple spaces but preserve line breaks.
        text = re.sub(r"[ \t]+", " ", text)
        out: dict[str, str] = {}
        current_label: str | None = None
        current_parts: list[str] = []

        def _flush():
            if current_label is not None:
                out[current_label] = " ".join(p.strip() for p in current_parts).strip()

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = FIELD_LABEL_RE.match(line)
            if m:
                _flush()
                current_label = m.group(1).lower()
                remainder = line[m.end():].strip()
                current_parts = [remainder] if remainder else []
            else:
                if current_label is not None:
                    current_parts.append(line)
        _flush()
        return out

    # ── Parsing helpers ────────────────────────────────────────────────
    @staticmethod
    def _parse_date(mmddyyyy: str) -> date | None:
        try:
            return datetime.strptime(mmddyyyy, "%m/%d/%Y").date()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _clean_money(raw: str) -> str:
        """Normalize `$    120,100.00` → `120100.00`. Empty string on miss."""
        if not raw:
            return ""
        m = MONEY_RE.search(raw)
        if not m:
            return ""
        return m.group(1).replace(",", "")

    @staticmethod
    def _split_address(raw: str) -> tuple[str, str, str]:
        """Split `STREET, CITY, Ohio ZIP` into (street, city, zip)."""
        if not raw:
            return "", "", ""
        cleaned = re.sub(r"\s+", " ", raw).strip().rstrip(",")
        m = ADDRESS_PARTS_RE.match(cleaned)
        if m:
            # Leave case as-is — the portal emits ALL CAPS and the rest of
            # the pipeline preserves that for Smarty/Zillow lookups.
            return (
                m.group("street").strip(),
                m.group("city").strip(),
                (m.group("zip") or "").strip(),
            )
        # Fallback: no comma separation — return street as-is.
        return cleaned, "", ""

    @staticmethod
    def _clean_defendant(raw: str) -> str:
        """Strip trailing `, et al.` and collapse whitespace.

        The portal sometimes truncates the field mid-word, leaving things
        like `NAME , e` or `NAME , et a`. Strip any trailing comma +
        partial `et al` token so we don't carry garbage into DataSift.
        """
        if not raw:
            return ""
        cleaned = re.sub(r"\s+", " ", raw).strip()
        # Full and partial ", et al." tails (also handles "et a", "e").
        cleaned = re.sub(
            r",\s*e(?:t(?:\.|\s+a(?:l\.?)?)?)?\.?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        # Bare trailing " et al." without leading comma.
        cleaned = re.sub(r"\s+et\.?\s*al\.?\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned.rstrip(",").strip()

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(self, row: _SaleRow) -> NoticeData:
        """Project a parsed _SaleRow onto the pipeline's NoticeData shape."""
        owner = self._clean_defendant(row.defendant)
        iso_sale = row.sale_date.isoformat() if row.sale_date else ""

        return NoticeData(
            # The portal exposes no publish/filing date — fall back to sale date.
            date_added=iso_sale,
            auction_date=iso_sale,
            county=self.county,
            state="OH",
            notice_type=row.notice_type,
            source_url=row.source_url,
            address=row.street,
            city=row.city,
            zip=row.zip_code,
            owner_name=owner,
            parcel_id=row.parcel_id,
            raw_text=row.raw_text,
        )


# ── Standalone test harness ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import csv
    import sys
    from dataclasses import asdict
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test Montgomery foreclosure scraper")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_montgomery_foreclosure.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} foreclosure records since {since}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if records:
            writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        else:
            f.write("")
    print(f"Wrote {out_path}")
    sys.exit(0)
