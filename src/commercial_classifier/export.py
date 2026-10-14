"""Write the deliverables into <work>/out/.

  classified.csv            every input column, in the input's own order, then
                            the cc_* result columns (prefixed so they can never
                            collide with a header the source already had)
  commercial_classified.xlsx Summary, Parcels, Review, Owners, Groups,
                            Crosswalk and QA tabs (needs openpyxl)
  entities.csv, ownership_groups.csv, related_parties.csv
The input is re-read by row number rather than stored twice. If the input
file changed since ingest, the CSV falls back to the canonical columns and
says so.
"""
from __future__ import annotations

import csv
import json
import os

from . import ingest, io_tables, owners, review, schema, store, value

CC_COLS = [
    ("cc_rid", "c.rid"), ("cc_asset_class", "c.asset_class"), ("cc_sub_type", "c.sub_type"),
    ("cc_confidence", "c.confidence"), ("cc_provenance", "c.provenance"), ("cc_method", "c.method"),
    ("cc_reason", "c.reason"), ("cc_commercial_target", "c.commercial_target"), ("cc_flags", "c.flags"),
    ("cc_units", "COALESCE(v.units_final, c.units)"), ("cc_unit_provenance", "COALESCE(v.unit_provenance_final, c.unit_provenance)"),
    ("cc_building_grade", "v.building_grade"), ("cc_value_estimate", "v.value_estimate"),
    ("cc_value_band", "v.value_band"), ("cc_value_basis", "v.value_basis"),
    ("cc_value_confidence", "v.value_confidence"), ("cc_value_per_sf", "v.value_per_sf"),
    ("cc_value_per_unit", "v.value_per_unit"), ("cc_businesses", "p.businesses"),
    ("cc_poi_check", "p.kind"), ("cc_poly_acres", "g.poly_acres"), ("cc_situs_outside_parcel", "g.situs_outside"),
    ("cc_owner_entity_id", "o.entity_id"), ("cc_owner_name", "o.owner_name"), ("cc_owner_type", "o.owner_type"),
    ("cc_owner_match_tier", "o.tier"), ("cc_owner_match_score", "o.match_score"),
    ("cc_owner_portfolio_parcels", "o.portfolio_parcels"), ("cc_owner_portfolio_value", "o.portfolio_value"),
    ("cc_owner_out_of_state", "o.out_of_state"), ("cc_owner_top_class", "o.top_class"),
    ("cc_owner_group_id", "o.group_id"), ("cc_owner_group_parcels", "o.group_parcels"),
    ("cc_owner_group_type", "o.group_type"), ("cc_review_reason", "rv.reason"),
]
OPTIONAL = {"v": "val", "p": "poi", "g": "geo", "o": "own", "rv": "review"}
NAVY, BLUE, GREEN, GOLD, GREY = "0A1130", "316AFF", "1B9E5A", "B7791F", "F4F6FA"
EXCEL_MAX = 1048575


def _results(con):
    have = {a: store.table_exists(con, t) for a, t in OPTIONAL.items()}
    cols = [(name, expr if all(have.get(a, True) for a in _aliases(expr)) else "NULL") for name, expr in CC_COLS]
    joins = " ".join("LEFT JOIN %s %s USING (rid)" % (t, a) for a, t in OPTIONAL.items() if have[a])
    sql = "SELECT r.rid AS _rid, r.src_row AS _src, %s FROM records r JOIN cls c USING (rid) %s" % (
        ", ".join("%s AS %s" % (e, n) for n, e in cols), joins)
    return sql


def _aliases(expr):
    out = []
    for a in OPTIONAL:
        if (a + ".") in expr:
            out.append(a)
    return out


