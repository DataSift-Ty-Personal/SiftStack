"""Franklin County, Ohio probate case scraper.

Pulls estate filings from the Franklin County Probate Court's NetData search
engine at probatesearch.franklincountyohio.gov. Estate filings expose the
decedent (plus their residence address + DOD) and the fiduciary/applicant —
the fiduciary is the decision-maker we contact.

Portal characteristics (researched against live site):
  * Tech stack: IBM NetData CGI (`*.ndm/input`), server-rendered HTML,
    no JS required, no SPA, no login, no CAPTCHA.
  * Landing page (probate.franklincountyohio.gov/record-search/
    general-case-index) is a static OpenCities page with a client-side
    dispatcher that redirects each search form to a specific NetData
    endpoint under probatesearch.franklincountyohio.gov.
  * Search by open-date endpoint:
        PBODateInx.ndm/input?string=YYYYMMDD
    Returns 40 rows per page, ALL case types (Estate, Miscellaneous,
    Civil Action, Trust, etc.). Must filter to Type=="ESTATE" ourselves.
  * Cursor pagination walks FORWARD in (open_date, case_number) order:
        input?stringf=YYYYMMDDNNNNNN  (inclusive of that row)
    and BACKWARD via `stringb=`. Pages overlap by 1 row (cursor row
    repeats as first row of next page) — dedup on case_number.
  * "No Dates Found >= Criteria" text on a page with cursor
    `stringf=99999999999999` signals end-of-data.
  * Detail page (ESTATE cases):
        PBCaseTypeE.ndm/ESTATE_DETAIL?caseno=NNNNNN;;
    Exposes Decedent Street/City/State/Zip, DOD, Case Subtype, dates.
  * Fiduciary page:
        PBFidy.ndm/input?caseno=NNNNNN;;
    Exposes fiduciary name + title code (01=Executor, 02=Administrator,
    11=Commissioner, 12=Applicant) + attorney. NO fiduciary mailing
    address is exposed publicly (docket-only, filed PDFs gated behind
    e-filing account).
  * Akamai edge WAF — requires realistic browser User-Agent and
    Accept-Language headers, otherwise returns 403.

NoticeData contract per CLAUDE.md domain rules:
  * owner_name = fiduciary/applicant (our decision-maker).
  * decedent_name = the deceased.
  * decision_maker_{street,city,state,zip} left empty — not exposed.
  * Property address (address/city/state/zip) intentionally left empty
    per task spec; the decedent's residence IS available on the detail
    page and is stashed in raw_text for downstream use.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

# Portal URLs.
LANDING_URL = "https://probate.franklincountyohio.gov/record-search/general-case-index"
SEARCH_BASE = "https://probatesearch.franklincountyohio.gov/netdata"
DATE_SEARCH_URL = f"{SEARCH_BASE}/PBODateInx.ndm/input"
ESTATE_DETAIL_BASE = f"{SEARCH_BASE}/PBCaseTypeE.ndm/ESTATE_DETAIL"
FIDUCIARY_BASE = f"{SEARCH_BASE}/PBFidy.ndm/input"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# The NetData engine caps each page at 40 rows, sometimes fewer. Safety
# cap on pagination — 50 pages * 40 rows = 2000 rows, which covers months
# of Franklin County volume even during peak.
MAX_PAGES = 100
END_SENTINEL_CURSOR = "99999999999999"

# Title code → human relationship label. Maps to the CLAUDE.md DM
# relationship taxonomy ("executor" | "administrator" | "personal_representative").
TITLE_CODE_MAP = {
    "01": ("executor", "EXECUTOR"),
    "02": ("administrator", "ADMINISTRATOR"),
    "03": ("personal_representative", "CO-EXECUTOR"),
    "04": ("personal_representative", "CO-ADMINISTRATOR"),
    "11": ("personal_representative", "COMMISSIONER"),
    "12": ("personal_representative", "APPLICANT"),
}

# Columns in the list-results table (index into the <td>s of each row).
COL_CASE_NUMBER = 0
COL_CASE_NAME = 1
COL_TYPE = 2
COL_SUBTYPE = 3
COL_STATUS = 4
COL_OPENED = 5
COL_CLOSED = 6
EXPECTED_COLS = 7


@dataclass
class _CaseDetail:
    """Bundle of fields parsed out of a case detail + fiduciary page."""
    case_number: str
    case_subtype: str
    status_code: str
    filing_date: date | None
    decedent_name: str
    decedent_street: str
    decedent_city: str
    decedent_state: str
    decedent_zip: str
    date_of_death: date | None
    fiduciary_name: str
    fiduciary_title_code: str
    fiduciary_title_desc: str
    fiduciary_appt_date: date | None
    attorney_name: str
    detail_url: str
    raw_text: str


class Scraper(NoticeScraper):
    """Franklin County Probate — estate (decedent) case scraper."""

    county = "Franklin"
    notice_type = "probate"
    source_name = "Franklin County Probate Court"
    source_url = LANDING_URL

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull all estate filings on/after `since_date` (default: 7 days ago)."""
        if since_date is None:
            since_date = date.today() - timedelta(days=7)
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Sync implementation ────────────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        session = self._build_session()
        logger.info(
            "Franklin probate scrape: looking for estates filed on/after %s",
            since_date.isoformat(),
        )

        estate_rows = self._walk_date_index(session, since_date)
        logger.info("Found %d ESTATE rows in open-date index", len(estate_rows))

        records: list[NoticeData] = []
        for case_number, case_subtype, opened in estate_rows:
            detail = self._lookup_case(session, case_number)
            if detail is None:
                logger.debug("Case %s: detail fetch failed — skipping", case_number)
                continue
            # Prefer the list-row opened date (authoritative) if detail is missing one.
            if detail.filing_date is None:
                detail.filing_date = opened
            if detail.filing_date is None or detail.filing_date < since_date:
                continue
            record = self._to_notice_data(detail)
            records.append(record)
            logger.info(
                "  %s  %s  decedent=%s  fiduciary=%s (%s)",
                detail.filing_date.isoformat(),
                detail.case_number,
                detail.decedent_name,
                detail.fiduciary_name or "(none)",
                detail.fiduciary_title_desc or detail.fiduciary_title_code or "?",
            )

        records.sort(key=lambda r: (r.date_added, r.source_url))
        logger.info("Franklin probate scrape done — %d records", len(records))
        return records

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        """Session with browser headers — Akamai WAF requires them."""
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
        })
        return s

    def _sleep(self) -> None:
        delay = random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
        time.sleep(delay)

    def _get(self, session: requests.Session, url: str) -> BeautifulSoup | None:
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = session.get(url, timeout=30)
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    return BeautifulSoup(resp.text, "html.parser")
                logger.debug("GET %s → %d (attempt %d)", url, resp.status_code, attempt + 1)
            except requests.RequestException as e:
                logger.debug("GET %s raised %s (attempt %d)", url, e, attempt + 1)
            self._sleep()
        logger.warning("GET %s exhausted retries", url)
        return None

    # ── Index walk ─────────────────────────────────────────────────────
    def _walk_date_index(
        self,
        session: requests.Session,
        since_date: date,
    ) -> list[tuple[str, str, date]]:
        """Walk the open-date index forward, collecting ESTATE case tuples.

        Returns list of (case_number, case_subtype, filing_date). Deduped.
        """
        url = f"{DATE_SEARCH_URL}?string={since_date.strftime('%Y%m%d')}"
        seen: set[str] = set()
        estate_rows: list[tuple[str, str, date]] = []
        last_cursor: str | None = None

        for page_idx in range(MAX_PAGES):
            soup = self._get(session, url)
            self._sleep()
            if soup is None:
                break

            text = soup.get_text(" ", strip=True)
            if "No Dates Found" in text:
                break

            page_rows = self._parse_list_page(soup)
            if not page_rows:
                break

            for row in page_rows:
                case_number = row["case_number"]
                if case_number in seen:
                    continue
                seen.add(case_number)
                if row["case_type"] != "ESTATE":
                    continue
                if row["opened_date"] is None or row["opened_date"] < since_date:
                    continue
                estate_rows.append((case_number, row["case_subtype"], row["opened_date"]))

            next_cursor = self._extract_next_cursor(soup)
            if next_cursor is None or next_cursor == END_SENTINEL_CURSOR:
                break
            if next_cursor == last_cursor:
                logger.debug("Pagination cursor did not advance — stopping.")
                break
            last_cursor = next_cursor
            url = f"{DATE_SEARCH_URL}?stringf={next_cursor}"

        if page_idx == MAX_PAGES - 1:
            logger.warning("Hit MAX_PAGES=%d cap on index walk", MAX_PAGES)
        return estate_rows

    def _parse_list_page(self, soup: BeautifulSoup) -> list[dict]:
        """Extract data rows from an open-date index page."""
        rows = soup.find_all("tr", bgcolor=re.compile(r"lightblue|white", re.I))
        out: list[dict] = []
        for tr in rows:
            cells = tr.find_all("td")
            if len(cells) < EXPECTED_COLS:
                continue
            case_no = self._clean(cells[COL_CASE_NUMBER].get_text(" ", strip=True))
            name = self._clean(cells[COL_CASE_NAME].get_text(" ", strip=True))
            ctype = self._clean(cells[COL_TYPE].get_text(" ", strip=True))
            csub = self._clean(cells[COL_SUBTYPE].get_text(" ", strip=True))
            status = self._clean(cells[COL_STATUS].get_text(" ", strip=True))
            opened = self._parse_mdy(cells[COL_OPENED].get_text(" ", strip=True))
            closed = self._parse_mdy(cells[COL_CLOSED].get_text(" ", strip=True))
            if not case_no:
                continue
            out.append({
                "case_number": case_no,
                "case_name": name,
                "case_type": ctype.upper(),
                "case_subtype": csub,
                "status_code": status,
                "opened_date": opened,
                "closed_date": closed,
            })
        return out

    def _extract_next_cursor(self, soup: BeautifulSoup) -> str | None:
        """Pull the `stringf=` value out of the first Next Cases link."""
        for a in soup.find_all("a", href=True):
            m = re.search(r"stringf=([0-9]+)", a["href"])
            if m:
                return m.group(1)
        return None

    # ── Case detail + fiduciary ────────────────────────────────────────
    def _lookup_case(self, session: requests.Session, case_number: str) -> _CaseDetail | None:
        detail_url = f"{ESTATE_DETAIL_BASE}?caseno={case_number};;"
        detail_soup = self._get(session, detail_url)
        if detail_soup is None:
            return None
        self._sleep()

        fidy_url = f"{FIDUCIARY_BASE}?caseno={case_number};;"
        fidy_soup = self._get(session, fidy_url)
        self._sleep()

        return self._parse_detail(
            detail_soup=detail_soup,
            fidy_soup=fidy_soup,
            detail_url=detail_url,
            case_number=case_number,
        )

    def _parse_detail(
        self,
        detail_soup: BeautifulSoup,
        fidy_soup: BeautifulSoup | None,
        detail_url: str,
        case_number: str,
    ) -> _CaseDetail | None:
        """Extract canonical fields from detail + fiduciary pages."""
        field_map = self._extract_label_value_map(detail_soup)

        # Detail-page fields (labels come from the left-column <th>).
        case_name = self._clean(field_map.get("Case Name", ""))
        case_subtype = self._clean(field_map.get("Case Subtype", ""))
        decedent_street = self._clean(field_map.get("Decedent Street", ""))
        decedent_city = self._clean(field_map.get("City", ""))
        decedent_state = self._clean(field_map.get("State", "")) or "OH"
        decedent_zip = self._clean(field_map.get("Zip", ""))
        date_of_death = self._parse_mdy(field_map.get("Date of Death", ""))
        date_opened = self._parse_mdy(field_map.get("Date Opened", ""))

        # Fiduciary page.
        fid_name = ""
        fid_title_code = ""
        fid_title_desc = ""
        fid_appt = None
        atty_name = ""
        if fidy_soup is not None:
            fid_row = self._first_fiduciary_row(fidy_soup)
            if fid_row:
                fid_name = fid_row.get("name", "")
                fid_title_code = fid_row.get("title_code", "")
                fid_title_desc = fid_row.get("title_desc", "")
                fid_appt = fid_row.get("appt_date")
                atty_name = fid_row.get("attorney_name", "")

        raw_text = (
            f"Case Number: {case_number}\n"
            f"Decedent: {case_name}\n"
            f"Case Subtype: {case_subtype}\n"
            f"Decedent Address: {decedent_street}, {decedent_city}, {decedent_state} {decedent_zip}\n"
            f"Date of Death: {date_of_death.isoformat() if date_of_death else 'unknown'}\n"
            f"Date Opened: {date_opened.isoformat() if date_opened else 'unknown'}\n"
            f"Fiduciary: {fid_name} ({fid_title_code} {fid_title_desc})\n"
            f"Appt Date: {fid_appt.isoformat() if fid_appt else 'n/a'}\n"
            f"Attorney: {atty_name}\n"
        )

        return _CaseDetail(
            case_number=case_number,
            case_subtype=case_subtype,
            status_code="",
            filing_date=date_opened,
            decedent_name=case_name,
            decedent_street=decedent_street,
            decedent_city=decedent_city,
            decedent_state=decedent_state,
            decedent_zip=decedent_zip,
            date_of_death=date_of_death,
            fiduciary_name=fid_name,
            fiduciary_title_code=fid_title_code,
            fiduciary_title_desc=fid_title_desc,
            fiduciary_appt_date=fid_appt,
            attorney_name=atty_name,
            detail_url=detail_url,
            raw_text=raw_text,
        )

    def _extract_label_value_map(self, soup: BeautifulSoup) -> dict[str, str]:
        """Pair up <th> labels with their sibling <td> values on detail pages.

        The NetData detail layout is: <tr><th>Label</th><td>Value</td></tr>
        repeated. Some rows have multiple value cells (street+city+state+zip
        each have their own label row); we key everything by label text.
        """
        result: dict[str, str] = {}
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            if th is None:
                continue
            label = self._clean(th.get_text(" ", strip=True)).rstrip(":")
            if not label:
                continue
            tds = tr.find_all("td")
            if not tds:
                continue
            # Concatenate multi-td values with space.
            value = " ".join(td.get_text(" ", strip=True) for td in tds)
            value = self._clean(value)
            if value and value != "N/A":
                result[label] = value
        return result

    def _first_fiduciary_row(self, soup: BeautifulSoup) -> dict | None:
        """Return the first non-empty fiduciary row from the Fidy page.

        Schema (9 columns): Fid No | Name | Title Code | Title Desc |
        Appt Date | Term Date | Date Case Closed | Atty Num | Atty Name.
        """
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 9:
                continue
            vals = [self._clean(c.get_text(" ", strip=True)) for c in cells]
            # Filter out nav rows and empties.
            if not any(vals):
                continue
            if "New Search" in " ".join(vals) or "Probate Homepage" in " ".join(vals):
                continue
            # Fiduciary No. is typically "01", "02", ...; skip if not numeric.
            fid_no = vals[0]
            if not re.fullmatch(r"\d{1,3}", fid_no):
                continue
            return {
                "fid_no": fid_no,
                "name": vals[1],
                "title_code": vals[2],
                "title_desc": vals[3],
                "appt_date": self._parse_mdy(vals[4]),
                "term_date": self._parse_mdy(vals[5]),
                "closed_date": self._parse_mdy(vals[6]),
                "attorney_number": vals[7],
                "attorney_name": vals[8] if vals[8] != "N/A" else "",
            }
        return None

    # ── Date / text helpers ────────────────────────────────────────────
    @staticmethod
    def _parse_mdy(raw: str) -> date | None:
        """Parse a MM/DD/YYYY string into a date. Returns None on failure."""
        if not raw:
            return None
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if not m:
            return None
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ""
        cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(self, detail: _CaseDetail) -> NoticeData:
        """Project a parsed _CaseDetail onto the pipeline's NoticeData shape."""
        relationship, _ = TITLE_CODE_MAP.get(
            detail.fiduciary_title_code,
            ("personal_representative", detail.fiduciary_title_desc or ""),
        )

        return NoticeData(
            date_added=detail.filing_date.isoformat() if detail.filing_date else "",
            county=self.county,
            state="OH",
            notice_type=self.notice_type,
            source_url=detail.detail_url,
            # Property address intentionally blank per task spec — the
            # enrichment pipeline's probate property-lookup step handles
            # resolution. Decedent residence is preserved in raw_text.
            address="",
            city="",
            zip="",
            # Decision-maker / fiduciary (applicant, executor, etc.) is our contact.
            owner_name=detail.fiduciary_name,
            decedent_name=detail.decedent_name,
            decision_maker_name=detail.fiduciary_name,
            decision_maker_relationship=relationship,
            decision_maker_source="court_record",
            decision_maker_status="unverified",
            # Fiduciary mailing address not publicly exposed on Franklin
            # portal (docket-only) — left empty, enrichment pipeline fills.
            owner_street="",
            owner_city="",
            owner_state="",
            owner_zip="",
            decision_maker_street="",
            decision_maker_city="",
            decision_maker_state="",
            decision_maker_zip="",
            date_of_death=detail.date_of_death.isoformat() if detail.date_of_death else "",
            raw_text=detail.raw_text,
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

    parser = argparse.ArgumentParser(description="Test Franklin probate scraper")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_franklin_probate.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} probate records since {since}")

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
