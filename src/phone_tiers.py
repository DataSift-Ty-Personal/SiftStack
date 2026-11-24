"""Trestle-score the numbers on a set of DataSift records and write one dial tier on each phone, on the account's
own Open API key.

  python src/phone_tiers.py --list Foreclosure                 # dry: how many numbers, what scoring would cost
  python src/phone_tiers.py --list Foreclosure --commit        # score, write the tiers, read a sample back
  python src/phone_tiers.py --uuids <uuid>,<uuid> --commit     # named records

What gets scored: a phone on the record's owner that has no tier yet, is not dispositioned wrong, dead or DNC, is not
flagged a litigator, and is a real 10-digit number. A number is scored once: the answer is kept in
output/phone_tiers/scores.json and never bought again. Tiers (activity score): Dial First 81-100, Dial Second 61-80,
Dial Third 41-60, Dial Fourth 21-40, Drop 0-20; a litigator risk is tagged DNC instead of a tier.

Writes go once per owner through POST /api/internal/owner/<uuid>/upsert-phones/ with each changed phone's full
object, so a phone ends with exactly one tier (a re-scored phone loses its old one). Then a sample of owners is read
back; any phone without exactly the tier written stops the run. Trestle costs about $0.015 a number
(TRESTLE_API_KEY in .env). tracerfy_ftm.py and enformion_ftm.py call tier_records() after they add numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import d4d_call_list as d4d  # noqa: E402
import phone_validator as pv  # noqa: E402

OUT = Path(os.environ.get("PHONE_TIERS_DIR") or "output/phone_tiers")
SCORES = OUT / "scores.json"
RECORDS = "/api/internal/property/"
WALL = 10000


def trestle_key() -> str:
    e = d4d.env()
    return (os.environ.get("TRESTLE_API_KEY") or e.get("TRESTLE_API_KEY")
            or os.environ.get("TRESTLE_PAID_API_KEY") or e.get("TRESTLE_PAID_API_KEY") or "").strip()


def list_rows(api, list_name: str, **must) -> list[dict]:
    """Records-search rows for every record on a list (up to the search's 10,000-row window)."""
    import pull_by_ids as pb  # noqa: PLC0415
    uuid = pb.list_uuid(api, list_name)
    d4d.gate(bool(uuid), "there is no list called %r in this account" % list_name)
    rows, offset = [], 0
    while offset < WALL:
        r = d4d._call(api, RECORDS, "POST", headers={"x-http-method-override": "GET"},
                      body={"limit": 100, "offset": offset, "query": {"must": {"any_lists": [uuid], **must}}})
        page = (r or {}).get("results") or []
        rows += page
        offset += len(page)
        if not page or offset >= int((r or {}).get("count") or 0):
            break
    return rows


def read_records(api, uuids: list[str], out=print) -> dict:
    """uuid -> the record's fields and its owner's phones (one read each)."""
    recs = {}
    for n, u in enumerate(dict.fromkeys(uuids), 1):
        try:
            rec = d4d._call(api, "/api/internal/property/%s/" % u)
        except Exception as e:  # noqa: BLE001  (a record removed since the list was read)
            out("  could not read %s: %s" % (u, str(e)[:100]))
            continue
        recs[u] = d4d._slim(rec or {}, str((rec or {}).get("dataflik_id") or ""), False)
        if n % 100 == 0:
            out("  read %d of %d records" % (n, len(uuids)))
    return recs


def score(nums: list[str], cache: dict, key: str, workers: int = 4, out=print) -> dict:
    """Score each number not in the cache; the cache is saved as it fills. Returns the counts."""
    router = d4d._router()
    d4d.gate(router is not None or bool(key), "no Trestle key: put TRESTLE_API_KEY in .env")
    todo = [n for n in nums if not (cache.get(n) or {}).get("tier")]
    done = errors = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(d4d._score_one, router, key, n): n for n in todo}
        for f in as_completed(futs):
            n = futs[f]
            try:
                cache[n] = f.result()
            except Exception as e:  # noqa: BLE001
                cache[n] = {"error": str(e)[:120]}
            errors += 1 if cache[n].get("error") else 0
            done += 1
            if done % 50 == 0:
                d4d._save(SCORES, cache)
                out("  scored %d of %d (%d errors)" % (done, len(todo), errors))
    d4d._save(SCORES, cache)
    if router is not None:
        router.flush()
    return {"scored": done, "errors": errors}


