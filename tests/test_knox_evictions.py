"""Detainer classification off the Knox civil docket.

The online docket has NO case-type column, so landlord/tenant cases are found by
classifying the plaintiff. Two bugs this guards, both from a live 2026-08 sweep
that classified 293 cases of which 78 were wrong:

  * Debt buyers carry "MANAGEMENT" and "PROPERTY" in their names. Midland Credit
    Management alone was 59 cases and is a collector, not a landlord.
  * Hints were matched as substrings, so "COURT" matched three people surnamed
    COURTNEY and "POINTE" matched one surnamed POINTER.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

from knox_evictions import looks_like_landlord  # noqa: E402


class TestRealLandlords:
    @pytest.mark.parametrize("name", [
        "KCDC",
        "KNOXVILLE'S COMMUNITY DEVELOPMENT CORPORATION",
        "THE DISTRICT APARTMENTS",
        "WINDSOR COURT",
        "SREIT EAGLE POINTE APARTMENTS LLC",
        "CALLAHAN FLATS LP",
        "FH RENTALS AKA HODGE PROP.",
        "BRUN PROPERTY MANAGEMENT",
        "COOPER REALTY INVESTMENTS INC",
        "RIDGEBROOK HOUSING I LLC",
        "OAKVIEW PROPERTIES",
    ])
    def test_classified_as_landlord(self, name):
        assert looks_like_landlord(name)


class TestDebtBuyersAndOtherFilers:
    @pytest.mark.parametrize("name", [
        "MIDLAND CREDIT MANAGEMENT, INC",
        "MIDLAND CREDIT MANAGEMENT",
        "MIDLAND MANAGEMENT CREDIT",
        "CROWN ASSET MANAGEMENT, LLC",
        "NCB MANAGEMENT SERVICES, INC",
        "ACCELERATED INVENTORY MANAGEMENT",
        "TRAVELERS PROPERTY CASUALTY COMPANY OF AMERICA",
        "TN DEPT OF LABOR & WORKFORCE DEVELOPMENT",
        "CATERPILLAR FINANCIAL SERVICES CORPORATION",
        "FT SANDERS REGIONAL MEDICAL CENTER",
        "GATEWAY COURT REPORTING & VIDEO",
    ])
    def test_not_a_landlord(self, name):
        assert not looks_like_landlord(name)


class TestWordBoundaries:
    """Substring matching put private individuals on the landlord list."""

    @pytest.mark.parametrize("name", [
        "MADAYCHIK COURTNEY",     # COURT inside COURTNEY
        "CLINE COURTNEY",
        "ISON COURTNEY",
        "POINTER ROBERT J",       # POINTE inside POINTER
    ])
    def test_surnames_containing_a_hint_are_not_landlords(self, name):
        assert not looks_like_landlord(name)

    def test_the_hint_still_matches_as_a_whole_word(self):
        assert looks_like_landlord("WINDSOR COURT")
        assert looks_like_landlord("GREYSTONE POINTE LLC")


class TestEdges:
    def test_empty_and_short(self):
        assert not looks_like_landlord("")
        assert not looks_like_landlord("AB")
        assert not looks_like_landlord(None)

    def test_plain_person_name(self):
        assert not looks_like_landlord("LIVESAY RYAN MICHAEL")
