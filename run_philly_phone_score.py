"""Philadelphia phone scoring job — standalone manual re-run tool.

Core logic lives in src/phone_scorer.py and is shared with the daily
pipeline (run_philly_daily.py).  Use this script to re-score phones on
a previously uploaded list without re-running the full scrape.

Targets the `siftstack_YYYY-MM-DD` run-cohort TAG that every uploaded record
carries. Use --list-name only to score a real list (e.g. a niche list).

Usage:
    python run_philly_phone_score.py
    python run_philly_phone_score.py --lookback-hours 48   # also score yesterday
    python run_philly_phone_score.py --list-name "Foreclosure"
    python run_philly_phone_score.py --wait-minutes 5 --wait-retries 3
    python run_philly_phone_score.py --no-upload --no-slack
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import sys
import time

sys.path.insert(0, "src")

import config
from phone_scorer import score_and_tag
from slack_notifier import _send_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("philly_phone_score")

for _noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _cohort_tags_for_window(lookback_hours: int) -> list[str]:
    """Run-cohort tags for the window, newest first. Matches the tag stamped by
    datasift_formatter.build_tags on every record of that run."""
    today = datetime.date.today()
    days = max(1, (lookback_hours + 23) // 24)
    return [f"siftstack_{(today - datetime.timedelta(days=i)).isoformat()}"
            for i in range(days)]


def _slack_summary(
    scored_lists: list[dict],
    skipped_lists: list[str],
    total_elapsed_s: float,
) -> None:
    if not config.SLACK_WEBHOOK_URL:
        return
    total_phones = sum(r["phones_scored"] for r in scored_lists)
    total_cost   = sum(r["cost"] for r in scored_lists)
    upload_ok    = all(r.get("upload_ok") for r in scored_lists) if scored_lists else True

    lines = [
        "*SiftStack Philly — Phone Scoring Complete*",
        f"Lists scored: {len(scored_lists)}  |  Skipped: {len(skipped_lists)}",
        f"Phones: {total_phones}  |  Cost: ${total_cost:.2f}  |  Tags: {'✓' if upload_ok else '✗'}",
    ]
    for r in scored_lists:
        tiers = "  ".join(f"{t}: {c}" for t, c in r["tier_counts"].items() if c)
        lines.append(f"  • {r['list_name']}: {r['phones_scored']} phones  {tiers}")
    if skipped_lists:
        lines.append(f"  ⚠ Skipped: {', '.join(skipped_lists)}")
    lines.append(f"Elapsed: {total_elapsed_s:.0f}s")
    _send_webhook("\n".join(lines))


async def main(
    targets: list[str],
    upload: bool,
    slack: bool,
    wait_minutes: int,
    wait_retries: int,
    as_tag: bool = True,
) -> None:
    t_start = time.time()

    email    = config.DATASIFT_EMAIL
    password = config.DATASIFT_PASSWORD
    api_key  = config.TRESTLE_API_KEY

    if not email or not password:
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set")
        sys.exit(1)
    if not api_key:
        logger.error("TRESTLE_API_KEY not set")
        sys.exit(1)

    wait_seconds  = wait_minutes * 60
    scored_lists: list[dict] = []
    skipped_lists: list[str] = []

    for target in targets:
        logger.info("── Processing %s: %s ──", "tag" if as_tag else "list", target)
        result = await score_and_tag(
            list_name=None if as_tag else target,
            tag_name=target if as_tag else None,
            email=email,
            password=password,
            api_key=api_key,
            do_upload=upload,
            max_retries=wait_retries,
            wait_seconds=wait_seconds,
        )
        if result["skipped"]:
            skipped_lists.append(target)
        else:
            scored_lists.append(result)

    elapsed = time.time() - t_start
    total_phones = sum(r["phones_scored"] for r in scored_lists)
    total_cost   = sum(r["cost"] for r in scored_lists)

    print(f"\n{'=' * 60}")
    print(f"  Philadelphia Phone Scoring — Summary")
    print(f"{'=' * 60}")
    print(f"  Lists scored  : {len(scored_lists)}")
    print(f"  Lists skipped : {len(skipped_lists)}"
          + (f"  ({', '.join(skipped_lists)})" if skipped_lists else ""))
    print(f"  Phones scored : {total_phones}")
    print(f"  Est. cost     : ${total_cost:.2f}  (@ $0.015/phone)")

    for r in scored_lists:
        tier_str   = "  ".join(f"{t}: {c}" for t, c in r["tier_counts"].items() if c)
        upload_str = "uploaded" if r["upload_ok"] else ("skipped" if not upload else "FAILED")
        print(f"\n  {r['list_name']}:")
        print(f"    Phones: {r['phones_scored']}  |  Tags: {upload_str}")
        if tier_str:
            print(f"    {tier_str}")
        if r["tag_csv_path"]:
            print(f"    Tag CSV: {r['tag_csv_path']}")

    print(f"\n  Elapsed: {elapsed:.0f}s")
    print(f"{'=' * 60}\n")

    if slack:
        _slack_summary(scored_lists, skipped_lists, elapsed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Philadelphia Trestle phone scoring — standalone")
    parser.add_argument("--list-name", type=str, default=None)
    parser.add_argument("--lookback-hours", type=int, default=24, metavar="HOURS")
    parser.add_argument("--wait-minutes",   type=int, default=5,  metavar="MIN")
    parser.add_argument("--wait-retries",   type=int, default=3,  metavar="N")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--no-slack",  action="store_true")
    args = parser.parse_args()

    as_tag  = args.list_name is None
    targets = ([args.list_name] if args.list_name
               else _cohort_tags_for_window(args.lookback_hours))
    logger.info("Phone scoring targets (%s): %s",
                "tag" if as_tag else "list", ", ".join(targets))

    asyncio.run(main(
        targets=targets,
        upload=not args.no_upload,
        slack=not args.no_slack,
        wait_minutes=args.wait_minutes,
        wait_retries=args.wait_retries,
        as_tag=as_tag,
    ))
