"""Rank Ohio counties for niche-sequential statewide expansion.

Reads Market Finder JSON extracts produced by extract_market_finder.py
and applies a 3-axis scoring rubric:

    Score = INV_DENSITY x AB_CLASS x PORTAL_REUSE

  - INV_DENSITY: sum of total_inv_trans_6mo across zips that themselves
    cleared the per-zip threshold (>=5 by default)
  - AB_CLASS: share of those qualifying zips with median_home_value
    inside the A/B band (default $150k-$600k)
  - PORTAL_REUSE: how much existing scraper code transfers — separate
    multipliers per notice type because OH's foreclosure portal landscape
    (RealAuction template) is wildly different from probate (per-county
    custom). See PORTAL_REUSE dict.

Two composite scores are produced per county: foreclosure_score and
probate_score. The user picks the top N by whichever notice type they
plan to expand.

Usage:
    PYTHONPATH=src python -m score_oh_counties
    PYTHONPATH=src python -m score_oh_counties --inv-threshold 8 --ab-min 180000 --ab-max 500000

Output:
    output/oh_county_scores_<ts>.csv (ranked by composite scores)
"""

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────

DEFAULT_INV_THRESHOLD = 5     # zip qualifies if total_inv_trans_6mo >= this
DEFAULT_AB_MIN = 150_000      # A/B band lower bound (median_home_value $)
DEFAULT_AB_MAX = 600_000      # A/B band upper bound

# Portal reuse multipliers per notice type. Update as portal recon completes.
# Multiplier rationale:
#   1.0 = scraper template already exists, county is drop-in (config change)
#   0.7 = same platform family as a built scraper (e.g., another ColdFusion
#         probate court) — moderate adapter work
#   0.5 = single-template platform (JWorks etc.) — adapter work
#   0.3 = unknown / custom — full new build
#
# Key fact: ~30 OH counties use RealAuction for foreclosure under the
# pattern {county}.sheriffsaleauction.ohio.gov. If recon confirms a county
# is on RealAuction, set foreclosure to 1.0.
PORTAL_REUSE: dict[str, dict[str, float]] = {
    # Foreclosure: all 20 top OH counties confirmed on RealAuction
    # (probed 2026-04-25 — every {county}.sheriffsaleauction.ohio.gov returns
    # a county-specific RealForeclose page). Template reuse = 1.0 across the
    # board for foreclosure. Probate stays per-county-custom.
    "franklin":   {"foreclosure": 1.0, "probate": 0.7},
    "cuyahoga":   {"foreclosure": 1.0, "probate": 0.3},
    "hamilton":   {"foreclosure": 1.0, "probate": 0.3},
    "summit":     {"foreclosure": 1.0, "probate": 0.3},
    "montgomery": {"foreclosure": 1.0, "probate": 0.7},  # probate built (ColdFusion)
    "lucas":      {"foreclosure": 1.0, "probate": 0.3},
    "stark":      {"foreclosure": 1.0, "probate": 0.3},
    "butler":     {"foreclosure": 1.0, "probate": 0.3},
    "lorain":     {"foreclosure": 1.0, "probate": 0.3},
    "mahoning":   {"foreclosure": 1.0, "probate": 0.3},
    "lake":       {"foreclosure": 1.0, "probate": 0.3},
    "warren":     {"foreclosure": 1.0, "probate": 0.3},
    "trumbull":   {"foreclosure": 1.0, "probate": 0.3},
    "clermont":   {"foreclosure": 1.0, "probate": 0.3},
    "greene":     {"foreclosure": 1.0, "probate": 0.5},  # probate built (JWorks)
    "licking":    {"foreclosure": 1.0, "probate": 0.3},
    "medina":     {"foreclosure": 1.0, "probate": 0.3},
    "delaware":   {"foreclosure": 1.0, "probate": 0.3},
    "fairfield":  {"foreclosure": 1.0, "probate": 0.3},
    "wood":       {"foreclosure": 1.0, "probate": 0.3},
}
DEFAULT_FORECLOSURE_REUSE = 0.3   # unknown portal — assume custom build
DEFAULT_PROBATE_REUSE = 0.3


# ── Core scoring ──────────────────────────────────────────────────────

def _portal_reuse(county: str, notice_type: str) -> float:
    entry = PORTAL_REUSE.get(county.lower())
    if not entry:
        return DEFAULT_FORECLOSURE_REUSE if notice_type == "foreclosure" else DEFAULT_PROBATE_REUSE
    return entry.get(notice_type, DEFAULT_FORECLOSURE_REUSE if notice_type == "foreclosure" else DEFAULT_PROBATE_REUSE)


