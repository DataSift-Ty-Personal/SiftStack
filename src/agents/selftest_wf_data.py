"""Atlas's data workflows selftest (wave 3): market report, idle records, list pull, land desk and account
setup, each against fakes. Zero network: a fake Market Finder, a fake DataSift (the budgeted client's
call, rows, record and list_uuid), a fake SiftMap search, quote and add, an invented land analysis and
a small invented blueprint. Every county, street, name, email, number and id here is invented.
"""
from __future__ import annotations

import ast
import copy
import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config

ENV = ("MARKET_FINDER_API_PARAMS", "MARKET_FINDER_API_GROUPS", "MARKET_FINDER_SOURCE", "MARKET_SCORER_WEIGHTS",
       "IDLE_RECORDS_DIR", "IDLE_RECORDS_LISTS", "IDLE_RECORDS_MAX_READS", "IDLE_RECORDS_REFRESH_DAYS",
       "IDLE_RECORDS_TOP", "DREAM100_WEIGHTS", "LIST_PULL_MAX_IDS", "LIST_PULL_PROBE_WAIT_MIN", "LIST_PULL_WAIT_MIN",
       "LAND_DESK_DIR", "LAND_DESK_MAX_MB", "ACCOUNT_SETUP_BLUEPRINT", "ACCOUNT_SETUP_ALLOW_APPLY",
       "ACCOUNT_SETUP_FAMILIES", "ACCOUNT_SETUP_USER_MAP", "REISIFT_API_KEY")
TODAY = date(2026, 11, 13)
_N = [91000]


def task(inputs: dict | None = None, details: str = "") -> dict:
    _N[0] += 1
    return {"id": _N[0], "payload": json.dumps({"inputs": inputs or {}, "details": details})}


