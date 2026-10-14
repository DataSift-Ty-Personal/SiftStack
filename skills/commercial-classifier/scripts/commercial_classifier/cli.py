"""Command line for the commercial classifier.

    cc run <file>                      everything, resumable; exits 3 when codes need labels
    cc probe-layer <arcgis layer url>  what a county's parcel layer holds
    cc fetch-parcels <url> --out f.csv download a county's parcels
    cc inspect <file>                  which profile and columns it would use, writes nothing
    cc distinct --work W               the distinct use codes to label
    cc label labels.csv --work W       merge labels into the learned crosswalk
    cc apply-review reviewed.csv --work W   corrections from the review queue -> overrides
    cc status --work W
Single phases: ingest, geometry, classify, fetch-places, poi, value, owners, review, export.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import (VERSION, arcgis, classify, export, geo, ingest, io_tables, learn, owners, places, poi, profiles,
               review, store, value)

EXIT_NEEDS_LABELS = 3


def _p(obj):
    print(json.dumps(obj, indent=1, default=str))


def _maps(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit("--map expects field=Header, got %r" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _work(a):
    if getattr(a, "work", None):
        return a.work
    if getattr(a, "file", None):
        return store.default_workdir(a.file)
    raise SystemExit("--work <dir> is required")


def cmd_inspect(a):
    headers, rows, name, score, scores, mapping = ingest.plan(a.file, a.profile, _maps(a.map), a.sheet)
    sample = []
    for i, r in enumerate(rows):
        sample.append(r)
        if i >= 199:
            break
    signals = {}
    for f in ("use_code", "use_desc", "structure_type", "vendor_type", "county_use_code", "property_class",
              "zoning", "business_name"):
        if f in mapping:
            vals = {}
            for r in sample:
                v = str(profiles.read_value(r, mapping[f]) or "").strip()
                if v:
                    vals[v] = vals.get(v, 0) + 1
            signals[f] = sorted(vals.items(), key=lambda kv: -kv[1])[:8]
    used = {s for s in mapping.values() if isinstance(s, str)}
    _p({"file": a.file, "profile": name, "profile_label": profiles.PROFILES[name].label,
        "fingerprint_scores": {k: round(v, 2) for k, v in scores.items()},
        "mapped": {k: profiles.header_of(v) for k, v in mapping.items()},
        "class_signals_missing": [f for f in ("use_code", "use_desc", "structure_type", "vendor_type", "zoning")
                                  if f not in mapping],
        "headers_not_used": [h for h in headers if h not in used][:80],
        "sample_class_signals": signals})
    try:
        profiles.check_mapping(mapping, headers)
    except profiles.ProfileError as e:
        print("\nWARNING: %s" % e)
        return 1
    return 0


def _needs_labels(con, accept):
    d = store.get_meta(con, "distinct") or {}
    if not d.get("needs_label"):
        return None
    if accept or store.get_meta(con, "accept_keywords") or store.get_meta(con, "labels"):
        return None
    return d


def cmd_run(a):
    work = _work(a)
    con = store.connect(work)
    meta = store.get_meta(con, "input") or {}
    src = os.path.abspath(a.file)
    changed = (meta.get("path") != src) or (meta.get("sha256") != ingest.file_sha256(src))
    if a.force or changed or not store.done(con, "ingest"):
        con.close()
        print("ingest:", json.dumps(ingest.run(a.file, work, a.profile, _maps(a.map), a.sheet, a.state, a.county_key)))
        con = store.connect(work)
    if a.accept_keywords:
        store.set_meta(con, "accept_keywords", True)
    if not store.done(con, "geometry"):
        print("geometry:", json.dumps(geo.run(work)))
    if not store.done(con, "classify"):
        d = learn.distinct(work, a.crosswalk)
        print("distinct: %d values, %d need a label (%d parcels)" % (
            d["distinct_values"], d["needs_label"], d["parcels_behind_needs_label"]))
        pending = _needs_labels(con, a.accept_keywords)
        if pending:
            print("\nLABELS NEEDED: %d distinct use values have no table entry and only a weak keyword guess.\n"
                  "  1. open %s and fill asset_class / sub_type on the rows marked needs_label=Y\n"
                  "  2. cc label %s --work %s\n"
                  "  3. cc run %s --work %s\n"
                  "Or re-run with --accept-keywords to keep the keyword guesses (they will be low confidence)."
                  % (pending["needs_label"], pending["path"], pending["path"], work, a.file, work))
            con.close()
            return EXIT_NEEDS_LABELS
        print("classify:", json.dumps(classify.run(work, a.crosswalk))[:600])
    if a.through == "classify":
        return 0
    if not store.done(con, "places"):
        if a.no_evidence:
            store.mark_done(con, "places", {"skipped": "--no-evidence"})
            store.mark_done(con, "poi", {"skipped": "--no-evidence"})
        else:
            print("fetch-places:", json.dumps(places.run(work, a.source, a.release, a.places)))
    if not store.done(con, "poi"):
        print("poi:", json.dumps(poi.run(work)))
    if a.through == "poi":
        return 0
    if not store.done(con, "value"):
        print("value:", json.dumps(value.run(work, a.recent_since)))
    if a.through == "value":
        return 0
    if not store.done(con, "owners"):
        print("owners:", json.dumps(owners.run(work)))
    if a.through == "owners":
        return 0
    if not store.done(con, "review"):
        print("review:", json.dumps(review.run(work)))
    if not store.done(con, "export"):
        print("export:", json.dumps(export.run(work), indent=1))
    con.close()
    return 0


def cmd_status(a):
    con = store.connect(_work(a))
    out = {"profile": (store.get_meta(con, "profile") or {}).get("name"),
           "distinct": store.get_meta(con, "distinct"), "labels": store.get_meta(con, "labels")}
    for p in store.PHASES:
        m = store.get_meta(con, "phase:" + p)
        out[p] = m if m else "not run"
    _p(out)
    con.close()
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="cc", description="Commercial property classifier %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd")

    def common_in(p):
        p.add_argument("--profile", choices=sorted(profiles.PROFILES), help="force a source profile")
        p.add_argument("--map", action="append", metavar="field=Header", help="point a field at a header")
        p.add_argument("--sheet", help="worksheet name for an .xlsx input")

    p = sub.add_parser("run", help="run every phase, resuming")
    p.add_argument("file")
    p.add_argument("--work")
    common_in(p)
    p.add_argument("--state", help="two-letter situs state when the file has none")
    p.add_argument("--county-key", help="county label when the file has no county or FIPS column")
    p.add_argument("--through", choices=["classify", "poi", "value", "owners", "export"], default="export")
    p.add_argument("--accept-keywords", action="store_true", help="skip labelling; keep keyword guesses")
    p.add_argument("--no-evidence", action="store_true", help="skip the business-points pass")
    p.add_argument("--source", choices=["auto", "overture", "osm"], default="auto")
    p.add_argument("--release", help="Overture release to use (default: newest)")
    p.add_argument("--places", help="your own business-points CSV (name, category, lat, lon)")
    p.add_argument("--recent-since", help="YYYY-MM-DD: sales on or after count as recent")
    p.add_argument("--crosswalk", action="append", default=[], help="extra crosswalk CSV to load")
    p.add_argument("--force", action="store_true", help="re-ingest even if the file is unchanged")

    p = sub.add_parser("inspect", help="show the detected profile and mapping; writes nothing")
    p.add_argument("file")
    common_in(p)

    p = sub.add_parser("ingest")
    p.add_argument("file")
    p.add_argument("--work")
    common_in(p)
    p.add_argument("--state")
    p.add_argument("--county-key")

    for name in ("geometry", "distinct", "classify", "poi", "value", "owners", "review", "export", "status"):
        p = sub.add_parser(name)
        p.add_argument("--work", required=True)
        if name in ("distinct", "classify"):
            p.add_argument("--crosswalk", action="append", default=[])
        if name == "value":
            p.add_argument("--recent-since")
        if name == "poi":
            p.add_argument("--radius", type=float, default=40.0)

    p = sub.add_parser("fetch-places")
    p.add_argument("--work", required=True)
    p.add_argument("--source", choices=["auto", "overture", "osm"], default="auto")
    p.add_argument("--release")
    p.add_argument("--places")

    p = sub.add_parser("label", help="merge labels (a filled distinct_codes.csv) into the learned crosswalk")
    p.add_argument("labels")
    p.add_argument("--work", required=True)
    p.add_argument("--source", choices=["claude", "human"], default="claude")

    p = sub.add_parser("apply-review", help="turn reviewed queue rows into overrides")
    p.add_argument("reviewed")
    p.add_argument("--work", required=True)

    p = sub.add_parser("probe-layer", help="describe an ArcGIS parcel layer")
    p.add_argument("url")

    p = sub.add_parser("fetch-parcels", help="download an ArcGIS parcel layer to CSV")
    p.add_argument("url")
    p.add_argument("--out", required=True)
    p.add_argument("--where", default="1=1")
    p.add_argument("--fields", default="*")
    p.add_argument("--geometry", choices=["polygon", "centroid", "none"], default="polygon")
    p.add_argument("--limit", type=int)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "inspect":
            return cmd_inspect(a)
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "ingest":
            _p(ingest.run(a.file, _work(a), a.profile, _maps(a.map), a.sheet, a.state, a.county_key))
        elif a.cmd == "geometry":
            _p(geo.run(a.work))
        elif a.cmd == "distinct":
            _p(learn.distinct(a.work, a.crosswalk))
        elif a.cmd == "classify":
            _p(classify.run(a.work, a.crosswalk))
        elif a.cmd == "fetch-places":
            _p(places.run(a.work, a.source, a.release, a.places))
        elif a.cmd == "poi":
            _p(poi.run(a.work, a.radius))
        elif a.cmd == "value":
            _p(value.run(a.work, a.recent_since))
        elif a.cmd == "owners":
            _p(owners.run(a.work))
        elif a.cmd == "review":
            _p(review.run(a.work))
        elif a.cmd == "export":
            _p(export.run(a.work))
        elif a.cmd == "label":
            ok, bad = learn.label(a.work, a.labels, a.source)
            print("accepted %d labels into %s" % (len(ok), os.path.join(a.work, "crosswalks", "learned.csv")))
            for line, why in bad:
                print("  rejected row %d: %s" % (line, why))
            return 1 if bad else 0
        elif a.cmd == "apply-review":
            ok, bad = review.apply(a.work, a.reviewed)
            print("wrote %d overrides; classify will re-run on the next `cc run`" % len(ok))
            for line, why in bad:
                print("  skipped row %d: %s" % (line, why))
            return 1 if bad else 0
        elif a.cmd == "probe-layer":
            info = arcgis.probe(a.url)
            info["fields"] = ["%s%s [%s]" % (n, "" if al == n else " (%s)" % al, t.replace("esriFieldType", ""))
                              for n, al, t in info["fields"]]
            _p(info)
        elif a.cmd == "fetch-parcels":
            _p(arcgis.fetch(a.url, a.out, a.where, a.fields, a.geometry, limit=a.limit))
        return 0
    except (profiles.ProfileError, io_tables.InputError, arcgis.LayerError, RuntimeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
