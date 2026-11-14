"""Zero-network checks for src/land_market.py: the rules that are silently wrong when they break.

    python tests/test_land_market.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import land_market as lm  # noqa: E402

N = 0


def check(cond, msg):
    global N
    N += 1
    if not cond:
        raise AssertionError(msg)


# the state's WATERSEWER field is "<water>/<sewer>"
check(lm.water_sewer("03 Public/Individual") == ("public", "individual"), "public water, septic")
check(lm.water_sewer("01 Public/Public") == ("public", "public"), "public sewer")
check(lm.water_sewer(None) == (None, None), "missing record")
check(lm.sewer_label("public") == "sewer likely" and lm.sewer_label("individual") == "septic area", "sewer words")

# GISLINK = county code + the cluster parcel number; four counties ship it dashed
check(lm.gis_key("005", "001m A 00400") == "005001MA00400", "Blount layout")
check(lm.gis_key("001", "019-028.00") == "00101902800", "Anderson dashed")
check(lm.gis_key("045", "023h-b-006.00") == "045023HB00600", "Jefferson dashed with group")
check(lm.pkey("005001M A 00400") == "005001MA00400", "state side drops spaces")

# lanes
check(lm.lane_of("vacant", 0.15) == "lot", "infill lot")
check(lm.lane_of("vacant", 0.05) is None, "sliver is not a lot")
check(lm.lane_of("vacant", 3) == "homesite", "homesite")
check(lm.lane_of("house", 3) is None, "a house on 3 acres is not land")
check(lm.lane_of("house", 40) == "ent_tract", "farmhouse on 40 acres is a tract")
check(lm.lane_of("ag", 200) == "large_tract", "large tract")
check(lm.status_of({"land_use": "Vacant"}) == "vacant", "vacant status")
check(lm.status_of({"land_use": "Agricultural", "property_type": "Agricultural / Rural"}) == "ag", "ag status")

# owners
check(lm.owner_cat("Knox County") == "public", "county is public")
check(lm.owner_cat("Tennessee Valley Authority") == "public", "TVA is public")
check(lm.owner_cat("D R Horton Inc") == "builder", "production builder")
check(lm.owner_cat("Smithbilt Llc") == "builder", "Smithbilt")
check(lm.owner_cat("Arconic Tennessee Llc") == "industrial", "industrial owner")
check(lm.owner_cat("Cmh Manufacturing Inc") == "builder", "Clayton is a builder before it is a manufacturer")
check(lm.owner_cat("Jordan E Sample") == "person", "person")
check(lm.owner_cat("") == "unknown", "no name")
check(not lm.is_seller({"owner_cat": "industrial"}) and lm.is_seller({"owner_cat": "trust_estate"}), "sellers")


# tract fit: sewer path, hard fails, house carve-out
def tract(**kw):
    f = {"fips": "47009", "lane": "ent_tract", "owner_cat": "person", "acres": 30, "flood_pct": 0.0,
         "steep_share": 0.05, "sewer_state": "public", "water_state": "public", "nb_3mi": 120, "nb_1mi": 30,
         "frontage_ft": 400, "landlocked": False, "with_house": False, "interstate_10min": "YES",
         "dev_opportunity": "DO", "state_class": "61"}
    f.update(kw)
    return f


v, s, hard, flags, path = lm.tract_fit(tract())
check(v == "ENTITLEMENT: SEWER" and path == "sewer subdivision", "sewered tract near builders is a sewer play")
v, s, hard, flags, path = lm.tract_fit(tract(sewer_state="individual"))
check(v == "ENTITLEMENT: SEPTIC LOTS" and path == "septic lots", "public water and septic")
v, s, hard, flags, path = lm.tract_fit(tract(landlocked=True))
check(v == "NOT A FIT" and any("no road" in h for h in hard), "landlocked tract fails")
v, s, hard, flags, path = lm.tract_fit(tract(flood_pct=0.7))
check(v == "NOT A FIT", "mostly floodplain fails")
v, s, hard, flags, path = lm.tract_fit(tract(state_class="41"))
check(v == "NOT A FIT" and any("utility" in h for h in hard), "state roll utility fails")
f = tract(with_house=True, acres=10)
lm.tract_fit(f)
check(f["dev_acres"] == round(10 * (1 - 0.5 * 0.05) - 2, 1), "house carves out 2 acres, slope takes half its share")
v, s, hard, flags, path = lm.tract_fit(tract(slope_mean_pct=40))
check(v == "NOT A FIT" and any("mountainside" in h for h in hard), "a 40% average grade is not a subdivision")
v, s, hard, flags, path = lm.tract_fit(tract(slope_mean_pct=22))
check(v.startswith("ENTITLEMENT") and any("lot yield" in x for x in flags), "a 22% grade is flagged, not failed")

# lots: East Tennessee slope rule (fail at a 45% average grade, price 15-45%)
lotf = {"lane": "lot", "owner_cat": "person", "built": [], "platted": True, "frontage_ft": 80, "landlocked": False,
        "width_ft": 80, "compactness": 0.6, "flood_pct": 0.0, "sewer": "sewer likely", "acres": 0.3,
        "steep_share": 1.0, "sits_low_ft": 0, "relief_ft": 10, "zoning_nonres": False, "geom_src": "state polygon"}
g = dict(lotf, slope_mean_pct=30)
lm.verdict_for(g)
check(g["verdict"] in ("BUILDABLE", "LIKELY BUILDABLE") and any("hillside" in x for x in g["flags"]),
      "a 30% hillside lot is priced, not rejected")
g = dict(lotf, slope_mean_pct=50)
lm.verdict_for(g)
check(g["verdict"] == "NOT BUILDABLE" and any("major grading" in h for h in g["hard"]), "a 50% grade fails")
g = dict(lotf, slope_mean_pct=8, steep_share=0.1)
lm.verdict_for(g)
check(g["verdict"] == "BUILDABLE", "a gentle lot on sewer is buildable")
v, s, hard, flags, path = lm.tract_fit(tract(owner_cat="builder"))
check(v == "BUILDER INVENTORY", "builder land is inventory")
v, s, hard, flags, path = lm.tract_fit(tract(fips="47093", sewer_state=None, water_state=None, sewer="septic area",
                                             gpp="RURAL", zoning="I"))
check(v == "NOT A FIT" and any("industrial" in h for h in hard), "Knox industrial zoning fails")
v, s, hard, flags, path = lm.tract_fit(tract(lane="large_tract", acres=300))
check(v.startswith("LARGE: ENTITLEMENT"), "large tracts keep their own label")

# motivation and tiers
m, tags = lm.motivation({"distress": ["is_recent_deliquent"], "owner_cat": "person", "mailing_address_state": "FL",
                         "tenure": 25, "owner_age": 80}, {"37920"}, 1)
check(tags >= {"tax", "oos", "longhold", "aged"} and m == 20 + 25 + 15 + 8, "motivation adds up")
m, tags = lm.motivation({"owner_cat": "person", "mailing_address_state": "TN",
                         "mailing_full_address": "1 Main St, Knoxville, TN 37920", "tenure": 1}, {"37920"}, 1)
check(m == 0 and tags == {"local"}, "a local recent buyer has no reason")
lot = {"lane": "lot", "verdict": "BUILDABLE", "score": 80, "motivation": 35, "tags": ["oos"], "n_fit": 2}
check(lm.tier_for(lot) == "A", "fit + buildable + reason is A")
check(lm.tier_for(dict(lot, n_fit=0)) == "R", "no builder fit is retail, not the lot desk")
check(lm.tier_for(dict(lot, verdict="NOT BUILDABLE")) is None, "unbuildable has no tier")
check(lm.tier_for({"lane": "ent_tract", "verdict": "ENTITLEMENT: SEWER", "score": 75, "motivation": 10,
                   "tags": ["estate"]}) == "A", "strong reason on a strong tract is A")

# pricing: tracts at farm price, approval math capped at a first phase
prices = {"47009|50-150 ac": {"n": 20, "median_price": 900000, "median_per_acre": 10000, "p25_per_acre": 8000,
                              "p75_per_acre": 14000},
          "47009|150+ ac": {"n": 12, "median_price": 2000000, "median_per_acre": 6000, "p25_per_acre": 5000,
                            "p75_per_acre": 8000}}
buyers = {"county": {"47009": {"finished_lot_med": 60000, "production_lot_buys": 40}}}
x = {"dataflik_id": 1, "fips": "47009", "lane": "ent_tract", "acres": 100, "dev_acres": 100,
     "verdict": "ENTITLEMENT: SEWER", "path": "sewer subdivision"}
lm.price_parcel(x, prices, {}, buyers)
check(x["farm_ref"] == 1000000 and x["offer_hi"] == 1000000 and x["offer_lo"] == 850000, "farm price offer")
check(x["est_lots"] == 200 and x["paper_lot_value"] == round(0.42 * 60000, -2), "sewer lot yield and paper value")
y = dict(x, dataflik_id=2, lane="large_tract", acres=400, dev_acres=400, verdict="LARGE: ENTITLEMENT: SEWER")
lm.price_parcel(y, prices, {}, buyers)
check(y["est_lots"] == int(lm.PHASE_AC * 2.0) and y.get("phase_note"), "big farms are priced one phase at a time")

# nearby tract comps beat the county band, and need 5 sales of a similar size
since = lm.bl.SINCE_MS + 86400000 * 400
land = {i: {"status": "vacant", "area_lot_acres": 20, "lat": 35.90 + i * 0.002, "lon": -83.90} for i in range(6)}
land[99] = {"status": "vacant", "area_lot_acres": 400, "lat": 35.90, "lon": -83.90}       # wrong size, ignored
sales = {i: [{"sale_date": since, "sale_amount": 20 * (10000 + 1000 * i), "buyer_full_name": "SMITH JOHN",
              "seller_full_name": "JONES MARY"}] for i in range(6)}
sales[99] = [{"sale_date": since, "sale_amount": 400 * 500, "buyer_full_name": "A LLC", "seller_full_name": "B"}]
tc = lm.TractComps(land, sales)
got = tc.near(35.905, -83.90, 25)
check(got and got["n"] == 6 and got["median_per_acre"] == 12500, "6 similar tract sales nearby, median $/ac")
check(tc.near(36.60, -83.90, 25) is None, "no comps 50 miles away")
check(tc.near(35.905, -83.90, 300) is None, "a 300 ac tract does not price off 20 ac sales")
z = {"dataflik_id": 3, "fips": "47009", "lane": "ent_tract", "acres": 25, "dev_acres": 25, "lat": 35.905,
     "lon": -83.90, "verdict": "ENTITLEMENT: SEWER", "path": "sewer subdivision"}
lm.price_parcel(z, prices, {}, buyers, tc)
check(z["farm_per_acre"] == 12500 and "within 5 mi" in z["ref_basis"], "tract priced off nearby sales")

# mail exclusions
base = {"owner_cat": "person", "tier": "A", "tenure": 10, "mailing_full_address": "1 Main St, Knoxville, TN 37920",
        "verdict": "BUILDABLE"}
check(lm.mail_exclusion(base) is None, "mailable")
check(lm.mail_exclusion(dict(base, tenure=1)) == "bought in the last 2 years", "recent buyer")
check(lm.mail_exclusion(dict(base, is_active_listing=True)) == "listed on the MLS", "listed")
check(lm.mail_exclusion(dict(base, owner_cat="public")).startswith("owner is"), "public owner")
check(lm.mail_exclusion(dict(base, tier=None, verdict="NOT BUILDABLE")).startswith("not marketable"), "no tier")

# the Economics sheet recalculates to the right answer (its first version pointed every formula one row too high)
import re  # noqa: E402
from openpyxl import Workbook  # noqa: E402

mail = [{"lane": "lot", "tier": "A", "n_fit": 1, "verdict": "BUILDABLE"} for _ in range(1000)]
mail += [{"lane": "ent_tract", "tier": "A", "verdict": "ENTITLEMENT: SEWER", "est_spread": 400000,
          "entitle_cost": 50000} for _ in range(240)]
an = {"boxes": [{"buys_12m": 500}]}
rows = lm.econ_sheet(Workbook(), mail, an)
cells = {"B%d" % i: v for i, (_l, v, *_r) in enumerate(rows, 2)}


def value(ref):
    v = cells[ref]
    if not (isinstance(v, str) and v.startswith("=")):
        return v
    expr = v[1:]
    m = re.match(r'IF\((B\d+)>0,(.*),""\)$', expr)
    if m:
        if not value(m.group(1)) > 0:
            return None
        expr = m.group(2)
    expr = re.sub(r"B\d+", lambda mm: repr(value(mm.group(0))), expr)
    check(re.fullmatch(r"[\d.*/+()\- ]+", expr), "formula reduces to plain arithmetic: %s" % expr)
    return eval(expr, {"__builtins__": {}})  # noqa: S307


by = {label: value("B%d" % i) for i, (label, v, *_r) in enumerate(rows, 2) if v is not None}
check(abs(by["Lots closed a month"] - 600 * 0.03 * 0.15 * 0.70) < 1e-9, "lots closed = marketed x answer x sign x take")
check(abs(by["Marketing a month"] - 600 * 5 * 0.65) < 1e-9, "lot marketing = marketed x touches x cost")
check(abs(by["Share of the builders' yearly buying we would supply"] - 600 * 0.03 * 0.15 * 0.70 * 12 / 500) < 1e-9,
      "supply share against the builders' yearly buying")
check(abs(by["Tracts signed a year"] - 20 * 12 * 0.04 * 0.08) < 1e-9, "tracts signed = marketed x 12 x answer x sign")
check(abs(by["Tracts approved and sold a year"] - by["Tracts signed a year"] * 0.60 * 0.80) < 1e-9,
      "sold = signed x pre-sold x approved")
check(abs(by["Gross a year"] - by["Tracts approved and sold a year"] * 400000 * 0.50) < 1e-6, "gross = sold x spread x kept")
check(abs(by["Entitlement spend a year (pre-sold tracts only)"] - by["Tracts pre-sold a year"] * 50000) < 1e-6,
      "engineering is spent only on pre-sold tracts")
check(abs(by["Net a year before people"] - (by["Gross a year"] - by["Marketing a year"]
                                            - by["Entitlement spend a year (pre-sold tracts only)"]
                                            - by["Earnest money lost a year"])) < 1e-6, "net adds up")

# the module carries no stray control bytes (a heredoc once turned a word boundary into a backspace)
with open(lm.__file__, "rb") as fh:
    check(b"\x08" not in fh.read(), "no backspace byte in land_market.py")

print("test_land_market: %d checks passed" % N)
