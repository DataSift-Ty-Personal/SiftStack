"""Value bands, unit counts and building grades, calibrated from the file's own
sales.

Estimate ladder per parcel:
  recent-sale           an arm's-length sale ($25K+) inside the recent window
  assessor-calibrated   market or assessed value x (median sale / value ratio)
                        for that county and class, falling back to the class
                        across counties, then the whole file
  vendor-avm            the source's own estimate (DataSift, PropStream)
  acre-median / sf-median  class medians when the parcel has no value at all
Windows are relative to the newest sale in the file (calibration pool: 5 years,
recent sale: 4 years), not to a hard-coded year, so the same code works on a
2019 extract and a 2026 one. With no sales at all, a market value is used
uncalibrated and an assessed-only value is NOT turned into a price, because
the assessment ratio differs by state and class and guessing it is fiction.

Accuracy is measured, not asserted: half the sales calibrate, the other half
are scored for exact and within-one-band agreement.
"""
from __future__ import annotations

import collections
import datetime as _dt
import hashlib
import re
import statistics

from . import store

BANDS = [(100_000, "<$100K"), (250_000, "$100K-$250K"), (500_000, "$250K-$500K"),
         (1_000_000, "$500K-$1M"), (2_500_000, "$1M-$2.5M"), (5_000_000, "$2.5M-$5M"),
         (10_000_000, "$5M-$10M"), (float("inf"), "$10M+")]
BAND_NAMES = [b for _, b in BANDS]
MF = ("Multifamily", "Residential Income")
UNIT_SF = {"Garden": 950, "Low-Rise": 950, "Mid-Rise": 900, "High-Rise": 850, "Dormitory": 500,
           "Manufactured Housing/Mobile Home": 1000}
QPTS = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 1}
CPTS = {"Excellent": 0.75, "Very Good": 0.75, "Good": 0.25, "Average": 0, "Fair": -0.25, "Poor": -1, "Unsound": -1.5}
VAL_COLS = ["rid", "value_estimate", "value_band", "value_basis", "value_confidence", "value_per_sf",
            "value_per_unit", "units_final", "unit_provenance_final", "building_grade", "grade_provenance"]


def band(v):
    for lim, name in BANDS:
        if v < lim:
            return name
    return BANDS[-1][1]


def _shift(date_s, months):
    d = _dt.date.fromisoformat(date_s)
    y, m = divmod(d.month - 1 - months, 12)
    return _dt.date(d.year + y, m + 1, min(d.day, 28)).isoformat()


def _grade(ac, st, sf, quality, condition, year):
    if not sf or sf <= 0 or ac in ("Land", "Civic/Exempt") or st == "Parking":
        return "", ""
    q = (quality or "")[:1].upper()
    if q in QPTS:
        s = QPTS[q] + CPTS.get(condition or "", 0)
        if year and year >= 2010:
            s += 0.5
        elif year and year < 1975:
            s -= 0.5
        prov = "sourced"
    else:
        s = 2.0 + CPTS.get(condition or "", 0)
        if year and year >= 2010:
            s += 1.0
        elif year and year >= 1990:
            s += 0.5
        elif year and year < 1975:
            s -= 0.5
        prov = "derived"
    return ("A" if s >= 3.5 else "B" if s >= 2.5 else "C" if s >= 1.5 else "D"), prov


def _park_key(r):
    street = re.sub(r"\s+(LOT|TRLR|#)\s*\S*$", "", (r.get("situs_address") or "").strip().upper())
    street = re.sub(r"^\d+\s+", "", street)
    return (r["county_key"], street, (r.get("owner1") or "")[:20].upper())


