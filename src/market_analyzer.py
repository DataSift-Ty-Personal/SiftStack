"""Market analysis, zip code scoring, and Market Finder reports.

Analyzes county-level data to identify target zip codes for investment.
Scores each zip by distress density, property values, equity, and competition.

Data sources:
  - Our own scraped notice data (aggregated by zip)
  - Zillow API (property values via OpenWeb Ninja)
  - Knox County Tax API (delinquency density)

Usage:
  python src/main.py market-analysis --counties Knox,Blount
  python src/main.py market-analysis --counties Knox --zip-codes 37918,37919,37920

Two named weight sets (build plan 5.2, decision D26, not decided yet):
  today   the six weights below, exactly as they have always scored. This is the default.
  taught  what the lessons teach: MORE investor sales in a ZIP ranks it HIGHER (it is where deals
          get bought and where a contract can be sold), with the guards the lessons put beside the
          count. Chosen by name: weights="taught", or MARKET_SCORER_WEIGHTS=taught in the environment.
The taught set needs investor sales per ZIP, which the notice CSVs do not carry: hand it a Market
Finder file (market_finder=<json>, or MARKET_FINDER_JSON). Before and after, side by side:

  python src/market_analyzer.py --compare --market-finder output/market_finder_<...>.json
"""

import copy
import csv
import glob
import json
import logging
import os
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import config

logger = logging.getLogger(__name__)

# ── Knox/Blount county zip codes ──────────────────────────────────────
KNOX_ZIPS = [
    "37901", "37902", "37909", "37912", "37914", "37915", "37916", "37917",
    "37918", "37919", "37920", "37921", "37922", "37923", "37924", "37931",
    "37932", "37934", "37938",
]
BLOUNT_ZIPS = [
    "37801", "37803", "37804", "37853", "37882", "37886",
]
COUNTY_ZIPS = {
    "knox": KNOX_ZIPS,
    "blount": BLOUNT_ZIPS,
}

# ── Scoring weights ───────────────────────────────────────────────────
WEIGHT_DISTRESS_DENSITY = 0.30   # Foreclosure/tax sale/probate count per zip
WEIGHT_MEDIAN_VALUE = 0.20       # Lower median = better for investors
WEIGHT_EQUITY_AVG = 0.15         # Higher equity = more room for deals
WEIGHT_TAX_DELINQUENCY = 0.15   # More delinquency = more distress
WEIGHT_COMPETITION = 0.10        # Lower investor activity = less competition
WEIGHT_DOM_AVG = 0.10            # Higher DOM = more negotiating leverage

# ── Named weight sets (decision D26) ──────────────────────────────────
# Each factor: (label, weight, the ZipProfile field it reads, higher is better).
# "today" restates the six constants above and must never drift from them: the default path does
# not even go through this table (see score_zip_codes), and a test holds the two to the same number.
# "taught" turns the investor factor round and makes it the largest. Median value stops being
# "lower is better" there (a $300,000 ZIP carries a bigger fee than a $150,000 one); it becomes a
# guard instead: the median has to sit inside the value band. Demand is months of inventory.
WEIGHT_SETS = {
    "today": {
        "guards": False,
        "factors": [
            ("distress", WEIGHT_DISTRESS_DENSITY, "total_notices", True),
            ("median value", WEIGHT_MEDIAN_VALUE, "median_value", False),
            ("equity", WEIGHT_EQUITY_AVG, "avg_equity_pct", True),
            ("tax delinquency", WEIGHT_TAX_DELINQUENCY, "avg_tax_delinquent_amount", True),
            ("investor activity", WEIGHT_COMPETITION, "competition_ratio", False),
            ("days on market", WEIGHT_DOM_AVG, "avg_dom", True),
        ],
    },
    "taught": {
        "guards": True,
        "factors": [
            ("investor activity", 0.50, "investor_purchase_count", True),
            ("distress", 0.20, "total_notices", True),
            ("equity", 0.10, "avg_equity_pct", True),
            ("tax delinquency", 0.10, "avg_tax_delinquent_amount", True),
            ("demand", 0.10, "months_inventory", False),
        ],
    },
}
DEFAULT_WEIGHTS = "today"
WEIGHTS_ENV = "MARKET_SCORER_WEIGHTS"
MARKET_FINDER_ENV = "MARKET_FINDER_JSON"

# ── Guards beside the investor count (lessons read 2026-11-13) ────────
VALUE_BAND = (100_000, 700_000)   # the median has to sit inside the buy box
IMPLAUSIBLE_SALE_FLOOR = 10_000   # a house does not sell for $1,000: that median is tax sales or auctions
IMPLAUSIBLE_SALE_RATIO = 0.20     # or a median sale under a fifth of the median value
STRONG_MONTHS = 3.5               # about three months of inventory reads as strong demand


