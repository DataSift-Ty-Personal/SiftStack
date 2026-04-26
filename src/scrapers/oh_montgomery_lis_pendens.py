"""Montgomery County, Ohio Lis Pendens (foreclosure case filing) scraper.

Pulls newly filed Lis Pendens cases from the Montgomery County Clerk of Courts
"PRO" public records portal (pro.mcohio.org). Lis Pendens (case type code
``LP``) is a notice of pending lawsuit affecting real property — in Montgomery
County these are filed in the Court of Common Pleas General Division and
typically precede the sheriff sale by 4-12 weeks. That makes them the
earliest possible signal in the foreclosure lifecycle, well ahead of the
sheriff-sale list scraped by ``oh_montgomery_foreclosure``.

Portal characteristics (researched against the live site, build 2026-04):
  * Tech stack: ASP.NET (*.aspx), JSON/HTML AJAX backend exposed under
    ``Helpers/`` paths. The user-facing page is a single SPA at ``/`` that
    fetches its search form via ``Helpers/buildSearchCriteria.aspx`` and
    posts to ``Helpers/generalSearchResults.aspx``.
  * Bootstrap flow:
        GET  Helpers/initializeSession.aspx
        POST Helpers/acceptDisclaimer.aspx   (disclaimer=true)
        POST Helpers/authenticateSearch.aspx (returns "True" / "False")
        POST Helpers/generalSearchResults.aspx (the actual search)
  * **reCAPTCHA v3 is enforced for guest sessions.** The site uses
    ``api.js?render={SITEKEY}`` to mint a token client-side, posts it to
    ``Helpers/generalSearchResults.aspx`` as ``captchaToken``, and the
    server validates the token via the standard Google ``siteverify`` API.
    Empty / placeholder tokens cause an HTTP 500 (the server-side
    siteverify call throws on rejection, propagated as a Runtime Error).
    There is no public bypass — this scraper requires a 2Captcha API key
    (env: ``CAPTCHA_API_KEY``) to mint a real token per search request.
  * Form fields (from buildSearchCriteria.aspx):
        case_number, last_name, first_name, company_name, ticket_number,
        gen_case_type, gen_action_type, begin_date (YYYY-MM-DD),
        end_date (YYYY-MM-DD), searchType=general, captchaToken
  * Case Type ``LP`` = LIS PENDENS (the value we filter on). Other relevant
    codes for foreclosure work: ``CV`` (CIVIL — broader), action type
    ``MF`` (MORTGAGE FORECLOSURE — used in CV cases).
  * Date filter (begin_date / end_date) operates on case-filing date.
    Server caps results at 1,000 rows per search.
  * Per-case detail is fetched via ``Helpers/caseInformation.aspx`` with
    ``case_id`` + ``screen=docket`` (or ``cpcgd``). The caption / parties
    block on the case-info page exposes property address (in the case
    caption for foreclosures) and the defendant (homeowner = our target).

NoticeData contract per CLAUDE.md domain rules:
  * ``notice_type = "lis_pendens"`` (NEW notice type for this build —
    Franklin County's LP scraper agent owns the matching ``datasift_formatter``
    edit, so this scraper just emits the type and lets the formatter pick it up).
  * ``date_added`` = case filing date (the actual lis pendens date).
  * ``owner_name`` = first defendant (the homeowner being foreclosed on).
    ``, et al.`` suffix stripped.
  * ``address`` / ``city`` / ``zip`` = property address from the case
    caption when the clerk exposes it; left blank otherwise (downstream
    enrichment can fill via parcel/owner lookup).
  * ``auction_date`` intentionally empty — sheriff sale isn't scheduled
    yet at lis-pendens stage.
  * ``raw_text`` bundles the case row + detail blob for downstream
    classification.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://pro.mcohio.org"
LANDING_URL = f"{BASE_URL}/"
INITIALIZE_URL = f"{BASE_URL}/Helpers/initializeSession.aspx"
DISCLAIMER_URL = f"{BASE_URL}/Helpers/acceptDisclaimer.aspx"
AUTH_SEARCH_URL = f"{BASE_URL}/Helpers/authenticateSearch.aspx"
SEARCH_URL = f"{BASE_URL}/Helpers/generalSearchResults.aspx"
CASE_INFO_URL = f"{BASE_URL}/Helpers/caseInformation.aspx"

# Hardcoded by the site in /Scripts/site.js — same key the JS uses to mint
# tokens against ``grecaptcha.execute(SITEKEY, {action: 'genSearch'})``.
RECAPTCHA_SITEKEY = "6LcIVYQcAAAAAB3UDYAT2rh-EelDlT7i48-tTvhv"
RECAPTCHA_ACTION = "genSearch"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Default look-back when no explicit since_date is supplied — matches the
# rest of the OH scrapers.
DEFAULT_LOOKBACK_DAYS = 14

# Case Type code for Lis Pendens cases on the PRO portal.
CASE_TYPE_LP = "LP"

# Regex helpers for case detail parsing.
CASE_NUMBER_RE = re.compile(r"\b(20\d{2})\s*(LP|CV)\s*(\d{3,7})\b", re.IGNORECASE)
DATE_MMDDYYYY_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Property address line — defensive: case captions sometimes embed
# "Property: 123 Main St, Dayton, OH 45402" or just "123 Main St ..."
ADDRESS_LINE_RE = re.compile(
    r"(?P<street>\d{1,6}\s+[A-Za-z0-9.\s\-']+?)[,]\s*"
    r"(?P<city>[A-Za-z .'\-]+?)[,]\s*OH(?:IO)?\s*(?P<zip>\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)


@dataclass
class _CaseRow:
    """One row from the search results listing."""
    case_id: str               # opaque id used to fetch case detail
    case_number: str           # e.g. "2026 LP 00123"
    case_type: str             # "LP", "CV", etc.
    caption: str               # plaintiff vs defendant title
    file_date: date | None
    status: str                # case status if shown ("OPEN", etc.)


@dataclass
class _CaseDetail:
    """Parsed case-information detail page."""
    case_id: str
    case_number: str
    file_date: date | None
    plaintiff: str
    defendant: str
    address_raw: str
    street: str
    city: str
    zip_code: str
    detail_url: str
    raw_text: str


class Scraper(NoticeScraper):
    """Montgomery County Common Pleas — Lis Pendens (foreclosure case filings)."""

    county = "Montgomery"
    notice_type = "lis_pendens"
    source_name = "Montgomery County Common Pleas — Foreclosure Cases"
    source_url = LANDING_URL
    requires_account = True   # needs CAPTCHA_API_KEY for reCAPTCHA v3 solving

    def required_credentials(self) -> list[str]:
        return ["CAPTCHA_API_KEY"]

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull lis pendens cases filed on/after ``since_date`` (default 14 days)."""
        if since_date is None:
            since_date = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        # The PRO date filter uses both ends; bound at today so we don't
        # accidentally pull future-dated weirdness.
        until_date = max(since_date, date.today())
        return await asyncio.to_thread(self._scrape_sync, since_date, until_date)

    # ── Private sync implementation ────────────────────────────────────
    def _scrape_sync(self, since_date: date, until_date: date) -> list[NoticeData]:
        if not config.CAPTCHA_API_KEY:
            logger.warning(
                "CAPTCHA_API_KEY not set — Montgomery Lis Pendens scrape "
                "skipped. Sign up at https://2captcha.com and add "
                "CAPTCHA_API_KEY=... to .env to enable."
            )
            return []

        session = self._build_session()
        logger.info(
            "Montgomery lis pendens scrape: file dates %s → %s",
            since_date.isoformat(), until_date.isoformat(),
        )

        if not self._bootstrap(session):
            logger.warning("Could not bootstrap PRO session — returning no records.")
            return []

        # Mint a captcha token for the search request.
        token = self._solve_captcha()
        if not token:
            logger.warning("CAPTCHA solve failed — returning no records.")
            return []

        rows = self._search(session, since_date, until_date, token)
        logger.info("Search returned %d case row(s)", len(rows))

        records: list[NoticeData] = []
        for row in rows:
            if row.file_date is None:
                logger.debug("Row %s: no file date — skipping", row.case_number)
                continue
            if row.file_date < since_date or row.file_date > until_date:
                # Defensive: server-side date filter should prevent this.
                continue

            detail = self._fetch_case_detail(session, row)
            record = self._to_notice_data(row, detail)
            records.append(record)
            logger.info(
                "  %s  %s  defendant=%s  addr=%s",
                row.file_date.isoformat(),
                row.case_number,
                (detail.defendant if detail else "")[:40] or "(blank)",
                (detail.address_raw if detail else "")[:60] or "(none)",
            )

        # Oldest-first for downstream consistency with other OH scrapers.
        records.sort(key=lambda r: (r.date_added, r.source_url))
        logger.info("Montgomery lis pendens scrape done — %d records", len(records))
        return records

    # ── HTTP / session helpers ─────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": LANDING_URL,
        })
        return s

    def _sleep(self) -> None:
        """Rate-limit: 2-3s jittered delay."""
        time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

    def _bootstrap(self, session: requests.Session) -> bool:
        """Run the GET-init → accept-disclaimer dance the SPA does on load.

        Returns True if the disclaimer was accepted; False on a hard
        network failure. We don't fail on individual non-200s — the SPA
        treats them as warnings and so do we.
        """
        try:
            session.get(LANDING_URL, timeout=20)
        except requests.RequestException as e:
            logger.debug("Landing GET failed (non-fatal): %s", e)

        # initializeSession.aspx is GET in the JS — must match.
        try:
            session.get(INITIALIZE_URL, timeout=20)
        except requests.RequestException as e:
            logger.debug("initializeSession failed (non-fatal): %s", e)

        try:
            session.post(
                DISCLAIMER_URL,
                data={"disclaimer": "true"},
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=20,
            )
        except requests.RequestException as e:
            logger.warning("acceptDisclaimer failed: %s", e)
            return False

        return True

    def _solve_captcha(self) -> str | None:
        """Mint a reCAPTCHA v3 token via 2Captcha.

        Uses ``twocaptcha-python`` (already in requirements.txt) to solve
        the v3 challenge against the same sitekey + action the SPA uses.
        Returns the resulting token, or None on failure.
        """
        try:
            from twocaptcha import TwoCaptcha
        except ImportError as e:
            logger.error("twocaptcha-python not installed: %s", e)
            return None

        solver = TwoCaptcha(config.CAPTCHA_API_KEY)
        for attempt in range(config.MAX_RETRIES):
            try:
                logger.debug("Requesting reCAPTCHA v3 token (attempt %d)", attempt + 1)
                result: dict[str, Any] = solver.recaptcha(
                    sitekey=RECAPTCHA_SITEKEY,
                    url=LANDING_URL,
                    version="v3",
                    action=RECAPTCHA_ACTION,
                    score=0.3,   # PRO's threshold is permissive
                )
                token = result.get("code", "") if isinstance(result, dict) else ""
                if token:
                    logger.debug("Got captcha token (%d chars)", len(token))
                    return token
            except Exception as e:
                logger.warning("CAPTCHA solve attempt %d failed: %s", attempt + 1, e)
                self._sleep()
        return None

    def _search(
        self,
        session: requests.Session,
        since_date: date,
        until_date: date,
        token: str,
    ) -> list[_CaseRow]:
        """POST the LP-typed search and parse out the result rows."""
        data = {
            "case_number": "",
            "last_name": "",
            "first_name": "",
            "company_name": "",
            "ticket_number": "",
            "gen_case_type": CASE_TYPE_LP,
            "gen_action_type": "",
            "begin_date": since_date.isoformat(),
            "end_date": until_date.isoformat(),
            "searchType": "general",
            "captchaToken": token,
        }
        for attempt in range(config.MAX_RETRIES):
            try:
                self._sleep()
                resp = session.post(
                    SEARCH_URL,
                    data=data,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    return self._parse_results(resp.text)
                logger.debug(
                    "Search POST → %d (attempt %d)", resp.status_code, attempt + 1,
                )
            except requests.RequestException as e:
                logger.debug("Search POST raised %s (attempt %d)", e, attempt + 1)
        return []

    # ── Result-list parsing ────────────────────────────────────────────
    def _parse_results(self, html: str) -> list[_CaseRow]:
        """Extract case rows from the search results HTML.

        The PRO results panel is a Bootstrap table with one ``<tr>`` per
        case. Each row carries an ``onclick`` handler like
        ``openTab('caseInfo','case_id=XYZ&screen=docket', 1, 'CASE-NUM')``
        that we mine for the case_id token. Visible columns: Case Number,
        Case Type, Caption, File Date, Status (column ordering varies
        between deployments — we key on labeled headers when present and
        fall back to position).
        """
        if not html or "<table" not in html.lower():
            return []
        soup = BeautifulSoup(html, "html.parser")

        # Find the most plausible results table — the one with the most
        # ``onclick``-bearing rows.
        best_table: Tag | None = None
        best_rowcount = 0
        for tbl in soup.find_all("table"):
            rowcount = sum(
                1 for tr in tbl.find_all("tr")
                if tr.get("onclick") or tr.find(attrs={"onclick": True})
            )
            if rowcount > best_rowcount:
                best_rowcount = rowcount
                best_table = tbl
        if best_table is None:
            return []

        # Header-driven column resolution. Falls back to fixed positions.
        header_cells = best_table.find("tr")
        col_index: dict[str, int] = {}
        if header_cells:
            for i, th in enumerate(header_cells.find_all(["th", "td"])):
                label = th.get_text(" ", strip=True).lower()
                if "case" in label and "number" in label:
                    col_index["case_number"] = i
                elif "case" in label and "type" in label:
                    col_index["case_type"] = i
                elif "caption" in label or "title" in label:
                    col_index["caption"] = i
                elif "file" in label and "date" in label:
                    col_index["file_date"] = i
                elif "status" in label:
                    col_index["status"] = i
        # Fixed-position fallback (PRO's typical layout).
        col_index.setdefault("case_number", 0)
        col_index.setdefault("case_type", 1)
        col_index.setdefault("caption", 2)
        col_index.setdefault("file_date", 3)
        col_index.setdefault("status", 4)

        rows: list[_CaseRow] = []
        for tr in best_table.find_all("tr"):
            onclick = tr.get("onclick") or ""
            if not onclick:
                # Some deployments wrap the click on a child td.
                child = tr.find(attrs={"onclick": True})
                onclick = child.get("onclick") if child else ""
            if not onclick or "case_id=" not in onclick:
                continue

            case_id = self._extract_case_id(onclick)
            if not case_id:
                continue

            cells = tr.find_all("td", recursive=False)
            if not cells:
                continue

            def _cell(name: str) -> str:
                idx = col_index.get(name, -1)
                if 0 <= idx < len(cells):
                    return cells[idx].get_text(" ", strip=True)
                return ""

            case_number = _cell("case_number")
            case_type = _cell("case_type")
            caption = _cell("caption")
            file_date = self._parse_loose_date(_cell("file_date"))
            status = _cell("status")

            rows.append(_CaseRow(
                case_id=case_id,
                case_number=case_number,
                case_type=case_type,
                caption=caption,
                file_date=file_date,
                status=status,
            ))
        return rows

    @staticmethod
    def _extract_case_id(onclick: str) -> str:
        """Pull the case_id token out of an openTab(...) onclick string."""
        # The serialized form looks like: 'case_id=2026LP00123&screen=docket'
        m = re.search(r"case_id=([^&'\"\s]+)", onclick)
        return m.group(1) if m else ""

    @staticmethod
    def _parse_loose_date(s: str) -> date | None:
        """Parse MM/DD/YYYY or YYYY-MM-DD; return None on miss."""
        if not s:
            return None
        m = DATE_MMDDYYYY_RE.search(s)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        m = DATE_ISO_RE.search(s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    # ── Case-detail parsing ────────────────────────────────────────────
    def _fetch_case_detail(
        self,
        session: requests.Session,
        row: _CaseRow,
    ) -> _CaseDetail | None:
        """Hit caseInformation.aspx and parse out parties + property address."""
        data = {
            "case_id": row.case_id,
            "screen": "docket",
        }
        for attempt in range(config.MAX_RETRIES):
            try:
                self._sleep()
                resp = session.post(
                    CASE_INFO_URL,
                    data=data,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=45,
                )
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    return self._parse_detail(resp.text, row)
            except requests.RequestException as e:
                logger.debug(
                    "caseInformation %s raised %s (attempt %d)",
                    row.case_id, e, attempt + 1,
                )
        return None

    def _parse_detail(self, html: str, row: _CaseRow) -> _CaseDetail | None:
        """Best-effort parse of the case-information panel HTML."""
        soup = BeautifulSoup(html, "html.parser")
        raw_text = soup.get_text("\n", strip=True)

        # Plaintiff / Defendant: the parties section is typically a small
        # table or definition list with labels. Walk all label/value
        # pairs and pluck what we recognize.
        labels = self._extract_labelled_pairs(soup)

        plaintiff = labels.get("plaintiff") or labels.get("plaintiff(s)") or ""
        defendant = labels.get("defendant") or labels.get("defendant(s)") or ""
        # Some deployments label it "Party 1" / "Party 2".
        if not defendant:
            defendant = labels.get("party 2") or labels.get("respondent") or ""

        # Property address: try a labeled "Property Address" field first,
        # then scan the caption / raw text for a "STREET, CITY, OH ZIP" line.
        address_raw = (
            labels.get("property address")
            or labels.get("property")
            or labels.get("address")
            or ""
        )
        street, city, zip_code = "", "", ""
        if address_raw:
            street, city, zip_code = self._split_address(address_raw)
        if not street:
            # Scan caption + raw text for an OH address.
            for haystack in (row.caption or "", raw_text):
                m = ADDRESS_LINE_RE.search(haystack)
                if m:
                    address_raw = m.group(0)
                    street = m.group("street").strip()
                    city = m.group("city").strip()
                    zip_code = (m.group("zip") or "").strip()
                    break

        # File date: prefer the row's parsed file_date; fall back to the
        # first MM-DD-YYYY in the docket text.
        file_date = row.file_date
        if file_date is None:
            m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", raw_text)
            if m:
                try:
                    file_date = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                except ValueError:
                    pass

        # Detail URL: opaque case_id-keyed deep link.
        detail_url = (
            f"{LANDING_URL}?case_id={row.case_id}"
            f"&case_number={row.case_number.replace(' ', '+')}"
        )

        return _CaseDetail(
            case_id=row.case_id,
            case_number=row.case_number,
            file_date=file_date,
            plaintiff=self._clean(plaintiff),
            defendant=self._clean(defendant),
            address_raw=self._clean(address_raw),
            street=street,
            city=city,
            zip_code=zip_code,
            detail_url=detail_url,
            raw_text=raw_text,
        )

    def _extract_labelled_pairs(self, soup: BeautifulSoup) -> dict[str, str]:
        """Walk 2-cell <tr>s and label/value pairs — returns lowercase keys."""
        out: dict[str, str] = {}
        # 2-cell table rows (label | value).
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) == 2:
                label = self._clean(cells[0].get_text(" ", strip=True)).rstrip(":").lower()
                value = self._clean(cells[1].get_text(" ", strip=True))
                if label and value and label not in out:
                    out[label] = value
        # <dl> definition lists.
        for dl in soup.find_all("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                label = self._clean(dt.get_text(" ", strip=True)).rstrip(":").lower()
                value = self._clean(dd.get_text(" ", strip=True))
                if label and value and label not in out:
                    out[label] = value
        return out

    @staticmethod
    def _split_address(raw: str) -> tuple[str, str, str]:
        """Split "STREET, CITY, OH ZIP" into (street, city, zip)."""
        if not raw:
            return "", "", ""
        cleaned = re.sub(r"\s+", " ", raw).strip().rstrip(",")
        m = ADDRESS_LINE_RE.search(cleaned)
        if m:
            return (
                m.group("street").strip(),
                m.group("city").strip(),
                (m.group("zip") or "").strip(),
            )
        return cleaned, "", ""

    @staticmethod
    def _clean(text: str) -> str:
        """Collapse whitespace + strip &nbsp;."""
        if not text:
            return ""
        cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ")
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _clean_defendant(raw: str) -> str:
        """Strip trailing ', et al.' / ', et al' suffixes."""
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
    def _to_notice_data(
        self,
        row: _CaseRow,
        detail: _CaseDetail | None,
    ) -> NoticeData:
        owner = self._clean_defendant(detail.defendant if detail else "")
        file_iso = row.file_date.isoformat() if row.file_date else ""
        detail_url = detail.detail_url if detail else (
            f"{LANDING_URL}?case_id={row.case_id}"
        )
        raw_text = (
            f"Case Number: {row.case_number}\n"
            f"Case Type: {row.case_type}\n"
            f"Caption: {row.caption}\n"
            f"Filing Date: {file_iso}\n"
            f"Status: {row.status}\n"
            f"Plaintiff: {detail.plaintiff if detail else ''}\n"
            f"Defendant: {detail.defendant if detail else ''}\n"
            f"Property Address: {detail.address_raw if detail else ''}\n"
        )
        return NoticeData(
            date_added=file_iso,
            auction_date="",
            county=self.county,
            state="OH",
            notice_type=self.notice_type,
            source_url=detail_url,
            address=detail.street if detail else "",
            city=detail.city if detail else "",
            zip=detail.zip_code if detail else "",
            owner_name=owner,
            raw_text=raw_text,
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

    parser = argparse.ArgumentParser(description="Test Montgomery lis pendens scraper")
    parser.add_argument("--days", type=int, default=14, help="Look back N days (default 14)")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_montgomery_lis_pendens.csv",
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
