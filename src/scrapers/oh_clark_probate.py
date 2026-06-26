"""Clark County, Ohio probate case scraper.

Pulls newly filed estate cases from the Clark County Probate Court's online
record search at https://probate.clarkcountyohio.gov/. The platform is
Henschen & Associates "CaseLook" (PHP); the same vendor runs the Miami and
Madison county probate portals, so most of this module will eventually be
hoisted into a shared `henschen_caselook_base.py` when Miami/Madison come
online. For now it's self-contained so the first county ships fast.

Portal characteristics (researched + probed against the live site 2026-06-03):
  * Tech stack: PHP, server-rendered HTML, jQuery on the client. Records
    rendered as div-based "<div class='record'>" rows (no <table>).
  * Disclaimer gate: GET ``/recordSearch.php?k=acceptAgreementsearchForm1203``
    sets the PHPSESSID + haTestCookie session cookies; the form page is
    then served at the same URL on subsequent GETs of the search-form
    variant (``k=searchForm1203``).
  * The search form regenerates a hidden ``k=`` token per page load —
    must be fetched fresh each scrape and submitted in the POST.
  * **Image CAPTCHA is required for every search submission.** The image
    is at ``/captcha/showCaptcha.php?m=image`` (JPEG, ~4KB). We solve via
    2Captcha (`CAPTCHA_API_KEY`, ~$0.002/solve, typical solve in 8-15s).
    Recon agent missed this — verified during inline probe.
  * **The form DOES expose a File Date filter** (month/day/year selectors)
    — recon agent missed this too. We use it directly. The form note
    "you can only search by one piece of information at a time" applies to
    name/case#/date — case-type checkboxes apply on top of any of those.
    Strategy: walk the lookback window day-by-day (7 CAPTCHAs/run for the
    default 7-day window). Trivial cost.
  * Case-type checkboxes: ``PC`` Civil, ``PE`` Estate, ``PG`` Guardianship,
    ``PR`` Marriage, ``PM`` Misc, ``PT`` Trusteeship. We send only ``PE``.
  * Agency ID for Clark is ``1203`` (hidden field ``searchAgency[]``).
  * Results: each match is a ``<div class="record">`` containing case
    number, decedent name, file date, case type, and links to the case
    detail + docket pages. Detail page URL embeds a session-bound 130+-char
    token in ``k=case1203<sessionToken>``.
  * Detail page exposes (this is the win — more data than Franklin or
    Montgomery probate):
      - Decedent: name, attorney, DOD ("Date Deceased"), aliases (DBA/AKA),
        last known address (street + city/state/zip)
      - Fiduciary: name, type (EXR/ADMR/etc.), date appointed, relationship,
        mailing address, **phone number**
      - Case timeline: filing date, will admitted date, deadlines

NoticeData contract per CLAUDE.md domain rules:
  * notice_type = "probate", county = "Clark", state = "OH"
  * owner_name = the fiduciary (EXR/ADMR — the PR / decision-maker we contact)
  * decedent_name = the deceased
  * decision_maker_name = same as owner_name for probate (the fiduciary IS
    the DM); decision_maker_relationship = the "Relationship" field
  * address/city/state/zip = decedent's last known address (Clark exposes
    this directly, unlike Franklin/Montgomery where the County Auditor
    lookup is needed downstream)
  * owner_street/city/state/zip = fiduciary mailing address (for direct-mail
    outreach — the WHOLE POINT of this scraper)
  * date_of_death = parsed from "Date Deceased" — saves an obituary lookup
  * primary_phone = fiduciary phone (Clark exposes this; bonus skip-trace data)
  * date_added = case filing date
  * raw_text = full structured blob of the detail page for downstream classification
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper, correct_state_against_zip

logger = logging.getLogger(__name__)

# Per-county Henschen wiring. When Miami/Madison come online, lift these
# four constants into a base class + subclass attribute.
COUNTY = "Clark"
AGENCY_ID = "1203"
BASE_URL = "https://probate.clarkcountyohio.gov"
DISCLAIMER_URL = f"{BASE_URL}/recordSearch.php?k=searchForm{AGENCY_ID}"
ACCEPT_URL = f"{BASE_URL}/recordSearch.php?k=acceptAgreementsearchForm{AGENCY_ID}"
SEARCH_POST_URL = f"{BASE_URL}/recordSearch.php"
CAPTCHA_IMG_URL = f"{BASE_URL}/captcha/showCaptcha.php?m=image"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Retry the whole search-with-CAPTCHA loop this many times per day before
# giving up on that day. CAPTCHA service occasionally returns wrong solves;
# 3 retries is plenty without ballooning cost.
MAX_CAPTCHA_RETRIES_PER_DAY = 3

# Defensive cap: we don't expect more than ~250 matches/day for Estate-only,
# but the form supports up to 250 per page. If a day has >250 we'd miss
# records — alarm the operator.
MAX_EXPECTED_PER_DAY = 250


@dataclass
class _Detail:
    """Fields parsed out of one Clark probate detail page."""
    case_number: str = ""
    filing_date: date | None = None
    decedent_name: str = ""
    decedent_dod: date | None = None
    decedent_aliases: str = ""
    decedent_street: str = ""
    decedent_city: str = ""
    decedent_state: str = "OH"
    decedent_zip: str = ""
    fiduciary_name: str = ""
    fiduciary_type: str = ""       # EXR, ADMR, etc.
    fiduciary_relationship: str = ""
    fiduciary_street: str = ""
    fiduciary_city: str = ""
    fiduciary_state: str = "OH"
    fiduciary_zip: str = ""
    fiduciary_phone: str = ""
    fiduciary_appointed: date | None = None
    attorney: str = ""
    detail_url: str = ""
    raw_text: str = ""


@dataclass
class _ListRow:
    """One row parsed from the search results list page."""
    case_number: str
    filing_date: date | None
    decedent_name: str
    detail_url: str


class Scraper(NoticeScraper):
    """Clark County Probate — Henschen CaseLook scraper."""

    county = COUNTY
    notice_type = "probate"
    source_name = "Clark County Probate Court"
    source_url = BASE_URL
    # CAPTCHA_API_KEY is what we actually need; the credential gate uses it.
    requires_account = True

    def required_credentials(self) -> list[str]:
        return ["CAPTCHA_API_KEY"]

    # ── Public entrypoint ─────────────────────────────────────────────
    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        if since_date is None:
            since_date = date.today() - timedelta(days=7)
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Sync core ─────────────────────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        if not config.CAPTCHA_API_KEY:
            logger.warning(
                "CAPTCHA_API_KEY not set — Clark probate scrape skipped. "
                "Add CAPTCHA_API_KEY=... to .env (2Captcha key)."
            )
            return []

        # Late import: twocaptcha is an optional dep at module-load time.
        try:
            from twocaptcha import TwoCaptcha
        except ImportError as e:
            logger.error("twocaptcha-python not installed: %s", e)
            return []

        solver = TwoCaptcha(config.CAPTCHA_API_KEY)
        logger.info(
            "Clark probate scrape: filings %s through %s",
            since_date.isoformat(), date.today().isoformat(),
        )

        session = self._build_session()

        # Walk each day in the window. One search submission per day,
        # one CAPTCHA solve per day. Day-loop is cheaper than ENV-style
        # case-number probing for low-volume counties (Clark is ~3 estate
        # filings/day average).
        all_rows: list[_ListRow] = []
        day = since_date
        while day <= date.today():
            try:
                rows = self._search_one_day(session, day, solver)
                all_rows.extend(rows)
                logger.info("  %s: %d estate filing(s)", day.isoformat(), len(rows))
            except Exception as e:
                logger.warning("Day %s failed (%s) — skipping", day, e)
            day += timedelta(days=1)

        # Drop duplicates by case_number (a case shouldn't appear twice
        # across days, but defensive).
        seen: set[str] = set()
        unique_rows: list[_ListRow] = []
        for r in all_rows:
            if r.case_number in seen:
                continue
            seen.add(r.case_number)
            unique_rows.append(r)

        logger.info("Clark probate: %d unique case(s) — fetching details", len(unique_rows))

        records: list[NoticeData] = []
        for row in unique_rows:
            try:
                detail = self._fetch_detail(session, row)
            except Exception as e:
                logger.debug("Detail fetch failed for %s: %s", row.case_number, e)
                continue
            if detail is None:
                continue
            records.append(self._to_notice_data(detail))
            logger.info(
                "  %s  %s  decedent=%s  fiduciary=%s",
                (detail.filing_date or row.filing_date).isoformat() if (detail.filing_date or row.filing_date) else "?",
                detail.case_number or row.case_number,
                detail.decedent_name or "(none)",
                detail.fiduciary_name or "(none yet)",
            )

        records.sort(key=lambda r: (r.date_added, r.case_number))
        logger.info("Clark probate scrape done — %d records", len(records))
        return records

    # ── Session bootstrap ─────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Disclaimer gate — required before any search will accept the cookies.
        try:
            s.get(DISCLAIMER_URL, timeout=30)
            self._sleep()
            s.get(ACCEPT_URL, timeout=30)
        except requests.RequestException as e:
            logger.warning("Session warmup failed (continuing): %s", e)
        return s

    def _sleep(self) -> None:
        """Polite delay between HTTP calls."""
        time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

    # ── Per-day search ────────────────────────────────────────────────
    def _search_one_day(
        self,
        session: requests.Session,
        day: date,
        solver,
    ) -> list[_ListRow]:
        """Submit one date-bounded search; returns parsed list rows.

        Retries the whole flow up to MAX_CAPTCHA_RETRIES_PER_DAY if 2Captcha
        gives us a wrong solve (the form will land back on itself).
        """
        last_error: str = ""
        for attempt in range(MAX_CAPTCHA_RETRIES_PER_DAY):
            # 1. Re-fetch form to get a fresh k= token (the previous one
            #    expires after one submission).
            form_resp = session.get(ACCEPT_URL, timeout=30)
            if form_resp.status_code != 200:
                last_error = f"form GET → {form_resp.status_code}"
                self._sleep()
                continue
            soup = BeautifulSoup(form_resp.text, "html.parser")
            k_input = soup.find("input", {"name": "k"})
            if not k_input:
                last_error = "no k= token in form"
                self._sleep()
                continue
            k_token = k_input.get("value", "")

            # 2. Fetch + solve CAPTCHA.
            self._sleep()
            img_resp = session.get(CAPTCHA_IMG_URL, timeout=30)
            if img_resp.status_code != 200 or not img_resp.content:
                last_error = "captcha image fetch failed"
                continue
            captcha_code = self._solve_captcha(solver, img_resp.content)
            if not captcha_code:
                last_error = "captcha solve returned empty"
                continue

            # 3. POST the search.
            data = [
                ("searchName", ""),
                ("searchCase", ""),
                ("searchFMonth", str(day.month)),
                ("searchFDay", str(day.day)),
                ("searchFYear", str(day.year)),
                ("searchAgency[]", AGENCY_ID),
                ("searchCaseType[]", "PE"),  # Estate only
                ("searchBlock", "250"),       # Max page size
                ("captchaResponse", captcha_code),
                ("searchType", "mainSearch"),
                ("k", k_token),
            ]
            self._sleep()
            resp = session.post(
                SEARCH_POST_URL,
                data=data,
                timeout=45,
                headers={"Referer": ACCEPT_URL},
            )
            if resp.status_code != 200:
                last_error = f"search POST → {resp.status_code}"
                continue
            text_lower = resp.text.lower()
            if "incorrect captcha" in text_lower or "invalid captcha" in text_lower:
                logger.debug("Clark %s: CAPTCHA rejected (attempt %d) — retrying", day, attempt + 1)
                last_error = "captcha rejected"
                continue
            # Parse — even if 0 results, the page renders cleanly with
            # "0 matches were found".
            rows = self._parse_list(resp.text, day)
            if len(rows) >= MAX_EXPECTED_PER_DAY:
                logger.warning(
                    "Clark %s: hit MAX_EXPECTED_PER_DAY (%d) — pagination "
                    "may be needed (this should be rare for Estate-type-only)",
                    day, MAX_EXPECTED_PER_DAY,
                )
            return rows

        logger.warning(
            "Clark %s: all %d search attempts failed (%s)",
            day, MAX_CAPTCHA_RETRIES_PER_DAY, last_error,
        )
        return []

    def _solve_captcha(self, solver, image_bytes: bytes) -> str:
        """Submit CAPTCHA image to 2Captcha; return text or empty on failure."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            tmp.write(image_bytes)
            tmp.flush()
            try:
                result = solver.normal(tmp.name)
                return (result or {}).get("code", "").strip()
            except Exception as e:
                logger.debug("2Captcha solve raised: %s", e)
                return ""

    # ── List parsing ──────────────────────────────────────────────────
    _CASE_NUM_RE = re.compile(r"[A-Z]?\d{5,}")

    def _parse_list(self, html: str, day_searched: date) -> list[_ListRow]:
        """Parse all <div class='record'> blocks from a search-results page."""
        soup = BeautifulSoup(html, "html.parser")
        records = soup.find_all("div", class_="record")
        out: list[_ListRow] = []
        for rec in records:
            case_num = ""
            cn_span = rec.find("span", class_="fullCaseNumber")
            if cn_span:
                case_num = cn_span.get_text(" ", strip=True)
            decedent = ""
            name_span = rec.find("span", class_="concerningName")
            if name_span:
                decedent = name_span.get_text(" ", strip=True)
            # File date is in its own div
            filing_date: date | None = None
            fd_div = rec.find("div", class_="fileDate")
            if fd_div:
                # Skip the <label> "Filed:" portion; everything after is the date.
                m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", fd_div.get_text(" ", strip=True))
                if m:
                    try:
                        filing_date = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                    except ValueError:
                        pass
            # Detail link
            detail_a = rec.find("a", class_="caseLink")
            detail_url = BASE_URL + detail_a.get("href", "") if detail_a else ""
            if not case_num or not detail_url:
                continue
            out.append(_ListRow(
                case_number=case_num,
                filing_date=filing_date or day_searched,
                decedent_name=decedent,
                detail_url=detail_url,
            ))
        return out

    # ── Detail parsing ────────────────────────────────────────────────
    def _fetch_detail(self, session: requests.Session, row: _ListRow) -> _Detail | None:
        self._sleep()
        resp = session.get(row.detail_url, timeout=30)
        if resp.status_code != 200:
            return None
        return self._parse_detail(resp.text, row)

    def _parse_detail(self, html: str, row: _ListRow) -> _Detail | None:
        """Extract structured fields from a Clark detail page.

        Layout is a dictionary of <label>:<value> rows grouped under section
        headers ("Decedent", "Fiduciary(s)", "Case Information"). We work
        from a single linearized text representation to keep parsing simple.
        """
        soup = BeautifulSoup(html, "html.parser")
        main = soup.find("main") or soup.find("div", id="mainContainer")
        if not main:
            return None
        lines = [ln.strip() for ln in main.get_text("\n", strip=True).splitlines() if ln.strip()]
        text = "\n".join(lines)
        full_text = main.get_text("\n", strip=True)

        d = _Detail(detail_url=row.detail_url, raw_text=full_text)

        # Sectioned label scan — we walk the line list and remember which
        # section header we last saw (Decedent vs Fiduciary). When we hit a
        # label line ("Decedent:", "Address:", etc.) the NEXT line is the
        # value. This survives the layout quirk where labels and values
        # render on separate lines.
        SECTION_HEADERS = {"Decedent", "Fiduciary(s)", "Case Information",
                            "Next/Most Recent Hearing", "Case Time-line",
                            "Reporting Time-line"}
        current_section = ""
        # Track which fiduciary number we're on (Fiduciary 1, 2, ...).
        # We only want #1.
        in_fiduciary_one = False
        fiduciary_count = 0
        i = 0
        while i < len(lines):
            line = lines[i]
            if line in SECTION_HEADERS:
                current_section = line
                if line == "Fiduciary(s)":
                    in_fiduciary_one = False
                i += 1
                continue
            # Fiduciary count switch (Fiduciary 1:, Fiduciary 2:, ...)
            m_fid = re.match(r"^Fiduciary\s+(\d+):$", line)
            if m_fid:
                fiduciary_count = int(m_fid.group(1))
                in_fiduciary_one = (fiduciary_count == 1)
                # The next line is the fiduciary's name
                if in_fiduciary_one and i + 1 < len(lines):
                    d.fiduciary_name = lines[i + 1]
                    i += 2
                    continue
                i += 1
                continue
            # Generic label: line ends with ':', next line is value
            if line.endswith(":"):
                label = line[:-1].strip()
                value = lines[i + 1] if i + 1 < len(lines) else ""
                # Only consume value if it isn't itself another label/section.
                if value.endswith(":") or value in SECTION_HEADERS:
                    i += 1
                    continue
                self._assign_field(d, current_section, in_fiduciary_one, label, value)
                i += 2
                continue
            # Case Information header line ("Case Information: 20260305")
            m_ci = re.match(r"^Case Information:\s+(\S+)\s*$", line)
            if m_ci:
                d.case_number = m_ci.group(1)
            i += 1

        # Back-fill from the list row if detail didn't expose the case#.
        if not d.case_number:
            d.case_number = row.case_number
        if not d.filing_date:
            d.filing_date = row.filing_date
        if not d.decedent_name:
            d.decedent_name = row.decedent_name

        return d

    def _assign_field(
        self,
        d: _Detail,
        section: str,
        in_fid1: bool,
        label: str,
        value: str,
    ) -> None:
        """Map (section, label) → _Detail field."""
        v = value.strip()
        if section == "Decedent":
            if label == "Decedent":
                d.decedent_name = v
            elif label == "Attorney":
                d.attorney = v
            elif label == "Date Deceased":
                d.decedent_dod = _parse_us_date(v)
            elif label == "D.B.A/A.K.A":
                d.decedent_aliases = v
            elif label == "Address":
                d.decedent_street = v
            elif label == "City/State/ZIP":
                d.decedent_city, d.decedent_state, d.decedent_zip = _split_csz(v)
        elif section == "Fiduciary(s)" and in_fid1:
            if label == "Fiduciary Type":
                d.fiduciary_type = v
            elif label == "Date Appointed":
                d.fiduciary_appointed = _parse_us_date(v)
            elif label == "Relationship":
                d.fiduciary_relationship = v
            elif label == "Address":
                d.fiduciary_street = v
            elif label == "City/State/ZIP":
                d.fiduciary_city, d.fiduciary_state, d.fiduciary_zip = _split_csz(v)
            elif label == "Phone Number":
                d.fiduciary_phone = _normalize_phone(v)
        elif section == "Case Information":
            if label == "File Date":
                fd = _parse_us_date(v)
                if fd:
                    d.filing_date = fd

    # ── NoticeData ────────────────────────────────────────────────────
    def _to_notice_data(self, d: _Detail) -> NoticeData:
        raw_lines = [
            f"Case Number: {d.case_number}",
            f"Filing Date: {d.filing_date.isoformat() if d.filing_date else ''}",
            f"Decedent: {d.decedent_name}",
            f"DOD: {d.decedent_dod.isoformat() if d.decedent_dod else ''}",
            f"Aliases: {d.decedent_aliases}",
            f"Decedent Address: {d.decedent_street}, {d.decedent_city}, {d.decedent_state} {d.decedent_zip}",
            f"Attorney: {d.attorney}",
            f"Fiduciary: {d.fiduciary_name}",
            f"Fiduciary Type: {d.fiduciary_type}",
            f"Fiduciary Relationship: {d.fiduciary_relationship}",
            f"Fiduciary Address: {d.fiduciary_street}, {d.fiduciary_city}, {d.fiduciary_state} {d.fiduciary_zip}",
            f"Fiduciary Phone: {d.fiduciary_phone}",
        ]

        # Clark exposes the decedent's last-known address directly. For
        # estate cases that address is usually the decedent's home — the
        # property we'd want to mail. For fiduciary mailing (the direct-
        # mail target), use the fiduciary block.
        return NoticeData(
            date_added=d.filing_date.isoformat() if d.filing_date else "",
            address=d.decedent_street,
            city=d.decedent_city,
            state=d.decedent_state or "OH",
            zip=d.decedent_zip,
            owner_name=d.fiduciary_name,      # the fiduciary IS the DM
            notice_type=self.notice_type,
            county=self.county,
            source_url=d.detail_url,
            case_number=d.case_number,
            decedent_name=d.decedent_name,
            owner_street=d.fiduciary_street,
            owner_city=d.fiduciary_city,
            owner_state=d.fiduciary_state or "OH",
            owner_zip=d.fiduciary_zip,
            date_of_death=d.decedent_dod.isoformat() if d.decedent_dod else "",
            decision_maker_name=d.fiduciary_name,
            decision_maker_relationship=d.fiduciary_relationship,
            decision_maker_street=d.fiduciary_street,
            decision_maker_city=d.fiduciary_city,
            decision_maker_state=d.fiduciary_state or "OH",
            decision_maker_zip=d.fiduciary_zip,
            primary_phone=d.fiduciary_phone,
            owner_deceased="",  # the OWNER (fiduciary) is alive; decedent is dead by definition
            raw_text="\n".join(raw_lines),
        )


