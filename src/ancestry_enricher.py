"""Ancestry.com All-Access enricher — Playwright automation for death records + family trees.

Search cascade:
  1. SSDI (Social Security Death Index) — most structured, 89M+ records
  2. Ancestry obituary collection — broad coverage
  3. Newspapers.com obituary index — 930M+ pages, recent TN papers (All-Access SSO)

Account protection is #1 priority:
  - Headed browser + persistent profile
  - Human-like delays (2-5s between actions)
  - Circuit breaker on any bot detection signal
  - Daily page load limit (100)
  - Single tab, sequential lookups only
"""

import asyncio
import json
import logging
import random
import re
from datetime import date
from pathlib import Path

import config as cfg

logger = logging.getLogger(__name__)

# Persistent browser profile directory
PROFILE_DIR = Path(__file__).resolve().parent.parent / ".ancestry_profile"
ANCESTRY_URL = "https://www.ancestry.com"
SIGNIN_URL = "https://www.ancestry.com/account/signin"

# Daily page load counter
PAGE_LOAD_FILE = Path(__file__).resolve().parent.parent / ".ancestry_page_loads.json"
DAILY_LIMIT = 100

# ── Page load tracking ──────────────────────────────────────────────


def _get_page_loads_today() -> int:
    if not PAGE_LOAD_FILE.exists():
        return 0
    try:
        data = json.loads(PAGE_LOAD_FILE.read_text())
        if data.get("date") == str(date.today()):
            return data.get("count", 0)
    except Exception:
        pass
    return 0


def _increment_page_loads() -> int:
    count = _get_page_loads_today() + 1
    PAGE_LOAD_FILE.write_text(json.dumps({"date": str(date.today()), "count": count}))
    if count >= DAILY_LIMIT:
        logger.warning("DAILY LIMIT REACHED (%d/%d)", count, DAILY_LIMIT)
    return count


def _can_load_page() -> bool:
    return _get_page_loads_today() < DAILY_LIMIT


# ── Human-like delays ───────────────────────────────────────────────


