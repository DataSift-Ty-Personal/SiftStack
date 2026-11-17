"""Zero-network tests for src/mortgage_model.py. Invented tracts and amounts.

    python tests/test_mortgage_model.py      (or pytest)
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mortgage_model as mm  # noqa: E402


class FakeRates(mm.Rates):
    def __init__(self, weekly):
        self.d = [w[0] for w in weekly]
        self.r30 = [w[1] for w in weekly]
        self.r15 = [w[2] for w in weekly]


RATES = FakeRates([(date(2020, 1, 2), 3.7, 3.1), (date(2020, 3, 5), 3.3, 2.8), (date(2023, 9, 7), 7.1, 6.5)])


def bare_model():
    m = mm.Model.__new__(mm.Model)
    m.rates, m.idx, m.spread, m.spread_pre2018, m.dated, m.hmda_loans = RATES, {}, {}, {}, {}, 0
    m.lock_days, m.method = mm.LOCK_DAYS, "pmms"
    return m


def test_fallback_is_chosen_by_the_matches():
    m = bare_model()
    real = [{"recorded": date(2020, 4, 15), "typ": 1, "term": 360, "rate": 3.30, "index_rate": 3.31} for _ in range(12)]
    out = m.choose_fallback(real, lags=(35,))
    assert out["chosen"] in ("pmms", "index") and m.method == out["chosen"]
    m.method = "index"
    assert m.loan(161_100, date(2020, 4, 27), None, "Conventional", {}, index_rate=3.4)["rate_source"] == "index estimate"
    m.method = "blend"
    b = m.loan(161_100, date(2020, 4, 27), None, "Conventional", {}, index_rate=3.4)
    assert b["rate_source"] == "average of index and Freddie Mac" and abs(b["rate"] - (3.3 + 3.4) / 2) < 0.01


def test_amortization():
    assert round(mm.payment(200_000, 3.0, 360)) == 843
    assert mm.balance_after(200_000, 3.0, 360, 0) == 200_000 and mm.balance_after(200_000, 3.0, 360, 360) == 0
    assert round(mm.balance_after(200_000, 3.0, 360, 60)) == 177_813      # checked against a month-by-month loop
    assert mm.payment(120_000, 0.0, 360) == 120_000 / 360


def test_term_and_band():
    assert mm.snap_term(mm.months_between(date(2020, 4, 27), date(2050, 5, 1))) == 360
    assert mm.snap_term(181) == 180 and mm.snap_term(None) == 360
    assert mm.amount_band(161_100) == 165_000 and mm.amount_band(160_000) == 165_000


def test_rates_lookup_uses_the_week_before():
    assert RATES.at(date(2020, 3, 4)) == 3.7 and RATES.at(date(2020, 3, 5)) == 3.3
    assert RATES.at(date(2020, 3, 10), 180) == 2.8 and RATES.at(date(2019, 1, 1)) is None


def test_hmda_match_unique_close_and_fallback():
    m = bare_model()
    tr = {"t2020": "47093000100", "t2010": "47093009900"}
    m.idx[(2020, "47093009900", 1, 165_000, 360)] = [{"rate": 3.125, "ltv": "96.5", "arm": False}]
    one = m.loan(161_100, date(2020, 4, 27), date(2050, 5, 1), "Conventional", tr, today=date(2026, 11, 16))
    assert one["rate"] == 3.125 and one["rate_source"] == "HMDA match" and one["term_months"] == 360
    assert one["mi"] > 0 and "PMI" in one["mi_note"]                       # 96.5% of value at the start
    m.idx[(2020, "47093009900", 1, 165_000, 360)] = [{"rate": 3.0}, {"rate": 3.125}, {"rate": 3.25}]
    close = m.loan(161_100, date(2020, 4, 27), None, "Conventional", tr, today=date(2026, 11, 16))
    assert close["rate"] == 3.125 and close["rate_source"] == "HMDA match (3 loans)"
    m.idx[(2020, "47093009900", 1, 165_000, 360)] = [{"rate": 2.75}, {"rate": 3.9}]
    far = m.loan(161_100, date(2020, 4, 27), None, "Conventional", tr, today=date(2026, 11, 16))
    assert far["rate"] == 3.3 and far["rate_source"].startswith("Freddie Mac week")   # lock 35 days before
    assert far["hmda_candidates"] == 2                                      # they disagree, so no match


def test_2020_tracts_from_2022_and_january_tries_prior_year():
    m = bare_model()
    tr = {"t2020": "47093000100", "t2010": "47093009900"}
    m.idx[(2022, "47093000100", 2, 205_000, 360)] = [{"rate": 4.5}]
    assert m.loan(200_000, date(2023, 1, 9), None, "FHA", tr)["rate"] == 4.5   # closed late 2022, recorded January


def test_calibration_uses_dated_gaps():
    m = bare_model()
    matches = [{"recorded": date(2020, 4, 15), "typ": 3, "term": 360, "rate": 3.3 - 0.3} for _ in range(12)]
    m.calibrate(matches)
    assert m.spread_for(2020, 3, 360) == -0.3 and m.spread_for(2021, 3, 360) == -0.3
    assert m.spread_for(2020, 1, 360) == 0.0                                  # no conventional cell yet


def test_unmodeled_and_insurance():
    m = bare_model()
    assert m.loan(50_000, date(2020, 4, 1), None, "Credit Line", {}) is None
    fha_new, _ = mm.mortgage_insurance(2, 200_000, 190_000, date(2024, 1, 1), None)
    fha_old, _ = mm.mortgage_insurance(2, 200_000, 190_000, date(2021, 1, 1), None)
    assert round(fha_new) == round(190_000 * 0.0055 / 12) and fha_old > fha_new
    assert mm.mortgage_insurance(3, 200_000, 190_000, date(2021, 1, 1), None)[0] == 0
    assert mm.mortgage_insurance(1, 200_000, 120_000, date(2015, 1, 1), "95")[0] == 0   # PMI has cancelled
    assert mm.mortgage_insurance(1, 200_000, 190_000, date(2021, 1, 1), None)[1] == "PMI unknown"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
