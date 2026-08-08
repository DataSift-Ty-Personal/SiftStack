"""Philadelphia production daily pipeline — single end-to-end command.

Scrape → Smarty → filters → bucket upload → SiftMap enrich → DataSift skip trace
→ 5 niche list uploads → wait for skip trace → Trestle phone scoring → tag upload
→ single combined Slack summary.

Defaults:
    limit=None        — all records, no cap
    upload=True       — uploads to DataSift
    slack=True        — sends Slack summary
    phone_scoring=True — waits for skip trace, scores phones with Trestle

Flags to split back into two scheduled workflows (GitHub Actions):
    --no-phone-scoring  passed to morning run
    run_philly_phone_score.py  scheduled 15 min later

Usage:
    python run_philly_daily.py --lookback 1
    python run_philly_daily.py --lookback 2 --no-phone-scoring
    python run_philly_daily.py --no-upload --no-slack           # dry run
    python run_philly_daily.py --resume-from output/philly_daily_782recs_20260428_122123.csv
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "src")

import config
from philly_pipeline import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("philly_daily")

for _noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ENABLED_SOURCES = [s.source_id for s in config.PHILLY_SOURCES if s.enabled]


async def main(
    lookback_days: int,
    sources: list[str],
    upload: bool,
    slack: bool,
    resume_from: str | None = None,
    phone_scoring: bool = True,
) -> None:
    if resume_from:
        logger.info(
            "Philadelphia Resume Run  (resume_from=%s, upload=%s, phone_scoring=%s)",
            resume_from, upload, phone_scoring,
        )
    else:
        logger.info(
            "Philadelphia Daily Run  (sources=%s, lookback=%dd, upload=%s, phone_scoring=%s)",
            ", ".join(sources), lookback_days, upload, phone_scoring,
        )

    result = await run_pipeline(
        sources=sources,
        lookback=lookback_days,
        limit=None,
        upload_datasift=upload,
        notify_slack=slack,
        resume_from=resume_from,
        phone_scoring=phone_scoring,
    )

    st  = result["stats"]
    pr  = result.get("phone_result", {})

    print(f"\n{'=' * 60}")
    print(f"  Philadelphia {'Resume' if resume_from else 'Daily'} Run — Summary")
    print(f"{'=' * 60}")

    if resume_from:
        print(f"  Source CSV        : {resume_from}")
        print(f"  Records loaded    : {st['total_after_dedup']}")
    else:
        print(f"  Sources run       : {', '.join(sources)}")
        for sid in sources:
            scraped = st["scraped_by_source"].get(sid, 0)
            f2d     = st["f2_dropped_by_source"].get(sid, 0)
            print(f"    {sid:<25} scraped={scraped:<5}  f2_dropped={f2d}")
        print(f"  After cap/dedup   : {st['total_after_dedup']}")

    print(f"  Smarty API calls  : {st['smarty_api_calls']}  (cache hits: {st['smarty_cache_hits']})")
    print(f"  RDI removed       : {st['rdi_removed']}")
    print(f"  Validation removed: {st['validation_removed']}")
    print(f"  CSV written       : {st['csv_written']}  (probate dropped: {st['probate_dropped']})")
    print(f"  Tier-0 dropped    : {st.get('tier0_dropped', 0)}")
    tier_dist = st.get("tier_distribution", {})
    if tier_dist:
        tier_labels = {1: "Cold", 2: "Warm", 3: "Hot", 4: "Critical"}
        tier_str = "  ".join(
            f"T{t}({tier_labels.get(t,'?')})={c}"
            for t, c in sorted(tier_dist.items())
        )
        print(f"  Distress tiers    : {tier_str}")
    print(f"  CSV path          : {result['csv_path']}")

    if upload:
        ur = result.get("upload_result") or {}
        status = "OK" if ur.get("success") else f"FAILED — {ur.get('message', 'no result')}"
        import datetime
        print(f"  DataSift bucket   : {status}  (tag siftstack_{datetime.date.today()})")

        niche = result.get("niche_results", [])
        if niche:
            print(f"  Niche list uploads: {len(niche)} lists")
            for nr in niche:
                tick = "✓" if nr.get("success") else "✗"
                print(f"    {tick} {nr['list_name']:<25} {nr['count']:>4} records"
                      + ("" if nr.get("success") else f"  — {nr.get('message','')}"))

    if pr:
        if pr.get("skipped"):
            print(f"  Phone scoring     : skipped  ({pr.get('message','')})")
        else:
            tier_str = "  ".join(f"{t}: {c}" for t, c in pr.get("tier_counts", {}).items() if c)
            tag_ok   = "✓" if pr.get("upload_ok") else "✗"
            print(f"  Phone scoring     : {pr.get('phones_scored', 0)} scored  "
                  f"tags {tag_ok}  |  {tier_str}")

    print(f"  Tracerfly cost    : ${st['tracerfy_cost']:.4f}"
          + ("  (skipped — resume)" if resume_from else ""))
    print(f"  Trestle cost      : ${st.get('trestle_cost', 0.0):.4f}")
    print(f"  Total cost        : ${st['total_cost']:.4f}")
    print(f"  Elapsed           : {st['elapsed_s']:.0f}s")
    print(f"{'=' * 60}\n")

    if result["csv_path"] is None:
        logger.error("Pipeline produced no output — check logs above")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Philadelphia production daily pipeline")
    parser.add_argument("--lookback", type=int, default=30, metavar="DAYS",
                        help="Lookback window in days (default: 30)")
    parser.add_argument("--sources", nargs="+", default=ENABLED_SOURCES,
                        metavar="SOURCE_ID",
                        help="Source IDs to run (default: all enabled)")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip DataSift upload (full-volume dry run)")
    parser.add_argument("--no-slack",  action="store_true",
                        help="Skip Slack notification")
    parser.add_argument("--no-phone-scoring", action="store_true",
                        help=(
                            "Skip Trestle phone scoring after skip trace. "
                            "Use when splitting into two GitHub Actions workflows — "
                            "run run_philly_phone_score.py 15 min later instead."
                        ))
    parser.add_argument("--resume-from", type=str, default=None, metavar="CSV_PATH",
                        help=(
                            "Resume from an existing DataSift CSV — reads records, "
                            "re-runs Smarty/RDI/validation, uploads with fixed Lists column."
                        ))
    args = parser.parse_args()

    asyncio.run(main(
        lookback_days=args.lookback,
        sources=args.sources,
        upload=not args.no_upload,
        slack=not args.no_slack,
        resume_from=args.resume_from,
        phone_scoring=not args.no_phone_scoring,
    ))
