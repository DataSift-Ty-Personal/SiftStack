"""Deal odds: the chance a house sells to an investor in the next six months, learned from what
actually sold in Knox and Blount.

The owner's ask (2026-11-14): rework the Dream 100 so it rests "on every qualitative and
quantitative metric that we can ... as objectively as possible", not on the few houses that carry
a D4D score.

WHY ODDS, NOT POINTS. The Dream 100 score added hand-set points (deal size 35, condition 20,
investor 20, evidence 15, reach 10). Nobody had measured whether those weights predict a deal, and
two of them were missing for most houses: the D4D condition score exists for 3,000 houses, and
reach read zero for every owner whose phones were never scored. On the 2026-11-13 Top 1,000, 938
of 1,000 owners had no condition score and 693 had zero reach, so deal size (which favors
expensive houses) decided the list. This module replaces the points with odds LEARNED FROM
OUTCOMES: every investor purchase in the two counties, set against what each house looked like
before it sold. SiftMap's flags carry the date each was added and removed, so a house's state on
any day since mid-2025 can be rebuilt; src/deal_odds_train.py fits the model on one half-year and
tests it on the next.

WHAT IT SEES, and why exactly this. Only what the box can read for EVERY house, so a house is
never scored on a different basis from its neighbor:
  - the SiftMap search row: 27 distress and owner flags, absentee, free and clear, high equity,
    a corporate owner, a cash purchase, the value, the size and the lot;
  - two facts the search can only give in bands: how long the owner has held it and the year it
    was built (the records search filters `last_sold` and `year`, or SiftMap's own sale-date and
    year-built filters);
  - the area's own investor activity: frozen tables of ZIP and neighborhood-cell rates from the
    year before the model was fitted.
Left out on purpose: DataFlik's AI scores and the D4D visual score. No copy of either exists from
before last year's sales, so their real lift cannot be measured yet; the monthly sweep keeps a copy
from now on, and they go in once a held-out half-year proves they add something.

The model is a logistic regression stored as plain weights in data/deal_odds_model.json, so the
box scores with nothing but this file (no scikit-learn there).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parents[1] / "data" / "deal_odds_model.json"

# The 27 flags a SiftMap search row carries in `distressors` (verified live 2026-11-14), plus the
# three owner traits the row carries elsewhere (absenteeOwner, presets.free_clear, presets.high_equity).
ROW_FLAGS = (
    "is_bad_credit", "is_bankruptcy_property", "is_court_order", "is_criminal_felony", "is_divorce_property",
    "is_estate_sales", "is_eviction", "is_federal_lien", "is_final_judgment", "is_heirs_property", "is_hoa_lien",
    "is_inheritance_property", "is_judgment_liens", "is_lis_pendens", "is_low_income", "is_notice_of_default",
    "is_notice_of_foreclosure", "is_obituary_property", "is_other_lien", "is_pre_probate", "is_probate_properties",
    "is_recent_deliquent", "is_senior_homeowners", "is_short_term_loan", "is_state_tax", "is_tired_landlord",
    "is_vacant_property",
)
OWNER_FLAGS = ("is_absentee_owners", "is_free_and_clear", "is_high_equity")
FLAG_KEYS = ROW_FLAGS + OWNER_FLAGS
# The flags the model is NOT allowed to learn from, because their past cannot be rebuilt honestly
# (measured 2026-11-14 on the houses that sold after 2025-11-14 against those that did not, by how
# often each carries any trace of the flag): a seller's senior flag mostly vanishes at the sale
# instead of being marked removed (0.35 times the unsold rate), heirs and short-term-loan flags
# vanish the same way, and absentee, out-of-state, low-income and bad-credit flags are re-set for
# the new owner (2.8 to 4.2 times), so the seller's state cannot be told from the buyer's. A model
# taught on them learns how DataFlik rewrites a sold house, not what a seller looked like. They stay
# on the facts (the gates and the reasons can read them) and go in once the monthly snapshots, kept
# from 2026-11 on, can measure them forward against real sales.
UNTESTABLE_FLAGS = ("is_senior_homeowners", "is_heirs_property", "is_short_term_loan", "is_absentee_owners",
                    "is_low_income", "is_bad_credit", "is_free_and_clear", "is_high_equity", "is_tired_landlord",
                    "is_criminal_felony")
MODEL_FLAGS = tuple(k for k in FLAG_KEYS if k not in UNTESTABLE_FLAGS)
# A blank size is read as the typical house. Blanks are filled after a sale (a flip's listing adds
# the bedroom count: 19% of unsold houses lack it against 7.5% of sold ones), so "unknown" would
# teach the model that a filled-in record sells.
IMPUTE = {"beds": "3", "baths": "2", "lot": "lt025", "built": "1960s_70s"}

# Bands. A band, not a raw number, because the box learns tenure and year built only as bands, and a
# band keeps one strange value (a $1 AVM, a 400-acre lot) from swinging a house's odds.
VALUE_BANDS = [(0, 75_000, "lt75k"), (75_000, 125_000, "75_125k"), (125_000, 175_000, "125_175k"),
               (175_000, 250_000, "175_250k"), (250_000, 350_000, "250_350k"), (350_000, 500_000, "350_500k"),
               (500_000, 700_000, "500_700k"), (700_000, float("inf"), "700k_plus")]
TENURE_BANDS = [(0, 2, "lt2"), (2, 5, "2_5"), (5, 10, "5_10"), (10, 20, "10_20"), (20, 30, "20_30"),
                (30, float("inf"), "30_plus")]
YEAR_BANDS = [(0, 1940, "pre1940"), (1940, 1960, "1940s_50s"), (1960, 1980, "1960s_70s"), (1980, 2000, "1980s_90s"),
              (2000, 2010, "2000s"), (2010, 10_000, "2010_plus")]
LOT_BANDS = [(0, 0.25, "lt025"), (0.25, 0.5, "025_05"), (0.5, 1.0, "05_1"), (1.0, 5.0, "1_5"), (5.0, float("inf"), "5_plus")]
BED_BANDS = [(0, 3, "le2"), (3, 4, "3"), (4, 100, "4_plus")]
# Debt against the house as a share of its value. Free and clear is its own band, since it is a
# different seller (no payoff to clear) and the commonest single answer.
LTV_BANDS = [(0, 0.001, "free_clear"), (0.001, 0.3, "lt30"), (0.3, 0.6, "30_60"), (0.6, 0.8, "60_80"),
             (0.8, float("inf"), "80_plus")]
BATH_BANDS = [(0, 2, "le1"), (2, 3, "2"), (3, 100, "3_plus")]
COUNTIES = {"47093": "knox", "47009": "blount"}
CELL = 0.01                      # degrees; a neighborhood cell is about 1.1 km by 0.9 km here
ZIP_RX = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")

LABELS = {
    "flag:is_bad_credit": "low credit score", "flag:is_bankruptcy_property": "bankruptcy",
    "flag:is_court_order": "court order", "flag:is_criminal_felony": "criminal record",
    "flag:is_divorce_property": "divorce", "flag:is_estate_sales": "estate sale",
    "flag:is_eviction": "eviction", "flag:is_federal_lien": "federal tax lien",
    "flag:is_final_judgment": "final judgment", "flag:is_heirs_property": "heirs property",
    "flag:is_hoa_lien": "HOA lien", "flag:is_inheritance_property": "inherited",
    "flag:is_judgment_liens": "judgment lien", "flag:is_lis_pendens": "lis pendens",
    "flag:is_low_income": "low income", "flag:is_notice_of_default": "notice of default",
    "flag:is_notice_of_foreclosure": "notice of foreclosure", "flag:is_obituary_property": "obituary on record",
    "flag:is_other_lien": "lien", "flag:is_pre_probate": "pre-probate", "flag:is_probate_properties": "probate",
    "flag:is_recent_deliquent": "tax delinquent", "flag:is_senior_homeowners": "senior owner",
    "flag:is_short_term_loan": "short-term loan", "flag:is_state_tax": "state tax lien",
    "flag:is_tired_landlord": "tired landlord", "flag:is_vacant_property": "vacant",
    "flag:is_absentee_owners": "absentee owner", "flag:is_free_and_clear": "free and clear",
    "flag:is_high_equity": "high equity", "owner:corporate": "company owner", "owner:cash_purchase": "bought with cash",
    "area:zip": "investors buy in this ZIP", "area:cell": "investors buy on these streets",
    "ppsf:low": "cheap for its size here", "ppsf:high": "expensive for its size here",
    "mls:failed_listing": "a listing that failed", "ltv:free_clear": "free and clear", "ltv:lt30": "little debt",
}


def ltv_from_equity(equity_pct):
    """Debt as a share of value from an equity percentage (SiftMap's equityPercent): 100% equity is free
    and clear, and negative equity reads as more debt than value."""
    try:
        e = float(equity_pct)
    except (TypeError, ValueError):
        return None
    return max(0.0, (100.0 - e) / 100.0)


def band(v, bands, missing: str = "unknown") -> str:
    if v is None:
        return missing
    try:
        v = float(v)
    except (TypeError, ValueError):
        return missing
    if v != v:  # NaN
        return missing
    for lo, hi, name in bands:
        if lo <= v < hi:
            return name
    return missing


def logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def cell_of(lat, lon) -> str:
    try:
        return "%.2f,%.2f" % (math.floor(float(lat) / CELL) * CELL, math.floor(float(lon) / CELL) * CELL)
    except (TypeError, ValueError):
        return ""


def zip_of(address) -> str:
    m = ZIP_RX.search(str(address or "").strip())
    return m.group(1) if m else ""


def encode(f: dict, tables: dict) -> dict[str, float]:
    """One house's features, from its facts. `f` keys: flags (iterable of FLAG_KEYS), corporate,
    cash, value, sqft, beds, baths, lot_acres, tenure_years, year_built, zip, lat, lon, fips.
    `tables`: base (the base rate), zip {zip: log-odds over base}, cell {cell: log-odds over its
    ZIP}, zip_ppsf {zip: median value per square foot}. Training and the box both call this."""
    x: dict[str, float] = {}
    raw = f.get("flags") or ()
    # A set of flags (the box: on or off) or {flag: how surely it was on} (a rebuilt past day, where
    # a removed flag's start date is lost and counts a little under 1).
    flags = {k: float(v) for k, v in raw.items()} if isinstance(raw, dict) else {k: 1.0 for k in raw}
    for k in MODEL_FLAGS:
        if flags.get(k, 0.0) > 0:
            x["flag:" + k] = min(1.0, flags[k])
    if f.get("corporate"):
        x["owner:corporate"] = 1.0
    if f.get("cash"):
        x["owner:cash_purchase"] = 1.0
    x["value:" + band(f.get("value") or None, VALUE_BANDS)] = 1.0
    x["tenure:" + band(f.get("tenure_years"), TENURE_BANDS)] = 1.0
    yb = f.get("year_built")
    x["built:" + band(yb if yb and float(yb) > 1700 else None, YEAR_BANDS, IMPUTE["built"])] = 1.0
    x["lot:" + band(f.get("lot_acres") or None, LOT_BANDS, IMPUTE["lot"])] = 1.0
    x["beds:" + band(f.get("beds") or None, BED_BANDS, IMPUTE["beds"])] = 1.0
    x["baths:" + band(f.get("baths") or None, BATH_BANDS, IMPUTE["baths"])] = 1.0
    x["ltv:" + band(f.get("ltv"), LTV_BANDS)] = 1.0
    if f.get("failed_listing"):
        x["mls:failed_listing"] = 1.0
    county = COUNTIES.get(str(f.get("fips") or ""), "other")
    x["county:" + county] = 1.0
    z = str(f.get("zip") or "")
    x["area:zip"] = max(-2.0, min(2.0, float((tables.get("zip") or {}).get(z, 0.0))))
    c = cell_of(f.get("lat"), f.get("lon"))
    x["area:cell"] = max(-2.0, min(2.0, float((tables.get("cell") or {}).get(c, 0.0))))
    med = (tables.get("zip_ppsf") or {}).get(z)
    v, s = f.get("value"), f.get("sqft")
    try:
        if med and v and s and float(s) >= 300 and float(v) > 0:
            r = math.log((float(v) / float(s)) / float(med))
            r = max(-1.5, min(1.5, r))
            if r < 0:
                x["ppsf:low"] = -r
            elif r > 0:
                x["ppsf:high"] = r
    except (TypeError, ValueError, ZeroDivisionError):
        pass                                     # no size or no ZIP median: read as a typical price per foot
    return x


# ---------------------------------------------------------------- the fitted model
def load(path: Path | str | None = None) -> dict:
    p = Path(path or MODEL_PATH)
    return json.loads(p.read_text(encoding="utf-8"))


def z_of(model: dict, x: dict[str, float]) -> float:
    w = model["weights"]
    return float(model["intercept"]) + sum(float(w.get(k, 0.0)) * v for k, v in x.items())


def prob(model: dict, facts: dict) -> float:
    """The chance this house sells to an investor within the model's window (six months)."""
    return sigmoid(z_of(model, encode(facts, model["tables"])))


def reasons(model: dict, facts: dict, k: int = 3) -> list[str]:
    """The features that raise this house's odds most, in plain words, strongest first. A band is
    named only when it raises the odds over the average house; an area rate only when it is high."""
    x = encode(facts, model["tables"])
    w = model["weights"]
    ref = model.get("band_ref") or {}
    scored = []
    for key, val in x.items():
        group = key.split(":")[0]
        lift = float(w.get(key, 0.0)) * val - (float(ref.get(group, 0.0)) if group in ref else 0.0)
        if lift > 0.05:
            scored.append((lift, key))
    out = []
    for lift, key in sorted(scored, reverse=True)[:k]:
        out.append("%s (x%.1f)" % (LABELS.get(key) or key.replace(":", " ").replace("_", " "), math.exp(lift)))
    return out


def owner_prob(probs) -> float:
    """The chance at least one of an owner's houses sells to an investor: one conversation, many doors."""
    q = 1.0
    for p in probs:
        q *= 1.0 - max(0.0, min(1.0, float(p)))
    return 1.0 - q


# ---------------------------------------------------------------- the box's facts
def facts_from_search_row(row: dict) -> dict:
    """A SiftMap search row as facts. Tenure and year built are not on the row; the caller adds them
    from its band searches (`with_bands`). A slimmed row (agents.autopilot.top1000.slim_row) carries
    the same facts under short keys and is read too."""
    if "distressors" in row or "estimatedValue" in row:
        d = row.get("distressors") or {}
        pre = row.get("presets") or {}
        flags = {k for k, v in d.items() if (v.get("is_active") if isinstance(v, dict) else v) and k in ROW_FLAGS}
        if row.get("absenteeOwner") or pre.get("absentee_owners"):
            flags.add("is_absentee_owners")
        if pre.get("free_clear"):
            flags.add("is_free_and_clear")
        if pre.get("high_equity"):
            flags.add("is_high_equity")
        return {"flags": flags, "corporate": bool(row.get("corporateOwned")), "cash": bool(pre.get("cash_buyer")),
                "value": row.get("estimatedValue"), "sqft": row.get("squareFeet"), "beds": row.get("bedrooms"),
                "baths": row.get("bathrooms"), "lot_acres": row.get("lotAcres"), "zip": zip_of(row.get("address")),
                "lat": row.get("latitude"), "lon": row.get("longitude"), "fips": str(row.get("fips") or ""),
                "tenure_years": None, "year_built": None, "ltv": ltv_from_equity(row.get("equityPercent")),
                "failed_listing": bool(row.get("mlsCancelled"))}
    # Only the dated flags the model learned from: the row's separate `presets.vacant` marker is a
    # different signal from the is_vacant_property flag, so it is not read as one.
    flags = {k for k in row.get("flags") or () if k in ROW_FLAGS}
    for short, key in (("abs", "is_absentee_owners"), ("fc", "is_free_and_clear"), ("he", "is_high_equity")):
        if row.get(short):
            flags.add(key)
    return {"flags": flags, "corporate": bool(row.get("corp")), "cash": bool(row.get("cash")), "value": row.get("v"),
            "sqft": row.get("sq"), "beds": row.get("bd"), "baths": row.get("ba"), "lot_acres": row.get("lot"),
            "zip": row.get("zip") or zip_of(row.get("addr")), "lat": row.get("lat"), "lon": row.get("lon"),
            "fips": str(row.get("fips") or ""), "tenure_years": row.get("ten"), "year_built": row.get("yb"),
            "ltv": ltv_from_equity(row.get("e")), "failed_listing": bool(row.get("mc"))}


def with_bands(facts: dict, tenure_band: str | None = None, year_band: str | None = None) -> dict:
    """Facts with tenure and year built filled from band searches (a band name from TENURE_BANDS
    or YEAR_BANDS). The middle of the band is enough: the model only ever sees the band."""
    out = dict(facts)
    for key, name, bands in (("tenure_years", tenure_band, TENURE_BANDS), ("year_built", year_band, YEAR_BANDS)):
        if name and out.get(key) is None:
            for lo, hi, nm in bands:
                if nm == name:
                    out[key] = lo if hi == float("inf") or hi >= 10_000 else (lo + hi) / 2.0
    return out


def facts_from_record(rec: dict, facts: dict | None = None, today=None) -> dict:
    """Facts completed from a full DataSift record (a finalist): exact tenure and year built, on the
    run's own clock (`today`, a date) so a replayed run reads the same."""
    from datetime import date
    today = today or date.today()
    out = dict(facts or {})
    try:
        y = int(rec.get("year") or 0)
        if y > 1700:
            out["year_built"] = y
    except (TypeError, ValueError):
        pass
    for key in ("owned_since", "last_sold"):
        raw = str(rec.get(key) or "")[:10]
        if len(raw) == 10:
            try:
                d = date.fromisoformat(raw)
                out["tenure_years"] = max(0.0, (today - d).days / 365.25)
                break
            except ValueError:
                continue
    return out
