"""Owner swap selftest: on an estate research already finished, the owner of record becomes the decision maker
research verified (Ty, 2026-11-23).

One check per rule and per refusal: which researched records are in scope, every refusal counted by name, plan
mode writing nothing, the change in both shapes DataSift may use (the owner object renamed, or a new owner
attached), what follows it (the research's numbers exactly as the record held them, the skip trace counter, the
empty fields, one note) and what stays (the death tags, the Obituary list), the stop after a first change that
does not behave as proven, undo, and the one-record proof. Zero network: a fake DataSift that keeps owners apart
from properties, research run folders written here by hand, and a throwaway database. Every name, street,
number and id is invented.
"""
from __future__ import annotations

import contextlib
import copy
import io
import itertools
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .research import writeback as wb

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 23, 10, 30, tzinfo=ET).astimezone(timezone.utc)   # a Monday, at the stage's slot
SLUG, STREET = "44-sample-hollow-rd", "44 Sample Hollow Rd"
U, U2, U3 = "rec-os-0001", "rec-os-0002", "rec-os-0003"
OW, OW2 = "own-os-0001", "own-os-0002"
OBIT = "https://obits.example.test/orrin-vexley"
OBIT_TEXT = ("Orrin Pell Vexley, 84, of Knoxville, died February 2, 2026. He was preceded in death by his wife, Edda "
             "Vexley. He is survived by his son, Corwin Vexley (Mae); a daughter, Lark Ambrose; and two grandchildren. "
             "A service will be held at the family church on Saturday.")
N_OWNER, N_CORWIN, N_LARK, N_CORWIN2, N_ODD = "8655550200", "8655550211", "8655550212", "8655550213", "8655550219"
PROPERTY = {"street": STREET, "city": "Knoxville", "state": "TN", "postal_code": "37901"}
HOME = {"street": "7 Example Ridge Way", "city": "Maryville", "state": "TN", "zip": "37801"}
HOME_ADDR = {"street": "7 Example Ridge Way", "city": "Maryville", "state": "TN", "postal_code": "37801"}
LARK_HOME = {"street": "19 Sample Bend", "city": "Sevierville", "state": "TN", "zip": "37862"}
TAGS = ("Courthouse Data", "Deep Prospected", "deceased")
STAGE = "estate_owner_swap"
EMPLOYEES = {"analysis", "research", "sales", "ops"}
NOTE = "Owner of record changed to Corwin Vexley (son), the decision maker research verified. The decedent stays on the record."
# The playbook block the report hands the orchestrator, exactly.
BLOCK = {"kind": "ownerswap", "owner": "research", "mode": "plan", "schedule": "10:30 Mon-Fri",
         "summary": "On estates research already finished, the owner of record becomes the decision maker research verified.",
         "max_records": 25, "max_reads": 150, "max_research_days": 120, "max_shared": 5, "recheck_days": 7,
         "researched_tags": ["Deep Prospected"], "death_tags": ["deceased"], "death_fields": ["Owner Deceased"],
         "carry_phones": True, "carry_counter": True, "fill_fields": True, "dm_field": "Decision Maker",
         "relationship_field": "DM Relationship", "decedent_field": "Decedent Name", "needs_switch": True}
PARAMS = {k: v for k, v in BLOCK.items() if k not in ("kind", "owner", "mode", "schedule", "summary")}
RUN = itertools.count(1)
TEN_DIGITS = re.compile(r"\d{10}")


# ------------------------------------------------------------------ the research, as a run folder leaves it --
def who(name, rel, key="", living="Living", role="signer", **kw):
    return dict({"ss_key": key, "name": name, "relationship": rel, "tag": rel, "living": living, "role": role,
                 "verified_by": "obituary"}, **kw)


def analysis(**over):
    an = {"slug": SLUG, "headline": "The owner died in 2026 and his son decides", "owner_status": "Deceased",
          "owner_status_note": "", "decision_maker": "Corwin Vexley", "level": "Level 4",
          "people": [who("Corwin Vexley", "Son", "k-corwin", role="decision maker"),
                     who("Lark Ambrose", "Daughter", "k-lark"),
                     who("Edda Vexley", "Wife", "", living="Deceased", role="none")],
          "must_sign": [{"who": "Corwin Vexley", "status": "Required", "why": "child of the owner", "short": "Corwin (son)"},
                        {"who": "Lark Ambrose", "status": "Required", "why": "child of the owner", "short": "Lark (daughter)"}],
          "call_order": [{"who": "Corwin Vexley", "number": N_CORWIN, "why": "son, Dial First mobile"}],
          "signing_flags": [], "open_items": [], "corrections": [],
          "title": {"vesting": "Orrin P Vexley, sole owner", "chain": [], "flags": []},
          "death": {"decedent": "Orrin Pell Vexley", "dod": "2026-02-02", "obituary_url": OBIT, "owner_deceased": True,
                    "confirmed": True},
          "crm_fields": {"Owner Deceased": "Yes", "Decedent Name": "Orrin Pell Vexley", "Decision Maker": "Corwin Vexley",
                         "DM Relationship": "Son"},
          "outreach": {"opener": "", "avoid": []}, "number_notes": {}, "wrong_party": {}, "also_reaches": {}}
    an.update(over)
    return an


def research_file():
    return {"slug": SLUG, "owner_of_record": "Orrin P Vexley", "owner_status": "Deceased",
            "decedents": [{"name": "Orrin Pell Vexley", "relationship_to_owner": "Owner of record", "dod": "2026-02-02",
                           "obituary_url": OBIT, "survivors_verbatim": "He is survived by his son, Corwin Vexley (Mae); "
                                                                       "a daughter, Lark Ambrose"}],
            "people": [], "red_flags": [], "owner_status_evidence": [], "identity_proof": [], "title_and_sales": [],
            "life_stage_signals": [], "property_history": []}


def person(key, first, last, home, phones, traced=True, **kw):
    """`traced`: the person's own skip trace returned these numbers for them as the subject."""
    return dict({"key": key, "name": "%s %s" % (first, last), "first": first, "last": last, "street": home["street"],
                 "city": home["city"], "state": home["state"], "zip": home["zip"], "is_subject": False,
                 "score_worthy": True, "own_trace": traced,
                 "phones": [{"number": n, "ss_type": "mobile", "sources": ["SmartSkip"], "on_crm": False, "crm_status": None,
                             "crm_tags": [], "own_trace": traced} for n in phones]}, **kw)


