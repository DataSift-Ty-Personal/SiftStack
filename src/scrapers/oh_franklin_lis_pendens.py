"""Franklin County (Columbus), Ohio Lis Pendens / foreclosure case-filing scraper.

Pulls residential foreclosure CASE FILINGS — the lis pendens stage, 4-12 weeks
BEFORE the sheriff sale — from the Franklin County Clerk of Courts'
Case Information Online (CIO) portal at
fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/.

Why this is "Lis Pendens"
-------------------------
In Ohio practice, when a lender files a foreclosure complaint in the Court of
Common Pleas, it simultaneously files a Lis Pendens notice (RC 2703.26) on the
property. The case appears in CIO immediately as a Civil filing with
"TYPE of CASE = FORECLOSURES" and the docket shows a "LIS PENDENS FILED" entry
on the same day. This is the EARLIEST public signal in the foreclosure
lifecycle — months before the case ripens to a sheriff sale on RealAuction.

Why we don't search the literal "LP" case type
----------------------------------------------
The CIO portal exposes "LP - Lis Pendens" as a case type code, but in practice
only ~3-15 records per year file under LP — those are mostly federal IRS or
mechanic's-lien notices filed against a parcel without a parent civil action.
Real residential mortgage-foreclosure lis pendens are filed under the
**CV (Civil)** case type with case-description "FORECLOSURES" — typically
~50-200 per 14-day window in Franklin County. We capture that population.

Portal characteristics (researched against live site)
-----------------------------------------------------
* Tech stack: IBM WebSphere / JSP, server-rendered HTML, no JS required for
  the data we need, no login, no CAPTCHA, no rate limiting beyond polite
  pacing.
* Disclaimer wall: GET / returns a "I AGREE" form. POST acceptDisclaimer
  with `fromPage=index&Accept=ACCEPT` issues a session JSESSIONID and lands
  us on the search dashboard.
* Search by name / date range:
      POST /CaseInformationOnline/nameSearch
      body: setField=1&lname=<prefix>&txtCalendar1=MM/DD/YYYY
            &txtCalendar2=MM/DD/YYYY&recs=350&advFlag=show
            &reallySubmit=true&selType=
  Returns a "Case Listing" HTML table with one <tr> per (case, party) pair
  (so the same case appears once per matching plaintiff/defendant). Each
  row has CASE NUMBER (e.g. "26 CV 003696"), CASE TYPE description (e.g.
  "FORECLOSURES"), party NAME, P/D indicator, plaintiff-vs-defendant
  description ("PLAINTIFF -VS- DEFENDANT ET AL"), FILED date, STATUS.
* The portal silently caps results at ~268-350 rows per query regardless
  of the `recs` setting. Single-letter prefixes (lname=A..Z) are
  insufficient for high-volume letters — we auto-fall-back to two-letter
  prefixes when a letter hits the cap.
* CAUTION: the form has a `selType` filter that maps to court division
  ("Civil"/"Criminal"/...). Setting selType=Civil counterintuitively caps
  output at 25 rows. We leave selType="" (All) so we get full pages.
* Case detail page:
      POST /CaseInformationOnline/caseSearch
      body: setField=2&caseYear=YY&caseType=CV&caseSeq=NNNNNN
            &caseYear_h=YY&caseType_h=CV&caseSeq_h=NNNNNN&advFlag=show
            &reallySubmit=true&selType=
  Returns "Civil Case Detail" with PLAINTIFF(S) and DEFENDANT(S) blocks.
  The detail page reveals each defendant's mailing address — the FIRST
  non-government defendant's address IS the foreclosed property address.
  (Govt parties like FRANKLIN COUNTY TREASURER, FRANKLIN COUNTY AUDITOR,
  STATE OF OHIO, UNITED STATES OF AMERICA are joined for lien priority,
  not as homeowners.)

NoticeData contract per CLAUDE.md domain rules
----------------------------------------------
* county = "Franklin", state = "OH", notice_type = "lis_pendens".
* date_added = case filing date (YYYY-MM-DD) — this IS the lis pendens date.
* address / city / zip = property address (FIRST homeowner-defendant's address).
* owner_name = the homeowner-defendant. ", et al." truncations cleaned.
* auction_date intentionally blank — this is the pre-sale stage, no sheriff
  sale has been scheduled yet (RealAuction sale dates surface 4-12 weeks
  later through the separate Franklin foreclosure scraper).
* raw_text = a structured blob with case#, plaintiff, all defendants,
  filing date — preserved for downstream classification + diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

# Portal URLs.
BASE_URL = "https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline"
LANDING_URL = f"{BASE_URL}/"
ACCEPT_URL = f"{BASE_URL}/acceptDisclaimer"
NAME_SEARCH_URL = f"{BASE_URL}/nameSearch"
CASE_SEARCH_URL = f"{BASE_URL}/caseSearch"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Server caps results per query around 268-350 rows. If we see >= this many,
# we suspect the result was truncated and fan out to two-letter prefixes.
CAP_FANOUT_THRESHOLD = 250

# Maximum cases to look up in detail per scrape. Hard ceiling against runaway
# CPU on broken queries — 14 days of Franklin County foreclosure filings
# generally tops out below 250.
MAX_DETAIL_LOOKUPS = 1000

# Government / institutional defendants whose mailing address is NOT the
# foreclosed property. Match a defendant to one of these patterns and skip
# its address when picking the property address.
GOVT_DEFENDANT_PATTERNS = [
    re.compile(r"\bCOUNTY\s+TREASURER\b", re.I),
    re.compile(r"\bCOUNTY\s+AUDITOR\b", re.I),
    re.compile(r"\bSTATE\s+OF\s+OHIO\b", re.I),
    re.compile(r"\bUNITED\s+STATES\b", re.I),
    re.compile(r"\bINTERNAL\s+REVENUE\s+SERVICE\b", re.I),
    re.compile(r"\bIRS\b", re.I),
    re.compile(r"\bDEPARTMENT\s+OF\s+TAXATION\b", re.I),
    re.compile(r"\bCITY\s+OF\s+COLUMBUS\b", re.I),
    re.compile(r"\bMEDICAID\b", re.I),
    re.compile(r"\bUNKNOWN\s+SPOUSE\b", re.I),
    re.compile(r"\bUNKNOWN\s+HEIRS\b", re.I),
]

# "John Doe" / placeholder defendants (commonly added so the bank can amend
# in the real party later) — skip when picking the homeowner.
PLACEHOLDER_DEFENDANT_PATTERNS = [
    re.compile(r"^\s*JOHN\s+DOE\s*$", re.I),
    re.compile(r"^\s*JANE\s+DOE\s*$", re.I),
    re.compile(r"^\s*UNKNOWN\b", re.I),
]

CASE_NUMBER_RE = re.compile(r"\b(\d{2})\s+(CV)\s+(\d{6})\b")
LISTING_ROW_RE = re.compile(
    r'value="(?P<casetag>\d{2}\s+CV\s+\d{6})\s*"[^>]*/></td>'
    r"<td>(?P<casetype>[^<]*)</td>"
    r"<td>(?P<party>[^<]*)</td>"
    r"<td>[^<]*</td>"      # ITN
    r"<td>[^<]*</td>"      # M/F
    r"<td>(?P<pd>[^<]*)</td>"   # P/D
    r"<td>[^<]*</td>"      # DOB
    r"<td>(?P<descr>[^<]*)</td>"
    r"<td>(?P<filed>\d{2}/\d{2}/\d{4})</td>"
    r"<td>(?P<status>[^<]*)</td>",
    re.I,
)

# Address lines on the detail page render as e.g. "1276 WOODNELL AVE<br/>COLUMBUS, OH 43219"
ADDRESS_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>[A-Z .'\-]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


@dataclass
class _ListingRow:
    case_number: str       # "26CV003696" (no spaces)
    case_year: str         # "26"
    case_type: str         # "CV"
    case_seq: str          # "003696"
    case_descr: str        # "FORECLOSURES"
    filing_date: date | None
    status: str
    party_name: str        # The matched lname-prefix party (could be plaintiff or defendant)


@dataclass
class _CaseDetail:
    case_number: str
    case_descr: str
    status: str
    filing_date: date | None
    plaintiff_name: str
    plaintiff_attorney: str
    defendants: list[dict] = field(default_factory=list)
    homeowner_name: str = ""
    property_street: str = ""
    property_city: str = ""
    property_state: str = "OH"
    property_zip: str = ""
    detail_url: str = ""
    raw_text: str = ""


class Scraper(NoticeScraper):
    """Franklin County Common Pleas — residential foreclosure case-filing scraper.

    Each record represents one CV-FORECLOSURES case filed within the date
    window. The case filing IS the lis pendens — `notice_type="lis_pendens"`.
    """

    county = "Franklin"
    notice_type = "lis_pendens"
    source_name = "Franklin County Common Pleas — Foreclosure Cases"
    source_url = LANDING_URL

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        if since_date is None:
            since_date = date.today() - timedelta(days=14)
        until_date = date.today()
        return await asyncio.to_thread(self._scrape_sync, since_date, until_date)

    # ── Sync implementation ────────────────────────────────────────────
    def _scrape_sync(self, since_date: date, until_date: date) -> list[NoticeData]:
        session = self._build_session()
        if not self._accept_disclaimer(session):
            logger.warning("Failed to accept CIO disclaimer — aborting scrape.")
            return []
        logger.info(
            "Franklin Common Pleas lis pendens: filings %s → %s",
            since_date.isoformat(),
            until_date.isoformat(),
        )

        listings = self._collect_foreclosure_listings(session, since_date, until_date)
        logger.info(
            "Found %d unique CV-FORECLOSURES cases in window", len(listings)
        )
        if len(listings) > MAX_DETAIL_LOOKUPS:
            logger.warning(
                "Capping detail lookups at %d (got %d candidates)",
                MAX_DETAIL_LOOKUPS, len(listings),
            )
            listings = listings[:MAX_DETAIL_LOOKUPS]

        records: list[NoticeData] = []
        for row in listings:
            detail = self._fetch_case_detail(session, row)
            if detail is None:
                logger.debug("Case %s — detail fetch failed", row.case_number)
                continue
            record = self._to_notice_data(detail)
            records.append(record)
            logger.info(
                "  %s  %s  plaintiff=%s  homeowner=%s  property=%s",
                record.date_added,
                row.case_number,
                detail.plaintiff_name or "(blank)",
                detail.homeowner_name or "(blank)",
                detail.property_street or "(blank)",
            )

        records.sort(key=lambda r: (r.date_added, r.source_url))
        logger.info("Franklin lis pendens scrape done — %d records", len(records))
        return records

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })
        return s

    def _sleep(self) -> None:
        delay = random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
        time.sleep(delay)

    def _accept_disclaimer(self, session: requests.Session) -> bool:
        """GET / and POST acceptDisclaimer to seed the session."""
        try:
            r1 = session.get(LANDING_URL, timeout=30)
            if r1.status_code != 200:
                logger.debug("Landing GET → %d", r1.status_code)
                return False
            self._sleep()
            r2 = session.post(
                ACCEPT_URL,
                data={"fromPage": "index", "Accept": "ACCEPT"},
                timeout=30,
                headers={"Referer": LANDING_URL},
            )
            if r2.status_code != 200:
                logger.debug("Disclaimer POST → %d", r2.status_code)
                return False
            return True
        except requests.RequestException as e:
            logger.debug("Disclaimer flow raised: %s", e)
            return False

    def _post(
        self,
        session: requests.Session,
        url: str,
        data: dict,
        referer: str | None = None,
    ) -> BeautifulSoup | None:
        for attempt in range(config.MAX_RETRIES):
            try:
                self._sleep()
                headers = {"Referer": referer} if referer else {}
                resp = session.post(url, data=data, timeout=45, headers=headers)
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "ISO-8859-1"
                    return BeautifulSoup(resp.text, "html.parser")
                logger.debug("POST %s → %d (attempt %d)", url, resp.status_code, attempt + 1)
            except requests.RequestException as e:
                logger.debug("POST %s raised %s (attempt %d)", url, e, attempt + 1)
        return None

    # ── Listing collection ─────────────────────────────────────────────
    def _collect_foreclosure_listings(
        self,
        session: requests.Session,
        since_date: date,
        until_date: date,
    ) -> list[_ListingRow]:
        """Walk the alphabet collecting CV-FORECLOSURES rows in the window.

        Single-letter prefixes are tried first; any letter whose result
        approaches the server cap (>= CAP_FANOUT_THRESHOLD rows) is
        re-queried with two-letter prefixes for that letter.
        """
        seen: dict[str, _ListingRow] = {}
        capped_letters: list[str] = []

        for letter in string.ascii_uppercase:
            rows, total = self._search_prefix(session, letter, since_date, until_date)
            for row in rows:
                if row.case_number not in seen:
                    seen[row.case_number] = row
            if total >= CAP_FANOUT_THRESHOLD:
                capped_letters.append(letter)

        if capped_letters:
            logger.info(
                "Letters %s hit row cap — fanning out to two-letter prefixes",
                ",".join(capped_letters),
            )
            for first in capped_letters:
                for second in string.ascii_uppercase:
                    prefix = f"{first}{second}"
                    rows, _ = self._search_prefix(session, prefix, since_date, until_date)
                    for row in rows:
                        if row.case_number not in seen:
                            seen[row.case_number] = row

        # Keep only rows with parseable filing dates inside the window.
        in_window = [
            r for r in seen.values()
            if r.filing_date is not None
            and since_date <= r.filing_date <= until_date
        ]
        in_window.sort(key=lambda r: (r.filing_date or date.min, r.case_number))
        return in_window

    def _search_prefix(
        self,
        session: requests.Session,
        prefix: str,
        since_date: date,
        until_date: date,
    ) -> tuple[list[_ListingRow], int]:
        """Run one nameSearch for an lname-prefix. Returns (FORECLOSURES rows, total rows).

        The "total rows" count is over ALL case types in the response and is
        used to detect server-side truncation (so we can fan out further).
        """
        data = {
            "setField": "1",
            "lname": prefix,
            "fname": "",
            "mint": "",
            "selType": "",          # All courts (NOT "Civil" — that caps output at 25)
            "caseYear": "",
            "caseSeq": "",
            "caseType": "",
            "caseType_h": "",
            "caseYear_h": "",
            "caseSeq_h": "",
            "txtCalendar1": since_date.strftime("%m/%d/%Y"),
            "txtCalendar2": until_date.strftime("%m/%d/%Y"),
            "recs": "350",
            "advFlag": "show",
            "reallySubmit": "true",
            "personType": "P",
            "attyNum": "",
            "attyIdx": "",
        }
        soup = self._post(session, NAME_SEARCH_URL, data, referer=LANDING_URL)
        if soup is None:
            return [], 0

        text = soup.get_text(" ", strip=True)
        if "NO NAMES MATCHED" in text.upper():
            return [], 0

        rows, total = self._parse_listing(soup.decode())
        # Filter to FORECLOSURES type AND within date window.
        keep = [
            r for r in rows
            if "FORECLOSURE" in r.case_descr.upper()
            and r.filing_date is not None
            and since_date <= r.filing_date <= until_date
        ]
        logger.debug(
            "lname=%-3s  total_rows=%d  FORECLOSURES=%d",
            prefix, total, len(keep),
        )
        return keep, total

    def _parse_listing(self, html: str) -> tuple[list[_ListingRow], int]:
        """Extract case rows from a Case Listing page.

        We use a regex against the raw HTML (rather than BeautifulSoup row
        traversal) because the listing renders one <tr> per (case, party)
        pair on a single line and the markup is messy enough that a regex
        is more reliable than nested BS lookups.
        """
        rows: list[_ListingRow] = []
        total = 0
        for m in LISTING_ROW_RE.finditer(html):
            total += 1
            casetag = m.group("casetag").strip()      # e.g. "26 CV 003696"
            cm = CASE_NUMBER_RE.search(casetag)
            if not cm:
                continue
            year, ctype, seq = cm.group(1), cm.group(2), cm.group(3)
            case_no_compact = f"{year}{ctype}{seq}"
            try:
                filed = datetime.strptime(m.group("filed"), "%m/%d/%Y").date()
            except ValueError:
                filed = None
            # NOTE on column mapping: in this listing layout the regex group
            # `casetype` holds the human-readable CASE TYPE column ("FORECLOSURES",
            # "WORKERS COMPENSATION", etc.) — that's what we filter on. The
            # `descr` group holds the "X -VS- Y ET AL" description blurb (not
            # used for filtering). The structural type code ("CV") is parsed
            # out of the casetag separately.
            rows.append(_ListingRow(
                case_number=case_no_compact,
                case_year=year,
                case_type=ctype,
                case_seq=seq,
                case_descr=self._clean(m.group("casetype")),
                filing_date=filed,
                status=self._clean(m.group("status")),
                party_name=self._clean(m.group("party")),
            ))
        return rows, total

    # ── Case detail ────────────────────────────────────────────────────
    def _fetch_case_detail(
        self,
        session: requests.Session,
        row: _ListingRow,
    ) -> _CaseDetail | None:
        data = {
            "setField": "2",
            "caseYear": row.case_year,
            "caseYear_h": row.case_year,
            "caseType": row.case_type,
            "caseType_h": row.case_type,
            "caseSeq": row.case_seq,
            "caseSeq_h": row.case_seq,
            "selType": "",
            "lname": "",
            "fname": "",
            "mint": "",
            "advFlag": "show",
            "reallySubmit": "true",
            "personType": "P",
        }
        soup = self._post(session, CASE_SEARCH_URL, data, referer=LANDING_URL)
        if soup is None:
            return None
        # Build a stable detail URL for the source_url field (uses GET-style
        # params for human readability — the portal accepts them on POST).
        detail_url = f"{CASE_SEARCH_URL}?" + urlencode({
            "caseYear": row.case_year,
            "caseType": row.case_type,
            "caseSeq": row.case_seq,
        })
        return self._parse_detail(soup, row, detail_url)

    def _parse_detail(
        self,
        soup: BeautifulSoup,
        row: _ListingRow,
        detail_url: str,
    ) -> _CaseDetail | None:
        """Extract plaintiff, defendants, and the property address from a detail page."""
        # Validate this is a case-detail response (not an error redirect).
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if "Civil Case Detail" not in title and "Case Detail" not in title:
            logger.debug("Case %s detail page title=%r — skipping", row.case_number, title)
            return None

        # Re-parse case header (the header table echoes the case number, type,
        # status, and date filed in a labeled row near the top of the page).
        # The layout repeats "<td>26 CV 003696</td><td>FORECLOSURES</td>
        # <td>ACTIVE</td><td>04/20/2026</td>" — pull authoritative values
        # from there in case the listing row was stale.
        case_descr = row.case_descr
        status = row.status
        filing_date = row.filing_date
        header_pat = re.compile(
            rf"{row.case_year}\s*{row.case_type}\s*{row.case_seq}\b.*?</td>"
            rf"\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>(\d{{2}}/\d{{2}}/\d{{4}})</td>",
            re.S,
        )
        m = header_pat.search(soup.decode())
        if m:
            case_descr = self._clean(m.group(1)) or case_descr
            status = self._clean(m.group(2)) or status
            try:
                filing_date = datetime.strptime(m.group(3), "%m/%d/%Y").date()
            except ValueError:
                pass

        plaintiff_name, plaintiff_atty = self._extract_first_party(soup, "plaintiff-body")
        defendants = self._extract_defendants(soup)

        homeowner_name, p_street, p_city, p_state, p_zip = self._pick_property(defendants)

        # Build raw_text with everything useful — this is what downstream
        # enrichment + report generators will read.
        defs_blob = "; ".join(
            f"{d['name']} @ {d.get('address', '')}".strip(" @")
            for d in defendants
        )
        raw_text = (
            f"Court: Franklin County Court of Common Pleas\n"
            f"Case Number: {row.case_year} {row.case_type} {row.case_seq}\n"
            f"Case Type: {case_descr}\n"
            f"Status: {status}\n"
            f"Date Filed (Lis Pendens): {filing_date.isoformat() if filing_date else 'unknown'}\n"
            f"Plaintiff: {plaintiff_name}\n"
            f"Plaintiff Attorney: {plaintiff_atty}\n"
            f"Defendants: {defs_blob}\n"
            f"Homeowner: {homeowner_name}\n"
            f"Property Address: {p_street}, {p_city}, {p_state} {p_zip}\n"
        )

        return _CaseDetail(
            case_number=row.case_number,
            case_descr=case_descr,
            status=status,
            filing_date=filing_date,
            plaintiff_name=plaintiff_name,
            plaintiff_attorney=plaintiff_atty,
            defendants=defendants,
            homeowner_name=homeowner_name,
            property_street=p_street,
            property_city=p_city,
            property_state=p_state,
            property_zip=p_zip,
            detail_url=detail_url,
            raw_text=raw_text,
        )

    def _extract_first_party(
        self,
        soup: BeautifulSoup,
        tbody_id: str,
    ) -> tuple[str, str]:
        """Return (name, attorney) for the first party row in the named tbody."""
        tbody = soup.find("tbody", id=tbody_id)
        if not tbody:
            return "", ""
        # First TR = name + attorney; first cell after the +/- toggle is the name.
        first_tr = tbody.find("tr")
        if not first_tr:
            return "", ""
        cells = first_tr.find_all("td")
        # Layout: [+/- img cell] [name] [attorney]
        if len(cells) >= 3:
            return self._clean(cells[1].get_text(" ", strip=True)), \
                   self._clean(cells[2].get_text(" ", strip=True))
        return "", ""

    def _extract_defendants(self, soup: BeautifulSoup) -> list[dict]:
        """Return list of {name, attorney, address, city, state, zip} for each defendant."""
        tbody = soup.find("tbody", id="defendant-body")
        if not tbody:
            return []
        out: list[dict] = []
        # The tbody alternates: name-row, detail-row, name-row, detail-row, ...
        # name-row has 3 cells; detail-row is hidden and contains the address.
        trs = tbody.find_all("tr", recursive=False)
        i = 0
        while i < len(trs):
            tr = trs[i]
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 3:
                i += 1
                continue
            name = self._clean(cells[1].get_text(" ", strip=True))
            attorney = self._clean(cells[2].get_text(" ", strip=True))
            # Sibling detail row: id="defdetail0001"
            address_block = ""
            if i + 1 < len(trs):
                next_tr = trs[i + 1]
                if next_tr.get("id", "").startswith("defdetail"):
                    detail_cells = next_tr.find_all("td", recursive=False)
                    if len(detail_cells) >= 2:
                        # Second cell holds the address with <br/> separators.
                        address_block = detail_cells[1].get_text("\n", strip=True)
                    i += 2
                else:
                    i += 1
            else:
                i += 1
            street, city, state, zipc = self._parse_address_block(address_block)
            out.append({
                "name": name,
                "attorney": attorney,
                "address": address_block.replace("\n", ", "),
                "street": street,
                "city": city,
                "state": state,
                "zip": zipc,
            })
        return out

    def _parse_address_block(
        self,
        block: str,
    ) -> tuple[str, str, str, str]:
        """Split a multi-line address into (street, city, state, zip)."""
        if not block:
            return "", "", "", ""
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            return "", "", "", ""
        # The last line is City, State Zip; everything before is street (apt/suite/etc).
        last = lines[-1]
        m = ADDRESS_CITY_STATE_ZIP_RE.match(last)
        if m:
            city, state, zipc = m.group("city").strip(), m.group("state").strip(), m.group("zip").strip()
            street = ", ".join(lines[:-1]).strip()
            return street, city, state, zipc
        # Fallback: no parsable city/state/zip — return raw as street.
        return ", ".join(lines), "", "", ""

    def _pick_property(
        self,
        defendants: list[dict],
    ) -> tuple[str, str, str, str, str]:
        """Choose the homeowner-defendant + property address.

        Walks defendants in filing order, skipping government / placeholder
        entries, and returns (homeowner_name, street, city, state, zip)
        from the first qualifying defendant whose address has a parseable
        street component.
        """
        for d in defendants:
            name = d.get("name", "")
            if not name:
                continue
            if any(p.search(name) for p in GOVT_DEFENDANT_PATTERNS):
                continue
            if any(p.search(name) for p in PLACEHOLDER_DEFENDANT_PATTERNS):
                continue
            if not d.get("street"):
                # Address didn't parse — accept the name but no property address.
                continue
            return (
                self._clean_owner(name),
                d.get("street", ""),
                d.get("city", ""),
                d.get("state", "") or "OH",
                d.get("zip", ""),
            )
        # Nothing matched — fall back to the first non-govt name regardless of address.
        for d in defendants:
            name = d.get("name", "")
            if name and not any(p.search(name) for p in GOVT_DEFENDANT_PATTERNS):
                return self._clean_owner(name), "", "", "OH", ""
        return "", "", "", "OH", ""

    # ── Text helpers ───────────────────────────────────────────────────
    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

    @staticmethod
    def _clean_owner(raw: str) -> str:
        """Strip ", et al" / ", et al." / trailing junk from a defendant name."""
        if not raw:
            return ""
        cleaned = re.sub(r"\s+", " ", raw).strip()
        cleaned = re.sub(
            r",\s*e(?:t(?:\.|\s+a(?:l\.?)?)?)?\.?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(r"\s+et\.?\s*al\.?\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned.rstrip(",").strip()

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(self, detail: _CaseDetail) -> NoticeData:
        return NoticeData(
            date_added=detail.filing_date.isoformat() if detail.filing_date else "",
            auction_date="",   # Pre-sale stage — no sheriff sale scheduled yet.
            county=self.county,
            state="OH",
            notice_type=self.notice_type,
            source_url=detail.detail_url,
            address=detail.property_street,
            city=detail.property_city,
            zip=detail.property_zip,
            owner_name=detail.homeowner_name,
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

    parser = argparse.ArgumentParser(description="Test Franklin lis pendens scraper")
    parser.add_argument("--days", type=int, default=14, help="Look back N days (default 14)")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_franklin_lis_pendens.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} lis pendens records since {since}")

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
