"""ftm_runner: the stages that are off until named, and proof that today's run is unchanged.

Zero network. Every module a stage would call is replaced by a fake; state lands in a temp
folder. Names, streets and instrument numbers are invented.

Run: python -m pytest tests/test_ftm_runner_stages.py
"""
import csv
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_runner as R  # noqa: E402
import ftm_staging  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TODAY = ("notices", "capture", "liens", "county", "sold")
NEW = ("tax_sale", "tax_delinquent", "public_notices", "appointments", "tax_sale_report", "code_violations",
       "deeds", "sold_reentry", "sold_audit")
# Every setting this build added. None of them may be set by the deployed configuration.
SWITCHES = ("FTM_RATE_BUDGET", "FTM_INTAKE_SCREEN", "FTM_NOTICE_TEXT", "FTM_TAG_APPEARED_AGAIN", "FTM_FEED_HEALTH",
            "FTM_FEED_LAG_ALARM_DAYS", "FTM_HEALTH_ALARM_FEEDS", "SOLD_REENTRY_COMMIT", "FTM_PUBLIC_NOTICE_SEARCHES",
            "SOLD_GUARD_EXTRA_UPLOAD_LISTS", "FTM_UPLOAD_BROWSER")


def args(**kw):
    base = dict(commit=True, no_upload=False, counties=None, types=None, since=None, no_proxy=False, max_notices=0,
                mode="daily", backfill_months=0, backfill_offset=0, deep_heirs=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(R.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(R.config, "OUTPUT_DIR", tmp_path)
    (tmp_path / "knox").mkdir()


# ── today's run is what it was ────────────────────────────────────────


def deployed_env() -> dict:
    """The [env] block of the first-to-market app's Fly file, read as text."""
    text = open(os.path.join(ROOT, "fly.ftm.toml"), encoding="utf-8").read()
    return dict(re.findall(r"^\s*([A-Z_]+)\s*=\s*'([^']*)'", text, flags=re.M))


def test_the_deployed_run_names_only_the_stages_it_always_ran():
    stages = re.search(r"--stages\s+(\S+)", deployed_env()["FTM_ARGS"]).group(1).split(",")
    assert tuple(stages) == TODAY
    assert not set(stages) & set(NEW)


def test_the_deployed_configuration_sets_none_of_the_new_switches():
    env = deployed_env()
    assert not [name for name in SWITCHES if name in env]


def test_every_new_stage_exists_and_none_is_in_the_default_set():
    for name in NEW:
        assert name in R.STAGE_FUNCS and name in R.ALL_STAGES
        assert name not in R.DEFAULT_STAGES and name not in R.TODAY_STAGES
    assert R.DEFAULT_STAGES == ("notices", "capture")


def test_todays_stages_still_run_the_functions_they_always_did():
    assert R.STAGE_FUNCS["notices"] is R._stage_notices and R.STAGE_FUNCS["capture"] is R._stage_capture
    assert R.STAGE_FUNCS["liens"] is R._stage_liens and R.STAGE_FUNCS["county"] is R._stage_county
    assert R.STAGE_FUNCS["sold"] is R._stage_sold
    assert R.ALL_STAGES[-1] == "sold", "sold stays last so a lead uploaded this run is revived this run"


def test_an_unknown_stage_is_refused_before_any_work():
    assert R.main(["--stages", "notices,not_a_stage"]) == 2


def test_the_upload_call_gains_no_argument_until_the_rule_is_on(monkeypatch):
    assert R._new_filing() == {}
    monkeypatch.setenv("FTM_TAG_APPEARED_AGAIN", "1")
    assert R._new_filing() == {"new_filing": True}


def test_with_no_intake_switch_the_notices_come_back_untouched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the screen must not run")

    notices = [SimpleNamespace(notice_type="foreclosure", address="412 Sample Hollow Rd", raw_text="NOTICE")]
    res = R.StageResult(name="notices")
    got, extras, said = R._intake(notices, res, "2026-11-13_063000", screen=boom)
    assert got is notices and extras is None and said == "" and res.warnings == []


def test_the_notice_text_switch_alone_adds_the_text_and_screens_nothing(monkeypatch):
    monkeypatch.setenv("FTM_NOTICE_TEXT", "field")
    n = SimpleNamespace(notice_type="foreclosure", address="412 Sample Hollow Rd", raw_text="NOTICE OF SALE")
    got, extras, said = R._intake([n], R.StageResult(name="notices"), "s",
                                  screen=lambda ns: (_ for _ in ()).throw(AssertionError("no screen")))
    assert got == [n] and extras[id(n)] == {"Notice Text": "NOTICE OF SALE"} and said == ""


def test_the_screen_writes_what_it_left_out_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    keep = SimpleNamespace(notice_type="foreclosure", address="412 Sample Hollow Rd", raw_text="", county="Knox",
                           city="Knoxville", zip="37918", source_url="u1")
    drop = SimpleNamespace(notice_type="foreclosure", address="9 Warehouse Row", raw_text="", county="Knox",
                           city="Knoxville", zip="37917", source_url="u2")

    def screen(ns):
        return {"kept": [keep], "extras": {id(keep): {"Est Equity": 60000}}, "warnings": ["one warning"],
                "dropped": {"not single family (warehouse)": 1}, "rejected": [(drop, "not single family (warehouse)")]}

    res = R.StageResult(name="notices")
    got, extras, said = R._intake([keep, drop], res, "2026-11-13_063000", screen=screen)
    assert got == [keep] and extras[id(keep)]["Est Equity"] == 60000
    assert said == "intake left out 1 not single family (warehouse)" and res.warnings == ["one warning"]
    with open(tmp_path / "ftm_intake_rejected_2026-11-13_063000.csv", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh))[0]["address"] == "9 Warehouse Row"


def test_an_intake_that_breaks_never_costs_the_upload(monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    notices = [SimpleNamespace(notice_type="foreclosure", address="412 Sample Hollow Rd", raw_text="")]
    res = R.StageResult(name="notices")
    got, extras, _ = R._intake(notices, res, "s", screen=lambda ns: 1 / 0)
    assert got is notices and extras is None
    assert "uploaded unscreened" in res.warnings[0]


# ── the county stage settles what was staged, and only after an upload ─


WITH_APPOINTMENTS = "notices,capture,liens,appointments,county,sold"
COMMANDS: list = []


def county(monkeypatch, tmp_path, created=1, rows=1):
    import datasift_api_upload

    COMMANDS.clear()

    def fake_system(cmd):
        COMMANDS.append(cmd)
        out = re.search(r'--out "([^"]+)"', cmd).group(1)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Property Street Address", "Lists"])
            for i in range(rows):
                w.writerow(["%d Sample Hollow Rd" % (400 + i), "Substitute Trustee"])
        return 0

    marked = []
    monkeypatch.setattr(R.os, "system", fake_system)
    monkeypatch.setattr(R, "_mark_liens_uploaded", lambda *a: marked.append("liens"))
    monkeypatch.setattr(datasift_api_upload, "upload_csv",
                        lambda path, commit=False, out_dir="": {"created": created if commit else 0})
    ftm_staging.stage(str(tmp_path / "knox"), "substitute_trustee", [{"address": "412 Sample Hollow Rd"}], ["INST-1"])
    return marked


def staged_is_open(tmp_path):
    return ftm_staging.is_new(str(tmp_path / "knox"), "substitute_trustee", "INST-1")


def test_a_committed_upload_that_landed_settles_the_staged_feeds(monkeypatch, tmp_path):
    marked = county(monkeypatch, tmp_path)
    res = R._stage_county(args(stages=WITH_APPOINTMENTS))
    assert res.status == "ok" and marked == ["liens"]
    assert not staged_is_open(tmp_path)
    assert '--staged-feeds "substitute_trustee"' in COMMANDS[0]


def test_todays_run_neither_loads_nor_settles_a_file_a_hand_run_left(monkeypatch, tmp_path):
    """A dry run of `appointments` on the box leaves a pending file. The next scheduled run, which
    does not name that stage, must run the same command as ever and leave the file alone."""
    county(monkeypatch, tmp_path)
    res = R._stage_county(args(stages=",".join(TODAY)))
    assert res.status == "ok" and "--staged-feeds" not in COMMANDS[0]
    assert staged_is_open(tmp_path)
    R._stage_county(args())                       # no --stages attribute at all reads the same way
    assert "--staged-feeds" not in COMMANDS[-1] and staged_is_open(tmp_path)


def test_a_dry_run_settles_nothing(monkeypatch, tmp_path):
    marked = county(monkeypatch, tmp_path)
    R._stage_county(args(commit=False, stages=WITH_APPOINTMENTS))
    assert marked == [] and staged_is_open(tmp_path)


def test_a_failed_upload_settles_nothing(monkeypatch, tmp_path):
    marked = county(monkeypatch, tmp_path, created=0)
    res = R._stage_county(args(stages=WITH_APPOINTMENTS))
    assert res.status == "failed" and marked == [] and staged_is_open(tmp_path)


def test_no_upload_settles_nothing(monkeypatch, tmp_path):
    county(monkeypatch, tmp_path)
    R._stage_county(args(no_upload=True, stages=WITH_APPOINTMENTS))
    assert staged_is_open(tmp_path)


def test_with_nothing_staged_the_county_stage_writes_no_new_file(monkeypatch, tmp_path):
    county(monkeypatch, tmp_path)
    for f in os.listdir(tmp_path / "knox"):
        os.remove(tmp_path / "knox" / f)
    R._stage_county(args())
    assert os.listdir(tmp_path / "knox") == []


# ── appointments ──────────────────────────────────────────────────────


def with_recorder_login(monkeypatch):
    monkeypatch.setattr(R.config, "PAXSUB_USERNAME", "someone")
    monkeypatch.setattr(R.config, "PAXSUB_PASSWORD", "not-a-real-password")


def test_appointments_skip_without_the_recorder_login(monkeypatch):
    monkeypatch.setattr(R.config, "PAXSUB_USERNAME", "")
    res = R._stage_appointments(args())
    assert res.status == "skipped" and "PAXSUB_USERNAME" in res.detail


def test_appointments_report_what_they_staged(monkeypatch):
    import knox_appointments
    with_recorder_login(monkeypatch)
    monkeypatch.setattr(knox_appointments, "prepare", lambda scratch: {
        "window": "11/01/2026 .. 11/30/2026", "in_window": 4, "new": 2, "staged": 1, "settled": 2, "lookups": 1,
        "outcomes": {"matched to a house": 1, "names no deed of trust the index can follow": 1},
        "subscription_days": 2})
    res = R._stage_appointments(args())
    assert res.status == "ok" and res.records == 1
    assert res.detail.startswith("2 new substitute trustee appointment(s)")
    assert any("expires in 2 day(s)" in w for w in res.warnings)


def test_a_feed_that_cannot_run_fails_by_name(monkeypatch):
    import knox_appointments
    with_recorder_login(monkeypatch)

    def unusable(scratch):
        raise knox_appointments.AppointmentsUnusable("3 appointments were read and none names its deed of trust")

    monkeypatch.setattr(knox_appointments, "prepare", unusable)
    res = R._stage_appointments(args())
    assert res.status == "failed" and res.detail.startswith("SUBSTITUTE TRUSTEE FEED CANNOT RUN")


def test_a_lapsed_recorder_subscription_is_named(monkeypatch):
    import knox_appointments
    import knox_rod
    with_recorder_login(monkeypatch)

    def lapsed(scratch):
        raise knox_rod.RodSubscriptionExpired("renew it")

    monkeypatch.setattr(knox_appointments, "prepare", lapsed)
    res = R._stage_appointments(args())
    assert res.status == "failed" and res.detail.startswith("REGISTER OF DEEDS SUBSCRIPTION EXPIRED")


# ── the two county document feeds ─────────────────────────────────────


def test_any_tax_sale_problem_fails_the_stage(monkeypatch):
    import knox_tax_sale
    stats = {"new_documents": [], "problems": ["the Trustee's tax sale page links no PDF at all."], "staged": 0,
             "lists": 0, "parcels_read": 0, "not_on_roll": 0, "no_house_number": 0, "left_for_tomorrow": 0,
             "no_sale_date": 0, "sale_held": 0, "lookups": 0}
    monkeypatch.setattr(knox_tax_sale, "prepare", lambda scratch: stats)
    res = R._stage_tax_sale_report(args())
    assert res.status == "failed" and "links no PDF" in res.detail
    monkeypatch.setattr(knox_tax_sale, "prepare", lambda scratch: dict(stats, problems=[]))
    assert R._stage_tax_sale_report(args()).status == "ok"


def test_a_code_violation_file_that_stopped_arriving_fails_the_stage(monkeypatch):
    import code_violations
    stats = {"files": 1, "new_files": 0, "rows": 6, "staged": 0, "routine": 1, "no_address": 1, "too_old": 1,
             "already_uploaded": 3, "problems": ["no new code violation file for 17 days (the last arrived 2026-11-13)."]}
    monkeypatch.setattr(code_violations, "prepare", lambda scratch: stats)
    res = R._stage_code_violations(args())
    assert res.status == "failed" and "17 days" in res.detail


# ── the notice feeds ──────────────────────────────────────────────────


def test_a_notice_feed_stage_wraps_the_feed_runner(monkeypatch):
    import ftm_feed_runner
    seen = {}

    def fake(a, name, **kw):
        seen.update(name=name, kw=kw)
        return {"name": name, "status": "empty", "detail": "could not be read", "records": 0, "uploaded": 0,
                "seconds": 1.0, "csv_path": "", "warnings": ["w"]}

    monkeypatch.setattr(ftm_feed_runner, "run", fake)
    res = R.STAGE_FUNCS["tax_sale"](args())
    assert isinstance(res, R.StageResult) and res.name == "tax_sale" and res.status == "empty" and not res.ok
    assert seen["name"] == "tax_sale" and seen["kw"]["next_suffix"] is R._next_proxy_session_suffix


# ── the sold guard's two new stages ───────────────────────────────────


def test_the_reentry_stage_is_dry_on_a_committed_run_until_its_own_switch(monkeypatch):
    import sold_guard
    got = []

    def fake(ns):
        got.append(ns.commit)
        ns.stats = {"reentry_due": True, "reentered": 0, "reentry_would": 40, "reentry_judged": 150, "reentry_waiting": 900}
        return 0

    monkeypatch.setattr(sold_guard, "reentry", fake)
    res = R._stage_sold_reentry(args(commit=True))
    assert got == [False] and "40 record(s) would come back" in res.detail and "nothing written" in res.detail
    monkeypatch.setenv("SOLD_REENTRY_COMMIT", "1")
    R._stage_sold_reentry(args(commit=True))
    assert got == [False, True]
    R._stage_sold_reentry(args(commit=False))
    assert got[-1] is False, "the switch alone is not enough: the run must be committed too"


def test_outside_january_the_reentry_stage_says_it_is_not_due(monkeypatch):
    import sold_guard

    def fake(ns):
        ns.stats = {"reentry_due": False}
        return 0

    monkeypatch.setattr(sold_guard, "reentry", fake)
    res = R._stage_sold_reentry(args())
    assert res.status == "ok" and res.detail == "not due: the sold window moves on January 1"
    assert res.status != "skipped", "a requested stage that reads skipped is a red finding in the health check"


def test_the_audit_stage_never_asks_to_write(monkeypatch):
    import sold_guard
    got = []

    def fake(ns):
        got.append(ns.commit)
        ns.stats = {"audit_ran": True, "audit_month": "2026-10",
                    "audit_summary": {"month": "2026-10", "sold_records": 31, "were_our_leads": 9, "never_touched": 4,
                                      "never_called": 6, "never_texted": 7, "never_mailed": 9, "unassigned": 5,
                                      "median_days_as_lead": 41, "by_list": {"Foreclosure": 6, "Liens": 3}}}
        return 0

    monkeypatch.setattr(sold_guard, "monthly_audit", fake)
    res = R._stage_sold_audit(args(commit=True))
    assert got == [False] and res.records == 9
    assert res.detail.startswith("2026-10: 9 of our leads sold") and "\n" not in res.detail


# ── deeds ─────────────────────────────────────────────────────────────


def test_the_deeds_stage_reads_todays_notice_files_and_writes_only_when_committed(monkeypatch, tmp_path):
    import rod_lookup
    from datetime import datetime
    with_recorder_login(monkeypatch)
    today = datetime.now().strftime("%Y-%m-%d")
    for name in ("ftm_notices_%s_063000.csv" % today, "ftm_notices_2020-01-01_063000.csv"):
        with open(tmp_path / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Property Street Address", "County", "Notice Type"])
            w.writerow(["412 Sample Hollow Rd", "Knox", "foreclosure"])
    seen = []

    def fake(rows, scratch, commit=False, **kw):
        seen.append((len(rows), commit))
        return {"candidates": 1, "looked_up": 1, "with_debt": 1, "written": 1 if commit else 0, "no_text": 0,
                "not_found": 0, "left_for_tomorrow": 0, "failed": 0}

    monkeypatch.setattr(rod_lookup, "annotate", fake)
    res = R._stage_deeds(args(commit=True))
    assert seen == [(1, True)] and res.status == "ok" and res.records == 1
    R._stage_deeds(args(commit=False))
    assert seen[-1] == (1, False)


def test_the_deeds_stage_says_when_the_notice_text_is_missing(monkeypatch):
    import rod_lookup
    with_recorder_login(monkeypatch)
    monkeypatch.setattr(rod_lookup, "annotate", lambda rows, scratch, commit=False, **kw: {
        "candidates": 3, "looked_up": 0, "with_debt": 0, "written": 0, "no_text": 3, "not_found": 0,
        "left_for_tomorrow": 0, "failed": 0})
    res = R._stage_deeds(args())
    assert any("FTM_NOTICE_TEXT=field" in w for w in res.warnings)


# ── the wiring check ──────────────────────────────────────────────────


def test_the_wiring_check_reports_nothing_switched_on_for_todays_run(monkeypatch):
    monkeypatch.setenv("FTM_ARGS", deployed_env()["FTM_ARGS"])
    lines = R.switch_report()
    assert "stages beyond today's five: none" in lines[0]
    assert lines[-1] == "  settings switched on: none"
    assert not [x for x in lines if "WARNING" in x or "live feed" in x]


def test_the_wiring_check_names_each_new_feed_and_a_stage_in_the_wrong_place(monkeypatch):
    monkeypatch.setenv("FTM_ARGS", "--stages notices,capture,liens,county,appointments,tax_sale,sold --commit")
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    text = "\n".join(R.switch_report())
    assert "stages beyond today's five: appointments, tax_sale" in text
    assert "live feed: Substitute trustee appointments, Knox -> list 'Substitute Trustee'" in text
    assert "live feed: Tax sale notices, Knox -> list 'Tax Sale'" in text
    assert "appointments is listed AFTER `county`" in text
    assert "settings switched on: FTM_INTAKE_SCREEN=1" in text


def test_the_wiring_check_warns_when_a_staging_stage_has_no_upload(monkeypatch):
    monkeypatch.setenv("FTM_ARGS", "--stages notices,appointments --commit")
    assert any("without `county` nothing uploads them" in x for x in R.switch_report())


def test_the_module_holds_no_long_dash():
    for name in ("src/ftm_runner.py", "tests/test_ftm_runner_stages.py"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
