"""One-notice Scrapfly validation spike.

Logs into tnpublicnotice.com through Scrapfly and fetches a single notice detail
page to verify the reCAPTCHA gate clears and a screenshot comes back, before we
rely on the Scrapfly backend for production scraping or backfill.

Run:
    python src/scrapfly_spike.py --id 541024
    python src/scrapfly_spike.py --url "https://www.tnpublicnotice.com/(S(x))/Details.aspx?ID=541024"

Needs SCRAPFLY_KEY (and TNPN_EMAIL / TNPN_PASSWORD) in .env.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrapfly single-notice validation spike")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", help="Notice ID (builds Details.aspx?ID=<id>)")
    g.add_argument("--url", help="Full notice detail URL")
    ap.add_argument("--session", default="tnpn-spike")
    ap.add_argument("--no-screenshot", action="store_true")
    args = ap.parse_args()

    if not config.SCRAPFLY_KEY:
        logger.error("SCRAPFLY_KEY not set in .env")
        sys.exit(1)

    from scrapfly_client import ScrapflyNoticeClient

    client = ScrapflyNoticeClient()

    logger.info("Step 1: login via Scrapfly (session=%s)...", args.session)
    if not client.login(session=args.session):
        logger.error("Login failed. Cannot proceed.")
        sys.exit(2)

    target = args.url or args.id
    logger.info("Step 2: fetch notice %s ...", target)
    res = client.fetch_notice(target, session=args.session, want_screenshot=not args.no_screenshot)

    print("\n===== SPIKE RESULT =====")
    print("ok                :", res.ok)
    print("url               :", res.url)
    print("upstream_status   :", res.upstream_status)
    print("cost (credits)    :", res.cost)
    print("error             :", res.error or "(none)")
    print("content length    :", len(res.content_html))
    print("has 'Notice Content':", "Notice Content" in res.content_html)

    if res.content_html:
        idx = res.content_html.find("Notice Content")
        if idx != -1:
            snippet = res.content_html[idx: idx + 400]
            import re as _re
            snippet = _re.sub(r"<[^>]+>", " ", snippet)
            snippet = _re.sub(r"\s+", " ", snippet).strip()
            print("\n--- text around 'Notice Content' ---")
            print(snippet[:300])

    if res.screenshot_bytes:
        out_dir = config.NOTICE_SCREENSHOT_DIR / "spike"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = (args.id or "url").replace("/", "_")[:40]
        p = out_dir / f"spike_{name}.png"
        p.write_bytes(res.screenshot_bytes)
        print(f"\nscreenshot saved  : {p} ({len(res.screenshot_bytes):,} bytes)")
    else:
        print("\nscreenshot        : NONE")

    print("========================\n")
    sys.exit(0 if res.ok else 3)


if __name__ == "__main__":
    main()
