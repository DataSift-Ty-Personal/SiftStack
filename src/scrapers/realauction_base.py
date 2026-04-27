"""Base class for Ohio RealAuction sheriff-sale foreclosure scrapers.

RealAuction operates ~30 OH county sheriff-sale auction sites under the
pattern `{county}.sheriffsaleauction.ohio.gov`. The HTML shape, AJAX
endpoints, login handshake, and pagination behavior are identical across
counties — only the subdomain differs. This base class encapsulates all
the shared logic; per-county subclasses set 4 class attributes and are
otherwise empty.

Subclass example:
    from scrapers.realauction_base import RealAuctionScraper

    class Scraper(RealAuctionScraper):
        realauction_subdomain = "montgomery"
        county = "Montgomery"
        source_name = "Montgomery County Sheriff Sale Auction"
        source_url = "https://montgomery.sheriffsaleauction.ohio.gov"

Portal characteristics (researched against live sites; identical per county):
  * Tech stack: ColdFusion (index.cfm) with jQuery + blockUI. Bot-blocked
    (403) via plain `requests` — Playwright required.
  * Login is OPTIONAL for the public PREVIEW page (case#, parcel, address,
    appraised, opening bid, status). Login unlocks the per-auction DETAILS
    page where plaintiff + defendant names live.
  * Splash → "AUCTION CALENDAR" lands on
      index.cfm?zaction=USER&zmethod=CALENDAR
    Each sale day is a div[dayid="MM/DD/YYYY"] tagged "Foreclosure".
  * Sale-day preview at
      index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE=MM/DD/YYYY
    contains three stacked areas:
      Area_W "Waiting"  — upcoming sales
      Area_R "Running"  — live/closed-today sales
      Area_C "Closed"   — withdrawn / bankruptcy / cancelled
    Each area paginates server-side (10 items / page). We walk every page.
  * Login POST: ZACTION=AJAX&ZMETHOD=LOGIN&func=LOGIN&USERNAME=&USERPASS=
    Returns JSON {isOk: "YES" | "MFA" | "NO"}. MFA accounts cannot be
    auto-logged-in — scraper logs the warning and continues with PREVIEW
    data only. Cookies persist per-county in realauction_cookies_<county>.json.
  * Anti-bot: realistic User-Agent + 3-5s jitter between navigations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# How far ahead to look for scheduled sales when caller doesn't ask for a
# specific window. RealAuction posts foreclosures roughly 4-8 weeks before
# the sale date.
DEFAULT_LOOKAHEAD_WEEKS = 12

# How far back to look by default. Set to 35 days so the daily run
# automatically captures all sales whose post-auction redemption window
# (typically 7-30 days under ORC §2329.33) is still open. Without this,
# the watcher misses cases where the sheriff sale happened ~10-25 days ago
# and confirmation hasn't yet been entered. RealAuction keeps past sale
# days visible in the calendar; PREVIEW pages still render their AITEMs
# with auction_status = "Sold" / "Withdrawn" / etc.
DEFAULT_LOOKBACK_DAYS = 35

# Max pagination pages per area per sale day (guard against bad counters).
MAX_PAGES_PER_AREA = 20


@dataclass
class _AuctionItem:
    """Internal tuple for fields parsed from a single AITEM preview row."""
    aid: str                     # internal RealAuction auction id, e.g. "52160"
    auction_date: date           # MM/DD/YYYY from sale-day URL
    auction_status: str          # "Scheduled", "Sold", "Withdrawn", "Bankruptcy", "Cancelled", ...
    auction_sold_amount: str     # "$330,000.00" if sold, else ""
    auction_sold_to: str         # "Plaintiff" / "Third Party" / "" if not sold
    case_status: str             # "ACTIVE" / "INACTIVE"
    case_number: str             # e.g. "24CV3703 (16200)"
    parcel_id: str
    street: str                  # property address line 1
    city: str
    zip_code: str                # 5-digit zip
    appraised_value: str         # "$237,000.00"
    opening_bid: str             # "$158,000.00"
    deposit_requirement: str     # "$10,000.00"
    source_url: str              # sale-day preview URL
    raw_text: str                # full visible text of the AITEM div


class RealAuctionScraper(NoticeScraper):
    """Base class for OH RealAuction sheriff-sale foreclosure scrapers.

    Subclasses set:
      - realauction_subdomain (e.g. "franklin", "montgomery", "greene")
      - county (e.g. "Franklin")
      - source_name (display name)
      - source_url (root URL — same as base_url)
    """

    # Per-subclass: must be set
    realauction_subdomain: str = ""

    # Defaults for all RealAuction scrapers
    notice_type: str = "foreclosure"
    requires_account: bool = True

    def required_credentials(self) -> list[str]:
        return ["REALAUCTION_EMAIL", "REALAUCTION_PASSWORD"]

    # ── Derived URLs ───────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        if not self.realauction_subdomain:
            raise ValueError(
                f"{type(self).__name__} must set `realauction_subdomain` class attribute"
            )
        return f"https://{self.realauction_subdomain}.sheriffsaleauction.ohio.gov"

    @property
    def landing_url(self) -> str:
        return f"{self.base_url}/"

    @property
    def login_post_url(self) -> str:
        return f"{self.base_url}/index.cfm"

    @property
    def cookies_file(self) -> Path:
        # Per-county cookies — separate logged-in sessions per subdomain.
        # If county isn't set yet (test instantiation), fall back to a generic name.
        suffix = (self.county or self.realauction_subdomain or "default").lower()
        return config.PROJECT_ROOT / f"realauction_cookies_{suffix}.json"

    # ── Public scrape entrypoint ───────────────────────────────────────
    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull all sheriff-sale foreclosures scheduled/closed on/after `since_date`.

        Default lookback is DEFAULT_LOOKBACK_DAYS (35) — wide enough to keep
        post-auction records flowing through the daily pipeline for the full
        statutory redemption window, so the redemption_watcher has fresh
        records to update each day.
        """
        if since_date is None:
            since_date = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

        if not config.REALAUCTION_EMAIL or not config.REALAUCTION_PASSWORD:
            raise ValueError(
                "REALAUCTION_EMAIL/REALAUCTION_PASSWORD required — "
                f"sign up free at {self.landing_url}"
            )

        return await self._scrape_async(since_date)

    # ── Core async flow ────────────────────────────────────────────────
    async def _scrape_async(self, since_date: date) -> list[NoticeData]:
        horizon = date.today() + timedelta(weeks=DEFAULT_LOOKAHEAD_WEEKS)
        logger.info(
            "%s foreclosure scrape: sales between %s and %s",
            self.county,
            since_date.isoformat(),
            horizon.isoformat(),
        )

        records: list[NoticeData] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            self._load_cookies(context)

            try:
                page = await context.new_page()

                logged_in = False
                try:
                    logged_in = await self._ensure_logged_in(page)
                except Exception as e:
                    logger.warning(
                        "Login failed (%s) — continuing with public-only data", e
                    )

                sale_days = await self._find_foreclosure_sale_days(
                    page, since_date, horizon
                )
                logger.info(
                    "Found %d %s foreclosure sale day(s)",
                    len(sale_days),
                    self.county,
                )

                for sale_day in sale_days:
                    items = await self._harvest_day(page, sale_day)
                    logger.info(
                        "  %s: %d auction item(s)",
                        sale_day.isoformat(),
                        len(items),
                    )

                    owner_lookup: dict[str, dict[str, str]] = {}
                    if logged_in:
                        for item in items:
                            parties = await self._fetch_details_parties(page, item.aid)
                            if parties:
                                owner_lookup[item.aid] = parties
                            await self._sleep()

                    for item in items:
                        parties = owner_lookup.get(item.aid, {})
                        records.append(self._to_notice_data(item, parties))

                self._save_cookies(context)
            finally:
                await context.close()
                await browser.close()

        records.sort(key=lambda r: (r.auction_date, r.source_url))
        logger.info(
            "%s foreclosure scrape done — %d records", self.county, len(records)
        )
        return records

    # ── Login / session ────────────────────────────────────────────────
    async def _ensure_logged_in(self, page: Page) -> bool:
        """Log in if not already. Returns True when authenticated."""
        await page.goto(self.landing_url, wait_until="domcontentloaded", timeout=30000)
        await self._sleep()

        if await page.query_selector("#LogName") is None:
            logger.info("Already logged into RealAuction (cookie reuse)")
            return True

        logger.info("Logging into %s RealAuction as %s", self.county, config.REALAUCTION_EMAIL)
        resp = await page.context.request.post(
            self.login_post_url,
            form={
                "ZACTION": "AJAX",
                "ZMETHOD": "LOGIN",
                "func": "LOGIN",
                "USERNAME": config.REALAUCTION_EMAIL,
                "USERPASS": config.REALAUCTION_PASSWORD,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.landing_url,
            },
            timeout=30000,
        )
        try:
            payload = json.loads(await resp.text())
        except json.JSONDecodeError:
            logger.warning("Login response was not JSON — assuming failure")
            return False

        is_ok = payload.get("isOk")
        if is_ok == "YES":
            logger.info("Login successful (%s)", self.county)
            return True
        if is_ok == "MFA":
            logger.warning(
                "%s account has MFA enabled — cannot auto-complete second factor. "
                "Continuing with public-only data.", self.county,
            )
            return False
        logger.warning(
            "%s login rejected: isOk=%s msg=%s",
            self.county,
            is_ok,
            payload.get("logMsg"),
        )
        return False

    def _load_cookies(self, context: BrowserContext) -> None:
        if not self.cookies_file.exists():
            return
        try:
            data = json.loads(self.cookies_file.read_text())
            asyncio.get_event_loop().create_task(context.add_cookies(data))
        except Exception as e:
            logger.debug("Cookie load failed (non-fatal): %s", e)

    def _save_cookies(self, context: BrowserContext) -> None:
        async def _dump() -> None:
            cookies = await context.cookies()
            self.cookies_file.write_text(json.dumps(cookies, indent=2))
        try:
            asyncio.get_event_loop().create_task(_dump())
        except Exception as e:
            logger.debug("Cookie save failed (non-fatal): %s", e)

    # ── Calendar discovery ─────────────────────────────────────────────
    async def _find_foreclosure_sale_days(
        self,
        page: Page,
        since_date: date,
        horizon: date,
    ) -> list[date]:
        """Walk the auction calendar month-by-month, return FC sale days."""
        days: set[date] = set()

        cursor = date(since_date.year, since_date.month, 1)
        while cursor <= horizon:
            cal_url = (
                f"{self.base_url}/index.cfm?zaction=user&zmethod=calendar"
                f"&selCalDate={cursor.month:02d}%2F01%2F{cursor.year}"
            )
            await page.goto(cal_url, wait_until="domcontentloaded", timeout=30000)
            await self._sleep()
            cells = await page.evaluate(
                """() => Array.from(document.querySelectorAll('[dayid]')).map(el => ({
                    dayid: el.getAttribute('dayid'),
                    text: el.innerText.trim()
                }))"""
            )
            for cell in cells:
                label = cell["text"] or ""
                if "Foreclosure" not in label:
                    continue
                try:
                    d = datetime.strptime(cell["dayid"], "%m/%d/%Y").date()
                except ValueError:
                    continue
                if since_date <= d <= horizon:
                    days.add(d)
            cursor = _add_month(cursor)

        return sorted(days)

    # ── Sale-day harvest ───────────────────────────────────────────────
    async def _harvest_day(self, page: Page, sale_day: date) -> list[_AuctionItem]:
        url = (
            f"{self.base_url}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW"
            f"&AUCTIONDATE={sale_day.month:02d}/{sale_day.day:02d}/{sale_day.year}"
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector('[id^="AITEM_"]', timeout=15000)
        except PlaywrightTimeoutError:
            logger.debug("No AITEMs rendered for %s on %s", self.county, sale_day)
            return []

        items: list[_AuctionItem] = []
        seen_aids: set[str] = set()

        for area_code, btn_area in (("W", "W"), ("C", "C"), ("R", "R")):
            page_num = 1
            while page_num <= MAX_PAGES_PER_AREA:
                page_items = await self._extract_area_items(
                    page, area_code, sale_day, url
                )
                new_count = 0
                for it in page_items:
                    if it.aid not in seen_aids:
                        seen_aids.add(it.aid)
                        items.append(it)
                        new_count += 1
                if new_count == 0:
                    break
                advanced = await self._advance_area_page(page, btn_area)
                if not advanced:
                    break
                page_num += 1
                await self._sleep()

        return items

    async def _extract_area_items(
        self,
        page: Page,
        area_code: str,
        sale_day: date,
        source_url: str,
    ) -> list[_AuctionItem]:
        """Parse all AITEM divs currently rendered inside Area_<code>."""
        raw_items = await page.evaluate(
            f"""() => {{
                const area = document.querySelector('#Area_{area_code}');
                if (!area) return [];
                return Array.from(area.querySelectorAll('[id^="AITEM_"]')).map(el => ({{
                    aid: el.getAttribute('aid'),
                    text: el.innerText
                }}));
            }}"""
        )
        parsed: list[_AuctionItem] = []
        for raw in raw_items:
            try:
                parsed.append(
                    self._parse_item(raw["aid"], raw["text"], sale_day, source_url)
                )
            except Exception as e:
                logger.debug("Failed to parse AITEM %s: %s", raw.get("aid"), e)
        return parsed

    async def _advance_area_page(self, page: Page, btn_area: str) -> bool:
        """Click the PageRight arrow for this area. Returns True if we moved."""
        counters = await page.evaluate(
            f"""() => {{
                const cur = document.querySelector('#curP{btn_area}B');
                const max = document.querySelector('#max{btn_area}B');
                return {{
                    cur: cur ? parseInt(cur.getAttribute('curpg') || cur.value || '1', 10) : 1,
                    max: max ? parseInt(max.innerText || '1', 10) : 1
                }};
            }}"""
        )
        if counters["cur"] >= counters["max"]:
            return False

        for frame_sel in (
            f'.PageFrame[area="{btn_area}"] .PageRight',
        ):
            el = await page.query_selector(frame_sel)
            if el is None:
                continue
            try:
                prev_text = await page.evaluate(
                    f"""() => {{
                        const a = document.querySelector('#Area_{btn_area}');
                        return a ? a.innerText.slice(0, 200) : '';
                    }}"""
                )
                await el.click(force=True)
                try:
                    await page.wait_for_function(
                        f"""() => {{
                            const a = document.querySelector('#Area_{btn_area}');
                            return a && a.innerText.slice(0, 200) !== {json.dumps(prev_text)};
                        }}""",
                        timeout=15000,
                    )
                    return True
                except PlaywrightTimeoutError:
                    return False
            except Exception as e:
                logger.debug("Click on %s failed: %s", frame_sel, e)
                continue
        return False

    # ── Parsing ────────────────────────────────────────────────────────
    _FIELD_RE = re.compile(
        r"(?P<label>Case Status|Case #|Parcel ID|Property Address|Appraised Value|Opening Bid|Deposit Requirement)\s*:?\s*(?P<value>.*)",
        re.IGNORECASE,
    )
    _ZIP_TAIL_RE = re.compile(r"(\d{5})(\d{0,4})$")

    def _parse_item(
        self,
        aid: str,
        text: str,
        sale_day: date,
        source_url: str,
    ) -> _AuctionItem:
        """Turn an AITEM's innerText blob into a structured _AuctionItem."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        status = ""
        sold_amount = ""
        sold_to = ""
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln == "Auction Starts":
                status = "Scheduled"
                i += 1
                continue
            if ln == "Auction Status":
                status = lines[i + 1] if i + 1 < len(lines) else "Unknown"
                i += 2
                continue
            if ln == "Auction Sold":
                status = "Sold"
                for j in range(i + 1, min(i + 6, len(lines))):
                    if lines[j].startswith("Amount "):
                        sold_amount = lines[j][len("Amount "):].strip()
                    if lines[j] == "Sold To" and j + 1 < len(lines):
                        sold_to = lines[j + 1]
                i += 1
                continue
            i += 1

        fields = self._split_label_value_pairs(lines)

        street, city, zip_code = self._split_address(
            fields.get("Property Address", ""),
            text,
        )

        return _AuctionItem(
            aid=aid,
            auction_date=sale_day,
            auction_status=status or "Unknown",
            auction_sold_amount=sold_amount,
            auction_sold_to=sold_to,
            case_status=fields.get("Case Status", ""),
            case_number=fields.get("Case #", "").strip(),
            parcel_id=fields.get("Parcel ID", "").strip(),
            street=street,
            city=city,
            zip_code=zip_code,
            appraised_value=fields.get("Appraised Value", "").strip(),
            opening_bid=fields.get("Opening Bid", "").strip(),
            deposit_requirement=fields.get("Deposit Requirement", "").strip(),
            source_url=source_url,
            raw_text=text,
        )

    @staticmethod
    def _split_label_value_pairs(lines: list[str]) -> dict[str, str]:
        """Key the rendered AITEM rows by label."""
        out: dict[str, str] = {}
        labels = (
            "Case Status",
            "Case #",
            "Parcel ID",
            "Property Address",
            "Appraised Value",
            "Opening Bid",
            "Deposit Requirement",
        )
        for ln in lines:
            if "\t" in ln:
                label, _, value = ln.partition("\t")
                label = label.rstrip(":").strip()
                if label in labels:
                    out[label] = value.strip()
        return out

    def _split_address(self, street_raw: str, full_text: str) -> tuple[str, str, str]:
        """Return (street, city, zip) for a PREVIEW AITEM."""
        street = street_raw.strip()
        city = ""
        zip5 = ""

        m = re.search(
            r"^\s*([A-Z][A-Z .'-]+)\s*,\s*(\d{5})(\d{4})?\s*$",
            full_text,
            re.MULTILINE,
        )
        if m:
            city = m.group(1).strip().title()
            zip5 = m.group(2)
        else:
            m2 = re.search(r"([A-Z][A-Z .'-]+)\s*,\s*(\d{5})(\d{4})?", full_text)
            if m2:
                city = m2.group(1).strip().title()
                zip5 = m2.group(2)

        return street, city, zip5

    # ── DETAILS page (plaintiff / defendant) ───────────────────────────
    async def _fetch_details_parties(
        self,
        page: Page,
        aid: str,
    ) -> dict[str, str]:
        """Visit the DETAILS URL and extract plaintiff / defendant. Login required."""
        url = f"{self.base_url}/index.cfm?zaction=AUCTION&Zmethod=DETAILS&AID={aid}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            return {}
        text = (await page.inner_text("body")).strip()
        if "User Name" in text and "User Password" in text and "Log" in text[:500]:
            return {}
        return _extract_parties(text)

    # ── NoticeData conversion ──────────────────────────────────────────
    def _to_notice_data(
        self,
        item: _AuctionItem,
        parties: dict[str, str],
    ) -> NoticeData:
        sale_iso = item.auction_date.isoformat()
        defendant = parties.get("defendant", "")
        plaintiff = parties.get("plaintiff", "")

        raw_lines = [
            f"Case #: {item.case_number}",
            f"Case Status: {item.case_status}",
            f"Parcel ID: {item.parcel_id}",
            f"Auction Date: {sale_iso}",
            f"Auction Status: {item.auction_status}",
            f"Appraised Value: {item.appraised_value}",
            f"Opening Bid: {item.opening_bid}",
            f"Deposit Requirement: {item.deposit_requirement}",
        ]
        if item.auction_sold_amount:
            raw_lines.append(f"Sold Amount: {item.auction_sold_amount}")
        if item.auction_sold_to:
            raw_lines.append(f"Sold To: {item.auction_sold_to}")
        if plaintiff:
            raw_lines.append(f"Plaintiff: {plaintiff}")
        if defendant:
            raw_lines.append(f"Defendant: {defendant}")
        raw_lines.append("---")
        raw_lines.append(item.raw_text)

        return NoticeData(
            date_added=sale_iso,
            auction_date=sale_iso,
            address=item.street,
            city=item.city,
            state="OH",
            zip=item.zip_code,
            owner_name=defendant,
            notice_type=self.notice_type,
            county=self.county,
            source_url=item.source_url,
            parcel_id=item.parcel_id,
            case_number=_normalize_case_number(item.case_number),
            raw_text="\n".join(raw_lines),
        )

    # ── Misc ───────────────────────────────────────────────────────────
    async def _sleep(self) -> None:
        """3-5s jittered delay between navigations (RealAuction anti-bot)."""
        delay = random.uniform(3.0, 5.0)
        await asyncio.sleep(delay)


# ── Pure-function helpers (no self; testable in isolation) ────────────
def _add_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


_DEFENDANT_RE = re.compile(
    r"Defendant[s]?\s*[:\-]?\s*(?P<name>[^\n]{2,200})",
    re.IGNORECASE,
)
_PLAINTIFF_RE = re.compile(
    r"Plaintiff[s]?\s*[:\-]?\s*(?P<name>[^\n]{2,200})",
    re.IGNORECASE,
)


def _extract_parties(body_text: str) -> dict[str, str]:
    """Pull plaintiff + defendant out of a DETAILS body."""
    out: dict[str, str] = {}
    m = _DEFENDANT_RE.search(body_text)
    if m:
        out["defendant"] = m.group("name").strip(" .;,")
    m = _PLAINTIFF_RE.search(body_text)
    if m:
        out["plaintiff"] = m.group("name").strip(" .;,")
    return out


# Case number patterns vary slightly per county. This normalizer strips
# parenthetical RealAuction internal IDs ("24CV3703 (16200)" → "24CV3703")
# and spaces ("24 CV 003703" → "24CV003703") so cross-source dedup works.
def _normalize_case_number(raw: str) -> str:
    """Normalize a case number for cross-source joining (e.g. with lis pendens)."""
    if not raw:
        return ""
    # Drop parenthetical suffix
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    # Drop internal whitespace
    cleaned = re.sub(r"\s+", "", cleaned).upper()
    return cleaned
