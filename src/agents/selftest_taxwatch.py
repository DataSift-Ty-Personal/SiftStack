"""Tax watch selftest: county tax status on the records the team works, re-read until paid. Zero network:
a fake DataSift held in memory, a fake county answer for the stage and a fake county site for the county
client itself, and this section's own database and data folder. Every name, street, account and id here
is invented.
"""
from __future__ import annotations

import copy
import csv
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, FakeCRM

# The line the orchestrator adds to autopilot/registry.py EXTRA. Until it is there, this section stands the
# same line up for itself and takes it down again afterwards.
REGISTRY_LINES = {"taxwatch": ("taxwatch", "run_taxwatch", "free", False, False)}
STAGE = "tax_watch"
NOW = datetime(2026, 11, 15, 6, 35, tzinfo=timezone.utc)       # a Sunday, 01:35 in Knoxville
LABEL_OF = {"f-tax": "Tax delinquency amount", "f-total": "Total Delinquency", "f-county": "County"}
PHONE_RX = re.compile(r"\d{7,}")
DASHES = (chr(0x2014), chr(0x2013))

# The block handed to the orchestrator for team/autopilot.yaml (everything but kind, owner, mode, schedule
# and summary), checked here exactly as written.
PROPOSED = {
    "statuses": ["new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead",
                 "follow_up", "lead", "underwrite", "under_contract", "push_deal", "transactions", "refer_lead"],
    "urgent_tags": ["Auction Critical", "Auction Soon"],
    "scope_lists": ["Tax Sale", "Probate", "Tax Delinquent", "Obituary"],
    "scope_tags": ["Top 1000"],
    "hold_tags": ["Do Not Market", "recently sold", "Estate Sold", "Wrong House"],
    "amount_field": "Tax delinquency amount",
    "year_field": "tax_delinquent_year",
    "paid_tag": "Taxes Paid",
    "recheck_days": 91, "recheck_owing_days": 28, "recheck_urgent_days": 7,
    "delinquent_from": "03-01",
    "max_records": 300, "max_scan": 5000, "max_minutes": 40, "county_pause_s": 0.5,
    "note": True, "alert_leads": True, "zero_on_blank": False,
    "individuals_only": True, "estates_are_people": True,
}


def rec(u, street, county="Knox", lists=("Probate",), tags=(), status=None, first="Pat", last="Example",
        company=None, year=None, state="TN"):
    return {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": state, "postal_code": "37917",
                                   "county": county},
            "owner": {"uuid": "o-" + u, "first_name": first, "last_name": last, "company": company, "phones": []},
            "tags": list(tags), "lists": list(lists), "status": status, "tax_delinquent_year": year,
            "predictivecall_attempts": 0, "directmail_attempts": 0, "skiptraced": True}


class TaxCRM(FakeCRM):
    """The shared fake DataSift, with the record PATCH this stage makes, custom fields by label, and the
    ways a write can go wrong."""

    fields = {"tax delinquency amount": "f-tax", "total delinquency": "f-total", "county": "f-county"}

    def __init__(self, records, custom=None, missing_tags=(), missing_lists=(), year_sticks=True, fail_tags=False,
                 rich_rows=False, fields=None):
        super().__init__(records)
        self.custom = copy.deepcopy(custom or {})
        self.missing_tags = {t.lower() for t in missing_tags}
        self.missing_lists = {t.lower() for t in missing_lists}
        self.year_sticks = year_sticks      # False: the PATCH answers and the record does not change
        self.fail_tags = fail_tags          # True: the tag route refuses
        self.rich_rows = rich_rows          # True: search rows carry the owner's names, as the real ones do
        if fields is not None:
            self.fields = dict(fields)
        self.patches = []
        self.reads = []

    def _row(self, r):
        row = super()._row(r)
        if self.rich_rows:
            row["owner"] = {k: r["owner"].get(k) for k in ("uuid", "first_name", "last_name", "company")}
        return row

    def record(self, u):
        self.reads.append(u)
        return super().record(u)

    def tag_uuid(self, title):
        return "" if title.strip().lower() in self.missing_tags else super().tag_uuid(title)

    def list_uuid(self, title):
        return "" if title.strip().lower() in self.missing_lists else super().list_uuid(title)

    def set_custom_field(self, u, field_uuid, value):
        self.writes.append(("field", u, field_uuid, value))
        self.custom.setdefault(u, {})[LABEL_OF.get(field_uuid, field_uuid)] = value

    def add_tags(self, u, add, rec=None):
        if self.fail_tags:
            raise RuntimeError("the tag route answered 500")
        super().add_tags(u, add, rec)

    def call(self, path, method="GET", body=None, headers=None):
        m = re.fullmatch(r"/api/internal/property/([^/]+)/", path)
        if m and method == "PATCH":
            u = m.group(1)
            self.patches.append((u, dict(body)))
            self.writes.append(("patch", u, tuple(sorted(body.items()))))
            if self.year_sticks:
                self.recs[u].update(body)
            return dict(self.recs[u])
        return super().call(path, method, body, headers)

    def of(self, u, kind):
        return [w for w in self.writes if len(w) > 1 and w[1] == u and w[0] == kind]

    def touched(self, u):
        return [w for w in self.writes if len(w) > 1 and w[1] == u]


class FakeCounty:
    """What county_tax would answer, by street: a dict, None (no parcel), or an exception to raise."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = []

    def __call__(self, street, county):
        self.calls.append((street, county))
        a = self.answers.get(street)
        if isinstance(a, BaseException):
            raise a
        return copy.deepcopy(a)


class FakeRoll:
    """A county site for the client: parcel rows by the exact query text, bills by account (an account
    with no bills answers 404, the way a business account does)."""

    def __init__(self, by_query=None, bills=None, down=False):
        self.by_query = by_query or {}
        self.bills = bills or {}
        self.down = down
        self.asked = []

    def __call__(self, url):
        self.asked.append(url)
        if self.down:
            raise OSError("the county answered HTTP 503")
        m = re.search(r"/parcels/([^?]+)\?", url)
        if m:
            rows = self.by_query.get(urllib.parse.unquote(m.group(1)))
            return None if rows is None else {"parcels": copy.deepcopy(rows)}
        m = re.search(r"/due/PPT/([^?]+)\?", url)
        if m:
            a = urllib.parse.unquote(m.group(1))
            return None if a not in self.bills else {"due": {"PPT": {"bills": copy.deepcopy(self.bills[a]), "parcels": []}}}
        raise AssertionError("unexpected county url %s" % url)


def bill(due, year, delinquent=True, paid=False):
    return {"due": due, "taxYear": str(year), "delinquent": delinquent, "paid": paid}


def answer(amount, years=(), account="010AA-001", address=""):
    return {"amount": amount, "years": list(years), "account": account, "address": address}


def playbook(mode="plan", **over) -> dict:
    stage = {"kind": "taxwatch", "owner": "analysis", "mode": mode, "schedule": "Sun 01:35", "county_pause_s": 0}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]}, "stages": {STAGE: stage}}


def said(ok, msg, got) -> str:
    """The rule as the check's message, and what came back when it does not hold."""
    return msg if ok else "%s (got %r)" % (msg, got)


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry, taxwatch as tw
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE)
    config.DATA_DIR = Path(tmp) / "taxwatch"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    real_fetch, real_clock = tw._fetch, tw._clock
    reached = []

    def no_network(url, pause=0):
        reached.append(url)
        raise AssertionError("the selftest tried to reach a county site")

    tw._fetch = no_network
    try:
        check(all(registry.EXTRA.get(k) == row for k, row in REGISTRY_LINES.items())
              and not [p for p in registry.problems() if p.startswith("taxwatch")],
              "the tax watch kind loads from its registry line")
        _client(check, tw, real_fetch)
        _rules(check, tw, policy)
        _runs(check, tw, store, policy)
        _edges(check, tw, store, policy)
        check(not reached, said(not reached, "no check reached the real county site", reached[:2]))
    finally:
        tw._fetch, tw._clock = real_fetch, real_clock
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        config.DATA_DIR, config.DB_PATH, config.MODE = keep


