"""The SmartSkip screen (Ty, 2026-11-23): every first-to-market record traced once through SmartSkip, a death on
file put on the record, a probate's representative found with their own address, the order book that never pays
twice, research's reuse of the answer, and the backfill over the orders SmartSkip already finished. Zero network:
a fake DataSift, a fake SmartSkip standing behind the REAL order book (sources.SmartSkip with its two client
modules swapped), a fake scorer, and this section's own databases and folders. Every name, street, number and id
here is invented; the numbers are on the 555 exchange.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import re
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

# The line the orchestrator adds to autopilot/registry.py EXTRA. Until it is there, this section stands the same
# line up for itself and takes it down again afterwards.
REGISTRY_LINES = {"skipscreen": ("skipscreen", "run_skipscreen", "paid", True, False)}
NOW = datetime(2026, 11, 23, 13, 5, tzinfo=timezone.utc)      # a Monday, 08:05 in Knoxville
STAGE = "ftm_skip_screen"
FTM = "FTM - 02 Ready to Call"
ESTATE = "Estate - 02 Ready to Call"
PHONE = re.compile(r"\d{7,}")
PRESETS = {
    FTM: lambda r: "FTM" in r["tags"] and not {"Probate", "Obituary"} & set(r.get("lists") or []),
    ESTATE: lambda r: "FTM" in r["tags"] and bool({"Probate", "Obituary"} & set(r.get("lists") or [])),
}


# ---------------------------------------------------------------------- the account
def ph(number, *tags, status="UNKNOWN", kind="MOBILE"):
    return {"number": number, "type": kind, "tags": list(tags), "status": status, "is_connected": True, "verified": False}


def rec(u, street, first, last, *, owner=None, mail=None, lists=("Foreclosure",), tags=("FTM",), phones=(), status=None,
        company=None, deceased=False):
    """A record; the owner mails to the house unless `mail` names a street ('' is no mailing address)."""
    m = street if mail is None else mail
    return {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": owner or "o-" + u, "first_name": first, "last_name": last, "company": company,
                      "deceased": deceased, "phones": [dict(p) for p in phones], "skiptrace_attempts": 0,
                      "address": ({"street": m, "city": "Knoxville", "state": "TN", "postal_code": "37917"} if m else {})},
            "status": status, "tags": list(tags), "lists": list(lists), "predictivecall_attempts": 0}


class FakeCRM:
    """The reads and the writes the stage and the backfill make, against records held in memory."""

    def __init__(self, records, custom=None, fields=None, sticky=True):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in records}
        self.custom = copy.deepcopy(custom or {})
        self.field_ids = dict(fields if fields is not None else {"decision maker": "f-dm", "dm relationship": "f-dmrel"})
        self.sticky = sticky
        self.writes: list = []
        self.msgs: list = []
        self.reads: list = []

    def add(self, r):
        self.recs[r["uuid"]] = copy.deepcopy(r)

    def presets(self):
        return {t: {"uuid": t, "folder": "", "must": {"_preset": t}} for t in PRESETS}

    def preset_must(self, title):
        if title not in PRESETS:
            raise KeyError("no preset titled %r in this account" % title)
        return {"_preset": title}

    def _row(self, r):
        return {"uuid": r["uuid"], "address": r["address"], "status": r.get("status"), "owner": {"uuid": r["owner"]["uuid"]}}

    def rows(self, must, cap=10000):
        return [self._row(r) for r in self.recs.values() if PRESETS[must["_preset"]](r)][:cap]

    def record(self, u):
        self.reads.append(u)
        return copy.deepcopy(self.recs.get(u) or {})

    def custom_fields(self, u):
        return dict(self.custom.get(u) or {})

    def custom_field_uuid(self, label):
        return self.field_ids.get(label.strip().lower(), "")

    def set_custom_field(self, u, fid, value):
        self.writes.append(("field", u, fid, value))
        label = next((k for k, v in self.field_ids.items() if v == fid), fid)
        self.custom.setdefault(u, {})[{"decision maker": "Decision Maker", "dm relationship": "DM Relationship"}.get(label, label)] = value

    def add_tags(self, u, add, rec=None):
        self.writes.append(("add_tags", u, tuple(add)))
        for t in add:
            if t not in self.recs[u]["tags"]:
                self.recs[u]["tags"].append(t)

    def remove_tags(self, u, drop):
        self.writes.append(("remove_tags", u, tuple(drop)))
        self.recs[u]["tags"] = [t for t in self.recs[u]["tags"] if t not in drop]

    def message(self, u, text, pin=False):
        self.msgs.append((u, text))
        self.writes.append(("message", u))
        return "m1"

    def _owners(self, owner_uuid):
        return [r["owner"] for r in self.recs.values() if r["owner"]["uuid"] == owner_uuid]

    def upsert_phones(self, owner_uuid, phones):
        self.writes.append(("upsert", owner_uuid, copy.deepcopy(phones)))
        for ow in self._owners(owner_uuid):
            by = {p["number"]: dict(p) for p in phones}
            ow["phones"] = [by.pop(p["number"], p) for p in ow["phones"]] + list(by.values())

    def skiptrace_attempts(self, owner_uuid, total):
        self.writes.append(("skiptrace_attempts", owner_uuid, total))
        for ow in self._owners(owner_uuid):
            ow["skiptrace_attempts"] = total

    def update_owner(self, owner_uuid, fields):
        self.writes.append(("owner", owner_uuid, copy.deepcopy(fields)))
        if not self.sticky:
            return {}
        for ow in self._owners(owner_uuid):
            if "deceased" in fields:
                ow["deceased"] = fields["deceased"]
            if "address" in fields:
                ow["address"] = dict(fields["address"])
        return {}

    def owner(self, owner_uuid):
        got = self._owners(owner_uuid)
        return copy.deepcopy(got[0]) if got else {}

    def owner_records(self, owner_uuid, cap=250):
        return [self._row(r) for r in self.recs.values() if r["owner"]["uuid"] == owner_uuid]

    def call(self, path, method="GET", body=None, headers=None):
        """The records search by address (backfill.find_record) and nothing else."""
        if path == "/api/internal/property/" and (headers or {}).get("x-http-method-override") == "GET":
            s = str((((body or {}).get("query") or {}).get("must") or {}).get("search") or "")
            want = s.split(":", 1)[1].strip().lower() if ":" in s else ""
            return {"results": [{"uuid": r["uuid"], "address": r["address"]} for r in self.recs.values()
                                if want and r["address"]["street"].lower().startswith(want)]}
        raise AssertionError("unexpected call %s" % path)

    def owner_writes(self, owner_uuid, kind="upsert"):
        return [w for w in self.writes if w[0] == kind and w[1] == owner_uuid]


class FakeScorer:
    """Tiers by the last digit: 1 Dial First, 2 Dial Second, 3 Dial Third, 9 Drop, 7 a litigator, else Dial Fourth."""
    unit_usd = 0.015

    def __init__(self):
        self.calls: list = []

    def configured(self):
        return True

    def score(self, nums):
        self.calls += list(nums)
        tiers = {"1": "Dial First", "2": "Dial Second", "3": "Dial Third", "9": "Drop"}
        out = {}
        for n in nums:
            if n.endswith("7"):
                out[n] = {"score": 60, "tier": "DNC", "line_type": "Mobile", "litigator": True}
            else:
                out[n] = {"score": 70, "tier": tiers.get(n[-1], "Dial Fourth"), "line_type": "Mobile", "litigator": False}
        return out


# ---------------------------------------------------------------------- SmartSkip
def rel(first, last, canon="Relative", street="", city="", state="", zipc="", phones=(), deceased=False):
    return {"first": first, "last": last, "name": "%s %s" % (first, last), "type_raw": canon, "canon_rel": canon,
            "gender": "?", "age": "", "mailing_street": street, "mailing_city": city, "mailing_state": state,
            "mailing_zip": zipc, "phones": [{"number": n, "type": "Wireless"} for n in phones], "deceased": deceased}


def ans(phones=(), relatives=(), deceased=False, first=None, last=None):
    return {"phones": list(phones), "relatives": list(relatives), "deceased": deceased, "first": first, "last": last}


def entry_for(row: dict, a: dict) -> dict:
    """What parse_smartskip reads out of a vertical file for one input row."""
    subj = [{"number": n, "type": "Wireless"} for n in a.get("phones") or []]
    rels = [copy.deepcopy(r) for r in a.get("relatives") or []]
    return {"input_name": "%s %s" % (row["First Name"], row["Last Name"]), "first": a.get("first") or row["First Name"],
            "last": a.get("last") or row["Last Name"], "property_address": row["Property Address"],
            "property_city": row["Property City"], "property_state": row["Property State"],
            "property_zip": row["Property Zip"], "mailing_address": row["Mailing Address"],
            "mailing_city": row["Mailing City"], "mailing_state": row["Mailing State"], "mailing_zip": row["Mailing Zip"],
            "deceased": bool(a.get("deceased")), "subject_phones": subj, "relatives": rels, "associate_keys": [],
            "has_results": bool(subj) or any(r["phones"] for r in rels), "format": 1}


class FakeSS:
    """smartskip_trace's calls against an in-memory SmartSkip. `answers` maps the name an order sends ("first last",
    lower case) to what SmartSkip says about that person; a name it does not know returns nobody."""

    def __init__(self, answers, pay=True, status="completed"):
        self.answers = answers
        self.pay_result = pay            # True, False (the card asks for 3DS) or "refuse" (declined)
        self.status = status             # what a paid order reads: completed or processing
        self.orders: dict = {}
        self.calls: list = []
        self.n = 0
        self.book = None                 # the source, to see what its book held when pay was called
        self.book_at_pay: list = []

    def Session(self):
        return object()

    def submit(self, sess, path, overrides):
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.n += 1
        bid = "bs-%d" % self.n
        self.orders[bid] = {"rows": rows, "paid": False, "status": "pending"}
        self.calls.append(("submit", bid))
        return {"bulkSkipId": bid, "entities": len(rows)}

    def pay(self, sess, bid):
        self.calls.append(("pay", bid))
        if self.book is not None:
            self.book_at_pay.append([e.get("status") for e in self.book.book() if e.get("bulkSkipId") == bid])
        if self.pay_result == "refuse":
            raise SystemExit("ERROR: payment-intent failed (402): card declined")
        if not self.pay_result:
            return False
        self.orders[bid].update(paid=True, status=self.status)
        return True

    def fetch_status(self, sess, bid=None):
        self.calls.append(("status", bid))
        items = [{"_id": b, "status": o["status"]} for b, o in self.orders.items() if o["paid"]]
        return [i for i in items if i["_id"] == bid] if bid else items

    def download(self, sess, bid, out, fmt):
        self.calls.append(("download", bid))
        o = self.orders.get(bid)
        if not o or not o["paid"] or o["status"] != "completed":
            raise SystemExit("ERROR: download failed (400)")
        Path(out).write_text(json.dumps({"bid": bid}), encoding="utf-8")

    def count(self, what):
        return sum(1 for c in self.calls if c[0] == what)


class FakePS:
    def __init__(self, ss):
        self.ss = ss

    def parse(self, path):
        bid = json.loads(Path(path).read_text(encoding="utf-8"))["bid"]
        o = self.ss.orders[bid]
        if "parsed" in o:                       # an order finished long ago (the backfill's)
            return copy.deepcopy(o["parsed"])
        out = []
        for row in o["rows"]:
            a = self.ss.answers.get(("%s %s" % (row["First Name"], row["Last Name"])).lower())
            if a is not None:
                out.append(entry_for(row, a))
        return out


def make_source(ss, folder, **kw):
    from .autopilot.sources import SmartSkip

    class TestSkip(SmartSkip):
        def configured(self):
            return True

        def _modules(self):
            return ss, FakePS(ss)

    src = TestSkip(max_wait_s=0, poll_s=0, folder=folder, **kw)
    ss.book = src
    return src


# ---------------------------------------------------------------------- the playbook
def playbook(mode="plan", **over) -> dict:
    # max_wait_s 0: the stage looks at its order once; the fake SmartSkip never makes a test wait.
    stage = {"kind": "skipscreen", "owner": "research", "mode": mode, "schedule": "08:05 Mon-Fri", "lane": "ftm",
             "presets": [FTM, ESTATE], "daily_usd": 25, "max_per_order": 100, "max_wait_s": 0,
             "write_tiers": ["Dial First", "Dial Second"], "reserve_tiers": ["Dial Third"], "keep_numbers": 3,
             "drop_status": "DEAD", "write_line_type": True, "agreement_tag": "Two Sources"}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
            "gate": {"read_fields": True}, "lanes": {"ftm": {"tags": ["FTM"]}}, "stages": {STAGE: stage}}


def fixture():
    recs = [
        rec("sa", "11 Example St", "Lou", "Sample", tags=("FTM", "living")),          # SmartSkip lists the owner as dead
        rec("sb", "12 Example St", "Kim", "Demo", owner="o-kim"),                     # living: numbers land
        rec("sq", "52 Example St", "Kim", "Demo", owner="o-kim"),                     # the same owner, another house
        rec("sc", "13 Example St", "Dale", "Testa", lists=("Obituary",), tags=("FTM", "Verify Deceased")),
        rec("sd", "14 Example St", "Robin", "Example", lists=("Probate",), tags=("FTM", "probate")),
        rec("se", "15 Example St", "Jo", "Sample", lists=("Probate",)),
        rec("sf", "16 Example St", "Ray", "Demo", lists=("Probate",)),
        rec("sg", "17 Example St", "Max", "Testa", lists=("Probate",)),
        rec("sh", "18 Example St", "Ann", "Sample", mail="77 Third Way", lists=("Probate",)),
        rec("si", "19 Example St", "Chris", "Example", lists=("Probate",)),
        rec("sj", "20 Example St", "Toni", "Demo", lists=("Probate",)),
        rec("sk", "21 Example St", "", "", company="Example Holdings LLC"),
        rec("sl", "22 Example St", "Mo", "Sample", status="Hot Lead"),
        rec("sm", "23 Example St", "Cy", "Demo"),
        rec("sn", "24 Example St", "Di", "Testa", tags=("FTM", "Do Not Market")),
        rec("so", "25 Example St", "Ed", "Sample", tags=("FTM", "Deep Prospected")),
        rec("sp", "26 Example St", "Flo", "Demo", deceased=True),
        rec("sr", "27 Example St", "Gus", "Sample"),
        rec("ss", "28 Example St", "Ned", "Example Jr"),
        rec("st", "29 Example St", "Pia", "Example", mail="", lists=("Probate",)),   # no mailing address at all
    ]
    custom = {"sd": {"Decedent Name": "Estate of Pat Q. Example"}, "se": {"Decedent Name": "Lee Sample"},
              "sf": {"Decedent Name": "Ann Demo"}, "sg": {"Decedent Name": "Eve Testa"},
              "sh": {"Decedent Name": "Bo Sample"}, "si": {"Decedent Name": "Dana Example"},
              "st": {"Decedent Name": "Vic Example"}}
    answers = {
        "lou sample": ans(phones=["8655550111"], deceased=True),
        "kim demo": ans(phones=["8655550121", "8655550129"],
                        relatives=[rel("Lee", "Demo", "Son", "5 Kin Ct", "Knoxville", "TN", "37917", ["8655550131"])]),
        "dale testa": ans(phones=["8655550141"]),
        "pat example": ans(phones=["8655550151"], deceased=True, relatives=[
            rel("Robin", "Example", "Daughter", "40 Other Rd", "Maryville", "TN", "37801", ["8655550201", "8655550202"]),
            rel("Sam", "Example", "Son", "41 Other Rd", "Maryville", "TN", "37801", ["8655550251"])]),
        "lee sample": ans(deceased=True, relatives=[
            rel("Jo", "Sample", "Daughter", "42 Other Rd", "Maryville", "TN", "37801", ["8655550261"], deceased=True)]),
        "ann demo": ans(relatives=[rel("Ray", "Demo", "Son", "43 Other Rd", "Maryville", "TN", "", ["8655550271"])]),
        "eve testa": ans(relatives=[rel("Max", "Testa", "Son", "17 Example St", "Knoxville", "TN", "37917", ["8655550281"])]),
        "bo sample": ans(relatives=[rel("Ann", "Sample", "Wife", "88 Fourth Ave", "Knoxville", "TN", "37919", ["8655550291"])]),
        "dana example": ans(relatives=[
            rel("Chris", "Example", "Son", "44 Other Rd", "Maryville", "TN", "37801", ["8655550301"]),
            rel("Christopher", "Example", "Brother", "45 Other Rd", "Maryville", "TN", "37801", ["8655550311"])]),
        "toni demo": ans(phones=["8655550321"]),
        "gus sample": ans(deceased=True, relatives=[rel("Gus", "Sample", "Son", "6 Kin Ct", "Knoxville", "TN", "37917",
                                                        ["8655550331"])]),
        "ned example jr": ans(deceased=True, first="Ned", last="Example Sr"),
        "vic example": ans(relatives=[rel("Pia", "Example", "Daughter", "46 Other Rd", "Maryville", "TN", "37801",
                                          ["8655550341"])]),
    }
    return recs, custom, answers


def said(ok, msg, got) -> str:
    """The rule as the check's message, and what came back when it does not hold."""
    return msg if ok else "%s (got %r)" % (msg, got)


