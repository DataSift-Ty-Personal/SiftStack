"""dispo_buyers: the registry reads deeds as it always has unless the taught rules are asked for
by name, and each taught rule does exactly one thing.

Zero network. Invented companies, people, streets, prices and dates.

    institutions by count    100 or more purchases in six months, whatever the name
    a flip                   a purchase resold within 18 months
    back to back             two companies on consecutive deeds: a wholesaler, then the buyer
    a loan for a price       where the deed carries none
    new construction         a "flip" of a house built after 2010 is a builder's sale

Run: python tests/test_dispo_buyers_rules.py   (or pytest)
"""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dispo_buyers as D  # noqa: E402

TODAY, TAUGHT = D.rules_for("today"), D.rules_for("taught")
LEGACY_KEYS = {"name", "mail", "county", "txn", "role", "prop", "date", "price", "cash", "portfolio_n",
               "portfolio_value", "saved_uuid", "secondary"} | set(D.PHYS_FIELDS)


def seg(txn, *ids):
    return {"Alder:%s" % txn: [{"id": i, "address": "%s Example St, Alderton, ZZ" % i} for i in ids]}


def deed(day, price, seller, buyer, cash=False):
    return {"date": day, "price": price, "cash": cash, "seller": seller, "buyer": buyer}


def one(rows, details, rules=None, stats=None):
    return D._observations(rows, details, rules, stats)


def by_name(obs):
    return {o["name"]: o for o in obs}


# --------------------------------------------------------------------------- today is the default
def test_today_is_the_default_and_reads_every_deed_as_before():
    assert D.DEFAULT_RULES == "today" and D.rules_name() == "today"
    assert not any(TODAY[k] for k in D.RULESETS["taught"])                # every rule is off today
    details = {
        # held nine years, then sold: today this is still read as an exited flip
        "p1": {"owner_name": "Pat Homebuyer", "year_built": 2019, "sale_history": [
            deed("2026-05-01", 300000, "Cedar Rentals LLC", "Pat Homebuyer"),
            deed("2017-04-01", 90000, "Old Owner", "Cedar Rentals LLC")]},
        # a purchase with no price and a loan recorded with it: today the price stays unknown
        "p2": {"owner_name": "Birch Holdings LLC", "owner_mail": "1 Office Rd", "sale_history": [
            deed("2026-03-10", 0, "Quick Contracts LLC", "Birch Holdings LLC"),
            deed("2026-03-08", 70000, "Sam Seller", "Quick Contracts LLC")],
            "mortgages": [{"date": "2026-03-11", "amount": 96000, "borrower": "Birch Holdings LLC", "lender": "A Bank"}]},
    }
    rows = dict(seg("flip", "p1"), **seg("pending", "p2"))
    obs = one(rows, details)
    assert obs == one(rows, details, TODAY)
    assert len(obs) == 2 and {o["role"] for o in obs} == {"exited", "holder"}
    flip, hold = by_name(obs)["Cedar Rentals LLC"], by_name(obs)["Birch Holdings LLC"]
    assert flip["price"] == 90000 and hold["price"] == 0
    assert set(hold) == LEGACY_KEYS and set(flip) == LEGACY_KEYS | {"buy_date", "exit_price"}   # no new key on any row


def test_the_ruleset_is_chosen_by_name_only():
    assert D.rules_for("taught") == dict(D.RULESETS["taught"], name="taught")
    os.environ[D.RULES_ENV] = "taught"
    try:
        assert D.rules_name() == "taught"
        D._ACTIVE["name"] = "today"                                       # the command line outranks the environment
        assert D.rules_name() == "today"
    finally:
        D._ACTIVE["name"] = ""
        os.environ.pop(D.RULES_ENV, None)
    assert D.rules_name() == "today"
    try:
        D.rules_for("strict")
        raise AssertionError("an unknown ruleset was accepted")
    except ValueError as e:
        assert "today" in str(e) and "taught" in str(e)


