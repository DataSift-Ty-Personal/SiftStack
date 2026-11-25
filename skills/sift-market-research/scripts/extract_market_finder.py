"""Extract Market Finder data from DataSift through the published API, on your Open API key.

Reads one county at a time: every ZIP code row, every neighborhood row (every page, never just
the first) and the county summary panel, and writes the same JSON shape the Market Finder table
shows. generate_market_excel.py turns that JSON into the workbook.

Usage:
    python extract_market_finder.py --state "Tennessee" --county "Knox"
    python extract_market_finder.py --state TN --county "Knox,Blount"
    python extract_market_finder.py --selftest          (checks the pull on invented data, no network)

Your key: REISIFT_API_KEY, your DataSift Open API key (see datasift_core.py). Every request is a
GET, so the pull never changes anything in your account. A county is about 8 requests.

Output: market_finder_<state>_<county>_<stamp>.json in --output-dir (default ./output).

How the pull keeps itself honest. The Market Finder routes and their answers are published
(developers.datasift.ai); the query parameter names are not. The names below are the ones
DataSift's own Market Finder page sends: a county is `fips`, a page is `offset` + `limit`. A name
the server does not know is silently ignored, which reads exactly like a real answer (`county=`
returned every ZIP code in the country), so the pull proves each filter by what comes back:
  * every ZIP row must be one of the county's own ZIP codes,
  * every page the server reports must be read,
  * the listing must echo the group asked for (ZIP or neighborhood) when it echoes one.
A pull that fails a check writes nothing and says why. If DataSift renames a parameter, set
MARKET_FINDER_API_PARAMS='{"county": "...", ...}' without changing the code.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_core import ApiError, DataSiftApi  # noqa: E402

logger = logging.getLogger(__name__)

# ── Column definitions: the Market Finder table's own columns ─────────

ZIP_COLUMNS = [
    "zip_code",
    "total_inv_trans_6mo",
    "homes_on_market",
    "homes_sold_last_month",
    "median_days_on_market",
    "median_home_value",
    "median_sale_price",
]

NEIGHBORHOOD_COLUMNS = [
    "neighborhood",
    "total_inv_trans_6mo",
    "homes_on_market",
    "homes_sold_last_month",
    "median_days_on_market",
    "median_home_value",
    "median_sale_price",
]


def _parse_currency(value: str) -> float | None:
    """Parse currency string like '$304,569' to float."""
    if not value:
        return None
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    """Parse integer string, stripping commas."""
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def _row_to_dict(row: list[str], columns: list[str]) -> dict:
    """Convert a raw table row to a named dict with parsed types."""
    d = {}
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else ""
        if col in ("median_home_value", "median_sale_price"):
            d[col] = _parse_currency(val)
        elif col in ("zip_code", "neighborhood"):
            d[col] = val
        else:
            d[col] = _parse_int(val)
        d[f"{col}_raw"] = val
    return d


def save_result(result: dict, output_dir: str | Path) -> Path:
    """Write one extraction as market_finder_<state>_<county>_<stamp>.json."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    filename = f"market_finder_{result['state']}_{result['county']}_{datetime.now():%Y%m%d_%H%M%S}.json"
    json_file = out_path / filename
    json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info("Saved extraction to %s", json_file)
    return json_file


# ── The published Market Map routes ───────────────────────────────────

API_ROUTES = {
    "states": "/api/internal/market-finder/states/",
    "counties": "/api/internal/market-finder/counties/",
    "zips": "/api/internal/market-finder/counties/zips/",
    "neighborhoods": "/api/internal/market-finder/counties/neighborhoods/",
    "metrics": "/api/internal/market-finder/metrics/",
    "paginated": "/api/internal/market-finder/paginated/",
}
API_PARAMS = {"state": "state", "county": "fips", "group": "geo_group", "offset": "offset", "size": "limit"}
API_GROUPS = {"zip": "zip", "neighborhood": "neighborhood"}
API_PAGE_SIZE = 100
API_MAX_PAGES = 200          # 20,000 rows: no county has that many ZIPs or neighborhoods


