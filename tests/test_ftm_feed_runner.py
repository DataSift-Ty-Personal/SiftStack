"""ftm_feed_runner: a notice feed that runs alone (tax sale, tax delinquent, public notices).

Zero network. The scrape, the enrichment, the screen and the upload are all fakes; the state
files land in a temp folder. Every notice and saved-search reading below is invented.

Run: python -m pytest tests/test_ftm_feed_runner.py
"""
import csv
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_feed_runner as R  # noqa: E402
from captcha_solver import NoticeAccessBlocked  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from scraper import LoginFailed, SubscriptionExpired  # noqa: E402

KNOX, BLOUNT = "Tax Sale V2 Knox", "Tax Sale V2 Blount"
TODAY = "2026-11-13"


def args(**kw):
    base = dict(counties=None, since=None, no_proxy=False, commit=True, no_upload=False, max_notices=0)
    base.update(kw)
    return SimpleNamespace(**base)


def notice(i=1, ntype="tax_sale", county="Knox", **kw):
    base = dict(address="%d Sample Hollow Rd" % (400 + i), city="Knoxville", state="TN", zip="37918",
                county=county, notice_type=ntype, owner_name="Marlowe Quillfeather", auction_date="2027-01-20",
                source_url="https://example.invalid/Details.aspx?ID=%d" % (9000 + i),
                raw_text="NOTICE OF DELINQUENT TAX SALE for the property described below")
    base.update(kw)
    return NoticeData(**base)


class Harness:
    """Every outside edge of one feed run, recorded."""

    def __init__(self, tmp_path, notices=(), rows=None, created=None, scrape_errors=()):
        self.tmp = tmp_path
        self.notices = list(notices)
        self.rows = {KNOX: 12, BLOUNT: 7} if rows is None else rows
        self.created = created
        self.scrape_errors = list(scrape_errors)
        self.scrapes, self.uploads, self.suffixes, self.screens, self.written = [], [], [], [], []

    def next_suffix(self):
        self.suffixes.append("_%d" % (len(self.suffixes) + 1))
        return self.suffixes[-1]

    def scrape(self, searches, since, seen, proxy_url, max_notices):
        self.scrapes.append({"searches": searches, "since": since, "proxy": proxy_url, "seen_before": dict(seen)})
        if self.scrape_errors:
            raise self.scrape_errors.pop(0)
        for n in self.notices:                       # the real scrape marks a notice seen as it opens it
            seen[n.source_url.rsplit("=", 1)[-1]] = TODAY
        return list(self.notices)

    def stats(self):
        return {label: {"reached": True, "rows": n} for label, n in self.rows.items()}

    def write(self, notices, spec, stamp, extras=None):
        self.written.append(R.write_feed_csv(notices, spec, stamp, extras, out_dir=self.tmp))
        return self.written[-1]

    def upload(self, path, commit):
        self.uploads.append((path, commit))
        with open(path, encoding="utf-8") as fh:
            n = len(list(csv.DictReader(fh)))
        made = (n if commit else 0) if self.created is None else self.created
        return {"created": made, "failed": n - made if commit else 0, "warnings": []}

    def screen(self, notices, force=False):
        self.screens.append(force)
        return {"kept": list(notices), "extras": {}, "dropped": {}, "warnings": []}

    def run(self, a=None, stage="tax_sale", **kw):
        return R.run(a or args(), stage, next_suffix=self.next_suffix,
                     resolve_proxy=lambda session_suffix="": "http://proxy.invalid/%s" % session_suffix,
                     scrape=self.scrape, stats=self.stats, enrich=lambda ns: list(ns), screen=self.screen,
                     write_csv=self.write, upload=self.upload, today=TODAY, state_dir=self.tmp,
                     out_dir=self.tmp, **kw)

    def rejects(self):
        """Rows of the left-out file this run wrote, or [] when it wrote none."""
        paths = sorted(self.tmp.glob("ftm_intake_rejected_*.csv"))
        if not paths:
            return []
        with open(paths[-1], encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def state(self, name):
        try:
            return json.loads((self.tmp / name).read_text(encoding="utf-8"))
        except OSError:
            return None


@pytest.fixture(autouse=True)
def no_switches(monkeypatch):
    for name in ("FTM_INTAKE_SCREEN", "FTM_NOTICE_TEXT", "FTM_PUBLIC_NOTICE_SEARCHES", "FTM_LIST_TAX_SALE",
                 "FTM_SEARCH_KNOX_TAX_SALE", "FTM_SEARCH_BLOUNT_TAX_SALE"):
        monkeypatch.delenv(name, raising=False)


# ── proof of life ─────────────────────────────────────────────────────


def test_a_day_with_nothing_new_is_ok_when_every_search_was_read(tmp_path):
    h = Harness(tmp_path, notices=[])
    res = h.run()
    assert res["status"] == "ok"
    assert res["detail"].startswith("nothing new: 19 notice(s) on the grid")
    assert h.uploads == []
    assert h.state("last_run_tax_sale.json") == {"last_run_date": TODAY}


def test_a_search_that_read_no_rows_is_empty_not_quiet(tmp_path):
    """A renamed or deleted saved search returns [] exactly like a quiet day."""
    h = Harness(tmp_path, notices=[], rows={KNOX: 0, BLOUNT: 0})
    res = h.run()
    assert res["status"] == "empty"
    assert KNOX in res["detail"] and BLOUNT in res["detail"] and "list-searches" in res["detail"]
    assert h.state("last_run_tax_sale.json") is None, "the cutoff must not move past notices nobody opened"


def test_one_unread_search_turns_the_stage_red_without_holding_the_other_hostage(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)], rows={KNOX: 12, BLOUNT: 0})
    res = h.run()
    assert res["status"] == "empty" and BLOUNT in res["detail"] and KNOX not in res["detail"].split("BUT")[1]
    assert len(h.uploads) == 1 and res["uploaded"] == 1, "what the readable search produced is still sent"
    assert "9001" in h.state("seen_ids_tax_sale.json")
    assert h.state("last_run_tax_sale.json") is None


