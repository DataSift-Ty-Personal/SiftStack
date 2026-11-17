"""A login that cannot be reached is a dead proxy IP, not an empty feed.

What happened: the scraper retried the login page three times on the SAME
upstream address, gave up, and returned []. The runner reported that as
"0 notices scraped, check the saved searches", which is the message for a
different failure, and a whole day's pull was lost to one bad address. Seen
live 2026-11-11: one proxy session timed out for 255.9 seconds while the
sessions either side of it logged in within ten.

Zero network. scrape_all and the proxy resolver are replaced.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

pytest.importorskip("playwright")
pytest.importorskip("twocaptcha")

import ftm_runner  # noqa: E402
import proxy_resolver  # noqa: E402
import scraper  # noqa: E402


def _args(**over):
    base = dict(counties="Blount", types="foreclosure", no_proxy=False, mode="daily",
                since=None, max_notices=0, backfill_months=0, backfill_offset=0,
                deep_heirs=False, no_upload=False, commit=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch):
    """Replace every outbound edge. Returns the list of sessions that were tried."""
    sessions: list[str] = []
    counter = {"n": 0}

    def next_suffix():
        counter["n"] += 1
        return f"_{counter['n']}"

    def resolve(session_suffix=""):
        sessions.append(session_suffix)
        return f"http://proxy.invalid:8000/{session_suffix}"

    monkeypatch.setattr(ftm_runner, "_next_proxy_session_suffix", next_suffix)
    monkeypatch.setattr(proxy_resolver, "resolve_proxy_url", resolve)
    monkeypatch.setattr(proxy_resolver, "describe", lambda suffix="": f"test proxy {suffix}")
    monkeypatch.setattr(scraper, "load_seen_ids", lambda: {})
    monkeypatch.setattr(scraper, "save_seen_ids", lambda ids: None)
    return sessions


def _scrape_failing(times: int, then):
    """A scrape_all that raises LoginFailed `times` times, then returns `then`."""
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        assert kwargs.get("raise_on_login_failure") is True
        if calls["n"] <= times:
            raise scraper.LoginFailed("login page timed out")
        return then

    return fake, calls


def test_a_failed_login_rotates_to_a_new_session(wired, monkeypatch):
    fake, calls = _scrape_failing(2, then=[])
    monkeypatch.setattr(scraper, "scrape_all", fake)
    res = ftm_runner._stage_notices(_args())
    assert calls["n"] == 3
    assert wired == ["_1", "_2", "_3"], "every attempt must use a different proxy session"
    # The third session logged in and the search really was empty.
    assert res.status == "empty"


def test_every_session_failing_is_reported_as_a_login_problem(wired, monkeypatch):
    fake, calls = _scrape_failing(99, then=[])
    monkeypatch.setattr(scraper, "scrape_all", fake)
    res = ftm_runner._stage_notices(_args())
    assert calls["n"] == ftm_runner.LOGIN_EGRESS_ATTEMPTS
    assert res.status == "failed"
    assert "LOGIN UNREACHABLE" in res.detail
    assert "saved searches" in res.detail      # it says what NOT to go and check
    assert "0 notices scraped" not in res.detail


def test_a_working_login_is_tried_exactly_once(wired, monkeypatch):
    fake, calls = _scrape_failing(0, then=[])
    monkeypatch.setattr(scraper, "scrape_all", fake)
    ftm_runner._stage_notices(_args())
    assert calls["n"] == 1 and wired == ["_1"]


def test_an_expired_subscription_is_named_and_never_retried(wired, monkeypatch):
    """Reported as an empty feed, this cost 39 days in October 2026."""
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        raise scraper.SubscriptionExpired("the subscription expired on 2026-10-04")

    monkeypatch.setattr(scraper, "scrape_all", fake)
    res = ftm_runner._stage_notices(_args())
    assert calls["n"] == 1, "no proxy rotation fixes an unpaid account"
    assert res.status == "failed"
    assert res.detail.startswith("SUBSCRIPTION EXPIRED")
    assert "0 notices scraped" not in res.detail


class TestSubscriptionReading:
    DASHBOARD = (
        "WELCOME TO SMART SEARCH\nSMART SEARCH SUBSCRIPTION DETAIL\n"
        "Username: Example User\nCurrent Sub. Started: 11/11/2026\n"
        "Current Sub. Expiration: 12/11/2026\nCurrent Product: 30 Day Subscription\n"
        "Notices to Date: 0\n"
    )

    def test_live_account(self):
        got = scraper.parse_subscription(
            self.DASHBOARD, ["Saved Searches", "Foreclosure V2 Knox", "Probate V2 Blount"])
        assert got == {"expires": "2026-12-11", "expired": False,
                       "product": "30 Day Subscription"}

    def test_lapsed_account_is_read_off_the_dropdown(self):
        got = scraper.parse_subscription(
            self.DASHBOARD.replace("12/11/2026", "10/4/2026"),
            ["Subscription or free trial expired"])
        assert got["expired"] is True
        assert got["expires"] == "2026-10-04"

    def test_a_page_without_the_block_reads_as_unknown_not_expired(self):
        assert scraper.parse_subscription("", []) == {
            "expires": "", "expired": False, "product": ""}


class TestSubscriptionWarning:
    def _res(self):
        return SimpleNamespace(warnings=[])

    def test_warns_inside_the_window(self, monkeypatch):
        from datetime import datetime
        monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11"})
        res = self._res()
        ftm_runner._subscription_warning(res, today=datetime(2026, 12, 9))
        assert len(res.warnings) == 1
        assert "2026-12-11" in res.warnings[0] and "2 days" in res.warnings[0]

    def test_silent_mid_term_on_the_auto_renewing_30_day_product(self, monkeypatch):
        # Ten days out on a 30-day term is an ordinary day, not a warning.
        from datetime import datetime
        monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11"})
        res = self._res()
        ftm_runner._subscription_warning(res, today=datetime(2026, 12, 1))
        assert res.warnings == []

    def test_silent_outside_the_window(self, monkeypatch):
        from datetime import datetime
        monkeypatch.setattr(scraper, "SUBSCRIPTION", {"expires": "2026-12-11"})
        res = self._res()
        ftm_runner._subscription_warning(res, today=datetime(2026, 11, 11))
        assert res.warnings == []

    def test_silent_when_the_date_was_never_read(self, monkeypatch):
        monkeypatch.setattr(scraper, "SUBSCRIPTION", {})
        res = self._res()
        ftm_runner._subscription_warning(res)
        assert res.warnings == []


def test_direct_egress_has_no_session_to_rotate(wired, monkeypatch):
    fake, calls = _scrape_failing(99, then=[])
    monkeypatch.setattr(scraper, "scrape_all", fake)
    res = ftm_runner._stage_notices(_args(no_proxy=True))
    assert calls["n"] == 1
    assert wired == []
    assert res.status == "failed" and "LOGIN UNREACHABLE" in res.detail


# ── a run whose every notice was filtered out ───────────────────────────


def _wire_pipeline(monkeypatch, scraped, saved, marked):
    """A scrape that returns `scraped` notices and an enrichment that keeps none."""
    import data_formatter
    import datasift_api_upload
    import datasift_formatter
    import enrichment_pipeline

    notices = [SimpleNamespace(notice_type="foreclosure", decedent_name="", address="1 Example St")
               for _ in range(scraped)]

    async def fake(**kwargs):
        return list(notices)

    monkeypatch.setattr(scraper, "scrape_all", fake)
    monkeypatch.setattr(scraper, "save_seen_ids", lambda ids: saved.append(1))
    monkeypatch.setattr(scraper, "save_last_run_date", lambda: marked.append(1))
    monkeypatch.setattr(enrichment_pipeline, "run_enrichment_pipeline", lambda n, opts: [])
    monkeypatch.setattr(data_formatter, "deduplicate", lambda n: n)
    monkeypatch.setattr(datasift_formatter, "write_datasift_csv", lambda n, filename="": "empty.csv")
    monkeypatch.setattr(datasift_api_upload, "upload_csv",
                        lambda path, commit=False, out_dir="": {"created": 0, "failed": 0,
                                                                 "fielded": 0, "warnings": []})


def test_a_quiet_day_whose_one_notice_was_filtered_out_is_not_a_failure(wired, monkeypatch):
    """2026-11-12: one notice scraped, the entity filter dropped it, and the
    stage read FAILED 'records but DataSift created 0'. Nothing had failed."""
    saved, marked = [], []
    _wire_pipeline(monkeypatch, 1, saved, marked)
    res = ftm_runner._stage_notices(_args(commit=True))
    assert res.status == "ok"
    assert "none passed the filters" in res.detail
    assert saved and marked, "the handled notice and the run date must both be recorded"


def test_many_notices_with_no_survivor_is_a_broken_pipeline(wired, monkeypatch):
    saved, marked = [], []
    _wire_pipeline(monkeypatch, ftm_runner.ALL_DROPPED_SUSPECT, saved, marked)
    res = ftm_runner._stage_notices(_args(commit=True))
    assert res.status == "failed"
    assert "every one was filtered out" in res.detail
    assert not saved and not marked, "nothing may be marked handled on a failed run"
