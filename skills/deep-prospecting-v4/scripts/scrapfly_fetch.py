#!/usr/bin/env python3
"""Deep prospecting L3 fallback -- fetch a Cloudflare/JS-gated page via Scrapfly.

Self-contained (only needs `requests`), so the whole REI community can run it with
their own Scrapfly key -- no repo/SDK required. The API-first Primary Path
(Enformion) resolves most heirs, but some gaps live behind an anti-bot wall that
plain HTTP fetches -- and the sandboxed browser your AI research agent uses --
cannot clear: county assessor & deed portals, FindAGrave / Legacy obituaries,
court record pages, and people-search detail pages. This routes the fetch through
Scrapfly's Anti-Scraping Protection (asp=true) + a headless browser
(render_js=true) + a residential proxy, which clears the JS challenge.

Where it works, and where it does NOT (validated live, mid-2026):
  - County / records / genealogy portals (assessor datalets, FindAGrave, Legacy,
    court info pages): ASP clears them reliably. This is the sweet spot.
  - Hardened people-search aggregators (TruePeopleSearch, FastPeopleSearch): ASP
    is frequently IP-banned after the first hit. Best-effort only -- prefer the
    API heir graph and reach relatives through a known contact.
  - Records a county does not publish online (some estate/probate cases, deed
    IMAGES behind a paid subscription): Scrapfly cannot fetch what is not online.
    That is a data-availability limit -- fall back to a phone/in-person request.

USAGE
-----
  export SCRAPFLY_KEY=scp-live-...
  python scrapfly_fetch.py "https://<assessor|findagrave|legacy|court>/..."
  python scrapfly_fetch.py "<url>" --raw --out page.html      # save full HTML
  python scrapfly_fetch.py "<url>" --datacenter --wait 4000    # cheaper proxies

Exit code 0 on success, 2 when the page stays blocked (challenge / ASP ban).
"""

import argparse
import os
import re
import sys

import requests

SCRAPE_URL = "https://api.scrapfly.io/scrape"
CHALLENGE_MARKERS = ("just a moment", "checking your browser", "cf-challenge",
                     "enable javascript and cookies", "attention required")
ASP_BAN_MARKERS = ("shield_protection_failed", "all proxy rotations exhausted")


def visible_text(html: str) -> str:
    t = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return re.sub(r"\s+", " ", t).strip()


def fetch(url, *, residential=True, render_js=True, wait_ms=5000, country="us", timeout=150):
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        sys.exit("Set SCRAPFLY_KEY in your environment (https://scrapfly.io dashboard).")
    params = {
        "key": key, "url": url,
        "asp": "true", "render_js": "true" if render_js else "false",
        "country": country, "rendering_wait": wait_ms,
    }
    if residential:
        params["proxy_pool"] = "public_residential_pool"
    try:
        r = requests.get(SCRAPE_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "reason": f"request_error: {e}"}
    if r.status_code != 200:
        low = r.text.lower()
        if any(m in low for m in ASP_BAN_MARKERS):
            return {"ok": False, "reason": "asp_shield_failed (site banned every proxy rotation)"}
        return {"ok": False, "reason": f"HTTP {r.status_code}: {r.text[:200]}"}
    result = (r.json() or {}).get("result") or {}
    content = result.get("content", "") or ""
    up = result.get("status_code")
    if content and any(m in content.lower() for m in CHALLENGE_MARKERS):
        return {"ok": False, "reason": "cloudflare_challenge (not cleared)", "upstream": up}
    if not content:
        return {"ok": False, "reason": "empty content", "upstream": up}
    return {"ok": True, "content": content, "text": visible_text(content), "upstream": up}


def main():
    ap = argparse.ArgumentParser(description="Fetch a Cloudflare/JS-gated URL via Scrapfly ASP")
    ap.add_argument("url")
    ap.add_argument("--datacenter", action="store_true", help="use datacenter proxies (cheaper, weaker)")
    ap.add_argument("--no-js", action="store_true", help="disable headless render")
    ap.add_argument("--wait", type=int, default=5000, help="render wait ms (default 5000)")
    ap.add_argument("--raw", action="store_true", help="print raw HTML instead of visible text")
    ap.add_argument("--out", default="", help="write the HTML to this file")
    args = ap.parse_args()

    res = fetch(args.url, residential=not args.datacenter, render_js=not args.no_js, wait_ms=args.wait)
    if not res["ok"]:
        print(f"FAILED ({res['reason']}) url={args.url}", file=sys.stderr)
        sys.exit(2)
    print(f"OK upstream={res.get('upstream')} bytes={len(res['content'])}", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(res["content"])
        print(f"wrote {args.out}", file=sys.stderr)
    print(res["content"] if args.raw else res["text"])


if __name__ == "__main__":
    main()