def test_a_search_missing_from_the_readings_counts_as_unread(tmp_path):
    h = Harness(tmp_path, notices=[], rows={KNOX: 12})
    assert h.run()["status"] == "empty"


# ── the upload and the state ──────────────────────────────────────────


def test_new_notices_are_uploaded_and_only_then_marked_seen(tmp_path):
    h = Harness(tmp_path, notices=[notice(1), notice(2)])
    res = h.run()
    assert res["status"] == "ok" and res["records"] == 2 and res["uploaded"] == 2
    assert h.uploads[0][1] is True
    assert set(h.state("seen_ids_tax_sale.json")) == {"9001", "9002"}
    assert h.state("last_run_tax_sale.json") == {"last_run_date": TODAY}


def test_the_rows_carry_the_feeds_own_list_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_LIST_TAX_SALE", "County Tax Sale")
    h = Harness(tmp_path, notices=[notice(1)])
    h.run()
    with open(h.written[0], encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["Lists"] == "County Tax Sale"
    assert rows[0]["Tax Auction Date"] == "1/20/2027", "the sale date has to reach a column"
    assert os.path.basename(h.written[0]).startswith("ftm_feed_tax_sale_")


def test_a_dry_run_uploads_nothing_and_moves_no_state(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)])
    res = h.run(args(commit=False))
    assert res["status"] == "ok" and "dry run" in res["detail"]
    assert h.uploads[0][1] is False
    assert h.state("seen_ids_tax_sale.json") is None and h.state("last_run_tax_sale.json") is None


def test_a_dry_run_with_nothing_new_moves_no_state_either(tmp_path):
    h = Harness(tmp_path, notices=[])
    h.run(args(commit=False))
    assert h.state("last_run_tax_sale.json") is None


