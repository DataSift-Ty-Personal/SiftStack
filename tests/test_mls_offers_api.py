"""Zero-network tests for src/mls_offers_api.py: a fake SiftMap answers the same filters the real one does.
Invented streets, agents and 555 numbers only.

    python tests/test_mls_offers_api.py      (or pytest)
"""
import csv
import shutil
from contextlib import contextmanager
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mls_offers as mo  # noqa: E402
import mls_offers_api as api  # noqa: E402
import mortgage_model as mm  # noqa: E402

TODAY = date(2026, 11, 23)


class FakeMap:
    """SiftMap search, detail, quote and add over an invented market. Hidden fields start with "_"."""

    def __init__(self, listings, sales, details):
        self.listings, self.sales, self.details = listings, sales, details
        self.adds, self.quotes, self.detail_reads = [], [], []

    @staticmethod
    def _in(v, lo, hi):
        return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)

    def search(self, body):
        f = body.get("filters") or {}
        addrs = body.get("addresses") or []
        if addrs and addrs[0].get("type") == "ids":
            want = {a["search"] for a in addrs}
            pool = [r for r in self.listings if str(r["id"]) in want]
        elif "extra_last_sale_investor_transaction_type" in f:
            pool = [s for s in self.sales if s["_type"] in f["extra_last_sale_investor_transaction_type"]
                    and self._in(s["_price"], f.get("extra_last_sale_value_min"), f.get("extra_last_sale_value_max"))]
        else:
            pool = list(self.listings)
            if "extra_mls_listing_price_max" in f:
                pool = [r for r in pool if (r["mlsListingPrice"] or 0) <= f["extra_mls_listing_price_max"]]
            if "extra_days_on_market" in f:
                pool = [r for r in pool if r["_dom"] <= int(f["extra_days_on_market"].rstrip("d"))]
            if "distressors_include" in f:
                pool = [r for r in pool if f["distressors_include"] in r["_flags"]]
        if "extra_suggested_rent_min" in f:
            pool = [r for r in pool if self._in(r.get("_rent"), f["extra_suggested_rent_min"], f["extra_suggested_rent_max"])]
        idx, size = int(body.get("result_index") or 1), int(body.get("size") or 250)
        return {"total_results": len(pool), "data": [{k: v for k, v in r.items() if not k.startswith("_")}
                                                     for r in pool[idx - 1: idx - 1 + size]]}

    def get_detail(self, i):
        self.detail_reads.append(str(i))
        return self.details[str(i)]

    def _request(self, path, method="GET", body=None):
        ids = [a["search"] for a in (body or {}).get("query", {}).get("addresses", [])]
        if path.endswith("/quote/"):
            self.quotes.append(ids)
            return {"total": len(ids), "in_account": 0, "new": len(ids), "billable": len(ids), "cost": "0.00",
                    "shortfall": "0.00", "blocked_count": 0, "property_usage": {"allowance_left": 1000}}
        self.adds.append(ids)
        for r in self.listings:
            if str(r["id"]) in ids:
                r["saved"], r["saved_uuid"] = True, "uuid-" + str(r["id"])
        return {}


def listing(i, street, price, *, dom=200, flags=(), saved=True, equity=100, rent=1_600, lat=35.98, lon=-83.92,
            sqft=1_200, value=None):
    return {"id": str(i), "address": f"{street}, Knoxville, TN 37917", "fips": "47093", "estimatedValue": value or price,
            "mlsListingPrice": price, "squareFeet": sqft, "bedrooms": 3, "bathrooms": 2, "lotAcres": 0.2,
            "latitude": lat, "longitude": lon, "corporateOwned": False, "absenteeOwner": False, "equityPercent": equity,
            "saved": saved, "saved_uuid": ("uuid-%s" % i) if saved else None, "_dom": dom, "_flags": set(flags),
            "_rent": rent}


def sale(i, price, kind="pending", sqft=1_200, lat=35.981, lon=-83.921):
    return {"id": str(i), "address": f"{i} Sample Rd, Knoxville, TN 37917", "estimatedValue": price * 1.3,
            "squareFeet": sqft, "latitude": lat, "longitude": lon, "_type": kind, "_price": price}


