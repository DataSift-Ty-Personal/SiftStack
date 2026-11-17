"""dispo_buyers: an exited flipper is priced at what they PAID, not at what they sold for.

Zero network. Invented names, streets and prices.

Why this exists: the registry recorded a flip at the price on its exit deed, the sale to the retail
buyer. Every exited flipper's price band was therefore a band of RESALE prices: one real buyer read
a $307,500 median on a book of $100,000 to $200,000 purchases, and the deal blast's band filter
skipped exactly the buyers who could afford the house. The purchase is the deed before the exit,
where the flipper is the buyer; when that deed is not in the history, the price is unknown.

Run: python tests/test_dispo_buyers_price.py   (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import dispo_buyers as D  # noqa: E402


def _rows(txn="flip"):
    return {"Knox:%s" % txn: [{"id": "p1", "address": "10 Example St, Knoxville, TN"},
                              {"id": "p2", "address": "20 Example St, Knoxville, TN"}]}


DETAILS = {
    # newest first: the exit to a homebuyer, then the flipper's own purchase
    "p1": {"owner_name": "Pat Homebuyer", "sale_history": [
        {"date": "2026-06-01", "price": 310000, "cash": False, "seller": "Example Flips LLC", "buyer": "Pat Homebuyer"},
        {"date": "2026-01-15", "price": 140000, "cash": True, "seller": "Old Owner", "buyer": "EXAMPLE FLIPS LLC"}]},
    # the purchase deed is not in the history: the price is unknown, never the exit price
    "p2": {"owner_name": "Sam Homebuyer", "sale_history": [
        {"date": "2026-05-01", "price": 280000, "cash": False, "seller": "Example Flips LLC", "buyer": "Sam Homebuyer"}]},
}


def test_an_exited_flip_is_priced_at_the_purchase():
    obs = {o["prop"]: o for o in D._observations(_rows(), DETAILS)}
    a = obs["10 Example St, Knoxville, TN"]
    assert a["name"] == "Example Flips LLC" and a["role"] == "exited"
    assert a["price"] == 140000, a
    assert a["exit_price"] == 310000 and a["buy_date"] == "2026-01-15" and a["cash"] is True, a


def test_an_unknown_purchase_stays_unknown():
    obs = {o["prop"]: o for o in D._observations(_rows(), DETAILS)}
    b = obs["20 Example St, Knoxville, TN"]
    assert b["price"] is None and b["exit_price"] == 280000 and b["buy_date"] is None, b


def test_a_holder_is_still_priced_at_the_last_sale():
    hold = sorted(D.HOLD_TYPES)[0]
    details = {"p1": {"owner_name": "Example Holdings LLC", "owner_mail": "1 Office Rd", "sale_history": [
        {"date": "2026-03-01", "price": 120000, "cash": True, "seller": "Old Owner", "buyer": "Example Holdings LLC"}]}}
    rows = {"Knox:%s" % hold: [{"id": "p1", "address": "10 Example St, Knoxville, TN"}]}
    o = D._observations(rows, details)[0]
    assert o["role"] == "holder" and o["price"] == 120000, o


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print("%d checks passed" % len(fns))
