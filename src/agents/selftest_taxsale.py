"""Tax sale results and the door-knock route sheet (research items 7 and 11). Zero network: a fake
DataSift, a fake county roll, a fake geocoder, invented report text and a PDF drawn here. Every
name, street, parcel, phone and id here is invented.
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

ROW = ("taxsale", "run_taxsale", "free", False, False)
NOW = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc)       # a Monday, 08:00 Eastern
LATER = datetime(2027, 7, 15, 13, 0, tzinfo=timezone.utc)      # after every window of the June 2026 sale
TODAY = date(2026, 11, 16)
SALE = "2026-06-02"
BUSINESS = {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
EM, EN = chr(0x2014), chr(0x2013)
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")

# file, tax id, address, defendants, instrument, bidder no, winning bidder, bid, minimum, excess, county, city
ROWS = [
    ("201", "082XA-012", "1234 SAMPLE DR", "DOE JANE A", "202012345678901", "45", "PINE TAX HOLDINGS LLC",
     12500.00, 3210.55, 9289.45, (12, "90 days"), (12, "90 days")),
    ("202", "081-033", "1500 AVENUE A", "ROE PAT", "202012345678902", "7", "QUILL AVERY",
     5000.00, 1100.00, 3900.00, (5, "12 Months"), (0, "")),
    ("203", "059-01701", "77 EXAMPLE PK", "SAMPLE ROBIN & SAMPLE CASEY", "0000WB280000243", "112", "KNOX COUNTY",
     1000.00, 1000.01, -0.01, (6, "180 days"), None),
    ("204", "091AB-004", "3300 EXAMPLE LN", "WEST MORGAN", "202011111111111", "45", "PINE TAX HOLDINGS LLC",
     8000.00, 2000.00, 6000.00, (4, "12 Months"), (4, "12 Months")),
    ("205", "092-115", "410 TESTWOOD RD", "BROOK HOLDINGS LLC", "202022222222222", "9", "MAPLE BIDS INC",
     20000.00, 5000.00, 15000.00, (7, "180 days"), (7, "180 days")),
    ("206", "093C-020", "55 COURT ST", "LAKE DANA", "202033333333333", "9", "MAPLE BIDS INC",
     9000.00, 3000.00, 6000.00, (5, "12 Months"), (8, "90 days")),
    ("207", "094-200", "900 EXAMPLE CIR", "HILL ROBIN", "202044444444444", "3", "QUILL AVERY",
     4000.00, 1000.00, 3000.00, (2, "12 Months"), (0, "")),
    ("208", "095-300", "12 HARBOR WAY", "STONE JORDAN", "202055555555555", "45", "PINE TAX HOLDINGS LLC",
     6000.00, 1500.00, 4500.00, (3, "12 Months"), (0, "")),
    ("209", "096-400", "61 SAMPLE CT", "MOSS TAYLOR", "202066666666666", "3", "QUILL AVERY",
     7000.00, 2000.00, 5000.00, (1, "12 Months"), (0, "")),
    ("210", "097-500", "18 EXAMPLE PL", "FERN CASEY", "202077777777777", "9", "MAPLE BIDS INC",
     3000.00, 1000.00, 2000.00, (6, "180 days"), (6, "180 days")),
    ("211", "098-600", "70 TWIN OAK DR", "GRAY ALEX", "202088888888888", "2", "BIRCH LANE INVESTORS LLC",
     2500.00, 900.00, 1600.00, (5, "12 Months"), (0, "")),
    ("212", "099-700", "5 RIVER RD", "BELL SAM", "202099999999999", "2", "BIRCH LANE INVESTORS LLC",
     2000.00, 800.00, 1200.00, (5, "12 Months"), (0, "")),
]


def _money(v: float) -> str:
    return ("-$" + "{:,.2f}".format(-v)) if v < 0 else "${:,.2f}".format(v)


def _line(r) -> str:
    parts = list(r[:7]) + [_money(r[7]), _money(r[8]), _money(r[9]), "%d %s" % r[10]]
    if r[11] is not None:
        parts.append(("%d %s" % r[11]).strip())
    return " ".join(parts)


def report(rows=ROWS, totals=True, head="REPORT OF SALE  Sale held on June 2, 2026", tamper=0.0) -> str:
    """The text of an invented results document in the row layout, with the printed totals line."""
    lines = ["KNOX COUNTY TAX SALE 99", head,
             "File Tax ID Property address Defendants Instrument Bid # Winning bidder Winning bid Minimum bid Excess "
             "Yrs Redemption Yrs Redemption"]
    for r in rows:
        lines.append(_line(r))
        if r[0] == "203":
            lines.append("ETAL")                       # a wrapped second line in the defendants cell
    if totals:
        lines.append("TOTALS %s %s %s" % (_money(sum(r[7] for r in rows)), _money(sum(r[8] for r in rows)),
                                          _money(sum(r[9] for r in rows) + tamper)))
    lines.append("Page 1 of 1")
    return "\n".join(lines)


def _rec(u, street, first="", last="", company="", county="Knox", status=None, tags=(), lat=None, lon=None, **kw):
    r = {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917",
                                "county": county, "latitude": lat, "longitude": lon},
         "owner": {"uuid": "o-" + u, "first_name": first, "last_name": last, "company": company,
                   "phones": [{"number": "8655550101", "type": "MOBILE", "tags": ["Dial First"], "status": "UNKNOWN"}]},
         "status": status, "tags": list(tags), "lists": [], "notes": "Owner asked for a call at 865-555-0199 after five.",
         "predictivecall_attempts": 0, "directmail_attempts": 0, "skiptraced": True}
    r.update(kw)
    return r


SALE_RECS = [
    _rec("r-doe", "1234 Sample Dr", "Jane", "Doe"),                  # its window closed: never looked up
    _rec("r-roe", "1500 Avenue A", "Pat", "Roe"),
    _rec("r-sample", "77 Example Pike", "Robin", "Sample"),        # the report prints Pk; the record says Pike
    _rec("r-brook", "410 Testwood Rd", company="Brook Holdings LLC"),
    _rec("r-lake", "55 Court St", "Dana", "Lake"),                   # closed under the shorter of two periods
    _rec("r-hill", "900 Example Cir", "Robin", "Hill", status="new_lead"),
    _rec("r-stone", "12 Harbor Way", "Riley", "Vale"),               # a new owner: not the former owner
    _rec("r-moss", "61 Sample Ct", "Taylor", "Moss", tags=["Do Not Market"]),
    _rec("r-fern", "18 Example Pl", "Casey", "Fern", tags=["Tax Sale Redemption"]),   # a person tagged it already
    _rec("r-twin1", "70 Twin Oak Dr", "Alex", "Gray"),
    _rec("r-twin2", "70 Twin Oak Dr", "Alex", "Gray"),               # two records at one address: no guess
    _rec("r-river", "5 River Rd", "Sam", "Bell", county="Blount"),   # the only match is in another county
    _rec("r-west", "3302 Example Ln", "Morgan", "West"),             # found only through the county roll
]

DOCS = {
    "aaa111": {"url": "https://trustee.example.test/assets/pdfs/TaxSale99-FINAL_RESULTS.pdf", "label": "Tax Sale 99 final results",
               "kind": "results", "first_seen": "2026-06-10", "checked": "2026-06-10", "path": "/elsewhere/aaa111.pdf"},
    "bbb222": {"url": "https://trustee.example.test/assets/pdfs/TaxSale99-DecreeConfirmingSale.pdf",
               "label": "Decree confirming sale", "kind": "results", "first_seen": "2026-07-01", "checked": "2026-07-01"},
    "ccc333": {"url": "https://trustee.example.test/assets/pdfs/TaxSale99-Properties.pdf", "label": "Properties for sale",
               "kind": "list", "first_seen": "2026-05-01", "checked": "2026-05-01"},
}


class FakeCRM:
    """The reads and writes the two files use, and nothing else: any other route raises, a record
    create above all."""

    def __init__(self, records, tags=("Tax Sale Redemption", "Auction Soon", "Do Not Market"), custom=None):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in records}
        self.tags = {t.lower() for t in tags}
        self.custom = dict(custom or {})
        self.calls, self.searches, self.reads = [], [], 0

    def tag_uuid(self, title):
        return ("tag-" + title.lower()) if (title or "").lower() in self.tags else ""

    def _row(self, r):
        return {"uuid": r["uuid"], "address": copy.deepcopy(r["address"]), "status": r.get("status"),
                "owner": {"uuid": r["owner"]["uuid"]}}

    def _match(self, r, must):
        for t in must.get("all_tags") or []:
            if t.replace("tag-", "") not in {x.lower() for x in r["tags"]}:
                return False
        return r.get("status") not in ((must.get("must_not") or {}).get("any_property_status") or [])

    def search(self, must, limit=250, offset=0):
        q = str(must.get("search") or "")
        if q.startswith("address_prefix:"):
            pre = q.split(":", 1)[1].strip().lower()
            self.searches.append(pre)
            self.reads += 1
            hits = [r for r in self.recs.values() if r["address"]["street"].lower().startswith(pre)]
        else:
            hits = [r for r in self.recs.values() if self._match(r, must)]
        rows = [self._row(r) for r in hits]
        return len(rows), rows[offset:offset + limit]

    def rows(self, must, cap=10000):
        return self.search(must, limit=cap)[1]

    def record(self, u):
        self.reads += 1
        return copy.deepcopy(self.recs.get(u) or {})

    def custom_fields(self, u):
        return dict(self.custom.get(u, {}))

    def add_tags(self, u, add, rec=None):
        self.calls.append(("add_tags", u, tuple(add)))
        for t in add:
            if t not in self.recs[u]["tags"]:
                self.recs[u]["tags"].append(t)

    def remove_tags(self, u, drop):
        self.calls.append(("remove_tags", u, tuple(drop)))
        self.recs[u]["tags"] = [t for t in self.recs[u]["tags"] if t not in drop]

    def message(self, u, text, pin=False):
        self.calls.append(("message", u, text))
        return ""

    def call(self, path, method="GET", body=None, headers=None):
        m = re.fullmatch(r"/api/internal/property/([^/]+)/custom-field/", path)
        if m and method == "GET":
            return {"results": [{"custom_field": {"label": k}, "value": v} for k, v in self.custom.get(m.group(1), {}).items()]}
        self.calls.append(("call", method, path))
        raise AssertionError("unexpected DataSift call %s %s" % (method, path))

    def writes(self):
        return [c for c in self.calls if c[0] in ("add_tags", "remove_tags", "message")]


def _register():
    """Until the orchestrator adds the registry line, this section registers the kind for itself and
    takes it out again afterwards. Once the line is in registry.py this does nothing."""
    from .autopilot import policy, registry
    if "taxsale" in registry.EXTRA:
        return lambda: None
    registry.EXTRA["taxsale"] = ROW
    kinds, no_presets = policy.KINDS, policy.NO_PRESETS
    policy.KINDS = kinds + ("taxsale",)
    policy.NO_PRESETS = no_presets + ("taxsale",)
    policy.SPEND_BY_KIND["taxsale"] = "free"

    def undo():
        registry.EXTRA.pop("taxsale", None)
        policy.KINDS, policy.NO_PRESETS = kinds, no_presets
        policy.SPEND_BY_KIND.pop("taxsale", None)
    return undo


# The block this build proposes for team/autopilot.yaml, as a mapping (it must parse clean).
SHIPPED = {"kind": "taxsale", "owner": "analysis", "mode": "plan", "schedule": ["07:50 Mon-Fri"], "summary": "x",
           "read_fields": False, "archive_dir": "", "county": "Knox", "tag": "Tax Sale Redemption", "note": True,
           "period_rule": "shorter", "confirmed": {}, "sale_dates": {}, "min_days_left": 5, "max_records": 40,
           "recheck_days": 7, "roll_check": True, "max_roll_lookups": 20, "buyers": True, "registry_dir": "",
           "government_bidders": ["Knox County", "City of Knoxville", "State of Tennessee"],
           "scales": [{"from": "2026-07-01", "steps": [[3, "12 months"]], "over": "90 days"},
                      {"from": "1900-01-01", "steps": [[5, "12 months"], [7, "180 days"]], "over": "90 days"}]}


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import taxsale
    from .workflows import doorknock_route as dk
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.RUNS_DIR, config.PUBLIC_URL, config.WEBHOOK_SECRET)
    hooks = (taxsale.READ, taxsale.lookup_parcel, dk.POLICY, dk.CRM, dk.GEOCODE, dk.NOW)
    env = {k: os.environ.get(k) for k in ("AGENTS_DOORKNOCK_GEOCODER", "AGENTS_DOORKNOCK_START", "AGENTS_DOORKNOCK_MAX_READS",
                                          "AGENTS_DOORKNOCK_MAX_STOPS", "AGENTS_BUYER_REGISTRY_DIR")}
    root = Path(tmp) / "taxsale"
    config.DATA_DIR = root / "data"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.RUNS_DIR = config.DATA_DIR / "runs"
    config.PUBLIC_URL, config.WEBHOOK_SECRET = "https://agents.example.test", "test-secret"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    undo_reg = _register()
    rolls = []

    def fake_roll(pin, county):
        # Never the live county roll: one parcel answers at its true street, one roll is down.
        rolls.append(pin)
        if pin == "099-700":
            raise OSError("the roll is down in this test")
        return {"address": "3302 EXAMPLE LN", "owner": "WEST MORGAN"} if pin == "091AB-004" else None

    taxsale.lookup_parcel = fake_roll
    dk.GEOCODE = lambda a: (_ for _ in ()).throw(AssertionError("the geocoder was called with no fake set"))
    for k in env:
        os.environ.pop(k, None)
    try:
        _pure(check, root)
        _wired(check)
        _stage(check, root, rolls)
        _route(check, root)
    finally:
        taxsale.READ, taxsale.lookup_parcel, dk.POLICY, dk.CRM, dk.GEOCODE, dk.NOW = hooks
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        undo_reg()
        config.DATA_DIR, config.DB_PATH, config.MODE, config.RUNS_DIR, config.PUBLIC_URL, config.WEBHOOK_SECRET = keep


# ---------------------------------------------------------------------- the rules on their own
def _pure(check, root: Path) -> None:
    from .autopilot import ledger, policy
    from .autopilot import taxsale as t
    print("  the redemption period")
    check(t.period("90 days") == ("days", 90) and t.period("12 Months") == ("months", 12) and t.period("1 year") == ("months", 12)
          and t.period("180 DAYS") == ("days", 180), "a printed period is read in days or months, a year as twelve months")
    check(t.period("") is None and t.period("0 days") is None and t.period("soon") is None and t.period("90 fortnights") is None,
          "anything that is not a period reads as none, never as a default")
    check(t.end_of(date(2026, 6, 30), ("days", 90)) == date(2026, 9, 28) and t.end_of(date(2026, 6, 30), ("months", 12)) == date(2027, 6, 30),
          "the window ends the period after the order: 90 days from Jun 30 is Sep 28, a year is Jun 30")
    check(t.end_of(date(2024, 2, 29), ("months", 12)) == date(2025, 2, 28) and t.end_of(date(2026, 6, 30), ("days", 400)) == date(2027, 6, 30),
          "a year from Feb 29 is Feb 28, and no window runs past one year after the order")
    old = date(2026, 6, 30)
    scales = t.options(None)["scales"]
    check([t.statute_period(y, old, scales) for y in (1, 5, 6, 7, 8, 12)]
          == [("months", 12), ("months", 12), ("days", 180), ("days", 180), ("days", 90), ("days", 90)],
          "before July 1, 2026: five years or less is a year, six and seven 180 days, eight or more 90 days (67-5-2701)")
    new = date(2026, 7, 1)
    check([t.statute_period(y, new, scales) for y in (1, 3, 4, 12)] == [("months", 12), ("months", 12), ("days", 90), ("days", 90)],
          "an order on or after July 1, 2026: three years or less is a year, more than three is 90 days (Public Chapter 971)")
    o = t.options(None)
    two = {"county_period": "12 Months", "city_period": "90 days", "county_years": 5, "city_years": 8}
    check(t.row_period(two, old, o) == (("days", 90), "report"),
          "a row printing two periods takes the shorter by default: a false 'you can still redeem' is the worse mistake")
    check(t.row_period(two, old, dict(o, period_rule="longer"))[0] == ("months", 12)
          and t.row_period(two, old, dict(o, period_rule="county"))[0] == ("months", 12),
          "period_rule longer or county picks the year instead")
    check(t.row_period({"county_years": 9, "city_years": 0}, old, o) == (("days", 90), "statute")
          and t.row_period({"county_years": 0, "city_years": 0}, old, o) == (None, ""),
          "a row with no printed period falls back to the statute by its years, and with no years it has no window")

    print("  reading a row of the report")
    check(t.money_of("$12,500.00") == 12500.0 and t.money_of("3,210.55") == 3210.55 and t.money_of("-$0.01") == -0.01
          and t.money_of("($0.01)") == -0.01, "a money figure reads with or without the dollar sign, and a buyback reads negative")
    check(t.money_of("12") is None and t.money_of("1,2") is None and t.money_of("90") is None,
          "a plain number (years delinquent) is never read as dollars")
    check(t.split_address("1500 AVENUE A ROE PAT".split())[:2] == ("1500 AVENUE A", "ROE PAT")
          and t.split_address("4100 DRIVE E SAMPLE PAT".split())[:2] == ("4100 DRIVE E", "SAMPLE PAT"),
          "Avenue A and Drive E keep their letter: the street ends after the type and a lone letter")
    check(t.split_address("55 COURT ST LAKE DANA".split())[:2] == ("55 COURT ST", "LAKE DANA")
          and t.split_address("123 EXAMPLE DR LANE MARY".split())[:2] == ("123 EXAMPLE DR", "LANE MARY"),
          "a street named for a type keeps its real type, and an owner surnamed Lane stays an owner")
    check(t.split_address("40 EXAMPLE AVE NW ROE JAN".split())[0] == "40 EXAMPLE AVE NW"
          and t.split_address("88 MAIN ST #D3 DOE JANE".split())[0] == "88 MAIN ST #D3"
          and t.split_address("9 EXAMPLE DR APT 4 SAMPLE PAT".split())[0] == "9 EXAMPLE DR APT 4",
          "a direction and a unit after the street type stay with the street")
    check(t.split_address("1234 BROADWAY SAMPLE PAT".split()) == ("1234 BROADWAY", "SAMPLE PAT", "guessed")
          and t.split_address("EXAMPLE RD SAMPLE PAT".split())[0] == "EXAMPLE RD",
          "with no street type the street is the number and one word, said as a guess")
    full = t.parse_line(_line(ROWS[0]))
    check(full and (full["pin"], full["address"], full["owner"], full["bidder"], full["bid"], full["excess"]) ==
          ("082XA-012", "1234 SAMPLE DR", "DOE JANE A", "PINE TAX HOLDINGS LLC", 12500.0, 9289.45)
          and (full["county_years"], full["county_period"], full["city_years"], full["city_period"]) == (12, "90 days", 12, "90 days"),
          "a full row reads from both ends: tax id, address, owner, bidder, money and both redemption groups")
    zero = t.parse_line(_line(ROWS[1]))
    check(zero and zero["county_period"] == "12 months" and zero["city_years"] == 0 and zero["city_period"] == "",
          "a city column printed as a bare 0 reads as no city period")
    lone = t.parse_line(_line(ROWS[2]))
    check(lone and lone["instrument"] == "0000WB280000243" and lone["excess"] == -0.01 and lone["city_period"] == ""
          and lone["address"] == "77 EXAMPLE PK" and lone["bidder"] == "KNOX COUNTY",
          "an instrument number with letters, a negative excess and no city group at all still read")
    check(t.parse_line("File Tax ID Property address Defendants Instrument Bid # Winning bidder") is None
          and t.parse_line("TOTALS $1.00 $2.00 $3.00") is None and t.parse_line("ETAL") is None,
          "a header, the totals line and a wrapped cell are not rows")
    got = t.parse_text(report())
    check(len(got["rows"]) == 12 and got["pins"] == 12 and t.check_totals(got["rows"], got["totals"]) == "confirmed",
          "every row of the report is read and the rows add up to the printed totals to the cent")
    check(got["rows"][2]["wrapped"] == "ETAL", "a wrapped second line is kept with the row above it")
    bad = t.parse_text(report(tamper=0.02))
    check(t.check_totals(bad["rows"], bad["totals"]) == "broken", "rows that do not add up to the totals line read as broken")
    none = t.parse_text(report(totals=False))
    check(t.check_totals(none["rows"], none["totals"]) == "unconfirmed", "a report with no totals line reads as unconfirmed")

    print("  the note, the queue's side and the buyers")
    r = t.parse_line(_line(ROWS[1]))
    note = t.note_text(r, SALE, date(2026, 6, 30), date(2027, 6, 30), ("months", 12), "report", True, True)
    check("redeem until Jun 30, 2027" in note and "12 months from the order confirming the sale on Jun 30, 2026" in note
          and "67-5-2701(b)(2)" in note and "lawyer" in note and "$5,000" in note,
          "the note says until when, counted from the order, the profiteering rule and to see a lawyer")
    quiet = t.note_text(r, SALE, date(2026, 6, 2), date(2027, 6, 2), ("months", 12), "report", False, False)
    check("$" not in quiet and "not on file" in quiet,
          "with an unconfirmed parse the note carries no dollar figure, and a window from the sale date says so")
    check(EM not in note + quiet and EN not in note + quiet and not PHONE.search(note + quiet), "no long dash and no phone in a note")
    ledger.set_fact("w-open", t.FACT, {"until": "2026-12-01", "sale": SALE})
    check(t.window_of("w-open", TODAY) and t.window_of("w-open", date(2026, 12, 2)) is None and t.window_of("w-none", TODAY) is None,
          "the research queue sees a window only while it is open")
    check(t.reason(t.window_of("w-open", TODAY), TODAY) == "tax sale on Jun 2, 2026, the owner can redeem until Dec 1, 2026 (15 days)",
          "the queue's reason names the sale, the deadline and the days left")
    sold = [(SALE, t.parse_line(_line(x))) for x in ROWS]
    o = t.options(None)
    entries = t.buyer_entries(sold, TODAY, o)
    by = {e["buyer_key"]: e for e in entries}
    check("KNOX COUNTY" not in by and set(by) == {"PINE TAX HOLDINGS LLC", "QUILL AVERY", "MAPLE BIDS INC", "BIRCH LANE INVESTORS LLC"},
          "every winning bidder becomes a buyer except the county striking a parcel to itself")
    pine = by["PINE TAX HOLDINGS LLC"]
    keys = {"buyer_key", "name", "aliases", "is_entity", "mail", "counties", "txn_types", "n_buys", "n_held", "n_exited", "cash_n",
            "price_min", "price_med", "price_max", "portfolio_n", "portfolio_value", "saved_uuid", "secondary", "tier",
            "tier_reason", "buys", "active_365d", "active_182d", "n_in_window", "last_active_buy"}
    check(keys <= set(pine) and pine["n_buys"] == 3 and pine["cash_n"] == 3 and pine["txn_types"] == ["tax_sale"]
          and pine["parcels"] == ["082XA-012", "091AB-004", "095-300"] and pine["is_entity"],
          "a bidder is written in the registry's own shape, with the parcels they bought and every purchase as cash")
    check(all(b.get("parcel") and b["txn"] == "tax_sale" and b["date"] == SALE for b in pine["buys"])
          and pine["price_min"] == 6000.0 and pine["price_max"] == 12500.0 and pine["active_365d"] and pine["active_182d"]
          and not t.buyer_entries(sold, date(2027, 1, 15), o)[0]["active_182d"],
          "each purchase carries its parcel, sale date and bid, and recency is measured from the sale")
    old_reg = [{"buyer_key": "QUILL AVERY", "name": "Quill Avery", "n_buys": 4, "cash_n": 1, "price_min": 90000.0,
                "price_med": 120000.0, "price_max": 150000.0, "txn_types": ["pending"], "counties": ["Knox"],
                "buys": [{"address": "1 Old Rd", "date": "2025-01-05", "price": 120000, "county": "Knox", "txn": "pending",
                          "role": "holder"}], "active_365d": False, "active_182d": False, "n_in_window": 0, "last_active_buy": ""}]
    merged = t.merge_buyers(old_reg, entries)
    q = next(b for b in merged if b["buyer_key"] == "QUILL AVERY")
    check(len(merged) == 4 and q["n_buys"] == 7 and q["cash_n"] == 4 and q["txn_types"] == ["pending", "tax_sale"]
          and (q["price_min"], q["price_max"]) == (90000.0, 150000.0) and q["active_365d"] and len(q["buys"]) == 4,
          "a known buyer gains the tax sale purchases and stays active, and keeps the price band its houses set")
    again = t.merge_buyers(merged, entries)
    check(len(again) == 4 and next(b for b in again if b["buyer_key"] == "QUILL AVERY")["n_buys"] == 7,
          "folding the same tax sale file in twice changes nothing")

    print("  the playbook block")
    shipped = policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25}, "people": {"callers": ["Emerson"]},
                            "stages": {"tax_sale_results": copy.deepcopy(SHIPPED)}}, EMPLOYEES)
    check(shipped.stage("tax_sale_results").kind == "taxsale", "the shipped playbook block parses clean")

    def bad(**kw) -> bool:
        st = policy.Stage(id="x", kind="taxsale", owner="analysis", mode="plan", summary="", schedule=[], presets=[],
                          params=dict(SHIPPED, **kw))
        return bool(t.block_problems(st))
    check(bad(period_rule="widest") and bad(confirmed={"2026-06-02": "soon"}) and bad(sale_dates={"a.pdf": "June"})
          and bad(scales=[{"from": "2026-07-01", "steps": [[3, "a while"]], "over": "90 days"}]) and bad(max_records=0)
          and bad(tag="") and not bad(), "a typo in the block is refused when the playbook loads, and the shipped block is not")


# ---------------------------------------------------------------------- the stage, run by run
def _wired(check) -> None:
    """The window as the rest of the autopilot reads it: the research queue's rank and the auction lane's
    close-out (the integration of 2026-11-15). Reads the open window _pure left on file for w-open."""
    from .autopilot import auctiontasks, lanes, researchqueue as rq
    from .autopilot import taxsale as t
    print("  the window in the research queue and the auction lane")
    o = rq.options(None)
    rec = {"uuid": "w-open", "owner": {"first_name": "Pat", "last_name": "Example"}, "tags": [], "lists": []}
    sig = rq.signals_of(rec, {}, o, today=TODAY)
    check((sig.get("redemption") or {}).get("until") == "2026-12-01"
          and not rq.signals_of(dict(rec, uuid="w-none"), {}, o, today=TODAY).get("redemption"),
          "the queue's signals carry the open window from the ledger, and nothing for a record with none")
    base = {"owner_kind": "person", "sales": [SALE], "held": "auction date passed"}
    r = rq.rank(dict(base, redemption={"until": "2026-12-01", "sale": SALE}), TODAY, o)
    check(r.cls == "tax_redemption" and rq.BASE["tax_redemption"] <= r.score < rq.BASE["hot_auction"]
          and r.reason == t.reason({"until": "2026-12-01", "sale": SALE}, TODAY),
          "a passed tax sale with an open window ranks just under a sale still ahead, with the deadline as its reason")
    later = rq.rank(dict(base, redemption={"until": "2027-06-01", "sale": SALE}), TODAY, o)
    check(later.cls == "tax_redemption" and later.score < r.score, "the sooner the deadline, the higher inside the class")
    check(rq.rank(base, TODAY, o).cls == "gone"
          and rq.rank(dict(base, redemption={"until": "2026-11-15", "sale": SALE}), TODAY, o).cls == "gone",
          "with no window, or one already closed, a passed sale still drops the record")
    check(rq.rank(dict(base, redemption={"until": "2026-12-01"}, lead=True), TODAY, o).cls == "lead"
          and rq.rank(dict(base, redemption={"until": "2026-12-01"}, held="listed or pending on the MLS"), TODAY, o).cls == "gone",
          "a lead is still left to the lead manager, and a house listed since is still held")
    check(auctiontasks._redeeming("w-open", TODAY) and not auctiontasks._redeeming("w-none", TODAY)
          and not auctiontasks._redeeming("w-open", date(2026, 12, 2)),
          "the auction lane sees the open window, so it neither closes the record out nor runs the deed check on it")
    check("tax_redemption" in lanes.RESEARCH_AT_ONCE, "a redemption window opens research at once on a lane that waits")


def _stage(check, root: Path, rolls: list) -> None:
    from . import store
    from .autopilot import ledger, policy, sweep, undo
    from .autopilot import taxsale as t
    arc, reg = root / "archive", root / "registry"
    arc.mkdir(parents=True, exist_ok=True)
    reg.mkdir(parents=True, exist_ok=True)
    (arc / "tax_sale_docs.json").write_text(json.dumps(DOCS), encoding="utf-8")
    (reg / "registry.json").write_text(json.dumps([{"buyer_key": "QUILL AVERY", "name": "Quill Avery"}]), encoding="utf-8")
    texts = {"aaa111.pdf": report(), "bbb222.pdf": ""}          # the decree is a scanned image: no text
    t.READ = lambda p: [("rows", texts.get(Path(p).name, ""))]

    def pb(mode="plan", queue=False, **over) -> dict:
        stage = {"kind": "taxsale", "owner": "analysis", "mode": mode, "schedule": ["07:50 Mon-Fri"], "read_fields": False,
                 "archive_dir": str(arc), "registry_dir": str(reg), "roll_check": False}
        stage.update(over)
        stages_ = {"t_sale": stage}
        if queue:
            stages_["t_queue"] = {"kind": "researchqueue", "owner": "research", "mode": "plan", "schedule": ["09:00 Mon-Fri"],
                                  "scope_tags": ["Auction Soon", "Tax Sale Redemption"]}
        return {"business": BUSINESS, "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
                "stages": stages_}

    def go(crm, now=NOW, mode="plan", queue=False, data="data", **over):
        config.DATA_DIR = root / data
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.MODE = "live" if mode == "live" else "dry"
        try:
            return sweep.run("t_sale", crm=crm, now=now, pol=policy.parse(pb(mode, queue, **over), EMPLOYEES),
                             sources={}, scorer=False)
        finally:
            config.MODE = "dry"

    def text(out) -> str:
        return "\n".join([out.head] + list(out.lines) + list(out.problems) + list(out.alerts))

    # ------------------------------------------------------------------ plan mode
    print("  a plan run")
    crm = FakeCRM(SALE_RECS)
    plan = go(crm)
    check(not crm.writes() and not [c for c in crm.calls if c[0] == "call"],
          "a plan run writes nothing to the account and calls no route it does not read")
    check(plan.head.startswith("Would tag 2 former owners who can still redeem their Knox tax sale house, for research"),
          "a plan run says how many former owners it would tag: %r" % plan.head)
    check("6 listed for a person" in plan.head and "1 held back by the gate" in plan.head
          and "would add 4 winning bidders to the buyer registry (3 not in it yet)" in plan.head,
          "the head says how many are listed, held, and what would go to the buyer registry")
    check("1234 sample" not in crm.searches and "55 court" not in crm.searches,
          "a sale past its redemption period is not tagged and not even looked up (the shorter period closed one)")
    check(plan.tally.get("redemption window closed") == 2, "both closed windows are counted as closed")
    check(plan.tally.get("listed: not in the account") == 2 and plan.tally.get("listed: two or more records match the address") == 1,
          "a parcel with no record, or only one in another county, is listed; two records at one address are never guessed")
    check(plan.tally.get("listed: a company or trust owns it (people only)") == 1
          and plan.tally.get("listed: a lead: the lead manager has it") == 1
          and plan.tally.get("listed: the record's owner is not the former owner on the report") == 1,
          "a company, a lead and a record whose owner is someone new are listed, not tagged")
    check(any(k.startswith("held back:") for k in plan.tally) and plan.tally.get("already carries the Tax Sale Redemption tag") == 1,
          "a Do Not Market record is held back by the gate, and a record a person already tagged is left alone")
    check(any("Knox tax sale on Jun 2, 2026: 12 parcels sold, 10 can still be redeemed, the soonest window closes Nov 29, 2026" in x
              for x in plan.lines), "the run names the sale, the parcels sold, how many can redeem and the soonest deadline")
    check(any("counted from the sale date" in x for x in plan.lines),
          "without the date the court confirmed the sale, the windows are counted from the sale date and the run says so")
    check(any("research queue does not read the Tax Sale Redemption tag" in x for x in plan.lines) and not plan.problems,
          "a plan run with no research queue reading the tag says so as a line, not a problem")
    check(plan.alerts == ["Knox tax sale results are in: 12 parcels sold on Jun 2, 2026; 10 former owners can still redeem, "
                          "the soonest by Nov 29, 2026."], "a newly read results document is one alert for the team: %r" % plan.alerts)
    csv_path = Path(plan.run_dir) / "files" / "Tax_Sale_Redemption_For_A_Person.csv"
    listed = csv_path.read_text(encoding="utf-8") if csv_path.is_file() else ""
    check("3300 Example Ln" in listed and "not in the account" in listed and "70 Twin Oak Dr" in listed,
          "the parcels that need a person are in a list with the reason, never created as records")
    fact = ledger.fact("r-roe", t.FACT) or {}
    check(fact.get("until") == "2027-06-02" and fact.get("from") == "sale" and fact.get("parcel") == "081-033",
          "what the stage learned about a record is kept in plan mode too: the window, where it was counted from, the parcel")
    check(ledger.fact("r-roe", t.MARK) is None and not (reg / t.BUYERS_FILE).exists(),
          "a plan run marks nothing as its own and writes no buyer registry file")
    check(EM not in text(plan) and EN not in text(plan) and not PHONE.search(text(plan)), "no long dash and no phone in what a run says")

    print("  a second plan run the same day")
    crm.reads = 0
    again = go(crm)
    check(not again.alerts and again.head.startswith("Would tag 2") and crm.reads == 0,
          "a repeat plan run raises no second alert and reports this week's verdicts without opening a record")

    print("  the read cap, the roll and the edges")
    g = go(FakeCRM(SALE_RECS), data="data_soon", min_days_left=20)
    check(g.head.startswith("Would tag 1 former owner") and g.tally.get("listed: too soon for research (under 20 days left)") == 2,
          "a window closing sooner than min_days_left is listed for a person instead of tagged: %r %r" % (g.head, dict(g.tally)))
    cap = FakeCRM(SALE_RECS)
    capped = go(cap, data="data_cap", max_records=3)
    check(capped.tally.get("left for the next run (3 records a run)") == 7 and cap.reads <= 6,
          "a run opens at most max_records report rows in the account and leaves the rest for the next run")
    rolled = go(FakeCRM(SALE_RECS), data="data_roll", roll_check=True)
    check(rolled.tally.get("found at the county roll's address") == 1 and rolled.head.startswith("Would tag 3")
          and "091AB-004" in rolls and rolled.tally.get("county roll did not answer") == 1,
          "a row whose report address finds nothing is found at the roll's address, and a roll that is down is counted")
    check(go(FakeCRM(SALE_RECS), data="data_none", archive_dir=str(root / "nowhere")).head
          == "No Knox tax sale results have reached this box yet.", "with no archive on the box the run says so in its head")
    t.READ = lambda p: []
    unread = go(FakeCRM(SALE_RECS), data="data_unread")
    check(any("could not be read on this box: the archived file is not on this box" in p for p in unread.problems),
          "a results document this box cannot read is a problem, said with the reason")
    t.READ = lambda p: [("rows", report(tamper=0.02) if Path(p).name == "aaa111.pdf" else "")]
    broken = go(FakeCRM(SALE_RECS), data="data_broken")
    check(any("did not add up to the printed totals" in p for p in broken.problems) and broken.head.startswith("Would tag 2"),
          "rows that do not add up to the totals are a loud problem, and the rows read are still worked")
    t.READ = lambda p: [("rows", report(head="REPORT OF SALE") if Path(p).name == "aaa111.pdf" else "")]
    nodate = go(FakeCRM(SALE_RECS), data="data_nodate")
    check(any("sale date could not be read" in p and "taxsale99-final_results.pdf".lower() in p.lower() for p in nodate.problems)
          and nodate.found == 0, "a report with no readable sale date is a problem that names the file, and nothing is worked")
    fixed = go(FakeCRM(SALE_RECS), data="data_nodate", sale_dates={"TaxSale99-FINAL_RESULTS.pdf": SALE})
    check(not fixed.problems and fixed.head.startswith("Would tag 2"), "setting the sale date for that file fixes it")
    t.READ = lambda p: [("rows", texts.get(Path(p).name, ""))]
    try:
        import pdfminer.high_level  # noqa: F401
        import reportlab  # noqa: F401
        have_pdf = True
    except Exception:  # noqa: BLE001
        have_pdf = False
    if have_pdf:
        pdf = _draw_report(root / "drawn.pdf")
        got = None
        for name, txt in t.readings(pdf):
            p = t.parse_text(txt)
            if name == "rows":
                got = p
        check(got is not None and len(got["rows"]) == 12 and t.check_totals(got["rows"], got["totals"]) == "confirmed"
              and got["rows"][1]["address"] == "1500 AVENUE A",
              "a real PDF drawn as a table reads one row a line and adds up to its totals")
    else:
        print("  (pdfminer or reportlab is not installed here: the PDF reading is checked where they are)")

    # ------------------------------------------------------------------ live mode
    print("  a live run")
    live = FakeCRM(SALE_RECS)
    done = go(live, mode="live", queue=True, confirmed={SALE: "2026-06-30"})
    tags = [c for c in live.calls if c[0] == "add_tags"]
    check(sorted(c[1] for c in tags) == ["r-roe", "r-sample"] and all(c[2] == ("Tax Sale Redemption",) for c in tags),
          "a live run tags exactly the former owners who can still redeem and pass every check")
    notes = {c[1]: c[2] for c in live.calls if c[0] == "message"}
    check(set(notes) == {"r-roe", "r-sample"} and "redeem until Jun 30, 2027" in notes["r-roe"]
          and "redeem until Dec 27, 2026" in notes["r-sample"] and "order confirming the sale on Jun 30, 2026" in notes["r-roe"],
          "each tagged record gets a note with its deadline, counted from the order once its date is set")
    check(not any(c[1] in ("r-fern", "r-moss", "r-hill", "r-stone", "r-brook", "r-doe", "r-lake", "r-twin1", "r-twin2", "r-river")
                  for c in live.writes()), "nothing is written on a record that was listed, held, already tagged or closed")
    check(not [c for c in live.calls if c[0] == "call"], "a live run creates no record and calls no other route")
    ch = [c for c in ledger.changes(record_uuid="r-roe") if c["what"] == "add_tags"]
    steps = undo.plan(ch[0]["run_id"]) if ch else []
    check(len(ch) == 1 and any(s["do"] == "remove_tags" and s["record_uuid"] == "r-roe" for s in steps),
          "every tag is recorded with its before-value, so the run can be undone")
    check((ledger.fact("r-roe", t.MARK) or {}).get("tag") == "Tax Sale Redemption" and ledger.fact("r-fern", t.MARK) is None,
          "the stage remembers the tags it put on, and only those")
    reg_file = json.loads((reg / t.BUYERS_FILE).read_text(encoding="utf-8")) if (reg / t.BUYERS_FILE).exists() else []
    check({b["buyer_key"] for b in reg_file} == {"PINE TAX HOLDINGS LLC", "QUILL AVERY", "MAPLE BIDS INC", "BIRCH LANE INVESTORS LLC"}
          and "added 4 winning bidders to the buyer registry (3 not in it yet)" in done.head,
          "a live run writes the winning bidders to the buyer registry's own folder")
    check(not any("research queue does not read" in x for x in done.lines) and not any("counted from the sale date" in x for x in done.lines)
          and not done.problems, "with the queue reading the tag and the order's date set, there is nothing to warn about")

    print("  a second live run")
    before = len(live.writes())
    again = go(live, mode="live", queue=True, confirmed={SALE: "2026-06-30"})
    check(len(live.writes()) == before and again.writes == 0 and again.acted == 0,
          "a repeat live run tags nothing twice and rewrites an unchanged buyer file not at all")

    print("  the windows close")
    closed = go(live, now=LATER, mode="live", queue=True, confirmed={SALE: "2026-06-30"})
    off = [c for c in live.calls if c[0] == "remove_tags"]
    check(sorted(c[1] for c in off) == ["r-roe", "r-sample"] and "took the tag off 2 whose window closed" in closed.head,
          "when a window closes the tag this stage put on comes off: %r" % closed.head)
    check("Tax Sale Redemption" in live.recs["r-fern"]["tags"], "a tag a person put on is never taken off")
    check((ledger.fact("r-roe", t.MARK) or {}).get("removed") and
          any(c["what"] == "remove_tags" for c in ledger.changes(record_uuid="r-roe")),
          "taking a tag off is remembered and recorded for undo")
    check(closed.head.startswith("No former owner from the Knox tax sales can still redeem (the last window closed Jun 30, 2027)"),
          "after the last window the head says no one can still redeem, and when the last window closed")

    print("  the tag does not exist")
    store.kv_set("unused", "")
    notag = FakeCRM(SALE_RECS, tags=("Auction Soon", "Do Not Market"))
    missing = go(notag, mode="live", queue=True, data="data_notag", confirmed={SALE: "2026-06-30"})
    check(not [c for c in notag.calls if c[0] == "add_tags"]
          and any("no 'Tax Sale Redemption' tag in this account" in p for p in missing.problems),
          "a live run refuses to tag when the tag does not exist, and says to create it first")


def _draw_report(path: Path) -> Path:
    """The invented report drawn as a real table, one cell per column, to read back through pdfminer."""
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.pdfgen import canvas
    cols = [18, 40, 82, 208, 312, 376, 400, 492, 545, 598, 652, 672, 712, 732]
    c = canvas.Canvas(str(path), pagesize=landscape(letter))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18, 580, "KNOX COUNTY TAX SALE 99")
    c.drawString(18, 567, "REPORT OF SALE  Sale held on June 2, 2026")
    c.setFont("Helvetica", 6)
    y = 540
    for r in ROWS:
        cells = list(r[:7]) + [_money(r[7]), _money(r[8]), _money(r[9]), str(r[10][0]), r[10][1]]
        if r[11] is not None:
            cells += [str(r[11][0]), r[11][1]]
        for x, v in zip(cols, cells):
            if v:
                c.drawString(x, y, v)
        y -= 13
    c.drawString(18, y, "TOTALS")
    for x, v in zip(cols[7:10], (_money(sum(r[7] for r in ROWS)), _money(sum(r[8] for r in ROWS)), _money(sum(r[9] for r in ROWS)))):
        c.drawString(x, y, v)
    c.save()
    return path


# ---------------------------------------------------------------------- the route sheet
def _route(check, root: Path) -> None:
    from . import store
    from .autopilot import policy
    from .workflows import doorknock_route as dk
    print("  the door-knock route")
    k9 = "00000000-0000-4000-8000-000000000009"
    knock = [
        _rec("k1", "100 Alpha St", "Pat", "Alpha", tags=["Auction Soon"], lat=35.901, lon=-83.901),
        _rec("k2", "200 Beta Rd", "Robin", "Beta", tags=["Auction Soon"], lat=35.960, lon=-83.960),
        _rec("k3", "300 Gamma Dr", "Casey", "Gamma", tags=["Auction Soon"], lat=35.903, lon=-83.903),
        _rec("k4", "400 Delta Ln", "Jordan", "Delta", tags=["Auction Soon"], lat=35.990, lon=-83.990),      # 24 days out
        _rec("k5", "500 Echo Ct", company="Echo Holdings LLC", tags=["Auction Soon"], lat=35.910, lon=-83.910),
        _rec("k6", "600 Fox Way", "Taylor", "Fox", tags=["Auction Soon", "Do Not Market"], lat=35.920, lon=-83.920),
        _rec("k7", "700 Golf Pl", "Avery", "Golf", tags=["Auction Soon"]),                                 # no map position
        _rec("k8", "800 Hotel Cir", "Morgan", "Hotel", tags=["Auction Soon"], status="new_lead", lat=35.915, lon=-83.915),
        _rec(k9, "900 India Ave", "J Marcus", "India", tags=["Auction Soon"], lat=35.930, lon=-83.930,
             tax_auction_date="2026-11-19"),
    ]
    sales = {"k1": "2026-11-18", "k2": "2026-11-20", "k3": "2026-11-22", "k4": "2026-12-10", "k5": "2026-11-19",
             "k6": "2026-11-21", "k7": "2026-11-23", "k8": "2026-11-17"}
    crm = FakeCRM(knock, custom={u: {"Foreclosure Date": d} for u, d in sales.items()})
    dk.CRM, dk.NOW = crm, NOW
    dk.POLICY = policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25}, "people": {"callers": ["Emerson"]},
                              "lanes": {"hot_auction": {"tags": ["Auction Soon"], "timeline": True}},
                              "stages": {"hot_auction_tasks": {"kind": "auctiontasks", "owner": "analysis", "mode": "plan",
                                                               "schedule": ["08:05 Mon-Fri"], "lane": "hot_auction",
                                                               "tag": "Auction Soon", "custom_date_fields": ["Foreclosure Date"],
                                                               "date_fields": ["auction_date", "tax_auction_date"],
                                                               "read_fields": False}}}, EMPLOYEES)

    def task(i, **inputs):
        return {"id": i, "agent": "analysis", "workflow": "doorknock_route", "payload": json.dumps({"inputs": inputs})}

    res = dk.run(task(301, start="35.90, -83.90"))
    streets = [s["street"] for s in res["stops"]]
    check(streets == ["100 Alpha St", "300 Gamma Dr", "900 India Ave", "200 Beta Rd", "700 Golf Pl"],
          "the stops run nearest first from the start, not in sale date order, and a house with no map position goes last: %r"
          % streets)
    check("400 Delta Ln" not in streets, "a sale outside the door-knock window is not on the route")
    why = {x["street"]: x["why"] for x in res["left_off"]}
    check(why.get("500 Echo Ct, Knoxville", "").startswith("a company or trust owns it")
          and why.get("600 Fox Way, Knoxville", "").startswith("held back")
          and why.get("800 Hotel Cir, Knoxville", "").startswith("held back"),
          "a company, a Do Not Market record and a lead are left off, each with the reason: %r" % why)
    first = res["stops"][0]
    check(first["first_name"] == "Pat" and first["leave"] == "Foreclosure packet" and first["sale"] == "2026-11-18"
          and res["stops"][2]["first_name"] == "Marcus" and res["stops"][2]["leave"] == "Tax sale packet",
          "each stop carries the owner's first name, the sale date and what to leave, a tax sale packet for a tax sale")
    check(res["stops"][0]["leg"] == 0.1 and res["stops"][-1]["leg"] is None and res["how"] == "from the start",
          "each stop carries the miles from the one before it, and the route says where it starts")
    blob = json.dumps(res)
    check(not PHONE.search(blob) and "phone" not in blob.lower() and EM not in blob and EN not in blob,
          "nothing about the route carries a phone number, though every record and note held one")
    sheet = Path(res["run_dir"]) / "files" / res["file"]
    check(res["file"] == "Door_Knock_Route_2026-11-16.pdf" and sheet.is_file() and sheet.read_bytes()[:5] == b"%PDF-",
          "the route is a printable PDF in the run's files folder")
    try:
        from pdfminer.high_level import extract_text
        pdf_text = extract_text(str(sheet))
        check("100 Alpha St" in pdf_text and "Door-knock route for Mon, Nov 16" in pdf_text and not PHONE.search(pdf_text)
              and EM not in pdf_text and EN not in pdf_text, "the printed sheet has the stops and no phone number on it")
    except ImportError:
        print("  (pdfminer is not installed here: the printed sheet's text is checked where it is)")
    check(res["maps"] == ["stops 1 to 4"], "the sheet links the placed stops in Google Maps, ten points a link")
    links = dk.map_links({"lat": 35.9, "lon": -83.9}, [{"lat": 35.9 + i / 100, "lon": -83.9} for i in range(12)])
    check(len(links) == 2 and links[0][0] == "stops 1 to 9" and links[1][0] == "stops 9 to 12"
          and "origin=35.900000,-83.900000" in links[0][1] and "waypoints=" in links[0][1],
          "a long route is split into links of ten points that share their ends")
    msg = dk.render(task(301), res)
    check(msg.startswith("Door-knock route: 5 houses, about") and "First stop: 100 Alpha St (sale Nov 18)." in msg
          and "1 house has no map position and comes last." in msg and "Left off:" in msg
          and "https://agents.example.test/files/301/" in msg and not PHONE.search(msg.replace(dk.slack.file_url(301, res["file"]), "")),
          "the reply names the route, the first stop, what was left off and links the sheet, with no phone: %r" % msg)
    check(not crm.writes() and not [c for c in crm.calls if c[0] == "call"], "the route writes nothing to the account")

    geo_calls = []
    dk.GEOCODE = lambda a: (geo_calls.append(a), (35.96, -83.96))[1] if "Office" in a else None
    placed = dk.run(task(302, start="9 Office Rd, Knoxville TN"))
    check(placed["stops"][0]["street"] == "200 Beta Rd" and geo_calls == ["9 Office Rd, Knoxville TN"],
          "a typed start address is placed through the geocoder and the route begins nearest it")
    os.environ["AGENTS_DOORKNOCK_GEOCODER"] = "off"
    offline = dk.run(task(303, start="9 Office Rd, Knoxville TN"))
    os.environ.pop("AGENTS_DOORKNOCK_GEOCODER", None)
    check(len(geo_calls) == 1 and offline["how"] == "from the soonest sale" and offline["stops"][0]["street"] == "100 Alpha St"
          and "begins at the soonest sale" in dk.render(task(303), offline),
          "with the geocoder off no address leaves the box, and the route begins at the soonest sale and says so")
    unset = dk.run(task(304))
    check(unset["how"] == "from the soonest sale" and unset["start"] == "", "with no start at all the route begins at the soonest sale")
    wide = dk.run(task(305, start="35.90, -83.90", days="30"))
    check("400 Delta Ln" in [s["street"] for s in wide["stops"]] and wide["days"] == 30,
          "a person can widen the window, and the farther sale joins the route")
    named = dk.run(task(306, addresses="100 Alpha St; %s; 999 Nowhere Rd" % k9))
    check([s["street"] for s in named["stops"]] == ["100 Alpha St", "900 India Ave"] and named["named"]
          and {"street": "999 Nowhere Rd", "why": "not in the account"} in named["left_off"],
          "the houses a person names are routed by street line or record id, and one not in the account is said so")
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_facts WHERE key='auction_dates'")
    capped = FakeCRM(knock, custom={u: {"Foreclosure Date": d} for u, d in sales.items()})
    dk.CRM = capped
    os.environ["AGENTS_DOORKNOCK_MAX_READS"] = "3"
    tight = dk.run(task(307, start="35.90, -83.90"))
    os.environ.pop("AGENTS_DOORKNOCK_MAX_READS", None)
    check(capped.reads <= 3 and any("read cap" in x["why"] for x in tight["left_off"]) and not tight["stops"]
          and dk.render(task(307), tight).startswith("No house can be knocked today."),
          "the route reads no more records than its cap, and says what the cap left off")
    check(dk.scrub("call 865-555-0101 or (865) 555-0102 today") == "call or today",
          "anything shaped like a phone number is scrubbed from printed text")
    nobody = FakeCRM([r for r in knock if r["uuid"] == "k4"], custom={"k4": {"Foreclosure Date": "2026-12-10"}})
    dk.CRM = nobody
    empty = dk.run(task(308))
    check(not empty["stops"] and not empty["file"] and "sale inside 7 days" in dk.render(task(308), empty),
          "with no house inside the window there is no sheet, and the reply says why")
    dk.POLICY = policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25}, "people": {"callers": ["Emerson"]},
                              "stages": {"other_auction_stage": {"kind": "auctiontasks", "owner": "analysis", "mode": "plan",
                                                                 "schedule": ["08:05 Mon-Fri"]}}}, EMPLOYEES)
    check(dk.render(task(309), dk.run(task(309))).startswith("The door-knock route did not run: the playbook has no hot_auction_tasks"),
          "a playbook with no hot auction stage is said plainly, not a crash")