class MarketFinderApiError(RuntimeError):
    """The API answered in a shape this pull cannot read, or a filter was not honored."""


def api_settings() -> tuple[dict, dict]:
    """(query parameter names, group values): the defaults above with any env override on top."""
    params, groups = dict(API_PARAMS), dict(API_GROUPS)
    for env, into in (("MARKET_FINDER_API_PARAMS", params), ("MARKET_FINDER_API_GROUPS", groups)):
        raw = (os.environ.get(env) or "").strip()
        if raw:
            try:
                into.update({str(k): str(v) for k, v in json.loads(raw).items()})
            except (ValueError, AttributeError) as exc:
                raise MarketFinderApiError("%s is not a JSON object: %s" % (env, exc)) from exc
    return params, groups


def _items(r) -> list:
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        for k in ("items", "results", "data"):
            if isinstance(r.get(k), list):
                return r[k]
    return []


def _fold(s) -> str:
    s = " ".join(str(s or "").replace(",", " ").split()).casefold()
    return s[: -len(" county")] if s.endswith(" county") else s


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _money_raw(v) -> str:
    """The table's own display form ("$304,569"), so the parsed and raw columns match the page."""
    v = _num(v)
    return "" if v is None else "${:,.0f}".format(v)


def _int_raw(v) -> str:
    v = _num(v)
    return "" if v is None else "{:,}".format(int(round(v)))


def _pct_raw(v) -> str:
    """A share as the page shows it (the API may send 0.072 or 7.2)."""
    v = _num(v)
    if v is None:
        return ""
    return "%.1f%%" % (v * 100 if v <= 1 else v)


def api_row(name: str, metrics: dict, columns: list[str]) -> dict:
    """One table row from the API, in the page's columns:
      total_inv_trans_6mo   num_investor_transactions (the six-month investor count)
      homes_on_market       homes_on_market
      homes_sold_last_month number_of_homes_sold
      median_days_on_market median_days_on_market
      median_home_value     median_property_value
      median_sale_price     median_sale_price"""
    m = metrics or {}
    raw = [str(name), _int_raw(m.get("num_investor_transactions")), _int_raw(m.get("homes_on_market")),
           _int_raw(m.get("number_of_homes_sold")), _int_raw(m.get("median_days_on_market")),
           _money_raw(m.get("median_property_value")), _money_raw(m.get("median_sale_price"))]
    return _row_to_dict(raw, columns)


def api_summary(m: dict) -> dict:
    """The county summary panel, keyed by its labels and by the keys generate_market_excel.py reads."""
    m = m or {}
    owners, renters = _num(m.get("num_owners")), _num(m.get("num_renters"))
    own = owners / (owners + renters) if owners is not None and renters is not None and owners + renters > 0 else None
    out = {
        "median home value": _money_raw(m.get("median_property_value")),
        "homes on market": _int_raw(m.get("homes_on_market")),
        "mo investor transactions": _int_raw(m.get("median_monthly_investor_transactions")),
        "homes sold last month": _int_raw(m.get("number_of_homes_sold")),
        "market rent": _money_raw(m.get("median_property_rental_value")),
        "gross rental yield": _pct_raw(m.get("gross_rental_yield")),
        "homeownership rate": _pct_raw(own),
        "market_rent": _money_raw(m.get("median_property_rental_value")).lstrip("$"),
        "gross_rental_yield": _pct_raw(m.get("gross_rental_yield")),
        "homeownership_rate": _pct_raw(own),
        "median_home_value_summary": _money_raw(m.get("median_property_value")).lstrip("$"),
        "homes_on_market_summary": _int_raw(m.get("homes_on_market")),
        "mo_investor_trans": _int_raw(m.get("median_monthly_investor_transactions")),
        "homes_sold_last_month": _int_raw(m.get("number_of_homes_sold")),
    }
    return {k: v for k, v in out.items() if v}


