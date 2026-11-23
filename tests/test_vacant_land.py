"""Zero-network checks on the vacant land workflow. python tests/test_vacant_land.py

The live half of the test is `python src/vacant_land.py --phase probe`, which
proves the SiftMap land filter by count delta and exits 3 if the contract moved.
This file pins everything that can be checked without the network: the probe's
own pass/fail logic, owner classes, geometry, the buildability verdict, the
topography math, motivation and the pull plan.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import vacant_land as v  # noqa: E402
from shapely import STRtree  # noqa: E402
from shapely.geometry import LineString, Polygon, box  # noqa: E402

n = 0


def check(cond, msg):
    global n
    n += 1
    assert cond, msg


# ---- the probe's verdict on a live run
ROW = {k: None for k in v.ROW_KEYS}
ROW.update(propertyUse="Residential-Vacant Land",
           distressors={"is_probate_properties": {"is_active": False}})
good = {"none": 40017, "bogus": 40017, "type_land": 4379, **{k: 40017 for k in v.PROBE_KEYS}}
fails, notes = v.probe_failures(good, [ROW] * 20)
check(not fails and not notes, "the 2026-11-05 counts pass: %s %s" % (fails, notes))
fails, _ = v.probe_failures(dict(good, bogus=39000), [ROW])
check(any("bogus" in f for f in fails), "a bogus key that moves the count is a failure")
fails, _ = v.probe_failures(dict(good, type_land=40017), [ROW])
check(any("type_land did not filter" in f for f in fails), "an ignored type_land is a failure")
_, notes = v.probe_failures(dict(good, type_vacant_land=3000), [ROW])
check(notes and "type_vacant_land" in notes[0], "a newly working key is surfaced as a note")
fails, _ = v.probe_failures(good, [dict(ROW, propertyUse="Single Family Residential")] * 10)
check(any("read as vacant" in f for f in fails), "non-land rows under type_land fail the probe")
fails, _ = v.probe_failures(good, [{k: x for k, x in ROW.items() if k != "lotAcres"}])
check(any("lotAcres" in f for f in fails), "a lost row field fails the probe")
fails, _ = v.probe_failures(good, [dict(ROW, distressors=["is_vacant_property"])])
check(any("distressors" in f for f in fails), "a changed distressor shape fails the probe")

# ---- distressors: every key ships on every row, only is_active counts
d = {"is_tired_landlord": {"is_active": True}, "is_probate_properties": {"is_active": False},
     "is_vacant_property": {"is_active": None}}
check(v.active_distressors(d) == ["is_tired_landlord"], v.active_distressors(d))
check(v.active_distressors(None) == [], "no distressors is an empty list")

# ---- search payload shape
c = v.COUNTIES["39159"]
addr = v.county_address(c)
check(addr["search"] == "Union" and addr["type"] == "county", "search is the BARE county name")
check(addr["counties"] == [{"fips": "39159", "county_name": "Union"}], addr["counties"])
body = v.search_body(c, {"type_land": True}, 251)
check(body["filters"] == {"type_land": True} and body["result_index"] == 251, body)

# ---- owner classes (word boundaries matter: EUBANKS is a person)
for name, want in [
        ("EUBANKS DALE R", "person"), ("Penland Roy C & Darla L", "person"),
        ("WOODSIDE HOMEOWNERS ASSOCIATION INC", "hoa"), ("JEROME VILLAGE COMMUNITY AUTHORITY", "public"),
        ("UNION COUNTY COMMISSIONERS", "public"), ("JEROME TOWNSHIP TRUSTEES", "public"),
        ("CITY OF MARYSVILLE", "public"), ("FIRST BAPTIST CHURCH OF MARYSVILLE", "church"),
        ("CSX TRANSPORTATION INC", "utility"), ("NORFOLK SOUTHERN RAILWAY CO", "utility"),
        ("OHIO POWER CO", "utility"), ("M/I HOMES OF CENTRAL OHIO LLC", "builder"),
        ("ROCKFORD HOMES INC", "builder"), ("SMITH CUSTOM HOMES LLC", "builder"),
        ("THE HUNTINGTON NATIONAL BANK", "bank"), ("JONES MARY E TRUSTEE", "trust_estate"),
        ("ESTATE OF ROBERT SMITH", "trust_estate"), ("ABC PROPERTIES LLC", "entity"),
        ("OAK GROVE CEMETERY ASSN", "hoa"), ("", "person")]:
    got = v.owner_category(name)
    check(got == want, "%r -> %s, want %s" % (name, got, want))
check(v.owner_key("PENLAND ROY C & DARLA L") == v.owner_key("Penland Roy C") == "PENLAND ROY",
      v.owner_key("PENLAND ROY C & DARLA L"))
check(v.owner_key("ABC PROPERTIES LLC") == "ABC PROPERTIES", v.owner_key("ABC PROPERTIES LLC"))
check(v.owner_key("SMITH JOHN TRUSTEE") == "SMITH JOHN", v.owner_key("SMITH JOHN TRUSTEE"))

# ---- geometry, in feet
lot = box(0, 0, 100, 400)
m = v.shape_metrics(lot)
check(abs(m["width_ft"] - 100) < 0.5 and abs(m["depth_ft"] - 400) < 0.5, m)
flag = Polygon([(0, 0), (20, 0), (20, 300), (200, 300), (200, 500), (0, 500)])
check(v.shape_metrics(flag)["width_ft"] < 140, "a flag lot reads narrower than its bounding box")
road = [LineString([(-500, -30), (600, -30)])]          # centerline 30 ft off the lot line
rt = STRtree(road)
a = v.road_access(lot, rt, road, None, [])
check(not a["landlocked"] and 95 <= a["frontage_ft"] <= 400 and a["road_ft"] == 30, a)
far = box(0, 1000, 100, 1400)
a = v.road_access(far, rt, road, None, [])
check(a["landlocked"] and a["frontage_ft"] == 0, a)
row = [LineString([(0, 1000), (100, 1000)])]             # a right-of-way line on the lot line
a = v.road_access(far, rt, road, STRtree(row), row)
check(not a["landlocked"] and a["frontage_ft"] >= 95, "a right-of-way line gives access: %s" % a)
fl = [box(0, 0, 100, 100)]
check(v.area_share(lot, STRtree(fl), fl) == 0.25, v.area_share(lot, STRtree(fl), fl))
p = v.Proj(-83.37, 40.24)
x, y = p.xy(-83.37, 40.25)
check(x == 0 and 3640 < y < 3650, "0.01 deg of latitude is about 3,644 ft: %s" % y)
back = p.lonlat(p.geom(box(-83.4, 40.2, -83.39, 40.21)))
check(abs(back.bounds[0] + 83.4) < 1e-9, "the projection round-trips")

# ---- sewer proxy and built-on evidence
check(v.sewer_proxy([0.25] * 6)[0] == "sewer likely", "houses on quarter acres = sewer")
check(v.sewer_proxy([2.0] * 6)[0] == "septic area", "houses on 2 acres = septic")
check(v.sewer_proxy([0.7] * 6)[0] == "mixed", "houses on 0.7 acre lots = mixed")
check(v.sewer_proxy([0.25] * 2, platted=False)[0] == "septic area", "unplatted with no houses around = rural septic")
check(v.sewer_proxy([], platted=True, acres=0.3)[0] == "sewer likely", "a 0.3 acre platted lot was approved on sewer")
check(v.sewer_proxy([], platted=True, acres=0.8)[0] == "unknown", "a big platted lot in an empty plat: unknown")
check(v.dwelling_year("0") is None and v.dwelling_year("") is None and v.dwelling_year("2024") == 2024,
      "dwelling age 0 is no house")
asof = dt.date(2026, 11, 5)
ms = lambda d: int(dt.datetime(d.year, d.month, d.day).timestamp() * 1000)  # noqa: E731
house = {"salePrice": 693600, "landValue": 49970, "saleDate": ms(dt.date(2026, 10, 15)),
         "NumberofParcelsInSale": 1}
check(v.built_since_reappraisal(house, None, None, asof), "a $693,600 sale on a $49,970 lot is a house")
land = dict(house, salePrice=60000)
check(not v.built_since_reappraisal(land, None, None, asof), "a $60,000 lot sale is a lot sale")
old = dict(house, saleDate=ms(dt.date(2015, 1, 1)))
check(not v.built_since_reappraisal(old, None, None, asof), "an old sale is not evidence of new building")
check(v.built_since_reappraisal(None, 2024, None, asof) and v.built_since_reappraisal(None, None, 2025, asof),
      "dwelling year and a recent permit are evidence")
check(v.zoning_nonres("Government//Civic") and not v.zoning_nonres("Agricultural Residential")
      and not v.zoning_nonres("") and not v.zoning_nonres("Mixed-Use Residential"), "zoning screen")

# ---- buildability verdict
BASE = {"owner_cat": "person", "built": [], "platted": True, "frontage_ft": 120, "landlocked": False,
        "width_ft": 90, "compactness": 0.6, "flood_pct": 0.0, "floodway_pct": 0.0,
        "sewer": "sewer likely", "acres": 0.4, "zoning_nonres": False, "class": "500",
        "steep_share": 0.0, "sits_low_ft": 0.0, "relief_ft": 3}
verdict, score, hard, flags = v.buildability(BASE)
check(verdict == "BUILDABLE" and score == 100 and not hard and not flags, (verdict, score, hard, flags))
verdict, score, hard, _ = v.buildability(dict(BASE, platted=False, landlocked=True, frontage_ft=0))
check(verdict == "NOT BUILDABLE" and "landlocked" in hard[0], (verdict, hard))
verdict, *_ = v.buildability(dict(BASE, platted=False, landlocked=True, frontage_ft=0, assemblage_frontage=True))
check(verdict != "NOT BUILDABLE", "a landlocked lot whose owner holds the road-front lot is not dead")
check(v.buildability(dict(BASE, owner_cat="hoa"))[0] == "PUBLIC OR COMMON", "common areas are not leads")
check(v.buildability(dict(BASE, owner_cat="builder"))[0] == "BUILDER INVENTORY", "builder lots are inventory")
check(v.buildability(dict(BASE, built=["x"]))[0] == "BUILT SINCE REAPPRAISAL", "built-on parcels")
verdict, _, _, flags = v.buildability(dict(BASE, sewer="septic area", acres=0.4))
check(verdict != "BUILDABLE" and any(f.startswith("septic risk") for f in flags), (verdict, flags))
verdict, _, hard, _ = v.buildability(dict(BASE, steep_share=0.6))
check(verdict == "NOT BUILDABLE" and "steeper" in hard[0], hard)
_, score_low, _, flags = v.buildability(dict(BASE, sits_low_ft=5))
check(score_low < 100 and any("below the land around it" in f for f in flags), flags)
_, s7, _, _ = v.buildability(dict(BASE, steep_share=None, sits_low_ft=None))
check(s7 == 97, "unread topography scores 7 of 10: %s" % s7)
check(v.buildability(dict(BASE, width_ft=20))[0] == "NOT BUILDABLE", "a 20 ft strip is not a house lot")
check(v.buildability(dict(BASE, flood_pct=0.7))[0] == "NOT BUILDABLE", "mostly floodplain fails")

# ---- topography math
pts = v.grid_points(box(0, 0, 100, 100), 25)
check(len(pts) == 16 and pts[0][:2] == (0, 0), "a 100 ft square on a 25 ft grid is 16 cells: %d" % len(pts))
check(len(v.grid_points(box(0, 0, 5, 5), 25)) == 1, "a lot smaller than one cell still gets a sample")
check(v.spacing_for(43560 * 2, 60) > v.spacing_for(43560 * 0.2, 60) == v.MIN_SPACING_FT, "spacing")
# a 30 ft grid sloping 6 ft per cell eastward (a 20% grade), flat to the north
M = 1 / v.Proj.M_TO_FT
steep = {(i, j): (100 + 6 * i) * M for i in range(4) for j in range(4)}
t = v.topo_from_samples(steep, 30, [(100 + 9) * M] * 5)
check(t["steep_share"] == 1.0 and t["slope_mean_pct"] == 20.0 and t["relief_ft"] == 18.0, t)
check(t["sits_low_ft"] == 0.0, "ring median 109 ft against lot median 109 ft: %s" % t["sits_low_ft"])
flat = {(i, j): 100 * M for i in range(4) for j in range(4)}
t = v.topo_from_samples(flat, 30, [104 * M] * 5)
check(t["steep_share"] == 0.0 and t["sits_low_ft"] == 4.0, "a flat lot 4 ft under its ring: %s" % t)
t = v.topo_from_samples({(0, 0): None, (1, 0): 1.0}, 30, [])
check(t["relief_ft"] is None and t["steep_share"] is None, "too few reads stays unread")

# ---- mailing, motivation, tier
mp = v.mail_parts("100 Example Rd, Plain City, OH 43064")
check(mp == {"street": "100 Example Rd", "city": "Plain City", "state": "OH", "zip": "43064"}, mp)
check(v.mail_parts("PO Box 12, Naples, FL 34102-1234")["zip"] == "34102", "zip+4 trims")
F = {"owner_cat": "person", "owner_lots_in_window": 1, "side_lot_of": ""}
mo = v.motivation(F, {"mail": "1 Gulf Dr, Naples, FL 34102", "sale_date": "1998-06-01"}, {"43040"}, asof)
check(mo["segment"] == "Out-of-state owner" and "Held 20+ years" in mo["segment_tags"]
      and mo["motivation"] == 40, mo)
mo = v.motivation(F, {"mail": "1 Main St, Marysville, OH 43040", "tax_delinquent_year": 2024,
                      "sale_date": "2010-01-01"}, {"43040"}, asof)
check(mo["segment"] == "Tax delinquent" and mo["motivation"] == 28, mo)
mo = v.motivation(F, {"mail": "1 Main St, Marysville, OH 43040", "sale_date": "2026-01-01"}, {"43040"}, asof)
check(mo["recent_buy"] and mo["motivation"] == 0 and mo["segment"] == "Local owner", mo)
mo = v.motivation(dict(F, side_lot_of="12 ELM ST"), {"mail": "12 Elm St, Marysville, OH 43040"}, {"43040"}, asof)
check(mo["segment"] == "Neighbor side lot", mo)
check(v.tier({"build_score": 70, "motivation": 25, "segment_tags": ["Out-of-state owner"]}) == "A",
      "a solid lot with a strong reason is A")
check(v.tier({"build_score": 70, "motivation": 12, "segment_tags": ["Out-of-county owner"]}) == "B",
      "a solid lot with a weak reason is B")
check(v.tier({"build_score": 60, "motivation": 40, "segment_tags": ["Tax delinquent"]}) == "B",
      "a motivated owner on a middling lot is B, not A")
check(v.tier({"build_score": 90, "motivation": 3, "segment_tags": ["Local owner"]}) == "C",
      "a great lot with no reason to sell is C")
check(v.zip5("MARYSVILLE OH 43040") == "43040" and v.zip5("43064-1234") == "43064" and v.zip5(None) == "",
      "Union's ZipCode field carries city and state")

# ---- comps: price per lot inside a size band, widening when thin
comps = ([{"township": "JEROME", "acres": 0.3, "price": 100000}] * 5
         + [{"township": "DARBY", "acres": 1.5, "price": 70000}] * 6)
ref, basis = v.comp_reference({"acres": 0.25, "township": "JEROME"}, comps)
check(ref == 100000 and "Jerome" in basis, (ref, basis))
ref, basis = v.comp_reference({"acres": 1.2, "township": "JEROME"}, comps)
check(ref == 70000 and basis.startswith("rural 1.00+"), (ref, basis))
check(v.acre_band(0.2) == "0.20-0.49 ac" and v.acre_band(1.0) == "1.00+ ac", "size bands")
lc = ([{"township": "JEROME", "acres": 0.3, "price": 120000, "land_value": 100000}] * 25
      + [{"township": "DARBY", "acres": 1.5, "price": 60000, "land_value": 30000}] * 6
      + [{"township": "PARIS", "acres": 1.5, "price": 60000, "land_value": 30000}] * 6)
r = v.market_ratios(lc)
check(r["JEROME"] == 1.2 and r["DARBY"] == 2.0 and r["_rural"] == 2.0,
      "a subdivision market is kept out of the rural ratio: %s" % r)
ref, basis = v.comp_reference({"acres": 0.4, "township": "CLAIBOURNE", "land_value_est": 20000,
                               "class": "500"}, lc, ratios=r)
check(ref == 40000 and "rural" in basis, "a thin township uses the rural ratio, not Jerome's: %s %s" % (ref, basis))
ref, basis = v.comp_reference({"acres": 1.4, "township": "YORK", "land_value_est": 171,
                               "class": "110"}, lc, ratios=r)
check(ref == 60000 and basis.startswith("rural 1.00+"),
      "a CAUV land value is never priced; rural lot sales are: %s %s" % (ref, basis))
check(v.market_ratios(lc[:5])["_rural"] is None, "the rural ratio needs 10 sales")

# ---- pull plan: mailable, matched, and not already in the account
plan = v.pull_plan([{"mailable": True, "sid": "1", "tier": "A"}, {"mailable": True, "sid": "2", "tier": "A", "saved": True},
                    {"mailable": False, "sid": "3", "tier": "B"}, {"mailable": True, "sid": None, "tier": "B"}],
                   "Union OH")
check(plan == {"list": "Union OH - Buildable Lots", "tiers": {"A": ["1"]}}, plan)
plan = v.pull_plan([{"mailable": False, "pull_candidate": True, "sid": "4", "tier": "C"},
                    {"mailable": False, "pull_candidate": True, "sid": "5", "tier": "C", "saved": True}], "Union OH")
check(plan["tiers"] == {"C": ["4"]}, "a pull candidate not yet in the account is in the pull plan: %s" % plan)

# ---- owners from the account: a DataSift record in the shape a SiftMap detail had (invented record)
REC = {"uuid": "rec-1", "apn": "9900100001", "status": "not_interested",
       "owner": {"first_name": "Avery", "last_name": "Testowner", "company": "", "deceased": False,
                 "address": {"street": "1 Example Rd", "city": "Sampleville", "state": "fl", "postal_code": "33001-0001"}},
       "secondary_owners": [{"first_name": "Blake", "last_name": "Testowner"}],
       "tags": [{"title": "Do Not Market"}], "lists": [{"title": "Sample OH - Buildable Lots"}],
       "tax_delinquent_value": "812.40", "tax_delinquent_year": 2025, "last_sold": "2001-04-02",
       "last_sale_price": 18000, "lot_type": "Standard"}
fr = v.flatten_record(REC)
check(fr["mail"] == "1 Example Rd, Sampleville, FL 33001", fr["mail"])
check(fr["owner_name"] == "Avery Testowner" and fr["co_owners"] == ["Blake Testowner"], fr)
check(fr["tax_delinquent"] and fr["tax_amount"] == 812.4 and fr["sale_date"] == "2001-04-02", fr)
check(fr["do_not_mail"] and fr["dead_status"] and fr["record_uuid"] == "rec-1", "do not market and a dead status read")
check(v.flatten_record({"owner": {"company": "Sample Land LLC", "address": {"city": "X"}}})["mail"] == "",
      "no mailing street means no mailing address, not a city alone")
check(v.flatten_record({"owner": {"company": "Sample Land LLC"}})["owner_name"] == "Sample Land LLC", "a company owner")
m = v.motivation({"owner_cat": "person"}, fr, {"43040"}, as_of=dt.date(2026, 11, 23))
check("Out-of-state owner" in m["segment_tags"] and "Tax delinquent" in m["segment_tags"]
      and "Held 20+ years" in m["segment_tags"], "a record scores like a detail: %s" % m["segment_tags"])

row, src_ = v.owner_row("7", {"7": dict(fr, assessed_land=None)}, {"7": {"assessed_land": 7000, "owner_name": "Old Name"}})
check(src_ == "account" and row["owner_name"] == "Avery Testowner" and row["assessed_land"] == 7000,
      "the record wins on what it fills, a cached detail fills the rest: %s" % row)
check(v.owner_row("8", {}, {"8": {"owner_name": "Cached"}})[1] == "siftmap cache", "a cached detail is the fallback")
check(v.owner_row("9", {}, {}) == (None, ""), "no owner anywhere")

# ---- score: record, cached detail, and a lot with no owner yet (an invented three-lot county)
import json as _json
import tempfile as _tempfile
_TMP = Path(_tempfile.mkdtemp())
_C = {"fips": "99001", "name": "Sample", "state": "OH", "label": "Sample OH", "slug": "sample_oh"}
_D = _TMP / "sample_oh"
(_D / "layers").mkdir(parents=True)
(_D / "layers" / "parcels.json").write_text(_json.dumps([{"attributes": {"ZipCode": "SAMPLEVILLE OH 43040"}}]))


def _lot(camano, sid, **kw):
    f = {"camano": camano, "sid": sid, "verdict": "BUILDABLE", "build_score": 80, "owner_cat": "person",
         "flags": [], "hard": [], "zip": "43040", "owner_lots_in_window": 1, "side_lot_of": "",
         "last_sale_date": "1999-01-01", "saved": False}
    f.update(kw)
    return f


_D.joinpath("screen.json").write_text(_json.dumps([
    _lot("9900100001", "11", saved=True, saved_uuid="rec-1"),
    _lot("9900100002", "12"),
    _lot("9900100003", "13"),
    _lot("9900100004", "14", last_sale_date="2026-06-01"),
]))
_D.joinpath("records.json").write_text(_json.dumps({"11": dict(fr, apn="9900100001", do_not_mail=False,
                                                               dead_status=False)}))
_D.joinpath("details.json").write_text(_json.dumps({"12": {"apn": "9900100002", "owner_name": "Casey Sample",
                                                           "mail": "2 Sample Ln, Plain City, OH 43064"}}))
_D.joinpath("saved_ids.json").write_text(_json.dumps({"saved": {"11": "rec-1"}}))
_keep = v.OUT_ROOT
v.OUT_ROOT = str(_TMP)
try:
    v.phase_score(_C, None)
    got = {f["sid"]: f for f in _json.loads(_D.joinpath("scored.json").read_text())}
finally:
    v.OUT_ROOT = _keep
check(got["11"]["owner_source"] == "account" and got["11"]["mailable"] and not got["11"]["pull_candidate"],
      "a record in the account is the owner and is mailable: %s" % {k: got["11"].get(k) for k in ("owner_source", "mailable")})
check(got["12"]["owner_source"] == "siftmap cache" and got["12"]["mailable"], "a cached detail still mails")
check(got["13"]["owner_source"] == "" and not got["13"]["mailable"] and got["13"]["pull_candidate"],
      "no owner yet: not mailable, a pull candidate")
check(not got["14"]["pull_candidate"], "a lot bought in the last 2 years is not pulled")
check([s for s in ("11", "12", "13", "14") if got[s]["to_pull"]] == ["12", "13"],
      "the pull list: mailable lots that are not records, and lots whose owner is not visible yet")


# ---- hydrate in account mode makes no SiftMap detail read (fakes: which ids are records, the record read)
class _NoDetail:
    def _request(self, *a, **kw):
        return {"data": []}

    def get_detail(self, *a, **kw):
        raise AssertionError("account mode made a SiftMap detail read")


class _FakeCRM:
    reads: list = []

    def get_record(self, uuid):
        self.reads.append(uuid)
        if uuid == "rec-gone":
            raise RuntimeError("HTTP 404 on GET /api/internal/property/rec-gone/: not found")
        return dict(REC, uuid=uuid)


import types as _types


class _SearchById:
    """SiftMap's ids-typed search: 11 and 13 are records, 12 is not, 99 is unknown to SiftMap."""
    bodies: list = []

    def _request(self, path, method="GET", body=None, **kw):
        self.bodies.append(body)
        rows = {"11": {"id": 11, "saved": True, "saved_uuid": "rec-1"}, "12": {"id": 12, "saved": False},
                "13": {"id": 13, "saved": True, "saved_uuid": "rec-gone"}}
        return {"data": [rows[a["search"]] for a in body["addresses"] if a["search"] in rows]}

    def get_detail(self, *a, **kw):
        raise AssertionError("account mode made a SiftMap detail read")