def _score_county(
    market_data: dict,
    inv_threshold: int,
    ab_min: int,
    ab_max: int,
) -> dict:
    county = market_data.get("county", "?")
    zip_rows = market_data.get("zip_data") or []

    qualifying = [
        z for z in zip_rows
        if (z.get("total_inv_trans_6mo") or 0) >= inv_threshold
    ]

    inv_density = sum((z.get("total_inv_trans_6mo") or 0) for z in qualifying)

    if qualifying:
        ab_zips = [
            z for z in qualifying
            if z.get("median_home_value") is not None
            and ab_min <= z["median_home_value"] <= ab_max
        ]
        ab_pct = len(ab_zips) / len(qualifying)
    else:
        ab_zips = []
        ab_pct = 0.0

    fc_reuse = _portal_reuse(county, "foreclosure")
    pr_reuse = _portal_reuse(county, "probate")

    # Composite score normalization: keep raw inv_density (count) so the
    # scale is interpretable — "this county yields N investor transactions
    # per 6mo discounted by AB% and portal cost." Higher = better.
    fc_score = round(inv_density * ab_pct * fc_reuse, 1)
    pr_score = round(inv_density * ab_pct * pr_reuse, 1)

    return {
        "county": county,
        "qualifying_zips": len(qualifying),
        "ab_class_zips": len(ab_zips),
        "ab_class_pct": round(ab_pct, 3),
        "inv_density": inv_density,
        "foreclosure_reuse": fc_reuse,
        "probate_reuse": pr_reuse,
        "foreclosure_score": fc_score,
        "probate_score": pr_score,
        "combined_score": round(fc_score + pr_score, 1),
    }


def _newest_per_county(extract_dir: Path) -> dict[str, Path]:
    """For each Ohio county, return the path to its newest JSON extract."""
    newest: dict[str, Path] = {}
    for path in extract_dir.glob("market_finder_Ohio_*.json"):
        # filename: market_finder_Ohio_<County>_<YYYYMMDD>_<HHMMSS>.json
        stem = path.stem  # drop .json
        parts = stem.split("_")
        # ["market", "finder", "Ohio", <County...>, <date>, <time>]
        if len(parts) < 6:
            continue
        county = "_".join(parts[3:-2])  # handles multi-word county names
        prior = newest.get(county)
        if prior is None or path.stat().st_mtime > prior.stat().st_mtime:
            newest[county] = path
    return newest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extract-dir", default="./output",
        help="Directory containing market_finder_Ohio_*.json extracts",
    )
    parser.add_argument(
        "--output-dir", default="./output",
        help="Where to write the scores CSV",
    )
    parser.add_argument("--inv-threshold", type=int, default=DEFAULT_INV_THRESHOLD)
    parser.add_argument("--ab-min", type=int, default=DEFAULT_AB_MIN)
    parser.add_argument("--ab-max", type=int, default=DEFAULT_AB_MAX)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    extract_dir = Path(args.extract_dir)
    files = _newest_per_county(extract_dir)
    if not files:
        logger.error("No Market Finder extracts found in %s", extract_dir)
        return 1

    logger.info("Scoring %d counties from extracts", len(files))

    scores = []
    for county, path in sorted(files.items()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skipping %s: failed to parse — %s", path.name, exc)
            continue
        if not data.get("success"):
            logger.warning("Skipping %s: extraction marked failed", county)
            continue
        scores.append(
            _score_county(data, args.inv_threshold, args.ab_min, args.ab_max)
        )

    scores.sort(key=lambda s: s["combined_score"], reverse=True)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"oh_county_scores_{datetime.now():%Y%m%d_%H%M%S}.csv"

    fieldnames = [
        "rank", "county", "combined_score",
        "foreclosure_score", "probate_score",
        "qualifying_zips", "ab_class_zips", "ab_class_pct",
        "inv_density", "foreclosure_reuse", "probate_reuse",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rank, row in enumerate(scores, 1):
            writer.writerow({"rank": rank, **row})

    print()
    print(f"Wrote {out_path}")
    print()
    print(f"  Rubric: inv_threshold={args.inv_threshold}  ab_band=${args.ab_min:,}-${args.ab_max:,}")
    print()
    header = f"  {'#':>2}  {'County':<14} {'Combined':>9}  {'Forecl':>7}  {'Prob':>6}  {'QZips':>5}  {'AB%':>5}  {'InvDens':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for rank, s in enumerate(scores[:25], 1):
        print(
            f"  {rank:>2}  {s['county']:<14} "
            f"{s['combined_score']:>9}  {s['foreclosure_score']:>7}  {s['probate_score']:>6}  "
            f"{s['qualifying_zips']:>5}  {s['ab_class_pct']*100:>4.0f}%  {s['inv_density']:>8}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
