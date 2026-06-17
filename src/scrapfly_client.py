"""Scrapfly-backed fetcher for tnpublicnotice.com notice detail pages.

Replaces the in-house Playwright stack for the gated notice detail pages with a
hybrid: Scrapfly handles residential proxying + the headless browser (which is
what was actually failing on a non-residential IP) and captures the screenshot,
while the reCAPTCHA is solved with 2Captcha and the token injected into the page
before clicking "View Notice". Scrapfly's headless browser does NOT render the
reCAPTCHA widget's hidden response field, so we create it and drop the token in;
ASP.NET reads it from the form POST. Returns rendered HTML + a full-page
screenshot in one call.

Typical use:
    client = ScrapflyNoticeClient()
    if client.login(session="tnpn"):
        res = client.fetch_notice("541024", session="tnpn")
        if res.ok:
            html, png = res.content_html, res.screenshot_bytes

Requires SCRAPFLY_KEY. Every call is best-effort and returns a NoticeFetchResult
with an explicit error string so callers can log, retry, or fall back.
"""

import logging
from dataclasses import dataclass

import config
from config import (
    BASE_URL,
    LOGIN_URL,
    RECAPTCHA_SITEKEY,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_VIEW_NOTICE_BUTTON,
)

logger = logging.getLogger(__name__)

# Markers used to confirm a successful login / a cleared notice gate.
_DASHBOARD_MARKERS = ("ddlSavedSearches", "Smart Search", "Saved Search")
_NOTICE_MARKERS = ("Notice Content", "Notice Publish Date")
_GATE_MARKERS = ("recaptcha", "You must complete", "btnViewNotice")
_BLOCK_MARKERS = ("not permitted to view public notices",)


@dataclass
class NoticeFetchResult:
    """Outcome of one Scrapfly notice fetch."""
    ok: bool = False
    content_html: str = ""
    screenshot_bytes: bytes | None = None
    error: str = ""
    cost: float | None = None
    upstream_status: int | None = None
    url: str = ""


def detail_url_for(notice_id_or_url: str) -> str:
    """Return a session-agnostic detail URL for a notice ID (or pass a URL through).

    Past runs store session-bound URLs (``/(S(sid))/Details.aspx?...``) whose SID
    is long expired. Given a bare numeric ID we build ``Details.aspx?ID=<id>`` and
    let ASP.NET assign a fresh cookieless session within the logged-in Scrapfly
    session. A value that already looks like a URL is returned unchanged.
    """
    s = (notice_id_or_url or "").strip()
    if s.lower().startswith("http"):
        return s
    return f"{BASE_URL}/Details.aspx?ID={s}"


