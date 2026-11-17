"""Selftest: the calling agency's results written back by Record ID (workflows/agency_results.py) and the monthly
agency scorecard (autopilot/agencyscore.py). Zero network: the files on the thread, DataSift, the PDF reader and
the queue are fakes, the account log is seeded, and every name, street, id and number here is invented (the
865-000 exchange is never assigned).
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from . import config

CHANNEL, THREAD = "C0AGENCY", "1700000600.000100"
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)       # the file is posted on a Tuesday in November
EM, EN = chr(0x2014), chr(0x2013)
HEADER = ["Record ID", "First Name", "Last Name", "Property Address", "Phone Dialed", "Disposition", "Call Date",
          "Callback Date", "Notes", "Agent"]


def U(n: int) -> str:
    return "aaaaaaaa-1111-4000-8000-%012d" % n


def P(n: int) -> str:
    return "8650000%03d" % n


def ph(n, status="UNKNOWN", tier="Dial First", kind="MOBILE"):
    return {"number": P(n), "type": kind, "tags": [tier] if tier else [], "status": status}


def _rec(n, street, first, last, phones=(), status=None, tags=(), lists=(), owner="", company=""):
    return {"uuid": U(n), "status": status, "tags": list(tags), "lists": list(lists),
            "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": owner or "own-%02d" % n, "first_name": first, "last_name": last, "company": company,
                      "deceased": False, "phones": [dict(p) for p in phones]}}


def main_records() -> list[dict]:
    return [_rec(1, "11 Remove Test Rd", "Pat", "Example", [ph(101, "CORRECT")], status="prospecting"),
            _rec(21, "13 Remove Test Rd", "Pat", "Example", [ph(101, "CORRECT")], owner="own-01"),
            _rec(2, "12 Quiet Test Ln", "Lee", "Sample", [ph(111)]),
            _rec(3, "20 Lead Test Way", "Dana", "Testerson", [ph(201)]),
            _rec(4, "21 Warm Test Ct", "Ash", "Kept", [ph(202)], status="Warm Lead"),
            _rec(5, "22 Held Test Dr", "Rae", "Held", [ph(203)], tags=["Do Not Market"]),
            _rec(6, "30 Calm Test St", "Kim", "Nope", [ph(301)], status="prospecting"),
            _rec(7, "31 Busy Test St", "Bo", "Leadful", [ph(302)], status="new_lead"),
            _rec(8, "40 Wrong Test Ave", "Cy", "Wrongly", [ph(401), ph(402, "CORRECT")]),
            _rec(9, "50 Gone Test Pl", "Ola", "Gone", [ph(501)]),
            _rec(10, "60 Sold Test Rd", "Morgan", "Otherly", [ph(601)]),
            _rec(11, "70 Firm Test Blvd", "", "", [ph(701)], company="Test Holdings LLC"),
            _rec(12, "80 Info Test Ln", "Sue", "Info", [ph(121)]),
            _rec(13, "81 Odd Test Ln", "Tom", "Odd", [ph(131)]),
            _rec(14, "82 Later Test Ln", "Val", "Later", [ph(141)]),
            _rec(15, "83 Quiet Test Ln", "Wes", "Quiet", [ph(151)]),
            _rec(17, "90 Mixed Test Rd", "Ann", "Mixed", [ph(171)], status="prospecting"),
            _rec(18, "91 Both Test Rd", "Ben", "Both", [ph(181), ph(182)])]


def main_rows() -> list[list]:
    d = "11/0%d/2026"
    return [[U(1), "Pat", "Example", "11 Remove Test Rd", P(101), "Do Not Call", d % 3, "", "", "Sam"],
            [U(2), "Lee", "Sample", "12 Quiet Test Ln", "(865) 000-0111", "Remove me", d % 4, "", "", "Sam"],
            [U(3), "Dana", "Testerson", "20 Lead Test Way", "865-000-0201", "Hot Lead", d % 5, "11/20/2026",
             "Wants to sell this spring, roof leaks", "Sam"],
            [U(4), "Ash", "Kept", "21 Warm Test Ct", P(202), "Appointment Set", d % 5, "", "", "Jo"],
            [U(5), "Rae", "Held", "22 Held Test Dr", P(203), "Interested", d % 6, "", "", "Jo"],
            [U(6), "Kim", "Nope", "30 Calm Test St", P(301), "Not Interested", d % 6, "", "", "Jo"],
            [U(7), "Bo", "Leadful", "31 Busy Test St", P(302), "NI", d % 6, "", "", "Jo"],
            [U(8), "Cy", "Wrongly", "40 Wrong Test Ave", P(401), "Wrong Number", d % 7, "", "", "Jo"],
            [U(8), "Cy", "Wrongly", "40 Wrong Test Ave", P(402), "Disconnected", d % 7, "", "", "Jo"],
            [U(9), "Ola", "Gone", "50 Gone Test Pl", P(501), "Owner deceased", d % 7, "", "Daughter answered", "Jo"],
            [U(10), "Pat", "Formerly", "60 Sold Test Rd", P(601), "DNC", d % 7, "", "", "Jo"],
            [U(11), "", "", "70 Firm Test Blvd", P(701), "Lead", d % 7, "", "", "Jo"],
            [U(12), "Sue", "Info", "80 Info Test Ln", P(121), "Already sold", d % 7, "", "", "Jo"],
            [U(13), "Tom", "Odd", "81 Odd Test Ln", P(131), "Code 77", d % 7, "", "", "Jo"],
            [U(14), "Val", "Later", "82 Later Test Ln", P(141), "Call Back", d % 7, "11/12/2026", "", "Jo"],
            [U(15), "Wes", "Quiet", "83 Quiet Test Ln", P(151), "Left Voicemail", d % 7, "", "", "Jo"],
            [U(16), "Xa", "Missing", "84 Missing Test Ln", P(161), "Lead", d % 7, "", "", "Jo"],
            ["12 Test St", "Yo", "Bad", "12 Test St", P(191), "Wrong Number", d % 7, "", "", "Jo"],
            ["", "Zed", "Blank", "20 Lead Test Way", P(192), "Do Not Call", d % 7, "", "", "Jo"],
            [U(17), "Ann", "Mixed", "90 Mixed Test Rd", P(171), "Not Interested", d % 7, "", "", "Jo"],
            [U(17), "Ann", "Mixed", "90 Mixed Test Rd", P(171), "Wrong Number", d % 8, "", "", "Jo"],
            [U(18), "Ben", "Both", "91 Both Test Rd", P(181), "Do Not Call", d % 8, "", "", "Jo"],
            [U(18), "Ben", "Both", "91 Both Test Rd", P(182), "Lead", d % 8, "", "", "Jo"]]


def csv_bytes(rows: list[list], header=HEADER, title=True) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    if title:
        w.writerow(["Agency results export"])
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------- fakes
class NotFound(Exception):
    code = 404


class FakeCRM:
    """Reads and the writes the workflow may make. Any other call (an owner change, an assignment, a mail
    piece) fails the test, which is how 'nothing else is touched' is proven."""

    def __init__(self, recs, tags=("Do Not Market", "Agency Lead", "Verify Deceased"), lists=("DNC", "Agency")):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in recs}
        self.tag_set = {t.lower() for t in tags}
        self.list_set = {t.lower() for t in lists}
        self.writes, self.reads, self.notes = [], [], []
        self.stick = True
        self.exhaust_after = None

    def record(self, u):
        self.reads.append(u)
        if u not in self.recs:
            raise NotFound("404")
        return copy.deepcopy(self.recs[u])

    def tag_uuid(self, t):
        return ("tag-" + t) if t.lower() in self.tag_set else ""

    def list_uuid(self, t):
        return ("list-" + t) if t.lower() in self.list_set else ""

    def owner_records(self, owner_uuid, cap=250):
        return [{"uuid": u} for u, r in self.recs.items() if r["owner"]["uuid"] == owner_uuid][:cap]

    def _w(self, *a):
        from .autopilot import budget
        if self.exhaust_after is not None and len(self.writes) >= self.exhaust_after:
            raise budget.BudgetExhausted("the hour's share of the account's requests is used")
        self.writes.append(a)

    def upsert_phones(self, owner_uuid, phones):
        self._w("phones", owner_uuid, tuple((p["number"], p["status"]) for p in phones))
        for r in self.recs.values():
            if r["owner"]["uuid"] == owner_uuid:
                have = {p["number"]: p for p in r["owner"]["phones"]}
                for p in phones:
                    have[p["number"]] = dict(have.get(p["number"], {}), **p)
                r["owner"]["phones"] = list(have.values())

    def set_status(self, u, s):
        self._w("status", u, s)
        if self.stick:
            self.recs[u]["status"] = s

    def add_tags(self, u, add, rec=None):
        self._w("add_tags", u, tuple(add))
        for t in add:
            if t not in self.recs[u]["tags"]:
                self.recs[u]["tags"].append(t)

    def remove_tags(self, u, drop):
        self._w("remove_tags", u, tuple(drop))
        self.recs[u]["tags"] = [t for t in self.recs[u]["tags"] if t not in drop]

    def add_lists(self, u, add):
        self._w("add_lists", u, tuple(add))
        for t in add:
            if t not in self.recs[u]["lists"]:
                self.recs[u]["lists"].append(t)

    def remove_lists(self, u, drop):
        self._w("remove_lists", u, tuple(drop))
        self.recs[u]["lists"] = [t for t in self.recs[u]["lists"] if t not in drop]

    def message(self, u, text, pin=False):
        self._w("note", u)
        self.notes.append((u, text))
        return ""

    def __getattr__(self, name):
        raise AssertionError("agency results called %s on DataSift" % name)


class NoCRM:
    def __getattr__(self, name):
        raise AssertionError("this path must not reach DataSift (it called %s)" % name)


class Queue:
    """store.enqueue's stand-in: remembers each continuation the workflow queues."""

    def __init__(self):
        self.items = []

    def __call__(self, agent, workflow, record_uuid="", subject="", **kw):
        tid = 9000 + len(self.items)
        self.items.append(dict(kw, id=tid, agent=agent, workflow=workflow))
        return tid, True

    def task(self, i=-1) -> dict:
        q = self.items[i]
        return {"id": q["id"], "agent": q["agent"], "workflow": q["workflow"], "payload": json.dumps(q["payload"]),
                "slack_channel": q.get("slack_channel") or "", "slack_thread": q.get("slack_thread") or "",
                "thread_id": q.get("thread_id") or "", "hop": 0, "source": q.get("source") or ""}