def weights_name(name: str | None = None) -> str:
    """The weight set to score with: the one asked for, else the environment's, else today's."""
    chosen = (name or os.environ.get(WEIGHTS_ENV) or DEFAULT_WEIGHTS).strip().lower()
    if chosen not in WEIGHT_SETS:
        raise ValueError("unknown weight set %r (use %s)" % (chosen, " or ".join(sorted(WEIGHT_SETS))))
    return chosen


# ── Data structures ───────────────────────────────────────────────────


@dataclass
class ZipProfile:
    """Profile for a single zip code."""
    zip_code: str = ""
    county: str = ""
    # Notice/distress data from our scraped records
    total_notices: int = 0
    foreclosure_count: int = 0
    tax_sale_count: int = 0
    tax_delinquent_count: int = 0
    probate_count: int = 0
    eviction_count: int = 0
    code_violation_count: int = 0
    # Property data (aggregated from Zillow)
    median_value: float = 0.0
    avg_equity_pct: float = 0.0
    avg_dom: float = 0.0
    property_count: int = 0
    # Tax delinquency data
    avg_tax_delinquent_amount: float = 0.0
    tax_delinquent_property_count: int = 0
    # Competition (investor activity)
    investor_purchase_count: int = 0
    competition_ratio: float = 0.0  # investor purchases / total sales
    # Calculated score
    score: float = 0.0
    rank: int = 0
    grade: str = ""  # A, B, C, D
    # Market Finder's own numbers for the ZIP, when a file was handed in (the taught weight set and
    # the guards read these; today's weights never do)
    median_sale_price: float = 0.0
    market_median_value: float = 0.0
    homes_on_market: int = 0
    homes_sold_last_month: int = 0
    months_inventory: float | None = None   # homes on the market / homes sold last month
    flags: list = field(default_factory=list)   # why a ZIP ranks under the others
    left_out: str = ""                           # why a ZIP is not ranked at all


@dataclass
class MarketReport:
    """Complete market analysis report."""
    county: str = ""
    analysis_date: str = ""
    total_zips: int = 0
    zip_profiles: list = field(default_factory=list)
    top_zips: list = field(default_factory=list)
    total_notices: int = 0
    avg_median_value: float = 0.0
    avg_equity: float = 0.0
    total_distress: int = 0
    weights: str = DEFAULT_WEIGHTS
    left_out: list = field(default_factory=list)   # ZIPs a guard kept out of the ranking
    notes: list = field(default_factory=list)


# ── Data aggregation from our CSVs ───────────────────────────────────

def _load_notice_data(counties: list[str] | None = None) -> dict[str, ZipProfile]:
    """Aggregate notice data from our output CSVs by zip code."""
    profiles = defaultdict(lambda: ZipProfile())

    csv_files = list(config.OUTPUT_DIR.glob("*.csv"))
    if not csv_files:
        logger.warning("No CSV files found in %s", config.OUTPUT_DIR)
        return dict(profiles)

    county_filter = {c.lower() for c in counties} if counties else None

    for csv_path in csv_files:
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    zip_code = (row.get("zip") or row.get("ZIP") or "").strip()[:5]
                    if not zip_code or not zip_code.isdigit():
                        continue

                    county = (row.get("county") or row.get("County") or "").strip().lower()
                    if county_filter and county not in county_filter:
                        continue

                    notice_type = (row.get("notice_type") or row.get("Notice Type") or "").strip().lower()

                    profile = profiles[zip_code]
                    profile.zip_code = zip_code
                    profile.county = county.title()
                    profile.total_notices += 1

                    type_map = {
                        "foreclosure": "foreclosure_count",
                        "tax_sale": "tax_sale_count",
                        "tax_delinquent": "tax_delinquent_count",
                        "probate": "probate_count",
                        "eviction": "eviction_count",
                        "code_violation": "code_violation_count",
                    }
                    attr = type_map.get(notice_type)
                    if attr:
                        setattr(profile, attr, getattr(profile, attr) + 1)

                    # Aggregate property data
                    est_val = row.get("estimated_value") or row.get("Estimated Value") or ""
                    if est_val:
                        try:
                            val = float(est_val.replace(",", "").replace("$", ""))
                            if val > 0:
                                # Running average
                                old_count = profile.property_count
                                profile.property_count += 1
                                profile.median_value = (
                                    (profile.median_value * old_count + val) / profile.property_count
                                )
                        except ValueError:
                            pass

                    equity_pct = row.get("equity_percent") or row.get("Equity Percentage") or ""
                    if equity_pct:
                        try:
                            pct = float(equity_pct.replace("%", ""))
                            if profile.avg_equity_pct:
                                profile.avg_equity_pct = (profile.avg_equity_pct + pct) / 2
                            else:
                                profile.avg_equity_pct = pct
                        except ValueError:
                            pass

                    tax_amt = row.get("tax_delinquent_amount") or ""
                    if tax_amt:
                        try:
                            amt = float(tax_amt.replace(",", "").replace("$", ""))
                            if amt > 0:
                                profile.tax_delinquent_property_count += 1
                                old = profile.avg_tax_delinquent_amount
                                cnt = profile.tax_delinquent_property_count
                                profile.avg_tax_delinquent_amount = (
                                    (old * (cnt - 1) + amt) / cnt
                                )
                        except ValueError:
                            pass

        except Exception as e:
            logger.debug("Error reading %s: %s", csv_path, e)

    return dict(profiles)