# ====================================================================== the county client
def _client(check, tw, real_fetch) -> None:
    print("  the county client")
    got = tw.delinquent([bill(812.40, 2023), bill(790.10, 2024), bill(1500.0, 2026, delinquent=False),
                         bill(300.0, 2022, paid=True), {"due": 99.0, "taxYear": "2021", "delinquent": True}])
    check(got == {"amount": 1602.5, "years": ["2023", "2024"]},
          said(got == {"amount": 1602.5, "years": ["2023", "2024"]},
               "only delinquent, unpaid bills count: the current year's unpaid bill, a paid bill and a bill that does "
               "not say it is unpaid are left out", got))

    roll = FakeRoll(by_query={"100 EXAMPLE ST": [{"account_number": "001AA-001", "parcel_address": "100 EXAMPLE ST",
                                                   "amount_due": 9999.99}]},
                    bills={"001AA-001": [bill(812.40, 2023), bill(790.10, 2024), bill(1500.0, 2026, delinquent=False)]})
    got = tw.county_tax("100 Example Street", "Knox", fetch=roll)
    check(got and got["amount"] == 1602.5 and got["years"] == ["2023", "2024"] and got["account"] == "001AA-001",
          said(bool(got and got["amount"] == 1602.5), "a spelled-out suffix is asked the way the county writes it, and "
                                                      "the delinquent amount comes from the account's own bills", got))
    check(got and got["amount"] != 9999.99,
          "the parcel row's amount due (every unpaid bill, the current year's included) is never read as a delinquency")

    roll = FakeRoll(by_query={"9 Demo Dr": [{"account_number": "002BB-009", "parcel_address": "9 DEMO DR",
                                             "amount_due": 2345.67}]},
                    bills={"002BB-009": [bill(2345.67, 2026, delinquent=False)]})
    got = tw.county_tax("9 Demo Dr", "Knox", fetch=roll)
    check(got and got["amount"] == 0 and got["years"] == [],
          said(bool(got and got["amount"] == 0), "a house whose only unpaid bill is this year's reads as owing nothing, "
                                                 "whatever its amount due says", got))

    roll = FakeRoll(by_query={"214 Sample Rd": [{"account_number": "002BB-002", "parcel_address": "2140 SAMPLE RD"},
                                                {"account_number": "002BB-003", "parcel_address": "214 SAMPLE LN"}]},
                    bills={"002BB-002": [bill(9000.0, 2024)], "002BB-003": [bill(4000.0, 2024)]})
    got = tw.county_tax("214 Sample Rd", "Knox", fetch=roll)
    check(got is None and not [u for u in roll.asked if "/due/PPT/" in u],
          said(got is None, "a parcel whose own address is not the same house is never used, and its bills are never "
                            "asked for", got))

    roll = FakeRoll(by_query={"214 Sample Rd": [{"account_number": "002BB-002", "parcel_address": "2140 SAMPLE RD"},
                                                {"account_number": "002BB-009", "parcel_address": "214 SAMPLE RD"}]},
                    bills={"002BB-002": [bill(9000.0, 2024)], "002BB-009": [bill(321.0, 2024)]})
    got = tw.county_tax("214 Sample Rd", "Knox", fetch=roll)
    check(got and got["account"] == "002BB-009" and got["amount"] == 321.0,
          said(bool(got), "the matching house is found behind a wrong first result", got))

    roll = FakeRoll(by_query={"8 Shop Pike": [{"account_number": "BUS-8", "parcel_address": "8 SHOP PIKE"},
                                              {"account_number": "004DD-008", "parcel_address": "8 SHOP PIKE"}]},
                    bills={"004DD-008": [bill(55.0, 2025)]})
    got = tw.county_tax("8 Shop Pike", "Knox", fetch=roll)
    check(got and got["account"] == "004DD-008" and got["amount"] == 55.0,
          said(bool(got), "a business account at the same street (its bill route answers 404) is passed over for the "
                          "house's own account", got))

    roll = FakeRoll(by_query={"16 Twin Rd": [{"account_number": "005EE-001", "parcel_address": "16 TWIN RD"},
                                             {"account_number": "005EE-002", "parcel_address": "16 TWIN RD"}]},
                    bills={"005EE-001": [bill(100.0, 2024)], "005EE-002": [bill(700.0, 2024)]})
    try:
        tw.county_tax("16 Twin Rd", "Knox", fetch=roll)
        check(False, "two accounts at one house that both answer with bills are refused, never guessed")
    except tw.Ambiguous:
        check(True, "two accounts at one house that both answer with bills are refused, never guessed")

    # Measured 2026-11-18: all seven houses the encumbrance check could not read had a second account at
    # the same street, and none of them was a real conflict.
    roll = FakeRoll(by_query={"15 Example Ave": [{"account_number": "094BA-040", "parcel_address": "15 EXAMPLE AVE", "owner": "SAMPLE PAT"},
                                                 {"account_number": "1358392", "parcel_address": "15 EXAMPLE AVE", "owner": "DEMO JEWELRY"}]},
                    bills={"094BA-040": [dict(bill(258.0, 2024), bill_type="Real")], "135-8392": []})
    got = tw.county_tax("15 Example Ave", "Knox", fetch=roll)
    check(got and got["account"] == "094BA-040" and got["amount"] == 258.0,
          said(bool(got), "a business at the house whose account answers with no bills is passed over for the house's own", got))
    roll = FakeRoll(by_query={"9 Sample Ln": [{"account_number": "31616", "parcel_address": "9 SAMPLE LN", "owner": "SAMPLE ROBIN"},
                                              {"account_number": "94451", "parcel_address": "9 SAMPLE LN", "owner": "DEMO COTTAGE"}]},
                    bills={"31616": [dict(bill(1710.0, 2025, paid=True), bill_type="Real"), dict(bill(1681.0, 2024, paid=True), bill_type="Real")],
                           "94451": [dict(bill(36.0, 2025, paid=True), bill_type="Real")]})
    got = tw.county_tax("9 Sample Ln", "Blount", fetch=roll)
    check(got and got["amount"] == 0 and got["account"] == "31616",
          said(bool(got), "two accounts at one house that give the same answer (nothing delinquent) are not a conflict", got))
    roll = FakeRoll(by_query={"16 Twin Rd": [{"account_number": "005EE-001", "parcel_address": "16 TWIN RD", "owner": "EXAMPLE PAT Q"},
                                             {"account_number": "005EE-002", "parcel_address": "16 TWIN RD", "owner": "DEMO HOLDINGS LLC"}]},
                    bills={"005EE-001": [bill(100.0, 2024)], "005EE-002": [bill(700.0, 2024)]})
    got = tw.county_tax("16 Twin Rd", "Knox", fetch=roll, owner="Pat Example")
    check(got and got["account"] == "005EE-001" and got["amount"] == 100.0,
          said(bool(got), "when two accounts differ, the one in the record owner's name is the record's", got))
    check(tw.call_lookup(lambda street, county: "plain", "1 Test Way", "Knox", "Pat Example") == "plain"
          and tw.call_lookup(lambda street, county, owner="": owner, "1 Test Way", "Knox", "Pat Example") == "Pat Example",
          "the owner hint goes only to a lookup that takes it")

    roll = FakeRoll(by_query={"21 Quiz Ct": [{"account_number": "006FF-001", "parcel_address": "21 QUIZ CT"}]})
    try:
        tw.county_tax("21 Quiz Ct", "Knox", fetch=roll)
        check(False, "a house on the roll whose bills cannot be read raises Unreadable, not a zero")
    except tw.Ambiguous:
        check(False, "a house on the roll whose bills cannot be read raises Unreadable, not Ambiguous")
    except tw.Unreadable:
        check(True, "a house on the roll whose bills cannot be read raises Unreadable, not a zero")

    roll = FakeRoll()
    check(tw.county_tax("Example Street", "Knox", fetch=roll) is None and roll.asked == [],
          "a street with no house number is never looked up: nothing could be checked against it")
    try:
        tw.county_tax("5 Far Rd", "Sevier", fetch=roll)
        check(False, "a county with no roll read here raises NoRoll")
    except tw.NoRoll as exc:
        check(isinstance(exc, LookupError) and roll.asked == [],
              "a county with no roll read here raises NoRoll (a LookupError) and asks no site")

    roll = FakeRoll(by_query={"30 Parcel Way": [{"account_number": "048DE017", "parcel_address": "30 PARCEL WAY"}]},
                    bills={"048DE-017": [bill(64.0, 2025)]})
    got = tw.county_tax("30 Parcel Way", "Knox", fetch=roll)
    check(got and got["account"] == "048DE-017" and any("/due/PPT/048DE-017?" in u for u in roll.asked),
          said(bool(got), "a Knox account without its dash is asked for with it, the way the bill route wants it", got))

    roll = FakeRoll(by_query={"300 Lakeside Dr": [{"account_number": 41822, "parcel_address": "LAKESIDE DR 300"}]},
                    bills={"41822": [bill(1250.75, 2025)]})
    got = tw.county_tax("300 Lakeside Dr", "Blount County", fetch=roll)
    check(got and got["amount"] == 1250.75 and all(u.startswith("https://blount-tn.mygovonline.com/") for u in roll.asked),
          said(bool(got), "Blount is read on Blount's own site, its account passed through and a house number printed "
                          "last matched", got))

    try:
        tw.county_tax("12 Example St", "Knox", fetch=FakeRoll(down=True))
        check(False, "a county site that does not answer raises OSError")
    except OSError:
        check(True, "a county site that does not answer raises OSError, so the run can stop asking it")

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def opener(kind):
        def f(req, timeout=None):
            seen.append(req.get_header("User-agent"))
            if kind in (404, 503):
                raise urllib.error.HTTPError(req.full_url, kind, "status", {}, None)
            return Resp(b"<html>down for maintenance</html>" if kind == "html" else b'{"parcels": []}')
        return f

    def flaky(fails, kind):
        calls = []

        def f(req, timeout=None):
            calls.append(1)
            seen.append(req.get_header("User-agent"))
            if len(calls) <= fails:
                if kind == "drop":
                    raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
                if kind == "timeout":
                    raise TimeoutError("timed out")
                raise urllib.error.HTTPError(req.full_url, kind, "status", {}, None)
            return Resp(b'{"parcels": []}')
        return f, calls

    real_open = urllib.request.urlopen
    real_waits = tw.ROLL_RETRY_WAITS_S
    tw.ROLL_RETRY_WAITS_S = (0, 0)
    try:
        for kind in ("drop", "timeout", 502):
            urllib.request.urlopen, calls = flaky(1, kind)
            check(real_fetch("https://knox.example.test/parcels/x", pause=0) == {"parcels": []} and len(calls) == 2,
                  said(len(calls) == 2, "one %s is asked again and the answer is read, not the county stopped" % kind, calls))
        urllib.request.urlopen, calls = flaky(99, "drop")
        try:
            real_fetch("https://knox.example.test/parcels/x", pause=0)
            check(False, "a county that keeps dropping the connection stops the county")
        except OSError:
            check(len(calls) == 3, said(len(calls) == 3, "a county that keeps dropping is asked 3 times, then stops", calls))
        urllib.request.urlopen, calls = flaky(99, 500)
        try:
            real_fetch("https://knox.example.test/parcels/x", pause=0)
            check(False, "a 500 stops the county")
        except OSError:
            check(len(calls) == 1, said(len(calls) == 1, "a 500 is not a gateway hiccup: asked once", calls))
        urllib.request.urlopen = opener(404)
        check(real_fetch("https://knox.example.test/parcels/x", pause=0) is None, "the site's 404 reads as no answer here")
        out = {}
        for kind in (503, "html"):
            urllib.request.urlopen = opener(kind)
            try:
                real_fetch("https://knox.example.test/parcels/x", pause=0)
                out[kind] = "answered"
            except OSError:
                out[kind] = "down"
        check(out == {503: "down", "html": "down"},
              said(out == {503: "down", "html": "down"}, "an error status or a page that is not JSON reads as the site "
                                                         "being down", out))
        urllib.request.urlopen = opener("json")
        check(real_fetch("https://knox.example.test/parcels/x", pause=0) == {"parcels": []}, "a JSON answer is read as it is")
    finally:
        urllib.request.urlopen = real_open
        tw.ROLL_RETRY_WAITS_S = real_waits
    check(seen and all(h == tw.ROLL_AGENT for h in seen),
          "the county is asked under a plain client name, never a person's name or an email")


