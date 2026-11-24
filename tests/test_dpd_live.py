"""Live Doors per Deal (src/dpd_live.py): the rung filters, live doors and the AI fallback, the market
speed from sold-property data, the monthly member snapshot, deals measured forward against it, and the
blend with the published ladder. A fake SiftMap answers every search from an invented county of 200
houses. Zero network.

Run: python -m pytest tests/test_dpd_live.py
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import dpd_live as D  # noqa: E402


# ---------------------------------------------------------------------------------- a fake county
class FakeMap:
    """SiftMap's search over an invented county: the filters the ladder uses, the sold-property window,
    paging by row offset. A filter key it does not know is ignored, as SiftMap does."""

    def __init__(self, houses, honor_ai_max=True):
        self.houses, self.honor_ai_max, self.bodies = houses, honor_ai_max, []

    def match(self, h, f):
        for k, v in f.items():
            if k == "distressors_include":
                if not set(v.split(",")) <= h["keys"]:
                    return False
            elif k == "preset_absentee_owners":
                if "is_absentee_owners" not in h["keys"]:
                    return False
            elif k.startswith("preset_"):
                if k not in h["keys"]:
                    return False
            elif k == "investor_off_market_score_min":
                if h.get("ai") is None or h["ai"] < v:
                    return False
            elif k == "investor_off_market_score_max" and self.honor_ai_max:
                if h.get("ai") is None or h["ai"] > v:
                    return False
            elif k == D.K_TYPE:
                if not h.get("sold"):
                    return False
            elif k == "extra_last_sale_date_min":
                if not h.get("sold") or h["sold"] < v[:7]:
                    return False
            elif k == "extra_last_sale_date_max":
                if not h.get("sold") or h["sold"] > v[:7]:
                    return False
        return True

    def post(self, b):
        self.bodies.append(b)
        hits = [h for h in self.houses if self.match(h, b["filters"])]
        off, size = b["result_index"] - 1, b.get("size") or 25
        return {"total_results": len(hits),
                "data": [{"id": h["id"], "saved_uuid": h.get("saved_uuid")} for h in hits[off:off + size]]}


def houses():
    out = []
    for i in range(1, 201):
        keys = set()
        if 1 <= i <= 10:
            keys.add("is_notice_of_default")
        if 5 <= i <= 44:
            keys.add("is_recent_deliquent")
        if 50 <= i <= 149:
            keys.add("is_absentee_owners")
        ai = 95 if 150 <= i <= 169 else (84 if 170 <= i <= 179 else 30)
        out.append({"id": "h%d" % i, "keys": keys, "ai": ai, "sold": None, "saved_uuid": ("rec-%d" % i) if i % 2 else None})
    # Ten purchases in the published ladder's own months (January to June 2026), and four lately.
    for i, m in zip(range(181, 191), ["2026-01", "2026-02", "2026-02", "2026-03", "2026-03", "2026-04", "2026-04", "2026-05",
                                      "2026-05", "2026-06"]):
        out[i - 1]["sold"] = m
    for i, m in zip(range(191, 201), ["2026-05", "2026-06", "2026-07", "2026-07", "2026-08", "2026-08", "2026-09", "2026-09",
                                      "2026-10", "2026-10"]):
        out[i - 1]["sold"] = m
    return out


CAP = {"fips": "47093", "name": "Knox", "st": "TN", "deals": 20, "base_supply": 200, "ai_provisioned": True,
       "window": "2026-01 .. 2026-06 (6 complete months)", "built": "2026-08-13",
       "variants": {"ai": [
           {"label": "Notice of Default", "keys": ["is_notice_of_default"], "ai": None, "priority": 1, "list_size": 10,
            "marg_deals": 2, "marg_supply": 10, "marg_dpd": 5.0, "cum_dpd": 5.0},
           {"label": "AI score 90+", "keys": [], "ai": 90, "priority": 1, "list_size": 20,
            "marg_deals": 2, "marg_supply": 20, "marg_dpd": 10.0, "cum_dpd": 7.5},
           {"label": "AI score 80-89", "keys": [], "ai": 80, "priority": 2, "list_size": 12,
            "marg_deals": 1, "marg_supply": 12, "marg_dpd": 12.0, "cum_dpd": 8.4},
           {"label": "Tax Delinquent", "keys": ["is_recent_deliquent"], "ai": None, "priority": 2, "list_size": 20,
            "marg_deals": 2, "marg_supply": 15, "marg_dpd": 7.5, "cum_dpd": 8.1},
           {"label": "Absentee", "keys": ["is_absentee_owners"], "ai": None, "priority": 3, "list_size": 100,
            "marg_deals": 3, "marg_supply": 100, "marg_dpd": 33.3, "cum_dpd": 15.6}]}}
CTY = {"name": "Knox", "state": "TN", "window": "2025-12 .. 2026-05 (6 complete months)",
       "rows": [{"seg": "Tax Delinquent", "keys": ["is_recent_deliquent"], "deals": 4, "list_size": 40}],
       "ai_demand": {"cutoffs": []}, "baseline": {"deals": 20, "list_size": 200}}


def fetch(url):
    return {"47093": dict(CAP)} if "capture-data" in url else {"47093": dict(CTY)}


@pytest.fixture(autouse=True)
def fresh():
    D._SHARDS.clear()
    yield
    D._SHARDS.clear()


def search_for(hs, **kw):
    fake = FakeMap(hs, **kw)
    return D.Search(post=fake.post), fake


# ---------------------------------------------------------------------------------- the pieces
def test_rung_filters_match_the_published_builder():
    f = D.rung_filters(["is_free_and_clear", "is_absentee_owners", "is_vacant_property"])
    assert f == {"preset_absentee_owners": True, "distressors_include": "is_free_and_clear,is_vacant_property",
                 "distressors_include_min_match": 2}
    assert D.rung_filters(["preset_negative_equity"]) == {"preset_negative_equity": True}
    assert D.rung_filters([], 80, (70, 80, 90, 95)) == {"investor_off_market_score_min": 80, "investor_off_market_score_max": 89}
    assert D.rung_filters([], 95, (70, 80, 90, 95)) == {"investor_off_market_score_min": 95}


def test_months():
    assert D.recorded_months(date(2026, 11, 24), 6, 1) == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert D.window_months("2026-01 .. 2026-06 (6 complete months)") == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert D.window_months("") == []
    assert D.shift("2026-12", 1) == "2027-01" and D.shift("2027-01", -1) == "2026-12"
    assert D.month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


def test_rows_page_by_offset(monkeypatch):
    monkeypatch.setattr(D, "PAGE", 7)
    s, fake = search_for(houses())
    rows, total = s.rows({"fips": "47093", "name": "Knox", "state": "TN"}, {"distressors_include": "is_recent_deliquent",
                                                                            "distressors_include_min_match": 1})
    assert total == 40 and len(rows) == 40 and len({r["id"] for r in rows}) == 40
    assert [b["result_index"] for b in fake.bodies] == [1, 8, 15, 22, 29, 36]
    assert all(b["addresses"][0]["search"] == "Knox" for b in fake.bodies), "the bare county name, never 'Knox County, TN'"


def test_the_search_cap_stops_a_run():
    s = D.Search(post=FakeMap(houses()).post, cap=2)
    c = {"fips": "47093", "name": "Knox", "state": "TN"}
    s.count(c, {})
    s.count(c, {})
    with pytest.raises(D.SearchError):
        s.count(c, {})


# ---------------------------------------------------------------------------------- one county
def test_the_live_ladder(tmp_path):
    s, _ = search_for(houses())
    res = D.run_county(s, "47093", root=tmp_path, today=date(2026, 11, 24), fetch=fetch)
    by = {r["label"]: r for r in res["rungs"]}
    assert res["base"]["live"] == 200 and res["ai_live"] is True
    assert by["Notice of Default"]["doors"] == 10 and by["Tax Delinquent"]["doors"] == 40
    assert by["AI score 90+"]["doors"] == 20 and by["AI score 80-89"]["doors"] == 10
    # Tax Delinquent doubled since the published ladder (20 to 40): it is named. The AI band moved 17%: it is not.
    assert {m["label"] for m in res["moved"]} == {"Tax Delinquent"}
    sp = res["speed"]
    # Published months (Jan to Jun) by the live query: 10 purchases + 2 lately (May, Jun) = 12 over 6 months.
    assert sum(sp["window"].values()) == 12 and sp["then_per_month"] == 2.0
    # Recorded months on 2026-11-24, lag 1: April to September = 1+1+... from the fake.
    assert list(sp["recent"]) == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert sp["unit_factor"] == round(20 / 12.0, 4)
    nod = by["Notice of Default"]
    assert nod["dpd_source"] == "published ladder at today's speed"
    assert nod["dpd"] == round(5.0 / sp["ratio"], 1)
    assert by["Tax Delinquent"]["dpd_alone"] == round(10.0 / sp["ratio"], 1), "the county table's own row, at today's speed"
    assert (tmp_path / "47093" / "ladder.json").exists() and (tmp_path / "47093" / "ladder.csv").exists()
    lines = D.summary_lines(res)
    assert any("best lists now" in l for l in lines) and any("moved since the published ladder" in l for l in lines)
    assert all(chr(0x2014) not in l and chr(0x2013) not in l for l in lines)


def test_an_account_without_the_ai_add_on_borrows_the_published_ai_rungs(tmp_path):
    hs = houses()
    for h in hs:
        h["ai"] = None
    s, _ = search_for(hs)
    res = D.run_county(s, "47093", root=tmp_path, today=date(2026, 11, 24), fetch=fetch)
    by = {r["label"]: r for r in res["rungs"]}
    assert res["ai_live"] is False and any("no off-market AI add-on" in n for n in res["notes"])
    assert by["AI score 90+"]["doors"] == 20 and by["AI score 90+"]["doors_source"].startswith("published size")


def test_an_ignored_ai_upper_bound_is_counted_as_two_cutoffs(tmp_path):
    s, _ = search_for(houses(), honor_ai_max=False)
    res = D.run_county(s, "47093", root=tmp_path, today=date(2026, 11, 24), fetch=fetch, phase="snapshot")
    by = {r["label"]: r for r in res["rungs"]}
    assert by["AI score 80-89"]["doors"] == 10 and by["AI score 80-89"]["doors_source"] == "live (two cutoffs)"
    assert any("upper bound" in n for n in res["notes"])
    assert "AI score 80-89" in res["snapshot"]["skipped"], "a band that cannot be filtered is not saved"


def test_deals_are_measured_forward_against_the_snapshot_before_the_sale(tmp_path):
    hs = houses()
    s, _ = search_for(hs)
    first = D.run_county(s, "47093", root=tmp_path, today=date(2026, 11, 24), fetch=fetch, phase="snapshot")
    assert first["snapshot"]["date"] == "2026-11-24" and first["snapshot"]["rungs"] == 5
    # December: one house on Notice of Default AND Tax Delinquent sells (it counts once, on the first
    # rung on the margin), one absentee house sells, and a house on no list sells.
    hs[5]["sold"] = "2026-12"      # h6: Notice of Default + Tax Delinquent
    hs[69]["sold"] = "2026-12"     # h70: absentee
    hs[0]["keys"].add("is_vacant_property")
    D._SHARDS.clear()
    s2, _ = search_for(hs)
    res = D.run_county(s2, "47093", root=tmp_path, today=date(2027, 2, 5), fetch=fetch, phase="all")
    m = res["measured"]
    assert m["Notice of Default"]["deals"] == 1 and m["Notice of Default"]["marg_deals"] == 1
    assert m["Tax Delinquent"]["deals"] == 1 and m["Tax Delinquent"]["marg_deals"] == 0, "on the margin it belongs to the rung above"
    assert m["Absentee"]["marg_deals"] == 1
    assert m["Notice of Default"]["from"] == "2026-12" and m["Notice of Default"]["months"] == 1
    by = {r["label"]: r for r in res["rungs"]}
    assert by["Notice of Default"]["dpd_source"].startswith("published ladder, moving to measured (1 sale)")
    assert res["measured_rungs"] == 3


def test_a_sale_before_the_first_snapshot_is_never_measured(tmp_path):
    hs = houses()
    hs[5]["sold"] = "2026-11"      # sold the month the members were first saved
    s, _ = search_for(hs)
    D.run_county(s, "47093", root=tmp_path, today=date(2026, 11, 24), fetch=fetch, phase="snapshot")
    D._SHARDS.clear()
    s2, _ = search_for(hs)
    res = D.run_county(s2, "47093", root=tmp_path, today=date(2027, 1, 5), fetch=fetch, phase="all")
    assert not (res["measured"].get("Notice of Default") or {}).get("deals"), \
        "a list that cannot show it held the house before its sale month gets no credit for it"


def test_blend_moves_from_the_ladder_to_the_measured_rate():
    assert D.blend(0.1, None, 0) == (0.1, "published ladder at today's speed")
    r, word = D.blend(0.1, 0.3, 8)
    assert abs(r - 0.2) < 1e-9 and word == "measured (8 sales)"
    r, word = D.blend(0.1, 0.3, 2)
    assert abs(r - (0.2 * 0.3 + 0.8 * 0.1)) < 1e-9 and word.startswith("published ladder, moving to measured")
    assert D.blend(None, 0.3, 4) == (0.3, "measured (4 sales)")


def test_marginal_sets_and_their_exactness():
    snap = {"rungs": {"A": ["1", "2"], "C": ["2", "3"]}}
    marg, exact = D.marginal_sets(snap, ["A", "B", "C"])
    assert marg == {"A": {"1", "2"}, "C": {"3"}} and exact == {"A": True, "C": False}


def test_no_long_dash_in_the_module():
    text = (ROOT / "src" / "dpd_live.py").read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