def fresh(tmp, name):
    """A database and data folder of this section's own, so one scenario's spend and memory cannot reach another."""
    from . import store
    config.DATA_DIR = Path(tmp) / "skipscreen" / name
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.RUNS_DIR = config.DATA_DIR / "runs"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    return config.DATA_DIR


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from .autopilot import policy, registry
    keep = (config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE, config.DAILY_CAP_USD)
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    allow_env = os.environ.pop("SKIPSCREEN_ALLOW", None)
    config.DAILY_CAP_USD = 100000.0
    try:
        check(all(registry.EXTRA.get(k) == row for k, row in REGISTRY_LINES.items())
              and not [p for p in registry.problems() if p.startswith("skipscreen")],
              "the skip screen kind loads from its registry line")
        _pure(check)
        _settings(check, policy)
        _plan(check, tmp, policy)
        _live(check, tmp, policy)
        _orders(check, tmp, policy)
        _caps(check, tmp, policy)
        _retrace(check, tmp)
        _research(check, tmp, policy)
        _backfill(check, tmp, policy)
        _hygiene(check)
    finally:
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        if allow_env is not None:
            os.environ["SKIPSCREEN_ALLOW"] = allow_env
        else:
            os.environ.pop("SKIPSCREEN_ALLOW", None)
        config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE, config.DAILY_CAP_USD = keep


