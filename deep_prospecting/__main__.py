"""CLI dispatch for `python -m deep_prospecting`.

Subcommands:
  run-batch    Run the orchestrator on every row of a DataSift CSV
  parse-bv     Fold a BeenVerified paste (HTML / markdown / text) into
               an existing research pack
  validate     Validate round-trip safety of an overlay CSV

Direct module invocation still works:
  python -m deep_prospecting.run_batch <csv>
  python -m deep_prospecting.parse_bv --case <slug> --input <path>
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="python -m deep_prospecting",
        description=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "run-batch", add_help=False,
        help="Run the orchestrator on every row of a DataSift CSV",
    )
    sub.add_parser(
        "parse-bv", add_help=False,
        help="Fold a BeenVerified paste into an existing research pack",
    )

    if not argv:
        parser.print_help(sys.stderr)
        return 2

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "run-batch":
        from deep_prospecting.run_batch import main as _bm
        return _bm(rest)
    if cmd == "parse-bv":
        from deep_prospecting.parse_bv import main as _pbm
        return _pbm(rest)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
