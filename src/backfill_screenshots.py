"""Backfill notice screenshots for the active foreclosure master list.

Re-runs the Knox + Blount foreclosure saved searches through the proven
Playwright scraper, TARGETED to only the notice IDs in the master CSV, and
captures a proof-of-source screenshot for each. Runs through the Apify
residential proxy (required: the notice gate only renders on a residential IP),
solves the reCAPTCHA with 2Captcha, and writes notice_screenshot_path /
notice_screenshot_url back to the CSV. Does NOT touch seen_ids / last_run.

Run:
    python src/backfill_screenshots.py                 # newest master CSV, all targets
    python src/backfill_screenshots.py --limit 2       # smoke test on the first 2
    python src/backfill_screenshots.py --csv <path> --since 2026-04-01

Needs APIFY_TOKEN (or APIFY_PROXY_PASSWORD) for the residential proxy, plus
TNPN_EMAIL / TNPN_PASSWORD / CAPTCHA_API_KEY in .env.
"""

import argparse
import asyncio
import csv
import glob
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _notice_id(url: str) -> str:
    m = re.search(r"[?&]ID=(\d+)", url or "")
    return m.group(1) if m else ""


def _parse_date(s: str):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _newest_master() -> str | None:
    matches = sorted(
        m for m in glob.glob(str(config.OUTPUT_DIR / "foreclosure_master_active_*.csv"))
        if "_with_screenshots" not in m  # don't pick our own output as input
    )
    return matches[-1] if matches else None


_STREET_SUFFIXES = {
    "dr", "drive", "ln", "lane", "way", "st", "street", "ave", "avenue", "rd", "road",
    "blvd", "boulevard", "ct", "court", "cir", "circle", "pl", "place", "pike", "hwy",
    "highway", "trl", "trail", "ter", "terrace", "pkwy", "parkway", "cv", "cove", "loop",
    "run", "pt", "point", "rdg", "ridge", "xing", "crossing", "row", "trace", "walk",
}


def _address_keyword(addr: str) -> str:
    """Derive a distinctive search keyword from an address: house number + street
    name with the suffix dropped (e.g. '4200 Fulton Dr' -> '4200 Fulton')."""
    toks = (addr or "").strip().split()
    if toks and toks[-1].lower().strip(".") in _STREET_SUFFIXES:
        toks = toks[:-1]
    return " ".join(toks).strip()


