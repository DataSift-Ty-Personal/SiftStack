"""Classify every record: tier 0 (code tables and learned crosswalks), tier 1
rules, unit counts, the commercial-target tag, and the overrides ledger.

Rules run on canonical fields, so they work on any source:
  shopping-center size ladder, regional-mall outparcels, fuel brand on a
  convenience store, no improvement -> land, unit-driven multifamily, and the
  industrial-on-3+-stories physics guard.
The no-improvement rule only fires when BOTH the building size and the
improvement value were in the file and both are zero; a missing column is not
evidence of a vacant lot.
"""
from __future__ import annotations

import collections
import csv
import os
import re

from . import crosswalk, keywords, profiles, schema, store, taxonomy
from .rulepacks import fa as fa_pack

CLS_COLS = ["rid", "asset_class", "sub_type", "confidence", "provenance", "method", "reason",
            "commercial_target", "units", "unit_provenance", "flags", "use_system", "use_county",
            "use_code", "use_desc", "label_source"]
RES_CLASSES = ("Residential", "Residential Income", "Multifamily")
MF_SUBS_KEEP = ("Dormitory", "High-Rise", "Mid-Rise", "Low-Rise", "Garden", "Manufactured Housing/Mobile Home")
# a condo unit or a single mobile home carries the complex-wide unit count;
# it is still one dwelling, not a hidden apartment building
_SINGLE_UNIT = re.compile(r"\bCONDO|\bMOBILE|\bMANUFACTURED")
_UNIT_WORDS = [(re.compile(r"\bDUPLEX"), 2), (re.compile(r"\bTRIPLEX"), 3), (re.compile(r"\b(FOURPLEX|QUADPLEX|QUADRUPLEX)"), 4)]


def overrides_path(work):
    return os.path.join(work, "overrides.csv")


def load_overrides(work):
    """overrides.csv: rid (or apn + county_key), asset_class, sub_type, note."""
    path = overrides_path(work)
    by_rid, by_apn = {}, {}
    if not os.path.exists(path):
        return by_rid, by_apn
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                ac, st = taxonomy.validate(r.get("asset_class"), r.get("sub_type", ""))
            except taxonomy.TaxonomyError:
                continue
            val = (ac, st, (r.get("note") or "").strip())
            if (r.get("rid") or "").strip():
                by_rid[r["rid"].strip()] = val
            elif (r.get("apn") or "").strip():
                by_apn[((r.get("county_key") or "").strip().upper(), schema.norm_apn(r["apn"]))] = val
    return by_rid, by_apn


def _stories_sub(stories):
    if not stories:
        return ""
    return "Garden" if stories <= 3 else "Mid-Rise" if stories <= 6 else "High-Rise"


def _land_sub(ac):
    if ac in RES_CLASSES:
        return "Residential/Multifamily"
    if ac == "Industrial":
        return "Industrial"
    return "Commercial"


