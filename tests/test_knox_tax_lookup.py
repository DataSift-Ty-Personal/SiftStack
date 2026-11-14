"""The Knox county tax lookup by street address. Zero network, invented data.

Two faults measured on 2026-11-11 against 51 real foreclosure addresses:
  * the county search wants "100 EXAMPLE ST", a notice says "100 Example Street",
    so 29 of 51 lookups found nothing and the record uploaded with no tax data;
  * the search is fuzzy, and the first result was taken unchecked, so 3 of 51
    would have carried a different house's tax bill.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import tax_enricher as te  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise te.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


@pytest.fixture
def county(monkeypatch):
    """A fake county API. `by_query` maps an exact search string to parcels."""
    state = {"by_query": {}, "asked": [], "bills": {}}

    def fake_get(url, timeout=None):
        from urllib.parse import unquote
        if "/due/PPT/" in url:
            account = unquote(url.split("/due/PPT/")[1].split("?")[0])
            if account not in state["bills"]:
                return _Resp({}, 404)
            return _Resp({"due": {"PPT": {"bills": state["bills"][account], "parcels": []}}})
        query = unquote(url.split("/parcels/")[1].split("?")[0])
        state["asked"].append(query)
        return _Resp({"parcels": state["by_query"].get(query, [])})

    def recording_get(url, timeout=None):
        state.setdefault("hosts", []).append(url.split("/api/")[0])
        return fake_get(url, timeout)

    monkeypatch.setattr(te.requests, "get", recording_get)
    monkeypatch.setattr(te.time, "sleep", lambda *_: None)
    return state


def _bill(due, delinquent=True, paid=False):
    return {"due": due, "delinquent": delinquent, "paid": paid}


def test_a_spelled_out_suffix_is_retried_the_way_the_county_writes_it(county):
    county["by_query"]["100 EXAMPLE ST"] = [
        {"account_number": "001AA-001", "parcel_address": "100 EXAMPLE ST"}]
    county["bills"]["001AA-001"] = [_bill(812.40), _bill(790.10), _bill(500.0, delinquent=False)]

    got = te._knox_lookup_by_address("100 Example Street")

    assert got["account_number"] == "001AA-001"
    assert got["delinquent_amount"] == 1602.5 and got["delinquent_years"] == 2
    assert county["asked"] == ["100 Example Street", "100 EXAMPLE ST"]


def test_the_first_result_is_not_taken_when_it_is_a_different_house(county):
    county["by_query"]["214 Sample Rd"] = [
        {"account_number": "002BB-002", "parcel_address": "2140 SAMPLE RD"},
        {"account_number": "002BB-003", "parcel_address": "214 SAMPLE LN"}]
    county["bills"]["002BB-002"] = [_bill(9000.0)]
    county["bills"]["002BB-003"] = [_bill(4000.0)]

    assert te._knox_lookup_by_address("214 Sample Rd") is None


def test_the_matching_house_is_found_behind_a_wrong_first_result(county):
    county["by_query"]["214 Sample Rd"] = [
        {"account_number": "002BB-002", "parcel_address": "2140 SAMPLE RD"},
        {"account_number": "002BB-009", "parcel_address": "214 SAMPLE RD"}]
    county["bills"]["002BB-002"] = [_bill(9000.0)]
    county["bills"]["002BB-009"] = [_bill(321.0)]

    got = te._knox_lookup_by_address("214 Sample Rd")

    assert got["account_number"] == "002BB-009" and got["delinquent_amount"] == 321.0


def test_only_the_trailing_word_is_treated_as_a_suffix():
    assert te._street_queries("77 Ridge Road") == [
        "77 Ridge Road", "77 RIDGE RD", "77 RIDGE", "RIDGE RD 77"]
    assert te._street_queries("9 North Mountain View Drive")[1] == "9 N MOUNTAIN VIEW DR"


def test_unit_and_city_are_dropped_from_the_search_but_the_original_is_tried_first():
    q = te._street_queries("12 Test Avenue Apt 4")
    assert q[0] == "12 Test Avenue Apt 4" and "12 TEST AVE" in q
    assert "12 TEST AVE" in te._street_queries("12 Test Avenue, Knoxville, TN 37917")


def test_no_house_number_means_no_lookup_at_all(county):
    assert te._knox_lookup_by_address("Example Street") is None
    assert county["asked"] == []


def test_a_paid_up_parcel_is_a_real_zero_not_a_miss(county):
    county["by_query"]["5 Clear Way"] = [{"account_number": "003CC-005", "parcel_address": "5 CLEAR WAY"}]
    county["bills"]["003CC-005"] = [_bill(0, delinquent=False, paid=True)]

    got = te._knox_lookup_by_address("5 Clear Way")

    assert got is not None and got["delinquent_amount"] == 0 and got["delinquent_years"] == 0


def test_an_account_with_no_bills_endpoint_falls_through_to_the_next_match(county):
    # A business account can share the street address; its bill route 404s.
    county["by_query"]["8 Shop Pike"] = [
        {"account_number": "BUS-8", "parcel_address": "8 SHOP PIKE"},
        {"account_number": "004DD-008", "parcel_address": "8 SHOP PIKE"}]
    county["bills"]["004DD-008"] = [_bill(55.0)]

    assert te._knox_lookup_by_address("8 Shop Pike")["account_number"] == "004DD-008"


# ── Blount: same vendor, same contract, its own host ─────────────────


class _Notice:
    def __init__(self, county, address, parcel_id=""):
        self.county, self.address, self.parcel_id = county, address, parcel_id
        self.tax_delinquent_amount = self.tax_delinquent_years = ""
        self.latitude = self.longitude = ""


def test_blount_is_looked_up_on_the_blount_host_not_knox(county):
    county["by_query"]["300 Demo Dr"] = [{"account_number": 41822, "parcel_address": "300 DEMO DR"}]
    county["bills"]["41822"] = [_bill(1250.75)]
    n = _Notice("Blount", "300 Demo Dr", parcel_id="058 123.00")

    te.enrich_tax_delinquency([n])

    assert n.tax_delinquent_amount == "1250.75" and n.tax_delinquent_years == "1"
    assert set(county["hosts"]) == {"https://blount-tn.mygovonline.com"}
    # A Blount tax account is not the parcel id; the notice keeps its own.
    assert n.parcel_id == "058 123.00"


def test_a_roll_address_with_the_house_number_last_still_matches(county):
    county["by_query"]["DEMO DR 300"] = [{"account_number": 7, "parcel_address": "DEMO DR 300"}]
    county["bills"]["7"] = [_bill(64.0)]

    got = te._knox_lookup_by_address("300 Demo Drive", te.BLOUNT_API_BASE)

    assert got["account_number"] == 7 and got["delinquent_amount"] == 64.0


def test_knox_and_blount_are_both_enriched_in_one_batch(county):
    county["by_query"]["1 Alpha St"] = [{"account_number": "010AA-001", "parcel_address": "1 ALPHA ST"}]
    county["by_query"]["2 Beta St"] = [{"account_number": 22, "parcel_address": "2 BETA ST"}]
    county["bills"]["010AA-001"] = [_bill(10.0)]
    county["bills"]["22"] = [_bill(0, delinquent=False, paid=True)]
    knox, blount, other = _Notice("Knox", "1 Alpha St"), _Notice("Blount", "2 Beta St"), _Notice("Sevier", "3 Gamma St")

    te.enrich_tax_delinquency([knox, blount, other])

    assert knox.tax_delinquent_amount == "10.0" and knox.parcel_id == "010AA-001"
    assert blount.tax_delinquent_amount == "" and other.tax_delinquent_amount == ""


def test_a_record_with_no_address_or_county_does_not_crash(county):
    n = _Notice("Knox", None)
    n.parcel_id = None
    te.enrich_tax_delinquency([n])          # nothing to look up, nothing raised
    assert n.tax_delinquent_amount == ""