def write_csv(con, work):
    meta = store.get_meta(con, "input") or {}
    path = os.path.join(review.out_dir(work), "classified.csv")
    names = [n for n, _ in CC_COLS]
    res = {}
    for r in con.execute(_results(con)):
        res[r["_src"]] = [r[n] for n in names]
    dups = {r["src_row"]: r["rid"] for r in con.execute("SELECT src_row, rid FROM dups")} \
        if store.table_exists(con, "dups") else {}
    by_rid = None
    src = meta.get("path")
    intact = src and os.path.exists(src) and ingest.file_sha256(src) == meta.get("sha256")
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if intact:
            headers, rows = io_tables.read_table(src, meta.get("sheet"))
            geo_h = meta.get("geometry_header")
            keep = [h for h in headers if h != geo_h]
            w.writerow(keep + names)
            for i, row in enumerate(rows):
                vals = res.get(i)
                if vals is None and i in dups:
                    if by_rid is None:
                        by_rid = {v[0]: v for v in res.values()}
                    vals = by_rid.get(dups[i])
                w.writerow([row.get(h, "") for h in keep] + list(vals or [""] * len(names)))
                n += 1
            note = None
        else:
            canon = [f for f in schema.FIELDS]
            w.writerow(canon + names)
            sql = _results(con).replace("SELECT r.rid AS _rid", "SELECT %s, r.rid AS _rid" %
                                        ", ".join("r.%s AS f_%s" % (c, c) for c in canon))
            for r in con.execute(sql):
                w.writerow([r["f_" + c] for c in canon] + [r[x] for x in names])
                n += 1
            note = "input file moved or changed since ingest; wrote canonical columns instead of the original ones"
    return path, n, note


def _tables_to_csv(con, work):
    paths = {}
    for table, fname, cols in (("entities", "entities.csv", owners.ENT_COLS),
                               ("groups", "ownership_groups.csv", owners.GRP_COLS),
                               ("related", "related_parties.csv", owners.REL_COLS)):
        if not store.table_exists(con, table):
            continue
        p = os.path.join(review.out_dir(work), fname)
        order = {"entities": "ORDER BY parcel_count DESC", "groups": "ORDER BY group_parcels DESC"}.get(table, "")
        io_tables.write_csv(p, cols, (dict(r) for r in con.execute("SELECT * FROM %s %s" % (table, order))))
        paths[table] = p
    return paths