# ------------------------------------------------------------------------- a flip is 18 months
def test_a_flip_is_a_purchase_resold_within_18_months():
    assert D.is_flip("2026-01-15", "2026-06-01", 18) is True
    assert D.is_flip("2024-06-01", "2025-11-30", 18) is True              # 547 days
    assert D.is_flip("2024-06-01", "2025-12-03", 18) is False             # 550 days
    assert D.is_flip(None, "2026-06-01", 18) is None and D.is_flip("2026-06-01", "", 18) is None
    assert D.is_flip("2026-06-01", "2026-01-01", 18) is None              # a purchase after the sale is not a hold
    details = {
        "p1": {"owner_name": "Pat Homebuyer", "sale_history": [
            deed("2026-06-01", 310000, "Fast Flips LLC", "Pat Homebuyer"),
            deed("2026-01-15", 140000, "Old Owner", "Fast Flips LLC", cash=True)]},
        "p2": {"owner_name": "Sam Homebuyer", "sale_history": [
            deed("2026-05-01", 300000, "Cedar Rentals LLC", "Sam Homebuyer"),
            deed("2017-04-01", 90000, "Old Owner", "Cedar Rentals LLC")]},
        "p3": {"owner_name": "Lee Homebuyer", "sale_history": [
            deed("2026-04-01", 280000, "Unknown Start LLC", "Lee Homebuyer")]},
    }
    stats = {}
    obs = by_name(one(seg("flip", "p1", "p2", "p3"), details, TAUGHT, stats))
    assert obs["Fast Flips LLC"]["role"] == "exited" and obs["Fast Flips LLC"]["days_held"] == 137
    assert obs["Cedar Rentals LLC"]["role"] == "long_hold" and obs["Cedar Rentals LLC"]["days_held"] > 3000
    # with no purchase deed to measure from, the label is all there is, so the flip stands
    assert obs["Unknown Start LLC"]["role"] == "exited" and obs["Unknown Start LLC"]["days_held"] is None
    assert stats["flips_checked"] == 3 and stats["long_holds"] == 1
    # the long hold is still a purchase that buyer made, but its 2017 price stays out of any band
    prices, _ = D._real_prices(list(obs.values()))
    assert prices == [140000]
    assert D._real_prices(one(seg("flip", "p1", "p2", "p3"), details, TODAY))[0] == [140000, 90000]


# ------------------------------------------------------------- back to back: a wholesaler, a buyer
def test_two_companies_back_to_back_are_a_wholesaler_and_then_the_buyer():
    held = {"p1": {"owner_name": "Birch Holdings LLC", "owner_mail": "1 Office Rd", "sale_history": [
        deed("2026-03-10", 88000, "Quick Contracts LLC", "Birch Holdings LLC", cash=True),
        deed("2026-03-08", 70000, "Sam Seller", "Quick Contracts LLC")]}}
    stats = {}
    obs = by_name(one(seg("pending", "p1"), held, TAUGHT, stats))
    mid, buyer = obs["Quick Contracts LLC"], obs["Birch Holdings LLC"]
    assert (mid["role"], mid["txn"], mid["price"], mid["exit_price"], mid["days_held"]) == ("wholesaler", "wholesale", 70000, 88000, 2)
    assert mid["sold_to"] == "Birch Holdings LLC" and mid["buy_date"] == "2026-03-08" and mid["date"] == "2026-03-10"
    assert buyer["role"] == "holder" and buyer["price"] == 88000 and buyer["via"] == "Quick Contracts LLC"
    assert stats["wholesalers"] == 1
    # the same chain behind a finished flip: the company in the middle is found there too
    flipped = {"p2": {"owner_name": "Pat Homebuyer", "sale_history": [
        deed("2026-09-01", 240000, "Birch Holdings LLC", "Pat Homebuyer"),
        deed("2026-03-10", 88000, "Quick Contracts LLC", "Birch Holdings LLC"),
        deed("2026-03-08", 70000, "Sam Seller", "Quick Contracts LLC")]}}
    got = by_name(one(seg("flip", "p2"), flipped, TAUGHT))
    assert got["Quick Contracts LLC"]["role"] == "wholesaler" and got["Birch Holdings LLC"]["role"] == "exited"
    assert got["Birch Holdings LLC"]["price"] == 88000 and got["Birch Holdings LLC"]["via"] == "Quick Contracts LLC"