def _writes_on(fake: FakeCRM, u: str) -> list:
    return [w for w in fake.writes if len(w) > 1 and w[1] == u]


def _phone(fake: FakeCRM, u: str, n: int) -> str:
    return next((p["status"] for p in fake.recs[u]["owner"]["phones"] if p["number"] == P(n)), "")


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import store
    from .workflows import agency_results as ar
    keep = (config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE)
    hooks = (ar.FILES, ar.FETCH, ar.CRM, ar.NOW, ar.ENQUEUE, ar.PDF_TEXT, ar.POLICY)
    env_saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTS_AGENCY_RE")}
    for k in env_saved:
        os.environ.pop(k, None)
    config.DATA_DIR = Path(tmp) / "agencyreturn"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.RUNS_DIR = config.DATA_DIR / "runs"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        from .autopilot import gate
        ar.NOW, ar.POLICY = NOW, SimpleNamespace(gate=gate.GateConfig(), tz="America/New_York")
        _reading_checks(check, ar, tmp)
        _outcome_checks(check, ar)
        fake = _main_checks(check, ar, store)
        _go_checks(check, ar, store)
        _guard_checks(check, ar, store)
        _pdf_checks(check, ar, store)
        _refusal_checks(check, ar, store)
        _score_checks(check, ar, store, fake)
        _hygiene_checks(check)
    finally:
        (ar.FILES, ar.FETCH, ar.CRM, ar.NOW, ar.ENQUEUE, ar.PDF_TEXT, ar.POLICY) = hooks
        config.DATA_DIR, config.DB_PATH, config.RUNS_DIR, config.MODE = keep
        for k in [k for k in os.environ if k.startswith("AGENTS_AGENCY_RE")]:
            os.environ.pop(k, None)
        os.environ.update(env_saved)


