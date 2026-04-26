"""Resilient batch Market Finder extraction for Ohio counties.

Wraps extract_market_finder.py to handle failures gracefully — one bad
county doesn't kill the whole run. Skips counties that already have a
fresh JSON in output/ unless --force is passed.

Usage:
    PYTHONPATH=src python -m run_oh_extraction --headless
    PYTHONPATH=src python -m run_oh_extraction --counties Cuyahoga,Franklin --headless
    PYTHONPATH=src python -m run_oh_extraction --force --headless

Output:
    output/market_finder_Ohio_<County>_<ts>.json (one per county)
    output/oh_extraction_run_<ts>.log (run manifest with status per county)
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from extract_market_finder import extract_market_finder

logger = logging.getLogger(__name__)

# Top 20 Ohio counties by population — covers ~85% of state housing transactions.
# Ordered population-descending so highest-yield counties run first
# (in case the run is interrupted, you still get the most useful data).
TOP_OH_COUNTIES = [
    "Cuyahoga", "Franklin", "Hamilton", "Summit", "Montgomery",
    "Lucas", "Stark", "Butler", "Lorain", "Mahoning",
    "Lake", "Warren", "Trumbull", "Clermont", "Greene",
    "Licking", "Medina", "Delaware", "Fairfield", "Wood",
]

# A JSON extract is considered "fresh" if newer than this many days.
FRESH_DAYS = 7


def _find_existing_extract(output_dir: Path, county: str) -> Path | None:
    """Return the newest fresh extract for a county, or None."""
    pattern = f"market_finder_Ohio_{county}_*.json"
    matches = sorted(output_dir.glob(pattern), reverse=True)
    if not matches:
        return None
    newest = matches[0]
    age = datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)
    if age < timedelta(days=FRESH_DAYS):
        return newest
    return None


async def _run_one(county: str, output_dir: str, headless: bool) -> dict:
    try:
        result = await extract_market_finder(
            state="Ohio",
            county=county,
            headless=headless,
            output_dir=output_dir,
        )
        return {
            "county": county,
            "status": "ok" if result["success"] else "failed",
            "zip_count": result.get("zip_count", 0),
            "neighborhood_count": result.get("neighborhood_count", 0),
        }
    except Exception as exc:
        logger.exception("County %s raised: %s", county, exc)
        return {
            "county": county,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counties",
        help="Comma-separated counties (default: top 20 OH by population)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if a fresh JSON already exists",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    counties = (
        [c.strip() for c in args.counties.split(",")]
        if args.counties
        else TOP_OH_COUNTIES
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now()
    manifest = {
        "started_at": started_at.isoformat(),
        "counties_requested": counties,
        "results": [],
    }

    for i, county in enumerate(counties, 1):
        existing = None if args.force else _find_existing_extract(output_dir, county)
        if existing:
            logger.info(
                "[%d/%d] %s: skipping — fresh extract at %s",
                i, len(counties), county, existing.name,
            )
            manifest["results"].append({
                "county": county,
                "status": "skipped_fresh",
                "existing_file": existing.name,
            })
            continue

        logger.info("[%d/%d] %s: extracting...", i, len(counties), county)
        result = asyncio.run(_run_one(county, args.output_dir, args.headless))
        manifest["results"].append(result)

        if result["status"] == "ok":
            logger.info(
                "[%d/%d] %s: OK (%d zips, %d neighborhoods)",
                i, len(counties), county,
                result["zip_count"], result["neighborhood_count"],
            )
        else:
            logger.warning(
                "[%d/%d] %s: %s — continuing to next county",
                i, len(counties), county, result["status"],
            )

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["duration_minutes"] = round(
        (datetime.now() - started_at).total_seconds() / 60, 1
    )

    summary_counts: dict[str, int] = {}
    for r in manifest["results"]:
        summary_counts[r["status"]] = summary_counts.get(r["status"], 0) + 1
    manifest["summary"] = summary_counts

    log_path = output_dir / f"oh_extraction_run_{started_at:%Y%m%d_%H%M%S}.log"
    log_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print(f"Run finished in {manifest['duration_minutes']} min")
    print(f"  Manifest: {log_path}")
    for status, count in summary_counts.items():
        print(f"  {status}: {count}")

    failed = sum(1 for r in manifest["results"] if r["status"] in ("failed", "error"))
    sys.exit(0 if failed == 0 else 2)


if __name__ == "__main__":
    main()
