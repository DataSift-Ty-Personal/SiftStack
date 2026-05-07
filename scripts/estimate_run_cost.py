"""CLI wrapper around src/cost_estimator.

Usage:
  python scripts/estimate_run_cost.py output/this_week_run_2026-05-06/2026-05-06/*.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src/ to path so this script works run from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cost_estimator import tally_csv, render_report  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python scripts/estimate_run_cost.py <csv_paths...>", file=sys.stderr)
        return 1
    paths = [Path(p) for p in argv[1:]]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"NOT FOUND: {p}", file=sys.stderr)
        return 1
    tallies = [tally_csv(p) for p in paths]
    print(render_report(tallies))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
