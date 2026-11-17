"""Lead intake selftest: the appointment alert (C8.20) and the encumbrances filled when a record becomes
a lead. Zero network: the lead selftest's fake DataSift for the appointments, a small fake DataSift
and a fake county roll for the encumbrances, its own database. Every name, street, number and id here
is invented; the one phone number uses the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import copy
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, ET, T0

# The exact line for autopilot/registry.py EXTRA. The section registers it for its own length when
# the table does not carry it yet, so it passes before and after the orchestrator adds it.
REGISTRY_LINES = {"encumbrance": ("encumbrance", "run_encumbrance", "free", False, False)}

# The exact stage block for team/autopilot.yaml. Parsed below, so a block that would not load fails here.
PLAYBOOK_BLOCK = """
  lead_encumbrances:
    kind: encumbrance
    owner: sales
    mode: "plan"
    schedule: "every 30 min 08:00-19:00 Mon-Sat"
    summary: When a record becomes a lead, its encumbrances are filled in from what can be checked (delinquent county tax from the Trustee's own bills, the liens our lien records put on it, the mortgage on the record), with Unknown where nothing was checked, in a note and in the intake fields that exist.
    # The lesson: "Fill the encumbrance fields: mortgage balance, months behind, back taxes (Unknown
    # until checked) and the back taxes amount, HOA, and code violation liens." Writes Tax delinquency
    # amount and a note "Encumbrances" on the record; never Total Delinquency (liens plus taxes, kept
    # by the lien records). County tax only, Knox and Blount (the Trustee sites), by street.
    gate_allow: [lead]
    lead_statuses: [new_lead, No Contact New Lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, follow_up, lead,
                    underwrite, under_contract, push_deal, transactions, refer_lead]
    first_window_days: 14         # how far back the first run looks for records that became leads
    max_records: 15               # new leads checked a run (a record read, its fields, up to four county requests)
    max_tries: 3                  # runs a lead waits for the county tax site before its note says Unknown
    note: true
    fields:                       # what each value fills; a label the account does not have goes in the note only
      tax: Tax delinquency amount
      mortgage_balance: Mortgage Balance
      months_behind: Months Behind
      hoa: HOA
      code_liens: Code Liens
    overwrite: [tax]              # the county's figure replaces an older one; every other field a person filled is kept
"""

BUSINESS = {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=ET).astimezone(timezone.utc)      # a Monday morning
NETWORK: list = []

ACCOUNT_FIELDS = [            # the fields prod-account carries today (2026-11-15), as far as this stage reads them
    {"uuid": "f-tax", "label": "Tax delinquency amount", "field_type": "money", "entity_type": "property", "options": []},
    {"uuid": "f-total", "label": "Total Delinquency", "field_type": "money", "entity_type": "property", "options": []},
    {"uuid": "f-lien", "label": "Outstanding Lien Amount", "field_type": "money", "entity_type": "property", "options": []},
    {"uuid": "f-count", "label": "Liens Active vs Total", "field_type": "text_input", "entity_type": "property", "options": []},
]
WITH_INTAKE = ACCOUNT_FIELDS + [
    {"uuid": "f-months", "label": "Months Behind", "field_type": "text_input", "entity_type": "property", "options": []},
    {"uuid": "f-mort", "label": "Mortgage Balance", "field_type": "money", "entity_type": "property", "options": []},
]


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def status_ev(i, rec, when, old, new):
    return {"id": "st-li-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": "app", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def _no_network(url, **_kw):
    NETWORK.append(url)
    raise AssertionError("the selftest reached for a county roll: %s" % url)


def _home(u, street, county, status, **kw):
    city = {"Knox": "Knoxville", "Blount": "Maryville"}.get(county, "Sevierville")
    r = {"uuid": u, "address": {"street": street, "city": city, "state": "TN", "postal_code": "37917", "county": county},
         "owner": {"uuid": "o-" + u, "first_name": "Pat", "last_name": "Example", "company": None, "phones": []},
         "tags": [], "lists": [], "status": status, "assigned_to": "u-emerson", "open_mortgages": None,
         "mortgage_type": None, "estimate_value": None, "equity_percent": None, "loan_to_value": None,
         "lien_type": None, "lien_recording_date": None}
    r.update(kw)
    return r


class EncCRM:
    """The few DataSift routes the encumbrance stage uses: the record, its custom fields, the account's
    field list, a field write, a note and the account log."""

    def __init__(self, recs, custom=None, fields=WITH_INTAKE, log_rows=()):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in recs}
        self.custom = copy.deepcopy(custom or {})
        self.fields = copy.deepcopy(fields)
        self.log_rows = list(log_rows)
        self.reads, self.writes, self.notes = [], [], []

    def record(self, u):
        self.reads.append(("record", u))
        return copy.deepcopy(self.recs.get(u) or {})

    def custom_fields(self, u):
        self.reads.append(("fields", u))
        return dict(self.custom.get(u, {}))

    def call(self, path, method="GET", body=None, headers=None):
        if method == "GET" and path.split("?")[0] == "/api/internal/custom-fields/":
            self.reads.append(("field list", ""))
            return {"count": len(self.fields), "results": copy.deepcopy(self.fields)}
        raise AssertionError("unexpected call %s %s" % (method, path))

    def set_custom_field(self, u, field_uuid, value):
        self.writes.append(("field", u, field_uuid, value))
        label = next(f["label"] for f in self.fields if f["uuid"] == field_uuid)
        self.custom.setdefault(u, {})[label] = value

    def message(self, u, text, pin=False):
        self.writes.append(("note", u))
        self.notes.append((u, text))
        return ""

    def users(self):
        return []

    def logs(self, must, limit=100, cursor="", include_owner=False):
        types = set(must.get("any_event_type") or [])
        lo, hi = (list(must.get("timestamp") or [None, None]) + [None, None])[:2]
        out = []
        for e in self.log_rows:
            ts = e["timestamp"].replace(" ", "T") + "Z"
            if (types and e["event_type"] not in types) or (lo and ts < lo) or (hi and ts > hi):
                continue
            out.append(e)
        return {"results": out, "next_cursor": None}

    def fields_written(self, u):
        return {f: v for kind, rec, f, v in [w for w in self.writes if w[0] == "field"] if rec == u}


class FakeRoll:
    """The county tax roll, as the stage's ctx.sources["county_tax"]: street -> an answer or an error."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, street, county):
        self.calls.append((street, county))
        a = self.answers.get(street)
        if isinstance(a, BaseException):
            raise a
        return copy.deepcopy(a)


# ---------------------------------------------------------------------- shared scaffolding
def _wipe() -> None:
    from . import store
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%' OR key LIKE 'slack:%'")


def _prime(crm, now) -> None:
    """The fake's log in the shared copy, synced up to `now`: the stage's own sync then reads only the
    last few hours (the sync itself is tested in phase 0)."""
    from . import store
    from .autopilot import activity
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_log")
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:log:%'")
    mark = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    for g in activity.GROUPS:
        store.kv_set("autopilot:log:%s:through" % g, mark)
    activity._store(list(crm.log_rows))
    crm.now = now


def _policy(stages: dict):
    from .autopilot import policy
    return policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25, "record_usd": 1.0},
                         "people": {"callers": ["Emerson"]}, "gate": {"read_fields": True}, "stages": stages}, EMPLOYEES)


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import encumbrance, policy, registry
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.SLACK_VOICE, getattr(config, "PLAN_POSTS", "digest"))
    config.DATA_DIR = Path(tmp) / "leadintake"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SLACK_VOICE, config.PLAN_POSTS = "plain", "digest"      # the free cleanup, no model, posts as routed
    store.init()
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    from .autopilot import taxwatch          # the county client both stages share calls taxwatch._fetch
    real_fetch = (encumbrance._fetch, taxwatch._fetch)
    encumbrance._fetch = taxwatch._fetch = _no_network
    del NETWORK[:]
    try:
        _alerts(check)
        _county(check)
        _encumbrances(check)
        _house(check)
    finally:
        encumbrance._fetch, taxwatch._fetch = real_fetch
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        config.DATA_DIR, config.DB_PATH, config.MODE, config.SLACK_VOICE, config.PLAN_POSTS = keep


