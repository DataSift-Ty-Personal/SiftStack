"""Zero-network tests for src/buyer_search: the port of the data team's buyer model and each fix,
SiftStack's own version on a user's key (open_api), and the client that marks non-targets. Invented names, streets and ids only (buyer_search/fakes.py).

The PINNED scores are the original Lambda's own output on the in-memory cluster, computed 2026-11-23
by running the unmodified lambda_function.py (dataflik-data-web-service c9df3c67affd) on
fakes.sample_cluster(). compat=True must reproduce them exactly, so the port cannot drift from the model.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from buyer_search import client, engine, fakes  # noqa: E402

PINNED = {
    "subject": {"b1": 69.0, "b2": 46.9, "b4": 86.9},
    "no embedding": {"b1": 69.0, "b2": 47.0, "b4": 86.9},
    "radius 5, weights": {"b1": 80.4, "b2": 48.9, "b4": 86.6},
}
WEIGHTS3 = {"similarity": 0.5, "total_investor_purchases": 0.3, "purchases_radius": 0.2}


def scores(result):
    return {b["buyer_id"]: b["score"] for b in result["buyers"]}


# --------------------------------------------------------------------- the original, reproduced

def test_compat_reproduces_the_original_scores():
    c = fakes.sample_cluster
    assert scores(engine.find_buyers(fakes.SUBJECT_ID, c(), engine.Params(), compat=True)) == PINNED["subject"]
    assert scores(engine.find_buyers(fakes.NO_EMBED_ID, c(), engine.Params(), compat=True)) == PINNED["no embedding"]
    p = engine.Params(radius_miles=5, weights=dict(WEIGHTS3))
    assert scores(engine.find_buyers(fakes.SUBJECT_ID, c(), p, compat=True)) == PINNED["radius 5, weights"]


def test_the_model_constants_are_the_teams():
    assert sum(engine.WEIGHTS.values()) == pytest.approx(1.05)
    assert engine.WEIGHTS["similarity"] == 0.25 and engine.WEIGHTS["total_investor_purchases"] == 0.25
    assert engine.SCALE_MIN_MAX["median_discount"] == [100, 125]
    assert engine.FILL_NA["similarity"] == 0.50
    assert (engine.RADIUS_MILES, engine.PERIOD_DAYS, engine.MIN_BUYER_SCORE, engine.TOP_N_BUYERS) == (25, 730, 10, 100)


def test_score_rounds_like_numpy():
    np = pytest.importorskip("numpy")      # the original rounded with np.round(x, 1)
    vals = [i / 1000 for i in range(0, 100001, 7)] + [46.85, 0.25, 0.35, 12.45, 99.95, 2.675]
    assert all(engine._round1(v) == float(np.round(v, 1)) for v in vals)


# --------------------------------------------------------------------------------- each fix

def test_fix1_period_is_on_the_sale_not_the_previous_sale():
    compat = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params(), compat=True)
    fixed = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params())
    assert compat["counts"]["sales"] == 3          # s1, s5, s7: a previous sale inside two years
    assert fixed["counts"]["sales"] == 10          # every sale inside two years (s11-s14 are out for other reasons)
    assert {"range": {"sale_date": {"gte": "now-730d/d"}}} in fixed["filters"]
    assert not engine.exists_field_in_filter(fixed["filters"], "last_sale_date")
    assert fixed["counts"]["sale_date_first"] <= fixed["counts"]["sale_date_last"]


def test_fix1_a_sale_date_filter_from_the_caller_is_not_doubled():
    p = engine.Params(sales_filters=[{"range": {"sale_date": {"gte": "now-30d/d"}}}])
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), p)
    assert sum(1 for f in r["filters"] if "range" in f and "sale_date" in f["range"]) == 1
    assert r["counts"]["sales"] == 3               # s8, s9, s10


def test_fix2_stats_come_from_the_subject_county():
    compat = {b["buyer_id"]: b for b in engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(),
                                                           engine.Params(), compat=True)["buyers"]}
    fixed = {b["buyer_id"]: b for b in engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(),
                                                          engine.Params())["buyers"]}
    assert compat["b1"]["locality_percentage"] == 25.0     # the neighboring county's row, one of two tied rows
    assert fixed["b1"]["locality_percentage"] == 75.0      # this county's row
    assert fixed["b1"]["stats_county"] == "Sample"
    assert fixed["b4"]["locality_percentage"] == 0.4


def test_fix3_every_answer_is_strict_json():
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params())
    text = json.dumps(r, allow_nan=False)                   # raises on NaN
    city = next(b for b in r["buyers"] if b["buyer_id"] == "b5")
    assert city["similarity"] is None and city["median_discount"] is None
    assert "NaN" not in text


def test_fix4_a_cut_is_reported():
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params(max_sales=3))
    assert r["counts"]["sales"] == 3 and r["counts"]["sales_total"] == 10 and r["counts"]["sales_truncated"]


def test_fix4_searches_are_sorted_and_paged():
    cl = fakes.sample_cluster()
    engine.find_buyers(fakes.SUBJECT_ID, cl, engine.Params())
    sales_calls = [b for ix, b in cl.calls if ix == "sales-backtesting"]
    assert sales_calls and sales_calls[0]["sort"][0] == {"sale_date": {"order": "desc"}}
    assert sales_calls[0]["_source"] == engine.SALE_FIELDS


def test_fix5_network_parameters_are_bounded():
    with pytest.raises(ValueError):
        engine.clean_params({"radius_miles": 80})
    with pytest.raises(ValueError):
        engine.clean_params({"sales_filters": [{"match_all": {}}]})
    with pytest.raises(ValueError):
        engine.clean_params({"weights": {"script": 1}})
    with pytest.raises(ValueError):
        engine.clean_params({"is_investor_sale": ["maybe"]})
    with pytest.raises(ValueError):
        engine.clean_params({"max_sales": 50000})                # trusted-only knob
    p = engine.clean_params({"radius_miles": 80, "sales_filters": [{"match_all": {}}]}, trusted=True)
    assert p.radius_miles == 80 and p.sales_filters == [{"match_all": {}}]
    assert engine.clean_params({"is_investor_sale": "true"}).is_investor_sale == ["true"]


def test_fix6_the_year_band_uses_this_year():
    f = engine.add_filters_for_similar_properties({"year_built": 1900}, [], 2026)
    assert f == [{"range": {"year_built": {"gte": 1869, "lte": 1931}}}]
    f = engine.add_filters_for_similar_properties({"year_built": 1900}, [], 2025)
    assert f == [{"range": {"year_built": {"gte": 1869, "lte": 1931}}}]   # int(31.5) and int(31.25) are both 31
    assert engine.add_filters_for_similar_properties({"year_built": 1990}, [], 2026)[0]["range"]["year_built"] == \
        {"gte": 1980, "lte": 2000}


def test_fix7_the_property_is_read_with_an_ids_query():
    cl = fakes.sample_cluster()
    engine.find_buyers(fakes.SUBJECT_ID, cl, engine.Params())
    first = cl.calls[0]
    assert first[0] == "properties" and first[1]["query"] == {"ids": {"values": [str(fakes.SUBJECT_ID)]}}


def test_fix8_a_zero_profile_proportion_does_not_crash():
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params())
    b3 = next(b for b in r["buyers"] if b["buyer_id"] == "b3")
    assert b3["buyer_size"] is None and b3["profile_size"] == 3


# ------------------------------------------------------------------------------ edges

def test_no_such_property_and_bad_ids():
    assert engine.find_buyers(424242, fakes.sample_cluster())["status"] == 400
    assert engine.find_buyers("not a number", fakes.sample_cluster())["status"] == 400


def test_the_embedding_falls_back_to_the_sold_houses():
    r = engine.find_buyers(fakes.NO_EMBED_ID, fakes.sample_cluster(), engine.Params())
    assert r["subject"]["embedding"] == "sales average"
    assert engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster())["subject"]["embedding"] == "property"


def test_filters_keep_out_far_condo_person_and_old_sales():
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params())
    ids = {b["buyer_id"] for b in r["buyers"]}
    assert "b8" not in ids and "b9" not in ids             # 60 miles away; a person
    assert r["counts"]["buyers"] == 7                       # b1-b7; the condo (s13) and the old sale (s14) are out


def test_storage_failure_is_a_500_not_a_raise():
    class Broken(fakes.FakeCluster):
        def search(self, index, *a, **k):
            if index == "sales-backtesting":
                raise RuntimeError("down")
            return super().search(index, *a, **k)
    r = engine.find_buyers(fakes.SUBJECT_ID, Broken(fakes.sample_docs()), engine.Params())
    assert r["status"] == 500 and r["buyers"] == []


def test_resolve_address_normalizes_the_street():
    cl = fakes.sample_cluster()
    got = engine.resolve_address(cl, "100 Example Street", "37999")
    assert [c["dataflik_id"] for c in got] == [1001] and got[0]["exact"]
    got = engine.resolve_address(cl, "100 Sample Avenue, Sampleton TN", "37999")
    assert got[0]["dataflik_id"] == 1003
    assert engine.street_key("5 North Main Road Apt 2") == "5 n main rd"
    with pytest.raises(ValueError):
        engine.resolve_address(cl, "Example St", "37999")
    with pytest.raises(ValueError):
        engine.resolve_address(cl, "100 Example St", "")


# --------------------------------------------------------------------------- the client

def test_annotate_marks_and_never_drops():
    r = engine.find_buyers(fakes.SUBJECT_ID, fakes.sample_cluster(), engine.Params())
    n = len(r["buyers"])
    client.annotate(r)
    by = {b["buyer_full_name"]: b for b in r["buyers"]}
    assert len(r["buyers"]) == n
    assert by["DEMO MORTGAGE LLC"]["not_a_target"].startswith("lender")
    assert by["CITY OF SAMPLETON"]["not_a_target"].startswith("government")
    assert by["WILLBANKS HOLDINGS LLC"]["not_a_target"] == ""       # BANK is a whole word, not a substring
    assert by["EXAMPLE HOLDINGS LLC"]["target_rank"] == 1
    assert r["counts"]["targets"] == 4


@pytest.mark.parametrize("name,kind", [
    ("OPENDOOR PROPERTY TRUST", "builder"), ("D R HORTON INC", "builder"), ("KNOX COUNTY", "government"),
    ("SECRETARY OF VETERANS AFFAIRS", "government"), ("NEWREZ LLC", "lender"), ("FIRST SAMPLE BANK", "lender"),
    ("WELLS EXAMPLE BANK N A", "lender"), ("HABITAT FOR HUMANITY OF SAMPLE", "not a buyer"),
    ("SAMPLE BANKS FAMILY TRUST", ""), ("COUNTY LINE HOMES LLC", ""), ("DEMO ACQUISITIONS LLC", ""),
    ("SAMPLE ASSEMBLY OF GOD INC", "church"), ("FIRST BAPTIST CHURCH OF SAMPLETON", "church"),
    ("SAMPLE COMMUNITY CHURCH", "church"), ("CHURCH STREET HOLDINGS LLC", ""), ("TEMPLETON HOMES LLC", ""),
])
def test_not_a_target(name, kind):
    got = client.not_a_target(name)
    assert (got.startswith(kind) if kind else got == ""), (name, got)


def test_cluster_runs_ask_for_investor_sales_only():
    assert client.CLUSTER_PARAMS == {"is_investor_sale": ["true"]}
    r = client.search_cluster({"dataflik_id": fakes.SUBJECT_ID}, backend=fakes.sample_cluster())
    assert {b["buyer_id"] for b in r["buyers"]} == {"b1", "b2", "b3", "b7"} and r["source"] == "cluster"


def test_search_cluster_by_address():
    r = client.search_cluster({"address": "100 Example St", "zip": "37999"}, backend=fakes.sample_cluster())
    assert r["dataflik_id"] == fakes.SUBJECT_ID and r["resolved"]["exact"]
    with pytest.raises(client.SearchFailed) as e:
        client.search_cluster({"address": "999 Example St", "zip": "37999"}, backend=fakes.sample_cluster())
    assert e.value.status == 404
    with pytest.raises(client.SearchFailed) as e:
        client.search_cluster({"address": "Example St", "zip": "37999"}, backend=fakes.sample_cluster())
    assert e.value.status == 400


# ------------------------------------------------------- SiftStack's own version (open_api)

from datetime import date  # noqa: E402

from buyer_search import open_api  # noqa: E402

TODAY = date(2026, 11, 23)


@pytest.fixture()
def no_registry(tmp_path, monkeypatch):
    import deal_buyer_match as dbm
    monkeypatch.setattr(dbm, "REG_DIR", tmp_path / "no_registry")    # never the workstation's deed cache
    return tmp_path


def run_open(tmp, sm=None, **kw):
    sm = sm or fakes.sample_siftmap()
    r = open_api.find_buyers(dict(fakes.SIFTMAP_SUBJECT), client=sm, work=tmp / "work", today=TODAY, **kw)
    return client.annotate(r), sm


def test_open_reads_only_houses_like_this_one(no_registry):
    r, sm = run_open(no_registry)
    assert r["status"] == 200
    assert "p4" not in sm.read                         # the $900,000 house is outside the value band: never read
    assert set(sm.read) == {"5000", "p1", "p2", "p3", "r1", "y1", "f1", "c1", "c2"}
    assert r["counts"]["investor_sales_seen"] == 7 and r["counts"]["similar"] == 6


def test_open_read_cap_takes_the_nearest_first(no_registry):
    r, sm = run_open(no_registry, read_cap=3)
    assert sorted(sm.read) == ["5000", "c1", "c2"]     # the house itself, then the two nearest
    assert r["counts"]["deeds_unread"] == 6


def test_open_a_second_run_reuses_deeds_and_reports_fresh_counts(no_registry):
    run_open(no_registry, read_cap=3)
    r, sm = run_open(no_registry, read_cap=150)          # the same work folder
    assert "c1" not in sm.read and len(sm.read) == 6      # only what the first run left unread
    assert r["counts"]["deeds_unread"] == 0 and r["counts"]["deeds_read"] == 8


def test_open_buyers_holders_flippers_and_unlabeled_company_buys(no_registry):
    r, _ = run_open(no_registry)
    by = {b["buyer_full_name"]: b for b in r["buyers"]}
    assert set(by) == {"EXAMPLE HOLDINGS LLC", "SAMPLE HOMES LLC", "DEMO MORTGAGE LLC", "TEST FLIPS LLC",
                       "PLACEHOLDER VENTURES LLC"}
    assert "NEWER HOMES LLC" not in by and "JANE SAMPLE" not in by        # built 1999; a homeowner, not a buyer
    assert by["TEST FLIPS LLC"]["roles"] == ["flipped"]                    # the flip's buyer is the SELLER
    assert by["TEST FLIPS LLC"]["bought_here"][0]["price"] == 110000       # what the flipper paid, not the exit
    assert by["PLACEHOLDER VENTURES LLC"]["roles"] == ["entity buy"]
    assert by["EXAMPLE HOLDINGS LLC"]["purchases_radius"] == 2


def test_open_portfolio_ceiling_and_profile(no_registry):
    r, _ = run_open(no_registry)
    ex = next(b for b in r["buyers"] if b["buyer_full_name"] == "EXAMPLE HOLDINGS LLC")
    assert ex["total_investor_purchases"] == 11 and ex["portfolio_11_or_more"]
    assert ex["profile_size"] == 13                     # 2 bought here and 11 held
    assert ex["locality_percentage"] == round(100 * 2 / 11, 2)
    assert 0 < ex["similarity"] <= 1
    assert ex["buyer_city"] == "Sampleton" and ex["buyer_state"] == "TN" and ex["buyer_zip"] == "37999"


def test_open_scores_with_the_models_own_math(no_registry):
    r, _ = run_open(no_registry)
    comps = [{k: b.get(k) for k in engine.WEIGHTS} for b in r["buyers"]]
    assert [b["score"] for b in r["buyers"]] == engine.score_buyers(comps, engine.WEIGHTS)
    assert [b["score"] for b in r["buyers"]] == sorted((b["score"] for b in r["buyers"]), reverse=True)


def test_price_guess_blends_each_buyers_deeds_with_the_area(no_registry):
    r, _ = run_open(no_registry)
    r = open_api.price_guesses(r)
    by = {b["buyer_full_name"]: b for b in r["buyers"]}
    ex = by["EXAMPLE HOLDINGS LLC"]                      # $140,000 of $210,000 and $150,000 of $240,000
    assert ex["paid_shares"] == [0.6667, 0.625] and ex["price_share"] == 64.6 and ex["priced_purchases"] == 2
    assert ex["median_paid"] == 145000
    # the area's typical (62.5%) counts as one more deed: (66.67 + 62.5 + 62.5) / 3 = 63.9% of $200,000
    assert ex["likely_share"] == 63.9 and ex["likely_price"] == 128000
    flip = by["TEST FLIPS LLC"]                          # a flip: the buy over the RESALE, both on the deeds
    assert flip["price_share"] == 44.9 and flip["likely_price"] == 107000
    assert flip["priced_deeds"] == [{"address": "170 Example St, Sampleton, TN 37999", "date": "2025-10-01",
                                     "price": 110000, "worth": 245000, "basis": "resale"}]
    assert by["SAMPLE HOMES LLC"]["likely_price"] == 139000 and by["PLACEHOLDER VENTURES LLC"]["likely_price"] == 124000
    assert ex["paid_bases"] == ["value", "value"] and flip["paid_bases"] == ["resale"]
    pr = r["pricing"]
    assert pr["value"] == 200000 and pr["arv"] is None
    assert (pr["holder_share"], pr["holder_deals"], pr["flip_share"], pr["flip_deals"]) == (64.6, 4, 44.9, 1)
    # the buyers to call only: the lender's $120,000 deed stays out of the $125,000 median
    assert pr["usual_price"] == 125000 and pr["priced_purchases"] == 5
    assert (pr["low"], pr["high"], pr["buyers_priced"]) == (107000, 139000, 4)
    json.dumps(r, allow_nan=False)


def test_an_arv_prices_the_flips_and_never_the_holders(no_registry):
    """A holder who paid 65% of a house as it stood did not pay 65% of a fixed-up house: on 3014 Sampleville (11-23)
    pricing every deal against the $225,000 ARV instead of the $142,000 estimate put the buyers at $195,000."""
    r, _ = run_open(no_registry)
    r = open_api.price_guesses(r, 300000)
    by = {b["buyer_full_name"]: b["likely_price"] for b in r["buyers"]}
    assert by["TEST FLIPS LLC"] == 134000                 # (44.9% of $300,000 + the $133,340 median) / 2
    assert by["EXAMPLE HOLDINGS LLC"] == 131000           # its deeds still price against the $200,000 estimate
    assert r["pricing"]["arv"] == 300000 and r["pricing"]["usual_price"] == 133000


def test_price_guess_capped_and_for_a_buyer_with_no_price():
    r = {"subject": {"value": 200000}, "buyers": [
        {"buyer_full_name": "A", "paid_shares": [0.646], "paid_bases": ["value"]},
        {"buyer_full_name": "B", "paid_shares": [1.095], "paid_bases": ["value"]},    # counts at most at 100%
        {"buyer_full_name": "C", "paid_shares": []},
        {"buyer_full_name": "D", "paid_shares": [0.4], "paid_bases": ["resale"]}]}
    open_api.price_guesses(r, 300000)
    # deals for this house: A $129,200, B $200,000 (capped), D 40% of the $300,000 ARV = $120,000; median $129,200
    assert [b["likely_price"] for b in r["buyers"]] == [129000, 165000, 129000, 125000]
    assert r["pricing"]["usual_price"] == 129000
    open_api.price_guesses(r)                                    # no ARV: D's flip prices against the estimate
    assert [b["likely_price"] for b in r["buyers"]] == [129000, 165000, 129000, 105000]
    r = {"subject": {"value": None}, "buyers": [{"paid_shares": [0.646], "paid_bases": ["value"]}]}
    open_api.price_guesses(r)
    assert r["buyers"][0]["likely_price"] is None and r["pricing"]["value"] is None
    open_api.price_guesses(r, 250000)                            # no estimate: the ARV stands in for it
    assert r["buyers"][0]["likely_price"] == 162000


def test_priced_deed_drops_paper_and_portfolio_deeds():
    houses = {"h": {"value": 200000}}
    assert open_api.priced_deed({"id": "h", "price": 150000, "role": "holder"}, houses) == {
        "share": 0.75, "price": 150000, "worth": 200000, "basis": "value"}
    assert open_api.priced_deed({"id": "h", "price": 5000, "role": "holder"}, houses) is None       # a paper deed
    assert open_api.priced_deed({"id": "h", "price": 30000, "role": "holder"}, houses) is None      # under a fifth
    assert open_api.priced_deed({"id": "h", "price": 450000, "role": "holder"}, houses) is None     # carried others
    flip = open_api.priced_deed({"id": "h", "price": 100000, "role": "flipped", "exit_price": 250000}, houses)
    assert flip["share"] == 0.4 and flip["basis"] == "resale"
    assert open_api.priced_deed({"id": "h", "price": 100000, "role": "flipped", "exit_price": 0}, houses)["share"] == 0.5
    assert open_api.priced_deed({"id": "x", "price": 100000, "role": "holder"}, houses) is None      # no value


def test_the_houses_own_sale_is_no_buyers_purchase(no_registry):
    """3014 Sampleville's own August sale listed the company selling it as a buyer to call and priced its deal in."""
    sm = fakes.sample_siftmap()
    sm.investor["pending"].append(fakes._row("5000", "100 Example St, Sampleton, TN 37999", 36.0005, -84.0,
                                             sqft=1300, value=200000))
    sm.details["5000"] = fakes._deed("OUR TEST HOMES LLC",
                                     [("2026-08-28", 91250, "OUR TEST HOMES LLC", "A PRIOR OWNER", True)], held=1)
    r, _ = run_open(no_registry, sm)
    names = {b["buyer_full_name"] for b in r["buyers"]}
    assert "OUR TEST HOMES LLC" not in names and "EXAMPLE HOLDINGS LLC" in names
    by_street = dict(fakes.SIFTMAP_SUBJECT, dataflik_id=None)                 # a record with no SiftMap id
    assert open_api.is_subject("x9", "100 Example Street, Sampleton, TN 37999", by_street)
    assert not open_api.is_subject("x9", "102 Example St, Sampleton, TN 37999", by_street)


