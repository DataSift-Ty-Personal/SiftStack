"""Knox + Blount TN (47093, 47009): the top 15,000 records by Doors per Deal, into ty+2.

Walks both counties' blended market share ladders from the County List
Playbook (learn.datasift.ai/capture-data/47.json, variant "ai": list rungs and
off-market AI score rungs in the page's own Work Order, each rung counting only
the records the rungs above did not already reach). The two ladders are merged
on marginal doors per deal, each county keeping its own rung order, so the
15,000 are the best rungs across both counties rather than a fixed split.

Filters on every rung (Ty, 2026-10-19):
  * single family, off market, estimated value $100,000 to $700,000
  * individuals only: extra_owner_type individual (no LLC, corp or trust)
  * none of the Market Finder neighborhoods with no investor sales, rural
    pockets, or a price point investors do not buy at (phase areas)
  * none of the rural ZIPs, judged by homes per square mile (phase areas)
  * Negative / Low Equity rungs and the owner-occupied catch-all are skipped
  * records ty+2 already holds in a dead status (sold, not interested, dead,
    buyer, under contract) or tagged / listed as recently sold, Do Not Market,
    Mail Only or a Dispo buyer are dropped and backfilled (phase suppress).
    72% of the first pass was already in ty+2 and 925 of those were tagged
    recently sold, so this is not a corner case.

ty+2 holds the off-market AI add-on for exactly these two counties, so sizing
and the pull both run on ty+2: the Open API key sizes, the minted user JWT
pulls by id. Nothing is pulled without --commit.

    python src/knox_blount_top15k.py --phase areas
    python src/knox_blount_top15k.py --phase count
    python src/knox_blount_top15k.py --phase sweep
    python src/knox_blount_top15k.py --phase suppress        # then sweep again
    python src/knox_blount_top15k.py --phase report
    python src/knox_blount_top15k.py --phase pull            # dry
    python src/knox_blount_top15k.py --phase pull --commit
    python src/knox_blount_top15k.py --phase qa
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from franklin_top30k import (AmbiguousSend, Search, _post, nbhd_filter,  # noqa: E402
                             rung_extra)
from siftmap_pull import BUY_BOX, WORLD, county_address  # noqa: E402

COUNTIES = {"47093": "Knox", "47009": "Blount"}
TARGET = 15000
VALUE_MIN, VALUE_MAX = 100000, 700000
PAGE = 250
SPLIT_AT = 9000        # keep every paged query under the 10,000-row offset wall
SFR = {"type_single_family": True}

OUT = os.path.join(os.environ.get("SIFTSTACK_OUTPUT_DIR", "output"),
                   "knox_blount_top15k")
CAPTURE = os.path.join(OUT, "capture-data-47.json")
COUNTY_DATA = os.path.join(OUT, "county-data-47.json")
ZCTA = os.path.join(OUT, "zcta_land_east_tn.csv")
AREAS_RAW = os.path.join(OUT, "areas_raw.json")
AREAS = os.path.join(OUT, "areas.json")
COUNTS = os.path.join(OUT, "counts.json")
SELECTION = os.path.join(OUT, "selection.json")
RUNG_CACHE = os.path.join(OUT, "rungs")


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _load(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def addr(fips: str) -> dict:
    return county_address(fips, rich=False)


def buy_box() -> dict:
    return dict(BUY_BOX, value_min=VALUE_MIN, value_max=VALUE_MAX)


def zip_of(address: str) -> str:
    tail = (address or "").strip().split(" ")[-1]
    return tail[:5] if tail[:5].isdigit() else ""


# ------------------------------------------------------------------- areas

# Rules, each one a reason printed beside every suppressed area.
#   no sales:   no investor transactions in six months (Market Finder)
#   rural:      a neighborhood whose median lot is an acre or more; a ZIP that
#               is sparse AND large-lot (density alone cut Maryville: 37801
#               and 37803 hold in-town half-acre lots plus mountain land, so
#               homes per square mile read rural while the houses are not)
#   bad:        the four price rules the Franklin builds used (Ty approved,
#               2026-08-19 and 2026-09-29): no investor demand at the price
#               point, or retail paying well over value with little investor
#               activity
NBHD_RURAL_LOT = 1.0      # median lot, acres (1.0 is a real cluster, not a default:
                          # 4-6% of rows, p75 of 2.3-3.5 acres in those areas)
RURAL_DENSITY = 150       # single family homes per square mile of ZCTA land
ZIP_RURAL_LOT = 0.9       # ... and a median lot this large
ZIP_RURAL_SHARE = 0.45    # ... or this share of lots on an acre or more
SPARSE_DENSITY = 50       # park or forest land inflates the area (Townsend,
SPARSE_LOT = 0.5          # Tallassee), so half-acre lots are enough there


def nbhd_reasons(n: dict) -> list:
    inv = n.get("total_inv_trans_6mo") or 0
    val = n.get("median_home_value") or 0
    sale = n.get("median_sale_price") or 0
    lot = n.get("lot_median")
    why = []
    if inv == 0:
        why.append("no investor sales in 6 months")
    elif inv == 1:
        why.append("one investor sale in 6 months")
    if lot is not None and lot >= NBHD_RURAL_LOT:
        why.append("rural: median lot %.2f acres" % lot)
    if 0 < inv <= 2 and val >= 400000:
        why.append("<=2 investor sales at $400K+ value")
    if val >= VALUE_MAX:
        why.append("median value above the $700K buy box")
    if val and sale >= 1.15 * val and inv <= 5:
        why.append("retail pays 15%+ over value, <=5 investor sales")
    return why


def zip_reasons(z: dict) -> list:
    why = []
    d, lot, share = z.get("density"), z.get("lot_median") or 0, z.get("lot_share_1ac") or 0
    if d is not None and d < RURAL_DENSITY and (
            lot >= ZIP_RURAL_LOT or share >= ZIP_RURAL_SHARE
            or (d < SPARSE_DENSITY and lot >= SPARSE_LOT)):
        why.append("rural: %d homes per sq mi, median lot %.2f acres, %d%% on 1+ acre"
                   % (d, lot, round(100 * share)))
    if not (z.get("total_inv_trans_6mo") or 0):
        why.append("no investor sales in 6 months")
    return why


def _lot_sample(s: Search, filters: dict, address: dict, total: int,
                pages: int) -> list:
    """lotAcres from up to `pages` result pages spread across the set (ids
    run roughly ascending, so page 1 alone is a geographic slice)."""
    reach = max(1, min(total, 9750))
    starts = sorted({1 + int(k * reach / pages) for k in range(pages)})
    lots = []
    for st in starts:
        if st > total:
            continue
        for row in s.page(filters, address, st).get("data") or []:
            if row.get("lotAcres") is not None:
                lots.append(float(row["lotAcres"]))
    return lots


def _lot_stats(lots: list) -> dict:
    if not lots:
        return {"lot_median": None, "lot_share_1ac": None, "lot_n": 0}
    return {"lot_median": round(statistics.median(lots), 3),
            "lot_share_1ac": round(sum(1 for x in lots if x >= 1) / len(lots), 3),
            "lot_n": len(lots)}


def _market_finder(county: str) -> tuple[str, dict]:
    files = sorted(glob.glob(os.path.join(
        os.path.dirname(OUT), "market_finder_Tennessee_%s_2026*.json" % county)))
    if not files:
        raise SystemExit("no Market Finder extraction for %s" % county)
    return os.path.basename(files[-1]), _load(files[-1])


def collect_areas(s: Search) -> dict:
    land = {}
    with open(ZCTA, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            land[r["zip"]] = float(r["land_sqmi"])
    raw = {"zips": {}, "nbhds": {}, "sources": {}}
    zip_total = {}
    for fips, county in COUNTIES.items():
        src, mf = _market_finder(county)
        raw["sources"][county] = src
        a = addr(fips)
        indiv = dict(buy_box(), extra_owner_type=["individual"])
        raw["county_sfr_" + fips] = s.count(SFR, a)
        print("== %s (%s): %d ZIPs, %d neighborhoods"
              % (county, src, len(mf["zip_data"]), len(mf["neighborhood_data"])))
        for z in mf["zip_data"]:
            zc = z["zip_code"]
            inc = {"zip_codes": json.dumps([zc]), "zip_codes_mode": "include"}
            sfr_in = s.count(dict(SFR, **inc), a)
            if zc not in zip_total:
                zip_total[zc] = s.count(SFR, {"search": zc, "type": "zip"})
            row = dict(z, fips=fips, county=county, sfr_in_county=sfr_in,
                       buybox_indiv=s.count(dict(indiv, **inc), a),
                       sfr_zip_all=zip_total[zc], land_sqmi=land.get(zc))
            row["density"] = (round(zip_total[zc] / land[zc], 1)
                              if land.get(zc) else None)
            row.update(_lot_stats(_lot_sample(s, dict(SFR, **inc), a, sfr_in, 4)))
            raw["zips"]["%s:%s" % (fips, zc)] = row
            print("  zip %s  sfr %6s  density %7s  lot med %s"
                  % (zc, f"{sfr_in:,}", row["density"], row["lot_median"]))
        for n in mf["neighborhood_data"]:
            nm = n["neighborhood"]
            inc = {"neighborhoods": json.dumps([nm]), "neighborhoods_mode": "include"}
            sfr_in = s.count(dict(SFR, **inc), a)
            row = dict(n, fips=fips, county=county, sfr_in_county=sfr_in,
                       buybox_indiv=s.count(dict(indiv, **inc), a))
            row.update(_lot_stats(_lot_sample(s, dict(SFR, **inc), a, sfr_in, 2)))
            raw["nbhds"]["%s:%s" % (fips, nm)] = row
            print("  nbhd %-34s sfr %6s  lot med %s"
                  % (nm[:34], f"{sfr_in:,}", row["lot_median"]))
    raw["calls"] = s.calls
    _save(AREAS_RAW, raw)
    return raw


def phase_areas(args) -> None:
    s = Search()
    raw = _load(AREAS_RAW)
    if raw is None or args.refresh:
        raw = collect_areas(s)
    out = {"rules": {"nbhd_rural_lot": NBHD_RURAL_LOT, "rural_density": RURAL_DENSITY,
                     "zip_rural_lot": ZIP_RURAL_LOT, "zip_rural_share": ZIP_RURAL_SHARE,
                     "sparse_density": SPARSE_DENSITY, "sparse_lot": SPARSE_LOT},
           "sources": raw["sources"], "counties": {}}
    for fips, county in COUNTIES.items():
        zips = [z for k, z in raw["zips"].items() if k.startswith(fips + ":")]
        nbs = [n for k, n in raw["nbhds"].items() if k.startswith(fips + ":")]
        for z in zips:
            z["reasons"] = zip_reasons(z)
        for n in nbs:
            n["reasons"] = nbhd_reasons(n)
        zx = sorted(z["zip_code"] for z in zips if z["reasons"])
        nx = sorted(n["neighborhood"] for n in nbs if n["reasons"])
        unmatched = [n["neighborhood"] for n in nbs if not n["sfr_in_county"]]
        a = addr(fips)
        indiv = dict(buy_box(), extra_owner_type=["individual"])
        before = s.count(indiv, a)
        f_z = dict(indiv, zip_codes=json.dumps(zx), zip_codes_mode="exclude") \
            if zx else dict(indiv)
        after_z = s.count(f_z, a)
        after = s.count(dict(f_z, **nbhd_filter(nx)), a)
        covered = sum(z["sfr_in_county"] for z in zips)
        out["counties"][fips] = {
            "county": county, "zips_excluded": zx, "nbhds_excluded": nx,
            "unmatched_nbhds": unmatched, "zips": zips, "nbhds": nbs,
            "universe_before": before, "after_zips": after_z, "after_all": after,
            "zip_coverage": round(covered / raw["county_sfr_" + fips], 4)}
        print("== %s" % county)
        print("  ZIP coverage of county SFR: %.1f%%" % (100 * covered / raw["county_sfr_" + fips]))
        print("  rural / dead ZIPs excluded (%d):" % len(zx))
        for z in sorted(zips, key=lambda z: z["zip_code"]):
            if z["reasons"]:
                print("    %s  %6s buy-box records  %s" % (
                    z["zip_code"], f"{z['buybox_indiv']:,}", "; ".join(z["reasons"])))
        print("  neighborhoods excluded (%d of %d):" % (len(nx), len(nbs)))
        for n in sorted(nbs, key=lambda n: n["neighborhood"]):
            if n["reasons"]:
                print("    %-34s %6s  %s" % (n["neighborhood"][:34],
                                            f"{n['buybox_indiv']:,}", "; ".join(n["reasons"])))
        if unmatched:
            print("  NAMES THAT DO NOT RESOLVE IN SIFTMAP: %s" % unmatched)
        print("  individuals in buy box: %s -> %s after ZIPs -> %s after neighborhoods"
              % (f"{before:,}", f"{after_z:,}", f"{after:,}"))
    _save(AREAS, out)


def area_filter(fips: str) -> dict:
    a = _load(AREAS)
    if not a:
        raise SystemExit("run --phase areas first")
    c = a["counties"][fips]
    f = {}
    if c["zips_excluded"]:
        f.update(zip_codes=json.dumps(c["zips_excluded"]), zip_codes_mode="exclude")
    f.update(nbhd_filter(c["nbhds_excluded"]))
    return f


# -------------------------------------------------------------------- walk

SKIP_KEYS = {"preset_negative_equity", "preset_low_equity"}


def base_filters(fips: str) -> dict:
    f = buy_box()
    f["extra_owner_type"] = ["individual"]
    f.update(area_filter(fips))
    return f


def ladder(fips: str) -> list:
    cap = _load(CAPTURE)[fips]["variants"]["ai"]
    ai_rungs = sorted({r["ai"] for r in cap if r.get("ai")}, reverse=True)
    rungs = []
    for i, r in enumerate(cap, 1):
        rung = {"fips": fips, "county": COUNTIES[fips], "n": i, "label": r["label"],
                "keys": r["keys"], "ai": r.get("ai"),
                "shard_size": r.get("list_size"), "shard_marg_dpd": r.get("marg_dpd"),
                "shard_cum_dpd": r.get("cum_dpd"), "skip": ""}
        if set(r["keys"]) & SKIP_KEYS:
            rung["skip"] = "negative/low equity rung (no wholesale room)"
        if "preset_owner_occupied" in r["keys"]:
            rung["skip"] = "owner occupied catch-all"
        extra = rung_extra(list(r["keys"]), r.get("ai"))
        if r.get("ai"):
            higher = [a for a in ai_rungs if a > r["ai"]]
            if higher:
                extra["investor_off_market_score_max"] = min(higher) - 1
        rung["filters"] = extra
        rungs.append(rung)
    return rungs


def merged_order() -> list:
    """Both ladders interleaved on marginal doors per deal, each county's own
    rung order preserved (a rung's marginal rate assumes the rungs above it)."""
    a = [r for r in ladder("47093") if not r["skip"]]
    b = [r for r in ladder("47009") if not r["skip"]]
    out = []
    while a or b:
        if not b or (a and a[0]["shard_marg_dpd"] <= b[0]["shard_marg_dpd"]):
            out.append(a.pop(0))
        else:
            out.append(b.pop(0))
    return out


def phase_count(args) -> None:
    s = Search()
    out = {"counties": {}, "sized_by": s.who}
    for fips, county in COUNTIES.items():
        a = addr(fips)
        base = base_filters(fips)
        cd = _load(COUNTY_DATA)[fips]["baseline"]
        rungs = ladder(fips)
        print("== %s: universe %s under every filter; county baseline %.1f doors/deal"
              % (county, f"{s.count(base, a):,}", cd["dpd"]))
        for r in rungs:
            if r["skip"]:
                print("  %-3d %-46s SKIP: %s" % (r["n"], r["label"][:46], r["skip"]))
                continue
            r["filtered"] = s.count(dict(base, **r["filters"]), a)
            print("  %-3d %-46s shard %7s filtered %7s mdpd %7s" % (
                r["n"], r["label"][:46], f"{r['shard_size'] or 0:,}",
                f"{r['filtered']:,}", r["shard_marg_dpd"]))
        out["counties"][fips] = {"county": county, "universe": s.count(base, a),
                                 "baseline_dpd": cd["dpd"], "rungs": rungs}
    out["calls"] = s.calls
    _save(COUNTS, out)


def sweep_rows(s: Search, filters: dict, address: dict, lo: int, hi: int) -> dict:
    """Every row for a query, id -> slim row. Pages by row offset but never
    past 10,000 rows (the server reorders beyond it): anything over SPLIT_AT
    is bisected on estimated value, and both halves must add back to the
    parent's total."""
    f = dict(filters, value_min=lo, value_max=hi)
    r = s.page(f, address)
    total = int(r.get("total_results") or 0)
    if total > SPLIT_AT and hi - lo >= 2:
        mid = (lo + hi) // 2
        a = sweep_rows(s, filters, address, lo, mid)
        b = sweep_rows(s, filters, address, mid + 1, hi)
        gaps = a["gaps"] + b["gaps"]
        if a["total"] + b["total"] != total:
            gaps.append((lo, hi, total, a["total"] + b["total"]))
        return {"total": total, "rows": {**a["rows"], **b["rows"]}, "gaps": gaps}
    out = {}
    idx = 1
    while True:
        page = r.get("data") or []
        for row in page:
            if row.get("id") is None:
                continue
            out[str(row["id"])] = {
                "zip": zip_of(row.get("address")), "fips": row.get("fips"),
                "value": row.get("estimatedValue"), "equity": row.get("equityPercent"),
                "lot": row.get("lotAcres"), "ai": row.get("investor_off_market_score"),
                "corp": row.get("corporateOwned"), "saved": bool(row.get("saved")),
                "saved_uuid": row.get("saved_uuid"),
                "saved_tags": row.get("saved_tags") or [],
                "saved_lists": row.get("saved_lists") or []}
        idx += PAGE
        if not page or idx > total:
            break
        r = s.page(f, address, idx)
    return {"total": total, "rows": out, "gaps": []}


def rung_rows(s: Search, r: dict) -> dict:
    path = os.path.join(RUNG_CACHE, "%s_%02d.json" % (r["fips"], r["n"]))
    got = _load(path)
    if got is None:
        got = sweep_rows(s, dict(base_filters(r["fips"]), **r["filters"]),
                         addr(r["fips"]), VALUE_MIN, VALUE_MAX)
        if abs(len(got["rows"]) - got["total"]) > max(3, 0.005 * got["total"]):
            raise SystemExit("%s rung %d: swept %s of reported %s (gaps %s)" % (
                r["county"], r["n"], len(got["rows"]), got["total"], got["gaps"]))
        _save(path, got)
    return got


# ---------------------------------------------------------------- suppress

# Records ty+2 already holds and must not be marketed again. ty+2's own
# presets exclude the same things (recently sold tag, sold / not interested
# status), and Dream 100 gated on them. Skip tracing or Trestle-scoring these
# buys numbers nobody will dial.
SUPPRESS = os.path.join(OUT, "suppress.json")
DEAD_STATUSES = ["sold", "not_interested", "Dead Lead", "buyer", "under_contract"]
SUPPRESS_TAGS = ["recently sold", "Do Not Market", "Mail Only", "Dispo Buyer"]
SUPPRESS_LISTS = ["Recently Sold", "Dispo - Flip Buyers", "Dispo - VIP Buyers",
                  "Dispo - Deal Blast"]
PAGE_CAP = 9900       # the records search refuses offset + limit past 10,000


def _crm(api, path: str, method: str = "GET", body=None, headers=None):
    """Api.call plus a backoff on the internal API's hard 429s and on dropped
    connections (a reset killed a 10K-record harvest at 2,200). Every caller
    is a read, so a retry cannot double-apply anything."""
    import urllib.error
    from datasift_api_upload import ApiError
    for attempt in range(8):
        try:
            return api.call(path, method, body, headers=headers)
        except ApiError as e:
            if e.code not in (429, 502, 503, 504):
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print("    network error on %s (%s), retry %d" % (path[:40], str(e)[:60], attempt + 1))
        time.sleep(min(60, 4 * (attempt + 1)))
    raise SystemExit("gave up on %s after 8 attempts" % path)


def _records(api, must: dict, offset: int = 0, limit: int = 100) -> dict:
    return _crm(api, "/api/internal/property/", "POST",
                {"limit": limit, "offset": offset, "query": {"must": must}},
                headers={"x-http-method-override": "GET"})


def suppress_reason(row: dict, sup: dict) -> str:
    if not sup or not row["saved"]:
        return ""
    u = row["saved_uuid"]
    if u in sup["status"]:
        return "status %s" % sup["status"][u]
    for t in row["saved_tags"]:
        if t in sup["tag_uuids"]:
            return "tag %s" % sup["tag_uuids"][t]
    for x in row["saved_lists"]:
        if x in sup["list_uuids"]:
            return "list %s" % sup["list_uuids"][x]
    return ""


def status_known(row: dict, sup: dict) -> bool:
    if not row["saved"]:
        return True
    u = row["saved_uuid"]
    return bool(sup) and (u in sup["status"] or u in sup["checked"]
                          or any(x in sup["paged_lists"] for x in row["saved_lists"]))


def phase_suppress(args) -> None:
    from datasift_api_upload import Api
    api = Api()
    sup = _load(SUPPRESS) or {"status": {}, "checked": [], "paged_lists": {},
                              "big_lists": {}}
    lists = {x["uuid"]: x["title"] for x in _crm(api, "/api/internal/list/?limit=999")
             .get("results", [])}
    tags = {x["uuid"]: x["title"] for x in _crm(
        api, "/api/internal/tag/?offset=0&limit=10000&ordering=title").get("results", [])}
    sup["tag_uuids"] = {u: t for u, t in tags.items() if t in SUPPRESS_TAGS}
    sup["list_uuids"] = {u: t for u, t in lists.items() if t in SUPPRESS_LISTS}
    # Every existing record any swept rung has seen, not just the current
    # selection, so a backfill rung is already covered.
    rows = {}
    for f in glob.glob(os.path.join(RUNG_CACHE, "*.json")):
        for i, r in _load(f)["rows"].items():
            if r["saved"]:
                rows[i] = r
    need = {x for r in rows.values() for x in r["saved_lists"]}
    print("%s existing ty+2 records across swept rungs, on %d lists"
          % (f"{len(rows):,}", len(need)))
    for lu in sorted(need, key=lambda x: lists.get(x, x)):
        if lu in sup["paged_lists"] or lu in sup["big_lists"]:
            continue
        must = {"any_lists": [lu], "any_property_status": DEAD_STATUSES}
        n = int(_records(api, must, limit=1).get("count") or 0)
        if n > PAGE_CAP:
            sup["big_lists"][lu] = n
            print("  %-40s %6s dead-status records: too many to page, skipped"
                  % (lists.get(lu, lu)[:40], f"{n:,}"))
            continue
        got = 0
        for off in range(0, n, 100):
            for x in _records(api, must, off).get("results") or []:
                sup["status"][x["uuid"]] = x.get("status")
                got += 1
            time.sleep(0.3)
        sup["paged_lists"][lu] = n
        print("  %-40s %6s dead-status records paged (%s read)"
              % (lists.get(lu, lu)[:40], f"{n:,}", f"{got:,}"))
        _save(SUPPRESS, sup)
    # Anything not on a fully paged list gets its own status read.
    checked = set(sup["checked"])
    todo = [r["saved_uuid"] for r in rows.values() if not status_known(r, sup)]
    print("%d records sit only on lists too big to page: reading each status" % len(todo))
    for k, u in enumerate(todo, 1):
        st = (_crm(api, "/api/internal/property/%s/" % u) or {}).get("status")
        st = st.get("title") if isinstance(st, dict) else st
        if st in DEAD_STATUSES:
            sup["status"][u] = st
        checked.add(u)
        if k % 100 == 0:
            sup["checked"] = sorted(checked)
            _save(SUPPRESS, sup)
            print("  read %d/%d" % (k, len(todo)))
        time.sleep(0.35)
    sup["checked"] = sorted(checked)
    _save(SUPPRESS, sup)
    hits = {}
    for r in rows.values():
        why = suppress_reason(r, sup)
        if why:
            hits[why] = hits.get(why, 0) + 1
    print("suppressed among existing swept records: %s" % f"{sum(hits.values()):,}")
    for k, v in sorted(hits.items(), key=lambda x: -x[1]):
        print("  %6s %s" % (f"{v:,}", k))


def phase_sweep(args) -> None:
    if not _load(COUNTS):
        raise SystemExit("run --phase count first")
    s = Search()
    sup = _load(SUPPRESS)
    claimed: dict[str, dict] = {}
    dropped: dict[str, str] = {}
    unknown = 0
    walk = []
    for r in merged_order():
        got = rung_rows(s, r)
        new, supp = [], 0
        for i, row in got["rows"].items():
            if i in claimed or i in dropped:
                continue
            why = suppress_reason(row, sup)
            if why:
                dropped[i] = why
                supp += 1
                continue
            new.append(i)
        room = TARGET - len(claimed)
        take = new[:room]
        for i in take:
            claimed[i] = dict(got["rows"][i], rung="%s:%d" % (r["fips"], r["n"]))
            unknown += not status_known(got["rows"][i], sup)
        walk.append({"fips": r["fips"], "county": r["county"], "n": r["n"],
                     "label": r["label"], "total": got["total"], "suppressed": supp,
                     "new": len(new), "taken": len(take), "cum": len(claimed),
                     "partial": len(new) > room,
                     "already_in_ty2": sum(1 for i in take if got["rows"][i]["saved"]),
                     "shard_marg_dpd": r["shard_marg_dpd"]})
        print("%-6s %-3d %-46s total %6s supp %5s new %6s taken %6s cum %6s  mdpd %6s%s" % (
            r["county"], r["n"], r["label"][:46], f"{got['total']:,}", f"{supp:,}",
            f"{len(new):,}", f"{len(take):,}", f"{len(claimed):,}", r["shard_marg_dpd"],
            "  (partial)" if len(new) > room else ""))
        if len(claimed) >= TARGET:
            break
    deals = sum(w["taken"] / w["shard_marg_dpd"] for w in walk if w["shard_marg_dpd"])
    _save(SELECTION, {"claimed": claimed, "walk": walk, "sized_by": s.who,
                      "suppressed": dropped, "status_unknown": unknown,
                      "est_deals_6mo": round(deals, 1),
                      "est_dpd": round(len(claimed) / deals, 1) if deals else None,
                      "calls": s.calls})
    saved = sum(1 for v in claimed.values() if v["saved"])
    print("\nselected %s unique: %s already in ty+2, %s new; %s suppressed on the way; "
          "est %.0f deals/6mo, ~%.0f doors/deal (%d search calls)" % (
              f"{len(claimed):,}", f"{saved:,}", f"{len(claimed) - saved:,}",
              f"{len(dropped):,}", deals, len(claimed) / deals if deals else 0, s.calls))
    if unknown:
        print("NOT FINAL: %d selected existing records have an unchecked status. Run "
              "--phase suppress, then --phase sweep again." % unknown)


# -------------------------------------------------------------- pull (ty+2)

LIST_TITLE = "Knox Blount - Top 15K DPD"
PULL_STATE = os.path.join(OUT, "pull_state.json")
TY2_ACCOUNT_EMAIL = "ty+2@dataflik.com"


def rung_tag(county: str, label: str) -> str:
    return "Top15K %s %s" % (county, label.replace("&", "and"))


def _ty2():
    from datasift_api_upload import Api, env
    from siftmap_pull import Map
    api = Api()
    email = (env().get("DATASIFT_EMAIL") or "").strip().lower()
    if email != TY2_ACCOUNT_EMAIL:
        raise SystemExit("REFUSING: DATASIFT_EMAIL is %r, not %s" % (email, TY2_ACCOUNT_EMAIL))
    return api, Map(api)


def saved_now(m, ids: list) -> dict:
    """id -> saved_uuid in ty+2 right now (the flag is per account)."""
    out = {}
    for i in range(0, len(ids), 100):
        r = _post(m, "/properties/search/", {
            "result_index": 1, "with_boundaries": False, "filters": {},
            "addresses": [{"search": str(x), "type": "ids"} for x in ids[i:i + 100]],
            "polygon": WORLD}, idempotent=True)
        for row in r.get("data") or []:
            if row.get("saved") and row.get("saved_uuid"):
                out[str(row["id"])] = row["saved_uuid"]
    return out


def add_ids(m, ids: list, tags: list):
    """Pull dataflik ids into ty+2 (d4d_call_list route A, verified live). The
    filters are EMPTY on purpose: the selection already happened at sizing."""
    return _post(m, "/properties/add-properties-by-query/", {
        "auto_add_enabled": False, "lists": [LIST_TITLE], "tags": tags,
        "replace_owners": False,
        "query": {"result_index": 1, "with_boundaries": False, "filters": {},
                  "addresses": [{"search": str(i), "type": "ids"} for i in ids],
                  "polygon": WORLD}}, idempotent=False)


def _by_rung(sel: dict) -> tuple[dict, dict]:
    labels = {"%s:%d" % (w["fips"], w["n"]): (w["county"], w["label"]) for w in sel["walk"]}
    by_rung: dict[str, list] = {}
    for i, v in sel["claimed"].items():
        by_rung.setdefault(v["rung"], []).append(i)
    return labels, by_rung


def phase_pull(args) -> None:
    from siftmap_pull import usage
    sel = _load(SELECTION)
    if not sel:
        raise SystemExit("run --phase sweep first")
    api, m = _ty2()
    labels, by_rung = _by_rung(sel)
    ids = list(sel["claimed"])
    saved = saved_now(m, ids)
    used, limit = usage(api)
    new = len(ids) - len(saved)
    print("target %s; selection %s ids: %s already in the account, %s new; "
          "allowance %s of %s used, %s left" % (
              TY2_ACCOUNT_EMAIL, f"{len(ids):,}", f"{len(saved):,}", f"{new:,}",
              f"{used:,}", f"{limit:,}", f"{limit - used:,}"))
    if new > limit - used:
        raise SystemExit("HARD STOP: %s new records > %s allowance left" % (new, limit - used))
    order = [k for k in dict.fromkeys(v["rung"] for v in sel["claimed"].values())]
    for k in order:
        print("  %-8s %6s ids -> tag %r" % (k, f"{len(by_rung[k]):,}", rung_tag(*labels[k])))
    print("  list %r" % LIST_TITLE)
    if not args.commit:
        print("DRY RUN: nothing pulled. Re-run with --commit.")
        return
    st = _load(PULL_STATE, {"done": [], "probe": None, "uncertain": [],
                            "allowance_before": used})
    if not st["probe"]:
        # The probe must be a NEW record: an existing one reads as saved at
        # once, before the async job attaches the list (seen live 2026-10-19:
        # the read-back came first, the list landed a minute later).
        first = next((k for k in order if any(i not in saved for i in by_rung[k])), order[0])
        probe_id = next((i for i in by_rung[first] if i not in saved), by_rung[first][0])
        try:
            add_ids(m, [probe_id], [rung_tag(*labels[first])])
        except AmbiguousSend as e:
            print("probe send lost its response (%s); the landing check decides" % e)
        landed, lists = {}, []
        for _ in range(30):
            landed = landed or saved_now(m, [probe_id])
            if landed:
                rec = api.call("/api/internal/property/%s/" % landed[probe_id])
                lists = [x.get("title") if isinstance(x, dict) else x
                         for x in rec.get("lists") or []]
                if LIST_TITLE in lists:
                    break
            time.sleep(20)
        if not landed:
            raise SystemExit("probe id %s did not land in 10 min; check "
                             "/api/internal/activity/" % probe_id)
        print("probe %s (new: %s) landed as %s; lists=%s" % (
            probe_id, probe_id not in saved, landed[probe_id], lists))
        if LIST_TITLE not in lists:
            raise SystemExit("probe landed but the list %r never attached in 10 min: "
                             "stopping" % LIST_TITLE)
        st["probe"] = {"id": probe_id, "uuid": landed[probe_id]}
        _save(PULL_STATE, st)
    done = set(st["done"])
    probe_id = st["probe"]["id"]
    for k in order:
        chunk_ids = [i for i in by_rung[k] if i != probe_id]
        for c in range(0, len(chunk_ids), 250):
            key = "%s:%d" % (k, c)
            if key in done:
                continue
            chunk = chunk_ids[c:c + 250]
            if key in st["uncertain"]:
                landed = saved_now(m, chunk)
                chunk = [i for i in chunk if i not in landed]
                print("  %s was uncertain: re-sending %d not yet in the account" % (key, len(chunk)))
            try:
                if chunk:
                    add_ids(m, chunk, [rung_tag(*labels[k])])
            except AmbiguousSend as e:
                print("  %s: response lost after send (%s), marked uncertain" % (key, e))
                if key not in st["uncertain"]:
                    st["uncertain"].append(key)
                _save(PULL_STATE, st)
                time.sleep(5)
                continue
            st["done"].append(key)
            if key in st["uncertain"]:
                st["uncertain"].remove(key)
            _save(PULL_STATE, st)
            time.sleep(1.0)
        print("  %s submitted (%s ids)" % (k, f"{len(chunk_ids):,}"))
    if st["uncertain"]:
        print("UNCERTAIN chunks: %s; re-run --phase pull --commit once the queue "
              "drains to re-send only what did not land" % st["uncertain"])
    u2, l2 = usage(api)
    print("all chunks submitted; allowance now %s used of %s" % (f"{u2:,}", f"{l2:,}"))


def phase_qa(args) -> None:
    sel = _load(SELECTION)
    api, m = _ty2()
    ids = list(sel["claimed"])
    saved = saved_now(m, ids)
    lists = api.call("/api/internal/list/?limit=999").get("results", [])
    uuid = next((x["uuid"] for x in lists if x["title"] == LIST_TITLE), None)
    n_list = 0
    if uuid:
        r = api.call("/api/internal/property/", method="POST",
                     headers={"x-http-method-override": "GET"},
                     body={"limit": 1, "offset": 0, "query": {"must": {"any_lists": [uuid]}}})
        n_list = int(r.get("count", 0))
    print("in ty+2: %s of %s selected; list %r holds %s"
          % (f"{len(saved):,}", f"{len(ids):,}", LIST_TITLE, f"{n_list:,}"))
    _save(os.path.join(OUT, "qa.json"), {"landed": len(saved), "selected": len(ids),
                                         "list_count": n_list})


# ------------------------------------------------------------------ export

# The calling-agency file (Ty, 2026-10-19): no Trestle on purpose, to measure
# the list without it, and the agency runs its own litigator scrub. So no tier
# orders or drops a number here; what comes off is only what ty+2 itself
# already knows is dead or must not be called.
EXPORT_CACHE = os.path.join(OUT, "export_records.json")
BAD_PHONE_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
LITIGATOR_TAG = "DNC"          # d4d_call_list convention: a prior litigator hit
LIVE_LEAD_STATUSES = {"new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead",
                      "Hot Lead", "Ghosting Lead", "follow_up"}
MAX_PHONES = 10


def _slim(d: dict) -> dict:
    ow = d.get("owner") or {}
    a = d.get("address") or {}
    m = ow.get("address") or {}
    sec = d.get("secondary_owners") or []
    st = d.get("status")
    return {"uuid": d.get("uuid"), "dataflik_id": str(d.get("dataflik_id") or ""),
            "status": st.get("title") if isinstance(st, dict) else st,
            "street": a.get("street"), "city": a.get("city"), "state": a.get("state"),
            "zip": (a.get("postal_code") or "")[:5], "county": a.get("county"),
            "vacant": a.get("vacant"),
            "owner_uuid": ow.get("uuid"), "first": ow.get("first_name"),
            "last": ow.get("last_name"), "company": ow.get("company"),
            "deceased": ow.get("deceased"), "dnc": ow.get("dnc"),
            "opt_out": ow.get("opt_out"), "skiptraced": ow.get("skiptraced"),
            "m_street": m.get("street"), "m_city": m.get("city"),
            "m_state": m.get("state"), "m_zip": (m.get("postal_code") or "")[:5],
            "phones": ow.get("phones") or [], "emails": ow.get("emails") or [],
            "owner2": " ".join(x for x in ((sec[0].get("first_name") or ""),
                                           (sec[0].get("last_name") or "")) if x).strip()
            if sec and isinstance(sec[0], dict) else "",
            "value": d.get("estimate_value"), "equity": d.get("equity_percent"),
            "year": d.get("year"), "beds": d.get("bedrooms"), "baths": d.get("bathrooms"),
            "sqft": d.get("sqft")}


def _uuids(sel: dict, need_new: bool) -> dict:
    """dataflik id -> ty+2 record uuid. Existing records come from the sweep;
    pulled ones are looked up (they only exist once the pull has landed)."""
    out = {i: v["saved_uuid"] for i, v in sel["claimed"].items()
           if v["saved"] and v["saved_uuid"]}
    missing = [i for i in sel["claimed"] if i not in out]
    if missing and need_new:
        _, m = _ty2()
        out.update(saved_now(m, missing))
    return out


def phase_harvest(args) -> None:
    """Full record per selected property (read only, resumable, ~2 req/s)."""
    from datasift_api_upload import Api
    sel = _load(SELECTION)
    api = Api()
    ids = _uuids(sel, need_new=not args.existing_only)
    cache = _load(EXPORT_CACHE, {})
    todo = [u for u in ids.values() if u not in cache]
    print("harvest: %s of %s selected records resolve to a ty+2 uuid; %s cached, "
          "%s to read" % (f"{len(ids):,}", f"{len(sel['claimed']):,}",
                          f"{len(cache):,}", f"{len(todo):,}"))
    from datasift_api_upload import ApiError
    t0 = time.time()
    for k, u in enumerate(todo, 1):
        try:
            cache[u] = _slim(_crm(api, "/api/internal/property/%s/" % u) or {})
        except ApiError as e:
            if e.code != 404:
                raise
            # Deleted or merged in ty+2 since the sweep: not callable, counted.
            cache[u] = {"gone": True}
        if k % 200 == 0 or k == len(todo):
            _save(EXPORT_CACHE, cache)
            rate = k / (time.time() - t0)
            print("  read %s/%s (%.1f/s, ~%d min left)" % (
                f"{k:,}", f"{len(todo):,}", rate, (len(todo) - k) / rate / 60))
        time.sleep(0.25)
    _save(EXPORT_CACHE, cache)


def clean_phones(r: dict) -> tuple[list, dict]:
    """Callable numbers, mobiles first, DataSift's order otherwise."""
    why = {}
    keep, seen = [], set()
    for p in r["phones"]:
        tags = [t.get("title") if isinstance(t, dict) else t for t in p.get("tags") or []]
        digits = "".join(c for c in str(p.get("number") or "") if c.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        reason = ""
        if len(digits) != 10 or digits[0] in "01":
            reason = "invalid number"
        elif (p.get("status") or "").upper() in BAD_PHONE_STATUSES:
            reason = "dispositioned %s" % p["status"].upper()
        elif LITIGATOR_TAG in tags:
            reason = "prior litigator flag"
        elif p.get("is_connected") is False:
            reason = "disconnected"
        elif digits in seen:
            reason = "duplicate"
        if reason:
            why[reason] = why.get(reason, 0) + 1
            continue
        seen.add(digits)
        keep.append((digits, (p.get("type") or "").title()))
    keep.sort(key=lambda x: x[1] != "Mobile")
    return keep, why


def _name(s) -> str:
    s = (s or "").strip()
    return s.title() if s.isupper() or s.islower() else s


_ENTITY = re.compile(
    r"\b(llc|llp|lp|inc|incorporated|corp|co|company|trust|tr|trustee|revocable|"
    r"irrevocable|living|est|estate|church|partners|partnership|properties|holdings|"
    r"investments?|enterprises?|enteprise|homes|rentals?|construction|development|"
    r"realty|mortgage|fund|series|acquis\w*|acquistion|restoration|habitat|solutions|"
    r"supply|management|manafement|group)\b", re.I)
_PLACEISH = re.compile(r"(-|wood|lawn|towne?|ville|view|side|land|field|ridge|grove|"
                       r"hill|park|creek|valley)$", re.I)


def _church_as_person(first, last) -> bool:
    return ((last or "").strip().lower() in ("church", "chapel", "ministries")
            and bool(_PLACEISH.search((first or "").strip()) or "-" in (first or "")))


_SUFFIX = {"street": "st", "avenue": "ave", "drive": "dr", "road": "rd", "lane": "ln",
           "court": "ct", "circle": "cir", "boulevard": "blvd", "place": "pl",
           "highway": "hwy", "parkway": "pkwy", "trail": "trl", "terrace": "ter",
           "north": "n", "south": "s", "east": "e", "west": "w"}
_UNIT = {"apt", "unit", "ste", "suite", "lot", "bldg", "#"}


def _addr_key(s) -> str:
    """'3703 Valley View Dr # B' and '3703 Valley View Drive' -> one key."""
    out = []
    for t in "".join(c if c.isalnum() or c == "#" else " " for c in (s or "").lower()).split():
        if t in _UNIT or t.startswith("#"):
            break
        out.append(_SUFFIX.get(t, t))
    return " ".join(out)


def _num(v, places: int = 0):
    """352000.00 -> 352000, 1.0 -> 1, 80.57 stays 80.57; None -> ''."""
    if v in (None, ""):
        return ""
    try:
        f = round(float(v), places)
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else f


def phase_export(args) -> None:
    sel = _load(SELECTION)
    cache = _load(EXPORT_CACHE, {})
    by_id ={r["dataflik_id"]: r for r in cache.values() if r.get("uuid")}
    labels = {"%s:%d" % (w["fips"], w["n"]): "%s %s" % (w["county"], w["label"])
              for w in sel["walk"]}
    rows, no_phone, held, held_rows = {}, [], {}, []
    phone_drops: dict[str, int] = {}
    missing = 0
    for i, v in sel["claimed"].items():       # walk order: best rung first
        r = by_id.get(i)
        if r is None:
            missing += 1
            continue
        why = ""
        if r.get("dnc") or r.get("opt_out"):
            why = "owner do not call / opted out"
        elif not (r.get("first") and r.get("last")):
            # SiftMap calls the owner an individual, but ty+2's owner of record
            # (and the phones on it) is an LLC, trust or church: individuals only.
            # A handful are people whose county-style LAST FIRST name landed in
            # the company field; the name order is ambiguous ("William David
            # Faulkner Sr" is first-first), so they are held, not guessed at.
            co = r.get("company") or ""
            if not co:
                why = "no owner name in ty+2"
            elif _ENTITY.search(co) or co[:1].isdigit() or co.lower().startswith("the "):
                why = "entity owner in ty+2: %s" % co
            else:
                why = "person name stored as a company in ty+2: %s" % co
        elif _church_as_person(r.get("first"), r.get("last")):
            # "West-Towne" / "Church" and "Woodlawn" / "Church" in the name
            # fields; David, Renee and Joseph Church are people and stay.
            why = "entity owner in ty+2: %s %s" % (r["first"], r["last"])
        elif r.get("status") in LIVE_LEAD_STATUSES:
            why = "live lead in ty+2 (%s)" % r["status"]
        elif r.get("equity") not in (None, "") and float(r["equity"]) < 0:
            # Ty's rule on the FTM pull: debt over value is not workable.
            why = "upside down (equity under 0%)"
        if why:
            cat = why.split(":")[0].split(" (")[0]
            held[cat] = held.get(cat, 0) + 1
            held_rows.append({"r": r, "phones": clean_phones(r)[0],
                              "segment": labels.get(v["rung"], ""), "others": [],
                              "why": why})
            continue
        key = r.get("owner_uuid") or r["uuid"]
        if key in rows:
            rows[key]["others"].append("%s, %s" % (r["street"], r["city"]))
            continue
        phones, why = clean_phones(r)
        for k, n in why.items():
            phone_drops[k] = phone_drops.get(k, 0) + n
        row = {"r": r, "phones": phones, "segment": labels.get(v["rung"], ""),
               "others": []}
        if phones:
            rows[key] = row
        else:
            no_phone.append(row)
    cols = (["Record ID", "First Name", "Last Name", "Owner 2", "Property Address",
             "Property City", "Property State", "Property ZIP", "County",
             "Mailing Address", "Mailing City", "Mailing State", "Mailing ZIP"]
            + [c for n in range(1, MAX_PHONES + 1) for c in ("Phone %d" % n,
                                                             "Phone %d Type" % n)]
            + ["Email 1", "Email 2", "Email 3", "Segment", "Estimated Value",
               "Equity %", "Year Built", "Beds", "Baths", "SqFt", "Vacant",
               "Absentee Owner", "Owner Deceased", "Other Properties on This List"])

    def line(x: dict) -> list:
        r = x["r"]
        ph = x["phones"][:MAX_PHONES] + [("", "")] * (MAX_PHONES - len(x["phones"][:MAX_PHONES]))
        absentee = bool(r.get("m_street")) and (
            _addr_key(r.get("m_street")) != _addr_key(r.get("street"))
            or (r.get("m_zip") or r.get("zip")) != r.get("zip"))
        emails = [e if isinstance(e, str) else (e.get("email") or "") for e in r["emails"]][:3]
        emails += [""] * (3 - len(emails))
        return ([r["uuid"], _name(r["first"]), _name(r["last"]), _name(r.get("owner2")),
                 r["street"], r["city"], r["state"], r["zip"], r["county"],
                 r.get("m_street"), r.get("m_city"), r.get("m_state"), r.get("m_zip")]
                + [c for p in ph for c in p] + emails
                + [x["segment"], _num(r.get("value")), _num(r.get("equity"), 1),
                   _num(r.get("year")), _num(r.get("beds")), _num(r.get("baths"), 1),
                   _num(r.get("sqft")),
                   "Yes" if r.get("vacant") else "", "Yes" if absentee else "",
                   "Yes" if r.get("deceased") else "", "; ".join(x["others"])])

    stamp = time.strftime("%Y-%m-%d")
    base = os.path.join(OUT, "Knox_Blount_Top15K_Call_List_%s" % stamp)
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for x in rows.values():
            w.writerow(line(x))
    with open(base + "_no_phones.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for x in no_phone:
            w.writerow(line(x))
    n_props = sum(1 + len(x["others"]) for x in rows.values())
    n_phones = sum(min(len(x["phones"]), MAX_PHONES) for x in rows.values())
    mobiles = sum(1 for x in rows.values() for p in x["phones"][:MAX_PHONES]
                  if p[1] == "Mobile")
    over = sum(1 for x in rows.values() if len(x["phones"]) > MAX_PHONES)
    summary = {"selected": len(sel["claimed"]),
               "gone_from_ty2": sum(1 for r in cache.values() if r.get("gone")),
               "not_harvested": missing,
               "rows": len(rows), "properties_covered": n_props,
               "no_phone_rows": len(no_phone), "held": held,
               "phones": n_phones, "mobiles": mobiles, "phone_drops": phone_drops,
               "rows_over_phone_cap": over,
               "deceased_rows": sum(1 for x in rows.values() if x["r"].get("deceased")),
               "file": base + ".csv"}
    _save(os.path.join(OUT, "export_summary.json"), summary)
    _export_xlsx(base + ".xlsx", cols, [line(x) for x in rows.values()],
                 [line(x) for x in no_phone],
                 [[x["why"]] + line(x) for x in held_rows], summary)
    print(json.dumps(summary, indent=1))


def _export_xlsx(path: str, cols: list, rows: list, no_phone: list, held: list,
                 s: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    head = Font(bold=True, color=NAVY)
    fill = PatternFill("solid", start_color=SOFT, end_color=SOFT)
    rule = Border(bottom=Side(style="thin", color=LINE))
    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    lines = [
        ("Knox and Blount top 15K: call list for the calling agency", True),
        ("Built %s from the Doors per Deal selection (single family, off market, "
         "$100,000 to $700,000, individual owners, rural and no-sale areas removed)."
         % time.strftime("%Y-%m-%d"), False),
        ("", False),
        ("Call list rows (one per owner)", s["rows"]),
        ("Properties those rows cover", s["properties_covered"]),
        ("Phone numbers on the list", s["phones"]),
        ("  of which mobile", s["mobiles"]),
        ("Rows with no callable number (separate sheet)", s["no_phone_rows"]),
        ("Rows held out (separate sheet, reason on each)", sum(s["held"].values())),
        ("Owner possibly deceased (flagged, still on the list)", s["deceased_rows"]),
        ("", False),
        ("Not scrubbed for litigators or the DNC registry: the agency runs that scrub.", False),
        ("Not Trestle scored on purpose. Phones are mobiles first, then DataSift's order.", False),
        ("Removed per number: wrong, dead and DNC dispositions already in ty+2, numbers "
         "DataSift marks disconnected, earlier litigator flags, invalid numbers, repeats.", False),
        ("One row per owner: a portfolio owner is called once, about their best-ranked "
         "property, with the rest in Other Properties on This List.", False),
        ("Record ID is the ty+2 record uuid: send it back with each disposition.", False),
    ]
    for text, v in lines:
        if isinstance(v, bool):
            ws.append([text])
            if v:
                ws.cell(ws.max_row, 1).font = Font(bold=True, size=14, color=NAVY)
        else:
            ws.append([text, v])
            ws.cell(ws.max_row, 2).number_format = "#,##0"
    ws.column_dimensions["A"].width = 95
    ws.column_dimensions["B"].width = 12
    for row in ws.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    def sheet(title, header, data):
        w = wb.create_sheet(title)
        w.append(header)
        for c in w[1]:
            c.font, c.fill, c.border = head, fill, rule
        for d in data:
            w.append(d)
        w.freeze_panes = "B2"
        for i, h in enumerate(header, 1):
            w.column_dimensions[w.cell(1, i).column_letter].width = max(10, min(32, len(h) + 4))
    sheet("Call list", cols, rows)
    sheet("No phones", cols, no_phone)
    sheet("Held out", ["Why held"] + cols, held)
    tmp = path.replace(".xlsx", "_PENDING_.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)


# ------------------------------------------------------------------ report

REPORT = os.path.join(OUT, "Knox_Blount_Top15K_Plan.xlsx")
NAVY, LINE, SOFT = "0A1130", "D9DEE8", "F4F6FA"


def phase_report(args) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    areas, counts, sel = _load(AREAS), _load(COUNTS), _load(SELECTION)
    if not (areas and counts and sel):
        raise SystemExit("run areas, count and sweep first")
    wb = Workbook()
    head = Font(bold=True, color=NAVY)
    fill = PatternFill("solid", start_color=SOFT, end_color=SOFT)
    rule = Border(bottom=Side(style="thin", color=LINE))

    def table(ws, cols, rows, widths, fmts=None):
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.font, c.fill, c.border = head, fill, rule
        start = ws.max_row
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        for col, fmt in (fmts or {}).items():
            for row in ws.iter_rows(min_row=start + 1, min_col=col, max_col=col):
                row[0].number_format = fmt
        ws.freeze_panes = ws.cell(start + 1, 1)

    claimed = sel["claimed"]
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Knox and Blount County TN: top 15,000 by doors per deal"])
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws.append(["Single family, off market, $100,000 to $700,000, individual owners only, "
               "rural and no-sale areas removed. Built %s." % time.strftime("%Y-%m-%d")])
    ws.append([])
    rows = []
    for fips, c in areas["counties"].items():
        n = sum(1 for v in claimed.values() if v["rung"].startswith(fips + ":"))
        s_ = sum(1 for v in claimed.values() if v["rung"].startswith(fips + ":") and v["saved"])
        rows.append([c["county"], c["universe_before"], c["universe_before"] - c["after_all"],
                     c["after_all"], len(c["zips_excluded"]), len(c["nbhds_excluded"]),
                     n, s_, n - s_, counts["counties"][fips]["baseline_dpd"]])
    tot = [sum(r[i] for r in rows) for i in range(1, 9)]
    rows.append(["Both"] + tot + [None])
    table(ws, ["County", "Individuals in buy box", "Removed by area rules",
               "Universe after rules", "ZIPs removed", "Neighborhoods removed",
               "Selected", "Already in ty+2", "New to ty+2", "County doors/deal"],
          rows, [10, 20, 20, 19, 12, 20, 11, 15, 12, 17],
          {k: "#,##0" for k in range(2, 10)} | {10: "0.0"})
    ws.append([])
    ws.append(["Estimated doors per deal for the selection", sel["est_dpd"]])
    ws.append(["Estimated deals per 6 months (ladder rates)", sel["est_deals_6mo"]])
    ws.append([])
    why = {}
    for v in (sel.get("suppressed") or {}).values():
        why[v] = why.get(v, 0) + 1
    ws.append(["Dropped because ty+2 already marks them do not market", len(sel.get("suppressed") or {})])
    ws.cell(ws.max_row, 1).font = head
    for k, v in sorted(why.items(), key=lambda x: -x[1]):
        ws.append(["  " + k, v])
    for row in ws.iter_rows(min_row=1, min_col=2, max_col=2):
        if isinstance(row[0].value, (int, float)) and row[0].row > len(rows) + 4:
            row[0].number_format = "#,##0"

    ws = wb.create_sheet("Ladder walk")
    table(ws, ["County", "Rung", "Segment", "Rung doors/deal", "Records under filters",
               "Dropped (do not market)", "New (not in a rung above)", "Taken",
               "Running total", "Already in ty+2"],
          [[w["county"], w["n"], w["label"], w["shard_marg_dpd"], w["total"],
            w.get("suppressed", 0), w["new"], w["taken"], w["cum"], w["already_in_ty2"]]
           for w in sel["walk"]],
          [9, 6, 44, 15, 19, 21, 22, 9, 13, 15],
          {4: "0.0", 5: "#,##0", 6: "#,##0", 7: "#,##0", 8: "#,##0", 9: "#,##0",
           10: "#,##0"})

    ws = wb.create_sheet("ZIPs")
    zrows = []
    for c in areas["counties"].values():
        for z in sorted(c["zips"], key=lambda z: (bool(not z["reasons"]), z["zip_code"])):
            zrows.append([c["county"], z["zip_code"], "Remove" if z["reasons"] else "Keep",
                          "; ".join(z["reasons"]), z.get("total_inv_trans_6mo"),
                          z.get("homes_sold_last_month"), z.get("median_days_on_market"),
                          z.get("median_home_value"), z["sfr_in_county"], z["buybox_indiv"],
                          z.get("land_sqmi"), z.get("density"), z.get("lot_median"),
                          z.get("lot_share_1ac")])
    table(ws, ["County", "ZIP", "Decision", "Why", "Investor sales 6 mo", "Sold last month",
               "Median DOM", "Median value", "SFR homes in county", "Buy-box individuals",
               "Land sq mi", "Homes per sq mi", "Median lot acres", "Lots 1 acre+"],
          zrows, [9, 8, 10, 46, 12, 10, 10, 13, 13, 13, 10, 12, 12, 11],
          {8: "$#,##0", 9: "#,##0", 10: "#,##0", 11: "0.0", 12: "#,##0", 13: "0.00",
           14: "0%"})

    ws = wb.create_sheet("Neighborhoods")
    nrows = []
    for c in areas["counties"].values():
        for n in sorted(c["nbhds"], key=lambda n: (bool(not n["reasons"]), n["neighborhood"])):
            nrows.append([c["county"], n["neighborhood"], "Remove" if n["reasons"] else "Keep",
                          "; ".join(n["reasons"]), n.get("total_inv_trans_6mo"),
                          n.get("homes_on_market"), n.get("homes_sold_last_month"),
                          n.get("median_days_on_market"), n.get("median_home_value"),
                          n.get("median_sale_price"), n["sfr_in_county"], n["buybox_indiv"],
                          n.get("lot_median"), n.get("lot_share_1ac")])
    table(ws, ["County", "Neighborhood", "Decision", "Why", "Investor sales 6 mo",
               "On market", "Sold last month", "Median DOM", "Median value",
               "Median sale", "SFR homes", "Buy-box individuals", "Median lot acres",
               "Lots 1 acre+"],
          nrows, [9, 32, 10, 52, 12, 10, 10, 10, 13, 13, 10, 13, 12, 11],
          {9: "$#,##0", 10: "$#,##0", 11: "#,##0", 12: "#,##0", 13: "0.00", 14: "0%"})

    ws = wb.create_sheet("Read me")
    for line in [
        "How this list was built",
        "1. Market Finder (ty+2, %s) supplied investor sales, inventory, DOM and prices "
        "for every ZIP and neighborhood in both counties." % ", ".join(areas["sources"].values()),
        "2. A ZIP is removed as rural when it has under %d single family homes per square "
        "mile (SiftMap home count over Census 2023 ZCTA land area) AND large lots: a median "
        "lot of %.1f acre or more, or %d%%+ of lots on an acre or more. Under %d homes per "
        "square mile, a %.1f acre median is enough, because park and forest land inflates "
        "the area. A ZIP with no investor sales in 6 months is also removed. Density alone "
        "would have cut in-town Maryville." % (
            RURAL_DENSITY, ZIP_RURAL_LOT, round(100 * ZIP_RURAL_SHARE), SPARSE_DENSITY,
            SPARSE_LOT),
        "3. A neighborhood is removed for zero or one investor sale in 6 months, a median "
        "lot of %.1f acre or more (rural pocket), or a price point investors do not buy "
        "at (the Franklin build's four rules)." % NBHD_RURAL_LOT,
        "4. Both counties' Doors per Deal ladders (learn.datasift.ai/capture-data/47.json, "
        "AI variant) were walked together, best marginal doors per deal first, each rung "
        "counting only records no rung above already took, until 15,000.",
        "5. Negative and low equity rungs and the owner-occupied catch-all were skipped.",
        "6. Records ty+2 already holds were dropped and backfilled when their status is "
        "sold, not interested, dead lead, buyer or under contract, or when tagged recently "
        "sold, Do Not Market, Mail Only or Dispo Buyer, or on a Dispo buyer list. Records "
        "already being worked (new, cold, warm, hot, prospecting) stay in.",
        "Lot size is a sample: up to 1,000 rows per ZIP and 500 per neighborhood, spread "
        "across the result set. SiftMap has no lot-size filter to count it exactly.",
        "Doors per deal per rung comes from the county ladder and assumes our filters do "
        "not change a segment's deal rate.",
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=12, color=NAVY)
    ws.column_dimensions["A"].width = 120
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    tmp = REPORT.replace(".xlsx", "_PENDING_.xlsx")
    wb.save(tmp)
    os.replace(tmp, REPORT)
    print("wrote %s" % REPORT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", required=True,
                    choices=["areas", "count", "sweep", "suppress", "pull", "qa",
                             "report", "harvest", "export"])
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--existing-only", action="store_true",
                    help="harvest/export: only records already in ty+2 (no pull needed)")
    ap.add_argument("--refresh", action="store_true",
                    help="areas: re-collect from SiftMap instead of the cache")
    args = ap.parse_args()
    {"areas": phase_areas, "count": phase_count, "sweep": phase_sweep,
     "suppress": phase_suppress, "pull": phase_pull, "qa": phase_qa,
     "report": phase_report, "harvest": phase_harvest,
     "export": phase_export}[args.phase](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
