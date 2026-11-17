"""Ancestry.com exploration test — auto-login + search.

Usage:
    python test_ancestry.py --search "John Smith" --city Knoxville --state TN
    python test_ancestry.py --search "John Smith" --ssdi         # Search SSDI death index only
    python test_ancestry.py --search "John Smith" --newspapers   # Search Newspapers.com
    python test_ancestry.py --search "John Smith" --explore      # Pause for manual inspection
"""

import argparse
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import config as cfg
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Persistent browser profile directory
PROFILE_DIR = Path(__file__).parent / ".ancestry_profile"
ANCESTRY_URL = "https://www.ancestry.com"
SIGNIN_URL = "https://www.ancestry.com/account/signin"
NEWSPAPERS_URL = "https://www.newspapers.com"

# Daily page load counter
PAGE_LOAD_FILE = Path(__file__).parent / ".ancestry_page_loads.json"
DAILY_LIMIT = 100


def _get_page_loads_today() -> int:
    """Get number of page loads made today."""
    if not PAGE_LOAD_FILE.exists():
        return 0
    try:
        data = json.loads(PAGE_LOAD_FILE.read_text())
        from datetime import date
        if data.get("date") == str(date.today()):
            return data.get("count", 0)
    except Exception:
        pass
    return 0


def _increment_page_loads():
    """Increment today's page load counter."""
    from datetime import date
    count = _get_page_loads_today() + 1
    PAGE_LOAD_FILE.write_text(json.dumps({"date": str(date.today()), "count": count}))
    if count >= DAILY_LIMIT:
        logger.warning("DAILY LIMIT REACHED (%d/%d) — stopping Ancestry lookups", count, DAILY_LIMIT)
    return count