# ====================================================================== the rules, one at a time
def _rules(check, tw, policy) -> None:
    print("  the rules")
    st = tw.settings({})
    p = tw.decide(None, 1234.56, ["2023", "2024"], None, None, set(), st)
    check(p["event"] == "owes" and p["field"] == "1234.56" and p["year"] == 2023 and not p["add_tags"],
          said(p["field"] == "1234.56", "a new delinquency writes the figure and the earliest delinquent year", p))
    p = tw.decide("858.00", 858.0, ["2024"], None, 2024, set(), st)
    check(p["event"] == "unchanged" and p["field"] is None and p["year"] is None and not tw.wants_note(p),
          said(p["field"] is None, "a figure the record already shows is not written again, and gets no note", p))
    p = tw.decide("500", 858.0, ["2024"], None, None, set(), st)
    check(p["event"] == "changed" and p["field"] == "858" and p["showed"] == 500.0 and tw.wants_note(p),
          said(p["event"] == "changed", "another figure is written over the old one, with a note naming the old one", p))
    p = tw.decide("858.00", 0.0, [], None, 2024, set(), st)
    check(p["event"] == "paid" and p["field"] == "0" and p["clear_year"] and p["add_tags"] == ["Taxes Paid"]
          and p["showed"] == 858.0 and tw.wants_note(p),
          said(p["event"] == "paid", "a record that showed tax owed and is current gets 0, Taxes Paid, the year cleared "
                                     "and a note", p))
    p = tw.decide(None, 0.0, [], {"status": "owes", "amount": 300.0}, None, set(), st)
    check(p["event"] == "paid" and p["field"] == "0" and p["showed"] == 300.0,
          said(p["event"] == "paid", "a record whose field was cleared still counts as having paid when the last live "
                                     "reading said it owed", p))
    p = tw.decide(None, 0.0, [], None, None, set(), st)
    check(p["event"] == "current" and p["field"] is None and not p["add_tags"] and not tw.wants_note(p),
          said(p["field"] is None, "a clean reading on a blank field writes nothing by default (blank already reads as "
                                   "not owing)", p))
    p = tw.decide(None, 0.0, [], None, None, set(), dict(st, zero_on_blank=True))
    check(p["field"] == "0" and not tw.wants_note(p), "with zero_on_blank a clean reading on a blank field writes 0, no note")
    p = tw.decide("0", 0.0, [], None, None, set(), st)
    check(p["event"] == "current" and p["field"] is None, "a field that already reads 0 on a clean reading is left alone")
    p = tw.decide("0", 222.0, ["2025"], None, None, {"taxes paid"}, st)
    check(p["event"] == "owes_again" and p["field"] == "222" and p["remove_tags"] == ["Taxes Paid"] and tw.wants_note(p),
          said(p["event"] == "owes_again", "a record tagged Taxes Paid that owes again loses the tag and gets the new "
                                           "figure", p))
    p = tw.decide("0", 0.0, [], {"status": "owes", "amount": 0.0, "owed": 410.0}, None, {"taxes paid"}, st)
    check(p["event"] == "paid" and p["field"] is None and not p["add_tags"] and tw.wants_note(p),
          said(p["event"] == "paid", "a paid record left half done by a refused write gets only what is missing (the "
                                     "note)", p))
    off = dict(st, year_field="")
    p1 = tw.decide(None, 100.0, ["2024"], None, None, set(), off)
    p2 = tw.decide("100", 0.0, [], None, 2024, set(), off)
    check(p1["year"] is None and not p2["clear_year"], "with year_field empty the year is never written or cleared")

    day = tw._day(NOW, "America/New_York")
    paid_note = tw.note_text(tw.decide("858.00", 0.0, [], None, None, set(), st), 0.0, [], "Knox", day)
    owe_note = tw.note_text(tw.decide(None, 1234.56, ["2023", "2024"], None, None, set(), st), 1234.56,
                            ["2023", "2024"], "Knox", day)
    check(day == "Nov 15, 2026" and "Nov 15, 2026" in paid_note and "Knox County Trustee" in paid_note and "$858" in paid_note
          and "city tax is billed separately" in paid_note,
          said("$858" in paid_note, "the paid note is dated, names the county and the old figure, and says it is county "
                                    "tax only", paid_note))
    check("$1,234.56" in owe_note and "for 2023 and 2024" in owe_note,
          said("for 2023 and 2024" in owe_note, "the note names the delinquent amount and every delinquent year", owe_note))
    alert = tw.alert_text(tw.decide("300", 0.0, [], None, None, set(), st), "10 Lead Ave", 0.0, [], "Knox")
    check("10 Lead Ave" in alert and not PHONE_RX.search(alert) and not any(d in alert + paid_note + owe_note for d in DASHES),
          said("10 Lead Ave" in alert, "an alert names the street and carries no phone number; no note or alert carries a "
                                       "dash", alert))
    check("2026" not in alert and "Nov" not in alert,
          said("2026" not in alert, "an alert carries no date, so the same news found again next week is not posted "
                                    "again", alert))

    tz = "America/New_York"
    check(tw.is_due(None, False, st, NOW, tz), "a record never checked is due")
    check(not tw.is_due({"at": (NOW - timedelta(days=10)).isoformat(), "status": "current"}, False, st, NOW, tz),
          "a clean reading 10 days old is not due")
    check(tw.is_due({"at": (NOW - timedelta(days=29)).isoformat(), "status": "owes"}, False, st, NOW, tz)
          and not tw.is_due({"at": (NOW - timedelta(days=29)).isoformat(), "status": "current"}, False, st, NOW, tz),
          "a reading that owes is due again after 28 days, a clean one is not")
    check(tw.is_due({"at": (NOW - timedelta(days=8)).isoformat(), "status": "current"}, True, st, NOW, tz),
          "a lead or a hot auction is due again after 7 days")
    check(tw.is_due({"at": (NOW - timedelta(days=7) + timedelta(seconds=40)).isoformat(), "status": "current"}, True, st,
                    NOW, tz)
          and not tw.is_due({"at": (NOW - timedelta(days=6)).isoformat(), "status": "current"}, True, st, NOW, tz),
          "a weekly slot that fires a few seconds early still finds last week's lead due, and six days is not a week")
    mar = datetime(2027, 3, 5, 12, 0, tzinfo=timezone.utc)
    check(tw.is_due({"at": datetime(2027, 2, 20, 12, 0, tzinfo=timezone.utc).isoformat(), "status": "current"},
                    False, st, mar, tz)
          and not tw.is_due({"at": datetime(2027, 3, 2, 12, 0, tzinfo=timezone.utc).isoformat(), "status": "current"},
                            False, st, mar, tz),
          "a reading taken before March 1 is due once March 1 passes (the prior year's bill turns delinquent then)")
    old, new = (NOW - timedelta(days=200)).isoformat(), (NOW - timedelta(days=100)).isoformat()
    keys = [tw.order_key(None, {"urgent": True, "rank": 1}), tw.order_key({"at": old}, {"urgent": True, "rank": 0}),
            tw.order_key(None, {"urgent": False, "rank": 3}), tw.order_key({"at": old}, {"urgent": False, "rank": 6}),
            tw.order_key({"at": new}, {"urgent": False, "rank": 3})]
    check(keys == sorted(keys), "a lead or hot auction first, then never checked, then the oldest check")

    def stage_of(**params):
        return policy.Stage(id="t", kind="taxwatch", owner="analysis", mode="plan", summary="", schedule=[], presets=[],
                            params=params)

    check(tw.block_problems(stage_of(**PROPOSED)) == [], said(tw.block_problems(stage_of(**PROPOSED)) == [],
                                                              "the block handed to the orchestrator is clean",
                                                              tw.block_problems(stage_of(**PROPOSED))))
    bad = {"amount_field": "Total Delinquency", "year_field": "foreclosure_date", "max_records": 0, "recheck_days": True,
           "delinquent_from": "02-30", "scope_lists": "Probate", "note": "yes", "max_minutes": 0}
    words = " | ".join(tw.block_problems(stage_of(**bad)))
    check(all(w in words for w in ("Total Delinquency", "year_field", "max_records", "recheck_days", "delinquent_from",
                                   "scope_lists", "note", "max_minutes")),
          said("Total Delinquency" in words, "a block naming Total Delinquency, another record field, a bad number, date, "
                                             "list or switch is refused when the playbook loads", words))
    try:
        policy.parse(playbook(amount_field="Total Delinquency"), EMPLOYEES)
        check(False, "a playbook pointing the tax figure at Total Delinquency does not load")
    except policy.PolicyError:
        check(True, "a playbook pointing the tax figure at Total Delinquency does not load")
    try:
        pol = policy.parse(playbook("plan", **PROPOSED), EMPLOYEES)
        check(pol.stage(STAGE).kind == "taxwatch", "the proposed stage block loads as written")
    except policy.PolicyError as exc:
        check(False, "the proposed stage block loads as written (%s)" % str(exc)[:200])

    pp = tw.settings({})
    check(tw.person_hold({"first_name": "Pat", "last_name": "Example"}, pp) == "", "a person is looked up")
    check(tw.person_hold({"company": "Example Holdings LLC"}, pp) == "owned by a company or trust",
          "a company owner is not looked up (people only)")
    check(tw.person_hold({"first_name": "Pat", "last_name": "Example Trust"}, pp) == "owned by a company or trust",
          "a trust counts as a company")
    check(tw.person_hold({"company": "Estate of Pat Example"}, pp) == ""
          and tw.person_hold({"company": "Estate of Pat Example"}, dict(pp, estates_are_people=False)) != "",
          "a person's estate counts as the person, unless estates_are_people is false")
    check(tw.person_hold({"company": "Example Real Estate"}, pp) == "owned by a company or trust",
          "a real estate business is not taken for a person's estate")
    check(tw.person_hold({"last_name": "Example"}, pp) == "",
          "an owner missing a first name, with no company, is a person whose record is incomplete")