def detail(agent="Pat Agent", phone="5555550100", loans=(), sold="2018-05-01", year=1955, hist=None):
    return {"mls_agent": {"agent_name": agent, "agent_office": "Example Realty", "agent_phone": phone},
            "mls_days_on_market": 400, "mls_listing_price": None, "fips": "47093",
            "mls_history": hist or [{"status_date": "2026-11-01", "status": "Active", "price": 180_000, "days_listed": 300,
                                     "agent_name": agent},
                                    {"status_date": "2026-06-01", "status": "Active", "price": 200_000, "days_listed": 150,
                                     "agent_name": agent}],
            "mortgage_history": list(loans), "sale_history": [{"sale_date": sold, "sale_price": 120_000}],
            "years_built": year, "tax_amount": 1_200, "investor_off_market_score": 80, "investor_on_market_score": 90,
            "realtor_score": 60, "owner_info": {"owner_name": "Sam Example", "secondary_owner_names": []},
            "distressor_data": {"is_vacant_property": {"is_active": True}}}


def test_resolve_market():
    m = api.resolve_market(county="Knox, TN")
    assert m["counties"] == [{"fips": "47093", "name": "Knox", "state": "TN"}] and m["name"] == "knox-tn"
    assert api.resolve_market(county="Knox County, Tennessee")["fips"] == ["47093"]
    assert api.resolve_market(fips="47093,47009")["fips"] == ["47093", "47009"]
    z = api.resolve_market(zips="37917, 37914")
    assert z["zips"] == ["37917", "37914"] and z["counties"] == []
    for bad, words in (({"county": "Knox"}, "with its state"), ({"county": "Knox, ZZ"}, "not a US state"),
                       ({"county": "Nowhere, TN"}, "No county named"), ({"zips": "3791"}, "five-digit"),
                       ({"fips": "99999"}, "FIPS"), ({}, "Name a market")):
        try:
            api.resolve_market(**bad)
            raise AssertionError("no error for %s" % bad)
        except ValueError as exc:
            assert words in str(exc), (bad, str(exc))


def test_address_split_and_county_address():
    assert api.split_address("1133 Example Ave, Knoxville, TN 37917") == ("1133 Example Ave", "Knoxville", "TN", "37917")
    a = api.county_address({"fips": "47093", "name": "Knox", "state": "TN"})
    assert a["search"] == "Knox" and a["type"] == "county" and a["counties"][0]["fips"] == "47093"


def test_loan_kind_and_open_loans():
    assert api.loan_kind("New Conventional") == "Conventional" and api.loan_kind("FHA") == "FHA"
    assert api.loan_kind("Credit Line (Revolving)") == "Credit Line" and api.loan_kind("VA") == "VA"
    hist = [{"record_date": "2015-01-05", "loan_type": "New Conventional", "amount": 100_000, "due_date": "2045-02-01"},
            {"record_date": "2018-05-02", "loan_type": "New Conventional", "amount": 160_000, "due_date": "2048-06-01"},
            {"record_date": "2018-05-02", "loan_type": "Stand Alone Second", "amount": 20_000, "due_date": "2033-06-01"},
            {"record_date": "2021-03-10", "loan_type": "Refinance", "amount": 150_000, "due_date": "2051-04-01"},
            {"record_date": "2022-01-01", "loan_type": "Credit Line", "amount": 40_000, "due_date": "2047-01-01"},
            {"record_date": "2019-01-01", "loan_type": "Construction", "amount": 90_000, "due_date": "2020-01-01"}]
    got = api.open_loans(hist, [{"sale_date": "2018-04-30"}], TODAY)
    kinds = [(l["kind"], l["amount"]) for l in got]
    assert ("Conventional", 150_000.0) in kinds                 # the refinance replaced the 2018 first and second
    assert ("Credit Line", 40_000.0) in kinds                   # a credit line after the refinance stays
    assert all(l["amount"] != 100_000 for l in got)            # before the owner bought: someone else's loan
    assert all(l["kind"] != "Construction" for l in got)       # due in 2020: closed
    assert len(got) == 2