def run(work, extra=()):
    con = store.connect(work)
    store.require(con, "ingest")
    pmeta = store.get_meta(con, "profile")
    prof = profiles.PROFILES[pmeta["name"]]
    xw = crosswalk.Crosswalks(work, extra)
    is_fa = prof.name == "fa"

    # pass 1: resolve every record, and collect the dataset-level facts the
    # rules need (true regional malls, how many 2038 "casinos" per county)
    first = {}
    mall_situs = set()
    fa2038 = collections.Counter()
    for row in con.execute("SELECT * FROM records"):
        rec = dict(row)
        best, key, _, flags = crosswalk.resolve(xw, rec, prof, rec["county_key"])
        first[rec["rid"]] = (best, key, flags)
        if best and (best.asset_class, best.sub_type) == ("Shopping Center", "Regional Mall") \
                and (rec.get("building_sqft") or 0) >= 250000:
            mall_situs.add((rec["county_key"], schema.addr_base(rec.get("situs_address"))))
        if is_fa and rec.get("use_code") == fa_pack.CASINO_CODE:
            fa2038[rec["county_key"]] += 1

    by_rid, by_apn = load_overrides(work)
    out = []
    stats = collections.Counter()
    for row in con.execute("SELECT * FROM records"):
        rec = dict(row)
        rid, ck = rec["rid"], rec["county_key"]
        best, key, flags = first[rid]
        flags = list(flags)
        if best:
            ac, st, conf, prov, method, reason = (best.asset_class, best.sub_type, best.confidence,
                                                  best.provenance, best.method, best.reason)
            hint, src = best.target_hint, best.source
        else:
            ac, st, conf, prov, method, reason, hint, src = "Other", "", 0.3, "derived", "none", "no usable use signal", "", ""
        sf, impr = rec.get("building_sqft"), rec.get("mv_impr")
        text = " ".join(x for x in (rec.get("subdivision"), rec.get("legal_desc"), rec.get("owner_last")) if x).upper()

        # business or tenant name on the parcel
        bn = rec.get("business_name")
        if bn:
            hit, _ = keywords.name(bn)
            if hit:
                if hit.asset_class == ac:
                    if prov != "sourced" and conf < 0.9:
                        conf = round(min(0.9, conf + 0.1), 2)
                        reason += "; business name agrees"
                    if not st and hit.sub_type:
                        st = hit.sub_type
                elif conf < 0.6 and hit.confidence >= 0.55:
                    ac, st, conf, prov, method = hit.asset_class, hit.sub_type, hit.confidence, "derived", "tier1-business-name"
                    reason = hit.reason
                elif conf < 0.9:
                    flags.append("business name suggests %s" % hit.asset_class)

        # tier 1 rules (one of these, in this order, as in the original pipeline)
        if is_fa and rec.get("use_code") == fa_pack.CASINO_CODE and fa2038[ck] >= fa_pack.CONDO_GATE:
            ac, st, conf, method = fa_pack.condo(text)
            prov, reason = "derived", "FA 2038 used for commercial condos in this county (%d parcels)" % fa2038[ck]
        elif (ac, st) == ("Shopping Center", "Regional Mall"):
            if (ck, schema.addr_base(rec.get("situs_address"))) in mall_situs:
                pass
            elif sf and sf < 30000:
                ac, st, conf, prov, method = "Retail", "Freestanding", 0.6, "derived", "tier1-mall-outparcel"
                reason = "coded regional mall but under 30,000 SF and not at a mall address"
            elif sf and sf < 100000:
                st, conf, prov, method = "Neighborhood Center", 0.7, "derived", "tier1-sc-size"
            elif sf and sf < 350000:
                st, conf, prov, method = "Community Center", 0.7, "derived", "tier1-sc-size"
        elif ac == "Shopping Center" and st in ("", "Strip Center", "Neighborhood Center", "Community Center") and sf:
            st2 = ("Strip Center" if sf < 30000 else "Neighborhood Center" if sf < 100000
                   else "Community Center" if sf < 350000 else None)
            if st2 and st2 != st:
                st, conf, prov, method = st2, 0.8, "derived", "tier1-sc-size"
                reason = "shopping center sized by building area (%d SF)" % sf
        elif (ac, st) == ("Retail", "Grocery/Convenience Store") and \
                keywords.has_fuel_brand(" ".join(x for x in (text, rec.get("business_name"), rec.get("owner1")) if x)):
            st, conf, prov, method = "Service/Gas Station", 0.85, "derived", "tier1-fuel-keyword"
            reason = "convenience store with a fuel brand on the record"

        # no improvement at all -> land (only when both fields were present)
        if (sf is not None and impr is not None and sf == 0 and impr == 0
                and ac not in ("Land", "Civic/Exempt") and st not in ("Parking", "Manufactured Housing/Mobile Home")):
            st = _land_sub(ac)
            ac, conf, prov, method = "Land", 0.7, "derived", "tier1-vacant"
            reason = "no building area and no improvement value"

        # units, and multifamily decided by them
        units, uprov = None, ""
        cu, ru = rec.get("units_comm"), rec.get("units_res")
        if cu and cu > 0:
            units, uprov = int(cu), "sourced"
        elif ru and ru > 1:
            units, uprov = int(ru), "sourced"
        elif is_fa and rec.get("use_code") in fa_pack.CODE_UNITS:
            units, uprov = fa_pack.CODE_UNITS[rec["use_code"]], "derived-code"
        else:
            ut = " ".join(x for x in (rec.get("use_desc"), rec.get("structure_type"), rec.get("vendor_type")) if x).upper()
            for rx, n in _UNIT_WORDS:
                if rx.search(ut):
                    units, uprov = n, "derived-code"
                    break
        use_text = " ".join(x for x in (rec.get("use_desc"), rec.get("structure_type"), rec.get("vendor_type")) if x).upper()
        unit_record = ac == "Residential" and (_SINGLE_UNIT.search(use_text) or
                                               (is_fa and rec.get("use_code") in ("1004", "1006")))
        if units and ac in RES_CLASSES and st != "Manufactured Housing/Mobile Home" and not unit_record:
            if units >= 5:
                new_st = st if (ac == "Multifamily" and st in MF_SUBS_KEEP) else _stories_sub(rec.get("stories"))
                if ac != "Multifamily" or new_st != st:
                    ac, st, method, prov = "Multifamily", new_st, "tier1-res-units", "derived"
                    conf = 0.85 if uprov == "sourced" else 0.75
                    reason = "%d units" % units
            elif units >= 2 and ac != "Residential Income":
                ac, st, method, prov = "Residential Income", "", "tier1-res-units", "derived"
                conf = 0.85 if uprov == "sourced" else 0.75
                reason = "%d units" % units

        # physics: a warehouse or flex building is one or two stories
        if ac == "Industrial" and (rec.get("stories") or 0) >= 3:
            if prov in ("sourced", "learned-human", "verified"):
                flags.append("industrial code on a %g-story building" % rec["stories"])
            else:
                ac, st, conf, prov, method = "Office", "", 0.6, "derived", "tier1-stories-correction"
                reason = "industrial label on a %g-story building" % rec["stories"]

        # a person or a review round said so
        ov = by_rid.get(rid) or by_apn.get((ck, schema.norm_apn(rec.get("apn"))))
        if ov:
            ac, st, note = ov
            conf, prov, method, reason = 0.9, "verified", "override", note or "overrides.csv"

        target =taxonomy.commercial_target(ac, st, hint if method.startswith("tier0") else "",
                                            rec.get("property_class") or "", prof.cre_class_ids)
        out.append((rid, ac, st, round(conf, 2), prov, method, reason, target, units, uprov, "; ".join(flags),
                    key.system if key else "", key.ck if key else "", key.code if key else "",
                    key.desc if key else "", src))
        stats[method.split(":")[0]] += 1
    store.create_table(con, "cls", [(c, "REAL" if c == "confidence" else "INTEGER" if c == "units" else "TEXT")
                                    for c in CLS_COLS])
    store.insert_many(con, "cls", CLS_COLS, out)
    n = len(out)
    by_class = collections.Counter(r[1] for r in out)
    res = {"parcels": n, "by_method": dict(stats.most_common()),
           "by_class": dict(by_class.most_common()),
           "conf_ge_070_pct": round(sum(1 for r in out if r[3] >= 0.7) * 100.0 / n, 1),
           "review_band_lt055": sum(1 for r in out if r[3] < 0.55),
           "overrides_applied": stats.get("override", 0),
           "crosswalk_sources": [(os.path.basename(p), k) for p, k in xw.sources]}
    store.mark_done(con, "classify", res)
    con.close()
    return res