def test_what_is_not_a_double_closing():
    def chain(first_buyer, gap_days, final_buyer="Birch Holdings LLC"):
        sold = date(2026, 3, 10)
        return {"p1": {"owner_name": final_buyer, "sale_history": [
            deed(sold.isoformat(), 88000, first_buyer, final_buyer),
            deed((sold - timedelta(days=gap_days)).isoformat(), 70000, "Sam Seller", first_buyer)]}}
    count = lambda details: sum(1 for o in one(seg("pending", "p1"), details, TAUGHT) if o["role"] == "wholesaler")  # noqa: E731
    assert count(chain("Quick Contracts LLC", 30)) == 1
    assert count(chain("Quick Contracts LLC", 31)) == 0                   # a company that held it is not passing a contract
    assert count(chain("Quick Contracts LLC", 3000)) == 0                 # a landlord selling to an investor
    assert count(chain("Morgan Seller", 2)) == 0                          # the first buyer is a person
    assert count(chain("Quick Contracts LLC", 2, final_buyer="Jordan Buyer")) == 0   # the second buyer is a person
    assert count({"p1": {"owner_name": "Birch Holdings LLC", "sale_history": [
        deed("2026-03-10", 88000, "Quick Contracts LLC", "Birch Holdings LLC"),
        deed("2026-03-08", 70000, "Sam Seller", "Someone Else LLC")]}}) == 0          # the deeds do not chain
    assert sum(1 for o in one(seg("pending", "p1"), chain("Quick Contracts LLC", 2), TODAY) if o["role"] == "wholesaler") == 0


# ------------------------------------------------------------------- a loan stands in for a price
def test_a_loan_stands_in_only_where_the_deed_has_no_price():
    loan = {"date": "2026-03-12", "amount": 96000, "borrower": "BIRCH HOLDINGS LLC", "lender": "A Bank"}
    base = {"owner_name": "Birch Holdings LLC", "owner_mail": "1 Office Rd"}
    no_price = {"p1": dict(base, sale_history=[deed("2026-03-10", 0, "Sam Seller", "Birch Holdings LLC")], mortgages=[loan])}
    stats = {}
    o = one(seg("pending", "p1"), no_price, TAUGHT, stats)[0]
    assert (o["price"], o["price_source"]) == (96000, "loan") and stats["loan_priced"] == 1
    assert one(seg("pending", "p1"), no_price, TODAY)[0]["price"] == 0                 # today: still unknown
    # a deed that carries a price keeps it
    priced = {"p1": dict(base, sale_history=[deed("2026-03-10", 81000, "Sam Seller", "Birch Holdings LLC")], mortgages=[loan])}
    o = one(seg("pending", "p1"), priced, TAUGHT)[0]
    assert o["price"] == 81000 and "price_source" not in o
    # somebody else's loan, or one recorded months away, is not this purchase's price
    for wrong in (dict(loan, borrower="Other Borrower LLC"), dict(loan, date="2026-07-01"), dict(loan, amount=0),
                  dict(loan, date=None)):
        d = {"p1": dict(base, sale_history=[deed("2026-03-10", 0, "Sam Seller", "Birch Holdings LLC")], mortgages=[wrong])}
        assert one(seg("pending", "p1"), d, TAUGHT)[0]["price"] == 0, wrong
    # two loans near the deed: the one recorded closest to it
    two = {"p1": dict(base, sale_history=[deed("2026-03-10", 0, "Sam Seller", "Birch Holdings LLC")],
                      mortgages=[dict(loan, date="2026-04-15", amount=150000), loan])}
    assert one(seg("pending", "p1"), two, TAUGHT)[0]["price"] == 96000
    # a detail read before loans were kept has none to offer: unknown stays unknown
    old = {"p1": dict(base, sale_history=[deed("2026-03-10", 0, "Sam Seller", "Birch Holdings LLC")])}
    assert one(seg("pending", "p1"), old, TAUGHT)[0]["price"] == 0


