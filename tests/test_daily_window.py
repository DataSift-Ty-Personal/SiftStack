"""The daily run's date window, and the run warning for a saved search that was never really read.

Found 2026-11-18: the default 12-month Knox probate search returned its 1000-row cap spread over
the year and dropped the newest weeks, so the feed read nothing for a week while every page read
"fine". Daily runs now search an explicit window. Zero network.

Run: python -m pytest tests/test_daily_window.py
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_runner as R  # noqa: E402
import scraper  # noqa: E402

NOW = datetime(2026, 11, 18, 6, 30)


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("FTM_DAILY_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("FTM_DAILY_WINDOW_MAX_DAYS", raising=False)


def test_window_reaches_back_two_weeks_after_a_normal_run():
    # Yesterday's run: the window still starts 14 days back, so a notice the site posts days after
    # its publication date is caught (the old since-last-run cutoff skipped those for good).
    assert scraper.daily_window("2026-11-17", NOW) == ("11/4/2026", "11/18/2026", "")


def test_window_reaches_to_the_last_run_after_a_short_gap():
    assert scraper.daily_window("2026-10-30", NOW)[:2] == ("10/29/2026", "11/18/2026")


def test_a_long_gap_is_floored_and_said():
    d_from, d_to, note = scraper.daily_window("2026-09-01", NOW)
    assert (d_from, d_to) == ("10/18/2026", "11/18/2026")
    assert "backfill" in note and "2026-09-01" in note


def test_an_explicit_since_is_used_as_given():
    assert scraper.daily_window("2026-08-01", NOW, explicit=True) == ("8/1/2026", "11/18/2026", "")


def test_no_previous_run_reads_two_weeks():
    assert scraper.daily_window(None, NOW)[:2] == ("11/4/2026", "11/18/2026")
    assert scraper.daily_window("not a date", NOW)[:2] == ("11/4/2026", "11/18/2026")


def test_settings_move_the_window(monkeypatch):
    monkeypatch.setenv("FTM_DAILY_WINDOW_DAYS", "3")
    assert scraper.daily_window("2026-11-17", NOW)[:2] == ("11/15/2026", "11/18/2026")
    monkeypatch.setenv("FTM_DAILY_WINDOW_MAX_DAYS", "10")
    assert scraper.daily_window("2026-10-01", NOW)[0] == "11/8/2026"


def test_a_future_since_never_makes_an_empty_window():
    assert scraper.daily_window("2026-12-01", NOW, explicit=True)[:2] == ("11/18/2026", "11/18/2026")


def _searches(*names):
    return [SimpleNamespace(saved_search_name=n) for n in names]


def test_unread_searches_become_run_warnings(monkeypatch):
    monkeypatch.setattr(scraper, "SEARCH_STATS", {
        "Probate V2 Knox": {"reached": True, "rows": 53},
        "Probate V2 Blount": {"reached": True, "window_failed": True},
    })
    monkeypatch.setattr(scraper, "DAILY_WINDOW_NOTE", [])
    res = SimpleNamespace(warnings=[])
    R._search_warnings(res, _searches("Probate V2 Knox", "Probate V2 Blount", "Foreclosure V2 Knox"))
    text = " | ".join(res.warnings)
    assert "Probate V2 Knox" not in text
    assert "Probate V2 Blount: the date window could not be applied" in text
    assert "Foreclosure V2 Knox: the search never opened" in text


def test_a_capped_run_does_not_call_the_searches_it_skipped_unread(monkeypatch):
    monkeypatch.setattr(scraper, "SEARCH_STATS", {"Probate V2 Knox": {"reached": True}})
    monkeypatch.setattr(scraper, "DAILY_WINDOW_NOTE", ["the last good run was 2026-09-01"])
    res = SimpleNamespace(warnings=[])
    R._search_warnings(res, _searches("Probate V2 Knox", "Foreclosure V2 Knox"), bounded=True)
    assert res.warnings == ["Daily date window: the last good run was 2026-09-01."]
