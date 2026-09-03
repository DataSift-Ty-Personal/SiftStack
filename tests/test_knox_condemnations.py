"""Knox condemnation agenda parsing.

The bug these guard against: the property address was being taken as the last
house-numbered line before the parcel marker, which walks back into the PREVIOUS
dossier and lands on a party's MAILING address. A live June 2026 agenda parsed
as "14160 Dallas Parkway Suite 900" (a Dallas law firm) and three owners' home
addresses instead of the five condemned properties.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from knox_condemnations import _address_from_head, _build_note  # noqa: E402

# Shape of the text between one parcel marker and the next: the tail of the
# previous dossier (parties with mailing addresses), then the item letter, then
# the real property address.
HEAD_LETTER_THEN_ADDRESS = """
MACKIE WOLF ZIENTZ & MANN PC
14160 DALLAS PARKWAY SUITE 900
DALLAS TEXAS 75254-4314

OWNER SINCE: 05/28/2015
RECORD NUMBER: CE-26-000170

B.

1620 JEFFERSON AVENUE INCLUDING ACCESSORY STRUCTURE

"""

HEAD_ADDRESS_ON_LETTER_LINE = """
KATHY H & GORDON D FOSTER
6721 DEANE HILL DRIVE
KNOXVILLE TENNESSEE 37919-5941

C.             2738 JEFFERSON AVENUE

"""

HEAD_NO_LETTER = """
SOME PARTY
620 MILLIGAN STREET
KNOXVILLE TENNESSEE 37914
"""


class TestAddressExtraction:
    def test_address_on_the_line_after_the_letter(self):
        assert _address_from_head(HEAD_LETTER_THEN_ADDRESS) == \
            "1620 JEFFERSON AVENUE INCLUDING ACCESSORY STRUCTURE"

    def test_address_on_the_same_line_as_the_letter(self):
        assert _address_from_head(HEAD_ADDRESS_ON_LETTER_LINE) == "2738 JEFFERSON AVENUE"

    def test_never_returns_a_prior_party_mailing_address(self):
        """The whole point: everything before the item letter is the previous
        dossier and must not be reachable."""
        got = _address_from_head(HEAD_LETTER_THEN_ADDRESS)
        assert "DALLAS" not in got.upper()
        assert "14160" not in got

    def test_second_case_ignores_the_owner_mailing_address(self):
        got = _address_from_head(HEAD_ADDRESS_ON_LETTER_LINE)
        assert "DEANE HILL" not in got.upper()

    def test_no_item_letter_yields_nothing(self):
        """Better an empty address than a confidently wrong one."""
        assert _address_from_head(HEAD_NO_LETTER) == ""

    def test_empty_input(self):
        assert _address_from_head("") == ""


class TestNote:
    def test_note_carries_the_distress_detail(self):
        note = _build_note({
            "kind": "bbb", "hearing": "2026-06-25", "parcel": "070IF004",
            "violations": "EXTERIOR, ELECTRICAL, PLUMBING",
            "fees": "7 LIENS = $2,406.50", "owner_since": "04/10/1991",
            "city_tax": "PAID", "cty_tax": "PAID", "record": "CE-20-002882",
            "deceased_parties": 7, "has_unknown_heirs": True,
        })
        for bit in ("Certified blight", "070IF004", "ELECTRICAL", "2,406.50",
                    "CE-20-002882", "7 deceased", "Unknown or unborn heirs"):
            assert bit in note, f"{bit!r} missing from: {note}"

    def test_fees_none_is_not_reported_as_a_fee(self):
        note = _build_note({"kind": "poh", "hearing": "", "fees": "NONE",
                            "deceased_parties": 0, "has_unknown_heirs": False})
        assert "Fees" not in note

    def test_public_officer_labelled_distinctly(self):
        note = _build_note({"kind": "poh", "hearing": "2026-06-26",
                            "deceased_parties": 0, "has_unknown_heirs": False})
        assert "Public Officer" in note
