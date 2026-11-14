"""market_analyzer: today's weights are the default and score exactly as before; the taught
weights are there by name, rank more investor sales higher, and carry the guards.

Zero network. Invented ZIP codes and counts.

Run: python tests/test_market_analyzer_weights.py   (or pytest)
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import market_analyzer as ma  # noqa: E402


def zp(z, **kw):
    base = dict(zip_code=z, county="Alder", total_notices=10, median_value=200000.0, avg_equity_pct=40.0,
                avg_tax_delinquent_amount=900.0, avg_dom=30.0)
    base.update(kw)
    return ma.ZipProfile(**base)


def profiles():
    return {
        "00101": zp("00101", total_notices=30, median_value=140000.0, avg_equity_pct=55.0, competition_ratio=0.30,
                    investor_purchase_count=66, avg_dom=20.0),
        "00102": zp("00102", total_notices=12, median_value=260000.0, avg_equity_pct=35.0, competition_ratio=0.05,
                    investor_purchase_count=4, avg_dom=60.0),
        "00103": zp("00103", total_notices=22, median_value=310000.0, avg_equity_pct=48.0, competition_ratio=0.18,
                    investor_purchase_count=31, avg_tax_delinquent_amount=2400.0),
        "00104": zp("00104", total_notices=3, median_value=520000.0, avg_equity_pct=62.0, competition_ratio=0.0,
                    investor_purchase_count=0, avg_dom=75.0),
    }


def order(zips):
    return [z.zip_code for z in zips if not z.left_out]


def test_the_default_is_todays_scoring_to_the_last_digit():
    assert ma.DEFAULT_WEIGHTS == "today" and ma.weights_name() == "today"
    old = ma._score_today(profiles())
    now = ma.score_zip_codes(profiles())
    assert [(z.zip_code, z.score, z.rank, z.grade) for z in now] == [(z.zip_code, z.score, z.rank, z.grade) for z in old]
    # the six numbers the skill quotes are still the six numbers the default scores with
    assert (ma.WEIGHT_DISTRESS_DENSITY, ma.WEIGHT_MEDIAN_VALUE, ma.WEIGHT_EQUITY_AVG, ma.WEIGHT_TAX_DELINQUENCY,
            ma.WEIGHT_COMPETITION, ma.WEIGHT_DOM_AVG) == (0.30, 0.20, 0.15, 0.15, 0.10, 0.10)
    # and the table that names them restates them exactly: scored through the table, same result
    via_table = ma._score_named(profiles(), "today", False, ma.VALUE_BAND)
    assert [(z.zip_code, z.rank, z.grade) for z in via_table] == [(z.zip_code, z.rank, z.grade) for z in old]
    assert all(abs(a.score - b.score) < 1e-9 for a, b in zip(via_table, old))
    assert abs(sum(w for _l, w, _a, _h in ma.WEIGHT_SETS["today"]["factors"]) - 1.0) < 1e-9
    assert abs(sum(w for _l, w, _a, _h in ma.WEIGHT_SETS["taught"]["factors"]) - 1.0) < 1e-9
    # no guard runs by default: nobody is flagged or left out
    assert all(not z.flags and not z.left_out for z in now)


def test_today_ranks_more_investor_activity_lower_and_the_taught_set_ranks_it_higher():
    def pair():
        return {"00101": zp("00101", competition_ratio=0.40, investor_purchase_count=60),
                "00102": zp("00102", competition_ratio=0.05, investor_purchase_count=6)}
    assert order(ma.score_zip_codes(pair())) == ["00102", "00101"]                       # today: the quiet ZIP wins
    assert order(ma.score_zip_codes(pair(), weights="taught")) == ["00101", "00102"]     # taught: the busy ZIP wins
    busy = ma.score_zip_codes(pair(), weights="taught")[0]
    assert busy.score - ma.score_zip_codes(pair(), weights="taught")[1].score == 50.0     # the investor factor is half the score


def test_the_taught_set_is_chosen_by_name_only():
    os.environ[ma.WEIGHTS_ENV] = "taught"
    try:
        assert ma.weights_name() == "taught"
        assert order(ma.score_zip_codes(profiles()))[0] == "00101"
    finally:
        os.environ.pop(ma.WEIGHTS_ENV, None)
    assert ma.weights_name() == "today"
    assert ma.weights_name("Taught ") == "taught"
    try:
        ma.score_zip_codes(profiles(), weights="doors")
        raise AssertionError("an unknown weight set was accepted")
    except ValueError as e:
        assert "today" in str(e) and "taught" in str(e)


def test_an_implausible_median_sale_price_marks_a_tax_sale_area_and_leaves_it_out():
    p = profiles()
    p["00105"] = zp("00105", investor_purchase_count=111, median_sale_price=1000.0, market_median_value=140000.0)
    scored = ma.score_zip_codes(p, weights="taught")
    out = [z for z in scored if z.left_out]
    assert [z.zip_code for z in out] == ["00105"] and scored[-1].zip_code == "00105"
    why = out[0].left_out
    assert "median sale price $1,000" in why and "$140,000 median value" in why and "111 investor sales" in why
    assert "tax sale or auction area" in why
    assert out[0].rank == 0 and out[0].grade == "" and out[0].score == 0.0
    assert "00105" not in order(scored) and len(order(scored)) == 4
    # the most investor sales in the set did not put it on top: the count is not taken at face value
    assert order(scored)[0] == "00101"
    # a median under a fifth of the median value is the same story at any price
    left, _ = ma.zip_guards(zp("00106", investor_purchase_count=40, median_sale_price=30000.0, market_median_value=210000.0))
    assert "tax sale or auction" in left
    # a normal median is not touched
    assert ma.zip_guards(zp("00106", investor_purchase_count=40, median_sale_price=190000.0, market_median_value=210000.0)) == ("", [])
    # with the guards switched off the same ZIP is ranked like any other
    off = ma.score_zip_codes(copy.deepcopy(p), weights="taught", guards=False)
    assert "00105" in order(off) and len(order(off)) == 5 and not any(z.left_out or z.flags for z in off)


def test_a_median_outside_the_value_band_ranks_under_the_clean_zips():
    p = {"00101": zp("00101", investor_purchase_count=20, market_median_value=250000.0),
         "00102": zp("00102", investor_purchase_count=90, market_median_value=82000.0),
         "00103": zp("00103", investor_purchase_count=10, market_median_value=300000.0, median_sale_price=760000.0)}
    scored = ma.score_zip_codes(p, weights="taught")
    assert order(scored) == ["00101", "00102", "00103"]            # the busiest ZIP is under the band, so it is not first
    by = {z.zip_code: z for z in scored}
    assert by["00101"].flags == []
    assert by["00102"].flags == ["median value $82,000 is outside the $100,000 to $700,000 band"]
    assert by["00103"].flags == ["median sale price $760,000 is outside the $100,000 to $700,000 band"]
    assert by["00102"].score > by["00101"].score                   # a flag orders it, the score is still its own
    # another market sets its own band
    wide = ma.score_zip_codes(copy.deepcopy(p), weights="taught", band=(50000, 900000))
    assert order(wide)[0] == "00102" and not any(z.flags for z in wide)


def test_a_zip_with_no_tracked_investor_sales_ranks_low_only_when_sales_were_loaded():
    p = profiles()
    scored = ma.score_zip_codes(p, weights="taught")
    assert order(scored)[-1] == "00104" and "no tracked investor sales" in scored[len(order(scored)) - 1].flags
    # no investor numbers at all is missing data, not four dead ZIPs: nobody is flagged for it
    none = {k: zp(k, total_notices=n) for k, n in (("00101", 5), ("00102", 9))}
    blind = ma.score_zip_codes(none, weights="taught")
    assert all(not z.flags for z in blind) and order(blind) == ["00102", "00101"]


def test_demand_is_months_of_inventory():
    assert ma.months_of_inventory(158, 54) == 2.9 and ma.demand_label(2.9) == "strong"
    assert ma.demand_label(3.5) == "strong" and ma.demand_label(6.0) == "6 months" and ma.demand_label(None) == ""
    for on, sold in ((40, 0), (40, None), (None, 12), ("x", 3), (-1, 3)):
        assert ma.months_of_inventory(on, sold) is None             # no pace to divide by is unknown, never zero
    p = {"00101": zp("00101", investor_purchase_count=20, homes_on_market=30, homes_sold_last_month=15),
         "00102": zp("00102", investor_purchase_count=20, homes_on_market=90, homes_sold_last_month=10),
         "00103": zp("00103", investor_purchase_count=20)}
    scored = ma.score_zip_codes(p, weights="taught")
    by = {z.zip_code: z for z in scored}
    assert (by["00101"].months_inventory, by["00102"].months_inventory, by["00103"].months_inventory) == (2.0, 9.0, None)
    # fewer months is stronger demand; a ZIP nobody has a number for scores in the middle
    assert order(scored) == ["00101", "00103", "00102"]
    assert round(by["00101"].score - by["00103"].score, 6) == 5.0 == round(by["00103"].score - by["00102"].score, 6)


def test_the_guards_can_run_under_todays_weights_too():
    p = profiles()
    p["00105"] = zp("00105", investor_purchase_count=111, median_sale_price=1000.0, market_median_value=140000.0)
    scored = ma.score_zip_codes(p, guards=True)
    assert [z.zip_code for z in scored if z.left_out] == ["00105"]
    clean = [z for z in scored if not z.left_out and not z.flags]
    old = [z.zip_code for z in ma._score_today(profiles()) if z.zip_code != "00104"]
    assert [z.zip_code for z in clean] == old                       # same weights, so the clean ZIPs keep today's order


def test_market_finder_numbers_load_from_the_extract_file():
    rows = {"success": True, "state": "Tennessee", "county": "Alder", "zip_data": [
        {"zip_code": "00101", "total_inv_trans_6mo": 66, "homes_on_market": 158, "homes_sold_last_month": 54,
         "median_days_on_market": 21, "median_home_value": 204000.0, "median_sale_price": 199000.0},
        {"zip_code": "00109", "total_inv_trans_6mo": 12, "homes_on_market": 20, "homes_sold_last_month": 0,
         "median_days_on_market": None, "median_home_value": None, "median_sale_price": None},
        {"zip_code": "bad", "total_inv_trans_6mo": 5}]}
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "mf.json"
        f.write_text(json.dumps(rows), encoding="utf-8")
        got = ma.load_market_finder(str(f))
        both = ma.load_market_finder("%s, %s" % (f, f))
    assert sorted(got) == ["00101", "00109"] and got["00101"]["county"] == "Alder" and sorted(both) == sorted(got)
    p = profiles()
    before = (p["00101"].median_value, p["00101"].avg_dom)
    assert ma.apply_market_finder(p, got, add_missing=False) == 1 and "00109" not in p
    z = p["00101"]
    assert (z.investor_purchase_count, z.median_sale_price, z.market_median_value, z.months_inventory) == (66, 199000.0, 204000.0, 2.9)
    assert (z.median_value, z.avg_dom) == before                    # the fields today's weights read are not replaced
    assert ma.apply_market_finder(p, got) == 2 and p["00109"].months_inventory is None and p["00109"].county == "Alder"


def test_the_before_and_after_picture_does_not_touch_what_it_is_given():
    p = profiles()
    p["00105"] = zp("00105", investor_purchase_count=111, median_sale_price=1000.0, market_median_value=140000.0)
    snapshot = copy.deepcopy(p)
    got = ma.compare_weight_sets(p, top=3)
    assert p == snapshot
    assert len(got["today"]) == 3 and len(got["taught"]) == 3
    assert got["taught"][0]["zip"] == "00101" and got["taught"][0]["investor_sales_6mo"] == 66
    assert [r["zip"] for r in got["left_out"]] == ["00105"]
    assert {r["zip"] for r in got["today"]} != {r["zip"] for r in got["taught"]} or got["today"] != got["taught"]


def test_the_report_says_which_weights_it_used(tmp=None):
    from openpyxl import load_workbook
    real = ma.config.OUTPUT_DIR
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "notices.csv").write_text(
            "zip,county,notice_type,estimated_value,equity_percent\n"
            "00101,Alder,foreclosure,150000,40\n00101,Alder,probate,170000,60\n00102,Alder,tax_sale,260000,30\n",
            encoding="utf-8")
        mf = out / "mf.json"
        mf.write_text(json.dumps({"county": "Alder", "zip_data": [
            {"zip_code": "00101", "total_inv_trans_6mo": 40, "homes_on_market": 30, "homes_sold_last_month": 15,
             "median_home_value": 160000.0, "median_sale_price": 150000.0},
            {"zip_code": "00102", "total_inv_trans_6mo": 111, "homes_on_market": 10, "homes_sold_last_month": 5,
             "median_home_value": 140000.0, "median_sale_price": 1000.0}]}), encoding="utf-8")
        ma.config.OUTPUT_DIR = out
        try:
            a = ma.run_market_analysis(counties=["Alder"], output_path=str(out / "today.xlsx"))
            b = ma.run_market_analysis(counties=["Alder"], output_path=str(out / "taught.xlsx"), weights="taught",
                                       market_finder=str(mf))
            c = ma.run_market_analysis(counties=["Alder"], output_path=str(out / "blind.xlsx"), weights="taught")
        finally:
            ma.config.OUTPUT_DIR = real
        wa, wb = load_workbook(a["report_path"]), load_workbook(b["report_path"])
        assert wa.sheetnames == ["Executive Summary", "Zip Scorecard", "Distress Density", "Competition Map", "Recommendations"]
        assert wb.sheetnames == ["Executive Summary", "Zip Scorecard", "Distress Density", "Investor Activity",
                                 "Recommendations", "Left Out"]
        assert a["report"].weights == "today" and a["report"].left_out == [] and wa["Executive Summary"]["A4"].value is None
        assert wb["Executive Summary"]["A4"].value.startswith("Weight set: taught. investor activity 50%")
        assert [z.zip_code for z in b["report"].left_out] == ["00102"] and [z.zip_code for z in b["report"].zip_profiles] == ["00101"]
        assert wb["Left Out"]["A5"].value == "00102" and "tax sale or auction" in wb["Left Out"]["E5"].value
        assert "More investor sales" in wb["Investor Activity"]["A2"].value
        assert "Lower competition ratio" in wa["Competition Map"]["A2"].value
        # a taught ranking with no investor sales loaded says so instead of printing a tidy top ten
        assert c["report"].notes and "No investor sales were loaded" in c["report"].notes[0]
        for book in (wa, wb):
            for ws in book.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for v in row:
                        assert not (isinstance(v, str) and (chr(8212) in v or chr(8211) in v)), v


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/market_analyzer.py", "tests/test_market_analyzer_weights.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text, name


def main() -> int:
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