# ====================================================================== whole runs
def _cast():
    """The records and the county's answers the main runs work on. r13 is sold (the search leaves it out),
    r17 has no street (its row is enough to skip it)."""
    from .autopilot import taxwatch as tw
    records = [
        rec("r1", "12 Example St", year=2024),
        rec("r2", "40 Sample Rd"),
        rec("r3", "77 Test Ln", lists=("Obituary",)),
        rec("r4", "9 Demo Dr", lists=("Tax Sale",)),
        rec("r5", "300 Mock Ave"),
        rec("r6", "21 Quiz Ct"),
        rec("r7", "15 Trial Way", county="Blount"),
        rec("r8", "5 Far Rd", county="Sevier"),
        rec("r9", "6 Firm St", first=None, last=None, company="Example Holdings LLC"),
        rec("r10", "7 Estate Ln", first=None, last=None, company="Estate of Pat Example"),
        rec("r11", "8 Sold Rd", tags=("recently sold",)),
        rec("r12", "10 Lead Ave", lists=(), status="new_lead"),
        rec("r13", "11 Gone St", status="sold"),
        rec("r14", "13 Again Way", tags=("Taxes Paid",)),
        rec("r15", "14 Ohio St", state="OH"),
        rec("r16", "16 Twin Rd"),
        rec("r17", ""),
        rec("r18", "18 Auction Rd", lists=(), tags=("Auction Critical",)),
    ]
    custom = {"r1": {"Tax delinquency amount": "858.00", "Total Delinquency": "13766.72", "County": "Knox"},
              "r3": {"Tax delinquency amount": "500"},
              "r12": {"Tax delinquency amount": "300"},
              "r14": {"Tax delinquency amount": "0"}}
    county = {
        "12 Example St": answer(0.0), "40 Sample Rd": answer(1234.56, ["2023", "2024"]),
        "77 Test Ln": answer(858.0, ["2024"]), "9 Demo Dr": answer(0.0), "300 Mock Ave": None,
        "21 Quiz Ct": tw.Unreadable("the county has the house, but no account there answered with bills"),
        "15 Trial Way": answer(410.1, ["2025"], account="41822"), "7 Estate Ln": answer(0.0),
        "10 Lead Ave": answer(0.0), "13 Again Way": answer(222.0, ["2025"]),
        "16 Twin Rd": tw.Ambiguous("2 county accounts at that house answer with bills"),
        "18 Auction Rd": answer(95.5, ["2025"]),
    }
    return records, custom, county


