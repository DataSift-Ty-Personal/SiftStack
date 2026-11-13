"""ftm_health: feed health (lag, monthly volume, last good day, days left on paid sources).

Zero network, no files outside a temp folder. The upload rows below are invented.

The rule these tests hold hardest: with no new setting the daily post and the verdict are
exactly what they were. New facts live in the full report; a feed finding reaches the post only
as the reason line of a red day.

Run: python -m pytest tests/test_ftm_health_feeds.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_health as H  # noqa: E402

TODAY = "2026-11-13"
TODAY_ARGS = "--stages notices,capture,liens,county,sold --commit"
STAGES = ["notices", "capture", "liens", "county", "sold"]


def notice_row(i, county="Knox", ntype="foreclosure", lst="Foreclosure"):
    return {"Property Street Address": "%d Sample Hollow Rd" % i, "Property ZIP Code": "37918", "County": county,
            "Notice Type": ntype, "Lists": lst, "Source URL": "https://example.invalid/Details.aspx?SID=x&ID=%d" % (7000 + i)}


def county_row(i, lst="Liens", ntype="lien", filed="2026-11-10"):
    return {"Property Street Address": "%d Invented Ave" % i, "Property ZIP Code": "37917", "County": "Knox",
            "Notice Type": ntype, "Lists": lst, "Date Added": filed, "Source URL": "https://example.invalid/rod"}


@pytest.fixture(autouse=True)
def no_switches(monkeypatch):
    for name in ("FTM_FEED_LAG_ALARM_DAYS", "FTM_HEALTH_ALARM_FEEDS", "FTM_ARGS", "FTM_FEED_HEALTH",
                 "FTM_LIST_SUBSTITUTE_TRUSTEE", "FTM_RATE_BUDGET"):
        monkeypatch.delenv(name, raising=False)


def run_record(day, **stages):
    return {"started": "%sT06:30:00" % day, "finished": "%sT06:50:00" % day, "committed": True, "blocked": False,
            "stages": [dict(name=k, status=v, detail="", warnings=[]) for k, v in stages.items()]}


def base(**kw):
    out = dict(feed_last={"%s/%s" % (c, t): TODAY for c, t in H.FEEDS}, feed_today={}, new_by_source={},
               uploaded_by_source={}, requested_stages=list(STAGES), ledger_seeded=False)
    out.update(kw)
    return out


# ── which feed a row belongs to ───────────────────────────────────────


def test_a_notice_row_is_placed_by_its_county_and_type():
    assert H.feed_key(notice_row(1), "notices") == "knox_foreclosure"
    assert H.feed_key(notice_row(1, "Blount", "probate", "Probate"), "notices") == "blount_probate"
    assert H.feed_key(notice_row(1, "Knox", "tax_sale", "Tax Sale"), "notices") == "", "not a core notice feed"
    assert H.feed_key(notice_row(1, "Knox", "tax_sale", "Tax Sale"), "feeds") == "knox_tax_sale"
    assert H.feed_key(notice_row(1, "Knox", "order", "Court Order"), "feeds") == "court_order"


def test_a_county_row_is_placed_by_the_list_it_landed_on():
    assert H.feed_key(county_row(1), "county") == "knox_liens"
    assert H.feed_key(county_row(1, "Condemned", "code_violation"), "county") == "knox_condemned"
    assert H.feed_key(county_row(1, "Code Violation", "code_violation"), "county") == "knox_code_violations"
    assert H.feed_key(county_row(1, "Substitute Trustee", "substitute_trustee"), "county") == "knox_substitute_trustee"
    assert H.feed_key(county_row(1, "Some Other List"), "county") == ""


def test_a_renamed_list_is_still_placed(monkeypatch):
    monkeypatch.setenv("FTM_LIST_SUBSTITUTE_TRUSTEE", "Pre-Foreclosure")
    assert H.feed_key(county_row(1, "Pre-Foreclosure", "substitute_trustee"), "county") == "knox_substitute_trustee"


# ── lag, volume, last good day ────────────────────────────────────────


def test_lag_is_the_pull_day_minus_the_filing_day_on_new_records():
    families = {"notices": {TODAY: [notice_row(1), notice_row(2), notice_row(3)]},
                "county": {TODAY: [county_row(1, filed="2026-11-08")]}, "feeds": {}}
    seen = {"7001": "2026-11-13", "7002": "2026-11-12", "7003": "2026-11-09"}
    rep = H.feed_report(TODAY, families, seen)
    fc = rep["knox_foreclosure"]
    assert fc["new_today"] == 3 and fc["lag_median"] == 1 and fc["lag_max"] == 4 and fc["lag_measured"] == 3
    assert rep["knox_liens"]["lag_median"] == 5


def test_a_row_that_was_sent_before_is_not_new_and_is_not_measured():
    """The county stage re-sends the same rows every morning. A re-send a month old is not a
    feed running a month late."""
    families = {"notices": {}, "feeds": {},
                "county": {"2026-10-13": [county_row(1, "Condemned", "code_violation", "2026-10-10")],
                           TODAY: [county_row(1, "Condemned", "code_violation", "2026-10-10")]}}
    rep = H.feed_report(TODAY, families, {})["knox_condemned"]
    assert rep["new_today"] == 0 and rep["lag_median"] is None and rep["last_new"] == "2026-10-13"


def test_volume_is_counted_per_feed_this_month_and_last():
    families = {"feeds": {}, "county": {},
                "notices": {"2026-09-30": [notice_row(1)], "2026-10-05": [notice_row(2), notice_row(3)],
                            "2026-11-02": [notice_row(4)], TODAY: [notice_row(5), notice_row(1)]}}
    rep = H.feed_report(TODAY, families, {})["knox_foreclosure"]
    assert rep["this_month"] == 2 and rep["last_month"] == 2 and rep["new_today"] == 1
    assert rep["last_new"] == TODAY


def test_a_new_record_with_no_filing_date_is_counted_apart_not_guessed():
    rep = H.feed_report(TODAY, {"notices": {TODAY: [notice_row(1)]}, "county": {}, "feeds": {}}, {})
    assert rep["knox_foreclosure"]["lag_median"] is None and rep["knox_foreclosure"]["undated_today"] == 1


def test_a_probate_falls_back_to_its_open_date():
    row = dict(notice_row(1, "Knox", "probate", "Probate"), **{"Probate Open Date": "11/11/2026"})
    rep = H.feed_report(TODAY, {"notices": {TODAY: [row]}, "county": {}, "feeds": {}}, {})
    assert rep["knox_probate"]["lag_median"] == 2


def test_the_earlier_signal_is_measured_against_the_sale_notices_that_follow():
    appt = county_row(1, "Substitute Trustee", "substitute_trustee", "2026-10-01")
    sale = dict(notice_row(9), **{"Property Street Address": "1 Invented Ave", "Property ZIP Code": "37917"})
    families = {"county": {"2026-10-02": [appt]}, "notices": {"2026-11-10": [sale]}, "feeds": {}}
    assert H.feed_report(TODAY, families, {})["_followed"] == {"appointments": 1, "later_a_sale_notice": 1}


# ── findings: nothing new unless a switch asks ────────────────────────


def late_report():
    return {"knox_foreclosure": {"new_today": 4, "lag_median": 5, "lag_max": 6, "last_new": TODAY},
            "knox_liens": {"new_today": 0, "lag_median": None, "last_new": "2026-10-01"},
            "knox_condemned": {"new_today": 0, "lag_median": None, "last_new": "2026-08-01"}}


def test_with_no_switch_a_late_feed_and_a_silent_lien_feed_add_no_finding():
    red, amber = H.feed_findings(TODAY, late_report(), [run_record(TODAY, notices="ok")], STAGES)
    assert red == [] and amber == []


def test_the_lag_alarm_is_a_setting(monkeypatch):
    monkeypatch.setenv("FTM_FEED_LAG_ALARM_DAYS", "2")
    red, _ = H.feed_findings(TODAY, late_report(), [], STAGES)
    assert red == ["Foreclosure notices, Knox is running 5 days behind its filings (4 new today)."]


def test_a_lag_inside_the_limit_is_not_an_alarm(monkeypatch):
    monkeypatch.setenv("FTM_FEED_LAG_ALARM_DAYS", "2")
    rep = {"knox_foreclosure": {"new_today": 4, "lag_median": 2, "last_new": TODAY}}
    assert H.feed_findings(TODAY, rep, [], STAGES)[0] == []


def test_liens_and_condemned_are_judged_by_silence_only_when_named(monkeypatch):
    monkeypatch.setenv("FTM_HEALTH_ALARM_FEEDS", "knox_liens")
    red, _ = H.feed_findings(TODAY, late_report(), [], STAGES)
    assert red == ["Liens, Knox has produced nothing for 43 days (last 2026-10-01)."]


def test_a_newly_switched_on_feed_is_judged_from_the_day_its_stage_first_ran():
    stages = STAGES + ["appointments"]
    runs = [run_record("2026-08-01", appointments="ok"), run_record(TODAY, appointments="ok")]
    red, _ = H.feed_findings(TODAY, {}, runs, stages)
    assert red == ["Substitute trustee appointments, Knox has produced nothing for 104 days "
                   "(never since it was switched on)."]
    fresh = [run_record("2026-11-01", appointments="ok")]
    assert H.feed_findings(TODAY, {}, fresh, stages)[0] == [], "twelve days in is not silence yet"


def test_a_feed_that_is_not_in_the_run_is_never_judged():
    runs = [run_record("2026-08-01", appointments="ok")]
    assert H.feed_findings(TODAY, {}, runs, STAGES)[0] == []


def test_feed_findings_come_after_the_runs_own_findings():
    """The post carries only the first red finding, and "the stage failed" names the cause."""
    runs = [run_record(TODAY, notices="failed")]
    d = H.analyze(runs, TODAY, **base(feed_red=["Liens, Knox has produced nothing for 43 days (last 2026-10-01)."]))
    assert d["verdict"] == "red"
    assert d["red"][0].startswith("Stage notices FAILED") and d["red"][-1].startswith("Liens, Knox")


def test_the_verdict_is_unchanged_when_no_feed_finding_is_passed():
    runs = [run_record(TODAY, notices="ok", capture="ok", liens="ok", county="ok", sold="ok")]
    assert H.analyze(runs, TODAY, **base())["verdict"] == "green"


# ── the post stays two lines ──────────────────────────────────────────


def green(**kw):
    runs = [run_record(TODAY, notices="ok", capture="ok", liens="ok", county="ok", sold="ok")]
    d = H.analyze(runs, TODAY, **base())
    d.update(window_days=14, baseline=8, ledger_size=900, history=[], new_by_list={"Foreclosure": 3, "Liens": 2},
             feeds={"knox_foreclosure": {"new_today": 3, "lag_median": 1, "lag_max": 2, "lag_measured": 3,
                                         "last_new": TODAY, "this_month": 30, "last_month": 41}},
             stages_requested=list(STAGES), paid_sources=[])
    d.update(kw)
    return d


def test_the_daily_post_is_still_two_lines_and_says_nothing_about_lag():
    post = H.render_short(green())
    body = [ln for ln in post.split("\n") if ln.strip()]
    assert len(body) == 2
    assert "lag" not in post.lower() and "month" not in post.lower() and "budget" not in post.lower()
    assert body[1].startswith("Foreclosure 3") and "Probate 0" in body[1] and "Condemned 0" in body[1]


def test_todays_lists_are_not_lengthened_by_the_feed_table():
    """Liens is in today's run. It still only shows when it has something, as before."""
    quiet = green(new_by_list={})
    assert "Liens" not in H.render_short(quiet)
    assert "Liens 2" in H.render_short(green())