# ── Scoring engine ────────────────────────────────────────────────────

def _normalize(values: list[float], higher_is_better: bool = True) -> list[float]:
    """Normalize values to 0-100 scale."""
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mn == mx:
        return [50.0] * len(values)
    normed = [(v - mn) / (mx - mn) * 100 for v in values]
    if not higher_is_better:
        normed = [100 - n for n in normed]
    return normed


def score_zip_codes(profiles: dict[str, ZipProfile], weights: str | None = None,
                    guards: bool | None = None, band: tuple | None = None) -> list[ZipProfile]:
    """Score and rank zip codes for investment potential.

    With nothing asked for, this is the scorer as it has always been. `weights` names another
    weight set (WEIGHT_SETS); `guards` switches the guards on or off for it (on by default for
    the taught set, off for today's).
    """
    if not profiles:
        return []
    name = weights_name(weights)
    if guards is None:
        guards = WEIGHT_SETS[name]["guards"]
    if name == DEFAULT_WEIGHTS and not guards:
        return _score_today(profiles)
    return _score_named(profiles, name, guards, band or VALUE_BAND)


def _score_today(profiles: dict[str, ZipProfile]) -> list[ZipProfile]:
    """Today's scoring, kept exactly as written so the default cannot drift."""
    zips = list(profiles.values())

    # Normalize each dimension
    distress = _normalize([z.total_notices for z in zips], higher_is_better=True)
    values = _normalize([z.median_value for z in zips], higher_is_better=False)  # Lower = better
    equity = _normalize([z.avg_equity_pct for z in zips], higher_is_better=True)
    tax_del = _normalize([z.avg_tax_delinquent_amount for z in zips], higher_is_better=True)
    competition = _normalize([z.competition_ratio for z in zips], higher_is_better=False)  # Lower = better
    dom = _normalize([z.avg_dom for z in zips], higher_is_better=True)  # Higher DOM = more leverage

    for i, z in enumerate(zips):
        z.score = (
            distress[i] * WEIGHT_DISTRESS_DENSITY +
            values[i] * WEIGHT_MEDIAN_VALUE +
            equity[i] * WEIGHT_EQUITY_AVG +
            tax_del[i] * WEIGHT_TAX_DELINQUENCY +
            competition[i] * WEIGHT_COMPETITION +
            dom[i] * WEIGHT_DOM_AVG
        )

    # Sort by score descending
    zips.sort(key=lambda z: z.score, reverse=True)

    # Assign ranks and grades
    for i, z in enumerate(zips):
        z.rank = i + 1
        pct = z.score
        if pct >= 75:
            z.grade = "A"
        elif pct >= 55:
            z.grade = "B"
        elif pct >= 35:
            z.grade = "C"
        else:
            z.grade = "D"

    return zips


def _grade(score: float) -> str:
    return "A" if score >= 75 else "B" if score >= 55 else "C" if score >= 35 else "D"


def _money(v) -> str:
    return "${:,.0f}".format(float(v))


def months_of_inventory(on_market, sold_last_month) -> float | None:
    """Homes on the market divided by homes sold last month. None when either is unknown or
    nothing sold: a ZIP with no sale last month has no pace to divide by, and calling that zero
    months would rank a dead ZIP as the strongest one."""
    try:
        on, sold = float(on_market), float(sold_last_month)
    except (TypeError, ValueError):
        return None
    if sold <= 0 or on < 0:
        return None
    return round(on / sold, 1)


def demand_label(months) -> str:
    if months is None:
        return ""
    return "strong" if months <= STRONG_MONTHS else "%s months" % ("%g" % months)