def _get(call, route: str, query: dict | None = None):
    path = API_ROUTES[route] + ("?" + urlencode(query) if query else "")
    return call(path)


def _find_state(rows: list, state: str) -> dict:
    want = _fold(state)
    for r in rows:
        if want in (_fold(r.get("name")), _fold(r.get("abbreviation")), _fold(r.get("code"))):
            return r
    raise MarketFinderApiError("Market Finder lists no state called %r" % state)


def _find_county(rows: list, county: str, st: dict) -> dict:
    marks = {_fold(st.get("name")), _fold(st.get("abbreviation")), _fold(st.get("code"))} - {""}
    hits = [r for r in rows if _fold(r.get("name")) == _fold(county)
            and (not r.get("state") or _fold(r.get("state")) in marks)]
    if not hits:
        raise MarketFinderApiError("Market Finder lists no county called %r in %s (%d counties came back)"
                                   % (county, st.get("name") or st.get("abbreviation"), len(rows)))
    if len({str(h.get("code")) for h in hits}) > 1:
        raise MarketFinderApiError("%d counties called %r came back for %s; the state filter was not honored"
                                   % (len(hits), county, st.get("name")))
    return hits[0]


def _page_all(call, query: dict, params: dict, page_size: int, label: str) -> tuple[list, int | None, str | None]:
    """Every page of one paginated listing: (rows, the total the server reports, its geoGroup echo).
    A page that adds nothing new ends the walk, so a paging parameter the server ignores (it sends
    the first page again) is caught by the total check instead of looping."""
    rows, seen, total, echo = [], set(), None, None
    for page in range(1, API_MAX_PAGES + 1):
        at = {params["page"]: page} if params.get("page") else {params.get("offset", "offset"): (page - 1) * page_size}
        r = _get(call, "paginated", dict(query, **at, **{params["size"]: page_size}))
        if not isinstance(r, dict) or not isinstance(r.get("data"), list):
            raise MarketFinderApiError("%s: the paginated listing came back as %s, not {data, total}"
                                       % (label, type(r).__name__))
        if total is None:
            total = r.get("total") if isinstance(r.get("total"), int) else None
            echo = r.get("geoGroup")
        new = 0
        for row in r["data"]:
            pf = row.get("property_fields") or {}
            code = str(pf.get("code") or pf.get("name") or "")
            if code and code not in seen:
                seen.add(code)
                rows.append(row)
                new += 1
        if not new or (total is not None and len(rows) >= total):
            break
    return rows, total, echo


def _neighborhood_names(listing) -> dict:
    """code -> name from the county's neighborhood listing."""
    out = {}
    for it in _items(listing):
        if isinstance(it, dict):
            code = it.get("code") or it.get("id") or it.get("uuid")
            name = it.get("name") or it.get("title")
            if code and name:
                out[str(code)] = str(name)
    return out


