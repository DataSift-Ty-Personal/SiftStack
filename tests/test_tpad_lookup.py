"""TPAD (Blount County) property lookup.

Two silent failures meant no Blount probate record could ever resolve a property
address, so every one of them was dropped later as "missing address":
  1. TPAD 403s any request without a browser User-Agent.
  2. The HTML page ships an empty table shell; rows come from a JSON endpoint.

TPAD also prints the house number LAST ("LAKESHORE DR  5705"), which fails
validation and Smarty standardization if passed through unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

import property_lookup  # noqa: E402
from property_lookup import _tpad_lookup, _tpad_normalize_address  # noqa: E402


class TestAddressNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("LAKESHORE DR  5705", "5705 Lakeshore Dr"),
            ("E BROADWAY AVE 213", "213 E Broadway Ave"),
            ("OLD NILES FERRY DR 425", "425 Old Niles Ferry Dr"),
            ("WRIGHTS FERRY RD 3532", "3532 Wrights Ferry Rd"),
        ],
    )
    def test_house_number_moves_to_the_front(self, raw, expected):
        assert _tpad_normalize_address(raw) == expected

    def test_unit_suffix_on_the_number(self):
        assert _tpad_normalize_address("MAIN ST 12B") == "12B Main St"

    def test_no_trailing_number_left_alone(self):
        assert _tpad_normalize_address("HIGHWAY 411 S") == "Highway 411 S"

    def test_empty_stays_empty(self):
        assert _tpad_normalize_address("") == ""
        assert _tpad_normalize_address(None) == ""

    def test_collapses_whitespace(self):
        assert _tpad_normalize_address("  LAKESHORE   DR   5705 ") == "5705 Lakeshore Dr"


class TestHeaders:
    def test_user_agent_is_sent(self):
        """Without this TPAD returns 403 on every lookup."""
        assert "User-Agent" in property_lookup.TPAD_HEADERS
        assert "Mozilla" in property_lookup.TPAD_HEADERS["User-Agent"]

    def test_results_endpoint_is_the_json_one(self):
        assert property_lookup.TPAD_RESULTS_URL.endswith("/Search/GetSearchResults")


class TestParsing:
    def test_parses_json_rows(self, monkeypatch):
        rows = [{
            "owner": "SMITH FELICIA & JOHN T JR ",
            "propertyAddress": "LAKESHORE DR  5705",
            "class": "Residential",
            "parcelId": "104J H 03300 000",
        }]

        class FakeResp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self): return rows

        monkeypatch.setattr(property_lookup.requests, "post", lambda *a, **k: FakeResp())
        out = _tpad_lookup("SMITH JOHN")
        assert len(out) == 1
        assert out[0]["address"] == "5705 Lakeshore Dr"
        assert out[0]["owner"] == "SMITH FELICIA & JOHN T JR"
        assert out[0]["parcel_id"] == "104J H 03300 000"
        assert out[0]["classification"] == "Residential"

    def test_rows_without_an_address_are_skipped(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self): pass

            def json(self): return [{"owner": "SMITH JOHN", "propertyAddress": ""}]

        monkeypatch.setattr(property_lookup.requests, "post", lambda *a, **k: FakeResp())
        assert _tpad_lookup("SMITH JOHN") == []

    def test_non_json_response_returns_empty_not_crash(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self): pass

            def json(self): raise ValueError("not json")

        monkeypatch.setattr(property_lookup.requests, "post", lambda *a, **k: FakeResp())
        assert _tpad_lookup("SMITH JOHN") == []

    def test_unexpected_shape_returns_empty(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self): pass

            def json(self): return {"error": "nope"}

        monkeypatch.setattr(property_lookup.requests, "post", lambda *a, **k: FakeResp())
        assert _tpad_lookup("SMITH JOHN") == []