def test_the_installs_own_company_is_set_aside(monkeypatch):
    monkeypatch.setenv("AGENTS_OWN_BUYER_NAMES", "Sample Home Buyers, Demo Homebuyers")
    assert client.not_a_target("SAMPLE HOME BUYERS GROUP LLC") == "our own company (Sample Home Buyers)"
    assert client.not_a_target("Demo Homebuyers LLC") == "our own company (Demo Homebuyers)"
    assert client.not_a_target("SAMPLE HOMES LLC") == ""                       # whole words only
    monkeypatch.delenv("AGENTS_OWN_BUYER_NAMES")
    assert client.not_a_target("SAMPLE HOME BUYERS GROUP LLC") == ""


def test_a_buyer_with_no_usable_price_says_why(no_registry):
    houses = {"h": {"value": 200000}, "k": {"value": 210000}}
    one = {"buyer": "SAMPLE PORTFOLIO LLC", "id": "h", "price": 1500000, "date": "2026-04-20", "role": "holder"}
    rows = open_api.buyer_rows(dict(fakes.SIFTMAP_SUBJECT), [one], {}, houses, 24)
    assert rows[0]["priced_purchases"] == 0 and rows[0]["unpriced"] == "their deed covered several houses"
    zero = dict(one, buyer="SAMPLE ZERO LLC", id="k", price=0)
    assert open_api.buyer_rows(dict(fakes.SIFTMAP_SUBJECT), [zero], {}, houses, 24)[0]["unpriced"] == \
        "their deed shows no price"
    r = open_api.price_guesses({"subject": {"value": 200000}, "buyers": rows + [
        {"buyer_full_name": "A", "paid_shares": [0.6]}]})
    assert r["buyers"][0]["likely_price"] == 120000            # no price of their own: the area's typical


