"""Notice-gate CAPTCHA solving for tnpublicnotice.com (2Captcha).

Every notice detail page (Details.aspx) puts the notice body behind a CAPTCHA
gate that must be cleared before "View Notice" will reveal the text.

THE GATE CHANGED. Around 2026-07-13 the site migrated from Google reCAPTCHA v2
to CLOUDFLARE TURNSTILE. This module kept solving reCAPTCHA and injecting into a
`g-recaptcha-response` field the page no longer reads, so every solve was paid
for and thrown away and the scrape sat dead for 19 days while still reporting
success. `config.CAPTCHA_KIND` now selects the method and the response field:

    turnstile  -> solver.turnstile(...)  -> input[name="cf-turnstile-response"]
    recaptcha  -> solver.recaptcha(...)  -> textarea[name="g-recaptcha-response"]

Two rules learned from that outage, both enforced below:
  1. The sitekey is read off the LIVE PAGE first and only falls back to the
     configured constant. A key rotation is logged, never silent.
  2. Success requires positively seeing the notice body. There is no "the
     CAPTCHA markup is gone, so we must have passed" inference, that is the
     exact reasoning that reported 13 consecutive dead runs as successful.

Flow:
  1. Verify we're on the detail page (View Notice button exists)
  2. Read the gate kind + sitekey off the page
  3. Send websiteURL + sitekey to 2Captcha (~10-30s)
  4. Inject the token into the field the page actually submits
  5. Click "View Notice"
  6. Verify the notice content is now visible
"""

import asyncio
import logging

from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

import config
from config import MAX_RETRIES, SEL_VIEW_NOTICE_BUTTON

logger = logging.getLogger(__name__)

# Markers that mean the site refused us outright. Retrying burns money.
_BLOCK_TEXT = "You are not permitted to view public notices"

# Positive proof the gate cleared and the body rendered.
_CONTENT_SELECTOR = "text='Notice Content'"


class NoticeAccessBlocked(Exception):
    """The site refuses to serve notices to this IP.

    Distinct from a failed solve. The detail page carries no CAPTCHA at all,
    just "You are not permitted to view public notices from this computer at
    this time." Every subsequent notice will say the same thing, so the caller
    must abort the run rather than work down the results grid, retrying 50
    results x 3 attempts against a wall is how a blocked IP turned into a
    twenty-minute run that captured nothing.

    The fix is egress, not code: route through a residential or proxied IP.
    """


async def _detect_gate(page: Page) -> tuple[str, str | None]:
    """Read the gate kind and sitekey off the live page.

    Returns (kind, sitekey). Falls back to the configured values when the widget
    markup is absent (it frequently is until the CAPTCHA script finishes loading),
    but a sitekey that DISAGREES with config is logged loudly, because a silent
    key rotation is what this module exists to survive.
    """
    try:
        found = await page.evaluate(
            """() => {
                const ts = document.querySelector('.cf-turnstile[data-sitekey], '
                    + '[class*="cf-turnstile"][data-sitekey]');
                if (ts) return {kind: 'turnstile', key: ts.getAttribute('data-sitekey')};
                const rc = document.querySelector('.g-recaptcha[data-sitekey]');
                if (rc) return {kind: 'recaptcha', key: rc.getAttribute('data-sitekey')};
                // Turnstile often renders only its response input + iframe.
                if (document.querySelector('[name="cf-turnstile-response"], '
                    + 'iframe[src*="challenges.cloudflare.com"]')) {
                    return {kind: 'turnstile', key: null};
                }
                if (document.querySelector('textarea[name="g-recaptcha-response"], '
                    + 'iframe[src*="recaptcha"]')) {
                    return {kind: 'recaptcha', key: null};
                }
                return null;
            }"""
        )
    except Exception:
        found = None

    kind = config.CAPTCHA_KIND
    key = config.TURNSTILE_SITEKEY if kind == "turnstile" else config.RECAPTCHA_SITEKEY

    if found:
        page_kind = found.get("kind") or kind
        if page_kind != kind:
            logger.warning(
                "GATE CHANGED: page renders %s but CAPTCHA_KIND=%s. Solving %s. "
                "Set CAPTCHA_KIND=%s to make this permanent.",
                page_kind, kind, page_kind, page_kind,
            )
            kind = page_kind
            key = config.TURNSTILE_SITEKEY if kind == "turnstile" else config.RECAPTCHA_SITEKEY
        page_key = found.get("key")
        if page_key and page_key != key:
            logger.warning(
                "SITEKEY ROTATED: page serves %s, config has %s. Using the page's. "
                "Update TURNSTILE_SITEKEY to keep this out of the logs.",
                page_key, key,
            )
            key = page_key

    return kind, key