# ---------------------------------------------------------------------- 1. the appointment alert
def _alerts(check) -> None:
    from .autopilot import appointments, sweep
    from .autopilot import leadroutes as routes
    from .selftest_leads import LeadCRM, _et, _iso, lead
    print("  appointment alerts")
    config.MODE = "live"
    stage = {"kind": "appointments", "owner": "sales", "mode": "live", "schedule": "every 30 min", "gate_allow": ["lead"],
             "comps_workflow": "", "check_column": False}
    pol = _policy({"a_appts": stage})

    def go(crm, when, plan=False):
        crm.now = when
        return sweep.run("a_appts", crm=crm, now=when, pol=pol, plan=plan)

    def appt(crm, u, start, holder="u-emerson", **kw):
        return crm.add_task(u, "Walkthrough", due=start, kind="appointment", source="app", created=T0 - timedelta(hours=1),
                            start_time=_iso(start), end_time=_iso(start + timedelta(hours=1)),
                            assigned_to_user=({"uuid": holder} if holder else None), **kw)

    _wipe()
    crm = LeadCRM([lead("w1", "101 Walk St", "Hot Lead")])
    a1 = appt(crm, "w1", _et(11, 13, 14, 0))
    _prime(crm, T0)
    want = "Walkthrough at 101 Walk St, Knoxville set for Fri Nov 13, 2pm. Emerson has it. Confirm call Thu Nov 12."
    p1 = go(crm, T0, plan=True)
    check(p1.alerts == [want] and not crm.wrote(),
          "a booked walkthrough is one alert with the street and city, the day and time, who has it and the confirm "
          "day, raised in plan mode with nothing written")
    check(any(ln.startswith("Walkthrough booked: 101 Walk St") for ln in p1.lines),
          "the stage's own line about the booking is still there")
    first = sweep.posts({"id": 11, "workflow": "a_appts", "source": "schedule"}, p1.as_dict())
    check([p["kind"] for p in first] == ["pipeline"] and "101 Walk St, Knoxville" in first[0]["text"]
          and "Fri Nov 13, 2pm" in first[0]["text"] and "Thu Nov 12" in first[0]["text"],
          "the alert is posted to the pipeline channel")
    p2 = go(crm, T0 + timedelta(minutes=30), plan=True)
    check(p2.alerts == [want] and sweep.posts({"id": 12, "workflow": "a_appts", "source": "schedule"}, p2.as_dict()) == [],
          "read again on the next run, the appointment raises the same text, which is not posted a second time")
    l1 = go(crm, T0 + timedelta(hours=1))
    confirm = [t for t in crm.open_tasks("w1") if t["title"].startswith("Confirm the walkthrough")]
    due = routes.parse_dt(confirm[0]["due_date"]) if confirm else None
    check(l1.alerts == [want] and len(confirm) == 1 and due is not None and due.astimezone(ET).date() == date(2026, 11, 12),
          "the day the alert names is the day the confirm task is dated")
    moved = _et(11, 17, 10, 0)
    crm.tasks[a1].update(start_time=_iso(moved), end_time=_iso(moved + timedelta(hours=1)))
    l2 = go(crm, T0 + timedelta(hours=2))
    mv = "Walkthrough at 101 Walk St, Knoxville moved to Tue Nov 17, 10am (was Fri Nov 13, 2pm). Emerson has it. Confirm call Mon Nov 16."
    check(l2.alerts == [mv], "a moved appointment gets a new alert that says it moved, and the old time is not raised again")
    posted = sweep.posts({"id": 13, "workflow": "a_appts", "source": "schedule"}, l2.as_dict())
    check(any("moved to Tue Nov 17, 10am" in p["text"] and p["kind"] == "pipeline" for p in posted),
          "and the move is posted to the pipeline channel")

    _wipe()
    crm2 = LeadCRM([lead("w2", "102 Today St", "Hot Lead"), lead("w3", "103 Nobody St", "Hot Lead", assigned_to=None),
                    lead("w4", "104 Ghost St", "Hot Lead"), lead("w5", "105 Stop St", "Hot Lead"),
                    lead("w6", "106 Walk St (865) 000-0106", "Hot Lead"), lead("w7", "107 Past St", "Hot Lead"),
                    lead("w8", "108 Half St", "Hot Lead")])
    crm2.recs["w5"]["tags"].append("Do Not Market")
    soon = _et(11, 14, 9, 0)
    appt(crm2, "w2", _et(11, 11, 17, 30))
    appt(crm2, "w3", soon, holder="")
    appt(crm2, "w4", soon, holder="u-ghost")
    appt(crm2, "w5", soon)
    appt(crm2, "w6", soon)
    appt(crm2, "w7", _et(11, 9, 14, 0), outcome_status="attended", outcome_result="offer_made")
    appt(crm2, "w8", _et(11, 16, 14, 30))
    _prime(crm2, T0)
    s = go(crm2, T0, plan=True)
    said = {a.split(" set for")[0].split(" moved to")[0].replace("Walkthrough at ", ""): a for a in s.alerts}
    check(said.get("102 Today St, Knoxville") == "Walkthrough at 102 Today St, Knoxville set for Wed Nov 11, 5:30pm. "
                                                 "Emerson has it. Confirm call the same day.",
          "a walkthrough set for today says the minutes and that the confirm call is the same day")
    check(said.get("103 Nobody St, Knoxville", "").endswith("Nobody is assigned to it yet. Confirm call Fri Nov 13."),
          "an appointment nobody holds, on a record nobody is assigned, says so")
    check("Whoever has it is not on the team list." in said.get("104 Ghost St, Knoxville", ""),
          "a holder the team list does not name is said plainly, never as a raw id")
    check(said.get("105 Stop St, Knoxville", "").endswith("Emerson has it. No confirm call task: tagged Do Not Market."),
          "a record the gate holds is still announced, with why it gets no confirm task")
    w6 = said.get("106 Walk St, Knoxville", "")
    check(w6.startswith("Walkthrough at 106 Walk St, Knoxville set for Sat Nov 14, 9am.")
          and not appointments.PHONE_RX.search(w6) and "0106" not in w6.replace("106 Walk", ""),
          "a phone number in the street field never reaches an alert")
    check(not any("107 Past St" in a for a in s.alerts), "an appointment already past, with its outcome, is no alert")
    check(said.get("108 Half St, Knoxville", "").startswith("Walkthrough at 108 Half St, Knoxville set for Mon Nov 16, 2:30pm."),
          "a half-hour start keeps its minutes")
    check(len(s.alerts) == 6 and not crm2.wrote(), "one alert per appointment ahead, and nothing written in plan mode")
    check(appointments.when_short(_et(11, 18, 0, 5), "America/New_York") == "Wed Nov 18, 12:05am"
          and appointments.confirm_day(_et(11, 18, 9), _et(11, 17, 20), "America/New_York") == date(2026, 11, 17)
          and appointments.confirm_day(_et(11, 18, 9), _et(11, 18, 7), "America/New_York") == date(2026, 11, 18),
          "midnight reads 12am, and the confirm day is the day before or, at the latest, the day itself")
    check(appointments.scrub_phones("101 Walk St 8650000101, Knoxville") == "101 Walk St, Knoxville"
          and appointments.scrub_phones("12345 Example Pike, Knoxville") == "12345 Example Pike, Knoxville",
          "the phone cut takes a ten-digit number and leaves a five-digit house number alone")
    config.MODE = "dry"


