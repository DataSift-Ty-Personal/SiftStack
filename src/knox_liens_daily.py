"""Daily Knox lien refresh: pull the recorder, find NEW debtors, match them to parcels.

Until 2026-11-11 liens were one hand-run 12-month pull. Nothing scheduled it,
the paid Register of Deeds subscription lapsed the day after, and no lien
reached DataSift for 11 weeks. This is the piece that runs every morning.

It only PREPARES. It writes into the scratch folder the county stage already
reads, and `knox_ftm_pull` + the uploader do the rest:

  rod2_<TYPE>.json         raw recorder rows for the window (overwritten each run)
  lien_releases.json       every released instrument seen so far (cumulative)
  lien_amounts.json        instrument -> recorded amount (cumulative)
  knox_lien_parcels.json   ONLY debtors with a lien not yet sent to DataSift
  lien_pending.json        the instruments that file accounts for
  lien_uploaded.json       instruments already handled (grows after an upload)

Three rules, each one a way this could look healthy while doing nothing:

  * A debtor is NEW when it holds an instrument that is not in
    lien_uploaded.json. Without that ledger the county stage would re-send the
    whole month every morning.
  * Instruments are marked handled only AFTER a successful upload
    (`mark_uploaded`), the same ordering rule as the notice seen-id cache: a
    failed run must leave them to be picked up tomorrow.
  * A debtor the county tax roll cannot match is marked handled too. Six in
    ten never match, each name search costs about 15 seconds, and re-trying
    them daily would turn a 5 minute stage into an hour.

    python src/knox_liens_daily.py --scratch output/knox            # prepare only
    python src/knox_liens_daily.py --scratch output/knox --months 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import knox_lien_index  # noqa: E402
import knox_lien_resolve  # noqa: E402

LIEN_TYPES = ("LEN", "STL", "FTL")
RELEASE_TYPES = ("REL", "RFTL", "RESL", "RSTL")
# Early in a month the previous month is pulled as well, so a filing indexed a
# few days late is not lost in the gap between two windows.
LOOKBACK_DAYS = 5
RESOLVE_WORKERS = 4      # the county name search is slow; more workers only queue


def months_to_pull(today: date | None = None) -> int:
    today = today or date.today()
    return 2 if today.day <= LOOKBACK_DAYS else 1


def _read(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=list)
    os.replace(tmp, path)


def pull(scratch: str, months: int, sleep: float = 2.0) -> dict:
    """One browser login, then a few large requests. Returns counts and problems.

    A type that fails is a PROBLEM, not an empty type: with releases missing
    the list would carry paid-off liens as live, so the caller refuses to go on.
    """
    import asyncio
    import time

    import knox_rod

    fields, headers = asyncio.run(knox_rod.capture_request())
    windows = knox_rod._month_windows(months)
    raw: dict[str, list] = {t: [] for t in LIEN_TYPES + RELEASE_TYPES}
    problems: list[str] = []
    for d_from, d_to in windows:
        for doc in LIEN_TYPES + RELEASE_TYPES:
            try:
                raw[doc].extend(knox_rod.fetch_type(fields, headers, doc, d_from, d_to, sleep=sleep))
            except Exception as exc:
                problems.append("%s %s: %s: %s" % (doc, d_from, type(exc).__name__, str(exc)[:120]))
            time.sleep(sleep)
    if not problems:
        for doc, rows in raw.items():
            _write(os.path.join(scratch, "rod2_%s.json" % doc), {"aaData": rows})
    return {"rows": {k: len(v) for k, v in raw.items()}, "problems": problems,
            "window": "%s .. %s" % (windows[0][0], windows[-1][1]),
            "subscription_days": (getattr(knox_rod, "SUBSCRIPTION", {}) or {}).get("days")}


def _instruments(debtor: dict) -> list[str]:
    """Every instrument on the debtor, not the 8 kept for the note."""
    return debtor.get("all_instruments") or debtor.get("instruments") or []


def _resolve_all(debtors: list[dict], workers: int = RESOLVE_WORKERS) -> list[dict]:
    if not debtors:
        return []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(knox_lien_resolve.resolve, debtors))


def prepare(scratch: str, months: int | None = None, puller=None, resolver=None) -> dict:
    """Pull, rebuild the lookups, and stage the debtors that are new."""
    os.makedirs(scratch, exist_ok=True)
    months = months or months_to_pull()
    pulled = (puller or pull)(scratch, months)
    if pulled.get("problems"):
        raise RuntimeError("Register of Deeds pull incomplete: " + "; ".join(pulled["problems"][:3]))

    index = knox_lien_index.build(scratch)

    handled = set(_read(os.path.join(scratch, "lien_uploaded.json"), []))
    debtors = knox_lien_resolve.load_debtors(scratch)
    fresh = [d for d in debtors if any(i not in handled for i in _instruments(d))]
    resolved = (resolver or _resolve_all)(fresh)

    pending = sorted({i for d in fresh for i in _instruments(d)})
    _write(os.path.join(scratch, "knox_lien_parcels.json"), resolved)
    _write(os.path.join(scratch, "lien_pending.json"), pending)

    leads = [d for d in resolved if d.get("n_parcels") and not d.get("institutional")]
    return {
        "window": pulled.get("window", ""),
        "lien_rows": sum(pulled["rows"].get(t, 0) for t in LIEN_TYPES),
        "release_rows": sum(pulled["rows"].get(t, 0) for t in RELEASE_TYPES),
        "debtors_in_window": len(debtors),
        "new_debtors": len(fresh),
        "with_parcels": len(leads),
        "parcels": sum(d["n_parcels"] for d in leads),
        "lookup_errors": sum(1 for d in resolved if d.get("error")),
        "released_total": index["released"],
        "subscription_days": pulled.get("subscription_days"),
    }


def mark_uploaded(scratch: str) -> int:
    """Call ONLY after the upload succeeded. Returns how many instruments moved."""
    pending_path = os.path.join(scratch, "lien_pending.json")
    pending = _read(pending_path, [])
    if not pending:
        return 0
    done_path = os.path.join(scratch, "lien_uploaded.json")
    done = set(_read(done_path, []))
    before = len(done)
    done.update(pending)
    _write(done_path, sorted(done))
    _write(pending_path, [])
    # Emptied so a later run that skips this stage cannot re-send yesterday's liens.
    _write(os.path.join(scratch, "knox_lien_parcels.json"), [])
    return len(done) - before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--months", type=int, default=0, help="0 = current month (plus the last one early in a month)")
    ap.add_argument("--mark-uploaded", action="store_true",
                    help="record the staged instruments as handled, without pulling")
    a = ap.parse_args()
    if a.mark_uploaded:
        print("instruments marked handled:", mark_uploaded(a.scratch))
        return 0
    stats = prepare(a.scratch, a.months or None)
    for k, v in stats.items():
        print("%-20s %s" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