def test_a_newly_switched_on_feed_shows_as_zero_instead_of_vanishing():
    d = green(new_by_list={}, stages_requested=STAGES + ["appointments"])
    assert "Substitute Trustee 0" in H.render_short(d)


def test_a_feed_finding_reaches_the_post_only_as_the_reason_line_of_a_red_day():
    runs = [run_record(TODAY, notices="ok")]
    d = H.analyze(runs, TODAY, **base(requested_stages=["notices"],
                                      feed_red=["Foreclosure notices, Knox is running 5 days behind its filings (4 new today)."]))
    d.update(window_days=14, baseline=8, ledger_size=900, history=[], new_by_list={}, stages_requested=["notices"])
    body = [ln for ln in H.render_short(d).split("\n") if ln.strip()]
    assert len(body) == 3 and body[0].startswith(":rotating_light:")
    assert body[2].startswith("Foreclosure notices, Knox is running 5 days behind")


# ── the full report ───────────────────────────────────────────────────


def test_the_full_report_carries_the_feed_table():
    text = H.render(green())
    assert "*Feed health*" in text
    assert ("Foreclosure notices, Knox: 3 new today, lag 1d (max 2d over 3), last good day today, "
            "30 this month, 41 last month") in text
    assert "Liens, Knox: 0 new today, no dated new record today, last good day never" in text
    assert "expects 100 to 200 new records a month" in text