def test_priced_deed_trusts_a_recent_sale_over_a_low_estimate():
    """Seen live: a buyer paid $250,000 for a house that had sold for $252,000 a year earlier and is estimated at
    $193,000 today, so it read as 130% of its worth."""
    houses = {"h": {"value": 193000}}
    det = {"h": {"sale_history": [{"date": "2025-08-25", "price": 250000}, {"date": "2024-10-18", "price": 252000},
                                  {"date": "2021-10-27", "price": 125000}]}}
    d = open_api.priced_deed({"id": "h", "price": 250000, "role": "holder", "date": "2025-08-25"}, houses, det)
    assert d["basis"] == "prior sale" and d["worth"] == 252000 and round(d["share"], 3) == 0.992
    old = {"h": {"sale_history": [{"date": "2025-08-25", "price": 250000}, {"date": "2019-10-18", "price": 252000}]}}
    d = open_api.priced_deed({"id": "h", "price": 250000, "role": "holder", "date": "2025-08-25"}, houses, old)
    assert d["basis"] == "value" and d["worth"] == 193000                  # six years back says nothing about today
    lower = {"h": {"sale_history": [{"date": "2025-08-25", "price": 150000}, {"date": "2024-10-18", "price": 120000}]}}
    d = open_api.priced_deed({"id": "h", "price": 150000, "role": "holder", "date": "2025-08-25"}, houses, lower)
    assert d["basis"] == "value"                                           # a lower sale never lowers the worth