def people_index(corwin=None, traced=True):
    corwin_kw = dict(corwin or {})
    return [person("k-orrin", "Orrin", "Vexley", dict(PROPERTY, zip="37901"), [], traced=False, is_subject=True),
            person("k-corwin", "Corwin", "Vexley", HOME, [N_CORWIN, N_CORWIN2], traced=traced, **corwin_kw),
            person("k-lark", "Lark", "Ambrose", LARK_HOME, [N_LARK])]


def name_check(*pairs):
    return {"%s|%s" % (n, nm): {"number": n, "name": nm, "match": ok, "grade": "A" if ok else "F", "activity": 90,
                                "linetype": "Mobile", "valid": True, "litigator": False} for n, nm, ok in pairs}


VERIFY = name_check((N_CORWIN, "Corwin Vexley", True), (N_LARK, "Lark Ambrose", False))
SCORES = {N_CORWIN: {"tier": "Dial First", "score": 95, "line_type": "mobile"},
          N_LARK: {"tier": "Dial Second", "score": 70, "line_type": "mobile"},
          N_CORWIN2: {"tier": "Dial Third", "score": 50, "line_type": "mobile"},
          N_OWNER: {"tier": "Dial Fourth", "score": 30, "line_type": "landline"},
          N_ODD: {"tier": "Dial Third", "score": 45, "line_type": "mobile"}}


def owner_obj(**over):
    """The owner of record as the research read it: the man who died, his landline and one number DataSift's
    own trace found that nobody could place."""
    o = {"first_name": "Orrin", "last_name": "Vexley", "company": None, "deceased": True, "skiptrace_attempts": 1,
         "address": dict(PROPERTY),
         "phones": [{"number": "+1" + N_OWNER, "type": "LANDLINE", "tags": ["Dial Fourth"], "status": "UNKNOWN", "is_connected": True},
                    {"number": "+1" + N_ODD, "type": "MOBILE", "tags": ["Dial Third"], "status": "UNKNOWN", "is_connected": True}]}
    o.update(over)
    return o


def live_owner(**over):
    """The same owner today: the research's write-back added the son's and the daughter's numbers, and callers
    have since marked the son's CORRECT and the daughter's WRONG."""
    o = owner_obj()
    o["phones"] = o["phones"] + [
        {"number": "+1" + N_CORWIN, "type": "MOBILE", "tags": ["Son", "Dial First", "SmartSkip"], "status": "CORRECT", "is_connected": True},
        {"number": "+1" + N_LARK, "type": "MOBILE", "tags": ["Daughter", "Dial Second", "SmartSkip"], "status": "WRONG", "is_connected": True}]
    o.update(over)
    return o