def write(api, recs: dict, cache: dict, readback: int = 10, out=print) -> dict:
    """Write the tiers once per owner, then read a sample back. A mismatch stops the run."""
    plan = d4d.planned_writes(recs, cache)
    failed = 0
    for owner, v in plan.items():
        try:
            d4d._call(api, "/api/internal/owner/%s/upsert-phones/" % owner, "POST", {"phones": v["phones"]})
        except Exception as e:  # noqa: BLE001
            failed += 1
            out("  write failed for owner %s: %s" % (owner, str(e)[:120]))
    bad = 0
    sample = random.sample(sorted(plan), min(readback, len(plan)))
    for owner in sample:
        v = plan[owner]
        rec = d4d._call(api, "/api/internal/property/%s/" % v["record"]) or {}
        phones = {pv.clean_phone(p.get("number") or ""): p for p in (rec.get("owner") or {}).get("phones") or []}
        for num, tier in v["expect"].items():
            got = [t for t in d4d.tag_names(phones.get(num) or {}) if t in d4d.TIER_NAMES or t == d4d.LITIGATOR_TAG]
            if got != [tier]:
                bad += 1
                out("  read-back: %s on record %s carries %s, not [%s]" % (num[-4:].rjust(10, "*"), v["record"], got, tier))
    return {"owners": len(plan), "phones": sum(len(v["phones"]) for v in plan.values()), "failed": failed,
            "read_back": len(sample), "mismatches": bad}


def tier_records(api, uuids: list[str], *, commit: bool = False, workers: int = 4, readback: int = 10,
                 out=print) -> dict:
    """Score the untiered numbers on these records and write one tier on each. Dry unless commit."""
    recs = read_records(api, uuids, out=out)
    elig = d4d.eligible_numbers(recs)
    cache = d4d._load(SCORES, {})
    todo = [n for n in elig if not (cache.get(n) or {}).get("tier")]
    res = {"records": len(recs), "numbers": len(elig), "already_scored": len(elig) - len(todo), "to_score": len(todo),
           "estimate_usd": round(len(todo) * pv.COST_PER_PHONE, 2), "committed": bool(commit)}
    out("tiers: %d records, %d numbers without a tier, %d scored before, %d to score (about $%.2f)" % (
        res["records"], res["numbers"], res["already_scored"], res["to_score"], res["estimate_usd"]))
    if not commit:
        return res
    if todo:
        res.update(score(todo, cache, trestle_key(), workers, out=out))
        d4d.gate(res["errors"] < max(1, len(todo) // 5), "more than one in five scoring calls failed (%d)" % res["errors"])
    res["by_tier"] = dict(Counter((cache.get(n) or {}).get("tier") or "error" for n in elig))
    res.update(write(api, recs, cache, readback, out=out))
    out("tiers: %s; wrote %d phones on %d owners, %d failed; read back %d owners, %d mismatches" % (
        json.dumps(res["by_tier"]), res["phones"], res["owners"], res["failed"], res["read_back"], res["mismatches"]))
    d4d.gate(res["mismatches"] == 0, "%d phones did not read back with the tier written" % res["mismatches"])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--list", dest="list_name", help="every record on this DataSift list")
    who.add_argument("--uuids", help="comma list of record uuids")
    ap.add_argument("--commit", action="store_true", help="score (paid) and write the tiers; dry without it")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--readback", type=int, default=10, help="owners read back after the write")
    args = ap.parse_args(argv)
    api = d4d.Api()
    if args.list_name:
        uuids = [r["uuid"] for r in list_rows(api, args.list_name, phone=1) if r.get("uuid")]
    else:
        uuids = [u.strip() for u in args.uuids.split(",") if u.strip()]
    res = tier_records(api, uuids, commit=args.commit, workers=args.workers, readback=args.readback)
    d4d._save(OUT / ("run_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")), res)
    if not args.commit:
        print("DRY RUN. Re-run with --commit to score and write the tiers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
