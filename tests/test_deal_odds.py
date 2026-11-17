"""Deal odds: the flag time machine, the deal label, the owner on a day, the encoding the box and the
trainer share, and the search-row mapping. Zero network; invented houses, names and places.

    python -m pytest -q tests/test_deal_odds.py
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import deal_odds as do  # noqa: E402
import deal_odds_train as tr  # noqa: E402

DAY = tr.DAY
S = tr.ms(dt.date(2025, 11, 14))
E = S + 182 * DAY
TABLES = {"base": 0.01, "zip": {"37999": 0.5, "37998": -3.0}, "cell": {"35.90,-83.90": 0.2}, "zip_ppsf": {"37999": 150.0}}


def house(**kw):
    p = {"id": 1, "fips": "47093", "zip": "37999", "ls": None, "lp": None, "lt": None, "intra": False, "cash": False,
         "occ": "Yes", "corp": False, "flags": {}}
    p.update(kw)
    return p


# ---------------------------------------------------------------- the time machine
def test_flag_on_reads_a_past_day():
    # The index's own shapes (verified live 2026-11-14): an active flag keeps its added date, a removed
    # flag keeps only its removal date, and one with neither was removed before removals were kept.
    assert tr.flag_on((S - 10 * DAY, None, True), S) == 1.0                # added before, still on
    assert tr.flag_on((S + 10 * DAY, None, True), S) == 0.0                # added after the day
    assert tr.flag_on((None, None, True), S) == 1.0                        # active with no date: on since the first load
    assert tr.flag_on((None, S + 5 * DAY, False), S) == 1.0                # removed after the day: it was on
    assert tr.flag_on((None, S - 5 * DAY, False), S) == 0.0                # removed before the day
    assert tr.flag_on((None, None, False), S) == 0.0


def test_a_removed_flag_is_dropped_by_a_fixed_draw_never_made_fractional():
    # A fraction would mark a removed flag as removed, and removal follows a sale: the tell that made
    # a boosted model look three times better than it was. The draw is fixed per house, flag and day.
    e = (None, S + 30 * DAY, False)
    draws = [tr.flag_on(e, S, q=0.2, key="h%d|is_vacant_property" % i) for i in range(4000)]
    assert set(draws) <= {0.0, 1.0}
    assert 0.76 <= sum(draws) / len(draws) <= 0.84
    assert [tr.flag_on(e, S, q=0.2, key="h7|f") for _ in range(3)] == [tr.flag_on(e, S, q=0.2, key="h7|f")] * 3
    assert tr.flag_on(e, S, q=1.0, key="any") == 0.0


def test_a_flag_cleared_after_a_sale_is_the_sellers_only_inside_the_window():
    sale = S + 60 * DAY
    trait_8mo, trait_10mo = (None, sale + 240 * DAY, False), (None, sale + 300 * DAY, False)
    event_5mo, event_7mo = (None, sale + 150 * DAY, False), (None, sale + 210 * DAY, False)
    assert tr.flag_on(trait_8mo, S, trait=True, sales_after=[sale]) == 1.0     # the seller's trait, clearing late
    assert tr.flag_on(trait_10mo, S, trait=True, sales_after=[sale]) == 0.0    # cleared too long after: the buyer's
    assert tr.flag_on(event_5mo, S, sales_after=[sale]) == 1.0
    assert tr.flag_on(event_7mo, S, sales_after=[sale]) == 0.0
    flip = [sale, sale + 120 * DAY]
    assert tr.flag_on((None, sale + 90 * DAY, False), S, trait=True, sales_after=flip) == 1.0
    assert tr.flag_on((None, sale + 130 * DAY, False), S, trait=True, sales_after=flip) == 0.0   # at the second sale: the middle owner's


def test_a_persons_trait_older_than_the_purchase_is_the_previous_owners():
    old = (S - 400 * DAY, None, True)
    assert tr.flag_on(old, S, owner_since=S - 100 * DAY, trait=True) == 0.0      # a senior flag from the seller, not cleared yet
    assert tr.flag_on(old, S, owner_since=S - 900 * DAY, trait=True) == 1.0
    assert tr.flag_on(old, S, owner_since=S - 100 * DAY, trait=False) == 1.0     # a fact about the house stays
    late = (None, S + 30 * DAY, False)
    assert tr.flag_on(late, S, owner_since=S - 100 * DAY, trait=True) == 0.0     # a recent buyer: the seller's flag clearing late
    assert tr.flag_on(late, S, owner_since=S - 900 * DAY, trait=True) == 1.0


def test_to_ms_reads_every_date_shape():
    assert tr.to_ms(1700000000000) == 1700000000000
    assert tr.to_ms("1700000000000") == 1700000000000
    assert tr.to_ms("2025-11-14") == S
    assert tr.to_ms("2025-11-14T00:00:00Z") == S
    assert tr.to_ms("") is None and tr.to_ms(None) is None and tr.to_ms("soon") is None


# ---------------------------------------------------------------- the label
def test_a_holding_label_in_the_window_is_a_deal():
    p = house(ls=S + 30 * DAY, lt="Pending", lp=90_000)
    assert tr.acquisitions(p, [], S, E, 150_000) == [S + 30 * DAY]
    assert tr.acquisitions(house(ls=S - 30 * DAY, lt="Pending", lp=90_000), [], S, E, 150_000) == []
    assert tr.acquisitions(house(ls=S + 30 * DAY, lt="Pending", lp=90_000, intra=True), [], S, E, 150_000) == []
    assert tr.acquisitions(house(ls=S + 30 * DAY, lt="Pending", lp=4_000), [], S, E, 150_000) == []


def test_a_flip_exit_dates_the_deal_at_the_buy_before_it():
    buy = {"date": S + 20 * DAY, "amt": 80_000, "cash": True, "buyer": "DOE JANE", "buyer2": "", "seller": "ROE RICHARD", "seller2": ""}
    p = house(ls=S + 150 * DAY, lt="Flip", lp=200_000)
    assert tr.acquisitions(p, [buy], S, E, 150_000) == [S + 20 * DAY]


def test_a_company_buyer_counts_and_a_bank_or_a_family_does_not():
    llc = {"date": S + 40 * DAY, "amt": 70_000, "cash": True, "buyer": "ACME HOLDINGS LLC", "buyer2": "", "seller": "ROE RICHARD", "seller2": ""}
    bank = dict(llc, buyer="FIRST EXAMPLE BANK NATIONAL ASSOCIATION")
    trust = dict(llc, buyer="ROE FAMILY TRUST")
    kin = dict(llc, buyer="ROE PROPERTIES LLC")                      # the seller's own name on the company: a family move
    assert tr.acquisitions(house(), [llc], S, E, 150_000) == [S + 40 * DAY]
    assert tr.acquisitions(house(), [bank], S, E, 150_000) == []
    assert tr.acquisitions(house(), [trust], S, E, 150_000) == []
    assert tr.acquisitions(house(), [kin], S, E, 150_000) == []


def test_a_cash_buyer_who_never_moved_in_counts_only_at_a_discount():
    cheap = house(ls=S + 60 * DAY, lp=100_000, cash=True, occ="No")
    retail = house(ls=S + 60 * DAY, lp=145_000, cash=True, occ="No")
    lives = house(ls=S + 60 * DAY, lp=100_000, cash=True, occ="Yes")
    assert tr.acquisitions(cheap, [], S, E, 150_000) == [S + 60 * DAY]
    assert tr.acquisitions(retail, [], S, E, 150_000) == []
    assert tr.acquisitions(lives, [], S, E, 150_000) == []


def test_a_quick_resale_for_a_quarter_more_is_a_flip_by_a_person():
    a = {"date": S + 10 * DAY, "amt": 100_000, "cash": False, "buyer": "DOE JANE", "buyer2": "", "seller": "ROE RICHARD", "seller2": ""}
    b = {"date": S + 200 * DAY, "amt": 130_000, "cash": False, "buyer": "POE PAT", "buyer2": "", "seller": "DOE JANE", "seller2": ""}
    slow = dict(b, date=S + 700 * DAY)
    assert tr.acquisitions(house(), [a, b], S, E, 150_000) == [S + 10 * DAY]
    assert tr.acquisitions(house(), [a, slow], S, E, 150_000) == []


def test_a_quick_resale_between_investors_is_not_a_deal_we_could_have():
    llc_buys = {"date": S + 40 * DAY, "amt": 70_000, "cash": True, "buyer": "ACME HOLDINGS LLC", "buyer2": "",
                "seller": "BETA VENTURES LLC", "seller2": ""}
    flipper = house(ls=S + 40 * DAY)              # the owner on the day bought eight months earlier, with cash
    before = {"date": S - 240 * DAY, "amt": 50_000, "cash": True, "buyer": "BETA VENTURES LLC", "buyer2": "", "seller": "ROE RICHARD", "seller2": ""}
    assert tr.acquisitions(flipper, [before, llc_buys], S, E, 150_000)       # an investor did buy it
    assert tr.deal_in(flipper, [before, llc_buys], S, E, 150_000) is False   # from an investor who just bought it
    landlord = dict(before, date=S - 9 * 365 * DAY)                          # a landlord who held nine years
    assert tr.deal_in(flipper, [landlord, llc_buys], S, E, 150_000) is True


def test_a_failed_listing_and_being_on_the_market_are_read_for_the_day():
    owner = S - 5 * 365 * DAY
    failed = [(S - 300 * DAY, S - 200 * DAY, "Fail")]
    assert tr.listing_on(failed, S, owner) == (False, True)
    assert tr.listing_on([(S - 900 * DAY, S - 800 * DAY, "Fail")], S, owner) == (False, False)      # over two years ago
    assert tr.listing_on([(owner - 400 * DAY, owner - 300 * DAY, "Fail")], S, owner) == (False, False)  # the previous owner's
    assert tr.listing_on([(S - 30 * DAY, None, "Active")], S, owner) == (True, False)              # on the market that day
    assert tr.listing_on([(S - 30 * DAY, S + 40 * DAY, "Sold")], S, owner) == (True, False)        # it sold off a listing open that day
    assert tr.listing_on(failed + [(S - 100 * DAY, S - 50 * DAY, "Sold")], S, owner) == (False, False)


def test_debt_on_a_day_is_the_latest_loan_paid_down_to_its_due_date():
    owner = S - 10 * 365 * DAY
    loan = (owner, 200_000.0, owner + int(30 * 365.25 * DAY), "first")
    left = tr.debt_on([loan], S, owner)
    assert 130_000 < left < 140_000                                          # a third of the term paid
    refi = (S - 365 * DAY, 150_000.0, S - 365 * DAY + int(30 * 365.25 * DAY), "first")
    assert 140_000 < tr.debt_on([loan, refi], S, owner) < 150_000             # the refinance replaced the first loan
    assert tr.debt_on([], S, owner) == 0.0                                    # no loan since the purchase: free and clear
    assert tr.debt_on([(owner - 20 * 365 * DAY, 90_000.0, owner + 10 * 365 * DAY, "first")], S, owner) == 0.0  # the seller's loan
    assert tr.debt_on([loan], S, None) > 0                                    # an owner from before the records: every loan on record


def test_the_box_reads_debt_and_a_failed_listing_off_the_row():
    f = do.facts_from_search_row({"estimatedValue": 100000.0, "equityPercent": 100, "mlsCancelled": True, "address": "1 X St, Y, TN 37999"})
    assert f["ltv"] == 0.0 and f["failed_listing"] is True
    x = do.encode(f, TABLES)
    assert x["ltv:free_clear"] == 1 and x["mls:failed_listing"] == 1
    assert do.ltv_from_equity(40) == 0.6 and do.ltv_from_equity(-20) == 1.2 and do.ltv_from_equity(None) is None


# ---------------------------------------------------------------- the owner on a day
def test_the_owner_on_a_day_is_read_off_the_sale_rows_for_every_house():
    bought = {"date": S - 3 * 365 * DAY, "amt": 90_000, "cash": True, "buyer": "ACME HOLDINGS LLC", "buyer2": "",
              "seller": "ROE RICHARD", "seller2": ""}
    p = house(ls=S - 3 * 365 * DAY, cash=False, corp=False)              # DataFlik's fields say otherwise; the sale row wins
    assert tr.owner_at(p, [bought], S) == {"date": bought["date"], "cash": True, "corp": True}
    sold_after = house(ls=S + 50 * DAY, cash=True, corp=True)
    before = {"date": S - 9 * 365 * DAY, "amt": 60_000, "cash": False, "buyer": "DOE JANE", "buyer2": "", "seller": "", "seller2": ""}
    o = tr.owner_at(sold_after, [before, dict(bought, date=S + 50 * DAY)], S)
    assert o["date"] == before["date"] and o["cash"] is False and o["corp"] is False
    assert tr.owner_at(house(ls=None, corp=True), [], S) == {"date": None, "cash": False, "corp": False}   # owned since before the records
    assert tr.owner_at(p, [], S)["date"] is None                          # no sale row: read as unknown for every house alike


# ---------------------------------------------------------------- the shared encoding
def test_encode_bands_and_area():
    f = {"flags": {"is_vacant_property", "is_free_and_clear", "not_a_flag"}, "corporate": True, "cash": False, "value": 120_000,
         "sqft": 1600, "beds": 3, "baths": 1, "lot_acres": 0.3, "tenure_years": 25, "year_built": 1955, "zip": "37999",
         "lat": 35.905, "lon": -83.895, "fips": "47093"}
    x = do.encode(f, TABLES)
    assert x["flag:is_vacant_property"] == 1 and "flag:not_a_flag" not in x
    assert "flag:is_free_and_clear" not in x                          # a flag whose past cannot be rebuilt is never encoded
    assert x["owner:corporate"] == 1 and "owner:cash_purchase" not in x
    assert x["value:75_125k"] == 1 and x["tenure:20_30"] == 1 and x["built:1940s_50s"] == 1 and x["lot:025_05"] == 1
    assert x["beds:3"] == 1 and x["baths:le1"] == 1 and x["county:knox"] == 1
    assert x["area:zip"] == 0.5 and x["area:cell"] == 0.2
    assert abs(x["ppsf:low"] - (-math.log(75 / 150))) < 1e-9          # $75 a square foot against a $150 ZIP
    clip = do.encode(dict(f, zip="37998"), TABLES)
    assert clip["area:zip"] == -2.0 and not any(k.startswith("ppsf:") for k in clip)   # clipped; no ZIP median: typical
    blank = do.encode({}, TABLES)
    assert blank["value:unknown"] == 1 and blank["tenure:unknown"] == 1
    # A blank size reads as the typical house: a blank is filled in after a sale, so "unknown" would leak it.
    assert blank["built:1960s_70s"] == 1 and blank["lot:lt025"] == 1 and blank["beds:3"] == 1 and blank["baths:2"] == 1


def test_the_flags_whose_past_cannot_be_rebuilt_are_never_learned_from():
    x = do.encode({"flags": set(do.UNTESTABLE_FLAGS)}, TABLES)
    assert not any(k.startswith("flag:") for k in x)
    assert set(do.MODEL_FLAGS) | set(do.UNTESTABLE_FLAGS) == set(do.FLAG_KEYS)
    assert "is_vacant_property" in do.MODEL_FLAGS and "is_senior_homeowners" not in do.MODEL_FLAGS
    past = do.encode({"flags": {"is_vacant_property": 0.8, "is_probate_properties": 0.0}}, TABLES)
    assert past["flag:is_vacant_property"] == 0.8 and "flag:is_probate_properties" not in past


def test_owner_prob_and_prob_and_reasons():
    assert abs(do.owner_prob([0.1, 0.2]) - 0.28) < 1e-12 and do.owner_prob([]) == 0.0
    model = {"intercept": -4.0, "weights": {"flag:is_vacant_property": 1.2, "value:75_125k": 0.3, "value:250_350k": -0.4},
             "band_ref": {"value": 0.05}, "tables": TABLES}
    f = {"flags": {"is_vacant_property"}, "value": 100_000}
    p = do.prob(model, f)
    assert abs(p - do.sigmoid(-4.0 + 1.2 + 0.3)) < 1e-12
    why = do.reasons(model, f)
    assert why[0].startswith("vacant") and len(why) == 2              # the value band raises it over the average band too
    assert do.reasons(model, {"value": 300_000}) == []


def test_the_search_row_reads_as_facts():
    row = {"distressors": {"is_vacant_property": {"is_active": True}, "is_probate_properties": {"is_active": False}},
           "presets": {"free_clear": True, "cash_buyer": True}, "absenteeOwner": True, "corporateOwned": False,
           "estimatedValue": 140000.0, "squareFeet": 1400.0, "bedrooms": 3, "bathrooms": 2, "lotAcres": 0.2,
           "address": "100 Example Way, Knoxville, TN 37999", "latitude": 35.9, "longitude": -83.9, "fips": "47093"}
    f = do.facts_from_search_row(row)
    assert f["flags"] == {"is_vacant_property", "is_free_and_clear", "is_absentee_owners"}
    assert f["cash"] is True and f["zip"] == "37999" and f["value"] == 140000.0 and f["tenure_years"] is None
    slim = {"v": 140000.0, "flags": ["is_vacant_property"], "abs": True, "fc": True, "corp": True, "addr": "1 X St, Y, TN 37999",
            "sq": 1400, "bd": 3, "ba": 2, "lot": 0.2, "lat": 35.9, "lon": -83.9, "fips": "47093", "ten": 12.0, "yb": 1960}
    g = do.facts_from_search_row(slim)
    assert g["flags"] == {"is_vacant_property", "is_absentee_owners", "is_free_and_clear"} and g["corporate"] is True
    assert g["tenure_years"] == 12.0 and g["year_built"] == 1960 and g["zip"] == "37999"
    banded = do.with_bands(f, "10_20", "1960s_70s")
    assert banded["tenure_years"] == 15.0 and banded["year_built"] == 1970.0
    assert do.with_bands(f, "30_plus")["tenure_years"] == 30


def test_the_shipped_model_holds_no_personal_data():
    p = do.MODEL_PATH
    if not p.exists():
        return
    m = json.loads(p.read_text(encoding="utf-8"))
    for key in ("intercept", "weights", "tables", "test", "band_ref", "window_days"):
        assert key in m, key
    assert all(len(z) == 5 and z.isdigit() for z in m["tables"]["zip"])
    assert all("," in c for c in m["tables"]["cell"])
    raw = p.read_text(encoding="utf-8")
    import re
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", raw), "a uuid in the public model file"
    assert not re.search(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b", raw), "a phone number in the public model file"
    assert "@" not in raw


def test_source_hygiene():
    for name in ("deal_odds.py", "deal_odds_train.py"):
        s = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert "\b" not in s and chr(0x2014) not in s and chr(0x2013) not in s, name


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