def _runs(check, tw, store, policy) -> None:
    from .autopilot import ledger, sweep, undo
    print("  a plan run, then the first live run")
    records, custom, answers = _cast()
    crm, county = TaxCRM(records, custom=custom), FakeCounty(answers)
    out = sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook("plan"), EMPLOYEES), sources={"county_tax": county})
    asked = {s for s, _ in county.calls}
    check(not out.live and crm.writes == [] and crm.msgs == [] and crm.patches == [],
          said(crm.writes == [], "a plan run writes nothing to DataSift", crm.writes[:3]))
    check(out.found == 17, said(out.found == 17, "every list, tag and lead status is in scope, a sold record is not", out.found))
    check("Would update 7 record(s)" in out.head and "5 owe $2,820.16" in out.head and "2 paid" in out.head,
          said("Would update 7" in out.head, "the plan's head says how many owe, how much, how many paid and what it "
                                             "would update", out.head))
    check(not asked & {"5 Far Rd", "6 Firm St", "8 Sold Rd", "11 Gone St", "14 Ohio St", ""},
          said(not asked & {"5 Far Rd", "6 Firm St"}, "another county, a company owner, a sold or held record, a record "
                                                      "outside Tennessee and a record with no street are never looked "
                                                      "up", sorted(asked)))
    check(("15 Trial Way", "Blount") in county.calls and "7 Estate Ln" in asked,
          "a Blount record is looked up as Blount, and a person's estate is looked up")
    check(all(ledger.fact(r["uuid"], tw.MEMORY) is None for r in records),
          "a plan run leaves the live memory empty, so the first live run still writes")
    pm = ledger.fact("r2", tw.MEMORY_PLAN) or {}
    check(pm.get("status") == "owes" and pm.get("amount") == 1234.56,
          said(pm.get("status") == "owes", "a plan run remembers the county's answer in its own memory", pm))
    check(len(out.alerts) == 1 and "10 Lead Ave" in out.alerts[0] and not PHONE_RX.search(out.alerts[0]),
          said(len(out.alerts) == 1, "a lead whose tax was paid is one alert for the lead manager, with no phone number "
                                     "(a hot auction that is not a lead gets none)", out.alerts))
    lines = " | ".join(out.lines)
    for frag, rule in (("Owe delinquent county tax: 5 record(s), $2,820.16 in all (largest $1,234.56 at 40 Sample Rd)",
                        "the run says how many owe, the total and the largest"),
                       ("Paid since the record showed them owing: 2", "the run counts the records that paid"),
                       ("Owe again after showing paid: 1 (13 Again Way)", "the run names a record that owes again"),
                       ("Would write: 7 tax figure(s), 6 delinquent year(s), 2 Taxes Paid tag(s) added, 1 removed, 7 note(s)",
                        "the plan counts every write it would make"),
                       ("No parcel at that street on the county roll: 1", "a street the roll does not have is counted"),
                       ("bills could not be read: 1", "an account whose bills cannot be read is counted"),
                       ("two or more accounts answering at one house: 1", "two accounts at one house is counted"),
                       ("Nothing was written for these", "nothing is written for a miss"),
                       ("Not on a county roll read here: 2 (Sevier County 1, outside Tennessee 1)",
                        "other counties are counted and named"),
                       ("Total Delinquency still holds the old tax on 1", "a stale Total Delinquency is said, not touched"),
                       ("owned by a company or trust", "a company owner is counted as not looked up"),
                       ("no street on the record", "a record with no street is counted as not looked up")):
        check(frag in lines, said(frag in lines, rule, lines))
    with (config.DATA_DIR / "autopilot" / "taxwatch" / tw.PERSON_FILE).open(encoding="utf-8", newline="") as fh:
        person = {r["record"]: r["why"] for r in csv.DictReader(fh)}
    check(set(person) == {"r8", "r15", "r16"},
          said(set(person) == {"r8", "r15", "r16"}, "another county, a record outside Tennessee and two accounts at one "
                                                    "house are listed for a person", sorted(person)))

    config.MODE = "live"
    try:
        crm = TaxCRM(records, custom=custom)
        county = FakeCounty(answers)
        live = sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook("live"), EMPLOYEES),
                         sources={"county_tax": county})
        check(live.live and "Updated 7 record(s)" in live.head, said("Updated 7" in live.head, "the first live run after a "
                                                                    "plan run writes, and says so", live.head))
        check(("field", "r2", "f-tax", "1234.56") in crm.writes and ("patch", "r2", (("tax_delinquent_year", 2023),))
              in crm.writes, said(("field", "r2", "f-tax", "1234.56") in crm.writes,
                                  "a new delinquency is written to Tax delinquency amount and the earliest year to the "
                                  "record's Tax Delinquent Year", crm.touched("r2")))
        check(("field", "r1", "f-tax", "0") in crm.writes and ("add_tags", "r1", ("Taxes Paid",)) in crm.writes
              and ("patch", "r1", (("tax_delinquent_year", None),)) in crm.writes,
              said(("add_tags", "r1", ("Taxes Paid",)) in crm.writes, "a paid record gets 0, the Taxes Paid tag and its "
                                                                      "year cleared", crm.touched("r1")))
        check(not [w for w in crm.writes if "f-total" in w],
              "Total Delinquency is never written, not even on the record that carries one")
        check(("remove_tags", "r14", ("Taxes Paid",)) in crm.writes and ("field", "r14", "f-tax", "222") in crm.writes,
              "a record that owes again loses Taxes Paid and gets the new figure")
        quiet = [u for u in ("r4", "r5", "r6", "r8", "r9", "r10", "r11", "r13", "r15", "r16", "r17") if crm.touched(u)]
        check(not quiet, said(not quiet, "nothing is written for a clean reading on a blank field, a miss, an unreadable "
                                         "account, two accounts at one house, another county or a held record", quiet))
        notes = {u: t for u, t in crm.msgs}
        check("Taxes paid" in notes.get("r1", "") and "$858" in notes.get("r1", "") and "The record showed $500"
              in notes.get("r3", "") and "for 2023 and 2024" in notes.get("r2", ""),
              said("Taxes paid" in notes.get("r1", ""), "each write carries a dated note in plain words", notes))
        check(not any(d in t for t in notes.values() for d in DASHES), "no note carries an em or en dash")
        changes = ledger.changes(record_uuid="r1")
        kinds = {c["what"]: c for c in changes}
        check(kinds.get("custom_field", {}).get("before") == {"field_uuid": "f-tax", "value": "858.00"}
              and kinds.get("record_fields", {}).get("before") == {"tax_delinquent_year": 2024}
              and kinds.get("add_tags", {}).get("after") == ["Taxes Paid"],
              said("custom_field" in kinds, "every live change keeps the value it replaced", changes))
        steps = undo.plan(changes[0]["run_id"]) if changes else []
        back = [(s["record_uuid"], s["do"], s["args"]) for s in steps]
        check(("r1", "custom_field", {"field_uuid": "f-tax", "value": "858.00"}) in back
              and ("r1", "remove_tags", ["Taxes Paid"]) in back and ("r14", "add_tags", ["Taxes Paid"]) in back,
              said(bool(back), "undo puts the old figure back, takes the new tag off and gives back a removed one", back[:6]))
        lm = ledger.fact("r2", tw.MEMORY) or {}
        check(lm.get("status") == "owes" and lm.get("amount") == 1234.56 and lm.get("at") == NOW.isoformat(timespec="seconds"),
              said(lm.get("status") == "owes", "a live run remembers the reading it acted on", lm))
        check(not [p for p in live.problems if "did not take" in p], "the year written on the first record read back as written")

        print("  the next runs: nothing twice, owing records again in 28 days, a lead every week")
        before = len(county.calls)
        again = sweep.run(STAGE, crm=crm, now=NOW + timedelta(hours=2), pol=policy.parse(playbook("live"), EMPLOYEES),
                          sources={"county_tax": county})
        check(len(county.calls) == before and again.head.startswith("Nothing is due"),
              said(len(county.calls) == before, "a run right after asks the county nothing", again.head))
        before = len(county.calls)
        month = sweep.run(STAGE, crm=crm, now=NOW + timedelta(days=29), pol=policy.parse(playbook("live"), EMPLOYEES),
                          sources={"county_tax": county})
        streets = sorted(s for s, _ in county.calls[before:])
        want = sorted(["40 Sample Rd", "77 Test Ln", "15 Trial Way", "13 Again Way", "18 Auction Rd", "10 Lead Ave",
                       "21 Quiz Ct"])
        check(streets == want, said(streets == want, "29 days on, only the records that owe, the unreadable one, the lead "
                                                     "and the hot auction are asked again", streets))
        check(month.acted == 0 and "Updated 0" in month.head,
              said(month.acted == 0, "an unchanged county answer writes nothing", month.head))
        answers2 = dict(answers, **{"12 Example St": answer(150.0, ["2026"])})
        county2 = FakeCounty(answers2)
        crm.writes.clear()
        crm.msgs.clear()
        sweep.run(STAGE, crm=crm, now=NOW + timedelta(days=92), pol=policy.parse(playbook("live"), EMPLOYEES),
                  sources={"county_tax": county2})
        notes = {u: t for u, t in crm.msgs}
        check(("remove_tags", "r1", ("Taxes Paid",)) in crm.writes and ("field", "r1", "f-tax", "150") in crm.writes
              and "It had shown as paid before" in notes.get("r1", ""),
              said(("field", "r1", "f-tax", "150") in crm.writes, "a record that paid and owes a new year loses the tag "
                                                                  "and says so, 91 days on", crm.touched("r1")))
    finally:
        config.MODE = "dry"


