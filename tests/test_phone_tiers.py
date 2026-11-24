"""phone_tiers.py and the account reader the re-skips use, on a fake DataSift account: no network, invented records."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import api_intake  # noqa: E402
import d4d_call_list as d4d  # noqa: E402
import phone_tiers as T  # noqa: E402


def rec(u, owner, phones, street="100 Example St"):
    return {"uuid": u, "dataflik_id": 1000 + len(u), "address": {"street": street, "city": "Testville", "state": "TN",
                                                                 "postal_code": "37000"},
            "owner": {"uuid": owner, "first_name": "Pat", "last_name": "Sample", "phones": phones}}


def ph(num, tags=(), status="UNKNOWN"):
    return {"number": num, "type": "MOBILE", "tags": [{"title": t} for t in tags], "status": status,
            "is_connected": True, "verified": False}


class FakeApi:
    def __init__(self, recs, lose=False):
        self.recs, self.lose, self.calls = {r["uuid"]: r for r in recs}, lose, []

    def call(self, path, method="GET", body=None, headers=None, **_kw):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/api/internal/list/?limit=999":
            return [{"uuid": "L1", "title": "Foreclosure"}]
        if path == "/api/internal/property/" and method == "POST":
            must = body["query"]["must"]
            rows = [{"uuid": r["uuid"], "address": {"street": r["address"]["street"], "city": "Testville", "state": "TN",
                                                    "zip5": "37000"},
                     "owner": {"first_name": "Pat", "last_name": "Sample", "company": None}}
                    for r in self.recs.values() if "phone" not in must or bool(r["owner"]["phones"]) == bool(must["phone"])]
            return {"count": len(rows), "results": rows[body["offset"]:body["offset"] + body["limit"]]}
        if path.startswith("/api/internal/property/") and method == "GET":
            return copy.deepcopy(self.recs[path.split("/")[4]])
        if path.endswith("/upsert-phones/"):
            owner = path.split("/")[4]
            for r in self.recs.values():
                if r["owner"]["uuid"] == owner and not self.lose:
                    by = {p["number"]: p for p in r["owner"]["phones"]}
                    for p in body["phones"]:
                        by[p["number"]] = {**p, "tags": [{"title": t} for t in p["tags"]]}
                    r["owner"]["phones"] = list(by.values())
            return {}
        raise AssertionError("unexpected call %s %s" % (method, path))


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "SCORES", tmp_path / "scores.json")
    monkeypatch.setattr(d4d, "_router", lambda: None)
    monkeypatch.setattr(d4d, "_last_call", [0.0])
    monkeypatch.setattr(d4d.time, "sleep", lambda _s: None)     # the read pacing is real time; a fake needs none
    monkeypatch.setattr(T, "trestle_key", lambda: "test-key-not-real")
    scored = []

    def fake_score(router, key, num):
        scored.append(num)
        return {"score": 90 if num.endswith("1") else 30, "tier": "Dial First" if num.endswith("1") else "Dial Fourth"}
    monkeypatch.setattr(d4d, "_score_one", fake_score)
    return scored


def records():
    return [rec("r1", "o1", [ph("8650000101"), ph("8650000102", tags=["Dial Second"]),
                             ph("8650000103", status="WRONG")]),
            rec("r2", "o2", [ph("8650000111"), ph("8650000112", tags=["DNC"])], street="200 Sample Rd")]


def test_dry_run_scores_nothing_and_writes_nothing(quiet):
    api = FakeApi(records())
    res = T.tier_records(api, ["r1", "r2"], commit=False, out=lambda *a: None)
    assert res["to_score"] == 2 and res["estimate_usd"] == 0.03
    assert quiet == [] and not [c for c in api.calls if c[1].endswith("/upsert-phones/")]


def test_commit_scores_only_untiered_numbers_and_writes_one_tier_each(quiet):
    api = FakeApi(records())
    res = T.tier_records(api, ["r1", "r2"], commit=True, out=lambda *a: None)
    # the tiered, the wrong number and the litigator are never bought
    assert sorted(quiet) == ["8650000101", "8650000111"]
    writes = [c for c in api.calls if c[1].endswith("/upsert-phones/")]
    assert len(writes) == 2 and res["mismatches"] == 0
    for _m, _p, body in writes:
        for p in body["phones"]:
            assert [t for t in p["tags"] if t.startswith("Dial ")] == ["Dial First"]
    # a second run buys nothing: the answer is kept
    quiet.clear()
    T.tier_records(FakeApi(records()), ["r1", "r2"], commit=True, out=lambda *a: None)
    assert quiet == []


def test_a_tier_that_does_not_read_back_stops_the_run():
    with pytest.raises(SystemExit) as e:
        T.tier_records(FakeApi(records(), lose=True), ["r1"], commit=True, out=lambda *a: None)
    assert "did not read back" in str(e.value)


def test_account_targets_reads_the_list_and_the_phone_flag():
    r3 = rec("r3", "o3", [], street="300 Demo Ln")
    api = FakeApi(records() + [r3])
    got = api_intake.account_targets(api, "Foreclosure")
    assert [t["uuid"] for t in got] == ["r3"]
    assert got[0] == {"uuid": "r3", "address": "300 Demo Ln", "city": "Testville", "state": "TN", "zip": "37000",
                      "first_name": "Pat", "last_name": "Sample", "company": ""}
    assert len(api_intake.account_targets(api, "Foreclosure", no_phone_only=False)) == 3
    with pytest.raises(SystemExit):
        api_intake.account_targets(api, "No Such List")


def test_the_reskips_are_dry_by_default_and_never_call_a_vendor(monkeypatch, capsys):
    import enformion_ftm
    import tracerfy_ftm
    monkeypatch.setattr(api_intake, "account_targets", lambda api, name, no_phone_only=True: [
        {"uuid": "r9", "address": "900 Example St", "city": "Testville", "state": "TN", "zip": "37000",
         "first_name": "Pat", "last_name": "Sample", "company": ""}])
    import datasift_api_upload as up
    monkeypatch.setattr(up, "Api", lambda *a, **k: object())
    monkeypatch.setattr(tracerfy_ftm, "batch_skip_trace", lambda *a, **k: pytest.fail("a dry run bought a trace"))
    monkeypatch.setattr(enformion_ftm, "person_search", lambda *a, **k: pytest.fail("a dry run bought a search"))
    assert tracerfy_ftm.main([]) == 0 and enformion_ftm.main([]) == 0
    out = capsys.readouterr().out
    assert "1 to trace" in out and "1 people to search" in out and out.count("DRY RUN") == 2


def test_the_reskips_refuse_over_the_spend_ceiling(monkeypatch):
    import tracerfy_ftm
    many = [{"uuid": "r%d" % i, "address": "%d Example St" % i, "city": "Testville", "state": "TN", "zip": "37000",
             "first_name": "Pat", "last_name": "Sample", "company": ""} for i in range(1, 60)]
    monkeypatch.setattr(api_intake, "account_targets", lambda api, name, no_phone_only=True: many)
    import datasift_api_upload as up
    monkeypatch.setattr(up, "Api", lambda *a, **k: object())
    with pytest.raises(SystemExit) as e:
        tracerfy_ftm.main(["--max-usd", "1"])
    assert "REFUSING" in str(e.value)
