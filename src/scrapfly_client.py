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
# Scrapfly-side ban signatures, mirroring scrapfly_browser._ASP_BAN_MARKERS.
_ASP_BAN_MARKERS = ("shield_protection_failed", "asp::", "all proxy rotations exhausted")


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

    def __init__(self, key: str | None = None, country: str | None = None,
                 proxy_pool: str | None = None):
        self.key = key or config.SCRAPFLY_KEY
        if not self.key:
            raise ValueError("SCRAPFLY_KEY not set; cannot use the Scrapfly backend")
        self.country = country or config.SCRAPFLY_COUNTRY
        # Residential proxying is the entire point of routing this site through
        # Scrapfly: tnpublicnotice serves a pre-CAPTCHA block page ("You are not
        # permitted to view public notices from this computer at this time") to
        # datacenter IPs. This was never passed through, so the client silently
        # ran on Scrapfly's default datacenter pool and hit the same block.
        self.proxy_pool = proxy_pool or getattr(
            config, "SCRAPFLY_PROXY_POOL", "public_residential_pool")
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
            code = str(getattr(exc, "code", "") or exc).lower()
            # Quota is terminal: the SDK maps ERR::SCRAPE::QUOTA_LIMIT_REACHED to
            # ScrapflyScrapeError (not TooManyRequest), so without this it gets
            # retried pointlessly and reads like an ordinary scrape failure.
            if "quota_limit_reached" in code:
                return None, "quota_exhausted: Scrapfly plan quota is used up"
            if any(m in code for m in _ASP_BAN_MARKERS):
                return None, f"asp_shield_failed: {exc}"
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
            proxy_pool=self.proxy_pool,
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
    # Turnstile puts its token in a hidden input named `cf-turnstile-response`
    # (ASP.NET reads it from the form POST). Set every candidate field, creating
    # one if the widget never rendered in the headless browser, and fire any
    # registered callback.
    _INJECT_TURNSTILE = (
        'var t="__TOKEN__";var out={set:0};'
        'document.querySelectorAll(\'[name="cf-turnstile-response"],'
        '[id^="cf-chl-widget"][id$="_response"]\').forEach(function(el){el.value=t;out.set++;});'
        'if(!out.set){var form=document.querySelector("form");'
        'var el=document.createElement("input");el.type="hidden";'
        'el.name="cf-turnstile-response";el.id="cf-turnstile-response";el.value=t;'
        '(form||document.body).appendChild(el);out.created=true;out.set=1;}'
        'try{if(window.turnstile&&turnstile.getResponse){out.tsLen=(turnstile.getResponse()||"").length;}}catch(e){}'
        'return JSON.stringify(out);'
    )

    # Legacy Google reCAPTCHA injection, kept for the reCAPTCHA fallback path.
    _INJECT_RECAPTCHA = (
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

    @property
    def _INJECT_TEMPLATE(self) -> str:
        return (self._INJECT_TURNSTILE
                if config.CAPTCHA_KIND == "turnstile" else self._INJECT_RECAPTCHA)

    def fetch_notice_via_search(self, keyword: str, index: int = 0, *,
                                session: str, county: str | None = None,
                                want_screenshot: bool = True) -> NoticeFetchResult:
        """Search and open the Nth result inside ONE Scrapfly call.

        This is the only flow that actually returns notice content. Every
        Scrapfly scrape gets a fresh ASP.NET cookieless session (the /(S(sid))/
        path changes per call), so a detail fetch issued as a separate call
        lands in a session that never ran a search, and the server returns a
        fully unpopulated shell: blank publish date, an empty download link, and
        only the "Web display limited to 1,000 characters" disclaimer. One
        browser context is one session, so the whole walk has to be one call.

        Budget note: Scrapfly caps a scenario at 30s total, so the waits below
        are deliberately tight.
        """
        token = self._solve_recaptcha(BASE_URL + "/Search.aspx", config.TURNSTILE_SITEKEY)
        if not token:
            return NoticeFetchResult(ok=False, error="captcha_solve_failed",
                                     url=BASE_URL + "/Search.aspx")

        pick = (
            "var b=document.querySelectorAll('input.viewButton');"
            "if(!b.length) return 'no results';"
            f"var el=b[{int(index)}];if(!el) return 'index out of range';"
            "var m=(el.getAttribute('onclick')||'')"
            ".match(/Details\\.aspx\\?SID=[a-z0-9]+&ID=\\d+/);"
            "if(!m) return 'no detail href';"
            "window.location.href=m[0];return m[0];"
        )
        # The county filter MUST match whatever enumerated the ids, or index N
        # here points at a different notice than index N in the enumeration.
        county_js = "return 'no county filter';"
        idx_map = {"Knox": 46, "Blount": 4}
        if county in idx_map:
            county_js = (
                "var c=document.getElementById("
                f"'ctl00_ContentPlaceHolder1_as1_lstCounty_{idx_map[county]}');"
                "if(c){c.checked=true;}return c?'county set':'county missing';")

        scen = [
            {"wait_for_selector": {"selector": "#ctl00_ContentPlaceHolder1_as1_txtSearch",
                                   "timeout": 8000}},
            {"fill": {"selector": "#ctl00_ContentPlaceHolder1_as1_txtSearch",
                      "value": keyword}},
            {"click": {"selector": "#ctl00_ContentPlaceHolder1_as1_btnGo"}},
            {"wait": 6000},
            {"execute": {"script": pick}},
            # A fixed wait, deliberately. wait_for_selector FAILS the whole
            # scenario when the button is late, and adding its timeout to the
            # budget tipped the run past Scrapfly's hard 30s scenario cap. The
            # occasional late render is handled by retrying the call instead.
            {"wait": 6500},
            {"execute": {"script": self._INJECT_TEMPLATE.replace("__TOKEN__", token)}},
            {"click": {"selector": SEL_VIEW_NOTICE_BUTTON}},
            {"wait": 5000},
        ]
        shots = {"notice": "fullpage"} if want_screenshot else None
        resp, err = None, ""
        for _ in range(config.SCRAPFLY_MAX_RETRIES + 1):
            resp, err = self._scrape(self._config(
                BASE_URL + "/Search.aspx", session,
                js_scenario=scen, screenshots=shots, rendering_wait=1500))
            if resp:
                break
            if "quota_exhausted" in err:
                break
            logger.warning("  search-walk attempt failed: %s", err[:120])
        if not resp:
            return NoticeFetchResult(ok=False, error=err, url=BASE_URL + "/Search.aspx")

        content = self._content(resp)
        landed = ""
        try:
            landed = (resp.scrape_result or {}).get("url", "") or ""
        except Exception:
            pass
        if any(m in content for m in _BLOCK_MARKERS):
            return NoticeFetchResult(ok=False, content_html=content,
                                     error="ip_blocked", url=landed)
        if any(m in content for m in _NOTICE_MARKERS):
            png = self._download_screenshot(resp) if want_screenshot else None
            return NoticeFetchResult(ok=True, content_html=content, screenshot_bytes=png,
                                     cost=self._cost(resp), url=landed)
        return NoticeFetchResult(ok=False, content_html=content,
                                 error="gate_not_cleared", url=landed)

    def fetch_binary(self, url: str, session: str) -> bytes | None:
        """Fetch a session-bound file (the notice PDF) inside the same session.

        PDFDocument.aspx is scoped to the ASP.NET session, so a bare requests.get
        returns an HTML error page rather than a PDF ("No /Root object").
        """
        from scrapfly import ScrapeConfig
        try:
            resp = self._client.scrape(ScrapeConfig(
                url=url, render_js=False, asp=True, country=self.country,
                session=session, proxy_pool=self.proxy_pool,
                raise_on_upstream_error=False))
        except Exception as exc:
            logger.warning("PDF fetch failed: %s: %s", type(exc).__name__, exc)
            return None
        try:
            body = (resp.scrape_result or {}).get("content") or ""
        except Exception:
            return None
        # Scrapfly hands back bytes, str, or a file-like depending on content type.
        if hasattr(body, "getvalue"):
            body = body.getvalue()
        elif hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            return body if body.lstrip().startswith(b"%PDF") else body
        if isinstance(body, str):
            if body.lstrip().startswith("%PDF"):
                return body.encode("latin-1", "ignore")
            logger.warning("PDF fetch returned non-PDF text (%d chars)", len(body))
        return None

    @staticmethod
    def _page_sitekey(html: str) -> str | None:
        """Read the gate sitekey off the live page, so a rotation is not silent."""
        import re
        m = re.search(r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"', html or "")
        if not m:
            m = re.search(r'data-sitekey="([^"]+)"', html or "")
        return m.group(1) if m else None

    @staticmethod
    def _solve_recaptcha(url: str, sitekey: str | None = None) -> str | None:
        """Solve the page's gate CAPTCHA via 2Captcha; return the token or None.

        The site runs Cloudflare Turnstile now, which is a different 2Captcha
        method and a different response field than reCAPTCHA. `sitekey` lets the
        caller pass the key scraped from the live page so a key rotation does not
        silently break the scrape the way the reCAPTCHA-to-Turnstile migration did.
        """
        if not config.CAPTCHA_API_KEY:
            logger.error("CAPTCHA_API_KEY not set; Scrapfly backend needs 2Captcha to clear the gate")
            return None
        try:
            from twocaptcha import TwoCaptcha
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            if config.CAPTCHA_KIND == "turnstile":
                sol = solver.turnstile(sitekey=sitekey or config.TURNSTILE_SITEKEY, url=url)
            else:
                sol = solver.recaptcha(sitekey=sitekey or RECAPTCHA_SITEKEY, url=url)
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
        last_content = ""
        for attempt in range(1, config.SCRAPFLY_MAX_RETRIES + 2):
            # Probe the page BEFORE paying for a solve. This does three jobs:
            # it detects an IP block without burning 2Captcha credit, it reads
            # the live sitekey (so a key rotation cannot silently break us), and
            # it confirms the gate is actually present.
            probe, perr = self._scrape(self._config(url, session))
            if not probe:
                last_err = perr
                logger.warning("  Scrapfly probe error (attempt %d): %s", attempt, perr)
                if "quota_exhausted" in perr:
                    break          # terminal, retrying cannot help
                continue
            probe_html = self._content(probe)
            last_content = probe_html or last_content

            if any(m in probe_html for m in _BLOCK_MARKERS):
                return NoticeFetchResult(
                    ok=False, content_html=probe_html, error="ip_blocked",
                    cost=self._cost(probe), url=url,
                )
            if any(m in probe_html for m in _NOTICE_MARKERS):
                # Occasionally the gate is already satisfied for this session.
                return NoticeFetchResult(
                    ok=True, content_html=probe_html, cost=self._cost(probe), url=url,
                )

            sitekey = self._page_sitekey(probe_html)
            token = self._solve_recaptcha(url, sitekey)
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
            last_content = content
            logger.warning("  Scrapfly gate not cleared (attempt %d) for %s", attempt, url)

        # Return the last body we saw. Without this the caller gets an empty
        # result and cannot tell an IP block from a captcha miss from an empty
        # ASP.NET shell, which is what made this failure mode invisible.
        return NoticeFetchResult(ok=False, content_html=last_content,
                                 error=last_err or "unknown", url=url)

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