def pull_market_finder_api(state: str, county: str, call, *, page_size: int = API_PAGE_SIZE) -> dict:
    """One county's Market Finder numbers. `call(path)` is a GET on the Open API key that returns parsed
    JSON. Never raises for a refusal: `success` is False and `problem` says what was wrong, so the
    caller reports it instead of scoring a county off rows that belong to another one."""
    params, groups = api_settings()
    result = {
        "success": False, "state": state, "county": county,
        "zip_data": [], "neighborhood_data": [], "summary": {},
        "extracted_at": datetime.now().isoformat(), "zip_count": 0, "neighborhood_count": 0,
        "source": "api", "api_checks": {}, "problem": "",
    }
    checks = result["api_checks"]
    try:
        st = _find_state(_items(_get(call, "states")), state)
        counties = _items(_get(call, "counties", {params["state"]: st.get("code") or st.get("abbreviation")}))
        co = _find_county(counties, county, st)
        fips = str(co.get("code") or "")
        if not fips:
            raise MarketFinderApiError("county %r came back with no code" % county)
        checks.update({"state": st.get("name") or state, "state_abbreviation": st.get("abbreviation") or "",
                       "county": co.get("name") or county, "county_code": fips})

        zips_listing = _items(_get(call, "zips", {params["county"]: fips}))
        county_zips = {str(z.get("zip_code") or "")[:5] for z in zips_listing
                       if str(z.get("fips") or "") in ("", fips)} - {""}
        checks["county_zip_codes"] = len(county_zips)
        if not county_zips:
            raise MarketFinderApiError("the county's ZIP listing came back empty, so the ZIP rows cannot be "
                                       "checked against it")

        rows, total, echo = _page_all(call, {params["group"]: groups["zip"], params["county"]: fips},
                                      params, page_size, "zip codes")
        codes = [str((r.get("property_fields") or {}).get("code") or "")[:5] for r in rows]
        stray = sorted({c for c in codes if c not in county_zips})
        checks.update({"zip_rows": len(rows), "zip_total": total, "zip_group_echo": echo,
                       "zip_rows_outside_county": len(stray)})
        if echo not in (None, "", groups["zip"]):
            raise MarketFinderApiError("asked for %r rows and the listing says %r; the group parameter is "
                                       "named differently (MARKET_FINDER_API_PARAMS)" % (groups["zip"], echo))
        if stray:
            raise MarketFinderApiError("%d of %d ZIP rows are not in %s County (%s ...): the county filter "
                                       "was ignored, so these numbers are not the county's"
                                       % (len(stray), len(rows), co.get("name") or county, ", ".join(stray[:3])))
        if total is not None and len(rows) < total:
            raise MarketFinderApiError("read %d of the %d ZIP rows the server reports; the page parameter "
                                       "was not honored" % (len(rows), total))
        if not rows:
            raise MarketFinderApiError("no ZIP rows came back for %s County" % (co.get("name") or county))
        result["zip_data"] = [api_row(c, r.get("metrics") or {}, ZIP_COLUMNS) for c, r in zip(codes, rows)]

        names = _neighborhood_names(_get(call, "neighborhoods", {params["county"]: fips}))
        nrows, ntotal, necho = _page_all(call, {params["group"]: groups["neighborhood"], params["county"]: fips},
                                         params, page_size, "neighborhoods")
        checks.update({"neighborhood_rows": len(nrows), "neighborhood_total": ntotal,
                       "neighborhood_group_echo": necho, "neighborhood_names_listed": len(names)})
        if necho not in (None, "", groups["neighborhood"]):
            raise MarketFinderApiError("asked for %r rows and the listing says %r" % (groups["neighborhood"], necho))
        if ntotal is not None and len(nrows) < ntotal:
            raise MarketFinderApiError("read %d of the %d neighborhood rows the server reports"
                                       % (len(nrows), ntotal))
        nb_codes = [str((r.get("property_fields") or {}).get("code") or "") for r in nrows]
        if set(nb_codes) & county_zips:
            raise MarketFinderApiError("the neighborhood listing came back with ZIP codes: the group "
                                       "parameter was ignored")
        if names and any(c not in names for c in nb_codes):
            raise MarketFinderApiError("%d neighborhood rows are not in the county's own neighborhood listing"
                                       % sum(1 for c in nb_codes if c not in names))
        result["neighborhood_data"] = [
            api_row(names.get(c) or (r.get("property_fields") or {}).get("name") or c, r.get("metrics") or {},
                    NEIGHBORHOOD_COLUMNS) for c, r in zip(nb_codes, nrows)]

        metrics = _get(call, "metrics", {params["county"]: fips})
        result["summary"] = api_summary(metrics if isinstance(metrics, dict) else {})
    except MarketFinderApiError as exc:
        result["problem"] = str(exc)
        return result
    except ApiError as exc:
        result["problem"] = "DataSift refused a request: %s" % exc
        return result
    result["zip_count"] = len(result["zip_data"])
    result["neighborhood_count"] = len(result["neighborhood_data"])
    result["success"] = result["zip_count"] > 0
    return result


# ── Selftest: the pull's checks on invented data, no network ──────────