def put(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def write_run(folder: Path, *, an=None, people=None, scores=None, verify=None, snap=None, audit=None, webcache=True,
              uuid=U) -> Path:
    """A run folder the way the research chain leaves it on the box after a live write-back."""
    snap = snap if snap is not None else dict(owner_obj(), uuid=OW)
    rec = {"uuid": uuid, "address": dict(PROPERTY), "owner": snap, "tags": ["Courthouse Data"], "lists": ["Obituary"],
           "secondary_owners": []}
    put(folder / "records.json", [{"rank": 1, "slug": SLUG, "uuid": uuid, "owner_uuid": snap.get("uuid"), "dataflik_id": None,
                                   "row": {"street": STREET, "zip": "37901", "owner": "Orrin Vexley", "mail_street": STREET},
                                   "record": rec, "messages": [], "custom_fields": [], "timeline": [],
                                   "title_owner": "VEXLEY ORRIN P"}])
    put(folder / "people.json", [{"slug": SLUG, "people": people or people_index(), "crm_only": []}])
    put(folder / "scores.json", scores or SCORES)
    put(folder / "analysis" / (SLUG + ".json"), an or analysis())
    put(folder / "research" / (SLUG + ".json"), research_file())
    put(folder / "research" / (SLUG + "_urls.json"), [OBIT])
    if webcache:
        put(folder / "research" / (SLUG + "_webcache.json"),
            {"search": {}, "index": {}, "page": {OBIT: {"ok": True, "text": OBIT_TEXT, "how": "direct"}}})
    put(folder / "verify.json", VERIFY if verify is None else verify)
    put(folder / "writeback_audit.json", audit if audit is not None else
        {SLUG: {"phones_written": [{"number": N_CORWIN}, {"number": N_LARK}]}})
    return folder


# ------------------------------------------------------------------ a fake DataSift --
class Account:
    """One account in memory that keeps owners apart from properties, the way DataSift does, so both shapes a
    change of owner can take are shown: `rename` (the owner object takes the new name and address) and
    `relink` (the property is pointed at an owner found by name and address, or a new one). `keep_old` is relink
    that also keeps the old owner as a second owner (what a retired route once did), `stuck` ignores the change.
    Any route the stage is not expected to call is an error, so a write nobody planned cannot pass unseen."""
    FIELDS = ("Owner Deceased", "Decedent Name", "Decision Maker", "DM Relationship")

    def __init__(self, mode="rename", fields=FIELDS):
        self.mode = mode
        self.props, self.owners, self.custom, self.board = {}, {}, {}, {}
        self.fields = {f.lower(): "fld-%d" % i for i, f in enumerate(fields)}
        self.labels = {"fld-%d" % i: f for i, f in enumerate(fields)}
        self.writes, self.reads, self._n = [], 0, 0

    def add(self, uuid, owner_uuid, owner=None, *, street=STREET, tags=TAGS, lists=("Obituary",), status=None,
            second=(), custom=None):
        if owner is not None:
            self.owners[owner_uuid] = copy.deepcopy(owner)
        self.props[uuid] = {"address": dict(PROPERTY, street=street), "owner": owner_uuid, "tags": list(tags),
                            "lists": list(lists), "status": status, "second": list(second)}
        self.custom[uuid] = dict({"Owner Deceased": "Yes"} if custom is None else custom)
        self.board[uuid] = []

    def _owner(self, ou):
        # Like DataSift: the owner route carries no house count (read live 2026-11-23). The records search
        # by owner (owner_records) is the only way to learn how many houses an owner object holds.
        o = copy.deepcopy(self.owners[ou])
        o["uuid"] = ou
        return o

    def _rec(self, u):
        p = self.props[u]
        ow = copy.deepcopy(self.owners[p["owner"]])
        ow["uuid"] = p["owner"]
        return {"uuid": u, "address": dict(p["address"]), "tags": list(p["tags"]), "lists": list(p["lists"]),
                "status": p["status"], "owner": ow, "notes": "", "mls": None,
                "secondary_owners": [{"full_name": "%s %s" % (self.owners[s]["first_name"], self.owners[s]["last_name"])}
                                     for s in p["second"]]}

    # ---- reads, by the autopilot CRM's names
    def record(self, u):
        self.reads += 1
        return self._rec(u) if u in self.props else {}

    def owner(self, ou):
        self.reads += 1
        return self._owner(ou) if ou in self.owners else {}

    def owner_records(self, ou, cap=250):
        self.reads += 1
        return [{"uuid": k, "address": dict(p["address"]), "status": p["status"]}
                for k, p in self.props.items() if p["owner"] == ou][:cap]

    def custom_fields(self, u):
        self.reads += 1
        return dict(self.custom.get(u, {}))

    def custom_field_uuid(self, label):
        return self.fields.get(str(label).strip().lower(), "")

    # ---- writes
    def set_custom_field(self, u, fid, value):
        self.writes.append(("field", u, self.labels[fid], value))
        self.custom.setdefault(u, {})[self.labels[fid]] = value

    def skiptrace_attempts(self, ou, total):
        assert isinstance(total, int), "the counter route takes the total as an integer"
        self.writes.append(("counter", ou, total))
        self.owners[ou]["skiptrace_attempts"] = total

    def upsert_phones(self, ou, phones):
        self.writes.append(("upsert", ou, sorted(wb.digits(p["number"]) for p in phones)))
        have = {wb.digits(p["number"]): p for p in self.owners[ou]["phones"]}
        for p in phones:
            have[wb.digits(p["number"])] = copy.deepcopy(p)
        self.owners[ou]["phones"] = list(have.values())

    def update_owner(self, ou, fields):
        self.writes.append(("owner_patch", ou, dict(fields)))
        self.owners[ou].update(fields)
        return self._owner(ou)

    def message(self, u, text, pin=False):
        self.writes.append(("note", u, text))
        self.board[u].append(text)
        return "msg-%d" % len(self.board[u])

    def _repoint(self, u, to):
        p = self.props[u]
        cur = p["owner"]
        self.writes.append(("owner", u, to["first_name"], to["last_name"]))
        if self.mode == "stuck":
            return
        if self.mode == "rename":
            self.owners[cur].update(first_name=to["first_name"], last_name=to["last_name"], address=dict(to["address"]))
            return
        key = (to["first_name"].lower(), to["last_name"].lower(), to["address"]["street"].lower(),
               str(to["address"]["postal_code"])[:5])
        hit = next((k for k, o in self.owners.items()
                    if (str(o.get("first_name") or "").lower(), str(o.get("last_name") or "").lower(),
                        str((o.get("address") or {}).get("street") or "").lower(),
                        str((o.get("address") or {}).get("postal_code") or "")[:5]) == key), None)
        if hit is None:
            self._n += 1
            hit = "own-new-%d" % self._n
            self.owners[hit] = {"first_name": to["first_name"], "last_name": to["last_name"], "company": None,
                                "deceased": False, "skiptrace_attempts": 0, "address": dict(to["address"]), "phones": []}
        if self.mode == "keep_old" and cur != hit and cur not in p["second"]:
            p["second"].append(cur)
        p["owner"] = hit

    def call(self, path, method="GET", body=None, headers=None):
        bits = path.split("?")[0].strip("/").split("/")[2:]          # after api/internal
        if bits[0] == "property" and len(bits) == 2:
            if method == "GET":
                return self.record(bits[1])
            if method == "PATCH" and set(body or {}) == {"owner"}:
                self._repoint(bits[1], body["owner"])
                return self._rec(bits[1])
        if bits[0] == "property" and bits[2:] == ["message"] and method == "POST":
            return {"uuid": self.message(bits[1], body["message"])}
        if bits[0] == "owner" and len(bits) == 2:
            if method == "GET":
                return self.owner(bits[1])
            if method == "PATCH":
                return self.update_owner(bits[1], body)
        raise AssertionError("unexpected call %s %s" % (method, path))

    def did(self, kind):
        return [w for w in self.writes if w[0] == kind]


# ------------------------------------------------------------------ helpers --
@contextlib.contextmanager
def switch(on: bool):
    """The research write-back's settings exactly as given: every one cleared first, so nothing this machine's
    own .env carries can change what a check sees."""
    saved = {k: os.environ.get(k) for k in wb.SETTING_NAMES}
    for k in wb.SETTING_NAMES:
        os.environ.pop(k, None)
    if on:
        os.environ["AGENTS_DP_OWNER_SWAP"] = "1"
    try:
        yield
    finally:
        for k in wb.SETTING_NAMES:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def _wipe(store) -> None:
    with store._LOCK, store._db() as c:
        for t in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_phones", "tasks"):
            c.execute("DELETE FROM " + t)


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DB_PATH, config.RUNS_DIR, config.MODE)
    # Its own database, always: run on its own (outside selftest.main, which points DB_PATH at a temp file), a
    # section that wipes tables would otherwise wipe the workstation's real agents.db (2026-11-19).
    config.DB_PATH = tmp / "selftest_ownerswap.db"
    config.RUNS_DIR = tmp / "ownerswap_runs"
    config.MODE = "dry"
    store.init()
    try:
        _run(check, tmp, store)
    finally:
        config.DB_PATH, config.RUNS_DIR, config.MODE = keep


