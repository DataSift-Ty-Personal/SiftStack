"""ftm_feeds and ftm_staging: the feed table, the routes by list, and the staging ledger.

Zero network. Invented keys and addresses.

Run: python -m pytest tests/test_ftm_feeds.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_feeds as F  # noqa: E402
import ftm_staging as S  # noqa: E402

TODAY_ARGS = "--stages notices,capture,liens,county,sold --commit"


# ── the feed table ────────────────────────────────────────────────────


def test_todays_run_holds_exactly_the_feeds_it_always_had():
    live = {f.key for f in F.live_feeds(F.requested_stages(TODAY_ARGS))}
    assert live == {"knox_foreclosure", "blount_foreclosure", "knox_probate", "blount_probate",
                    "knox_condemned", "knox_liens"}


def test_no_new_feed_is_live_without_its_stage_name():
    live = {f.key for f in F.live_feeds(F.requested_stages(TODAY_ARGS))}
    for key in ("knox_substitute_trustee", "knox_tax_sale", "blount_tax_sale", "knox_tax_delinquent",
                "blount_tax_delinquent", "knox_tax_sale_report", "knox_code_violations", "public_sale",
                "court_order"):
        assert key not in live, "%s must stay off until its stage is named" % key


def test_each_new_feed_has_its_own_stage_so_it_can_be_switched_alone():
    stage = {f.key: f.stage for f in F.FEEDS}
    assert stage["knox_tax_sale"] == "tax_sale" and stage["knox_tax_delinquent"] == "tax_delinquent"
    assert stage["knox_substitute_trustee"] == "appointments"
    assert stage["knox_tax_sale_report"] == "tax_sale_report"
    assert stage["knox_code_violations"] == "code_violations"
    assert stage["public_sale"] == stage["court_order"] == "public_notices"
    for s in set(stage.values()) - set(F.TODAY_STAGES):
        assert s not in F.requested_stages(TODAY_ARGS)


def test_a_recorder_feed_is_not_live_without_the_stage_that_uploads_it():
    """appointments only STAGES rows. Named without county, nothing would ever upload them."""
    keys = {f.key for f in F.live_feeds(["appointments"])}
    assert "knox_substitute_trustee" not in keys
    keys = {f.key for f in F.live_feeds(["appointments", "county"])}
    assert "knox_substitute_trustee" in keys


def test_requested_stages_reads_both_spellings_of_the_flag():
    assert F.requested_stages("--stages=notices,county --commit") == ["notices", "county"]
    assert F.requested_stages("--commit") == []
    assert F.requested_stages("") == []


def test_the_tax_delinquent_notice_feed_does_not_share_datasifts_own_list_name():
    """DataSift attaches a "Tax Delinquent" list by itself. A courthouse upload on that same
    list could not be told from it by the sold guard."""
    assert F.BY_KEY["knox_tax_delinquent"].list_name != "Tax Delinquent"


def test_a_list_can_be_renamed_by_a_setting(monkeypatch):
    assert F.BY_KEY["knox_substitute_trustee"].list_name == "Substitute Trustee"
    monkeypatch.setenv("FTM_LIST_SUBSTITUTE_TRUSTEE", "Pre-Foreclosure")
    assert F.BY_KEY["knox_substitute_trustee"].list_name == "Pre-Foreclosure"
    assert "Pre-Foreclosure" in F.routes()


def test_the_lists_in_todays_run_cannot_be_renamed():
    for key in ("knox_foreclosure", "knox_probate", "knox_condemned", "knox_liens"):
        assert F.BY_KEY[key].list_env == ""


def test_a_code_violation_file_row_is_kept_off_the_condemned_list():
    assert F.list_for("code_violation", "capture") == "Condemned"
    assert F.list_for("code_violation", "code_violations") == "Code Violation"


def test_public_notice_searches_have_no_default_label(monkeypatch):
    monkeypatch.delenv("FTM_PUBLIC_NOTICE_SEARCHES", raising=False)
    assert F.public_notice_searches() == []
    monkeypatch.setenv("FTM_PUBLIC_NOTICE_SEARCHES",
                       "knox|public_sale|Public Sale Test;Knox|order|Orders Test;Knox|bogus|X;broken")
    assert F.public_notice_searches() == [("Knox", "public_sale", "Public Sale Test"), ("Knox", "order", "Orders Test")]


def test_a_saved_search_label_can_be_corrected_by_a_setting(monkeypatch):
    assert F.BY_KEY["knox_tax_sale"].search_label == "Tax Sale V2 Knox"
    monkeypatch.setenv("FTM_SEARCH_KNOX_TAX_SALE", "Tax Sale V3 Knox")
    assert F.BY_KEY["knox_tax_sale"].search_label == "Tax Sale V3 Knox"


def test_one_search_of_a_feed_can_be_switched_off(monkeypatch):
    """A search that really holds nothing reads as unreadable and would fail its stage daily."""
    import ftm_feed_runner
    monkeypatch.setenv("FTM_SEARCH_BLOUNT_TAX_SALE", "off")
    assert F.BY_KEY["blount_tax_sale"].search_label == ""
    assert [s[2] for s in ftm_feed_runner.spec_for("tax_sale").searches] == ["Tax Sale V2 Knox"]


# ── routes by list (build plan 1.17) ──────────────────────────────────


def test_liens_are_researched_only_at_exhaustion_and_condemned_on_arrival():
    r = F.routes()
    assert r["Liens"]["research"] == F.AT_EXHAUSTION
    assert r["Condemned"]["research"] == F.ON_ARRIVAL
    assert r["Foreclosure"]["research"] == F.ON_ARRIVAL and r["Probate"]["research"] == F.ON_ARRIVAL
    assert r["Liens"]["in_todays_run"] and r["Condemned"]["in_todays_run"]


def test_sale_dated_lists_ride_the_hot_lane_and_the_appointment_gets_a_long_runway():
    r = F.routes()
    assert r["Foreclosure"]["hot_by_sale_date"] and r["Tax Sale"]["hot_by_sale_date"]
    assert not r["Liens"]["hot_by_sale_date"]
    assert r["Substitute Trustee"]["long_runway"] and not r["Substitute Trustee"]["in_todays_run"]


def test_tax_delinquent_and_code_violations_carry_the_ninety_day_second_chance():
    r = F.routes()
    assert r[F.BY_KEY["knox_tax_delinquent"].list_name]["second_chance_days"] == 90
    assert r["Code Violation"]["second_chance_days"] == 90


def test_the_routes_print_as_json():
    assert "Liens" in json.loads(json.dumps(F.routes()))


# ── the staging ledger ────────────────────────────────────────────────


def test_nothing_staged_means_nothing_settled_and_nothing_written(tmp_path):
    assert S.mark_all_uploaded(str(tmp_path)) == {}
    assert os.listdir(tmp_path) == [], "a run with no staged feed must leave the folder untouched"
    assert not S.anything_pending(str(tmp_path))


def test_a_staged_filing_is_new_until_the_upload_settles_it(tmp_path):
    scratch = str(tmp_path)
    S.stage(scratch, "substitute_trustee", [{"address": "12 Sample Ln"}], ["INST-1", "INST-2"])
    assert S.is_new(scratch, "substitute_trustee", "INST-1"), "staged is not handled"
    assert S.pending_rows(scratch, "substitute_trustee") == [{"address": "12 Sample Ln"}]
    assert S.mark_uploaded(scratch, "substitute_trustee", today="2026-11-13") == 2
    assert not S.is_new(scratch, "substitute_trustee", "INST-1")
    assert not S.is_new(scratch, "substitute_trustee", "INST-2"), "a settled miss is not looked up again"
    assert S.pending_rows(scratch, "substitute_trustee") == [], "emptied so it cannot be sent twice"


def test_staging_replaces_what_was_pending(tmp_path):
    scratch = str(tmp_path)
    S.stage(scratch, "code_violations", [{"address": "1 Old Ct"}], ["A"])
    S.stage(scratch, "code_violations", [{"address": "2 New Ct"}], ["B"])
    assert S.pending_rows(scratch, "code_violations") == [{"address": "2 New Ct"}]
    S.mark_uploaded(scratch, "code_violations")
    assert S.is_new(scratch, "code_violations", "A"), "a key that was never uploaded stays open"


def test_each_feed_keeps_its_own_ledger(tmp_path):
    scratch = str(tmp_path)
    S.stage(scratch, "tax_sale_report", [], ["K1"])
    S.mark_all_uploaded(scratch)
    assert not S.is_new(scratch, "tax_sale_report", "K1")
    assert S.is_new(scratch, "code_violations", "K1")


def test_a_blank_key_is_never_new(tmp_path):
    assert not S.is_new(str(tmp_path), "code_violations", "")


def test_a_broken_ledger_file_reads_as_empty_not_as_a_crash(tmp_path):
    (tmp_path / "substitute_trustee_handled.json").write_text("{not json", encoding="utf-8")
    assert S.handled(str(tmp_path), "substitute_trustee") == {}