def test_a_flipper_with_no_purchase_deed_is_priced_at_the_loan_they_bought_with():
    details = {"p1": {"owner_name": "Pat Homebuyer", "sale_history": [
        deed("2026-06-01", 310000, "Fast Flips LLC", "Pat Homebuyer")],
        "mortgages": [{"date": "2026-01-20", "amount": 155000, "borrower": "Fast Flips LLC", "lender": "A Bank"},
                      {"date": "2026-03-01", "amount": 40000, "borrower": "Fast Flips LLC", "lender": "A Bank"},
                      {"date": "2026-06-02", "amount": 290000, "borrower": "Pat Homebuyer", "lender": "B Bank"},
                      {"date": "2019-01-01", "amount": 60000, "borrower": "Fast Flips LLC", "lender": "A Bank"}]}}
    o = one(seg("flip", "p1"), details, TAUGHT)[0]
    # the earliest of their own loans in the two years before they sold: not the homebuyer's, not a 2019 one
    assert (o["price"], o["price_source"], o["buy_date"], o["exit_price"]) == (155000, "loan", "2026-01-20", 310000)
    assert o["role"] == "exited" and o["days_held"] == 132
    t = one(seg("flip", "p1"), details, TODAY)[0]
    assert t["price"] is None and t["buy_date"] is None                               # today: unknown stays unknown


# ----------------------------------------------------------------- new construction is not a flip
def test_a_house_built_after_2010_is_new_construction_not_a_flip():
    def flip(year):
        d = {"owner_name": "Pat Homebuyer", "sale_history": [
            deed("2026-06-01", 420000, "Ridge Homes LLC", "Pat Homebuyer"),
            deed("2025-09-01", 60000, "Land Seller", "Ridge Homes LLC")]}
        if year != "missing":
            d["year_built"] = year
        return d
    details = {"p1": flip(2025), "p2": flip(1985), "p3": flip("missing"), "p4": flip(2010), "p5": flip(None)}
    stats = {}
    obs = one(seg("flip", "p1", "p2", "p3", "p4", "p5"), details, TAUGHT, stats)
    assert sorted(o["prop"][:2] for o in obs) == ["p2", "p3", "p4", "p5"]                # the 2025 house is left out
    assert stats["new_builds"] == 1 and stats["year_unknown"] == 2                       # an unknown year is kept, and counted
    assert len(one(seg("flip", "p1", "p2", "p3", "p4", "p5"), details, TODAY)) == 5      # today reads all five as flips
    # the search asks for the same thing, on flips only, and today's search is untouched
    legacy = {"type_single_family": True, D.K_TXN: ["flip"], "extra_last_sale_value_min": 1000,
              "extra_is_last_sale_interfamily": False}
    assert D._body("99001", "Alder", "ZZ", "flip", 1, False)["filters"] == legacy
    assert D._body("99001", "Alder", "ZZ", "flip", 1, False, built_by=2010)["filters"] == dict(legacy, extra_year_built_max=2010)
    held = D._body("99001", "Alder", "ZZ", "pending", 1, True, built_by=2010)["filters"]
    assert "extra_year_built_max" not in held and held["extra_collapse_by_owner"] is True


def test_the_year_filter_is_proven_by_a_count_that_moves():
    class Search:
        def __init__(self, with_key, without):
            self.with_key, self.without, self.asked = with_key, without, 0

        def search(self, body):
            self.asked += 1
            return {"total_results": self.with_key if "extra_year_built_max" in body["filters"] else self.without}

    moved, same, off = Search(310, 480), Search(480, 480), Search(1, 1)
    assert D._year_filter_proven(moved, "99001", "Alder", "ZZ", "flip", False, "", 2010) is True and moved.asked == 2
    assert D._year_filter_proven(same, "99001", "Alder", "ZZ", "flip", False, "", 2010) is False
    assert D._year_filter_proven(off, "99001", "Alder", "ZZ", "flip", False, "", 0) is True and off.asked == 0
    assert D._year_filter_proven(off, "99001", "Alder", "ZZ", "pending", True, "", 2010) is True and off.asked == 0


