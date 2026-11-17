"""datasift_api_upload: the columns and the request budget this build added, on and off.

Zero network. urlopen is replaced by a script of responses; nothing sleeps for real.

Run: python -m pytest tests/test_ftm_uploader_switches.py
"""
import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api_upload as U  # noqa: E402
import ftm_budget  # noqa: E402

SWITCHES = ("FTM_RATE_BUDGET", "FTM_NOTICE_TEXT", "FTM_TAG_APPEARED_AGAIN", "FTM_TAX_SALE_DATE_FIELD",
            "FTM_DEBT_FIELD", "FTM_LISTING_FIELD", "FTM_NOTICE_TEXT_FIELD")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)


# ── the field map ─────────────────────────────────────────────────────


def test_every_column_the_uploader_has_always_mapped_is_still_mapped_the_same_way():
    fm = U.field_map()
    for column, label in U.FIELD_MAP.items():
        assert fm[column] == label
    assert U.FIELD_MAP["Foreclosure Date"] == "Foreclosure Date" and len(U.FIELD_MAP) == 16


def test_the_added_columns_are_blank_on_todays_files_so_they_send_nothing():
    """A notice row leaves Tax Auction Date empty unless it is a tax sale, and the intake columns
    do not exist until the intake switch is on."""
    from datasift_formatter import DATASIFT_COLUMNS, _build_row
    from notice_parser import NoticeData

    idx = {label: {"uuid": "f-%d" % i, "field_type": "text", "options": {}}
           for i, label in enumerate(sorted(set(U.field_map().values())))}
    for ntype in ("foreclosure", "probate"):
        n = NoticeData(address="412 Sample Hollow Rd", city="Knoxville", zip="37918", county="Knox",
                       notice_type=ntype, owner_name="Marlowe Quillfeather", auction_date="2026-12-10",
                       date_added="2026-11-13", date_published="2026-11-12")
        row = _build_row(n)
        sent = {p["field_uuid"] for p in U.field_pairs(row, idx, set())}
        added = {idx[U.field_map()[c]]["uuid"] for c in ("Tax Auction Date", "Mortgage Debt", "Listing Status")}
        assert not sent & added, "a %s row must not write any of the added fields" % ntype
    assert "Mortgage Debt" not in DATASIFT_COLUMNS and "Listing Status" not in DATASIFT_COLUMNS


def test_a_tax_sale_row_sends_its_sale_date_as_a_date():
    idx = {"Tax Auction Date": {"uuid": "f-tax", "field_type": "date", "options": {}}}
    warn = set()
    pairs = U.field_pairs({"Tax Auction Date": "1/20/2027"}, idx, warn)
    assert pairs == [{"field_uuid": "f-tax", "value": "2027-01-20"}] and not warn


def test_a_field_the_account_lacks_is_skipped_with_a_warning_never_guessed():
    warn = set()
    assert U.field_pairs({"Mortgage Debt": "140000"}, {}, warn) == []
    assert warn == {"Mortgage Debt"}


def test_each_added_label_is_a_setting(monkeypatch):
    monkeypatch.setenv("FTM_TAX_SALE_DATE_FIELD", "Foreclosure Date")
    monkeypatch.setenv("FTM_DEBT_FIELD", "Loan Balance")
    fm = U.field_map()
    assert fm["Tax Auction Date"] == "Foreclosure Date" and fm["Mortgage Debt"] == "Loan Balance"


# ── the request budget inside Api.call ────────────────────────────────


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def refused(seconds=3):
    return urllib.error.HTTPError("https://example.invalid", 429, "Too Many Requests", {},
                                  io.BytesIO(b'{"detail":"Request was throttled. Expected available in %d seconds."}' % seconds))


def api_with(monkeypatch, script):
    """An Api on a key (so nothing is minted) whose urlopen plays `script` in order."""
    calls, slept = [], []

    def urlopen(req, timeout=60):
        calls.append(req.full_url)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Response(step)

    monkeypatch.setattr(U.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(U.time, "sleep", slept.append)
    return U.Api(api_key="test-key-not-real"), calls, slept


def test_with_the_budget_off_a_refusal_is_raised_exactly_as_before(monkeypatch):
    api, calls, slept = api_with(monkeypatch, [refused()])
    with pytest.raises(U.ApiError) as exc:
        api.call("/api/internal/list/")
    assert exc.value.code == 429 and len(calls) == 1 and slept == []


def test_with_the_budget_on_a_refusal_waits_on_the_hint_and_goes_again(monkeypatch, tmp_path):
    monkeypatch.setenv("FTM_RATE_BUDGET", "1")
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    api, calls, slept = api_with(monkeypatch, [refused(3), {"results": []}])
    assert api.call("/api/internal/list/") == {"results": []}
    assert len(calls) == 2 and slept == [5]
    assert ftm_budget.usage()["server_429_today"] == 1, "the refusal is counted for the health report"


def test_a_server_that_keeps_refusing_is_given_up_on(monkeypatch, tmp_path):
    monkeypatch.setenv("FTM_RATE_BUDGET", "1")
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    api, calls, _ = api_with(monkeypatch, [refused(), refused(), refused(), refused(), refused()])
    with pytest.raises(U.ApiError):
        api.call("/api/internal/list/")
    assert len(calls) == 4, "three more tries, then the error"


def test_every_call_is_counted_against_the_share_when_the_budget_is_on(monkeypatch, tmp_path):
    monkeypatch.setenv("FTM_RATE_BUDGET", "1")
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    api, _, _ = api_with(monkeypatch, [{}, {}, {}])
    for _ in range(3):
        api.call("/api/internal/list/")
    assert ftm_budget.usage()["hour"] == 3


def test_with_the_budget_off_no_counter_file_is_ever_created(monkeypatch, tmp_path):
    monkeypatch.setenv("FTM_RATE_DB", str(tmp_path / "rate.db"))
    api, _, _ = api_with(monkeypatch, [{}])
    api.call("/api/internal/list/")
    assert not (tmp_path / "rate.db").exists()


def test_an_upload_stops_when_the_hours_budget_is_spent_and_says_how_many_were_left(monkeypatch, tmp_path):
    class Spent:
        def __init__(self):
            self.creates = 0

        def call(self, path, method="GET", body=None, headers=None):
            if method == "POST" and path == U.CREATE_ROUTE:
                self.creates += 1
                if self.creates > 1:
                    raise ftm_budget.BudgetExhausted("this box has used its 1200 datasift requests for the hour")
                return {"uuid": "u-%d" % self.creates}
            return {}

    stub = Spent()
    monkeypatch.setattr(U, "Api", lambda: stub)
    rows = [{"Property Street Address": "%d Sample Hollow Rd" % i, "Owner First Name": "Marlowe",
             "Owner Last Name": "Quillfeather"} for i in range(400, 405)]
    res = U.upload_rows(rows, commit=True, sleep=0, out_dir=str(tmp_path))
    assert res["created"] == 1 and res["failed"] == 4
    assert stub.creates == 2, "it stopped at the first refusal instead of failing every row in turn"
    assert any("upload stopped early" in w for w in res["warnings"])


def test_the_module_holds_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/datasift_api_upload.py", "src/ftm_budget.py", "tests/test_ftm_uploader_switches.py",
                 "tests/test_ftm_budget.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