def _task(inputs=None, thread=THREAD) -> dict:
    from . import store
    tid, _ = store.enqueue("ops", "agency_results", "", subject="", force=True,
                           payload={"inputs": inputs or {}, "details": ""}, source="agent:ops",
                           slack_channel=CHANNEL, slack_thread=thread)
    return store.task(tid)


def _post(ar, name: str, data: bytes) -> None:
    ar.FILES = lambda ch, th, _n=name: [{"id": "F-" + _n, "name": _n, "kind": "text", "url": "https://files.example/x",
                                         "needs_info": False}]
    ar.FETCH = lambda f, _d=data: _d


def _env(**kw) -> None:
    for k in [k for k in os.environ if k.startswith("AGENTS_AGENCY_RE")]:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ["AGENTS_AGENCY_RESULTS_" + k.upper()] = str(v)


# ---------------------------------------------------------------------- reading the file
def _reading_checks(check, ar, tmp: Path) -> None:
    print("  reading the agency's file")
    c = ar.classify
    check(c("Do Not Call") == c("DNC") == c("remove me") == c("Not interested - take me off the list") == "remove",
          "every way of asking to be removed is a removal, and a removal outranks a no in the same words")
    check(c("Not Interested") == "not_interested" and c("Hot Lead") == "lead" and c("Interested in an offer") == "lead",
          "not interested is a no, and interested alone is a lead")
    check(c("not a lead") == "unknown" and c("Unqualified") == "unknown",
          "a negated lead word is never read as a lead: it goes to a person")
    check(c("Left Voicemail") == "no_contact" and c("Call Back") == "callback" and c("") == "blank",
          "voicemail, a callback and an empty cell write nothing")
    check(c("Owner passed away last year") == "deceased" and c("Disconnected") == "dead" and c("Wrong Number") == "wrong",
          "a death, a dead line and a wrong number are each their own class")
    check(c("Code 77") == "unknown" and c("Already sold") == "person",
          "a code the table does not know is unknown, never guessed; sold is known but for a person")
    mapped, bad = ar.codes('{"NIX": "not_interested", "ZZ": "nonsense"}')
    check(mapped == {"nix": "not_interested"} and bad and "ZZ" in bad[0] and c("nix", mapped) == "not_interested",
          "an agency's own codes map onto the classes, and a code mapped to no class is said, not used")
    check(ar.as_day("11/03/2026 2:15 PM") == ar.as_day("2026-11-03T14:22") == ar.as_day("Nov 3, 2026") == "2026-11-03"
          and ar.as_day("someday") == "", "a call date reads in the shapes dialers export, and nonsense reads as no date")
    # the header is found under a title row, and the file's own columns are matched by name
    table = ar.tables_in(_tmp_file(tmp, "a.csv", csv_bytes(main_rows()[:3])))[0][1]
    hi, cols, why = ar.header_of(table)
    check(hi == 1 and "record" in cols and "disposition" in cols and "phone" in cols and not why,
          "the header row is found under a title line, and Record ID, disposition and number are matched by name")
    _, _, why = ar.header_of([["Owner", "Street", "Phone", "Disposition"], ["Pat", "1 Test Rd", P(1), "DNC"]])
    check("no Record ID column" in why and "never by address or name" in why,
          "a file with no Record ID column is refused, and the answer says rows are matched by Record ID only")
    _, _, why = ar.header_of([["Record ID", "Street"], [U(1), "1 Test Rd"]])
    check("no disposition column" in why, "a file with no disposition column is refused")
    # an Excel workbook: the first sheet has no table, the second is our own export with a disposition per number
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "Summary"
    wb.active.append(["October calls", 1200])
    ws = wb.create_sheet("Results")
    ws.append(["Record ID", "First Name", "Last Name", "Phone 1", "Phone 1 Type", "Phone 1 Disposition", "Phone 2",
               "Phone 2 Disposition"])
    ws.append([U(60), "Ida", "Wide", 8650000611, "Mobile", "Wrong Number", 8650000612, "Not Interested"])
    buf = io.BytesIO()
    wb.save(buf)
    rows, why = ar.parse_file({"name": "results.xlsx", "path": _tmp_file(tmp, "r.xlsx", buf.getvalue())}, {})
    check(not why and [(r["phone"], r["class"]) for r in rows] == [(P(611), "wrong"), (P(612), "not_interested")],
          "a workbook's sheets are tried in turn, and a disposition beside each Phone N is one row per number "
          "(a phone Excel stored as a number still reads as ten digits)")


def _tmp_file(tmp: Path, name: str, data: bytes) -> Path:
    p = Path(tmp) / "agencyreturn" / "in" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _row(cls, phone="", reached=None, **kw):
    return dict({"row": 3, "file": "f.csv", "disposition": cls, "class": cls, "phone": phone, "notes": "",
                 "callback": "", "call_date": "2026-11-03", "reached": reached, "last": "", "address": "", "agent": ""}, **kw)