def test_history_rows_feed_the_cut_count():
    rows = api.history_rows([{"status_date": "2026-11-01", "status": "Active", "price": 180_000, "days_listed": 300},
                             {"status_date": "2026-03-01", "status": "Fail", "price": 200_000, "days_listed": 60},
                             {"status_date": "2026-03-05", "status": "Active", "price": 200_000, "days_listed": 64},
                             {"status_date": "2026-01-05", "status": "Active", "price": 210_000, "days_listed": 5}])
    s = mo.summarize_history(rows, 180_000, TODAY)
    assert s["cuts"] == 2 and s["relists"] == 1 and s["original_price"] == 210_000
    assert s["dom"] == (TODAY - date(2025, 12, 31)).days


def test_evidence_guards_and_band_pricing():
    assert api.slim_evidence({"latitude": 1, "longitude": 1, "squareFeet": 1_000, "estimatedValue": 50_000}, 100_000,
                             109_999, "Investor") is None          # twice the value or more: a portfolio deed
    assert api.slim_evidence({"latitude": 1, "longitude": 1, "squareFeet": 300}, 50_000, 59_999, "Investor") is None
    fm = FakeMap([], [sale(1, 87_000), sale(2, 143_500), sale(3, 412_000, "flip")], {})
    out = []
    for lo, hi in api.BAND_STARTS:
        api.banded(fm, {"search": "Knox", "type": "county"}, {"extra_last_sale_investor_transaction_type": ["pending"]},
                   lo, hi, out)
    prices = sorted(round((lo + hi) / 2) for _r, lo, hi in out)
    assert len(prices) == 2 and abs(prices[0] - 87_000) <= 5_000 and abs(prices[1] - 143_500) <= 5_000


def test_rent_bands():
    fm = FakeMap([listing(1, "1 Example St", 150_000, rent=1_640), listing(2, "2 Example St", 150_000, rent=3_310)],
                 [], {})
    got = {}
    for lo, hi in api.RENT_STARTS:
        api.rent_bands(fm, ["1", "2"], lo, hi, got)
    assert abs(got["1"] - 1_640) <= 50 and abs(got["2"] - 3_310) <= 125


@contextmanager
def patched(*pairs):
    """Set module attributes for one test and put the originals back after it, so other tests in the same
    pytest process see the real modules."""
    saved = [(mod, name, getattr(mod, name)) for mod, name, _v in pairs]
    try:
        for mod, name, v in pairs:
            setattr(mod, name, v)
        yield
    finally:
        for mod, name, v in saved:
            setattr(mod, name, v)


def _fake_mortgage(tmp):
    """mortgage_model without the network: a few Freddie Mac weeks, no HMDA, no geocoder."""
    p = tmp / "pmms.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["observation_date", "MORTGAGE30US", "MORTGAGE15US"])
        for d, r30, r15 in (("2018-01-04", 3.95, 3.4), ("2020-04-02", 3.33, 2.8), ("2021-01-07", 2.65, 2.2),
                            ("2022-01-06", 3.22, 2.4), ("2026-11-12", 6.9, 6.1)):
            w.writerow([d, r30, r15])
    return [(mm, "CACHE", tmp / "mm"), (mm, "fetch_pmms", lambda force=False: p),
            (mm, "fetch_hmda", lambda county, year, force=False: None),
            (mm, "tract_ids", lambda lat, lon, cache: cache.setdefault(f"{lat:.6f},{lon:.6f}", {}))]