saved, unknown = v.saved_now(_SearchById(), ["11", "12", "13", "99"])
check(saved == {"11": "rec-1", "13": "rec-gone"} and unknown == ["99"], (saved, unknown))
check(all(a["type"] == "ids" for a in _SearchById.bodies[0]["addresses"]) and _SearchById.bodies[0]["filters"] == {},
      "the lookup is an ids-typed search")
check(len(v.saved_now(_SearchById(), [str(1000 + i) for i in range(250)])[1]) == 250 and len(_SearchById.bodies) == 4,
      "100 ids a search")
_keep_crm = v._crm
v._crm = lambda: _FakeCRM()
v.OUT_ROOT = str(_TMP)
try:
    _D.joinpath("records.json").write_text(_json.dumps({"12": {"read_at": "2026-11-01T00:00:00"}}))
    args = _types.SimpleNamespace(owners="account", limit=0, record_days=7, refresh_owners=False)
    rc = v._hydrate_account(_C, args, _SearchById(), ["11", "12", "13"])
    recs = _json.loads(_D.joinpath("records.json").read_text())
    sv_ = _json.loads(_D.joinpath("saved_ids.json").read_text())
finally:
    v.OUT_ROOT, v._crm = _keep, _keep_crm
check(rc == 0 and sorted(_FakeCRM.reads) == ["rec-1", "rec-gone"], "only lots that are records are read: %s" % _FakeCRM.reads)
check(set(recs) == {"11"} and recs["11"]["record_uuid"] == "rec-1",
      "a lot that left the account and a 404 record are dropped: %s" % sorted(recs))
check(sv_["saved"] == {"11": "rec-1", "13": "rec-gone"}, "which lots are records is kept for score")

# ---- the output folder follows VACANT_LAND_DIR (the box keeps it on its volume)
import os as _os
import subprocess as _sp
out = _sp.run([sys.executable, "-c", "import vacant_land as v; print(v.OUT_ROOT)"], capture_output=True, text=True,
              cwd=str(Path(__file__).resolve().parents[1] / "src"), env=dict(_os.environ, VACANT_LAND_DIR="/data/vl"))
check(out.stdout.strip() == "/data/vl", "VACANT_LAND_DIR moves the output: %r %s" % (out.stdout, out.stderr[-300:]))

# ---- house rules on the source itself
src = (Path(__file__).resolve().parents[1] / "src" / "vacant_land.py").read_text(encoding="utf-8")
check(chr(0x2014) not in src and chr(0x2013) not in src, "no em or en dash in the module")
check(chr(8) not in src, "no backspace byte (the heredoc word-boundary trap)")
print("test_vacant_land: %d checks passed" % n)
