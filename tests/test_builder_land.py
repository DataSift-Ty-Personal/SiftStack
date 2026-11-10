"""Zero-network checks on the builder land desk. python tests/test_builder_land.py

The live half is the run itself (`python src/builder_land.py --market ... --phase all`), which reads the opensearch
MCP. This file pins everything that can be checked without it: names, the deed-chain jump, what happens after a lot
purchase, buyer types, takedown pricing, buy boxes, the fit score, the offer, the national ranking and the workbook
text (which once crashed on a bare % in a formatted string).
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import builder_land as bl  # noqa: E402

n = 0


def check(cond, msg):
    global n
    n += 1
    assert cond, msg


def ms(y, m=6, d=1):
    return dt.datetime(y, m, d).timestamp() * 1000


def sale(y, amt, buyer, seller, m=6, tid=0):
    return {"sale_date": ms(y, m), "sale_amount": amt, "buyer_full_name": buyer, "seller_full_name": seller, "transaction_id": tid}


# ---- names
check(bl.key("BRADEN FAMILY PROPERTIES, LLC") == "BRADEN FAMILY PROPERTIES", "suffix stripped")
check(bl.is_entity("NS HOMES LLC") and not bl.is_entity("SMITH JOHN"), "entity detection")
check(bl.same_party("GUESS NICHOLAS", "Nicholas Guess"), "deed LAST FIRST matches index First Last")
check(not bl.same_party("SMITH JOHN", "Jane Doe"), "different people do not match")
check(bl.family({"buyer_full_name": "SMITH JOHN", "seller_full_name": "SMITH MARY"}), "shared surname is a family transfer")
check(not bl.family({"buyer_full_name": "SMITH HOMES LLC", "seller_full_name": "SMITH MARY"}), "an entity buyer is not family")
check(bl.pretty("GUESS NICHOLAS") == "Nicholas Guess", "a person reads first name first")
check(bl.pretty("BRADEN FAMILY PROPERTIES LLC") == "Braden Family Properties LLC", "an entity keeps LLC in caps")
check(bl.pretty("JOHN A EXAMPLE TRUST") == "John A Example Trust", "a trust is not rotated like a person")
check(bl.pretty("PULTE HOMES OF OHIO LLC") == "Pulte Homes of Ohio LLC", "small words stay lower case")
check(bl.area_name("Beaumont - Knoxville") == "Beaumont" and bl.area_name("Dipple") == "Dipple", "neighborhood city suffix dropped")

# ---- settings
check(bl.setting(1960, 0.10, 40) == "infill_old", "old neighborhood")
check(bl.setting(2000, 0.20, 40) == "leftover_lot", "built-out subdivision")
check(bl.setting(2015, 0.60, 40) == "production", "new subdivision")
check(bl.setting(1960, 0.10, 10) == "rural", "few homes is rural")

# ---- the deed-chain jump
house = {"year_built": 2022, "property_value": 300000}
c = bl.chain(house, [sale(2019, 10000, "SMITH JOHN", "SMITH MARY", tid=1),
                     sale(2020, 30000, "NS HOMES LLC", "DOE JANE", tid=2),
                     sale(2022, 280000, "BUYER ANN", "NS HOMES LLC", m=9, tid=3)])
check(c and c["acq"]["sale_amount"] == 30000 and c["exit"]["sale_amount"] == 280000, "lot buy and new-home exit read off the jump")
c = bl.chain(house, [sale(2019, 25000, "SMP HOLDINGS LLC", "DOE JANE", m=3, tid=1),
                     sale(2019, 40000, "NS HOMES LLC", "SMP HOLDINGS LLC", m=7, tid=2),
                     sale(2022, 280000, "BUYER ANN", "NS HOMES LLC", m=9, tid=3)])
check(c and c["acq"]["buyer_full_name"] == "NS HOMES LLC" and c["mid"]["sale_amount"] == 25000,
      "a lot wholesaled at 1.6x before the build is the middleman, not the builder's purchase")
c = bl.chain(house, [sale(2020, 2000000, "BIG LLC", "DOE JANE"), sale(2022, 280000, "BUYER ANN", "BIG LLC")])
check(c is None, "a deed over 3x the parcel value carried other parcels and is not a lot price")
c = bl.chain({"year_built": 2021, "property_value": 400000}, [sale(2019, 40000, "JONES BOB", "DOE JANE")])
check(c and c["acq"]["sale_amount"] == 40000 and c["exit"] is None, "no jump: the lot buyer still holds the house")
c = bl.chain({"year_built": 2021, "property_value": 400000}, [sale(2019, 40000, "JONES BOB", "DOE JANE"),
                                                             sale(2023, 60000, "ROE SAM", "JONES BOB")])
check(c is None, "a later priced sale with no jump cannot be placed")

# ---- after a vacant-lot purchase
s0 = sale(2023, 40000, "X LLC", "DOE JANE")
check(bl.resale_kind(s0, sale(2024, 300000, "BUYER ANN", "X LLC"))[0] == "house", "3x and $150K+ is a house the roll missed")
check(bl.resale_kind(s0, {"sale_date": s0["sale_date"] + 60 * bl.DAY_MS, "sale_amount": 55000})[0] == "quick_flip", "inside 120 days")
check(bl.resale_kind(s0, {"sale_date": s0["sale_date"] + 300 * bl.DAY_MS, "sale_amount": 55000})[0] == "lot_resale", "later lot resale")
check(bl.resale_kind(s0, None) == (None, None), "no resale")

# ---- buyer types
B = {"status": "vacant", "resale_kind": None}
check(bl.buyer_type("CITY OF KNOXVILLE", [B, B]) == "government or lender", "government")
check(bl.buyer_type("SMP HOLDINGS LLC", [dict(B, resale_kind="quick_flip")] * 2) == "lot wholesaler", "double closer")
check(bl.buyer_type("ACME LLC", [dict(B, status="built"), B]) == "builder", "built on a lot")
check(bl.buyer_type("ACME LLC", [dict(B, resale_kind="lot_resale")] * 2) == "lot flipper", "resold lots slowly")
check(bl.buyer_type("ACME LLC", [B, B]) == "holder (not built yet)", "holder")

# ---- takedowns and purchase events
now = bl.NOW_MS
buys = [{"key": "K", "date": now - 10 * bl.DAY_MS, "price": 300000} for _ in range(3)] + \
       [{"key": "K", "date": now - 400 * bl.DAY_MS, "price": 40000}, {"key": "K", "date": now - 30 * bl.DAY_MS, "price": 2000000, "multi": True}]
bl.bulk_flags(buys)
check(buys[0]["bulk"] and buys[0]["unit_price"] == 100000, "a 3-lot takedown is priced per lot")
check(not buys[3]["bulk"] and buys[3]["unit_price"] == 40000, "a single deed keeps its price")
check(buys[4]["bulk"] and buys[4]["unit_price"] is None, "a deed that also carried other parcels gets no lot price")
check(bl.events(buys) == 3 and bl.events(buys, 365) == 2, "a takedown counts as one purchase")

# ---- buy box and fit
check(bl.acre_range([0.1, 0.12, 0.15, 0.2, 0.3], "lot")[:2] == (0.08, 0.38), "p10-p90 widened")
check(bl.acre_range([], "tract")[:2] == bl.DEFAULT_ACRES["tract"], "no acreage: defaults")
lo, hi, _, _ = bl.acre_range([20.0], "tract")
check(lo == 10.0 and hi == 40.0, "one tract: half to double, inside the defaults")
mk = lambda d, ac, p: {"niche": "urban_infill", "acres": ac, "price": p, "unit_price": p, "bulk": False, "date": d,
                       "seller_entity": False, "area": "Beaumont - Knoxville", "zip": "37921"}
own = [mk(now - 100 * bl.DAY_MS, 0.15, 30000), mk(now - 200 * bl.DAY_MS, 0.2, 40000), mk(now - 900 * bl.DAY_MS, 0.18, 35000)]
box = bl.buy_box(own, [])
cb = box["classes"]["lot"]
check(cb["events"] == 3 and cb["price_med"] == 35000 and box["buys_12m"] == 2, "box: events, median paid, last 12 months")
check(box["from_person_share"] == 1.0 and box["areas"] == ["Beaumont - Knoxville"], "box: sellers and areas")
near = [(100.0, own[0]), (300.0, own[1])]
f = bl.fit({"acres": 0.18}, "lot", near, box)
check(f and f["dist"] == 100 and f["nearby"] == 2 and f["prices"] == [30000, 40000], "a lot near their own lots fits")
check(bl.fit({"acres": 2.0}, "lot", near, box) is None, "outside their size range")
check(bl.fit({"acres": 0.18}, "tract", near, box) is None, "no box for that class")
thin = bl.buy_box(own[:1], [])
check(bl.fit({"acres": 0.15}, "lot", near[:1], thin) is None, "one purchase is not a pattern")
check(bl.fit({}, "lot", near, box)["score"] < f["score"], "no acreage on record fits on location without the size credit")

# ---- price and offer
ref, basis = bl.reference_price([{"prices": [30000, 40000, 50000]}], [])
check(ref == 40000 and "fitting" in basis, "fitting builders' own prices")
ref, basis = bl.reference_price([{"prices": [30000]}], [20000, 25000, 30000])
check(ref == 25000 and "all niche" in basis, "falls back to every buy nearby")
check(bl.reference_price([], [1]) == (None, None), "too thin to price")
check(bl.offer(40000, 12000) == 28000 and bl.offer(18000, 12000) is None and bl.offer(None, 12000) is None, "offer")

# ---- geometry and streams
check(abs(bl.meters(36.0, -84.0, 36.01, -84.0) - 1105.4) < 1, "meters north")
pi = bl.PointIndex(600, 36.0)
pi.add(36.0, -84.0, "a")
pi.add(36.004, -84.0, "b")
pi.add(36.02, -84.0, "c")
check(sorted(i for _, i in pi.near(36.001, -84.0)) == ["a", "b"], "near returns only points inside the radius")
e = bl.balanced_edges([(0, 900), (100, 50), (200, 50)], 2, 0, 300)
check(e == [0, 100, 300], "most rows in the first bucket: the first stream gets only that bucket")
check(bl.balanced_edges([(0, 10)], 4, 0, 50) == [0, 50], "one bucket cannot be split")


class FakeCluster:
    """Answers the histogram aggregation over a fixed id list, the way the cluster does."""
    def __init__(self, ids):
        self.ids = ids

    def search(self, body, size, index="properties"):
        rng = body["query"]["bool"]["filter"][-1]["range"]["dataflik_id"]
        step = body["aggs"]["h"]["histogram"]["interval"]
        got = {}
        for i in self.ids:
            if rng["gte"] <= i < rng["lt"]:
                got[i // step * step] = got.get(i // step * step, 0) + 1
        return {"aggregations": {"h": {"buckets": [{"key": k, "doc_count": v} for k, v in sorted(got.items())]}}}


ids = list(range(0, 9000)) + list(range(10 ** 7, 10 ** 7 + 1000))      # 90% of the rows in the first 0.1% of the ids
base = {"bool": {"filter": [{"terms": {"fips": ["x"]}}], "must_not": []}}
b = bl.id_buckets(FakeCluster(ids), base, 0, 10 ** 7 + 1000, len(ids), 4)
e = bl.balanced_edges(b, 4, 0, 10 ** 7 + 1000)
per = [sum(1 for i in ids if e[k] <= i < e[k + 1]) for k in range(len(e) - 1)]
check(len(per) == 4 and max(per) <= 3500, "clustered ids are refined until every stream gets about a quarter: %s" % per)

# ---- sellers
check(bl.place({"has_postal_address": False, "property_city": "Columbus Grove", "property_zip": "45830"}) == (None, None, None),
      "no postal address: the district-as-city artifact is dropped")
check(bl.place({"property_city": "Knoxville", "property_zip": "37917", "neighborhood": "Parkridge"})[2] == "Parkridge", "a real place")
ok = {"full_name": "Jane Doe", "last_sale_date": ms(2010)}
check(bl.seller_reason(ok, set()) is None, "a person who has owned it for years is mailable")
check(bl.seller_reason(dict(ok, full_name="ACME HOMES LLC"), set()), "entity")
check(bl.seller_reason(dict(ok, is_active_listing=True), set()), "listed")
check(bl.seller_reason(dict(ok, last_sale_date=now - 100 * bl.DAY_MS), set()), "bought recently")
check(bl.seller_reason(dict(ok, lot_type="Landlocked"), set()), "landlocked")
check(bl.seller_reason(ok, {bl.key("Jane Doe")}), "a repeat lot buyer is a buyer, not a seller")

# ---- national
by = {"ALL": [{"fips": "47093", "county_name": "Knox", "buyer_full_name": "BRADEN FAMILY PROPERTIES LLC",
               "local_group_investor_purchases": 29, "total_investor_purchases": 60},
              {"fips": "47093", "county_name": "Knox", "buyer_full_name": "WOODLANDS HOMEOWNERS ASSOCIATION",
               "local_group_investor_purchases": 90, "total_investor_purchases": 90},
              {"fips": "39159", "county_name": "Union", "buyer_full_name": "ACME LLC",
               "local_group_investor_purchases": 3, "total_investor_purchases": 3}],
      "FLIP": [{"fips": "47093", "buyer_full_name": "BRADEN FAMILY PROPERTIES LLC", "local_group_investor_purchases": 8,
                "total_investor_purchases": 60},
               {"fips": "47093", "buyer_full_name": "DR HORTON INC", "local_group_investor_purchases": 5,
                "total_investor_purchases": 9000}],
      "WHOLESALE": [{"fips": "47093", "buyer_full_name": "SMP HOLDINGS LLC", "local_group_investor_purchases": 3}]}
named = {"47093": {"n": 29, "buys": 200, "county": "Knox", "top": ["BALL HOMES LLC (36)"]},
         "39159": {"n": 3, "buys": 20, "county": "Union", "top": []}}
r = bl.national_rank(by, {"47093": 4856, "39159": 1303}, named)
k = r[0]
check(k["fips"] == "47093" and k["rank"] == 1 and k["builders"] == 29 and k["wholesalers"] == 1, "national: named builders rank")
check(k["active_buyers"] == 1 and k["new_homes"] == 4856, "national: an HOA is not a land buyer")
check(k["resellers"] == 2 and k["top_wholesalers"] == ["SMP HOLDINGS LLC (3)"], "national: land resold as land is a reseller")
check(r[1]["fips"] == "39159" and r[1]["county"] == "Union", "national: the county name comes along")

# ---- workbook text formats without crashing and carries no dash
bx = dict(box, key="K", name="NS HOMES LLC", type="builder", buys=3, built=1, vacant=2, main_niche="urban_infill",
          niche_mix="urban_infill 3", last_buy="2026-01-01", counties=["Knox"], sample=[], reach="phones on file",
          matched_lots=0, matched_core=0, matched_teardowns=0, matched_tracts=0)
check("Tell us exactly" in bl.pitch(bx), "no matched lots: the pitch asks instead of claiming zero")
bx["matched_lots"] = 41
check("41 lots" in bl.pitch(bx) and "$35,000" in bl.pitch(bx), "the pitch quotes their own record")
meta = {"name": "Knox", "fips": ["47093"], "analyzed": "2026-11-06", "min_buys": 3, "active_months": 24,
        "radius": bl.RADIUS, "fee": 12000, "parcels_on_disk": 10, "new_builds": 5, "lux": bl.LUX, "mid": bl.MID}
a = {"meta": meta, "boxes": [bx], "lots": [{"n_fit": 3, "ref": 40000}], "lots_unmatched": 0, "teardowns": [{}],
     "tracts": [{}], "competitors": [{"name": "SMP HOLDINGS LLC", "quick_flips": 4}],
     "quick_flips": {"n": 5, "sellers": 3, "buy_median": 25000, "markup_median": 15000, "markup_p25": 9000,
                     "markup_p75": 26000, "days_median": 48}, "dropped": {"landlocked": 3}}
a["summary"] = [{"niche": "rural_homesite", "end_user_share": 0.635}]
check(bl.verdict(a).startswith("No.") and "64% still owned" in bl.verdict(a), "one lot builder is not a desk market")
big = dict(a, boxes=[bx] * 20, lots=[{"n_fit": 1}] * 300)
check(bl.verdict(big).startswith("Yes."), "20 lot builders and 300 fitting lots is a desk market")
text = " ".join(str(v) for row in bl.overview_rows(a, {"counties": r}) + bl.caveats(a) for v in row)
check(chr(0x2014) not in text and chr(0x2013) not in text, "no em or en dash in the workbook text")

# ---- house rules on the source itself
src = (Path(__file__).resolve().parents[1] / "src" / "builder_land.py").read_text(encoding="utf-8")
check(chr(0x2014) not in src and chr(0x2013) not in src, "no em or en dash in the module")
check(chr(8) not in src, "no backspace byte (the heredoc word-boundary trap)")
check("add-properties-by-query" not in src, "read-only: the module never pulls records into an account")
print("test_builder_land: %d checks passed" % n)