async def detect_captcha(page: Page) -> bool:
    """True when this page is showing a solvable gate.

    Useful on its own as a diagnostic, because there are three distinct states
    and they look alike from the outside:

      gate present   -> Turnstile/reCAPTCHA widget + View Notice button
      already open   -> notice body visible, nothing to solve
      IP refused     -> no widget at all, just "not permitted to view public
                        notices from this computer at this time"

    Raises NoticeAccessBlocked for the third, so a caller cannot mistake a
    blocked IP for "no CAPTCHA needed" and report a clean run.
    """
    if await page.query_selector(f":text('{_BLOCK_TEXT}')"):
        raise NoticeAccessBlocked(
            "Site refuses notices from this IP ('not permitted to view')."
        )
    if await page.query_selector(_CONTENT_SELECTOR):
        return False  # already through the gate
    kind, sitekey = await _detect_gate(page)
    has_button = await page.query_selector(SEL_VIEW_NOTICE_BUTTON) is not None
    logger.info("Gate: kind=%s sitekey=%s view_button=%s", kind, sitekey, has_button)
    return has_button


def _solve(kind: str, sitekey: str, url: str) -> str | None:
    """Solve via 2Captcha. Blocking, so callers run it off the event loop."""
    solver = TwoCaptcha(config.CAPTCHA_API_KEY)
    if kind == "turnstile":
        result = solver.turnstile(sitekey=sitekey, url=url)
    else:
        result = solver.recaptcha(sitekey=sitekey, url=url)
    if isinstance(result, dict):
        return result.get("code")
    return str(result) if result else None


# Turnstile submits its token in a hidden input named `cf-turnstile-response`,
# which ASP.NET reads off the form POST. In a headless browser the widget often
# never renders that input at all, so create it when it is missing.
_INJECT_TURNSTILE = """(token) => {
    const out = {set: 0, created: false};
    document.querySelectorAll(
        '[name="cf-turnstile-response"], [id^="cf-chl-widget"][id$="_response"]'
    ).forEach(el => { el.value = token; out.set++; });
    if (!out.set) {
        const form = document.querySelector('form');
        const el = document.createElement('input');
        el.type = 'hidden';
        el.name = 'cf-turnstile-response';
        el.id = 'cf-turnstile-response';
        el.value = token;
        (form || document.body).appendChild(el);
        out.created = true;
        out.set = 1;
    }
    // Fire any registered success callback (data-callback or turnstile.render).
    try {
        const host = document.querySelector('.cf-turnstile[data-callback]');
        if (host) {
            const fn = window[host.getAttribute('data-callback')];
            if (typeof fn === 'function') { fn(token); out.cb = true; }
        }
    } catch (e) { /* callback is a bonus, the form POST is what matters */ }
    return out;
}"""

_INJECT_RECAPTCHA = """(token) => {
    const out = {set: 0, cb: 0};
    let ta = document.getElementById('g-recaptcha-response')
        || document.querySelector('textarea[name="g-recaptcha-response"]');
    if (!ta) {
        const form = document.querySelector('form');
        ta = document.createElement('textarea');
        ta.name = 'g-recaptcha-response';
        ta.id = 'g-recaptcha-response';
        ta.style.display = 'none';
        (form || document.body).appendChild(ta);
        out.created = true;
    }
    ta.value = token;
    out.set = 1;
    try {
        const clients = ___grecaptcha_cfg.clients;
        Object.keys(clients).forEach(k => {
            (function walk(x) {
                if (!x || typeof x !== 'object') return;
                Object.values(x).forEach(v => {
                    if (v && typeof v === 'object') {
                        if (typeof v.callback === 'function') {
                            try { v.callback(token); out.cb++; } catch (e) {}
                        }
                        walk(v);
                    }
                });
            })(clients[k]);
        });
    } catch (e) { /* no grecaptcha config on the page */ }
    return out;
}"""