# ---------------------------------------------------------------------- 2. the county roll client
def _county(check) -> None:
    from .autopilot import encumbrance as en
    print("  the county tax roll")
    knox, blount = en.COUNTY_ROLLS["knox"], en.COUNTY_ROLLS["blount"]

    def parcels(base, q):
        return "%s/parcels/%s?detail_level=public&start=0&length=10" % (base, urllib.parse.quote(q))

    def bills(base, account):
        return "%s/due/PPT/%s?detail_level=public" % (base, urllib.parse.quote(account))

    pages = {
        parcels(knox, "101 Walk St"): {"parcels": [
            {"parcel_address": "103 WALK ST", "account_number": "048DE019", "amount_due": 999.0},
            {"parcel_address": "101 WALK ST", "account_number": "048DE017", "amount_due": 3000.0}]},
        bills(knox, "048DE-017"): {"due": {"PPT": {"bills": [
            {"taxYear": 2023, "delinquent": True, "paid": False, "due": 600.0},
            {"taxYear": 2024, "delinquent": True, "paid": False, "due": 634.56},
            {"taxYear": 2025, "delinquent": True, "paid": True, "due": 0},
            {"taxYear": 2026, "delinquent": False, "paid": False, "due": 1765.44}]}}},
        parcels(blount, "202 OAK AVE"): {"parcels": [{"parcel_address": "202 OAK AVE", "account_number": "12345",
                                                      "amount_due": 1765.44}]},
        bills(blount, "12345"): {"due": {"PPT": {"bills": [{"taxYear": 2026, "delinquent": False, "paid": False,
                                                            "due": 1765.44}]}}},
        parcels(knox, "909 Ash Way"): {"parcels": [{"parcel_address": "909 ASH WAY", "account_number": "059AB012"}]},
        parcels(knox, "111 Other St"): {"parcels": [{"parcel_address": "113 OTHER ST", "account_number": "070CD001"}]},
    }
    asked: list = []

    def fetch(url):
        asked.append(url)
        return copy.deepcopy(pages.get(url))

    try:
        got = en.county_tax("101 Walk St", "Knox", fetch)
    except en.Unreadable:
        got = None                       # only when the wrong house's account was asked for its bills
    check(got == {"amount": 1234.56, "years": ["2023", "2024"], "account": "048DE-017", "address": "101 WALK ST"},
          "only bills both delinquent and unpaid count: the parcel row's amount due and the current year's unpaid bill do not")
    check(bills(knox, "048DE019") not in asked and bills(knox, "048DE-017") in asked,
          "the first search result, another house, is passed over, and a Knox account is asked with its dash")
    del asked[:]
    b = en.county_tax("202 Oak Avenue", "Blount County", fetch)
    check(b and b["amount"] == 0 and b["years"] == [] and b["account"] == "12345"
          and asked[:2] == [parcels(blount, "202 Oak Avenue"), parcels(blount, "202 OAK AVE")],
          "the street is asked as written, then the way the county writes it, and a Blount account is passed as it is")
    try:
        en.county_tax("909 Ash Way", "Knox", fetch)
        unreadable = False
    except en.Unreadable:
        unreadable = True
    check(unreadable, "a parcel whose bills cannot be read is Unreadable, never a zero")
    check(en.county_tax("111 Other St", "Knox", fetch) is None, "a roll with no parcel at that house answers nothing")
    del asked[:]
    try:
        en.county_tax("1 Hill Rd", "Sevier", fetch)
        no_roll = False
    except en.NoRoll:
        no_roll = True
    check(no_roll and not asked, "a county with no roll read here is said so, and nothing is asked")
    check(en.county_tax("Walk St", "Knox", fetch) is None and not asked, "a street with no house number is not looked up")
    check(en.delinquent([{"taxYear": 2024, "delinquent": True, "due": 50}, {"taxYear": 2023, "delinquent": True, "paid": False,
                                                                            "due": "12.50"}]) == {"amount": 12.5, "years": ["2023"]},
          "a bill that does not say it is unpaid is not counted, and a due amount written as text still counts")