def run(work, recent_since=None, calib_years=5, recent_years=4):
    con = store.connect(work)
    store.require(con, "classify")
    rows = [dict(r) for r in con.execute(
        "SELECT r.rid, r.county_key, r.situs_address, r.owner1, r.building_sqft, r.lot_acres, r.year_built, "
        "r.quality, r.condition, r.mv_total, r.assessed_total, r.vendor_avm, r.sale_price, r.sale_date, "
        "c.asset_class, c.sub_type, c.units, c.unit_provenance FROM records r JOIN cls c USING (rid)")]
    dates = [r["sale_date"] for r in rows if r["sale_date"] and (r["sale_price"] or 0) >= 25000]
    newest = max(dates) if dates else None
    cal_since = recent_since or (_shift(newest, 12 * calib_years) if newest else None)
    rec_since = recent_since or (_shift(newest, 12 * recent_years) if newest else None)
    has_market = any(r["mv_total"] for r in rows)
    basis_field = "mv_total" if has_market else "assessed_total"

    def base(r):
        return r["mv_total"] if has_market else r["assessed_total"]

    def is_cal_sale(r):
        sp, mv = r["sale_price"] or 0, base(r) or 0
        return (sp >= 25000 and mv > 10000 and r["sale_date"] and cal_since and r["sale_date"] >= cal_since
                and 0.2 < sp / mv < 8)

    def holdout(r):
        return int(hashlib.sha1(r["rid"].encode()).hexdigest()[:2], 16) % 2 == 1

    def calibrate(pool_rows):
        cell, by_class, allr = collections.defaultdict(list), collections.defaultdict(list), []
        for r in pool_rows:
            x = r["sale_price"] / base(r)
            cell[(r["county_key"], r["asset_class"])].append(x)
            by_class[r["asset_class"]].append(x)
            allr.append(x)
        c1 = {k: statistics.median(v) for k, v in cell.items() if len(v) >= 8}
        c2 = {k: statistics.median(v) for k, v in by_class.items() if len(v) >= 20}
        g = statistics.median(allr) if allr else None
        return c1, c2, g

    sales = [r for r in rows if is_cal_sale(r)]
    cal_rows = [r for r in sales if not holdout(r)]
    test_rows = [r for r in sales if holdout(r)]
    c1, c2, g = calibrate(cal_rows)
    exact = within = 0
    for r in test_rows:
        f = c1.get((r["county_key"], r["asset_class"])) or c2.get(r["asset_class"]) or g
        if not f:
            continue
        b0, b1 = BAND_NAMES.index(band(r["sale_price"])), BAND_NAMES.index(band(base(r) * f))
        exact += b0 == b1
        within += abs(b0 - b1) <= 1
    holdout_stats = ({"sales_scored": len(test_rows), "exact_pct": round(exact * 100.0 / len(test_rows), 1),
                      "within_one_pct": round(within * 100.0 / len(test_rows), 1)} if test_rows else
                     {"note": "no usable sales to score against"})
    # final factors use every sale
    c1, c2, g = calibrate(sales)

    def factor(r):
        return c1.get((r["county_key"], r["asset_class"])) or c2.get(r["asset_class"]) or g

    psf, pacre = collections.defaultdict(list), collections.defaultdict(list)
    for r in rows:
        b, sf = base(r) or 0, r["building_sqft"] or 0
        if b > 10000 and sf > 0:
            psf[(r["county_key"], r["asset_class"])].append(b / sf)
        if r["asset_class"] == "Land" and b > 1000 and (r["lot_acres"] or 0) > 0.05:
            pacre[r["county_key"]].append(b / r["lot_acres"])
    med_psf = {k: statistics.median(v) for k, v in psf.items() if len(v) >= 20}
    med_pacre = {k: statistics.median(v) for k, v in pacre.items() if len(v) >= 20}

    parks = collections.defaultdict(list)
    for r in rows:
        if r["sub_type"] == "Manufactured Housing/Mobile Home":
            parks[_park_key(r)].append(r)
    park_pads = {}
    for k, members in parks.items():
        best = max([m["units"] or 0 for m in members] + [round((m["lot_acres"] or 0) * 6) for m in members
                                                          if (m["lot_acres"] or 0) >= 1] + [0])
        park_pads[k] = int(max(best, len(members) if len(members) >= 5 else 0))

    out = []
    cnt = collections.Counter()
    for r in rows:
        ac, st, sf = r["asset_class"], r["sub_type"] or "", r["building_sqft"] or 0
        b = base(r) or 0
        sp, sd = r["sale_price"] or 0, r["sale_date"]
        if ac == "Civic/Exempt":
            est, basis, conf = 0, "none", None
        elif sp >= 25000 and sd and rec_since and sd >= rec_since:
            est, basis, conf = sp, "recent-sale", 0.8
        elif b > 10000 and factor(r):
            est, basis, conf = b * factor(r), "%s-calibrated" % ("assessor" if has_market else "assessed"), 0.5
        elif b > 10000 and has_market:
            est, basis, conf = b, "assessor-uncalibrated", 0.35
        elif (r["vendor_avm"] or 0) > 10000:
            est, basis, conf = r["vendor_avm"], "vendor-avm", 0.45
        elif ac == "Land" and (r["lot_acres"] or 0) > 0 and r["county_key"] in med_pacre and (factor(r) or has_market):
            est, basis, conf = r["lot_acres"] * med_pacre[r["county_key"]] * (factor(r) or 1.0), "acre-median", 0.3
        elif sf > 0 and (r["county_key"], ac) in med_psf and (factor(r) or has_market):
            est, basis, conf = sf * med_psf[(r["county_key"], ac)] * (factor(r) or 1.0), "sf-median", 0.35
        else:
            est, basis, conf = 0, "none", None
        if est and sf and 0 < sf < 1500 and est / sf > 2000:
            basis += "-check"
            conf = 0.3
        units, uprov = r["units"] or 0, r["unit_provenance"] or ""
        if ac in MF and units < 1:
            if st == "Manufactured Housing/Mobile Home":
                units, uprov = park_pads.get(_park_key(r), 0), "derived-park"
            elif sf >= 2500:
                units, uprov = round(sf / UNIT_SF.get(st, 950)), "derived-sf"
            elif est:
                units, uprov = round(est / (90000 if ac == "Residential Income" else 60000)), "imputed-value"
            floor = 2 if ac == "Residential Income" else 5
            units = max(units, floor)
            if ac == "Residential Income":
                units = min(units, 4)
        g_, gprov = _grade(ac, st, sf, r["quality"], r["condition"], r["year_built"])
        out.append((r["rid"], int(round(est, -3)) if est else None, band(est) if est else "", basis,
                    conf, round(est / sf) if est and sf else None,
                    int(round(est / units, -3)) if est and units >= 2 else None,
                    int(units) if units else None, uprov, g_, gprov))
        cnt[basis] += 1
    store.create_table(con, "val", [(c, "REAL" if c in ("value_estimate", "value_confidence", "value_per_sf",
                                                         "value_per_unit") else "INTEGER" if c == "units_final"
                                     else "TEXT") for c in VAL_COLS])
    store.insert_many(con, "val", VAL_COLS, out)
    valued = [o for o in out if o[2]]
    elig = [o for o, r in zip(out, rows) if r["asset_class"] != "Civic/Exempt"]
    stats = {"newest_sale": newest, "calibration_since": cal_since, "recent_sale_since": rec_since,
             "value_basis_field": basis_field, "calibration_sales": len(sales),
             "county_class_cells": len(c1), "global_factor": round(g, 3) if g else None,
             "holdout": holdout_stats, "by_basis": dict(cnt.most_common()),
             "value_coverage_pct": round(len(valued) * 100.0 / max(len(elig), 1), 1)}
    if not sales:
        has_assessed = any(r["assessed_total"] for r in rows)
        stats["note"] = ("no sale could be paired with a market or assessed value, so nothing was calibrated: " +
                         ("market values are used as-is" if has_market else
                          "assessed values are NOT turned into prices (the assessment ratio is unknown)"
                          if has_assessed else "values come only from recent sales and the source's own estimate"))
    store.mark_done(con, "value", stats)
    con.close()
    return stats
