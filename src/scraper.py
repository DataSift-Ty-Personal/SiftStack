"""Core scraping logic — login, navigate saved searches, paginate results."""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

from captcha_solver import solve_captcha_and_view
import config
from config import (
    BASE_URL,
    COOKIES_FILE,
    LOGIN_URL,
    MAX_RETRIES,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    RESULTS_PER_PAGE,
    SAVED_SEARCHES,
    SEEN_IDS_FILE,
    SEEN_IDS_PRUNE_DAYS,
    CAPTCHA_FAILED_IDS_FILE,
    CAPTCHA_FAILED_PRUNE_DAYS,
    SMART_SEARCH_URL,
    STATE_FILE,
    SavedSearch,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_NEXT_PAGE_BUTTON,
    SEL_PAGE_INFO,
    SEL_PER_PAGE_DROPDOWN,
    SEL_SAVED_SEARCHES_DROPDOWN,
    SEL_VIEW_BUTTON_PATTERN,
)
from data_formatter import _notice_id_from_url
from foreclosure_filter import is_valid_foreclosure
from notice_parser import NoticeData, is_target_county, parse_notice_page

logger = logging.getLogger(__name__)


async def delay() -> None:
    """Random delay between requests to avoid detection."""
    wait = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(wait)


# ── Scrapfly detail backend ───────────────────────────────────────────
# When config.SCRAPE_BACKEND == "scrapfly", the gated notice detail fetch
# (anti-bot, reCAPTCHA, and the proof-of-source screenshot) runs through the
# Scrapfly API instead of Playwright + 2Captcha. Playwright still drives the
# saved-search navigation and supplies each notice ID; on any Scrapfly failure
# the code falls back to the in-house 2Captcha path, so the swap is safe.

_scrapfly_client = None
_scrapfly_login_ok: bool | None = None
_SCRAPFLY_SESSION = "tnpn-scrape"


def _get_scrapfly_client():
    """Lazily create + log in a Scrapfly client (cached). Returns None on failure."""
    global _scrapfly_client, _scrapfly_login_ok
    if _scrapfly_login_ok is False:
        return None  # already tried and failed this run; don't hammer login
    if _scrapfly_client is None:
        try:
            from scrapfly_client import ScrapflyNoticeClient
            _scrapfly_client = ScrapflyNoticeClient()
        except Exception:
            logger.error("Scrapfly client init failed; using Playwright path", exc_info=True)
            _scrapfly_login_ok = False
            return None
    if _scrapfly_login_ok is None:
        _scrapfly_login_ok = _scrapfly_client.login(session=_SCRAPFLY_SESSION)
        if not _scrapfly_login_ok:
            logger.error("Scrapfly login failed; using Playwright path")
            return None
    return _scrapfly_client


async def _scrapfly_notice(notice_id: str, source_url: str, search, llm_api_key):
    """Fetch + parse one notice via Scrapfly (content + screenshot).

    Returns a NoticeData on success, or None to signal the caller should fall
    back to the Playwright + 2Captcha path.
    """
    client = _get_scrapfly_client()
    if client is None:
        return None
    want_shot = (
        config.CAPTURE_NOTICE_SCREENSHOTS
        and search.notice_type in config.NOTICE_SCREENSHOT_TYPES
    )
    try:
        res = client.fetch_notice(
            notice_id or source_url, session=_SCRAPFLY_SESSION, want_screenshot=want_shot,
        )
    except Exception:
        logger.warning("  Scrapfly fetch raised for ID=%s", notice_id, exc_info=True)
        return None
    if not res.ok:
        logger.warning("  Scrapfly fetch not ok for ID=%s: %s", notice_id, res.error)
        return None

    from notice_parser import parse_notice_html
    notice = await parse_notice_html(
        res.content_html, search.county, search.notice_type, source_url, llm_api_key,
    )
    if res.screenshot_bytes:
        try:
            from notice_screenshot import _screenshot_filename
            out_dir = config.NOTICE_SCREENSHOT_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            shot_path = out_dir / _screenshot_filename(notice_id, notice.address)
            shot_path.write_bytes(res.screenshot_bytes)
            notice.notice_screenshot_path = str(shot_path)
        except Exception:
            logger.debug("  Saving Scrapfly screenshot failed", exc_info=True)
    return notice


# ── Login ─────────────────────────────────────────────────────────────