# ====================================================================== the rules, one at a time
def _pure(check) -> None:
    from .autopilot import skipscreen as sk
    print("  names")
    check(sk.person_name("Estate of Pat Q. Example") == ("Pat", "Example") and sk.person_name("EXAMPLE, PAT Q") == ("Pat", "Example")
          and sk.person_name("Pat Q Example, Deceased") == ("Pat", "Example")
          and sk.person_name("Pat Q. Example, De- Ceased") == ("Pat", "Example"),
          "a decedent's name reads the same however the notice wrote it (estate of, county order, deceased)")
    check(sk.person_name("Pat Example A/K/A Patty Example") == ("Pat", "Example") and sk.person_name("Van Sample, Lou") == ("Lou", "Van Sample")
          and sk.person_name("Pat and Robin Example") == ("Pat", "Example"),
          "an a/k/a is cut, a surname particle stays with the surname, and the first of two people is taken")
    check(all(sk.person_name(v) == ("", "") for v in ("By The Chancery Court", "Unknown", "", "J Example", "Example Law Firm PLLC", "Pat 2")),
          "a court, an unknown, a blank, a bare initial, a firm and a number name nobody")
    check(sk.firsts_agree("Pat", "Patricia") and sk.firsts_agree("Bill", "William") and not sk.firsts_agree("Robert", "Rachel", True)
          and sk.firsts_agree("R", "Rachel", True) and not sk.firsts_agree("R", "Rachel"),
          "a nickname is the same first name; a shared first letter is not, unless one side is a bare initial and initials are allowed")
    check(sk.same_person("Ned", "Example Sr", "Ned", "Example Jr") and sk.generation("Ned", "Example Sr") == "sr",
          "a generation word is read apart from the surname")
    print("  the representative among the relatives")
    one = {"relatives": [rel("Robin", "Example", "Daughter", "40 Other Rd"), rel("Sam", "Example", "Son", "41 Other Rd")]}
    pr, why = sk.find_pr(one, "Robin", "Example")
    check(pr is not None and pr["first"] == "Robin" and not why, "the owner's name among the relatives is the representative")
    check(sk.find_pr({"relatives": [rel("R", "Example", street="40 Other Rd")]}, "Robin", "Example")[0] is not None,
          "a bare initial with the same surname is a candidate")
    two = {"relatives": [rel("Chris", "Example", street="44 Other Rd"), rel("Christopher", "Example", street="45 Other Rd")]}
    check(sk.find_pr(two, "Chris", "Example") == (None, sk.TWO_PR), "two candidates means none")
    twice = {"relatives": [rel("Robin", "Example", street="40 Other Rd"), rel("Robin", "Example", street="40 Other Road")]}
    check(sk.find_pr(twice, "Robin", "Example")[0] is not None, "one person listed twice at one street is one candidate")
    check(sk.find_pr({"relatives": [rel("Rachel", "Example", street="40 Other Rd")]}, "Robert", "Example") == (None, sk.NO_PR),
          "a sister is never read as her brother on a shared first letter")
    print("  addresses")
    full = sk._place("40 Other Rd", "Maryville", "tn", "37801-1234")
    check(sk.complete(full) and full["zip"] == "37801" and full["state"] == "TN", "a street, a city, a two-letter state and a five-digit ZIP make a complete address")
    check(not sk.complete(sk._place("40 Other Rd", "Maryville", "TN", "")) and not sk.complete(sk._place("40 Other Rd", "", "TN", "37801"))
          and not sk.complete(sk._place("", "Maryville", "TN", "37801")) and not sk.complete(sk._place("40 Other Rd", "Maryville", "Tenn", "37801")),
          "an address missing its ZIP, city, street or a two-letter state is not complete")
    check(sk.same_place(sk._place("40 Other Road", "", "", "37801"), full) and not sk.same_place(sk._place("41 Other Rd", "", "", "37801"), full)
          and not sk.same_place(sk._place("40 Other Rd", "", "", "37919"), full),
          "the same house is the same number, street and ZIP")


