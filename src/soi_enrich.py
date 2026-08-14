"""SOI stage 3: enrich matched contacts with SiftMap property detail + AI scores.

For each owner-matched contact: autocomplete the county situs address ->
get_detail(dataflik_id) -> realtor_score / investor scores / value / equity /
portfolio. The county owner name must token-overlap SiftMap's owner_info, or the
row is flagged owner_mismatch instead of trusted (stale roll or wrong candidate).

Resumable: checkpoints to output/soi_enrich_state.json after every record.

Usage:
    python src/soi_enrich.py                       # status=unique matches
    python src/soi_enrich.py --status unique,ambiguous --limit 20
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("REISIFT_ACCOUNT", "datasift-apikey")
sys.path.insert(0, r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients")

STATE_PATH = Path("output/soi_enrich_state.json")

SCORE_FIELDS = ("realtor_score", "investor_on_market_score", "investor_off_market_score")


def norm_tokens(s):
    return {t for t in re.sub(r"[^A-Z ]", " ", (s or "").upper()).split() if len(t) >= 3}


def addr_line(cand):
    m = cand["candidates"][0]
    city = m["situs_city"] or ""
    return f"{m['situs_addr']}, {city}, OH".strip()


def enrich_one(client, rec):
    m = rec["candidates"][0]
    query = addr_line(rec)
    cands = client.autocomplete(query)
    if not cands:
        return {"status": "no_autocomplete", "query": query}
    best = cands[0]
    detail = client.get_detail(best.get("dataflik_id") or best.get("id"))
    oi = detail.get("owner_info") or {}
    sift_owner = " ".join(filter(None, [oi.get("owner_name") or ""]
                                 + list(oi.get("secondary_owner_names") or [])))
    county_owner = f"{m['owner1']} {m['owner2']}"
    overlap = norm_tokens(sift_owner) & norm_tokens(county_owner)
    out = {
        "status": "ok" if overlap else "owner_mismatch",
        "dataflik_id": best.get("dataflik_id") or best.get("id"),
        "sift_address": best.get("address") or query,
        "sift_owner": sift_owner.strip(),
        "county_owner": county_owner.strip(),
        "total_market_value": detail.get("total_market_value"),
        "last_sale_date": detail.get("last_sale_date"),
        "last_sale_price": detail.get("last_sale_price"),
        "total_equity": oi.get("total_equity"),
        "total_mortgage": oi.get("total_mortgage"),
        "total_properties": oi.get("total_properties"),
        "portfolio_value": oi.get("portfolio_value"),
        "property_type": detail.get("property_type"),
        "years_built": detail.get("years_built"),
    }
    for f in SCORE_FIELDS:
        out[f] = detail.get(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", default="output/soi_owner_matches.json")
    ap.add_argument("--status", default="unique")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="output/soi_enriched")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    from siftmap_api import SiftMapClient, SiftMapError

    statuses = set(args.status.split(","))
    matches = [r for r in json.load(open(args.matches, encoding="utf-8"))
               if r["status"] in statuses and r["candidates"]]
    if args.limit:
        matches = matches[:args.limit]

    state = json.load(open(STATE_PATH, encoding="utf-8")) if STATE_PATH.exists() else {}
    client = SiftMapClient()
    done = errors = 0
    for rec in matches:
        key = rec["contact"]["email"] or f"{rec['contact']['first_name']}|{rec['contact']['last_name']}"
        if key in state:
            continue
        try:
            state[key] = enrich_one(client, rec)
        except SiftMapError as e:
            errors += 1
            state[key] = {"status": "error", "error": str(e)[:200]}
            if errors >= 5 and errors > done:
                print("FATAL: SiftMap erroring on every record - AUTH problem, not coverage. Stopping.")
                break
        except Exception as e:
            errors += 1
            state[key] = {"status": "error", "error": str(e)[:200]}
        done += 1
        if done % 10 == 0:
            print(f"{done}/{len(matches)} enriched", flush=True)
            STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")
        time.sleep(args.delay)
    STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")

    # Merge and write deliverable, ranked by realtor_score.
    rows = []
    for rec in matches:
        key = rec["contact"]["email"] or f"{rec['contact']['first_name']}|{rec['contact']['last_name']}"
        e = state.get(key, {})
        c = rec["contact"]
        m = rec["candidates"][0]
        rows.append({
            "first_name": c["first_name"], "last_name": c["last_name"], "email": c["email"],
            "sources": "; ".join(c["sources"]), "match_status": rec["status"],
            "county": m["county"], "address": m["situs_addr"], "city": m["situs_city"],
            "zip": m["situs_zip"], "enrich_status": e.get("status", "pending"),
            "realtor_score": e.get("realtor_score"),
            "investor_on_market_score": e.get("investor_on_market_score"),
            "investor_off_market_score": e.get("investor_off_market_score"),
            "market_value": e.get("total_market_value") or m["market_total"],
            "equity": e.get("total_equity"), "mortgage": e.get("total_mortgage"),
            "owned_properties": e.get("total_properties"),
            "portfolio_value": e.get("portfolio_value"),
            "last_sale_date": e.get("last_sale_date") or m["sale_date"],
            "sift_owner": e.get("sift_owner", ""), "county_owner": e.get("county_owner", m["owner1"]),
            "priority_95plus": 1 if (e.get("realtor_score") or 0) >= 95 else 0,
        })
    rows.sort(key=lambda r: -(r["realtor_score"] or -1))
    out = Path(f"{args.out}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    ok = sum(1 for r in rows if r["enrich_status"] == "ok")
    hot = sum(1 for r in rows if r["priority_95plus"])
    mm = sum(1 for r in rows if r["enrich_status"] == "owner_mismatch")
    print(f"\nenriched ok: {ok}, owner_mismatch: {mm}, realtor_score 95+: {hot}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
