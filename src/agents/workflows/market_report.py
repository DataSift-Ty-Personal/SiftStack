"""market_report: one county's Market Finder numbers, scored, and built into the county workbook.

Atlas's workflow (team/roster.yaml). A county and a state in; out come the Market Finder file (the
same JSON the browser extractor writes), each ZIP's score from market_analyzer on today's weights
(MARKET_SCORER_WEIGHTS picks another set, exactly as on the command line), each neighborhood's
wholesaling score, and the 7-sheet county workbook from county_market_report.

Why it pulls over the API: the old pull drove the Market Finder page in a browser with a password
login. The brain has neither, and DataSift now publishes the Market Map routes, so the pull runs on
the Open API key through the budgeted autopilot client. Its query parameter names are not in the
reference, so the pull checks itself (extract_market_finder.pull_market_finder_api) and a refusal is
reported in plain words with the workstation's browser command beside it, never scored.

Read-only and free: it runs the same in dry mode and live, and writes nothing to the account.
"""
from __future__ import annotations

import copy
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import county_market_report as cmr  # noqa: E402
import extract_market_finder as emf  # noqa: E402

CRM = None              # the selftest injects a fake; otherwise the budgeted Open API key client
NOTICE_LOADER = None    # the selftest injects a fake; otherwise market_analyzer reads this box's notice CSVs
NAME = "market_report"
TOP = 5