def _settings(check, policy) -> None:
    from .autopilot import skipscreen as sk
    print("  settings")
    pol = policy.parse(playbook(), EMPLOYEES)
    check(sk.block_problems(pol.stage(STAGE)) == [], "the stage's block loads with every default")
    bad = [{"max_per_order": 0}, {"max_per_order": 5000}, {"possible_death": "sometimes"}, {"probate_marks": []},
           {"pr_tag": "Dial First"}, {"write_tiers": ["Dial Fifth"]}, {"record_usd": 0}, {"collect_wait_s": -1},
           {"dm_field": ""}, {"max_reads": True}]
    refused = 0
    for over in bad:
        try:
            policy.parse(playbook(**over), EMPLOYEES)
        except policy.PolicyError:
            refused += 1
    check(refused == len(bad), said(refused == len(bad), "a mistyped setting is refused when the playbook loads, not read as a default",
                                    "%d of %d" % (refused, len(bad))))


# ====================================================================== plan mode
def _run(policy, crm, src, mode="plan", **over):
    from .autopilot import sweep
    return sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook(mode, **over), EMPLOYEES),
                     sources={"smartskip": src}, scorer=FakeScorer())


def _seed_tracerfy(u: str) -> None:
    from .autopilot import ledger
    ledger.event("r0", "ftm_retrace", u, "trace", {"source": "tracerfy", "found": 0, "new": 0}, usd=0.02, now=NOW)


def _plan(check, tmp, policy) -> None:
    from .autopilot import ledger
    from .autopilot import skipscreen as sk
    print("  a plan run")
    home = fresh(tmp, "plan")
    recs, custom, answers = fixture()
    _seed_tracerfy("sm")
    config.MODE = "live"            # the box is live: the stage's own plan mode must still keep it from buying or writing
    crm, ss = FakeCRM(recs, custom), FakeSS(answers)
    src = make_source(ss, home / "smartskip")
    out = _run(policy, crm, src)
    want = ("Would trace 13 record(s) through SmartSkip, up to $1.95 before scoring: 5 owner(s) of record, 7 probate "
            "decedent(s) to find the representative, 1 probate representative(s).")
    check(out.head == want, said(out.head == want, "the plan says how many records it would trace, by who is traced, and the most it would cost",
                                 out.head))
    check(not ss.calls and not crm.writes and out.writes == 0 and not src.book(),
          said(not ss.calls and not crm.writes, "a plan run neither orders from SmartSkip nor writes to DataSift", (ss.calls, crm.writes)))
    check(abs(out.would_usd - 1.95) < 1e-6 and out.usd == 0, said(abs(out.would_usd - 1.95) < 1e-6, "the plan prices the order and spends nothing",
                                                                   (out.would_usd, out.usd)))
    check(not ledger.events(action="trace", stage=STAGE), "a plan run records no trace, so a later live run is not fooled")
    t = out.tally
    check(t.get("passed over: " + sk.ENTITY_WHY) == 1, said(t.get("passed over: " + sk.ENTITY_WHY) == 1, "a company is never traced: people only", dict(t)))
    check(t.get("held back: already a lead (hot lead)") == 1, said(t.get("held back: already a lead (hot lead)") == 1, "a lead is never touched", dict(t)))
    check(t.get("held back: tagged Do Not Market") == 1, "the gate holds a Do Not Market record from a paid trace")
    check(t.get("already traced (by SmartSkip or another source)") == 1, "a record any source already traced is not bought again")
    check(t.get("passed over: research already has it (deep prospected)") == 1, "a record research already has is left to research")
    check(t.get("passed over: the owner's death is already on the record") == 1, "a death already settled on the record is not traced again")
    check(t.get("shares its owner with another record in the order") == 1, "one owner with two houses is one row in the order")
    check(not PHONE.search(" ".join([out.head] + list(out.lines) + list(out.problems))), "no phone number appears in what the stage says")
    crm2 = FakeCRM(recs, custom)
    _run(policy, crm2, make_source(FakeSS(answers), home / "smartskip2"))
    check(not {"sk", "so", "sp"} & set(crm2.reads), said(not {"sk", "so", "sp"} & set(crm2.reads),
                                                          "a record passed over for a lasting reason is not read again the next day", crm2.reads))
    hold = _run(policy, FakeCRM(recs, custom), make_source(FakeSS(answers), home / "smartskip3"), possible_death="hold")
    check(hold.tally.get("held back: the owner has died or may have (tag verify deceased, Obituary list); research first") == 1
          and hold.head.startswith("Would trace 12 record(s)"),
          said(hold.head.startswith("Would trace 12"), "with possible_death hold a possible death is left to research", (hold.head, dict(hold.tally))))
    config.MODE = "dry"
    dry_crm, dry_ss = FakeCRM(recs, custom), FakeSS(answers)
    dry = _run(policy, dry_crm, make_source(dry_ss, home / "smartskip4"), mode="live")
    check(not dry_ss.calls and not dry_crm.writes and dry.head.startswith("Would trace"), "a live stage on a dry box buys and writes nothing")
    config.MODE = "live"


# ====================================================================== live
def _phones(crm, u):
    return {p["number"]: p for p in crm.recs[u]["owner"]["phones"]}