# ---------------------------------------------------------------------- institutions by count
def _registry(rules_name, rows, details):
    """Run the real aggregate phase on a throwaway folder and hand back its registry."""
    real_out, real_state = D.OUT, D.STATE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        D.OUT, D.STATE_PATH = Path(tmp), Path(tmp) / "state.json"
        D._ACTIVE["name"] = rules_name
        try:
            D._save(D.OUT / "rows.json", rows)
            D._save(D.OUT / "details.json", details)
            D._save(D.STATE_PATH, {"hydrate": {"when": "now"}})
            said = io.StringIO()
            with redirect_stdout(said):
                D.phase_aggregate(types.SimpleNamespace())
            reg = json.loads((D.OUT / "registry.json").read_text(encoding="utf-8"))
            state = json.loads(D.STATE_PATH.read_text(encoding="utf-8"))
            return {b["name"]: b for b in reg}, said.getvalue(), state
        finally:
            D.OUT, D.STATE_PATH = real_out, real_state
            D._ACTIVE["name"] = ""


def _many(owner, n, start_id, days_ago=30, price=150000):
    when = (date.today() - timedelta(days=days_ago)).isoformat()
    ids = ["%s%03d" % (start_id, i) for i in range(n)]
    return ids, {i: {"owner_name": owner, "owner_mail": "", "sale_history": [deed(when, price, "A Seller", owner)]} for i in ids}


def test_a_buyer_with_100_purchases_in_six_months_is_an_institution_whatever_its_name():
    big_ids, big = _many("Plain Holdings LLC", 120, "b")
    builder_ids, builder = _many("Maple Ridge Builders LLC", 9, "m")
    old_ids, old = _many("Slow Steady LLC", 130, "s", days_ago=400)          # 130 purchases, none in six months
    rows = seg("pending", *(big_ids + builder_ids + old_ids))
    details = dict(big, **builder, **old)
    today, said_today, state_today = _registry("today", rows, details)
    taught, said, state = _registry("taught", rows, details)
    # today: no count rule, and the registry carries no new key at all
    assert today["Plain Holdings LLC"]["tier"] != "EXCLUDE" and "buys_6mo" not in today["Plain Holdings LLC"]
    assert "rules" not in said_today and "rules" not in state_today["aggregate"]
    # taught: dropped by count, with the count in the reason
    big_row = taught["Plain Holdings LLC"]
    assert big_row["tier"] == "EXCLUDE" and big_row["tier_reason"] == "institution by count: 120 purchases in six months"
    assert big_row["buys_6mo"] == 120 and D.buyer_type(big_row)[0] == "not a target"
    # the local builder with nine purchases is the best buyer on the list and is not touched
    assert taught["Maple Ridge Builders LLC"]["tier"] == today["Maple Ridge Builders LLC"]["tier"] != "EXCLUDE"
    assert taught["Maple Ridge Builders LLC"]["buys_6mo"] == 9
    assert D.buyer_type(taught["Maple Ridge Builders LLC"])[0] == "self-performer"
    # volume alone, spread over years, is not the rule: six months is
    assert taught["Slow Steady LLC"]["tier"] != "EXCLUDE" and taught["Slow Steady LLC"]["buys_6mo"] == 0
    assert "dropped as institutions by count (100 or more purchases in six months): 1 (Plain Holdings LLC)" in said
    assert "a holder's count is a floor" in said and state["aggregate"]["institutions_by_count"] == 1
    # every other field of an untouched buyer is the same under both rulesets
    a, b = today["Maple Ridge Builders LLC"], taught["Maple Ridge Builders LLC"]
    assert {k: v for k, v in b.items() if k in a} == a


def test_six_month_purchases_are_counted_by_the_purchase_date():
    now = date(2026, 11, 13)
    d = lambda n: (now - timedelta(days=n)).isoformat()  # noqa: E731
    obs = [{"role": "holder", "date": d(10)}, {"role": "holder", "date": d(182)}, {"role": "holder", "date": d(183)},
           {"role": "exited", "date": d(5), "buy_date": d(100)},              # bought inside the window
           {"role": "exited", "date": d(5), "buy_date": d(400)},              # sold last week, bought last year
           {"role": "exited", "date": d(5), "buy_date": None},                # no purchase date: not counted
           {"role": "wholesaler", "date": d(20), "buy_date": d(22)}, {"role": "holder", "date": "not a date"}]
    assert D.six_month_buys(obs, today=now) == 4


