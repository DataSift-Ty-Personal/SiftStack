"""ai_pools.py on a fake SiftMap: no network, invented houses on invented streets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ai_pools as P  # noqa: E402
import dpd_live as D  # noqa: E402


def house(i, fips="47999", score=50, realtor=40, equity=50.0, value=200000, sold="", corp=False, zip5="37000",
          dist=(), absentee=False):
    return {"id": i, "fips": fips, "inv": score, "rea": realtor, "equityPercent": equity, "estimatedValue": value,
            "sold": sold, "corporateOwned": corp, "address": "%d Example St, Testville, TN %s" % (i, zip5),
            "dist": set(dist), "absentee": absentee}


class FakeMap:
    """Answers SiftMap search bodies the way the live search does for the filters ai_pools sends."""

    def __init__(self, houses, scores_visible=True):
        self.houses, self.visible, self.bodies = houses, scores_visible, []

    def __call__(self, body):
        self.bodies.append(body)
        f = body["filters"]
        fips = body["addresses"][0]["counties"][0]["fips"]
        out = []
        for h in self.houses:
            if h["fips"] != fips or not f.get("type_single_family"):
                continue
            if not (f.get("value_min", 0) <= h["estimatedValue"] <= f.get("value_max", 10 ** 9)):
                continue
            ok = True
            for key, field in (("investor_off_market_score", "inv"), ("realtor_score", "rea")):
                if key + "_min" in f or key + "_max" in f:
                    if not self.visible:
                        ok = False
                    elif not (f.get(key + "_min", 0) <= h[field] <= f.get(key + "_max", 100)):
                        ok = False
            if f.get("extra_last_sale_date_min") and not (h["sold"] and h["sold"] >= f["extra_last_sale_date_min"]):
                ok = False
            if f.get("distressors_include"):
                want = set(f["distressors_include"].split(","))
                if len(want & h["dist"]) < int(f.get("distressors_include_min_match") or 1):
                    ok = False
            if f.get("preset_absentee_owners") and not h["absentee"]:
                ok = False
            if ok:
                out.append(h)
        start = int(body.get("result_index") or 1) - 1
        size = int(body.get("size") or 250)
        rows = [{"id": h["id"], "address": h["address"], "equityPercent": h["equityPercent"],
                 "estimatedValue": h["estimatedValue"], "corporateOwned": h["corporateOwned"]}
                for h in out[start:start + size]]
        return {"total_results": len(out), "data": rows}


COUNTY = {"fips": "47999", "name": "Sample", "state": "TN"}
QUIET = lambda *a, **k: None  # noqa: E731


def search(fake):
    return D.Search(post=fake, gap=0)


def test_investor_order_and_cuts():
    hs = [house(1, score=95, equity=10), house(2, score=95, equity=80), house(3, score=90),
          house(4, score=99, corp=True), house(5, score=98, sold="2024-03-01"), house(6, score=97, zip5="37111"),
          house(7, score=96), house(8, score=20), house(9, score=60, value=900000)]
    fake = FakeMap(hs)
    res = P.build(search(fake), [COUNTY], [("investor", 4)], exclude_zips={"37111"}, skip={"7"}, out=QUIET)
    got = [x["dataflik_id"] for x in res["pools"]["investor"]]
    # 99 is a company, 98 sold since 2023, 97 an excluded ZIP, 96 on the exclude list; inside 95 more equity first
    assert got == [2, 1, 3, 8]
    assert [x["rank"] for x in res["pools"]["investor"]] == [1, 2, 3, 4]
    d = res["summary"]["dropped"]
    assert d["company owner"] == 1 and d["sold since 2023-01-01"] == 1 and d["excluded ZIP"] == 1
    assert d["on the exclude list"] == 1
    assert res["pools"]["investor"][0]["investor_off_market_score"] == 95


def test_two_pools_never_overlap_and_keep_their_sizes():
    hs = [house(i, score=100 - i, realtor=60 + i) for i in range(1, 21)]
    res = P.build(search(FakeMap(hs)), [COUNTY], [("investor", 8), ("realtor", 5)], margin=2, out=QUIET)
    inv = {x["dataflik_id"] for x in res["pools"]["investor"]}
    rea = {x["dataflik_id"] for x in res["pools"]["realtor"]}
    assert len(inv) == 8 and len(rea) == 5 and not inv & rea
    # the investor pool ranks houses 1..8 highest; the realtor pool ranks 20 down, so it never needs them
    assert inv == set(range(1, 9)) and rea == set(range(16, 21))


def test_disjoint_only_counts_what_each_pool_keeps():
    a = [{"dataflik_id": i} for i in [1, 2, 3, 4, 5, 6]]
    b = [{"dataflik_id": i} for i in [9, 8, 6, 7, 5]]
    fa, fb, moved = P.disjoint(a, b, 4, 2)
    # b keeps only 9 and 8, so 5 and 6 never collide and stay out of the way
    assert [x["dataflik_id"] for x in fa] == [1, 2, 3, 4] and [x["dataflik_id"] for x in fb] == [9, 8]
    fa, fb, moved = P.disjoint(a, b, 6, 3)
    assert not {x["dataflik_id"] for x in fa} & {x["dataflik_id"] for x in fb}
    assert 6 in {x["dataflik_id"] for x in fb}     # b ranks 6 third, a ranks it sixth


def test_big_results_are_cut_by_value(monkeypatch):
    monkeypatch.setattr(P, "SPLIT_AT", 3)
    hs = [house(i, score=80, value=100000 + i * 1000) for i in range(1, 11)]
    fake = FakeMap(hs)
    got = P.members(search(fake), COUNTY, {"type_single_family": True}, 1, 700000)
    assert sorted(int(k) for k in got) == list(range(1, 11))
    assert len(fake.bodies) > 3      # it split


def test_ladder_rungs_preset_skip_and_fill():
    hs = [house(1, score=40, dist=["tax_delinquent"]), house(2, score=45, dist=["tax_delinquent", "vacant"]),
          house(3, score=99, absentee=True), house(4, score=70), house(5, score=30, dist=["vacant"])]
    shard = {"47": {"47999": {"variants": {"lists": [
        {"label": "Tax delinquent + vacant", "keys": ["tax_delinquent", "vacant"], "priority": 1, "marg_dpd": 40, "list_size": 9},
        {"label": "Negative equity", "keys": ["preset_negative_equity"], "priority": 2, "marg_dpd": 50, "list_size": 9},
        {"label": "Absentee", "keys": ["is_absentee_owners"], "priority": 2, "marg_dpd": 60, "list_size": 9},
        {"label": "Tax delinquent", "keys": ["tax_delinquent"], "priority": 3, "marg_dpd": 80, "list_size": 9}]}}}}
    D._SHARDS.clear()
    res = P.build(search(FakeMap(hs)), [COUNTY], [("investor", 5)], rank="ladder", since="",
                  shard_fetch=lambda url: shard["47"], out=QUIET)
    rows = res["pools"]["investor"]
    assert [x["dataflik_id"] for x in rows[:3]] == [2, 3, 1]
    assert rows[0]["rung"] == "Tax delinquent + vacant" and rows[0]["priority"] == 1
    assert rows[3]["rung"].startswith("Fill: investor AI") and [x["dataflik_id"] for x in rows[3:]] == [4, 5]
    skipped = [r for r in res["summary"]["investor_rungs"] if r.get("skipped")]
    assert skipped and skipped[0]["label"] == "Negative equity"
    D._SHARDS.clear()


def test_no_ai_add_on_is_said_and_refused():
    fake = FakeMap([house(1, score=90, realtor=90), house(2, score=80, realtor=80)], scores_visible=False)
    with pytest.raises(SystemExit) as e:
        P.build(search(fake), [COUNTY], [("realtor", 1)], out=QUIET)
    assert "sees no realtor AI scores" in str(e.value)


def test_inputs():
    assert P.parse_pools(["investor:30000", "realtor:20"]) == [("investor", 30000), ("realtor", 20)]
    for bad in (["investor:0"], ["seller:5"], ["investor:5", "investor:6"]):
        with pytest.raises(SystemExit):
            P.parse_pools(bad)
    assert P.parse_value("$100,000-$700,000") == (100000, 700000)
    with pytest.raises(SystemExit):
        P.parse_value("700000-100000")
    assert P.resolve_counties(["Knox, TN", "47093"]) == [{"fips": "47093", "name": "Knox", "state": "TN"}]
    with pytest.raises(SystemExit):
        P.resolve_counties(["Nowhere, ZZ"])


def test_row_zip():
    assert P.row_zip({"address": "1 Example St, Testville, TN 37000"}) == "37000"
    assert P.row_zip({"address": "1 Example St, Testville, TN 37000-1234"}) == "37000"
    assert P.row_zip({"address": ""}) == ""


def test_never_adds_records():
    src = (ROOT / "src" / "ai_pools.py").read_text(encoding="utf-8")
    assert "add-properties" not in src and "opensearch" not in src.lower()