def _live(check, tmp, policy) -> None:
    from .autopilot import ledger
    from .autopilot import skipscreen as sk
    print("  a live run")
    home = fresh(tmp, "live")
    recs, custom, answers = fixture()
    _seed_tracerfy("sm")
    config.MODE = "live"
    crm, ss = FakeCRM(recs, custom), FakeSS(answers)
    src = make_source(ss, home / "smartskip")
    out = _run(policy, crm, src, mode="live")
    check(ss.count("submit") == 1 and ss.count("pay") == 1, said(ss.count("submit") == 1, "one order a run", ss.calls))
    check(ss.book_at_pay == [["submitted"]], said(ss.book_at_pay == [["submitted"]], "the order is in the book before the card is charged",
                                                  ss.book_at_pay))
    check([e["status"] for e in src.book()] == ["claimed"], "an order whose results were used is closed in the book")
    print("  a death on the owner of record")
    check(crm.owner_writes("o-sa", "owner") == [("owner", "o-sa", {"deceased": True})] and crm.recs["sa"]["owner"]["deceased"] is True,
          "the owner SmartSkip lists as deceased is marked deceased")
    check("deceased" in crm.recs["sa"]["tags"] and "living" not in crm.recs["sa"]["tags"], "the death tag goes on and living comes off")
    check(("sa", "SmartSkip lists Lou Sample as deceased. Research is next; do not call or text the owner.") in crm.msgs
          and sum(1 for u, _ in crm.msgs if u == "sa") == 1, "one note says SmartSkip lists the owner as deceased and research is next")
    check(ledger.fact("sa", sk.DEATH_FACT).get("order") == "bs-1", "a fact names the order that settled the death")
    check(not crm.owner_writes("o-sa"), "a dead owner's numbers are not landed")
    check(not [e for e in ledger.events(action="handoff")], "the stage never queues research itself")
    print("  a living owner")
    sb = _phones(crm, "sb")
    check("8655550121" in sb and set(sb["8655550121"]["tags"]) >= {"SmartSkip", "Dial First"},
          "a living owner's own number lands, tiered and tagged with its source")
    check("8655550129" not in sb, "a Drop number is kept off the record, as the retrace keeps it")
    check("8655550131" not in sb, "a relative's number never lands on a living owner")
    check(len(crm.owner_writes("o-kim")) == 1 and crm.recs["sb"]["owner"]["skiptrace_attempts"] == 1,
          "one owner with two houses gets their numbers and the skip trace counter once")
    sq = [e for e in ledger.events("sq", "trace")]
    check(len(sq) == 1 and sq[0]["detail"].get("shared_with") == "sb" and sq[0]["usd"] == 0,
          "the second house is recorded as traced at no cost, on the first one's answer")
    check("8655550141" in _phones(crm, "sc") and not crm.owner_writes("o-sc", "owner"),
          "a possible death SmartSkip does not confirm is traced and its numbers land; nothing is marked")
    print("  a probate traced through its decedent")
    sd = _phones(crm, "sd")
    check({"8655550201", "8655550202"} <= set(sd) and all("Personal Representative" in sd[n]["tags"] for n in ("8655550201", "8655550202")),
          "the representative's own numbers land on the owner, tagged Personal Representative")
    check("8655550251" not in sd and "8655550151" not in sd, "another relative's numbers and the decedent's own never land")
    sd_addr = crm.recs["sd"]["owner"]["address"]
    check(sd_addr == {"street": "40 Other Rd", "city": "Maryville", "state": "TN", "postal_code": "37801"},
          said(sd_addr.get("street") == "40 Other Rd", "a probate mailing to the house is sent to the representative's own address", sd_addr))
    check(crm.custom["sd"].get("Decision Maker") == "Robin Example" and crm.custom["sd"].get("DM Relationship") == "Personal Representative",
          "the empty decision maker fields name the representative")
    check(("add_tags", "se", ("Verify Deceased",)) in crm.writes and not crm.owner_writes("o-se", "owner")
          and any(u == "se" and "the owner of record on this probate" in t for u, t in crm.msgs),
          "a representative SmartSkip lists as deceased is held to verify, never marked deceased")
    check(not crm.owner_writes("o-sf") and not crm.owner_writes("o-sf", "owner") and crm.custom["sf"].get("Decision Maker") == "Ray Demo",
          "a representative found without a complete address gets no numbers and no address change, and is still named")
    check(crm.owner_writes("o-sg") and not crm.owner_writes("o-sg", "owner")
          and out.tally.get("representatives who live at the house (mailing kept)") == 1,
          "a representative who lives at the house keeps it, and is counted")
    check(not crm.owner_writes("o-sh", "owner") and ledger.fact("sh", sk.PERSON_FACT) and sk.PERSON_PROBLEM in out.problems,
          "a probate mailing to a third address is left alone and listed for a person")
    with sk.person_file().open(encoding="utf-8", newline="") as fh:
        listed = {r["record"] for r in csv.DictReader(fh)}
    check(listed == {"sh"}, said(listed == {"sh"}, "the list for a person holds the third-address record", listed))
    st_addr = crm.recs["st"]["owner"]["address"]
    check(st_addr.get("street") == "46 Other Rd" and st_addr.get("postal_code") == "37801",
          said(st_addr.get("street") == "46 Other Rd", "an owner with no mailing address gets the representative's own", st_addr))
    check(not crm.owner_writes("o-si") and not crm.owner_writes("o-si", "owner") and out.tally.get(sk.TWO_PR) == 1
          and not crm.custom.get("si", {}).get("Decision Maker"),
          "two relatives who could be the representative mean nothing is written")
    sj = _phones(crm, "sj")
    check("8655550321" in sj and "Personal Representative" not in sj["8655550321"]["tags"],
          "a probate with no decedent named traces its owner for numbers only")
    print("  namesakes")
    check(not crm.owner_writes("o-sr", "owner") and out.tally.get("death left alone: a living relative shares the owner's name") == 1,
          "a living relative with the owner's name stops the deceased mark")
    check(not crm.owner_writes("o-ss", "owner") and out.tally.get("death left alone: a senior and a junior share the name") == 1,
          "a senior is never marked for his junior")
    print("  what is remembered")
    traced = {e["record_uuid"] for e in ledger.events(action="trace", stage=STAGE)}
    want = {"sa", "sb", "sq", "sc", "sd", "se", "sf", "sg", "sh", "si", "sj", "sr", "ss", "st"}
    check(traced == want, said(traced == want, "every traced record has the ledger's trace row", sorted(traced)))
    check(all(e["detail"].get("source") == "smartskip" and e["detail"].get("order") == "bs-1"
              for e in ledger.events(action="trace", stage=STAGE)), "each trace row names the source and the order")
    trace_usd = round(sum(e["usd"] for e in ledger.events(action="trace", stage=STAGE)), 2)
    check(trace_usd == 1.80, said(trace_usd == 1.80, "the trace rows carry what SmartSkip billed: 15 cents a record that returned anything",
                                  trace_usd))
    f = ledger.fact("sd", sk.FACT)
    check(f and f["role"] == "decedent" and f["deceased"] is True and f["relatives"] == 2 and f["pr_found"] is True
          and f["order"] == "bs-1" and isinstance(f.get("parsed"), dict),
          said(bool(f), "the fact for research holds the flag, the relatives, the representative and the order", f))
    check(ledger.fact("sb", sk.FACT)["role"] == "owner" and ledger.fact("sb", sk.FACT)["pr_found"] is None, "a non-probate fact has no representative")
    check(ledger.tried_sources("sa") == {"smartskip"}, "the retrace sees SmartSkip as already tried on a record traced here")
    check(not PHONE.search(" ".join([out.head] + list(out.lines) + list(out.problems))), "no phone number appears in what the live run says")
    check(out.head.startswith("Traced 14 record(s) through SmartSkip for $"), said(out.head.startswith("Traced 14"), "the live run says what it did", out.head))
    print("  the next run")
    again_ss = FakeSS(answers)
    again = _run(policy, FakeCRM(list(crm.recs.values()), crm.custom), make_source(again_ss, home / "smartskip"), mode="live")
    check(again_ss.count("submit") == 0 and again.head == "No first-to-market record is waiting for a SmartSkip trace.",
          said(again_ss.count("submit") == 0, "nothing traced once is bought again", again.head))
    print("  undo")
    run_id = next(c["run_id"] for c in ledger.changes() if c["what"] == "owner_deceased")
    changes = {c["what"]: c for c in ledger.changes(run_id=run_id) if c["what"] in sk.UNDOABLE and c["record_uuid"] in ("sa", "sd")}
    blank_before = next((c for c in ledger.changes(run_id=run_id) if c["what"] == "owner_address" and c["record_uuid"] == "st"), None)
    check(set(changes) == {"owner_deceased", "owner_address"} and changes["owner_address"]["before"]["address"]["street"] == "14 Example St",
          "the owner changes are recorded with the values they replaced")
    sk.undo_step(crm, changes["owner_deceased"])
    sk.undo_step(crm, changes["owner_address"])
    check(crm.recs["sa"]["owner"]["deceased"] is False and crm.recs["sd"]["owner"]["address"]["street"] == "14 Example St",
          "undo puts the owner's deceased mark and mailing address back")
    try:
        sk.undo_step(crm, blank_before or {})
        blank = False
    except RuntimeError:
        blank = True
    check(blank_before is not None and blank_before["before"].get("address") == {} and blank,
          "a mailing address that was blank before is recorded as blank, and undo says so instead of guessing one back")
    print("  a write that does not stick")
    home2 = fresh(tmp, "sticky")
    bad = FakeCRM([rec("ta", "11 Example St", "Lou", "Sample"), rec("tb", "61 Example St", "Pia", "Sample")], sticky=False)
    loose = _run(policy, bad, make_source(FakeSS({"lou sample": ans(phones=["8655550111"], deceased=True),
                                                  "pia sample": ans(phones=["8655550411"], deceased=True)}), home2 / "smartskip"), mode="live")
    marks = [w for w in bad.writes if w[0] == "owner"]
    check(len(marks) == 1 and any(p.startswith("the owner's deceased mark did not stick on") for p in loose.problems),
          said(len(marks) == 1, "a deceased mark that did not stick is said, and no more are made that run", loose.problems))


