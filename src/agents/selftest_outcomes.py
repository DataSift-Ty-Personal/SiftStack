"""The monthly lost-deal audit and the neighborhood suppression suggestions (Ty, 2026-11-15). Zero
network: a fake CRM, a fake SiftMap, events seeded into the brain's log copy, its own database and
data folder. Every name, street and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config
from .selftest_autopilot import EMPLOYEES
from .selftest_phase0 import _ts, status_ev, tag_ev

NOW = datetime(2026, 11, 2, 9, 20, tzinfo=timezone.utc)          # Monday 4:20am Eastern, the night list
DASHES = (chr(0x2014), chr(0x2013))                               # the two dash characters, built so this file holds neither

# The lines for autopilot/registry.py EXTRA, exactly as handed over.
REGISTRY_LINES = {
    "lostdeals": ("lostdeals", "run_lostdeals", "free", False, True),
    "suppression": ("suppression", "run_suppression", "free", False, True),
}

# The stage blocks for team/autopilot.yaml, exactly as handed over (two spaces in, under `stages:`).
STAGE_BLOCKS = """
  lost_deal_audit:
    kind: lostdeals
    owner: analysis
    mode: "plan"                  # read only: it writes nothing to the account in any mode
    schedule: "1st 04:20"         # monthly, on the night list, after the cadence and research reports
    summary: Every house in our account that sold last month to an investor, who bought it and what we did or missed first (never worked, worked but never reached, reached but not followed up, a lead we lost), with the repeat buyers named as possible dispo buyers.
    # C10.37 and lesson 2-06 at 2:28. The investor test is the deal-odds label's own
    # (deal_odds_train.investor_entity). DataSift refreshes last_sold about two months after a
    # closing, so the months before the audited one are read again for late arrivals.
    months_back: 1                # the month audited: 1 is the calendar month before this one
    catch_up_months: 2
    max_records: 150              # sold houses opened a run (one record read each)
    siftmap_cap: 150              # SiftMap deed reads a run (the house is in the account)
    portfolio_min: 3              # a buyer SiftMap shows owning this many properties is an investor
    nominal_floor: 10000          # under this a deed is a family or no-price transfer
    resale_months: 24             # a seller who bought within this long is an investor reselling
    gap_lines: 3                  # the channel shows six lines: the biggest gaps, the repeat buyers, the file

  neighborhood_suppression:
    kind: suppression
    owner: analysis
    mode: "plan"                  # it suggests; a person applies the change file with preset_patch.py
    schedule: "1st 04:40"
    summary: Neighborhoods whose worked records keep going bad (dead leads, not interested, a bad-area tag) far more often than the account's own rate, proven by a count and written into a preset change file for you to approve; it never changes a preset.
    # C10.34. A neighborhood the presets already skip is not suggested again.
    window_days: 180
    bad_statuses: [Dead Lead, not_interested]
    bad_tags: [Bad Area]          # a caller tags a lead from a bad area; the tag does not exist yet
    min_bad: 5
    min_worked: 8
    min_lift: 0.15                # at least 15 points above the account's own bad share
    max_chance: 0.05              # and under a 1 in 20 chance at the account's rate
    read_cap: 400                 # worked records opened a run for their neighborhood, each once
    apply_to: excluding           # excluding: the presets that already skip neighborhoods; marketing: every call, mail and pre-call preset
