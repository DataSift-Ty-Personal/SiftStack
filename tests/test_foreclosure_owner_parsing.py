"""Foreclosure owner names: the borrower, never the lender riding along.

A live notice on 2026-11-12 read "executed by <borrower> payable to the order of
<lender>, LLC". The name pattern had no stop for "payable to", so the owner
uploaded as "<Borrower> Payable To The Order Of <Lender>, Llc", the entity-owner
filter dropped it as an LLC, and the day's only notice vanished. Names here are
invented. Zero network.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData, _parse_name  # noqa: E402


def _owner(text: str) -> str:
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.raw_text = text
    _parse_name(n)
    return n.owner_name


def test_payable_to_the_order_of_ends_the_borrower_name():
    text = ("WHEREAS, default has occurred in the performance of a Promissory Note "
            "executed by Jordan Example payable to the order of Sample Funding, LLC, "
            "secured by a Deed of Trust dated March 3, 2021.")
    assert _owner(text) == "Jordan Example"


def test_two_borrowers_before_payable_to():
    text = ("a Deed of Trust executed by JORDAN EXAMPLE AND TAYLOR EXAMPLE, payable to "
            "Sample Bank, N.A., recorded in the Register's Office.")
    owner = _owner(text)
    assert owner.startswith("Jordan Example")
    assert "Payable" not in owner and "Sample" not in owner


def test_in_favor_of_and_to_secure_end_the_name():
    assert _owner("Deed of Trust executed by Casey Placeholder in favor of Sample Mortgage Corp, "
                  "as beneficiary.") == "Casey Placeholder"
    assert _owner("Deed of Trust executed by Casey Placeholder to secure the payment of a note "
                  "described therein.") == "Casey Placeholder"


def test_made_by_stops_at_payable_to():
    text = "the note made by Riley Sample payable to the order of Example Lending, LLC."
    owner = _owner(text)
    assert owner == "Riley Sample"


def test_the_ordinary_conveying_form_is_unchanged():
    text = ("Deed of Trust executed by Morgan Specimen, an unmarried person, conveying certain "
            "real property to Pat Trustee, Trustee.")
    assert _owner(text) == "Morgan Specimen"
