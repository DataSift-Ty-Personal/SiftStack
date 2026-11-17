"""KPI source adapters register here, one line each.

    adapter id -> (module in this package, the adapter object in it)

The autopilot's stage kinds work the same way (autopilot/registry.py): a new tool is ONE module with its
`Provides`, its parser and its fixtures, plus one line here, and collect() picks it up. Nothing else changes.

No cloud_login adapter ships. Several tools' terms forbid reading their pages with a robot (Smarter Contact,
smrtPhone and Kixie among them, the adapter design section d1), so whether any tool gets one is the owner's
call. When one is added it must carry its tool's automation clause (terms_note), which connect() shows before
the connection can be switched on.
"""
from __future__ import annotations

import importlib

ADAPTERS: dict[str, tuple[str, str]] = {
    # the brain's own copy of the DataSift account log: calls, texts, phone statuses, stages
    "datasift_log": ("datasift_log", "ADAPTER"),
    # the autopilot's ledger and the brain's spend table: what was paid, and the mail pieces handed to a printer
    "ledger_spend": ("ledger_spend", "ADAPTER"),
    # any CSV or Excel export with a column map, and the agency files the agency results workflow keeps
    "file_import": ("file_import", "ADAPTER"),
    # a generic receiver for any tool that posts (Kixie, a Zapier trigger), mapped per tool
    "webhook": ("webhook", "ADAPTER"),
}


def adapter(name: str):
    """The adapter object for a registered id, imported the first time it is asked for. A module path with a dot
    in it is taken as written (a selftest stands a fake in that way)."""
    module, attr = ADAPTERS[name]
    path = module if "." in module else "%s.%s" % (__package__, module)
    return getattr(importlib.import_module(path), attr)


def problems() -> list[str]:
    """What is wrong with the table: a module that does not import, an adapter whose id is not its key, a
    declaration that is not a Provides, a cloud login with no automation clause. The selftest runs this so a bad
    line fails before a deploy."""
    from . import Adapter, Provides
    out = []
    for name, row in ADAPTERS.items():
        if not (isinstance(row, tuple) and len(row) == 2):
            out.append("%s: expected (module, adapter), found %r" % (name, row))
            continue
        try:
            ad = adapter(name)
        except Exception as exc:  # noqa: BLE001
            out.append("%s: %s.%s could not be loaded (%s)" % (name, row[0], row[1], str(exc)[:120]))
            continue
        if not isinstance(ad, Adapter):
            out.append("%s: %s.%s is not an Adapter" % (name, row[0], row[1]))
            continue
        if ad.id != name:
            out.append("%s: the adapter calls itself %r" % (name, ad.id))
        if not ad.provides or not all(isinstance(p, Provides) for p in ad.provides):
            out.append("%s: it does not declare what it provides" % name)
        if "cloud_login" in ad.modes() and not str(ad.terms_note or "").strip():
            out.append("%s: a cloud login must carry its tool's automation clause (terms_note)" % name)
        gap = getattr(ad, "normal_gap_hours", None)
        if gap is not None and not (isinstance(gap, (int, float)) and gap > 0):
            out.append("%s: normal_gap_hours must be a number of hours above zero, or None" % name)
    return out