def _edges(check, tw, store, policy) -> None:
    from .autopilot import ledger, stages, sweep
    print("  the limits and the ways a run can go wrong")
    config.MODE = "live"
    pol = policy.parse(playbook("live"), EMPLOYEES)
    try:
        # the cap, and urgent first
        recs = [rec("c%d" % i, "%d Cap Rd" % i) for i in range(1, 5)] + [rec("c9", "9 Cap Rd", lists=(), status="Warm Lead")]
        county = FakeCounty({"%d Cap Rd" % i: answer(0.0) for i in (1, 2, 3, 4, 9)})
        crm = TaxCRM(recs)
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook("live", max_records=2), EMPLOYEES),
                        sources={"county_tax": county})
        check(len(county.calls) == 2 and county.calls[0][0] == "9 Cap Rd" and crm.reads[0] == "c9",
              said(len(county.calls) == 2, "a run reads no more than max_records, and a lead first", county.calls))
        check("left for later runs: 3" in " | ".join(out.lines), "the records past the cap are said to wait for later runs")

        # the county site stops answering
        recs = [rec("d1", "1 Down Rd"), rec("d2", "2 Down Rd"), rec("d3", "3 Down Rd")]
        county = FakeCounty({"1 Down Rd": OSError("timed out"), "2 Down Rd": answer(10.0, ["2025"]),
                             "3 Down Rd": answer(0.0)})
        crm = TaxCRM(recs)
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": county})
        check(len(county.calls) == 1 and crm.writes == [] and len(crm.reads) == 1
              and any("did not answer" in p for p in out.problems),
              said(len(county.calls) == 1, "a county site that does not answer stops the run's reading, says so, and "
                                           "writes nothing", (county.calls, out.problems)))
        check(all(ledger.fact(u, tw.MEMORY) is None for u in ("d1", "d2", "d3")),
              "records the county could not answer are not remembered, so the next run asks again")
        county.answers["1 Down Rd"] = answer(0.0)
        sweep.run(STAGE, crm=crm, now=NOW + timedelta(hours=1), pol=pol, sources={"county_tax": county})
        check(len(county.calls) == 4 and ("field", "d2", "f-tax", "10") in crm.writes,
              said(len(county.calls) == 4, "the next run reads them all once the site answers", county.calls))

        # a refused write, finished on the next run without a second note
        w1 = [rec("w1", "1 Write Way")]
        county = FakeCounty({"1 Write Way": answer(0.0)})
        crm = TaxCRM(w1, custom={"w1": {"Tax delinquency amount": "500"}}, fail_tags=True)
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": county})
        mem = ledger.fact("w1", tw.MEMORY) or {}
        check(any("refused a tax write" in p for p in out.problems) and not crm.msgs and mem.get("at") == ""
              and mem.get("owed") == 500.0,
              said(mem.get("owed") == 500.0, "a refused write is said, stops the record before its note, and leaves it "
                                             "due with what it owed", (out.problems, mem)))
        crm2 = TaxCRM(list(crm.recs.values()), custom=crm.custom)
        sweep.run(STAGE, crm=crm2, now=NOW + timedelta(hours=1), pol=pol, sources={"county_tax": county})
        check(not crm2.of("w1", "field") and crm2.of("w1", "add_tags") and len(crm2.msgs) == 1
              and "$500" in crm2.msgs[0][1],
              said(len(crm2.msgs) == 1, "the next run writes only what was missing, and the paid note once", crm2.writes))

        # the year does not take
        ys = [rec("y1", "1 Year Rd"), rec("y2", "2 Year Rd")]
        county = FakeCounty({"1 Year Rd": answer(50.0, ["2024"]), "2 Year Rd": answer(60.0, ["2025"])})
        crm = TaxCRM(ys, year_sticks=False)
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": county})
        check(len(crm.patches) == 1 and any("did not take" in p for p in out.problems)
              and crm.of("y1", "field") and crm.of("y2", "field"),
              said(len(crm.patches) == 1, "a year that does not read back stops the year writes for the run, not the "
                                          "figures", (crm.patches, out.problems)))

        # the tag is missing, and the field is missing
        crm = TaxCRM([rec("t1", "1 Tag Rd")], custom={"t1": {"Tax delinquency amount": "400"}}, missing_tags={"Taxes Paid"})
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": FakeCounty({"1 Tag Rd": answer(0.0)})})
        check(any("'Taxes Paid' tag" in p for p in out.problems) and not crm.of("t1", "add_tags") and len(crm.msgs) == 1,
              said(not crm.of("t1", "add_tags"), "a missing Taxes Paid tag is said, not written, and the record still gets "
                                                 "its note", out.problems))
        crm = TaxCRM([rec("f1", "1 Field Rd")], fields={"total delinquency": "f-total"})
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol,
                        sources={"county_tax": FakeCounty({"1 Field Rd": answer(120.0, ["2025"])})})
        check(any("no 'Tax delinquency amount' custom field" in p for p in out.problems) and not crm.of("f1", "field")
              and crm.msgs and "$120" in crm.msgs[0][1],
              said(not crm.of("f1", "field"), "a missing tax field is said, and the figure goes into the note instead",
                   (out.problems, crm.msgs)))
        crm = TaxCRM([rec("f2", "2 Field Rd")], fields={"tax delinquency amount": "f-total", "total delinquency": "f-total"})
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol,
                        sources={"county_tax": FakeCounty({"2 Field Rd": answer(75.0, ["2025"])})})
        check(not [w for w in crm.writes if "f-total" in w] and any("Total Delinquency" in p for p in out.problems),
              "a tax field that resolves to Total Delinquency is refused at write time too")

        # the playbook check bypassed: the stage itself refuses Total Delinquency
        bypass = policy.Stage(id="tw_raw", kind="taxwatch", owner="analysis", mode="live", summary="", schedule=[],
                              presets=[], params={"amount_field": "total delinquency"})
        county = FakeCounty({"1 Raw Rd": answer(10.0)})
        crm = TaxCRM([rec("x1", "1 Raw Rd")])
        summ = stages.Summary(stage="tw_raw", kind="taxwatch", owner="analysis", mode="live", live=True)
        ctx = stages.Ctx(policy=pol, stage=bypass, crm=crm, run_id="raw", now=NOW, live=True, out=summ,
                         sources={"county_tax": county})
        tw.run_taxwatch(ctx)
        check(summ.head.startswith("Not run") and not county.calls and crm.writes == [],
              said(not county.calls, "a stage pointed at Total Delinquency does nothing, even past the playbook check",
                   summ.head))

        # search rows that carry the owner's names decide a company owner without reading it
        crm = TaxCRM([rec("e1", "1 Firm Rd", first=None, last=None, company="Example Holdings LLC"), rec("e2", "2 Firm Rd")],
                     rich_rows=True)
        county = FakeCounty({"2 Firm Rd": answer(0.0)})
        sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": county})
        check("e1" not in crm.reads and "e2" in crm.reads, "a company owner on a search row is skipped without a read")

        # estates held when the setting says so; zero_on_blank; a list that does not exist
        crm = TaxCRM([rec("s1", "1 Estate Rd", first=None, last=None, company="Estate of Pat Example"),
                      rec("s2", "2 Zero Rd")], missing_lists={"Tax Delinquent"})
        county = FakeCounty({"1 Estate Rd": answer(0.0), "2 Zero Rd": answer(0.0)})
        out = sweep.run(STAGE, crm=crm, now=NOW, sources={"county_tax": county},
                        pol=policy.parse(playbook("live", estates_are_people=False, zero_on_blank=True), EMPLOYEES))
        check([s for s, _ in county.calls] == ["2 Zero Rd"], "with estates_are_people false an estate owner is not looked up")
        check(("field", "s2", "f-tax", "0") in crm.writes and not crm.msgs,
              "with zero_on_blank a clean reading on a blank field is written as 0, with no note")
        check(any("no list titled 'Tax Delinquent'" in p for p in out.problems),
              "a scope list the account does not have is said, not read as empty")

        # most of the county's answers failing is said out loud, not read as a quiet week
        many = [rec("g%d" % i, "%d Ghost Rd" % i) for i in range(1, 11)]
        county = FakeCounty({"%d Ghost Rd" % i: tw.Unreadable("no bills") for i in range(1, 11)})
        out = sweep.run(STAGE, crm=TaxCRM(many), now=NOW, pol=pol, sources={"county_tax": county})
        check(any("could not be read for most records" in p for p in out.problems),
              said(len(out.problems) > 0, "a run where most bills cannot be read says so as a problem", out.problems))
        many = [rec("h%d" % i, "%d Empty Rd" % i) for i in range(1, 11)]
        county = FakeCounty({})
        out = sweep.run(STAGE, crm=TaxCRM(many), now=NOW, pol=pol, sources={"county_tax": county})
        check(any("no parcel for most of the streets" in p for p in out.problems),
              said(len(out.problems) > 0, "a run where the county finds no parcel for most streets says so as a problem",
                   out.problems))

        # a record whose address and fields name no county is listed for a person, not asked of either site
        county = FakeCounty({"1 Blank Rd": answer(5.0, ["2025"])})
        out = sweep.run(STAGE, crm=TaxCRM([rec("z1", "1 Blank Rd", county="")]), now=NOW, pol=pol,
                        sources={"county_tax": county})
        check(not county.calls and "no county named 1" in " | ".join(out.lines),
              said(not county.calls, "a record that names no county is never looked up and is said as such", out.lines))

        # notes switched off
        crm = TaxCRM([rec("n1", "1 Quiet Rd")])
        sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook("live", note=False), EMPLOYEES),
                  sources={"county_tax": FakeCounty({"1 Quiet Rd": answer(42.0, ["2025"])})})
        check(crm.of("n1", "field") and not crm.msgs, "with note false the figure is written and no note is added")

        # the time limit
        ticks = iter([0.0, 0.0, 2500.0, 2500.0, 2500.0])
        tw._clock = lambda: next(ticks, 2500.0)
        crm = TaxCRM([rec("k1", "1 Clock Rd"), rec("k2", "2 Clock Rd")])
        county = FakeCounty({"1 Clock Rd": answer(0.0), "2 Clock Rd": answer(0.0)})
        out = sweep.run(STAGE, crm=crm, now=NOW, pol=pol, sources={"county_tax": county})
        check(len(county.calls) == 1 and "Stopped at the 40 minute limit" in " | ".join(out.lines),
              said(len(county.calls) == 1, "a run stops reading at its time limit and says so", out.lines))
        tw._clock = __import__("time").monotonic

        # March 1: a clean reading in February is asked again in March
        crm = TaxCRM([rec("m1", "1 March Rd")])
        county = FakeCounty({"1 March Rd": answer(0.0)})
        sweep.run(STAGE, crm=crm, now=datetime(2027, 2, 20, 12, 0, tzinfo=timezone.utc), pol=pol,
                  sources={"county_tax": county})
        county.answers["1 March Rd"] = answer(310.0, ["2026"])
        sweep.run(STAGE, crm=crm, now=datetime(2027, 3, 5, 12, 0, tzinfo=timezone.utc), pol=pol,
                  sources={"county_tax": county})
        check(len(county.calls) == 2 and ("field", "m1", "f-tax", "310") in crm.writes,
              said(len(county.calls) == 2, "a clean February reading is asked again after March 1 and catches the new "
                                           "delinquency", county.calls))

        # the module's own text carries no dash
        src = Path(tw.__file__).read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
        check(not any(d in src for d in DASHES) and "\x08" not in src, "the stage and its selftest hold no em or en dash")
    finally:
        config.MODE = "dry"
        tw._clock = __import__("time").monotonic
