"""Delaware County, Ohio TAX foreclosure case-filing scraper (JWorks/Equivant).

Pulls Court of Common Pleas FORECLOSURE case filings from Delaware County's
Equivant/JWorks "eServices" portal and classifies each into:
  * tax_foreclosure  — plaintiff is the County Treasurer / a tax-lien-certificate
                       holder (ORC 5721.18 et seq.)
  * lis_pendens      — plaintiff is a mortgage lender (ordinary foreclosure)

The case FILING is the earliest public signal — weeks to months before the
property reaches the RealAuction sheriff sale captured by
`scrapers.oh_delaware_foreclosure`. Same intent as the Franklin Common Pleas
scraper (`scrapers.oh_franklin_lis_pendens`), but Delaware runs a completely
different platform (JWorks, not WebSphere CIO), so the transport differs.

──────────────────────────────────────────────────────────────────────────
PORTAL FACTS (reverse-engineered live, 2026-06-18 — all confirmed working)
──────────────────────────────────────────────────────────────────────────
Base:     https://court.co.delaware.oh.us/eservices/
WAF:      Barracuda (sets BNES_/BNIS_ cookies on first GET — a plain
          requests.Session carries them automatically; no special handling).

STEP 1 — Apache Wicket BrowserInfoPage handshake  [IMPLEMENTED + TESTED]
    GET  home.page  →  returns a BrowserInfoPage: a hidden <form id="id1"
    method=post action=";jsessionid=...?x=<token>"> plus a 0-second
    <meta refresh>. The form collects JS-populated browser properties.
    We POST it with realistic values. Confirmed: the POST lands on
    `home.page.2` (the real public portal, ~22KB) and seeds the session.
    Hidden/posted fields (exact names, confirmed live):
      id1_hf_0, navigatorAppName, navigatorAppVersion, navigatorAppCodeName,
      navigatorCookieEnabled, navigatorJavaEnabled, navigatorLanguage,
      navigatorPlatform, navigatorUserAgent, screenWidth, screenHeight,
      screenColorDepth, utcOffset, utcDSTOffset, browserWidth, browserHeight,
      hostname.

STEP 2 — Anonymous CAPTCHA gate  [DEAD END — do not use]
    The anonymous public portal renders a Wicket image CAPTCHA (form id27,
    answer field `captchaPanel:challengePassword`, submit `linkFrag:beginButton`,
    image `img.captchaImg`). Code to fetch+solve it via 2Captcha is present and
    structurally correct, BUT the image itself is a faint, washed-out ~2-3 char
    glyph (126x60px) that 2Captcha's human solvers CANNOT reliably read. Verified
    2026-06-18 over raw HTTP and headless Playwright (element-screenshot capture,
    so the bound image was sent): ~15 solves, all wrong 2-char answers, gate
    never cleared. This path is abandoned — see the BYPASS below.

    BYPASS (recommended) — FREE PUBLIC eServices ACCOUNT:
    Equivant/JWorks does not CAPTCHA-gate AUTHENTICATED users on each search.
    Register a free public account at `register.page?prtlCd=PUBLIC`, then log in
    via `login.page` to get a captcha-free search session — same pattern as the
    RealAuction free account. Needs DELAWARE_ESERVICES_USERNAME/PASSWORD in
    config/.env. This is the activation path; `_SEARCH_READY` gates it.

STEP 3 — Case Search submission + result paging  [SKELETON — LIVE FINISH]
    Delaware's public portal exposes a "Smart Search" / Case Search by name and
    by case number. The search form is a STATEFUL Wicket form: its <form action>
    and field component-paths (e.g. "searchPanel:...:caseType") are only visible
    on the post-CAPTCHA page, and Wicket re-issues them per page version — so
    they MUST be read from a live solved session, not hard-coded blind. The
    `_run_case_search` / `_parse_results` / `_parse_detail` methods below mark
    exactly where those live values plug in. Everything up to and including the
    CAPTCHA solve is real; this last mile is the ~2-3h live-iteration finish.

REUSE NOTE: Steps 1-2 are platform-generic JWorks/Equivant. When Delaware
probate is activated, hoist `_browserinfo_handshake` + `_solve_captcha_gate`
into a `scrapers/jworks_base.py` and have both this module and
`oh_delaware_probate.py` subclass it (the SOP "refactor on the 3rd instance"
trigger — Greene would be the 3rd).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData
from scrapers.base import NoticeScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://court.co.delaware.oh.us/eservices/"
LANDING_URL = urljoin(BASE_URL, "home.page")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Master switch — STEP 3 (search) is not implemented yet AND the anonymous
# CAPTCHA path is a dead end (see _scrape_sync guard). Keep False so the daily
# run does a cheap handshake-only health check and returns 0 without spending
# CAPTCHA credits. Flip to True once the PUBLIC-account login + search land.
_SEARCH_READY = False

# Tax-foreclosure plaintiff classifier. Kept in sync with the canonical set in
# scrapers.oh_franklin_lis_pendens.TAX_FORECLOSURE_PLAINTIFF_PATTERNS — if you
# add a Delaware-specific tax-cert servicer here, add it there too (and vice
# versa). When a 3rd county needs this, lift it into a shared module.
TAX_FORECLOSURE_PLAINTIFF_PATTERNS = [
    # ANY treasurer as PLAINTIFF = tax foreclosure (treasurer is only ever a
    # defendant in a mortgage foreclosure). Covers all caption orderings:
    # "<COUNTY> COUNTY TREASURER", "TREASURER FOR/OF <COUNTY> COUNTY".
    re.compile(r"\bTREASURER\b", re.I),
    re.compile(r"\bTAX\s*EASE\b", re.I),
    re.compile(r"\bWOODS\s+COVE\b", re.I),
    re.compile(r"\bMTAG\b", re.I),
    re.compile(r"\bATCF\b", re.I),
    re.compile(r"\bALTERNA\b", re.I),
    re.compile(r"\bTAX\s+CERTIFICATE\b", re.I),
    re.compile(r"\bTAX\s+LIEN\b", re.I),
]


@dataclass
class _CaseRow:
    """One foreclosure case parsed from the Delaware search results."""

    case_number: str
    filing_date: date | None
    plaintiff_name: str = ""
    defendant_name: str = ""
    property_street: str = ""
    property_city: str = ""
    property_zip: str = ""
    detail_url: str = ""
    raw_text: str = ""


class Scraper(NoticeScraper):
    """Delaware County Common Pleas — tax/mortgage foreclosure case filings.

    Primary notice_type is "tax_foreclosure" (the reason this module exists),
    but, like the Franklin scraper, one sweep emits BOTH tax_foreclosure and
    lis_pendens records — split per-record by plaintiff in `_to_notice_data`.
    """

    county = "Delaware"
    notice_type = "tax_foreclosure"
    source_name = "Delaware County Common Pleas — Foreclosure Cases (JWorks)"
    source_url = LANDING_URL
    requires_account = True  # needs CAPTCHA_API_KEY (2Captcha) to clear the gate

    def required_credentials(self) -> list[str]:
        return ["CAPTCHA_API_KEY"]

    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        if since_date is None:
            since_date = date.today() - timedelta(days=14)
        return await asyncio.to_thread(self._scrape_sync, since_date)

    # ── Orchestration ──────────────────────────────────────────────────
    def _scrape_sync(self, since_date: date) -> list[NoticeData]:
        until_date = date.today()
        session = self._build_session()

        # STEP 1 — handshake (works today; gates everything downstream).
        if not self._browserinfo_handshake(session):
            logger.error(
                "Delaware tax foreclosure: BrowserInfoPage handshake failed — "
                "portal may have changed. Returning 0."
            )
            return []
        logger.info("Delaware JWorks handshake OK — public portal session seeded.")

        # GUARD — do not attempt the anonymous CAPTCHA gate on the daily run.
        # Finding (2026-06-18, verified via raw HTTP AND headless Playwright with
        # element-screenshot capture): the anonymous-access CAPTCHA is a faint,
        # washed-out ~2-3 char image (126x60px) that 2Captcha's human solvers
        # cannot reliably read — ~15 solves across both transports all returned
        # wrong 2-char answers; the gate never cleared. Retrying just burns
        # CAPTCHA credits every run. The correct path is the FREE PUBLIC eServices
        # account (register.page?prtlCd=PUBLIC): authenticated users are NOT
        # CAPTCHA-gated per search (same model as the RealAuction free account).
        # Activation plan when credentials exist:
        #   1. Add DELAWARE_ESERVICES_USERNAME/PASSWORD to config + .env.
        #   2. Replace `_solve_captcha_gate` with `_login_public_account` (POST
        #      login.page form), which yields a captcha-free search session.
        #   3. Implement `_run_case_search` (STEP 3 below).
        # Flip _SEARCH_READY to True once 2+3 are done.
        if not _SEARCH_READY:
            logger.warning(
                "Delaware tax foreclosure: handshake OK but search not yet "
                "enabled. Anonymous CAPTCHA is unsolvable; needs a free PUBLIC "
                "eServices account. Returning 0 (no CAPTCHA spend). See module "
                "docstring + _scrape_sync guard for the activation plan."
            )
            return []

        # STEP 2 — CAPTCHA gate. No key → behave as an honest scaffold: we
        # proved the portal is reachable and the handshake works, but we can't
        # search. Surface that loudly instead of silently returning [].
        if not config.CAPTCHA_API_KEY:
            logger.warning(
                "Delaware tax foreclosure: CAPTCHA_API_KEY not set — cannot pass "
                "the image-CAPTCHA gate. Handshake verified; 0 records. Add "
                "CAPTCHA_API_KEY=<2captcha key> to .env to enable searching."
            )
            return []

        solver = self._load_solver()
        if solver is None:
            return []

        if not self._solve_captcha_gate(session, solver):
            logger.error("Delaware tax foreclosure: could not clear CAPTCHA gate. 0 records.")
            return []

        # STEP 3 — search + parse (live-finish; see method docstrings).
        rows = self._run_case_search(session, since_date, until_date)
        records = [self._to_notice_data(r) for r in rows]
        records.sort(key=lambda r: (r.date_added, r.source_url))

        n_tax = sum(1 for r in records if r.notice_type == "tax_foreclosure")
        logger.info(
            "Delaware Common Pleas scrape done — %d records (%d tax_foreclosure, "
            "%d lis_pendens)",
            len(records), n_tax, len(records) - n_tax,
        )
        return records

    # ── HTTP plumbing ──────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        return s

    def _sleep(self) -> None:
        time.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

    # ── STEP 1: BrowserInfoPage handshake (confirmed working) ──────────
    def _browserinfo_handshake(self, session: requests.Session) -> bool:
        """GET landing, POST the Wicket BrowserInfoPage form, land on the portal.

        Returns True if the post-handshake page looks like the real public
        portal (contains the search UI + captcha chrome).
        """
        try:
            r = session.get(LANDING_URL, timeout=30)
            if r.status_code != 200 or "wicket" not in r.text.lower():
                logger.debug("Landing GET → %d (no wicket marker)", r.status_code)
                return False

            action = self._extract_form_action(r.text, form_id="id1")
            if not action:
                logger.debug("BrowserInfoPage: no postback form action found.")
                return False

            self._sleep()
            post_url = urljoin(LANDING_URL, action)
            r2 = session.post(post_url, data=self._browserinfo_payload(), timeout=30,
                              headers={"Referer": LANDING_URL})
            if r2.status_code != 200:
                logger.debug("BrowserInfo POST → %d", r2.status_code)
                return False
            # Real portal exposes the captcha panel + case-search chrome.
            body = r2.text.lower()
            return "captcha" in body and ("search" in body or "case" in body)
        except requests.RequestException as e:
            logger.debug("Handshake raised: %s", e)
            return False

    @staticmethod
    def _browserinfo_payload() -> dict:
        """Realistic browser-info values (field names confirmed live)."""
        return {
            "id1_hf_0": "",
            "navigatorAppName": "Netscape",
            "navigatorAppVersion": "5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "navigatorAppCodeName": "Mozilla",
            "navigatorCookieEnabled": "true",
            "navigatorJavaEnabled": "false",
            "navigatorLanguage": "en-US",
            "navigatorPlatform": "MacIntel",
            "navigatorUserAgent": USER_AGENT,
            "screenWidth": "1920",
            "screenHeight": "1080",
            "screenColorDepth": "24",
            "utcOffset": "-5",
            "utcDSTOffset": "-4",
            "browserWidth": "1280",
            "browserHeight": "900",
            "hostname": "court.co.delaware.oh.us",
        }

    @staticmethod
    def _extract_form_action(html: str, form_id: str) -> str | None:
        m = re.search(
            rf'<form[^>]*id="{re.escape(form_id)}"[^>]*action="([^"]+)"',
            html, re.I,
        )
        return m.group(1) if m else None

    # ── STEP 2: CAPTCHA gate ───────────────────────────────────────────
    def _load_solver(self):
        try:
            from twocaptcha import TwoCaptcha
        except ImportError as e:
            logger.error("twocaptcha-python not installed: %s", e)
            return None
        return TwoCaptcha(config.CAPTCHA_API_KEY)

    def _solve_captcha_gate(self, session: requests.Session, solver) -> bool:
        """Fetch the captchaImg, solve via 2Captcha, submit challengePassword.

        Confirmed element facts (home.page.2):
          image:  <img class="captchaImg" src="?x=<token>&wicket...">
          answer field name: "captchaPanel:challengePassword"
        The submit target is the enclosing Wicket form's action — read live from
        the same page (we locate it relative to the captcha panel).
        """
        try:
            page = session.get(urljoin(BASE_URL, "home.page.2"), timeout=30)
            soup = BeautifulSoup(page.text, "html.parser")
            img = soup.find("img", class_="captchaImg")
            if not img or not img.get("src"):
                logger.debug("No captchaImg on portal page.")
                return False
            img_url = urljoin(page.url, img["src"])

            self._sleep()
            img_resp = session.get(img_url, timeout=30, headers={"Referer": page.url})
            if img_resp.status_code != 200 or not img_resp.content:
                logger.debug("CAPTCHA image fetch failed (%d).", img_resp.status_code)
                return False

            code = self._solve_captcha(solver, img_resp.content)
            if not code:
                logger.debug("CAPTCHA solve returned empty.")
                return False

            # Submit the answer on the captcha form. The form action + any extra
            # hidden Wicket fields live on this same page; collect them generically.
            form = img.find_parent("form")
            action = form.get("action") if form else None
            if not action:
                logger.debug("CAPTCHA panel has no enclosing form action.")
                return False
            payload = self._collect_hidden_fields(form)
            payload["captchaPanel:challengePassword"] = code

            self._sleep()
            resp = session.post(urljoin(page.url, action), data=payload, timeout=30,
                                headers={"Referer": page.url})
            ok = resp.status_code == 200 and "incorrect" not in resp.text.lower()
            if not ok:
                logger.debug("CAPTCHA submission rejected.")
            return ok
        except requests.RequestException as e:
            logger.debug("CAPTCHA gate raised: %s", e)
            return False

    def _solve_captcha(self, solver, image_bytes: bytes) -> str:
        """Submit CAPTCHA image to 2Captcha; return text or '' on failure.

        Mirrors scrapers.oh_clark_probate._solve_captcha.
        """
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

    @staticmethod
    def _collect_hidden_fields(form) -> dict:
        """Gather all <input> name/value pairs from a Wicket form (hidden + text)."""
        out: dict[str, str] = {}
        if not form:
            return out
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                out[name] = inp.get("value", "")
        return out

    # ── STEP 3: Case search + parse (LIVE FINISH) ──────────────────────
    def _run_case_search(
        self, session: requests.Session, since_date: date, until_date: date,
    ) -> list[_CaseRow]:
        """Submit the Case Search form filtered to FORECLOSURE cases in window.

        LIVE FINISH — the post-CAPTCHA page exposes a stateful Wicket Case Search
        form whose component-paths must be read from that live page (they are not
        stable enough to hard-code blind). To complete:
          1. GET the search page; locate the search <form> + its action.
          2. Identify the case-type/category select and choose the FORECLOSURE
             option, set the filed-date-from / filed-date-to fields to
             since_date / until_date, submit.
          3. Page through results into _CaseRow objects via `_parse_results`.
          4. For each, follow the detail link and fill plaintiff/defendant/
             property address via `_parse_detail` (defendant address = property).
        Until implemented, returns [] so the daily run stays green.
        """
        logger.warning(
            "Delaware tax foreclosure: CAPTCHA cleared but Case Search submission "
            "is the remaining live-finish step (Wicket form paths). Returning 0. "
            "See _run_case_search docstring for the 4-step completion plan."
        )
        return []

    def _parse_results(self, html: str) -> list[_CaseRow]:
        """Parse the JWorks results grid into _CaseRow stubs. (LIVE FINISH)"""
        return []

    def _parse_detail(self, html: str, row: _CaseRow) -> _CaseRow:
        """Fill plaintiff/defendant/property from a case detail page. (LIVE FINISH)"""
        return row

    # ── Classification + conversion ────────────────────────────────────
    @staticmethod
    def _classify_notice_type(plaintiff_name: str) -> str:
        name = plaintiff_name or ""
        if any(p.search(name) for p in TAX_FORECLOSURE_PLAINTIFF_PATTERNS):
            return "tax_foreclosure"
        return "lis_pendens"

    def _to_notice_data(self, row: _CaseRow) -> NoticeData:
        return NoticeData(
            date_added=row.filing_date.isoformat() if row.filing_date else "",
            auction_date="",  # filing stage — no sheriff sale scheduled yet
            county=self.county,
            state="OH",
            notice_type=self._classify_notice_type(row.plaintiff_name),
            source_url=row.detail_url or LANDING_URL,
            address=row.property_street,
            city=row.property_city,
            zip=row.property_zip,
            owner_name=row.defendant_name,
            raw_text=row.raw_text,
        )


# ── Standalone test harness ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test Delaware tax foreclosure scraper")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--handshake-test", action="store_true",
                        help="Only exercise the BrowserInfoPage handshake (no CAPTCHA needed)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    scraper = Scraper()

    if args.handshake_test:
        sess = scraper._build_session()
        ok = scraper._browserinfo_handshake(sess)
        print(f"Handshake {'OK — portal session seeded' if ok else 'FAILED'}")
        raise SystemExit(0 if ok else 1)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(scraper.scrape(since_date=since))
    print(f"Scraped {len(records)} Delaware foreclosure records since {since}")
