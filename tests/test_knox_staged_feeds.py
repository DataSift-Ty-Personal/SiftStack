"""knox_ftm_pull: the rows the newer feeds stage, carried through the county upload file.

Zero network. Invented addresses, owners and instrument numbers.

The failure these tests exist for: a staged row that reaches the upload file with no list name
uploads without complaint and lands on nothing.

Run: python -m pytest tests/test_knox_staged_feeds.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import code_violations  # noqa: E402
import ftm_staging  # noqa: E402
import knox_appointments  # noqa: E402
import knox_ftm_pull as K  # noqa: E402
import knox_tax_sale  # noqa: E402

RUN_DATE = "2026-11-13"


@pytest.fixture(autouse=True)
def default_lists(monkeypatch):
    for name in ("FTM_LIST_SUBSTITUTE_TRUSTEE", "FTM_LIST_TAX_SALE", "FTM_LIST_CODE_VIOLATION"):
        monkeypatch.delenv(name, raising=False)


def appointment_row():
    appt = {"instrument": "202611040000111", "recorded": "2026-11-04", "grantors": ["FIRST EXAMPLE BANK"],
            "grantees": ["HOLLIS AND PIKE LAW FIRM"]}
    deed = {"instrument": "201903150000444", "recorded": "2019-03-15", "grantees": ["FIRST EXAMPLE BANK"],
            "consideration": 185000.0}
    found = {"address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE", "account": "081JA018",
             "borrowers": ["QUILLFEATHER MARLOWE"], "via": "parcel id on the deed of trust"}
    return knox_appointments._row(appt, deed, found)


def tax_sale_row():
    return {"source": "knox_tax_sale", "feed": knox_tax_sale.FEED_KEY, "notice_type": "tax_sale",
            "address": "88 Invented Way", "owner_raw": "Thornbury Esme", "case_number": "094KB007",
            "tax_account": "094KB007", "sale_date": "2027-03-04", "event_date": "2027-03-04",
            "filed_date": "2026-11-13", "notes": "On the Knox County Trustee's tax sale list.", "source_url": "u"}


def code_row():
    return {"source": "knoxville_code_file", "feed": code_violations.FEED_KEY, "notice_type": "code_violation",
            "address": "17 Made Up Ct", "owner_raw": "", "case_number": "CE-26-0001", "event_date": "2026-11-10",
            "filed_date": "2026-11-10", "tax_account": "057012", "notes": "City of Knoxville record CE-26-0001.",
            "source_url": "u"}


ALL = ftm_staging.STAGED_FEEDS


def stage_all(scratch):
    ftm_staging.stage(scratch, knox_appointments.FEED, [appointment_row()], ["202611040000111"])
    ftm_staging.stage(scratch, knox_tax_sale.FEED, [tax_sale_row()], ["sha:094KB007"])
    ftm_staging.stage(scratch, code_violations.FEED, [code_row()], ["CE-26-0001"])


def test_nothing_staged_adds_nothing_to_the_pull(tmp_path):
    assert K.load_staged(str(tmp_path), ALL) == []


def test_each_staged_feed_is_loaded_for_the_upload(tmp_path):
    scratch = str(tmp_path)
    stage_all(scratch)
    got = K.load_staged(scratch, ALL)
    assert [r["notice_type"] for r in got] == ["substitute_trustee", "tax_sale", "code_violation"]


def test_a_pending_file_is_not_read_unless_its_feed_is_named(tmp_path):
    """Today's command line names no staged feed. A file a hand-run left behind must not upload."""
    scratch = str(tmp_path)
    stage_all(scratch)
    assert K.load_staged(scratch) == [] and K.load_staged(scratch, ()) == []
    got = K.load_staged(scratch, ("code_violations",))
    assert [r["notice_type"] for r in got] == ["code_violation"]


def test_a_staged_row_without_an_address_or_a_type_is_not_loaded(tmp_path):
    ftm_staging.stage(str(tmp_path), code_violations.FEED,
                      [{"notice_type": "code_violation", "address": ""}, {"address": "1 No Type Ln"}, "junk"], ["a"])
    assert K.load_staged(str(tmp_path), ALL) == []