def test_the_registry_types_follow_the_new_counts_only_when_they_exist():
    base = {"tier": "1", "name": "Quick Contracts LLC", "txn_types": ["wholesale"], "n_held": 0, "n_exited": 0}
    assert D.buyer_type(dict(base, n_wholesaled=6))[0] == "wholesaler"
    assert "back to back" in D.buyer_type(dict(base, n_wholesaled=6))[1]
    assert D.buyer_type(dict(base, name="Cedar Rentals LLC", txn_types=["flip"], n_long_holds=4, n_exited=1))[0] == "long-term holder"
    # a registry built today has neither count, and every label comes out as it always did
    assert D.buyer_type(dict(base, txn_types=["flip"], n_exited=5))[0] == "exited flipper"
    assert D.buyer_type(dict(base, txn_types=["rental"], n_held=4, n_exited=1))[0] == "landlord"
    assert D.buyer_type(dict(base, n_held=3, n_wholesaled=1, txn_types=["pending"]))[0] == "active holder"


# ----------------------------------------------------------------------------- the phases around it
def test_profiles_refuse_a_registry_built_on_other_rules_and_state_the_rules_they_used():
    from openpyxl import load_workbook
    ids, details = _many("Birch Holdings LLC", 3, "h")
    details["f1"] = {"owner_name": "Pat Homebuyer", "sale_history": [
        deed("2026-05-01", 300000, "Cedar Rentals LLC", "Pat Homebuyer"),
        deed("2017-04-01", 90000, "Old Owner", "Cedar Rentals LLC")]}
    details["f2"] = {"owner_name": "Sam Homebuyer", "sale_history": [
        deed("2026-04-01", 280000, "Cedar Rentals LLC", "Sam Homebuyer"),
        deed("2016-04-01", 80000, "Old Owner", "Cedar Rentals LLC")]}
    rows = dict(seg("pending", *ids), **seg("flip", "f1", "f2"))
    real_out, real_state = D.OUT, D.STATE_PATH
    args = types.SimpleNamespace(profile_min_buys=2, require_active=False, recent_days=365)
    with tempfile.TemporaryDirectory() as tmp:
        D.OUT, D.STATE_PATH = Path(tmp), Path(tmp) / "state.json"
        try:
            D._save(D.OUT / "rows.json", rows)
            D._save(D.OUT / "details.json", details)
            D._save(D.STATE_PATH, {"hydrate": {"when": "now"}})
            D._ACTIVE["name"] = "taught"
            with redirect_stdout(io.StringIO()):
                D.phase_aggregate(types.SimpleNamespace())
            D._ACTIVE["name"] = ""
            try:
                with redirect_stdout(io.StringIO()):
                    D.phase_profiles(args)
                raise AssertionError("profiles ran on a registry built with other rules")
            except SystemExit as stop:
                assert "built on the taught rules" in str(stop) and "--phase aggregate --rules today" in str(stop)
            D._ACTIVE["name"] = "taught"
            with redirect_stdout(io.StringIO()):
                D.phase_profiles(args)
            profiles = {p["name"]: p for p in json.loads((D.OUT / "buyer_profiles.json").read_text(encoding="utf-8"))}
            cedar = profiles["Cedar Rentals LLC"]
            assert cedar["rules"] == "taught" and cedar["n_long_holds"] == 2 and cedar["n_exited"] == 0
            assert cedar["buyer_type"] == "long-term holder" and cedar["box_price"]["n"] == 0    # 2016 and 2017 prices are not a buy box
            book = load_workbook(next(D.OUT.glob("Knox_Blount_Buyer_Profiles_*.xlsx")))
            cells = [str(c) for row in book["Sources and Caveats"].iter_rows(values_only=True) for c in row if c]
            assert any(c.startswith("Deeds were read on the taught rules") for c in cells)
        finally:
            D.OUT, D.STATE_PATH = real_out, real_state
            D._ACTIVE["name"] = ""