def test_open_marks_the_lender_and_answers_strict_json(no_registry):
    r, _ = run_open(no_registry)
    lender = next(b for b in r["buyers"] if b["buyer_full_name"] == "DEMO MORTGAGE LLC")
    assert lender["not_a_target"].startswith("lender") and lender["target_rank"] is None
    assert r["counts"]["targets"] == 4
    json.dumps(r, allow_nan=False)


def test_open_needs_a_map_point(no_registry):
    s = dict(fakes.SIFTMAP_SUBJECT, lat=None)
    r = open_api.find_buyers(s, client=fakes.sample_siftmap(), work=no_registry / "w", today=TODAY)
    assert r["status"] == 400


def test_open_siftmap_failure_is_a_502_not_a_raise(no_registry):
    class Down(fakes.FakeSiftMap):
        def search(self, body):
            raise RuntimeError("HTTP 503")
    r = open_api.find_buyers(dict(fakes.SIFTMAP_SUBJECT), client=Down({}, [], [], {}), work=no_registry / "w",
                             today=TODAY)
    assert r["status"] == 502 and r["buyers"] == []


def test_subject_by_address_uses_the_map_not_the_ignored_address_search():
    sm = fakes.sample_siftmap()
    s, why = open_api.subject_from_siftmap(sm, "100 Example Street, Sampleton, TN 37999",
                                           geocode=lambda a: (36.0, -84.0))
    assert s and s["dataflik_id"] == "5000" and s["lot_sf"] == round(0.24 * 43560), why
    assert all("addresses" not in b for b in sm.bodies)          # a polygon around the geocoded point
    s, why = open_api.subject_from_siftmap(sm, "104 Example St, Sampleton, TN 37999",
                                           geocode=lambda a: (36.0, -84.0))
    assert s is None and "104" in why
    s, why = open_api.subject_from_siftmap(sm, "Example St", geocode=lambda a: (36.0, -84.0))
    assert s is None and "house number" in why
    s, why = open_api.subject_from_siftmap(sm, "100 Example St", geocode=lambda a: None)
    assert s is None and "geocoder" in why


