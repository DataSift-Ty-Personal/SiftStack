"""2Captcha integration for solving reCAPTCHA v2 on tnpublicnotice.com.

Every notice detail page (Details.aspx) requires solving a reCAPTCHA v2
checkbox before the full notice text is revealed.  Flow:
  1. Verify we're on the detail page (View Notice button exists)
  2. Send websiteURL + sitekey to 2Captcha API
  3. 2Captcha returns a g-recaptcha-response token (~10-30s)
  4. Inject token into the page's hidden textarea
  5. Click "View Notice" button to submit
  6. Verify the notice content is now visible
"""

import asyncio
import logging

from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

import config
from config import MAX_RETRIES, RECAPTCHA_SITEKEY, SEL_VIEW_NOTICE_BUTTON

logger = logging.getLogger(__name__)


async def solve_captcha_and_view(page: Page) -> bool:
    """Solve reCAPTCHA v2 and click View Notice to reveal the full text.

    Retries up to MAX_RETRIES times on failure.
    Returns True if the notice text is now visible, False otherwise.
    """
    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set — cannot solve CAPTCHA")
        return False

    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Check for IP block message before wasting time on CAPTCHA
            block_msg = await page.query_selector(
                "text='You are not permitted to view public notices'"
            )
            if block_msg:
                logger.error(
                    "IP BLOCKED: Site says 'not permitted to view' — "
                    "need residential proxy or different IP"
                )
                return False  # Bail immediately, don't retry

            # Check if the notice content is already visible (no CAPTCHA needed)
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.info("Notice content already visible — no CAPTCHA needed")
                return True

            # Wait for the View Notice button to confirm we're on the detail page.
            # This prevents wasting CAPTCHA solves if the page didn't load properly.
            try:
                view_btn = await page.wait_for_selector(
                    SEL_VIEW_NOTICE_BUTTON, timeout=15000
                )
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            # Solve reCAPTCHA v2 via 2Captcha API (~10-30s)
            logger.warning(
                "Solving reCAPTCHA for %s (attempt %d/%d)", page_url, attempt, MAX_RETRIES
            )
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            result = solver.recaptcha(
                sitekey=RECAPTCHA_SITEKEY,
                url=page_url,
            )
            token = result.get("code") if isinstance(result, dict) else str(result)

            if not token:
                logger.warning("2Captcha returned empty token (attempt %d)", attempt)
                continue

            # Inject the token into the page's hidden reCAPTCHA response field
            await page.evaluate(
                """(token) => {
                    const el = document.getElementById('g-recaptcha-response');
                    if (el) { el.value = token; el.style.display = 'block'; }
                    const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta) { ta.value = token; ta.style.display = 'block'; }
                    // Trigger the reCAPTCHA callback if it exists
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        const clients = ___grecaptcha_cfg.clients;
                        if (clients) {
                            Object.keys(clients).forEach(key => {
                                const client = clients[key];
                                const findCallback = (obj) => {
                                    if (!obj || typeof obj !== 'object') return;
                                    Object.values(obj).forEach(v => {
                                        if (typeof v === 'object' && v !== null) {
                                            if (typeof v.callback === 'function') {
                                                v.callback(token);
                                            }
                                            findCallback(v);
                                        }
                                    });
                                };
                                findCallback(client);
                            });
                        }
                    }
                }""",
                token,
            )

            # Brief pause for any callback-triggered actions
            await asyncio.sleep(1)

            # Click the "View Notice" button to submit with the solved CAPTCHA.
            # Re-find the button in case the callback caused a DOM update.
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                # Callback may have auto-submitted — check if content is visible
                content_el = await page.query_selector("text='Notice Content'")
                if content_el:
                    logger.warning("CAPTCHA solved — callback auto-submitted form")
                    return True
                logger.warning("View Notice button gone after token inject (attempt %d)", attempt)
                continue

            await view_btn.click()
            # Use domcontentloaded, not networkidle: the notice content is
            # server-rendered (present at DOM parse), and networkidle frequently
            # never settles through a residential proxy (60s false timeout).
            await page.wait_for_load_state("domcontentloaded")

            # Verify the notice content is now visible
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning("CAPTCHA solved — notice text visible")
                return True

            # Fallback: check if CAPTCHA message is gone
            captcha_msg = await page.query_selector(
                "text='You must complete the reCAPTCHA'"
            )
            if not captcha_msg:
                logger.warning("CAPTCHA solved — gate cleared")
                return True

            logger.warning("CAPTCHA still present after attempt %d", attempt)

        except Exception:
            logger.exception("CAPTCHA solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d CAPTCHA attempts failed for %s", MAX_RETRIES, page_url)
    return False
