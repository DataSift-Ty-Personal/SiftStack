"""A paid subscription that is ending reaches a person (2026-11-19).

Both paid sources, the tnpublicnotice Smart Search account and the Knox Register of Deeds, lapse
around 2026-12-11, and the recorder's renewal is a purchase a person has to make. The warning used
to be 3 days out and only a stage warning: the 8am digest scored it amber, the two-line post prints
amber as the all-clear, and the brain's relay drops a run report that did not fail, so the first
anyone heard was the red run on the day it lapsed. These tests hold the fix end to end:

  * both lead times are 10 days: SUBSCRIPTION_WARN_DAYS sets both (as it always has) and
    ROD_SUBSCRIPTION_WARN_DAYS sets the recorder alone;
  * every run keeps what it read in its own run log line, with no setting switched on;
  * the 8am post says each ending subscription in its own line, on any verdict, and stays two
    lines when nothing is ending;
  * the relay shows that line in the ops board's First to market section and never sends it to
    #issues, checked against a post the health check really rendered.

Zero network: state lands in a temp folder and nothing is posted. Every name here is invented.

Run: python -m pytest tests/test_subscription_expiry.py
"""
import json
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import ftm_feeds  # noqa: E402
import ftm_health as H  # noqa: E402
import ftm_runner as R  # noqa: E402
import knox_rod  # noqa: E402
import scraper  # noqa: E402
from agents import relay  # noqa: E402  (imported here, before the fixture clears the settings)