def test_search_open_end_to_end(no_registry):
    r = client.search_open(address="100 Example St, Sampleton, TN 37999", client=fakes.sample_siftmap(),
                           work=no_registry / "w", geocode=lambda a: (36.0, -84.0))
    assert r["source"] == "siftmap" and r["counts"]["targets"] == 4
    with pytest.raises(client.SearchFailed) as e:
        client.search_open(address="104 Example St, Sampleton, TN 37999", client=fakes.sample_siftmap(),
                           work=no_registry / "w2", geocode=lambda a: (36.0, -84.0))
    assert e.value.status == 404


def test_bands_match_the_models():
    b = open_api.bands(fakes.SIFTMAP_SUBJECT, 2026)
    assert b["value"] == (100000.0, 300000.0) and b["beds"] == (2.0, 4.0) and b["sqft"] == (650.0, 1950.0)
    assert b["year_built"] == (1944.0, 1976.0)
    f = engine.add_filters_for_similar_properties({"property_value": 200000, "bedroom_count": 3, "property_sqft": 1300,
                                                   "year_built": 1960}, [], 2026)
    assert f[0]["range"]["property_value"] == {"gte": 100000, "lte": 300000}
    assert f[-1]["range"]["year_built"] == {"gte": 1944, "lte": 1976}


def test_hydrate_keeps_the_owners_houses_without_the_lot():
    import dispo_buyers

    class One:
        def get_detail(self, rid):
            return {"owner_info": {"owner_name": "EXAMPLE HOLDINGS LLC", "total_properties": 11,
                                   "owner_properties": [{"bedrooms": 3, "bathrooms": 1, "estimatedValue": 190000,
                                                         "lastSaleAmount": 120000, "squareFeet": 11000,
                                                         "address": "1 Example St"}]},
                    "sale_history": [], "years_built": 1960}
    d = dispo_buyers._hydrate_one(One(), "x1")
    assert d["owner_properties"] == [{"beds": 3, "baths": 1, "value": 190000, "paid": 120000}]


def test_no_backspace_or_dash_in_the_package():
    root = Path(__file__).resolve().parent.parent / "src"
    for p in list((root / "buyer_search").glob("*.py")) + [root / "buyer_search_cli.py",
                                                         root / "agents" / "workflows" / "buyer_search.py"]:
        text = p.read_text(encoding="utf-8")
        assert "\x08" not in text and chr(0x2014) not in text and chr(0x2013) not in text, p
