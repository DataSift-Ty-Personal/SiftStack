"""ftm_intake: the buy box for notices, their figures, the notice text, and Appeared Again.

Zero network. A fake stands in for SiftMap and a scripted stub for the DataSift API. Every
address, owner and notice below is invented.

Run: python -m pytest tests/test_ftm_intake.py
"""
import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api_upload as U  # noqa: E402
import ftm_intake as I  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

SWITCHES = ("FTM_INTAKE_SCREEN", "FTM_NOTICE_TEXT", "FTM_TAG_APPEARED_AGAIN", "FTM_INTAKE_DROP_UPSIDE_DOWN",
            "FTM_INTAKE_MAX_LOOKUPS", "FTM_INTAKE_TYPES", "FTM_INTAKE_VALUE_MAX", "FTM_INTAKE_VALUE_MIN",
            "FTM_INTAKE_NO_SALE_DATE_TAG", "FTM_INTAKE_LISTED_TAG", "FTM_NOTICE_TEXT_MAX", "FTM_RATE_BUDGET",
            "FTM_APPEARED_AGAIN_GAP_DAYS", "FTM_APPEARED_AGAIN_TAG")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)


def notice(address="412 Sample Hollow Rd", ntype="foreclosure", **kw):
    base = dict(address=address, city="Knoxville", state="TN", zip="37918", county="Knox", notice_type=ntype,
                owner_name="Marlowe Quillfeather", auction_date="2026-12-10",
                source_url="https://example.invalid/Details.aspx?ID=1001")
    base.update(kw)
    return NoticeData(**base)


class Map:
    """A SiftMap that knows a few invented houses."""

    def __init__(self, details=None, hits=None, fail=False):
        self.details, self.hits, self.fail = details or {}, hits, fail
        self.calls = []

    def autocomplete(self, query, limit=5):
        self.calls.append(("autocomplete", query))
        if self.fail:
            raise RuntimeError("503")
        if self.hits is not None:
            return self.hits
        street = query.split(",")[0]
        return [{"id": street.split()[0], "address": "%s, Knoxville, TN 37918" % street}]

    def get_detail(self, pid):
        self.calls.append(("detail", pid))
        return self.details.get(str(pid), {})


def house(value=180000, ptype="single_family_residence", mortgage=None, properties=1, **kw):
    d = {"property_type": ptype, "total_market_value": value,
         "owner_info": {"total_properties": properties, "total_mortgage": mortgage}}
    d.update(kw)
    return d


# ── off by default ────────────────────────────────────────────────────


def test_with_no_switch_set_nothing_is_screened_and_siftmap_is_never_asked():
    m = Map({"412": house(ptype="warehouse")})
    ns = [notice()]
    out = I.screen(ns, client=m)
    assert out["kept"] == ns and out["extras"] == {} and out["dropped"] == {}
    assert m.calls == []
    assert not I.enabled() and I.notice_text_mode() == "off" and not I.appeared_again_on()


def test_the_notice_text_is_not_added_unless_asked_for():
    assert I.text_extras([notice(raw_text="NOTICE OF SALE ...")]) == {}


# ── the screen ────────────────────────────────────────────────────────


