"""Delaware County, Ohio TAX foreclosure scraper (JWorks eServices, authenticated).

Delaware's ANONYMOUS portal is gated by an image CAPTCHA that proved unsolvable
(0/31 solves across HTTP, Playwright, and image-preprocessing — see git history).
The working path is a court-authorized free PUBLIC eServices account
(`DELAWARE_ESERVICES_USERNAME` / `DELAWARE_ESERVICES_PASSWORD`), which is NOT
CAPTCHA-gated. Validated end-to-end live 2026-06.

Driven via **Playwright** (not requests): the search results grid and the case
detail page are Wicket AJAX / stateful — raw HTTP returns empty shells, and the
case-number links must be CLICKED (a direct GET returns a ~758-byte error stub).

Volume + strategy
-----------------
Delaware is LOW VOLUME for tax foreclosures (~10/yr, measured over 365 days) and,
being an affluent county, the **Treasurer does NOT foreclose directly** — tax
foreclosures come almost entirely from tax-lien-certificate buyers (Tax Ease
Ohio) as PLAINTIFF. So we search by tax-authority / cert-holder company names and
keep ONLY cases where that company is the PLAINTIFF. Every emitted record is
`notice_type="tax_foreclosure"` by construction (no lis_pendens dual-emit here —
there's no clean foreclosure-only case-type filter for mortgage foreclosures, and
those aren't the goal for Delaware).

Recommended cadence: **WEEKLY**, not daily — one Playwright login+search per run
for ~1 new case/month doesn't belong in the daily critical path.

Portal flow (reverse-engineered + validated live 2026-06)
---------------------------------------------------------
  login.page    POST username / password / submitLink (form id46) — captcha-free
  search.page   form id ida7: companyName, fileDateRange:dateInputBegin / :dateInputEnd
                (MM/DD/YYYY), submitLink
  results       tr.roweven / tr.rowodd cells: [Pay, eFile, party_name, role,
                case#, file_date, status, case_type]; case# is a Wicket link that
                must be CLICKED (same-page nav to the detail).
  detail        party blocks div.roweven / div.rowodd, each with:
                  .ptyInfoLabel  = party name
                  .ptyType       = "- Plaintiff" / "- Defendant"
                  .ptyContactInfo = address (.addrLn1 street, then city/state/zip)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://court.co.delaware.oh.us/eservices/"
LOGIN_URL = BASE_URL + "login.page"
SEARCH_URL = BASE_URL + "search.page"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Plaintiff company-name searches that surface Delaware tax foreclosures. Tax
# Ease is the dominant (effectively only) source today; the rest are
# future-proofing for other cert buyers / direct Treasurer foreclosures. We keep
# only results where the searched company is the PLAINTIFF.
TAX_PLAINTIFF_SEARCHES = ["TAX EASE", "TREASURER", "WOODS COVE", "MTAG", "ATCF"]

# Defendants that are NOT the homeowner — government lien-holders, the HOA, the
# foreclosing tax authority itself, etc. Skipped when picking the homeowner.
GOVT_DEFENDANT_PATTERNS = [re.compile(p, re.I) for p in [
    r"\bTREASURER\b", r"\bAUDITOR\b", r"\bSTATE\s+OF\s+OHIO\b",
    r"\bATTORNEY\s+GENERAL\b", r"\bPROSECUTOR\b", r"\bUNITED\s+STATES\b",
    r"\bINTERNAL\s+REVENUE\b", r"\bIRS\b", r"\bDEPARTMENT\s+OF\b",
    r"\bBUREAU\s+OF\b", r"\bHOMEOWNERS\s+ASSOCIATION\b", r"\bCONDOMINIUM\b",
    r"\bTAX\s*EASE\b", r"\bWOODS\s+COVE\b",
]]
PLACEHOLDER_DEFENDANT_PATTERNS = [re.compile(p, re.I) for p in [
    r"^\s*JOHN\s+DOE", r"^\s*JANE\s+DOE", r"^\s*UNKNOWN\b",
]]

# Case-number shapes seen live: "26 CV E 05 0818", "25 CV E 09 1011".
CASE_NUMBER_RE = re.compile(r"\b\d{2}\s*CV\s*[A-Z]?\s*\d{2}\s*\d{3,5}\b")
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
ADDR_CSZ_RE = re.compile(r"([A-Za-z .'\-]+?)\s*,?\s*\b([A-Z]{2})\b\s+(\d{5}(?:-\d{4})?)")

DEFAULT_LOOKBACK_DAYS = 14


class Scraper(NoticeScraper):
    """Delaware County Common Pleas — TAX foreclosure case filings (JWorks).

    Emits only `notice_type="tax_foreclosure"` — every record is a case where a
    tax authority / cert-holder is the plaintiff.
    """

    county = "Delaware"
    notice_type = "tax_foreclosure"
    source_name = "Delaware County Common Pleas — Tax Foreclosures (JWorks eServices)"
    source_url = SEARCH_URL
    requires_account = True

    def required_credentials(self) -> list[str]:
        return ["DELAWARE_ESERVICES_USERNAME", "DELAWARE_ESERVICES_PASSWORD"]

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        if since_date is None:
            since_date = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        until_date = date.today()

        if not (config.DELAWARE_ESERVICES_USERNAME and config.DELAWARE_ESERVICES_PASSWORD):
            logger.warning(
                "Delaware tax foreclosure: DELAWARE_ESERVICES_USERNAME/PASSWORD "
                "not set — skipping (need the court-authorized eServices account). "
                "0 records."
            )
            return []

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            logger.error("playwright not installed: %s", e)
            return []

        records: list[NoticeData] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=USER_AGENT)
                page = await ctx.new_page()
                if not await self._login(page):
                    logger.error("Delaware tax foreclosure: login failed — 0 records.")
                    return []
                logger.info("Delaware eServices login OK.")

                seen: set[str] = set()
                for company in TAX_PLAINTIFF_SEARCHES:
                    cases = await self._search_plaintiff_cases(
                        page, company, since_date, until_date
                    )
                    logger.info(
                        "Delaware '%s' plaintiff cases in window: %d", company, len(cases)
                    )
                    for case in cases:
                        if case["case_number"] in seen:
                            continue
                        seen.add(case["case_number"])
                        rec = await self._fetch_and_parse_detail(page, case)
                        if rec is not None:
                            records.append(rec)
            finally:
                await browser.close()

        records.sort(key=lambda r: (r.date_added, r.source_url))
        logger.info("Delaware tax foreclosure scrape done — %d records", len(records))
        return records

    # ── Login ──────────────────────────────────────────────────────────
    async def _login(self, page) -> bool:
        try:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(1200)
            await page.fill("input[name=username]", config.DELAWARE_ESERVICES_USERNAME)
            await page.fill("input[name=password]", config.DELAWARE_ESERVICES_PASSWORD)
            await page.click("input[name=submitLink]")
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(1200)
            body = (await page.content()).lower()
            if "not yet been authorized" in body:
                logger.error(
                    "Delaware account exists but is NOT court-authorized yet "
                    "(contact the Clerk of Courts to authorize the Portal User)."
                )
                return False
            # "welcome" appears on the authenticated landing.
            return "welcome" in body or "search" in body
        except Exception as e:
            logger.debug("Login raised: %s", e)
            return False

    # ── Search ─────────────────────────────────────────────────────────
    async def _search_plaintiff_cases(
        self, page, company: str, since_date: date, until_date: date,
    ) -> list[dict]:
        """Run a companyName search and return cases where it is the PLAINTIFF.

        Each returned dict: {case_number, file_date (date), plaintiff (company)}.
        """
        try:
            await page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(1000)
            await page.fill("input[name=companyName]", company)
            await page.fill(
                'input[name="fileDateRange:dateInputBegin"]',
                since_date.strftime("%m/%d/%Y"),
            )
            await page.fill(
                'input[name="fileDateRange:dateInputEnd"]',
                until_date.strftime("%m/%d/%Y"),
            )
            await page.click("input[name=submitLink]")
            await page.wait_for_load_state("networkidle", timeout=40000)
            await page.wait_for_timeout(1800)
        except Exception as e:
            logger.debug("Search for %r raised: %s", company, e)
            return []

        rows = await page.eval_on_selector_all(
            "tr.roweven, tr.rowodd",
            "(trs)=>trs.map(tr=>[...tr.querySelectorAll('td')]"
            ".map(td=>td.textContent.trim()))",
        )
        out: list[dict] = []
        for cells in rows:
            upper = [c.upper() for c in cells]
            if "PLAINTIFF" not in upper:
                continue
            case_number = next(
                (c for c in cells if CASE_NUMBER_RE.search(c)), ""
            )
            if not case_number:
                continue
            file_date = None
            for c in cells:
                m = DATE_RE.search(c)
                if m:
                    try:
                        file_date = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                        break
                    except ValueError:
                        pass
            if file_date is None or not (since_date <= file_date <= until_date):
                continue
            out.append({
                "case_number": self._norm_case(case_number),
                "case_number_raw": case_number.strip(),
                "file_date": file_date,
                "plaintiff": company,
            })
        return out

    # ── Detail ─────────────────────────────────────────────────────────
    async def _fetch_and_parse_detail(self, page, case: dict) -> NoticeData | None:
        """Click into the case detail, parse homeowner + property, return a record."""
        case_raw = case["case_number_raw"]
        try:
            link = page.locator(
                "td.bookmarkablePageLinkPropertyColumnLink a", has_text=case_raw
            ).first
            if await link.count() == 0:
                link = page.get_by_role("link", name=case_raw).first
            await link.click()
            await page.wait_for_load_state("networkidle", timeout=40000)
            await page.wait_for_timeout(1500)
            html = await page.content()
            detail_url = page.url
        except Exception as e:
            logger.debug("Detail click for %s raised: %s", case_raw, e)
            return None
        finally:
            # Return to results so the next case's link is clickable.
            try:
                await page.go_back(wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(800)
            except Exception:
                pass

        plaintiff, homeowner, street, city, state, zipc = self._parse_detail(html)
        if not (plaintiff or homeowner):
            logger.debug("Detail parse empty for %s", case_raw)

        raw_text = (
            f"Court: Delaware County Court of Common Pleas\n"
            f"Case Number: {case['case_number_raw']}\n"
            f"Case Type: Tax Foreclosure\n"
            f"Date Filed: {case['file_date'].isoformat()}\n"
            f"Plaintiff: {plaintiff or case['plaintiff']}\n"
            f"Homeowner (Defendant): {homeowner}\n"
            f"Property Address: {street}, {city}, {state} {zipc}\n"
        )
        return NoticeData(
            date_added=case["file_date"].isoformat(),
            auction_date="",
            county=self.county,
            state="OH",
            notice_type="tax_foreclosure",
            source_url=detail_url,
            address=street,
            city=city,
            zip=zipc,
            owner_name=homeowner,
            raw_text=raw_text,
        )

    def _parse_detail(self, html: str):
        """Return (plaintiff, homeowner, street, city, state, zip) from a detail page."""
        soup = BeautifulSoup(html, "html.parser")
        parties = []
        for block in soup.select("div.roweven, div.rowodd"):
            label = block.select_one(".ptyInfoLabel")
            rt = block.select_one(".ptyType")
            if not label or not rt:
                continue
            name = self._clean(label.get_text(" ", strip=True))
            role = rt.get_text(" ", strip=True).lstrip("-").strip().upper()
            ci = block.select_one(".ptyContactInfo")
            street, city, state, zipc = self._parse_party_address(ci)
            parties.append({
                "name": name, "role": role,
                "street": street, "city": city, "state": state, "zip": zipc,
            })

        plaintiff = next(
            (p["name"] for p in parties if "PLAINTIFF" in p["role"]), ""
        )
        # Homeowner = first DEFENDANT that isn't a govt lien-holder / placeholder.
        homeowner_party = None
        for p in parties:
            if "DEFENDANT" not in p["role"]:
                continue
            if any(rx.search(p["name"]) for rx in GOVT_DEFENDANT_PATTERNS):
                continue
            if any(rx.search(p["name"]) for rx in PLACEHOLDER_DEFENDANT_PATTERNS):
                continue
            homeowner_party = p
            break
        if homeowner_party is None:
            homeowner_party = next(
                (p for p in parties if "DEFENDANT" in p["role"]), None
            )

        if homeowner_party is None:
            return plaintiff, "", "", "", "", ""
        return (
            plaintiff,
            homeowner_party["name"],
            homeowner_party["street"],
            homeowner_party["city"],
            homeowner_party["state"],
            homeowner_party["zip"],
        )

    def _parse_party_address(self, ci) -> tuple[str, str, str, str]:
        """Split a .ptyContactInfo block into (street, city, state, zip)."""
        if ci is None:
            return "", "", "", ""
        a1 = ci.select_one(".addrLn1")
        street = self._clean(a1.get_text(" ", strip=True)) if a1 else ""
        full = ci.get_text(" ", strip=True)
        rest = full[len(a1.get_text(" ", strip=True)):] if a1 else full
        m = ADDR_CSZ_RE.search(rest)
        if m:
            return street, self._clean(m.group(1)), m.group(2), m.group(3)
        return street, "", "", ""

    # ── Helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _norm_case(s: str) -> str:
        return re.sub(r"\s+", "", s or "").upper()

    @staticmethod
    def _clean(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().strip(",").strip()


# ── Standalone test harness ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import asyncio
    import csv
    from dataclasses import asdict
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Test Delaware tax foreclosure scraper")
    parser.add_argument("--days", type=int, default=365,
                        help="Look back N days (default 365 — Delaware is low-volume)")
    parser.add_argument("--output", default="output/test_oh_delaware_tax_foreclosure.csv")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))
    print(f"Scraped {len(records)} Delaware tax foreclosure records since {since}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        if records:
            w = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            w.writeheader()
            for r in records:
                w.writerow(asdict(r))
        else:
            f.write("")
    print(f"Wrote {out}")
