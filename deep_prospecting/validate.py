"""Round-trip validation CLI for DataSift overlay CSVs.

Spot-check an overlay CSV before DataSift upload, verify operator-edited
CSVs, or sanity-check the weekly probate workflow's enriched output.

Usage:
    python -m deep_prospecting validate --csv path/to/any.csv
    python -m deep_prospecting validate --csv path/to/any.csv --out diff.csv

Exit codes:
    0  round-trip safe (ok=True)
    1  round-trip unsafe (ok=False) or input missing
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from deep_prospecting.datasift_csv_writer import (
    DROP_ON_WRITE,
    validate_round_trip,
)


def _write_diff_csv(in_csv: Path, out_diff: Path) -> int:
    """Re-parse src + roundtrip output and emit a row/col mismatch CSV.

    Columns: line, col_index, header, src_value, dst_value. Returns the
    number of mismatched cells written.
    """
    roundtrip_csv = in_csv.with_name(in_csv.stem + "_roundtrip.csv")

    def _parse(path: Path) -> list[list[str]]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    src_lines = _parse(in_csv)
    dst_lines = _parse(roundtrip_csv)
    if not src_lines:
        return 0

    src_header = src_lines[0]
    keep_indices = [
        i for i, h in enumerate(src_header) if h not in DROP_ON_WRITE
    ]
    src_filtered = [
        [ln[i] for i in keep_indices if i < len(ln)] for ln in src_lines
    ]
    kept_headers = [src_header[i] for i in keep_indices]

    mismatches = 0
    with open(out_diff, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["line", "col_index", "header", "src_value", "dst_value"])
        for i, (a, b) in enumerate(zip(src_filtered, dst_lines)):
            if a == b:
                continue
            max_len = max(len(a), len(b))
            for idx in range(max_len):
                av = a[idx] if idx < len(a) else ""
                bv = b[idx] if idx < len(b) else ""
                if av == bv:
                    continue
                header = kept_headers[idx] if idx < len(kept_headers) else ""
                w.writerow([i, idx, header, av, bv])
                mismatches += 1
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deep_prospecting validate",
        description="Round-trip validate an overlay CSV before upload.",
    )
    parser.add_argument(
        "--csv", required=True, type=Path,
        help="Path to the overlay CSV to validate",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Optional diff CSV path (written only when ok=False)",
    )
    args = parser.parse_args(argv)

    in_csv: Path = args.csv
    if not in_csv.is_file():
        print(f"error: input not found: {in_csv}", file=sys.stderr)
        return 1

    result = validate_round_trip(in_csv)
    ok = result.get("ok", False)
    diffs = result.get("differences", [])

    print(f"ok={ok}")
    print(f"differences={diffs}")

    if not ok and args.out is not None:
        n = _write_diff_csv(in_csv, args.out)
        print(f"wrote {n} mismatched cells → {args.out}")

    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
