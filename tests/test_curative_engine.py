"""The curative engine: ranking, county readers, heir research runner, phone checks, workbook, and the hand-off to
the loader. Zero network: every client is a fake. Every name, street and number here is invented.

Run: python -m pytest tests/test_curative_engine.py
"""
import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from curative_engine import account, assemble, common as C, counties, deaths, family, phones, score, stories, workbook  # noqa: E402

TODAY = date(2026, 6, 1)


@pytest.fixture(autouse=True)
def pinned(monkeypatch, tmp_path):
    monkeypatch.setenv("CURATIVE_TODAY", TODAY.isoformat())
    monkeypatch.setenv("CURATIVE_DIR", str(tmp_path / "cur"))
    for k in ("ENFORMION_AP_NAME", "ENFORMION_AP_PASSWORD", "SCRAPFLY_KEY", "TRESTLE_API_KEY", "TRESTLE_PAID_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(C, "load_env", lambda: {})
    yield


def ws(name="sample-tn"):
    return C.Workspace(name)


# ======================================================================================== names and dates
def test_first_names_never_cross_gender_and_stems_match():
    assert C.first_ok("JACK", "JACKSON")
    assert not C.first_ok("ERNEST", "ERNESTINE")
    assert not C.first_ok("PAUL", "PAULA")
    assert C.person_on("John Q Sample", "SAMPLE JOHN Q & JANE SAMPLE")
    assert not C.person_on("Jane Doe", "SAMPLE JOHN Q")


def test_estate_words_and_companies():
    assert C.is_estate("Estate of John Sample")
    assert C.is_estate("SAMPLE JOHN HEIRS")
    assert not C.is_estate("Example Real Estate Holdings LLC")
    assert C.is_entity("Example Holdings LLC")
    assert not C.is_entity("Estate of John Sample")


def test_roll_people_splits_last_first_pairs():
    people, etal = C.roll_people("SAMPLE JOHN A & MARY B")
    assert people == ["SAMPLE JOHN A", "MARY B SAMPLE"] and not etal
    people, etal = C.roll_people("DOE JANE ETAL")
    assert people == ["DOE JANE"] and etal


def test_split_names_drop_spelled_middles():
    assert C.split_name("John Quincy Sample") == ("John", "", "Sample")
    assert C.split_name("John Q Sample") == ("John", "Q", "Sample")
    assert C.split_roll_name("SAMPLE JOHN Q") == ("John", "Q", "Sample")


def test_today_is_pinned():
    assert C.today() == TODAY


# ======================================================================================== obituary matching
def rows_for(*lines):
    return deaths.index_rows("\n".join(lines))


def test_age_confirms_a_death():
    rows = rows_for("SAMPLE, JOHN Q | age 84 | News | obituary date Mar 03, 2025")
    v, row = deaths.match_person("John Q Sample", rows, owner_age=85, obit_day=None, last_sale=None, today=TODAY)
    assert v == "confirmed" and row["age"] == 84


def test_a_namesake_who_died_before_the_purchase_is_not_the_owner():
    rows = rows_for("SAMPLE, JOHN | age 70 | News | obituary date Mar 03, 2001")
    v, _ = deaths.match_person("John Sample", rows, owner_age=60, obit_day=None, last_sale=date(2010, 1, 1), today=TODAY)
    assert v == "not found"


def test_jr_and_sr_are_two_people():
    rows = rows_for("SAMPLE, JOHN SR | age 90 | News | obituary date Mar 03, 2020")
    v, _ = deaths.match_person("John Sample Jr", rows, owner_age=60, obit_day=None, last_sale=None, today=TODAY)
    assert v == "not found"


def test_spelled_middle_names_that_differ_are_two_people():
    rows = rows_for("SAMPLE, JOHN MICHAEL | age 80 | News | obituary date Mar 03, 2024")
    v, _ = deaths.match_person("John Matthew Sample", rows, owner_age=82, obit_day=None, last_sale=None, today=TODAY)
    assert v == "not found"


def test_two_people_by_one_name_is_only_likely():
    rows = rows_for("SAMPLE, JOHN | age 80 | News | obituary date Mar 03, 2024",
                    "SAMPLE, JOHN | age 41 | News | obituary date Jan 05, 2019")
    v, _ = deaths.match_person("John Sample", rows, owner_age=82, obit_day=None, last_sale=None, today=TODAY)
    assert v == "likely"


# ======================================================================================== county readers
CITY_HTML = """
<html>Owner Names: SAMPLE JOHN Q Assessed Value: 40,000
Taxes and Fees for Prior Years: $812.40 Total Balance Due: $1,500.00
<h3>Tax Summary</h3><table>
<tr><td>2026</td><td>x</td><td>DUE</td><td></td><td>$700.00</td><td>$700.00</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td></td><td></td><td>$700.00</td></tr>
<tr><td>2024</td><td>x</td><td>DUE</td><td></td><td>$650.00</td><td>$650.00</td><td>$50.00</td><td>$12.40</td><td>$100.00</td><td></td><td></td><td>$812.40</td></tr>
<tr><td>2023</td><td>x</td><td>PAID</td><td>01/01/2024</td><td>$640.00</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table></html>"""


def test_city_page_reads_prior_years_and_a_tax_suit():
    got = counties.parse_knoxville(CITY_HTML, this_year=2026)
    assert got["owner"] == "SAMPLE JOHN Q" and got["prior"] == 812.40
    by = {y["year"]: y for y in got["years"]}
    assert by["2024"]["owed"] == 812.40 and by["2023"]["owed"] == 0.0
    assert got["suit"] is True


def test_resolve_counties():
    assert counties.resolve("Knox, TN").fips == "47093"
    assert counties.resolve("47009").name == "Blount"
    other = counties.resolve("Polk County, FL")
    assert other.name == "Polk" and other.state == "FL" and not other.roll_key and not other.sources()
    # any US county, from the Census table: the FIPS a SiftMap search needs, by name or by code
    assert other.fips == "12105"
    assert (counties.resolve("12105").name, counties.resolve("12105").state) == ("Polk", "FL")
    assert counties.resolve("St. Louis, MO").fips == "29189"
    assert counties.resolve("Franklin, OH").fips == "39049"
    assert counties.resolve("Nowhere, TN").fips == ""


def probate_csv(path):
    rows = [
        {"Docket": "P-1", "Name": "SAMPLE JOHN Q", "Type": "TESTATE", "Title": "JOHN QUINCY SAMPLE", "Date Open": "05/01/2023",
         "Status": "CLOSED", "Date Closed": "01/01/2024", "Will Book-Page Number": "12-3"},
        {"Docket": "P-2", "Name": "DOE JANE", "Type": "INTESTATE", "Title": "JANE ANN DOE", "Date Open": "06/01/2023",
         "Status": "OPEN", "Date Closed": "", "Will Book-Page Number": ""},
        {"Docket": "P-3", "Name": "DOE JANE", "Type": "INTESTATE", "Title": "JANE BETH DOE", "Date Open": "06/01/2023",
         "Status": "OPEN", "Date Closed": "", "Will Book-Page Number": ""},
        {"Docket": "P-4", "Name": "ROE RICHARD", "Type": "INTESTATE", "Title": "RICHARD ROE", "Date Open": "01/01/2001",
         "Status": "CLOSED", "Date Closed": "01/01/2002", "Will Book-Page Number": ""},
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def test_probate_index_matches_by_name_middle_and_window(tmp_path):
    idx = counties.ProbateIndex(probate_csv(tmp_path / "p.csv"))
    hits = idx.lookup("John Q Sample", date(2023, 3, 1))
    assert hits and hits[0]["kind"] == "will" and hits[0]["sure"]
    jane = idx.lookup("Jane Ann Doe", date(2023, 1, 1))
    assert [c["docket"] for c in jane] == ["P-2"]                   # Jane Beth is someone else
    assert idx.lookup("Richard Roe", date(2020, 1, 1)) == []         # opened before the death
    assert idx.covers_to == date(2023, 6, 1)


def test_read_roll_uses_the_fake_county(monkeypatch):
    from agents.autopilot import taxwatch as tw
    monkeypatch.setitem(tw.COUNTY_ROLLS, "samplecounty", "https://roll.example/api/v2")
    bills = {"due": {"PPT": {"bills": [
        {"taxYear": 2024, "delinquent": True, "paid": False, "due": 900.0, "bill_type": "real", "turnedOver": True,
         "payments": [], "parcelInformation": {"mailingAddress": "100 EXAMPLE ST", "cityNumber": "386"}},
        {"taxYear": 2023, "delinquent": True, "paid": True, "due": 0, "bill_type": "real",
         "payments": [{"payor": "DOE JANE", "date": "2024-02-01", "amount": 800}], "parcelInformation": {}}]}}}

    def fetch(url):
        if "/parcels/" in url:
            return {"parcels": [{"parcel_address": "100 EXAMPLE ST", "account_number": "A1", "owner": "SAMPLE JOHN Q"}]}
        return bills
    got = counties.read_roll("samplecounty", "100 Example St", "John Sample", fetch=fetch)
    assert got["status"] == "ok" and got["chosen"] == "A1"
    view = assemble.tax_view(got)
    assert view["amount"] == 900.0 and view["years"] == ["2024"] and view["turned_over"]
    assert view["last_payor"] == "DOE JANE" and view["city_codes"] == ["386"]


# ======================================================================================== the account
class FakeCRM:
    def __init__(self, records, lists, tags=None, custom=None):
        self.records, self.lists, self.tags, self.custom = records, lists, tags or {}, custom or {}
        self.reads = 0

    def list_uuid(self, title):
        return "L-" + title if title in self.lists else ""

    def tag_uuid(self, title):
        return "T-" + title if title in self.tags else ""

    def _match(self, must):
        if "any_lists" in must:
            t = must["any_lists"][0][2:]
            out = [u for u in self.lists[t]]
        else:
            out = [u for u in self.tags[must["any_tags"][0][2:]]]
        if "created" in must:
            lo, hi = must["created"]
            out = [u for u in out if lo <= self.records[u]["created"] < hi]
        return [{"uuid": u, "address": {"state": self.records[u]["address"]["state"]}} for u in out]

    def count(self, must):
        return len(self._match(must))

    def rows(self, must, cap=10000):
        return self._match(must)[:cap]

    def record(self, u):
        self.reads += 1
        return json.loads(json.dumps(self.records[u]))

    def custom_fields(self, u):
        return dict(self.custom.get(u) or {})

    def call(self, path, *a, **k):
        return {"results": [{"uuid": "F1", "options": [{"uuid": "OPT-YES", "label": "Yes"}]}]}


def rec(uuid, street, county="Sample", state="TN", created="2025-01-01", **kw):
    base = {"uuid": uuid, "created": created, "dataflik_id": kw.pop("df", None), "lists": kw.pop("lists", []),
            "tags": kw.pop("tags", []), "status": kw.pop("status", None),
            "address": {"street": street, "city": "Exampleton", "state": state, "postal_code": "37000", "county": county},
            "owner": {"first_name": kw.pop("first", "John"), "last_name": kw.pop("last", "Sample"), "company": None,
                      "age": kw.pop("age", 80), "deceased": kw.pop("deceased", False),
                      "address": {"street": kw.pop("mail", street), "state": state},
                      "phones": [{"number": "8650000001", "status": "UNKNOWN"}], "emails": ["x@example.com"]},
            "estimate_value": str(kw.pop("value", 250000)), "equity_percent": str(kw.pop("eq", 100.0)),
            "secondary_owners": kw.pop("second", []), "year": 1975, "sqft": 1400.0}
    base.update(kw)
    return base


def test_scan_splits_past_the_wall():
    records = {"u%d" % i: rec("u%d" % i, "%d Example St" % i, created="2025-01-%02d" % (1 + i % 28)) for i in range(30)}
    crm = FakeCRM(records, {"Obituary": list(records)})
    got = account.scan(crm, {"any_lists": ["L-Obituary"]}, wall=8, start="2025-01-01", end="2025-02-01")
    assert sorted(r["uuid"] for r in got) == sorted(records)


def test_pull_reads_records_drops_phones_and_keeps_only_the_county():
    records = {"a": rec("a", "100 Example St", lists=["Obituary"]), "b": rec("b", "200 Sample Ave", county="Other", lists=["Obituary"]),
               "c": rec("c", "300 Demo Rd", state="GA", lists=["Obituary"])}
    crm = FakeCRM(records, {"Obituary": ["a", "b", "c"]}, custom={"a": {"Owner Deceased": "OPT-YES"}})
    w = ws()
    s = account.pull(w, crm, None, lists=["Obituary"], counties=["Sample"], states=["TN"], out=lambda *a: None)
    assert s["found"] == 2 and s["in_scope"] == 1                       # GA never read; "Other" county read, then left out
    saved = w.read("records.json")
    assert "phones" not in saved["a"]["record"]["owner"] and saved["a"]["record"]["owner"]["phone_count"] == 1
    assert saved["a"]["custom"]["Owner Deceased"] == "Yes"              # a select's option uuid becomes its label
    assert w.read("scope.json")["uuids"] == ["a"]
    reads = crm.reads
    account.pull(w, crm, None, lists=["Obituary"], counties=["Sample"], states=["TN"], out=lambda *a: None)
    assert crm.reads == reads                                            # fresh records are not read again


# ======================================================================================== assemble and score
def details(df, owner, second=(), assessed=60000, free_clear=True, flags=(), sales=(), loans=()):
    return {"fips": "47093", "county": "Knox", "state": "TN",
            "owner_info": {"owner_name": owner, "secondary_owner_names": list(second)},
            "total_assessed_value": assessed, "total_market_value": 999999,
            "distressor_data": {f: {"is_active": True} for f in flags},
            "presets": {"free_clear": free_clear}, "sale_history": list(sales), "mortgage_history": list(loans)}


def roll(owner, years=(), payor=""):
    bills = [{"year": str(y), "type": "real", "delinquent": True, "paid": False, "due": 500.0, "turned_over": False,
              "payor": None, "city_no": "0"} for y in years]
    bills.append({"year": "2022", "type": "real", "delinquent": True, "paid": True, "due": 0, "payor": payor,
                  "pay_date": "2023-01-15", "city_no": "0"})
    return {"status": "ok", "chosen": "A1", "accounts": [{"account": "A1", "owner": owner, "bills": bills}]}


def build_world():
    records = {
        "one": {"record": rec("one", "100 Example St", county="Knox", df=11, lists=["Obituary"], deceased=True,
                              last_obituary_date="2025-03-01", mail="900 Sample Rd"), "custom": {}, "via": ["list Obituary"]},
        "two": {"record": rec("two", "200 Example St", county="Knox", df=22, lists=["Obituary"], first="Jane", last="Doe"),
                "custom": {}, "via": ["list Obituary"]},
        "co": {"record": rec("co", "300 Example St", county="Knox", df=33, lists=["Foreclosure"], first="", last=""),
               "custom": {}, "via": ["list Foreclosure"]},
        "sold": {"record": rec("sold", "400 Example St", county="Knox", df=44, lists=["Obituary"], deceased=True,
                               last_obituary_date="2024-01-01"), "custom": {}, "via": ["list Obituary"]},
        "lot": {"record": rec("lot", "500 Example St", county="Knox", df=55, lists=["Obituary"], deceased=True,
                              last_obituary_date="2024-01-01", value=300000), "custom": {}, "via": ["list Obituary"]},
    }
    records["co"]["record"]["owner"]["company"] = "Example Holdings LLC"
    det = {"11": details(11, "John Q Sample"),
           "22": details(22, "Jane A Doe & John B Doe"),
           "33": details(33, "Example Holdings LLC", flags=["is_notice_of_default"]),
           "44": details(44, "Mary Sample", sales=[{"sale_date": "2025-05-01", "sale_price": 210000, "buyer_name": "BUYER CO LLC"}]),
           "55": details(55, "Paul Sample", assessed=2000)}
    county = {"one": roll("SAMPLE JOHN Q", years=("2024", "2025")), "two": roll("DOE JANE A & JOHN B")}
    return records, det, county


def test_assemble_joins_and_gates():
    records, det, county = build_world()
    rows = {r["street"]: r for r in assemble.build(records, det, county, {}, research={})}
    one = rows["100 Example St"]
    assert one["death_conf"] == 3 and one["title_holder_dead"] is None or one["death_conf"] == 3
    assert one["tax_years"] == ["2024", "2025"] and one["tax_amount"] == 1000.0
    assert one["appraised"] == 240000 and one["free_clear"] and one["mortgage"] == 0
    assert one["absentee"] is True and one["fips"] == "47093"
    assert rows["300 Example St"]["gate"] == "company owner"
    assert rows["400 Example St"]["gate"].startswith("sold after the death")
    assert rows["200 Example St"]["deed_count"] == 2


def test_score_tiers_and_traps():
    records, det, county = build_world()
    joined = assemble.build(records, det, county, {}, research={})
    out = {r["street"]: score.score_row(r, obits={}, today=TODAY) for r in joined}
    one = out["100 Example St"]
    assert one["tier"] == "1 Work now" and one["play"].startswith("Magic three"), one["why"]
    assert out["300 Example St"]["tier"] == "Out"
    assert out["500 Example St"]["disq"].startswith("county appraisal under 10%")
    two = out["200 Example St"]
    assert two["tier"] != "1 Work now"                                 # a possible death with two owners never works now


def test_spouse_trap_when_only_the_second_owner_has_an_obituary():
    records, det, county = build_world()
    joined = {r["street"]: r for r in assemble.build(records, det, county, {}, research={})}
    r = joined["200 Example St"]
    obits = {"47093|DOE|JOHN": "DOE, JOHN B | age 81 | News | obituary date Jan 10, 2024"}
    r = dict(r, owner_age=None)
    got = score.score_row(r, obits=obits, today=TODAY)
    assert got["spouse_trap"] or got["survivor_risk"]
    assert got["tier"] != "1 Work now"


def test_a_will_in_the_probate_index_makes_it_a_probate_deal(tmp_path):
    records, det, county = build_world()
    joined = {r["street"]: r for r in assemble.build(records, det, county, {}, research={})}
    r = dict(joined["100 Example St"], death_day="2023-03-01", months_since_death=38.0)
    idx = counties.ProbateIndex(probate_csv(tmp_path / "p.csv"))
    got = score.score_row(r, obits={}, probate={"47093": idx}, today=TODAY)
    assert got["play"] == "Probate open: a probate deal" and "will probated" in got["probate_index_case"]


def test_an_heir_story_overrides_the_tier():
    records, det, county = build_world()
    r = [x for x in assemble.build(records, det, county, {}, research={}) if x["street"] == "100 Example St"][0]
    alive = score.score_row(r, obits={}, stories={"100 Example St": {"verdict": "Not curative (owner alive)"}}, today=TODAY)
    assert alive["tier"] == "Out" and "alive" in alive["disq"]
    small = score.score_row(r, obits={}, stories={"100 Example St": {"verdict": "Under $50K a share", "heirs": 9,
                                                                     "equity_per_heir": 20000}}, today=TODAY)
    assert small["tier"] == "Out"


# ======================================================================================== family trees
def test_family_tree_orders_heirs_and_joins_by_address(tmp_path):
    w = ws()
    folder = w.work / "smartskip" / "20260101_000000"
    folder.mkdir(parents=True)
    C.write_json(folder / "ss_rows.json", [{"First Name": "John", "Last Name": "Sample", "Mailing Address": "100 Example St",
                                             "Property Address": "100 Example St", "_key": "df:11"}])
    C.write_json(folder / "ss_parsed.json", [{"first": "John", "last": "Sample", "mailing_address": "100 Example St",
                                               "deceased": True, "has_results": True, "subject_phones": [],
                                               "relatives": [{"first": "Ann", "last": "Sample", "name": "ANN SAMPLE",
                                                              "canon_rel": "Child", "age": "55", "phones": [{"number": "8650000002"}]},
                                                             {"first": "Bo", "last": "Roe", "name": "BO ROE", "canon_rel": "In-Law"}]}])
    fam = family.build(w, out=lambda *a: None)
    f = fam["df:11"]
    assert f["heirs"] == ["Ann Sample"] and f["all_deceased"] and f["heir_order"] == "spouse and children"


def test_family_picks_skip_traced_and_researched_houses():
    scored = [{"key": "k1", "tier": "1 Work now", "deed": ["John Q Sample"], "street": "100 Example St", "city": "X", "zip": "37000"},
              {"key": "k2", "tier": "2 Research first", "deed": ["Jane Doe"], "tags": ["Deep Prospected"], "street": "2 Ex"},
              {"key": "k3", "tier": "Out", "deed": ["Al Roe"], "street": "3 Ex"}]
    rows = family.picks(scored, have={})
    assert [r["_key"] for r in rows] == ["k1"] and rows[0]["First Name"] == "John" and rows[0]["Middle Name"] == "Q"


# ======================================================================================== heir research
def scored_for_stories():
    return [{"key": "df:11", "street": "100 Example St", "city": "Exampleton", "state": "TN", "zip": "37000", "fips": "47093",
             "tier": "1 Work now", "play": "Ghost property", "score": 80, "conf2": 3, "equity": 240000, "avm": 250000,
             "appraised": 200000, "deed": ["John Q Sample"], "decedents": [], "death_day2": "2024-03-01", "n_owners": 1,
             "dms": [["Ann Sample", "daughter"]], "fc_ahead": False},
            {"key": "df:55", "street": "500 Example St", "city": "Exampleton", "state": "TN", "zip": "37000", "fips": "47093",
             "tier": "2 Research first", "play": "Ghost property", "score": 70, "conf2": 2, "equity": 290000, "avm": 300000,
             "appraised": 8000, "deed": ["Paul Sample"], "n_owners": 1}]


def test_select_leaves_out_lots_and_prepare_writes_inputs():
    w = ws()
    w.write("scored.json", scored_for_stories())
    w.write("obits.json", {"47093|SAMPLE|JOHN": "SAMPLE, JOHN Q | age 84 | News | obituary date Mar 03, 2024"})
    q = stories.select(w, top=5, out=lambda *a: None)
    assert [x["slug"] for x in q] == ["100-example-st"] and q[0]["path"] == "X"
    assert stories.prepare(w, out=lambda *a: None) == 1
    inp = C.read_json(w.houses / "100-example-st" / "input.json")
    assert inp["obituary_index_rows"] and inp["crm_says"]["dms"] == [["Ann Sample", "daughter"]]
    b = stories.brief(q[0])
    assert "Slug: 100-example-st" in b and "Limits:" in b


def test_the_research_session_gets_no_secrets(monkeypatch):
    monkeypatch.setenv("REISIFT_API_KEY", "k-should-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-should-not-pass")
    monkeypatch.setenv("SMARTSKIP_PASSWORD", "p")
    monkeypatch.setenv("SOME_WEBHOOK_URL", "w")
    monkeypatch.setenv("CURATIVE_PEOPLE_RUN_CAP", "5")
    env = stories.child_env("plan")
    for k in ("REISIFT_API_KEY", "ANTHROPIC_API_KEY", "SMARTSKIP_PASSWORD", "SOME_WEBHOOK_URL"):
        assert k not in env
    assert env["CURATIVE_PEOPLE_RUN_CAP"] == "5"
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
    assert stories.child_env("api")["ANTHROPIC_API_KEY"] == "a-should-not-pass"


def test_tools_header_only_offers_configured_tools():
    w = ws()
    h = stories.tools_header(w, "100-example-st", {})
    assert "No people-search tool" in h and "scrapfly" not in h
    h2 = stories.tools_header(w, "100-example-st", {"ENFORMION_AP_NAME": "x", "ENFORMION_AP_PASSWORD": "y", "SCRAPFLY_KEY": "z"})
    assert "people --dir" in h2 and "--house 100-example-st" in h2 and "scrapfly_browser.py" in h2


class FakeProc:
    def __init__(self, folder, outcome, stdout):
        self.folder, self.outcome, self.stdout, self.pid = folder, outcome, stdout, 1
        self.stdin = SimpleNamespace(write=lambda s: None, close=lambda: None)

    def poll(self):
        if self.outcome == "done":
            C.write_json(self.folder / "heirs.json", {"heirs": [{"name": "Ann Sample", "share": "1/1", "share_num": 1.0,
                                                                 "living": "yes", "confidence": "high"}],
                                                      "title_holders": [{"name": "John Q Sample", "alive": "no"}],
                                                      "confidence": "high", "will_or_probate": "none found"})
            (self.folder / "story.md").write_text("# 100 Example St: heir story\n", encoding="utf-8")
            self.stdout.write(json.dumps({"total_cost_usd": 2.5, "num_turns": 30, "result": "ok"}))
        elif self.outcome == "auth":
            self.stdout.write(json.dumps({"result": "Invalid API key. Please run /login", "subtype": "error"}))
        self.stdout.flush()
        return 0


def runner_world(outcome):
    w = ws()
    w.write("scored.json", scored_for_stories())
    q = stories.select(w, top=1, out=lambda *a: None)
    stories.prepare(w, out=lambda *a: None)
    lines = []
    procs = []

    def popen(cmd, **kw):
        assert cmd[1] == "-p" and "--max-budget-usd" in cmd and "Bash(python:*)" in cmd
        assert "REISIFT_API_KEY" not in kw["env"]
        p = FakeProc(Path(kw["cwd"]), outcome, kw["stdout"])
        procs.append(p)
        return p
    r = stories.Runner(w, run_dir=w.root / "runs", claude="claude", out=lines.append, popen=popen)
    return w, q, r, lines, procs


def test_runner_collects_a_finished_house(monkeypatch):
    monkeypatch.setattr(stories.time, "sleep", lambda s: None)
    w, q, r, lines, procs = runner_world("done")
    r.run(q, wait=False)
    st = C.read_json(w.stories / "run_state.json")
    assert st["houses"]["100-example-st"]["status"] == "done" and st["houses"]["100-example-st"]["usd"] == 2.5
    assert (w.houses / "100-example-st" / "story.md").exists()
    assert "heir_method" not in (w.root / "runs" / "100-example-st" / "CLAUDE.md").read_text(encoding="utf-8")
    md = (w.root / "runs" / "100-example-st" / "CLAUDE.md").read_text(encoding="utf-8")
    assert md.startswith("# This run") and "## Hard rules" in md


def test_runner_stops_when_claude_code_is_not_signed_in(monkeypatch):
    monkeypatch.setattr(stories.time, "sleep", lambda s: None)
    w, q, r, lines, procs = runner_world("auth")
    got = r.run(q, wait=False)
    assert got.get("stopped") == "not signed in"
    assert any("not signed in" in ln for ln in lines)


def test_runner_refuses_without_claude_code():
    w, q, r, lines, procs = runner_world("done")
    r.claude = ""
    with pytest.raises(SystemExit):
        r.run(q)


def test_verdicts():
    inp = {"equity": 200000, "avm": 250000, "appraised": 150000}
    heirs = [{"name": "A", "share": "1/2", "living": "yes", "confidence": "high"},
             {"name": "B", "share": "1/2", "living": "yes", "confidence": "high"}]
    assert stories.verdict({"heirs": heirs, "confidence": "high"}, inp, None)[0] == "Work now"
    assert stories.verdict({"heirs": heirs, "confidence": "high"}, inp, [])[0] == "Work now, no confirmed number yet"
    five = [dict(h, share="1/5") for h in heirs * 3]
    assert stories.verdict({"heirs": five, "confidence": "high"}, inp, None)[0] == "Under $50K a share"
    assert stories.verdict({"heirs": heirs, "confidence": "high", "will_or_probate": "TESTATE P-1 opened 2023"}, inp, None)[0] \
        == "Will or probate on file"
    assert stories.verdict({"title_holders": [{"alive": "yes"}]}, inp, None)[0] == "Not curative (owner alive)"
    lot = dict(inp, appraised=5000)
    assert stories.verdict({"heirs": heirs}, lot, None)[0].startswith("Not a house")


def test_summary_is_what_the_loader_reads(monkeypatch):
    monkeypatch.setattr(stories.time, "sleep", lambda s: None)
    w, q, r, lines, procs = runner_world("done")
    r.run(q, wait=False)
    stories.summarize(w, out=lambda *a: None)
    from agents.autopilot import curative_setup as cs
    houses = cs.work_now(w.stories)
    assert [h["slug"] for h in houses] == ["100-example-st"]
    d, inp, checked = cs.read_house(houses[0]["folder"])
    tree, _ = cs.build_tree("100-example-st", d, inp, checked)
    assert tree["heirs"][0]["name"] == "Ann Sample" and tree["street"] == "100 Example St"


def test_people_search_is_capped_and_needs_an_anchor(monkeypatch):
    w = ws()
    (w.houses / "100-example-st").mkdir(parents=True)
    got = stories.people_search(w, "100-example-st", "Ann", "Sample", post=lambda body: {"persons": []})
    assert "anchor" in got["error"]
    monkeypatch.setenv("CURATIVE_PEOPLE_HOUSE_CAP", "1")
    first = stories.people_search(w, "100-example-st", "Ann", "Sample", city="Exampleton", state="TN",
                                  post=lambda body: {"persons": [{"name": {"firstName": "Ann", "lastName": "Sample"},
                                                                  "age": 55, "phoneNumbers": [{"phoneNumber": "(865) 000-0003"}]}]})
    assert first["people_found"] == 1 and first["people"][0]["phones"][0]["number"] == "8650000003"
    second = stories.people_search(w, "100-example-st", "Ann", "Sample", city="Exampleton", state="TN",
                                   post=lambda body: {"persons": []})
    assert second["error"] == "lookup cap reached"
    assert stories.people_search(w, "../etc", "A", "B", city="X")["error"].startswith("unknown house")


# ======================================================================================== phones and the workbook
def test_phone_checks_write_the_file_the_loader_reads(monkeypatch):
    monkeypatch.setattr(stories.time, "sleep", lambda s: None)
    w, q, r, lines, procs = runner_world("done")
    r.run(q, wait=False)
    heirs = C.read_json(w.houses / "100-example-st" / "heirs.json")
    heirs["heirs"][0]["phones_found"] = ["865-000-0004"]
    C.write_json(w.houses / "100-example-st" / "heirs.json", heirs)
    assert phones.check(w, commit=False, out=lambda *a: None)["pairs"] == 1
    got = phones.check(w, commit=True, out=lambda *a: None,
                       get=lambda params: (200, {"phone.name_match": True, "phone.contact_grade": "A",
                                                 "phone.activity_score": 90, "phone.linetype": "Mobile"}))
    assert got["good"] == 1
    checked = C.read_json(w.houses / "100-example-st" / "phones_checked.json")
    assert checked[0]["phone"] == "8650000004" and checked[0]["match"]
    assert phones.check(w, commit=False, out=lambda *a: None)["pairs"] == 0      # never paid for twice


def test_workbook_builds_every_sheet():
    records, det, county = build_world()
    w = ws()
    joined = assemble.build(records, det, county, {}, research={})
    w.write("scored.json", [score.score_row(r, obits={}, today=TODAY) for r in joined])
    path = workbook.build(w, out=lambda *a: None)
    from openpyxl import load_workbook
    wb = load_workbook(path)
    for name in ("Overview", "Work list", "All ranked", "Taxes behind", "Probate deals", "Ruled out", "Sources and method"):
        assert name in wb.sheetnames
    text = " ".join(str(c.value) for sh in wb for row in sh.iter_rows() for c in row if c.value)
    assert "\u2014" not in text and "\u2013" not in text


# ======================================================================================== a fresh install's playbook
def test_placeholder_ids_from_the_public_release_read_as_unset():
    from agents.autopilot import curative as cur
    from agents.autopilot import policy
    nil = policy.PLACEHOLDER_ID
    got = policy.unset_placeholders({"a": nil, "b": [nil, "x"], "c": {"d": nil, "e": "keep"}})
    assert got == {"a": "", "b": ["x"], "c": {"d": "", "e": "keep"}}
    data = {"stages": {"curative_pipeline": {"kind": "curative", "owner": "sales", "mode": "plan",
                                             "board": "Curative Title", "tag": "Curative",
                                             "column_ids": {t: nil for t in cur.COLUMNS}}}}
    pol = policy.parse(data)
    assert cur.options(pol.stages["curative_pipeline"])["column_ids"] == {}     # learned from a card, never the zero id


# ======================================================================================== the command and hygiene
def test_next_says_what_to_run(capsys):
    import curative_title as T
    assert T.main(["next", "--market", "sample-tn"]) == 0
    assert "rank the market" in capsys.readouterr().out


ENGINE = sorted((SRC / "curative_engine").glob("*")) + [SRC / "curative_title.py"]


def test_no_dashes_and_nothing_from_a_real_account_in_the_engine():
    for p in ENGINE:
        if p.suffix not in (".py", ".md"):
            continue
        t = p.read_text(encoding="utf-8")
        assert "\u2014" not in t and "\u2013" not in t, p
        # no machine path and no account, record or list id: the engine reads every id from the user's own account
        assert "C:\\Users" not in t and "/home/" not in t, p
        assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", t), p
