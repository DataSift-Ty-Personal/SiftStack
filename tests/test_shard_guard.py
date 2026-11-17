"""shard_guard: a month the doors-per-deal data cannot stand behind is dropped, named and explained,
and nothing is ever filled in. Zero network: invented counties with invented counts.

Run: python tests/test_shard_guard.py   (or pytest)
"""
import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import shard_guard as sg  # noqa: E402

WINDOW = "2026-01 .. 2026-06 (6 complete months)"


def county(trend, name="Alder", window=WINDOW, rows=None):
    deals = sum(trend)
    return {"name": name, "state": "ZZ", "window": window, "deals": deals, "trend": list(trend),
            "baseline": {"deals": deals, "list_size": deals * 150, "dpd": 150.0},
            "rows": rows if rows is not None else [
                {"seg": "Vacant", "keys": ["is_vacant_property"], "deals": 40, "list_size": 2000, "dpd": 50.0,
                 "lift": 3.0, "priority": 1},
                {"seg": "Tired Landlord", "keys": ["is_tired_landlord"], "deals": 9, "list_size": 900, "dpd": 100.0,
                 "lift": 1.5, "priority": 2}]}


def test_a_month_reading_zero_in_a_busy_county_is_left_out_and_said_so():
    c = sg.guard_county(county([150, 160, 155, 170, 148, 0]))
    g = c["guard"]
    assert g["line"] == "5 complete months, June left out: it reads zero", g["line"]
    assert g["left_out"] == ["June"] and g["months_used"] == 5 and g["months_claimed"] == 6 and g["usable"]
    assert c["window_used"] == "2026-01 .. 2026-06 (5 complete months, June left out: it reads zero)"
    assert [m["kept"] for m in g["months"]] == [True] * 5 + [False]


def test_the_published_figures_stay_and_the_six_month_rate_sits_beside_them():
    before = county([150, 160, 155, 170, 148, 0])
    c = sg.guard_county(before)
    assert (c["deals"], c["baseline"]["dpd"], c["window"]) == (783, 150.0, WINDOW)      # nothing published was changed
    assert c["guard"]["six_month_factor"] == 1.2 and c["guard"]["restated"] is False
    assert c["deals_6mo"] == 940 and c["baseline"]["dpd_6mo"] == 125.0                    # 783 x 6/5, 150 x 5/6
    vacant, landlord = c["rows"]
    assert (vacant["dpd"], vacant["dpd_6mo"], vacant["deals"], vacant["deals_6mo"]) == (50.0, 41.7, 40, 48)
    assert (landlord["dpd_6mo"], landlord["deals_6mo"]) == (83.3, 11)                    # 9 x 6/5 = 10.8, a whole number from 10 up
    small = sg.guard_county(county([150, 160, 155, 170, 148, 0], rows=[{"seg": "Probate", "deals": 4, "dpd": 30.0}]))
    assert small["rows"][0]["deals_6mo"] == 4.8                                          # under 10 keeps its decimal
    # the ranking does not move: every list is scaled by the same factor
    assert [(r["lift"], r["priority"]) for r in c["rows"]] == [(3.0, 1), (1.5, 2)]
    assert round(c["baseline"]["dpd_6mo"] / vacant["dpd_6mo"], 1) == vacant["lift"]


def test_nothing_is_filled_in_and_the_input_is_not_touched():
    before = county([150, 160, 155, 170, 148, 0])
    snapshot = copy.deepcopy(before)
    c = sg.guard_county(before)
    assert before == snapshot                                                             # the caller's dict is as it was
    assert c["trend"] == [150, 160, 155, 170, 148, 0]                                     # June is still zero, not a guess
    assert c["guard"]["months"][5]["deals"] == 0 and c["guard"]["deals_used"] == 783


def test_the_named_switch_moves_the_six_month_figures_in_and_keeps_the_published_ones():
    c = sg.guard_county(county([150, 160, 155, 170, 148, 0]), restate=True)
    assert c["guard"]["restated"] is True
    assert (c["deals"], c["deals_published"]) == (940, 783)
    assert (c["baseline"]["dpd"], c["baseline"]["dpd_published"]) == (125.0, 150.0)
    assert (c["rows"][0]["dpd"], c["rows"][0]["dpd_published"]) == (41.7, 50.0)
    assert c["window"].endswith("(5 complete months, June left out: it reads zero)") and c["window_published"] == WINDOW
    os.environ[sg.RESTATE_ENV] = "1"
    try:
        assert sg.guard_county(county([150, 160, 155, 170, 148, 0]))["guard"]["restated"] is True
    finally:
        os.environ.pop(sg.RESTATE_ENV, None)
    assert sg.guard_county(county([150, 160, 155, 170, 148, 0]))["guard"]["restated"] is False     # off unless asked
    # with nothing to drop, the switch changes nothing at all
    clean = sg.guard_county(county([150, 160, 155, 170, 148, 152]), restate=True)
    assert clean["guard"]["restated"] is False and "dpd_published" not in clean["baseline"] and clean["window"] == WINDOW


