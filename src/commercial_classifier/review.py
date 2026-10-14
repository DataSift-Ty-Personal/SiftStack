"""The review queue, the per-county QA report, and closing the loop.

Queue reasons, highest weight wins per parcel:
  4  a property-level business inside the parcel disagrees with our class
  3  confidence under 0.55, or two confident signals disagree
  2  value implausible for the building size, an industrial code on a 3+ story
     building, or a business seen near (not inside) the parcel disagrees
  1  spot check: the three highest-value parcels behind each label Claude
     learned, because a wrong label on a code repeats on every parcel with it
Priority = weight x log10(value), so the expensive mistakes come first.

`apply` reads a reviewed queue back: a correct_class (and correct_sub) becomes
a row in overrides.csv, applied as 'verified' on the next classify.
"""
from __future__ import annotations

import collections
import csv
import json
import math
import os

from . import classify, schema, store, taxonomy

REVIEW_COLS = ["rid", "apn", "situs", "city", "state", "county_key", "asset_class", "sub_type", "confidence",
               "method", "value_band", "value_estimate", "reason", "suggestion", "nearby_businesses", "latitude",
               "longitude", "verdict", "correct_class", "correct_sub", "notes"]


def out_dir(work):
    d = os.path.join(work, "out")
    os.makedirs(d, exist_ok=True)
    return d