def test_an_upload_that_created_nothing_fails_and_keeps_the_notices_unseen(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)], created=0)
    res = h.run()
    assert res["status"] == "failed" and "created 0" in res["detail"]
    assert h.state("seen_ids_tax_sale.json") is None, "they must be scraped again tomorrow"


def test_no_upload_writes_the_file_and_stops(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)])
    res = h.run(args(no_upload=True))
    assert res["status"] == "ok" and h.uploads == [] and h.written
    assert h.state("seen_ids_tax_sale.json") is None


def test_many_notices_with_no_survivor_is_a_broken_step_not_a_quiet_day(tmp_path):
    h = Harness(tmp_path, notices=[notice(i) for i in range(1, 7)])
    res = R.run(args(), "tax_sale", next_suffix=h.next_suffix, resolve_proxy=lambda session_suffix="": None,
                scrape=h.scrape, stats=h.stats, enrich=lambda ns: [], write_csv=h.write, upload=h.upload,
                today=TODAY, state_dir=tmp_path)
    assert res["status"] == "failed" and "every one was filtered out" in res["detail"]
    assert h.state("seen_ids_tax_sale.json") is None


def test_one_notice_filtered_out_is_an_ordinary_day(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)])
    res = R.run(args(), "tax_sale", next_suffix=h.next_suffix, resolve_proxy=lambda session_suffix="": None,
                scrape=h.scrape, stats=h.stats, enrich=lambda ns: [], write_csv=h.write, upload=h.upload,
                today=TODAY, state_dir=tmp_path)
    assert res["status"] == "ok" and "nothing to upload" in res["detail"]
    assert "9001" in h.state("seen_ids_tax_sale.json")


# ── its own clock, its own seen file, its own address ─────────────────


def test_the_feed_never_reads_or_writes_the_core_feeds_state(tmp_path):
    (tmp_path / "seen_ids.json").write_text(json.dumps({"9001": "2026-11-10"}), encoding="utf-8")
    (tmp_path / "last_run.json").write_text(json.dumps({"last_run_date": "2026-11-12"}), encoding="utf-8")
    h = Harness(tmp_path, notices=[notice(1)])
    h.run()
    assert h.scrapes[0]["seen_before"] == {}, "a notice the foreclosure search opened is still new to this feed"
    assert h.scrapes[0]["since"] == "2026-11-06", "first run looks a week back, not at the core feed's date"
    assert json.loads((tmp_path / "seen_ids.json").read_text(encoding="utf-8")) == {"9001": "2026-11-10"}
    assert json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8")) == {"last_run_date": "2026-11-12"}


def test_the_second_run_starts_from_the_feeds_own_last_good_day(tmp_path):
    R.save_last_run("tax_sale", "2026-11-09", tmp_path)
    h = Harness(tmp_path)
    h.run()
    assert h.scrapes[0]["since"] == "2026-11-09"
    h2 = Harness(tmp_path)
    h2.run(args(since="2026-10-01"))
    assert h2.scrapes[0]["since"] == "2026-10-01"


def test_each_run_takes_its_own_proxy_session(tmp_path):
    h = Harness(tmp_path)
    h.run()
    assert h.suffixes == ["_1"] and h.scrapes[0]["proxy"] == "http://proxy.invalid/_1"


def test_a_dead_proxy_address_is_rotated_and_then_named(tmp_path):
    h = Harness(tmp_path, scrape_errors=[LoginFailed("timed out"), LoginFailed("timed out"), LoginFailed("timed out")])
    res = h.run()
    assert h.suffixes == ["_1", "_2", "_3"]
    assert res["status"] == "failed" and "LOGIN UNREACHABLE through 3" in res["detail"]


def test_a_login_that_recovers_on_the_second_address_goes_on(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)], scrape_errors=[LoginFailed("timed out")])
    res = h.run()
    assert res["status"] == "ok" and len(h.scrapes) == 2