def test_a_single_family_house_in_the_band_is_kept(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice()
    out = I.screen([n], client=Map({"412": house()}))
    assert out["kept"] == [n] and out["dropped"] == {}


def test_another_property_type_is_left_out_with_its_reason(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice()
    out = I.screen([n], client=Map({"412": house(ptype="multi_family_commercial")}))
    assert out["kept"] == []
    assert out["dropped"] == {"not single family (multi family commercial)": 1}
    assert out["rejected"] == [(n, "not single family (multi family commercial)")]


def test_a_value_outside_the_band_is_left_out(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    out = I.screen([notice()], client=Map({"412": house(value=1250000)}))
    assert out["dropped"] == {"value outside the buy box": 1}


def test_the_one_dollar_floor_keeps_a_cheap_house(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    out = I.screen([notice()], client=Map({"412": house(value=18000)}))
    assert len(out["kept"]) == 1


def test_not_found_on_siftmap_keeps_the_record(monkeypatch):
    """A missed match is not a finding about the house. Only a positive verdict screens out."""
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice()
    out = I.screen([n], client=Map(hits=[]))
    assert out["kept"] == [n] and out["not_verified"] == 1
    assert "not verified" in out["extras"][id(n)]["_note"]


def test_a_siftmap_outage_keeps_every_record_and_says_so(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    ns = [notice("%d Sample Hollow Rd" % i) for i in range(400, 406)]
    out = I.screen(ns, client=Map(fail=True))
    assert out["kept"] == ns and out["dropped"] == {}
    assert any("verified none of 6" in w for w in out["warnings"])


def test_a_house_with_no_type_or_no_value_on_file_is_kept(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    out = I.screen([notice()], client=Map({"412": {"owner_info": {}}}))
    assert len(out["kept"]) == 1


def test_the_first_search_hit_is_not_taken_on_trust(monkeypatch):
    """The same street name in another town must not lend its house to this record."""
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    wrong = [{"id": "999", "address": "412 Sample Hollow Rd, Memphis, TN 38103"},
             {"id": "998", "address": "414 Sample Hollow Rd, Knoxville, TN 37918"}]
    m = Map({"999": house(ptype="warehouse"), "998": house(ptype="warehouse")}, hits=wrong)
    out = I.screen([notice()], client=m)
    assert len(out["kept"]) == 1 and out["not_verified"] == 1
    assert not [c for c in m.calls if c[0] == "detail"], "no detail was read for a house that is not this one"


def test_the_allowed_types_are_a_setting(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    monkeypatch.setenv("FTM_INTAKE_TYPES", "single_family_residence, mobile_home")
    out = I.screen([notice()], client=Map({"412": house(ptype="mobile_home")}))
    assert len(out["kept"]) == 1


def test_the_lookup_cap_keeps_the_rest_unscreened(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    monkeypatch.setenv("FTM_INTAKE_MAX_LOOKUPS", "1")
    m = Map({"412": house(), "414": house(ptype="warehouse")})
    out = I.screen([notice(), notice("414 Sample Hollow Rd")], client=m)
    assert len(out["kept"]) == 2 and out["looked_up"] == 1


def test_force_runs_the_screen_for_a_feed_that_always_needs_it():
    out = I.screen([notice()], force=True, client=Map({"412": house(ptype="warehouse")}))
    assert out["kept"] == []


# ── figures ───────────────────────────────────────────────────────────


def test_a_foreclosure_with_no_loan_on_file_is_not_called_free_and_clear():
    """The notice exists because a loan is in default. No mortgage row means unknown, not 100% equity."""
    got = I.figures(notice(), house(value=200000, mortgage=None))
    assert "Est Equity" not in got and "Mortgage Debt" not in got


def test_the_same_house_as_a_probate_is_free_and_clear():
    got = I.figures(notice(ntype="probate"), house(value=200000, mortgage=None))
    assert got["Est Equity"] == 200000


def test_the_debt_and_the_equity_come_from_the_loan_on_record():
    got = I.figures(notice(tax_delinquent_amount="1500.00"), house(value=200000, mortgage=140000))
    assert got["Mortgage Debt"] == 140000 and got["Est Equity"] == 60000
    assert got["Total Delinquency"] == 1500 and got["Debt vs Equity Pct"] == 2.5
    assert "SiftMap" in got["_debt_source"]


def test_the_notice_states_the_debt_when_siftmap_has_none():
    text = "securing the original principal amount of $132,500.00 and recorded in the Register's Office"
    got = I.figures(notice(raw_text=text), house(mortgage=None))
    assert got["Mortgage Debt"] == 132500 and "notice states" in got["_debt_source"]
    assert I.stated_principal("for the sum of ten dollars") is None
    assert I.stated_principal("principal amount of $75.00") is None, "not a loan"


def test_an_upside_down_record_is_flagged_and_kept_unless_the_setting_says_drop(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice(tax_delinquent_amount="95000")
    out = I.screen([n], client=Map({"412": house(value=100000, mortgage=40000)}))
    assert out["kept"] == [n] and out["extras"][id(n)]["Upside Down"] == "yes"
    monkeypatch.setenv("FTM_INTAKE_DROP_UPSIDE_DOWN", "1")
    out = I.screen([n], client=Map({"412": house(value=100000, mortgage=40000)}))
    assert out["dropped"] == {"debt is larger than the equity": 1}


# ── sale date, listing ────────────────────────────────────────────────


def test_a_foreclosure_with_no_sale_date_is_tagged_for_a_look_up(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice(auction_date="")
    out = I.screen([n], client=Map({"412": house()}))
    assert out["extras"][id(n)]["_tags"] == ["Sale Date Needed"]
    assert any("no sale date" in w for w in out["warnings"])
    assert "no sale date in the notice" in out["extras"][id(n)]["_note"]


def test_a_probate_never_needs_a_sale_date(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice(ntype="probate", auction_date="")
    out = I.screen([n], client=Map({"412": house()}))
    assert "_tags" not in out["extras"][id(n)]
    assert not I.needs_sale_date(n)


def test_the_listing_status_prefers_the_listing_feed_over_siftmap():
    assert I.listing_status(notice(mls_status="Active"), {"mls_status": "not_listed"}) == "listed"
    assert I.listing_status(notice(), {"mls_status": "pending"}) == "pending"
    assert I.listing_status(notice(mls_status="Off Market"), None) == "off market"
    assert I.listing_status(notice(mls_status="Sold"), None) == "off market"
    assert I.listing_status(notice(), {"mls_history": [{"status": "active"}]}) == "", "nothing is inferred from a history"
    assert I.listing_status(notice(), None) == ""


def test_only_a_listed_or_pending_house_gets_the_listed_tag(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    a, b = notice(mls_status="Pending"), notice("414 Sample Hollow Rd", mls_status="Off Market")
    out = I.screen([a, b], client=Map({"412": house(), "414": house()}))
    assert out["extras"][id(a)]["_tags"] == ["Listed"] and out["extras"][id(a)]["Listing Status"] == "pending"
    assert "_tags" not in out["extras"][id(b)] and out["extras"][id(b)]["Listing Status"] == "off market"


def test_a_tag_can_be_switched_off_by_an_empty_setting(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    monkeypatch.setenv("FTM_INTAKE_NO_SALE_DATE_TAG", "")
    n = notice(auction_date="")
    out = I.screen([n], client=Map({"412": house()}))
    assert "_tags" not in out["extras"][id(n)]


# ── the notice text ───────────────────────────────────────────────────


def test_the_notice_text_is_cleaned_and_a_cut_is_marked(monkeypatch):
    n = notice(raw_text="NOTICE OF   SALE\n\n\n  WHEREAS,   default has occurred \n" + ("word " * 400))
    assert I.notice_text(n, limit=10000).startswith("NOTICE OF SALE\nWHEREAS, default has occurred")
    cut = I.notice_text(n, limit=200)
    assert cut.endswith("[cut at 200 characters]") and len(cut) < 240


def test_the_text_rides_as_a_column_in_both_modes(monkeypatch):
    n = notice(raw_text="NOTICE OF SALE of the property at 412 Sample Hollow Rd")
    for mode in ("field", "notes"):
        monkeypatch.setenv("FTM_NOTICE_TEXT", mode)
        assert I.text_extras([n])[id(n)]["Notice Text"].startswith("NOTICE OF SALE")
    monkeypatch.setenv("FTM_NOTICE_TEXT", "banana")
    assert I.notice_text_mode() == "off"


def test_the_text_is_mapped_to_a_field_only_in_field_mode(monkeypatch):
    assert "Notice Text" not in U.field_map()
    monkeypatch.setenv("FTM_NOTICE_TEXT", "notes")
    assert "Notice Text" not in U.field_map()
    monkeypatch.setenv("FTM_NOTICE_TEXT", "field")
    assert U.field_map()["Notice Text"] == "Notice Text"


# ── the upload file ───────────────────────────────────────────────────


def test_extras_fold_into_the_row_without_losing_what_was_there():
    row = {"Tags": "Courthouse Data,FTM,foreclosure", "Notes": "Foreclosure | Auction: 12/10/2026"}
    I.apply_extras(row, {"Est Equity": 60000, "_tags": ["Listed", "FTM"], "_note": "Intake check: value $200,000."})
    assert row["Tags"] == "Courthouse Data,FTM,foreclosure,Listed", "no duplicate tag"
    assert row["Notes"].startswith("Intake check:") and row["Notes"].endswith("Auction: 12/10/2026")
    assert row["Est Equity"] == 60000 and "_tags" not in row


def test_the_intake_file_carries_the_pipelines_own_columns_plus_the_extras(tmp_path):
    from datasift_formatter import DATASIFT_COLUMNS
    n = notice()
    path = I.write_csv([n], {id(n): {"Est Equity": 60000, "Mortgage Debt": 140000.0}}, "x.csv", out_dir=tmp_path)
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0])[:len(DATASIFT_COLUMNS)] == DATASIFT_COLUMNS
    assert rows[0]["Est Equity"] == "60000" and rows[0]["Mortgage Debt"] == "140000.0"
    assert rows[0]["Property Street Address"] == "412 Sample Hollow Rd" and rows[0]["Lists"] == "Foreclosure"


def test_every_record_left_out_is_written_down_with_its_reason(tmp_path):
    n = notice()
    assert I.write_rejects([(n, "not single family (warehouse)")], tmp_path / "r.csv") == 1
    with open(tmp_path / "r.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["address"] == "412 Sample Hollow Rd" and rows[0]["reason"] == "not single family (warehouse)"
    assert I.write_rejects([], tmp_path / "none.csv") == 0 and not (tmp_path / "none.csv").exists()


# ── appeared again ────────────────────────────────────────────────────


def ledger(tmp_path, today="2026-11-13"):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return I.AppearedLedger(state_dir=tmp_path, out_dir=out, today=today)


def upload_file(tmp_path, name, rows):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    with open(out / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Property Street Address", "Property ZIP Code", "Lists"])
        w.writeheader()
        w.writerows(rows)


def test_a_property_never_sent_to_this_list_appears_again(tmp_path):
    assert ledger(tmp_path).should_tag("412 Sample Hollow Rd", "37918", ["Foreclosure"])


def test_the_mornings_re_send_of_the_same_row_never_tags(tmp_path):
    """The county stage re-sends the same condemnation rows every day."""
    led = ledger(tmp_path, "2026-11-12")
    led.record("77 Invented Ave", "37917", ["Condemned"])
    led.save()
    again = ledger(tmp_path, "2026-11-13")
    assert not again.should_tag("77 Invented Ave", "37917", ["Condemned"])
    assert again.should_tag("77 Invented Ave", "37917", ["Liens"]), "a NEW list for the same house does"


def test_street_and_st_are_the_same_house(tmp_path):
    led = ledger(tmp_path, "2026-11-12")
    led.record("412 Sample Hollow Road", "37918", ["Foreclosure"])
    assert not led.should_tag("412 SAMPLE HOLLOW RD", "37918", ["Foreclosure"])


def test_a_filing_after_a_long_silence_appears_again(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_APPEARED_AGAIN_GAP_DAYS", "180")
    led = ledger(tmp_path, "2025-12-01")
    led.record("412 Sample Hollow Rd", "37918", ["Foreclosure"])
    led.save()
    assert not ledger(tmp_path, "2026-03-01").should_tag("412 Sample Hollow Rd", "37918", ["Foreclosure"])
    assert ledger(tmp_path, "2026-11-13").should_tag("412 Sample Hollow Rd", "37918", ["Foreclosure"])


def test_the_ledger_is_seeded_from_the_upload_files_already_on_the_volume(tmp_path):
    """Day one must not announce everything we uploaded before the ledger existed."""
    upload_file(tmp_path, "ftm_notices_2026-11-10_063000.csv",
                [{"Property Street Address": "412 Sample Hollow Rd", "Property ZIP Code": "37918", "Lists": "Foreclosure"}])
    upload_file(tmp_path, "knox_ftm_pull_2026-11-12.csv",
                [{"Property Street Address": "77 Invented Ave", "Property ZIP Code": "37917", "Lists": "Condemned"}])
    upload_file(tmp_path, "knox_ftm_pull_2026-11-12_upside_down.csv",
                [{"Property Street Address": "9 Skipped Ct", "Property ZIP Code": "37917", "Lists": "Liens"}])
    upload_file(tmp_path, "ftm_notices_2026-11-13_063000.csv",
                [{"Property Street Address": "5 Today Ln", "Property ZIP Code": "37918", "Lists": "Probate"}])
    led = ledger(tmp_path, "2026-11-13")
    assert not led.should_tag("412 Sample Hollow Rd", "37918", ["Foreclosure"])
    assert not led.should_tag("77 Invented Ave", "37917", ["Condemned"])
    assert led.should_tag("9 Skipped Ct", "37917", ["Liens"]), "a row held out of the upload was never sent"
    assert led.should_tag("5 Today Ln", "37918", ["Probate"]), "today's own file is judged, not pre-seeded"


def test_a_row_with_no_address_or_no_list_is_never_tagged(tmp_path):
    led = ledger(tmp_path)
    assert not led.should_tag("", "37918", ["Foreclosure"])
    assert not led.should_tag("412 Sample Hollow Rd", "37918", [])


# ── the uploader, with the rule on and off ────────────────────────────

UUID = "11111111-2222-3333-4444-555555555555"
DUP = '{"non_field_errors":["Property address already exists!"],"property":["%s"]}' % UUID


class Stub:
    def __init__(self, exists=True, notes=""):
        self.exists, self.notes, self.calls = exists, notes, []

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == U.CREATE_ROUTE:
            if self.exists:
                raise U.ApiError(400, "POST", path, DUP)
            return {"uuid": UUID}
        if method == "GET" and path == "/api/internal/property/%s/" % UUID:
            return {"tags": ["Courthouse Data"], "owner": {"first_name": "Marlowe", "last_name": "Quillfeather"},
                    "notes": self.notes}
        return {}

    def tagged(self):
        return [c[2] for c in self.calls if c[1].endswith("/add-tags/")]


ROW = {"Property Street Address": "412 Sample Hollow Rd", "Property City": "Knoxville", "Property State": "TN",
       "Property ZIP Code": "37918", "Owner First Name": "Marlowe", "Owner Last Name": "Quillfeather",
       "Lists": "Foreclosure", "Tags": "Courthouse Data"}


@pytest.fixture
def uploader(tmp_path, monkeypatch):
    import config
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")

    def go(stub, **kw):
        monkeypatch.setattr(U, "Api", lambda: stub)
        return U.upload_rows([dict(ROW, **kw.pop("row", {}))], commit=True, sleep=0, out_dir=str(tmp_path / "out"), **kw)
    return go


def test_the_uploader_tags_nothing_when_the_rule_is_off(uploader):
    stub = Stub(exists=True)
    res = uploader(stub, new_filing=True)
    assert stub.tagged() == [] and res["appeared_again"] == 0 and res["created"] == 1


def test_an_existing_record_with_a_new_filing_is_tagged_once(uploader, monkeypatch):
    monkeypatch.setenv("FTM_TAG_APPEARED_AGAIN", "1")
    stub = Stub(exists=True)
    res = uploader(stub, new_filing=True)
    assert stub.tagged() == [{"tags": "Appeared Again"}] and res["appeared_again"] == 1
    second = Stub(exists=True)
    res = uploader(second, new_filing=True)
    assert second.tagged() == [] and res["appeared_again"] == 0, "the same filing the next morning is not new"


def test_a_brand_new_record_is_not_tagged(uploader, monkeypatch):
    monkeypatch.setenv("FTM_TAG_APPEARED_AGAIN", "1")
    stub = Stub(exists=False)
    uploader(stub, new_filing=True)
    assert stub.tagged() == []
    later = Stub(exists=True)
    uploader(later, new_filing=True)
    assert later.tagged() == [], "we created it yesterday; it did not appear again"


def test_a_hand_re_send_never_tags_even_with_the_rule_on(uploader, monkeypatch):
    monkeypatch.setenv("FTM_TAG_APPEARED_AGAIN", "1")
    stub = Stub(exists=True)
    uploader(stub)                                 # new_filing defaults to False
    assert stub.tagged() == []


def test_the_tag_name_is_a_setting(uploader, monkeypatch):
    monkeypatch.setenv("FTM_TAG_APPEARED_AGAIN", "1")
    monkeypatch.setenv("FTM_APPEARED_AGAIN_TAG", "Second Filing")
    stub = Stub(exists=True)
    uploader(stub, new_filing=True)
    assert stub.tagged() == [{"tags": "Second Filing"}]


def test_notes_mode_adds_the_text_once(uploader, monkeypatch):
    monkeypatch.setenv("FTM_NOTICE_TEXT", "notes")
    text = "NOTICE OF SALE of the property at 412 Sample Hollow Rd, default having occurred under a deed of trust"
    stub = Stub(exists=False)
    uploader(stub, row={"Notice Text": text})
    added = [c[2]["notes"] for c in stub.calls if c[1].endswith("/add-notes/")]
    assert added == ["Notice text: " + text]
    again = Stub(exists=True, notes="Notice text: " + text)
    uploader(again, row={"Notice Text": text})
    assert not [c for c in again.calls if c[1].endswith("/add-notes/")], "a republished notice is not stacked"


def test_field_mode_sends_the_text_as_a_custom_field_and_not_as_a_note(uploader, monkeypatch):
    monkeypatch.setenv("FTM_NOTICE_TEXT", "field")
    monkeypatch.setattr(U, "field_index", lambda api: {"Notice Text": {"uuid": "f-text", "field_type": "text", "options": {}}})
    stub = Stub(exists=False)
    uploader(stub, row={"Notice Text": "NOTICE OF SALE ..."})
    patches = [c[2] for c in stub.calls if c[0] == "PATCH" and c[1].endswith("/custom-field/update-values/")]
    assert patches == [[{"field_uuid": "f-text", "value": "NOTICE OF SALE ..."}]]
    assert not [c for c in stub.calls if c[1].endswith("/add-notes/")]


def test_the_module_holds_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/ftm_intake.py", "tests/test_ftm_intake.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