def zip_guards(z: ZipProfile, band: tuple = VALUE_BAND, any_investor_data: bool = True) -> tuple[str, list]:
    """(why the ZIP is left out, or "", and the flags that put it under the clean ones).

    An investor count is not taken at face value (lesson 3-04):
      * a median sale price a house cannot sell for, beside real investor sales, is a tax sale or
        auction area. Those are bought at the sale, not by calling owners, so the ZIP is left out
        and named, never dropped quietly.
      * the median has to sit inside the value band, or the ZIP is not where the buy box is.
      * a ZIP with no tracked investor sale ranks under every ZIP that has one.
    """
    value = z.market_median_value or z.median_value
    sale = z.median_sale_price
    if sale and sale > 0 and (sale < IMPLAUSIBLE_SALE_FLOOR or (value and sale < IMPLAUSIBLE_SALE_RATIO * value)):
        beside = " beside %d investor sales" % z.investor_purchase_count if z.investor_purchase_count else ""
        against = " against a %s median value" % _money(value) if value else ""
        return ("median sale price %s%s%s: a tax sale or auction area, check it before pulling"
                % (_money(sale), against, beside)), []
    flags = []
    lo, hi = band
    for what, v in (("median value", value), ("median sale price", sale)):
        if v and not lo <= v <= hi:
            flags.append("%s %s is outside the %s to %s band" % (what, _money(v), _money(lo), _money(hi)))
    if any_investor_data and not z.investor_purchase_count:
        flags.append("no tracked investor sales")
    return "", flags


def _score_named(profiles: dict[str, ZipProfile], name: str, guards: bool, band: tuple) -> list[ZipProfile]:
    """Score with a named weight set. Ranked: clean ZIPs by score, then flagged ZIPs by score.
    A ZIP a guard leaves out keeps rank 0 and its reason, and is returned last so a report can
    list it."""
    zips = list(profiles.values())
    any_investor = any(z.investor_purchase_count for z in zips)
    for z in zips:
        if z.months_inventory is None:
            z.months_inventory = months_of_inventory(z.homes_on_market, z.homes_sold_last_month)
        z.left_out, z.flags = zip_guards(z, band, any_investor) if guards else ("", [])
        z.score, z.rank, z.grade = 0.0, 0, ""
    ranked = [z for z in zips if not z.left_out]
    for _label, weight, attr, higher in WEIGHT_SETS[name]["factors"]:
        if not weight:
            continue
        raw = [getattr(z, attr) for z in ranked]
        known = [v for v in raw if v is not None]
        norm = dict(zip([i for i, v in enumerate(raw) if v is not None], _normalize(known, higher_is_better=higher)))
        for i, z in enumerate(ranked):
            z.score += norm.get(i, 50.0) * weight      # a number nobody has scores in the middle, never at an end
    ranked.sort(key=lambda z: (bool(z.flags), -z.score))
    for i, z in enumerate(ranked):
        z.rank, z.grade = i + 1, _grade(z.score)
    return ranked + sorted((z for z in zips if z.left_out), key=lambda z: z.zip_code)


# ── Market Finder numbers per ZIP ─────────────────────────────────────

def load_market_finder(path: str) -> dict[str, dict]:
    """zip -> its Market Finder row, from the JSON extract_market_finder writes (zip_data rows:
    total_inv_trans_6mo, homes_on_market, homes_sold_last_month, median_days_on_market,
    median_home_value, median_sale_price). Several files may be given, separated by commas."""
    out: dict[str, dict] = {}
    for p in [x.strip() for x in str(path or "").split(",") if x.strip()]:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for row in (data.get("zip_data") if isinstance(data, dict) else data) or []:
            z = str(row.get("zip_code") or "").strip()[:5]
            if len(z) == 5 and z.isdigit():
                out[z] = dict(row, county=(data.get("county") if isinstance(data, dict) else "") or row.get("county") or "")
    return out


def apply_market_finder(profiles: dict[str, ZipProfile], rows: dict[str, dict], add_missing: bool = True) -> int:
    """Put each ZIP's Market Finder numbers on its profile. Returns how many ZIPs got them.
    The notice-derived fields (median_value, avg_dom) are left as they were, so today's weights
    read the same inputs whether or not a file was loaded."""
    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    n = 0
    for z, row in rows.items():
        p = profiles.get(z)
        if p is None:
            if not add_missing:
                continue
            p = profiles[z] = ZipProfile(zip_code=z, county=str(row.get("county") or "").split(",")[0].title())
        p.investor_purchase_count = int(num(row.get("total_inv_trans_6mo")))
        p.median_sale_price = float(num(row.get("median_sale_price")))
        p.market_median_value = float(num(row.get("median_home_value")))
        p.homes_on_market = int(num(row.get("homes_on_market")))
        p.homes_sold_last_month = int(num(row.get("homes_sold_last_month")))
        p.months_inventory = months_of_inventory(row.get("homes_on_market"), row.get("homes_sold_last_month"))
        n += 1
    return n