def test_the_full_report_says_when_days_left_are_not_being_recorded():
    assert "Days left are not being recorded (set FTM_FEED_HEALTH=1 on the runner)." in H.render(green())


def test_the_full_report_shows_days_left_on_each_paid_source():
    paid = [{"source": "register_of_deeds", "days_left": 27, "expires": "", "read": TODAY, "age_days": 0, "state": "active"},
            {"source": "tnpublicnotice", "days_left": 28, "expires": "2026-12-11", "read": "2026-11-11", "age_days": 2, "state": ""}]
    text = H.render(green(paid_sources=paid))
    assert "register_of_deeds: 27 days left" in text
    assert "tnpublicnotice: 28 days left, expires 2026-12-11 (read 2d ago)" in text


def test_the_inventory_follows_the_run_instead_of_a_hand_written_list():
    states = {name: state for name, state, _ in H.sources(STAGES)}
    assert states["Liens, Knox"] == H.AUTOMATED, "liens have been a daily stage since 2026-11-11"
    assert states["Substitute trustee appointments, Knox"] == H.SWITCHED_OFF
    assert states["Trustee deeds (TRS)"] == H.RETIRED
    on = {name: state for name, state, _ in H.sources(STAGES + ["appointments"])}
    assert on["Substitute trustee appointments, Knox"] == H.AUTOMATED