def run(work):
    con = store.connect(work)
    store.require(con, "classify")
    has_val = store.table_exists(con, "val") and store.done(con, "value")
    has_poi = store.table_exists(con, "poi") and store.done(con, "poi")
    sql = ("SELECT r.rid, r.apn, r.situs_address, r.situs_city, r.situs_state, r.county_key, r.lat, r.lon, "
           "r.mv_total, c.asset_class, c.sub_type, c.confidence, c.method, c.flags, c.label_source, c.use_system, "
           "c.use_county, c.use_code, c.use_desc %s %s FROM records r JOIN cls c USING (rid) %s %s" % (
               ", v.value_band, v.value_estimate, v.value_basis" if has_val else "",
               ", p.kind AS poi_kind, p.mode AS poi_mode, p.expect_class, p.expect_sub, p.businesses" if has_poi else "",
               "LEFT JOIN val v USING (rid)" if has_val else "", "LEFT JOIN poi p USING (rid)" if has_poi else ""))
    queue = {}
    spot = collections.defaultdict(list)

    def add(r, reason, weight, suggestion=""):
        if r["rid"] in queue and queue[r["rid"]][0] >= weight:
            return
        queue[r["rid"]] = (weight, reason, suggestion, r)

    no_signal = 0
    for row in con.execute(sql):
        r = dict(row)
        flags = r.get("flags") or ""
        if r["method"] == "none" and r.get("poi_kind") != "queue":
            # nothing in the file says what this parcel is (often a right of
            # way or a fresh split); counted in the QA report, not queued
            no_signal += 1
            continue
        if r.get("poi_kind") == "queue":
            sug = r["expect_class"] + ("/" + r["expect_sub"] if r.get("expect_sub") else "")
            add(r, "business on the parcel suggests %s" % sug, 4 if r.get("poi_mode") == "polygon" else 2, sug)
        if (r["confidence"] or 0) < 0.55:
            add(r, "low confidence (%s)" % r["method"], 3)
        if "disagree" in flags or "mixed-use" in flags:
            add(r, flags, 3)
        if "industrial code on" in flags:
            add(r, flags, 2, "Office")
        if has_val and (r.get("value_basis") or "").endswith("-check"):
            add(r, "value implausible for the building size", 2)
        if r.get("label_source") == "claude":
            spot[(r["use_system"], r["use_county"], r["use_code"], r["use_desc"])].append(r)
    for key, rs in spot.items():
        rs.sort(key=lambda x: -((x.get("value_estimate") or x.get("mv_total") or 0)))
        for r in rs[:3]:
            add(r, "spot check a learned label (%s %s)" % (key[0], key[2] or key[3]), 1)
    items = []
    for rid, (w, reason, sug, r) in queue.items():
        v = r.get("value_estimate") or r.get("mv_total") or 0
        items.append((w * math.log10(max(v, 10)), {
            "rid": rid, "apn": r["apn"], "situs": r["situs_address"], "city": r["situs_city"],
            "state": r["situs_state"], "county_key": r["county_key"], "asset_class": r["asset_class"],
            "sub_type": r["sub_type"], "confidence": r["confidence"], "method": r["method"],
            "value_band": r.get("value_band") or "", "value_estimate": r.get("value_estimate") or "",
            "reason": reason, "suggestion": sug, "nearby_businesses": r.get("businesses") or "",
            "latitude": r["lat"], "longitude": r["lon"], "verdict": "", "correct_class": "", "correct_sub": "",
            "notes": ""}))
    items.sort(key=lambda t: -t[0])
    rows = [d for _, d in items]
    store.create_table(con, "review", [(c, "TEXT") for c in REVIEW_COLS])
    store.insert_many(con, "review", REVIEW_COLS, ([d[c] for c in REVIEW_COLS] for d in rows))
    path = os.path.join(out_dir(work), "review_queue.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=REVIEW_COLS)
        wr.writeheader()
        wr.writerows(rows)
    by_reason = collections.Counter(d["reason"].split(" (")[0].split(":")[0] for d in rows)
    stats = {"queued": len(rows), "by_reason": dict(by_reason.most_common()), "path": path,
             "parcels_with_no_use_signal": no_signal}
    qa = qa_report(con)
    with open(os.path.join(out_dir(work), "qa_report.json"), "w", encoding="utf-8") as f:
        json.dump(qa, f, indent=2)
    stats["qa_report"] = os.path.join(out_dir(work), "qa_report.json")
    store.mark_done(con, "review", stats)
    con.close()
    return stats


def qa_report(con):
    """Gates per county plus every phase's own stats, including what was
    skipped and why. A skipped phase is reported, never silently green."""
    prof = store.get_meta(con, "profile") or {}
    counties = []
    has_val = store.done(con, "value")
    has_own = store.done(con, "owners")
    sql = ("SELECT r.county_key, c.asset_class, c.confidence, c.method, c.sub_type, r.building_sqft %s %s "
           "FROM records r JOIN cls c USING (rid) %s %s" % (
               ", v.value_band, v.building_grade" if has_val else "", ", o.tier, o.portfolio_parcels" if has_own else "",
               "LEFT JOIN val v USING (rid)" if has_val else "", "LEFT JOIN own o USING (rid)" if has_own else ""))
    per = collections.defaultdict(list)
    for r in con.execute(sql):
        per[r["county_key"] or "(no county)"].append(dict(r))
    for ck, rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        conf = [r["confidence"] or 0 for r in rs]
        sec = {"county_key": ck, "parcels": n,
               "class_conf_ge_070_pct": round(sum(c >= 0.7 for c in conf) * 100.0 / n, 1),
               "class_conf_ge_055_pct": round(sum(c >= 0.55 for c in conf) * 100.0 / n, 1),
               "review_band_lt055": sum(c < 0.55 for c in conf),
               "by_class": dict(collections.Counter(r["asset_class"] for r in rs).most_common()),
               "by_method": dict(collections.Counter(r["method"].split(":")[0] for r in rs).most_common(12))}
        if has_val:
            elig = [r for r in rs if r["asset_class"] != "Civic/Exempt"]
            grd = [r for r in rs if (r["building_sqft"] or 0) > 0 and r["asset_class"] not in ("Land", "Civic/Exempt")
                   and r["sub_type"] != "Parking"]
            sec["value_coverage_pct"] = round(sum(1 for r in elig if r["value_band"]) * 100.0 / max(len(elig), 1), 1)
            sec["grade_coverage_pct"] = round(sum(1 for r in grd if r["building_grade"]) * 100.0 / max(len(grd), 1), 1)
        if has_own:
            multi = [r for r in rs if (r.get("portfolio_parcels") or 0) > 1]
            sec["owner_green_pct_of_multi_parcel"] = round(
                sum(1 for r in multi if r["tier"] == "green") * 100.0 / len(multi), 1) if multi else None
        counties.append(sec)
    phases = {p: store.stats_of(con, p) for p in store.PHASES if store.done(con, p)}
    skipped = {p: s["skipped"] for p, s in phases.items() if isinstance(s, dict) and s.get("skipped")}
    unmapped_signals = [f for f in schema.CLASS_SIGNALS if f not in (prof.get("mapping") or {})]
    return {"profile": prof.get("name"), "profile_label": prof.get("label"), "mapping": prof.get("mapping"),
            "class_signals_not_in_file": unmapped_signals, "situs_state_inferred": prof.get("situs_state_inferred"),
            "counties": counties, "phases": phases, "skipped": skipped,
            "distinct": store.get_meta(con, "distinct"), "labels": store.get_meta(con, "labels"),
            "limits": ["Classes come from codes, rules and business points; nothing here walked the property.",
                       "Value bands are bands, not appraisals: check the holdout accuracy before quoting one.",
                       "Owner groups that share a mailing address are leads, not proven corporate parents."]}


def apply(work, reviewed_csv):
    """Turn reviewed queue rows into overrides. Returns (written, skipped)."""
    written, skipped = [], []
    with open(reviewed_csv, encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), 2):
            cc = (r.get("correct_class") or "").strip()
            verdict = (r.get("verdict") or "").strip().lower()
            if not cc and verdict in ("ok", "right", "correct", "y", "yes", "confirm", "confirmed"):
                cc, cs = r.get("asset_class"), r.get("sub_type", "")
            else:
                cs = r.get("correct_sub", "")
            if not cc:
                continue
            try:
                ac, st = taxonomy.validate(cc, cs)
            except taxonomy.TaxonomyError as e:
                skipped.append((i, str(e)))
                continue
            written.append({"rid": (r.get("rid") or "").strip(), "apn": r.get("apn", ""),
                            "county_key": r.get("county_key", ""), "asset_class": ac, "sub_type": st,
                            "note": ("review: " + (r.get("notes") or verdict or "corrected")).strip()})
    path = classify.overrides_path(work)
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            existing = list(csv.DictReader(f))
    keyed = {(e.get("rid") or "", e.get("apn") or ""): e for e in existing}
    for w in written:
        keyed[(w["rid"], w["apn"])] = w
    cols = ["rid", "apn", "county_key", "asset_class", "sub_type", "note"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for e in keyed.values():
            wr.writerow({c: e.get(c, "") for c in cols})
    con = store.connect(work)
    store.invalidate_after(con, "geometry")
    con.close()
    return written, skipped