def _apify_proxy_url() -> str | None:
    """Build an Apify residential proxy URL from APIFY_PROXY_PASSWORD or the token."""
    pw = os.getenv("APIFY_PROXY_PASSWORD", "").strip()
    if not pw:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            return None
        try:
            import requests
            me = requests.get(
                "https://api.apify.com/v2/users/me", params={"token": token}, timeout=30
            ).json()
            pw = ((me.get("data") or {}).get("proxy") or {}).get("password", "")
        except Exception as exc:
            logger.error("Could not fetch Apify proxy password: %s", exc)
            return None
    if not pw:
        return None
    return f"http://groups-RESIDENTIAL,country-US:{pw}@proxy.apify.com:8000"


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill notice screenshots via Playwright + Apify residential proxy")
    ap.add_argument("--csv", default=None, help="Input master CSV (default: newest)")
    ap.add_argument("--out", default=None, help="Output CSV (default: <input>_with_screenshots.csv)")
    ap.add_argument("--limit", type=int, default=0, help="Only target the first N IDs (smoke test)")
    ap.add_argument("--since", default=None, help="Override publish-date cutoff (YYYY-MM-DD)")
    ap.add_argument("--keyword", action="store_true",
                    help="Find each target by an address keyword search (catches notices "
                         "that rolled off the saved-search date window)")
    ap.add_argument("--months", type=int, default=12, help="Keyword search look-back window (months)")
    ap.add_argument("--force", action="store_true",
                    help="Re-capture even targets already on disk (e.g. to apply a new screenshot style)")
    args = ap.parse_args()

    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set; needed to solve the notice gate.")
        sys.exit(1)
    if not (config.TNPN_EMAIL and config.TNPN_PASSWORD):
        logger.error("TNPN_EMAIL / TNPN_PASSWORD not set.")
        sys.exit(1)
    proxy = _apify_proxy_url()
    if not proxy:
        logger.error("No residential proxy. Set APIFY_TOKEN or APIFY_PROXY_PASSWORD in .env.")
        sys.exit(1)
    logger.info("Residential proxy: proxy.apify.com:8000 (RESIDENTIAL, US)")

    csv_path = Path(args.csv) if args.csv else (Path(_newest_master()) if _newest_master() else None)
    if not csv_path or not csv_path.exists():
        logger.error("No master CSV. Pass --csv or run consolidate_foreclosures.py first.")
        sys.exit(1)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        logger.error("Empty CSV: %s", csv_path)
        sys.exit(1)

    id_of = {id(r): _notice_id(r.get("source_url")) for r in rows}
    targets = [r for r in rows if id_of[id(r)]]
    if args.limit:
        targets = targets[: args.limit]
    target_ids = {id_of[id(r)] for r in targets}
    if not target_ids:
        logger.error("No notice IDs found in CSV (need source_url with ?ID=).")
        sys.exit(1)
    logger.info("Targets: %d notice IDs from %s (%d rows total)", len(target_ids), csv_path.name, len(rows))

    from notice_screenshot import _screenshot_filename

    def _png_path(nid: str) -> Path:
        return config.NOTICE_SCREENSHOT_DIR / _screenshot_filename(nid)

    # Resume: skip targets already captured on disk, so chunked re-runs continue
    # where a prior run left off (a long run through a residential proxy may not
    # finish every target in one pass).
    already = set() if args.force else {nid for nid in target_ids if _png_path(nid).exists()}
    to_fetch = target_ids - already
    logger.info("Targets: %d total | %d already captured | %d to fetch",
                len(target_ids), len(already), len(to_fetch))

    notices = []
    if to_fetch:
        to_fetch_rows = [r for r in targets if id_of[id(r)] in to_fetch]
        if args.keyword:
            kw_targets = [
                {"keyword": _address_keyword(r.get("address", "")), "target_id": id_of[id(r)]}
                for r in to_fetch_rows if _address_keyword(r.get("address", ""))
            ]
            logger.info("Keyword backfill (%d-month window) for %d targets:", args.months, len(kw_targets))
            for t in kw_targets:
                logger.info("   %-22s -> ID %s", t["keyword"], t["target_id"])
            from scraper import scrape_by_keywords
            notices = asyncio.run(scrape_by_keywords(
                kw_targets, proxy_url=proxy,
                llm_api_key=config.ANTHROPIC_API_KEY or None, months=args.months,
            ))
        else:
            if args.since:
                cutoff = args.since
            else:
                tdates = [_parse_date(r.get("date_published")) for r in targets]
                if tdates and all(tdates):
                    cutoff = (min(tdates) - timedelta(days=14)).isoformat()
                else:
                    cutoff = (date.today() - timedelta(days=180)).isoformat()
            logger.info("Publish-date cutoff (bounds navigation): %s", cutoff)
            searches = [s for s in config.SAVED_SEARCHES if s.notice_type == "foreclosure"]
            logger.info("Searches: %s", ", ".join(s.saved_search_name for s in searches))
            from scraper import scrape_all
            notices = asyncio.run(scrape_all(
                mode="historical",
                searches=searches,
                proxy_url=proxy,
                since_date_override=cutoff,
                llm_api_key=config.ANTHROPIC_API_KEY or None,
                max_notices=len(to_fetch),    # stop once the remaining targets are captured
                target_ids=to_fetch,
                persist_state=False,          # never touch seen_ids / last_run
                seen_ids={},                  # don't even load the real cache
            ))
        logger.info("Captured %d new screenshot(s) this run",
                    len([n for n in notices if n.notice_screenshot_path]))
    else:
        logger.info("All targets already captured on disk; just refreshing the CSV.")

    # Map captured screenshots to master rows by notice ID AND by address keyword
    # (the address match handles notices whose ID rolled off / was republished).
    def _akw(addr: str) -> str:
        return _address_keyword(addr).lower()

    path_by_id: dict[str, str] = {}
    path_by_kw: dict[str, str] = {}
    for n in notices:
        if not n.notice_screenshot_path:
            continue
        nid = _notice_id(n.source_url)
        if nid:
            path_by_id[nid] = n.notice_screenshot_path
        k = _akw(n.address)
        if k:
            path_by_kw[k] = n.notice_screenshot_path
    # Prior on-disk captures (resume), keyed by the master notice ID. Skipped
    # under --force: we re-captured fresh, and a stale master-ID PNG must not
    # shadow the new address-matched capture (whose current ID may differ).
    if not args.force:
        for nid in target_ids:
            if nid not in path_by_id and _png_path(nid).exists():
                path_by_id[nid] = str(_png_path(nid))

    def _row_path(r: dict) -> str | None:
        return path_by_id.get(id_of[id(r)]) or path_by_kw.get(_akw(r.get("address", "")))

    # Host on Drive if configured; otherwise the local path is the link.
    folder, key = config.GOOGLE_DRIVE_FOLDER_ID, config.GOOGLE_SERVICE_ACCOUNT_KEY
    drive_url: dict[str, str] = {}  # local path -> Drive link
    hosted = 0
    if folder and key:
        from drive_uploader import upload_file
        for r in rows:
            p = _row_path(r)
            if p and p not in drive_url and Path(p).exists():
                link = upload_file(Path(p), folder, key, mimetype="image/png")
                if link:
                    drive_url[p] = link
                    hosted += 1

    cols = list(rows[0].keys())
    for c in ("notice_screenshot_path", "notice_screenshot_url"):
        if c not in cols:
            cols.append(c)
    got = 0
    for r in rows:
        p = _row_path(r)
        if p:
            r["notice_screenshot_path"] = p
            r["notice_screenshot_url"] = drive_url.get(p, p)
            got += 1
        else:
            r.setdefault("notice_screenshot_path", "")
            r.setdefault("notice_screenshot_url", "")

    out_path = Path(args.out) if args.out else csv_path.with_name(csv_path.stem + "_with_screenshots.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    missed = [r.get("address", "?") for r in rows if not _row_path(r)]
    logger.info("")
    logger.info("Backfill: %d/%d targets have screenshots (%d hosted on Drive).",
                got, len(rows), hosted)
    if missed:
        logger.warning("Still missing %d: %s", len(missed), ", ".join(missed))
    logger.info("Updated CSV: %s", out_path)


if __name__ == "__main__":
    main()