async def login(page: Page, _retries: int = 3) -> bool:
    """Log in to tnpublicnotice.com Smart Search. Returns True on success.

    Retries up to ``_retries`` times on transient network errors (e.g. after
    Apify container migration).
    """
    for attempt in range(1, _retries + 1):
        try:
            logger.info("Logging in to %s (attempt %d/%d)", LOGIN_URL, attempt, _retries)
            await page.goto(LOGIN_URL)
            await page.wait_for_load_state("networkidle")
            break  # page loaded successfully
        except Exception as exc:
            logger.warning("Login navigation failed (attempt %d/%d): %s", attempt, _retries, exc)
            if attempt < _retries:
                await asyncio.sleep(5 * attempt)  # back off 5s, 10s
                continue
            logger.error("Login navigation failed after %d attempts — giving up", _retries)
            return False

    # No CAPTCHA on the login page (confirmed via research)
    await page.fill(SEL_LOGIN_EMAIL, config.TNPN_EMAIL)
    await page.fill(SEL_LOGIN_PASSWORD, config.TNPN_PASSWORD)
    await page.click(SEL_LOGIN_SUBMIT)
    await page.wait_for_load_state("networkidle")
    await delay()

    # Successful login redirects to /Smartsearch/Default.aspx
    if "smartsearch" in page.url.lower():
        logger.info("Login successful — on Smart Search dashboard")
        return True

    # Check for error message
    error = await page.query_selector(".error, .validation-summary-errors")
    if error:
        msg = await error.inner_text()
        logger.error("Login failed: %s", msg.strip())
    else:
        logger.error("Login failed — landed on %s", page.url)
    return False


# ── Saved Search Execution ────────────────────────────────────────────


def _get_session_base(page_url: str) -> str:
    """Extract the session-aware base URL from the current page URL.

    ASP.NET embeds session IDs in URL paths: /(S({guid}))/
    Returns the base URL including the session path segment.
    """
    m = re.search(r"(https?://[^/]+/\(S\([^)]+\)\)/)", page_url)
    if m:
        return m.group(1)
    return BASE_URL + "/"


async def _navigate_to_dashboard(page: Page) -> bool:
    """Ensure we're on the Smart Search dashboard.

    Returns True on success, False if session is dead and re-login is needed.
    """
    if "smartsearch/default" not in page.url.lower():
        session_base = _get_session_base(page.url)
        dashboard_url = session_base + "Smartsearch/Default.aspx"
        logger.info("Navigating to Smart Search dashboard: %s", dashboard_url)
        try:
            await page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PwTimeout:
            logger.warning("Dashboard navigation timed out")
            return False
        except Exception:
            logger.warning("Dashboard navigation failed", exc_info=True)
            return False
        await delay()

    # Session expired → ASP.NET redirected to authenticate page
    if "authenticate" in page.url.lower():
        logger.warning("Session expired — redirected to login page")
        return False

    dropdown = await page.query_selector(SEL_SAVED_SEARCHES_DROPDOWN)
    if not dropdown:
        logger.error("Saved Searches dropdown not found on dashboard")
        return False
    return True


async def _set_per_page(page: Page) -> None:
    """Set the results-per-page dropdown to max (50) if present."""
    dropdown = await page.query_selector(SEL_PER_PAGE_DROPDOWN)
    if dropdown:
        current = await dropdown.input_value()
        if current != str(RESULTS_PER_PAGE):
            logger.info("Setting results per page to %d", RESULTS_PER_PAGE)
            await page.select_option(SEL_PER_PAGE_DROPDOWN, str(RESULTS_PER_PAGE))
            await page.wait_for_load_state("networkidle")
            await delay()
            await delay()  # extra wait — ASP.NET DOM rebuild after postback


async def _get_page_info(page: Page) -> tuple[int, int]:
    """Parse 'Page X of Y Pages' text. Returns (current_page, total_pages)."""
    try:
        info_el = await page.query_selector(SEL_PAGE_INFO)
        if info_el:
            text = await info_el.inner_text()
            # "Page 1 of 100 Pages"
            import re
            m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1, 1