def _outcome_checks(check, ar) -> None:
    print("  one house, one outcome")
    o = ar.outcome([_row("lead", P(1)), _row("remove", P(2))])
    check(o["top"] == "remove" and o["phones"] == {P(1): "correct", P(2): "dnc"}
          and any("only the removal was written" in p for p in o["person"]),
          "a removal outranks a lead on the same house; the lead goes to a person")
    o = ar.outcome([_row("not_interested", P(3)), _row("wrong", P(3))])
    check(o["phones"] == {P(3): "conflict"} and o["hold"] and any("ending 0003" in p for p in o["person"]),
          "a number one call reached and another called wrong is written for nobody, and the house gets no status")
    o = ar.outcome([_row("not_interested", P(4), reached=False)])
    check(o["top"] == "not_interested" and o["phones"] == {},
          "a not interested the file says came from someone other than the owner does not mark the number correct")
    check(ar.outcome([_row("not_interested", P(5))])["phones"] == {P(5): "correct"},
          "a not interested reached the owner, so its number is marked correct (L10.19)")
    check(ar.outcome([_row("not_interested", P(5))], ni_correct=False)["phones"] == {},
          "with AGENTS_AGENCY_RESULTS_NI_CORRECT off, a not interested leaves the number as it was")
    o = ar.outcome([_row("deceased", P(6)), _row("not_interested", P(7))])
    check(o["top"] == "deceased" and o["phones"] == {},
          "when the owner is said to have died, a number that reached the family is not marked as the owner's")
    o = ar.outcome([_row("wrong", P(8)), _row("dead", P(9)), _row("no_contact", P(10))])
    check(o["top"] == "wrong" and o["phones"] == {P(8): "wrong", P(9): "dead"},
          "wrong and dead numbers are phone writes only, and a voicemail writes nothing (L10.20, L10.32)")


# ---------------------------------------------------------------------- the main file: dry, then live
def _main_checks(check, ar, store) -> FakeCRM:
    from .autopilot import gate, ledger, undo
    print("  the agency's file, dry")
    for n in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 18]:
        ledger.set_fact(U(n), ar.FACT_SENT, "2026-11-01")       # every house but U(2) was in a file we sent
    fake = FakeCRM(main_records())
    ar.CRM, ar.ENQUEUE = fake, Queue()
    data = csv_bytes(main_rows())
    _post(ar, "November results.csv", data)
    _env(max_records=3)
    config.MODE = "dry"
    r = ar.run(_task())
    text = ar.render(_task(), r)
    check(r["ok"] and not r["live"] and fake.writes == [] and not ar.ENQUEUE.items,
          "a dry box writes nothing to DataSift and queues no background run")
    check(fake.reads == [U(1), U(2), U(10)],
          "a run reads at most AGENTS_AGENCY_RESULTS_MAX_RECORDS records, removals first")
    check("dry run, nothing written" in text and "Read 3 of the 14 records with something to write" in text
          and "would write: 2 removals" in text,
          "the dry answer says how many it read and what a live run would write on them")
    check(r["month"] == "2026-11" and r["month_from"] == "the call dates",
          "the month the agency called in comes from the file's own call dates")
    ret = ar.load_return(r["rid"])
    check(ret and ret["kind"] == "records" and len(ret["records"]) == 18 and len(ret["unmatched"]) == 2,
          "every parsed file is kept under the brain's data folder, with its records and its unmatched rows")
    leak = [x for x in (P(101), P(201), "000-0201", "Testerson", "Example", "roof leaks", EM, EN) if x in text]
    check(not leak, "the answer in the thread carries no phone number, no owner name and none of the agency's notes")
    folder = Path(r["run_dir"]) / "files"
    person = (folder / ar.CSV_PERSON).read_text(encoding="utf-8")
    check("not a DataSift record id" in person and "no Record ID" in person and P(191) not in person
          and P(192) not in person and ",0191," in person,
          "rows with no usable Record ID go to the for-a-person file, with the number's last four only")

    print("  the agency's file, live")
    config.MODE = "live"
    _env(max_records=50, ask_records=100)
    r = ar.run(_task())
    text = ar.render(_task(), r)
    check(r["ok"] and r["live"] and r["done"] and not ar.ENQUEUE.items,
          "a file that fits in one run is written in that run, and no background run is queued")
    check(_phone(fake, U(1), 101) == "CORRECT_DNC" and _phone(fake, U(2), 111) == "DNC",
          "a removal marks the number dialed do not call, keeping a number already judged correct as CORRECT_DNC")
    for u in (U(1), U(21), U(2), U(18)):
        rec = fake.recs[u]
        check("Do Not Market" in rec["tags"] and "DNC" in rec["lists"],
              "a removal tags %s Do Not Market and puts it on the DNC list" % rec["address"]["street"])
    v = gate.check(fake.recs[U(1)], "text")
    check(not v.ok and v.code == "do_not_market" and gate.check(fake.recs[U(21)], "mail").code == "do_not_market",
          "an owner who asked the agency to be removed is held by the gate in every lane, the owner's other house too")
    check(any(u == U(1) and "asked to remove this owner" in t and "Do Not Market" in t for u, t in fake.notes),
          "a removal leaves a note saying why on the record")
    rec3 = fake.recs[U(3)]
    note3 = next((t for u, t in fake.notes if u == U(3)), "")
    check(rec3["status"] == "new_lead" and "Agency Lead" in rec3["tags"] and _phone(fake, U(3), 201) == "CORRECT"
          and "Wants to sell this spring" in note3 and "Callback set for 2026-11-20" in note3,
          "a lead becomes New Lead with the tag Agency Lead, its number marked correct and a note with the agency's notes")
    check(fake.recs[U(4)]["status"] == "Warm Lead" and "Agency Lead" in fake.recs[U(4)]["tags"]
          and any(u == U(4) and "already a lead" in t for u, t in fake.notes),
          "a lead a person is already working keeps its status; it gets the tag and a note")
    check(_writes_on(fake, U(5)) == [] and _writes_on(fake, U(11)) == [],
          "no lead is written on a record tagged Do Not Market, nor on one a company owns (people only)")
    check(fake.recs[U(6)]["status"] == "not_interested" and _phone(fake, U(6), 301) == "CORRECT",
          "not interested becomes Not Interested over a prospecting status, its number marked correct")
    check(fake.recs[U(7)]["status"] == "new_lead", "not interested never overwrites a lead a person is working")
    check(_phone(fake, U(8), 401) == "WRONG" and _phone(fake, U(8), 402) == "CORRECT" and fake.recs[U(8)]["status"] is None,
          "a wrong number marks only that number, and never a number a person marked correct")
    check("Verify Deceased" in fake.recs[U(9)]["tags"] and fake.recs[U(9)]["owner"]["deceased"] is False
          and any(u == U(9) and "Nobody is marked dead" in t for u, t in fake.notes),
          "a death the agency was told about tags Verify Deceased and never marks the owner dead")
    check(_phone(fake, U(10), 601) == "DNC" and "Do Not Market" not in fake.recs[U(10)]["tags"],
          "when the owner changed since the file was sent, the number is marked but the new owner is not tagged")
    check(P(601) not in [x["number"] for x in gate.check(fake.recs[U(10)], "call").numbers],
          "the number the person asked about is no longer a number any call may use")
    check(all(_writes_on(fake, U(n)) == [] for n in (12, 13, 14, 15, 17)),
          "sold, an unknown code, a callback, a voicemail and a house whose calls disagree write nothing")
    check(fake.recs[U(18)]["status"] is None and "Do Not Market" in fake.recs[U(18)]["tags"],
          "a house that asked to be removed on one call and passed a lead on another gets the removal, never New Lead")
    check("Do Not Market" not in fake.recs[U(3)]["tags"],
          "a row with no Record ID is never matched by its address, so the house on that street is not touched")
    ret = ar.load_return(r["rid"])
    check(ret["records"][U(16)]["state"] == "unmatched", "a Record ID the account no longer holds goes to a person")
    changes = ledger.changes(run_id=r["run_id"])
    check(r["run_id"] == "agency_results-" + r["rid"] and len(changes) >= 15
          and {c["what"] for c in changes} >= {"phones", "status", "add_tags", "add_lists"},
          "every change is recorded under one run id for the whole file, with the value it replaced")
    check("Wrote 10 records now" in text and "autopilot undo %s" % r["run_id"] in text
          and "Leads now in DataSift: 20 Lead Test Way, Knoxville; 21 Warm Test Ct, Knoxville." in text
          and "3 leads not written (for a person, in the file)" in text,
          "the live answer says what was written, names the leads by street and says how to put the writes back")
    check("1 record read was never in a file the autopilot knows it sent" in text,
          "a record the autopilot has no memory of sending is said, so a stranger's file would show")
    check("Needs a person: 12 (2 with a lead the gate holds, 2 with no usable Record ID, " in text and ", and 6 more)" in text,
          "what needs a person is counted by its reason, in words that read with any count, the biggest first")
    person = (Path(r["run_dir"]) / "files" / ar.CSV_PERSON).read_text(encoding="utf-8")
    check("the owner on this record has changed since the file was sent" in person
          and "the agency passed a lead, but the record is tagged Do Not Market" in person
          and "a disposition I do not know: Code 77" in person and "one call asked to be removed and another passed a lead" in person,
          "every reason a person must look is in the for-a-person file, one line each")
    check(not [x for x in (P(101), P(201), "Testerson", "roof leaks", EM, EN) if x in text],
          "the live answer carries no phone number, no owner name and none of the agency's notes")
    everything = (Path(r["run_dir"]) / "files" / ar.CSV_ALL).read_text(encoding="utf-8")
    check(U(3) in everything and "status new_lead" in everything and P(201) not in everything,
          "every record and what was written is in a file, with no full phone number")

    print("  the same answer is never written twice")
    before = len(fake.writes)
    r2 = ar.run(_task())
    check(r2.get("already") and len(fake.writes) == before and "nothing was written twice" in ar.render(_task(), r2),
          "the same file posted again writes nothing")
    fixed = main_rows() + [[U(19), "Ivy", "Extra", "92 Extra Test Rd", P(195), "No Answer", "11/08/2026", "", "", "Jo"]]
    _post(ar, "November results v2.csv", csv_bytes(fixed))
    r3 = ar.run(_task())
    ret3 = ar.load_return(r3["rid"])
    check(r3["rid"] != r["rid"] and len(fake.writes) == before and ret3["records"][U(3)]["state"] == "already",
          "a corrected file with the same answers writes none of them again")

    print("  the file can be put back")
    done = undo.run(r["run_id"], crm=fake, commit=True)
    check(done.get("undone", 0) >= 15 and not done.get("failed"), "undo replays the file's changes")
    check(fake.recs[U(3)]["status"] is None and "Agency Lead" not in fake.recs[U(3)]["tags"]
          and _phone(fake, U(1), 101) == "CORRECT" and "Do Not Market" not in fake.recs[U(21)]["tags"]
          and "DNC" not in fake.recs[U(21)]["lists"],
          "undo puts every status, tag, list and number back the way it was")
    check("Do Not Market" in fake.recs[U(5)]["tags"] and fake.recs[U(4)]["status"] == "Warm Lead",
          "undo never removes a tag the record carried before the file, nor touches what the file did not change")
    return fake