# ====================================================================== the order book
def _orders(check, tmp, policy) -> None:
    from .autopilot import ledger
    from .autopilot import skipscreen as sk
    print("  an order an earlier run paid for")
    home = fresh(tmp, "book")
    recs, custom, answers = fixture()
    two = [r for r in recs if r["uuid"] in ("sa", "sb")]
    config.MODE = "live"
    ss = FakeSS(answers, status="processing")
    crm = FakeCRM(two, custom)
    src = make_source(ss, home / "smartskip")
    first = _run(policy, crm, src, mode="live")
    check(not crm.writes and not ledger.events(action="trace") and [e["status"] for e in src.book()] == ["paid"],
          said(not crm.writes, "a paid order still running writes nothing and stays open in the book", (crm.writes, src.book())))
    check(first.usd == 0 and "still running" in " ".join(first.lines), "a run whose order is still running books no spend and says why")
    crm.add(next(r for r in recs if r["uuid"] == "sc"))
    second = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live")
    check(ss.count("submit") == 1 and second.head == "Nothing traced: waiting on an open SmartSkip order.",
          said(ss.count("submit") == 1, "while an earlier order is still running nothing new is ordered", (ss.calls, second.head)))
    ss.orders["bs-1"]["status"] = "completed"
    mark = len(ss.calls)
    third = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live")
    names = [c for c in ss.calls[mark:] if c[0] in ("download", "submit")]
    check(names[:2] == [("download", "bs-1"), ("submit", "bs-2")],
          said(names[:2] == [("download", "bs-1"), ("submit", "bs-2")], "the earlier order is collected before a new one is placed", names))
    check(ss.count("pay") == 2 and crm.recs["sa"]["owner"]["deceased"] is True and "8655550121" in _phones(crm, "sb"),
          "the earlier order's results are used without paying for them again")
    check({e["record_uuid"] for e in ledger.events(action="trace") if e["detail"].get("order") == "bs-1"} == {"sa", "sb"}
          and third.tally.get("orders from an earlier run collected") == 1, "the collected order's records get their trace rows")
    check(round(ledger.spent(record_uuid="sb"), 3) >= 0.15, "what the earlier order cost is booked when it is collected")
    print("  a card that asks for a confirmation")
    home = fresh(tmp, "card")
    ss = FakeSS(answers, pay=False)
    crm = FakeCRM(two, custom)
    out = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live")
    check(sk.CARD_PROBLEM in out.problems and [e["status"] for e in make_source(ss, home / "smartskip").book()] == ["needs_card"]
          and not crm.writes, "a payment that needs a card confirmation says so and writes nothing")
    again = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live")
    check(ss.count("submit") == 1 and sk.CARD_PROBLEM in again.problems, "no new order while one waits for the card confirmation")
    ss.pay_result = True
    later = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live", card_wait_days=0)
    book = {e["bulkSkipId"]: e["status"] for e in make_source(ss, home / "smartskip").book()}
    check(book.get("bs-1") == "abandoned" and ss.count("submit") == 2 and crm.recs["sa"]["owner"]["deceased"] is True,
          said(book.get("bs-1") == "abandoned", "an order never paid is given up after card_wait_days and the records are ordered again",
               (book, later.problems)))
    print("  a refused payment")
    home = fresh(tmp, "refused")
    ss = FakeSS(answers, pay="refuse")
    crm = FakeCRM(two, custom)
    out = _run(policy, crm, make_source(ss, home / "smartskip"), mode="live")
    check(any(p.startswith("SmartSkip failed, nothing was traced: SmartSkip refused the payment") for p in out.problems)
          and not crm.writes and not ledger.events(action="trace")
          and [e["status"] for e in make_source(ss, home / "smartskip").book()] == ["pay_failed"],
          "a refused payment is said, recorded and nothing is written")


