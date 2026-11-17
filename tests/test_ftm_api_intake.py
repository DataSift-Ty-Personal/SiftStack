"""api_intake: the photo import and the trace merges, over the API instead of the browser.

Zero network. A scripted stub stands in for the DataSift API and records every call, so the
tests assert the ROUTE and the BODY that would go over the wire. Every address, owner and phone
number is invented (the numbers use the 555 exchange).

Run: python -m pytest tests/test_ftm_api_intake.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import api_intake as A  # noqa: E402
import datasift_api_upload as U  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
OWNER = "99999999-8888-7777-6666-555555555555"
DUP = '{"non_field_errors":["Property address already exists!"],"property":["%s"]}' % UUID


class Account:
    """A one-record DataSift. It keeps the lists, tags and phones that are written to it, so a
    read-back sees what an upload really left behind."""

    def __init__(self, exists=False, phones=(), lists=(), tags=(), traced=0, findable=True,
                 lose_lists=False, lose_phones=False):
        self.exists, self.findable = exists, findable
        self.lists, self.tags = list(lists), list(tags)
        self.phones = [{"number": p, "status": "CORRECT", "tags": ["Dial First"]} for p in phones]
        self.traced = traced
        self.lose_lists, self.lose_phones = lose_lists, lose_phones
        self.calls = []

    def record(self):
        return {"uuid": UUID, "lists": list(self.lists), "tags": list(self.tags), "notes": "",
                "owner": {"uuid": OWNER, "first_name": "Marlowe", "last_name": "Quillfeather",
                          "phones": list(self.phones), "skiptrace_attempts": self.traced}}

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path, body))
        if headers and headers.get("x-http-method-override") == "GET":
            rows = [{"uuid": UUID, "address": {"street": "412 Sample Hollow Rd", "postal_code": "37918"}}]
            return {"results": rows if (self.exists and self.findable) else []}
        if method == "POST" and path == U.CREATE_ROUTE:
            if self.exists:
                raise U.ApiError(400, "POST", path, DUP)
            self.exists = True
            if not self.lose_lists:
                self.lists += [x.strip() for x in str(body.get("lists") or "").split(",") if x.strip()]
            self.tags += list(body.get("tags") or [])
            return {"uuid": UUID}
        if path.endswith("/add-lists/") and not self.lose_lists:
            self.lists.append(body["lists"])
        if method == "PATCH" and path == "/api/internal/property/%s/" % UUID and "tags" in (body or {}):
            self.tags = list(body["tags"])
        if path.endswith("/upsert-phones/") and not self.lose_phones:
            self.phones += [dict(p) for p in body["phones"]]
        if path.endswith("/skiptrace-attempts/"):
            self.traced = body["skiptrace_attempts"]
        if method == "GET" and path == "/api/internal/property/%s/" % UUID:
            return self.record()
        return {}

    def sent(self, suffix):
        return [c[2] for c in self.calls if c[1].endswith(suffix)]


def row(**kw):
    base = {"Property Street Address": "412 Sample Hollow Rd", "Property City": "Knoxville", "Property State": "TN",
            "Property ZIP Code": "37918", "Owner First Name": "Marlowe", "Owner Last Name": "Quillfeather",
            "Lists": "Eviction", "Tags": "Courthouse Data,FTM,eviction", "Notes": "Eviction | photo import",
            "Phone 1": "(865) 555-0101", "Phone 2": "865-555-0102", "Phone 7": "18655550103"}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def no_switches(monkeypatch):
    monkeypatch.delenv("FTM_UPLOAD_BROWSER", raising=False)
    monkeypatch.delenv("FTM_RATE_BUDGET", raising=False)


def upload(acct, rows=None, **kw):
    return A.upload_rows(rows or [row()], commit=True, api=acct, sleep=0, **kw)


# ── phones ────────────────────────────────────────────────────────────


def test_phone_columns_become_ten_digit_numbers_each_once():
    got = A.phones_of(row(**{"Phone 3": "865.555.0101", "Phone 4": "555-0199", "Phone 8": ""}))
    assert got == [("8655550101", "UNKNOWN"), ("8655550102", "MOBILE"), ("8655550103", "LANDLINE")]


def test_a_number_the_owner_already_has_is_never_sent():
    """upsert-phones replaces the phone BY NUMBER, so re-sending one would wipe the status and
    the tier a person set on it."""
    acct = Account(exists=True, phones=["8655550101"])
    fresh = A.new_phones(acct.record(), A.phones_of(row()))
    assert [n for n, _k in fresh] == ["8655550102", "8655550103"]


def test_new_numbers_are_written_with_their_source_and_the_trace_is_counted():
    acct = Account(exists=True, phones=["8655550101"], traced=2)
    n = A.write_phones(acct, acct.record(), A.phones_of(row()), "Tracerfy")
    assert n == 2
    body = acct.sent("/upsert-phones/")[0]
    assert [p["number"] for p in body["phones"]] == ["8655550102", "8655550103"]
    assert body["phones"][0] == {"number": "8655550102", "type": "MOBILE", "tags": ["Tracerfy"],
                                 "status": "UNKNOWN", "is_connected": True, "verified": False}
    assert acct.sent("/skiptrace-attempts/") == [{"skiptrace_attempts": 3}], "the TOTAL, one higher"


def test_no_new_number_means_no_write_and_no_counted_trace():
    acct = Account(exists=True, phones=["8655550101", "8655550102", "8655550103"])
    assert A.write_phones(acct, acct.record(), A.phones_of(row())) == 0
    assert acct.sent("/upsert-phones/") == [] and acct.sent("/skiptrace-attempts/") == []


# ── the photo import's upload ─────────────────────────────────────────


def test_a_new_record_is_created_with_its_list_tags_notes_and_phones():
    acct = Account()
    res = upload(acct, source_tag="Tracerfy")
    assert res["created"] == 1 and res["existing"] == 0 and res["phones"] == 3 and not res["stopped"]
    create = [c[2] for c in acct.calls if c[1] == U.CREATE_ROUTE][0]
    assert create["lists"] == "Eviction" and create["tags"] == ["Courthouse Data", "FTM", "eviction"]
    assert acct.sent("/add-notes/") == [{"notes": "Eviction | photo import"}]
    assert len(acct.phones) == 3


def test_an_existing_record_gets_the_list_and_only_the_numbers_it_lacks():
    acct = Account(exists=True, phones=["8655550101"], lists=["Liens"], tags=["Courthouse Data"])
    res = upload(acct)
    assert res["existing"] == 1 and res["phones"] == 2
    assert acct.sent("/add-lists/") == [{"lists": "Eviction"}]
    assert "Liens" in acct.lists and "Eviction" in acct.lists, "lists accumulate"
    assert acct.phones[0] == {"number": "8655550101", "status": "CORRECT", "tags": ["Dial First"]}, "untouched"


def test_the_first_record_is_read_back_and_a_missing_list_stops_everything():
    acct = Account(lose_lists=True)
    res = upload(acct, rows=[row(), row(**{"Property Street Address": "88 Invented Way"})])
    assert res["stopped"].startswith("the first record did not read back: the record is not on the list 'Eviction'")
    assert res["created"] == 1 and res["failed"] == 1
    assert len([c for c in acct.calls if c[1] == U.CREATE_ROUTE]) == 1, "the second row was never sent"


def test_a_phone_that_did_not_stick_stops_everything():
    acct = Account(lose_phones=True)
    res = upload(acct, rows=[row(), row()])
    assert "a phone number that was sent is not on the owner" in res["stopped"]


def test_a_dry_run_makes_no_call_at_all():
    acct = Account()
    res = A.upload_rows([row()], commit=False, api=acct)
    assert acct.calls == [] and res["created"] == 0 and res["submitted"] == 1


def test_notices_are_uploaded_as_the_pipelines_own_rows():
    n = NoticeData(address="412 Sample Hollow Rd", city="Knoxville", state="TN", zip="37918", county="Knox",
                   notice_type="eviction", owner_name="Marlowe Quillfeather", primary_phone="8655550101",
                   source_url="photo:IMG_0001.jpg")
    rows = A.rows_from_notices([n])
    assert rows[0]["Lists"] == "Eviction" and rows[0]["Phone 1"] == "8655550101"
    assert "photo_import" in rows[0]["Tags"]
    acct = Account()
    res = A.upload_notices([n], commit=True, api=acct)
    assert res["created"] == 1 and res["phones"] == 1


# ── the trace merge ───────────────────────────────────────────────────

TAGS = ["FTM", "foreclosure", "Courthouse Data"]


def merge(acct, rows=None, **kw):
    return A.merge_phones(rows or [row()], commit=True, api=acct, source_tag="Tracerfy",
                          list_name="Foreclosure", tags=TAGS, sleep=0, **kw)


def test_a_merge_writes_the_new_numbers_onto_the_existing_record():
    acct = Account(exists=True, phones=["8655550101"], lists=["Foreclosure"], tags=["FTM"])
    res = merge(acct)
    assert res["matched"] == 1 and res["phones"] == 2 and not res["stopped"]
    assert [p["number"] for p in acct.sent("/upsert-phones/")[0]["phones"]] == ["8655550102", "8655550103"]
    assert set(TAGS) <= set(acct.tags)


def test_a_merge_never_creates_a_record():
    acct = Account(exists=False)
    res = merge(acct)
    assert res["not_in_account"] == 1 and res["phones"] == 0
    assert not [c for c in acct.calls if c[1] == U.CREATE_ROUTE and c[0] == "POST" and c[2] and "owner" in c[2]]
    assert acct.sent("/upsert-phones/") == []


def test_a_merge_with_nothing_new_leaves_the_record_alone():
    acct = Account(exists=True, phones=["8655550101", "8655550102", "8655550103"])
    res = merge(acct)
    assert res["no_new_numbers"] == 1
    assert acct.sent("/upsert-phones/") == [] and acct.sent("/add-lists/") == []


def test_a_merge_reads_the_first_record_back():
    acct = Account(exists=True, lose_phones=True, lists=["Foreclosure"], tags=TAGS)
    res = merge(acct, rows=[row(), row()])
    assert "did not read back" in res["stopped"]
    assert len(acct.sent("/upsert-phones/")) == 1, "the second record was never written"


def test_a_row_with_no_phone_is_skipped_without_a_lookup():
    acct = Account(exists=True)
    res = merge(acct, rows=[row(**{"Phone 1": "", "Phone 2": "", "Phone 7": ""})])
    assert acct.calls == [] and res["matched"] == 0


def test_a_dry_merge_makes_no_call():
    acct = Account(exists=True)
    A.merge_phones([row()], commit=False, api=acct)
    assert acct.calls == []


# ── which route the callers take ──────────────────────────────────────


def test_the_api_is_the_default_and_the_browser_is_a_switch(monkeypatch):
    assert A.browser_wanted() is False
    monkeypatch.setenv("FTM_UPLOAD_BROWSER", "1")
    assert A.browser_wanted() is True


def test_the_tracerfy_merge_goes_over_the_api_unless_the_browser_is_asked_for(tmp_path):
    import csv
    import tracerfy_ftm

    path = tmp_path / "merge.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row()))
        w.writeheader()
        w.writerow(row())
    seen = {}

    def fake(rows, **kw):
        seen.update(kw, rows=rows)
        return {"phones": 2, "stopped": ""}

    out = tracerfy_ftm.merge(str(path), TAGS, merger=fake)
    assert out["route"] == "api" and seen["source_tag"] == "Tracerfy" and seen["list_name"] == "Foreclosure"
    assert seen["commit"] is True and seen["rows"][0]["Phone 2"] == "865-555-0102"


def test_the_enformion_merge_goes_over_the_api_too(tmp_path):
    import csv
    import enformion_ftm

    path = tmp_path / "merge.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row()))
        w.writeheader()
        w.writerow(row())
    seen = {}
    out = enformion_ftm.merge(str(path), TAGS, merger=lambda rows, **kw: seen.update(kw) or {"phones": 1})
    assert out["route"] == "api" and seen["source_tag"] == "Enformion"


def test_neither_trace_module_imports_the_browser_wizard_at_import_time():
    import enformion_ftm
    import tracerfy_ftm
    assert not hasattr(tracerfy_ftm, "run_upload") and not hasattr(enformion_ftm, "run_upload")


def test_the_photo_watcher_uploads_over_the_api_and_reports_in_the_shape_slack_reads(monkeypatch):
    dropbox_watcher = pytest.importorskip("dropbox_watcher")
    monkeypatch.setenv("REISIFT_API_KEY", "test-key-not-real")
    calls = []

    def fake(notices, commit=False, source_tag=""):
        calls.append((len(notices), commit, source_tag))
        return {"submitted": 2, "created": 2, "existing": 1, "phones": 3, "failed": 0, "stopped": ""}

    out = dropbox_watcher.upload_to_datasift(["n1", "n2"], uploader=fake)
    assert calls == [(2, True, "Tracerfy")]
    assert out["success"] is True and out["records_uploaded"] == 2
    assert "2 of 2 record(s) into DataSift over the API" in out["message"]
    assert "enrichment and the skip trace were not started" in out["message"]


def test_the_photo_watcher_reports_a_stopped_upload_as_a_failure(monkeypatch):
    dropbox_watcher = pytest.importorskip("dropbox_watcher")
    monkeypatch.setenv("REISIFT_API_KEY", "test-key-not-real")
    out = dropbox_watcher.upload_to_datasift(["n1"], uploader=lambda n, **k: {
        "created": 1, "stopped": "the first record did not read back: the record is not on the list 'Eviction'."})
    assert out["success"] is False and "did not read back" in out["message"]


def test_the_photo_watcher_takes_the_browser_route_only_when_asked(monkeypatch):
    dropbox_watcher = pytest.importorskip("dropbox_watcher")
    monkeypatch.setenv("FTM_UPLOAD_BROWSER", "1")
    took = []
    monkeypatch.setattr(dropbox_watcher, "_upload_via_browser", lambda notices: took.append(len(notices)) or {"success": True})
    api_called = []
    dropbox_watcher.upload_to_datasift(["n1"], uploader=lambda *a, **k: api_called.append(1))
    assert took == [1] and api_called == []


def test_the_modules_hold_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/api_intake.py", "src/tracerfy_ftm.py", "src/enformion_ftm.py", "src/dropbox_watcher.py",
                 "tests/test_ftm_api_intake.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
