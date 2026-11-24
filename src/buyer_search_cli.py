"""The buyers most likely to buy one house, from the command line.

    python src/buyer_search_cli.py --address "123 Example St, Knoxville, TN 37920"
    python src/buyer_search_cli.py --address "123 Example St, Knoxville, TN 37920" --radius 3 --csv buyers.csv
    python src/buyer_search_cli.py --source cluster --dataflik-id 123456789            # staff: the data team's model
    python src/buyer_search_cli.py --source cluster --dataflik-id 123456789 --compat   # its original behavior

Default (--source siftmap): SiftStack's own version on your DataSift Open API key (REISIFT_API_KEY in .env).
It finds the house, reads the investor sales around it from SiftMap, reads up to --reads deeds (150; nearest
houses like this one first), and scores every buyer with the data team's model. It writes nothing but the
files you name and its work folder.

--source cluster runs the data team's own model through the read-only `opensearch` MCP: staff only, because
the property cluster reads every account's data. Buyers SiftStack never sells to (builders, iBuyers, funds,
lenders, government) are marked with the reason, never dropped.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from buyer_search import client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = ["target_rank", "score", "likely_price", "price_share", "priced_purchases", "median_paid",
           "not_a_target", "buyer_full_name", "buyer_address", "buyer_city", "buyer_state",
           "buyer_zip", "is_buyer_corporation", "purchases_radius", "total_investor_purchases", "purchases_per_year",
           "locality_percentage", "median_discount", "month_active_perc", "active_years", "similarity",
           "buyer_size", "profile_size", "last_purchase", "last_activity_date", "portfolio", "cash_share",
           "stats_county", "buyer_id"]


def _load_env() -> None:
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "house").lower()).strip("-")[:60] or "house"


def table(result: dict, show: int) -> str:
    subj = result.get("subject") or {}
    c = result.get("counts") or {}
    head = "%s  (%s; %s bed, %s bath, %s sqft, built %s, value %s)" % (
        subj.get("address") or subj.get("property_address") or result.get("dataflik_id"),
        "SiftMap on your key" if result.get("source") == "siftmap" else "the data team's model",
        subj.get("beds") or subj.get("bedroom_count"), subj.get("baths") or subj.get("bath_count"),
        subj.get("sqft") or subj.get("property_sqft"), subj.get("year_built"),
        subj.get("value") or subj.get("property_value"))
    if result.get("source") == "siftmap":
        meter = ("Investor sales seen %s, like this house %s | deeds read %s, left unread %s | buyers %s | to call %s"
                 % (c.get("investor_sales_seen"), c.get("similar"), c.get("deeds_read"), c.get("deeds_unread"),
                    c.get("buyers"), c.get("targets")))
    else:
        meter = ("Sales read %s of %s | buyers %s | with a profile %s | with stats %s | to call %s"
                 % (c.get("sales"), c.get("sales_total"), c.get("buyers"), c.get("profiles_found"),
                    c.get("stats_found"), c.get("targets")))
    lines = [head, meter]
    pr = result.get("pricing") or {}
    if pr.get("usual_price"):
        lines.append("Would pay, a guess: about $%s here (median of %s deals). Holders paid %s%% of value (times the "
                     "estimate, $%s); flippers %s%% of resale (times %s)" % (
                         format(pr["usual_price"], ","), pr.get("priced_purchases"), pr.get("holder_share", "-"),
                         format(int(pr.get("value") or 0), ","), pr.get("flip_share", "-"),
                         ("your ARV, $%s" % format(int(pr["arv"]), ",")) if pr.get("arv") else "the estimate"))
    lines.append("")
    for b in (result.get("buyers") or [])[:show or None]:
        tag = "  [%s]" % b["not_a_target"] if b.get("not_a_target") else ""
        sim = b.get("similarity")
        pays = "$%s" % format(b["likely_price"], ",") if b.get("likely_price") else "-"
        lines.append("%3s  %5s  %-40s %-14s %-2s  pays %-9s here %-3s total %-6s match %-5s last %s%s" % (
            b.get("target_rank") or "-", b.get("score"), (b.get("buyer_full_name") or "")[:40],
            (b.get("buyer_city") or "")[:14], b.get("buyer_state") or "", pays, b.get("purchases_radius"),
            b.get("total_investor_purchases"), "%.2f" % sim if isinstance(sim, (int, float)) else "-",
            b.get("last_purchase") or b.get("last_activity_date") or "-", tag))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--address", help="the house: street line with city, state and ZIP")
    ap.add_argument("--source", choices=["siftmap", "cluster"], default="siftmap",
                    help="siftmap: your Open API key (default); cluster: the data team's model, staff only")
    ap.add_argument("--radius", type=float, help="miles around the house (siftmap 5, at most 10; cluster 25)")
    ap.add_argument("--months", type=int, help="months of sales (siftmap 24)")
    ap.add_argument("--reads", type=int, help="deed reads at most (siftmap 150)")
    ap.add_argument("--arv", type=float, help="siftmap: price each buyer's guess against this ARV instead of the "
                                              "house's estimated value")
    ap.add_argument("--limit", type=int, help="buyers returned (100)")
    ap.add_argument("--min-score", type=float, help="lowest score returned (10)")
    ap.add_argument("--work", help="siftmap work folder (default output/buyer_search/<address>)")
    ap.add_argument("--dataflik-id", help="cluster: the property's dataflik_id")
    ap.add_argument("--zip", default="", help="cluster: 5-digit ZIP for --address")
    ap.add_argument("--all-sales", action="store_true",
                    help="cluster: also count company purchases that are not investor sales (brings in lenders)")
    ap.add_argument("--compat", action="store_true", help="cluster: the original Lambda's behavior, bugs included")
    ap.add_argument("--show", type=int, default=25, help="rows printed; 0 prints every buyer (all are in --csv)")
    ap.add_argument("--json", help="write the whole answer here")
    ap.add_argument("--csv", help="write every buyer here")
    a = ap.parse_args(argv)
    _load_env()

    params = {k: v for k, v in (("limit", a.limit), ("min_score", a.min_score)) if v is not None}
    if a.source == "siftmap":
        params.setdefault("limit", 250)          # every buyer the deeds found, not just the first 100
    try:
        if a.source == "siftmap":
            if not a.address:
                ap.error("give --address (street, city, state and ZIP)")
            if not os.environ.get("REISIFT_API_KEY"):
                print("Set REISIFT_API_KEY (your DataSift Open API key) in .env first.")
                return 3
            work = Path(a.work) if a.work else ROOT / "output" / "buyer_search" / _slug(a.address)
            result = client.search_open(address=a.address, work=work, radius_mi=a.radius, months=a.months,
                                        read_cap=a.reads, params=params, arv=a.arv)
        else:
            if not (a.dataflik_id or (a.address and a.zip)):
                ap.error("the cluster takes --dataflik-id, or --address with --zip")
            payload = dict(params, **({"radius_miles": a.radius} if a.radius else {}))
            if a.all_sales or a.compat:
                payload["is_investor_sale"] = ["true", "false"]      # the model's own default
            payload.update({"dataflik_id": int(a.dataflik_id)} if a.dataflik_id else {"address": a.address,
                                                                                        "zip": a.zip})
            result = client.search_cluster(payload, compat=a.compat)
    except client.SearchFailed as exc:
        print("Buyer search refused (%s): %s" % (exc.status, exc))
        for cnd in (exc.detail or {}).get("candidates") or []:
            print("  %s  %s, %s %s" % (cnd["dataflik_id"], cnd["property_address"], cnd["property_city"],
                                       cnd["property_zip"]))
        return 2
    if result.get("status", 200) != 200:
        print("Buyer search failed: %s" % result.get("error"))
        return 2
    print(table(result, a.show))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("\nWrote %s" % a.json)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for b in result.get("buyers") or []:
                w.writerow(b)
        print("Wrote %s" % a.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