def compare_weight_sets(profiles: dict[str, ZipProfile], top: int = 10) -> dict:
    """The top ZIPs under each weight set, next to their investor sales: the proof decision D26
    asks for. Works on copies, so the profiles handed in are not scored or reordered."""
    out = {}
    for name in ("today", "taught"):
        scored = score_zip_codes(copy.deepcopy(profiles), weights=name)
        out[name] = [{"rank": z.rank, "zip": z.zip_code, "score": round(z.score, 1), "grade": z.grade,
                      "investor_sales_6mo": z.investor_purchase_count, "notices": z.total_notices,
                      "flags": list(z.flags)} for z in scored if not z.left_out][:top]
        if name == "taught":
            out["left_out"] = [{"zip": z.zip_code, "why": z.left_out} for z in scored if z.left_out]
    return out


# ── Budget allocation ─────────────────────────────────────────────────

def _allocate_budget(zips: list[ZipProfile], monthly_budget: float = 5000.0,
                     max_zips: int = 5) -> list[tuple[str, float, str]]:
    """Allocate marketing budget across top zip codes by score weight."""
    top = zips[:max_zips]
    if not top:
        return []

    total_score = sum(z.score for z in top)
    if total_score == 0:
        equal = monthly_budget / len(top)
        return [(z.zip_code, round(equal), z.grade) for z in top]

    return [
        (z.zip_code, round(monthly_budget * z.score / total_score), z.grade)
        for z in top
    ]


# ── Excel report ──────────────────────────────────────────────────────

_HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_TITLE_FONT = Font(name="Calibri", bold=True, size=16, color="2F5496")
_SUBTITLE_FONT = Font(name="Calibri", bold=True, size=12, color="333333")
_LABEL_FONT = Font(name="Calibri", size=11, color="555555")
_VALUE_FONT = Font(name="Calibri", bold=True, size=13, color="222222")
_GRADE_COLORS = {
    "A": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "B": PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid"),
    "C": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
    "D": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
}
_THIN_BORDER = Border(bottom=Side(style="thin", color="D9D9D9"))
_MONEY_FMT = '#,##0'


