"""Roma's deal workflows: quick screen, underwrite, offer sheet, deal buyers and the lender package.

Zero network: a fake DataSift, a fake SiftMap and a fake Zillow answer everything, and the real
transports (urllib for SiftMap, requests for Zillow) are shut while the section runs, so a call that
slipped past a fake fails loudly instead of reaching a vendor. Every name, street, id and number here
is invented (555 phone numbers, example.com mail, streets that do not exist).
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import io
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import config, store

TODAY = date.today()
SRC = Path(__file__).resolve().parents[1]
U_SUBJ = "aaaaaaaa-0000-4000-8000-000000000001"
U_NOMAP = "aaaaaaaa-0000-4000-8000-000000000002"
U_PLAIN = "aaaaaaaa-0000-4000-8000-000000000003"
U_OWNER = "bbbbbbbb-0000-4000-8000-000000000001"
LAT, LON = 35.9600, -83.9200
K_TXN = "extra_last_sale_investor_transaction_type"
THREAD = "1700000000.000100"


def _d(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _ms(days: int) -> int:
    return int(datetime.combine(TODAY - timedelta(days=days), time(12), tzinfo=timezone.utc).timestamp() * 1000)


def _record(uuid=U_SUBJ, street="12 Testwood Dr", lat=LAT, lon=LON, sqft=1300) -> dict:
    return {"uuid": uuid, "dataflik_id": "900001" if uuid == U_SUBJ else "",
            "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37900",
                        "county": "Knox", "latitude": lat, "longitude": lon},
            "owner": {"uuid": U_OWNER, "first_name": "Pat", "last_name": "Example",
                      "phones": [{"number": "8655550101", "type": "MOBILE"}]},
            "bedrooms": 3, "bathrooms": 1, "sqft": sqft, "year": 1955, "estimate_value": 180000,
            "equity_percent": 100, "structure_type": "Single Family Residential", "status": "new_lead",
            "lists": [], "tags": [], "notes": ""}


# id, street, dlat, dlon, beds, baths, sqft, estimate, deed price, days ago, buyer, cash
COMPS = [
    ("700001", "101 Sampleton Ave", 0.002, 0.001, 3, 1, 1300, 250000, 245000, 40, "Jamie Placeholder", False),
    ("700002", "102 Sampleton Ave", 0.003, -0.002, 3, 1, 1350, 255000, 250000, 70, "Robin Testcase", False),
    ("700003", "103 Sampleton Ave", -0.002, 0.003, 3, 1, 1250, 240000, 238000, 120, "Casey Mockup", False),
    ("700004", "104 Sampleton Ave", 0.004, 0.004, 3, 1, 1400, 262000, 258000, 200, "Drew Sample", False),
    ("700005", "105 Sampleton Ave", -0.004, -0.003, 3, 1, 1320, 248000, 99000, 90, "Acme Test Homes LLC", True),
    ("700006", "106 Sampleton Ave", 0.005, -0.004, 3, 1, 1280, 245000, 105000, 150, "Acme Test Homes LLC", True),
    ("700007", "107 Sampleton Ave", -0.005, 0.005, 3, 1, 1310, 246000, 5000, 60, "Lee Family Trust", False),
    ("700008", "108 Sampleton Ave", 0.006, 0.006, 3, 1, 1330, 251000, 247000, 30, "Morgan Example", False),
]


def _row(rid, street, lat, lon, beds, baths, sqft, est, corp=False) -> dict:
    return {"id": rid, "address": "%s, Knoxville, TN 37900" % street, "latitude": lat, "longitude": lon,
            "bedrooms": beds, "bathrooms": baths, "squareFeet": sqft, "estimatedValue": est, "lotAcres": 0.2,
            "mlsSold": not corp, "corporateOwned": corp}


def _sale(days, price, buyer, seller, cash):
    return {"sale_date": _d(days), "sale_price": price, "buyer_name": buyer, "seller_name": seller, "is_cash_sale": cash}


class FakeMap:
    """SiftMap: the comp search, the investor sweeps, the dead-area count and the deed reads."""

    def __init__(self, overlap: int = 0):
        self.rows, self.details, self.investor = [], {}, {"pending": [], "flip": []}
        for cid, street, dl, do, beds, baths, sqft, est, price, days, buyer, cash in COMPS:
            self.rows.append(_row(cid, street, LAT + dl, LON + do, beds, baths, sqft, est, "LLC" in buyer))
            self.details[cid] = {"sale_history": [_sale(days, price, buyer, "Prior Owner %s" % cid[-1], cash)],
                                 "owner_info": {"owner_name": buyer.upper()}}
        for i, (dl, days, price) in enumerate(((0.004, 30, 118000), (-0.006, 100, 125000), (0.008, 250, 132000)), 1):
            rid = "80000%d" % i
            self.investor["pending"].append(_row(rid, "%d Holder Test Rd" % (200 + i), LAT + dl, LON, 3, 1, 1300, 200000, True))
            self.details[rid] = {"sale_history": [_sale(days, price, "Acme Test Homes LLC", "Seller %d" % i, True)],
                                 "owner_info": {"owner_name": "ACME TEST HOMES LLC",
                                                "owner_mail_address": "9 Office Test Pl, Knoxville, TN 37900"}}
        self.investor["flip"].append(_row("800004", "300 Flipside Test Ct", LAT - 0.007, LON + 0.002, 3, 2, 1350, 270000))
        self.details["800004"] = {"sale_history": [_sale(20, 265000, "Taylor Homebuyer", "Bright Test Builders LLC", False),
                                                   _sale(200, 120000, "Bright Test Builders LLC", "Sam Oldowner", True)],
                                  "owner_info": {"owner_name": "TAYLOR HOMEBUYER"}}
        self.inv_total, self.overlap = 4, overlap
        self.bodies, self.reads = [], []

    def search(self, body):
        self.bodies.append(copy.deepcopy(body))
        f = body.get("filters") or {}
        if body.get("addresses") and body["addresses"][0].get("type") == "address":
            q = body["addresses"][0]["search"].split(",")[0]
            return {"total_results": 1, "data": [r for r in self.rows if r["address"].startswith(q)][:1]}
        if K_TXN in f:
            if body.get("size") == 1:
                return {"total_results": self.inv_total, "data": []}
            rows = [r for t in f[K_TXN] for r in self.investor.get(t, [])]
        else:
            rows = list(self.rows)
        size = int(body.get("size") or 250)
        start = int(body.get("result_index") or 0)
        start = 0 if start <= 1 else max(0, start - self.overlap)
        return {"total_results": len(rows), "data": rows[start:start + size]}

    def get_detail(self, pid):
        self.reads.append(str(pid))
        if str(pid) not in self.details:
            raise RuntimeError("no detail for %s" % pid)
        return copy.deepcopy(self.details[str(pid)])


class FakeCRM:
    """DataSift over the key: records, custom fields, the owner board, the log, notes, the search."""

    def __init__(self, records, fields=None):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in records}
        self.fields = fields or {}
        self.notes, self.writes, self.paths = {}, [], []

    def record(self, u):
        self.paths.append("record:%s" % u)
        r = self.recs.get(u)
        return dict(r, notes=self.notes.get(u, "")) if r else {}

    def custom_fields(self, u):
        return dict(self.fields.get(u, {}))

    def record_logs(self, u, event_types=None, limit=250):
        return [{"event_type": "owner.call.made", "timestamp": _d(3) + " 15:00:00"}]

    def message(self, u, text, pin=False):
        self.writes.append(("note", u, text))
        self.notes[u] = self.notes.get(u, "") + "\n" + text
        return ""

    def call(self, path, method="GET", body=None, headers=None):
        self.paths.append("%s %s" % (method, path))
        if path.startswith("/api/internal/owner/") and "/message/" in path:
            return {"results": [{"created": _d(5) + "T10:00:00Z", "message": "Seller wants a quick close",
                                 "author": {"first_name": "Avery", "last_name": "Staff"}}]}
        if path == "/api/internal/property/" and method == "POST":
            prefix = (((body or {}).get("query") or {}).get("must") or {}).get("search", "").split(":", 1)[-1].lower()
            return {"count": 0, "results": [r for r in self.recs.values()
                                            if (r["address"]["street"] or "").lower().startswith(prefix)]}
        if path.startswith("/api/internal/property/") and path.endswith("/custom-field/?limit=1000&offset=0"):
            u = path.split("/")[4]
            return {"results": [{"custom_field": {"label": k}, "value": v} for k, v in self.fields.get(u, {}).items()]}
        if path.startswith("/api/internal/property/") and "/logs/" in path:
            return {"results": self.record_logs("")}
        if path.startswith("/api/internal/property/") and path.count("/") == 5:
            return self.record(path.split("/")[4])
        raise AssertionError("unexpected DataSift call: %s %s" % (method, path))


def _zillow_items() -> list:
    def item(zpid, street, dl, do, price, days, home="SINGLE_FAMILY", sqft=1290, est=248000):
        return {"zpid": zpid, "streetAddress": street, "city": "Knoxville", "state": "TN", "zipcode": "37900",
                "latitude": LAT + dl, "longitude": LON + do, "bedrooms": 3, "bathrooms": 1, "livingArea": sqft,
                "homeType": home, "homeStatus": "RECENTLY_SOLD", "unformattedPrice": price, "dateSold": _ms(days),
                "zestimate": est}
    return [item("z1", "201 Mocktail Way", 0.003, -0.003, 243000, 50),
            item("z2", "202 Mocktail Way", -0.003, 0.002, 249000, 80, sqft=1340, est=252000),
            item("z3", "101 Sampleton Ave", 0.002, 0.001, 246000, 40),          # the same street as a deed: the deed wins
            item("z4", "900 Faraway Test Rd", 0.06, 0.06, 199000, 30),          # outside the radius
            item("z5", "5 Vacant Lot Test Ln", 0.001, 0.004, 40000, 60, home="LOT", sqft=0, est=45000)]


def _zillow_api():
    import zillow_market_api as zm

    class FakeZillow(zm.ZillowMarketAPI):
        def __init__(self):
            super().__init__(api_key="test-key")
            self.calls = 0

        def search(self, location, home_status="FOR_SALE", **params):
            self.calls += 1
            if home_status == "FOR_SALE":
                return [{"zpid": "a1", "streetAddress": "303 Listing Test St", "city": "Knoxville", "state": "TN",
                         "zipcode": "37900", "latitude": LAT + 0.002, "longitude": LON, "bedrooms": 3,
                         "bathrooms": 1, "livingArea": 1300, "homeType": "SINGLE_FAMILY", "homeStatus": "FOR_SALE",
                         "unformattedPrice": 255000}]
            lo, hi = params.get("min_price", 0), params.get("max_price", 10 ** 9)
            return [i for i in _zillow_items() if lo <= i["unformattedPrice"] <= hi]
    return FakeZillow()


@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _task(workflow, record="", inputs=None, details="", source="agent:sales", thread=THREAD, agent="sales"):
    tid, _ = store.enqueue(agent, workflow, record, subject="", force=True,
                           payload={"inputs": inputs or {}, "details": details}, source=source,
                           slack_channel="C0TEST" if thread else "", slack_thread=thread)
    return store.task(tid)


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


_PHONE = re.compile(r"(?<![\d$])(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")


def _clean(text: str) -> bool:
    return "\u2014" not in text and "\u2013" not in text and not _PHONE.search(text)


def run(check, tmp: Path) -> None:
    import urllib.request
    import requests
    import zillow_market_api as zm
    import deal_buyer_dp as dbp
    import deal_buyer_match as dbm
    saved = {"data": config.DATA_DIR, "ws": config.WORKSPACES_DIR, "cap": config.DAILY_CAP_USD, "mode": config.MODE,
             "urlopen": urllib.request.urlopen, "get": requests.get, "delay": zm.REQUEST_DELAY,
             "reg_m": dbm.REG_DIR, "reg_p": dbp.REG_DIR, "trace": dbp.phase_trace, "verify": dbp.phase_verify}

    def shut(*a, **k):
        raise AssertionError("the deal selftest tried to reach the network")

    config.DATA_DIR, config.WORKSPACES_DIR = tmp / "wf_deals", tmp / "workspaces"
    config.DAILY_CAP_USD, config.MODE = 100000.0, "dry"
    urllib.request.urlopen, requests.get, zm.REQUEST_DELAY = shut, shut, 0
    dbm.REG_DIR = dbp.REG_DIR = tmp / "no_registry"
    from .workflows import quick_screen as qs
    old_clients = (qs.CRM, qs.SIFTMAP, qs.ZILLOW)
    try:
        with _env(AGENTS_ZILLOW_CALL_USD="0.01", AGENTS_COMPS_DAILY_USD="5", AGENTS_DEALS_RUN_CAP_USD="2",
                  AGENTS_DEAL_READS_PER_DAY="1000", AGENTS_DEALS_DIR=None, AGENTS_ZILLOW_COMPS=None,
                  SIFTSTACK_DEAL_ROOM_CLIENTS=None, OPENWEBNINJA_API_KEY=None):
            _run(check, tmp, qs, dbm, dbp)
    finally:
        qs.CRM, qs.SIFTMAP, qs.ZILLOW = old_clients
        config.DATA_DIR, config.WORKSPACES_DIR = saved["data"], saved["ws"]
        config.DAILY_CAP_USD, config.MODE = saved["cap"], saved["mode"]
        urllib.request.urlopen, requests.get, zm.REQUEST_DELAY = saved["urlopen"], saved["get"], saved["delay"]
        dbm.REG_DIR, dbp.REG_DIR = saved["reg_m"], saved["reg_p"]
        dbp.phase_trace, dbp.phase_verify = saved["trace"], saved["verify"]


def _run(check, tmp, qs, dbm, dbp) -> None:
    import comp_package as cp
    import post_walkthrough as pw
    for key in ("deals:zillow_usd:%s" % store.today(), "deals:siftmap_reads:%s" % store.today()):
        store.kv_set(key, "0")
    _parsers(check, cp, qs)
    _siftmap_client(check)
    fake_map = FakeMap()
    crm = FakeCRM([_record(), _record(U_NOMAP, "14 Testwood Dr", lat=0, lon=0), _record(U_PLAIN, "16 Testwood Dr")],
                  fields={U_SUBJ: {"Asking Price": "$95,000", "Assignment Price": "$135,000"}})
    qs.CRM, qs.SIFTMAP, qs.ZILLOW = crm, fake_map, _zillow_api()
    _deed_comps(check, cp, tmp)
    _lead_and_hygiene(check, pw, crm)
    _quick_screen(check, qs, crm, fake_map)
    full = _underwrite(check, qs, pw, crm, fake_map, tmp)
    _offer_sheet(check, qs, tmp, full)
    _deal_buyers(check, qs, dbm, dbp, crm, fake_map, tmp)
    _lender(check, tmp)
    _source_hygiene(check)


# ------------------------------------------------------------------------------- parsers

def _parsers(check, cp, qs) -> None:
    print("  pasted inputs")
    shapes = {
        "bbox": ("35.996,36.016,-83.895,-83.840", "bbox", "bbox"),
        "two corners": ("35.996,-83.895 36.016,-83.840", "bbox", "corners"),
        "lat,lon points, closed": ("35.99,-83.90 36.00,-83.88 35.98,-83.87 35.99,-83.90", "polygon", "points"),
        "lat, lon points apart by semicolons": ("35.99, -83.90; 36.00, -83.88; 35.98, -83.87", "polygon", "points"),
        "lon,lat points": ("-83.90,35.99 -83.88,36.00 -83.87,35.98", "polygon", "points"),
        "a KML block from Google My Maps": (
            "<Placemark><name>Pocket</name><Polygon><outerBoundaryIs><LinearRing><coordinates>-83.90,35.99,0 "
            "-83.88,36.00,0 -83.87,35.98,0 -83.90,35.99,0</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            "</Placemark>", "polygon", "kml"),
        "bare KML tuples": ("-83.90,35.99,0 -83.88,36.00,0 -83.87,35.98,0", "polygon", "kml"),
        "a GeoJSON Feature": ('{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-83.90,35.99],'
                              '[-83.88,36.00],[-83.87,35.98],[-83.90,35.99]]]}}', "polygon", "geojson"),
        "a GeoJSON FeatureCollection": ('{"type":"FeatureCollection","features":[{"type":"Feature","geometry":'
                                        '{"type":"Polygon","coordinates":[[[-83.90,35.99],[-83.88,36.00],'
                                        '[-83.87,35.98]]]}}]}', "polygon", "geojson"),
        "a bare GeoJSON coordinates array": ("[[[-83.90,35.99],[-83.88,36.00],[-83.87,35.98]]]", "polygon", "geojson"),
    }
    for label, (text, kind, source) in shapes.items():
        b, why = cp.parse_boundary(text)
        ok = bool(b) and b["kind"] == kind and b["source"] == source and not why
        if ok and kind == "polygon":
            ok = b["points"][0] == (35.99, -83.90) and len(b["points"]) == 3
        if ok and kind == "bbox":
            ok = b["bbox"] == (35.996, 36.016, -83.895, -83.84)
        check(ok, "a boundary pasted as %s is read, latitude first" % label)
    for bad in ("the area by the school", "35.99,-83.90 36.00", '{"type": "Point", "coordinates": [-83.9, 35.99]}',
                "35.99,36.01,-83.89"):
        check(cp.parse_boundary(bad) == (None, cp.BOUNDARY_HELP),
              "a boundary it cannot read is refused with one sentence naming the shapes it can (%s)" % bad[:24])
    check(cp.parse_boundary("") == (None, ""), "no boundary is not an error")
    ring = cp.boundary_ring(cp.parse_boundary("35.99,-83.90 36.00,-83.88 35.98,-83.87")[0])
    check(ring[0] == ring[-1] and len(ring) == 4 and set(ring[0]) == {"lat", "lon"},
          "a boundary goes to SiftMap as a closed ring of lat and lon points")

    n1 = cp.parse_card_notes("2/1, 1946 sqft, built 1938", this_year=2026)
    check((n1.get("beds"), n1.get("baths"), n1.get("sqft"), n1.get("year_built"), n1["context"]) == (2, 1.0, 1946, 1938, ""),
          "'2/1, 1946 sqft, built 1938' reads as 2 bed, 1 bath, 1,946 sqft, built 1938")
    n2 = cp.parse_card_notes("3 bed 2 bath 1,400 sf 1962", this_year=2026)
    check((n2.get("beds"), n2.get("baths"), n2.get("sqft"), n2.get("year_built")) == (3, 2.0, 1400, 1962),
          "'3 bed 2 bath 1,400 sf 1962' reads as 3 bed, 2 bath, 1,400 sqft, built 1962 (a bare year counts)")
    n3 = cp.parse_card_notes("3br/2ba 1,250 sq ft year built: 1955 seller says roof is new", this_year=2026)
    check(n3.get("sqft") == 1250 and n3.get("year_built") == 1955 and n3["context"] == "seller says roof is new",
          "whatever is not a card fact is kept word for word as context")
    n4 = cp.parse_card_notes("closing 2/1/2026, 3/2 1400 sf", this_year=2026)
    check((n4.get("beds"), n4.get("baths")) == (3, 2.0) and "2/1/2026" in n4["context"],
          "a date like 2/1/2026 is never read as 2 bed 1 bath")
    check(cp.parse_card_notes("1946 sqft and 1938 and 1962", this_year=2026).get("year_built") is None,
          "two bare years leave the year unread rather than guessed")
    check(cp.parse_card_notes("99 bed 40 bath 50 sf", this_year=2026).keys() == {"context"},
          "a figure outside a plausible range is dropped, never clamped")
    check(cp.parse_streets("old state|nash rd") == ("old state|nash rd", "") and
          cp.parse_streets("Old State Rd, Nash Rd")[0] == "Old State Rd|Nash Rd",
          "a street list is the --streets form, and commas between names read as |")
    check(cp.parse_streets("bad(regex") == ("", cp.STREETS_HELP), "a street list that is not a pattern is refused in one sentence")
    area = qs.area_inputs({"boundary": "radius 0.75", "notes": "2/1, 1946 sqft, asking 95k"})
    check(area["radius"] and area["radius_mi"] == 0.75 and area["ask"] == 95000 and area["notes"]["sqft"] == 1946,
          "'radius 0.75' asks for comps in a circle, and an asking price in the notes is read")
    check(qs.area_inputs({"notes": ""}, details="3/1, 1,400 sqft")["notes_from"] == "the request",
          "with no notes, the card facts are read from the request's own words")
    check(qs.area_inputs({"boundary": "near the park"})["why"] == cp.BOUNDARY_HELP,
          "the workflow refuses an unreadable boundary with the same sentence")
    check(qs.money("$92,000") == 92000 and qs.money("92k") == 92000 and qs.money("n/a") == 0,
          "money reads $92,000 and 92k alike, and nothing else")
    check(qs.safe_text("call 865-555-0101 now") == "call ends 0101 now" and qs.safe_text("a\u2014b") == "a-b",
          "the text scrub keeps only the last four of a phone and turns a long dash into a hyphen")
    check(not qs.from_slack({"source": "", "event_id": 4}) and not qs.from_slack(
        {"source": "autopilot:lead_appointments"}) and qs.from_slack({"source": "agent:sales", "slack_thread": THREAD}),
          "only an employee's ask on a Slack thread counts as an ask in Slack")
    check(qs.region_for("Blount") == "blount" and qs.region_for("Sevier County") == "national"
          and qs.region_for("") == "knoxville", "labor region: Blount is Blount, an unknown county is national, "
                                               "no county is the team's market")


# ------------------------------------------------------------------------------- SiftMap client

def _siftmap_client(check) -> None:
    import urllib.request
    import siftmap_standalone as sm
    print("  siftmap client")
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def fake_urlopen(req, timeout=0):
        sent.append({"url": req.full_url, "method": req.get_method(), "auth": req.headers.get("Authorization", ""),
                     "body": json.loads(req.data.decode()) if req.data else None})
        if req.full_url.endswith("/properties/search/"):
            return Resp({"total_results": 2, "data": [{"id": "1", "address": "15 Other Test St, Knoxville, TN"},
                                                      {"id": "2", "address": "12 Testwood Dr, Knoxville, TN"}]})
        return Resp({"id": "2", "sale_history": []})

    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        c = sm.SiftMapClient(api_key="test-key", min_interval=0)
        c.search({"polygon": [], "filters": {}, "size": 5, "result_index": 0})
        check(sent[0]["url"] == "https://map.reisift.io/properties/search/" and sent[0]["method"] == "POST"
              and sent[0]["auth"] == "Api-Key test-key", "SiftMap search is the documented POST /properties/search/ on the Api-Key")
        d = c.resolve("12 Testwood Dr, Knoxville, TN")
        check(all("autocomplete" not in s["url"] for s in sent), "resolve looks an address up through search, never autocomplete")
        check(d.get("id") == "2" and sent[-1]["url"].endswith("/properties/detail/2/"),
              "resolve reads the detail of the row whose house number matches, not the first row")
    finally:
        urllib.request.urlopen = old


# ------------------------------------------------------------------------------- deed comps

def _deed_comps(check, cp, tmp) -> None:
    print("  deed comps")
    m = FakeMap()
    subj = {"lat": LAT, "lon": LON, "beds": 3, "sqft": 1300, "dataflik_id": "900001", "address": "12 Testwood Dr"}
    cache: dict = {}
    r = cp.siftmap_sold(subj, m, radius_mi=1.0, months=12, detail_cap=5, cache=cache)
    check(r["read"] == 5 and r["not_read"] == 3 and len(m.reads) == 5,
          "deed reads stop at the cap and the rest are counted as unread")
    check(r["dropped"].get("deed under $10,000 (not arm's length)", 0) <= 1 and all(l.price >= 10000 for l in r["sold"]),
          "a deed under $10,000 is never a comp")
    check(all(l.zpid.startswith("sm") and l.home_type == "SINGLE_FAMILY" for l in r["sold"]),
          "deed comps come back as the MarketListing rows the ARV math takes")
    check(all(l.zestimate > 0 for l in r["sold"]), "the comp's own estimate rides along for the condition bucket")
    r2 = cp.siftmap_sold(subj, m, radius_mi=1.0, months=12, detail_cap=8, cache=cache)
    check(r2["cached"] == 5 and r2["read"] == 3, "a deed read once is reused from the cache, not bought again")
    check(all(r["deed"]["date"] for r in cache.values()), "the cache holds the deed, not the owner")
    m2 = FakeMap(overlap=1)
    r3 = cp.siftmap_sold(subj, m2, radius_mi=1.0, months=12, detail_cap=20, cache={}, page_size=3, max_rows=20)
    ids = [l.zpid for l in r3["sold"]]
    check(len(ids) == len(set(ids)) == 7 and len(m2.bodies) > 1,
          "pages that overlap by a row never count one sale twice, and never stop before the last sale")
    poly = cp.boundary_ring(cp.parse_boundary("35.95,35.97,-83.93,-83.91")[0])
    cp.siftmap_sold(subj, m, radius_mi=1.0, months=12, detail_cap=0, cache={}, polygon=poly)
    check(m.bodies[-1]["polygon"] == poly, "a pasted boundary is the area SiftMap searches")
    check(cp.siftmap_sold({"lat": 0, "lon": 0}, m)["why"].startswith("the record has no map point"),
          "no map point says so instead of searching the world")
    check(cp.investor_activity(m, LAT, LON, months=6) == 4 and m.bodies[-1]["filters"][K_TXN] == list(cp.INVESTOR_TXN_TYPES),
          "the dead-area check is one search on every investor label, as a list")
    import sku_pricing
    st = sku_pricing.status()
    check(st["locked"] == sku_pricing.DATA_PATH.exists(), "the materials status says whether the locked list is on this machine")
    old = sku_pricing.DATA_PATH
    sku_pricing.DATA_PATH = tmp / "missing_lock.json"
    sku_pricing.reset_cache()
    try:
        check(sku_pricing.status() == {"locked": False, "stamp": "engine", "path": str(tmp / "missing_lock.json"),
                                       "why": "the locked list is not on this machine"},
              "a missing lock file is said plainly, not priced quietly off the engine tables")
    finally:
        sku_pricing.DATA_PATH = old
        sku_pricing.reset_cache()


# ------------------------------------------------------------------------------- the lead, by the key

def _lead_and_hygiene(check, pw, crm) -> None:
    print("  lead over the key")
    documented = ("/api/internal/property/{u}/", "/api/internal/property/{u}/custom-field/",
                  "/api/internal/owner/{u}/message/", "/api/internal/property/{u}/logs/", "/api/internal/property/")
    before = len(crm.paths)
    lead = pw.load_lead("", record_uuid=U_SUBJ, crm=crm)
    used = {re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{u}", p.split(" ", 1)[-1].split("?")[0])
            for p in crm.paths[before:] if " " in p}
    check(lead["available"] and lead["beds"] == 3 and lead["sqft"] == 1300 and lead["lat"] == LAT,
          "the lead is read over the Open API key with no Deal Room checkout")
    check(used <= set(documented), "the key lead uses documented routes only (%s)" % sorted(used))
    check(lead["messages"] and lead["messages"][0]["who"] == "Avery Staff", "the owner's message board is the documented board")
    check(lead["cards"] == [] and lead["url"].endswith(U_SUBJ), "cards are not read (no documented route), the record link is")
    row, why = pw.find_record(crm, "12 Testwood Dr")
    check(row and row["uuid"] == U_SUBJ and not why, "a street line finds its one record")
    two = FakeCRM([_record(), _record("cccccccc-0000-4000-8000-000000000009", "12 Testwood Dr")])
    check(pw.find_record(two, "12 Testwood Dr")[1].startswith("2 Sift records match"),
          "two records at one street line are refused, never guessed between")
    check(pw.find_record(crm, "12 Testwood")[0] is None,
          "the search matches a prefix, so a longer street it returns is not the house asked for")
    check(pw.deal_room_clients() is None, "with no setting there is no Deal Room checkout, so the key path is the only path")
    text = (SRC / "post_walkthrough.py").read_text(encoding="utf-8")
    check("Deal Room Coaching Call" not in text and "C:\\Users" not in text and "OneDrive\\Desktop" not in text,
          "post_walkthrough carries no path on Ty's machine")
    out = pw.load_lead("12 Nowhere Test Way", crm=FakeCRM([]))
    check(not out["available"] and "no Sift record" in out["reason"], "a missed lead degrades with its reason, never raises")


# ------------------------------------------------------------------------------- quick screen

def _quick_screen(check, qs, crm, fake_map) -> None:
    print("  quick screen")
    zillow = qs.ZILLOW
    t = _task("quick_screen", U_SUBJ, {"record": "12 Testwood Dr", "boundary": "", "streets": "", "notes": ""})
    r = qs.run(t)
    check(r["ok"] and r["value"] > 0 and r["call"] in ("Go", "No go", "Look closer"),
          "a screen on the record gives a value and a call")
    check(r["ask"] == 95000 and r["ask_from"] == "Asking Price", "the asking price comes off the record's field when none was said")
    check(zillow.calls == 9 and r["zillow"]["spent_usd"] == 0.09 and r["spent_usd"] == 0.09,
          "an ask in Slack buys the Zillow pull, even in dry mode, and says its cost (9 calls, $0.09)")
    check(float(store.kv_get("deals:zillow_usd:%s" % store.today())) == 0.09, "the pull counts against the day's comp budget")
    check(not crm.writes and "dry run" in r["note"], "a dry screen writes nothing to the record")
    files = Path(r["run_dir"]) / "files"
    check((files / "comps.csv").is_file() and (files / "screen.json").is_file(), "the comps and the screen are kept as files")
    csv_text = (files / "comps.csv").read_text(encoding="utf-8")
    check("900 Faraway Test Rd" not in csv_text and csv_text.count("101 Sampleton Ave") == 1,
          "Zillow rows outside the radius are clipped, and a street the deeds cover keeps the deed")
    text = qs.render(t, r)
    check(_clean(text) and "Zillow: 9 call(s), $0.09" in text and "Call:" in text,
          "the screen posts its call and its cost, with no phone number and no long dash")
    r2 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ}))
    check(zillow.calls == 9 and r2["zillow"]["cached_from"] == TODAY.isoformat(),
          "a second ask for the same ZIP and window reuses the pull instead of buying it again")
    check(r2["value"] == r["value"], "the same comps give the same value")
    zillow.calls = 0
    for label, src, thread in (("a DataSift webhook", "", ""), ("an autopilot stage", "autopilot:lead_appointments", "")):
        r3 = qs.run(_task("quick_screen", U_SUBJ, {}, source=src, thread=thread))
        check(r3["ok"] and zillow.calls == 0 and not r3["zillow"]["asked"],
              "a screen started by %s never buys the Zillow pull" % label)
    qs._zcache("37900", 18).unlink()
    with _env(AGENTS_COMPS_DAILY_USD="0.5"):
        r4 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ}))
    check(r4["ok"] and zillow.calls == 0 and "today's comp budget" in r4["zillow"]["why"],
          "a pull that could pass the day's comp budget is not made, and the screen says why")
    qs.ZILLOW = None
    r5 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ}))
    check(r5["ok"] and "OPENWEBNINJA_API_KEY is not set" in r5["zillow"]["why"],
          "no Zillow key on the box is said in one line, and the free comps still screen")
    qs.ZILLOW = zillow
    r6 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ, "notes": "3/1, 1,500 sqft, built 1955, seller is relocating"}))
    check("1,500 sqft" in r6["from_notes"] and r6["context"] == "seller is relocating",
          "card facts in the notes beat the record, and the rest is carried as context")
    r7 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ, "boundary": "the area by the school"}))
    check(not r7["ok"] and r7["why"].startswith("I could not read that boundary"), "an unreadable boundary stops the screen with the sentence")
    r8 = qs.run(_task("quick_screen", U_NOMAP, {"record": U_NOMAP}))
    check(not r8["ok"] and "no map point" in r8["why"], "a record with no map point is not screened, and says so")
    check(not qs.run(_task("quick_screen", "", {"record": ""}))["ok"], "no record named is a plain refusal")
    r11 = qs.run(_task("quick_screen", "", {"address": "12 Testwood Dr", "boundary": "radius"}))
    check(r11["ok"] and r11["uuid"] == U_SUBJ,
          "a screen named by street line finds its record itself, so a second ask with new notes is not deduped away")
    config.MODE = "live"
    try:
        before = len(crm.writes)
        r9 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ}))
        check(len(crm.writes) == before + 1 and r9["note"] == "note written to the record and read back",
              "a live screen writes one note on the record and reads it back")
        with _env(AGENTS_QUICK_SCREEN_NOTE="0"):
            r10 = qs.run(_task("quick_screen", U_SUBJ, {"record": U_SUBJ}))
        check(len(crm.writes) == before + 1 and "AGENTS_QUICK_SCREEN_NOTE is off" in r10["note"],
              "the record note can be switched off")
    finally:
        config.MODE = "dry"
    v = qs.verdict(value=200000, n_comps=5, rehab=30000, rule=0.7, fee=15000, est_value=150000, equity_pct=10,
                   ask=0, dead=0, structure="Single Family", buy_box_max=700000)
    check(v["call"] == "No go" and any("dead area" in x for x in v["no"]) and any("owes about" in x for x in v["no"]),
          "a dead area or an owner who owes more than we could pay is a no go, with the reason")
    v2 = qs.verdict(value=240000, n_comps=2, rehab=30000, rule=0.7, fee=15000, est_value=180000, equity_pct=100,
                    ask=150000, dead=3, structure="Single Family", buy_box_max=700000)
    check(v2["call"] == "Look closer" and v2["ceiling"] == 123000, "an ask over the ceiling or a thin comp set is look closer")
    check(qs.verdict(value=0, n_comps=0, rehab=0, rule=0.7, fee=15000, est_value=0, equity_pct=None, ask=0, dead=1,
                     structure="", buy_box_max=700000)["call"] == "No call", "no value is no call, never a guess")


# ------------------------------------------------------------------------------- underwrite

WALK = {"walk_date": "", "single_scenario": {"key": "reno", "label": "Comp-Match Reno (3/2)", "tier": 2, "scope": "full",
                                             "beds": 3, "baths": 2, "gut": False, "drop": []},
        "photo_notes": [{"file": "inbox/%s/kitchen1.jpg" % THREAD, "room": "Kitchen", "condition": "dated",
                         "finding": "Original cabinets, laminate counters", "scope_line": "Kitchen: cabinets and counters",
                         "quote": "cabinet doors sag and the counters are burned"},
                        {"file": "inbox/%s/bath1.jpg" % THREAD, "room": "Bath", "condition": "failing",
                         "finding": "Soft floor at the toilet", "scope_line": "Bath: subfloor and toilet reset"}],
        "flags": [{"item": "Termite inspection and treatment", "cost": 1800, "note": "frass at the sill", "scenarios": ["mid"]}],
        "work_done": [{"item": "Roof", "detail": "Replaced two years ago per the seller", "credit": 3000, "scenarios": []}]}


def _underwrite(check, qs, pw, crm, fake_map, tmp) -> dict:
    import openpyxl
    from .workflows import underwrite as uw
    print("  underwrite")
    searches = len(fake_map.bodies)
    zillow = qs.ZILLOW
    calls = zillow.calls
    t = _task("underwrite", "", {"address": "12 Testwood Dr", "walk": json.dumps(WALK), "boundary": "", "streets": "",
                                 "notes": "3/1, 1,300 sqft, roof was replaced in 2022"})
    r = uw.run(t)
    check(r["ok"] and r["mode"] == "rehab", "a walk with no comps asked for builds the rehab on its own")
    check(len(fake_map.bodies) == searches and zillow.calls == calls,
          "a rehab-only request pulls no comps, free or paid, even when asked in Slack")
    files = Path(r["run_dir"]) / "files"
    book = openpyxl.load_workbook(files / r["files"][0])
    check(book.sheetnames == ["Rehab", "Photo Findings", "Repair Detail", "Comps", "Offer"],
          "the rehab workbook carries the photo findings, the one number, the repair detail and plain comp and offer tabs")
    check(book["Comps"]["A3"].value == pw.NO_COMPS and book["Offer"]["A3"].value == pw.NO_OFFER,
          "the comp and offer tabs say no comps were asked for instead of rendering empty")
    pf = book["Photo Findings"]
    check(pf["A5"].value.endswith("kitchen1.jpg") and pf["F5"].value == "cabinet doors sag and the counters are burned"
          and pf["F6"].value == "Soft floor at the toilet" and pf.max_row == 8,
          "one row per photo with its evidence quote, then the walk's own findings")
    used = json.loads((files / r["files"][1]).read_text(encoding="utf-8"))
    check(used["single_scenario"]["baths"] == 1.0 and any("held to the 3 bed 1 bath" in b for b in r["beat"]),
          "a layout in the operator's notes beats the plan the photos suggested")
    subject = uw._pw_subject(dict(qs.subject_of(crm.record(U_SUBJ)), sqft=1300))
    expect = pw.build_rehab_matrix(subject, used)["scenarios"][0]["grand"]
    check(r["rehab"] == expect and len(pw.build_rehab_matrix(subject, used)["scenarios"]) == 1,
          "the rehab is ONE number, the module's own figure for the walk it used")
    check(any("Termite" in w and "not the plan 'reno'" in w for w in r["warnings"]),
          "a flag that names a scenario the plan does not use is named out loud, never dropped silently")
    check("roof was replaced in 2022" in r["context"] and "beat the photos" in used.get("note", ""),
          "a stated repair in the notes is carried into the workbook as the operator's word")
    text = uw.render(t, r)
    check(_clean(text) and "No comps were asked for" in text and "(rehab only)" in text, "the rehab-only post says what it is")

    walk_nosingle = {k: v for k, v in WALK.items() if k != "single_scenario"}
    r2 = uw.run(_task("underwrite", "", {"address": "77 Nowhere Test Ln, Knoxville, TN 37900",
                                         "walk": json.dumps(walk_nosingle), "boundary": "",
                                         "notes": "3 bed 1 bath 1,200 sqft built 1948"}))
    check(r2["ok"] and not r2["in_datasift"] and r2["defaulted"] and r2["rehab_label"].startswith("Mid Reno"),
          "a house not in DataSift gets its rehab from the walk and the notes, on the engine's mid plan when the walk names none")
    r3 = uw.run(_task("underwrite", "", {"address": "77 Nowhere Test Ln", "walk": json.dumps(WALK), "boundary": ""}))
    check(not r3["ok"] and "square footage" in r3["why"], "a house with no square footage anywhere is refused with what to send")

    ws = Path(config.WORKSPACES_DIR) / "sales" / "inbox" / THREAD
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "walk.json").write_text(json.dumps(WALK), encoding="utf-8")
    r4 = uw.run(_task("underwrite", U_SUBJ, {"address": U_SUBJ, "walk": "inbox/%s/walk.json" % THREAD}))
    check(r4["ok"] and r4["walk_from"] == "walk.json", "a walk file in the employee's workspace is read")
    outside = tmp / "elsewhere_walk.json"
    outside.write_text(json.dumps(WALK), encoding="utf-8")
    for ref in (str(outside), "../../elsewhere_walk.json"):
        r5 = uw.run(_task("underwrite", U_SUBJ, {"address": U_SUBJ, "walk": ref}))
        check(not r5["ok"] and "outside the workspace" in r5["why"],
              "a walk path outside the workspace and the run folders is refused (%s)" % ref[-24:])
    r6 = uw.run(_task("underwrite", U_SUBJ, {"address": U_SUBJ, "walk": "{not json"}))
    check(not r6["ok"] and "could not be read" in r6["why"], "broken inline JSON is refused in a sentence")

    full = uw.run(_task("underwrite", U_SUBJ, {"address": U_SUBJ, "walk": json.dumps(WALK), "boundary": "radius"}))
    check(full["ok"] and full["mode"] == "full" and full["offer"] and full["value"] > 0,
          "a walk with comps asked for builds the pack, the package and the offer")
    files = Path(full["run_dir"]) / "files"
    names = sorted(p.name for p in files.iterdir())
    check(any(n.endswith("_Offer.xlsx") for n in names) and any(n.endswith("_pack.json") for n in names)
          and any(n.endswith("_Post_Walkthrough.xlsx") for n in names), "the full shape writes all four files")
    pack = json.loads(next(files.glob("*_pack.json")).read_text(encoding="utf-8"))
    walk = json.loads(next(files.glob("*_walk.json")).read_text(encoding="utf-8"))
    check(len(pack["rehab"]["scenarios"]) == 1, "the package carries one rehab plan, never the menu")

    # The same comps, walk and county card through the command lines: post_walkthrough --save-pack, then
    # offer_sheet --pack. Every number must match what the workflow posted.
    d = tmp / "cli_equivalence"
    d.mkdir(exist_ok=True)
    (d / "spec.json").write_text(json.dumps({"address": "12 Testwood Dr", "sold_comps": pack["sold_comps"],
                                             "active_comps": pack["active_comps"],
                                             "land_comps": pack.get("land_comps") or []}), encoding="utf-8")
    (d / "walk.json").write_text(json.dumps(walk), encoding="utf-8")
    (d / "buyers.json").write_text(json.dumps({"records": [dict(v, address=k) for k, v in pack["deeds"].items()]}),
                                   encoding="utf-8")
    argv = sys.argv
    sys.argv = ["post_walkthrough.py", "--spec", str(d / "spec.json"), "--walkthrough", str(d / "walk.json"),
                "--buyers", str(d / "buyers.json"), "--no-sift", "--no-api", "--city", "Knoxville", "--state", "TN",
                "--zip", "37900", "--beds", "3", "--baths", "1", "--sqft", "1300", "--year-built", "1955",
                "--months", "18", "--save-pack", str(d / "cli_pack.json"), "--out", str(d / "cli.xlsx")]
    try:
        _quiet(pw.main)
        cli = json.loads((d / "cli_pack.json").read_text(encoding="utf-8"))
        sys.argv = ["offer_sheet.py", "--pack", str(d / "cli_pack.json"), "--walk", str(d / "walk.json"),
                    "--out", str(d / "cli_offer.xlsx")]
        buf = io.StringIO()
        import offer_sheet as osheet
        with contextlib.redirect_stdout(buf):
            osheet.main()
    finally:
        sys.argv = argv
    check(cli["arv"]["base"]["arv"] == pack["arv"]["base"]["arv"] == full["value"],
          "the workflow's value is the command line's value on the same comps")
    check(cli["rehab"]["totals"] == pack["rehab"]["totals"] and cli["exits"]["inputs"]["as_is_point"]
          == pack["exits"]["inputs"]["as_is_point"], "rehab and as-is match the command line too")
    m = re.search(r"OFFER TO SELLER \$(-?[\d,]+)", buf.getvalue())
    check(m and int(m.group(1).replace(",", "")) == full["offer"]["offer"],
          "the offer posted is the offer offer_sheet.py prints for the same pack and walk")

    pre = uw.run(_task("underwrite", U_SUBJ, {"address": U_SUBJ, "boundary": "radius"}))
    check(pre["ok"] and pre["mode"] == "prewalk" and not pre["offer"] and pre["value"] == full["value"],
          "with no walk there is a package and no offer, on the same value")
    check("No offer sheet" in uw.render({"id": 1}, pre), "the pre-walk post says why there is no offer")
    return {"task": full, "pack_name": next(files.glob("*_pack.json")).name}


# ------------------------------------------------------------------------------- offer sheet

def _offer_sheet(check, qs, tmp, full) -> None:
    import offer_sheet as osheet
    from .workflows import offer_sheet as ow
    print("  offer sheet")
    task_id = Path(full["task"]["run_dir"]).name.rsplit("-", 1)[1]
    t = _task("offer_sheet", "", {"pack": "task %s" % task_id, "walk": ""})
    r = ow.run(t)
    check(r["ok"] and r["offer"] == full["task"]["offer"]["offer"] and r["walk_from"].endswith("_walk.json"),
          "'task N' finds that underwrite's pack and the walk beside it, and prices the same offer")
    files = Path(full["task"]["run_dir"]) / "files"
    pack = json.loads((files / full["pack_name"]).read_text(encoding="utf-8"))
    walk = json.loads(next(files.glob("*_walk.json")).read_text(encoding="utf-8"))
    direct = osheet.build_offer(pack, walk, osheet.offer_args(), str(tmp / "direct_offer.xlsx"))
    check(direct["offer"] == r["offer"] and direct["buyer_max"] == r["buyer_max"],
          "the workflow's offer is build_offer's offer, the function the command line prints")
    r2 = ow.run(_task("offer_sheet", "", {"pack": "task %s" % task_id, "walk": json.dumps(
        {k: v for k, v in walk.items() if k != "single_scenario"})}))
    check(r2["ok"] and any("no single plan" in w for w in r2["warnings"]), "a walk with no single plan is warned as a menu")
    r3 = ow.run(_task("offer_sheet", "", {"pack": "task %s" % task_id, "fee": "12000"}))
    check(r3["offer"] == r["offer"] + 3000, "an operator's fee moves the offer dollar for dollar")
    check(not ow.run(_task("offer_sheet", "", {"pack": "missing_pack.json"}))["ok"], "a pack that is not there is a plain miss")
    r4 = ow.run(_task("offer_sheet", "", {"pack": str(tmp / "elsewhere_walk.json")}))
    check(not r4["ok"] and "outside the workspace" in r4["why"], "a pack path outside the allowed folders is not opened")
    text = ow.render(t, r)
    check(_clean(text) and "Offer to seller" in text, "the offer post is clean")


# ------------------------------------------------------------------------------- deal buyers

def _dossier() -> list:
    return [{"rank": 1, "buyer": "Acme Test Homes LLC", "group": "", "members": [], "score": 80,
             "decision_makers": [{"name": "Pat Builder", "role": "member", "confidence": "high", "source": "research/x",
                                  "via": "", "address": "44 Mockingbird Test Ln, Knoxville, TN 37900", "evidence": [],
                                  "seen": []}],
             "phones": [{"number": "8655550123", "type": "Mobile", "sources": ["datasift-native"], "status": "",
                         "traced_for": "", "via": "Acme Test Homes LLC"}], "crm_owner": ""}]


def _deal_buyers(check, qs, dbm, dbp, crm, fake_map, tmp) -> None:
    from .workflows import deal_buyers as db
    print("  deal buyers")
    called = []

    def refuse(*a, **k):
        called.append(a)
        raise AssertionError("a paid phase ran in a dry selftest")

    dbp.phase_trace, dbp.phase_verify = refuse, refuse
    with _env(AGENTS_DEAL_BUYERS_DEED_READS="10"):
        t = _task("deal_buyers", U_SUBJ, {}, source="", thread="")     # a webhook: a record and nothing else
        r = db.run(t)
    check(r["ok"] and not called, "a webhook start runs the free phases and no paid one")
    check(r["buyers"] and r["buyers"][0]["buyer"].upper() == "ACME TEST HOMES LLC",
          "the repeat local buyer ranks first")
    check(r["reads"] == 10 and r["unread"] == 3, "deed reads stop at the run cap, nearest first, and the unread count is kept")
    check(r["ask"] == 135000 and r["ask_from"] == "Assignment Price",
          "the buyer ask comes off the record's assignment price, never the seller's asking price")
    bodies = [b for b in fake_map.bodies if K_TXN in (b.get("filters") or {}) and b.get("size") != 1]
    check(bodies and all(b.get("size") == 100 and "addresses" in b for b in bodies),
          "the brain's sweep sends the documented page size and holds a Knox house to Knox")
    text = db.render(t, r)
    check(_clean(text) and "Nothing written to DataSift; nobody contacted" in text, "the buyer post carries no phone number")
    with _env(AGENTS_DEAL_BUYERS_DEED_READS="1"):
        r2 = db.run(_task("deal_buyers", U_PLAIN, {}, source="", thread=""))
    check(r2["ok"] and r2["ask"] == 0 and "price fit is not scored" in db.render({"id": 2}, r2),
          "no asking price anywhere is said, and no band is invented")
    with _env(AGENTS_DEAL_BUYERS_DEED_READS="0"):
        r0 = db.run(_task("deal_buyers", U_PLAIN, {}, source="", thread=""))
    check(not r0["ok"] and "deed reads are used" in r0["why"], "no reads left today is said, not run into a gate")
    check(not db.run(_task("deal_buyers", U_NOMAP, {}))["ok"], "no map point is a plain refusal")

    work = tmp / "paid_work"
    work.mkdir(exist_ok=True)
    (work / "dossier.json").write_text(json.dumps(_dossier()), encoding="utf-8")
    a = argparse.Namespace(commit=False, slug="paid")
    t2 = _task("deal_buyers", "", {})
    res = db.paid_steps(a, work, qs.Budget(t2["id"], 3.0), [])
    check(not called and res["would_usd"] == 0.18 and sum("(dry run)" in s for s in res["not_run"]) == 2,
          "dry mode prices SmartSkip and Real Contact and runs neither")
    config.MODE = "live"
    try:
        with _env(SMARTSKIP_EMAIL=None, SMARTSKIP_PASSWORD=None, TRESTLE_PAID_API_KEY=None, TRESTLE_API_KEY=None):
            res = db.paid_steps(a, work, qs.Budget(t2["id"], 3.0), [])
        check(not called and any("SMARTSKIP_EMAIL" in s for s in res["not_run"])
              and any("TRESTLE" in s for s in res["not_run"]), "live with no vendor keys says which key is missing")
        with _env(SMARTSKIP_EMAIL="x@example.com", SMARTSKIP_PASSWORD="x", TRESTLE_API_KEY="x"):
            res = db.paid_steps(a, work, qs.Budget(t2["id"], 0.01), [])
        check(not called and sum("would pass the cap" in s for s in res["not_run"]) == 2,
              "a paid step that would pass the run cap does not run")

        def fake_trace(args, out):
            assert args.commit
            dbp._save(out / "ledger.json", [{"key": "smartskip", "amount": 0.15, "note": "1 hit"}])
            dbp._save(out / "traced.json", {"Pat Builder": {"rank": 1, "via": "smartskip", "phones": ["8655550199"]}})

        def fake_verify(args, out):
            assert args.commit
            dbp._save(out / "verify.json", {"Pat Builder|8655550123": {"rank": 1, "name": "Pat Builder",
                                                                        "number": "8655550123"}})
        dbp.phase_trace, dbp.phase_verify = fake_trace, fake_verify
        with _env(SMARTSKIP_EMAIL="x@example.com", SMARTSKIP_PASSWORD="x", TRESTLE_API_KEY="x"):
            res = db.paid_steps(a, work, qs.Budget(t2["id"], 3.0), [])
        check(len(res["ran"]) == 2 and not a.commit and round(store.spent(task_id=t2["id"]), 2) == 0.21,
              "live, keyed and under the cap, both paid steps run and their spend is recorded")
    finally:
        config.MODE = "dry"
        dbp.phase_trace, dbp.phase_verify = refuse, refuse
    budget = qs.Budget(t2["id"], 100.0)
    old = config.DAILY_CAP_USD
    config.DAILY_CAP_USD = store.spent(day=store.today(), research=False) + 0.05
    try:
        check(not budget.allows(0.10, "a paid step") and "day $" in budget.skipped[-1],
              "the brain's day cap stops a paid step the run cap would allow")
    finally:
        config.DAILY_CAP_USD = old


# ------------------------------------------------------------------------------- lender package

def _spec() -> dict:
    return {"deal_name": "Test Deal One", "date": "November 1, 2026", "lender_name": "Example Lending Partners LLC",
            "lender_salutation": "Jordan", "closing_agent": "Example Title Company", "under_contract": True,
            "contract_price": 90000, "work_budget": 30000, "rehab_total": 30000, "deal_type": "flip",
            "borrower": {"entity": "Example Holdings LLC", "state": "TN", "signer": "Pat Example", "title": "Member",
                         "phone": "(865) 555-0100", "email": "pat@example.com", "address": "1 Example Plaza, Knoxville, TN",
                         "formation": "a Tennessee limited liability company", "members": ["Pat Example", "Sam Example"]},
            "property": {"full_address": "12 Testwood Dr, Knoxville, TN 37900", "short_address": "12 Testwood Dr",
                         "county": "Knox", "parcel_id": "000 00000", "type": "Single family", "sqft": 1300,
                         "lot": "0.20 ac", "beds": 3, "baths": 1, "year_built": 1955, "condition": "dated",
                         "occupancy": "vacant", "zoning": "residential"},
            "loan": {"amount": 110000, "rate": 0.12, "points": 2, "term_months": 6, "min_interest_months": 3,
                     "draws": 2, "lien": "first", "payment": "interest at payoff", "default_terms": "liquidate"},
            "resale": {"point": 240000, "lo": 225000, "hi": 255000, "basis": "five comps"},
            "as_is": {"point": 120000, "basis": "investor buys"},
            "holding": {"taxes_annual": 1200, "insurance_annual": 1800, "electric_monthly": 80, "water_monthly": 40},
            "buying_costs": {"escrow": 900, "title_pct": 0.0077, "recording": 100},
            "selling_costs": {"escrow": 900, "realtor_pct": 0.05, "transfer_pct": 0.0037}}


def _lender(check, tmp) -> None:
    import lender_package as lp
    from .workflows import lender_package as lw
    print("  lender package")
    spec = _spec()
    t = _task("lender_package", "", {"spec": json.dumps(spec)})
    r = lw.run(t)
    m = lp.compute(spec)
    check(r["ok"] and all(r["metrics"][k] == m[k] for k in r["metrics"]),
          "the lender numbers posted are lender_package.compute's numbers")
    files = Path(r["run_dir"]) / "files"
    check((files / lw.WORKBOOK).is_file() and r["files"][0] == lw.WORKBOOK, "the workbook is document 2, built in the run folder")
    try:
        import lender_docs
        direct = lender_docs.build_all(spec, tmp / "lender_direct")
        check(r["docs"] == len(direct) and sorted(r["files"][1:]) == sorted(p.name for p in direct),
              "the letters built are the letters lender_docs builds from the same spec")
    except ImportError:
        check("python-docx" in r["docs_why"], "without python-docx the letters are said missing, the workbook still builds")
    bad = dict(spec)
    bad.pop("contract_price")
    r2 = lw.run(_task("lender_package", "", {"spec": json.dumps(bad)}))
    check(not r2["ok"] and "contract_price" in r2["why"], "a spec missing what the math needs names the gap")
    r3 = lw.run(_task("lender_package", "", {"spec": str(tmp / "elsewhere_walk.json")}))
    check(not r3["ok"] and "outside the workspace" in r3["why"], "a spec path outside the allowed folders is not opened")
    text = lw.render(t, r)
    check(_clean(text) and "Nothing was sent to a lender" in text, "the lender post is clean and says nothing was sent")


# ------------------------------------------------------------------------------- what the code may reach

def _top_imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:
        nodes = [node] + (list(ast.walk(node)) if isinstance(node, ast.Try) else [])
        for n in nodes:
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                out.add(n.module.split(".")[0])
    return out


def _source_hygiene(check) -> None:
    import deal_buyer_match as dbm
    import dispo_buyers
    print("  what the deal code reaches")
    tree = ast.parse((SRC / "sms_agent" / "dispo_campaign.py").read_text(encoding="utf-8"))
    source = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "NAMED_EXCLUSIONS" for t in n.targets))
    check(tuple(source) == dbm.NAMED_EXCLUSIONS, "deal_buyer_match's named exclusions are the dispo blast's, name for name")
    check((dbm.HOLD_TYPES, dbm.ALL_TYPES, dbm.K_TXN, dbm.PAGE_SIZE) ==
          (dispo_buyers.HOLD_TYPES, dispo_buyers.ALL_TYPES, dispo_buyers.K_TXN, dispo_buyers.PAGE_SIZE),
          "the investor labels and page size are the dispo registry's")
    for name in ("deal_buyer_match.py", "deal_buyer_dp.py"):
        tops = _top_imports(SRC / name)
        check(not tops & {"enterprise_prospects", "dispo_buyers", "sms_agent", "crm_api", "reisift_auth"},
              "%s loads no texting app, registry or Deal Room client at import" % name)
    mine = ["comp_package.py", "zillow_market_api.py", "siftmap_standalone.py", "post_walkthrough.py", "offer_sheet.py",
            "rehab_estimator.py", "sku_pricing.py", "deal_buyer_match.py", "deal_buyer_dp.py", "lender_package.py",
            "lender_docs.py", "agents/workflows/quick_screen.py", "agents/workflows/offer_sheet.py",
            "agents/workflows/underwrite.py", "agents/workflows/deal_buyers.py",
            "agents/workflows/lender_package.py", "agents/selftest_wf_deals.py"]
    bad = [f for f in mine if any(ch in (SRC / f).read_text(encoding="utf-8") for ch in ("\u2014", "\u2013", "\x08"))]
    check(not bad, "no long dash, short dash or backspace byte in the deal files: %s" % (bad or "ok"))
