"""Model a seller's mortgage from when it was made: the rate, the monthly payment and what is left on it today.

The SiftMap index estimates a rate, balance and payment for every open loan, but it does not say how. This
models the same three numbers from public facts, best evidence first:

  1. HMDA (the federal mortgage disclosure file, public, free) lists every loan a covered lender made, with its
     ACTUAL interest rate, loan type, amount (in $10,000 bands), term and census tract, but no address. A
     recorded loan whose (year, tract, type, amount band, term) holds exactly one HMDA loan is almost surely
     that loan, so its rate is the real one ("HMDA match"). Several HMDA loans on the key with rates within a
     quarter point give their median ("HMDA match, n loans"). Measured on Knox 2023: 49% of first-lien
     single-family loans are alone on their key.
  2. Otherwise: Freddie Mac's weekly 30-year (or 15-year) rate in the week the loan was locked (about 35 days
     before it recorded), plus the spread HMDA shows in the market's own counties for that loan type and year.
  3. The payment and today's balance come from the original amount, that rate and the real term (the due date
     says 15 or 30 years), amortized month by month; FHA, USDA and private mortgage insurance are added.

Traps: HMDA has interest rates only from 2018 on, so older loans use the PMMS + spread route with the 2018-2019
spread. HMDA switched from 2010 to 2020 census tracts in 2022, so each house needs both tract ids. A loan that
closes in late December can record in January, so January recordings also try the prior HMDA year. Credit
lines, construction and commercial loans are not modeled (variable or not amortizing).

    python src/mortgage_model.py --fetch --counties 47093,47009     # HMDA 2018-2025 + Freddie Mac, cached
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import math
import os
import subprocess
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
# The HMDA and Freddie Mac downloads are kept in output/ in a SiftStack checkout, else in ~/SiftStack/cache (an
# installed skill's own folder is replaced on every update). MORTGAGE_MODEL_CACHE overrides both.
CACHE = Path(os.environ.get("MORTGAGE_MODEL_CACHE")
             or ((ROOT / "output" if (ROOT / "src").is_dir() else Path.home() / "SiftStack" / "cache") / "mortgage_model"))
logger = logging.getLogger("mortgage_model")

HMDA_YEARS = range(2018, 2026)
TYPE_CODE = {"conventional": 1, "fha": 2, "va": 3, "usda": 4}
UNMODELED = {"credit line", "construction", "commercial", ""}   # "Other" / "Unknown" are priced as conventional
FALLBACKS = ("pmms", "index", "blend")
LOCK_DAYS = 35
TERMS = (120, 180, 240, 300, 360, 480)


# --------------------------------------------------------------------------------------------- sources

class DownloadFailed(RuntimeError):
    """A public download (Freddie Mac, HMDA) did not arrive. A RuntimeError, not SystemExit, so a program that
    calls the model as a library (the MLS offer engine, the agents brain) gets an error to say, not an exit."""


def _get(url: str, timeout: int = 300) -> bytes:
    """curl, not requests: the HMDA host answers a Python client with 403 and serves curl a redirect."""
    try:
        out = subprocess.run(["curl", "-s", "-L", "-m", str(timeout), url], capture_output=True, check=True).stdout
    except FileNotFoundError as exc:
        raise DownloadFailed("curl is not installed; the public mortgage data downloads need it") from exc
    except subprocess.CalledProcessError as exc:
        raise DownloadFailed("a public mortgage data download failed (curl exit %s)" % exc.returncode) from exc
    return gzip.decompress(out) if out[:2] == b"\x1f\x8b" else out


def fetch_pmms(force: bool = False) -> Path:
    p = CACHE / "pmms.csv"
    if force or not p.exists() or time.time() - p.stat().st_mtime > 6 * 86400:
        body = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US,MORTGAGE15US", 60)
        if b"MORTGAGE30US" not in body[:200]:
            raise DownloadFailed("Freddie Mac series did not download")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    return p


HMDA_KEEP = ("activity_year", "census_tract", "loan_type", "loan_purpose", "loan_amount", "interest_rate",
             "loan_term", "loan_to_value_ratio", "property_value", "intro_rate_period")


def fetch_hmda(county: str, year: int, force: bool = False) -> Path:
    """First-lien, closed-end, single-family originations with a rate, slimmed to the matching fields."""
    p = CACHE / "hmda" / f"{county}_{year}.json"
    if p.exists() and not force:
        return p
    raw = _get(f"https://ffiec.cfpb.gov/v2/data-browser-api/view/csv?counties={county}&years={year}&actions_taken=1")
    text = raw.decode("utf-8", "replace")
    if not text.startswith("activity_year"):
        raise DownloadFailed(f"HMDA {county} {year} did not download ({text[:120]!r})")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if (r["lien_status"] != "1" or r["open-end_line_of_credit"] != "2"
                or not r["derived_dwelling_category"].startswith("Single Family")
                or r["interest_rate"] in ("", "NA", "Exempt") or r["loan_amount"] in ("", "NA")):
            continue
        rows.append({k: r.get(k) for k in HMDA_KEEP})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows), encoding="utf-8")
    logger.info("HMDA %s %d: %d first-lien single-family loans with a rate", county, year, len(rows))
    return p


def tract_ids(lat: float, lon: float, cache: dict) -> dict:
    """Both census tract ids for a point: 2020 tracts (HMDA 2022 on) and 2010 tracts (HMDA 2018-2021)."""
    k = f"{lat:.6f},{lon:.6f}"
    if k in cache:
        return cache[k]
    out = {}
    for label, bench, vint in (("t2020", "Public_AR_Current", "Census2020_Current"),
                               ("t2010", "Public_AR_Census2020", "Census2010_Census2020")):
        url = (f"https://geocoding.geo.census.gov/geocoder/geographies/coordinates?x={lon}&y={lat}"
               f"&benchmark={bench}&vintage={vint}&layers=Census%20Tracts&format=json")
        for attempt in range(3):
            try:
                js = json.loads(_get(url, 60))
                tr = (js.get("result", {}).get("geographies", {}).get("Census Tracts") or [{}])[0]
                out[label] = tr.get("GEOID")
                break
            except (ValueError, subprocess.CalledProcessError):
                time.sleep(2 * (attempt + 1))
    cache[k] = out
    return out


# --------------------------------------------------------------------------------------------- the model

class Rates:
    """Freddie Mac weekly rates, looked up as of a date."""

    def __init__(self, path: Path):
        self.d, self.r30, self.r15 = [], [], []
        for row in csv.DictReader(path.read_text(encoding="utf-8").splitlines()):
            try:
                d = date.fromisoformat(row["observation_date"])
                r30 = float(row["MORTGAGE30US"])
            except (ValueError, KeyError):
                continue
            r15 = row.get("MORTGAGE15US")
            self.d.append(d)
            self.r30.append(r30)
            self.r15.append(float(r15) if r15 not in (None, "", ".") else None)

    def at(self, d: date, term_months: int = 360) -> Optional[float]:
        i = bisect_right(self.d, d) - 1
        if i < 0:
            return None
        if term_months <= 180:
            for j in range(i, -1, -1):
                if self.r15[j] is not None:
                    return self.r15[j]
        return self.r30[i]

    def year_mean(self, year: int, term_months: int = 360) -> Optional[float]:
        vals = [(self.r15[i] if term_months <= 180 else self.r30[i]) for i, d in enumerate(self.d) if d.year == year]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def latest(self) -> tuple[date, float]:
        return self.d[-1], self.r30[-1]


def amount_band(amount: float) -> int:
    """HMDA publishes the loan amount as the midpoint of its $10,000 band."""
    return int(amount // 10_000) * 10_000 + 5_000


def snap_term(months: Optional[float]) -> int:
    if not months or months <= 0:
        return 360
    return min(TERMS, key=lambda t: abs(t - months))


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def payment(principal: float, rate_pct: float, n: int) -> float:
    r = rate_pct / 100 / 12
    return principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)


def balance_after(principal: float, rate_pct: float, n: int, k: int) -> float:
    """What is owed after k payments on a fixed loan."""
    if k <= 0:
        return principal
    if k >= n:
        return 0.0
    r = rate_pct / 100 / 12
    if r == 0:
        return principal * (1 - k / n)
    pmt = payment(principal, rate_pct, n)
    return principal * (1 + r) ** k - pmt * ((1 + r) ** k - 1) / r


class Model:
    def __init__(self, counties: list[str], rates: Rates):
        self.rates = rates
        self.idx: dict[tuple, list[dict]] = {}
        by_cell: dict[tuple, list[float]] = {}
        for c in counties:
            for y in HMDA_YEARS:
                p = CACHE / "hmda" / f"{c}_{y}.json"
                if not p.exists():
                    continue
                for h in json.loads(p.read_text(encoding="utf-8")):
                    try:
                        rate, amt = float(h["interest_rate"]), float(h["loan_amount"])
                        term, typ = int(float(h["loan_term"])), int(h["loan_type"])
                    except (TypeError, ValueError):
                        continue
                    key = (int(h["activity_year"]), h["census_tract"], typ, int(amt), snap_term(term))
                    self.idx.setdefault(key, []).append({"rate": rate, "ltv": h.get("loan_to_value_ratio"),
                                                         "purpose": h.get("loan_purpose"),
                                                         "arm": h.get("intro_rate_period") not in (None, "", "NA")})
                    base = rates.year_mean(int(h["activity_year"]), term)
                    if base is not None:
                        by_cell.setdefault((int(h["activity_year"]), typ, 180 if term <= 180 else 360), []).append(rate - base)
        # The spread over Freddie Mac by year x loan type x 15/30, median, from at least 30 loans.
        self.spread = {k: round(median(v), 3) for k, v in by_cell.items() if len(v) >= 30}
        early = {}
        for (y, t, b), s in self.spread.items():
            if y in (2018, 2019):
                early.setdefault((t, b), []).append(s)
        self.spread_pre2018 = {k: round(median(v), 3) for k, v in early.items()}
        self.hmda_loans = sum(map(len, self.idx.values()))

        self.dated: dict[tuple, float] = {}
        self.lock_days = LOCK_DAYS
        self.method = "pmms"        # what prices a loan with no HMDA match; chosen by choose_fallback()

    def pmms_rate(self, recorded: date, typ: int, term: int) -> Optional[float]:
        base = self.rates.at(recorded - timedelta(days=self.lock_days), term)
        return None if base is None else round(base + self.spread_for(recorded.year, typ, term), 3)

    def choose_fallback(self, matches: list[dict], lags=(0, 15, 30, 45, 60, 90)) -> dict:
        """Let the exact HMDA matches pick how unmatched loans are priced: the lock lag first, then Freddie Mac
        + spread vs the index's own estimate vs the average of the two, by median miss against the real rate.
        Chosen on the same loans it is scored on, but it is one pick among a handful, so the score barely flatters."""
        def miss(preds):
            e = sorted(abs(p - mt["rate"]) for p, mt in zip(preds, matches) if p is not None)
            return {"n": len(e), "median_abs": round(median(e), 3) if e else None,
                    "within_quarter": round(sum(1 for x in e if x <= 0.25) / len(e), 3) if e else None}
        by_lag = {}
        for lag in lags:
            self.lock_days = lag
            self.calibrate(matches)
            by_lag[lag] = miss([self.pmms_rate(mt["recorded"], mt["typ"], mt["term"]) for mt in matches])
        best = min((l for l in by_lag if by_lag[l]["median_abs"] is not None), key=lambda l: by_lag[l]["median_abs"])
        self.lock_days = best
        self.calibrate(matches)
        pm = [self.pmms_rate(mt["recorded"], mt["typ"], mt["term"]) for mt in matches]
        ix = [mt.get("index_rate") for mt in matches]
        bl = [(p + i) / 2 if p is not None and i is not None else None for p, i in zip(pm, ix)]
        methods = {"pmms": miss(pm), "index": miss(ix), "blend": miss(bl)}
        self.method = min((k for k in methods if methods[k]["median_abs"] is not None),
                          key=lambda k: methods[k]["median_abs"])
        return {"lock_days": best, "by_lag": by_lag, "methods": methods, "chosen": self.method}

    def calibrate(self, matches: list[dict]) -> dict:
        """Re-fit the spread from loans matched one-to-one in HMDA. They carry a real recording date and a real
        rate, so the gap to Freddie Mac in the lock week is measured, not inferred from a year average (2022
        rose from about 3% to 7% and its volume was front-loaded, which skews any year-average spread). Cells
        need 12 loans: (year, type, 15/30) first, then (type, 15/30) across years."""
        cell: dict[tuple, list[float]] = {}
        for mt in matches:
            base = self.rates.at(mt["recorded"] - timedelta(days=self.lock_days), mt["term"])
            if base is None:
                continue
            gap = mt["rate"] - base
            b = 180 if mt["term"] <= 180 else 360
            cell.setdefault((mt["recorded"].year, mt["typ"], b), []).append(gap)
            cell.setdefault((None, mt["typ"], b), []).append(gap)
        self.dated = {k: round(median(v), 3) for k, v in cell.items() if len(v) >= 12}
        return {str(k): v for k, v in self.dated.items()}

    def spread_for(self, year: int, typ: int, term: int) -> float:
        b = 180 if term <= 180 else 360
        for k in ((year, typ, b), (None, typ, b)):
            if k in self.dated:
                return self.dated[k]
        if year < 2018:
            return self.spread_pre2018.get((typ, b), self.spread_pre2018.get((1, b), 0.0))
        return self.spread.get((year, typ, b), self.spread.get((year, 1, b), 0.0))

    def hmda_candidates(self, recorded: date, tracts: dict, typ: int, amount: float, term: int) -> list[dict]:
        years = [recorded.year] + ([recorded.year - 1] if recorded.month == 1 else [])
        out = []
        for y in years:
            tr = tracts.get("t2020") if y >= 2022 else tracts.get("t2010")
            if tr:
                out += self.idx.get((y, tr, typ, amount_band(amount), term), [])
        return out

    def loan(self, amount: float, recorded: date, due: Optional[date], loan_type: str, tracts: dict,
             today: Optional[date] = None, index_rate: Optional[float] = None) -> Optional[dict]:
        """One open loan, modeled. None for a loan this model does not price (credit line, construction).
        With no HMDA match the rate comes from `self.method`: Freddie Mac + spread, the index's estimate, or
        their average."""
        today = today or date.today()
        lt = (loan_type or "").strip().lower()
        if lt in UNMODELED or not amount or amount <= 0 or not recorded:
            return None
        typ = TYPE_CODE.get(lt, 1)
        term = snap_term(months_between(recorded, due) if due else None)
        cands = self.hmda_candidates(recorded, tracts or {}, typ, amount, term)
        rates = sorted(c["rate"] for c in cands)
        ltv = None
        if len(rates) == 1:
            rate, source = rates[0], "HMDA match"
            ltv = cands[0].get("ltv")
        elif rates and rates[-1] - rates[0] <= 0.25:
            rate, source = median(rates), f"HMDA match ({len(rates)} loans)"
        else:
            pm = self.pmms_rate(recorded, typ, term)
            if self.method == "index" and index_rate:
                rate, source = float(index_rate), "index estimate"
            elif self.method == "blend" and index_rate and pm is not None:
                rate, source = round((pm + float(index_rate)) / 2, 3), "average of index and Freddie Mac"
            elif pm is not None:
                # The spread is measured in this market's own HMDA; with none cached it is zero, so say so.
                rate = pm
                source = "Freddie Mac week + local spread" if (self.spread or self.dated) else "Freddie Mac week"
            elif index_rate:
                rate, source = float(index_rate), "index estimate"
            else:
                return None
        # The first payment is due about two months after recording.
        k = max(0, months_between(recorded, today) - 1)
        pi = payment(amount, rate, term)
        bal = balance_after(amount, rate, term, k)
        mi, mi_note = mortgage_insurance(typ, amount, bal, recorded, ltv)
        return {"rate": round(rate, 3), "rate_source": source, "hmda_candidates": len(rates),
                "term_months": term, "payments_made": min(k, term),
                "pi": round(pi), "balance": round(bal), "mi": round(mi), "mi_note": mi_note,
                "paid_off": bal <= 0, "original": round(amount)}


def mortgage_insurance(typ: int, original: float, balance: float, recorded: date, ltv) -> tuple[float, str]:
    """Monthly mortgage insurance. FHA annual premium is 0.55% for loans after 2023-03-20, 0.85% before, and
    stays for the life of a low-down-payment loan. USDA's annual fee is 0.35% (0.50% before Oct 2016). VA has
    none. A conventional loan carries PMI only when HMDA shows it started above 80% of value; without that we
    cannot know and say so."""
    if typ == 2:
        pct = 0.0055 if recorded >= date(2023, 3, 20) else 0.0085
        return balance * pct / 12, f"FHA premium {pct:.2%} a year"
    if typ == 4:
        pct = 0.0035 if recorded >= date(2016, 10, 1) else 0.005
        return balance * pct / 12, f"USDA fee {pct:.2%} a year"
    if typ == 1:
        try:
            v = float(ltv)
        except (TypeError, ValueError):
            return 0.0, "PMI unknown"
        value0 = original * 100 / v if v else None
        if v > 80 and value0 and balance > 0.78 * value0:    # PMI cancels at 78% of the original value
            return original * 0.005 / 12, f"PMI (started at {v:.0f}% of value)"
        return 0.0, ""
    return 0.0, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--counties", default="47093")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    counties = [c.strip() for c in a.counties.split(",") if c.strip()]
    if a.fetch:
        fetch_pmms(a.force)
        for c in counties:
            for y in HMDA_YEARS:
                try:
                    fetch_hmda(c, y, a.force)
                except DownloadFailed as e:
                    logger.warning("%s", e)
    m = Model(counties, Rates(fetch_pmms()))
    d, r = m.rates.latest()
    print(f"Freddie Mac 30-year {r:.2f}% on {d}; {m.hmda_loans:,} HMDA loans indexed; {len(m.spread)} spread cells")
    for (y, t, b), s in sorted(m.spread.items()):
        if b == 360:
            print(f"  {y} {['', 'Conv', 'FHA', 'VA', 'USDA'][t]:5s} 30-yr spread over Freddie Mac {s:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