"""

WRITES = ("assign", "add_tags", "remove_tags", "add_lists", "remove_lists", "set_status", "message", "set_custom_field",
          "update_owner", "upsert_phones", "skiptrace_scoped", "skiptrace_all", "call_attempts", "sms_attempts",
          "skiptrace_attempts", "directmail_attempts", "set_last_mailed", "mail_campaign")


class _Writes:
    """Every write the real client has, recorded, so a check can prove none was called."""

    def __init__(self):
        self.writes = []

    def __getattr__(self, name):
        if name in WRITES:
            return lambda *a, **k: self.writes.append((name, a))
        raise AttributeError(name)


# ====================================================================== the lost-deal audit's world
def _house(u, street, sold, created="2026-03-01", owner=None, lists=(), tags=(), status=None, counters=(0, 0, 0),
           assigned="", value=0, did=True, phones=()):
    o = {"uuid": "o-" + u, "first_name": "Pat", "last_name": "Quill" + u, "company": None, "phones": list(phones)}
    o.update(owner or {})
    return {"uuid": u, "last_sold": sold, "_created": created,
            "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": o, "status": status, "tags": list(tags), "lists": list(lists), "assigned_to": assigned,
            "estimate_value": value, "predictivecall_attempts": counters[0], "sms_attempts": counters[1],
            "directmail_attempts": counters[2], "dataflik_id": ("df-" + u) if did else None}


def _deed(day, price, buyer, seller="OLD OWNER", cash=None):
    return {"sale_date": day, "sale_price": price, "buyer_name": buyer, "seller_name": seller, "is_cash_sale": cash}


def _detail(rows, owner_name="", mail="", props=None):
    return {"sale_history": list(rows), "owner_info": {"owner_name": owner_name, "owner_mail_address": mail,
                                                       "total_properties": props}}


class LostCRM(_Writes):
    def __init__(self, recs, budget_after=None):
        super().__init__()
        self.recs = recs
        self.reads, self.searches = [], []
        self.budget_after = budget_after

    def rows(self, must, cap=10000):
        self.searches.append(copy.deepcopy(must))
        lo, hi = must["last_sold"]
        c_lo, c_hi = must["created"]
        out = []
        for u, r in self.recs.items():
            if lo <= (r.get("last_sold") or "") < hi and c_lo <= (r.get("_created") or "") < c_hi:
                out.append({"uuid": u, "owner": dict(r["owner"]), "status": r.get("status"),
                            "list_count": len(r.get("lists") or [])})
        return out[:cap]

    def record(self, u):
        if self.budget_after is not None and len(self.reads) >= self.budget_after:
            from .autopilot.budget import BudgetExhausted
            raise BudgetExhausted("the hour's share of the request limit is used (test)")
        self.reads.append(u)
        r = copy.deepcopy(self.recs[u])
        r.pop("_created", None)
        return r

    def users(self):
        return [{"uuid": "u1", "first_name": "Emerson"}]


class FakeSiftMap:
    def __init__(self, details):
        self.details, self.reads = details, []

    def get_detail(self, did):
        self.reads.append(did)
        return copy.deepcopy(self.details.get(did) or {})


def _ev(i, kind, owner, when, duration=0, inbound=False):
    """One call or text in the account log, the shape the published route answers."""
    if kind.startswith("owner.sms."):
        payload = {"sms": {"direction": "inbound" if inbound else "outbound", "origin_number": "8650000099",
                           "destination_number": "8650000001"}}
    else:
        payload = {"call": {"direction": "inbound" if inbound else "outbound", "origin_number": "8650000099",
                            "destination_number": "8650000001", "duration": duration, "source": "kixie",
                            "user": {"first_name": "Emerson", "uuid": "u1"}}}
    return {"id": "ld-%s" % i, "event_type": kind, "resource": owner, "resource_type": "owner", "source": "kixie",
            "author": "system", "timestamp": _ts(when), "payload": payload}


def _et(month, day, hour=11):
    return datetime(2026, month, day, hour, 0, tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


def lost_world():
    """Thirteen houses that sold, each a case the audit has to tell apart."""
    recs = {
        # a lead we lost: a Warm Lead with a conversation, bought by a company
        "l1": _house("l1", "101 Example St", "2026-10-14", lists=["Foreclosure"], tags=["FTM"], assigned="u1",
                     value=280000, counters=(6, 2, 0), owner={"first_name": "Pat", "last_name": "Quillon"}),
        # reached, never a lead: a cash buyer who lives elsewhere
        "r1": _house("r1", "102 Example St", "2026-10-09", lists=["Probate"], counters=(2, 0, 0),
                     owner={"first_name": "Robin", "last_name": "Vexley"}),
        # worked, never reached: three dials, a company buyer
        "w1": _house("w1", "103 Example St", "2026-10-20", counters=(3, 1, 0), owner={"first_name": "Casey", "last_name": "Brindle"}),
        # never worked: the same company under another spelling
        "n1": _house("n1", "104 Example St", "2026-10-03", owner={"first_name": "Jordan", "last_name": "Pellow"}),
        # a homebuyer who moved in
        "h1": _house("h1", "105 Example St", "2026-10-05"),
        # a no-price family transfer
        "f1": _house("f1", "106 Example St", "2026-10-07"),
        # a lender taking it back
        "x1": _house("x1", "107 Example St", "2026-10-08"),
        # an investor reselling a house it bought in June: the June purchase was the deal
        "s1": _house("s1", "108 Example St", "2026-10-12"),
        # the deed has not reached SiftMap
        "d1": _house("d1", "109 Example St", "2026-10-16"),
        # created AFTER its own sale: never ours to lose, never asked for
        "a1": _house("a1", "110 Example St", "2026-10-15", created="2026-10-20"),
        # a September sale DataSift recorded late
        "late1": _house("late1", "111 Example St", "2026-09-15"),
        # a company owned it (an entity seller still teaches something)
        "e1": _house("e1", "112 Example St", "2026-10-22", owner={"first_name": None, "last_name": None,
                                                                  "company": "Example Rentals LLC"}),
        # the record now names the buyer (an owner swap after the sale); texted twice before
        "o1": _house("o1", "113 Example St", "2026-10-25", counters=(0, 2, 0),
                     owner={"first_name": "Casey", "last_name": "Tramwell"}),
    }
    details = {
        "df-l1": _detail([_deed("2026-10-14", 158000, "EXAMPLE HOMES LLC", "QUILLON PAT", True)], "EXAMPLE HOMES LLC",
                         "500 Office Pkwy, Knoxville TN 37902", 12),
        "df-r1": _detail([_deed("2026-10-09", 120000, "MORGAN DRAYE", "VEXLEY ROBIN", True)], "MORGAN DRAYE",
                         "77 Other Rd, Knoxville TN 37919", 1),
        "df-w1": _detail([_deed("2026-10-20", 95000, "SAMPLE CAPITAL LLC", "BRINDLE CASEY")], "SAMPLE CAPITAL LLC", "", 4),
        "df-n1": _detail([_deed("2026-10-03", 88000, "Sample Capital, L.L.C.", "PELLOW JORDAN")], "SAMPLE CAPITAL LLC", "", 4),
        "df-h1": _detail([_deed("2026-10-05", 240000, "JORDAN HOMEBUYER", "QUILLH1 PAT", False)], "JORDAN HOMEBUYER",
                         "105 EXAMPLE ST, KNOXVILLE TN 37917", 1),
        "df-f1": _detail([_deed("2026-10-07", 5000, "QUILLF1 ROBIN", "QUILLF1 PAT")]),
        "df-x1": _detail([_deed("2026-10-08", 150000, "FIRST EXAMPLE BANK", "QUILLX1 PAT")]),
        "df-s1": _detail([_deed("2026-10-12", 310000, "AVERY RETAIL", "FLIP EXAMPLE LLC", False),
                          _deed("2026-06-01", 90000, "FLIP EXAMPLE LLC", "QUILLS1 PAT", True)], "AVERY RETAIL",
                         "108 EXAMPLE ST, KNOXVILLE TN 37917", 1),
        "df-d1": _detail([]),
        "df-a1": _detail([_deed("2026-10-15", 99000, "SHOULD NOT BE READ LLC")]),
        "df-late1": _detail([_deed("2026-09-15", 99000, "LATE BUYER LLC", "QUILLLATE1 PAT")]),
        "df-e1": _detail([_deed("2026-10-22", 130000, "Example Homes LLC", "EXAMPLE RENTALS LLC")], "EXAMPLE HOMES LLC", "", 12),
        "df-o1": _detail([_deed("2026-10-25", 101000, "TRAMWELL CASEY", "OLDER OWNER", True)], "CASEY TRAMWELL",
                         "9 Elsewhere Ln, Knoxville TN 37920", 1),
    }
    from .autopilot import activity
    activity._store([
        status_ev("l1a", "l1", _et(9, 10, 10), "prospecting", "Warm Lead", source=""),
        _ev("l1d", "owner.call.made", "o-l1", _et(9, 10, 10)),
        _ev("l1c", "owner.call.answered", "o-l1", _et(9, 10, 10), duration=180),
        _ev("r1d", "owner.call.made", "o-r1", _et(9, 20)),
        _ev("r1c", "owner.call.answered", "o-r1", _et(9, 20), duration=90),
        _ev("w1a", "owner.call.made", "o-w1", _et(9, 25)),
        _ev("w1b", "owner.call.made", "o-w1", _et(9, 26)),
        _ev("w1c", "owner.call.made", "o-w1", _et(9, 27)),
    ])
    return recs, details


def lost_playbook(**over):
    blocks = yaml.safe_load("stages:" + STAGE_BLOCKS)["stages"]
    stage = dict(blocks["lost_deal_audit"], sync=False)
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
            "stages": {"lost_deal_audit": stage}}


# ====================================================================== the suppression check's world
class SuppCRM(_Writes):
    def __init__(self, hoods, account, presets):
        super().__init__()
        self.hoods = hoods                       # record -> (neighborhood, value)
        self.account = account                   # the search's records: [{"hood", "phone"}]
        self.preset_rows = presets               # title -> {"uuid", "folder", "must"}
        self.reads, self.counts, self.calls = [], [], []

    def record(self, u):
        self.reads.append(u)
        hood, value = self.hoods[u][:2]
        lists = self.hoods[u][2] if len(self.hoods[u]) > 2 else []
        return {"uuid": u, "neighborhood": hood, "estimate_value": value, "lists": [{"title": t} for t in lists]}

    def count(self, must):
        self.counts.append(copy.deepcopy(must))
        out = 0
        for r in self.account:
            if must.get("phone") and not r["phone"]:
                continue
            if r["hood"] in ((must.get("must_not") or {}).get("any_neighborhood") or []):
                continue
            out += 1
        return out

    def presets(self):
        return {t: {"uuid": p["uuid"], "folder": p["folder"], "must": copy.deepcopy(p["must"])} for t, p in self.preset_rows.items()}

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path))
        for t, p in self.preset_rows.items():
            if path == "/api/internal/filter-preset/%s/" % p["uuid"]:
                if method == "PATCH":
                    self.writes.append(("preset", t))
                    p["must"] = copy.deepcopy((body or {}).get("filters", {}).get("must"))
                    return {}
                return {"uuid": p["uuid"], "title": t, "filters": {"must": copy.deepcopy(p["must"])}}
        if method != "GET":
            self.writes.append(("call", path))
        return {}


def supp_world():
    """Seven neighborhoods: one to suppress, one already suppressed, one that matches nothing in the
    search, one big and ordinary, one small, one close to the line, and records with no neighborhood."""
    from .autopilot import activity
    hoods, events, n = {}, [], [0]

    def add(prefix, hood, outcomes, value=150000):
        for i, to in enumerate(outcomes):
            u = "%s%02d" % (prefix, i)
            hoods[u] = (hood, value)
            n[0] += 1
            when = NOW - timedelta(days=10 + (n[0] % 60))
            if to == "tag":
                events.append(tag_ev("sp%d" % n[0], u, when, ["Bad Area"]))
                events[-1]["source"] = ""
            else:
                events.append(status_ev("sp%d" % n[0], u, when, "prospecting", to, source=""))

    add("eh", "Example Hills", ["not_interested"] * 9 + ["Dead Lead"] * 2 + ["tag"], value=410000)
    add("sp", "Sample Park", ["not_interested"] * 15 + ["new_lead"] * 25)
    add("ot", "Old Town", ["Dead Lead"] * 9)
    add("tc", "Tiny Court", ["not_interested"] * 3)
    add("pg", "Pine Grove", ["not_interested"] * 9 + ["new_lead"] * 3)
    add("gh", "Ghost Hollow", ["not_interested"] * 10)
    add("bl", "", ["not_interested"] * 2 + ["new_lead"] * 3)
    # A Pine Grove owner said no and later became a lead: the last outcome is the one that counts.
    events.append(status_ev("sp-first", "pg09", NOW - timedelta(days=150), "prospecting", "not_interested", source=""))
    # Automation is not a person's judgment: twenty Sample Park records a sequence marked Not Interested,
    # and one Example Hills record the autopilot's own key put back to Not Interested.
    for i in range(20):
        u = "sq%02d" % i
        hoods[u] = ("Sample Park", 150000)
        events.append(status_ev("sq%d" % i, u, NOW - timedelta(days=20), "prospecting", "not_interested", source="sequence"))
    hoods["ia00"] = ("Example Hills", 150000)
    events.append(status_ev("ia0", "ia00", NOW - timedelta(days=20), "sold", "not_interested", source="internal-api"))
    # Ten buyers on the dispo list said no to a deal blast: buyers, not seller leads.
    for i in range(10):
        u = "by%02d" % i
        hoods[u] = ("Buyer Row", 150000, ["Dispo - VIP Buyers"])
        events.append(status_ev("by%d" % i, u, NOW - timedelta(days=15), "prospecting", "not_interested", source=""))
    # A stop long ago, outside the 180 days.
    hoods["old00"] = ("Example Hills", 150000)
    events.append(status_ev("old0", "old00", NOW - timedelta(days=200), "prospecting", "not_interested", source=""))
    activity._store(events)
    account = ([{"hood": "Example Hills", "phone": True}] * 42 + [{"hood": "Example Hills", "phone": False}] * 3
               + [{"hood": "Old Town", "phone": True}] * 5 + [{"hood": "Sample Park", "phone": True}] * 30
               + [{"hood": "Pine Grove", "phone": True}] * 9)
    presets = {
        "FTM - 02 Ready to Call": {"uuid": "p-call", "folder": "05. FTM - CALL",
                                   "must": {"any_tags": ["t-ftm"], "must_not": {"any_neighborhood": ["Old Town"], "any_tags": ["t-dnm"]}}},
        "FTM - Mail 01 Handwritten Letter": {"uuid": "p-mail", "folder": "06. FTM - MAIL",
                                             "must": {"any_tags": ["t-ftm"], "must_not": {"any_neighborhood": ["Old Town"]}}},
        "Top 1000 - 02 Ready to Call": {"uuid": "p-top", "folder": "02. TOP 1000 - CALL", "must": {"any_tags": ["t-top"]}},
        "Dispo - 03 Ready to Call": {"uuid": "p-dispo", "folder": "21. Dispo Sequential Marketing", "must": {"any_lists": ["l-d"]}},
    }
    return hoods, account, presets


def supp_playbook(**over):
    blocks = yaml.safe_load("stages:" + STAGE_BLOCKS)["stages"]
    stage = dict(blocks["neighborhood_suppression"], sync=False)
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
            "stages": {"neighborhood_suppression": stage}}


# ====================================================================== the section
def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry, sweep
    keep = (config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE)
    config.DATA_DIR = Path(tmp) / "outcomes"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.RUNS_DIR = config.DATA_DIR / "runs"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    # The orchestrator adds one line per kind to registry.EXTRA. Until it has, the same lines stand in
    # for the length of this section, exactly as policy and sweep read them at import.
    added = [k for k in REGISTRY_LINES if k not in registry.EXTRA]
    saved = (policy.KINDS, policy.NO_PRESETS, sweep.REPORT_KINDS)
    for k in added:
        registry.EXTRA[k] = REGISTRY_LINES[k]
        policy.SPEND_BY_KIND[k] = REGISTRY_LINES[k][2]
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k in added if not REGISTRY_LINES[k][3])))
    sweep.REPORT_KINDS = set(sweep.REPORT_KINDS) | {k for k in added if REGISTRY_LINES[k][4]}
    try:
        _blocks(check)
        _lost_pure(check)
        _lost_runs(check)
        _lost_after_sale(check)
        _supp_pure(check)
        _supp_runs(check)
    finally:
        for k in added:
            registry.EXTRA.pop(k, None)
            policy.SPEND_BY_KIND.pop(k, None)
        policy.KINDS, policy.NO_PRESETS, sweep.REPORT_KINDS = saved
        config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE = keep


def _blocks(check) -> None:
    from .autopilot import policy, registry
    print("  the handed-over lines")
    check(registry.problems() == [], "both new kinds load from the registry lines handed over: %s" % (registry.problems() or "ok"))
    pol = policy.parse({"business": {"tz": "America/New_York"}, "caps": {}, "people": {},
                        "stages": yaml.safe_load("stages:" + STAGE_BLOCKS)["stages"]}, EMPLOYEES)
    check(set(pol.stages) == {"lost_deal_audit", "neighborhood_suppression"} and all(s.mode == "plan" for s in pol.stages.values()),
          "the two playbook blocks parse, and both ship in plan mode")
    for kind, bad, word in (("lostdeals", {"months_back": 0}, "months_back"), ("lostdeals", {"max_records": "lots"}, "max_records"),
                            ("suppression", {"apply_to": "everything"}, "apply_to"),
                            ("suppression", {"max_chance": 2}, "max_chance")):
        stage = {"kind": kind, "owner": "analysis", "mode": "plan", "schedule": "1st 04:20"}
        stage.update(bad)
        try:
            policy.parse({"business": {}, "caps": {}, "people": {}, "stages": {"x": stage}}, EMPLOYEES)
            refused = False
        except policy.PolicyError as exc:
            refused = word in str(exc)
        check(refused, "a %s block with a bad %s is refused when the playbook loads" % (kind, word))


# ---------------------------------------------------------------------- the lost-deal audit
def _lost_pure(check) -> None:
    from collections import Counter
    from .autopilot import lostdeals as L
    print("  lost deals: the rules")
    st = L.settings({})
    first, nxt, start = L.window(NOW, "America/New_York", 1, 2)
    check((first, nxt, start) == (date(2026, 10, 1), date(2026, 11, 1), date(2026, 8, 1)),
          "early November audits October, and looks again at August and September for late arrivals")
    side = L.deed_for(_detail([_deed("2026-10-10", 500, "SIDE PARTY"), _deed("2026-10-14", 158000, "EXAMPLE HOMES LLC")]),
                      "2026-10-11")
    check(side.get("buyer") == "EXAMPLE HOMES LLC",
          "a priced deed near the sale wins over a small side transfer a few days before it")
    check(L.deed_for(_detail([_deed("2026-03-01", 90000, "EARLY LLC")]), "2026-10-11") == {},
          "a deed months away from the sale is not this sale's deed")
    check(L.moved_in("PO BOX 12, Knoxville TN", "12 Example St") is None and L.moved_in("12 EXAMPLE ST, KNOXVILLE TN", "12 Example St")
          and L.moved_in("40 Other Rd, Knoxville TN", "12 Example St") is False,
          "a mailing address at the house means the buyer moved in, another street means not, a PO box cannot say")
    check(L.buyer_key("Example Homes, L.L.C.") == L.buyer_key("EXAMPLE HOMES LLC"),
          "one buyer under two spellings is one buyer")
    base = {"deed": "found", "buyer": "X", "buyer_key": "X", "dataflik_id": True}
    kinds = [L.verdict(dict(base, **kw), Counter(), st)[0] for kw in (
        {"company": True}, {"cash": True, "moved_in": False}, {"portfolio": 5}, {"cash": True, "moved_in": None},
        {"cash": False, "moved_in": True}, {"not_investor": True}, {"lender": True, "not_investor": True},
        {"family": True, "company": True}, {"resale": {"date": "2026-06-01", "price": 90000}, "company": True},
        {"deed": "none"})]
    check(kinds == ["investor", "investor", "investor", "unclear", "retail", "other", "lender", "family", "resale", "unknown"],
          "a company, a cash buyer who lives elsewhere and an owner of several houses are investors; a church, a lender, "
          "a family transfer, an investor's resale and a missing deed are not")
    check(L.verdict(dict(base, buyer_key="PAT EXAMPLE"), Counter({"PAT EXAMPLE": 2}), st)
          == ("investor", "bought 2 of our houses"), "a buyer seen on two of our houses is an investor")
    _, company, rx = L._investor_test()
    check(L.verdict(dict(base, buyer="EXAMPLE BAPTIST CHURCH", not_investor=bool(rx.search(L._upper("EXAMPLE BAPTIST CHURCH")))),
                    Counter(), st)[0] == "other", "a church buying a house is not an investor")
    old = {"date": "2026-10-12", "seller": "FLIP EXAMPLE LLC",
           "earlier": [{"date": "2024-01-01", "price": 90000, "buyer": "FLIP EXAMPLE LLC"}]}
    check(L.resale_of(old, 24) is None and L.resale_of(dict(old, earlier=[{"date": "2026-06-01", "price": 90000,
                                                                          "buyer": "Flip Example, LLC"}]), 24),
          "a seller is an investor reselling only when it bought the house within resale_months")
    check(L.verdict(dict(base, buyer="ROBIN QUILLZ1"), Counter(), st)[0] == "retail"
          and L.deed_for(_detail([_deed("2026-10-12", 4000, "ROBIN QUILLZ1", "PAT QUILLZ1")]), "2026-10-12")["price"] == 4000,
          "a person with no investor sign is a homebuyer, and the deed's own price is kept for the family test")
    line = L.gap_line({"street": "9 Sample Rd, Knoxville", "list": "", "sold": "2026-10-03", "buyer": "EXAMPLE HOMES LLC",
                       "price": 88000, "value": 0, "miss": "never", "born_sold": True}, "a company")
    check(line.startswith("Never worked: 9 Sample Rd") and "Held as a sold record" in line and DASHES[0] not in line,
          "a house held only as a sold record says so instead of reading as an untouched lead")


def _fresh_lost() -> None:
    from . import store
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_facts WHERE key LIKE 'lostdeal:%'")


def _lost_runs(check) -> None:
    from .autopilot import lostdeals as L
    from .autopilot import policy, sweep
    print("  lost deals: a run")
    recs, details = lost_world()
    crm, sm = LostCRM(recs), FakeSiftMap(details)
    pol = policy.parse(lost_playbook(), EMPLOYEES)
    out = sweep.run("lost_deal_audit", crm=crm, now=NOW, pol=pol, sources={"siftmap": sm})
    text = "\n".join([out.head] + out.lines)
    check(not crm.writes, "the audit writes nothing to the account")
    check(out.head == ("In October 2026, 10 houses in our account sold, 6 of them to investors: 1 a lead we lost, 1 reached "
                       "but not followed up, 2 worked but never reached and 2 never worked."),
          "the head says how many sold, how many to investors and the split by what we missed: %s" % out.head)
    check("a1" not in crm.reads and "df-a1" not in sm.reads,
          "a house whose record was created after its own sale is never asked for")
    oct14 = [m for m in crm.searches if m["last_sold"][0] == "2026-10-14"]
    check(oct14 and oct14[0]["created"] == [L.CREATED_FLOOR, "2026-10-14"] and oct14[0]["last_sold"] == ["2026-10-14", "2026-10-15"],
          "each sale day is asked with the record's created date held before that day")
    facts = {s["record"]: s for s in L.remembered()}
    rep = L._repeats(L.remembered())
    v = {u: L.verdict(s, rep, L.settings({}))[0] for u, s in facts.items()}
    check(v == {"l1": "investor", "r1": "investor", "w1": "investor", "n1": "investor", "h1": "retail", "f1": "family",
                "x1": "lender", "s1": "resale", "d1": "unknown", "late1": "investor", "e1": "investor", "o1": "investor"},
          "every house is told apart: %s" % v)
    misses = {u: facts[u]["miss"] for u in ("l1", "r1", "w1", "n1", "e1", "o1")}
    check(misses == {"l1": "lead_lost", "r1": "reached", "w1": "worked", "n1": "never", "e1": "never", "o1": "worked"},
          "a lead, a conversation, dials or texts, and nothing at all each read as their own miss: %s" % misses)
    check(facts["l1"]["ftm"] and facts["r1"]["ftm"] and not facts["w1"]["ftm"] and facts["l1"]["assigned_to"] == "Emerson"
          and facts["l1"]["status_before"] == "warm lead" and facts["l1"]["talk_day"] == "2026-09-10",
          "the audit keeps the lists, the status before the sale, the last conversation and who had the record")
    check(facts["e1"]["owner_company"] and facts["o1"]["owner_is_buyer"] and not facts["l1"]["owner_is_buyer"],
          "a company owner is marked, and a record that now names the buyer is marked")
    check("Lead lost: 101 Example St, Knoxville, Foreclosure list, Emerson. Warm lead; last conversation Sep 10, 34 days before "
          "it sold Oct 14 to EXAMPLE HOMES LLC (a company) for $158,000, 56% of its $280,000 value." in text,
          "the biggest gap is one line: the house, the list, who had it, the last conversation, who bought and for how much")
    check(0 <= text.find("Lead lost") < text.find("Reached, not followed up") < text.find("Worked, never reached")
          and "113 Example St" in out.lines[2] and "The record now names the buyer" in out.lines[2],
          "the closest misses come first, and a record that now names the buyer says so")
    check("Repeat buyers, possible dispo buyers:" in text and "Example Homes LLC 2 of our houses (2 in October)" in text
          and "SAMPLE CAPITAL LLC 2 of our houses" in text, "the repeat buyers are named, two spellings of one buyer joined")
    check(out.lines[3].startswith("Repeat buyers") and out.lines[4].startswith("Every sale is in lost_deals_2026-10.csv")
          and "3 more investor sales are there" in out.lines[4],
          "the channel's six lines hold the biggest gaps, the repeat buyers and the file")
    check("Late arrivals: 1 sale(s) from September" in text and "111 Example St" in text,
          "a sale from an earlier month that reached DataSift late is reported as a late arrival")
    check("First to market: 2 of the 6 investor sales" in text, "how many of the lost deals were courthouse records")
    check("1 an investor reselling (its purchase was the deal)" in text and "1 taken back by a lender" in text
          and "1 deed not in SiftMap yet" in text, "what was not lost to an investor is counted, each kind named")
    rows = list(csv.DictReader(open(Path(out.run_dir) / "files" / "lost_deals_2026-10.csv", encoding="utf-8")))
    check(len(rows) == 12 and {r["record"] for r in rows} == set(facts) and out.run_dir,
          "every judged sale in the window is in the run's CSV, served from the run folder")
    check(all(d not in x for d in DASHES for x in [out.head] + out.lines), "no dash characters in what is posted")

    print("  lost deals: memory and caps")
    crm2, sm2 = LostCRM(recs), FakeSiftMap(details)
    again = sweep.run("lost_deal_audit", crm=crm2, now=NOW, pol=pol, sources={"siftmap": sm2})
    check(not crm2.reads and not sm2.reads and again.head == out.head,
          "a second run reads no sold house again and reports the same month from memory")
    from . import store
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT value FROM ap_facts WHERE record_uuid='d1' AND key LIKE 'lostdeal:%'").fetchone()
        val = json.loads(row["value"])
        val["audited"] = "2026-10-20"
        c.execute("UPDATE ap_facts SET value=? WHERE record_uuid='d1' AND key LIKE 'lostdeal:%'", (json.dumps(val),))
    crm3, sm3 = LostCRM(recs), FakeSiftMap(details)
    sweep.run("lost_deal_audit", crm=crm3, now=NOW, pol=pol, sources={"siftmap": sm3})
    check(crm3.reads == ["d1"], "a sale whose deed was not on SiftMap is looked at again after recheck_days, and only it")
    _fresh_lost()
    crm4, sm4 = LostCRM(recs), FakeSiftMap(details)
    capped = sweep.run("lost_deal_audit", crm=crm4, now=NOW, pol=policy.parse(lost_playbook(max_records=3), EMPLOYEES),
                       sources={"siftmap": sm4})
    check(len(crm4.reads) == 3 and all(recs[u]["last_sold"] >= "2026-10-01" for u in crm4.reads)
          and "9 wait for the next run" in "\n".join(capped.lines),
          "no more records read than max_records allows, the audited month first, and the rest are said to wait")
    crm5, sm5 = LostCRM(recs), FakeSiftMap(details)
    sweep.run("lost_deal_audit", crm=crm5, now=NOW, pol=policy.parse(lost_playbook(max_records=3), EMPLOYEES),
              sources={"siftmap": sm5})
    check(len(crm5.reads) == 3 and not set(crm5.reads) & set(crm4.reads), "the next run reads the next houses, none twice")
    _fresh_lost()
    sm6 = FakeSiftMap(details)
    sweep.run("lost_deal_audit", crm=LostCRM(recs), now=NOW, pol=policy.parse(lost_playbook(siftmap_cap=2), EMPLOYEES),
              sources={"siftmap": sm6})
    check(len(sm6.reads) == 2, "no more SiftMap reads than siftmap_cap allows")
    _fresh_lost()
    crm7 = LostCRM(recs, budget_after=2)
    short = sweep.run("lost_deal_audit", crm=crm7, now=NOW, pol=pol, sources={"siftmap": FakeSiftMap(details)})
    check(any("stopped reading the sold houses" in p for p in short.problems) and short.head.startswith("In October 2026")
          and len(crm7.reads) == 2, "a spent request budget stops the reading and the report goes out on what was read")

    print("  lost deals: live mode and the channel")
    _fresh_lost()
    config.MODE = "live"
    crm8 = LostCRM(recs)
    live = sweep.run("lost_deal_audit", crm=crm8, now=NOW, pol=policy.parse(lost_playbook(mode="live"), EMPLOYEES),
                     sources={"siftmap": FakeSiftMap(details)})
    config.MODE = "dry"
    check(live.live and not crm8.writes and live.writes == 0, "even live, the audit makes no write")
    posts = sweep.posts({"id": 7, "workflow": "lost_deal_audit", "source": "schedule"}, out.as_dict())
    check(len(posts) == 1 and posts[0]["kind"] in ("chat", "pipeline"), "a scheduled audit posts its report once")


def _lost_after_sale(check) -> None:
    """What happened AFTER a house sold must not read as what we did before it."""
    from .autopilot import activity, lostdeals as L, policy, sweep
    print("  lost deals: after the sale is not before it")
    check(L.before_sale(5, [], [1, 2, 3], True) == 2 and L.before_sale(5, [], [1, 2, 3], False) == 5
          and L.before_sale(1, [1, 2], [1, 2, 3], True) == 2,
          "touches the log holds after the sale come off the counter, never below what the log shows before it")
    activity._store([
        status_ev("q1a", "q1", _et(6, 1), "prospecting", "sold", source="bulk"),        # held as Sold for months
        status_ev("q2a", "q2", _et(10, 9), "prospecting", "sold", source=""),           # marked Sold at the sale
        status_ev("q3a", "q3", _et(10, 25), "prospecting", "new_lead", source=""),      # a lead only after the sale
    ])
    sold = {"sold"}
    start = L.kpi.day_start("2026-10-12", "America/New_York")
    check(L.held_as_sold("q1", start, sold) and not L.held_as_sold("q2", start, sold) and not L.held_as_sold("q3", start, sold),
          "a record that sat in Sold for months was held as a sold record; a Sold mark at the sale is about the sale")
    end = start + timedelta(days=1)
    check(L.status_at_sale("q3", end, sold, "new_lead") == "prospecting" and L.status_at_sale("q9", end, sold, "warm lead") == "warm lead"
          and L.status_at_sale("q9", end, sold, "sold") == "",
          "the status on the sale day is what the first later change changed from, never a lead made afterwards")
    recs = {"z1": _house("z1", "201 Sample Rd", "2026-10-18", counters=(3, 0, 0)),
            "z2": _house("z2", "202 Sample Rd", "2026-10-19"),
            "z3": _house("z3", "203 Sample Rd", "2026-10-20", lists=["Dispo - VIP Buyers"])}
    details = {"df-z1": _detail([_deed("2026-10-18", 99000, "QUIET BUYER LLC", "QUILLZ1 PAT")]),
               "df-z2": _detail([_deed("2026-10-19", 99000, "QUIET BUYER LLC", "QUILLZ2 PAT")]),
               "df-z3": _detail([_deed("2026-10-20", 99000, "RETAIL FAMILY", "SAMPLE FLIPS LLC")])}
    activity._store([_ev("z1a", "owner.call.made", "o-z1", _et(10, 20)), _ev("z1b", "owner.call.made", "o-z1", _et(10, 21)),
                     _ev("z1c", "owner.call.made", "o-z1", _et(10, 22)),
                     status_ev("z2a", "z2", _et(10, 25), "prospecting", "new_lead", source=""),
                     _ev("z2b", "owner.call.made", "o-z2", _et(10, 25)),
                     _ev("z2c", "owner.call.answered", "o-z2", _et(10, 25), duration=300)])
    _fresh_lost()
    sm = FakeSiftMap(details)
    run1 = sweep.run("lost_deal_audit", crm=LostCRM(recs), now=NOW, pol=policy.parse(lost_playbook(), EMPLOYEES),
                     sources={"siftmap": sm})
    facts = {s["record"]: s for s in L.remembered()}
    crm_again = LostCRM(recs)
    sweep.run("lost_deal_audit", crm=crm_again, now=NOW, pol=policy.parse(lost_playbook(), EMPLOYEES),
              sources={"siftmap": FakeSiftMap(details)})
    check("z3" not in facts and "df-z3" not in sm.reads and run1.tally.get("sales left out: a buyer's own house") == 1
          and "z3" not in crm_again.reads,
          "a house on a buyer list is a buyer's own sale: left out, counted, and not opened again")
    check(facts["z1"]["miss"] == "never" and facts["z1"]["dials"] == 0,
          "a house dialed only after it quietly sold was never worked before the sale")
    check(facts["z2"]["miss"] == "never" and not facts["z2"]["ever_lead"] and not facts["z2"]["talk_day"],
          "a lead and a conversation with the new owner after the sale are not a lead we lost")


# ---------------------------------------------------------------------- the suppression suggestions
def _supp_pure(check) -> None:
    from .autopilot import suppression as S
    print("  suppression: the rules")
    check(abs(S.chance_at_least(10, 10, 0.5) - 0.5 ** 10) < 1e-12 and S.chance_at_least(0, 5, 0.3) == 1.0
          and S.chance_at_least(6, 5, 0.3) == 0.0, "the chance of k or more bad outcomes out of n is the binomial tail")
    big = S.chance_at_least(1000, 2000, 0.5)
    check(0.4 < big < 0.6, "a neighborhood of thousands of records cannot overflow the chance")
    check(S.odds_text(0.025) == "a 1 in 40 chance" and S.odds_text(0.0004) == "under a 1 in 1,000 chance"
          and S.odds_text(0.7) == "about an even chance", "a chance is said the way a person says it")
    check(S.flat("Dead Lead") == S.flat("dead_lead"), "two spellings of one status are one status")
    check(S.hood_of({"neighborhood": "  Example   Hills "}) == "Example Hills" and S.hood_of({"neighborhood": {"title": "Old Town"}}) == "Old Town",
          "a neighborhood is read whether it comes as text or as an object")


def _fresh_supp() -> None:
    from . import store
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_facts WHERE key='suppression_hood'")
        c.execute("DELETE FROM ap_log")
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _supp_runs(check) -> None:
    from .autopilot import preset_patch as pp
    from .autopilot import policy, suppression as S, sweep
    print("  suppression: a run")
    _fresh_supp()
    hoods, account, presets = supp_world()
    st = S.settings({})
    res = S.outcomes(st, NOW - timedelta(days=180), NOW)
    check(not [u for u in res if u.startswith("sq")] and "ia00" not in res and "old00" not in res,
          "a status set by a sequence or by the autopilot's own key, and anything older than the window, are not counted")
    check(res["pg09"]["bad"] is False and res["eh11"]["bad"] and res["eh11"]["why"] == "tagged Bad Area",
          "the last outcome a record reached counts, and a bad-area tag is a bad outcome")
    crm = SuppCRM(hoods, account, presets)
    pol = policy.parse(supp_playbook(), EMPLOYEES)
    out = sweep.run("neighborhood_suppression", crm=crm, now=NOW, pol=pol, sources={})
    text = "\n".join([out.head] + out.lines)
    check(not crm.writes and not [c for c in crm.calls if c[0] != "GET"], "the check changes no preset and writes nothing")
    check(out.head == "Neighborhoods to suppress, for you to approve: Example Hills (12 of 12 worked records went bad), "
                      "against 66% across the account. Nothing was changed.", "the head names what to suppress: %s" % out.head)
    check("Sample Park" not in out.head and "Tiny Court" not in text,
          "a big neighborhood is not flagged for being big, and a small sample is not flagged at all")
    check("Already skipped by the presets: Old Town (9 of 9 bad)." in text and "Old Town" not in out.head,
          "a neighborhood the presets already skip is not suggested again")
    check("Ghost Hollow" in text and "matched no record in the records search" in text,
          "a neighborhood whose name moves no count in the search is left out of the change file, and the report says so")
    check("Example Hills: 12 of 12 worked records went bad (9 not interested, 2 dead lead and 1 tagged Bad Area)" in text
          and "42 records with a phone sit there" in text and "Median value $410,000, against $150,000" in text,
          "each suggestion carries its counts, its chance, the records it would take out and its values")
    check("Closest to the line: Pine Grove 9 of 12 bad" in text and "Sample Park" not in text.split("Closest to the line")[1],
          "the neighborhoods closest to the line are named, and one doing better than the account is not")
    check("Buyer Row" not in text and "10 buyer record(s) left out" in text,
          "buyers on the dispo list saying no to a deal are not counted against their neighborhood")
    plan_path = Path(out.run_dir) / "files" / "suppression_plan_2026-11-02.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = {e["title"]: e for e in plan["presets"]}
    check(set(entries) == {"FTM - 02 Ready to Call", "FTM - Mail 01 Handwritten Letter"} and plan["creates"] == []
          and [n["name"] for n in plan["neighborhoods"]] == ["Example Hills"],
          "the change file holds the presets that already skip neighborhoods, and only the proven suggestion")
    call = entries["FTM - 02 Ready to Call"]
    check(call["must_after"]["must_not"]["any_neighborhood"] == ["Old Town", "Example Hills"]
          and call["must_after"]["must_not"]["any_tags"] == ["t-dnm"]
          and call["before_sha"] == pp.sha(presets["FTM - 02 Ready to Call"]["must"]),
          "each change keeps every exclusion the preset had and appends the new neighborhood after them")
    check("PRESET_PATCH_ALLOW=1 python src/agents/autopilot/preset_patch.py apply --plan" in text and str(plan_path) in text,
          "the exact preset_patch.py command a person runs is printed with the file")

    print("  suppression: the change file applies with the preset tool")
    keep = os.environ.get(pp.ALLOW_ENV)
    os.environ[pp.ALLOW_ENV] = "1"
    try:
        done = pp.apply_plan(crm, plan, commit=True, out=lambda *_: None)
        twice = pp.apply_plan(crm, plan, commit=True, out=lambda *_: None)
    finally:
        if keep is None:
            os.environ.pop(pp.ALLOW_ENV, None)
        else:
            os.environ[pp.ALLOW_ENV] = keep
    check(sorted(done["patched"]) == sorted(entries) and presets["FTM - 02 Ready to Call"]["must"] == call["must_after"]
          and sorted(twice["already"]) == sorted(entries),
          "preset_patch.py applies the file as written, reads each preset back, and a second apply finds it done")
    check(pp.apply_plan(None, plan, commit=False, out=lambda *_: None)["would"] == ["patch " + e["title"] for e in plan["presets"]],
          "without both switches the preset tool only says what it would patch")
    check(out.lines[2].startswith("The change for 2 preset(s) is in suppression_plan_2026-11-02.json"),
          "the channel's six lines hold the suggestions and the command that applies them")

    print("  suppression: memory, caps and the other target")
    _fresh_supp()
    hoods, account, presets = supp_world()
    capped = SuppCRM(hoods, account, presets)
    c1 = sweep.run("neighborhood_suppression", crm=capped, now=NOW, pol=policy.parse(supp_playbook(read_cap=10), EMPLOYEES), sources={})
    first = list(capped.reads)
    capped.reads.clear()
    sweep.run("neighborhood_suppression", crm=capped, now=NOW, pol=policy.parse(supp_playbook(read_cap=10), EMPLOYEES), sources={})
    check(len(first) == 10 and len(capped.reads) == 10 and not set(first) & set(capped.reads)
          and "wait for the next run" in "\n".join(c1.lines),
          "no more records read than read_cap allows, the rest wait, and a record's neighborhood is read once")
    _fresh_supp()
    hoods, account, presets = supp_world()
    wide = SuppCRM(hoods, account, presets)
    m = sweep.run("neighborhood_suppression", crm=wide, now=NOW, pol=policy.parse(supp_playbook(apply_to="marketing"), EMPLOYEES),
                  sources={})
    mplan = json.loads((Path(m.run_dir) / "files" / "suppression_plan_2026-11-02.json").read_text(encoding="utf-8"))
    titles = {e["title"] for e in mplan["presets"]}
    check(titles == {"FTM - 02 Ready to Call", "FTM - Mail 01 Handwritten Letter", "Top 1000 - 02 Ready to Call"}
          and next(e for e in mplan["presets"] if e["title"].startswith("Top"))["must_after"]["must_not"]["any_neighborhood"]
          == ["Example Hills"], "apply_to: marketing reaches every call and mail preset, never the buyer program")
    later = sweep.run("neighborhood_suppression", crm=SuppCRM(hoods, account, presets), now=NOW + timedelta(days=30),
                      pol=policy.parse(supp_playbook(apply_to="marketing"), EMPLOYEES), sources={})
    check("First suggested Nov 2." in "\n".join(later.lines), "a suggestion made before says when it was first made")
    _fresh_supp()
    quiet = sweep.run("neighborhood_suppression", crm=SuppCRM({}, [], presets), now=NOW, pol=pol, sources={})
    check(quiet.head.startswith("No neighborhood to suppress: no record was worked to an outcome") and not quiet.run_dir,
          "with nothing worked there is nothing to suggest, and no change file")
    posts = sweep.posts({"id": 9, "workflow": "neighborhood_suppression", "source": "schedule"}, out.as_dict())
    check(len(posts) == 1 and posts[0]["kind"] in ("chat", "pipeline"), "a scheduled check posts its report once")