# ====================================================================== caps
def _caps(check, tmp, policy) -> None:
    print("  money caps")
    fresh(tmp, "caps")
    recs, custom, answers = fixture()
    config.MODE = "live"
    out = _run(policy, FakeCRM(recs, custom), make_source(FakeSS(answers), Path(config.DATA_DIR) / "smartskip"), daily_usd=0.30)
    check(out.head.startswith("Would trace 2 record(s)") and out.tally.get("not traced today (stage cap $0.30/day)", 0) >= 1,
          said(out.head.startswith("Would trace 2"), "the stage's day cap stops the order", (out.head, dict(out.tally))))
    tight = _run(policy, FakeCRM(recs, custom), make_source(FakeSS(answers), Path(config.DATA_DIR) / "smartskip"), record_usd=0.10)
    check(tight.head.startswith("Nothing traced:") and tight.tally.get("at the per-record cap", 0) >= 1,
          said(tight.head.startswith("Nothing traced:"), "the per-record cap stops a record's trace", (tight.head, dict(tight.tally))))
    small = _run(policy, FakeCRM(recs, custom), make_source(FakeSS(answers), Path(config.DATA_DIR) / "smartskip"), max_per_order=3)
    check(small.head.startswith("Would trace 3 record(s)") and small.tally.get("left for the next run (3 an order)", 0) >= 1,
          "no order holds more records than max_per_order")
    reads = FakeCRM(recs, custom)
    _run(policy, reads, make_source(FakeSS(answers), Path(config.DATA_DIR) / "smartskip"), max_reads=4)
    check(len([u for u in reads.reads]) <= 4, said(len(reads.reads) <= 4, "no more records are opened in a run than max_reads allows", reads.reads))


# ====================================================================== the retrace's contract
def _retrace(check, tmp) -> None:
    from .autopilot.sources import subject_of
    print("  the retrace's trace(), on the same book")
    home = fresh(tmp, "trace")
    recs, custom, answers = fixture()
    subs = [subject_of(r) for r in recs if r["uuid"] in ("sb", "sc")]
    ss = FakeSS(answers, status="processing")
    src = make_source(ss, home / "smartskip")
    try:
        src.trace(subs)
        raised = ""
    except RuntimeError as exc:
        raised = str(exc)
    check("still running" in raised and [e["status"] for e in src.book()] == ["paid"],
          said("still running" in raised, "a timed-out order raises as before and stays open in the book", raised))
    ss.orders["bs-1"]["status"] = "completed"
    found, billed = src.trace(subs)
    check(ss.count("submit") == 1 and ss.count("pay") == 1, "the next call collects the paid order instead of buying the records again")
    check(set(found) == {"sb", "sc"} and [n["number"] for n in found["sb"]] == ["8655550121", "8655550129"] and billed == 0.30,
          said(billed == 0.30, "the numbers come back as before, with what that order cost", (found, billed)))
    check(src.details["sb"]["order"] == "bs-1" and len(src.details["sb"]["relatives"]) == 1 and src.details["sb"]["deceased"] is False,
          "the record's details keep SmartSkip's flag, the relatives and the order id")
    check([e["status"] for e in src.book()] == ["claimed"], "a collected order is closed in the book")
    ss.status = "completed"
    more = [subject_of(r) for r in recs if r["uuid"] == "sa"]
    found2, billed2 = src.trace(more)
    check(ss.count("submit") == 2 and set(found2) == {"sa"} and billed2 == 0.15 and src.details["sa"]["deceased"] is True,
          "a new call orders as before, and the flag is kept beside the numbers")


# ====================================================================== research's reuse
def _research(check, tmp, policy) -> None:
    import dream100_dp as dp
    from .autopilot import ledger
    from .autopilot import skipscreen as sk
    print("  research reuses the answer")
    home = fresh(tmp, "research")
    config.MODE = "live"
    crm = FakeCRM([rec("dp-1", "71 Example St", "Lou", "Sample")])
    _run(policy, crm, make_source(FakeSS({"lou sample": ans(phones=["8655550111"], deceased=True)}), home / "smartskip"),
         mode="live")
    stored = ledger.fact("dp-1", sk.FACT)
    check(isinstance(stored, dict) and stored.get("role") == "owner" and stored.get("first") == "Lou"
          and stored.get("last") == "Sample" and stored.get("order") == "bs-1" and isinstance(stored.get("parsed"), dict),
          said(isinstance(stored, dict), "the stage keeps the answer research reads: role, the name it sent, the order, the answer",
               stored))
    # Stamped with today's clock so the 30 days are counted from now, not from the test's fixed date.
    real_now = datetime.now(timezone.utc)
    ledger.set_fact("dp-1", sk.FACT, dict(stored, at=real_now.isoformat(timespec="seconds")))
    for u, change in (("dp-2", {"role": "decedent"}), ("dp-3", {"at": (real_now - timedelta(days=40)).isoformat(timespec="seconds")}),
                      ("dp-4", {"first": "Mary"})):
        ledger.set_fact(u, sk.FACT, dict(dict(stored, at=real_now.isoformat(timespec="seconds")), **change))

    class Bought(Exception):
        pass

    def no_buying(run):
        raise Bought()

    saved_ss = dp._ss_module
    dp._ss_module = no_buying
    try:
        def case(u, folder):
            run = dp.Run(str(home / folder))
            run.save("records.json", [{"slug": "71-example-st", "title_owner": "",
                                       "record": {"uuid": u, "owner": {"first_name": "Lou", "last_name": "Sample",
                                                                       "address": {"street": "71 Example St", "city": "Knoxville",
                                                                                   "state": "TN", "postal_code": "37917"}},
                                                  "address": {"street": "71 Example St", "city": "Knoxville", "state": "TN",
                                                              "postal_code": "37917"}},
                                       "row": {"uuid": u, "street": "71 Example St", "city": "Knoxville", "zip": "37917"}}])
            try:
                dp.phase_smartskip(types.SimpleNamespace(commit=True), run)
                return run, False
            except Bought:
                return run, True

        run, bought = case("dp-1", "dp_reuse")
        got = run.load("smartskip.json")
        check(not bought and got and got[0]["input_name"] == "Lou Sample" and got[0]["deceased"] is True
              and "order" not in got[0] and run.load("ledger.json", {}).get("smartskip", {}).get("usd") == 0.0,
              said(not bought, "a research run reuses the stage's answer for the same owner, in the parser's shape, and buys nothing",
                   (bought, got)))
        _, bought2 = case("dp-2", "dp_decedent")
        check(bought2, "a probate decedent's trace is never reused as the owner's")
        _, bought3 = case("dp-3", "dp_old")
        check(bought3, "an answer older than 30 days is not reused")
        _, bought4 = case("dp-4", "dp_other")
        check(bought4, "an answer about another person is never reused")
    finally:
        dp._ss_module = saved_ss


