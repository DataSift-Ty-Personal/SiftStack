"""Stage kinds added after the first build register here, one line each.

    kind -> (module in this package, run function, spend class, needs a preset, always reports)

The first build listed its kinds in policy.KINDS and an if-chain in sweep._runner, so every new
kind meant edits in three files, and two builds adding stages on the same day met on the same
lines. A new kind now adds its own module and ONE line here; policy and sweep read this table.

  spend class     free, paid or outreach (policy.SPEND_BY_KIND)
  needs a preset  False for a kind that reads the account without preset titles of its own
  always reports  True for a kind whose run is a report: it posts even when it changed nothing
"""
from __future__ import annotations

import importlib

EXTRA: dict[str, tuple[str, str, str, bool, bool]] = {
}


def runner(kind: str):
    """The run function for a registered kind, imported the first time it is asked for."""
    module, fn = EXTRA[kind][0], EXTRA[kind][1]
    return getattr(importlib.import_module("%s.%s" % (__package__, module)), fn)


def problems() -> list[str]:
    """What is wrong with the table itself: a module that does not import, a function that is not
    there, a spend class nobody knows. Checked by the selftest so a bad line fails before a deploy."""
    out = []
    for kind, row in EXTRA.items():
        if len(row) != 5:
            out.append("%s: expected five fields, found %d" % (kind, len(row)))
            continue
        if row[2] not in ("free", "paid", "outreach"):
            out.append("%s: spend class %r is not free, paid or outreach" % (kind, row[2]))
        try:
            if not callable(runner(kind)):
                out.append("%s: %s.%s is not a function" % (kind, row[0], row[1]))
        except Exception as exc:  # noqa: BLE001
            out.append("%s: %s.%s could not be loaded (%s)" % (kind, row[0], row[1], str(exc)[:120]))
    return out
