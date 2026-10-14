"""Tests for src/commercial_classifier: zero network, fictional data only.

    python tests/test_commercial_classifier.py      (or pytest)

Every name and address here is made up. The live checks (Knox regression,
Overture, a real county layer) live in tools/regress_commercial_knox.py and
run by hand, never in CI.
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from commercial_classifier import (arcgis, cli, crosswalk, geo, keywords, learn, owners, places,  # noqa: E402
                                   profiles, schema, taxonomy, value)

FA_HEADERS = ["apn", "apnseqnbr", "fips", "situsfullstreetaddress", "situscity", "situsstate", "situszip5",
              "situslatitude", "situslongitude", "landusecode", "countylandusecode", "propertyclassid", "zoning",
              "legaldescription", "subdivisionname", "sumbuildingsqft", "storiesnbrcode", "yearbuilt",
              "lotsizeacres", "sumresidentialunits", "sumcommercialunits", "buildingqualitycode",
              "buildingconditioncode", "markettotalvalue", "marketvalueland", "marketvalueimprovement",
              "assdtotalvalue", "currentsalesprice", "currentsalerecordingdate", "ownername1full", "ownername2full",
              "owner1lastname", "owner1corpind", "mailingfullstreetaddress", "mailingcity", "mailingstate",
              "mailingzip5"]


def _tmp():
    return tempfile.mkdtemp(prefix="cc_test_")


def _write(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in headers})
    return path


def _fa_row(apn, code, sf, impr=150000, owner="MAPLE EXAMPLE HOLDINGS LLC", **kw):
    n = int(apn.split("-")[-1])
    r = {"apn": apn, "apnseqnbr": "1", "fips": "47093", "situsfullstreetaddress": "%d SAMPLE ST" % (100 + n),
         "situscity": "TESTVILLE", "situsstate": "TN", "situszip5": "37999",
         "situslatitude": "%.6f" % (35.9 + n * 0.0005), "situslongitude": "%.6f" % (-83.9 - n * 0.0005),
         "landusecode": code, "sumbuildingsqft": sf, "markettotalvalue": 400000, "marketvalueland": 100000,
         "marketvalueimprovement": impr, "assdtotalvalue": 160000, "ownername1full": owner,
         "owner1lastname": owner.split()[0], "mailingfullstreetaddress": "1 EXAMPLE PLAZA",
         "mailingcity": "TESTVILLE", "mailingstate": "TN", "mailingzip5": "37999", "yearbuilt": 1990}
    r.update(kw)
    return r


def _fa_fixture():
    rows = [
        _fa_row("T-1", "2008", 25000),          # strip center by size
        _fa_row("T-2", "2008", 29999),
        _fa_row("T-3", "2009", 30000),          # neighborhood
        _fa_row("T-4", "2009", 99999),
        _fa_row("T-5", "2008", 100000),         # community
        _fa_row("T-6", "2008", 349999),
        _fa_row("T-7", "2008", 350000),         # stays as coded
        _fa_row("T-8", "2018", 3000, owner="ACME FUEL MART LLC"),        # convenience + fuel brand
        _fa_row("T-9", "2004", 0, impr=0),       # no improvement -> land
        _fa_row("T-10", "8000", 0, impr=0, countylandusecode="391"),     # Knox county code -> commercial land
        _fa_row("T-11", "5003", 40000, storiesnbrcode="400"),          # sourced industrial, 4 stories -> flag
        _fa_row("T-12", "1101", 1800, sumresidentialunits="1"),         # duplex code implies 2 units
    ]
    for i in range(10):                          # ten "casinos" in one county = the condo miscode
        rows.append(_fa_row("C-%d" % (20 + i), "2038", 1200, subdivisionname="OAK MEDICAL CONDOS" if i == 0 else "",
                            legaldescription="UNIT %d" % i))
    rows.append(_fa_row("S-40", "2001", 5000, currentsalesprice=500000, currentsalerecordingdate="2025-03-01"))
    return rows


def _run(argv):
    return cli.main(argv)


def _cls(work):
    con = sqlite3.connect(os.path.join(work, "cc.sqlite"))
    con.row_factory = sqlite3.Row
    out = {r["apn"]: dict(r) for r in con.execute(
        "SELECT r.apn, c.* FROM records r JOIN cls c USING (rid)")}
    con.close()
    return out


# ---- taxonomy and parsing ---------------------------------------------------
def test_taxonomy_validate():
    assert taxonomy.validate("sports and entertainment", "golf/driving range") == \
        ("Sports & Entertainment", "Golf / Driving Range")
    assert taxonomy.validate("industrial", "") == ("Industrial", "")
    for bad in (("Industrial", "Hotel"), ("Spaceport", "")):
        try:
            taxonomy.validate(*bad)
        except taxonomy.TaxonomyError:
            continue
        raise AssertionError("accepted %r" % (bad,))
    assert sum(len(v) for k, v in taxonomy.CLASSES.items() if k in taxonomy.LISTING_CLASSES) == 76


def test_missing_is_none_never_zero():
    assert schema.num("") is None and schema.num(None) is None and schema.num("N/A") is None
    assert schema.num("$1,234.50") == 1234.5 and schema.num("(500)") == -500
    assert schema.parse_date("2023-08-31") == "2023-08-31"
    assert schema.parse_date("20230831") == "2023-08-31"
    assert schema.parse_date("8/31/2023") == "2023-08-31"
    assert schema.parse_date(1693440000000) == "2023-08-31"   # ArcGIS epoch ms
    assert schema.parse_date("garbage") is None


# ---- profiles ----------------------------------------------------------------
def test_profile_detection():
    assert profiles.detect(FA_HEADERS)[0] == "fa"
    ds = ["First Name", "Last Name", "Mailing address", "Property address", "Property city", "Structure type",
          "Building use code", "Land zoning", "Storeys", "Apn", "exported from REISift.io", "List Stack"]
    assert profiles.detect(ds)[0] == "datasift"
    rg = ["parcelnumb", "usecode", "usedesc", "lbcs_activity", "lbcs_function", "mailadd", "ll_gisacre", "ll_uuid",
          "szip", "owner"]
    assert profiles.detect(rg)[0] == "regrid"
    assert profiles.detect(["Parcel ID", "Land Use", "Owner"])[0] == "generic"


def test_mapping_override_and_hard_error():
    m = profiles.build_mapping("generic", ["PIN", "Weird Use Col", "Owner"], {"use_desc": "weird use col"})
    assert m["use_desc"] == "Weird Use Col" and m["apn"] == "PIN"
    try:
        profiles.check_mapping(profiles.build_mapping("generic", ["PIN", "Owner"]), ["PIN", "Owner"])
    except profiles.ProfileError as e:
        assert "Headers seen: PIN, Owner" in str(e)
    else:
        raise AssertionError("a file with no class signal must stop")
    try:
        profiles.build_mapping("generic", ["PIN"], {"use_desc": "Nope"})
    except profiles.ProfileError as e:
        assert "no such header" in str(e)
    else:
        raise AssertionError("bad --map header must stop")


# ---- keywords ----------------------------------------------------------------
def test_keywords_whole_words_and_order():
    assert keywords.use("OFFICE/WAREHOUSE")[0].sub_type == "Flex"
    assert keywords.use("VACANT COMMERCIAL LAND")[0][:2] == ("Land", "Commercial")
    assert keywords.use("COMMERCIAL CONDOMINIUM")[0].asset_class != "Residential"
    assert keywords.use("single_family_residence")[0].asset_class == "Residential"
    assert keywords.use("MINI WAREHOUSE")[0].sub_type == "Self-Storage"
    assert keywords.name("WILLBANKS HOLDINGS")[0] is None or keywords.name("WILLBANKS HOLDINGS")[0].sub_type != "Bank"
    assert keywords.name("McDonald's")[0].asset_class == "Restaurant"
    assert keywords.name("Casey's General Store")[0].sub_type == "Service/Gas Station"
    assert keywords.zoning("M-1").asset_class == "Industrial"
    assert keywords.zoning("C-2").asset_class == "Retail"


# ---- shipped data hygiene ----------------------------------------------------
def test_fa_tables_carry_no_vendor_text():
    data = os.path.join(ROOT, "src", "commercial_classifier", "data")
    with open(os.path.join(data, "xw_fa.csv"), encoding="utf-8") as f:
        hdr = next(csv.reader(f))
    assert "desc" not in hdr
    with open(os.path.join(data, "fa_desc_index.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and all(len(r["desc_sha1_12"]) == 12 and all(c in "0123456789abcdef" for c in r["desc_sha1_12"])
                        for r in rows)
    xw = crosswalk.Crosswalks()
    lab = xw.table_label(crosswalk.Key("structure_type", "*", "", crosswalk.desc_key("Condominium Unit (Residential)")))
    assert lab and lab.asset_class == "Residential" and lab.method == "tier0-xw:fa-desc"


def test_package_text_hygiene():
    pkg = os.path.join(ROOT, "src", "commercial_classifier")
    bad = []
    for dp, _, files in os.walk(pkg):
        for fn in files:
            if fn.endswith((".py", ".csv", ".txt")):
                b = open(os.path.join(dp, fn), "rb").read()
                if bytes([8]) in b:
                    bad.append(fn + ": backspace byte")
                t = b.decode("utf-8", "replace")
                if "—" in t or "–" in t:
                    bad.append(fn + ": dash character")
    assert not bad, bad


# ---- end to end on an FA-shaped file -----------------------------------------
def test_fa_rules_end_to_end():
    d = _tmp()
    src = _write(os.path.join(d, "fa.csv"), FA_HEADERS, _fa_fixture())
    work = os.path.join(d, "w")
    assert _run(["run", src, "--work", work, "--no-evidence", "--accept-keywords"]) == 0
    c = _cls(work)
    assert c["T-1"]["sub_type"] == "Strip Center" and c["T-2"]["sub_type"] == "Strip Center"
    assert c["T-3"]["sub_type"] == "Neighborhood Center" and c["T-4"]["sub_type"] == "Neighborhood Center"
    assert c["T-5"]["sub_type"] == "Community Center" and c["T-6"]["sub_type"] == "Community Center"
    assert c["T-7"]["sub_type"] == "Community Center" and c["T-7"]["method"] == "tier0-xw:fa"
    assert c["T-8"]["sub_type"] == "Service/Gas Station"
    assert (c["T-9"]["asset_class"], c["T-9"]["sub_type"]) == ("Land", "Commercial")
    assert (c["T-10"]["asset_class"], c["T-10"]["sub_type"]) == ("Land", "Commercial")
    assert c["T-11"]["asset_class"] == "Industrial" and "story" in c["T-11"]["flags"]
    assert c["T-12"]["asset_class"] == "Residential Income" and c["T-12"]["units"] == 2
    assert c["C-20"]["sub_type"] == "Medical" and c["C-21"]["method"] == "tier1-condo-default"
    out = os.path.join(work, "out", "classified.csv")
    with open(out, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(_fa_fixture())
    hdr = list(rows[0].keys())
    assert hdr[:len(FA_HEADERS)] == FA_HEADERS and hdr[len(FA_HEADERS)] == "cc_rid"
    qa = json.load(open(os.path.join(work, "out", "qa_report.json"), encoding="utf-8"))
    assert qa["skipped"]["places"] == "--no-evidence"
    shutil.rmtree(d, ignore_errors=True)


def test_condo_rule_gated_on_volume():
    d = _tmp()
    rows = [_fa_row("C-1", "2038", 1200), _fa_row("T-2", "2001", 3000)]
    src = _write(os.path.join(d, "fa.csv"), FA_HEADERS, rows)
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--through", "classify", "--accept-keywords"])
    assert _cls(work)["C-1"]["sub_type"] == "Casino"     # one real casino stays a casino
    shutil.rmtree(d, ignore_errors=True)


def test_shuffled_input_gives_identical_results():
    d = _tmp()
    rows = _fa_fixture()
    a = _write(os.path.join(d, "a.csv"), FA_HEADERS, rows)
    random.Random(7).shuffle(rows)
    b = _write(os.path.join(d, "b.csv"), FA_HEADERS, rows)
    for src, w in ((a, "wa"), (b, "wb")):
        assert _run(["run", src, "--work", os.path.join(d, w), "--no-evidence", "--accept-keywords"]) == 0

    def snap(w):
        con = sqlite3.connect(os.path.join(d, w, "cc.sqlite"))
        out = {r[0]: r[1:] for r in con.execute(
            "SELECT c.rid, c.asset_class, c.sub_type, v.value_band, o.entity_id FROM cls c "
            "JOIN val v USING (rid) JOIN own o USING (rid)")}
        con.close()
        return out
    assert snap("wa") == snap("wb")
    shutil.rmtree(d, ignore_errors=True)


def test_missing_improvement_columns_never_mean_vacant():
    d = _tmp()
    rows = [{"Parcel ID": "P-%d" % i, "Land Use": "RETAIL STORE", "Owner": "BIRCH SAMPLE LLC"} for i in range(5)]
    src = _write(os.path.join(d, "g.csv"), ["Parcel ID", "Land Use", "Owner"], rows)
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--through", "classify", "--accept-keywords", "--county-key", "TEST"])
    assert {r["asset_class"] for r in _cls(work).values()} == {"Retail"}
    shutil.rmtree(d, ignore_errors=True)


def test_datasift_export_resolves_through_fa_index():
    d = _tmp()
    hdr = ["First Name", "Last Name", "Mailing address", "Mailing city", "Mailing state", "Mailing zip5",
           "Property address", "Property city", "Property state", "Property zip5", "Property county", "Business Name",
           "Structure type", "Building use code", "Land zoning", "Storeys", "Sqft", "Apn", "Number of units",
           "Estimated value", "Last sale price", "Last sold", "exported from REISift.io"]
    rows = [{"Apn": "D-1", "Structure type": "Condominium Unit (Residential)", "Number of units": "72",
             "Building use code": "Residential", "Property county": "Example", "Property state": "TN",
             "First Name": "Pat", "Last Name": "Example", "Estimated value": "250000"},
            {"Apn": "D-2", "Structure type": "single family residential", "Number of units": "4",
             "Property county": "Example", "Property state": "TN", "Business Name": "ELM SAMPLE LLC"}]
    src = _write(os.path.join(d, "ds.csv"), hdr, rows)
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--no-evidence", "--accept-keywords"])
    c = _cls(work)
    assert c["D-1"]["method"] == "tier0-xw:fa-desc" and c["D-1"]["asset_class"] == "Residential"  # a condo unit stays one
    assert c["D-2"]["asset_class"] == "Residential Income"                                       # SFR with 4 units
    con = sqlite3.connect(os.path.join(work, "cc.sqlite"))
    assert con.execute("SELECT owner1 FROM records WHERE apn='D-2'").fetchone()[0] == "ELM SAMPLE LLC"
    assert con.execute("SELECT owner1 FROM records WHERE apn='D-1'").fetchone()[0] == "Pat Example"
    con.close()
    shutil.rmtree(d, ignore_errors=True)


def test_learned_labels_validated_and_kept_in_their_county():
    d = _tmp()
    rows = []
    for county in ("Alpha", "Beta"):
        for i in range(30):
            rows.append({"Parcel ID": "%s-%d" % (county, i), "County": county, "State": "OH", "LUC": "447",
                         "Owner": "ASPEN SAMPLE %d LLC" % i})
    src = _write(os.path.join(d, "g.csv"), ["Parcel ID", "County", "State", "LUC", "Owner"], rows)
    work = os.path.join(d, "w")
    assert _run(["run", src, "--work", work]) == cli.EXIT_NEEDS_LABELS
    dist = list(csv.DictReader(open(os.path.join(work, "distinct_codes.csv"), encoding="utf-8")))
    assert len(dist) == 2 and all(r["needs_label"] == "Y" for r in dist)
    labels = os.path.join(d, "labels.csv")
    alpha = [r for r in dist if r["county_key"] == "OH:ALPHA"][0]
    alpha.update({"asset_class": "Industrial", "sub_type": "Warehouse"})
    bad = dict(alpha, asset_class="Spaceport", county_key="OH:BETA")
    _write(labels, list(alpha.keys()), [alpha, bad])
    assert _run(["label", labels, "--work", work]) == 1          # one row rejected, said out loud
    learned = list(csv.DictReader(open(os.path.join(work, "crosswalks", "learned.csv"), encoding="utf-8")))
    assert len(learned) == 1 and float(learned[0]["confidence"]) == 0.75
    assert _run(["run", src, "--work", work, "--through", "classify"]) == 0
    c = _cls(work)
    assert c["Alpha-0"]["asset_class"] == "Industrial" and c["Alpha-0"]["provenance"] == "learned-claude"
    assert c["Beta-0"]["asset_class"] == "Other"                  # Alpha's label never leaks into Beta
    shutil.rmtree(d, ignore_errors=True)


# ---- geometry ------------------------------------------------------------------
SQUARE_WITH_HOLE = ("POLYGON ((-84.0 36.0, -83.99 36.0, -83.99 36.01, -84.0 36.01, -84.0 36.0), "
                    "(-83.996 36.004, -83.994 36.004, -83.994 36.006, -83.996 36.006, -83.996 36.004))")


def test_geometry_parse_contains_and_area():
    p = geo.parse(SQUARE_WITH_HOLE)
    assert geo.contains(p, -83.999, 36.001) and not geo.contains(p, -83.995, 36.005)
    ac = geo.acres(p)
    assert 230 < ac < 245, ac           # 0.9 km x 1.11 km = 247.7 acres, minus a 9.9-acre hole
    esri = json.dumps({"rings": [[[-84.0, 36.0], [-84.0, 36.01], [-83.99, 36.01], [-83.99, 36.0], [-84.0, 36.0]]]})
    assert geo.contains(geo.parse(esri), -83.995, 36.005)
    mp = geo.parse("MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((5 5, 6 5, 6 6, 5 5)))")
    assert len(mp) == 2
    try:
        import shapely
        g = shapely.from_wkt(SQUARE_WITH_HOLE)
        for x, y in ((-83.999, 36.001), (-83.995, 36.005), (-83.98, 36.0)):
            assert g.contains(shapely.Point(x, y)) == geo.contains(p, x, y)
    except ImportError:
        pass


def test_bbox_trims_outliers():
    pts = [(36.0 + i * 1e-4, -84.0 - i * 1e-4) for i in range(400)] + [(0.1, 0.1)]
    s, w, n, e = geo.bbox_from_points(pts)
    assert s > 35.9 and e < -83.9


# ---- business points --------------------------------------------------------------
def _poi_case(d, with_polygons):
    hdr = ["Parcel ID", "Land Use", "Owner", "Latitude", "Longitude", "WKT", "Assessed Value"]
    rows = [{"Parcel ID": "G-1", "Land Use": "COMMERCIAL", "Owner": "CEDAR SAMPLE LLC", "Latitude": "36.005",
             "Longitude": "-83.995", "WKT": "POLYGON ((-84.0 36.0, -83.99 36.0, -83.99 36.01, -84.0 36.01, -84.0 36.0))"
             if with_polygons else ""},
            {"Parcel ID": "G-2", "Land Use": "WAREHOUSE", "Owner": "SPRUCE SAMPLE LLC", "Latitude": "36.105",
             "Longitude": "-83.895", "WKT": "POLYGON ((-83.9 36.1, -83.89 36.1, -83.89 36.11, -83.9 36.11, -83.9 36.1))"
             if with_polygons else ""},
            {"Parcel ID": "G-3", "Land Use": "Condominium Unit (Residential)", "Owner": "PINE SAMPLE",
             "Latitude": "36.205", "Longitude": "-83.795",
             "WKT": "POLYGON ((-83.8 36.2, -83.79 36.2, -83.79 36.21, -83.8 36.21, -83.8 36.2))" if with_polygons else ""}]
    src = _write(os.path.join(d, "g.csv"), hdr, rows)
    pl = _write(os.path.join(d, "places.csv"), ["name", "category", "lat", "lon", "confidence"],
                [{"name": "Example Fuel", "category": "gas_station", "lat": 36.0051, "lon": -83.9951, "confidence": 0.9},
                 {"name": "Example Inn", "category": "hotel", "lat": 36.1051, "lon": -83.8951, "confidence": 0.9},
                 {"name": "Sample Suites", "category": "hotel", "lat": 36.2051, "lon": -83.7951, "confidence": 0.9}])
    work = os.path.join(d, "w")
    assert _run(["run", src, "--work", work, "--places", pl, "--accept-keywords", "--county-key", "TEST"]) == 0
    return _cls(work)


def test_poi_corrects_weak_labels_inside_polygons_only():
    d = _tmp()
    c = _poi_case(d, True)
    # generic "commercial" (0.35) + a gas station inside: same class, so it confirms and fills the sub-type
    assert (c["G-1"]["asset_class"], c["G-1"]["sub_type"]) == ("Retail", "Service/Gas Station")
    assert c["G-1"]["method"].endswith("+poi-confirm") and c["G-1"]["confidence"] == 0.85
    # a weak keyword warehouse (0.6) with a hotel inside the polygon is corrected
    assert (c["G-2"]["asset_class"], c["G-2"]["provenance"]) == ("Hospitality", "imputed")
    # a label from a source table is never auto-flipped, only queued
    assert c["G-3"]["asset_class"] == "Residential" and c["G-3"]["provenance"] == "sourced"
    rq = list(csv.DictReader(open(os.path.join(d, "w", "out", "review_queue.csv"), encoding="utf-8")))
    assert any(r["apn"] == "G-3" and r["suggestion"] == "Hospitality/Hotel" for r in rq)
    shutil.rmtree(d, ignore_errors=True)
    d = _tmp()
    c = _poi_case(d, False)
    assert c["G-2"]["asset_class"] == "Industrial"     # a point match near the parcel never corrects
    rq = list(csv.DictReader(open(os.path.join(d, "w", "out", "review_queue.csv"), encoding="utf-8")))
    assert any(r["apn"] == "G-2" and "Hotel" in r["suggestion"] for r in rq)
    shutil.rmtree(d, ignore_errors=True)


def test_place_category_hierarchy():
    cmap = __import__("commercial_classifier.poi", fromlist=["x"]).category_map()
    poi = importlib.import_module("commercial_classifier.poi")
    assert poi.expect(cmap, "baptist_place_of_worship")[1] == "Religious Facility"
    assert poi.expect(cmap, "some_new_thing", "travel_and_transportation>fueling_station>some_new_thing")[1] == \
        "Service/Gas Station"


# ---- value ------------------------------------------------------------------------
def test_value_bands_and_calibration():
    assert value.band(99999) == "<$100K" and value.band(100000) == "$100K-$250K"
    assert value.band(10_000_000) == "$10M+"
    d = _tmp()
    rows = [_fa_row("V-%d" % i, "2001", 5000, markettotalvalue=200000 + i * 1000,
                    currentsalesprice=int((200000 + i * 1000) * 1.5), currentsalerecordingdate="2025-0%d-01" % (1 + i % 9))
            for i in range(40)]
    src = _write(os.path.join(d, "fa.csv"), FA_HEADERS, rows)
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--no-evidence", "--accept-keywords", "--through", "value"])
    con = sqlite3.connect(os.path.join(work, "cc.sqlite"))
    stats = json.loads(con.execute("SELECT v FROM meta WHERE k='phase:value'").fetchone()[0])["stats"]
    assert abs(stats["global_factor"] - 1.5) < 0.01 and stats["holdout"]["sales_scored"] > 0
    con.close()
    shutil.rmtree(d, ignore_errors=True)


def test_assessed_only_without_sales_is_not_priced():
    d = _tmp()
    rows = [{"Parcel ID": "A-%d" % i, "Land Use": "OFFICE BUILDING", "Assessed Value": 50000, "Owner": "X%d LLC" % i}
            for i in range(5)]
    src = _write(os.path.join(d, "g.csv"), ["Parcel ID", "Land Use", "Assessed Value", "Owner"], rows)
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--no-evidence", "--accept-keywords", "--through", "value", "--county-key", "T"])
    con = sqlite3.connect(os.path.join(work, "cc.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM val WHERE value_band <> ''").fetchone()[0] == 0
    con.close()
    shutil.rmtree(d, ignore_errors=True)


# ---- owners -------------------------------------------------------------------------
def _p(rid, owner, mail, ms="TN", ss="TN", co=()):
    street, z = mail
    return {"rid": rid, "apn_or_rid": rid, "owner": owner, "co": list(co), "mail_base": "%s|%s" % (street, z),
            "mail_state": ms, "situs_state": ss, "corp": False, "asset_class": "Retail", "value": 100000, "units": 0}


def test_owner_tiers_and_aggregators():
    ps = [_p("a1", "MAPLE EXAMPLE HOLDINGS LLC", ("1 EXAMPLE PLAZA", "37999")),
          _p("a2", "MAPLE EXAMPLE HOLDINGS LLC", ("1 EXAMPLE PLAZA", "37999")),
          _p("b1", "SOLO SAMPLE LLC", ("9 LONE RD", "37999")),
          _p("c1", "OAK EXAMPLE LLC", ("2 MAIN ST", "43001"), ms="OH", ss="OH"),
          _p("c2", "OAK EXAMPLE LLC", ("2 MAIN ST", "43001"), ms="OH", ss="TN")]
    for i, nm in enumerate(["ALDER ONE LLC", "BIRCH TWO INC", "CEDAR THREE LP", "DOGWOOD FOUR CO", "ELM FIVE LLC"]):
        ps.append(_p("s%d" % i, nm, ("PO BOX 100", "10001")))   # a servicer box behind five unrelated owners
    ents, pe, rel, groups, gmap = owners.resolve(ps)
    assert pe["a1"]["entity_id"] == pe["a2"]["entity_id"] and pe["a1"]["tier"] == "green"
    assert pe["b1"]["tier"] == "single"
    assert pe["c1"]["out_of_state"] == "mixed"
    assert len({pe["s%d" % i]["entity_id"] for i in range(5)}) == 5
    assert not any(kind == "shared-address" and "PO BOX 100" in ev for _, _, kind, ev in rel)


def test_two_people_with_one_common_name_stay_apart():
    ps = []
    for i in range(30):   # make SMITH and JOHN common tokens in this county
        ps.append(_p("f%d" % i, "SMITH FILLER%d" % i, ("%d A ST" % i, "37999")))
        ps.append(_p("g%d" % i, "JOHN FILLER%d" % i, ("%d B ST" % i, "37999")))
    ps += [_p("j1", "SMITH JOHN", ("10 FIRST ST", "37999")), _p("j2", "SMITH JOHN", ("99 OTHER RD", "38888"))]
    _, pe, _, _, _ = owners.resolve(ps)
    assert pe["j1"]["entity_id"] != pe["j2"]["entity_id"]


# ---- ArcGIS downloader (stubbed) --------------------------------------------------------
def test_arcgis_pager_writes_rows_and_wkt():
    meta = {"name": "Parcels", "geometryType": "esriGeometryPolygon", "maxRecordCount": 2,
            "advancedQueryCapabilities": {"supportsPagination": True}, "objectIdField": "OBJECTID",
            "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                       {"name": "PIN", "alias": "Parcel Number", "type": "esriFieldTypeString"},
                       {"name": "LUC", "alias": "Land Use Code", "type": "esriFieldTypeString"},
                       {"name": "SALEDT", "alias": "Sale Date", "type": "esriFieldTypeDate"},
                       {"name": "Shape", "type": "esriFieldTypeGeometry"}]}
    ring = [[-84.0, 36.0], [-84.0, 36.01], [-83.99, 36.01], [-83.99, 36.0], [-84.0, 36.0]]

    def feat(i):
        return {"attributes": {"OBJECTID": i, "PIN": "X-%d" % i, "LUC": "400", "SALEDT": 1693440000000},
                "geometry": {"rings": [ring]}}
    pages = [{"features": [feat(1), feat(2)], "exceededTransferLimit": True}, {"features": [feat(3)]}]
    calls = []

    def fake(url, params, timeout=120, tries=4):
        calls.append(params)
        if url.endswith("/0"):
            return meta
        if params.get("returnCountOnly"):
            return {"count": 3}
        return pages[min(len([c for c in calls if "resultOffset" in c]) - 1, 1)]
    real = arcgis._call
    arcgis._call = fake
    try:
        d = _tmp()
        out = os.path.join(d, "p.csv")
        st = arcgis.fetch("https://example.invalid/arcgis/rest/services/X/FeatureServer/0", out, log=lambda *_: None)
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        assert st["rows"] == 3 and len(rows) == 3 and "Shape" not in rows[0]
        assert rows[0]["SALEDT"] == "2023-08-31" and rows[0]["geometry_wkt"].startswith("POLYGON")
        side = json.load(open(out + ".fields.json", encoding="utf-8"))
        assert side["fields"]["PIN"] == "Parcel Number"
        assert not os.path.exists(out + ".progress.json")
        shutil.rmtree(d, ignore_errors=True)
    finally:
        arcgis._call = real
    try:
        arcgis.layer_url("https://example.invalid/not/a/layer")
    except arcgis.LayerError:
        pass
    else:
        raise AssertionError("a non-layer URL must be refused")


# ---- the review loop -------------------------------------------------------------------
def test_review_round_trip_becomes_verified():
    d = _tmp()
    src = _write(os.path.join(d, "fa.csv"), FA_HEADERS, _fa_fixture())
    work = os.path.join(d, "w")
    _run(["run", src, "--work", work, "--no-evidence", "--accept-keywords"])
    rq = list(csv.DictReader(open(os.path.join(work, "out", "review_queue.csv"), encoding="utf-8")))
    target = [r for r in rq if r["apn"] == "C-21"][0]
    target.update({"correct_class": "Office", "correct_sub": "Medical", "notes": "walked it"})
    reviewed = _write(os.path.join(d, "reviewed.csv"), list(target.keys()), [target])
    assert _run(["apply-review", reviewed, "--work", work]) == 0
    _run(["run", src, "--work", work, "--no-evidence"])
    c = _cls(work)["C-21"]
    assert (c["asset_class"], c["sub_type"], c["provenance"]) == ("Office", "Medical", "verified")
    shutil.rmtree(d, ignore_errors=True)


# ---- packaging ---------------------------------------------------------------------------
def test_core_imports_are_stdlib_only():
    before = set(sys.modules)
    for m in ("taxonomy", "schema", "io_tables", "profiles", "keywords", "crosswalk", "ingest", "learn", "classify",
              "geo", "places", "poi", "value", "owners", "review", "export", "arcgis", "cli"):
        importlib.import_module("commercial_classifier." + m)
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not new & {"openpyxl", "duckdb", "shapely", "pyarrow", "requests", "numpy", "pandas"}, new


def test_skill_copy_matches_source_package():
    """skills/commercial-classifier ships a COPY of the package; it must not drift."""
    src = os.path.join(ROOT, "src", "commercial_classifier")
    dst = os.path.join(ROOT, "skills", "commercial-classifier", "scripts", "commercial_classifier")
    if not os.path.isdir(dst):
        raise AssertionError("missing skill copy: run python tools/sync_commercial_classifier_skill.py")
    for dp, _, files in os.walk(src):
        if "__pycache__" in dp:
            continue
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            a = os.path.join(dp, fn)
            b = os.path.join(dst, os.path.relpath(a, src))
            assert os.path.exists(b), "%s missing from the skill: run tools/sync_commercial_classifier_skill.py" % fn
            assert open(a, "rb").read() == open(b, "rb").read(), \
                "%s drifted: run python tools/sync_commercial_classifier_skill.py" % fn
    entry = os.path.join(ROOT, "skills", "commercial-classifier", "scripts", "cc.py")
    assert open(os.path.join(ROOT, "src", "commercial_classify.py"), "rb").read() == open(entry, "rb").read()


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print("ok    " + n)
        except Exception as e:  # report every failure, then exit non-zero
            failed += 1
            print("FAIL  %s: %s: %s" % (n, type(e).__name__, e))
    print("%d of %d tests passed" % (len(names) - failed, len(names)))
    raise SystemExit(1 if failed else 0)