# ---------------------------------------------------------------------- 3. the encumbrances
def _encumbrances(check) -> None:
    from .autopilot import encumbrance as en
    from .autopilot import ledger, sweep, undo
    print("  encumbrances on a new lead")
    stage = {"kind": "encumbrance", "owner": "sales", "mode": "live", "schedule": "every 30 min 08:00-19:00 Mon-Sat",
             "gate_allow": ["lead"]}
    pol = _policy({"e_lead": stage, "e_cap": dict(stage, max_records=1)})
    h = timedelta(hours=1)
    recs = [
        _home("e1", "101 Walk St", "Knox", "new_lead", lists=["Foreclosure"], lien_type="LEN", lien_recording_date="2026-08-25",
              open_mortgages=1, mortgage_type="Conventional", estimate_value="150000.00", equity_percent="36.00"),
        _home("e2", "202 Oak Ave", "Blount", "Warm Lead", lists=["Condemned"], open_mortgages=0),
        _home("e3", "303 Pine Rd", "Knox", "not_interested"),
        _home("e4", "404 Elm St", "Knox", "new_lead", owner={"uuid": "o-e4", "first_name": None, "last_name": None,
                                                              "company": "Example Holdings LLC", "phones": []}),
        _home("e5", "505 Birch Ln", "Knox", "new_lead", tags=["Do Not Market"]),
        _home("e6", "606 Cedar Ct", "Sevier", "Hot Lead", lien_type="HOA lien"),
        _home("e7", "707 Maple Dr", "Knox", "new_lead"),
        _home("e8", "808 Spruce St", "Knox", "Warm Lead"),
        _home("e9", "909 Ash Way", "Knox", "new_lead"),
        _home("e10", "1010 Old Rd", "Knox", "new_lead"),
        _home("e11", "1111 Gone St", "Knox", "new_lead"),
    ]
    custom = {"e1": {"Tax delinquency amount": 858.0, "Outstanding Lien Amount": 12908.72, "Liens Active vs Total": "3 of 8",
                     "Total Delinquency": 13766.72},
              "e2": {"Months Behind": "2"}}
    log = [status_ev(1, "e1", NOW - 2 * h, "prospecting", "new_lead"),
           status_ev(2, "e2", NOW - 3 * h, "", "Warm Lead"),
           status_ev(3, "e3", NOW - 5 * h, "prospecting", "new_lead"),
           status_ev(4, "e3", NOW - 4 * h, "new_lead", "not_interested"),
           status_ev(5, "e4", NOW - 6 * h, "prospecting", "new_lead"),
           status_ev(6, "e5", NOW - 7 * h, "prospecting", "new_lead"),
           status_ev(7, "e6", NOW - 8 * h, "prospecting", "Hot Lead"),
           status_ev(8, "e7", NOW - timedelta(minutes=30), "prospecting", "new_lead"),
           status_ev(9, "e8", NOW - h, "Cold Lead", "Warm Lead"),
           status_ev(10, "e9", NOW - 10 * h, "prospecting", "new_lead"),
           status_ev(11, "e10", NOW - timedelta(days=20), "prospecting", "new_lead"),
           status_ev(12, "e11", NOW - 11 * h, "prospecting", "new_lead")]
    roll = FakeRoll({"101 Walk St": {"amount": 1234.56, "years": ["2023", "2024"], "account": "048DE-017"},
                     "202 Oak Ave": {"amount": 0.0, "years": [], "account": "12345"},
                     "707 Maple Dr": OSError("timed out"),
                     "909 Ash Way": en.Unreadable("the county's bills for that account could not be read"),
                     "1111 Gone St": None})
    _wipe()
    crm = EncCRM(recs, custom, WITH_INTAKE, log)

    def go(sid, when, plan=False):
        return sweep.run(sid, crm=crm, now=when, pol=pol, plan=plan, sources={"county_tax": roll})

    config.MODE = "live"
    p1 = go("e_lead", NOW, plan=True)
    read = [r[1] for r in crm.reads if r[0] == "record"]
    check(not crm.writes and p1.head == "Would fill the encumbrances on 5 of 9 new lead(s). 1 left for the next run.",
          "a plan run checks every record that became a lead, says what it would fill, and writes nothing")
    check("e8" not in read and "e10" not in read,
          "a move from one lead status to another is not a new lead, and the first run looks back two weeks only")
    check(any(ln == "101 Walk St, Knoxville: back taxes $1,234.56 for 2023 and 2024; liens $12,908.72 outstanding, "
                    "3 of 8 active; mortgage about $96,000 owed" for ln in p1.lines),
          "each new lead gets one line: back taxes, liens and the mortgage")
    check(p1.tally.get("no longer a lead when it was checked") == 1 and p1.tally.get("owned by a company or a trust (people only)") == 1
          and p1.tally.get("held back: tagged Do Not Market") == 1,
          "a record that left the lead statuses, one a company owns and one tagged Do Not Market are not filled")
    check(any("not in DataSift yet" in p and p.endswith("these went into the note only: HOA, Code Liens. Create them as text "
                                                          "fields and the next new lead fills them") for p in p1.problems),
          "the fields the account does not have are listed once, as a problem")
    check(any("909 Ash Way" in p and "could not be read" in p for p in p1.problems)
          and any("county tax site did not answer" in p for p in p1.problems),
          "an unreadable account and a county site that does not answer are both problem lines")
    p2 = go("e_lead", NOW + timedelta(minutes=30), plan=True)
    read2 = [r[1] for r in crm.reads if r[0] == "record"]
    check(read2.count("e1") == 1 and read2.count("e7") == 2 and not crm.writes,
          "a plan run checks each new lead once; only the one waiting on the county is read again")
    check(p2.head.startswith("Would fill the encumbrances on 0 of 1 new lead(s)"), "and says so")

    l1 = go("e_lead", NOW + timedelta(minutes=35))
    f1, f2 = crm.fields_written("e1"), crm.fields_written("e2")
    check(f1.get("f-tax") == "1234.56" and f2.get("f-tax") == "0",
          "live, the county's delinquent figure goes into Tax delinquency amount, and a 0 says it was checked and nothing is owed")
    check(not any(w[0] == "field" and w[2] == "f-total" for w in crm.writes),
          "Total Delinquency is never written: it is liens plus taxes")
    check("f-mort" not in f1 and f2.get("f-mort") == "0",
          "an estimate never goes into a money field; free and clear is a zero of record")
    check(f1.get("f-months") == "Unknown" and "f-months" not in f2,
          "an empty intake field gets Unknown, and one a person already filled is kept")
    check(not crm.fields_written("e9").get("f-tax") and not crm.fields_written("e11").get("f-tax")
          and not crm.fields_written("e6").get("f-tax"),
          "an unreadable account, a street the roll does not have and a county with no roll leave the tax field alone")
    note = dict(crm.notes).get("e1", "")
    want = ("Encumbrances, filled when this became a lead (Nov 16, 2026):\n"
            "Back taxes: $1,234.56 delinquent county tax for 2023 and 2024. Source: Knox County Trustee bills (county tax "
            "only; a city bill is separate), Nov 16, 2026.\n"
            "Liens: $12,908.72 outstanding, 3 of 8 active, type LEN, recorded Aug 25, 2026. Source: our lien records, as "
            "written on the record, Nov 16, 2026.\n"
            "Mortgage: 1 open mortgage (Conventional), about $96,000 owed. Source: the record's $150,000 value estimate and "
            "36% equity (an estimate, not a payoff), Nov 16, 2026.\n"
            "Months behind: Unknown. Ask the seller; a foreclosure notice is on the record, so the loan is behind.\n"
            "HOA: Unknown. Not checked.\n"
            "Code liens: Unknown. Confirmed at title.")
    check(note == want, "the note lists each item in one fixed format, with its source and date, Unknown where not checked")
    n2, n6, n9, n11 = (dict(crm.notes).get(u, "") for u in ("e2", "e6", "e9", "e11"))
    check("Back taxes: None delinquent. Source: Blount County Trustee bills" in n2
          and "Liens: Unknown. Our lien records cover Knox County only." in n2
          and "Mortgage: No open mortgage of record (free and clear). Source: the record (0 open mortgages)" in n2
          and "Code liens: Unknown. A code action is on the record (Condemned); a lien, if any, is confirmed at title." in n2,
          "a Blount lead: nothing delinquent, no lien records there, free and clear, and its code action named")
    check("Back taxes: Unknown. No county tax roll is read for Sevier County." in n6
          and "HOA: An HOA lien is on our lien records." in n6,
          "a county with no roll is Unknown with the reason, and an HOA lien on the record is named")
    check("Back taxes: Unknown. The Knox County Trustee's bills could not be read" in n9
          and "Back taxes: Unknown. The Knox County Trustee's roll has no parcel at 1111 Gone St." in n11
          and "Liens: None on our lien records." in n11,
          "an unreadable account and a missing parcel say why in the note")
    check(not any(w[1] in ("e3", "e4", "e5", "e7", "e8", "e10") for w in crm.writes),
          "nothing is written on a record that left the lead statuses, a company's, a Do Not Market one or one still waiting")
    check(dict(crm.notes).keys() == {"e1", "e2", "e6", "e9", "e11"} and l1.acted == 5,
          "the first live run fills what the plan runs said it would, from its own memory")
    ch = [c for c in ledger.changes(record_uuid="e1") if c["what"] == "custom_field"]
    tax_ch = [c for c in ch if (c["after"] or {}).get("field_uuid") == "f-tax"]
    steps = undo.plan(tax_ch[0]["run_id"]) if tax_ch else []
    check(tax_ch and tax_ch[0]["before"] == {"field_uuid": "f-tax", "value": 858.0}
          and any(s["do"] == "custom_field" and s["args"] == {"field_uuid": "f-tax", "value": 858.0} for s in steps),
          "every field write keeps the value it replaced, and undo puts 858 back")
    check(len([e for e in ledger.events(action="encumbrance")]) == 5, "each filled lead is in the ledger")
    reads_fields = [r[1] for r in crm.reads if r[0] == "fields"]
    check(reads_fields.count("e1") == 2, "the gate reads the custom fields the stage just read, not a second copy")

    print("  waiting on the county, and once per lead")
    l2 = go("e_lead", NOW + timedelta(minutes=65))
    memo7 = ledger.fact("e7", en.MEMO) or {}
    check(not any(w[1] == "e7" for w in crm.writes) and memo7.get("tries") == 2 and memo7.get("done") is False,
          "a lead whose county site did not answer waits for the next run, counting its tries")
    l3 = go("e_lead", NOW + timedelta(minutes=95))
    n7 = dict(crm.notes).get("e7", "")
    check("Back taxes: Unknown. The county tax site did not answer after 3 tries." in n7
          and any("gave up on the county tax for 707 Maple Dr, Knoxville after 3 tries" in p for p in l3.problems)
          and not crm.fields_written("e7").get("f-tax") and [c[0] for c in roll.calls].count("707 Maple Dr") == 5,
          "after three tries the note says Unknown, the field stays blank, and a problem says so")
    before = len(crm.writes)
    l4 = go("e_lead", NOW + timedelta(minutes=125))
    check(len(crm.writes) == before and l4.head == "No record became a lead since the last check." and not l4.acted,
          "a lead entry is filled once: the next run writes nothing")
    crm.log_rows += [status_ev(20, "e1", NOW + timedelta(minutes=130), "new_lead", "not_interested"),
                     status_ev(21, "e1", NOW + timedelta(minutes=140), "not_interested", "new_lead")]
    before = len(crm.writes)
    l5 = go("e_lead", NOW + timedelta(minutes=155))
    again = crm.writes[before:]
    check(again == [("note", "e1")] and l5.tally.get("fields that already read right") == 1
          and l5.tally.get("fields a person already filled (kept)") == 1,
          "a record that left the lead statuses and came back is checked again; a field that already reads right is not rewritten")
    check(l2.acted == 0, "a run with only a waiting lead fills nothing")

    print("  caps, the county going down, and the account as it is today")
    _wipe()
    crm_c = EncCRM([_home("c1", "11 Cap St", "Knox", "new_lead"), _home("c2", "12 Cap St", "Knox", "new_lead")], {},
                   ACCOUNT_FIELDS, [status_ev(30, "c1", NOW - 2 * h, "prospecting", "new_lead"),
                                    status_ev(31, "c2", NOW - h, "prospecting", "new_lead")])
    roll_c = FakeRoll({"11 Cap St": None, "12 Cap St": None})
    c1 = sweep.run("e_cap", crm=crm_c, now=NOW, pol=pol, sources={"county_tax": roll_c})
    check(dict(crm_c.notes).keys() == {"c1"} and c1.head.endswith("1 left for the next run."),
          "no more new leads are read in a run than max_records allows, oldest first")
    sweep.run("e_cap", crm=crm_c, now=NOW + timedelta(minutes=30), pol=pol, sources={"county_tax": roll_c})
    check(dict(crm_c.notes).keys() == {"c1", "c2"} and [r[1] for r in crm_c.reads if r[0] == "record"].count("c1") == 1,
          "the next run picks up the one the cap left, and the mark never skips it")
    check(any(p.endswith("these went into the note only: Mortgage Balance, Months Behind, HOA, Code Liens. Create them as "
                         "text fields and the next new lead fills them") for p in c1.problems),
          "on the account as it is today, all four intake fields are named as missing")
    _wipe()
    crm_d = EncCRM([_home("d1", "21 Down St", "Knox", "new_lead"), _home("d2", "22 Down St", "Blount", "new_lead")], {},
                   ACCOUNT_FIELDS, [status_ev(40, "d1", NOW - 2 * h, "prospecting", "new_lead"),
                                    status_ev(41, "d2", NOW - h, "prospecting", "new_lead")])
    roll_d = FakeRoll({"21 Down St": OSError("timed out"), "22 Down St": {"amount": 0.0, "years": []}})
    d1 = sweep.run("e_lead", crm=crm_d, now=NOW, pol=pol, sources={"county_tax": roll_d})
    memo2 = ledger.fact("d2", en.MEMO) or {}
    check(roll_d.calls == [("21 Down St", "Knox")] and not crm_d.writes and not memo2.get("tries")
          and d1.tally.get("new leads waiting for the county tax site") == 2,
          "once the county site fails in a run, the rest wait without being asked and without spending a try")
    config.MODE = "dry"