def test_the_preview_shows_before_and_after_and_writes_nothing():
    details = {
        "p1": {"owner_name": "Pat Homebuyer", "sale_history": [
            deed("2026-06-01", 310000, "Fast Flips LLC", "Pat Homebuyer"),
            deed("2026-01-15", 140000, "Quick Contracts LLC", "Fast Flips LLC"),
            deed("2026-01-13", 120000, "Sam Seller", "Quick Contracts LLC")]},
        "p2": {"owner_name": "Sam Homebuyer", "sale_history": [
            deed("2026-05-01", 300000, "Fast Flips LLC", "Sam Homebuyer"),
            deed("2015-04-01", 60000, "Old Owner", "Fast Flips LLC")]},
        "p3": {"owner_name": "Lee Homebuyer", "sale_history": [
            deed("2026-04-01", 250000, "Fast Flips LLC", "Lee Homebuyer"),
            deed("2025-12-01", 130000, "Old Owner", "Fast Flips LLC")]},
    }
    rows = seg("flip", "p1", "p2", "p3")
    got = D.rules_preview(rows, details, buyer="Fast Flips LLC")
    assert got["observations"] == {"today": 3, "taught": 4} and got["buyers"] == {"today": 1, "taught": 2}
    assert got["new_buyers"] == ["QUICK CONTRACTS LLC"] and got["institutions_by_count"] == []
    assert got["stats"]["long_holds"] == 1 and got["stats"]["wholesalers"] == 1
    b = got["buyer"]
    assert b["today"]["band"]["lo"] == 60000 and b["today"]["band"]["n"] == 3      # today the 2015 price sits in the band
    assert b["taught"]["band"]["lo"] == 130000 and b["taught"]["band"]["n"] == 2 and b["taught"]["long_holds"] == 1
    real_out, real_state = D.OUT, D.STATE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        D.OUT, D.STATE_PATH = Path(tmp), Path(tmp) / "state.json"
        try:
            D._save(D.OUT / "rows.json", rows)
            D._save(D.OUT / "details.json", details)
            before = sorted(p.name for p in D.OUT.iterdir())
            said = io.StringIO()
            with redirect_stdout(said):
                D.phase_rules(types.SimpleNamespace(buyer="Fast Flips LLC"))
            assert sorted(p.name for p in D.OUT.iterdir()) == before                # nothing new on disk
        finally:
            D.OUT, D.STATE_PATH = real_out, real_state
    text = said.getvalue()
    assert "rules preview: nothing was written" in text and "1 were held longer and read as long holds" in text
    assert "3 of 3 cached properties were read before loans were kept" in text
    assert "Fast Flips LLC, taught rules: 3 purchases, price band $130K to $140K on 2 priced sale(s)" in text


def test_a_property_read_keeps_the_loans_and_the_year_built():
    class Map:
        def get_detail(self, rid):
            return {"owner_info": {"owner_name": "Birch Holdings LLC", "owner_mail_address": "1 Office Rd, Alderton, ZZ 00101",
                                   "total_properties": 4, "portfolio_value": 600000, "secondary_owner_names": []},
                    "years_built": 1978,
                    "sale_history": [{"sale_date": "2026-03-10", "sale_price": 0, "buyer_name": "Birch Holdings LLC",
                                      "seller_name": "Sam Seller", "is_cash_sale": False}],
                    "mortgage_history": [{"record_date": "2026-03-12", "loan_type": "Purchase", "loan_type_code": "P",
                                          "amount": 96000, "grantee_name": "BIRCH HOLDINGS LLC", "lender_name": "A Bank",
                                          "due_date": None}, "not a loan"]}

    d = D._hydrate_one(Map(), "p1")
    assert d["mortgages"] == [{"date": "2026-03-12", "amount": 96000, "borrower": "BIRCH HOLDINGS LLC", "lender": "A Bank"}]
    assert d["year_built"] == 1978
    # everything the registry read before is still there, unchanged
    assert (d["id"], d["owner_name"], d["owner_mail"], d["portfolio_n"]) == ("p1", "Birch Holdings LLC", "1 Office Rd, Alderton, ZZ 00101", 4)
    assert d["sale_history"] == [{"date": "2026-03-10", "price": 0, "buyer": "Birch Holdings LLC", "seller": "Sam Seller", "cash": False}]
    # and what it kept is enough for the loan rule, end to end
    o = one(seg("pending", "p1"), {"p1": d}, TAUGHT)[0]
    assert (o["price"], o["price_source"]) == (96000, "loan")


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/dispo_buyers.py", "tests/test_dispo_buyers_rules.py", "tests/test_dispo_buyers_price.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text, name


def main() -> int:
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