def test_each_feed_belongs_to_the_stage_that_prepares_it():
    assert ftm_staging.feeds_for_stages(["notices", "capture", "liens", "county", "sold"]) == ()
    assert ftm_staging.feeds_for_stages(["liens", "appointments", "county"]) == ("substitute_trustee",)
    assert ftm_staging.feeds_for_stages(["code_violations", "tax_sale_report"]) == ("tax_sale_report", "code_violations")
    assert set(ftm_staging.STAGE_OF) == set(ALL)


def test_every_staged_feed_lands_on_a_named_list():
    assert K._list_name(appointment_row()) == "Substitute Trustee"
    assert K._list_name(tax_sale_row()) == "Tax Sale"
    assert K._list_name(code_row()) == "Code Violation", "not Condemned: that list is the agendas"


def test_a_condemnation_still_lands_on_condemned_and_a_lien_on_liens():
    assert K._list_name({"notice_type": "code_violation"}) == "Condemned"
    assert K._list_name({"notice_type": "lien"}) == "Liens"


def test_a_renamed_list_follows_the_setting(monkeypatch):
    monkeypatch.setenv("FTM_LIST_SUBSTITUTE_TRUSTEE", "Pre-Foreclosure")
    assert K._list_name(appointment_row()) == "Pre-Foreclosure"


def test_a_row_can_name_its_own_list_outright():
    assert K._list_name({"notice_type": "lien", "list_name": "Special List"}) == "Special List"


def test_a_type_nobody_named_a_list_for_has_no_list():
    assert K._list_name({"notice_type": "something_new"}) == ""


def test_the_appointment_reaches_the_upload_file_with_its_list_date_and_note():
    row = K.to_datasift(dict(appointment_row(), sm={}, sm_title="412 Sample Hollow Rd, Knoxville, TN 37918"), RUN_DATE)
    assert row["Lists"] == "Substitute Trustee" and row["Notice Type"] == "substitute_trustee"
    assert row["Date Added"] == "2026-11-04", "the county recording date, as for every county row"
    assert row["County"] == "Knox" and row["Property City"] == "Knoxville" and row["Property ZIP Code"] == "37918"
    assert "Substitute trustee appointed 2026-11-04" in row["Notes"] and "$185,000.00" in row["Notes"]
    assert "substitute_trustee" in row["Tags"] and "filed_2026-Q4" in row["Tags"] and "Knox" in row["Tags"]
    assert row["Tax Auction Date"] == "" and row["Foreclosure Date"] == ""


def test_a_tax_sale_row_carries_its_sale_date_in_a_column():
    row = K.to_datasift(dict(tax_sale_row(), sm={}), RUN_DATE)
    assert row["Lists"] == "Tax Sale" and row["Tax Auction Date"] == "2027-03-04"


def test_a_row_may_name_another_county_and_city_and_the_old_rows_stay_knox():
    row = K.to_datasift({"notice_type": "lien", "address": "5 Elsewhere Rd", "county": "Blount", "city": "Maryville",
                         "sm": {}}, RUN_DATE)
    assert row["County"] == "Blount" and row["Property City"] == "Maryville" and "Blount" in row["Tags"]
    old = K.to_datasift({"notice_type": "lien", "address": "5 Elsewhere Rd", "sm": {}}, RUN_DATE)
    assert old["County"] == "Knox" and old["Property City"] == "Knoxville" and "Knox" in old["Tags"]


def test_the_staged_rows_take_the_true_delinquency_check_like_a_lien():
    rows = [appointment_row(), tax_sale_row()]
    stats = K.apply_true_delinquency(rows, lookup=lambda acct: {"delinquent_amount": 912.5, "delinquent_year_list": ["2024"]},
                                     sleep=0)
    assert stats["checked"] == 2 and rows[0]["tax_due"] == 912.5 and rows[0]["notes"].startswith("Delinquent county tax $912.50")


def test_the_module_holds_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/knox_ftm_pull.py", "tests/test_knox_staged_feeds.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