def _run(check, tmp: Path, store) -> None:
    from . import channels
    from .autopilot import gate, ledger, ownerswap, policy, registry, stages, undo
    assert Path(config.DB_PATH).parent == Path(tmp), "this section only ever runs on its own temp database"
    root = tmp / "ownerswap_folders"

    def add_research(uuid, folder, *, days=3, live=True, written=True, status="Deceased", busy=False):
        tid, _ = store.enqueue("research", "deep_prospect", uuid, force=True)
        store.finish(tid, "done", {"slug": SLUG, "street": STREET, "run_dir": str(folder), "live": live,
                                   "written_back": written, "owner_status": status, "verified": ["Corwin Vexley"]})
        with store._LOCK, store._db() as c:
            c.execute("UPDATE tasks SET finished=? WHERE id=?",
                      ((NOW - timedelta(days=days)).isoformat(timespec="seconds"), tid))
        if busy:
            store.enqueue("research", "deep_prospect", uuid, force=True)
        return tid

    def scene(name, *, mode="rename", an=None, people=None, verify=None, scores=None, audit=None, snap=None,
              webcache=True, days=3, live=True, written=True, status="Deceased", busy=False, owner=None, tags=TAGS,
              rec_status=None, custom=None, folder=True):
        _wipe(store)
        acct = Account(mode)
        acct.add(U, OW, owner if owner is not None else live_owner(), tags=tags, status=rec_status, custom=custom)
        path = root / name
        if folder:
            write_run(path, an=an, people=people, scores=scores, verify=verify, snap=snap, audit=audit, webcache=webcache)
        tid = add_research(U, path, days=days, live=live, written=written, status=status, busy=busy)
        return acct, tid

    def go(acct, *, live=False, on=False, **params):
        st = policy.Stage(id=STAGE, kind="ownerswap", owner="research", mode="live" if live else "plan", summary="",
                          schedule=["10:30 Mon-Fri"], presets=[], params=dict(PARAMS, **params))
        pol = policy.Policy(tz="America/New_York", hours=(540, 1080), days=frozenset(range(5)), daily_usd=25.0,
                            record_usd=1.0, callers=[], stages={st.id: st}, gate=gate.GateConfig())
        out = stages.Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=live)
        ctx = stages.Ctx(policy=pol, stage=st, crm=acct, run_id="os-%d" % next(RUN), now=NOW, live=live, out=out)
        with switch(on):
            ownerswap.run_ownerswap(ctx)
        return out, ctx.run_id

    def left(out, why):
        return out.tally.get("left alone: " + why, 0) == 1 and out.acted == 0

    # ------------------------------------------------------------------ the playbook block
    print("  the playbook block")
    saved = (dict(registry.EXTRA), policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    try:
        # Registered for these parses exactly as the orchestrator's one line in registry.py will register it.
        registry.EXTRA["ownerswap"] = ("ownerswap", "run_ownerswap", "free", False, False)
        policy.KINDS = tuple(dict.fromkeys(policy.KINDS + ("ownerswap",)))
        policy.SPEND_BY_KIND["ownerswap"] = "free"
        policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + ("ownerswap",)))

        def book(block):
            return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                    "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
                    "stages": {STAGE: block}}
        pol = policy.parse(book(copy.deepcopy(BLOCK)), EMPLOYEES)
        st = pol.stage(STAGE)
        check(st.mode == "plan" and st.spend == "free" and st.get("max_records") == 25 and st.get("max_research_days") == 120,
              "the stage block for team/autopilot.yaml loads in plan mode, spends nothing, 25 records a run, research up to 120 days old")
        for bad, why in ((dict(BLOCK, max_records=-1), "max_records"), (dict(BLOCK, researched_tags=[]), "researched_tags"),
                         (dict(BLOCK, needs_switch="yes"), "needs_switch"), (dict(BLOCK, max_research_days=0), "max_research_days"),
                         (dict(BLOCK, dm_field=""), "dm_field"), (dict(BLOCK, max_shared=99), "max_shared")):
            try:
                policy.parse(book(bad), EMPLOYEES)
                refused = ""
            except policy.PolicyError as exc:
                refused = str(exc)
            check(why in refused, "a mistyped owner swap setting is refused when the playbook loads (%s)" % why)
    finally:
        registry.EXTRA.clear()
        registry.EXTRA.update(saved[0])
        policy.KINDS = saved[1]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[2])
        policy.NO_PRESETS = saved[3]
    check(ownerswap.settings(policy.Stage(STAGE, "ownerswap", "research", "plan", "", [], [], {}))["needs_switch"] is True,
          "with nothing set the stage acts only while the write-back's own owner swap switch is on")

    # ------------------------------------------------------------------ plan mode
    print("  plan mode")
    acct, tid = scene("plan")
    out, rid = go(acct)
    check(out.found == 1 and out.acted == 1 and out.head.startswith("Would change the owner of record"),
          "a researched estate whose research verified one decision maker is planned for the change")
    line = out.lines[0] if out.lines else ""
    check(STREET in line and "Orrin Vexley" in line and "Corwin Vexley (son)" in line and "Maryville, TN" in line
          and "Decision Maker" in line and "2 research numbers follow" in line,
          "the plan names the house, the person who died, the decision maker, their town and what follows the change")
    check(HOME["street"] not in line and not TEN_DIGITS.search(" ".join(out.lines + [out.head] + out.problems)),
          "a posted line carries neither the heir's street nor a phone number")
    check(acct.writes == [] and not ledger.changes(run_id=rid) and not ledger.events(U, "owner_swap")
          and ledger.fact(U, ownerswap.DONE_FACT) is None,
          "plan mode writes nothing: no account write, no change recorded, no event, never marked done")
    check(acct.reads <= 4 and out.writes >= 1 and out.alerts == [],
          "a plan reads the record, its fields and its owner, counts the writes it would make, and raises no alert")

    print("  live, with the switch off")
    acct, _ = scene("switch_off")
    out, rid = go(acct, live=True, on=False)
    check(acct.writes == [] and not ledger.changes(run_id=rid) and ownerswap.SWITCH_PROBLEM in out.problems
          and "waits on its switch" in out.head,
          "a live stage changes nothing while AGENTS_DP_OWNER_SWAP is off, and says how to switch it on")
    check(out.acted == 0 and out.lines and out.lines[0].startswith(STREET + ", Knoxville: would change"),
          "it counts nothing as done (so no update is posted as if it acted), and its lines still say what it would do")
    check(channels.classify(ownerswap.SWITCH_PROBLEM, "ownerswap") == "setup", "the switch problem is a setup a person makes")

    # ------------------------------------------------------------------ live: the owner object renamed
    print("  live: DataSift renames the owner object")
    acct, tid = scene("rename", mode="rename")
    out, rid = go(acct, live=True, on=True)
    rec = acct._rec(U)
    ow = rec["owner"]
    check(rec["owner"]["uuid"] == OW and (ow["first_name"], ow["last_name"]) == ("Corwin", "Vexley")
          and ow["address"] == HOME_ADDR,
          "the owner of record is now the decision maker, at his own address from his own trace")
    check(ow["deceased"] is False and ("owner_patch", OW, {"deceased": False}) in acct.writes,
          "the renamed owner object loses the dead man's deceased mark, because the person it names is alive")
    check(rec["address"] == PROPERTY and rec["tags"] == list(TAGS) and rec["lists"] == ["Obituary"],
          "the property address, the death tags and the Obituary list stay as they were")
    check(acct.board[U] == [NOTE], "one note says who the owner is now, that research verified them, and that the decedent stays")
    check(acct.custom[U] == {"Owner Deceased": "Yes", "Decision Maker": "Corwin Vexley", "DM Relationship": "Son",
                             "Decedent Name": "Orrin Pell Vexley"},
          "the empty Decision Maker, DM Relationship and Decedent Name fields are filled from the research")
    check(not acct.did("upsert") and not acct.did("counter"),
          "a renamed owner keeps its own numbers and counter, so nothing is carried and no counter is written")
    ch = ledger.changes(run_id=rid)
    check([c["what"] for c in ch] == ["owner_swap", "custom_field", "custom_field", "custom_field"]
          and ch[0]["before"]["first_name"] == "Orrin" and ch[0]["before"]["deceased"] is True
          and ch[0]["before"]["address"] == PROPERTY and ch[0]["before"]["owner_uuid"] == OW,
          "every write is recorded first, the owner with its name, mailing address and deceased mark before the change")
    done = ledger.fact(U, ownerswap.DONE_FACT)
    check(len(ledger.events(U, "owner_swap")) == 1 and done["pending"] is False and done["same_owner"] is True,
          "the change is logged once and the record is marked done")
    check(out.head.startswith("Changed the owner of record") and "decedent stays" in out.head
          and "owner of record changed from Orrin Vexley to Corwin Vexley (son)" in out.lines[0],
          "the update says what changed, in plain words")
    n = len(acct.writes)
    out2, _ = go(acct, live=True, on=True)
    check(len(acct.writes) == n and out2.tally.get("already changed") == 1, "a second run changes nothing: the record is done")

    print("  undo, after a rename")
    for c in reversed(ledger.changes(run_id=rid)):
        if c["what"] == "custom_field":
            label = acct.labels[c["before"]["field_uuid"]]
            acct.set_custom_field(U, c["before"]["field_uuid"], c["before"]["value"])
            check(acct.custom[U][label] == "", "undo empties the %s field the change filled" % label)
        elif c["what"] in ownerswap.UNDOABLE:
            ownerswap.undo_step(acct, c)
    ow = acct._rec(U)["owner"]
    check(ow["uuid"] == OW and (ow["first_name"], ow["last_name"]) == ("Orrin", "Vexley") and ow["address"] == PROPERTY
          and ow["deceased"] is True,
          "the undo step renames the owner object back, with its mailing address and its deceased mark")
    check(acct.board[U][-1] == "Owner change undone: the owner of record is Orrin Vexley again.",
          "and leaves a note that the change was put back")
    steps = {s["what"]: s for s in undo.plan(rid)}
    check(steps["custom_field"]["do"] == "custom_field",
          "the fields the change filled are undone by the undo command's own field step")
    try:
        ownerswap.undo_step(acct, {"record_uuid": U, "before": {}})
        check(False, "an owner change with no owner before it is refused")
    except RuntimeError as exc:
        check("does not name the owner" in str(exc), "an owner change with no owner before it is refused, not guessed")

    # ------------------------------------------------------------------ live: a new owner attached
    print("  live: DataSift attaches a new owner")
    acct, tid = scene("relink", mode="relink")
    out, rid = go(acct, live=True, on=True)
    new = acct.props[U]["owner"]
    nw = acct.owners[new]
    check(new != OW and (nw["first_name"], nw["last_name"]) == ("Corwin", "Vexley")
          and acct.owners[OW]["first_name"] == "Orrin" and len(acct.owners[OW]["phones"]) == 4,
          "a new owner is attached for the decision maker, and the old owner object keeps its name and numbers")
    by = {wb.digits(p["number"]): p for p in nw["phones"]}
    check(set(by) == {N_CORWIN, N_LARK} and by[N_CORWIN]["status"] == "CORRECT" and by[N_LARK]["status"] == "WRONG"
          and by[N_CORWIN]["tags"] == ["Son", "Dial First", "SmartSkip"],
          "the research's numbers follow to the new owner exactly as the record held them: a CORRECT stays CORRECT, a WRONG stays WRONG")
    check(N_OWNER not in by and N_ODD not in by,
          "the dead man's own line and a number nobody could place stay behind")
    check(nw["skiptrace_attempts"] == 1 and ("counter", new, 1) in acct.writes,
          "the new owner's skip trace counter goes from 0 to 1, because research traced him")
    ch = ledger.changes(run_id=rid)
    check([c["what"] for c in ch] == ["owner_swap", "add_numbers", "skiptrace_attempts", "custom_field", "custom_field", "custom_field"]
          and ch[2]["before"] == 0,
          "the carried numbers and the counter are recorded in order, the counter with the value before")
    check(acct.board[U] == [NOTE] and "2 numbers carried to the new owner" in out.lines[0] and "skip trace counter 1" in out.lines[0],
          "one note, and the update says what followed the change")

    print("  undo, after a new owner was attached")
    for c in reversed(ledger.changes(run_id=rid)):
        if c["what"] == "custom_field":
            acct.set_custom_field(U, c["before"]["field_uuid"], c["before"]["value"])
        elif c["what"] == "skiptrace_attempts":
            acct.skiptrace_attempts(acct.props[U]["owner"], int(c["before"] or 0))
        elif c["what"] in ownerswap.UNDOABLE:
            ownerswap.undo_step(acct, c)
    check(acct.props[U]["owner"] == OW and acct.owners[OW]["deceased"] is True and acct.owners[new]["skiptrace_attempts"] == 0,
          "undo points the property back at the old owner object, found by its name and address, and the counter goes back first")
    check(undo.IRREVERSIBLE.get("add_numbers", "").startswith("numbers added"),
          "numbers carried to the new owner are reported as staying (there is no delete route)")

    print("  live: the decision maker already has an owner record")
    acct, _ = scene("existing", mode="relink")
    acct.owners[OW2] = {"first_name": "Corwin", "last_name": "Vexley", "company": None, "deceased": True, "skiptrace_attempts": 2,
                        "address": dict(HOME_ADDR), "phones": [{"number": "+1" + N_CORWIN, "type": "MOBILE",
                                                                "tags": ["Dial Second"], "status": "UNKNOWN"}]}
    out, rid = go(acct, live=True, on=True)
    have = {wb.digits(p["number"]): p for p in acct.owners[OW2]["phones"]}
    check(acct.props[U]["owner"] == OW2 and have[N_CORWIN]["tags"] == ["Dial Second"] and have[N_LARK]["status"] == "WRONG",
          "a number the decision maker's own owner record already holds is left as it is; the rest follow")
    check(acct.owners[OW2]["deceased"] is False and acct.owners[OW2]["skiptrace_attempts"] == 3,
          "his owner record is not marked dead, and its counter goes up by one from where it was")

    # ------------------------------------------------------------------ fields already filled
    print("  fields")
    acct, _ = scene("fields", custom={"Owner Deceased": "Yes", "Decision Maker": "Corwin Vexley (son)",
                                      "Decedent Name": "Orrin P. Vexley"})
    out, _ = go(acct, live=True, on=True)
    check(acct.did("field") == [("field", U, "DM Relationship", "Son")],
          "a field that already names the decision maker or the decedent is never overwritten; only the empty one is filled")
    acct, _ = scene("placeholder", custom={"Owner Deceased": "Yes",
                                           "Decision Maker": "Undetermined: Orrin Vexley has died. Heirs to confirm with the family",
                                           "DM Relationship": "See the research note"})
    out, _ = go(acct, live=True, on=True)
    check(acct.custom[U]["Decision Maker"] == "Corwin Vexley" and acct.custom[U]["DM Relationship"] == "Son",
          "the write-back's own Undetermined placeholder counts as empty and is filled")
    acct, _ = scene("no_field")
    acct.fields.pop("decedent name")
    out, _ = go(acct)
    check(any("Decedent Name custom field does not exist in this account" in p for p in out.problems)
          and channels.classify(next(p for p in out.problems if "does not exist" in p), "ownerswap") == "setup",
          "a field the account lacks is said once as a setup, and the rest still go ahead")

    # ------------------------------------------------------------------ refusals, each by name
    print("  refusals")
    acct, _ = scene("lead", rec_status="Hot Lead")
    out, _ = go(acct)
    check(left(out, "the record is a lead"), "a lead is never re-pointed: people work it")
    acct, _ = scene("dm_company", an=analysis(people=[who("Corwin Vexley", "Son", "k-corwin", role="decision maker",
                                                          rc_name="Vexley Holdings LLC")] + analysis()["people"][1:]))
    out, _ = go(acct)
    check(left(out, "the decision maker is a company or trust"), "a decision maker whose name reads as a company is refused (people only)")
    acct, _ = scene("dm_no_zip", people=people_index(corwin={"zip": ""}))
    out, _ = go(acct)
    check(left(out, "the decision maker's address is incomplete") and acct.reads == 0,
          "a decision maker with no complete address is refused, from the run folder alone")
    acct, _ = scene("dm_untraced", people=people_index(traced=False))
    out, _ = go(acct)
    check(left(out, "the decision maker's own trace never confirmed an address"),
          "an address that only rode in on the dead owner's relative list is refused")
    acct, _ = scene("dm_flagged", people=people_index(corwin={"ss_deceased": True}))
    out, _ = go(acct)
    check(left(out, "the decision maker is flagged deceased"), "a decision maker the skip trace flags deceased is refused for a person to check")
    both = name_check((N_CORWIN, "Corwin Vexley", True), (N_LARK, "Lark Ambrose", True))
    acct, _ = scene("tie_two", an=analysis(decision_maker=""), verify=both)
    out, _ = go(acct)
    check(left(out, "two decision makers tie"), "two verified people and no decision maker named: a tie, refused")
    acct, _ = scene("tie_named", an=analysis(decision_maker="Lark Ambrose"))
    out, _ = go(acct)
    check(left(out, "two decision makers tie"), "research names one decision maker and verified another: a tie, refused")
    acct, _ = scene("tie_field", custom={"Owner Deceased": "Yes", "Decision Maker": "Lark Ambrose"})
    out, _ = go(acct)
    check(left(out, "two decision makers tie"), "the record's own Decision Maker field names someone else: a tie, refused")
    acct, _ = scene("one_verified", an=analysis(decision_maker=""))
    out, _ = go(acct)
    check(out.acted == 1, "one verified person and no decision maker named is no tie: that person is the one")
    acct, _ = scene("old", days=121)
    out, _ = go(acct)
    check(left(out, "the research is more than 120 days old"), "research older than the setting is not acted on")
    acct, _ = scene("old_ok", days=121)
    out, _ = go(acct, max_research_days=200)
    check(out.acted == 1, "the age limit is a setting")
    acct, _ = scene("dry", live=False)
    out, _ = go(acct)
    check(left(out, "the research never wrote back to the record"), "research that ran dry, and so wrote nothing back, is not acted on")
    acct, _ = scene("no_folder", folder=False)
    out, _ = go(acct)
    check(left(out, "the research files are not on this box"), "research whose run folder is not on this box is not acted on")
    acct, _ = scene("other_record")
    write_run(root / "other_record", uuid="rec-os-0999", snap=dict(owner_obj(), uuid="own-os-0999"))
    out, _ = go(acct)
    check(left(out, "the run folder now holds another record's research") and not out.problems,
          "a run folder another record on the same street line wrote over is a refusal, not breakage")
    acct, _ = scene("busy", busy=True)
    out, _ = go(acct)
    check(left(out, "in research now"), "a record in research again right now waits for that research")
    acct, _ = scene("untagged", tags=("Courthouse Data", "deceased"))
    out, _ = go(acct)
    check(left(out, "not tagged Deep Prospected"), "a record the research never tagged Deep Prospected is not acted on")
    acct, _ = scene("no_mark", owner=live_owner(deceased=False), tags=("Courthouse Data", "Deep Prospected"), custom={})
    out, _ = go(acct)
    check(left(out, "no death mark on the record"), "a record with no death mark on it is not acted on")
    acct, _ = scene("field_mark", owner=live_owner(deceased=False), tags=("Courthouse Data", "Deep Prospected"))
    out, _ = go(acct)
    check(out.acted == 1, "the Owner Deceased field alone is a death mark")
    acct, _ = scene("dnm", tags=TAGS + ("Do Not Market",))
    out, _ = go(acct)
    check(left(out, "held back: tagged Do Not Market"), "the gate holds a Do Not Market record, research channel included")
    lit = dict(SCORES, **{N_LARK: {"tier": "DNC", "score": 60, "litigator": True}})
    acct, _ = scene("litigator", scores=lit)
    out, _ = go(acct)
    check(left(out, "a litigator's number is tied to the research"), "a record a litigator's number is tied to is not acted on")
    acct, _ = scene("moved", owner=dict(live_owner()))
    acct.owners["own-os-0777"] = acct.owners.pop(OW)
    acct.props[U]["owner"] = "own-os-0777"
    out, _ = go(acct)
    check(left(out, "the owner changed since the research"), "an owner that changed since the research ran is left for a person")
    acct, _ = scene("pr_owner", snap=dict(owner_obj(first_name="Mae", last_name="Vexley"), uuid=OW))
    out, _ = go(acct)
    check(left(out, "the owner of record is not the person who died") and acct.reads == 0,
          "an owner of record who is not the person who died (a representative the court named) is left alone, without a read")
    acct, _ = scene("company_owner", owner=live_owner(company="Vexley Holdings LLC"))
    out, _ = go(acct)
    check(left(out, "the owner of record is a company or trust"),
          "a company on the title is not replaced, even beside a person's name (people only)")
    acct, _ = scene("trust_owner", owner=live_owner(type="trust"))
    out, _ = go(acct)
    check(left(out, "the owner of record is a company or trust"), "an owner DataSift types as a trust is not replaced either")
    acct, _ = scene("no_way_back", owner=live_owner(address={"street": STREET, "city": "Knoxville", "state": "TN", "postal_code": ""}))
    out, _ = go(acct)
    check(left(out, "the owner of record has no complete mailing address, so a change could not be put back"),
          "an owner with no complete mailing address is not changed, since undo could not put it back")
    acct, _ = scene("living", status="Living")
    out, _ = go(acct)
    check(out.found == 0 and out.acted == 0 and not out.tally and out.head.startswith("No researched estate"),
          "research that finds the owner living is not this stage's: nothing is counted")
    acct, _ = scene("unconfirmed", an=analysis(death=dict(analysis()["death"], owner_deceased=False)))
    out, _ = go(acct)
    check(left(out, "the research does not confirm the owner's death"), "research whose death guard did not confirm the death is refused")

    print("  an owner that holds other houses")
    acct, _ = scene("shared_same")
    acct.add(U2, OW, None, street="46 Sample Hollow Rd")
    out, _ = go(acct)
    check(out.acted == 0 and out.tally.get("left for a person: the owner holds other houses of the same estate") == 1
          and any("46 Sample Hollow Rd" in ln and "for a person" in ln for ln in out.lines),
          "an owner object that holds another house of the same estate is listed for a person, never changed")
    check(any(p.startswith("the owner of record also holds other houses") for p in out.problems)
          and channels.classify(next(p for p in out.problems if p.startswith("the owner of record also")), "ownerswap") == "finding",
          "the listing reaches the team as a finding, not as breakage")
    reads = acct.reads
    out, _ = go(acct)
    check(acct.reads == reads and out.tally.get("left for a person: the owner holds other houses of the same estate") == 1
          and any("46 Sample Hollow Rd" in ln for ln in out.lines),
          "the listing is remembered for the same research, so the next run lists it again without reading the account")
    acct, _ = scene("shared_other")
    acct.add(U2, OW, None, street="46 Sample Hollow Rd", status="Warm Lead")
    out, _ = go(acct)
    check(left(out, "the owner holds other houses that are not only this estate"),
          "an owner object whose other house is someone's lead is refused outright")
    acct, _ = scene("count_unread")
    acct.owner_records = lambda ou, cap=250: []           # the records search failed: the count is unknown
    out, _ = go(acct)
    check(out.acted == 0 and acct.writes == []
          and out.tally.get("waiting for a later run (the owner's houses could not be read)") == 1,
          "when the owner's houses cannot be read the stage waits and changes nothing, never treating it as one house")
    acct, _ = scene("count_capped")
    for i in range(int(BLOCK["max_shared"]) + 4):
        acct.add("prop-many-%d" % i, OW, None, street="%d Sample Hollow Rd" % (100 + i))
    rows_of_many = acct.owner_records(OW)
    acct.owner_records = lambda ou, cap=250: [r for r in rows_of_many if r["uuid"] != U][:cap]
    out, _ = go(acct)
    check(left(out, "the owner holds other houses that are not only this estate") and acct.writes == [],
          "an owner of more houses than the stage reads, whose first page leaves this one out, is refused, never left waiting")
    acct, _ = scene("count_no_owner_field")
    out, _ = go(acct)
    check(out.acted == 1 and "properties_count" not in acct.owner(OW),
          "an owner record with no house count field (as DataSift answers) is counted through the records search")

    print("  remembered refusals")
    acct, tid = scene("remember", rec_status="Hot Lead")
    go(acct)
    reads = acct.reads
    out, _ = go(acct)
    check(acct.reads == reads and left(out, "the record is a lead"),
          "a refusal a read found is trusted for the same research, so the next run makes no read for it")
    tid2 = add_research(U, root / "remember")
    out, _ = go(acct)
    check(acct.reads > reads and left(out, "the record is a lead"), "new research on the record is checked again")

    # ------------------------------------------------------------------ caps
    print("  caps")
    _wipe(store)
    acct = Account("rename")
    for u, o in ((U, OW), (U3, "own-os-0003")):
        acct.add(u, o, dict(live_owner(), first_name="Orrin"))
        add_research(u, write_run(root / ("cap_" + u), snap=dict(owner_obj(), uuid=o), uuid=u))
    out, _ = go(acct, max_records=1)
    check(out.acted == 1 and out.tally.get("waiting for a later run (cap)") == 1, "the stage changes at most max_records owners a run")
    out, _ = go(acct, max_reads=5)
    check(out.acted == 1 and out.tally.get("waiting for a later run (read cap)") == 1,
          "and stops reading at its read cap, leaving the rest for a later run")

    # ------------------------------------------------------------------ a change that does not behave
    print("  a change that does not take, or does too much")
    acct, _ = scene("stuck", mode="stuck")
    out, rid = go(acct, live=True, on=True)
    check(any("did not stick" in p for p in out.problems) and not acct.did("field") and not acct.did("note")
          and ledger.fact(U, ownerswap.DONE_FACT) is None and out.tally.get("left alone: the owner change did not take") == 1,
          "an owner change that does not stick writes no field and no note, and is tried again on the next run")
    check(channels.classify(next(p for p in out.problems if "did not stick" in p), "ownerswap") == "breakage",
          "and that reaches the issues channel as breakage")
    _wipe(store)
    acct = Account("keep_old")
    for u, o in ((U, OW), (U3, "own-os-0003")):
        acct.add(u, o, live_owner())
        add_research(u, write_run(root / ("keep_" + u), snap=dict(owner_obj(), uuid=o), uuid=u))
    out, rid = go(acct, live=True, on=True)
    check(len(acct.did("owner")) == 1 and out.tally.get("waiting for a later run (the run stopped)") == 1
          and any("did not behave as the proof showed" in p and "second owner" in p for p in out.problems),
          "a first change that keeps the old owner as a second owner stops the run: no other owner is changed")

    # ------------------------------------------------------------------ the one-record proof
    print("  the one-record proof")
    for mode, shape in (("rename", "renamed the owner object"), ("relink", "attached a new owner object")):
        _wipe(store)
        acct = Account(mode)
        acct.add(U, OW, live_owner(), tags=("Proof Test",), lists=(), custom={})
        dry = ownerswap.prove(acct, U)
        check(dry["ok"] is None and acct.writes == [], "the proof is dry unless commit: it reads and writes nothing (%s)" % mode)
        r = ownerswap.prove(acct, U, commit=True)
        ow = acct._rec(U)["owner"]
        check(r["took"] and r["behavior"] == shape and r["restored"] and r["second_owners_restored"] and r["ok"],
              "the proof shows which shape DataSift used (%s) and that undo put the owner back as it was" % shape)
        check(ow["uuid"] == OW and ow["first_name"] == "Orrin" and ow["deceased"] is True
              and acct.board[U][0] == ownerswap.PROOF_NOTE and acct.board[U][-1].startswith("Owner change undone"),
              "after the proof the record holds its own owner again, deceased mark included (%s)" % mode)
    _wipe(store)
    acct = Account("keep_old")
    acct.add(U, OW, live_owner(), tags=("Proof Test",), lists=(), custom={})
    r = ownerswap.prove(acct, U, commit=True)
    check(r["old_in_second_owners"] and r["ok"] is False, "the proof fails when the old owner is kept as a second owner")
    acct = Account("rename")
    acct.add(U, OW, live_owner(), tags=("Proof Test",), lists=(), custom={})
    acct.add(U2, OW, None, street="46 Sample Hollow Rd")
    r = ownerswap.prove(acct, U, commit=True)
    check(r["ok"] is False and "holds 2 records" in r["note"] and acct.writes == [],
          "the proof refuses a record whose owner object holds another house: a rename would move both")
    acct = Account("rename")
    acct.add(U, OW, live_owner(), status="Warm Lead")
    r = ownerswap.prove(acct, U, commit=True)
    check(r["ok"] is False and acct.writes == [], "the proof refuses a lead")
    keys = {k: os.environ.get(k) for k in ("REISIFT_API_KEY", "OWNERSWAP_PROOF", "OWNERSWAP_TEST_KEY")}
    try:
        for k in keys:
            os.environ.pop(k, None)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            missing = ownerswap.main(["proof", "--record", U, "--key-env", "OWNERSWAP_TEST_KEY"])
            os.environ["REISIFT_API_KEY"] = os.environ["OWNERSWAP_TEST_KEY"] = "key-for-the-test"
            prod = ownerswap.main(["proof", "--record", U, "--key-env", "OWNERSWAP_TEST_KEY", "--commit"])
            prod_flag = ownerswap.main(["proof", "--record", U, "--key-env", "OWNERSWAP_TEST_KEY", "--allow-prod", "--commit"])
        check(missing == 2, "the proof needs the environment variable that holds the target account's key")
        check(prod == 3 and prod_flag == 3,
              "the production key is refused, and --allow-prod alone does not lift it (OWNERSWAP_PROOF=1 is needed too)")
    finally:
        for k, v in keys.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    # ------------------------------------------------------------------ text
    print("  text")
    here = Path(__file__).resolve()
    mod = here.parent / "autopilot" / "ownerswap.py"
    texts = [here.read_text(encoding="utf-8"), mod.read_text(encoding="utf-8")]
    check(all(chr(0x2014) not in t and chr(0x2013) not in t for t in texts), "no long dash in the stage or its selftest")
    check(all("\x08" not in t for t in texts), "no backspace byte in the stage or its selftest")
    probs = [ownerswap.SWITCH_PROBLEM, ownerswap.PERSON_PROBLEM % STREET]
    check(all(not re.match(r"\s*\d", p) for p in probs), "no problem begins with a number, so one shape is said once a day")
    check(ownerswap.NOTE % ("Corwin Vexley", " (son)") == NOTE, "the note reads exactly as Ty's request asks")