async def solve_captcha_and_view(page: Page) -> bool:
    """Clear the notice gate and click View Notice to reveal the full text.

    Retries up to MAX_RETRIES times. Returns True only when the notice body is
    positively visible.
    """
    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set, cannot clear the notice gate")
        return False

    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Check for an IP block before spending a solve against a wall.
            # NOTE: quoted text='...' is an EXACT match in Playwright and the live
            # message is longer ("...from this computer at this time."), so the
            # old exact selector never fired. :text() is a substring match.
            if await page.query_selector(f":text('{_BLOCK_TEXT}')"):
                raise NoticeAccessBlocked(
                    "Site refuses notices from this IP ('not permitted to view "
                    "public notices from this computer'). Route the run through "
                    "a proxy (SIFTSTACK_PROXY_URL), no code change will fix it."
                )

            # Already through the gate (solved earlier in this session)?
            if await page.query_selector(_CONTENT_SELECTOR):
                logger.info("Notice content already visible, no solve needed")
                return True

            # Confirm we're actually on a detail page before paying for a solve.
            try:
                await page.wait_for_selector(SEL_VIEW_NOTICE_BUTTON, timeout=15000)
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            kind, sitekey = await _detect_gate(page)
            if not sitekey:
                logger.error(
                    "No %s sitekey available (page did not expose one and config is empty)",
                    kind,
                )
                return False

            logger.warning(
                "Solving %s gate for %s (attempt %d/%d)",
                kind, page_url, attempt, MAX_RETRIES,
            )
            # 2Captcha's client is blocking and takes 10-30s. Run it in a thread
            # so the browser's event loop keeps servicing the page; a blocked loop
            # here is what makes long solves look like a hung navigation.
            token = await asyncio.to_thread(_solve, kind, sitekey, page_url)

            if not token:
                logger.warning("2Captcha returned an empty token (attempt %d)", attempt)
                continue

            injected = await page.evaluate(
                _INJECT_TURNSTILE if kind == "turnstile" else _INJECT_RECAPTCHA,
                token,
            )
            logger.debug("Token injected (%s): %s", kind, injected)

            # Brief pause for any callback-triggered DOM work.
            await asyncio.sleep(1)

            # Re-find the button: the callback may have re-rendered the form.
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                if await page.query_selector(_CONTENT_SELECTOR):
                    logger.warning("Gate cleared, callback auto-submitted the form")
                    return True
                logger.warning(
                    "View Notice button gone after token inject (attempt %d)", attempt
                )
                continue

            await view_btn.click()
            # domcontentloaded, not networkidle: the notice body is server-rendered
            # (present at DOM parse) and networkidle frequently never settles
            # through a residential proxy, producing a 60s false timeout.
            await page.wait_for_load_state("domcontentloaded")

            if await page.query_selector(_CONTENT_SELECTOR):
                logger.warning("Gate cleared, notice text visible")
                return True

            # There is deliberately NO "the CAPTCHA markup is gone, so we must
            # have succeeded" fallback. That inference reported success on every
            # one of 13 dead runs: after the Turnstile migration the old reCAPTCHA
            # string was always absent, so the check passed on pages holding no
            # notice at all. Success requires positively seeing the notice.
            if await page.query_selector(f":text('{_BLOCK_TEXT}')"):
                raise NoticeAccessBlocked(
                    "Site refused after a solve, this IP is blocked from notices."
                )

            logger.warning("Gate still closed after attempt %d", attempt)

        except NoticeAccessBlocked:
            raise  # never swallow: the caller must abort the whole run
        except Exception:
            logger.exception("Gate solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d gate attempts failed for %s", MAX_RETRIES, page_url)
    return False
