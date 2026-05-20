"""CLI dispatch for `python -m deep_prospecting`.

Subcommands:
  run-batch    Run the orchestrator on every row of a DataSift CSV
  parse-bv     Fold a BeenVerified paste (HTML / markdown / text) into
               an existing research pack
  validate     Round-trip validate an overlay CSV before DataSift upload

Direct module invocation still works:
  python -m deep_prospecting.run_batch <csv>
  python -m deep_prospecting.parse_bv --case <slug> --input <path>
  python -m deep_prospecting.validate --csv <path>
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deep_prospecting",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    sub.add_parser(
        "validate", add_help=False,
        help="Round-trip validate an overlay CSV before DataSift upload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Top-level --help / -h must short-circuit to argparse's own help
    # exit. Without this, the manual cmd-dispatch below treats "--help"
    # as an unknown command and returns 2.
    if not argv or argv[0] in ("-h", "--help"):
        _build_parser().print_help(sys.stdout)
        return 0

    parser = _build_parser()
    cmd = argv[0]
    rest = argv[1:]

    if cmd == "run-batch":
        from deep_prospecting.run_batch import main as _bm
        return _bm(rest)
    if cmd == "parse-bv":
        from deep_prospecting.parse_bv import main as _pbm
        return _pbm(rest)
    if cmd == "validate":
        from deep_prospecting.validate import main as _vm
        return _vm(rest)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