# ---------------------------------------------------------------------- a large file: removals now, the rest after a go
def _go_checks(check, ar, store) -> None:
    print("  a large file waits for a go, removals first")
    recs = [_rec(n, "%d Big Test Rd" % n, "Al", "Big%d" % n, [ph(n)], status="prospecting") for n in range(30, 38)]
    fake = FakeCRM(recs)
    q = Queue()
    ar.CRM, ar.ENQUEUE = fake, q
    rows = [[U(n), "Al", "Big%d" % n, "%d Big Test Rd" % n, P(n), "Do Not Call" if n < 33 else "Not Interested",
             "06/02/2026", "", "", "Jo"] for n in range(30, 38)]
    _post(ar, "June results.csv", csv_bytes(rows))
    config.MODE = "live"
    _env(max_records=2, ask_records=2)
    r = ar.run(_task())
    text = ar.render(_task(), r)
    check(r["need_go"] and [w[1] for w in fake.writes if w[0] == "add_tags"] == [U(30), U(31)],
          "a file over AGENTS_AGENCY_RESULTS_ASK_RECORDS writes its removals first, without waiting for a go")
    check("Say go" in text and "Removals still to write, in the background first: 1" in text and len(q.items) == 1,
          "the answer asks for a go, and the last removal goes on in the background")
    check("2 records read were never in a file" in text, "a count of records reads as plain English in the plural too")
    gap = datetime.fromisoformat(q.items[0]["not_before"]) - datetime.now(timezone.utc)
    check(timedelta(seconds=30) < gap <= timedelta(seconds=61), "the next run waits AGENTS_AGENCY_RESULTS_GAP_S")
    c1 = ar.run(q.task(0))
    t1 = ar.render(q.task(0), c1)
    check(fake.recs[U(32)]["tags"] == ["Do Not Market"] and c1.get("paused") and "Say go" in t1 and len(q.items) == 1,
          "once the removals are all written the background stops, says so once, and waits for the go")
    check(all(fake.recs[U(n)]["status"] == "prospecting" for n in range(33, 38)),
          "nothing but removals is written before a person says go")
    stale = ar.run(q.task(0))
    check(stale.get("quiet") and ar.render(q.task(0), stale) == "", "a background run the file no longer expects does nothing")
    r = ar.run(_task({"go": "go"}))
    check(not r["need_go"] and fake.recs[U(33)]["status"] == "not_interested" and len(q.items) == 2,
          "after a go the rest is written, the next records in the background")
    c2 = ar.run(q.task(1))
    check(c2.get("quiet") and ar.render(q.task(1), c2) == "" and len(q.items) == 3,
          "a background run in the middle of a file posts nothing")
    c3 = ar.run(q.task(2))
    t3 = ar.render(q.task(2), c3)
    check(c3.get("done") and "finished" in t3 and "Written: 8 records over 5 runs" in t3 and "autopilot undo" in t3,
          "the last background run posts the whole file's result once, every run counted, with how to put it back")


