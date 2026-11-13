"""Pure checks for the deal inputs Roma reads from Slack (comp_package): a pasted boundary in every
shape, the street list, the county card notes, and the SiftMap deed comps with their read cap.

Zero network: the SiftMap client is a fake. Every street and number is invented.

    python tests/test_wf_deals.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comp_package as cp  # noqa: E402

TESTS = []


def case(fn):
    TESTS.append(fn)
    return fn


@case
def test_box_and_corners():
    b, why = cp.parse_boundary("35.996,36.016,-83.895,-83.840")
    assert not why and b["kind"] == "bbox" and b["bbox"] == (35.996, 36.016, -83.895, -83.84)
    c, _ = cp.parse_boundary("35.996,-83.895 36.016,-83.840")
    assert c["bbox"] == b["bbox"] and c["source"] == "corners"


@case
def test_points_both_orders():
    a, _ = cp.parse_boundary("35.99,-83.90 36.00,-83.88 35.98,-83.87 35.99,-83.90")
    b, _ = cp.parse_boundary("-83.90,35.99 -83.88,36.00 -83.87,35.98")
    assert a["points"] == b["points"] == [(35.99, -83.90), (36.00, -83.88), (35.98, -83.87)]


@case
def test_kml_and_geojson():
    k, _ = cp.parse_boundary("<coordinates>-83.90,35.99,0 -83.88,36.00,0 -83.87,35.98,0</coordinates>")
    g, _ = cp.parse_boundary('{"type":"Polygon","coordinates":[[[-83.90,35.99],[-83.88,36.00],[-83.87,35.98]]]}')
    assert k["source"] == "kml" and g["source"] == "geojson" and k["points"] == g["points"]


@case
def test_unreadable_boundary_is_one_sentence():
    for bad in ("by the school", "35.99,-83.90 36.00", '{"type": "Point", "coordinates": [-83.9, 35.99]}'):
        assert cp.parse_boundary(bad) == (None, cp.BOUNDARY_HELP)
    assert cp.BOUNDARY_HELP.count(".") == 2 and "KML" in cp.BOUNDARY_HELP and "GeoJSON" in cp.BOUNDARY_HELP


@case
def test_card_notes_phrasings():
    assert {k: v for k, v in cp.parse_card_notes("2/1, 1946 sqft, built 1938", 2026).items() if k != "context"} == \
        {"beds": 2, "baths": 1.0, "sqft": 1946, "year_built": 1938}
    assert {k: v for k, v in cp.parse_card_notes("3 bed 2 bath 1,400 sf 1962", 2026).items() if k != "context"} == \
        {"beds": 3, "baths": 2.0, "sqft": 1400, "year_built": 1962}


@case
def test_card_notes_never_guess():
    n = cp.parse_card_notes("3/1, 1,300 sqft, roof was replaced in 2022", 2026)
    assert "year_built" not in n and n["context"] == "roof was replaced in 2022"
    d = cp.parse_card_notes("closing 2/1/2026, 3/2 1400 sf", 2026)
    assert (d["beds"], d["baths"]) == (3, 2.0)


@case
def test_streets():
    assert cp.parse_streets("Old State Rd, Nash Rd") == ("Old State Rd|Nash Rd", "")
    assert cp.parse_streets("bad(regex") == ("", cp.STREETS_HELP)


class Map:
    def __init__(self):
        today = date.today()
        self.rows, self.details, self.reads = [], {}, 0
        for i in range(6):
            rid = "6000%02d" % i
            self.rows.append({"id": rid, "address": "%d Sampleton Ave, Knoxville, TN 37900" % (10 + i),
                              "latitude": 35.96 + 0.001 * i, "longitude": -83.92, "bedrooms": 3, "bathrooms": 1,
                              "squareFeet": 1300, "estimatedValue": 250000, "lotAcres": 0.2})
            self.details[rid] = {"sale_history": [{"sale_date": (today - timedelta(days=30 + i)).isoformat(),
                                                   "sale_price": 240000 if i else 4000, "buyer_name": "A Buyer",
                                                   "seller_name": "A Seller", "is_cash_sale": False}]}

    def search(self, body):
        start = int(body.get("result_index") or 0)
        start = max(0, start - 1) if start else 0          # overlapping pages
        return {"total_results": len(self.rows), "data": self.rows[start:start + int(body.get("size") or 25)]}

    def get_detail(self, pid):
        self.reads += 1
        return self.details[pid]


@case
def test_deed_comps_cap_cache_and_no_double_count():
    m = Map()
    cache = {}
    subj = {"lat": 35.96, "lon": -83.92, "beds": 3, "sqft": 1300}
    r = cp.siftmap_sold(subj, m, detail_cap=3, cache=cache, page_size=2, max_rows=10)
    assert r["read"] == 3 and m.reads == 3 and r["not_read"] == 3
    ids = [c.zpid for c in r["sold"]]
    assert len(ids) == len(set(ids)) and all(c.price >= cp.NOMINAL_SALE for c in r["sold"])
    r2 = cp.siftmap_sold(subj, m, detail_cap=10, cache=cache, page_size=2, max_rows=10)
    assert r2["cached"] == 3 and r2["read"] == 3


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s %s" % (fn.__name__, exc))
    print("\n%d tests %s" % (len(TESTS), "passed" if not failed else "run, %d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
