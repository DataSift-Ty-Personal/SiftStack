"""Dream 100: the hundred largest opportunities in the D4D top 3,000.

Joins three things the account already holds for every D4D record:
  * the Driving for Dollars visual distress rank (output/d4d_call_list/cohort.json),
  * the record's own DataFlik scores (investor_score, realtor_score, on_market_score)
    plus every deal-size field on the CRM record (value, equity, mortgage, taxes,
    last sale, rental value, vacancy, age, deceased, dates),
  * the lists actually attached to the record (Tired Landlord, Vacant, Tax
    Delinquent, Pre-Foreclosure, Probate, ...), which are the account's own
    distress evidence,
and the phone tiers the D4D run already bought (phone_scores.json).

    python src/dream100.py --phase pull          # full CRM records (resumable)
    python src/dream100.py --phase build         # score, rank, write the workbook
    python src/dream100.py --phase attach [--commit]   # list + tag "Dream 100" on the top rows

pull and build are read-only. attach is the ONLY write, DRY by default, and it
reads one record back before touching the rest.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import d4d_call_list as d4d  # noqa: E402

OUT = Path("output/dream100")
FULL_PATH = OUT / "full_records.json"


def _load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=0, default=str), encoding="utf-8")
    tmp.replace(p)


def phase_pull(args) -> None:
    cohort = _load(d4d.COHORT_PATH, [])
    full = _load(FULL_PATH, {})
    todo = [r for r in cohort if r.get("saved_uuid") and (args.refresh or r["saved_uuid"] not in full)]
    print("pull: %d to read, %d cached" % (len(todo), len(full)), flush=True)
    api = d4d.Api()
    failed = 0
    t0 = time.time()
    for n, r in enumerate(todo, 1):
        u = r["saved_uuid"]
        try:
            rec = d4d._call(api, "/api/internal/property/%s/" % u)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  read failed %s: %s" % (u, str(e)[:120]), flush=True)
            continue
        rec["_fetched_at"] = datetime.now().isoformat(timespec="seconds")
        full[u] = rec
        if n % 100 == 0:
            _save(FULL_PATH, full)
            rate = n / max(time.time() - t0, 1)
            print("  %d/%d  (%.2f/s, %d failed)" % (n, len(todo), rate, failed), flush=True)
    _save(FULL_PATH, full)
    print("pull: done, %d records, %d failed" % (len(full), failed), flush=True)


ATTACH_LIST = "Dream 100"
ATTACH_TAG = "Dream 100"


def phase_attach(args) -> None:
    """Attach the Dream 100 list + tag to the ranked top rows (accumulates)."""
    ranked = _load(OUT / "dream100_ranked.json", [])
    if not ranked:
        raise SystemExit("no ranking; run --phase build first")
    top = ranked[: args.top]
    api = d4d.Api()
    for kind, title in (("list", ATTACH_LIST), ("tag", ATTACH_TAG)):
        d4d._ensure_title(api, kind, title, args.commit)
    if not args.commit:
        print("attach: DRY RUN, would attach list %r + tag %r to %d records; re-run with --commit" % (
            ATTACH_LIST, ATTACH_TAG, len(top)))
        return
    state = _load(OUT / "attach_state.json", {})
    body = {"lists": [ATTACH_LIST], "tags": [ATTACH_TAG]}
    # one first, read back, then the rest
    first = top[0]
    d4d.attach_to_record(api, first["uuid"], body)
    rec = d4d._call(api, "/api/internal/property/%s/" % first["uuid"])
    lists = [(l.get("title") if isinstance(l, dict) else l) for l in rec.get("lists") or []]
    tags = d4d.tag_names(rec)
    if ATTACH_LIST not in lists or ATTACH_TAG not in tags:
        raise SystemExit("read-back FAILED on %s: lists=%s tags=%s" % (first["uuid"], lists, tags))
    print("attach: read-back OK on rank 1 (%s)" % first["street"], flush=True)
    state[first["uuid"]] = "ok"
    done = 0
    for r in top[1:]:
        if state.get(r["uuid"]) == "ok":
            continue
        try:
            d4d.attach_to_record(api, r["uuid"], body)
            state[r["uuid"]] = "ok"
            done += 1
        except Exception as e:  # noqa: BLE001
            state[r["uuid"]] = "failed: %s" % str(e)[:120]
            print("  failed %s: %s" % (r["street"], str(e)[:120]), flush=True)
        if done % 20 == 0:
            _save(OUT / "attach_state.json", state)
    _save(OUT / "attach_state.json", state)
    ok = sum(1 for v in state.values() if v == "ok")
    print("attach: %d of %d records carry list %r + tag %r" % (ok, len(top), ATTACH_LIST, ATTACH_TAG))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pull", "build", "attach"], required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args(argv)
    if args.phase == "pull":
        phase_pull(args)
    elif args.phase == "attach":
        phase_attach(args)
    else:
        from dream100_build import phase_build  # noqa: F401
        phase_build(args)


if __name__ == "__main__":
    main()