# ── Pure-function helpers (testable in isolation) ─────────────────────
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_CSZ_RE = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$")
_PHONE_RE = re.compile(r"(\d{3})\D+(\d{3})\D+(\d{4})")


def _parse_us_date(s: str) -> date | None:
    """Parse M/D/YYYY or MM/DD/YYYY → date."""
    s = (s or "").strip()
    m = _US_DATE_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _split_csz(s: str) -> tuple[str, str, str]:
    """Split 'City, ST 12345' or 'City, ST 12345-6789' → (city, state, zip5).

    If the typed state disagrees with the ZIP prefix (clerk typo at the source),
    the ZIP wins — see `correct_state_against_zip`.
    """
    s = (s or "").strip()
    m = _CSZ_RE.match(s)
    if not m:
        return s, "", ""
    city = m.group(1).strip().title()
    state = m.group(2).strip().upper()
    zip_code = m.group(3)
    return city, correct_state_against_zip(state, zip_code), zip_code


def _normalize_phone(s: str) -> str:
    """'(937) 267-6759' → '9372676759'."""
    m = _PHONE_RE.search(s or "")
    if not m:
        return ""
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"


# ── Standalone test harness ───────────────────────────────────────────
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
    parser = argparse.ArgumentParser(description="Test Clark probate scraper")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    parser.add_argument(
        "--output", type=str,
        default="output/test_oh_clark_probate.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))
    print(f"Scraped {len(records)} Clark probate records since {since}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        if records:
            writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        else:
            f.write("")
    print(f"Wrote {out}")
    sys.exit(0)