def test_a_lapsed_subscription_is_named_and_never_retried(tmp_path):
    h = Harness(tmp_path, scrape_errors=[SubscriptionExpired("it expired on 2026-12-11")])
    res = h.run()
    assert res["status"] == "failed" and res["detail"].startswith("SUBSCRIPTION EXPIRED")
    assert len(h.scrapes) == 1


def test_an_egress_block_fails_this_stage_only(tmp_path):
    """It must not raise: the stages after this one do not use the notice site."""
    h = Harness(tmp_path, scrape_errors=[NoticeAccessBlocked("not permitted to view")])
    res = h.run()
    assert res["status"] == "failed" and "EGRESS BLOCKED" in res["detail"]


def test_counties_narrows_the_searches(tmp_path):
    h = Harness(tmp_path, rows={KNOX: 5})
    res = h.run(args(counties="Knox"))
    assert [s[2] for s in h.scrapes[0]["searches"]] == [KNOX] and res["status"] == "ok"
    assert Harness(tmp_path).run(args(counties="Sevier"))["status"] == "failed"


# ── which feeds exist ─────────────────────────────────────────────────


def test_a_stage_that_is_not_a_notice_feed_is_refused(tmp_path):
    assert Harness(tmp_path).run(stage="notices")["status"] == "failed"
    assert R.spec_for("county") is None


def test_the_tax_delinquent_feed_uses_its_own_searches_and_list():
    spec = R.spec_for("tax_delinquent")
    assert [s[2] for s in spec.searches] == ["Tax Delinquent V2 Knox", "Tax Delinquent V2 Blount"]
    assert spec.lists == {"tax_delinquent": "Tax Delinquent Notice"} and not spec.always_screen


def test_public_notices_cannot_run_until_its_searches_are_named(tmp_path):
    res = Harness(tmp_path).run(stage="public_notices")
    assert res["status"] == "failed" and "FTM_PUBLIC_NOTICE_SEARCHES" in res["detail"]


