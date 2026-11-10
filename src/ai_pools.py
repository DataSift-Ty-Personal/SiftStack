"""Ranked, buy-boxed, non-overlapping record pools from the properties index (read through the opensearch MCP).

Built 2026-11-05 for agency test lists (Ty): each pool is pulled into data+staging by id with
pull_by_ids.py, DataSift's unlimited skip trace fills the phones, no Trestle.

  python src/ai_pools.py --market franklin      # 20,000 realtor AI + 20,000 investor ladder, zero overlap
  python src/ai_pools.py --market knox_blount --rank ai \
      --exclude output/ai_pools/knox_blount/exclude_ty2_do_not_market.json   # 30,000 by investor AI (Ty's pick)

Rankings
  realtor   realtor_score high to low (ties: equity, then dataflik_id).
  investor  the county's Doors per Deal `ai` ladder (learn.datasift.ai/capture-data/<st>.json) in work
            order: the Priority 1 lists and the top investor AI band first, then down. An AI rung is a band
            of investor_off_market_score (identical to investor_score on 100 of 100 sampled); a list rung
            needs every one of its distressor_data flags active. preset_* rungs (negative/low equity, owner
            occupied) are skipped for the equity goal, as in the Sep 2026 Franklin run. Inside a rung the
            highest investor AI goes first; if the ladder runs dry, investor AI top-down fills the rest.
            Two-county ladders merge on marginal doors/deal, each county keeping its own rung order.
            --rank ai ranks the investor pool by investor_off_market_score alone, no list rungs (Ty chose
            this for Knox/Blount 2026-11-05, where the ladder ran 17,747 of 30,000 deep into P3).

No overlap: a record in both Franklin pools goes to the pool where it ranks higher and the other pool
refills from its next-best record, repeated until neither pool holds the other's records.

Buy box (Ty, 2026-11-05): single family; off market AND not listed per MLS status (is_active_listing, and
any MLS status other than Sold, both excluded); no deed or MLS sale since 2023-01-01; individuals only
(not corporate, not trust); owner name present, since skip trace needs one. Franklin $1-700K with the 69
Market Finder neighborhoods and ZIPs 43204 + 43211 out. Knox/Blount $100K-700K with the 18 rural ZIPs and
44 neighborhoods of output/knox_blount_top15k/areas.json out. Every area cut is scoped to its own county.

Output: output/ai_pools/<market>/<pool>.json (ordered rows carrying the rung or score that put each record
there) and summary.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opensearch_mcp import OpenSearchMCP  # noqa: E402

OUT = Path("output/ai_pools")
SINCE = "2023-01-01"
SHARD_URL = "https://learn.datasift.ai/capture-data/%s.json"
INVESTOR = "investor_off_market_score"
FIELDS = ["dataflik_id", "fips", "property_zip", "realtor_score", INVESTOR, "equity_percentage_est", "property_value"]
SORT_INVESTOR = [{INVESTOR: "desc"}, {"equity_percentage_est": {"order": "desc", "missing": "_last"}},
                 {"dataflik_id": "asc"}]
SORT_REALTOR = [{"realtor_score": "desc"}, {"equity_percentage_est": {"order": "desc", "missing": "_last"}},
                {"dataflik_id": "asc"}]


def _franklin_areas():
    rows = csv.DictReader(open("output/franklin_top30k/neighborhood_suppress.csv", encoding="utf-8"))
    return {"39049": ["43204", "43211"]}, {"39049": [r["neighborhood"] for r in rows]}


def _knox_blount_areas():
    a = json.load(open("output/knox_blount_top15k/areas.json", encoding="utf-8"))["counties"]
    return {f: v["zips_excluded"] for f, v in a.items()}, {f: v["nbhds_excluded"] for f, v in a.items()}


MARKETS = {
    "franklin": {"fips": ["39049"], "value": (1, 700000), "areas": _franklin_areas, "shard": "39",
                 "fallback": "output/franklin_top30k/capture-data-39.json",
                 "pools": [("realtor", 20000), ("investor", 20000)],
                 "prior": ("Franklin OH - Top 30K DPD (Sep 2026)", "output/franklin_top30k/selection.json")},
    "knox_blount": {"fips": ["47093", "47009"], "value": (100000, 700000), "areas": _knox_blount_areas,
                    "shard": "47", "fallback": "output/knox_blount_top15k/capture-data-47.json",
                    "pools": [("investor", 30000)],
                    "prior": ("Knox Blount - Top 15K DPD (Oct 2026, ty+2)", "output/knox_blount_top15k/selection.json")},
}


def _no_sale_since(field: str) -> dict:
    return {"bool": {"should": [{"range": {field: {"lt": SINCE, "format": "yyyy-MM-dd"}}},
                                {"bool": {"must_not": {"exists": {"field": field}}}}],
                     "minimum_should_match": 1}}


def buy_box(mk: dict, zips_out: dict, nbhds_out: dict, require_name: bool = True) -> dict:
    lo, hi = mk["value"]
    listed = {"bool": {"filter": [{"exists": {"field": "mls_data.status"}}],
                       "must_not": [{"term": {"mls_data.status": "Sold"}}]}}
    must_not = [{"term": {"is_active_listing": True}}, listed,
                {"term": {"is_corporate_owned": True}}, {"term": {"is_trust_owned": True}}]
    for fips, zips in zips_out.items():
        must_not.append({"bool": {"filter": [{"term": {"fips": fips}}, {"terms": {"property_zip": zips}}]}})
    for fips, names in nbhds_out.items():
        must_not.append({"bool": {"filter": [{"term": {"fips": fips}}, {"terms": {"neighborhood.keyword": names}}]}})
    flt = [{"terms": {"fips": mk["fips"]}}, {"term": {"property_type_group": "single_family_residence"}},
           {"range": {"property_value": {"gte": lo, "lte": hi}}},
           _no_sale_since("last_sale_date"), _no_sale_since("mls_data.last_sold_date")]
    if require_name:
        flt.append({"exists": {"field": "last_name"}})
    return {"bool": {"filter": flt, "must_not": must_not}}


def load_shard(st: str, fallback: str, run: Path) -> tuple:
    try:
        req = urllib.request.Request(SHARD_URL % st, headers={"User-Agent": "Mozilla/5.0 siftstack"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data, src = json.loads(r.read()), SHARD_URL % st
    except Exception as e:  # noqa: BLE001
        print("  shard fetch failed (%s); using %s" % (str(e)[:80], fallback))
        data, src = json.load(open(fallback, encoding="utf-8")), fallback
    (run / ("capture-data-%s.json" % st)).write_text(json.dumps(data), encoding="utf-8")
    return data, src


def rungs(county: dict, fips: str) -> list:
    rows = county["variants"]["ai"]
    bands = sorted({r["ai"] for r in rows if r.get("ai") is not None}, reverse=True)
    out = []
    for r in rows:
        base = {"fips": fips, "label": r["label"], "priority": r.get("priority"), "dpd": r.get("marg_dpd"),
                "list_size": r.get("list_size")}
        if r.get("ai") is not None:
            higher = [b for b in bands if b > r["ai"]]
            base["clause"] = {"range": {INVESTOR: {"gte": r["ai"], "lte": (min(higher) - 1) if higher else 100}}}
        elif any(k.startswith("preset_") for k in r["keys"]):
            base["skip"] = "preset rung (equity goal)"
        else:
            base["clause"] = {"bool": {"filter": [{"term": {"distressor_data.%s.is_active" % k: True}}
                                                  for k in r["keys"]]}}
        out.append(base)
    return out


def merge_ladders(ladders: list) -> list:
    """Merge on marginal doors/deal, each county keeping its own rung order."""
    idx, out = [0] * len(ladders), []
    while True:
        best = None
        for li, lad in enumerate(ladders):
            if idx[li] < len(lad):
                d = lad[idx[li]]["dpd"] if lad[idx[li]]["dpd"] is not None else float("inf")
                if best is None or d < best[0]:
                    best = (d, li)
        if best is None:
            return out
        out.append(ladders[best[1]][idx[best[1]]])
        idx[best[1]] += 1


def walk(c: OpenSearchMCP, box: dict, seq: list, need: int, skip: frozenset = frozenset()) -> tuple:
    taken, rows, per = set(skip), [], []
    for r in seq:
        if len(rows) >= need:
            break
        if "skip" in r:
            per.append({k: r[k] for k in ("fips", "label", "priority", "dpd")} | {"skipped": r["skip"]})
            continue
        q = {"bool": {"filter": [box, {"term": {"fips": r["fips"]}}, r["clause"]]}}
        in_box, added = c.count(q), 0
        if in_box:
            for x in c.page(q, SORT_INVESTOR, FIELDS, in_box):
                if x["dataflik_id"] in taken:
                    continue
                taken.add(x["dataflik_id"])
                rows.append(dict(x, rung=r["label"], priority=r["priority"]))
                added += 1
                if len(rows) >= need:
                    break
        per.append({k: r[k] for k in ("fips", "label", "priority", "dpd")} | {"in_box": in_box, "added": added})
        print("  %s P%s %-46s in box %6d  added %6d  total %6d" % (
            r["fips"], r["priority"], r["label"][:46], in_box, added, len(rows)), flush=True)
    if len(rows) < need:
        added = 0
        for x in c.page(box, SORT_INVESTOR, FIELDS, need * 3):
            if x["dataflik_id"] not in taken:
                taken.add(x["dataflik_id"])
                rows.append(dict(x, rung="Fill: investor AI", priority=None))
                added += 1
                if len(rows) >= need:
                    break
        per.append({"label": "Fill: investor AI", "added": added})
        print("  fill by investor AI: added %d  total %d" % (added, len(rows)), flush=True)
    return rows, per


def disjoint(a: list, b: list, n: int) -> tuple:
    """A record in both pools goes where it ranks higher (ties to a); the other pool refills."""
    pa = {x["dataflik_id"]: i for i, x in enumerate(a)}
    pb = {x["dataflik_id"]: i for i, x in enumerate(b)}
    owner = {}
    for _ in range(100):
        fa = [x for x in a if owner.get(x["dataflik_id"]) != "b"][:n]
        fb = [x for x in b if owner.get(x["dataflik_id"]) != "a"][:n]
        both = {x["dataflik_id"] for x in fa} & {x["dataflik_id"] for x in fb}
        if not both:
            return fa, fb, Counter(owner.values())
        for i in both:
            owner[i] = "a" if pa[i] <= pb[i] else "b"
    raise SystemExit("GATE FAILED: overlap resolution did not converge")


def prior_ids(path: str) -> set:
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(d, dict) and isinstance(d.get("claimed"), dict):
        return {str(k) for k in d["claimed"]}  # franklin_top30k / knox_blount_top15k key the ids under claimed
    if isinstance(d, dict) and not any(isinstance(v, list) for v in d.values()):
        return {str(k) for k in d}  # a dict keyed by id, e.g. id -> suppress reason
    rows = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
    return {str(r.get("dataflik_id") or r.get("id")) if isinstance(r, dict) else str(r) for r in rows}


def describe(rows: list, prior: set) -> dict:
    inv = [x[INVESTOR] for x in rows if x.get(INVESTOR) is not None]
    rea = [x["realtor_score"] for x in rows if x.get("realtor_score") is not None]
    return {"records": len(rows),
            "by_priority": dict(Counter("P%s" % x["priority"] if x.get("priority") else x["rung"] for x in rows)),
            "top_rungs": Counter(x["rung"] for x in rows).most_common(12),
            "investor_ai": [min(inv), max(inv)] if inv else None,
            "realtor_ai": [min(rea), max(rea)] if rea else None,
            "by_county": dict(Counter(x["fips"] for x in rows)),
            "already_on_prior_list": sum(1 for x in rows if str(x["dataflik_id"]) in prior)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--market", required=True, choices=sorted(MARKETS))
    ap.add_argument("--margin", type=float, default=1.5,
                    help="candidates per slot fetched before the overlap split (two-pool markets)")
    ap.add_argument("--rank", default="ladder", choices=["ladder", "ai"],
                    help="investor pool: the doors-per-deal ladder, or investor AI score top-down")
    ap.add_argument("--exclude", default="", help="ids never to pick (a JSON list, or a dict keyed by id), e.g. "
                    "owners an account already marked do-not-market; the pools refill past them")
    args = ap.parse_args()
    skip = frozenset(int(i) for i in prior_ids(args.exclude) if str(i).isdigit()) if args.exclude else frozenset()
    mk = MARKETS[args.market]
    run = OUT / args.market
    run.mkdir(parents=True, exist_ok=True)
    zips_out, nbhds_out = mk["areas"]()
    box = buy_box(mk, zips_out, nbhds_out)
    c = OpenSearchMCP()
    universe = c.count(box)
    no_name = c.count(buy_box(mk, zips_out, nbhds_out, require_name=False)) - universe
    print("%s buy box: %d records (%d more dropped for no owner name)" % (args.market, universe, no_name), flush=True)

    src, seq, check = None, [], []
    if args.rank == "ladder":
        shard, src = load_shard(mk["shard"], mk["fallback"], run)
        ladders = [rungs(shard[f], f) for f in mk["fips"]]
        seq = ladders[0] if len(ladders) == 1 else merge_ladders(ladders)
        check = [r for r in seq if "clause" in r][:4]
        for r in check:  # does the flag translation reproduce the shard's own list sizes?
            r["county_count"] = c.count({"bool": {"filter": [{"term": {"fips": r["fips"]}}, r["clause"]]}})
            print("  check %-40s shard %6s  index %6d" % (r["label"][:40], r["list_size"], r["county_count"]),
                  flush=True)

    pools = dict(mk["pools"])
    two = len(pools) == 2
    picked, per_rung = {}, {}
    if "realtor" in pools:
        need = int(pools["realtor"] * (args.margin if two else 1))
        picked["realtor"] = [dict(x, rung="Realtor AI %s" % x["realtor_score"], priority=None)
                             for x in c.page(box, SORT_REALTOR, FIELDS, need + len(skip))
                             if x["dataflik_id"] not in skip][:need]
        print("realtor candidates: %d" % len(picked["realtor"]), flush=True)
    need = int(pools["investor"] * (args.margin if two else 1))
    if args.rank == "ai":
        picked["investor"] = [dict(x, rung="Investor AI %s" % x[INVESTOR], priority=None)
                              for x in c.page(box, SORT_INVESTOR, FIELDS, need + len(skip))
                              if x["dataflik_id"] not in skip][:need]
    else:
        picked["investor"], per_rung["investor"] = walk(c, box, seq, need, skip)
    c.close()

    split = None
    if two:
        picked["realtor"], picked["investor"], moved = disjoint(picked["realtor"], picked["investor"],
                                                                pools["realtor"])
        split = {"overlaps_kept_by_realtor": moved.get("a", 0), "overlaps_kept_by_investor": moved.get("b", 0)}
        ids = [{x["dataflik_id"] for x in picked[p]} for p in ("realtor", "investor")]
        if ids[0] & ids[1]:
            raise SystemExit("GATE FAILED: the pools still overlap")
    prior_label, prior_path = mk["prior"]
    prior = prior_ids(prior_path)
    summary = {"market": args.market, "rank": args.rank, "when": datetime.now().isoformat(timespec="seconds"),
               "shard": src,
               "buy_box_records": universe, "dropped_no_owner_name": no_name, "excluded_ids": len(skip),
               "translation_check": [
                   {"rung": r["label"], "shard": r["list_size"], "index": r["county_count"]} for r in check],
               "overlap_split": split, "prior_list": prior_label, "pools": {}, "investor_rungs": per_rung.get("investor")}
    for p, n in mk["pools"]:
        rows = picked[p]
        if len(rows) < n:
            print("WARNING: %s pool holds %d of %d" % (p, len(rows), n))
        for rank, x in enumerate(rows, 1):
            x["rank"] = rank
        (run / ("%s.json" % p)).write_text(json.dumps(rows, indent=0), encoding="utf-8")
        summary["pools"][p] = describe(rows, prior)
    (run / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("buy_box_records", "dropped_no_owner_name", "overlap_split")},
                     default=str))
    for p, d in summary["pools"].items():
        print(p, json.dumps(d, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
