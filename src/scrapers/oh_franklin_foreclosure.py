"""Franklin County (Columbus), Ohio foreclosure scraper.

Pulls sheriff-sale foreclosures from the Franklin County Sheriff's RealAuction
portal (franklin.sheriffsaleauction.ohio.gov). The platform is shared across
most Ohio counties that use RealAuction — the per-county URL differs but the
HTML shape and session handshake is identical.

Portal characteristics (researched against live site):
  * Tech stack: ColdFusion (index.cfm) with a thin jQuery layer and blockUI.
    Endpoints follow ?zaction=X&zmethod=Y. Bot-blocked (403) via plain requests,
    so Playwright is required.
  * Login is OPTIONAL for most of what we need. The auction "preview" page
    (case #, parcel, address, appraised value, opening bid, auction status)
    is fully public. Login only unlocks the per-auction DETAILS page, which
    is where plaintiff + defendant names live.
  * Splash → "AUCTION CALENDAR" button lands on
      index.cfm?zaction=USER&zmethod=CALENDAR
    Each sale day is a div[dayid="MM/DD/YYYY"] tagged "Foreclosure" with a
    scheduled / sold count (e.g. "0 / 16 FC"). Sales run Fridays at 9:00 AM ET.
  * A sale-day preview lives at
      index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE=MM/DD/YYYY
    and contains three stacked areas:
      Area_W  "Waiting"  — upcoming sales (for an upcoming day)
      Area_R  "Running"  — live/closed-today sales (live on sale day)
      Area_C  "Closed"   — withdrawn / bankruptcy / cancelled
    Each area has server-side pagination (10 items per page, #curPWB + #maxWB,
    etc.). We walk every page of every area to gather the full day's list.
  * Login is a POST to /index.cfm with body
      ZACTION=AJAX&ZMETHOD=LOGIN&func=LOGIN&USERNAME=...&USERPASS=...
    Response is JSON {isOk: "YES" | "MFA" | "NO", logMsg: "..."}. Accounts
    with MFA enabled will require a one-time code we cannot automate — the
    scraper surfaces MFA as a clear error and continues without plaintiff
    names (still produces useful records).
  * Anti-bot: fresh headless Chromium with a realistic User-Agent works; the
    plain `requests` library is blocked with 403. Cookies persist the logged-in
    session across runs in `realauction_cookies.json`.

Notes on fields:
  * Property address is shown across two rows (line 1 = street, line 2 =
    "CITY , ZIP" as a 9-digit zip+4 with no dash — e.g. "COLUMBUS , 432070000").
  * Appraised Value + Opening Bid are formatted "$234,567.89" strings.
  * Auction status varies: "Auction Starts MM/DD/YYYY HH:MM AM ET" for
    upcoming, "Auction Sold MM/DD/YYYY ... Amount $X Sold To Plaintiff/3rd Party"
    for closed sales, or "Auction Status Withdrawn/Bankruptcy/Cancelled".

NoticeData contract per CLAUDE.md domain rules:
  * county = "Franklin", state = "OH", notice_type = "foreclosure".
  * date_added = the sheriff-sale auction date (the "publish" date we have).
  * auction_date = same ISO date as date_added.
  * owner_name = defendant from the DETAILS page when logged in; blank
    otherwise (the enrichment pipeline resolves it via Franklin County
    Auditor / parcel lookup downstream).
  * source_url = direct link to the sale-day preview page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

BASE_URL = "https://franklin.sheriffsaleauction.ohio.gov"
LANDING_URL = f"{BASE_URL}/"
CALENDAR_URL = f"{BASE_URL}/index.cfm?zaction=USER&zmethod=CALENDAR"
LOGIN_POST_URL = f"{BASE_URL}/index.cfm"
COOKIES_FILE = config.PROJECT_ROOT / "realauction_cookies.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# How far out to look for scheduled sales when the caller doesn't ask for a
# specific window. RealAuction posts foreclosures roughly 4-8 weeks before
# the sale — we pull anything on or after `since_date` and up to 12 weeks
# past today so the daily pipeline reliably catches newly-listed cases.
DEFAULT_LOOKAHEAD_WEEKS = 12

# Max pages per area per sale day — generous guard against infinite loops
# if pagination ever misreports its last page.
MAX_PAGES_PER_AREA = 20

# Credential sign-up URL surfaced in the credential-missing error message.
SIGNUP_URL = "https://franklin.sheriffsaleauction.ohio.gov"


@dataclass
class _AuctionItem:
    """Internal tuple for fields parsed from a single AITEM preview row."""
    aid: str                     # internal RealAuction auction id, e.g. "52160"
    auction_date: date           # MM/DD/YYYY from the sale-day URL
    auction_status: str          # "Scheduled", "Sold", "Withdrawn", "Bankruptcy", "Cancelled", ...
    auction_sold_amount: str     # "$330,000.00" if sold, else ""
    auction_sold_to: str         # "Plaintiff" / "Third Party" / "" if not sold
    case_status: str             # "ACTIVE" / "INACTIVE" — from the table
    case_number: str             # e.g. "24CV3703 (16200)"
    parcel_id: str
    street: str                  # property address line 1
    city: str
    zip_code: str                # 5-digit zip extracted from the "432070000" form
    appraised_value: str         # "$237,000.00"
    opening_bid: str             # "$158,000.00"
    deposit_requirement: str     # "$10,000.00"
    source_url: str              # sale-day preview URL
    raw_text: str                # full visible text of the AITEM div


class Scraper(NoticeScraper):
    """Franklin County Foreclosure — RealAuction sheriff-sale scraper."""

    county = "Franklin"
    notice_type = "foreclosure"
    source_name = "Franklin County Sheriff Sale Auction"
    source_url = LANDING_URL
    requires_account = True

    def required_credentials(self) -> list[str]:
        return ["REALAUCTION_EMAIL", "REALAUCTION_PASSWORD"]

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull all sheriff-sale foreclosures scheduled/closed on/after `since_date`.

        Fetches the public preview pages first, which is enough for
        address/case#/appraised/opening bid. If REALAUCTION credentials are
        set, logs in and visits each item's DETAILS page to pick up the
        plaintiff / defendant names.
        """
        if since_date is None:
            since_date = date.today() - timedelta(days=7)

        # Credentials are technically required per the ScraperSource registry
        # (Phase 6 uses requires_account + required_credentials to gate
        # dispatch), but the PREVIEW endpoints work without them. Fail fast
        # when neither env var is set so misconfiguration is obvious.
        if not config.REALAUCTION_EMAIL or not config.REALAUCTION_PASSWORD:
            raise ValueError(
                "REALAUCTION_EMAIL/REALAUCTION_PASSWORD required — "
                f"sign up free at {SIGNUP_URL}"
            )

        return await self._scrape_async(since_date)

    # ── Core async flow ────────────────────────────────────────────────
    async def _scrape_async(self, since_date: date) -> list[NoticeData]:
        horizon = date.today() + timedelta(weeks=DEFAULT_LOOKAHEAD_WEEKS)
        logger.info(
            "Franklin foreclosure scrape: sales between %s and %s",
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

                # Log in opportunistically — a failure here doesn't block the
                # PREVIEW-only harvest, we just skip the DETAILS pass.
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
                logger.info("Found %d foreclosure sale day(s)", len(sale_days))

                for sale_day in sale_days:
                    items = await self._harvest_day(page, sale_day)
                    logger.info(
                        "  %s: %d auction item(s)",
                        sale_day.isoformat(),
                        len(items),
                    )

                    # DETAILS pass — only if logged in. Pulls plaintiff/defendant
                    # onto each item. (Owner lookup happens later via the
                    # enrichment pipeline if this step is skipped.)
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
        logger.info("Franklin foreclosure scrape done — %d records", len(records))
        return records

    # ── Login / session ────────────────────────────────────────────────
    async def _ensure_logged_in(self, page: Page) -> bool:
        """Log in if not already. Returns True when authenticated."""
        # Posting the AJAX login endpoint directly skips the need to interact
        # with the splash form's JS. Uses APIRequestContext so cookies apply
        # to the shared BrowserContext.
        await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=30000)
        await self._sleep()

        # If we're already logged in from persisted cookies, the landing page
        # won't render the #LogName input (it's replaced with a user menu).
        if await page.query_selector("#LogName") is None:
            logger.info("Already logged into RealAuction (cookie reuse)")
            return True

        logger.info("Logging into RealAuction as %s", config.REALAUCTION_EMAIL)
        resp = await page.context.request.post(
            LOGIN_POST_URL,
            form={
                "ZACTION": "AJAX",
                "ZMETHOD": "LOGIN",
                "func": "LOGIN",
                "USERNAME": config.REALAUCTION_EMAIL,
                "USERPASS": config.REALAUCTION_PASSWORD,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": LANDING_URL,
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
            logger.info("Login successful")
            return True
        if is_ok == "MFA":
            logger.warning(
                "Account has MFA enabled — cannot auto-complete second factor. "
                "Continuing with public-only data."
            )
            return False
        logger.warning(
            "Login rejected: isOk=%s msg=%s",
            is_ok,
            payload.get("logMsg"),
        )
        return False

    def _load_cookies(self, context: BrowserContext) -> None:
        if not COOKIES_FILE.exists():
            return
        try:
            data = json.loads(COOKIES_FILE.read_text())
            # Playwright's add_cookies is synchronous via its wrapper, but
            # we use the async form in the caller — keep it simple: schedule
            # via asyncio.run_coroutine_threadsafe is overkill. Instead, call
            # through the context.add_cookies method synchronously from the
            # containing coroutine.
            asyncio.get_event_loop().create_task(context.add_cookies(data))
        except Exception as e:
            logger.debug("Cookie load failed (non-fatal): %s", e)

    def _save_cookies(self, context: BrowserContext) -> None:
        async def _dump() -> None:
            cookies = await context.cookies()
            COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
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

        # Navigate months from `since_date`'s month through `horizon`'s month.
        cursor = date(since_date.year, since_date.month, 1)
        while cursor <= horizon:
            cal_url = (
                f"{BASE_URL}/index.cfm?zaction=user&zmethod=calendar"
                f"&selCalDate={cursor.month:02d}%2F01%2F{cursor.year}"
            )
            await page.goto(cal_url, wait_until="domcontentloaded", timeout=30000)
            await self._sleep()
            # Every day cell with a sale carries dayid="MM/DD/YYYY" and the
            # type label lives in a nested <b> tag — "Foreclosure" / "Tax Lien".
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
            # Advance one month.
            cursor = _add_month(cursor)

        return sorted(days)

    # ── Sale-day harvest ───────────────────────────────────────────────
    async def _harvest_day(self, page: Page, sale_day: date) -> list[_AuctionItem]:
        url = (
            f"{BASE_URL}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW"
            f"&AUCTIONDATE={sale_day.month:02d}/{sale_day.day:02d}/{sale_day.year}"
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Wait briefly for the areas to render; the page fires a few AJAX
        # LOAD requests on arrival before the AITEMs populate.
        try:
            await page.wait_for_selector('[id^="AITEM_"]', timeout=15000)
        except PlaywrightTimeoutError:
            # A day with zero items is possible (sale cancelled, etc).
            logger.debug("No AITEMs rendered for %s", sale_day)
            return []

        items: list[_AuctionItem] = []
        seen_aids: set[str] = set()

        # Walk Waiting (W) and Closed (C). Running (R) only populates during
        # the live sale; its items overlap with W/C at other times.
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
                # Stop when this page added nothing new (end of area or we've
                # loopbacked to an already-scraped page).
                if new_count == 0:
                    break
                # Try to go to the next page of this area. Each PageFrame has
                # two clickable navigators (PageLeft/PageRight spans). Click
                # the first visible PageRight whose nearest ancestor frame
                # is this area.
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
        # The PageFrame for each area has attribute area="W"/"C"/"R" and
        # contains a .PageRight <span> we can click. jQuery binds the click
        # handler via main_site.js; the server returns the next page of items
        # via Zmethod=UPDATE and swaps them in.
        #
        # First check the current page vs max to avoid overshooting.
        counters = await page.evaluate(
            f"""() => {{
                // Pagination counters render TWICE per area (top + bottom
                // PageFrames — #curPW A/B, #maxW A/B, same for C/R). Read
                // the bottom one (B suffix) since it always matches the
                // visible rows.
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

        # Click the bottom-frame PageRight (more reliable — it's inside the
        # scroll area). We fall back to the top frame if needed.
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
                # Wait for the area's innerText to change, indicating AJAX
                # swap completed.
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

        # Auction status — the first non-label line after "Auction Starts"
        # / "Auction Status" / "Auction Sold" sentinels.
        status = ""
        sold_amount = ""
        sold_to = ""
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln == "Auction Starts":
                # Next line is a datetime. Treat as Scheduled.
                status = "Scheduled"
                i += 1
                continue
            if ln == "Auction Status":
                # Next line is the status word ("Withdrawn" / "Bankruptcy" / ...).
                status = lines[i + 1] if i + 1 < len(lines) else "Unknown"
                i += 2
                continue
            if ln == "Auction Sold":
                status = "Sold"
                # Lookahead for "Amount $X" and "Sold To <party>".
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
            # The address's second line is LABEL-less in the rendered innerText
            # — it lives in the raw text but may not match the Property
            # Address key. We pass the full text so _split_address can locate it.
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
        """Accept the innerText of an AITEM and key its rows by label.

        Each row on the page is a 2-cell row; innerText joins them with a
        tab character. Some rows (like the address continuation line) are
        label-less and render as a plain line — we don't emit a key for
        those here; the caller handles the address specially.
        """
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
            # Each row is "Label:\tValue" after innerText.
            if "\t" in ln:
                label, _, value = ln.partition("\t")
                label = label.rstrip(":").strip()
                if label in labels:
                    out[label] = value.strip()
        return out

    def _split_address(self, street_raw: str, full_text: str) -> tuple[str, str, str]:
        """Return (street, city, zip) for a PREVIEW AITEM.

        The rendered block looks like:
            Property Address:\t123 MAIN ST
                               \tCOLUMBUS , 432070000
        We locate the city-line by matching " CITY , DDDDDDDDD" in the blob.
        The zip "432070000" is zip5 + zip+4 concatenated with no dash; we
        take only the first 5 digits.
        """
        street = street_raw.strip()
        city = ""
        zip5 = ""

        # Pull the "CITY , 9DIGITS" line.
        m = re.search(
            r"^\s*([A-Z][A-Z .'-]+)\s*,\s*(\d{5})(\d{4})?\s*$",
            full_text,
            re.MULTILINE,
        )
        if m:
            city = m.group(1).strip().title()
            zip5 = m.group(2)
        else:
            # Rare: the continuation line isn't on its own row. Fall back to
            # scanning for the same pattern anywhere in the text.
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
        """Visit the DETAILS URL for an auction and extract plaintiff / defendant.

        Only callable when logged in. Returns {} if the page is gated or
        the expected labels aren't present (defensive against layout changes).
        """
        url = f"{BASE_URL}/index.cfm?zaction=AUCTION&Zmethod=DETAILS&AID={aid}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            return {}
        text = (await page.inner_text("body")).strip()
        # Cheap gate detection: if the body still renders the login form,
        # we're not authenticated and should bail gracefully.
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
    """Pull plaintiff + defendant names out of a DETAILS page body.

    The DETAILS layout isn't visible to us without credentials; we match
    the most common RealAuction label forms. This is defensive — if the
    regexes don't match, we return {} and the enrichment pipeline will
    fill owner_name from the Franklin County Auditor.
    """
    out: dict[str, str] = {}
    m = _DEFENDANT_RE.search(body_text)
    if m:
        out["defendant"] = m.group("name").strip(" .;,")
    m = _PLAINTIFF_RE.search(body_text)
    if m:
        out["plaintiff"] = m.group("name").strip(" .;,")
    return out


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

    parser = argparse.ArgumentParser(description="Test Franklin foreclosure scraper")
    parser.add_argument("--days", type=int, default=30, help="Look back N days")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_franklin_foreclosure.csv",
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
