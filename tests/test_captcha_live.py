"""Live diagnostic: login -> open a notice -> clear the gate -> verify text.

Not a pytest module (no test functions); it is a hands-on probe kept under
tests/ so it sits next to the suite. Run it from the project root:

    .venv/Scripts/python.exe tests/test_captcha_live.py

The gate is Cloudflare TURNSTILE, not reCAPTCHA (the site migrated around
2026-07-13). It also runs through the configured proxy: tnpublicnotice.com
refuses notice pages outright from most IPs, and this probe is exactly where
that gets mistaken for "no CAPTCHA needed".
"""

import asyncio
import logging
import sys
import os

# Add src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from playwright.async_api import async_playwright

from config import (
    LOGIN_URL,
    SMART_SEARCH_URL,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_SAVED_SEARCHES_DROPDOWN,
    SEL_VIEW_BUTTON_PATTERN,
    TNPN_EMAIL,
    TNPN_PASSWORD,
    CAPTCHA_API_KEY,
)
from captcha_solver import detect_captcha, solve_captcha_and_view

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_captcha")


async def main():
    # Pre-flight checks
    if not TNPN_EMAIL or not TNPN_PASSWORD:
        logger.error("TNPN_EMAIL / TNPN_PASSWORD not set in .env")
        return
    if not CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set in .env")
        return

    logger.info("2Captcha API key: %s...%s", CAPTCHA_API_KEY[:4], CAPTCHA_API_KEY[-4:])

    from proxy_resolver import describe as describe_proxy, resolve_proxy_url

    logger.info("Egress: %s", describe_proxy())
    proxy_url = resolve_proxy_url()
    launch: dict = {"headless": False}  # visible for debugging
    if proxy_url:
        from urllib.parse import urlparse

        parsed = urlparse(proxy_url)
        launch["proxy"] = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            "username": parsed.username,
            "password": parsed.password,
        }

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch)
        context = await browser.new_context()
        context.set_default_timeout(60_000)
        page = await context.new_page()

        # ── Step 1: Login ──────────────────────────────────────────
        logger.info("Step 1: Logging in...")
        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("networkidle")

        await page.fill(SEL_LOGIN_EMAIL, TNPN_EMAIL)
        await page.fill(SEL_LOGIN_PASSWORD, TNPN_PASSWORD)
        await page.click(SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("networkidle")

        if "smartsearch" not in page.url.lower():
            logger.error("Login failed — landed on %s", page.url)
            await browser.close()
            return

        logger.info("Login successful — on dashboard")

        # ── Step 2: Run a saved search to get results ──────────────
        logger.info("Step 2: Selecting 'Foreclosure V2 Knox' saved search...")
        # The dropdown selection triggers an ASP.NET postback → full page navigation.
        # Must wait for navigation explicitly, not just networkidle.
        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(SEL_SAVED_SEARCHES_DROPDOWN, label="Foreclosure V2 Knox")
        await asyncio.sleep(2)

        logger.info("On results page: %s", page.url)

        # ── Step 3: Click first View button to open a notice ───────
        logger.info("Step 3: Clicking first View button...")
        view_buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
        if not view_buttons:
            logger.error("No View buttons found on results page")
            await browser.close()
            return

        logger.info("Found %d View buttons", len(view_buttons))
        await view_buttons[0].click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        logger.info("On notice page: %s", page.url)

        # ── Step 4: Check for CAPTCHA ──────────────────────────────
        has_captcha = await detect_captcha(page)
        logger.info("Step 4: CAPTCHA detected: %s", has_captcha)

        if not has_captcha:
            logger.warning("No CAPTCHA found — maybe already solved?")
            body = await page.inner_text("body")
            logger.info("Page text (first 500 chars): %s", body[:500])
            await browser.close()
            return

        # ── Step 5: Solve CAPTCHA ──────────────────────────────────
        logger.info("Step 5: Solving reCAPTCHA via 2Captcha...")
        logger.info("This will take ~10-30 seconds...")

        success = await solve_captcha_and_view(page)

        if success:
            logger.info("SUCCESS — CAPTCHA solved and notice text visible!")
            # Dump the FULL page text so we can see the notice structure
            body = await page.inner_text("body")
            logger.info("=== FULL PAGE TEXT ===\n%s\n=== END ===", body)
        else:
            logger.error("FAILED — CAPTCHA solve did not work")
            body = await page.inner_text("body")
            logger.info("Page text after failure:\n%s", body[:500])

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