SETTINGS = ("SUBSCRIPTION_WARN_DAYS", "ROD_SUBSCRIPTION_WARN_DAYS", "FTM_FEED_HEALTH", "FTM_ARGS")
RUN_STAGES = ("notices", "capture", "liens", "county", "estate_ready", "sold")
DAY = "2026-12-01"
ROD_RENEW = "Renew it at paxsub.knoxrod.org, on the Subscription tab."


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(R.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(R.config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(R.config, "SLACK_WEBHOOK_URL", "")
    return tmp_path


def args(**kw):
    base = dict(commit=False, no_upload=True, counties=None, types=None, since=None, no_proxy=False,
                max_notices=0, mode="daily", backfill_months=0, backfill_offset=0, deep_heirs=False)
    base.update(kw)
    return SimpleNamespace(**base)


def reading(source, days, *, state="active", read=DAY, age=0, expires=""):
    return {"source": source, "days_left": days, "expires": expires, "read": read, "age_days": age, "state": state}


def digest(*paid, red=False):
    """The data the 8am post is drawn from, for a run that went fine (or failed at notices)."""
    stages = [dict(name=n, status="failed" if red and n == "notices" else "ok", detail="", warnings=[])
              for n in RUN_STAGES]
    d = H.analyze([{"started": DAY + "T06:30:00", "stages": stages}], DAY,
                  feed_last={"%s/%s" % (c, t): DAY for c, t in H.FEEDS}, feed_today={}, new_by_source={},
                  uploaded_by_source={}, requested_stages=list(RUN_STAGES), ledger_seeded=False)
    d.update(new_by_list={"Foreclosure": 2}, stages_requested=list(RUN_STAGES), paid_sources=list(paid))
    return d


def body(post):
    return [ln for ln in post.split("\n") if ln.strip()]


# ── the lead times ────────────────────────────────────────────────────


def test_the_lead_times_are_3_days_for_the_notice_site_and_10_for_the_recorder():
    # The notice site auto-renews (Ty, 2026-11-11), so its line shows 3 days out; the recorder's
    # renewal is a purchase, so its line shows 10 days out.
    assert ftm_feeds.warn_days("tnpublicnotice") == 3
    assert ftm_feeds.warn_days("register_of_deeds") == 10
    assert R.SUBSCRIPTION_WARN_DAYS == 3 and R.ROD_SUBSCRIPTION_WARN_DAYS == 10


def test_the_old_name_still_sets_both_and_the_new_one_sets_the_recorder_alone(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_WARN_DAYS", "5")
    assert ftm_feeds.warn_days("tnpublicnotice") == 5 and ftm_feeds.warn_days("register_of_deeds") == 5
    monkeypatch.setenv("ROD_SUBSCRIPTION_WARN_DAYS", "14")
    assert ftm_feeds.warn_days("tnpublicnotice") == 5 and ftm_feeds.warn_days("register_of_deeds") == 14


def test_a_setting_that_is_not_a_number_is_passed_over(monkeypatch):
    monkeypatch.setenv("ROD_SUBSCRIPTION_WARN_DAYS", "ten")
    monkeypatch.setenv("SUBSCRIPTION_WARN_DAYS", "7")
    assert ftm_feeds.warn_days("register_of_deeds") == 7
    monkeypatch.setenv("SUBSCRIPTION_WARN_DAYS", "soon")
    assert ftm_feeds.warn_days("register_of_deeds") == 10, "a bad value never stops the run from starting"


def test_the_new_setting_reaches_the_box_through_the_secrets_sync():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "deploy", "sync_ftm_secrets.py")
    spec = importlib.util.spec_from_file_location("sync_ftm_secrets_for_test", path)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    names = {name for name, _why in sync.OPTIONAL}
    assert {"SUBSCRIPTION_WARN_DAYS", "ROD_SUBSCRIPTION_WARN_DAYS"} <= names


# ── the runner ────────────────────────────────────────────────────────


def recorder_login(monkeypatch):
    monkeypatch.setattr(R.config, "PAXSUB_USERNAME", "someone")
    monkeypatch.setattr(R.config, "PAXSUB_PASSWORD", "not-a-real-password")


def test_the_lien_stage_warns_ten_days_out(monkeypatch, clean):
    import knox_lien_releases
    import knox_liens_daily
    recorder_login(monkeypatch)
    stats = {"new_debtors": 0, "window": "12/01/2026 .. 12/31/2026", "with_parcels": 0, "parcels": 0,
             "lookup_errors": 0, "subscription_days": 10}
    monkeypatch.setattr(knox_liens_daily, "prepare", lambda scratch: stats)
    monkeypatch.setattr(knox_lien_releases, "reconcile", lambda scratch, commit=False, log=None: {
        "to_update": 0, "committed": False, "updated": 0, "fully_released": 0, "failed": 0, "mismatch": 0})
    res = R._stage_liens(args())
    assert res.status == "ok" and any("expires in 10 day(s)" in w for w in res.warnings), res.warnings
    stats["subscription_days"] = 11
    assert not [w for w in R._stage_liens(args()).warnings if "subscription" in w]


def test_the_appointments_stage_uses_the_same_window(monkeypatch):
    import knox_appointments
    recorder_login(monkeypatch)
    stats = {"window": "12/01/2026 .. 12/31/2026", "in_window": 0, "new": 0, "staged": 0, "settled": 0,
             "lookups": 0, "outcomes": {}, "subscription_days": 10}
    monkeypatch.setattr(knox_appointments, "prepare", lambda scratch: stats)
    assert any("expires in 10 day(s)" in w for w in R._stage_appointments(args()).warnings)


def test_every_run_keeps_what_it_read_in_its_own_log_line(monkeypatch, clean):
    monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11", "expired": False,
                                                  "product": "30 Day Subscription", "read": "2026-12-01"})
    monkeypatch.setattr(knox_rod, "SUBSCRIPTION", {"state": "active", "days": 9, "read": "2026-12-02"})
    monkeypatch.setitem(R.STAGE_FUNCS, "notices",
                        lambda a: R.StageResult(name="notices", status="ok", detail="1 record, 1 into DataSift"))
    assert R.main(["--stages", "notices"]) == 0
    rows = [json.loads(ln) for ln in (clean / "ftm_runs.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    got = rows[-1]["sources"]
    assert got["tnpublicnotice"]["expires"] == "2026-12-11" and got["tnpublicnotice"]["read"] == "2026-12-01"
    assert got["tnpublicnotice"]["state"] == "active"
    assert got["register_of_deeds"] == {"days_left": 9, "state": "active", "read": "2026-12-02"}
    assert not (clean / "ftm_sources.json").exists(), "the optional file still needs FTM_FEED_HEALTH=1"


def test_a_reading_keeps_the_day_it_was_taken():
    """The scheduler runs every run in one process. A stage that stopped before it logged in leaves
    the last reading on the module, and dating that today would add a day to the recorder's count."""
    keep_s, keep_k = scraper.SUBSCRIPTION, knox_rod.SUBSCRIPTION
    try:
        scraper.SUBSCRIPTION = {"expires": "2026-10-04", "expired": True, "read": "2026-10-05"}
        knox_rod.SUBSCRIPTION = {"state": "active", "days": 12, "read": "2026-11-28"}
        from datetime import datetime
        got = R.source_state(datetime(2026, 12, 1))
        assert got["register_of_deeds"]["read"] == "2026-11-28"
        assert got["tnpublicnotice"]["state"] == "expired" and got["tnpublicnotice"]["read"] == "2026-10-05"
        knox_rod.SUBSCRIPTION = {"state": "active", "days": 12}
        assert R.source_state(datetime(2026, 12, 1))["register_of_deeds"]["read"] == "2026-12-01", \
            "an unstamped reading (an older process) is dated the day of the run, as before"
    finally:
        scraper.SUBSCRIPTION, knox_rod.SUBSCRIPTION = keep_s, keep_k


# ── the 8am post ──────────────────────────────────────────────────────


def test_the_post_says_the_ending_subscription_with_no_setting_switched_on(monkeypatch, clean):
    """End to end on the volume: the run log line written by a run is what the post reads."""
    today = H._today()
    run = {"started": today + "T06:30:00", "finished": today + "T06:50:00", "committed": True, "blocked": False,
           "stages": [dict(name=n, status="ok", detail="", warnings=[]) for n in RUN_STAGES],
           "sources": {"register_of_deeds": {"days_left": 9, "state": "active", "read": today}}}
    (clean / "ftm_runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    monkeypatch.setenv("FTM_ARGS", "--stages %s --commit" % ",".join(RUN_STAGES))
    data = H.collect(update_ledger=False)
    lines = body(H.render_short(data))
    ends = date.fromisoformat(today) + timedelta(days=9)
    assert data["verdict"] != "red" and lines[0].startswith(":white_check_mark:")
    assert lines[2] == "Register of Deeds subscription ends in 9 days (%s %d). %s" % (ends.strftime("%b"), ends.day, ROD_RENEW)
    assert len(lines) == 3
    assert "register_of_deeds: 9 days left" in H.render(data), "the full report reads the run log too"


def test_nothing_ending_keeps_the_post_at_two_lines():
    quiet = digest(reading("register_of_deeds", 27), reading("tnpublicnotice", 11, expires="2026-12-12"))
    assert len(body(H.render_short(quiet))) == 2


def test_the_line_is_said_on_a_red_day_after_the_reason():
    lines = body(H.render_short(digest(reading("register_of_deeds", 9), red=True)))
    assert lines[0].startswith(":rotating_light:") and lines[2].startswith("Stage notices FAILED")
    assert lines[3] == "Register of Deeds subscription ends in 9 days (Dec 10). " + ROD_RENEW


# ── the relay, against what the health check really wrote ────────────


def test_the_relay_shows_the_line_on_the_ops_board_and_never_in_issues():
    rod = reading("register_of_deeds", 9)
    tnpn = reading("tnpublicnotice", 3, expires="2026-12-04")
    amber = H.render_short(digest(rod, tnpn))
    said = [ln for ln in body(amber) if "subscription" in ln]
    assert len(said) == 2 and all(relay.EXPIRY_RX.search(ln) for ln in said)
    assert relay.classify(amber) == "ftm_digest" and [p["kind"] for p in relay.route(amber)] == ["pipeline"]
    sec = relay.to_board(amber)
    assert sec["key"] == "relay_ftm_digest" and sec["dept"] == "ops" and said == sec["lines"][-2:]

    red = H.render_short(digest(rod, tnpn, red=True))
    routed = relay.route(red)
    assert [p["kind"] for p in routed] == ["pipeline", "issues"]
    assert "Stage notices FAILED" in routed[1]["text"] and "subscription" not in routed[1]["text"]
    assert relay.to_board(red)["lines"][-2:] == said


def test_a_lapse_is_said_without_the_relays_failure_words():
    lapsed = H.render_short(digest(reading("register_of_deeds", None, state="expired"),
                                   reading("tnpublicnotice", -1, expires="2026-11-30")))
    lines = body(lapsed)
    assert lines[2] == "Register of Deeds subscription has ended. " + ROD_RENEW
    assert lines[3] == ("tnpublicnotice subscription ended Nov 30. "
                        "Renew it on tnpublicnotice.com (My Smart Search, then Renew).")
    assert [p["kind"] for p in relay.route(lapsed)] == ["pipeline"], "the line alone is never breakage"
    assert chr(0x2014) not in lapsed and chr(0x2013) not in lapsed