def test_a_month_far_below_the_months_around_it_is_left_out_with_both_numbers():
    c = sg.guard_county(county([150, 160, 155, 170, 148, 20]))
    g = c["guard"]
    assert g["line"] == "5 complete months, June left out: it reads 20, far below the 155 a month around it", g["line"]
    assert g["deals_counted"] == 803 and g["deals_used"] == 783
    assert c["deals_6mo"] == 940                       # the kept months' pace times six, not the partial month scaled up
    assert c["deals"] == 803                           # and the published count still includes the partial month


def test_a_quiet_month_that_chance_explains_is_kept():
    # 3 against a median of 10 is under 35 percent, but it happens about one month in a hundred
    assert sg.guard_county(county([10, 12, 9, 11, 3, 10]))["guard"]["left_out"] == []
    # 4 is not even far below
    assert sg.guard_county(county([10, 12, 9, 11, 4, 10]))["guard"]["left_out"] == []
    # a market that doubles in six months has no bad month
    assert sg.guard_county(county([50, 60, 70, 80, 90, 100]))["guard"]["left_out"] == []
    clean = sg.guard_county(county([150, 160, 155, 170, 148, 152]))
    assert clean["guard"]["line"] == "6 complete months" and "window_used" not in clean
    assert clean["deals_6mo"] == clean["deals"] and clean["baseline"]["dpd_6mo"] == clean["baseline"]["dpd"]
    assert sg.lines(clean) == []


def test_a_thin_county_keeps_its_zero_months_until_the_state_file_shows_the_gap():
    thin = county([1, 0, 2, 0, 1, 0], name="Birch")
    alone = sg.guard_county(thin)
    assert alone["guard"]["left_out"] == [] and alone["guard"]["line"] == "6 complete months"
    shard = {"99001": county([150, 160, 155, 170, 148, 0]), "99003": thin,
             "99005": county([40, 44, 38, 41, 45, 0], name="Cedar")}
    state = sg.state_verdict(shard)
    assert state["checked"] and state["counties"] == 3
    assert [m["kept"] for m in state["months"]] == [True] * 5 + [False]
    assert state["months"][5]["counties_at_zero"] == 3
    birch = sg.county_from(shard, "99003")
    # April's zero is a real quiet month and stays; June is missing across the file and goes
    assert birch["guard"]["left_out"] == ["June"] and birch["guard"]["months"][3]["kept"] is True
    assert birch["guard"]["line"] == "5 complete months, June left out: it reads zero"
    assert sg.county_from(shard, "99999") is None


def test_a_county_short_where_the_whole_state_is_short_says_so():
    shard = {"99001": county([150, 160, 155, 170, 148, 12]), "99003": county([4, 5, 3, 6, 4, 1], name="Birch"),
             "99005": county([40, 44, 38, 41, 45, 3], name="Cedar")}
    birch = sg.county_from(shard, "99003")
    assert sg.guard_county(shard["99003"])["guard"]["left_out"] == []                    # 1 against 4 proves nothing alone
    assert birch["guard"]["left_out"] == ["June"]
    assert "it reads 1 here and the whole state file is short that month" in birch["guard"]["line"]
    # a county on a different window is judged on its own numbers only
    other = county([4, 5, 3, 6, 4, 1], name="Dogwood", window="2025-10 .. 2026-03 (6 complete months)")
    shard["99007"] = other
    assert sg.county_from(shard, "99007")["guard"]["left_out"] == []


def test_two_months_out_and_too_few_months_left():
    two = sg.guard_county(county([150, 160, 155, 170, 0, 0]))
    assert two["guard"]["line"] == "4 complete months, May and June left out: each reads zero", two["guard"]["line"]
    assert two["guard"]["six_month_factor"] == 1.5 and two["baseline"]["dpd_6mo"] == 100.0
    few = sg.guard_county(county([30, 0, 0, 28], window="2026-03 .. 2026-06 (4 complete months)"), restate=True)
    g = few["guard"]
    assert g["months_used"] == 2 and g["usable"] is False and g["restated"] is False
    assert g["line"].endswith("Too few complete months are left to rank on")
    assert few["baseline"]["dpd_6mo"] == few["baseline"]["dpd"] == 150.0                  # not projected from two months
    assert "should not be ranked on" in " ".join(sg.lines(few))


def test_data_with_no_monthly_counts_is_reported_as_unchecked_never_as_clean():
    for bad in (None, [], "12,9,11", [12, None, 11], [12, -1, 11], [True, False]):
        c = sg.guard_county(dict(county([1, 1, 1]), trend=bad))
        assert c["guard"]["checked"] is False and c["guard"]["left_out"] == []
        assert "window was not checked" in c["guard"]["line"] and c["baseline"]["dpd"] == 150.0
        assert sg.lines(c) == ["No monthly counts in this county's data, so the window was not checked."]
    assert sg.judge("nope") == [] and sg.state_verdict({"x": {"trend": None}})["checked"] is False