def test_the_full_report_has_no_long_dash():
    text = H.render(green()) + H.render_short(green())
    assert chr(0x2014) not in text and chr(0x2013) not in text


# ── paid sources ──────────────────────────────────────────────────────


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return tmp_path


def test_days_left_are_recomputed_from_the_day_they_were_read(state_dir):
    (state_dir / "ftm_sources.json").write_text(json.dumps({
        "tnpublicnotice": {"expires": "2026-12-11", "days_left": 30, "read": "2026-11-11"},
        "register_of_deeds": {"days_left": 29, "state": "active", "read": "2026-11-11"}}), encoding="utf-8")
    got = {p["source"]: p for p in H.paid_sources(TODAY)}
    assert got["tnpublicnotice"]["days_left"] == 28
    assert got["register_of_deeds"]["days_left"] == 27 and got["register_of_deeds"]["age_days"] == 2


def test_no_state_file_means_no_paid_source_rows(state_dir):
    assert H.paid_sources(TODAY) == []


def test_the_runner_writes_the_readings_only_when_asked(state_dir, monkeypatch):
    import ftm_runner
    import scraper

    monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11", "product": "30 Day Subscription"})
    ftm_runner._write_source_state()
    assert not (state_dir / "ftm_sources.json").exists(), "off by default: the run writes no new file"
    monkeypatch.setenv("FTM_FEED_HEALTH", "1")
    ftm_runner._write_source_state()
    held = json.loads((state_dir / "ftm_sources.json").read_text(encoding="utf-8"))
    assert held["tnpublicnotice"]["expires"] == "2026-12-11"


def test_a_source_not_read_this_run_keeps_its_last_reading(state_dir, monkeypatch):
    import ftm_runner
    import scraper

    monkeypatch.setenv("FTM_FEED_HEALTH", "1")
    (state_dir / "ftm_sources.json").write_text(json.dumps({"register_of_deeds": {"days_left": 12, "read": "2026-11-12"}}),
                                                encoding="utf-8")
    monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11"})
    monkeypatch.delitem(sys.modules, "knox_rod", raising=False)
    ftm_runner._write_source_state()
    held = json.loads((state_dir / "ftm_sources.json").read_text(encoding="utf-8"))
    assert held["register_of_deeds"]["days_left"] == 12 and "tnpublicnotice" in held


def test_the_health_selftest_still_passes():
    assert H.selftest() == 0
