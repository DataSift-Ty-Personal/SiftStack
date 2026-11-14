"""Deal-odds selftest: the Top 1,000 ranked on the chance an owner sells to an investor within six
months (src/deal_odds.py), the default since 2026-11-14.

What is checked: each pool record gets its tenure and year-built band from the record search, a
house with too little discount room is left out, an owner's odds combine every house they hold,
the measured reach table lifts an owner reached on a Dial First number, the ranking file reads as
odds with reasons, and a block whose model cannot be read is refused instead of quietly ranked on
the old points. Zero network: the two-track selftest's fake account and fake SiftMap, a fake
model written to the suite's temporary folder. Every name, street, number and id is invented.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, PRESETS
from .selftest_textlane import mob
from .selftest_tracks import NOV, TRACK_PRESETS, FakeMap, TrackCRM, _wipe, house, playbook

MODEL = {
    "version": 1, "fitted": "2026-11-14", "window_days": 182, "intercept": -5.0,
    "weights": {"flag:is_vacant_property": 2.0, "tenure:20_30": 1.2, "tenure:10_20": 0.4, "tenure:lt2": -2.0,
                "built:1940s_50s": 0.3},
    "band_ref": {"tenure": 0.1, "built": 0.0},
    "tables": {"base": 0.01, "zip": {}, "cell": {}, "zip_ppsf": {}},
    "reach": {"Dial First": 0.46, "untiered": 0.37},
    "test": {"capture": [{"top": 1000, "deals": 50, "lift": 9.0}]},
}


class BandCRM(TrackCRM):
    """The fake account plus the two record-search ranges the band scans send: last_sold dates and
    year built. A record with no value for the field matches no band, as on the live search."""

    def _match(self, r, must):
        if not super()._match(r, must):
            return False
        for key in ("last_sold", "year"):
            rng = must.get(key)
            if not rng:
                continue
            v = r.get(key)
            if v in (None, ""):
                return False
            v = int(v) if key == "year" else str(v)[:10]
            lo, hi = rng
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                return False
        return True


class OddsMap(FakeMap):
    """The fake SiftMap rows with what the deal-odds model reads off a row."""

    def row(self, r, tags):
        out = super().row(r, tags)
        out.update({"presets": {"free_clear": bool(r.get("free_clear"))}, "squareFeet": 1400.0, "bedrooms": 3,
                    "bathrooms": 1, "lotAcres": 0.3, "latitude": 35.95, "longitude": -83.95})
        return out


def account() -> list:
    """V: a vacant house held 28 years, reached on a Dial First number. W: two plain houses held 14
    years. N: vacant but bought last year. S: too little room for a fee. Z: no sale on record."""
    return [
        house("v1", "1 Odds St", "o-v", value=150000, equity=100.0, flags=["is_vacant_property"], last_sold="1998-05-01",
              year=1955, phones=[mob(6101, "Dial First")]),
        house("w1", "2 Odds St", "o-w", value=200000, equity=40.0, last_sold="2012-06-01", year=1990),
        house("w2", "3 Odds St", "o-w", value=200000, equity=40.0, last_sold="2012-06-01", year=1990),
        house("n1", "4 Odds St", "o-n", value=180000, equity=30.0, flags=["is_vacant_property"], last_sold="2025-06-01", year=1985),
        house("s1", "5 Odds St", "o-s", value=60000, equity=10.0, flags=["is_vacant_property"], last_sold="1990-01-01", year=1950),
        house("z1", "6 Odds St", "o-z", value=300000, equity=50.0, year=1975),
    ]


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import top1000
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = Path(tmp) / "deal_odds", 100000.0, 5.0
    config.MODE = "dry"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS.update(TRACK_PRESETS)
    saved_search = top1000.search
    saved_env = os.environ.pop(top1000.ALLOW_ENV, None)
    try:
        _wipe()
        _checks(check, Path(tmp))
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        top1000.search = saved_search
        if saved_env is not None:
            os.environ[top1000.ALLOW_ENV] = saved_env
        _wipe()
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = saved
        config.MODE = "dry"


def _checks(check, tmp: Path) -> None:
    import deal_odds
    from .autopilot import policy, sweep, top1000
    print("  the Top 1,000 on deal odds")
    path = tmp / "deal_odds_test_model.json"
    path.write_text(json.dumps(MODEL), encoding="utf-8")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    pol = policy.parse(playbook(tmp, score="deal_odds", odds_model=str(path), size=2), EMPLOYEES)
    r = sweep.run("t_top", crm=crm, now=NOV, pol=pol, plan=True)
    pool = top1000.load_json(top1000.state_path("t_top", "2026-11", "plan").with_name("t_top-2026-11-plan-pool.json"), {})
    rows = pool.get("rows") or {}
    check(rows.get("v1", {}).get("ten") == "20_30" and rows.get("v1", {}).get("yb") == "1940s_50s"
          and rows.get("n1", {}).get("ten") == "lt2" and rows.get("w1", {}).get("ten") == "10_20"
          and "ten" not in rows.get("z1", {}) and pool.get("bands") is True,
          "each pool record gets its tenure and year-built band from the record search; one with no sale reads unknown")
    check(r.tally.get("left out: under $15,000 of discount room (no room for a fee)") == 1,
          "a house with too little discount room for a fee is left out whatever its odds")
    state = top1000.month_state("t_top", "2026-11")
    check(state.get("owners") == ["o-v", "o-w"] and state.get("tagged") == ["v1", "w1", "w2"],
          "the owners with the best odds win: the long-held vacant house, then the owner of two plain ones")
    out = list(csv.reader(Path(state["summary"]["file"]).open(encoding="utf-8")))
    p_v = deal_odds.sigmoid(-5.0 + 2.0 + 1.2 + 0.3)
    p_w = deal_odds.owner_prob([deal_odds.sigmoid(-5.0 + 0.4)] * 2)
    check(out[0] == top1000.CSV_HEAD_ODDS and out[1][:4] == ["1", "%s" % round(100 * p_v, 2), "1", "1 Odds St"]
          and out[2][:3] == ["2", "%s" % round(100 * p_w, 2), "2"],
          "the ranking file reads as odds in six months, and an owner's odds combine every house they hold")
    check("vacant" in out[1][6] and out[1][8] == "%s" % round(0.46 / 0.37, 3) and out[2][8] == "1.0",
          "the file says why (the strongest signals) and lifts an owner reached on a Dial First number by the measured rate")
    check(any(line.startswith("Ranked on deal odds (fitted 2026-11-14") for line in r.lines)
          and not any("condition score" in line for line in r.lines),
          "the run says what it ranked on and how that model tested, and no longer speaks of condition scores")

    # --- a block whose model cannot be read is refused, never ranked on the old points. The month's
    # files go first: a settled month stops before the model is ever asked for.
    _wipe()
    for f in top1000.tracks_dir().glob("t_top-*"):
        f.unlink()
    bad = policy.parse(playbook(tmp, score="deal_odds", odds_model=str(tmp / "no_such_model.json"), size=2), EMPLOYEES)
    r2 = sweep.run("t_top", crm=crm, now=NOV, pol=bad, plan=True)
    check(any("deal-odds model could not be read" in p for p in r2.problems)
          and not top1000.month_state("t_top", "2026-11").get("owners"),
          "a deal-odds block with no model is refused and ranks nothing")

    # --- the playbook loader refuses an unknown score and a negative room floor, naming both
    try:
        policy.parse(playbook(tmp, score="points", min_room=-1), EMPLOYEES)
        said = ""
    except policy.PolicyError as exc:
        said = str(exc)
    check("score must be deal_odds or dream100" in said and "min_room" in said,
          "the playbook check refuses an unknown score and a negative room floor")


if __name__ == "__main__":
    raise SystemExit("run through: python src/agents/cli.py selftest")