class ScrapflyNoticeClient:
    """Thin, robust wrapper over the Scrapfly SDK for this one site."""

    def __init__(self, key: str | None = None, country: str | None = None):
        self.key = key or config.SCRAPFLY_KEY
        if not self.key:
            raise ValueError("SCRAPFLY_KEY not set; cannot use the Scrapfly backend")
        self.country = country or config.SCRAPFLY_COUNTRY
        # Lazy import so the rest of the app runs without scrapfly-sdk installed.
        try:
            from scrapfly import ScrapflyClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "scrapfly-sdk not installed. Run: pip install 'scrapfly-sdk'"
            ) from exc
        self._client = ScrapflyClient(key=self.key)

    # ── low-level scrape with retry + error mapping ──────────────────

    def _scrape(self, scrape_config):
        """Run one scrape, returning (api_response | None, error_str)."""
        from scrapfly import (
            ScrapflyScrapeError,
            UpstreamHttpClientError,
            UpstreamHttpServerError,
        )

        try:
            resp = self._client.scrape(scrape_config)
            return resp, ""
        except ScrapflyScrapeError as exc:
            # ASP/CAPTCHA and other Scrapfly-side failures carry a code.
            return None, f"{getattr(exc, 'code', 'ScrapflyScrapeError')}: {exc}"
        except (UpstreamHttpClientError, UpstreamHttpServerError) as exc:
            return None, f"upstream_http_error: {exc}"
        except Exception as exc:  # network, SDK, etc.
            return None, f"{type(exc).__name__}: {exc}"

    def _config(self, url: str, session: str, *, js_scenario=None,
                screenshots=None, rendering_wait=None):
        from scrapfly import ScrapeConfig

        # Note: Scrapfly rejects a custom `timeout` while its auto-retry is on
        # ("Timeout is not customizable when retry is enabled"), so we rely on
        # Scrapfly's built-in retry + default timeout here.
        kwargs = dict(
            url=url,
            render_js=True,
            asp=True,
            country=self.country,
            session=session,
            rendering_wait=rendering_wait if rendering_wait is not None else config.SCRAPFLY_RENDER_WAIT_MS,
            raise_on_upstream_error=False,
        )
        if js_scenario:
            kwargs["js_scenario"] = js_scenario
        if screenshots:
            kwargs["screenshots"] = screenshots
        return ScrapeConfig(**kwargs)

    @staticmethod
    def _content(resp) -> str:
        try:
            return resp.scrape_result.get("content", "") or ""
        except Exception:
            return ""

    @staticmethod
    def _cost(resp):
        try:
            return resp.scrape_result.get("cost")
        except Exception:
            return None

    # ── login ────────────────────────────────────────────────────────

    def login(self, session: str) -> bool:
        """Log in to Smart Search within a Scrapfly session (cookies + sticky IP).

        Fills the login form via a JS scenario and submits. The forms-auth cookie
        then persists under the session for subsequent notice fetches.
        """
        if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
            logger.error("TNPN_EMAIL / TNPN_PASSWORD not set; cannot log in via Scrapfly")
            return False

        scenario = [
            {"wait_for_selector": {"selector": SEL_LOGIN_EMAIL, "timeout": 15000}},
            {"fill": {"selector": SEL_LOGIN_EMAIL, "value": config.TNPN_EMAIL}},
            {"fill": {"selector": SEL_LOGIN_PASSWORD, "value": config.TNPN_PASSWORD}},
            {"click": {"selector": SEL_LOGIN_SUBMIT}},
            {"wait": 5000},
        ]
        resp, err = self._scrape(
            self._config(LOGIN_URL, session, js_scenario=scenario, rendering_wait=2000)
        )
        if not resp:
            logger.error("Scrapfly login failed: %s", err)
            return False

        content = self._content(resp)
        if any(m in content for m in _DASHBOARD_MARKERS):
            logger.info("Scrapfly login successful (session=%s)", session)
            return True
        logger.error(
            "Scrapfly login did not reach the dashboard (session=%s). "
            "Check credentials / login selectors.", session,
        )
        return False

    # ── notice fetch ──────────────────────────────────────────────────

    # JS injected into Scrapfly's browser: drop the 2Captcha token into the
    # reCAPTCHA response field, CREATING that field if the widget never rendered
    # it (which is the case in Scrapfly's headless browser). ASP.NET reads it
    # from the form POST when "View Notice" is clicked.
    _INJECT_TEMPLATE = (
        'var t="__TOKEN__";var out={};'
        'var ta=document.querySelector(\'textarea[name="g-recaptcha-response"]\')'
        '||document.getElementById("g-recaptcha-response");'
        'if(!ta){var form=document.querySelector("form");ta=document.createElement("textarea");'
        'ta.name="g-recaptcha-response";ta.id="g-recaptcha-response";ta.style.display="none";'
        '(form||document.body).appendChild(ta);out.created=true;}'
        'ta.value=t;out.setLen=ta.value.length;'
        'var n=0;try{var cs=___grecaptcha_cfg.clients;Object.keys(cs).forEach(function(k){'
        '(function f(x){if(!x||typeof x!=="object")return;Object.values(x).forEach(function(v){'
        'if(v&&typeof v==="object"){if(typeof v.callback==="function"){try{v.callback(t);n++}catch(e){}}f(v)}})})(cs[k])});}'
        'catch(e){}out.cb=n;return JSON.stringify(out);'
    )

    @staticmethod
    def _solve_recaptcha(url: str) -> str | None:
        """Solve the page's reCAPTCHA v2 via 2Captcha; return the token or None."""
        if not config.CAPTCHA_API_KEY:
            logger.error("CAPTCHA_API_KEY not set; Scrapfly backend needs 2Captcha to clear the gate")
            return None
        try:
            from twocaptcha import TwoCaptcha
            sol = TwoCaptcha(config.CAPTCHA_API_KEY).recaptcha(sitekey=RECAPTCHA_SITEKEY, url=url)
            return sol.get("code") if isinstance(sol, dict) else str(sol)
        except Exception as exc:
            logger.warning("  2Captcha solve error: %s", exc)
            return None

    def _gate_scenario(self, token: str) -> list:
        """JS scenario: wait for render, inject token, click View Notice, settle."""
        inject = self._INJECT_TEMPLATE.replace("__TOKEN__", token)
        return [
            {"wait_for_selector": {"selector": SEL_VIEW_NOTICE_BUTTON, "timeout": 15000}},
            {"wait": config.SCRAPFLY_RENDER_WAIT_MS},  # let the reCAPTCHA script load
            {"execute": {"script": inject}},
            {"click": {"selector": SEL_VIEW_NOTICE_BUTTON}},
            {"wait_for_navigation": {"timeout": 10000}},  # ASP.NET postback (max 10s)
            {"wait": 2500},
        ]

    def fetch_notice(
        self,
        notice_id_or_url: str,
        session: str,
        want_screenshot: bool = True,
    ) -> NoticeFetchResult:
        """Fetch one notice detail page: clear the gate, return HTML + screenshot.

        Solves the reCAPTCHA with 2Captcha, injects the token, clicks "View
        Notice", and reads the revealed legal text. Retries up to
        SCRAPFLY_MAX_RETRIES extra times (a fresh token + rotated IP usually
        succeeds) when the gate does not clear.
        """
        url = detail_url_for(notice_id_or_url)
        screenshots = {"notice": "fullpage"} if want_screenshot else None

        last_err = ""
        for attempt in range(1, config.SCRAPFLY_MAX_RETRIES + 2):
            token = self._solve_recaptcha(url)
            if not token:
                last_err = "captcha_solve_failed"
                logger.warning("  2Captcha solve failed (attempt %d) for %s", attempt, url)
                continue

            resp, err = self._scrape(
                self._config(
                    url, session, js_scenario=self._gate_scenario(token), screenshots=screenshots,
                )
            )
            if not resp:
                last_err = err
                logger.warning("  Scrapfly fetch error (attempt %d): %s", attempt, err)
                continue

            content = self._content(resp)
            cost = self._cost(resp)
            upstream = getattr(resp, "upstream_status_code", None)

            if any(m in content for m in _BLOCK_MARKERS):
                # Not authenticated for this session; caller should re-login.
                return NoticeFetchResult(
                    ok=False, content_html=content, error="not_authenticated",
                    cost=cost, upstream_status=upstream, url=url,
                )

            if any(m in content for m in _NOTICE_MARKERS):
                png = self._download_screenshot(resp) if want_screenshot else None
                return NoticeFetchResult(
                    ok=True, content_html=content, screenshot_bytes=png,
                    cost=cost, upstream_status=upstream, url=url,
                )

            last_err = "gate_not_cleared (no Notice Content in response)"
            logger.warning("  Scrapfly gate not cleared (attempt %d) for %s", attempt, url)

        return NoticeFetchResult(ok=False, error=last_err or "unknown", url=url)

    # ── screenshot download ──────────────────────────────────────────

    def _download_screenshot(self, resp) -> bytes | None:
        """Download the full-page screenshot PNG referenced in the response."""
        try:
            shots = resp.scrape_result.get("screenshots") or {}
        except Exception:
            shots = {}
        if not shots:
            logger.debug("  No screenshots in Scrapfly response")
            return None
        # We requested a single screenshot named "notice"; tolerate any key.
        meta = shots.get("notice") or next(iter(shots.values()), None)
        if not isinstance(meta, dict) or not meta.get("url"):
            return None
        import requests

        try:
            r = requests.get(meta["url"], params={"key": self.key}, timeout=60)
            r.raise_for_status()
            return r.content
        except Exception as exc:
            logger.warning("  Screenshot download failed: %s", exc)
            return None


# ── convenience: log in once, fetch many ──────────────────────────────


def fetch_notices(notice_ids, *, session: str = "tnpn", want_screenshot: bool = True):
    """Log in once, then yield (notice_id, NoticeFetchResult) for each ID.

    Skips the whole batch (yields not-ok results) if login fails, so callers get
    a result per input either way.
    """
    client = ScrapflyNoticeClient()
    logged_in = client.login(session=session)
    for nid in notice_ids:
        if not logged_in:
            yield nid, NoticeFetchResult(ok=False, error="login_failed")
            continue
        res = client.fetch_notice(nid, session=session, want_screenshot=want_screenshot)
        # One automatic re-login if the session dropped mid-batch.
        if not res.ok and res.error == "not_authenticated":
            logged_in = client.login(session=session)
            if logged_in:
                res = client.fetch_notice(nid, session=session, want_screenshot=want_screenshot)
        yield nid, res