async def _delay(min_s=2.0, max_s=4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# ── Circuit breaker ─────────────────────────────────────────────────

_circuit_broken = False


async def _check_blocked(page) -> bool:
    """Return True if bot detection detected. Sets global circuit breaker."""
    global _circuit_broken
    if _circuit_broken:
        return True

    try:
        url = page.url.lower()
        title = (await page.title()).lower()
    except Exception:
        # Navigation in progress — not blocked, just transient
        return False

    if any([
        "captcha" in url,
        "challenge" in url,
        "verify" in url and "human" in title,
        "blocked" in title,
        "access denied" in title,
    ]):
        _circuit_broken = True
        logger.error("CIRCUIT BREAKER: Bot detection! URL=%s Title=%s", page.url, title)
        return True
    return False


def is_circuit_broken() -> bool:
    return _circuit_broken


def reset_circuit_breaker():
    global _circuit_broken
    _circuit_broken = False


# ── Login ───────────────────────────────────────────────────────────


async def _ensure_logged_in(page) -> bool:
    """Check existing session or auto-login.

    Ancestry allows anonymous browsing (no redirect to signin), so we can't
    just check the URL. Instead, check for 'Sign In' link in the nav bar
    which indicates no active session.
    """
    await page.goto(f"{ANCESTRY_URL}/search/", wait_until="domcontentloaded")
    _increment_page_loads()
    await _delay(1, 3)

    if await _check_blocked(page):
        return False

    # Check for actual login state — not just URL
    is_signed_in = await page.evaluate("""() => {
        const text = document.body.textContent || '';
        // "Sign In" in nav = NOT logged in; user menu/account link = logged in
        const hasSignIn = !!document.querySelector('a[href*="signin"]');
        const hasAccount = !!document.querySelector('a[href*="account/profile"], [class*="userName"]');
        return hasAccount || !hasSignIn;
    }""")

    if is_signed_in:
        logger.info("Ancestry session valid (authenticated)")
        return True

    logger.info("Not logged in — auto-logging in...")
    return await _auto_login(page)


async def _auto_login(page) -> bool:
    email = cfg.ANCESTRY_EMAIL
    password = cfg.ANCESTRY_PASSWORD

    if not email or not password:
        logger.error("ANCESTRY_EMAIL or ANCESTRY_PASSWORD not set")
        return False

    await page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    _increment_page_loads()
    await _delay(2, 4)

    if await _check_blocked(page):
        return False

    # Fill email
    for sel in ["input[name='username']", "input[type='email']", "#username"]:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            await el.click()
            await _delay(0.3, 0.6)
            await el.fill(email)
            break
    else:
        logger.error("Cannot find email field")
        return False

    await _delay(0.5, 1)

    # Fill password
    for sel in ["input[name='password']", "input[type='password']", "#password"]:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            await el.click()
            await _delay(0.3, 0.6)
            await el.fill(password)
            break
    else:
        logger.error("Cannot find password field")
        return False

    await _delay(0.5, 1)

    # Submit
    for sel in ["button[type='submit']", "input[type='submit']"]:
        btn = await page.query_selector(sel)
        if btn and await btn.is_visible():
            await btn.click()
            break
    else:
        logger.error("Cannot find sign-in button")
        return False

    await _delay(3, 5)

    if await _check_blocked(page):
        return False

    if "signin" in page.url.lower():
        logger.error("Login failed — still on signin page")
        return False

    logger.info("Login successful: %s", page.url)

    # Warm-up
    await page.goto(ANCESTRY_URL, wait_until="domcontentloaded")
    _increment_page_loads()
    await _delay(2, 3)

    return True


# ── Browser lifecycle ───────────────────────────────────────────────


async def launch_browser():
    """Launch persistent browser context. Returns (playwright, context, page).

    Caller must close context when done.
    """
    from playwright.async_api import async_playwright

    PROFILE_DIR.mkdir(exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()

    if not await _ensure_logged_in(page):
        await context.close()
        await pw.stop()
        return None, None, None

    return pw, context, page


async def close_browser(pw, context):
    """Cleanup browser resources."""
    if context:
        try:
            await context.close()
        except Exception:
            pass
    if pw:
        try:
            await pw.stop()
        except Exception:
            pass


# ── Search: SSDI ────────────────────────────────────────────────────


async def _search_ssdi(page, first_name: str, last_name: str, state: str = "OH",
                       middle_initial: str = "", city: str = "") -> dict | None:
    """Search SSDI death index. Returns structured result or None."""
    if not _can_load_page() or _circuit_broken:
        return None

    # SSDI collection ID is 3693
    ssdi_url = f"{ANCESTRY_URL}/search/collections/ssdi/"
    await page.goto(ssdi_url, wait_until="domcontentloaded")
    _increment_page_loads()
    await _delay(2, 3)

    if await _check_blocked(page):
        return None

    # Fill first name
    el = await page.query_selector("#sfs_FirstNameExactModule")
    if el and await el.is_visible():
        await el.fill(first_name)
    await _delay(0.3, 0.6)

    # Fill last name
    el = await page.query_selector("#sfsLastNameExactModule")
    if el and await el.is_visible():
        await el.fill(last_name)
    await _delay(0.3, 0.6)

    # Fill "Lived In Location" to narrow results to state
    if state:
        loc_el = await page.query_selector("#sfs__SelfResidencePlace")
        if loc_el and await loc_el.is_visible():
            state_name = {"OH": "Ohio", "TN": "Tennessee", "AL": "Alabama"}.get(state, state)
            await loc_el.fill(state_name)
            await _delay(1, 2)
            # Wait for autocomplete dropdown and select first match
            try:
                suggestion = await page.wait_for_selector(
                    "[class*='autocomplete'] li, [class*='suggestion'], [role='option']",
                    timeout=3000,
                )
                if suggestion:
                    await suggestion.click()
                    await _delay(0.3, 0.6)
            except Exception:
                # No autocomplete — just leave the text
                pass

    # Submit
    btn = await page.query_selector("#searchButton")
    if btn and await btn.is_visible():
        await btn.click()
    else:
        logger.warning("SSDI: no search button found")
        return None

    # Wait for results page to load
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await _delay(3, 5)
    _increment_page_loads()

    if await _check_blocked(page):
        return None

    # Parse SSDI results table
    # Columns: Name | Birth Date | Death Date | Last Residence
    results = await _parse_ssdi_results(page)
    if not results:
        logger.debug("SSDI: no results for %s %s", first_name, last_name)
        return None

    # Score and rank matches — don't just take the first one
    scored = []
    for r in results:
        rname = r.get("name", "")
        rloc = r.get("location", "")
        rdeath = r.get("death_date", "")

        if not _name_matches(first_name, last_name, rname, middle_initial):
            continue

        loc_ok, loc_score = _location_matches(rloc, state, city)
        if not loc_ok:
            logger.debug("SSDI skip (wrong state): %s — %s", rname, rloc)
            continue

        # Extract death year from full date ("19 Mar 2013") or year-only ("2013")
        death_year = 0
        if rdeath:
            yr_match = re.search(r"\d{4}", rdeath)
            if yr_match:
                death_year = int(yr_match.group())

        # Reject matches that died before 2000 — we're looking for recent/recent-ish deaths
        # Property owners who died 30+ years ago are almost certainly different people
        if death_year > 0 and death_year < 2000:
            logger.debug("SSDI skip (too old, died %d): %s", death_year, rname)
            continue

        # Composite score: location match (0-2) * 10000 + death year (prefer recent)
        composite = loc_score * 10000 + death_year
        scored.append((composite, loc_score, r))

    if not scored:
        logger.debug("SSDI: no valid matches for %s %s after filtering", first_name, last_name)
        return None

    # Pick best match (highest composite = best location + most recent death)
    scored.sort(key=lambda x: x[0], reverse=True)
    best_composite, best_loc_score, best = scored[0]

    # If multiple candidates remain and NONE have county-level match,
    # this is ambiguous — could be anyone in TN with this name
    if len(scored) > 1 and best_loc_score < 2:
        logger.info("SSDI: %d ambiguous TN matches for %s %s — skipping (no county match)",
                     len(scored), first_name, last_name)
        for _, ls, r in scored[:5]:
            logger.debug("  candidate: %s (death: %s, loc: %s, loc_score: %d)",
                         r.get("name"), r.get("death_date", ""), r.get("location", ""), ls)
        return None

    # Quality gate: if we have city info, require county-level location match (score >= 2)
    # to avoid matching a "Dora Wilson in Tipton County" when looking for "Dora Wilson in Knox County"
    # Exception: globally unique names (1-2 total results) are safe even without county match
    if city and best_loc_score < 2:
        best_name = best.get("name", "")
        has_middle = middle_initial and middle_initial.upper() in best_name.upper()
        total_results = len(results)
        if not has_middle and total_results > 2:
            logger.info("SSDI: match for %s %s but loc_score=%d, %d results (need county match) — skipping",
                         first_name, last_name, best_loc_score, total_results)
            return None

    # Even single matches need a quality check — if no location AND common name,
    # we can't be sure it's the right person
    if best_loc_score == 0 and len(scored) == 1:
        best_name = best.get("name", "")
        has_middle = middle_initial and middle_initial.upper() in best_name.upper()
        if not has_middle:
            total_results = len(results)
            if total_results > 1:
                logger.info("SSDI: single match but no location, no middle initial, %d total results — too risky",
                             total_results)
                return None

    logger.info("SSDI match: %s (death: %s, residence: %s) [%d candidates, loc_score=%d]",
                best.get("name"), best.get("death_date", ""), best.get("location", ""),
                len(scored), best_loc_score)
    return {
        "confirmed_deceased": True,
        "date_of_death": best.get("death_date", ""),
        "source_url": page.url,
        "source_type": "ssdi",
        "full_name": best.get("name", ""),
        "birth_date": best.get("birth_date", ""),
        "last_residence": best.get("location", ""),
        "family_members": [],
        "obituary_text": None,
    }


async def _parse_ssdi_results(page) -> list[dict]:
    """Parse SSDI search results table.

    SSDI table has 6 columns:
      [0] "View Record" link
      [1] Name (e.g., "John H Smith")
      [2] Birth Date (e.g., "x xxx xxxx" or "xx xxx 1940")
      [3] Death Date (e.g., "xx xxx 1996")
      [4] Last Residence (e.g., "xxxxxxxxx Knox, Tennessee, USA")
      [5] "Primary record"

    Dates are partially masked with x's — only year is visible.
    """
    results = []

    try:
        data = await page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll('tr');
            const debug = [];
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                // Log all row structures for debugging
                if (cells.length >= 4) {
                    debug.push('ROW[' + cells.length + ']: ' +
                        Array.from(cells).map((c, i) => i + '=' + (c.textContent || '').trim().substring(0, 60)).join(' | '));
                }
                // Data rows have exactly 6 cells
                if (cells.length === 6) {
                    const name = (cells[1].textContent || '').trim();
                    // Skip header rows (cell[1] = "Name")
                    if (name === 'Name' || name.length < 3) continue;
                    results.push({
                        name: name,
                        birth_date: (cells[2].textContent || '').trim(),
                        death_date: (cells[3].textContent || '').trim(),
                        location: (cells[4].textContent || '').trim()
                    });
                }
            }
            return {results: results.slice(0, 20), debug: debug.slice(0, 10)};
        }""")

        # Log debug rows to see actual table structure
        raw = data or {}
        for dbg in (raw.get("debug") or []):
            logger.debug("SSDI row: %s", dbg)

        for item in (raw.get("results") or []):
            if item.get("name") and len(item["name"]) > 3:
                # Extract year from masked dates ("xx xxx 1996" → "1996")
                for key in ["birth_date", "death_date"]:
                    val = item.get(key, "")
                    if "x" in val.lower():
                        year_match = re.search(r"\d{4}", val)
                        item[key] = year_match.group() if year_match else ""

                # Clean location — remove x-masked city
                loc = item.get("location", "")
                loc = re.sub(r"^x+\s*", "", loc)
                item["location"] = loc

                results.append(item)

    except Exception as e:
        logger.debug("SSDI result parsing error: %s", e)

    # Fallback: use generic parser
    if not results:
        results = await _parse_search_results(page)

    return results


# ── Search: Ancestry obituary collection ────────────────────────────


async def _search_obituaries(page, first_name: str, last_name: str, state: str = "OH",
                             city: str = "", middle_initial: str = "") -> dict | None:
    """Search Ancestry obituary collection via direct URL. Returns result or None."""
    if not _can_load_page() or _circuit_broken:
        return None

    # Navigate directly to search results URL — bypasses SPA form issues
    # Category 34 = "Death, Burial, Cemetery & Obituaries"
    import urllib.parse
    state_name = {"TN": "Tennessee", "AL": "Alabama"}.get(state, state)
    params = {
        "name": f"{first_name}_{last_name}",
        "birth": "",
        "death": "",
        "residence": f"_{state_name.lower().replace(' ', '-')}-usa",
        "category": "34",  # Death, Burial, Cemetery & Obituaries
    }
    search_url = f"{ANCESTRY_URL}/search/categories/34/?" + urllib.parse.urlencode(params)
    logger.debug("Obituary search URL: %s", search_url)

    await page.goto(search_url, wait_until="domcontentloaded")
    _increment_page_loads()

    # Wait for SPA results to render
    try:
        await page.wait_for_selector(
            "table tbody tr, .srp-row, [class*='searchResult'], [class*='conRes'], "
            "[class*='result-item'], [data-testid*='result']",
            timeout=10000,
        )
    except Exception:
        pass

    await _delay(3, 5)
    _increment_page_loads()

    if await _check_blocked(page):
        return None

    results = await _parse_obituary_results(page)
    if not results:
        logger.debug("Obituary search: no results parsed. URL: %s", page.url)
        return None

    # Score and filter results
    scored = []
    for r in results:
        rname = r.get("name", "")
        rloc = r.get("location", "") or r.get("death_location", "")
        rdeath = r.get("death_date", "")

        if not _name_matches(first_name, last_name, rname, middle_initial):
            continue

        # Check both residence AND death location for best loc_score
        loc_ok, loc_score = _location_matches(rloc, state, city)
        death_loc = r.get("death_location", "")
        if death_loc and death_loc != rloc:
            _, dl_score = _location_matches(death_loc, state, city)
            if dl_score > loc_score:
                loc_score = dl_score
                loc_ok = True

        if not loc_ok:
            continue

        # Quality gate: must have EITHER county-level location match OR death date
        if loc_score < 2 and not rdeath:
            logger.debug("Obituary skip (no location or death date): %s — %s", rname, rloc)
            continue

        death_year = 0
        if rdeath:
            yr_match = re.search(r"\d{4}", rdeath)
            if yr_match:
                death_year = int(yr_match.group())

        # Reject deaths before 2000 (same as SSDI)
        if death_year > 0 and death_year < 2000:
            logger.debug("Obituary skip (too old, died %d): %s", death_year, rname)
            continue

        composite = loc_score * 10000 + death_year
        scored.append((composite, loc_score, r))

    if not scored:
        logger.debug("Obituary: no quality matches for %s %s", first_name, last_name)
        return None

    # Pick best match
    scored.sort(key=lambda x: x[0], reverse=True)
    _, best_loc_score, best = scored[0]

    # Ambiguity filter: multiple candidates with no county match
    if len(scored) > 1 and best_loc_score < 2:
        logger.info("Obituary: %d ambiguous matches for %s %s — skipping (no county match)",
                     len(scored), first_name, last_name)
        return None

    # City quality gate: if city provided, need county match unless middle initial confirms
    # Exception: globally unique names (1 candidate total) are safe
    if city and best_loc_score < 2:
        best_name = best.get("name", "")
        has_middle = middle_initial and middle_initial.upper() in best_name.upper()
        if not has_middle and len(scored) > 1:
            logger.info("Obituary: match but loc_score=%d, %d results (need county match) — skipping",
                         best_loc_score, len(scored))
            return None

    logger.info("Obituary match: %s (death: %s, residence: %s, loc_score=%d) [%d candidates]",
                 best.get("name"), best.get("death_date", ""), best.get("location", ""),
                 best_loc_score, len(scored))
    return {
        "confirmed_deceased": True,
        "date_of_death": best.get("death_date", ""),
        "source_url": best.get("record_url", page.url),
        "source_type": "obituary_collection",
        "full_name": best.get("name", ""),
        "birth_date": best.get("birth_date", ""),
        "last_residence": best.get("location", ""),
        "family_members": best.get("family_members", []),
        "obituary_text": None,
    }


async def _search_newspapers(page, first_name: str, last_name: str, state: str = "OH",
                              city: str = "", middle_initial: str = "") -> dict | None:
    """Search Newspapers.com obituary index via All-Access SSO (Tier 3).

    Newspapers.com has 930M+ pages including recent TN obituaries from
    Knoxville News Sentinel, The Daily Times (Blount), etc.
    Shares SSO with Ancestry All-Access — no separate login needed.
    """
    if not _can_load_page() or _circuit_broken:
        return None

    import urllib.parse

    # Build search URL with obituary category filter
    # Newspapers.com search URL format: /search/?query=FIRSTNAME+LASTNAME&t=4268
    # t=4268 = Obituaries category (from the category dropdown)
    state_abbr_to_full = {"OH": "Ohio", "TN": "Tennessee", "AL": "Alabama"}
    state_full = state_abbr_to_full.get(state, state)

    query_parts = [first_name, last_name]
    search_query = " ".join(query_parts)

    params = {
        "query": search_query,
        "t": "4268",  # Obituaries category
    }

    # Add location filter if available
    if city:
        params["pl"] = f"{city}, {state_full}"

    search_url = f"https://www.newspapers.com/search/?{urllib.parse.urlencode(params)}"
    logger.debug("Newspapers.com search URL: %s", search_url)

    await page.goto(search_url, wait_until="domcontentloaded")
    _increment_page_loads()

    # Cross-site delay (different domain from ancestry.com)
    await _delay(8, 15)

    if await _check_blocked(page):
        return None

    # Check if redirected to login (SSO may not carry over)
    current_url = page.url.lower()
    if "signin" in current_url or "login" in current_url or "account" in current_url:
        logger.info("Newspapers.com requires separate login — SSO did not carry over. Skipping.")
        return None

    # Parse search results
    results = await _parse_newspapers_results(page)
    if not results:
        logger.debug("Newspapers.com: no results for %s %s", first_name, last_name)
        return None

    # Score and filter results (same logic as obituary search)
    scored = []
    for r in results:
        rname = r.get("name", "")
        rloc = r.get("location", "")
        rdeath = r.get("date", "")

        if not _name_matches(first_name, last_name, rname, middle_initial):
            continue

        loc_ok, loc_score = _location_matches(rloc, state, city) if rloc else (True, 0)

        death_year = 0
        if rdeath:
            yr_match = re.search(r"\d{4}", rdeath)
            if yr_match:
                death_year = int(yr_match.group())

        # Reject deaths before 2000
        if death_year > 0 and death_year < 2000:
            continue

        composite = loc_score * 10000 + death_year
        scored.append((composite, loc_score, r))

    if not scored:
        logger.debug("Newspapers.com: no quality matches for %s %s", first_name, last_name)
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    _, best_loc_score, best = scored[0]

    # Ambiguity filter
    if len(scored) > 1 and best_loc_score < 2:
        logger.info("Newspapers.com: %d ambiguous matches for %s %s — skipping",
                     len(scored), first_name, last_name)
        return None

    logger.info("Newspapers.com match: %s (date: %s, paper: %s, loc_score=%d)",
                 best.get("name"), best.get("date", ""), best.get("newspaper", ""),
                 best_loc_score)
    return {
        "confirmed_deceased": True,
        "date_of_death": best.get("date", ""),
        "source_url": best.get("url", page.url),
        "source_type": "newspapers",
        "full_name": best.get("name", ""),
        "birth_date": "",
        "last_residence": best.get("location", ""),
        "family_members": [],
        "obituary_text": best.get("snippet", None),
    }


async def _parse_newspapers_results(page) -> list[dict]:
    """Parse Newspapers.com search results.

    Result cards typically contain: title (with name), publication name,
    date, location, and a text snippet.

    NOTE: Selectors are based on Newspapers.com's typical structure.
    May need refinement after live selector discovery via test_ancestry.py --newspapers.
    """
    try:
        data = await page.evaluate("""() => {
            const results = [];

            // Try common result card selectors
            const cards = document.querySelectorAll(
                '.result-card, .search-result, [class*="SearchResult"], ' +
                '[class*="result-item"], [class*="clipping-card"], article.result'
            );

            for (const card of cards) {
                const text = (card.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text.length < 20) continue;

                const r = { raw_text: text.substring(0, 500) };

                // Extract title/name — typically in h2/h3/a.title or strong
                const titleEl = card.querySelector(
                    'h2, h3, .title, a[class*="title"], [class*="Title"], strong'
                );
                if (titleEl) r.name = titleEl.textContent.trim();

                // Extract newspaper name
                const paperEl = card.querySelector(
                    '.publication, [class*="publication"], [class*="paper"], ' +
                    '[class*="source"], .newspaper-name'
                );
                if (paperEl) r.newspaper = paperEl.textContent.trim();

                // Extract date
                const dateEl = card.querySelector(
                    '.date, [class*="date"], time, [class*="Date"]'
                );
                if (dateEl) r.date = dateEl.textContent.trim();

                // Extract location
                const locEl = card.querySelector(
                    '.location, [class*="location"], [class*="Location"]'
                );
                if (locEl) r.location = locEl.textContent.trim();

                // Extract snippet text
                const snippetEl = card.querySelector(
                    '.snippet, [class*="snippet"], [class*="preview"], .text-content, p'
                );
                if (snippetEl) r.snippet = snippetEl.textContent.trim();

                // Extract URL
                const linkEl = card.querySelector('a[href]');
                if (linkEl) r.url = linkEl.href;

                // Fallback: parse from raw text if structured elements missing
                if (!r.name && text) {
                    // Try to extract name from beginning of text
                    const nameMatch = text.match(/^([A-Z][a-zA-Z .'-]+?)\\s*[-–—|·]/);
                    if (nameMatch) r.name = nameMatch[1].trim();
                }

                if (!r.date && text) {
                    const dateMatch = text.match(
                        /(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\.?\\s+\\d{1,2},?\\s+\\d{4}/
                    );
                    if (dateMatch) r.date = dateMatch[0];
                }

                if (r.name || r.snippet) {
                    results.push(r);
                }
            }
            return results;
        }""")

        logger.debug("Newspapers.com: parsed %d results", len(data) if data else 0)
        return data or []
    except Exception as e:
        logger.warning("Newspapers.com result parse error: %s", e)
        return []


async def _parse_obituary_results(page) -> list[dict]:
    """Parse Ancestry obituary/death category results (global-results-card format).

    Each result card contains structured fields:
      Name: John Smith[maiden name]
      Spouse/Father/Mother: ...
      Birth: xx xxx 1940 Location
      Death: xx xxx 2012 Location
      Residence: City, County, State
    """
    try:
        data = await page.evaluate("""() => {
            // Only desktop results — mobile section duplicates everything
            const container = document.querySelector('.desktop-results-section') || document;
            const cards = container.querySelectorAll('[class*="searchResult"], .global-results-card');
            const results = [];
            for (const card of cards) {
                const text = (card.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text.length < 20) continue;

                const r = { raw_text: text.substring(0, 400) };

                // Extract Name field: "NameJohn Smith[Jane Doe]" or "NameJohn Smith"
                const nameMatch = text.match(/Name([A-Z][a-zA-Z .'-]+?)(?:\\[|Spouse|Father|Mother|Parent|Birth|Death|Residence|Relative)/);
                if (nameMatch) r.name = nameMatch[1].trim();

                // Extract death info: "Death12 Dec 2008 City, County, State" or "Deathxx xxx 1996"
                // Must NOT match "Death, Burial, Cemetery" category label — require digit/x after "Death"
                const deathMatch = text.match(/Death((?:[x\\d]{1,2}\\s+[x\\w]+\\s+[x\\d]{4})|(?:\\d{1,2}\\s+\\w+\\s+\\d{4})|(?:abt\\s+\\d{4}))\\s*(.*?)(?=Residence|Spouse|Father|Mother|Parent|Birth|Preview|Record|$)/);
                if (deathMatch) {
                    const dateStr = (deathMatch[1] || '').trim();
                    const yearMatch = dateStr.match(/\\d{4}/);
                    r.death_date = yearMatch ? yearMatch[0] : '';
                    // Full date if available (e.g., "12 Dec 2008")
                    if (/^\\d{1,2}\\s+\\w+\\s+\\d{4}$/.test(dateStr)) r.death_date_full = dateStr;
                    // Location after date
                    const locStr = (deathMatch[2] || '').trim();
                    r.death_location = locStr.replace(/^[x\\s]+/i, '').trim();
                }

                // Extract residence: "ResidenceCity, County, State"
                const resMatch = text.match(/Residence([A-Z][^]*?)(?=Preview|Record|$)/);
                if (resMatch) {
                    r.location = resMatch[1].replace(/^[x\\s]+/i, '').trim().substring(0, 100);
                }

                // Extract birth year
                const birthMatch = text.match(/Birth[x\\d]+\\s+[x\\w]+\\s+([x\\d]{4})/);
                if (birthMatch) {
                    const yr = birthMatch[1];
                    r.birth_date = /\\d{4}/.test(yr) ? yr : '';
                }

                // Extract record URL
                const link = card.querySelector('a[href*="/record"], a[href*="/discoveryui"]');
                if (link) r.record_url = link.href;

                // Extract family members from Spouse/Father/Mother fields
                r.family_members = [];
                const spouseMatch = text.match(/Spouse([A-Z][a-zA-Z .'-]+?)(?=Father|Mother|Birth|Death|Residence)/);
                if (spouseMatch) r.family_members.push({name: spouseMatch[1].trim(), relationship: 'spouse'});

                if (r.name && r.name.length > 3) {
                    // Deduplicate by record URL
                    const url = r.record_url || '';
                    const isDupe = results.some(x => x.record_url && x.record_url === url);
                    if (!isDupe) results.push(r);
                }
            }
            return results.slice(0, 20);
        }""")

        return data or []

    except Exception as e:
        logger.debug("Obituary result parsing error: %s", e)
        return []


# ── Search: Family trees ────────────────────────────────────────────


async def _search_family_trees(page, first_name: str, last_name: str) -> list[dict]:
    """Search family trees for heir identification. Returns list of family members."""
    if not _can_load_page() or _circuit_broken:
        return []

    # Navigate to family tree search
    tree_url = f"{ANCESTRY_URL}/search/?name={first_name}+{last_name}&treesTypes=on"
    await page.goto(tree_url, wait_until="domcontentloaded")
    _increment_page_loads()
    await _delay(3, 5)

    if await _check_blocked(page):
        return []

    # TODO: Parse family tree results — need to discover selectors
    # For now, return empty (family tree parsing is Phase 2)
    logger.debug("Family tree search not yet implemented")
    return []


# ── Result parsing ──────────────────────────────────────────────────


async def _parse_search_results(page) -> list[dict]:
    """Parse search results from Ancestry results page. Returns list of result dicts."""
    results = []

    # Wait for SPA to render results
    try:
        await page.wait_for_selector(
            "table tbody tr, .srp-row, [class*='searchResult'], [data-testid*='result']",
            timeout=8000,
        )
    except Exception:
        # Try waiting for any visible content change
        await _delay(2, 3)

    # Strategy 1: Table rows (SSDI results are typically in a table)
    rows = await page.query_selector_all("table.result tbody tr, table tbody tr")
    if rows:
        for row in rows[:10]:
            cells = await row.query_selector_all("td")
            text_parts = []
            for cell in cells:
                t = (await cell.text_content() or "").strip()
                if t:
                    text_parts.append(t)
            if text_parts:
                result = _parse_result_row(" | ".join(text_parts))
                if result:
                    results.append(result)
        if results:
            return results

    # Strategy 2: Generic result containers
    for sel in [".srp-row", "[class*='searchResult']", "[class*='conRes']"]:
        items = await page.query_selector_all(sel)
        if items:
            for item in items[:10]:
                text = (await item.text_content() or "").strip()
                text = " ".join(text.split())  # collapse whitespace
                if len(text) > 20:
                    result = _parse_result_row(text)
                    if result:
                        results.append(result)
            if results:
                return results

    # Strategy 3: Use page.evaluate to extract from Ancestry's Angular/React data model
    try:
        data = await page.evaluate("""() => {
            // Try extracting from the visible result rows
            const rows = document.querySelectorAll('#searchResults tr, .conRes, [class*="result"]');
            const results = [];
            for (const row of rows) {
                const text = row.textContent?.trim();
                if (text && text.length > 20 && text.length < 500) {
                    results.push(text.replace(/\\s+/g, ' '));
                }
            }
            return results.slice(0, 10);
        }""")
        for text in (data or []):
            result = _parse_result_row(text)
            if result:
                results.append(result)
    except Exception:
        pass

    return results


def _parse_result_row(text: str) -> dict | None:
    """Extract name, birth/death dates, and location from a result row text."""
    if not text or len(text) < 10:
        return None

    result = {"raw_text": text[:300]}

    # Extract name (usually first part)
    # Ancestry results: "John Smith | Birth: 1940 | Death: 2020 | Tennessee"
    parts = re.split(r"\s*[|·—]\s*", text)
    if parts:
        result["name"] = parts[0].strip()

    # Extract death date/year
    death_match = re.search(r"(?:death|died|d\.?)\s*:?\s*(\d{1,2}\s+\w+\s+\d{4}|\d{4})", text, re.IGNORECASE)
    if death_match:
        result["death_date"] = death_match.group(1).strip()

    # Extract birth date/year
    birth_match = re.search(r"(?:birth|born|b\.?)\s*:?\s*(\d{1,2}\s+\w+\s+\d{4}|\d{4})", text, re.IGNORECASE)
    if birth_match:
        result["birth_date"] = birth_match.group(1).strip()

    # Extract location
    loc_match = re.search(r"(?:Tennessee|TN|Knoxville|Knox\s+County|Blount)", text, re.IGNORECASE)
    if loc_match:
        result["location"] = loc_match.group(0).strip()

    # Only return if we got at least a name
    if result.get("name") and len(result["name"]) > 3:
        return result
    return None


def _name_matches(first: str, last: str, full_name: str, middle_initial: str = "") -> bool:
    """Check if a first/last name matches a full name string.

    Args:
        first: First name to match
        last: Last name to match
        full_name: Full name from SSDI result (e.g. "John H Smith")
        middle_initial: Optional middle initial to cross-check (e.g. "H")
    """
    full_lower = full_name.lower()
    first_lower = first.lower()
    last_lower = last.lower()

    # Both first and last must appear
    if first_lower not in full_lower or last_lower not in full_lower:
        return False

    # If we have a middle initial, check it doesn't CONFLICT
    # (allow missing middle in result, but reject different middle)
    if middle_initial:
        mi = middle_initial.lower().rstrip(".")
        # Extract middle part from full name
        full_parts = full_lower.split()
        if len(full_parts) >= 3:
            # Middle is between first and last
            for part in full_parts[1:-1]:
                part_clean = part.rstrip(".")
                if len(part_clean) == 1 and part_clean != mi:
                    return False  # Different middle initial
                if len(part_clean) > 1 and not part_clean.startswith(mi):
                    return False  # Different middle name

    return True


def _location_matches(location: str, state: str = "OH", city: str = "") -> tuple[bool, int]:
    """Check if an SSDI result location matches our target area.

    Returns (matches, score):
      - matches: False if location contradicts target (wrong state)
      - score: 0 = no location data, 1 = right state, 2 = right county/city
    """
    loc_lower = location.lower().strip()
    state_names = {
        "TN": "tennessee", "AL": "alabama", "GA": "georgia",
        "KY": "kentucky", "NC": "north carolina", "VA": "virginia",
    }
    state_name = state_names.get(state, state.lower())

    # No location data — can't confirm or deny
    if not loc_lower or loc_lower.startswith("x"):
        return True, 0

    # Must be in the right state
    if state_name not in loc_lower and state.lower() not in loc_lower:
        return False, 0

    # Right state — base score 1
    score = 1

    # Check county/city match for extra confidence
    # SSDI locations look like: "Knox, Tennessee, USA" or "Knoxville, Knox, Tennessee"
    city_lower = city.lower() if city else ""
    # Map cities to counties for Knox/Blount area
    county_aliases = {
        "knoxville": ["knox"],
        "farragut": ["knox"],
        "powell": ["knox"],
        "corryton": ["knox"],
        "maryville": ["blount"],
        "alcoa": ["blount"],
    }

    if city_lower:
        # Direct city name match
        if city_lower in loc_lower:
            score = 2
        # County match via city mapping
        counties = county_aliases.get(city_lower, [])
        for county in counties:
            if county in loc_lower:
                score = 2
                break

    return True, score


def _parse_owner_name(name: str) -> tuple[str, str, str]:
    """Parse owner name into (first, middle_initial, last).

    Handles: "John Smith", "John H Smith", "John H. Smith",
    "Stanley Darrell Keathley", "RITA STEWART"
    """
    # Strip common suffixes
    clean = re.sub(r",?\s*(?:Jr\.?|Sr\.?|II|III|IV)$", "", name.strip(), flags=re.IGNORECASE)
    parts = clean.split()

    if len(parts) < 2:
        return (parts[0] if parts else "", "", "")

    first = parts[0]
    last = parts[-1]

    # Middle initial: single letter or initial with period between first and last
    middle = ""
    if len(parts) >= 3:
        mid_part = parts[1].rstrip(".")
        if len(mid_part) == 1:
            middle = mid_part.upper()
        elif len(mid_part) > 1:
            # Full middle name — use first letter as initial
            middle = mid_part[0].upper()

    return first, middle, last


# ── Main lookup function ────────────────────────────────────────────


async def lookup_deceased(
    page,
    name: str,
    city: str = "",
    state: str = "TN",
) -> dict | None:
    """Search Ancestry for a deceased person. Returns structured result or None.

    Search cascade:
      1. SSDI death records (89M+ records, 1935-2014)
      2. Ancestry obituary collection (Death category 34)
      3. Newspapers.com obituary index (930M+ pages, shares All-Access SSO)

    Returns dict with keys:
      confirmed_deceased: bool
      date_of_death: str
      source_url: str
      source_type: "ssdi" | "obituary_collection" | "newspapers"
      full_name: str
      family_members: list[dict]  (empty for now)
      obituary_text: str | None
    """
    if _circuit_broken or not _can_load_page():
        return None

    # Parse name into components
    first_name, middle_initial, last_name = _parse_owner_name(name)
    if not first_name or not last_name:
        logger.debug("Ancestry: skipping unparseable name '%s'", name)
        return None

    # Tier 1: SSDI
    mi_label = f" {middle_initial}." if middle_initial else ""
    logger.info("Ancestry SSDI search: %s%s %s", first_name, mi_label, last_name)
    result = await _search_ssdi(page, first_name, last_name, state, middle_initial, city)
    if result:
        return result

    await _delay(2, 4)  # Extra delay between tiers

    # Tier 2: Obituary collection
    if _can_load_page() and not _circuit_broken:
        logger.info("Ancestry obituary search: %s %s", first_name, last_name)
        result = await _search_obituaries(page, first_name, last_name, state, city, middle_initial)
        if result:
            return result

    await _delay(2, 4)

    # Tier 3: Newspapers.com obituary index (shares All-Access SSO)
    if _can_load_page() and not _circuit_broken:
        logger.info("Newspapers.com obituary search: %s %s", first_name, last_name)
        result = await _search_newspapers(page, first_name, last_name, state, city, middle_initial)
        if result:
            return result

    logger.debug("Ancestry: no match for %s %s", first_name, last_name)
    return None