# ---------------------------------------------------------------------- the guards on a live write
def _guard_checks(check, ar, store) -> None:
    print("  read back, the request budget, a dry box and a missing tag")
    config.MODE = "live"
    _env(max_records=10)
    fake = FakeCRM([_rec(40, "40 Stuck Test Rd", "Ed", "Stuck", [ph(40)], status="prospecting"),
                    _rec(41, "41 Stuck Test Rd", "Fay", "Stuck", [ph(41)], status="prospecting")])
    fake.stick = False
    q = Queue()
    ar.CRM, ar.ENQUEUE = fake, q
    rows = [[U(n), "Ed", "Stuck", "%d Stuck Test Rd" % n, P(n), "Not Interested", "06/03/2026", "", "", "Jo"] for n in (40, 41)]
    _post(ar, "stuck.csv", csv_bytes(rows))
    r = ar.run(_task())
    check(r["stopped"] and "did not read back" in ar.render(_task(), r) and _writes_on(fake, U(41)) == [] and not q.items,
          "a write that does not read back on the first record stops the file: nothing more is written or queued")

    fake = FakeCRM([_rec(45, "45 Busy Test Rd", "Gus", "Busy", [ph(45)]), _rec(46, "46 Busy Test Rd", "Hal", "Busy", [ph(46)])])
    fake.exhaust_after = 0
    ar.CRM = fake
    rows = [[U(n), "Gus", "Busy", "%d Busy Test Rd" % n, P(n), "Lead", "06/04/2026", "", "", "Jo"] for n in (45, 46)]
    _post(ar, "busy.csv", csv_bytes(rows))
    r = ar.run(_task())
    ret = ar.load_return(r["rid"])
    gap = datetime.fromisoformat(q.items[-1]["not_before"]) - datetime.now(timezone.utc)
    check(r["ok"] and ret["records"][U(45)]["state"] == "pending" and gap > timedelta(seconds=800)
          and "continues in 15 minutes" in ar.render(_task(), r),
          "when the hour's request share runs out the file stops, keeps its place and goes on in 15 minutes")
    config.MODE = "dry"
    d = ar.run(q.task(-1))
    check("The box is dry now" in ar.render(q.task(-1), d) and fake.writes == [],
          "a background run on a box that went dry writes nothing and says so")

    config.MODE = "live"
    fake = FakeCRM([_rec(50, "50 Tagless Test Rd", "Ida", "Tagless", [ph(50)])], tags=("Do Not Market", "Verify Deceased"))
    ar.CRM = fake
    _post(ar, "tagless.csv", csv_bytes([[U(50), "Ida", "Tagless", "50 Tagless Test Rd", P(50), "Lead", "06/05/2026",
                                          "", "", "Jo"]]))
    r = ar.run(_task())
    text = ar.render(_task(), r)
    check(fake.recs[U(50)]["status"] == "new_lead" and fake.recs[U(50)]["tags"] == []
          and "the tag Agency Lead does not exist in this account yet" in text,
          "a tag the account lacks is left off and named for a person to create; the status and note still go on")

    from .autopilot import gate
    fake = FakeCRM([_rec(51, "51 Listless Test Rd", "Jan", "Listless", [ph(51)]),
                    _rec(52, "52 Firm Test Rd", "", "", [ph(52)], company="Sample Rentals LLC")],
                   tags=("Agency Lead", "Verify Deceased"))
    ar.CRM = fake
    _post(ar, "stops.csv", csv_bytes([[U(51), "Jan", "Listless", "51 Listless Test Rd", P(51), "Remove me", "06/06/2026", "", "", "Jo"],
                                      [U(52), "", "", "52 Firm Test Rd", P(52), "Do Not Call", "06/06/2026", "", "", "Jo"]]))
    r = ar.run(_task())
    v = gate.check(fake.recs[U(51)], "text")
    check("DNC" in fake.recs[U(51)]["lists"] and not v.ok and v.code == "opted_out"
          and "the tag Do Not Market does not exist" in ar.render(_task(), r),
          "with no Do Not Market tag in the account a removal still reaches the gate through the DNC list, and says so")
    check(_phone(fake, U(52), 52) == "DNC" and "DNC" in fake.recs[U(52)]["lists"],
          "a removal is written on a record a company owns too: people only never stops a stop")