def selftest() -> int:
    from urllib.parse import parse_qs, urlparse

    def fake(county_filter_honored=True, paging_honored=True):
        zips = ["00001", "00002", "00003"]
        hoods = [("n1", "Example Heights"), ("n2", "Sample Park")]

        def call(path):
            u = urlparse(path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            route = next(k for k, v in API_ROUTES.items() if v == u.path)
            if route == "states":
                return [{"name": "Testland", "abbreviation": "TL", "code": "99"}]
            if route == "counties":
                return [{"name": "Demo", "state": "TL", "code": "99001"}]
            if route == "zips":
                return [{"zip_code": z, "fips": "99001"} for z in zips]
            if route == "neighborhoods":
                return [{"code": c, "name": n} for c, n in hoods]
            if route == "metrics":
                return {"median_property_value": 250000, "num_owners": 700, "num_renters": 300,
                        "gross_rental_yield": 0.072, "median_property_rental_value": 1500}
            group = q.get("geo_group")
            codes = zips if group == "zip" else [c for c, _ in hoods]
            if group == "zip" and (not county_filter_honored or "fips" not in q):
                codes = codes + ["99999"]
            off = int(q.get("offset", 0)) if paging_honored else 0
            size = int(q.get("limit", 100))
            page = codes[off:off + size]
            return {"total": len(codes), "geoGroup": group,
                    "data": [{"property_fields": {"code": c}, "metrics": {
                        "num_investor_transactions": 12, "homes_on_market": 40, "number_of_homes_sold": 9,
                        "median_days_on_market": 33, "median_property_value": 210000,
                        "median_sale_price": 199000}} for c in page]}
        return call

    good = pull_market_finder_api("TL", "Demo County", fake(), page_size=2)
    stray = pull_market_finder_api("TL", "Demo", fake(county_filter_honored=False), page_size=2)
    stuck = pull_market_finder_api("TL", "Demo", fake(paging_honored=False), page_size=2)
    nb = good["neighborhood_data"]
    checks = [
        ("a good pull succeeds", good["success"], True),
        ("every ZIP page is read (3 rows at 2 a page)", good["zip_count"], 3),
        ("neighborhood rows carry their names", sorted(n["neighborhood"] for n in nb),
         ["Example Heights", "Sample Park"]),
        ("rows parse money and counts", (good["zip_data"][0]["median_home_value"],
                                         good["zip_data"][0]["total_inv_trans_6mo"]), (210000.0, 12)),
        ("the summary reads ownership and yield", (good["summary"].get("homeownership_rate"),
                                                    good["summary"].get("gross_rental_yield")), ("70.0%", "7.2%")),
        ("a ZIP outside the county refuses the pull", (stray["success"], "not in" in stray["problem"]),
         (False, True)),
        ("an ignored page parameter refuses the pull", (stuck["success"], "page parameter" in stuck["problem"]),
         (False, True)),
    ]
    bad = 0
    for label, got, want in checks:
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    print(f"{len(checks) - bad} of {len(checks)} passed")
    return 1 if bad else 0


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract Market Finder data from DataSift on your Open API key")
    parser.add_argument("--state", help="State name or abbreviation (e.g., Tennessee or TN)")
    parser.add_argument("--county", help="County name(s), comma-separated (e.g., Knox or Knox,Blount)")
    parser.add_argument("--output-dir", default="./output", help="Output directory for JSON files (default: ./output)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--selftest", action="store_true", help="Check the pull on invented data, no network")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if not args.state or not args.county:
        parser.error("--state and --county are required")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    api = DataSiftApi()
    for county in [c.strip() for c in args.county.split(",") if c.strip()]:
        result = pull_market_finder_api(args.state, county, api)
        if result["success"]:
            save_result(result, args.output_dir)
            print(f"  {county}: {result['zip_count']} ZIP codes, {result['neighborhood_count']} neighborhoods")
        else:
            print(f"  {county}: the pull was refused and nothing was written: {result['problem']}")
            sys.exit(1)
    print(f"  {api.calls} requests")


if __name__ == "__main__":
    main()
