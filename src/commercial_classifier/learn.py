"""The learned crosswalk: classify a county's codes once, not its parcels.

`distinct` writes one row per distinct use value (system, county, code,
description) with what the parcels behind it look like: how many, median
building size, share improved, median units, the words that recur in owner and
business names. A county carries 50 to 400 distinct values, so Claude (or a
person) labels a few hundred rows instead of a hundred thousand parcels.

`label` validates every label against the taxonomy, caps its confidence by who
made it (keyword 0.6, Claude 0.75, a human 0.9), and merges it into
`crosswalks/learned.csv`, which the next classify run picks up.
"""
from __future__ import annotations

import collections
import csv
import os
import re
import statistics

from . import crosswalk, profiles, schema, store, taxonomy

ENTITY_RX = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|COMPANY|LP|LLP|LTD|TRUST|PARTNERS|PARTNERSHIP|"
                       r"HOLDINGS|PROPERTIES|ASSOCIATES|ENTERPRISES|CHURCH|MINISTRIES|BANK|AUTHORITY|"
                       r"CITY|COUNTY|STATE|UNIVERSITY|SCHOOL|HOSPITAL|FOUNDATION|ASSOCIATION|CLUB)\b")
STOP = {"LLC", "L", "C", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LP", "LLP", "LTD", "TRUST", "THE",
        "OF", "AND", "PARTNERS", "PARTNERSHIP", "HOLDINGS", "PROPERTIES", "PROPERTY", "ASSOCIATES", "GROUP",
        "INVESTMENTS", "ENTERPRISES", "ETAL", "ET", "AL", "TR", "TRUSTEE", "REVOCABLE", "LIVING", "FAMILY",
        "REAL", "ESTATE", "DEVELOPMENT", "MANAGEMENT", "VENTURES", "HOLDING", "II", "III", "IV", "JR", "SR"}
DISTINCT_COLS = ["system", "county_key", "code", "desc", "count", "share_pct", "median_sqft", "pct_improved",
                 "median_units", "median_value", "top_name_words", "sample_address", "parent_label",
                 "prelabel_class", "prelabel_sub", "prelabel_conf", "prelabel_source", "prelabel_reason",
                 "needs_label", "asset_class", "sub_type", "notes"]
LEARNED_COLS = crosswalk.XW_COLS


class _Agg:
    __slots__ = ("count", "sqft", "improved", "units", "values", "words", "addrs", "parents")

    def __init__(self):
        self.count = 0
        self.sqft, self.units, self.values = [], [], []
        self.improved = 0
        self.words = collections.Counter()
        self.addrs = []
        self.parents = collections.Counter()

    def add(self, rec, parent=None):
        self.count += 1
        sf, impr = rec.get("building_sqft"), rec.get("mv_impr")
        if len(self.sqft) < 2000 and sf:
            self.sqft.append(sf)
        if (sf or 0) > 0 or (impr or 0) > 0:
            self.improved += 1
        u = rec.get("units_res") or rec.get("units_comm")
        if u and len(self.units) < 2000:
            self.units.append(u)
        v = rec.get("mv_total") or rec.get("assessed_total")
        if v and len(self.values) < 2000:
            self.values.append(v)
        for nm in (rec.get("business_name"), rec.get("owner1")):
            if nm and (nm == rec.get("business_name") or ENTITY_RX.search(nm.upper())):
                for w in re.findall(r"[A-Z]{3,}", nm.upper()):
                    if w not in STOP:
                        self.words[w] += 1
        if len(self.addrs) < 2 and rec.get("situs_address"):
            self.addrs.append("%s %s" % (rec["situs_address"], rec.get("situs_city") or ""))
        if parent:
            self.parents[parent] += 1


def _median(xs):
    return round(statistics.median(xs)) if xs else ""


