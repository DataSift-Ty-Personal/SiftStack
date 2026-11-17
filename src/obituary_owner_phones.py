"""Score the owner phone numbers already sitting on the top obituary records.

Every one of these records was skip traced by DataSift already, so the CRM holds
owner numbers before SmartSkip is involved at all. Scoring them is not just free
contact data, it is diagnostic: on an obituary record the owner may be the person
who died, and a dead person's line goes inactive. A record whose owner numbers
all score dead needs full heir resolution. A record with a live, high-activity
owner line may mean the OWNER IS ALIVE and somebody else died, which is the
spouse-obituary trap and the single most expensive mistake on this list.

Uses the Trestle free tier (1,000 lookups a month) by default.

Usage:
  python src/obituary_owner_phones.py --top 60 --dry-run
  python src/obituary_owner_phones.py --top 60 --commit
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import CACHE, assemble, parse_date  # noqa: E402

TRESTLE_ENDPOINT = "https://api.trestleiq.com/3.0/phone_intel"
TIERS = [("Dial First", 81, 100), ("Dial Second", 61, 80), ("Dial Third", 41, 60),
         ("Dial Fourth", 21, 40), ("Drop", 0, 20)]


def load_env():
    env = {}
    p = Path(".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def tier_for(score):
    if score is None:
        return "Unknown"
    for name, lo, hi in TIERS:
        if lo <= score <= hi:
            return name
    return "Unknown"


def collect(qualified, top_n, raw_by_uuid):
    """Owner phones for the top N, with whatever tier tag the CRM already holds."""
    out = []
    for i, r in enumerate(qualified[:top_n], 1):
        own = ((raw_by_uuid[r["uuid"]]["record"] or {}).get("owner") or {})
        for p in (own.get("phones") or []):
            num = "".join(ch for ch in str(p.get("number") or "") if ch.isdigit())
            if len(num) == 11 and num.startswith("1"):
                num = num[1:]
            if len(num) != 10:
                continue
            out.append({
                "rank": i, "uuid": r["uuid"], "address": r["street"], "city": r["city"],
                "owner": r["owner_name"], "score": r["score"],
                "number": num, "crm_type": p.get("type"),
                "crm_status": p.get("status"),
                "crm_tags": ", ".join(p.get("tags") or []),
                "already_scored": bool(p.get("tags")),
            })
    return out


def call_trestle(number, api_key, timeout=25):
    url = TRESTLE_ENDPOINT + "?" + urllib.parse.urlencode({"phone": number})
    req = urllib.request.Request(url, headers={"x-api-key": api_key,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}"), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        return {}, f"HTTP {e.code}: {body}"
    except Exception as e:
        return {}, str(e)[:160]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--min-months", type=int, default=3)
    ap.add_argument("--outdir", default="output/dp")
    ap.add_argument("--commit", action="store_true", help="actually call Trestle")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--paid", action="store_true", help="use the paid key instead of the free tier")
    ap.add_argument("--rescore-all", action="store_true",
                    help="also rescore numbers that already carry a CRM tier tag")
    args = ap.parse_args()

    env = load_env()
    today = date.today()
    qualified, _, _ = assemble(Path(args.cache), args.min_months, today)
    blob = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    raw = {x["uuid"]: x for x in blob["records"]}

    rows = collect(qualified, args.top, raw)
    todo = [r for r in rows if args.rescore_all or not r["already_scored"]]
    uniq = sorted({r["number"] for r in todo})

    print(f"top {args.top} records: {len(rows)} owner phone numbers on file")
    print(f"  already carry a CRM dial tier : {sum(1 for r in rows if r['already_scored'])}")
    print(f"  to score now (unique numbers) : {len(uniq)}")
    key_name = "TRESTLE_PAID_API_KEY" if args.paid else "TRESTLE_FREE_API_KEY"
    api_key = env.get(key_name) or env.get("TRESTLE_API_KEY") or ""
    print(f"  key: {key_name}"
          + ("" if args.paid else f" (free tier, {env.get('TRESTLE_FREE_LIMIT','?')}/month)"))
    if not args.commit or args.dry_run:
        print(f"\nDRY RUN. Rerun with --commit to score {len(uniq)} numbers.")
        return
    if not api_key:
        print(f"\nNo {key_name} in .env, nothing scored.")
        return

    scored, errors = {}, []
    t0 = time.time()
    for i, n in enumerate(uniq, 1):
        d, err = call_trestle(n, api_key)
        if err:
            errors.append({"number": n, "error": err})
            if "429" in err or "quota" in err.lower() or "limit" in err.lower():
                print(f"  stopping at {i}: {err}")
                break
        else:
            s = d.get("activity_score")
            scored[n] = {"activity_score": s, "tier": tier_for(s),
                         "line_type": d.get("line_type"), "is_valid": d.get("is_valid"),
                         "carrier": d.get("carrier") or ""}
        if i % 25 == 0:
            print(f"    {i}/{len(uniq)}  ({time.time()-t0:.0f}s)", flush=True)
        time.sleep(0.12)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for r in rows:
        s = scored.get(r["number"], {})
        r["activity_score"] = s.get("activity_score")
        r["new_tier"] = s.get("tier")
        r["line_type"] = s.get("line_type")
        r["is_valid"] = s.get("is_valid")

    with (outdir / "owner_phone_scores.csv").open("w", newline="", encoding="utf-8") as fh:
        cols = ["rank", "address", "city", "owner", "score", "number", "crm_type",
                "crm_status", "crm_tags", "activity_score", "new_tier", "line_type",
                "is_valid", "uuid"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # Per record: does this owner still have a live line?
    per = {}
    for r in rows:
        p = per.setdefault(r["rank"], {"rank": r["rank"], "address": r["address"],
                                       "owner": r["owner"], "numbers": 0, "live": 0,
                                       "best": None, "best_tier": "Unscored"})
        p["numbers"] += 1
        sc = r.get("activity_score")
        if sc is None and r["crm_tags"]:
            continue
        if sc is not None:
            if sc >= 41:
                p["live"] += 1
            if p["best"] is None or sc > p["best"]:
                p["best"], p["best_tier"] = sc, r["new_tier"]

    print(f"\nscored {len(scored)} numbers, {len(errors)} errors, {time.time()-t0:.0f}s")
    print(f"  tier mix: {dict(Counter(v['tier'] for v in scored.values()))}")
    dead = [p for p in per.values() if p["numbers"] and p["live"] == 0 and p["best"] is not None]
    live = [p for p in per.values() if p["live"] > 0]
    print(f"  records with at least one live owner line : {len(live)}")
    print(f"  records where every owner line is dead    : {len(dead)}  "
          f"(these need full heir resolution)")
    (outdir / "owner_phone_summary.json").write_text(json.dumps(
        {"numbers_on_file": len(rows), "scored": len(scored), "errors": errors[:20],
         "tier_mix": dict(Counter(v["tier"] for v in scored.values())),
         "records_with_live_line": len(live), "records_all_dead": len(dead),
         "per_record": sorted(per.values(), key=lambda x: x["rank"])}, indent=1),
        encoding="utf-8")
    print(f"\nwrote {outdir}/owner_phone_scores.csv and owner_phone_summary.json")


if __name__ == "__main__":
    main()