def _setting(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return default


def _crm():
    if CRM is not None:
        return CRM
    from ..autopilot.crm import CRM as Client  # noqa: PLC0415
    return Client()


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def _stars(text: str) -> int:
    return str(text or "").count(chr(0x2605))


def score_zips(sift: dict, county: str) -> dict:
    """Every ZIP the pull returned, scored by market_analyzer. Today's weights rank on our own notices
    (distress, equity, tax, days on market); the taught set ranks on Market Finder's investor sales.
    When this box holds no notices for the county, today's weights give every ZIP the same score, and
    a table that looks ranked but is not would be read as a finding. The note says so, and the taught
    order sits beside it because it is the only order these numbers support."""
    import market_analyzer as ma  # noqa: PLC0415
    loader = NOTICE_LOADER or ma._load_notice_data
    profiles = loader([county])
    notices = sum(int(getattr(p, "total_notices", 0) or 0) for p in profiles.values())
    rows = {}
    for r in sift.get("zip_data") or []:
        z = str(r.get("zip_code") or "").strip()[:5]
        if len(z) == 5 and z.isdigit():
            rows[z] = dict(r, county=county)
    ma.apply_market_finder(profiles, rows, add_missing=True)
    weights = ma.weights_name(None)
    today = ma.score_zip_codes(copy.deepcopy(profiles), weights=weights)
    taught = {z.zip_code: z for z in ma.score_zip_codes(copy.deepcopy(profiles), weights="taught")}
    out = []
    for z in today:
        t = taught.get(z.zip_code)
        out.append({"zip": z.zip_code, "rank": z.rank, "score": round(z.score, 1), "grade": z.grade,
                    "left_out": z.left_out, "flags": "; ".join(z.flags),
                    "taught_rank": (t.rank or "") if t else "", "taught_score": round(t.score, 1) if t else "",
                    "taught_left_out": t.left_out if t else "", "taught_flags": "; ".join(t.flags) if t else "",
                    "investor_sales_6mo": z.investor_purchase_count, "months_inventory": z.months_inventory,
                    "notices": z.total_notices})
    note = ""
    if not notices:
        note = ("%s weights rank on our own notices, and this box holds none for %s, so every ZIP scored the "
                "same. The taught order (investor sales first) is in the scores file." % (weights.capitalize(), county))
    return {"weights": weights, "notices": notices, "rows": out, "note": note}


def rank_neighborhoods(sift: dict, fred: int) -> list[dict]:
    """The neighborhoods in the workbook's own order (investor sales, most first) with its wholesaling
    score, counted as stars out of 5."""
    out = []
    for r in sorted(sift.get("neighborhood_data") or [], key=lambda v: v.get("total_inv_trans_6mo") or 0, reverse=True):
        inv, dom, value = r.get("total_inv_trans_6mo") or 0, r.get("median_days_on_market") or 0, r.get("median_home_value") or 0
        out.append({"neighborhood": r.get("neighborhood") or "", "investor_sales_6mo": inv,
                    "median_days_on_market": dom, "median_home_value": value,
                    "homes_on_market": r.get("homes_on_market") or 0,
                    "homes_sold_last_month": r.get("homes_sold_last_month") or 0,
                    "score_of_5": _stars(cmr.wholesaling_score(inv, dom, value, fred))})
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _slug(s: str) -> str:
    return "_".join(p for p in "".join(c if c.isalnum() else " " for c in str(s)).split() if p)


def run(task: dict) -> dict:
    inputs = store.payload_of(task).get("inputs") or {}
    county = str(inputs.get("county") or "").strip()
    state = str(inputs.get("state") or "").strip()
    folder = _folder(task)
    if not county or not state:
        return {"ok": False, "problem": "a market report needs a county and a state (Knox, TN)", "run_dir": str(folder)}
    crm = _crm()
    sift = emf.pull_market_finder_api(state, county, lambda path: crm.call(path))
    base = {"county": county, "state": state, "run_dir": str(folder), "checks": sift.get("api_checks") or {}}
    if not sift.get("success"):
        return dict(base, ok=False, problem=sift.get("problem") or "Market Finder returned no ZIP rows")
    files = folder / "files"
    st = (sift["api_checks"].get("state_abbreviation") or state).upper()
    name = sift["api_checks"].get("county") or county
    fred = _setting("MARKET_REPORT_FRED_DOM", 52)
    made = [emf.save_result(sift, files).name]
    scores = score_zips(sift, name)
    hoods = rank_neighborhoods(sift, fred)
    stem = "%s_%s" % (_slug(name), st)
    _write_csv(files / ("%s_zip_scores.csv" % stem), scores["rows"])
    made.append("%s_zip_scores.csv" % stem)
    _write_csv(files / ("%s_neighborhoods.csv" % stem), hoods)
    made.append("%s_neighborhoods.csv" % stem)
    book = "%s_Market_Report_%s.xlsx" % (stem, datetime.now().strftime("%Y%m%d"))
    cmr.generate_report(sift, {}, name, st, fred, files / book)
    made.append(book)
    by_sales = sorted(sift["zip_data"], key=lambda v: v.get("total_inv_trans_6mo") or 0, reverse=True)
    return dict(base, ok=True, county=name, state=st, zips=sift["zip_count"], neighborhoods=sift["neighborhood_count"],
                weights=scores["weights"], notices=scores["notices"], note=scores["note"], files=made,
                top_zips=[(r["zip_code"], r.get("total_inv_trans_6mo") or 0) for r in by_sales[:TOP]],
                top_neighborhoods=[(h["neighborhood"], h["investor_sales_6mo"], h["score_of_5"]) for h in hoods[:TOP]],
                top_scored=[(r["zip"], r["score"], r["grade"]) for r in scores["rows"][:TOP] if not r["left_out"]])


def render(task: dict, result: dict) -> str:
    where = "%s %s" % (result.get("county") or "", result.get("state") or "")
    if not result.get("ok"):
        return ("Market report for %s did not run: %s\nOn a workstation the browser pull still works: "
                "python src/extract_market_finder.py --state %s --county %s"
                % (where.strip(), result.get("problem") or "no reason given",
                   result.get("state") or "<state>", result.get("county") or "<county>"))
    lines = ["Market report, %s County %s: %d ZIP codes and %d neighborhoods from Market Finder." % (
        result["county"], result["state"], result["zips"], result["neighborhoods"])]
    if result.get("top_zips"):
        lines.append("Most investor sales in 6 months: " + ", ".join("%s (%d)" % z for z in result["top_zips"]) + ".")
    if result.get("top_neighborhoods"):
        lines.append("Neighborhoods: " + ", ".join("%s (%d, %d of 5)" % n for n in result["top_neighborhoods"]) + ".")
    if result.get("note"):
        lines.append(result["note"])
    elif result.get("top_scored"):
        lines.append("%s weights rank first: %s." % (str(result.get("weights") or "today").capitalize(),
                                                     ", ".join("%s (%s, %s)" % s for s in result["top_scored"][:3])))
    links = [(n, slack.file_url(task["id"], n)) for n in result.get("files") or []]
    shown = [("%s: %s" % (n, u)) if u else n for n, u in links if n.endswith((".xlsx", ".csv"))]
    if shown:
        lines.append("Files: " + "; ".join(shown))
    return "\n".join(lines)
