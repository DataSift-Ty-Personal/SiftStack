#!/usr/bin/env python3
"""Local regression: the generalized engine against the original Knox build.

Runs `commercial_knox_tn all.csv` through the fa profile and joins the result
to `knox_universe_v9.csv` by APN. The gate is agreement on the rows the
original classified deterministically (tier 0 tables and tier 1 rules); rows
the original sent through its Knox-only models or aerial review get a
confusion table, not a gate, because the engine deliberately leaves those for
the review queue.

Never runs in CI: the input is licensed data that lives outside the repo.

    set CC_KNOX_DIR=C:/Users/Tyrus/OneDrive/Desktop/Commerical Classification
    python tools/regress_commercial_knox.py [--work <dir>] [--through owners]
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from commercial_classifier import cli, store  # noqa: E402

DETERMINISTIC = {"tier0", "tier1-vacant-county", "tier1-vacant", "tier1-knox-condo-keyword", "tier1-sc-size",
                 "tier1-mall-outparcel", "tier1-fuel-keyword"}
BANDS = ["<$100K", "$100K-$250K", "$250K-$500K", "$500K-$1M", "$1M-$2.5M", "$2.5M-$5M", "$5M-$10M", "$10M+"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knox-dir", default=os.environ.get("CC_KNOX_DIR"))
    ap.add_argument("--work", default=None)
    ap.add_argument("--through", default="owners", choices=["classify", "value", "owners"])
    ap.add_argument("--gate", type=float, default=99.0)
    a = ap.parse_args()
    if not a.knox_dir or not os.path.isdir(a.knox_dir):
        print("set CC_KNOX_DIR (or --knox-dir) to the Desktop 'Commerical Classification' folder")
        return 2
    src = os.path.join(a.knox_dir, "commercial_knox_tn all.csv")
    ref_path = os.path.join(a.knox_dir, "knox_universe_v9.csv")
    work = a.work or os.path.join(tempfile.gettempdir(), "cc_knox_regress")
    rc = cli.main(["run", src, "--work", work, "--through", a.through, "--accept-keywords", "--no-evidence"])
    if rc not in (0, None):
        print("engine exited %s" % rc)
        return rc
    con = store.connect(work)
    ours = {}
    for r in con.execute("SELECT r.apn, c.asset_class, c.sub_type, c.method FROM records r JOIN cls c USING (rid)"):
        ours[(r["apn"] or "").strip()] = (r["asset_class"], r["sub_type"] or "", r["method"])
    vals = {}
    if store.table_exists(con, "val"):
        for r in con.execute("SELECT r.apn, v.value_band FROM records r JOIN val v USING (rid)"):
            vals[(r["apn"] or "").strip()] = r["value_band"] or ""
    own = {}
    if store.table_exists(con, "own"):
        for r in con.execute("SELECT r.apn, o.entity_id FROM records r JOIN own o USING (rid)"):
            own[(r["apn"] or "").strip()] = r["entity_id"] or ""
    with open(ref_path, encoding="utf-8", newline="") as f:
        ref = {r["apn"].strip(): r for r in csv.DictReader(f)}

    by_method = collections.defaultdict(lambda: [0, 0])
    confusion = collections.Counter()
    det_n = det_ok = 0
    band_n = band_exact = band_one = 0
    for apn, (ac, st, m) in ours.items():
        r = ref.get(apn)
        if not r:
            continue
        same = (ac, st) == (r["asset_class"], r["sub_type"] or "")
        by_method[r["class_method"]][0] += 1
        by_method[r["class_method"]][1] += same
        if r["class_method"] in DETERMINISTIC:
            det_n += 1
            det_ok += same
            if not same:
                confusion[("%s/%s" % (r["asset_class"], r["sub_type"]), "%s/%s" % (ac, st), r["class_method"])] += 1
        b0, b1 = r.get("value_band") or "", vals.get(apn, "")
        if b0 in BANDS and b1 in BANDS:
            band_n += 1
            d = abs(BANDS.index(b0) - BANDS.index(b1))
            band_exact += d == 0
            band_one += d <= 1
    print("\nmatched %d of %d engine rows to v9 (%d v9 rows)" % (sum(v[0] for v in by_method.values()), len(ours), len(ref)))
    print("\nclass + sub-type agreement by original method:")
    for m, (n, ok) in sorted(by_method.items(), key=lambda kv: -kv[1][0]):
        print("  %-28s %6d  %5.1f%%%s" % (m, n, ok * 100.0 / n, "  (gated)" if m in DETERMINISTIC else ""))
    pct = det_ok * 100.0 / max(det_n, 1)
    print("\nDETERMINISTIC ROWS: %d of %d agree (%.2f%%), gate %.1f%%" % (det_ok, det_n, pct, a.gate))
    if confusion:
        print("top disagreements (v9 -> engine, original method):")
        for (x, y, m), c in confusion.most_common(12):
            print("  %5d  %-45s -> %-45s %s" % (c, x, y, m))
    if band_n:
        print("\nvalue band vs v9: exact %.1f%%, within one band %.1f%% (%d parcels)" % (
            band_exact * 100.0 / band_n, band_one * 100.0 / band_n, band_n))
    if own:
        tp = fp = fn = 0
        groups_ref, groups_our = collections.defaultdict(set), collections.defaultdict(set)
        for apn, e in own.items():
            r = ref.get(apn)
            if r and r.get("owner_entity_id") and e:
                groups_ref[r["owner_entity_id"]].add(apn)
                groups_our[e].add(apn)
        ref_of = {a: g for g, s in groups_ref.items() for a in s}
        our_of = {a: g for g, s in groups_our.items() for a in s}
        for g, s in groups_our.items():
            s = sorted(s)
            for i in range(len(s)):
                for j in range(i + 1, min(len(s), i + 60)):
                    if ref_of.get(s[i]) == ref_of.get(s[j]):
                        tp += 1
                    else:
                        fp += 1
        for g, s in groups_ref.items():
            s = sorted(s)
            for i in range(len(s)):
                for j in range(i + 1, min(len(s), i + 60)):
                    if our_of.get(s[i]) != our_of.get(s[j]):
                        fn += 1
        prec = tp / float(tp + fp) if tp + fp else 1.0
        rec = tp / float(tp + fn) if tp + fn else 1.0
        print("ownership pairs vs v9: precision %.3f, recall %.3f" % (prec, rec))
    con.close()
    return 0 if pct >= a.gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