async def _extract_published_date(row_text: str) -> str:
    """Pull the 'Published: M/D/YYYY' date from a result row's text."""
    import re
    m = re.search(r"Published:\s*(\d{1,2}/\d{1,2}/\d{4})", row_text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return m.group(1)
    return ""


async def run_saved_search(
    page: Page,
    search: SavedSearch,
    since_date: str | None = None,
    llm_api_key: str | None = None,
    on_page_batch=None,
    start_page: int = 1,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,
    target_ids: set[str] | None = None,
) -> list[NoticeData]:
    """Select a saved search from the dropdown, paginate, and scrape each notice.

    When ``target_ids`` is provided (screenshot backfill), only notices whose ID
    is in the set are opened/solved; all others are skipped before the CAPTCHA,
    and the cross-run seen-ID skip is bypassed so already-seen targets re-process.

    Args:
        on_page_batch: Optional async callback(list[NoticeData]) called after each page
                       to push results incrementally.
        start_page: Page number to start scraping from (default 1). Use this to
                    resume a previous run without re-scraping earlier pages.

    Returns list of parsed and filtered NoticeData.
    """
    logger.info("Running saved search: %s", search.saved_search_name)

    # Navigate to dashboard and select the saved search from dropdown
    if not await _navigate_to_dashboard(page):
        # Try re-login once and retry
        if await _try_relogin(page) and await _navigate_to_dashboard(page):
            pass  # recovered — continue below
        else:
            return []

    # Selecting from the dropdown triggers an ASP.NET postback → full page navigation.
    # Must wait for navigation explicitly or the execution context gets destroyed.
    try:
        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(
                SEL_SAVED_SEARCHES_DROPDOWN,
                label=search.saved_search_name,
            )
    except Exception:
        logger.error("Could not select '%s' from dropdown", search.saved_search_name)
        return []

    await delay()

    # Verify we're on search results
    if "search" not in page.url.lower():
        logger.error("Expected Search.aspx but got %s", page.url)
        return []

    # Maximize results per page
    await _set_per_page(page)

    # Scrape all pages
    notices: list[NoticeData] = []
    current_page, total_pages = await _get_page_info(page)
    logger.info("  %d pages of results for %s", total_pages, search.saved_search_name)

    # Skip ahead to start_page if needed
    if start_page > 1:
        logger.info("  Skipping to page %d (start_page)", start_page)
        while current_page < start_page:
            next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
            if not next_btn:
                logger.error("  Cannot reach page %d — no next button at page %d", start_page, current_page)
                return []
            await next_btn.click()
            await page.wait_for_load_state("load")
            await delay()
            current_page, total_pages = await _get_page_info(page)
        logger.info("  Reached page %d/%d", current_page, total_pages)

    while True:
        logger.info("  Scraping page %d/%d", current_page, total_pages)
        page_notices = await _scrape_results_page(
            page, search, since_date, llm_api_key, seen_ids, captcha_failed_ids,
            target_ids=target_ids,
        )
        notices.extend(page_notices)

        # Push this page's results immediately so they survive timeouts
        if on_page_batch and page_notices:
            await on_page_batch(page_notices)

        # Stop early if we've hit the max_notices limit
        if max_notices and len(notices) >= max_notices:
            logger.info("  Reached max_notices limit (%d) — stopping", max_notices)
            notices = notices[:max_notices]
            break

        # Check if there's a next page
        if current_page >= total_pages:
            break

        next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
        can_advance = next_btn and not await next_btn.get_attribute("disabled") if next_btn else False

        if can_advance:
            await next_btn.click()
            await page.wait_for_load_state("load")
            await delay()
            await delay()
            current_page, total_pages = await _get_page_info(page)
        else:
            # Grid lost or next button missing — attempt recovery to next page
            if current_page < total_pages:
                logger.warning(
                    "  Grid lost on page %d/%d — attempting recovery",
                    current_page, total_pages,
                )
                recovered = await _recover_to_search_page(
                    page, search, current_page + 1,
                )
                if recovered:
                    current_page, total_pages = await _get_page_info(page)
                    continue
                logger.error("  Recovery failed — stopping after page %d", current_page)
            break

    logger.info("  Found %d notices for %s", len(notices), search.saved_search_name)
    return notices


# ── Per-Page Scraping ─────────────────────────────────────────────────


def _address_matches(target: str, addr: str) -> bool:
    """True if the target address keyword (house# + street) appears in addr."""
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower()).strip()
    t = norm(target)
    return bool(t) and t in norm(addr)