async def _human_delay(min_s=2, max_s=5):
    """Random delay to mimic human browsing. Faster than production but still safe for testing."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Human delay: %.1fs", delay)
    await asyncio.sleep(delay)


async def _check_blocked(page) -> bool:
    """Check if we hit a CAPTCHA, 403, or verification page. CIRCUIT BREAKER."""
    url = page.url.lower()
    title = (await page.title()).lower()

    blocked_signals = [
        "captcha" in url,
        "challenge" in url,
        "verify" in url and "human" in title,
        "blocked" in title,
        "access denied" in title,
    ]

    if any(blocked_signals):
        logger.error("CIRCUIT BREAKER: Possible bot detection! URL=%s, Title=%s", page.url, title)
        logger.error("STOPPING all Ancestry lookups. Fall back to DuckDuckGo pipeline.")
        return True
    return False


async def _auto_login(page) -> bool:
    """Automatically log in to Ancestry using credentials from .env."""
    email = cfg.ANCESTRY_EMAIL
    password = cfg.ANCESTRY_PASSWORD

    if not email or not password:
        logger.error("ANCESTRY_EMAIL or ANCESTRY_PASSWORD not set in .env")
        return False

    logger.info("Auto-login: navigating to sign-in page...")
    await page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    _increment_page_loads()
    await _human_delay(2, 4)

    if await _check_blocked(page):
        return False

    # Fill email
    email_filled = False
    for selector in [
        "input[name='username']", "input[type='email']",
        "input[id*='username' i]", "input[id*='email' i]",
        "#username", "#email",
    ]:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.click()
            await _human_delay(0.3, 0.8)
            await el.fill(email)
            email_filled = True
            logger.info("Filled email via %s", selector)
            break

    if not email_filled:
        logger.error("Could not find email field on login page")
        inputs = await page.query_selector_all("input")
        for i, inp in enumerate(inputs):
            inp_type = await inp.get_attribute("type") or ""
            inp_name = await inp.get_attribute("name") or ""
            inp_id = await inp.get_attribute("id") or ""
            visible = await inp.is_visible()
            logger.info("  Input %d: type=%s, name=%s, id=%s, visible=%s", i, inp_type, inp_name, inp_id, visible)
        return False

    await _human_delay(0.5, 1.5)

    # Fill password
    password_filled = False
    for selector in [
        "input[name='password']", "input[type='password']",
        "input[id*='password' i]", "#password",
    ]:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.click()
            await _human_delay(0.3, 0.8)
            await el.fill(password)
            password_filled = True
            logger.info("Filled password via %s", selector)
            break

    if not password_filled:
        logger.error("Could not find password field on login page")
        return False

    await _human_delay(0.5, 1.5)

    # Click sign-in button
    submitted = False
    for selector in [
        "button[type='submit']",
        "button:has-text('Sign in')", "button:has-text('Sign In')",
        "input[type='submit']",
        "#signInBtn",
    ]:
        btn = await page.query_selector(selector)
        if btn and await btn.is_visible():
            logger.info("Clicking sign-in button: %s", selector)
            await btn.click()
            submitted = True
            break

    if not submitted:
        logger.error("Could not find sign-in button")
        return False

    logger.info("Waiting for login redirect...")
    try:
        await page.wait_for_url("**/account/signin**", timeout=5000)
    except Exception:
        pass

    await _human_delay(3, 5)

    if await _check_blocked(page):
        return False

    current_url = page.url.lower()
    if "signin" in current_url:
        logger.error("Login may have failed — still on sign-in page: %s", page.url)
        error_el = await page.query_selector("[class*='error'], [class*='alert'], [role='alert']")
        if error_el:
            error_text = (await error_el.text_content() or "").strip()
            logger.error("Login error: %s", error_text)
        return False

    logger.info("Login successful! URL: %s", page.url)

    # Warm-up: browse homepage briefly
    logger.info("Warm-up: visiting homepage...")
    await page.goto(ANCESTRY_URL, wait_until="domcontentloaded")
    _increment_page_loads()
    await _human_delay(2, 4)

    return True


async def _ensure_logged_in(page) -> bool:
    """Ensure we're logged in — use persistent session or auto-login.

    Ancestry allows anonymous browsing, so we check for 'Sign In' link
    in the nav bar rather than just checking URL.
    """
    await page.goto(f"{ANCESTRY_URL}/search/", wait_until="domcontentloaded")
    _increment_page_loads()
    await _human_delay(1, 3)

    if await _check_blocked(page):
        return False

    # Check for actual login state — anonymous users can browse /search/ without redirect
    is_signed_in = await page.evaluate("""() => {
        const hasSignIn = !!document.querySelector('a[href*="signin"]');
        const hasAccount = !!document.querySelector('a[href*="account/profile"], [class*="userName"]');
        return hasAccount || !hasSignIn;
    }""")

    if is_signed_in:
        logger.info("Existing session is valid (authenticated)")
        return True

    logger.info("Not logged in — auto-logging in...")
    return await _auto_login(page)


async def _dump_page_elements(page, label: str = ""):
    """Dump all visible inputs, selects, and buttons for debugging."""
    if label:
        logger.info("--- %s ---", label)

    # Inputs
    inputs = await page.query_selector_all("input")
    visible_inputs = []
    for inp in inputs:
        if await inp.is_visible():
            attrs = {
                "type": await inp.get_attribute("type") or "",
                "name": await inp.get_attribute("name") or "",
                "id": await inp.get_attribute("id") or "",
                "placeholder": await inp.get_attribute("placeholder") or "",
                "aria-label": await inp.get_attribute("aria-label") or "",
            }
            visible_inputs.append(attrs)
    logger.info("Visible inputs (%d):", len(visible_inputs))
    for i, a in enumerate(visible_inputs):
        logger.info("  [%d] type=%s name=%s id=%s placeholder=%s label=%s",
                     i, a["type"], a["name"], a["id"], a["placeholder"], a["aria-label"])

    # Selects
    selects = await page.query_selector_all("select")
    visible_selects = []
    for sel in selects:
        if await sel.is_visible():
            attrs = {
                "name": await sel.get_attribute("name") or "",
                "id": await sel.get_attribute("id") or "",
                "aria-label": await sel.get_attribute("aria-label") or "",
            }
            visible_selects.append(attrs)
    if visible_selects:
        logger.info("Visible selects (%d):", len(visible_selects))
        for i, a in enumerate(visible_selects):
            logger.info("  [%d] name=%s id=%s label=%s", i, a["name"], a["id"], a["aria-label"])

    # Buttons
    buttons = await page.query_selector_all("button, input[type='submit']")
    visible_buttons = []
    for btn in buttons:
        if await btn.is_visible():
            text = (await btn.text_content() or "").strip()[:60]
            btn_type = await btn.get_attribute("type") or ""
            visible_buttons.append({"text": text, "type": btn_type})
    logger.info("Visible buttons (%d):", len(visible_buttons))
    for i, b in enumerate(visible_buttons):
        logger.info("  [%d] text='%s' type=%s", i, b["text"], b["type"])


async def _dump_results(page):
    """Try multiple selectors to find and log search results."""
    logger.info("Results URL: %s", page.url)
    logger.info("Results title: %s", await page.title())

    # Try various result selectors
    selector_attempts = [
        ("tr.tblrow", "table row"),
        ("tr[class*='Row']", "capitalized Row"),
        (".srp-row", "srp-row"),
        ("[class*='result']", "class contains 'result'"),
        ("[class*='record']", "class contains 'record'"),
        ("[data-testid*='result']", "data-testid result"),
        ("table tbody tr", "any table row"),
        (".searchResult", "searchResult class"),
        ("#searchResults tr", "searchResults table rows"),
        ("[class*='conRes']", "conRes class"),
    ]

    for selector, desc in selector_attempts:
        results = await page.query_selector_all(selector)
        if results:
            logger.info("Found %d results via '%s' (%s)", len(results), selector, desc)
            for i, res in enumerate(results[:5]):
                text = (await res.text_content() or "").strip()[:300]
                # Clean up whitespace
                text = " ".join(text.split())
                logger.info("  Result %d: %s", i, text)
            return

    # Nothing found — dump page structure for debugging
    logger.warning("No results found with any known selector")
    # Check if there's an error or no-results message
    for sel in ["[class*='noResult']", "[class*='no-result']", "[class*='empty']", ".srp-noResults"]:
        el = await page.query_selector(sel)
        if el:
            text = (await el.text_content() or "").strip()[:200]
            logger.info("No-results element found (%s): %s", sel, text)


async def run_search_mode(name: str, city: str = "", state: str = "TN",
                          explore: bool = False, ssdi: bool = False, newspapers: bool = False):
    """Search Ancestry for a person with auto-login."""
    PROFILE_DIR.mkdir(exist_ok=True)

    loads_today = _get_page_loads_today()
    if loads_today >= DAILY_LIMIT:
        logger.error("Daily page load limit reached (%d/%d). Try again tomorrow.", loads_today, DAILY_LIMIT)
        return

    mode = "SSDI" if ssdi else "Newspapers.com" if newspapers else "All Collections"
    logger.info("Searching %s for: %s, %s %s", mode, name, city, state)
    logger.info("Page loads today: %d/%d", loads_today, DAILY_LIMIT)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            # Ensure logged in (auto-login if needed)
            if not await _ensure_logged_in(page):
                logger.error("Could not log in to Ancestry. Aborting.")
                await context.close()
                return

            logger.info("Logged in. Current URL: %s", page.url)

            if newspapers:
                await _run_newspapers_search(page, name, city, state, explore)
            elif ssdi:
                await _run_ssdi_search(page, name, city, state, explore)
            else:
                await _run_ancestry_search(page, name, city, state, explore)

        except Exception as e:
            logger.error("Error during search: %s", e, exc_info=True)

        await context.close()


async def _run_ancestry_search(page, name: str, city: str, state: str, explore: bool):
    """Search Ancestry.com all collections."""
    # Navigate to search
    if "/search" not in page.url.lower():
        await page.goto(f"{ANCESTRY_URL}/search/", wait_until="domcontentloaded")
        _increment_page_loads()
        await _human_delay(2, 4)

    await _dump_page_elements(page, "Search page structure")

    # Parse name
    parts = name.strip().split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""

    # Fill first name — use discovered selectors from run 1
    el = await page.query_selector("#sfs_FirstNameExactModule")
    if el and await el.is_visible():
        await el.fill(first_name)
        logger.info("Filled first name '%s' via #sfs_FirstNameExactModule", first_name)
    else:
        logger.error("Could not find first name field")

    await _human_delay(0.5, 1.5)

    # Fill last name
    el = await page.query_selector("#sfsLastNameExactModule")
    if el and await el.is_visible():
        await el.fill(last_name)
        logger.info("Filled last name '%s' via #sfsLastNameExactModule", last_name)
    else:
        logger.error("Could not find last name field")

    await _human_delay(1, 2)

    # Click "Show more options" to reveal death/location filters
    more_btn = await page.query_selector("button:has-text('Show more options')")
    if more_btn and await more_btn.is_visible():
        logger.info("Clicking 'Show more options' to reveal filters...")
        await more_btn.click()
        await _human_delay(1, 2)

        # Dump the expanded options
        await _dump_page_elements(page, "Expanded search options")

    # Click the Death life-event button to reveal death year/location fields
    death_btn = await page.query_selector("input#Death[type='button']")
    if death_btn and await death_btn.is_visible():
        logger.info("Clicking 'Death' life-event button to reveal death filters...")
        await death_btn.click()
        await _human_delay(1, 2)
        # Dump the new fields that appeared
        await _dump_page_elements(page, "After clicking Death button")

    await _human_delay(1, 2)

    # Submit search using the discovered #searchButton
    search_btn = await page.query_selector("#searchButton")
    if search_btn and await search_btn.is_visible():
        logger.info("Clicking #searchButton")
        await search_btn.click()
    else:
        logger.error("Could not find #searchButton")

    # Wait for navigation to results
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    await _human_delay(3, 5)
    _increment_page_loads()

    if await _check_blocked(page):
        return

    # Dump results
    await _dump_results(page)

    if explore:
        await _wait_for_browser_close(page, context=None)


async def _run_ssdi_search(page, name: str, city: str, state: str, explore: bool):
    """Search Ancestry SSDI (Social Security Death Index) specifically."""
    ssdi_url = f"{ANCESTRY_URL}/search/collections/ssdi/"
    logger.info("Navigating to SSDI search: %s", ssdi_url)
    await page.goto(ssdi_url, wait_until="domcontentloaded")
    _increment_page_loads()
    await _human_delay(2, 4)

    if await _check_blocked(page):
        return

    await _dump_page_elements(page, "SSDI search page structure")

    # Parse name
    parts = name.strip().split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""

    # Fill name fields (SSDI page may have different selectors)
    for selector in [
        "#sfs_FirstNameExactModule", "input[name*='first' i]",
        "input[aria-label*='First' i]",
    ]:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.fill(first_name)
            logger.info("Filled first name '%s' via %s", first_name, selector)
            break

    await _human_delay(0.5, 1)

    for selector in [
        "#sfsLastNameExactModule", "input[name*='last' i]",
        "input[aria-label*='Last' i]",
    ]:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.fill(last_name)
            logger.info("Filled last name '%s' via %s", last_name, selector)
            break

    await _human_delay(1, 2)

    # Submit
    submit_btns = await page.query_selector_all("button[type='submit'], input[type='submit']")
    for btn in submit_btns:
        if await btn.is_visible():
            logger.info("Clicking SSDI search submit")
            await btn.click()
            break

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    await _human_delay(3, 5)
    _increment_page_loads()

    if await _check_blocked(page):
        return

    await _dump_results(page)

    if explore:
        await _wait_for_browser_close(page, context=None)


async def _run_newspapers_search(page, name: str, city: str, state: str, explore: bool):
    """Search Newspapers.com for obituaries/death notices."""
    logger.info("Navigating to Newspapers.com...")

    # Newspapers.com should share SSO with Ancestry via All-Access
    await page.goto(f"{NEWSPAPERS_URL}/search/", wait_until="domcontentloaded")
    _increment_page_loads()
    await _human_delay(2, 4)

    if await _check_blocked(page):
        return

    # Check if we need to log in separately to newspapers.com
    current_url = page.url.lower()
    if "signin" in current_url or "login" in current_url:
        logger.warning("Newspapers.com requires separate login — checking for Ancestry SSO link...")
        # Look for "Sign in with Ancestry" or similar SSO button
        sso_btn = await page.query_selector("a:has-text('Ancestry'), button:has-text('Ancestry')")
        if sso_btn:
            logger.info("Found Ancestry SSO button, clicking...")
            await sso_btn.click()
            await _human_delay(3, 5)
        else:
            logger.warning("No SSO button found. Dumping login page elements...")
            await _dump_page_elements(page, "Newspapers.com login page")

    await _dump_page_elements(page, "Newspapers.com search page")

    # Try to fill search fields
    search_filled = False
    search_query = f"{name} obituary"
    if city:
        search_query += f" {city}"

    for selector in [
        "input[name*='query' i]", "input[type='search']",
        "input[name*='search' i]", "input[id*='search' i]",
        "input[placeholder*='search' i]", "input[placeholder*='keyword' i]",
        "input[aria-label*='search' i]",
    ]:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.fill(search_query)
            search_filled = True
            logger.info("Filled search '%s' via %s", search_query, selector)
            break

    if not search_filled:
        logger.warning("Could not find search field on Newspapers.com")

    await _human_delay(1, 2)

    # Submit
    submit_btns = await page.query_selector_all("button[type='submit'], input[type='submit']")
    for btn in submit_btns:
        if await btn.is_visible():
            logger.info("Clicking Newspapers.com search submit")
            await btn.click()
            break

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    await _human_delay(3, 5)
    _increment_page_loads()

    if await _check_blocked(page):
        return

    await _dump_results(page)

    # Always pause for newspapers.com during development
    logger.info("")
    logger.info("=== Browser open for Newspapers.com inspection ===")
    logger.info("Page loads today: %d/%d", _get_page_loads_today(), DAILY_LIMIT)

    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass


async def _wait_for_browser_close(page, context):
    """Wait for user to close the browser."""
    logger.info("")
    logger.info("=== Browser open for inspection ===")
    logger.info("Close browser when done.")
    logger.info("Page loads today: %d/%d", _get_page_loads_today(), DAILY_LIMIT)

    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass


async def run_batch_mode(count: int = 5):
    """Test ancestry_enricher against ground truth deceased owners.

    Picks a mix of SSDI-era (pre-2014) and recent deaths to measure hit rate.
    Uses the production ancestry_enricher module directly.
    """
    import ancestry_enricher

    # Load ground truth. The file holds real obituary records, so it is kept
    # local and is not part of the public release: without it, skip cleanly.
    gt_path = Path(__file__).parent / "tests" / "obituary_ground_truth.json"
    if not gt_path.exists():
        logger.warning("SKIPPED batch mode: %s is not present. It holds real obituary records and is not "
                       "shipped with the public release; put your own ground truth file there to run this.",
                       gt_path)
        return

    gt = json.load(open(gt_path))

    # Deduplicate and filter to clean names with death dates
    seen = set()
    candidates = []
    for r in gt:
        name = r["owner_name"]
        dod = r.get("date_of_death", "")
        if not dod or name in seen:
            continue
        # Skip joint owners, estates, suffixes that confuse search
        if any(x in name for x in ["And ", "Estate", "A.K.A", "Unmarried"]):
            continue
        seen.add(name)
        candidates.append(r)

    # Sort by death date — pick mix of old (SSDI-era) and recent
    candidates.sort(key=lambda r: r.get("date_of_death", ""))

    # Pick: 2 oldest (pre-2014, SSDI should have), 3 recent (post-2020, may need obituary tier)
    old = [c for c in candidates if c["date_of_death"] < "2015"][:2]
    recent = [c for c in candidates if c["date_of_death"] >= "2020"][:min(count - len(old), 10)]
    # If not enough old ones, fill with more recent
    test_batch = old + recent[:count - len(old)]

    logger.info("=" * 70)
    logger.info("BATCH TEST: %d ground truth deceased owners via ancestry_enricher", len(test_batch))
    logger.info("=" * 70)
    for i, r in enumerate(test_batch):
        logger.info("  [%d] %s (died %s, %s)", i + 1, r["owner_name"], r["date_of_death"], r.get("city", ""))

    # Launch browser via ancestry_enricher
    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        logger.error("Failed to launch browser / login")
        return

    hits = 0
    misses = 0
    results_summary = []

    try:
        for i, record in enumerate(test_batch):
            name = record["owner_name"]
            city = record.get("city", "")
            expected_dod = record.get("date_of_death", "")

            logger.info("")
            logger.info("-" * 60)
            logger.info("[%d/%d] Searching: %s (expected death: %s)",
                        i + 1, len(test_batch), name, expected_dod)

            result = await ancestry_enricher.lookup_deceased(page, name=name, city=city, state="TN")

            if result and result.get("confirmed_deceased"):
                hits += 1
                status = "HIT"
                logger.info("  FOUND: %s via %s (death: %s)",
                            result.get("full_name", ""), result.get("source_type", ""),
                            result.get("date_of_death", ""))
            else:
                misses += 1
                status = "MISS"
                logger.info("  NOT FOUND on Ancestry")

            results_summary.append({
                "name": name,
                "expected_dod": expected_dod,
                "status": status,
                "source_type": result.get("source_type", "") if result else "",
                "found_name": result.get("full_name", "") if result else "",
                "found_dod": result.get("date_of_death", "") if result else "",
            })

            # Delay between lookups
            if i < len(test_batch) - 1:
                await _human_delay(3, 6)

    finally:
        await ancestry_enricher.close_browser(pw, context)

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("BATCH RESULTS: %d/%d hits (%.0f%% hit rate)", hits, len(test_batch),
                100 * hits / len(test_batch) if test_batch else 0)
    logger.info("=" * 70)
    for r in results_summary:
        marker = "+" if r["status"] == "HIT" else "-"
        logger.info("  [%s] %s (expected: %s) → %s %s %s",
                     marker, r["name"], r["expected_dod"],
                     r["status"], r["source_type"], r["found_dod"])
    logger.info("")
    logger.info("Page loads today: %d/%d", _get_page_loads_today(), DAILY_LIMIT)


async def run_login_mode():
    """Open browser for manual login. Session saved to persistent profile for future automation."""
    PROFILE_DIR.mkdir(exist_ok=True)

    logger.info("Opening browser for manual Ancestry login...")
    logger.info("Profile directory: %s", PROFILE_DIR.resolve())
    logger.info("")
    logger.info("Instructions:")
    logger.info("  1. Log in to Ancestry.com with your All-Access account")
    logger.info("  2. Verify you see your subscription status (not 'Subscribe' button)")
    logger.info("  3. Close the browser when done")
    logger.info("")
    logger.info("The session will be saved automatically to the persistent profile.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(f"{ANCESTRY_URL}/account/signin", wait_until="domcontentloaded")

        # Wait for user to close browser
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        await context.close()

    logger.info("Session saved! Future automation will reuse this login.")


def main():
    parser = argparse.ArgumentParser(description="Ancestry.com exploration test (auto-login)")
    parser.add_argument("--search", type=str, help="Person name to search")
    parser.add_argument("--city", type=str, default="", help="City filter")
    parser.add_argument("--state", type=str, default="TN", help="State filter (default: TN)")
    parser.add_argument("--explore", action="store_true", help="Keep browser open for manual inspection")
    parser.add_argument("--ssdi", action="store_true", help="Search SSDI death index only")
    parser.add_argument("--newspapers", action="store_true", help="Search Newspapers.com")
    parser.add_argument("--batch", type=int, nargs="?", const=5, default=None,
                        help="Batch test N ground truth owners (default: 5)")
    parser.add_argument("--login", action="store_true",
                        help="Open browser for manual login (saves session to persistent profile)")
    args = parser.parse_args()

    if args.login:
        asyncio.run(run_login_mode())
    elif args.batch is not None:
        asyncio.run(run_batch_mode(args.batch))
    elif args.search:
        asyncio.run(run_search_mode(
            args.search, args.city, args.state,
            args.explore, args.ssdi, args.newspapers
        ))
    else:
        parser.print_help()
        print("\nExamples:")
        print('  python test_ancestry.py --search "John Smith" --city Knoxville')
        print('  python test_ancestry.py --search "John Smith" --ssdi')
        print('  python test_ancestry.py --search "John Smith" --newspapers')
        print('  python test_ancestry.py --batch 5    # Test 5 ground truth owners')
        print('  python test_ancestry.py --batch 10   # Test 10 ground truth owners')


if __name__ == "__main__":
    main()