def test_months_are_named_from_the_window_and_never_guessed():
    assert sg.month_labels(WINDOW, 6) == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert sg.month_labels("2025-11 .. 2026-01 (3 complete months)", 3) == ["2025-11", "2025-12", "2026-01"]
    # a window that names a different number of months than the counts hold is not trusted
    assert sg.month_labels(WINDOW, 5) == ["month 1", "month 2", "month 3", "month 4", "month 5"]
    assert sg.month_labels("the last half year", 2) == ["month 1", "month 2"]
    assert sg.month_labels("", 2, fallback=["Nov", "Dec"]) == ["Nov", "Dec"]
    span = sg.guard_county(county([150, 160, 155, 170, 148, 0], window="2025-08 .. 2026-01 (6 complete months)"))
    assert span["guard"]["line"] == "5 complete months, January 2026 left out: it reads zero"
    numbered = sg.guard_county(county([150, 160, 155, 170, 148, 0], window="recent"))
    assert numbered["guard"]["line"] == "5 complete months, month 6 left out: it reads zero"
    assert numbered["window_used"].startswith("the published window (")


def test_a_median_on_few_sales_is_marked():
    c = sg.guard_county(county([150, 160, 155, 170, 148, 152]))
    assert c["rows"][0]["median_trusted"] is True and c["rows"][1]["median_trusted"] is False      # 40 sales, 9 sales
    assert "median_trusted" not in c["baseline"] and "median_trusted" not in c
    # it rests on the sales that were counted: 13 sales carried to a six-month pace of 16 is still 13 sales
    short = sg.guard_county(county([150, 160, 155, 170, 148, 0], rows=[{"seg": "Probate", "deals": 13, "dpd": 30.0}]))
    assert short["rows"][0]["deals_6mo"] == 16 and short["rows"][0]["median_trusted"] is False


def test_the_lines_a_reader_prints():
    c = sg.guard_county(county([150, 160, 155, 170, 148, 0]))
    said = sg.lines(c)
    assert said[0] == "Window used: 5 complete months, June left out: it reads zero."
    assert "as published, on 5 months" in said[1] and "940 deals, baseline 125 doors per deal" in said[1]
    r = sg.lines(sg.guard_county(county([150, 160, 155, 170, 148, 0]), restate=True))
    assert "at a six-month rate" in r[1] and "Published: 783 deals, baseline 150 doors per deal." in r[1]


def test_the_chance_test_itself():
    assert sg.chance_of_at_most(0, 7) < 0.001 < sg.chance_of_at_most(0, 6)
    assert abs(sg.chance_of_at_most(3, 10) - 0.0103) < 0.0005
    assert sg.chance_of_at_most(20, 5000) == 0.0 and sg.chance_of_at_most(5, 0) == 1.0
    assert sg.chance_of_at_most(10 ** 6, 3.0) == 1.0


def test_the_command_line_reads_a_local_file_only(tmp_path=None):
    import io
    import json
    import tempfile
    from contextlib import redirect_stdout
    shard = {"99001": county([150, 160, 155, 170, 148, 0]), "99003": county([1, 0, 2, 0, 1, 0], name="Birch")}
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "shard.json"
        p.write_text(json.dumps(shard), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert sg.main([str(p)]) == 0
    text = buf.getvalue()
    assert "state file: 2 counties, 5 complete months, June left out: it reads zero" in text
    assert "99003 Birch: 5 complete months, June left out: it reads zero" in text
    assert "2 of 2 counties had a month left out" in text
    code = (ROOT / "src/shard_guard.py").read_text(encoding="utf-8")
    for word in ("urllib", "requests", "http://", "socket"):
        assert word not in code, word                                                      # it cannot reach a network


def test_the_inventory_reads_its_county_through_the_guard():
    import list_inventory as li
    import list_inventory_report as rep
    shard = {"99001": county([150, 160, 155, 170, 148, 0]), "99003": county([40, 44, 38, 41, 45, 0], name="Cedar")}
    c = li.guarded(shard, "99001")
    assert c["guard"]["left_out"] == ["June"] and c["baseline"]["dpd"] == 150.0 and c["rows"][0]["dpd_6mo"] == 41.7
    try:
        li.guarded(shard, "99999")
        raise AssertionError("a county the file does not hold was accepted")
    except SystemExit as stop:
        assert "no playbook data" in str(stop)
    assert rep.shard_guard is sg
    # an inventory file written before the guard existed adds nothing to the report
    assert sg.lines({"window": WINDOW, "deals": 5, "baseline": {"dpd": 1.0}}) == []
    # one written since carries the honest window and the caveat the report prints under the band
    saved = {k: v for k, v in c.items() if k != "rows"}
    assert saved["window_used"].endswith("June left out: it reads zero)") and len(sg.lines(saved)) == 2


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/shard_guard.py", "tests/test_shard_guard.py"):
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