def _write_headers(ws, row, headers):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _auto_widths(ws, min_w=12, max_w=30):
    for col in ws.columns:
        mx = max((len(str(c.value or "")) for c in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max(mx + 2, min_w), max_w)


def generate_market_report(report: MarketReport, budget: list[tuple],
                           output_path: str = "") -> str:
    """Generate a 5-tab Market Finder Excel workbook."""
    wb = Workbook()

    # ── Tab 1: Executive Summary ──────────────────────────────────
    ws = wb.active
    ws.title = "Executive Summary"
    ws.cell(row=1, column=1, value="Market Analysis Report").font = _TITLE_FONT
    ws.cell(row=2, column=1, value=f"County: {report.county}").font = _SUBTITLE_FONT
    ws.cell(row=3, column=1, value=f"Date: {report.analysis_date}").font = _LABEL_FONT
    taught = report.weights != DEFAULT_WEIGHTS
    if taught:
        ws.cell(row=4, column=1, value="Weight set: %s. %s" % (report.weights, " ".join(
            "%s %d%%" % (label, round(w * 100)) for label, w, _a, _h in WEIGHT_SETS[report.weights]["factors"]))
        ).font = _LABEL_FONT

    ws.cell(row=5, column=1, value="Top 10 Target Zip Codes").font = _SUBTITLE_FONT
    _write_headers(ws, 6, ["Rank", "ZIP", "County", "Grade", "Score",
                            "Total Notices", "Median Value", "Avg Equity %"])
    for i, z in enumerate(report.top_zips[:10], 7):
        ws.cell(row=i, column=1, value=z.rank)
        ws.cell(row=i, column=2, value=z.zip_code)
        ws.cell(row=i, column=3, value=z.county)
        grade_cell = ws.cell(row=i, column=4, value=z.grade)
        grade_cell.fill = _GRADE_COLORS.get(z.grade, PatternFill())
        ws.cell(row=i, column=5, value=round(z.score, 1))
        ws.cell(row=i, column=6, value=z.total_notices)
        ws.cell(row=i, column=7, value=round(z.median_value)).number_format = _MONEY_FMT
        ws.cell(row=i, column=8, value=f"{z.avg_equity_pct:.0f}%" if z.avg_equity_pct else "")
        for c in range(1, 9):
            ws.cell(row=i, column=c).border = _THIN_BORDER

    summary_row = 18
    summary_data = [
        ("Total Zip Codes Analyzed", str(report.total_zips)),
        ("Total Distress Notices", str(report.total_notices)),
        ("Avg Median Home Value", f"${report.avg_median_value:,.0f}" if report.avg_median_value else "N/A"),
        ("Avg Equity", f"{report.avg_equity:.0f}%" if report.avg_equity else "N/A"),
    ]
    for label, value in summary_data:
        ws.cell(row=summary_row, column=1, value=label).font = _LABEL_FONT
        ws.cell(row=summary_row, column=2, value=value).font = _VALUE_FONT
        summary_row += 1
    for text in report.notes:
        summary_row += 1
        ws.cell(row=summary_row, column=1, value=text).font = _LABEL_FONT

    _auto_widths(ws)

    # ── Tab 2: Zip Scorecard ──────────────────────────────────────
    ws2 = wb.create_sheet("Zip Scorecard")
    ws2.cell(row=1, column=1, value="All Zip Codes: Scored & Ranked").font = _TITLE_FONT
    headers = ["Rank", "ZIP", "County", "Grade", "Score", "Total Notices",
               "Foreclosures", "Tax Sales", "Tax Delinquent", "Probate",
               "Evictions", "Code Violations", "Median Value", "Avg Equity %",
               "Avg Tax Delinquent $", "Properties Analyzed"]
    _write_headers(ws2, 3, headers)
    for i, z in enumerate(report.zip_profiles, 4):
        vals = [z.rank, z.zip_code, z.county, z.grade, round(z.score, 1),
                z.total_notices, z.foreclosure_count, z.tax_sale_count,
                z.tax_delinquent_count, z.probate_count, z.eviction_count,
                z.code_violation_count, round(z.median_value),
                f"{z.avg_equity_pct:.0f}%" if z.avg_equity_pct else "",
                round(z.avg_tax_delinquent_amount), z.property_count]
        for col, val in enumerate(vals, 1):
            cell = ws2.cell(row=i, column=col, value=val)
            if col == 4:
                cell.fill = _GRADE_COLORS.get(val, PatternFill())
            if col == 13:
                cell.number_format = _MONEY_FMT
            cell.border = _THIN_BORDER
    _auto_widths(ws2)

    # ── Tab 3: Distress Density ───────────────────────────────────
    ws3 = wb.create_sheet("Distress Density")
    ws3.cell(row=1, column=1, value="Notice Distribution by Type & ZIP").font = _TITLE_FONT
    _write_headers(ws3, 3, ["ZIP", "County", "Foreclosure", "Tax Sale", "Tax Delinquent",
                             "Probate", "Eviction", "Code Violation", "TOTAL"])
    for i, z in enumerate(sorted(report.zip_profiles, key=lambda x: x.total_notices, reverse=True), 4):
        vals = [z.zip_code, z.county, z.foreclosure_count, z.tax_sale_count,
                z.tax_delinquent_count, z.probate_count, z.eviction_count,
                z.code_violation_count, z.total_notices]
        for col, val in enumerate(vals, 1):
            ws3.cell(row=i, column=col, value=val).border = _THIN_BORDER
    _auto_widths(ws3)

    # ── Tab 4: Competition Map (today) or Investor Activity (taught) ──
    if taught:
        # The taught set reads investor sales the other way round, so this tab must not keep saying
        # "less investor activity = better". It shows what the ranking now rests on, and the guards.
        ws4 = wb.create_sheet("Investor Activity")
        ws4.cell(row=1, column=1, value="Investor Sales by ZIP").font = _TITLE_FONT
        ws4.cell(row=2, column=1,
                 value="More investor sales = more buyers for a contract = ranked higher. "
                       "Flagged ZIPs rank under the clean ones.").font = _LABEL_FONT
        _write_headers(ws4, 4, ["ZIP", "County", "Investor Sales (6 mo)", "A Month", "Months of Inventory",
                                "Demand", "Grade", "Flags"])
        for i, z in enumerate(sorted(report.zip_profiles, key=lambda x: -x.investor_purchase_count), 5):
            vals = [z.zip_code, z.county, z.investor_purchase_count, round(z.investor_purchase_count / 6, 1),
                    z.months_inventory if z.months_inventory is not None else "", demand_label(z.months_inventory),
                    z.grade, "; ".join(z.flags)]
            for col, val in enumerate(vals, 1):
                cell = ws4.cell(row=i, column=col, value=val)
                if col == 7:
                    cell.fill = _GRADE_COLORS.get(val, PatternFill())
                cell.border = _THIN_BORDER
        _auto_widths(ws4, max_w=60)
    else:
        ws4 = wb.create_sheet("Competition Map")
        ws4.cell(row=1, column=1, value="Investor Activity by ZIP").font = _TITLE_FONT
        ws4.cell(row=2, column=1,
                 value="Lower competition ratio = less investor activity = better opportunity").font = _LABEL_FONT
        _write_headers(ws4, 4, ["ZIP", "County", "Investor Purchases", "Competition Ratio", "Grade"])
        for i, z in enumerate(sorted(report.zip_profiles, key=lambda x: x.competition_ratio), 5):
            ws4.cell(row=i, column=1, value=z.zip_code)
            ws4.cell(row=i, column=2, value=z.county)
            ws4.cell(row=i, column=3, value=z.investor_purchase_count)
            ws4.cell(row=i, column=4, value=f"{z.competition_ratio:.1%}" if z.competition_ratio else "N/A")
            ws4.cell(row=i, column=5, value=z.grade).fill = _GRADE_COLORS.get(z.grade, PatternFill())
            for c in range(1, 6):
                ws4.cell(row=i, column=c).border = _THIN_BORDER
        _auto_widths(ws4)

    # ── Tab 5: Budget Recommendations ─────────────────────────────
    ws5 = wb.create_sheet("Recommendations")
    ws5.cell(row=1, column=1, value="Marketing Budget Allocation").font = _TITLE_FONT
    ws5.cell(row=2, column=1, value="Budget weighted by zip score: higher-scoring zips get more spend").font = _LABEL_FONT

    _write_headers(ws5, 4, ["ZIP", "Monthly Budget", "Grade", "% of Total"])
    total_budget = sum(b[1] for b in budget) if budget else 1
    for i, (zip_code, amount, grade) in enumerate(budget, 5):
        ws5.cell(row=i, column=1, value=zip_code)
        ws5.cell(row=i, column=2, value=amount).number_format = _MONEY_FMT
        ws5.cell(row=i, column=3, value=grade).fill = _GRADE_COLORS.get(grade, PatternFill())
        ws5.cell(row=i, column=4, value=f"{amount / total_budget:.0%}")
        for c in range(1, 5):
            ws5.cell(row=i, column=c).border = _THIN_BORDER
    _auto_widths(ws5)

    # ── Tab 6: Left Out (only when a guard kept a ZIP out of the ranking) ──
    if report.left_out:
        ws6 = wb.create_sheet("Left Out")
        ws6.cell(row=1, column=1, value="ZIPs Left Out of the Ranking").font = _TITLE_FONT
        ws6.cell(row=2, column=1, value="Nothing is dropped quietly: each one is here with the reason.").font = _LABEL_FONT
        _write_headers(ws6, 4, ["ZIP", "County", "Investor Sales (6 mo)", "Median Sale Price", "Why"])
        for i, z in enumerate(report.left_out, 5):
            vals = [z.zip_code, z.county, z.investor_purchase_count, round(z.median_sale_price), z.left_out]
            for col, val in enumerate(vals, 1):
                cell = ws6.cell(row=i, column=col, value=val)
                if col == 4:
                    cell.number_format = _MONEY_FMT
                cell.border = _THIN_BORDER
        _auto_widths(ws6, max_w=90)

    # Save
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # A report on another weight set says so in its name, so it is never taken for today's.
        tag = f"{report.weights}_" if taught else ""
        output_path = str(config.OUTPUT_DIR / f"market_analysis_{report.county}_{tag}{timestamp}.xlsx")

    wb.save(output_path)
    logger.info("Market report saved to %s", output_path)
    return output_path


# ── Main entry point ──────────────────────────────────────────────────

def run_market_analysis(counties: list[str] | None = None,
                        zip_codes: list[str] | None = None,
                        monthly_budget: float = 5000.0,
                        output_path: str = "",
                        weights: str | None = None,
                        market_finder: str | None = None,
                        guards: bool | None = None,
                        band: tuple | None = None) -> dict:
    """Run market analysis for specified counties.

    Returns dict with report data and output path. `weights` names the weight set (today's unless
    asked, or MARKET_SCORER_WEIGHTS); `market_finder` is the Market Finder JSON that carries each
    ZIP's investor sales (or MARKET_FINDER_JSON). Neither changes anything unless it is given.
    """
    counties = counties or ["Knox", "Blount"]
    county_str = ", ".join(counties)
    weights = weights_name(weights)
    logger.info("Starting market analysis for: %s", county_str)

    # Step 1: Load and aggregate our own notice data
    profiles = _load_notice_data(counties)

    # If specific zips requested, filter
    if zip_codes:
        profiles = {k: v for k, v in profiles.items() if k in zip_codes}

    # Add known zips that may not have notices yet
    if not zip_codes:
        for county in counties:
            for z in COUNTY_ZIPS.get(county.lower(), []):
                if z not in profiles:
                    profiles[z] = ZipProfile(zip_code=z, county=county.title())

    # Market Finder's numbers per ZIP, when a file was handed in. Only the ZIPs already in the
    # analysis get them: a file for a wider area must not add ZIPs nobody asked about.
    notes = []
    market_finder = market_finder or os.environ.get(MARKET_FINDER_ENV) or ""
    if market_finder:
        got = apply_market_finder(profiles, load_market_finder(market_finder), add_missing=False)
        logger.info("Market Finder numbers loaded for %d of %d zip codes", got, len(profiles))
    if weights != DEFAULT_WEIGHTS and not any(p.investor_purchase_count for p in profiles.values()):
        # A ranking "by investor sales" with no investor sales loaded would still print a tidy top
        # ten. Say it out loud instead.
        notes.append("No investor sales were loaded, so the investor factor scored every ZIP the same. "
                     "Hand in a Market Finder file to rank on it.")
        logger.warning(notes[-1])

    if not profiles:
        logger.warning("No data found for analysis")
        return {"error": "No data found"}

    logger.info("Loaded notice data for %d zip codes", len(profiles))

    # Step 2: Score and rank. A ZIP a guard left out is listed on its own and never ranked.
    everything = score_zip_codes(profiles, weights=weights, guards=guards, band=band)
    scored = [z for z in everything if not z.left_out]
    left_out = [z for z in everything if z.left_out]
    top = scored[:10]

    # Step 3: Budget allocation
    budget = _allocate_budget(scored, monthly_budget)

    # Step 4: Build report
    values = [z.median_value for z in scored if z.median_value > 0]
    equities = [z.avg_equity_pct for z in scored if z.avg_equity_pct > 0]

    report = MarketReport(
        county=county_str,
        analysis_date=datetime.now().strftime("%Y-%m-%d"),
        total_zips=len(scored),
        zip_profiles=scored,
        top_zips=top,
        total_notices=sum(z.total_notices for z in scored),
        avg_median_value=sum(values) / len(values) if values else 0,
        avg_equity=sum(equities) / len(equities) if equities else 0,
        total_distress=sum(z.total_notices for z in scored),
        weights=weights,
        left_out=left_out,
        notes=notes,
    )

    # Step 5: Generate Excel report
    report_path = generate_market_report(report, budget, output_path)

    logger.info("Market analysis complete: %d zips scored, top zip %s (score %.1f, grade %s)",
                len(scored), top[0].zip_code if top else "N/A",
                top[0].score if top else 0, top[0].grade if top else "N/A")
    if left_out:
        logger.info("%d zip(s) left out of the ranking: %s", len(left_out),
                    "; ".join("%s (%s)" % (z.zip_code, z.left_out) for z in left_out[:5]))

    return {
        "report": report,
        "budget": budget,
        "report_path": report_path,
    }


def compare_report(counties: list[str] | None = None, market_finder: str = "", top: int = 10) -> dict:
    """The top ZIPs under today's weights and under the taught ones, from the same inputs. Reads
    local files only and writes nothing: this is the picture to decide D26 on."""
    counties = counties or ["Knox", "Blount"]
    profiles = _load_notice_data(counties)
    for county in counties:
        for z in COUNTY_ZIPS.get(county.lower(), []):
            profiles.setdefault(z, ZipProfile(zip_code=z, county=county.title()))
    loaded = apply_market_finder(profiles, load_market_finder(market_finder), add_missing=False) if market_finder else 0
    out = compare_weight_sets(profiles, top=top)
    out["zips"] = len(profiles)
    out["market_finder_zips"] = loaded
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ZIP scoring: today's weights against the taught ones.")
    ap.add_argument("--compare", action="store_true", help="print the top ZIPs under both weight sets")
    ap.add_argument("--counties", default="Knox,Blount")
    ap.add_argument("--market-finder", default=os.environ.get(MARKET_FINDER_ENV, ""),
                    help="the Market Finder JSON with each ZIP's investor sales")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)
    if not args.compare:
        ap.print_help()
        return 2
    got = compare_report([c.strip() for c in args.counties.split(",") if c.strip()], args.market_finder, args.top)
    print("%d zip codes, Market Finder numbers on %d of them" % (got["zips"], got["market_finder_zips"]))
    if not got["market_finder_zips"]:
        print("No investor sales loaded: the taught order below rests on the other factors only.")
    for name in ("today", "taught"):
        print("")
        print("%s weights" % name)
        print("  rank  zip    score  investor sales (6 mo)  notices  flags")
        for r in got[name]:
            print("  %4d  %s  %5.1f  %21d  %7d  %s" % (r["rank"], r["zip"], r["score"], r["investor_sales_6mo"],
                                                      r["notices"], "; ".join(r["flags"])))
    for r in got.get("left_out") or []:
        print("left out: %s (%s)" % (r["zip"], r["why"]))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