# ---------------------------------------------------------------------- 4. the house rules
def _house(check) -> None:
    import yaml

    from .autopilot import encumbrance, policy, registry
    print("  the playbook block and the files")
    blocks = yaml.safe_load("stages:" + PLAYBOOK_BLOCK)["stages"]
    try:
        st = policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25}, "people": {"callers": ["Emerson"]},
                           "stages": blocks}, EMPLOYEES).stages
    except policy.PolicyError as exc:
        st = {}
        print("    " + str(exc))
    s = st.get("lead_encumbrances")
    check(s is not None and s.mode == "plan" and s.schedule and s.owner == "sales" and "lead" in (s.get("gate_allow") or [])
          and encumbrance.settings(s)["fields"]["tax"] == "Tax delinquency amount",
          "the playbook block parses, ships in plan mode with a schedule, and names leads in gate_allow")
    check(callable(registry.runner("encumbrance")) and REGISTRY_LINES["encumbrance"][2] == "free",
          "the registry line names a module and a run function that load, spend class free")
    bad = dict(blocks["lead_encumbrances"], lead_statuses="new_lead", fields={"tax": "Total Delinquency", "roof": "Roof Age"},
               max_records=0, overwrite=["roof"], note="yes")
    try:
        policy.parse({"business": BUSINESS, "caps": {"daily_usd": 25}, "people": {"callers": ["Emerson"]},
                      "stages": {"bad": bad}}, EMPLOYEES)
        msg = ""
    except policy.PolicyError as exc:
        msg = str(exc)
    check(all(w in msg for w in ("lead_statuses", "Total Delinquency", "'roof'", "max_records", "overwrite", "note is true")),
          "a block that names Total Delinquency, an unknown field, a zero cap or a bad switch is refused when the playbook loads")
    here = Path(__file__).resolve().parent
    mine = [here / "autopilot" / "encumbrance.py", here / "autopilot" / "appointments.py", Path(__file__).resolve()]
    text = "".join(f.read_text(encoding="utf-8") for f in mine)
    check(chr(8212) not in text and chr(8211) not in text and chr(8) not in text,
          "no dash character and no backspace byte in the lead intake files")
    check(not NETWORK, "nothing in this section reached a county roll")
