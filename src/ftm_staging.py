"""Stage now, mark handled only after the upload: one small ledger for the county-stage feeds.

The lien stage already works this way and the rule is why it is safe. A feed PREPARES rows into
the scratch folder; the county stage uploads them; only then are they recorded as handled. The
order is the whole point. The notice scrape once marked 204 notices as seen while the run was
still going, the run was blocked before its upload, and a retry would have skipped every one of
them for good.

This module gives every new county-stage feed (substitute trustee appointments, the county tax
sale report, the code violation file) that same ledger instead of three hand-rolled copies:

    <feed>_pending.json   {"rows": [...], "keys": [...]}   what the next county upload carries
    <feed>_handled.json   {key: "YYYY-MM-DD"}              what has been uploaded, or settled as a miss

A key is whatever identifies one filing for that feed (an instrument number, a parcel and sale,
a case number). `keys` can be longer than `rows`: a filing that resolved to nothing (no parcel,
ambiguous owner) is settled too, or it would be looked up again every morning forever.

With no pending file there is nothing to do and nothing is written, which is what keeps the
county stage's daily run unchanged until a feed is switched on.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

# The feeds that stage through this ledger, in the order the county stage settles them.
STAGED_FEEDS = ("substitute_trustee", "tax_sale_report", "code_violations")

# The runner stage that prepares each feed. The county stage loads, uploads and settles ONLY the
# feeds whose stage is in the same run. A pending file left by a hand-run of one of these stages
# (a dry run on the box, say) must never ride into the scheduled upload of a run that did not ask
# for that feed, or a switched-off feed would upload anyway.
STAGE_OF = {"substitute_trustee": "appointments", "tax_sale_report": "tax_sale_report",
            "code_violations": "code_violations"}


def feeds_for_stages(stages) -> tuple:
    """The staged feeds a run with these stages may load and settle, in STAGED_FEEDS order."""
    want = {str(s).strip() for s in (stages or []) if str(s).strip()}
    return tuple(f for f in STAGED_FEEDS if STAGE_OF.get(f) in want)
PRUNE_DAYS = 800      # a handled key older than this is forgotten; no source re-lists a filing this old


def _path(scratch: str, feed: str, kind: str) -> str:
    return os.path.join(scratch, "%s_%s.json" % (feed, kind))


def _read(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def handled(scratch: str, feed: str) -> dict:
    """{key: day it was settled} for one feed."""
    got = _read(_path(scratch, feed, "handled"), {})
    return got if isinstance(got, dict) else {}


def pending(scratch: str, feed: str) -> dict:
    got = _read(_path(scratch, feed, "pending"), {})
    if not isinstance(got, dict):
        return {"rows": [], "keys": []}
    return {"rows": list(got.get("rows") or []), "keys": list(got.get("keys") or [])}


def pending_rows(scratch: str, feed: str) -> list[dict]:
    return pending(scratch, feed)["rows"]


def is_new(scratch: str, feed: str, key: str) -> bool:
    """True when this filing has neither been uploaded nor settled as a miss."""
    return bool(key) and key not in handled(scratch, feed)


def stage(scratch: str, feed: str, rows: list[dict], keys: list[str]) -> dict:
    """Write what the next county upload should carry for this feed, REPLACING what was staged.

    Replacing is deliberate. A run that staged rows and was then cut off before the county stage
    leaves them pending; the next morning's pull finds the same filings still unhandled, resolves
    them again and stages the fresh result. Appending would upload them twice."""
    keys = [k for k in dict.fromkeys(str(k) for k in keys) if k]
    _write(_path(scratch, feed, "pending"), {"rows": rows, "keys": keys, "staged": date.today().isoformat()})
    return {"rows": len(rows), "keys": len(keys)}


def mark_uploaded(scratch: str, feed: str, today: str | None = None) -> int:
    """Call ONLY after the county upload succeeded. Returns how many filings were settled.

    A missing or empty pending file settles nothing and writes nothing."""
    pend = pending(scratch, feed)
    if not pend["keys"] and not pend["rows"]:
        return 0
    day = today or date.today().isoformat()
    done = handled(scratch, feed)
    added = 0
    for key in pend["keys"]:
        if key not in done:
            done[key] = day
            added += 1
    cutoff = (date.fromisoformat(day) - timedelta(days=PRUNE_DAYS)).isoformat()
    done = {k: v for k, v in done.items() if str(v) >= cutoff}
    _write(_path(scratch, feed, "handled"), done)
    # Emptied so a later run that skips the feed's own stage cannot re-send yesterday's rows.
    _write(_path(scratch, feed, "pending"), {"rows": [], "keys": [], "staged": ""})
    return added


def mark_all_uploaded(scratch: str, today: str | None = None, feeds=None) -> dict:
    """Settle the staged feeds (all of them, or only `feeds`). For the county stage, after its
    upload has landed; it passes exactly the feeds it loaded, so nothing it did not send is settled."""
    out = {}
    for feed in (STAGED_FEEDS if feeds is None else feeds):
        n = mark_uploaded(scratch, feed, today)
        if n:
            out[feed] = n
    return out


def anything_pending(scratch: str) -> bool:
    return any(pending(scratch, feed)["keys"] or pending(scratch, feed)["rows"] for feed in STAGED_FEEDS)
