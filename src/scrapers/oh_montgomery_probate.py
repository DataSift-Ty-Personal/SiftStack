"""Montgomery County, Ohio probate case scraper.

Pulls estate filings from the county Probate Court's ColdFusion search form
(go.mcohio.org/applications/probate/prodcfm/casesearchx.cfm). Estate filings
expose the decedent and the fiduciary (executor/PR) — the fiduciary is the
decision-maker we contact.

Portal characteristics (researched against live site):
  * Tech stack: ColdFusion (*.cfm), server-rendered HTML, no JS required.
  * No login, no CAPTCHA.
  * Search form only supports name prefix or case-year+number lookup —
    NO date-range filter is exposed. Name-prefix queries are capped at
    ~500 rows (silent truncation), so prefix sweeps miss recent cases.
  * Reliable strategy: probe case numbers directly for the current year
    (`caseyear=YYYY&casenbr=NNNNN`). Sequential integer numbering makes
    this deterministic and efficient for short lookback windows.
  * A POST to `casesearch_actionx.cfm` returns a 1-row list page; the row
    links to `casesearchresultx.cfm?TOKEN` (obfuscated session-scoped id)
    which renders the detail page with decedent, fiduciary + mailing
    address, case type, open/closed date, and attorney.
  * The Case Status row ("OPEN  MM-DD-YYYY" or "CLOSED MM-DD-YYYY") holds
    the most recent status-change date, which for newly filed cases
    equals the filing date. The docket page's first entry is the true
    filing date when status has since changed.

NoticeData contract per CLAUDE.md domain rules:
  * owner_name = fiduciary (the PR / decision-maker we contact).
  * decedent_name = the deceased.
  * Property address intentionally left empty — Montgomery probate
    records carry no property address. The enrichment pipeline's probate
    property-lookup step resolves it downstream.
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
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://go.mcohio.org/applications/probate/prodcfm"
SEARCH_FORM_URL = f"{BASE_URL}/casesearchx.cfm"
SEARCH_ACTION_URL = f"{BASE_URL}/casesearch_actionx.cfm"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Case-number probing bounds. Filings are sequential per year; realistic
# upper bound for late April is ~1000, but we cap generously for safety.
# The walk stops short in practice — we quit as soon as the case's filing
# date drops below `since_date`.
MAX_PROBE_CASE_NUM = 9999          # CF field allows up to 5 digits; 2026 < 1000 today
MIN_WALK_CASE_NUM = 1              # Safety floor to prevent infinite loops
UPPER_BOUND_SEARCH_STEP = 50       # Step size when seeking the current-year max
MAX_EMPTY_STREAK = 50              # Consecutive missing case numbers before giving up

# Regex patterns — tolerant of the CF page's HTML/whitespace quirks.
CASE_NUMBER_RE = re.compile(r"20\d{2}EST\d+")
STATUS_DATE_RE = re.compile(r"(OPEN|CLOSED|REOPENED|PENDING)\s*(\d{2}-\d{2}-\d{4})", re.IGNORECASE)
DOCKET_DATE_RE = re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")


@dataclass
class _CaseDetail:
    """Internal tuple bundling the fields parsed out of a detail page."""
    case_number: str           # e.g. "2026EST00778"
    filing_date: date | None   # parsed from status-line or docket
    decedent_name: str
    fiduciary_name: str
    fiduciary_address: str     # free-text mailing line
    case_type_code: str        # e.g. "1"
    case_type_label: str       # e.g. "FULL ADMIN;PROBATE WILL"
    case_status: str           # "OPEN", "CLOSED", etc.
    attorney: str
    detail_url: str            # absolute URL including ?TOKEN
    raw_text: str              # full visible text for downstream classification


class Scraper(NoticeScraper):
    """Montgomery County Probate — estate (decedent) case scraper."""

    county = "Montgomery"
    notice_type = "probate"
    source_name = "Montgomery County Probate Court"
    source_url = SEARCH_FORM_URL

    # Track the last max case number seen — state file owned by the pipeline
    # can be wired up later. For now we re-discover the max each run.

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull all probate case filings on/after `since_date` (default: 7 days ago)."""
        if since_date is None:
            since_date = date.today() - timedelta(days=7)

        # Run the synchronous requests-based scraper in a thread so callers
        # that await this method don't block the event loop.
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Private sync implementation ────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        session = self._build_session()
        logger.info(
            "Montgomery probate scrape: looking for cases filed on/after %s",
            since_date.isoformat(),
        )

        target_year = date.today().year
        max_case = self._find_max_case_number(session, target_year)
        if max_case is None:
            logger.warning("Could not locate any %d estate cases — is the portal reachable?", target_year)
            return []
        logger.info("Most recent %d estate case number: %s", target_year, max_case)

        records: list[NoticeData] = []
        empty_streak = 0
        # Walk backward from max. We stop when we've seen enough consecutive
        # cases with filing_date < since_date, to tolerate out-of-order entries.
        stale_streak = 0
        stop_after_stale = 5

        for case_num in range(max_case, MIN_WALK_CASE_NUM - 1, -1):
            detail = self._lookup_case(session, target_year, case_num)
            if detail is None:
                empty_streak += 1
                if empty_streak >= MAX_EMPTY_STREAK:
                    logger.info(
                        "Hit %d consecutive missing case numbers — ending walk.",
                        MAX_EMPTY_STREAK,
                    )
                    break
                continue
            empty_streak = 0

            if detail.filing_date is None:
                logger.debug("Case %s: no filing date parsed — skipping", detail.case_number)
                continue

            if detail.filing_date < since_date:
                stale_streak += 1
                logger.debug(
                    "Case %s: filed %s is before cutoff %s (stale streak=%d)",
                    detail.case_number, detail.filing_date, since_date, stale_streak,
                )
                if stale_streak >= stop_after_stale:
                    logger.info(
                        "Reached %d consecutive cases older than %s — stopping.",
                        stop_after_stale, since_date,
                    )
                    break
                continue
            stale_streak = 0

            record = self._to_notice_data(detail)
            records.append(record)
            logger.info(
                "  %s  %s  decedent=%s  fiduciary=%s",
                detail.filing_date.isoformat(),
                detail.case_number,
                detail.decedent_name,
                detail.fiduciary_name or "(none yet)",
            )

        # Return oldest-first to match pipeline conventions.
        records.sort(key=lambda r: (r.date_added, r.source_url))
        logger.info("Montgomery probate scrape done — %d records", len(records))
        return records

    # ── HTTP helpers ───────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Warm cookies by hitting the form page once (harmless if it fails).
        try:
            s.get(SEARCH_FORM_URL, timeout=20)
        except requests.RequestException as e:
            logger.debug("Initial form GET failed (non-fatal): %s", e)
        return s

    def _sleep(self) -> None:
        """Rate-limit — 2-3s jittered delay between probate requests."""
        delay = random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
        time.sleep(delay)

    def _get(self, session: requests.Session, url: str) -> BeautifulSoup | None:
        """GET with retries + encoding handling, returns parsed soup or None."""
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = session.get(url, timeout=30)
                if resp.status_code == 200:
                    # ColdFusion declares utf-8 in meta tag; stick with that
                    # and fall back to apparent_encoding only if text looks garbled.
                    resp.encoding = resp.encoding or resp.apparent_encoding or "utf-8"
                    return BeautifulSoup(resp.text, "html.parser")
                logger.debug("GET %s → %d (attempt %d)", url, resp.status_code, attempt + 1)
            except requests.RequestException as e:
                logger.debug("GET %s raised %s (attempt %d)", url, e, attempt + 1)
            self._sleep()
        logger.warning("GET %s exhausted retries", url)
        return None

    def _post_search(
        self,
        session: requests.Session,
        caseyear: int | None = None,
        casenbr: str | None = None,
        last_name: str = "",
        first_name: str = "",
    ) -> BeautifulSoup | None:
        """POST the estate-search form. Returns the result LIST page soup."""
        data = {
            "caseyear": str(caseyear) if caseyear else "",
            "casenbr": casenbr or "",
            "own1": last_name,
            "own2": first_name,
            "SEARCH": "GO",
        }
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = session.post(
                    SEARCH_ACTION_URL,
                    data=data,
                    timeout=30,
                    headers={"Referer": SEARCH_FORM_URL},
                )
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    return BeautifulSoup(resp.text, "html.parser")
                logger.debug("POST search → %d (attempt %d)", resp.status_code, attempt + 1)
            except requests.RequestException as e:
                logger.debug("POST search raised %s (attempt %d)", e, attempt + 1)
            self._sleep()
        return None

    # ── Case lookup / detail parsing ───────────────────────────────────
    def _find_max_case_number(self, session: requests.Session, year: int) -> int | None:
        """Discover the highest-numbered case filed this year.

        Probes upward with `UPPER_BOUND_SEARCH_STEP` sized jumps until a hit
        disappears, then bisects to find the exact boundary. ~10 HTTP calls.
        """
        # Phase 1: exponential-ish expansion to bracket the max.
        last_hit: int | None = None
        probe = UPPER_BOUND_SEARCH_STEP
        while probe <= MAX_PROBE_CASE_NUM:
            if self._case_exists(session, year, probe):
                last_hit = probe
                probe += UPPER_BOUND_SEARCH_STEP
                self._sleep()
                continue
            self._sleep()
            break

        if last_hit is None:
            # Very early in the year — walk 1..50 for a low bound.
            for n in range(1, UPPER_BOUND_SEARCH_STEP + 1):
                if self._case_exists(session, year, n):
                    last_hit = n
                    self._sleep()
                else:
                    self._sleep()
                    break
            if last_hit is None:
                return None

        # Phase 2: linear search from last_hit upward until first miss.
        current = last_hit
        consecutive_miss = 0
        while consecutive_miss < 5 and current < MAX_PROBE_CASE_NUM:
            current += 1
            if self._case_exists(session, year, current):
                last_hit = current
                consecutive_miss = 0
            else:
                consecutive_miss += 1
            self._sleep()
        return last_hit

    def _case_exists(self, session: requests.Session, year: int, case_num: int) -> bool:
        """Quick existence check — does caseyear=Y & casenbr=N return a row?"""
        soup = self._post_search(
            session,
            caseyear=year,
            casenbr=f"{case_num:05d}",
        )
        if soup is None:
            return False
        text = soup.get_text(" ", strip=True)
        if "No matches were found" in text:
            return False
        return bool(soup.find("a", href=re.compile(r"casesearchresultx\.cfm\?")))

    def _lookup_case(
        self,
        session: requests.Session,
        year: int,
        case_num: int,
    ) -> _CaseDetail | None:
        """Full lookup: POST search → follow detail link → parse detail."""
        list_soup = self._post_search(
            session,
            caseyear=year,
            casenbr=f"{case_num:05d}",
        )
        if list_soup is None:
            return None
        text = list_soup.get_text(" ", strip=True)
        if "No matches were found" in text:
            self._sleep()
            return None

        detail_link = list_soup.find("a", href=re.compile(r"casesearchresultx\.cfm\?"))
        if detail_link is None:
            self._sleep()
            return None
        detail_url = f"{BASE_URL}/{detail_link['href']}"

        self._sleep()
        detail_soup = self._get(session, detail_url)
        if detail_soup is None:
            return None

        return self._parse_detail(detail_soup, detail_url, session)

    def _parse_detail(
        self,
        soup: BeautifulSoup,
        detail_url: str,
        session: requests.Session,
    ) -> _CaseDetail | None:
        """Extract the canonical fields from a case-detail page."""
        raw_text = soup.get_text("\n", strip=True)

        # Field label → value lookups. Each data row has two <td> cells;
        # we pair them up by scanning the outer table.
        field_map = self._extract_label_value_map(soup)

        case_number = self._clean(field_map.get("Case Number", ""))
        if not case_number:
            # Fallback: pull any 20xxESTnnn token from the raw text.
            m = CASE_NUMBER_RE.search(raw_text)
            case_number = m.group(0) if m else ""
        if not case_number:
            return None

        decedent_name = self._clean(field_map.get("Decedent's Name", ""))
        attorney = self._clean(field_map.get("Attorney", ""))

        # Case Type is "N \n  LABEL" — split into code + label.
        case_type_raw = field_map.get("Case Type", "")
        case_type_code, case_type_label = self._split_case_type(case_type_raw)

        # Case Status line: "OPEN  MM-DD-YYYY" — extract both halves.
        status_raw = field_map.get("Case Status", "")
        case_status, status_date = self._split_status(status_raw)

        # Fiduciary: first line = name, second line (after <br>) = address.
        fiduciary_name, fiduciary_address = self._split_fiduciary(
            field_map.get("Fiduciary", "")
        )

        # Filing date strategy:
        #   1. If status == OPEN, the status date IS the filing date.
        #   2. Otherwise, walk the docket page and take the earliest entry.
        filing_date = status_date if case_status.upper() == "OPEN" else None
        if filing_date is None:
            filing_date = self._earliest_docket_date(soup, session)
        # Final fallback — status date even if case is closed.
        if filing_date is None and status_date is not None:
            filing_date = status_date

        return _CaseDetail(
            case_number=case_number,
            filing_date=filing_date,
            decedent_name=decedent_name,
            fiduciary_name=fiduciary_name,
            fiduciary_address=fiduciary_address,
            case_type_code=case_type_code,
            case_type_label=case_type_label,
            case_status=case_status,
            attorney=attorney,
            detail_url=detail_url,
            raw_text=raw_text,
        )

    def _extract_label_value_map(self, soup: BeautifulSoup) -> dict[str, str]:
        """Pair up label <td>s with their sibling value <td>s.

        The detail page's data table has 2-column rows: label | value.
        We key on the label text (stripped of &nbsp;/colons).
        """
        result: dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 2:
                continue
            label = cells[0].get_text(" ", strip=True).strip(":").strip()
            value = cells[1].get_text("\n", strip=True)
            if label:
                result[label] = value
        return result

    def _earliest_docket_date(
        self,
        detail_soup: BeautifulSoup,
        session: requests.Session,
    ) -> date | None:
        """Pull the earliest MM-DD-YYYY date from the docket page if linked."""
        docket_link = detail_soup.find(
            "a", href=re.compile(r"CASESEARCH_DOCKETx\.cfm\?", re.IGNORECASE)
        )
        if not docket_link:
            return None
        docket_url = f"{BASE_URL}/{docket_link['href']}"
        self._sleep()
        docket_soup = self._get(session, docket_url)
        if docket_soup is None:
            return None
        dates: list[date] = []
        for m in DOCKET_DATE_RE.finditer(docket_soup.get_text(" ", strip=True)):
            try:
                dates.append(datetime.strptime(m.group(1), "%m-%d-%Y").date())
            except ValueError:
                pass
        return min(dates) if dates else None

    # ── Parsing helpers ────────────────────────────────────────────────
    @staticmethod
    def _clean(text: str) -> str:
        """Collapse whitespace and strip &nbsp; remnants."""
        if not text:
            return ""
        cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _split_case_type(self, raw: str) -> tuple[str, str]:
        """Case Type cell looks like: `1 \n  FULL ADMIN;PROBATE WILL`."""
        cleaned = self._clean(raw)
        if not cleaned:
            return "", ""
        # First whitespace-separated token is the numeric code.
        parts = cleaned.split(None, 1)
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    def _split_status(self, raw: str) -> tuple[str, date | None]:
        """Status cell: `OPEN  04-23-2026` → ("OPEN", date(2026,4,23))."""
        cleaned = self._clean(raw)
        if not cleaned:
            return "", None
        m = STATUS_DATE_RE.search(cleaned)
        if m:
            status = m.group(1).upper()
            try:
                d = datetime.strptime(m.group(2), "%m-%d-%Y").date()
                return status, d
            except ValueError:
                return status, None
        # Status without a date — return the leading word.
        return cleaned.split()[0].upper(), None

    def _split_fiduciary(self, raw: str) -> tuple[str, str]:
        """Fiduciary cell: `SMITH, ANNETTE \n 2309 WRENSIDE LANE, ...`."""
        if not raw:
            return "", ""
        # The <br> in the original HTML becomes a newline in get_text("\n").
        lines = [self._clean(ln) for ln in raw.splitlines() if self._clean(ln)]
        if not lines:
            return "", ""
        name = lines[0]
        address = " ".join(lines[1:]) if len(lines) > 1 else ""
        return name, address

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(self, detail: _CaseDetail) -> NoticeData:
        """Project a parsed _CaseDetail onto the pipeline's NoticeData shape."""
        # Normalize decedent name — portal uses "LAST, FIRST MIDDLE" mostly
        # but also some "FIRST LAST" forms (e.g., "DOROTHEA SMITH").
        decedent = detail.decedent_name
        # Fiduciary relationship guess — Montgomery doesn't label executor
        # vs administrator in the list view. Infer from case type label:
        #   "PROBATE WILL" → executor, "ADMINISTER" or default → administrator.
        label_upper = detail.case_type_label.upper()
        if "WILL" in label_upper:
            relationship = "executor"
        elif "ADMIN" in label_upper:
            relationship = "administrator"
        else:
            relationship = "personal_representative"

        # Parse fiduciary mailing address into street/city/state/zip when
        # the format matches "STREET, CITY, ST ZIP".
        f_street, f_city, f_state, f_zip = self._parse_address(detail.fiduciary_address)

        return NoticeData(
            date_added=detail.filing_date.isoformat() if detail.filing_date else "",
            county=self.county,
            state="OH",
            notice_type=self.notice_type,
            source_url=detail.detail_url,
            # Property address intentionally blank — see module docstring.
            address="",
            city="",
            zip="",
            # Decision-maker / fiduciary is our contact.
            owner_name=detail.fiduciary_name,
            decedent_name=decedent,
            decision_maker_name=detail.fiduciary_name,
            decision_maker_relationship=relationship,
            # Fiduciary mailing address populates the DM / owner mailing fields.
            owner_street=f_street,
            owner_city=f_city,
            owner_state=f_state,
            owner_zip=f_zip,
            decision_maker_street=f_street,
            decision_maker_city=f_city,
            decision_maker_state=f_state,
            decision_maker_zip=f_zip,
            decision_maker_source="court_record",
            decision_maker_status="unverified",
            raw_text=(
                f"Case Number: {detail.case_number}\n"
                f"Case Type: {detail.case_type_code} {detail.case_type_label}\n"
                f"Case Status: {detail.case_status}\n"
                f"Decedent: {detail.decedent_name}\n"
                f"Fiduciary: {detail.fiduciary_name}\n"
                f"Fiduciary Address: {detail.fiduciary_address}\n"
                f"Attorney: {detail.attorney}\n"
                f"Filing Date: {detail.filing_date.isoformat() if detail.filing_date else 'unknown'}\n"
            ),
        )

    @staticmethod
    def _parse_address(raw: str) -> tuple[str, str, str, str]:
        """Best-effort split of "STREET, CITY, STATE ZIP" into parts.

        Returns ("", "", "", "") if the format doesn't match.
        """
        if not raw:
            return "", "", "", ""
        # Collapse whitespace first.
        cleaned = re.sub(r"\s+", " ", raw).strip().rstrip(",")
        # Primary match: STREET_WITH_COMMAS_MAYBE , CITY , ST ZIP
        m = re.match(
            r"^(?P<street>.+?),\s*(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$",
            cleaned,
        )
        if m:
            return (
                m.group("street").strip(),
                m.group("city").strip(),
                m.group("state").strip(),
                m.group("zip").strip(),
            )
        # Fallback: unit/suite without comma before city — e.g.
        # "2324 MADISON ROAD, #1804 CINCINNATI, OH 45206" or
        # "130 W. SECOND ST, SUITE 1622 DAYTON, OH 45402".
        m = re.match(
            r"^(?P<street>.+?),\s*(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Z]{2})"
            r"\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$",
            # Insert a comma between the unit token (#NNN / SUITE NNN / APT NNN)
            # and the following city name so the primary shape matches.
            re.sub(
                r"(#\S+|\bSUITE\s+\S+|\bSTE\s+\S+|\bAPT\s+\S+|\bUNIT\s+\S+|\bFL\s+\S+)\s+([A-Z][A-Z .'-]+),\s*([A-Z]{2})\s+",
                r"\1, \2, \3 ",
                cleaned,
            ),
        )
        if m:
            return (
                m.group("street").strip(),
                m.group("city").strip(),
                m.group("state").strip(),
                m.group("zip").strip(),
            )
        # Final fallback: no ZIP present — capture just street + city + state.
        m = re.match(
            r"^(?P<street>.+?),\s*(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Z]{2})\s*$",
            cleaned,
        )
        if m:
            return (
                m.group("street").strip(),
                m.group("city").strip(),
                m.group("state").strip(),
                "",
            )
        return "", "", "", ""


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

    parser = argparse.ArgumentParser(description="Test Montgomery probate scraper")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_montgomery_probate.csv",
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
            f.write("")  # empty file, headers N/A
    print(f"Wrote {out_path}")
    sys.exit(0)