# ---------------------------------------------------------------------- only the PDF came back
PDF_PAGES = ["Calling report\nOctober 2026\nDials\n1,200\nLeads 8\nDials to connect\n150\nConnection rate 70.5%\n"
             "Total spend $2.995\nThank you for marching with us"]


def _pdf_checks(check, ar, store) -> None:
    print("  only the agency's summary PDF came back")
    ar.CRM = NoCRM()
    ar.PDF_TEXT = lambda path: list(PDF_PAGES)
    _post(ar, "agency_report.pdf", b"%PDF-1.4 invented")
    config.MODE = "live"
    _env()
    r = ar.run(_task())
    text = ar.render(_task(), r)
    labels = {x["label"]: x for x in r.get("numbers") or []}
    check(r["kind"] == "pdf" and labels.get("Dials", {}).get("value") == 1200 and labels.get("Leads", {}).get("value") == 8
          and labels.get("Connection rate", {}).get("kind") == "percent",
          "the PDF's numbers are read with their labels, a number on a line of its own paired with its label")
    check(labels.get("Total spend", {}).get("value") == 2995, "a dollar figure printed with a European separator reads right")
    check(r["month"] == "2026-10" and r["month_from"] == "the month named in the file",
          "the month comes from the month the PDF names, and a word like marching is not read as March")
    check("is dials per lead: 1,200 over 8" in text and "it is not live conversations" in text,
          "a ratio label is checked against the agency's own counts, and a connect count is said not to be conversations")
    check("Nothing was written: the PDF has no record ids" in text and "Ask the agency for one row for every number dialed" in text,
          "with only the PDF, the answer says plainly that no removal can be honored and what to ask the agency for")
    check("Total spend $2,995" in text and "$2.995" not in text and "Connection rate 70.5%" in text,
          "the agency's numbers are shown cleaned and rounded, never as the PDF printed them")
    _env(ask="the per-record file with our Record ID on every row")
    text = ar.render(_task(), ar.run(_task()))
    check("Ask the agency for the per-record file with our Record ID on every row." in text,
          "what to ask the agency for is a setting (AGENTS_AGENCY_RESULTS_ASK)")
    _env()
    ar.PDF_TEXT = None


# ---------------------------------------------------------------------- refusals
def _refusal_checks(check, ar, store) -> None:
    print("  what the workflow refuses")
    fake = FakeCRM([])
    ar.CRM = fake
    ar.FILES = lambda ch, th: [{"id": "F-p", "name": "walk.jpg", "kind": "photo", "url": "x"}]
    r = ar.run(_task())
    check(not r["ok"] and "No CSV, Excel or PDF" in ar.render(_task(), r), "a thread with no agency file says what to post")
    _post(ar, "no ids.csv", csv_bytes([["Pat", "1 Test Rd", P(1), "DNC"]], header=["Owner", "Street", "Phone", "Disposition"]))
    r = ar.run(_task())
    text = ar.render(_task(), r)
    check(not r["ok"] and "no Record ID column" in text and fake.reads == [] and fake.writes == [],
          "a file with no Record ID is refused before any record is read")
    r = ar.run(_task({"month": "October"}))
    check("written like 2026-10" in ar.render(_task(), r), "a month that is not written YYYY-MM is refused")
    r = ar.run(_task({"files": "missing.csv"}))
    check("No file named missing.csv" in ar.render(_task(), r), "a file name the thread does not hold is refused")
    ar.FETCH = lambda f: (_ for _ in ()).throw(OSError("gone"))
    r = ar.run(_task())
    check(not r["ok"] and "could not be saved" in ar.render(_task(), r), "a file Slack will not hand over is said, not crashed on")


# ---------------------------------------------------------------------- the monthly scorecard
def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ctx(params: dict, now: datetime):
    from .autopilot import gate, policy as policy_mod, stages
    st = policy_mod.Stage(id="agency_scorecard", kind="agencyscore", owner="ops", mode="plan", summary="",
                          schedule=["8th 08:10"], presets=[], params=dict({"sync": False}, **params))
    pol = SimpleNamespace(tz="America/New_York", gate=gate.GateConfig(), stages={st.id: st})
    out = stages.Summary(stage=st.id, kind=st.kind, owner="ops", mode="plan", live=False)
    return stages.Ctx(policy=pol, stage=st, crm=NoCRM(), run_id="score", now=now, live=False, out=out)