# ====================================================================== the backfill
def _backfill(check, tmp, policy) -> None:
    from .autopilot import ledger
    from .autopilot import skipscreen as sk
    print("  the backfill over finished orders")
    home = fresh(tmp, "backfill")
    pol = policy.parse(playbook(), EMPLOYEES)
    recs = [rec("ba", "31 Example St", "Hal", "Sample"),
            rec("bb", "32 Example St", "Ivy", "Demo", lists=("Probate",)),
            rec("bc", "33 Example St", "Jon", "Testa", status="Hot Lead"),
            rec("bd", "34 Example St", "Kai", "Example Jr"),
            rec("be", "35 Example St", "Lin", "Sample"),
            rec("bf", "36 Example St", "Meg", "Demo"),
            rec("bg", "37 Example St", "Nan", "Testa", status="not_interested")]

    def sub(first, last, street, deceased=True, relatives=()):
        row = {"First Name": first, "Middle Name": "", "Last Name": last, "Mailing Address": street, "Mailing City": "Knoxville",
               "Mailing State": "TN", "Mailing Zip": "37917", "Property Address": street, "Property City": "Knoxville",
               "Property State": "TN", "Property Zip": "37917" if street else ""}
        return entry_for(row, ans(phones=["8655550511"], deceased=deceased, relatives=relatives))

    ss = FakeSS({})
    ss.orders = {
        "bs-90": {"paid": True, "status": "completed", "rows": [], "parsed": [
            sub("Hal", "Sample", "31 Example St"), sub("Ivy", "Demo", "32 Example St"), sub("Jon", "Testa", "33 Example St"),
            sub("Zed", "Nobody", "99 Nowhere Rd"), sub("Ann", "Living", "31 Example St", deceased=False), sub("Pip", "Sample", "")]},
        "bs-91": {"paid": True, "status": "completed", "rows": [], "parsed": [
            sub("Kai", "Example Sr", "34 Example St"),
            sub("Lin", "Sample", "35 Example St", relatives=[rel("Lin", "Sample", "Son", "8 Kin Ct", "Knoxville", "TN", "37917")]),
            sub("Hugh", "Demo", "36 Example St"), sub("Hal", "Sample", "31 Example St"), sub("Nan", "Testa", "37 Example St")]},
        "bs-92": {"paid": True, "status": "processing", "rows": []},
    }
    crm = FakeCRM(recs)
    src = make_source(ss, home / "smartskip")
    res = sk.paid_orders(crm=crm, src=src, pol=pol, now=NOW)
    c = res["counts"]
    want = {"orders read": 2, "subjects SmartSkip lists as deceased": 10, "would mark deceased": 1,
            "would hold to verify a death": 1, "left alone: a lead: the lead manager has it": 1,
            "no record at that address, or more than one": 1, "no property address on the order": 1,
            "left alone: a senior and a junior share the name": 1, "left alone: a living relative shares the owner's name": 1,
            "left alone: the person SmartSkip lists is not the owner of record by name": 1,
            "the same record again (decided once)": 1, "left alone: status not_interested": 1}
    check(c == want, said(c == want, "the dry backfill decides every death SmartSkip listed with the stage's own rule", c))
    check(not res["live"] and not crm.writes, said(not crm.writes, "the backfill is dry by default", crm.writes))
    check(ss.count("submit") == 0 and ss.count("pay") == 0, "the backfill never buys anything")
    check(ss.count("download") == 2 and Path(src.order_file("bs-90")).exists(), "each finished order is downloaded once and kept")
    with open(res["csv"], encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    check(len(rows) == 10 and {r["outcome"] for r in rows if r["record"] == "ba"} == {"would mark deceased", "the same record again (decided once)"},
          said(len(rows) == 10, "the CSV says what it would do with each death", len(rows)))
    sk.paid_orders(crm=crm, src=src, pol=pol, now=NOW)
    check(ss.count("download") == 2, "a second run downloads nothing")

    class Down(FakeSS):
        def fetch_status(self, sess, bid=None):
            raise SystemExit("ERROR: status fetch failed (503)")

    down = sk.paid_orders(crm=FakeCRM(recs), src=make_source(Down({}), home / "down"), pol=pol, now=NOW)
    check(not down["counts"] and any(p.startswith("SmartSkip's order list could not be read") for p in down["problems"]),
          said(not down["counts"], "an order list that cannot be read is said and nothing is decided", down))
    sk.paid_orders(commit=True, crm=crm, src=src, pol=pol, now=NOW)
    check(not crm.writes, "--commit alone writes nothing: SKIPSCREEN_ALLOW=1 must be set too")
    os.environ["SKIPSCREEN_ALLOW"] = "1"
    try:
        live = sk.paid_orders(commit=True, crm=crm, src=src, pol=pol, now=NOW)
    finally:
        os.environ.pop("SKIPSCREEN_ALLOW", None)
    lc = live["counts"]
    check(live["live"] and lc.get("marked deceased") == 1 and lc.get("held to verify a death") == 1,
          said(live["live"], "with both switches the backfill marks the owner and holds the probate", lc))
    check(crm.recs["ba"]["owner"]["deceased"] is True and "deceased" in crm.recs["ba"]["tags"]
          and ("ba", "SmartSkip lists Hal Sample as deceased. Research is next; do not call or text the owner.") in crm.msgs,
          "the backfill writes the same marks and note as the stage")
    check("Verify Deceased" in crm.recs["bb"]["tags"] and crm.recs["bb"]["owner"]["deceased"] is False,
          "a probate's representative gets the hold, never the settled marks")
    touched = {w[1] for w in crm.writes if w[0] in ("owner", "add_tags", "remove_tags", "message")}
    check(touched <= {"o-ba", "ba", "bb"}, said(touched <= {"o-ba", "ba", "bb"}, "a lead, a namesake, a junior, a spouse and a finished record are untouched",
                                               touched))
    change = next((x for x in ledger.changes(run_id=live["run"]) if x["what"] == "owner_deceased"), None)
    check(change is not None and change["before"] == {"owner_uuid": "o-ba", "deceased": False}, "the backfill's mark is undoable")
    sk.undo_step(crm, change)
    check(crm.recs["ba"]["owner"]["deceased"] is False, "undo takes the backfill's mark back off")


# ====================================================================== house rules
def _hygiene(check) -> None:
    from .autopilot import skipscreen as sk
    print("  house rules")
    here = Path(__file__).resolve()
    texts = {p.name: p.read_text(encoding="utf-8") for p in (Path(sk.__file__), here,
                                                              here.parent / "autopilot" / "sources.py")}
    bad = [n for n, t in texts.items() if chr(0x2014) in t or chr(0x2013) in t or chr(8) in t]
    check(not bad, said(not bad, "no em dash, en dash or backspace byte in the skip screen's files", bad))
    for p in (sk.LOGIN_PROBLEM, sk.CARD_PROBLEM, sk.PERSON_PROBLEM):
        check(not re.search(r"\d", " ".join(p.split()[:4])), "a problem's first words carry no count: %s" % p[:40])
