"""Completing incomplete records owned by people (Ty, 2026-11-15; the course's C2.27). Zero network:
a fake CRM and a fake SiftMap, its own database and data folder. Every name, street and id here is
invented.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

COUNTS = {"first": {"pat": 50, "robin": 40, "casey": 30, "marcus": 20, "morgan": 15, "avery": 12, "jordan": 25},
          "last": {"example": 10, "sample": 8, "quill": 3}}
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)


def _row(u, first=None, last=None, company=None, lists=1):
    return {"uuid": u, "owner": {"uuid": "o-" + u, "first_name": first, "last_name": last, "company": company},
            "status": None, "list_count": lists, "has_phones": False}


def _full(row, **kw):
    o = dict(row["owner"])
    # No "created": a full record does not carry one (read on prod-account 2026-11-15). When each record was
    # added lives in the fake search below, the only place DataSift answers it.
    rec = {"uuid": row["uuid"], "type": "incomplete", "status": None, "tags": [], "lists": [], "dataflik_id": "df-" + row["uuid"],
           "last_sold": "2019-05-01",
           "address": {"street": "1 Example St", "city": "Knoxville", "state": "TN", "postal_code": "37917"},
           "owner": dict(o, type="incomplete", type_override=None, deceased=False, phones=[],
                         address={"street": "9 Mail Rd", "city": "Knoxville", "state": "TN", "postal_code": "37917"})}
    rec.update(kw)
    return rec


class FakeCRM:
    """The records search as DataSift answers it: a created window [from, to) on the day, the end being
    the start of that day, and a record id list honored like any other filter."""

    def __init__(self, rows, recs, made=None):
        self.rows_, self.recs, self.calls = rows, recs, []
        self.made = {r["uuid"]: (made or {}).get(r["uuid"], "2026-07-01") for r in rows}
        self.counts = []
        self.unclean = set()   # completed records the search still lists on the incomplete tab

    def _in(self, u, must):
        span = must.get("created")
        return not span or str(span[0])[:10] <= self.made.get(u, "") < str(span[1])[:10]

    def count(self, must):
        self.counts.append(copy.deepcopy(must))
        if must.get("properties"):
            return sum(1 for u in must["properties"] if u in self.made and self._in(u, must))
        if must.get("property_type") == "incomplete":
            return sum(1 for r in self.rows_ if self._in(r["uuid"], must))
        return 0

    def rows(self, must, cap=9900):
        if must.get("properties") and must.get("property_type") == "clean":
            return [{"uuid": u} for u in must["properties"] if u not in self.unclean][:cap]
        if must.get("property_type") != "incomplete":
            return []
        return copy.deepcopy([r for r in self.rows_ if self._in(r["uuid"], must)][:cap])

    def record(self, u):
        return copy.deepcopy(self.recs[u])

    def custom_fields(self, u):
        return {}

    def update_owner(self, owner_uuid, fields):
        self.calls.append(("owner", owner_uuid, dict(fields)))
        return {}

    def add_tags(self, u, tags, rec=None):
        self.calls.append(("tags", u, list(tags)))
        return []

    def remove_tags(self, u, drop):
        self.calls.append(("untag", u, list(drop)))

    def message(self, u, text, pin=False):
        self.calls.append(("note", u, text))
        return ""

    def skiptrace_scoped(self, uuids, dry_run, tags=None, force_refresh=False):
        self.calls.append(("trace", tuple(uuids), dry_run, tuple(tags or ())))
        return {"number_of_records": len(uuids)}


class FakeSiftMap:
    def __init__(self, names):
        self.names, self.reads = names, 0

    def get_detail(self, did):
        self.reads += 1
        return {"owner_info": {"owner_name": self.names.get(did, "")}}


def playbook(mode="plan", **over) -> dict:
    stage = {"kind": "complete", "owner": "research", "mode": mode, "schedule": ["03:20 daily"], "read_fields": False,
             "per_run": 300, "read_cap": 400, "siftmap_cap": 150, "trusts": "hold", "min_batch": 20}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
            "stages": {"c_complete": stage}}


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE)
    config.DATA_DIR = Path(tmp) / "complete"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        _pure(check)
        _runs(check)
        _gone(check)
    finally:
        config.DATA_DIR, config.DB_PATH, config.MODE = keep


class _GoneCRM(FakeCRM):
    """A record the search still lists while the record route answers 404 (deleted or merged)."""

    def record(self, u):
        if u == "g1":
            raise RuntimeError('HTTP 404 on GET /api/internal/property/g1/: {"detail":"No Property matches"}')
        return super().record(u)


def _gone(check) -> None:
    from . import store
    from .autopilot import policy, sweep
    print("  a record the search lists but DataSift has deleted")
    with store._LOCK, store._db() as db:
        db.execute("DELETE FROM ap_facts")
    for f in ("pool.json", "for_a_person.csv"):
        p = Path(config.DATA_DIR) / "autopilot" / "complete" / f
        if p.exists():
            p.unlink()
    config.MODE = "dry"
    rows = [_row("g1", company="Example Pat Q", lists=3), _row("p1", company="Example Pat Q", lists=1)]
    recs = {"p1": _full(rows[1])}
    _, _, sm = _fixture()
    out = sweep.run("c_complete", crm=_GoneCRM(rows, recs), now=NOW, pol=policy.parse(playbook("plan"), EMPLOYEES),
                    sources={"siftmap": sm})
    check(not [p for p in out.problems if "could not read" in p]
          and out.tally.get("gone from DataSift (the search still lists it)") == 1,
          "a 404 on a listed record is counted as gone, not posted as a problem")
    again = sweep.run("c_complete", crm=_GoneCRM(rows, recs), now=NOW, pol=policy.parse(playbook("plan"), EMPLOYEES),
                      sources={"siftmap": sm})
    check(not again.tally.get("gone from DataSift (the search still lists it)"),
          "a record found gone is remembered and not read again the next night")


def _pure(check) -> None:
    from .autopilot import complete as c
    print("  reading an owner")
    check([c.classify(o) for o in (
        {"company": "Example Pat Q"}, {"company": "Sample Robin L & Casey M Life Est"}, {"company": "Quill Avery J & Morgan K Tr"},
        {"company": "Pinewood Swim Club"}, {"company": "Northside Rentals Ii"}, {"company": "Not Available From The Data Source"},
        {"company": "Estate Of Pat Example"}, {"first_name": "J", "last_name": "Example"}, {"first_name": "Pat", "last_name": "Example"},
        {"last_name": "Example"}, {"company": "Example Tools Co"})]
          == ["person_company", "life_estate", "trust", "entity", "entity", "junk", "estate", "initials", "first_last",
              "last_only", "entity"],
          "every owner shape the live sample showed is told apart from the row alone")
    check([c.classify({"company": t}) for t in ("Sample Trust Company Fbo Pat Example", "First Example Bank Land Trust",
                                                 "Example Pat Q & Robin L Rev Living Trust")] == ["entity", "entity", "trust"],
          "a custodian account or a bank's land trust is a company; a family trust named for people is a trust")
    print("  splitting a name")
    check(c.parse("Example Pat Q", COUNTS) == {"first": "Pat", "last": "Example", "co_owner": "", "order": "county"}
          and c.parse("Pat Q Example", COUNTS)["order"] == "natural" and c.parse("Pat Q Example", COUNTS)["last"] == "Example",
          "county style puts the surname first and natural order puts it last, and both read right")
    s = c.parse("Sample Robin L & Casey M", COUNTS)
    check((s["first"], s["last"], s["co_owner"]) == ("Robin", "Sample", "Casey Sample"), "a spouse after & shares the surname")
    n = c.parse("Pat & Jordan Example", COUNTS)
    check((n["first"], n["last"], n["co_owner"]) == ("Pat", "Example", "Jordan Example"), "\"Pat & Jordan Example\" is two people named Example")
    r = c.parse("Example Pat Q Example Jordan M", COUNTS)
    check((r["first"], r["co_owner"]) == ("Pat", "Jordan Example"), "a surname written twice starts a second owner")
    check(c.parse("Zzyx Qqv", COUNTS).get("hold") == "cannot tell which word is the surname",
          "an order the counts cannot tell is held, never guessed")
    check("not one the account has seen" in c.parse("Example Barbarajordan", COUNTS).get("hold", ""),
          "a first name no clean record has ever carried is held (garbled county strings)")
    check(c.parse("Example P Q", COUNTS).get("hold") == "initials only", "initials alone are not a first name")
    t = c.parse("Quill Avery J & Morgan K Rev Tr", COUNTS, trust=True)
    check((t.get("first"), t.get("last")) == ("Avery", "Quill"), "a trust named for people reads as its first named person")
    check(c.from_siftmap("J Marcus Example", "Example") == "Marcus" and c.from_siftmap("Pat Other", "Example") == ""
          and c.from_siftmap("J M Example", "Example") == "",
          "SiftMap supplies the name a person goes by only when it names the same surname")
    print("  the record gate")
    base = _full(_row("g1", company="Example Pat Q"))
    check([c.record_hold(dict(base, **kw)) for kw in ({"status": "sold"}, {"tags": [{"title": "recently sold"}]},
                                                      {"owner": dict(base["owner"], deceased=True)}, {"type": "clean"},
                                                      {"created": "2026-07-01T00:00:00Z", "last_sold": "2026-09-01"},
                                                      {"last_sold": "2026-09-01"}, {})]
          == ["a finished status", "tagged recently sold", "the owner is marked deceased: research first", "already complete",
              c.SOLD_AFTER, "", ""],
          "a finished status, the sold tag, a dead owner, a clean record and a sale after a created date on the record all "
          "hold; with no created date on the record (DataSift sends none) the run asks the search instead")
    print("  when a record was added: the search, since the record does not say")
    from types import SimpleNamespace
    rows = [_row("a1", company="Example Pat Q"), _row("a2", company="Sample Robin L"), _row("a3", company="Quill Casey")]
    fake = FakeCRM(rows, {}, made={"a1": "2025-03-14", "a2": "2026-06-25", "a3": "2026-10-19"})
    probe = SimpleNamespace(crm=fake, now=NOW)
    cache = {}
    first = c._first_made(probe, cache)
    asked = len(fake.counts)
    check(first == "2025-03-14" and asked <= 16 and c._first_made(probe, cache) == first and len(fake.counts) == asked,
          "the day the oldest incomplete record was added is found by halving the window, in at most 16 counts, once a run")
    check(c.added_before(probe, "a2", "2026-09-01") and not c.added_before(probe, "a3", "2026-09-01")
          and not c.added_before(probe, "a2", "2026-06-25") and c.added_before(probe, "a2", "2026-06-26"),
          "a record added before its sale day counts; one added on or after the sale day does not")
    check(fake.counts[-1] == {"properties": ["a2"], "created": [c.BASE_DAY, "2026-06-26"]},
          "the check is one count on the record id with a created window ending at the start of the sale day")
    empty = SimpleNamespace(crm=FakeCRM([], {}), now=NOW)
    check(c._first_made(empty, {}) == "2026-11-17", "with no incomplete records the day is tomorrow, so no sale is ever asked about")


def _fixture():
    rows = [_row("p1", company="Example Pat Q", lists=3), _row("p2", company="Sample Robin L & Casey M Life Est", lists=2),
            _row("p3", first="J", last="Example"), _row("p4", first="K", last="Example"), _row("p5", last="Example"),
            _row("p6", first="Pat", last="Example"), _row("p7", first="Robin", last="Example"),
            _row("e1", company="Pinewood Swim Club"), _row("e2", company="Northside Rentals Ii"),
            _row("t1", company="Quill Avery J & Morgan K Tr"), _row("s1", company="Example Jordan M"),
            _row("j1", company="Not Available From The Data Source")]
    recs = {r["uuid"]: _full(r) for r in rows}
    recs["s1"]["last_sold"] = "2026-10-01"                          # sold after it was added
    sm = FakeSiftMap({"df-p3": "J Marcus Example", "df-p4": "J Marcus Example", "df-p5": "Pat Other",
                      "df-p6": "Pat Q Example", "df-p7": "Casey Example"})
    return rows, recs, sm


def _names_file():
    d = Path(config.DATA_DIR) / "autopilot" / "complete"
    d.mkdir(parents=True, exist_ok=True)
    (d / "names.json").write_text(json.dumps(dict(COUNTS, built=NOW.isoformat())), encoding="utf-8")


def _runs(check) -> None:
    from . import store
    from .autopilot import complete as c
    from .autopilot import ledger, policy, sweep, undo
    _names_file()
    rows, recs, sm = _fixture()
    crm = FakeCRM(rows, recs)
    print("  a plan run")
    out = sweep.run("c_complete", crm=crm, now=NOW, pol=policy.parse(playbook("plan"), EMPLOYEES), sources={"siftmap": sm})
    check(not [x for x in crm.calls if x[0] != "trace"] and out.head.startswith("Would complete"),
          "a plan run writes nothing and says what it would complete")
    check("2 companies" in out.head and "1 trusts" in out.head, "companies and trusts are counted and left alone")
    held = (Path(config.DATA_DIR) / "autopilot" / "complete" / "for_a_person.csv").read_text(encoding="utf-8")
    check("s1" in held and "p4" in held and "p5" in held and "p7" in held and "e1" not in held,
          "every hold goes to the file for a person with its reason; companies do not")
    print("  a live run")
    with store._LOCK, store._db() as db:
        db.execute("DELETE FROM ap_facts")
    for f in ("pool.json", "for_a_person.csv"):
        p = Path(config.DATA_DIR) / "autopilot" / "complete" / f
        if p.exists():
            p.unlink()
    config.MODE = "live"
    rows, recs, sm = _fixture()
    crm = FakeCRM(rows, recs)
    live = sweep.run("c_complete", crm=crm, now=NOW, pol=policy.parse(playbook("live"), EMPLOYEES), sources={"siftmap": sm})
    owners = {x[1]: x[2] for x in crm.calls if x[0] == "owner"}
    check(owners.get("o-p1") == {"first_name": "Pat", "last_name": "Example", "company": None, "type_override": "person"},
          "a name in the company field moves into first and last, the company is cleared and the owner is a person")
    check(owners.get("o-p2", {}).get("first_name") == "Robin" and ("tags", "p2", [c.TAG, c.LIFE_TAG]) in crm.calls
          and any(x[0] == "note" and x[1] == "p2" and "Casey Sample" in x[2] for x in crm.calls),
          "a life estate is completed to its named person, tagged Life Estate, and the spouse goes in a note")
    check(owners.get("o-p3", {}).get("first_name") == "Marcus" and "o-p4" not in owners and "o-p5" not in owners,
          "an initial becomes the name SiftMap gives the same person; another initial or another surname is held")
    check("o-p6" in owners and "o-p7" not in owners,
          "a record with both names is re-saved when SiftMap names the same person, and held when it does not")
    check(not any(x[1] in ("o-e1", "o-e2", "o-t1", "o-s1", "o-j1") for x in crm.calls if x[0] == "owner"),
          "companies, trusts (until decided), junk and a house that sold are never renamed")
    check(all(("tags", u, [c.TAG]) in crm.calls for u in ("p1", "p3", "p6")),
          "every completed record carries the Incomplete Data tag (C14.20)")
    check(not any(x[0] == "trace" for x in crm.calls) and "wait for a skip trace" in " ".join(live.lines),
          "under 20 completed records, the skip trace waits")
    steps = undo.plan(live.run_id if hasattr(live, "run_id") else "")
    ch = [x for x in ledger.changes() if x["what"] == "owner"]
    check(ch and ch[0]["before"]["company"] == "Example Pat Q" or any(x["before"].get("company") == "Example Pat Q" for x in ch),
          "every rename keeps the old names and company for undo")
    run_id = ch[0]["run_id"] if ch and "run_id" in ch[0] else None
    if run_id:
        steps = undo.plan(run_id)
        crm2 = FakeCRM(rows, recs)
        undo.run(run_id, crm=crm2, commit=True)
        back = [x for x in crm2.calls if x[0] == "owner" and x[1] == "o-p1"]
        check(back and back[0][2] == {"first_name": None, "last_name": None, "company": "Example Pat Q"},
              "undo puts the old company back and sends no override the owner never had")
    else:
        check(False, "the owner change carries its run id for undo")
    print("  the next nights")
    crm3 = FakeCRM(rows, recs)
    again = sweep.run("c_complete", crm=crm3, now=NOW, pol=policy.parse(playbook("live"), EMPLOYEES), sources={"siftmap": sm})
    check(not [x for x in crm3.calls if x[0] == "owner"] and again.tally.get("decided on an earlier night", 0) >= 4,
          "a record decided on an earlier night is not read or renamed again")
    store.kv_set(c.QUEUE_KEY, json.dumps(["q%02d" % i for i in range(21)]))
    crm4 = FakeCRM([], {})
    sweep.run("c_complete", crm=crm4, now=NOW, pol=policy.parse(playbook("live"), EMPLOYEES), sources={"siftmap": sm})
    traces = [x for x in crm4.calls if x[0] == "trace"]
    check(len(traces) == 2 and traces[0][2] is True and traces[1][2] is False and len(traces[1][1]) == 21
          and traces[1][3] == (c.TAG,) and json.loads(store.kv_get(c.QUEUE_KEY)) == [],
          "at 20 or more, the completed records get DataSift's own skip trace: a dry run first, then exactly those")
    store.kv_set(c.QUEUE_KEY, json.dumps(["s%02d" % i for i in range(25)]))
    store.kv_set(c.MISS_KEY, "{}")
    stuck = {"s03", "s11", "s19"}
    for night in range(c.MISS_LIMIT):
        if night:   # each night completes more records, so the queue reaches the batch floor again
            store.kv_set(c.QUEUE_KEY, json.dumps(json.loads(store.kv_get(c.QUEUE_KEY)) + ["n%d%02d" % (night, i) for i in range(20)]))
        crm5 = FakeCRM([], {})
        crm5.unclean = set(stuck)
        r5 = sweep.run("c_complete", crm=crm5, now=NOW, pol=policy.parse(playbook("live"), EMPLOYEES), sources={"siftmap": sm})
        t5 = [x for x in crm5.calls if x[0] == "trace"]
        if night == 0:
            check(len(t5) == 2 and set(t5[1][1]) == {"s%02d" % i for i in range(25)} - stuck
                  and not any("dry run covers" in p for p in r5.problems)
                  and sorted(json.loads(store.kv_get(c.QUEUE_KEY))) == sorted(stuck),
                  "a completed record still off the clean tab does not stop the trace: the 22 clean ones go, the 3 wait")
    left = json.loads(store.kv_get(c.QUEUE_KEY))
    held = (Path(config.DATA_DIR) / "autopilot" / "complete" / "for_a_person.csv").read_text(encoding="utf-8")
    check(left == [] and all(u in held for u in stuck) and c.STILL_INCOMPLETE in held,
          "a completed record still incomplete after %d nights leaves the queue for a person" % c.MISS_LIMIT)
    print("  caps and the trust switch")
    with store._LOCK, store._db() as db:
        db.execute("DELETE FROM ap_facts")
    (Path(config.DATA_DIR) / "autopilot" / "complete" / "pool.json").unlink()
    rows, recs, sm = _fixture()
    crm5 = FakeCRM(rows, recs)
    sweep.run("c_complete", crm=crm5, now=NOW, pol=policy.parse(playbook("live", per_run=1), EMPLOYEES), sources={"siftmap": sm})
    check(len([x for x in crm5.calls if x[0] == "owner"]) == 1, "no more completions in a run than per_run allows")
    with store._LOCK, store._db() as db:
        db.execute("DELETE FROM ap_facts")
    crm6 = FakeCRM(rows, recs)
    sweep.run("c_complete", crm=crm6, now=NOW, pol=policy.parse(playbook("live", trusts="complete"), EMPLOYEES),
              sources={"siftmap": sm})
    t1 = [x for x in crm6.calls if x[0] == "owner" and x[1] == "o-t1"]
    check(t1 and t1[0][2]["first_name"] == "Avery" and t1[0][2]["last_name"] == "Quill",
          "with trusts: complete, a trust named for people is rewritten to its first named person (C11.28)")
    config.MODE = "dry"
