"""ftm_budget: this box's share of the account's DataSift request limit (build plan 0.14).

Zero network. Every test points the counter at a throwaway file and drives a fake clock, so
nothing sleeps and nothing touches the volume.

Run: python -m pytest tests/test_ftm_budget.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api_upload as U  # noqa: E402
import ftm_budget as B  # noqa: E402


class Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.t = 1_800_000_000.0
        self.t -= self.t % 3600          # the top of an hour, so windows are easy to count
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def budget(tmp_path, monkeypatch):
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    monkeypatch.setenv("FTM_RATE_BUDGET", "1")
    monkeypatch.setenv("FTM_RATE_PER_MIN", "4")
    monkeypatch.setenv("FTM_RATE_PER_HOUR", "10")
    monkeypatch.setitem(B._warned, "db", False)
    return Clock()


def take(clock, **kw):
    return B.acquire(clock=clock.now, sleep=clock.sleep, **kw)


def test_off_by_default_counts_nothing_and_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FTM_RATE_BUDGET", raising=False)
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    c = Clock()
    for _ in range(500):
        assert take(c) == 0.0
    assert c.slept == []
    assert not (tmp_path / "rate.db").exists(), "an off budget must not even create its counter"


def test_the_default_share_is_twenty_a_minute(monkeypatch):
    monkeypatch.delenv("FTM_RATE_PER_MIN", raising=False)
    monkeypatch.delenv("FTM_RATE_PER_HOUR", raising=False)
    assert B.per_min("datasift") == 20
    assert B.per_hour("datasift") == 1200


def test_a_full_minute_waits_for_the_next_one(budget):
    for _ in range(4):
        assert take(budget) == 0.0
    waited = take(budget)
    assert 59 < waited <= 61 and len(budget.slept) == 1
    assert B.usage(t=budget.now())["minute"] == 1, "the waiting request was counted in the NEW minute"


def test_a_full_hour_is_refused_not_slept_on(budget):
    for _ in range(10):
        take(budget)
    before = len(budget.slept)
    with pytest.raises(B.BudgetExhausted) as exc:
        take(budget)
    assert "hour" in str(exc.value)
    assert len(budget.slept) == before, "an hourly refusal must not sleep the run for forty minutes"


def test_refusals_and_waits_are_counted_for_the_report(budget):
    for _ in range(10):
        take(budget)
    with pytest.raises(B.BudgetExhausted):
        take(budget)
    u = B.usage(t=budget.now())
    assert u["hour"] == 10 and u["hour_cap"] == 10
    assert u["hour_refusals_today"] == 1
    assert u["minute_waits_today"] >= 1


def test_two_processes_share_one_count(budget):
    """The counter is a file, not a variable: a second caller sees what the first one used."""
    for _ in range(3):
        take(budget)
    other = Clock()
    other.t = budget.t
    take(other)                                   # the fourth of four
    waited = take(other)                          # the fifth has to wait
    assert waited > 0


def test_a_long_scan_stops_short_of_the_whole_minute(budget):
    """Work without priority keeps a fifth of each minute free for the morning upload."""
    for _ in range(3):                            # 80% of 4 is 3
        assert take(budget, priority=False) == 0.0
    assert take(budget, priority=True) == 0.0, "priority work may use the reserved slot"
    assert take(budget, priority=False) > 0


def test_the_siftmap_bucket_is_counted_apart(budget, monkeypatch):
    monkeypatch.setenv("FTM_SIFTMAP_PER_MIN", "2")
    for _ in range(4):
        take(budget, name="datasift")
    assert take(budget, name="siftmap") == 0.0, "a full DataSift minute does not block SiftMap"
    assert B.usage("siftmap", t=budget.now())["minute"] == 1


def test_a_counter_that_cannot_be_opened_never_blocks_the_upload(budget, monkeypatch):
    def broken(*a, **k):
        raise OSError("volume is read only")

    monkeypatch.setattr(B, "_take", broken)
    waited = take(budget)
    assert waited == pytest.approx(60 / 4), "falls back to an even gap, it does not raise"


# ── the hook inside the uploader ──────────────────────────────────────


def test_the_uploader_does_not_touch_the_budget_when_it_is_off(monkeypatch):
    monkeypatch.delenv("FTM_RATE_BUDGET", raising=False)
    assert U._budget() is None


def test_the_uploader_uses_the_budget_when_it_is_on(budget):
    assert U._budget() is B


def test_a_server_refusal_waits_on_the_hint_it_gives():
    assert U._refusal_wait("Request was throttled. Expected available in 7 seconds.", None, 0) == 9
    assert U._refusal_wait("", "12", 0) == 13
    assert U._refusal_wait("no hint here", None, 1) == 20
    assert U._refusal_wait("Expected available in 900 seconds.", None, 0) == 90, "capped"


def test_the_module_selftest_passes():
    assert B.selftest() == 0