def _q(path: str) -> tuple[str, dict]:
    parts = urlsplit(path)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def _sheets(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path)
    return {ws.title: [[c for c in row] for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def run(check, tmp: Path) -> None:
    from . import store
    from .workflows import account_setup, idle_records, land_desk, list_pull, market_report
    keep = (config.MODE, config.DATA_DIR, config.RUNS_DIR)
    env = {k: os.environ.get(k) for k in ENV}
    injected = [(market_report, "CRM"), (market_report, "NOTICE_LOADER"), (idle_records, "CRM"),
                (idle_records, "TODAY"), (list_pull, "CRM"), (list_pull, "MAP"), (land_desk, "TODAY"),
                (account_setup, "CRM"), (account_setup, "MAP")]
    for k in ENV:
        os.environ.pop(k, None)
    config.DATA_DIR, config.RUNS_DIR, config.MODE = tmp / "wfd_data", tmp / "wfd_runs", "dry"
    store.init()
    try:
        _market(check, market_report)
        _idle(check, tmp, idle_records)
        _list_pull(check, list_pull)
        _land(check, tmp, land_desk)
        _account(check, tmp, account_setup)
        _hygiene(check)
    finally:
        config.MODE, config.DATA_DIR, config.RUNS_DIR = keep
        for mod, name in injected:
            setattr(mod, name, None)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# =========================================================================== market report

MF = "/api/internal/market-finder/"


class FakeMarketFinder:
    """The published Market Map routes for one invented county (Alder, TN, code 47901). Switches make it
    misbehave the ways an unpublished parameter name would."""

    def __init__(self, zips=3, ignore_state=False, stray_zip=False, ignore_page=False, echo=None, hood_zips=False,
                 county_param="county"):
        self.zips = ["379%02d" % (90 + i) if i < 10 else "38%03d" % i for i in range(zips)]
        self.ignore_state, self.stray_zip, self.ignore_page = ignore_state, stray_zip, ignore_page
        self.echo, self.hood_zips, self.county_param = echo, hood_zips, county_param
        self.asked, self.methods = [], []

    def _metrics(self, i: int) -> dict:
        return {"num_investor_transactions": 10.0 * (i + 1), "homes_on_market": 40 + i, "number_of_homes_sold": 10 + i,
                "median_days_on_market": 30 + i, "median_property_value": 200000.0 + 1000 * i,
                "median_sale_price": 195000.0 + 1000 * i}

    def call(self, path, method="GET", body=None, headers=None):
        self.asked.append(path)
        self.methods.append(method)
        p, q = _q(path)
        if p == MF + "states/":
            return {"total": 2, "items": [{"name": "Tennessee", "code": "47", "abbreviation": "TN", "type": "state"},
                                          {"name": "Ohio", "code": "39", "abbreviation": "OH", "type": "state"}]}
        if p == MF + "counties/":
            tn = [{"state": "TN", "name": "Alder", "code": "47901", "type": "county"},
                  {"state": "TN", "name": "Birch", "code": "47903", "type": "county"}]
            oh = [{"state": "OH", "name": "Alder", "code": "39901", "type": "county"}]
            rows = tn + oh if (self.ignore_state or q.get("state") != "47") else tn
            return {"total": len(rows), "items": rows}
        county = q.get(self.county_param)
        if p == MF + "counties/zips/":
            return {"total": len(self.zips), "items": [{"zip_code": z, "fips": "47901", "state": "TN"} for z in self.zips]
                    if county == "47901" else []}
        if p == MF + "counties/neighborhoods/":
            return [{"code": "n-1", "name": "Birch Hollow"}, {"code": "n-2", "name": "Cedar Flats"}]
        if p == MF + "metrics/":
            return dict(self._metrics(0), median_property_rental_value=1450.0, gross_rental_yield=0.072,
                        median_monthly_investor_transactions=21.0, num_owners=650.0, num_renters=350.0)
        if p == MF + "paginated/":
            group = q.get("geo_group")
            if group == "zip" or (group == "neighborhood" and self.hood_zips):
                codes = list(self.zips) + (["99999"] if self.stray_zip else [])
            else:
                codes = ["n-1", "n-2"]
            rows = [{"property_fields": {"state": "TN", "code": c}, "metrics": self._metrics(i)} for i, c in enumerate(codes)]
            size, page = int(q.get("limit") or 20), int(q.get("page") or 1)
            start = 0 if self.ignore_page else (page - 1) * size
            return {"data": rows[start:start + size], "total": len(rows), "geoGroup": self.echo or group}
        raise AssertionError("the market report asked an unexpected route: %s %s" % (method, path))


def _market(check, mr) -> None:
    import extract_market_finder as emf
    import market_analyzer as ma
    print("  market report: the API pull")
    f = FakeMarketFinder()
    got = emf.pull_market_finder_api("TN", "Alder", f.call)
    browser_keys = set(emf._row_to_dict([""] * 7, emf.ZIP_COLUMNS))
    check(got["success"] and got["zip_count"] == 3 and got["neighborhood_count"] == 2,
          "the API pull reads every ZIP and neighborhood row for the county")
    check(all(set(r) == browser_keys for r in got["zip_data"]),
          "each ZIP row carries exactly the browser extractor's columns, so the report reads either source")
    z0 = got["zip_data"][0]
    check(z0["median_home_value"] == 200000.0 and z0["median_home_value_raw"] == "$200,000"
          and z0["total_inv_trans_6mo"] == 10 and z0["total_inv_trans_6mo_raw"] == "10",
          "numbers are parsed the way the browser parses the page's own display text")
    check([r["neighborhood"] for r in got["neighborhood_data"]] == ["Birch Hollow", "Cedar Flats"],
          "neighborhood codes are named from the county's own neighborhood listing")
    check(got["summary"].get("market rent") == "$1,450" and got["summary"].get("gross rental yield") == "7.2%",
          "the summary panel is keyed the way the browser keys it")
    check(set(f.methods) == {"GET"} and all(p.startswith(MF) for p in f.asked),
          "the pull only reads, and only the published Market Map routes")
    check(got["source"] == "api" and got["api_checks"]["zip_rows_outside_county"] == 0,
          "the pull says where its numbers came from and what it checked")
    both = emf.pull_market_finder_api("Tennessee", "Alder County", FakeMarketFinder(ignore_state=True).call)
    check(both["success"] and both["api_checks"]["county_code"] == "47901",
          "a state filter the server ignores still finds the county in the state asked for, by full name too")
    bad = emf.pull_market_finder_api("TN", "Alder", FakeMarketFinder(stray_zip=True).call)
    check(not bad["success"] and "not in Alder County" in bad["problem"] and not bad["zip_data"],
          "a ZIP row from outside the county refuses the whole pull: the county filter was ignored")
    big = emf.pull_market_finder_api("TN", "Alder", FakeMarketFinder(zips=150).call)
    check(big["success"] and big["zip_count"] == 150, "every page is read when a county has more rows than a page")
    short = emf.pull_market_finder_api("TN", "Alder", FakeMarketFinder(zips=150, ignore_page=True).call)
    check(not short["success"] and "page parameter" in short["problem"],
          "a page parameter the server ignores is caught by the total, never read as a small county")
    echo = emf.pull_market_finder_api("TN", "Alder", FakeMarketFinder(echo="county").call)
    check(not echo["success"] and "group parameter" in echo["problem"], "rows of another group are refused")
    hz = emf.pull_market_finder_api("TN", "Alder", FakeMarketFinder(hood_zips=True).call)
    check(not hz["success"] and "ZIP codes" in hz["problem"], "a neighborhood listing that returns ZIP codes is refused")
    os.environ["MARKET_FINDER_API_PARAMS"] = json.dumps({"county": "fips"})
    try:
        f2 = FakeMarketFinder(county_param="fips")
        renamed = emf.pull_market_finder_api("TN", "Alder", f2.call)
    finally:
        os.environ.pop("MARKET_FINDER_API_PARAMS", None)
    check(renamed["success"] and any("fips=47901" in p for p in f2.asked),
          "a parameter name can be changed by setting, without a code change")
    nostate = emf.pull_market_finder_api("ZZ", "Alder", FakeMarketFinder().call)
    check(not nostate["success"] and "no state" in nostate["problem"], "an unknown state is said plainly")
    check(emf.default_source() == "browser", "the command line stays on the browser unless MARKET_FINDER_SOURCE=api")
    check(emf.create_browser.__module__ == emf.__name__ and "datasift_core" not in emf.__dict__,
          "the browser helpers load only when the browser path runs")

    print("  market report: the workflow")
    mr.CRM = FakeMarketFinder()
    mr.NOTICE_LOADER = lambda counties: {}
    t = task({"county": "Alder", "state": "TN"})
    res = mr.run(t)
    files = Path(res["run_dir"]) / "files"
    check(res["ok"] and res["zips"] == 3 and res["neighborhoods"] == 2 and res["state"] == "TN",
          "a county and a state in: the report runs on the API pull")
    names = res["files"]
    check(any(n.startswith("market_finder_TN_Alder_") and n.endswith(".json") for n in names)
          and any(n.endswith("_Market_Report_%s.xlsx" % date.today().strftime("%Y%m%d")) for n in names)
          and "Alder_TN_zip_scores.csv" in names and "Alder_TN_neighborhoods.csv" in names,
          "the Market Finder file, the workbook and both score files are made")
    sift = json.loads((files / next(n for n in names if n.endswith(".json"))).read_text(encoding="utf-8"))
    check({"success", "state", "county", "zip_data", "neighborhood_data", "summary", "extracted_at", "zip_count",
           "neighborhood_count"} <= set(sift), "the saved Market Finder file has the browser's own top-level shape")
    book = _sheets(files / next(n for n in names if n.endswith(".xlsx")))
    check(list(book)[:3] == ["Executive Summary", "ZIP Code Analysis", "Neighborhood Analysis"] and len(book) == 7,
          "the county workbook is the 7-sheet report, built with no public-data bundle")
    with (files / "Alder_TN_zip_scores.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    check(len(rows) == 3 and {"rank", "score", "taught_rank", "investor_sales_6mo"} <= set(rows[0]),
          "every ZIP is scored, with the taught order beside today's")
    check(res["weights"] == "today" and "scored the" in res["note"] and "same" in res["note"],
          "today's weights by default; with no notices on the box the report says every ZIP scored the same")
    text = mr.render(t, res)
    check("3 ZIP codes and 2 neighborhoods" in text and "37992 (30)" in text and "scored the same" in text,
          "the Slack text names the counts, the busiest ZIPs and the scoring caveat")
    mr.NOTICE_LOADER = lambda counties: {"37990": ma.ZipProfile(zip_code="37990", county="Alder", total_notices=9,
                                                                median_value=150000.0, avg_equity_pct=60.0)}
    withn = mr.run(task({"county": "Alder", "state": "TN"}))
    check(withn["ok"] and withn["notices"] == 9 and not withn["note"] and withn["top_scored"][0][0] == "37990",
          "with our own notices loaded, today's weights rank on them and no caveat is printed")
    mr.CRM = FakeMarketFinder(stray_zip=True)
    refused = mr.run(task({"county": "Alder", "state": "TN"}))
    rtext = mr.render(task(), refused)
    check(not refused["ok"] and "did not run" in rtext and "--source browser" not in rtext
          and "extract_market_finder.py --state TN --county Alder" in rtext,
          "a refused pull is reported with the workstation's browser command, and nothing is scored")
    check(not list((Path(refused["run_dir"]) / "files").glob("*.xlsx")), "a refused pull builds no workbook")
    missing = mr.run(task({"county": "", "state": "TN"}))
    check(not missing["ok"] and "county and a state" in missing["problem"], "a missing county is asked for, not guessed")


# =========================================================================== idle records

def _obit_rec(u, street, value, obit_day, owner="Gus Example", mail="9 Mail Rd", lists=("Obituary",), **kw):
    first, last = owner.split(" ", 1)
    r = {"uuid": u, "type": "clean", "address": {"street": street, "city": "Exampleton", "state": "TN",
                                                 "postal_code": "37990", "county": "Alder", "vacant": False},
         "owner": {"uuid": "o-" + u, "first_name": first, "last_name": last, "type": "person",
                   "address": {"street": mail, "postal_code": "37991", "city": "Exampleton", "state": "TN"},
                   "phones": [{"number": "8655550301", "tags": ["Dial First"], "type": "MOBILE", "status": "UNKNOWN"}],
                   "emails": []},
         "status": None, "lists": list(lists), "tags": [], "estimate_value": value, "equity_percent": 100,
         "last_obituary_date": obit_day, "structure_type": "Single Family Residential", "mls": "",
         "investor_score": 80, "realtor_score": 20, "year": 1955, "sqft": 1400}
    r.update(kw)
    return r


def _list_rec(u, street, value, owner_uuid, first, last, lists, **kw):
    r = {"uuid": u, "type": "clean", "dataflik_id": 5000 + int(u.split("-")[1]),
         "address": {"street": street, "city": "Exampleton", "state": "TN", "postal_code": "37990", "county": "Alder",
                     "vacant": False},
         "owner": {"uuid": owner_uuid, "type": "person", "first_name": first, "last_name": last, "age": 66,
                   "address": {"street": "5 Mail Rd", "city": "Exampleton", "state": "TN", "postal_code": "37991"},
                   "phones": [{"number": "8655550401", "type": "MOBILE", "tags": ["Dial Second"], "status": "UNKNOWN"}],
                   "emails": []},
         "status": None, "lists": list(lists), "tags": [], "estimate_value": str(value), "equity_percent": "80",
         "last_sale_price": "40000", "last_sold": "2002-01-01", "structure_type": "Single Family Residential",
         "mls": "Off Market", "investor_score": 70, "realtor_score": 30}
    r.update(kw)
    return r


class FakeIdleCRM:
    """Reads only: lists, the records search (by list and property type), one record, its custom fields."""

    def __init__(self, records: dict):
        self.recs = records
        self.list_ids = {"Obituary": "l-obit", "Tired Landlord": "l-tl"}
        self.reads, self.writes = 0, []

    def list_uuid(self, title):
        return next((u for t, u in self.list_ids.items() if t.casefold() == (title or "").strip().casefold()), "")

    def _on(self, list_uuid, ptype="clean"):
        title = next((t for t, u in self.list_ids.items() if u == list_uuid), None)
        return [r for r in self.recs.values() if title in (r.get("lists") or []) and r.get("type", "clean") == ptype]

    def rows(self, must, cap=10000):
        return [{"uuid": r["uuid"]} for r in self._on(must["any_lists"][0], must.get("property_type", "clean"))][:cap]

    def record(self, u):
        self.reads += 1
        return copy.deepcopy(self.recs.get(u) or {})

    def call(self, path, method="GET", body=None, headers=None):
        reading = method == "GET" or (headers or {}).get("x-http-method-override") == "GET"
        if not reading:
            self.writes.append((method, path))
            raise AssertionError("idle_records wrote to DataSift: %s %s" % (method, path))
        p, _ = _q(path)
        if p == "/api/internal/list/":
            return {"results": [{"title": t, "uuid": u} for t, u in self.list_ids.items()]}
        if p == "/api/internal/property/":
            must = body["query"]["must"]
            rows = [{"uuid": r["uuid"], "address": r["address"]} for r in self._on(must["any_lists"][0], must["property_type"])]
            off, lim = body.get("offset", 0), body.get("limit", 100)
            return {"count": len(rows), "results": rows[off:off + lim]}
        if p == "/api/internal/custom-fields/":
            return {"results": []}
        if p.endswith("/custom-field/"):
            self.reads += 1
            return {"results": [{"custom_field": {"label": "Est Equity"}, "value": "120000"}]}
        if p.startswith("/api/internal/property/"):
            return self.record(p.split("/")[4])
        raise AssertionError("unexpected route %s" % path)


def _idle(check, tmp: Path, ir) -> None:
    import dream100_build as build
    import obituary_opportunity as oo
    print("  idle records: the obituary list")
    os.environ["IDLE_RECORDS_DIR"] = str(tmp / "wfd_idle")
    ir.TODAY = TODAY
    recs = {"ob-1": _obit_rec("ob-1", "11 Cedar Rd", 210000, "2025-01-15"),
            "ob-2": _obit_rec("ob-2", "12 Cedar Rd", 160000, "2025-06-01"),
            "ob-3": _obit_rec("ob-3", "13 Cedar Rd", 180000, "2025-03-01")}
    crm = ir.CRM = FakeIdleCRM(recs)
    os.environ["IDLE_RECORDS_MAX_READS"] = "2"
    t = task({"list": "Obituary"})
    r1 = ir.run(t)
    x = r1["lists"][0]
    check(x["engine"] == "obituary" and x["reads"] == 2 and x["not_read_yet"] == 1 and x["failed_reads"] == 0,
          "a capped run reads two of three records and names the third as not read yet, never as a failed read")
    book = _sheets(Path(r1["run_dir"]) / "files" / x["files"][0])
    check("1 Overview" in book and "7 Portfolio owners" in book,
          "the obituary engine's own workbook, with the portfolio owners sheet added after it")
    cells = [c for row in book.get("6 Excluded", []) for c in row if isinstance(c, str)]
    check(any("Not read yet" in c for c in cells) and not any("Could not load" in c for c in cells),
          "the record past the cap shows as not read yet on the Excluded sheet, never as a failed read")
    check(x["portfolio_owners"] == 1 and x["largest_portfolio"] >= 2,
          "an owner holding several obituary records is grouped by name and mailing street")
    before = crm.reads
    r2 = ir.run(task({"list": "Obituary"}))
    check(r2["lists"][0]["not_read_yet"] == 0 and r2["lists"][0]["reads"] == 1 and crm.reads - before == 2,
          "the next run reads only the record left over (its detail and its custom fields)")
    os.environ["IDLE_RECORDS_MAX_READS"] = "10"
    ir.TODAY = TODAY + timedelta(days=10)
    r3 = ir.run(task({"list": "Obituary"}))
    check(r3["lists"][0]["reads"] == 3, "a record older than the refresh age is read again")
    ir.TODAY = TODAY
    check(not crm.writes, "idle records never writes to the account")
    check("Idle records on the Obituary list" in ir.render(t, r1) and "not read yet" in ir.render(t, r1),
          "the Slack text names the list and the records left for the next run")

    print("  idle records: any other list, on the Dream 100 score")
    recs.update({"tl-1": _list_rec("tl-1", "21 Oak Ln", 250000, "own-1", "Ann", "Example", ["Tired Landlord"]),
                 "tl-2": _list_rec("tl-2", "22 Oak Ln", 180000, "own-1", "Ann", "Example", ["Tired Landlord"]),
                 "tl-3": _list_rec("tl-3", "23 Pine Ct", 300000, "own-2", "Bo", "Sample", ["Tired Landlord"]),
                 "tl-4": _list_rec("tl-4", "24 Elm Way", 120000, "own-3", "Cy", "Demo", ["Tired Landlord"], status="sold")})
    r4 = ir.run(task({"list": "Tired Landlord"}))
    y = r4["lists"][0]
    check(y["engine"] == "dream100" and y["ranked"] == 3 and y["excluded"] == 1 and not y["condition"],
          "another list ranks on the Dream 100 score, with a sold record gated out")
    check(y["portfolio_owners"] == 1 and y["largest_portfolio"] == 2, "the owner with two of the list's records is summed")
    wb = _sheets(Path(r4["run_dir"]) / "files" / y["files"][0])
    first = list(wb)[0]
    check(first == "Top 100" and "Tired Landlord" in str(wb[first][0][0]) and "D4D" not in str(wb[first][0][0]),
          "the workbook names the list it ranked and never claims the D4D cohort")
    ov = " ".join(str(c) for row in wb["Overview"] for c in row if c)
    check("no street-view condition score" in ov and "other 80 points" in ov,
          "the sheet says condition scores 0 for a list without a street-view score")
    ranked, _x, _p = build.rank_records({u: recs[u] for u in ("tl-1", "tl-2", "tl-3")}, {}, {})
    check(all(r["pts"]["cond"] == 0 for r in ranked), "with no D4D row a record earns no condition points")
    recs["tl-3"]["lists"] = []
    r5 = ir.run(task({"list": "Tired Landlord"}))
    check(r5["lists"][0]["ranked"] == 2, "a record that left the list leaves the ranking, even though it is cached")
    check(ir.lists_to_rank({"id": 1, "payload": "{}"}) == ["Obituary"], "a scheduled run ranks the Obituary list by default")
    os.environ["IDLE_RECORDS_LISTS"] = "Tired Landlord, Obituary"
    check(ir.lists_to_rank({"id": 1, "payload": "{}"}) == ["Tired Landlord", "Obituary"],
          "IDLE_RECORDS_LISTS sets the lists a scheduled run ranks")
    os.environ.pop("IDLE_RECORDS_LISTS", None)
    nope = ir.run(task({"list": "Nope List"}))
    check("no list titled" in nope["lists"][0]["problem"], "an unknown list is reported, not ranked as empty")

    print("  idle records: the defaults the command lines keep")
    words = build.d4d_words(100)
    check(words["title"] == "Dream 100: the largest opportunities in the D4D top 3,000"
          and words["cohort_list"] == "D4D Top 3000", "the D4D Dream 100 keeps its own words by default")
    out = tmp / "wfd_dream_default.xlsx"
    rk, ex, pf = build.rank_records({u: recs[u] for u in ("tl-1", "tl-2")}, {}, {})
    build.write_workbook(rk, [], rk, ex, pf, out, 100)
    check(_sheets(out)["Dream 100"][0][0] == words["title"], "write_workbook with no words writes the D4D title")
    try:
        oo.Reader("another-store-account")
        refused = False
    except SystemExit:
        refused = True
    check(refused, "the obituary reader refuses an account name from the old desktop token store")
    rd = oo.Reader(call=lambda p, m="GET", b=None, h=None: {"results": [{"title": "Obituary", "uuid": "l-obit"}]})
    check(rd.list_uuid("Obituary") == "l-obit", "the obituary reader runs on a handed-in transport with no key")

    class TinyReader:
        def list_uuid(self, t): return "l"
        def list_records(self, lu, ptype): return [{"uuid": "z1"}] if ptype == "clean" else []
        def detail(self, u): return {"uuid": u}
        def custom_fields(self, u): return []
        def option_labels(self): return {}
    cache = tmp / "wfd_obit_default.json"
    oo.pull("datasift-apikey", cache, reader=TinyReader())
    entry = json.loads(cache.read_text(encoding="utf-8"))["records"][0]
    check(set(entry) == {"uuid", "thin", "record", "custom_fields", "error"},
          "the command line's cache keeps its own shape: no fetched date or pending mark unless capped")


# =========================================================================== list pull

QUOTE = "/properties/add-properties-by-query/quote/"
ADD = "/properties/add-properties-by-query/"


class FakeListCRM:
    def __init__(self):
        self.lists, self.tags, self.recs, self.writes = {}, {}, {}, []
        self.sticky = True

    def call(self, path, method="GET", body=None, headers=None):
        p, _ = _q(path)
        for kind, pool in (("list", self.lists), ("tag", self.tags)):
            if p == "/api/internal/%s/" % kind:
                if method == "POST":
                    self.writes.append(("create " + kind, body["title"]))
                    pool[body["title"]] = "%s-%d" % (kind, len(pool) + 1)
                    return {"uuid": pool[body["title"]]}
                return {"results": [{"title": t, "uuid": u} for t, u in pool.items()]}
        if p.endswith("/add-lists/"):
            self.writes.append(("add-lists", p.split("/")[4]))
            if self.sticky:
                self.recs[p.split("/")[4]]["lists"].append(body["lists"])
            return {}
        if p.startswith("/api/internal/property/") and method == "GET":
            return copy.deepcopy(self.recs.get(p.split("/")[4]) or {})
        raise AssertionError("unexpected route %s %s" % (method, path))


class FakeMap:
    """SiftMap's search (ids-typed), quote and add for invented ids."""

    def __init__(self, crm, known, saved=(), land=True, truncate=None, allowance=None, attach=True):
        self.crm, self.known = crm, set(known)
        self.saved = {i: "rec-" + i for i in saved}
        for i in saved:
            crm.recs["rec-" + i] = {"uuid": "rec-" + i, "dataflik_id": int(i), "lists": []}
        self.land, self.truncate, self.allowance, self.attach = land, truncate, allowance, attach
        self.adds, self.quotes = [], []

    def _request(self, path, method="POST", body=None):
        if path == "/properties/search/":
            ids = [a["search"] for a in body["addresses"]]
            return {"data": [{"id": i, "saved": i in self.saved, "saved_uuid": self.saved.get(i)} for i in ids if i in self.known]}
        ids = [a["search"] for a in body["query"]["addresses"]]
        if path == QUOTE:
            self.quotes.append(body)
            total = min(len(ids), self.truncate) if self.truncate else len(ids)
            inside = sum(1 for i in ids[:total] if i in self.saved)
            return {"total": total, "in_account": inside, "new": total - inside, "billable": total - inside,
                    "property_usage": {"allowance_left": self.allowance}}
        if path == ADD:
            self.adds.append(body)
            for i in ids:
                if i in self.saved:
                    if self.attach:
                        self.crm.recs[self.saved[i]]["lists"].append(body["lists"][0])
                    continue
                if self.land:
                    self.saved[i] = "rec-" + i
                    self.crm.recs["rec-" + i] = {"uuid": "rec-" + i, "dataflik_id": int(i),
                                                 "lists": list(body["lists"]) if self.attach else []}
            return {}
        raise AssertionError("unexpected SiftMap route %s" % path)


def _ids(n, start=700000):
    return [str(start + i) for i in range(n)]


def _list_pull(check, lp) -> None:
    print("  list pull: quote first")
    os.environ["LIST_PULL_PROBE_WAIT_MIN"] = "-1"
    os.environ["LIST_PULL_WAIT_MIN"] = "-1"
    crm = lp.CRM = FakeListCRM()
    ids = _ids(5)
    m = lp.MAP = FakeMap(crm, ids + ["800001"], saved=[ids[4]])
    t = task({"ids": ",".join(ids + ["999999"]), "list": "Pool Alder", "approved_count": "0"})
    q = lp.run(t)
    check(q["ok"] and q["mode"] == "quote" and q["quote"]["billable"] == 4 and q["unknown"] == 1
          and q["already_records"] == 1 and not m.adds and not crm.writes,
          "approved 0 quotes only: four new, one already a record, one unknown to SiftMap, nothing sent or created")
    check("would use the record allowance" in lp.render(t, q) and "approves 4 new records" in lp.render(t, q),
          "the quote tells the operator the exact count to approve")
    config.MODE = "dry"
    dry = lp.run(task({"ids": ",".join(ids), "list": "Pool Alder", "approved_count": "10"}))
    check(dry["mode"] == "quote" and not m.adds and "dry mode" in lp.render(task(), dry),
          "an approved pull on a dry box still only quotes")
    config.MODE = "live"
    try:
        low = lp.run(task({"ids": ",".join(ids), "list": "Pool Alder", "approved_count": "3"}))
        check(not low["ok"] and "3 were approved" in low["problem"] and not m.adds,
              "an approval smaller than the quote refuses the pull")
        lp.MAP = FakeMap(crm, ids, truncate=2)
        cut = lp.run(task({"ids": ",".join(ids), "list": "Pool Alder", "approved_count": "10"}))
        check(not cut["ok"] and "counted 2 of 5" in cut["problem"] and not lp.MAP.adds,
              "a quote that counts fewer ids than were asked about refuses the pull")
        lp.MAP = FakeMap(crm, ids, allowance=2)
        poor = lp.run(task({"ids": ",".join(ids), "list": "Pool Alder", "approved_count": "10"}))
        check(not poor["ok"] and "left in its allowance" in poor["problem"] and not lp.MAP.adds,
              "a quote that reports too little allowance left refuses the pull")

        print("  list pull: live, approved")
        crm = lp.CRM = FakeListCRM()
        many = _ids(300, start=710000)
        m = lp.MAP = FakeMap(crm, many)
        t = task({"ids": ",".join(many), "list": "Pool Birch", "approved_count": "300"})
        got = lp.run(t)
        check(got["ok"] and got["mode"] == "pulled" and got["probe_uuid"] == "rec-" + many[0] and got["landed"] == 300,
              "the first id lands, reads back with its own dataflik_id, then the rest land")
        check(len(m.adds) == 3 and [len(a["query"]["addresses"]) for a in m.adds] == [1, 250, 49],
              "one probe, then chunks of 250")
        check(("create list", "Pool Birch") in crm.writes and ("create tag", "Pool Birch") in crm.writes,
              "the list and the tag are created before the first add")
        check(all(q["query"] == lp.d4d.add_query_body([a["search"] for a in q["query"]["addresses"]], "Pool Birch",
                                                      "Pool Birch")["query"] for q in m.quotes),
              "the quote is asked about exactly the query the add sends")
        again = lp.run(task({"ids": ",".join(many), "list": "Pool Birch", "approved_count": "300"}))
        check(len(m.adds) == 3 and again["mode"] in ("nothing", "pulled"), "a re-run on the same ids sends nothing twice")

        crm = lp.CRM = FakeListCRM()
        lp.MAP = FakeMap(crm, _ids(3, start=720000), land=False)
        lost = lp.run(task({"ids": ",".join(_ids(3, start=720000)), "list": "Pool Cedar", "approved_count": "3"}))
        check(not lost["ok"] and "did not land" in lost["problem"] and len(lp.MAP.adds) == 1,
              "a probe that never lands stops the pull after one id")
        crm = lp.CRM = FakeListCRM()
        crm.sticky = False
        lp.MAP = FakeMap(crm, _ids(3, start=730000), attach=False)
        bare = lp.run(task({"ids": ",".join(_ids(3, start=730000)), "list": "Pool Dogwood", "approved_count": "3"}))
        check(not bare["ok"] and "without the list" in bare["problem"] and len(lp.MAP.adds) == 1
              and any(w[0] == "add-lists" for w in crm.writes),
              "a probe that lands without the list is fixed on the documented route, and stops when that does not stick")
        os.environ["LIST_PULL_MAX_IDS"] = "4"
        over = lp.run(task({"ids": ",".join(_ids(5)), "list": "Pool Elm", "approved_count": "5"}))
        os.environ.pop("LIST_PULL_MAX_IDS", None)
        check(not over["ok"] and "over the 4" in over["problem"], "a file over the per-run cap is refused")
    finally:
        config.MODE = "dry"
    prior = config.RUNS_DIR / "idle_records-1" / "files"
    prior.mkdir(parents=True, exist_ok=True)
    (prior / "ranked_pool.json").write_text(json.dumps([{"dataflik_id": 701}, {"dataflik_id": "702"}, {"x": 1}]),
                                            encoding="utf-8")
    got_ids, why = lp.resolve_ids("ranked_pool.json")
    check(got_ids == ["701", "702"] and not why, "ids come from a file an earlier task left on the box")
    check(lp.resolve_ids("../secrets.json")[0] == [] and lp.resolve_ids("701, 702 703")[0] == ["701", "702", "703"],
          "a path is refused and a typed list of ids is read")


# =========================================================================== land desk

def _land_analysis() -> dict:
    import builder_land as bl
    cls = {"lot": {"events": 3, "n": 3, "acres_lo": 0.1, "acres_hi": 0.4, "price_p25": 30000, "price_med": 35000,
                   "price_p75": 42000}}
    box = {"key": "K1", "name": "ALDER HOMES LLC", "type": "builder", "buys": 6, "buys_12m": 3, "per_year": 2.0,
           "last_buy": "2026-09-01", "areas": [], "zips": ["37990"], "classes": cls, "build_sqft": 1600,
           "sells_for": 310000, "lot_to_sale_days": 210, "from_person_share": 0.5, "matched_lots": 1, "matched_core": 1,
           "matched_teardowns": 0, "matched_tracts": 0, "reach": "no phones yet", "main_niche": "urban_infill",
           "niche_mix": "urban_infill 6", "price_med": 35000, "built": 4, "vacant": 2, "counties": ["Alder"],
           "sample": []}
    lot = {"segment": "lot", "address": "12 Birch Rd", "city": "Exampleton", "zip": "37990", "area": "a-1", "acres": 0.2,
           "n_fit": 1, "matches": [{"key": "K1", "name": "ALDER HOMES LLC", "dist": 120, "nearby": 2,
                                    "last_nearby": "2026-08-01", "score": 0.8}],
           "ref": 35000, "ref_basis": "builder buys nearby", "offer": 23000, "owner": "Pat Example",
           "mailing": "9 Mail Rd, Exampleton TN", "mail_state": "TN", "tenure_years": 12, "flags": [],
           "on_distress_list": False, "flood": "", "id": "1001"}
    meta = {"name": "Alder test market", "fips": ["99001"], "analyzed": "2026-11-01", "min_buys": 3, "active_months": 24,
            "radius": bl.RADIUS, "fee": 12000, "parcels_on_disk": 10, "new_builds": 5, "lux": bl.LUX, "mid": bl.MID}
    return {"meta": meta, "boxes": [box], "lots": [lot], "lots_unmatched": 4, "teardowns": [], "tracts": [],
            "competitors": [], "dropped": {},
            "quick_flips": {"n": 0, "sellers": 0, "buy_median": None, "markup_median": None, "markup_p25": None,
                            "markup_p75": None, "days_median": None},
            "summary": [{"county": "Alder", "niche": "urban_infill", "built_per_year": 10, "vacant_lots_now": 50,
                         "buys_since_2021": 30, "buyers": 12, "repeat_buyers_3plus": 3, "share_by_repeat_buyers": 0.4,
                         "builder_share": 0.5, "median_buy": 35000, "median_exit": 310000,
                         "bought_from_person_share": 0.6, "end_user_share": 0.2}]}


def _land(check, tmp: Path, ld) -> None:
    import builder_land as bl
    print("  land desk")
    root = tmp / "wfd_land"
    (root / "alder_test").mkdir(parents=True, exist_ok=True)
    (root / "alder_test" / "analysis.json").write_text(json.dumps(_land_analysis()), encoding="utf-8")
    os.environ["LAND_DESK_DIR"] = str(root)
    ld.TODAY = TODAY
    keep_out = bl.OUT
    t = task({"market": "alder_test"}, details="the Alder Homes lots please")
    res = ld.run(t)
    files = Path(res["run_dir"]) / "files"
    check(res["ok"] and res["builders"] == 1 and res["lots_fit"] == 1 and res["verdict"].startswith("No."),
          "a finished analysis on the volume renders into the land desk read")
    check(any(n.endswith(".xlsx") for n in res["files"]) and "alder_test_alder_homes_llc_lots.csv" in res["files"],
          "the workbook and the named builder's own lot list are made")
    check(sorted(p.name for p in (root / "alder_test").iterdir()) == ["analysis.json"] and bl.OUT == keep_out,
          "nothing is written beside the analysis, and the module's folder setting is put back")
    check(res["age_days"] == 12 and "Land desk, Alder test market" in ld.render(t, res),
          "the read says how old the analysis is")
    miss = ld.run(task({"market": "union_zz"}))
    check(not miss["ok"] and "builder_land.py --market union_zz" in miss["problem"],
          "a market with no analysis on the box names the workstation command that builds it")
    check(ld.find_market("../alder_test") is None, "a market name cannot walk the disk")
    os.environ["LAND_DESK_MAX_MB"] = "0.000001"
    big = ld.run(task({"market": "alder_test"}))
    os.environ.pop("LAND_DESK_MAX_MB", None)
    check(not big["ok"] and "over the" in big["problem"], "an analysis too big for this box's memory is refused, not loaded")


# =========================================================================== account setup

SRC_ACCT = "0a0a0a0a-1111-2222-3333-444444444444"
OWN_ACCT = "0b0b0b0b-5555-6666-7777-888888888888"
PUBLISHED = ("/api/internal/user/", "/api/internal/list/", "/api/internal/tag/", "/api/internal/global-status/",
             "/api/internal/siftline/board/", "/api/internal/task-group/", "/api/internal/custom-fields/",
             "/api/internal/filter-preset-folder/", "/api/internal/filter-preset/")


def _blueprint() -> dict:
    from account_blueprint.blueprint import empty, ref
    bp = empty()
    bp["source"] = {"label": "test", "email": "", "account_uuid": SRC_ACCT, "auth_by_family": {}}
    bp["statuses"] = [{"title": "Hot Seller", "color": "#cc0000", "system": False},
                      {"title": "new_lead", "color": "#316aff", "system": True}]
    bp["lists"] = [{"title": "Alder Probate"}]
    bp["tags"] = [{"title": "Alder Tag"}]
    bp["custom_field_groups"] = [{"title": "Alder Group", "entity_type": "property"}]
    bp["custom_fields"] = [{"label": "Alder Field", "field_type": "text", "group": "Alder Group"}]
    bp["task_groups"] = [{"title": "Alder Tasks"}]
    bp["task_presets"] = [{"group": "Alder Tasks", "title": "Call back", "notes": "", "round_robin": False,
                           "expires_in": 1, "all_day": True, "due_time": None, "skip_weekends": True,
                           "assigned_to_user": ref("self", "self")}]
    bp["boards"] = [{"title": "Alder Board", "columns": ["New", "Done"]}]
    bp["preset_folders"] = [{"title": "01. Alder", "numbered": True, "presets": [
        {"title": "Alder - Ready", "quick_filter": False,
         "filters": {"must": {"any_lists": [ref("list", "Alder Probate")], "any_property_status": ["Hot Seller"]}}}]}]
    bp["sequences"] = [{"title": "Alder sequence", "trigger": {"event": "property.status.updated"}, "conditions": [],
                        "actions": [], "is_active": False}]
    bp["siftmap_presets"] = [{"name": "Alder feeder", "lists": [], "tags": [], "filter_data": {}, "auto_add_enabled": True}]
    return bp


class FakeSetupCRM:
    """One install's account, on the published routes only. Anything else is an assertion."""

    def __init__(self, email="owner@example.test", account=OWN_ACCT, flags=()):
        self.user = {"uuid": "u-own-1", "email": email, "account": account, "first_name": "Pat", "last_name": "Example",
                     "feature_flags": list(flags)}
        self.lists, self.tags = {}, {}
        self.groups = [{"uuid": "gs-1", "title": "new_lead", "statuses": [{"title": "new_lead"}]}]
        self.boards = [{"uuid": "b-1", "title": "Alder Board"}]
        self.tgroups, self.tpresets = {}, {}
        self.cgroups, self.cfields = {}, {}
        self.folders, self.presets = {}, {}
        self.asked, self.writes = [], []

    def _n(self, kind: str) -> str:
        return "%s-%d" % (kind, len(self.writes) + 1)

    def call(self, path, method="GET", body=None, headers=None):
        p, _ = _q(path)
        self.asked.append(p)
        if not p.startswith(PUBLISHED):
            raise AssertionError("account setup asked an unpublished route: %s %s" % (method, path))
        if method == "POST" and (headers or {}).get("x-http-method-override") != "GET":
            self.writes.append((p, body))
        if p == "/api/internal/user/":
            return dict(self.user)
        for kind, pool in (("list", self.lists), ("tag", self.tags)):
            if p == "/api/internal/%s/" % kind:
                if method == "POST":
                    pool[body["title"]] = self._n(kind)
                    return {"uuid": pool[body["title"]]}
                return {"results": [{"title": t, "uuid": u} for t, u in pool.items()], "next": None}
        if p == "/api/internal/global-status/":
            return {"results": self.groups, "next": None}
        if p == "/api/internal/siftline/board/":
            return {"results": self.boards, "next": None}
        if p == "/api/internal/custom-fields/group/":
            if method == "POST":
                self.cgroups[body["label"]] = len(self.cgroups) + 1
                return {"id": self.cgroups[body["label"]]}
            return {"results": [{"label": k, "id": v} for k, v in self.cgroups.items()], "next": None}
        if p == "/api/internal/custom-fields/":
            if method == "POST":
                self.cfields[body["label"]] = dict(body, uuid=self._n("cf"), id=len(self.cfields) + 1)
                return self.cfields[body["label"]]
            return {"results": list(self.cfields.values()), "next": None}
        if p == "/api/internal/task-group/":
            if method == "POST":
                self.tgroups[body["title"]] = self._n("tg")
                return {"uuid": self.tgroups[body["title"]]}
            return {"results": [{"title": t, "uuid": u} for t, u in self.tgroups.items()], "next": None}
        if p.startswith("/api/internal/task-group/") and p.endswith("/task-preset/"):
            g = p.split("/")[4]
            if method == "POST":
                self.tpresets.setdefault(g, []).append(dict(body, uuid=self._n("tp")))
                return self.tpresets[g][-1]
            return {"results": list(self.tpresets.get(g, [])), "next": None}
        if p == "/api/internal/filter-preset-folder/":
            if method == "POST":
                self.folders[body["title"]] = {"uuid": self._n("fold"), "title": body["title"]}
                return self.folders[body["title"]]
            return {"results": list(self.folders.values()), "next": None}
        if p.startswith("/api/internal/filter-preset-folder/"):
            f = p.split("/")[4]
            return {"results": [x for x in self.presets.values() if x["folder"] == f], "next": None}
        if p == "/api/internal/filter-preset/" and method == "POST":
            u = self._n("pre")
            self.presets[u] = dict(body, uuid=u)
            return self.presets[u]
        if p.startswith("/api/internal/filter-preset/"):
            return dict(self.presets.get(p.split("/")[4]) or {})
        raise AssertionError("account setup asked an unexpected route: %s %s" % (method, path))


def _account(check, tmp: Path, ac) -> None:
    from account_blueprint.blueprint import validate
    print("  account setup: on the install's own key")
    bp = _blueprint()
    check(validate(bp)[0] == [], "the invented blueprint is valid by the package's own validator")
    path = tmp / "wfd_blueprint.json"
    path.write_text(json.dumps(bp), encoding="utf-8")
    os.environ["ACCOUNT_SETUP_BLUEPRINT"] = str(path)
    crm = ac.CRM = FakeSetupCRM()
    t = task({"phase": "plan", "account_email": "owner@example.test"})
    plan = ac.run(t)
    check(plan["ok"] and plan["phase"] == "plan" and not crm.writes, "a plan reads the account and writes nothing")
    check(plan["manual_by_family"] == {"statuses": 1, "boards": 1, "sequences": 1},
          "statuses, board columns and sequences are named as set-up-by-hand items, never silent gaps")
    check(all(p.startswith(PUBLISHED) for p in crm.asked), "the key path asks only published routes")
    check("siftmap" not in plan["families"], "SiftMap feeders are left out by default (they carry the source's counties)")
    text = ac.render(t, plan)
    check("By hand in the app: 1 statuses, 1 boards, 1 sequences" in text, "the Slack text lists what is by hand")
    config.MODE = "live"
    try:
        held = ac.run(task({"phase": "apply", "account_email": "owner@example.test"}))
        check(held["phase"] == "plan" and "ACCOUNT_SETUP_ALLOW_APPLY" in held["why_plan"] and not crm.writes,
              "apply on a live box without ACCOUNT_SETUP_ALLOW_APPLY runs as a plan and says why")
        os.environ["ACCOUNT_SETUP_ALLOW_APPLY"] = "1"
        done = ac.run(task({"phase": "apply", "account_email": "owner@example.test"}))
        made = sorted({w[0] for w in crm.writes})
        check(done["ok"] and done["phase"] == "apply" and done["mismatches"] == 0
              and "/api/internal/filter-preset/" in made and "/api/internal/list/" in made
              and "/api/internal/task-group/" in made and "/api/internal/custom-fields/" in made,
              "apply creates the list, tag, field, task preset and preset, each read back as sent")
        preset = next(x for x in crm.presets.values() if x["title"] == "Alder - Ready")
        check(preset["filters"]["account"] == OWN_ACCT and preset["filters"]["must"]["any_lists"] == [crm.lists["Alder Probate"]],
              "the preset points at this account and its own list")
        n = len(crm.writes)
        again = ac.run(task({"phase": "apply", "account_email": "owner@example.test"}))
        check(again["ok"] and len(crm.writes) == n, "a second apply finds everything there and writes nothing")
        wrong = ac.run(task({"phase": "apply", "account_email": "someone@example.test"}))
        check(not wrong["ok"] and "belongs to owner@example.test" in wrong["problem"],
              "the key must belong to the email named")
        ac.CRM = FakeSetupCRM(account=SRC_ACCT)
        self_clone = ac.run(task({"phase": "apply", "account_email": "owner@example.test"}))
        check(not self_clone["ok"] and "source account" in self_clone["problem"],
              "the blueprint's own source account can never be applied onto")
        drift = ac.run(task({"phase": "verify", "account_email": "owner@example.test"}))
        check(drift["phase"] == "verify" and drift["same_as_source"] and "drift check" in ac.render(task(), drift),
              "the source account may check itself against its blueprint (read only)")
        ac.CRM = FakeSetupCRM(flags=["staff"])
        staff = ac.run(task({"phase": "apply", "account_email": "owner@example.test"}))
        check(not staff["ok"] and "staff" in staff["problem"], "a staff account is set up from the command line")
    finally:
        config.MODE = "dry"
        os.environ.pop("ACCOUNT_SETUP_ALLOW_APPLY", None)
    from account_blueprint.client import ApiError

    class Boom:
        def call(self, *a, **k):
            e = RuntimeError("refused")
            e.code, e.body = 400, "title must be unique"
            raise e
    try:
        ac.KeyClient(Boom()).call("/api/internal/list/", "POST", {"title": "x"})
        typed = False
    except ApiError as exc:
        typed = exc.code == 400 and "unique" in exc.body
    check(typed, "a refusal from the key client reaches the blueprint package as its own ApiError")
    bad = ac.run(task({"phase": "destroy", "account_email": "owner@example.test"}))
    check(not bad["ok"] and "plan, verify or apply" in bad["problem"], "an unknown phase is refused")


# =========================================================================== house rules

def _hygiene(check) -> None:
    print("  house rules on the files this track owns")
    src = Path(__file__).resolve().parents[1]
    mine = [src / "extract_market_finder.py", src / "dream100.py", src / "dream100_build.py",
            src / "obituary_opportunity.py", src / "d4d_call_list.py", src / "pull_by_ids.py", src / "builder_land.py",
            src / "agents" / "selftest_wf_data.py"] + sorted((src / "agents" / "workflows").glob("*.py"))
    mine = [p for p in mine if p.name in ("extract_market_finder.py", "dream100.py", "dream100_build.py",
                                          "obituary_opportunity.py", "d4d_call_list.py", "pull_by_ids.py",
                                          "builder_land.py", "selftest_wf_data.py", "market_report.py",
                                          "idle_records.py", "list_pull.py", "land_desk.py", "account_setup.py")]
    texts = {p.name: p.read_text(encoding="utf-8") for p in mine}
    check(all(chr(0x2014) not in t and chr(0x2013) not in t for t in texts.values()), "no em or en dash in these files")
    check(all(chr(8) not in t for t in texts.values()), "no backspace byte in these files")

    def top_imports(text: str) -> set:
        out = set()
        for node in ast.parse(text).body:
            if isinstance(node, ast.Import):
                out |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
        return out
    check("datasift_core" not in top_imports(texts["extract_market_finder.py"]),
          "the Market Finder module does not load the browser client at import")
    d4d = texts["d4d_call_list.py"]
    check("datasift_api_upload" not in top_imports(d4d) and "playwright" not in d4d and "C:\\Users" not in d4d,
          "the D4D module loads no password client at import, drives no browser, holds no desktop path")
    check("reisift_auth" not in texts["obituary_opportunity.py"] and "C:\\Users" not in texts["obituary_opportunity.py"],
          "the obituary ranking reads no desktop token store")
    pbi = texts["pull_by_ids.py"]
    check("/api/internal/account/user/" not in pbi and "/api/internal/activity/skiptrace/stats/" not in pbi
          and "siftmap_pull" not in top_imports(pbi),
          "the id pull asks no unpublished users list or trace stats, and loads no allowance client at import")
    for name in ("market_report.py", "idle_records.py", "list_pull.py", "land_desk.py", "account_setup.py"):
        t = texts[name]
        check(not any(x in t for x in ("/api/token/", "impersonate/", "/api/internal/account/user", "DATASIFT_PASSWORD",
                                        "upload/usage", "/api/internal/sequence", "/api/internal/status/")),
              "%s reaches DataSift on published routes and the key only" % name)
