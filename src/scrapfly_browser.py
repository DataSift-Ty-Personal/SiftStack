"""Generic Scrapfly ASP browser fetcher -- the deep-prospecting L3 fallback.

The Primary Path (Enformion) resolves heirs from an API, but some gaps live
behind a Cloudflare / JavaScript wall that plain HTTP fetches -- and the
sandboxed WebFetch used by research agents -- cannot clear: county assessor and
deed portals, FindAGrave / Legacy obituaries, court record pages, and
people-search detail pages. This module routes those fetches through Scrapfly's
Anti-Scraping Protection (asp=True) + a headless browser (render_js=True) + a
residential proxy, which clears the JS challenges.

Where it works, and where it does not (validated live, July 2026):
  - County / records / genealogy portals (Knox assessor datalet, FindAGrave,
    Legacy, court info pages): ASP clears them reliably. HIGH value -- this is
    the sweet spot.
  - Hardened people-search aggregators (TruePeopleSearch, FastPeopleSearch):
    ASP is frequently IP-banned (SHIELD_PROTECTION_FAILED) after the first hit.
    Treat as best-effort; prefer Enformion for heirs and reach relatives through
    a known contact instead of grinding these sites.
  - Records a county does not publish online (Knox County TN estate/probate
    cases, Register-of-Deeds document images behind a paid subscription):
    Scrapfly cannot retrieve what is not online. That is a data-availability
    limit, not a Scrapfly failure -- fall back to a phone/in-person request.

Failure is detected by upstream HTTP status + a Cloudflare-challenge marker scan
(NOT by any single field). Every call returns a FetchResult, so callers can log,
retry, or fall back. Reuses the same asp+render_js core as scrapfly_client.py
(which is the tnpublicnotice-specific notice fetcher); this one is URL-generic.

CLI:
    python src/scrapfly_browser.py "https://www.findagrave.com/memorial/search?..."
    python src/scrapfly_browser.py "<url>" --raw --out page.html --wait 6000
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)

# Markers that mean the page is still the anti-bot interstitial, not the target.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "enable javascript and cookies",
    "attention required",
    "please verify you are a human",
    "ray id",
)

# ASP shield exhaustion -> the site banned every proxy rotation. Distinct from a
# transient error: retrying rarely helps and burns credits, so we surface it.
_ASP_BAN_MARKERS = ("shield_protection_failed", "asp::", "all proxy rotations exhausted")


@dataclass
class FetchResult:
    """Outcome of one Scrapfly browser fetch."""
    ok: bool = False
    url: str = ""
    content: str = ""            # raw HTML
    text: str = ""              # visible text (tags stripped), when want_text
    upstream_status: int | None = None
    cost: int | float | None = None
    blocked_reason: str = ""     # "cloudflare_challenge" | "asp_shield_failed" | ""
    error: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)


def visible_text(html: str) -> str:
    """Strip scripts/styles/tags and collapse whitespace to readable text."""
    if not html:
        return ""
    t = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", t).strip()


class ScrapflyBrowserClient:
    """URL-generic Scrapfly wrapper: asp + render_js + residential, with retries."""

    def __init__(self, key: str | None = None, country: str | None = None,
                 proxy_pool: str | None = None):
        self.key = key or config.SCRAPFLY_KEY
        if not self.key:
            raise ValueError("SCRAPFLY_KEY not set; cannot use the Scrapfly backend")
        self.country = country or config.SCRAPFLY_COUNTRY
        self.proxy_pool = proxy_pool or getattr(
            config, "SCRAPFLY_PROXY_POOL", "public_residential_pool")
        try:
            from scrapfly import ScrapflyClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError("scrapfly-sdk not installed. Run: pip install 'scrapfly-sdk'") from exc
        self._client = ScrapflyClient(key=self.key)

    def fetch(
        self,
        url: str,
        *,
        render_js: bool = True,
        asp: bool = True,
        residential: bool = True,
        rendering_wait: int | None = None,
        js_scenario: list | None = None,
        retries: int | None = None,
        want_text: bool = True,
    ) -> FetchResult:
        """Fetch one URL through Scrapfly ASP. Returns a FetchResult (never raises).

        Retries transient errors up to `retries` extra times (default
        SCRAPFLY_MAX_RETRIES). An ASP shield ban is NOT retried past one attempt
        (rotations are already exhausted upstream) -- it is returned as blocked.
        """
        from scrapfly import ScrapeConfig
        from scrapfly import (
            ScrapflyScrapeError,
            UpstreamHttpClientError,
            UpstreamHttpServerError,
        )

        extra = config.SCRAPFLY_MAX_RETRIES if retries is None else retries
        wait = config.SCRAPFLY_RENDER_WAIT_MS if rendering_wait is None else rendering_wait

        last = FetchResult(url=url, error="not_attempted")
        for attempt in range(1, extra + 2):
            kwargs = dict(
                url=url,
                render_js=render_js,
                asp=asp,
                country=self.country,
                rendering_wait=wait,
                raise_on_upstream_error=False,
            )
            if residential:
                kwargs["proxy_pool"] = self.proxy_pool
            if js_scenario:
                kwargs["js_scenario"] = js_scenario
            try:
                resp = self._client.scrape(ScrapeConfig(**kwargs))
            except ScrapflyScrapeError as exc:
                code = str(getattr(exc, "code", "") or exc).lower()
                if any(m in code for m in _ASP_BAN_MARKERS):
                    # Proxy rotations exhausted -- retrying wastes credits.
                    return FetchResult(url=url, blocked_reason="asp_shield_failed",
                                       error=f"{getattr(exc, 'code', 'ASP')}")
                last = FetchResult(url=url, error=f"{getattr(exc, 'code', 'ScrapflyScrapeError')}: {exc}")
                logger.warning("  Scrapfly error (attempt %d) for %s: %s", attempt, url, last.error)
                continue
            except (UpstreamHttpClientError, UpstreamHttpServerError) as exc:
                last = FetchResult(url=url, error=f"upstream_http_error: {exc}")
                continue
            except Exception as exc:  # network / SDK / ScrapflyAspError (NOT a ScrapflyScrapeError subclass)
                msg = f"{type(exc).__name__}: {exc}"
                if any(m in msg.lower() for m in _ASP_BAN_MARKERS):
                    # Proxy rotations exhausted upstream -- retrying wastes credits.
                    return FetchResult(url=url, blocked_reason="asp_shield_failed", error=msg)
                last = FetchResult(url=url, error=msg)
                continue

            sr = getattr(resp, "scrape_result", None) or {}
            content = sr.get("content", "") or ""
            up = getattr(resp, "upstream_status_code", None) or sr.get("status_code")
            cost = sr.get("cost")

            if content and any(m in content.lower() for m in _CHALLENGE_MARKERS):
                last = FetchResult(url=url, content=content, upstream_status=up, cost=cost,
                                   blocked_reason="cloudflare_challenge",
                                   error="challenge interstitial returned")
                logger.warning("  Cloudflare challenge not cleared (attempt %d) for %s", attempt, url)
                continue

            if not content:
                last = FetchResult(url=url, upstream_status=up, cost=cost, error="empty content")
                continue

            return FetchResult(
                ok=True, url=url, content=content,
                text=visible_text(content) if want_text else "",
                upstream_status=up, cost=cost,
            )
        return last

    def fetch_many(self, urls, **kwargs):
        """Yield (url, FetchResult) for each URL (sequential -- ASP is rate-shy)."""
        for u in urls:
            yield u, self.fetch(u, **kwargs)


def fetch(url: str, **kwargs) -> FetchResult:
    """Module-level convenience: one Scrapfly ASP fetch."""
    return ScrapflyBrowserClient().fetch(url, **kwargs)


def is_configured() -> bool:
    return bool(config.SCRAPFLY_KEY)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch a Cloudflare/JS-gated URL via Scrapfly ASP")
    ap.add_argument("url")
    ap.add_argument("--no-residential", action="store_true", help="use datacenter proxies")
    ap.add_argument("--no-js", action="store_true", help="disable headless render")
    ap.add_argument("--wait", type=int, default=None, help="render wait ms")
    ap.add_argument("--retries", type=int, default=None, help="extra retries on transient error")
    ap.add_argument("--raw", action="store_true", help="print raw HTML instead of text")
    ap.add_argument("--out", default="", help="write the HTML to this file")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not is_configured():
        sys.exit("SCRAPFLY_KEY not set in environment/.env")

    res = fetch(args.url, residential=not args.no_residential, render_js=not args.no_js,
                rendering_wait=args.wait, retries=args.retries, want_text=not args.raw)
    if not res.ok:
        detail = res.blocked_reason or res.error
        sys.exit(f"FAILED ({detail}) upstream={res.upstream_status} url={args.url}")
    print(f"OK upstream={res.upstream_status} cost={res.cost} bytes={len(res.content)}", file=sys.stderr)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(res.content, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    print(res.content if args.raw else res.text)


if __name__ == "__main__":
    _main()