async def _scrape_results_page(
    page: Page,
    search: SavedSearch,
    since_date: str | None,
    llm_api_key: str | None = None,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,
    target_ids: set[str] | None = None,
    match_address: str | None = None,
    stop_after: int = 0,
) -> list[NoticeData]:
    """Click each View button on a results page, solve CAPTCHA, parse notice.

    match_address (backfill): only capture notices whose property address contains
    this keyword (robust to republished/changed notice IDs). stop_after: return as
    soon as this many notices are captured on the page.
    """
    notices: list[NoticeData] = []

    # Wait for view buttons to be stable in the DOM before interacting.
    # SPA hydration over residential proxies can be slow — try 30s, then one
    # recovery attempt (networkidle + re-query) before giving up. A silent
    # empty return here is what caused the 2026-04-15 Blount miss.
    try:
        await page.wait_for_selector(SEL_VIEW_BUTTON_PATTERN, state="attached", timeout=30_000)
    except PwTimeout:
        logger.warning(
            "  No view buttons for %s after 30s — waiting for networkidle and retrying",
            search.saved_search_name,
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PwTimeout:
            pass
        try:
            await page.wait_for_selector(SEL_VIEW_BUTTON_PATTERN, state="attached", timeout=15_000)
        except PwTimeout:
            logger.warning(
                "  %s returned zero results after retry — check site manually "
                "(saved search may have legitimate hits that didn't render)",
                search.saved_search_name,
            )
            return notices

    # Find all View buttons in the results grid
    view_buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
    num_results = len(view_buttons)
    logger.info("  %d results on this page", num_results)

    if num_results == 0:
        logger.warning(
            "  %s: selector matched but 0 buttons returned — treating as empty page",
            search.saved_search_name,
        )
        return notices

    # We need to iterate by index because clicking a view button navigates away.
    # After parsing each notice, we navigate back and re-find the buttons.
    grid_lost = False
    for idx in range(num_results):
        if grid_lost:
            break
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Re-find all view buttons (DOM refreshes after back-navigation)
                view_buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
                if idx >= len(view_buttons):
                    logger.warning("  Button index %d out of range (%d buttons)", idx, len(view_buttons))
                    if len(view_buttons) == 0:
                        logger.warning("  Results grid lost — stopping this page")
                        grid_lost = True
                    break

                # Grab the row text for date and preview before navigating
                btn = view_buttons[idx]
                row = await btn.evaluate_handle("el => el.closest('tr').parentElement.closest('tr')")
                row_text = ""
                try:
                    row_text = await row.evaluate("el => el.innerText")
                except Exception:
                    pass

                # Check published date for daily mode cutoff
                pub_date = await _extract_published_date(row_text)
                if since_date and pub_date and pub_date < since_date:
                    logger.debug("  Skipping old notice (%s < %s)", pub_date, since_date)
                    break

                # Backfill: read the notice ID from the row and skip non-targets
                # WITHOUT clicking (the ID is in the row HTML), so we avoid slow
                # detail navigation for everything except the targets.
                if target_ids is not None:
                    row_html = ""
                    try:
                        row_html = await row.evaluate("el => el.innerHTML")
                    except Exception:
                        pass
                    m = re.search(r"ID=(\d+)", row_html)
                    if not m or m.group(1) not in target_ids:
                        break  # not a target; move to next result without clicking

                # Click the View button → navigates to Details.aspx.
                # domcontentloaded (not networkidle): the detail page is
                # server-rendered and networkidle often never settles through a
                # residential proxy, causing a 60s false timeout + grid loss.
                await btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await delay()

                notice_id = _notice_id_from_url(page.url)

                # Backfill targeting: only process the requested IDs; skip the
                # rest before the CAPTCHA so we don't pay to solve them.
                if target_ids is not None and notice_id and notice_id not in target_ids:
                    await page.go_back()
                    await page.wait_for_load_state("domcontentloaded")
                    await delay()
                    break  # next result

                # Cross-run dedup: skip notices seen in prior runs (no CAPTCHA).
                # Bypassed during backfill so already-seen targets re-process.
                if target_ids is None and seen_ids is not None and notice_id and notice_id in seen_ids:
                    logger.info("  Skipping already-processed notice ID=%s", notice_id)
                    await page.go_back()
                    await page.wait_for_load_state("domcontentloaded")
                    await delay()
                    break  # next result

                # ── Scrapfly backend: fetch content + screenshot via API ──
                if config.SCRAPE_BACKEND == "scrapfly":
                    sf_notice = await _scrapfly_notice(
                        notice_id, page.url, search, llm_api_key,
                    )
                    if sf_notice is not None:
                        if pub_date:
                            sf_notice.date_published = pub_date
                        if seen_ids is not None and notice_id:
                            seen_ids[notice_id] = sf_notice.date_published or datetime.now().strftime("%Y-%m-%d")
                        if not is_valid_foreclosure(sf_notice):
                            logger.debug("  Filtered out (not foreclosure): %s", sf_notice.source_url)
                        elif not is_target_county(sf_notice.raw_text, search.county):
                            logger.debug("  Filtered out (wrong county): %s", sf_notice.source_url)
                        else:
                            notices.append(sf_notice)
                            logger.debug("  Kept notice (scrapfly): %s", sf_notice.source_url)
                        await page.go_back()
                        await page.wait_for_load_state("domcontentloaded")
                        if "details" in page.url.lower():
                            await page.go_back()
                            await page.wait_for_load_state("domcontentloaded")
                        await delay()
                        break  # next result
                    # Scrapfly failed: fall through to the Playwright + 2Captcha path
                    logger.warning("  Scrapfly miss for ID=%s; falling back to 2Captcha", notice_id)

                # Check if notice content is already visible (CAPTCHA previously solved in session)
                content_visible = await page.query_selector("text='Notice Content'")
                if not content_visible:
                    # Need to solve CAPTCHA
                    if not await solve_captcha_and_view(page):
                        logger.warning("  CAPTCHA solve failed for result %d (attempt %d)", idx + 1, attempt)
                        # Track which IDs we lost to CAPTCHA failure so the next run
                        # can prioritize them and the end-of-run summary surfaces them.
                        # Record on the final scraper-level attempt, not intermediate retries.
                        if attempt >= MAX_RETRIES and captcha_failed_ids is not None and notice_id:
                            captcha_failed_ids[notice_id] = {
                                "url": page.url,
                                "search": search.saved_search_name,
                                "county": search.county,
                                "notice_type": search.notice_type,
                                "pub_date": pub_date or "",
                                "first_seen": datetime.now().strftime("%Y-%m-%d"),
                            }
                        # Navigate back and retry
                        await page.go_back()
                        await page.wait_for_load_state("domcontentloaded")
                        await delay()
                        continue

                # Ensure the notice body actually rendered before parsing/capturing.
                # Through a residential proxy the View-Notice postback content can
                # arrive after the solve returns (especially via the "gate cleared"
                # fallback), which would otherwise parse an empty/partial notice and
                # leave the sale date unhighlighted.
                try:
                    await page.wait_for_function(
                        "() => { const e = document.querySelector('#right_content');"
                        " return e && e.innerText && e.innerText.length > 400; }",
                        timeout=15000,
                    )
                except PwTimeout:
                    logger.warning("  Notice body slow to render for ID=%s; proceeding", notice_id)

                # Parse the now-visible notice text
                notice = await parse_notice_page(page, search.county, search.notice_type, llm_api_key)
                # The results-grid "Published:" date is the authoritative publication
                # date. date_added (when we added the record) is stamped later by the
                # enrichment pipeline with the actual run date.
                if pub_date:
                    notice.date_published = pub_date

                # Record this notice ID so future runs don't re-process it
                if seen_ids is not None and notice_id:
                    seen_ids[notice_id] = notice.date_published or datetime.now().strftime("%Y-%m-%d")

                # Apply foreclosure filter
                if not is_valid_foreclosure(notice):
                    logger.debug("  Filtered out (not foreclosure): %s", notice.source_url)
                # Apply county validation — reject notices where the property
                # is actually in a different county (search false positive)
                elif not is_target_county(notice.raw_text, search.county):
                    logger.debug("  Filtered out (wrong county): %s", notice.source_url)
                # Backfill address match: only capture the requested property
                elif match_address and not _address_matches(match_address, notice.address):
                    logger.debug("  Skipping non-matching address: %s", notice.address)
                else:
                    # Capture a proof-of-source screenshot of the live notice
                    # page before navigating back (foreclosures only by default).
                    # Best-effort: never let a screenshot failure drop a record.
                    if (config.CAPTURE_NOTICE_SCREENSHOTS
                            and notice.notice_type in config.NOTICE_SCREENSHOT_TYPES):
                        from notice_screenshot import capture_notice_screenshot
                        shot = await capture_notice_screenshot(
                            page, notice_id=notice_id, address=notice.address,
                            owner_name=notice.owner_name, auction_date=notice.auction_date,
                        )
                        if shot:
                            notice.notice_screenshot_path = str(shot)
                    notices.append(notice)
                    logger.debug("  Kept notice: %s", notice.source_url)

                # Navigate back to the results page
                await page.go_back()
                await page.wait_for_load_state("domcontentloaded")
                # Sometimes the back takes us to the CAPTCHA page, need another back
                if "details" in page.url.lower():
                    await page.go_back()
                    await page.wait_for_load_state("domcontentloaded")
                await delay()
                if stop_after and len(notices) >= stop_after:
                    return notices  # captured enough on this page; stop early
                break  # Success — next result

            except PwTimeout:
                logger.warning("  Timeout on result %d (attempt %d/%d)", idx + 1, attempt, MAX_RETRIES)
                # Try to recover by going back to results
                try:
                    await page.go_back()
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                await delay()

            except Exception:
                logger.exception("  Error on result %d (attempt %d/%d)", idx + 1, attempt, MAX_RETRIES)
                # Only go back if we actually navigated away from search results
                if "search" not in page.url.lower():
                    try:
                        await page.go_back()
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                await delay()

    return notices


# ── Session Persistence ───────────────────────────────────────────────


async def _save_cookies(context) -> None:
    """Save browser cookies to disk for session reuse."""
    try:
        cookies = await context.cookies()
        config.save_state(COOKIES_FILE, cookies)
        logger.debug("Saved %d cookies to %s", len(cookies), COOKIES_FILE)
    except Exception:
        logger.debug("Could not save cookies", exc_info=True)


async def _load_cookies(context) -> bool:
    """Load saved cookies into browser context. Returns True if loaded."""
    cookies = config.load_state(COOKIES_FILE)
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        logger.debug("Loaded %d cookies from %s", len(cookies), COOKIES_FILE)
        return True
    except Exception:
        logger.debug("Could not load cookies", exc_info=True)
        return False


async def _try_relogin(page: Page) -> bool:
    """Detect if session expired and attempt re-login. Returns True if re-login succeeded."""
    # Check if we're on the authenticate page or if dashboard nav fails
    is_dead = "authenticate" in page.url.lower()
    if not is_dead:
        # Quick check: try navigating to dashboard
        try:
            await page.goto(SMART_SEARCH_URL, wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            is_dead = True
        else:
            is_dead = "authenticate" in page.url.lower()

    if not is_dead:
        return False  # Session is fine, failure was something else

    logger.warning("Session expired — attempting re-login")
    if await login(page):
        logger.info("Re-login successful")
        return True

    logger.error("Re-login failed")
    return False


async def _recover_to_search_page(
    page: Page, search: SavedSearch, target_page: int,
) -> bool:
    """Recover from a lost results grid by re-logging in and navigating to target_page."""
    logger.warning("Attempting to recover search session (target page %d)", target_page)

    # Re-login if session expired
    if "authenticate" in page.url.lower() or not await _navigate_to_dashboard(page):
        if not await _try_relogin(page):
            logger.error("Cannot re-login — recovery failed")
            return False
        if not await _navigate_to_dashboard(page):
            return False

    # Re-select the saved search
    try:
        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(
                SEL_SAVED_SEARCHES_DROPDOWN,
                label=search.saved_search_name,
            )
    except Exception:
        logger.error("Could not re-select '%s' during recovery", search.saved_search_name)
        return False

    await delay()

    if "search" not in page.url.lower():
        return False

    await _set_per_page(page)

    # Navigate to target page by clicking "Next page" repeatedly
    current, total = await _get_page_info(page)
    while current < target_page:
        try:
            next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
            if not next_btn:
                logger.error("Next page button not found during recovery at page %d", current)
                return False
            await next_btn.click()
            await page.wait_for_load_state("load")
            await delay()
            await delay()
            current, total = await _get_page_info(page)
        except Exception:
            logger.warning("Recovery navigation failed at page %d", current, exc_info=True)
            return False

    logger.info("Recovery successful — now on page %d/%d", current, total)
    return True


async def _is_session_valid(page: Page) -> bool:
    """Check if saved cookies give us a valid logged-in session."""
    try:
        await page.goto(SMART_SEARCH_URL)
        await page.wait_for_load_state("networkidle")
        # If we land on the dashboard, session is valid
        if "smartsearch" in page.url.lower():
            dropdown = await page.query_selector(SEL_SAVED_SEARCHES_DROPDOWN)
            if dropdown:
                logger.info("Reusing saved session — already logged in")
                return True
    except Exception:
        pass
    return False


# ── State Tracking ────────────────────────────────────────────────────


def load_last_run_date() -> str | None:
    """Load the date of the last successful run from state file."""
    data = config.load_state(STATE_FILE)
    return data.get("last_run_date")


def save_last_run_date() -> None:
    """Save today's date as the last run date."""
    config.save_state(STATE_FILE, {"last_run_date": datetime.now().strftime("%Y-%m-%d")})


def load_seen_ids() -> dict[str, str]:
    """Load notice IDs already processed in prior runs, pruning entries older than SEEN_IDS_PRUNE_DAYS.

    Returns a dict of {notice_id: "YYYY-MM-DD"}. The date is when we first saw the
    notice, used only for pruning to bound file size.
    """
    data = config.load_state(SEEN_IDS_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=SEEN_IDS_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {nid: d for nid, d in data.items() if d >= cutoff}
    if len(pruned) < len(data):
        logger.info("Pruned %d seen IDs older than %d days", len(data) - len(pruned), SEEN_IDS_PRUNE_DAYS)
    return pruned


def save_seen_ids(seen: dict[str, str]) -> None:
    """Persist the seen-notice-ID cache to disk."""
    config.save_state(SEEN_IDS_FILE, seen)


def load_captcha_failed_ids() -> dict[str, dict]:
    """Load notices that exhausted CAPTCHA retries in prior runs.

    Pruned to CAPTCHA_FAILED_PRUNE_DAYS (default 14) — short window because
    most failures are transient proxy/2Captcha hiccups; if a notice is still
    failing after two weeks the site likely changed or the notice was removed.

    Structure: {notice_id: {url, search, county, notice_type, pub_date, first_seen}}.
    """
    data = config.load_state(CAPTCHA_FAILED_IDS_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=CAPTCHA_FAILED_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {
        nid: meta for nid, meta in data.items()
        if isinstance(meta, dict) and meta.get("first_seen", "") >= cutoff
    }
    if len(pruned) < len(data):
        logger.info(
            "Pruned %d CAPTCHA-failed IDs older than %d days",
            len(data) - len(pruned), CAPTCHA_FAILED_PRUNE_DAYS,
        )
    return pruned


def save_captcha_failed_ids(failed: dict[str, dict]) -> None:
    """Persist the CAPTCHA-failed-notice-ID cache to disk."""
    config.save_state(CAPTCHA_FAILED_IDS_FILE, failed)


# ── Main Entry Point ─────────────────────────────────────────────────


async def run_keyword_search(
    page: Page,
    keyword: str,
    llm_api_key: str | None = None,
    months: int = 12,
    target_ids: set[str] | None = None,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,
    match_address: str | None = None,
    stop_after: int = 0,
) -> list[NoticeData]:
    """Run an ad-hoc keyword search over a wide date window and scrape matches.

    Used to backfill screenshots for notices that rolled off the saved search's
    date window. The keyword (e.g. a street address) is matched against notice
    text; target_ids narrows the capture to the exact notice(s) wanted.
    """
    logger.info("Keyword search: %r (last %d months)", keyword, months)
    if not await _navigate_to_dashboard(page):
        if not (await _try_relogin(page) and await _navigate_to_dashboard(page)):
            return []

    # Synthetic context: foreclosure + Knox (is_target_county still validates the
    # actual property county, so Blount/Powell properties pass too).
    search = SavedSearch(county="Knox", notice_type="foreclosure",
                         saved_search_name=f"keyword:{keyword}")
    try:
        await page.fill(config.SEL_KEYWORD_SEARCH, keyword)
        # Date-range controls live in a collapsed panel; set them via JS.
        await page.evaluate(
            """(m) => {
                const r = document.querySelector('#ctl00_ContentPlaceHolder1_as1_rbLastNumMonths');
                if (r) { r.checked = true; r.dispatchEvent(new Event('click', {bubbles: true})); }
                const t = document.querySelector('#ctl00_ContentPlaceHolder1_as1_txtLastNumMonths');
                if (t) { t.value = String(m); }
            }""",
            months,
        )
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=40000):
            await page.click(config.SEL_SEARCH_GO)
    except Exception:
        logger.error("Keyword search submit failed for %r", keyword)
        return []

    await delay()
    if "search" not in page.url.lower():
        logger.error("Keyword search did not reach results for %r (%s)", keyword, page.url)
        return []

    await _set_per_page(page)

    notices: list[NoticeData] = []
    current_page, total_pages = await _get_page_info(page)
    while True:
        page_notices = await _scrape_results_page(
            page, search, None, llm_api_key, seen_ids, captcha_failed_ids,
            target_ids=target_ids, match_address=match_address, stop_after=stop_after,
        )
        notices.extend(page_notices)
        if target_ids and len(notices) >= len(target_ids):
            break
        if stop_after and len(notices) >= stop_after:
            break
        if current_page >= total_pages:
            break
        next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
        if next_btn and not await next_btn.get_attribute("disabled"):
            await next_btn.click()
            await page.wait_for_load_state("domcontentloaded")
            await delay()
            current_page, total_pages = await _get_page_info(page)
        else:
            break

    logger.info("  Keyword %r: captured %d", keyword, len(notices))
    return notices


async def scrape_by_keywords(
    targets: list[dict],
    proxy_url: str | None = None,
    llm_api_key: str | None = None,
    months: int = 12,
) -> list[NoticeData]:
    """Log in once, then run a keyword search per target and scrape matches.

    targets: list of {"keyword": str, "target_id": str|None}. Returns all
    captured NoticeData. Never persists seen_ids / last_run (backfill only).
    """
    all_notices: list[NoticeData] = []
    async with async_playwright() as p:
        launch_opts: dict = {"headless": True}
        if proxy_url:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_cfg: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_opts["proxy"] = proxy_cfg
            logger.info("Using proxy: %s:%s", parsed.hostname, parsed.port)
        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(60_000)
        await _load_cookies(context)
        page = await context.new_page()
        if not await _is_session_valid(page):
            if not await login(page):
                logger.error("Login failed, aborting keyword backfill")
                await browser.close()
                return []
            await _save_cookies(context)
        for t in targets:
            kw = t.get("keyword")
            if not kw:
                continue
            # Capture the property's current notice by address (not by a stored
            # ID, which may have rolled off), the first foreclosure that matches.
            try:
                all_notices.extend(
                    await run_keyword_search(
                        page, kw, llm_api_key, months,
                        match_address=kw, stop_after=1,
                    )
                )
            except Exception:
                logger.exception("Keyword search crashed for %r", kw)
                await _try_relogin(page)
        await browser.close()
    logger.info("Keyword backfill captured %d notices total", len(all_notices))
    return all_notices


async def scrape_all(
    mode: str = "daily",
    searches: list[SavedSearch] | None = None,
    proxy_url: str | None = None,
    on_batch=None,
    since_date_override: str | None = None,
    llm_api_key: str | None = None,
    start_page: int = 1,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,
    on_search_complete=None,
    target_ids: set[str] | None = None,
    persist_state: bool = True,
) -> list[NoticeData]:
    """Main entry point for scraping.

    Args:
        mode: "daily" (only new since last run) or "historical" (last 12 months).
        searches: Optional subset of searches to run. Defaults to all.
        proxy_url: Optional proxy URL (e.g. Apify residential proxy).
        on_batch: Optional async callback(list[NoticeData]) called after each search.
        since_date_override: If set (YYYY-MM-DD), overrides the mode-based date logic.
        start_page: Start scraping from this page number (default 1).
        seen_ids: Cross-run dict of already-processed notice IDs. If None, loads from
                  SEEN_IDS_FILE. Caller (e.g. Apify) can pass its own dict loaded
                  from KVS to participate in the dedup cache.
        on_search_complete: Optional async callback(seen_ids) fired after each search
                            completes, so callers can persist seen_ids to their own
                            backing store (e.g. Apify KVS).

    Returns:
        All scraped and filtered NoticeData.
    """
    if searches is None:
        searches = SAVED_SEARCHES

    # Load the cross-run seen-ID cache (caller may have pre-loaded for KVS-backed stores)
    if seen_ids is None:
        seen_ids = load_seen_ids()
    logger.info("Cross-run dedup: %d previously-seen notice IDs loaded", len(seen_ids))

    # Load the CAPTCHA-failed-ID queue from prior runs so the end-of-run summary
    # can show which IDs have been repeatedly failing, not just the current run.
    if captcha_failed_ids is None:
        captcha_failed_ids = load_captcha_failed_ids()
    prior_failed = len(captcha_failed_ids)
    if prior_failed:
        logger.info(
            "CAPTCHA failure queue: %d IDs from prior runs still pending",
            prior_failed,
        )

    # Determine date cutoff
    since_date: str | None = None
    if since_date_override:
        since_date = since_date_override
        logger.info("Using since_date override: %s", since_date)
    elif mode == "daily":
        since_date = load_last_run_date()
        if since_date:
            logger.info("Daily mode: pulling notices since %s", since_date)
        else:
            logger.info("Daily mode: no previous run found, pulling last 7 days")
            since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif mode == "historical":
        since_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        logger.info("Historical mode: pulling notices since %s", since_date)

    all_notices: list[NoticeData] = []

    async with async_playwright() as p:
        launch_opts: dict = {"headless": True}
        if proxy_url:
            # Parse proxy URL (format: http://user:pass@host:port)
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_cfg: dict = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            }
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_opts["proxy"] = proxy_cfg
            logger.info("Using proxy: %s:%s", parsed.hostname, parsed.port)

        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        # Generous timeout for ASP.NET postbacks + CAPTCHA solving
        context.set_default_timeout(60_000)

        # Try to reuse saved session cookies
        await _load_cookies(context)
        page = await context.new_page()

        if not await _is_session_valid(page):
            # Fresh login required
            if not await login(page):
                logger.error("Login failed — aborting scrape")
                await browser.close()
                return []
            # Save cookies for next run
            await _save_cookies(context)

        for search in searches:
            # Proactive session check — re-login if session died between searches
            if "authenticate" in page.url.lower():
                if not await _try_relogin(page):
                    logger.error("Cannot recover session — aborting remaining searches")
                    break

            remaining = (max_notices - len(all_notices)) if max_notices else 0
            try:
                search_notices = await run_saved_search(
                    page, search, since_date, llm_api_key,
                    on_page_batch=on_batch, start_page=start_page,
                    max_notices=remaining, seen_ids=seen_ids,
                    captcha_failed_ids=captcha_failed_ids,
                    target_ids=target_ids,
                )
                all_notices.extend(search_notices)
            except Exception:
                logger.exception("Failed to scrape: %s", search.saved_search_name)
                # Check if failure was due to session expiration and re-login
                if await _try_relogin(page):
                    try:
                        search_notices = await run_saved_search(
                            page, search, since_date, llm_api_key,
                            on_page_batch=on_batch, start_page=start_page,
                            max_notices=remaining, seen_ids=seen_ids,
                            target_ids=target_ids,
                        )
                        all_notices.extend(search_notices)
                    except Exception:
                        logger.exception("Still failing after re-login: %s", search.saved_search_name)

            # Incremental persistence — if a later search crashes fatally, progress
            # from completed searches is not lost. Covers the re-pull bug where a
            # single end-of-run save at line 722 used to silently skip on exceptions.
            try:
                if persist_state:
                    save_seen_ids(seen_ids)
                    if mode == "daily":
                        save_last_run_date()
                if on_search_complete is not None:
                    await on_search_complete(seen_ids)
            except Exception:
                logger.exception("Failed to persist seen_ids after %s", search.saved_search_name)

            if max_notices and len(all_notices) >= max_notices:
                logger.info("Reached max_notices limit (%d) — stopping", max_notices)
                break

        await browser.close()

    # Persist run state unless this is a backfill (persist_state=False), which
    # must not advance last_run.json or clobber the seen-ID cache.
    if persist_state:
        if mode == "daily":
            save_last_run_date()
        save_seen_ids(seen_ids)
        # Persist CAPTCHA failures so operators can follow up on silent drops.
        save_captcha_failed_ids(captcha_failed_ids)
    new_failed = len(captcha_failed_ids) - prior_failed
    if new_failed > 0:
        by_search: dict[str, int] = {}
        for meta in captcha_failed_ids.values():
            if not isinstance(meta, dict):
                continue
            s = meta.get("search", "unknown")
            by_search[s] = by_search.get(s, 0) + 1
        breakdown = ", ".join(f"{s}: {c}" for s, c in sorted(by_search.items()))
        logger.warning(
            "CAPTCHA DROPOUT: %d new notice(s) failed all retries this run "
            "(total queue: %d). Breakdown: %s. See captcha_failed_ids.json.",
            new_failed, len(captcha_failed_ids), breakdown,
        )

    logger.info("Total notices scraped: %d", len(all_notices))
    return all_notices