def test_run_end_to_end_dry_then_add():
    tmp = Path(tempfile.mkdtemp())
    before = (mo.OUT, mm.CACHE, mm.fetch_hmda, api.key_owner)
    no_client = lambda key="": (_ for _ in ()).throw(AssertionError("the test passes its own client"))  # noqa: E731
    try:
        with patched(*_fake_mortgage(tmp), (api, "key_owner", lambda key="": "owner@example.com"),
                     (api, "make_client", no_client)):
            _run_end_to_end(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert (mo.OUT, mm.CACHE, mm.fetch_hmda, api.key_owner) == before   # every module back as it was found


def _run_end_to_end(tmp):
    loan = {"record_date": "2020-05-01", "loan_type": "New Conventional", "amount": 150_000, "due_date": "2050-06-01"}
    L = [listing(101, "101 Example St", 180_000, dom=420, flags=("is_vacant_property",), equity=20, rent=2_400),
         listing(102, "102 Example St", 160_000, dom=95, equity=100, rent=1_700),
         listing(103, "103 Example St", 170_000, dom=40, saved=False, equity=100, rent=1_700),
         listing(104, "104 Example St", 900_000, dom=10)]
    S = [sale(1000 + k, 95_000 + 1_000 * k) for k in range(8)] + [sale(2000 + k, 260_000 + 2_000 * k, "flip")
                                                                   for k in range(5)]
    D = {"101": detail(loans=[loan], sold="2020-04-28"), "102": detail(agent="Lee Agent", phone="5555550101"),
         "103": detail(agent="Kim Agent", phone="5555550102")}
    fm = FakeMap(L, S, D)
    m = api.resolve_market(county="Knox, TN", name="test-market")
    res = api.run(m, out=tmp / "run", shortlist=10, client=fm, today=TODAY)
    c = res["counts"]
    assert c["listings"] == 3                      # the $900,000 listing is over the buy box ceiling
    assert c["in_account"] == 2 and c["added"] == 0 and fm.adds == [] and fm.quotes == [["103"]]
    assert set(fm.detail_reads) == {"101", "102"}  # never a detail read on a listing outside the account
    assert c["screened_only"] == 1 and Path(res["workbook"]).exists() and Path(res["csv"]).exists()
    assert c["zillow_calls"] == 0                  # no --zillow, no paid call booked
    offers = mo.load_json(tmp / "run" / "offers.json")
    by = {r["address"]: r for r in offers["rows"]}
    r101 = by.get("101 Example St") or next(r for r in offers["excluded"] if r["address"] == "101 Example St")
    assert r101["agent"] == "Pat Agent" and r101["agent_phone"] == "(555) 555-0100"
    assert r101.get("mtg_balance") and r101["mtg_source"].startswith("Freddie Mac")
    assert r101["owed_pct"] and abs(r101["rent"] - 2_400) <= 60 and r101["cuts"] == 1
    assert r101["mtg_source"] == "Freddie Mac week"      # no HMDA cached here, so no local spread is claimed
    assert any("not in your account" in n for n in res["notes"])
    from openpyxl import load_workbook
    wb = load_workbook(res["workbook"])
    assert "More listings" in wb.sheetnames and wb["More listings"].max_row == 2
    texts = " ".join(str(v) for ws in wb.worksheets for row in ws.iter_rows(values_only=True) for v in row if v)
    assert chr(0x2014) not in texts and chr(0x2013) not in texts and "this is Ty" not in texts

    # The same market with --add --commit: the one listing outside the account is added, then read.
    res2 = api.run(m, out=tmp / "run2", shortlist=10, add=True, commit=True, client=fm, today=TODAY)
    assert fm.adds == [["103"]] and res2["counts"]["added"] == 1 and "103" in fm.detail_reads
    assert res2["counts"]["screened_only"] == 0


def test_a_failed_download_is_an_error_not_an_exit():
    """A library caller (the brain, a skill user's Python) gets an error to say, never a process exit."""
    def no_curl(*a, **k):
        raise FileNotFoundError("curl")
    with patched((mm.subprocess, "run", no_curl)):
        try:
            mm._get("https://example.com/data.csv")
            raise AssertionError("no error raised")
        except mm.DownloadFailed as exc:
            assert isinstance(exc, RuntimeError) and "curl is not installed" in str(exc)


def test_expect_email_refuses_the_wrong_account():
    tmp = Path(tempfile.mkdtemp())
    try:
        out = tmp / "x"
        out.mkdir()
        mo.save_json(out / "listings.json", {"rows": [{"id": "9", "saved": False}]})
        mo.save_json(out / "shortlist.json", {"ids": ["9"]})
        fm = FakeMap([], [], {})
        with patched((api, "key_owner", lambda key="": "someone@example.com")):
            try:
                api.phase_add(fm, out, commit=True, expect_email="owner@example.com")
                raise AssertionError("added to the wrong account")
            except RuntimeError as exc:
                assert "not owner@example.com" in str(exc) and fm.adds == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