def _xlsx(con, work):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = os.path.join(review.out_dir(work), "commercial_classified.xlsx")
    wb = Workbook()
    hfont = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    hfill = PatternFill("solid", fgColor=NAVY)
    body = Font(name="Arial", size=9)

    def sheet(title, headers, rows, widths=None, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = hfont, hfill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        n = 0
        for r in rows:
            ws.append(r)
            n += 1
        ws.freeze_panes = "A2"
        if n:
            ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(headers)), n + 1)
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, min(max(len(h) + 2, 10), 40))
        return ws, n

    # Summary
    prof = store.get_meta(con, "profile") or {}
    total = con.execute("SELECT COUNT(*) FROM cls").fetchone()[0]
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Commercial classification"
    ws["A1"].font = Font(name="Arial", bold=True, size=14, color=NAVY)
    ws["A2"] = "%s | %d parcels | source: %s" % ((store.get_meta(con, "input") or {}).get("path", ""), total,
                                                prof.get("label", ""))
    ws["A2"].font = Font(name="Arial", size=9, color="555555")
    r0 = 4

    def block(title, pairs, row):
        ws.cell(row=row, column=1, value=title).font = Font(name="Arial", bold=True, size=11, color=NAVY)
        row += 1
        for k, v in pairs:
            ws.cell(row=row, column=1, value=k).font = body
            c = ws.cell(row=row, column=2, value=v)
            c.font = Font(name="Arial", size=9, bold=True)
            c.number_format = "#,##0"
            row += 1
        return row + 1

    by_class = con.execute("SELECT asset_class, COUNT(*) n FROM cls GROUP BY asset_class ORDER BY n DESC").fetchall()
    r0 = block("Parcels by asset class", [(r[0], r[1]) for r in by_class], r0)
    tgt = con.execute("SELECT commercial_target, COUNT(*) n FROM cls GROUP BY 1 ORDER BY n DESC").fetchall()
    r0 = block("Commercial target", [(r[0], r[1]) for r in tgt], r0)
    if store.table_exists(con, "val"):
        bands = dict(con.execute("SELECT value_band, COUNT(*) FROM val WHERE value_band <> '' GROUP BY 1").fetchall())
        r0 = block("Value bands", [(b, bands.get(b, 0)) for b in value.BAND_NAMES], r0)
    rq = con.execute("SELECT COUNT(*) FROM review").fetchone()[0] if store.table_exists(con, "review") else 0
    notes = [("Parcels to review", rq)]
    for p, s in ((p, store.stats_of(con, p)) for p in store.PHASES):
        if isinstance(s, dict) and s.get("skipped"):
            notes.append(("%s skipped" % p, s["skipped"]))
    r0 = block("Read this first", notes, r0)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 90

    # Parcels
    canon = ["apn", "situs_address", "situs_city", "situs_state", "situs_zip", "county", "use_code", "use_desc",
             "structure_type", "building_sqft", "year_built", "lot_acres", "mv_total", "sale_price", "sale_date",
             "owner1"]
    names = [n for n, _ in CC_COLS]
    sql = _results(con).replace("SELECT r.rid AS _rid", "SELECT %s, r.rid AS _rid" %
                                ", ".join("r.%s AS f_%s" % (c, c) for c in canon))
    rows = ([r["f_" + c] for c in canon] + [r[x] for x in names] for r in con.execute(sql + " LIMIT %d" % EXCEL_MAX))
    sheet("Parcels", canon + names, rows)
    if total > EXCEL_MAX:
        ws.cell(row=r0, column=1, value="Parcels tab is truncated at Excel's row limit; classified.csv has all %d" % total)
    # Review
    if store.table_exists(con, "review"):
        sheet("Review", review.REVIEW_COLS,
              ([r[c] for c in review.REVIEW_COLS] for r in con.execute("SELECT * FROM review")))
    if store.table_exists(con, "entities"):
        sheet("Owners", owners.ENT_COLS, ([r[c] for c in owners.ENT_COLS] for r in con.execute(
            "SELECT * FROM entities WHERE parcel_count > 1 ORDER BY parcel_count DESC LIMIT 100000")))
        sheet("Groups", owners.GRP_COLS, ([r[c] for c in owners.GRP_COLS] for r in con.execute(
            "SELECT * FROM groups WHERE member_entities > 1 ORDER BY group_parcels DESC LIMIT 100000")))
    dpath = os.path.join(work, "distinct_codes.csv")
    if os.path.exists(dpath):
        d = io_tables.read_csv_dicts(dpath)
        if d:
            hs = list(d[0].keys())
            sheet("Crosswalk", hs, ([x[h] for h in hs] for x in d))
    qa = review.qa_report(con)
    qrows = []
    for sec in qa["counties"]:
        for k, v in sec.items():
            qrows.append([sec["county_key"], k, json.dumps(v) if isinstance(v, (dict, list)) else v])
    for p, s in qa["phases"].items():
        qrows.append(["(all)", "phase:" + p, json.dumps(s)[:30000]])
    sheet("QA", ["county", "measure", "value"], qrows, {"value": 100})
    tmp = path + ".pending.xlsx"
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except PermissionError:
        return tmp, "the workbook is open in Excel; saved alongside as %s" % os.path.basename(tmp)
    return path, None


def run(work):
    con = store.connect(work)
    store.require(con, "classify")
    csv_path, n, note = write_csv(con, work)
    paths = {"classified_csv": csv_path}
    paths.update(_tables_to_csv(con, work))
    rq = os.path.join(review.out_dir(work), "review_queue.csv")
    if os.path.exists(rq):
        paths["review_queue"] = rq
    notes = [note] if note else []
    if io_tables.has_module("openpyxl"):
        x, xnote = _xlsx(con, work)
        paths["workbook"] = x
        if xnote:
            notes.append(xnote)
    else:
        notes.append("workbook skipped: pip install openpyxl for the .xlsx (the CSVs are complete)")
    stats = {"rows": n, "files": paths, "notes": notes}
    store.mark_done(con, "export", stats)
    con.close()
    return stats