def distinct(work, extra=()):
    con = store.connect(work)
    store.require(con, "ingest")
    prof = profiles.PROFILES[store.get_meta(con, "profile")["name"]]
    xw = crosswalk.Crosswalks(work, extra)
    agg = collections.defaultdict(_Agg)
    total = 0
    for row in con.execute("SELECT * FROM records"):
        rec = dict(row)
        total += 1
        ck = rec["county_key"]
        best, bkey, _, _ = crosswalk.resolve(xw, rec, prof, ck)
        if bkey is None:
            agg[crosswalk.Key("none", ck or "", "", "")].add(rec)
            continue
        agg[bkey].add(rec)
        if best and bkey.system == "fa" and best.confidence <= 0.7:
            for k in crosswalk.use_keys(rec, prof, ck):
                if k.system == "county_use":
                    agg[k].add(rec, parent="FA %s = %s%s" % (bkey.code, best.asset_class,
                                                             (" / " + best.sub_type) if best.sub_type else ""))
    rows = []
    for k, a in agg.items():
        tab = xw.table_label(k) if k.system != "none" else None
        kw = None
        if not tab and k.system != "none":
            kw, _ = xw.keyword_label(k)
        pre = tab or kw
        share = a.count / float(total)
        needs = k.system != "none" and (pre is None or (pre.source in ("keyword", "derived")
                                                        and (pre.confidence < 0.6 or share > 0.01)))
        rows.append({
            "system": k.system, "county_key": k.ck, "code": k.code, "desc": k.desc, "count": a.count,
            "share_pct": round(share * 100, 2), "median_sqft": _median(a.sqft),
            "pct_improved": round(a.improved * 100.0 / a.count), "median_units": _median(a.units),
            "median_value": _median(a.values),
            "top_name_words": ", ".join("%s(%d)" % kv for kv in a.words.most_common(6)),
            "sample_address": " | ".join(a.addrs),
            "parent_label": a.parents.most_common(1)[0][0] if a.parents else "",
            "prelabel_class": pre.asset_class if pre else "", "prelabel_sub": pre.sub_type if pre else "",
            "prelabel_conf": pre.confidence if pre else "", "prelabel_source": pre.source if pre else "",
            "prelabel_reason": pre.reason if pre else "", "needs_label": "Y" if needs else "",
            "asset_class": "", "sub_type": "", "notes": "",
        })
    rows.sort(key=lambda r: (r["needs_label"] != "Y", -r["count"]))
    path = os.path.join(work, "distinct_codes.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DISTINCT_COLS)
        w.writeheader()
        w.writerows(rows)
    need = [r for r in rows if r["needs_label"] == "Y"]
    stats = {"distinct_values": len(rows), "needs_label": len(need),
             "parcels_behind_needs_label": sum(r["count"] for r in need), "parcels": total, "path": path}
    store.set_meta(con, "distinct", stats)
    con.close()
    return stats


def label(work, labels_path, default_source="claude"):
    """Merge labels into the learned crosswalk. Returns (accepted, rejected)."""
    accepted, rejected = [], []
    with open(labels_path, encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), 2):
            if not (r.get("asset_class") or "").strip():
                continue
            system = (r.get("system") or "").strip()
            if not system or not ((r.get("code") or "").strip() or (r.get("desc") or "").strip()):
                rejected.append((i, "row needs system and a code or desc"))
                continue
            try:
                ac, st = taxonomy.validate(r["asset_class"], r.get("sub_type", ""))
            except taxonomy.TaxonomyError as e:
                rejected.append((i, str(e)))
                continue
            src = (r.get("source") or "").strip().lower()
            if src not in ("claude", "human"):
                src = default_source
            cap = crosswalk.CAPS[src]
            try:
                conf = min(float(r.get("confidence") or cap), cap)
            except ValueError:
                conf = cap
            accepted.append({"system": system, "county_key": (r.get("county_key") or "*").strip().upper() or "*",
                             "code": crosswalk.code_key(system, r.get("code")),
                             "desc": crosswalk.desc_key(r.get("desc")), "asset_class": ac, "sub_type": st,
                             "confidence": conf, "source": src, "notes": (r.get("notes") or "").strip()})
    path = crosswalk.learned_path(work)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                existing[(r["system"], r["county_key"], r["code"], r["desc"])] = r
    for r in accepted:
        existing[(r["system"], r["county_key"], r["code"], r["desc"])] = r
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEARNED_COLS)
        w.writeheader()
        for r in existing.values():
            w.writerow({c: r.get(c, "") for c in LEARNED_COLS})
    con = store.connect(work)
    store.set_meta(con, "labels", {"accepted": len(accepted), "rejected": len(rejected),
                                   "learned_rows": len(existing), "from": os.path.abspath(labels_path)})
    store.invalidate_after(con, "geometry")
    con.close()
    return accepted, rejected