def _score_checks(check, ar, store, fake) -> None:
    from .autopilot import activity, agencyscore, ledger
    print("  the monthly scorecard")
    bid = ledger.batch_create("agency_export", "agency", [{"record_uuid": U(1)}], 0.0)
    ledger.batch_set(bid, "sent", {"owners": 400, "records": 410})
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_batches SET released=? WHERE id=?", ("2026-11-02T14:00:00+00:00", bid))
    day = lambda d, h=15: datetime(2026, 11, d, h, 0, tzinfo=timezone.utc)   # noqa: E731
    activity._store([
        {"id": "s1", "timestamp": _ts(day(12)), "event_type": "owner.call.made", "resource": "own-ih1",
         "payload": {"call": {"direction": "outbound", "destination_number": P(990), "origin_number": P(999)}}},
        {"id": "s2", "timestamp": _ts(day(12, 16)), "event_type": "owner.call.made", "resource": "own-ih1",
         "payload": {"call": {"direction": "outbound", "destination_number": P(990), "origin_number": P(999)}}},
        {"id": "s3", "timestamp": _ts(day(12, 17)), "event_type": "owner.call.answered", "resource": "own-ih1",
         "payload": {"call": {"direction": "outbound", "destination_number": P(990), "origin_number": P(999),
                              "duration": 120, "user": {"first_name": "Jo"}}}},
        {"id": "s4", "timestamp": _ts(day(13)), "event_type": "property.status.updated", "resource": "rec-ih1",
         "payload": {"property": {"status": ["prospecting", "new_lead"]}}},
        {"id": "s5", "timestamp": _ts(day(13)), "event_type": "property.status.updated", "resource": U(3),
         "payload": {"property": {"status": [None, "new_lead"]}}},
        {"id": "s6", "timestamp": _ts(day(14)), "event_type": "property.status.updated", "resource": "rec-ih2",
         "payload": {"property": {"status": ["prospecting", "not_interested"]}}},
        {"id": "s7", "timestamp": _ts(day(14)), "event_type": "owner.phone.status.updated", "resource": "own-ih1",
         "payload": {"owner": {"phone": P(990), "status": ["UNKNOWN", "WRONG"]}}},
        {"id": "s8", "timestamp": _ts(day(14)), "event_type": "owner.phone.status.updated", "resource": "own-ih1",
         "payload": {"owner": {"phone": P(991), "status": ["UNKNOWN", "DNC"]}}},
        {"id": "s9", "timestamp": _ts(day(14)), "event_type": "owner.phone.status.updated", "resource": "own-08",
         "payload": {"owner": {"phone": P(401), "status": ["UNKNOWN", "WRONG"]}}},
        {"id": "s10", "timestamp": _ts(day(15)), "event_type": "task.created", "resource": "t-walk",
         "payload": {"task": {"assigned_to_property": U(3), "event_type": "appointment", "title": "Walkthrough"}}},
        {"id": "s11", "timestamp": _ts(day(18)), "event_type": "task.completed", "resource": "t-walk",
         "payload": {"task": {"assigned_to_property": U(3), "event_type": "appointment", "title": "Walkthrough"}}},
    ])
    now = datetime(2026, 12, 8, 13, 10, tzinfo=timezone.utc)
    ctx = _ctx({"monthly_fee": 4000, "sales_test_min_leads": 1}, now)
    agencyscore.run_agencyscore(ctx)
    out = ctx.out
    lines = "\n".join(out.lines)
    check(out.head.startswith("Agency, November 2026: 5 leads, 3 not interested and 4 asked to be removed, from 400 owners sent")
          and "$800.00 a lead" in out.head,
          "the head names the agency's leads, its nos and its removals against the owners sent, and the cost per lead")
    check("Back from the agency (24 rows on 19 records)" in lines and "Written to DataSift: 10 records" in lines,
          "the scorecard counts what came back and what was written, a corrected file read as the same calls")
    check("1 with an appointment, 1 walked, 0 under contract" in lines and "sales problem" in lines and "C13.18" in lines,
          "the agency's leads are followed to appointments and walks, and no contract this month reads as a sales problem")
    check("In-house, the same month: 2 dials, 1 conversation, 1 lead, 1 not interested, 1 wrong number, 0 disconnected "
          "numbers and 1 do not call" in lines,
          "the in-house side is counted by the catalog's own definitions, leaving out what the agency's file touched")
    check("Cost per lead: agency $800.00 ($4,000.00 over 5 leads)" in lines and out.writes == 0 and not out.problems,
          "the cost per lead comes from the monthly fee, and the scorecard writes nothing")
    activity._store([{"id": "s12", "timestamp": _ts(day(20)), "event_type": "property.status.updated", "resource": U(3),
                      "payload": {"property": {"status": ["new_lead", "under_contract"]}}}])
    ctx = _ctx({"sales_test_min_leads": 1}, now)
    agencyscore.run_agencyscore(ctx)
    lines = "\n".join(ctx.out.lines)
    check("1 under contract" in lines and "sales problem" not in lines and "Set monthly_fee" in lines,
          "a contract this month from an agency lead clears the sales line; with no fee set the report says to set it")
    ctx = _ctx({"month": "2026-10"}, now)
    agencyscore.run_agencyscore(ctx)
    lines = "\n".join(ctx.out.lines)
    check("only its summary PDF came back, so no removal it was given can be held by our gate" in ctx.out.head
          and "The agency's own numbers, from its PDF: Dials 1,200" in lines and "Ask the agency for" in lines,
          "a month with only the PDF says no removal can be honored, shows the agency's own numbers and the ask")
    check("starts 2026-11-12, after this month, so there is nothing to compare" in lines and "0 dials" not in lines,
          "a month the brain's log copy does not reach says so instead of reporting zeros as the in-house side")
    ledger.set_fact(U(99), ar.FACT_SENT, "2026-08-03")
    ctx = _ctx({"month": "2026-08", "ask": "the per-record file, please"}, now)
    agencyscore.run_agencyscore(ctx)
    check(ctx.out.head == "Agency, August 2026: nothing came back yet from the 1 house sent."
          and "Ask the agency for the per-record file, please." in ctx.out.lines,
          "a month that sent records and got nothing back says so, with the ask as the setting words it")
    ctx = _ctx({"month": "2026-07"}, now)
    agencyscore.run_agencyscore(ctx)
    check(ctx.out.head == "" and ctx.out.lines == [], "a month with nothing sent and nothing back posts nothing")
    from .autopilot import policy as policy_mod
    bad = policy_mod.Stage(id="s", kind="agencyscore", owner="ops", mode="plan", summary="", schedule=[], presets=[],
                           params={"month": "Oct", "monthly_fee": -5, "lead_months": 0, "contract_statuses": "closed"})
    good = policy_mod.Stage(id="s", kind="agencyscore", owner="ops", mode="plan", summary="", schedule=[], presets=[],
                            params={"month": "last", "monthly_fee": 3995, "lead_months": 3, "max_log_calls": 150})
    probs = agencyscore.block_problems(bad)
    check(len(probs) == 4 and agencyscore.block_problems(good) == [],
          "a mistyped block (month, fee, months, statuses) is refused when the playbook loads; a good one passes")


def _hygiene_checks(check) -> None:
    print("  house rules")
    here = Path(__file__).resolve().parent
    files = [here / "workflows" / "agency_results.py", here / "autopilot" / "agencyscore.py", Path(__file__).resolve()]
    bad = [f.name for f in files if any(x in f.read_text(encoding="utf-8") for x in (EM, EN, "\x08"))]
    check(not bad, "no long or short dash and no backspace byte in the agency results files")