def test_public_notices_drops_junk_and_always_runs_the_buy_box(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_PUBLIC_NOTICE_SEARCHES", "Knox|public_sale|Public Sale Test;Knox|order|Orders Test")
    good = notice(1, "order", raw_text="NON-RESIDENT NOTICE. In the matter of the property at 401 Sample Hollow Rd")
    car = notice(2, "public_sale", raw_text="NOTICE OF PUBLIC SALE of abandoned vehicle VIN 1X2Y3Z, to be sold")
    venue = notice(3, "public_sale", address="400 Main Street", raw_text="PUBLIC SALE of real property by the Clerk")
    bids = notice(4, "public_sale", raw_text="ADVERTISEMENT FOR BIDS. Sealed bids will be received by the City")
    h = Harness(tmp_path, notices=[good, car, venue, bids], rows={"Public Sale Test": 30, "Orders Test": 9})
    res = h.run(stage="public_notices")
    assert res["status"] == "ok" and res["records"] == 1
    assert h.screens == [True], "the buy box is forced for this feed"
    assert "vehicle, storage or boat sale" in res["detail"] and "courthouse or sale venue" in res["detail"]
    with open(h.written[0], encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["Lists"] for r in rows] == ["Court Order"]


def test_a_public_notice_day_that_was_all_junk_is_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_PUBLIC_NOTICE_SEARCHES", "Knox|public_sale|Public Sale Test")
    cars = [notice(i, "public_sale", raw_text="NOTICE OF PUBLIC SALE of vehicle") for i in range(1, 8)]
    h = Harness(tmp_path, notices=cars, rows={"Public Sale Test": 30})
    res = h.run(stage="public_notices")
    assert res["status"] == "ok" and "nothing to upload" in res["detail"]
    assert len(h.rejects()) == 7, "even on a day with nothing to send, what was thrown away is kept"


def test_what_a_feed_leaves_out_is_kept_with_its_reason(tmp_path, monkeypatch):
    """The detail line has the counts; a junk rule can only be tuned by reading the notices."""
    monkeypatch.setenv("FTM_PUBLIC_NOTICE_SEARCHES", "Knox|public_sale|Public Sale Test")
    good = notice(1, "public_sale", raw_text="NOTICE OF SALE of real property at 401 Sample Hollow Rd")
    car = notice(2, "public_sale", raw_text="NOTICE OF PUBLIC SALE of abandoned vehicle VIN 1X2Y3Z")
    condo = notice(3, "public_sale", raw_text="NOTICE OF SALE of real property at 403 Sample Hollow Rd")
    h = Harness(tmp_path, notices=[good, car, condo], rows={"Public Sale Test": 30})
    h.screen = lambda notices, force=False: {
        "kept": [n for n in notices if n is good], "extras": {}, "warnings": [],
        "dropped": {"not single family": 1}, "rejected": [(condo, "not single family")]}
    res = h.run(stage="public_notices")
    assert res["records"] == 1 and "1 not single family" in res["detail"]
    left = {r["address"]: r["reason"] for r in h.rejects()}
    assert left == {"402 Sample Hollow Rd": "vehicle, storage or boat sale", "403 Sample Hollow Rd": "not single family"}
    name = sorted(tmp_path.glob("ftm_intake_rejected_*.csv"))[0].name
    assert name.startswith("ftm_intake_rejected_public_notices_"), "never a name an upload-file reader would pick up"
    assert not name.startswith(("ftm_feed_", "ftm_notices_", "knox_ftm_pull_"))


def test_a_run_that_left_nothing_out_writes_no_such_file(tmp_path):
    h = Harness(tmp_path, notices=[notice(1)])
    h.run()
    assert h.rejects() == [] and not list(tmp_path.glob("ftm_intake_rejected_*.csv"))


def test_the_tax_feeds_do_not_screen_unless_the_intake_switch_is_on(tmp_path, monkeypatch):
    h = Harness(tmp_path, notices=[notice(1)])
    h.run()
    assert h.screens == []
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    h2 = Harness(tmp_path, notices=[notice(2)])
    h2.run()
    assert h2.screens == [False]


def test_the_screens_tags_and_note_are_merged_into_the_upload_row(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_INTAKE_SCREEN", "1")
    n = notice(1)
    h = Harness(tmp_path, notices=[n])
    h.screen = lambda notices, force=False: {
        "kept": list(notices), "dropped": {}, "warnings": ["1 thing to know"],
        "extras": {id(notices[0]): {"Est Equity": 60000, "_tags": ["Listed"], "_note": "Intake check: value $200,000.",
                                    "_debt_source": "x"}}}
    res = h.run()
    with open(h.written[0], encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        row = next(reader)
        columns = reader.fieldnames
    assert row["Tags"].endswith(",Listed") and row["Tags"].startswith("Courthouse Data,FTM,tax_sale")
    assert row["Notes"].startswith("Intake check: value $200,000.") and row["Est Equity"] == "60000"
    assert not [c for c in columns if c.startswith("_")], "the screen's private keys are not columns"
    assert res["warnings"] == ["1 thing to know"]


def test_the_notice_text_rides_on_a_feed_row_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_NOTICE_TEXT", "field")
    h = Harness(tmp_path, notices=[notice(1)])
    h.run()
    with open(h.written[0], encoding="utf-8") as fh:
        assert next(csv.DictReader(fh))["Notice Text"].startswith("NOTICE OF DELINQUENT TAX SALE")


def test_junk_is_judged_on_the_head_of_the_notice_only():
    house = notice(1, "public_sale", raw_text=("NOTICE OF SALE of real property. " + "x " * 300
                                               + "including the detached storage building and boat dock"))
    assert R.is_junk(house) == ""
    assert R.is_junk(notice(2, "public_sale", address="")) == "no property address in the notice"
    assert R.is_junk(notice(3, "order", address="Old Courthouse Rd")).startswith("the only address")


def test_the_module_holds_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/ftm_feed_runner.py", "tests/test_ftm_feed_runner.py", "src/ftm_feeds.py", "src/ftm_staging.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
