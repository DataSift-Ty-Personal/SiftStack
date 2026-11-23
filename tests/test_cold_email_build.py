"""Building a cold email list past the records search's 10,000-row window (2026-11-20): a preset that
large is read in slices of its own created dates, newest first, and a resumed build reads on where it
stopped. Zero network: the records search is a fake that refuses an offset past its window exactly as
DataSift does (the window is shrunk to 12 rows here). Every name, street and email is invented."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "cold_email"))
sys.path.insert(0, str(ROOT / "src"))

import build_list  # noqa: E402


def _at(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S" if "T" in s else "%Y-%m-%d")


class FakeCRM:
    def __init__(self, records):
        self.records = sorted(records, key=lambda r: r["_created"], reverse=True)
        self.searches, self.reads = [], 0

    def preset_must(self, title):
        return {"any_lists": ["Agency"]}

    def search(self, must, limit=250, offset=0):
        if offset + limit > build_list.WINDOW:
            raise RuntimeError("Can't fetch more than %d items!" % build_list.WINDOW)
        self.searches.append((dict(must), limit, offset))
        rows = self.records
        if must.get("created"):
            lo, hi = (_at(x) for x in must["created"])
            assert ("T" in must["created"][0]) == ("T" in must["created"][1]), "a window is all dates or all datetimes"
            rows = [r for r in rows if lo <= r["_created"] < hi]
        return len(rows), [{"uuid": r["uuid"], "status": None, "owner": r["owner"], "address": {"state": "TN"}}
                           for r in rows[offset:offset + limit]]

    def record(self, uuid):
        self.reads += 1
        r = next(x for x in self.records if x["uuid"] == uuid)
        return {k: v for k, v in r.items() if k != "_created"}


def _records():
    out = []
    start = datetime(2026, 6, 1)
    for i in range(30):                                   # a record every few days
        out.append(start + timedelta(days=4 * i, hours=3))
    for m in range(15):                                   # a bulk pull: fifteen records in one quarter hour
        out.append(datetime(2026, 10, 19, 14, m, 30))
    recs = []
    for i, created in enumerate(out):
        owner = {"uuid": "o%d" % i, "first_name": "Pat", "last_name": "Sample%d" % i,
                 "emails": [{"email": "pat.sample%d@example.com" % i}], "phones": []}
        recs.append({"uuid": "u%d" % i, "_created": created, "status": None, "tags": [], "lists": [], "owner": owner,
                     "address": {"street": "%d Example St" % (100 + i), "city": "Knoxville", "state": "TN",
                                 "postal_code": "37900"}})
    return recs


@pytest.fixture
def small(monkeypatch, tmp_path):
    monkeypatch.setattr(build_list, "WINDOW", 12)
    monkeypatch.setattr(build_list, "PAGE", 4)
    monkeypatch.setattr(build_list, "REJECTED", tmp_path / "rejected.json")
    return tmp_path


def _state():
    return {"seen": [], "candidates": [], "skipped": {}, "zb": {}, "owner_check": {}}


def test_a_large_preset_is_cut_into_windows_the_search_can_page(small):
    crm = FakeCRM(_records())
    slices = build_list.created_slices(crm, {"any_lists": ["Agency"]}, 45, now=datetime(2026, 11, 20))
    assert all(n <= 12 for _, _, n in slices), slices
    assert sum(n for _, _, n in slices) == 45, "every record falls in exactly one window"
    assert any("T" in lo for lo, _, _ in slices), "the bulk day is cut below a day"
    los = [_at(lo) for lo, _, _ in slices]
    assert los == sorted(los, reverse=True), "newest first"


def test_a_preset_that_fits_is_one_window(small):
    crm = FakeCRM(_records()[:10])
    assert build_list.created_slices(crm, {"any_lists": ["Agency"]}, 10) == [[None, None, 10]]
    assert build_list.created_slices(crm, {"created": ["2026-01-01", "2026-02-01"]}, 400) == [[None, None, 400]]


def test_the_pull_reads_every_record_past_the_window_and_resumes(small):
    crm = FakeCRM(_records())
    state = _state()
    build_list.pull(crm, "Agency - 01 Queue", 10, state, {"owners": {}, "emails": {}}, small / "list")
    assert len(state["candidates"]) == 10 and state["slices"] and state["slice"] < len(state["slices"])
    build_list.pull(crm, "Agency - 01 Queue", 100, state, {"owners": {}, "emails": {}}, small / "list")
    got = [c["record"] for c in state["candidates"]]
    assert len(got) == 45 and len(set(got)) == 45, "all 45 records, past the 12-row window, none twice"
    assert all(off + lim <= build_list.WINDOW for _, lim, off in crm.searches)


def test_the_email_channel_is_the_gate_the_list_uses(small):
    rec = _records()[0]
    rec["tags"] = ["Return Mail"]
    assert build_list.email_verdict({k: v for k, v in rec.items() if k != "_created"}).ok, \
        "mail that came back says nothing about an email address"
    rec["tags"] = ["Do Not Market"]
    assert build_list.email_verdict({k: v for k, v in rec.items() if k != "_created"}).code == "do_not_market"
