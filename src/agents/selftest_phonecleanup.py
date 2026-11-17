"""Phone cleanup when the executor is named (Ty, 2026-11-15; the course's C3.14). Zero network: a fake
DataSift with its own account log, and this section's own database and data folder. Every name, street,
number and id here is invented; the numbers are on the 555 exchange.
"""
from __future__ import annotations

import copy
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

# The line the orchestrator adds to autopilot/registry.py EXTRA. Until it is there, this section stands
# the same line up for itself and takes it down again afterwards.
REGISTRY_LINES = {"phonecleanup": ("phonecleanup", "run_phonecleanup", "free", False, False)}
NOW = datetime(2026, 11, 17, 13, 5, tzinfo=timezone.utc)      # a Tuesday, 08:05 in Knoxville
STAGE = "phone_cleanup"
LOW_NUMBER = re.compile(r"\d{7,}|555")                          # a phone number, whole or in part


def ph(number, *tags, status="UNKNOWN", kind="MOBILE"):
    return {"number": number, "type": kind, "tags": list(tags), "status": status, "is_connected": True, "verified": False}


def rec(u, street, first="Pat", last="Example", *, company=None, lists=("Probate",), tags=("Courthouse Data",),
        phones=(), status=None, pr=None, pr_phone=None, deceased=False):
    return {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": "o-" + u, "first_name": first, "last_name": last, "company": company,
                      "deceased": deceased, "phones": [dict(p) for p in phones]},
            "status": status, "tags": list(tags), "lists": list(lists), "personal_representative": pr,
            "personal_representative_phone": pr_phone}


def joined(i, u, when, lists=("Probate",)):
    """A list join in the account log (what a probate upload leaves)."""
    return {"id": "pc-ev-%s" % i, "event_type": "property.lists.added", "resource": u, "resource_type": "property",
            "source": "internal-api", "author": "system",
            "timestamp": when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "payload": {"lists": list(lists)}}


class NoScorer:
    unit_usd = 0.015

    def configured(self) -> bool:
        return False


class FakeCRM:
    """The documented reads and the one write this stage makes, against records held in memory."""

    def __init__(self, records, custom=None, events=(), sticky=True, fail_custom=()):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in records}
        self.custom = copy.deepcopy(custom or {})
        self.events = list(events)
        self.sticky = sticky            # False: the upsert answers and nothing changes (a write that did not take)
        self.fail_custom = set(fail_custom)   # records whose custom fields cannot be read
        self.writes = []                # ("upsert", owner uuid, phones)
        self.reads = []                 # every full record read, in order

    def list_uuid(self, title):
        t = title.strip().lower()
        return "list:" + t if t in ("probate", "obituary") else ""

    def tag_uuid(self, title):
        t = title.strip().lower()
        return "tag:" + t if t in ("deep prospected", "heir identified") else ""

    def _match(self, r, must):
        lists = {str(x).lower() for x in r.get("lists") or []}
        tags = {str(x).lower() for x in r.get("tags") or []}
        if "any_lists" in must and not {x.split(":", 1)[1] for x in must["any_lists"]} & lists:
            return False
        if "all_tags" in must and not {x.split(":", 1)[1] for x in must["all_tags"]} <= tags:
            return False
        return r.get("status") not in ((must.get("must_not") or {}).get("any_property_status") or [])

    def _row(self, r):
        o, phones = r["owner"], r["owner"]["phones"]
        first = phones[0] if phones else None
        return {"uuid": r["uuid"], "address": r["address"], "status": r.get("status"),
                "owner": {k: o.get(k) for k in ("uuid", "first_name", "last_name", "company")},
                "list_count": len(r.get("lists") or []), "tag_count": len(r.get("tags") or []), "has_phones": bool(phones),
                "last_skip_traced": "2026-08-01",
                "phone": dict(first, tag_count=len(first.get("tags") or [])) if first else None}

    def rows(self, must, cap=10000):
        return [self._row(r) for r in self.recs.values() if self._match(r, must)][:cap]

    def record(self, u):
        self.reads.append(u)
        return copy.deepcopy(self.recs.get(u) or {})

    def custom_fields(self, u):
        if u in self.fail_custom:
            raise OSError("connection reset")
        return dict(self.custom.get(u) or {})

    def upsert_phones(self, owner_uuid, phones):
        self.writes.append(("upsert", owner_uuid, copy.deepcopy(phones)))
        if not self.sticky:
            return
        for r in self.recs.values():
            if r["owner"]["uuid"] == owner_uuid:
                by = {p["number"]: dict(p) for p in phones}
                r["owner"]["phones"] = [by.pop(p["number"], p) for p in r["owner"]["phones"]] + list(by.values())

    def logs(self, must, limit=100, cursor="", include_owner=False):
        from .autopilot import activity
        lo, hi = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in must["timestamp"])
        types = set(must.get("any_event_type") or [])
        hits = [e for e in self.events
                if (not types or e["event_type"] in types) and lo <= activity.parse_ts(e["timestamp"]) <= hi]
        return {"results": sorted(hits, key=lambda e: e["timestamp"], reverse=True), "next_cursor": ""}


def playbook(mode="plan", **over) -> dict:
    stage = {"kind": "phonecleanup", "owner": "research", "mode": mode, "schedule": "08:05 Mon-Fri"}
    stage.update(over)
    return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
            "gate": {"read_fields": True}, "stages": {STAGE: stage}}


def said(ok, msg, got) -> str:
    """The rule as the check's message, and what came back when it does not hold."""
    return msg if ok else "%s (got %r)" % (msg, got)


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE)
    config.DATA_DIR = Path(tmp) / "phonecleanup"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    try:
        check(all(registry.EXTRA.get(k) == row for k, row in REGISTRY_LINES.items())
              and not [p for p in registry.problems() if p.startswith("phonecleanup")],
              "the phone cleanup kind loads from its registry line")
        _pure(check)
        _settings(check, policy)
        _runs(check, store, policy)
    finally:
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        config.DATA_DIR, config.DB_PATH, config.MODE = keep


# ====================================================================== the rules, one at a time
def _pure(check) -> None:
    from .autopilot import phonecleanup as pc
    o = dict(pc.DEFAULTS)
    w = pc.vocab(o)
    print("  relationships")
    check(pc.relation_in("Son", w) == ("child", "son") and pc.relation_in("Daughter-in-law", w) == ("in law", "daughter")
          and pc.relation_in("step daughter", w) == ("child", "daughter") and pc.relation_in("Brothers", w) == ("sibling", "brother"),
          "a relationship tag is read however it is spelled (in-law, step, plural)")
    check(pc.relation_in("Grand Son", w) == ("grandchild", "grandson") and pc.relation_in("Great Granddaughter", w)[0] == "grandchild",
          "a grandson is never read as a son")
    check(all(pc.relation_in(t, w) is None for t in ("Dial First", "Dial Fourth", "SmartSkip", "Tracerfy", "Before Executor",
                                                    "Two Sources", "DNC", "Owner")),
          "a tier, a source, the stage's own tag and the owner's line are not relationships")
    check(pc.relation_in("STATUS UNVERIFIED (not shown living, not shown dead). son", w) == ("child", "son"),
          "research's unverified note does not hide the relationship written after it")
    check(pc.differs(("child", "son"), ("child", "daughter")) and pc.differs(("child", "son"), ("sibling", "brother"))
          and pc.differs(("in law", "son"), ("child", "son")),
          "a daughter is not the son, a brother is not the son and a son-in-law is not the son")
    check(not pc.differs(("child", "son"), ("child", "son")) and not pc.differs(("child", "son"), ("child", ""))
          and not pc.differs(("relative", ""), ("sibling", "brother")) and not pc.differs(None, ("child", "son")),
          "a second son, a child, a relative or a missing relationship could be the executor")

    print("  names")
    check(pc.person_names("By The Chancery Court") == ([], False) and pc.person_names("Estate of Pat Example") == ([], False)
          and pc.person_names("Example Law Firm PLLC") == ([], False) and pc.person_names("Unknown") == ([], False)
          and pc.person_names("From Pat Q Example") == ([], False) and pc.person_names("") == ([], False),
          "a court, an estate, a law firm, an unknown and a parsing fragment name nobody")
    check(pc.person_names("Pat & Robin Example") == (["Pat Example", "Robin Example"], False)
          and pc.person_names("Example, Pat") == (["Example, Pat"], False),
          "two executors in one field are two people sharing the surname; a county order name is one person")
    check(pc.person_names("Pat Example & Robin") == ([], True),
          "a field naming people in a way that cannot be read is unclear, never half-read")
    check(pc.same("Example, Pat", "Pat Example") and not pc.same("Lou Example Sr", "Lou Example Jr"),
          "a county order name is the same person, and a senior is never his junior")
    check(pc.maybe_same("Bill Example", "William Example") and pc.maybe_same("Kim Sample", "Kim Example")
          and not pc.maybe_same("Lou Example Sr", "Lou Example Jr") and not pc.maybe_same("Lee Other", "Morgan Sample"),
          "keeping a number errs wide (a nickname, a married name) but a senior is not his junior")

    print("  who is named")
    both = pc.named_person(rec("x1", "1 A St", pr="Pat Example"), {"Decision Maker": "Kim Example"}, o)
    ok = both == {"names": ["Pat Example"], "field": "the record's personal representative", "court": True, "unclear": False}
    check(ok, said(ok, "the probate filing's personal representative comes before a decision maker", both))
    dm = pc.named_person(rec("x2", "2 A St"), {"Decision Maker": "Kim Example", "DM Confidence": "high"}, o)
    check(dm is not None and dm["names"] == ["Kim Example"] and dm["court"] is False and dm["field"] == "the Decision Maker field",
          "with no court executor, the decision maker field names the person, and is not a court naming")
    court = pc.named_person(rec("x3", "3 A St"), {"Personal Representative": "Robin Sample"}, o)
    check(court is not None and court["court"] is True, "the Personal Representative custom field counts as the court's naming")
    check(pc.named_person(rec("x4", "4 A St", pr="By The Chancery Court"), {}, o) is None
          and pc.named_person(rec("x5", "5 A St", pr="Pat Example & Robin"), {}, o)["unclear"] is True,
          "a field naming nobody names nobody, and an unreadable field is flagged unclear")
    rel = pc.relation_of("Pat Example", {"Decision Maker": "Patricia Example", "DM Relationship": "Daughter"}, None, o, w)
    check(rel is None, "a relationship is never borrowed from another person who only shares a first name")
    disagree = pc.relation_of("Pat Example", {"Decision Maker": "Pat Example", "DM Relationship": "Son"},
                              {"people": [{"name": "Pat Example", "tag": "Brother", "relationship": "brother"}]}, o, w)
    check(disagree is None, "when the record and the research disagree on a relationship, it is unknown")
    agree = pc.relation_of("Pat Example", {"Decision Maker": "Pat Example", "DM Relationship": "Son"},
                           {"people": [{"name": "Pat Example", "tag": "Relative", "relationship": "relative"}]}, o, w)
    check(agree == ("child", "son"), "a generic relative from one source does not cancel a son from another")
    check(pc.decedent_clash(["Lou Example"], rec("x6", "6 A St"), {"Decedent Name": "Lou Example"}, o)
          and pc.decedent_clash(["Pat Example"], rec("x7", "7 A St", deceased=True), {}, o)
          and not pc.decedent_clash(["Kim Example"], rec("x8", "8 A St", deceased=True), {"Decedent Name": "Lou Example"}, o),
          "the executor field holding the name of the person who died is caught")

    print("  what research says")
    unver = {"must_sign": [{"who": "Casey Quill", "status": "UNVERIFIED: not shown living"}], "people": []}
    check(pc.research_objection(["Casey Quill"], unver) == "research could not confirm the named executor is living",
          "an executor the research marked UNVERIFIED holds the record")
    for state, want in (("Unknown", "research could not confirm the named executor is living"),
                        ("Deceased", "research shows the named executor deceased"), ("Living", "")):
        got = pc.research_objection(["Casey Quill"], {"must_sign": [], "people": [{"name": "Casey Quill", "living": state}]})
        check(got == want, said(got == want, "research showing the executor %s gives %s" % (state, want or "no objection"), got))
    check(pc.research_objection(["Casey Quill"], {"must_sign": [], "people": [{"name": "Lee Other", "living": "Unknown"}]}) == ""
          and pc.research_objection(["Casey Quill"], None) == "",
          "research that does not name the executor has nothing against them")
    rmap, role = pc.research_numbers({"call_order": [{"who": "the executor", "number": "(865) 555-0601"},
                                                     {"who": "the heirs of Lou Example", "number": "8655550602"},
                                                     {"who": "Kim", "number": "8655550603"}],
                                      "people": [{"name": "Kimberly Example", "ss_key": "k9"}],
                                      "index": {"k9": {"name": "Kim Example", "phones": [{"number": "8655550604"}]}},
                                      "also_reaches": {"8655550605": ["Pat Example"]}})
    check(role == {"8655550601"} and "8655550602" not in rmap and rmap.get("8655550603") == {"Kim"}
          and rmap.get("8655550604") == {"Kim Example", "Kimberly Example"} and rmap.get("8655550605") == {"Pat Example"},
          "research's call order, trace and name check map numbers to people; a role names the executor's number, a group names nobody")

    print("  sorting the numbers")
    lower = o["lower_tier"]

    def one(phone, people, rmap=None, pr=()):
        return pc.sort_numbers({"owner": {"phones": [phone]}}, people, rmap or {}, set(pr), o, w)

    son = [{"name": "William Example", "relation": ("child", "son")}]
    r = one(ph("8655550611", "Daughter", "Dial First", "SmartSkip"), son)
    check(len(r["move"]) == 1, "a daughter's number moves when a son is the executor")
    r = one(ph("8655550612", "Son", "Dial First"), son)
    check(not r["move"] and len(r["unsure"]) == 1, "a son's number when a son is the executor could be his: it stays, for a person")
    r = one(ph("8655550613", "Son", "Dial First"), son, {"8655550613": {"Bill Example"}})
    check(not r["move"] and not r["unsure"], "a number research filed under the executor's nickname stays")
    r = one(ph("8655550614", "Daughter", "Dial First"), [{"name": "Kim Example", "relation": None}], {"8655550614": {"Kim Sample"}})
    check(not r["move"], "a number research filed under the executor's married name stays")
    r = one(ph("8655550615", "Son", "Dial First"), [{"name": "Lou Example Sr", "relation": None}], {"8655550615": {"Lou Example Jr"}})
    check(len(r["move"]) == 1, "a junior's number moves when his father is the executor")
    r = one(ph("8655550616", "Brother", "Dial First"), [{"name": "Pat Example", "relation": None}])
    check(not r["move"] and len(r["unsure"]) == 1, "with the executor's relationship unknown, an unmapped number stays for a person")
    r = one(ph("8655550617", "Daughter", "Dial First"), son, pr={"8655550617"})
    check(not r["move"] and r["kept"]["of the executor's own"] == 1, "the number in the PR phone field is the executor's own")
    r = one(ph("8655550618", "Executor", "Dial First"), son)
    check(not r["move"] and r["kept"]["of the executor's own"] == 1, "a number with a role tag is the executor's own")
    for status in ("CORRECT", "WRONG", "DNC", "NO_ANSWER", "CORRECT_DNC"):
        r = one(ph("8655550619", "Daughter", "Dial First", status=status), son)
        check(not r["move"] and r["kept"]["with a status a person or the dialer set"] == 1,
              "a number whose status is %s is never touched" % status)
    r = one(ph("8655550620", "Daughter", "Dial First", "DNC"), son)
    check(not r["move"] and r["kept"]["marked do not call"] == 1, "a number marked do not call is never touched")
    r = one(ph("8655550621", "Daughter", "SmartSkip"), son)
    check(not r["move"] and r["kept"]["with no tier yet"] == 1, "a number with no tier is not given one")
    r = one(ph("8655550622", "Daughter", "Drop"), son)
    check(not r["move"] and r["kept"]["already at %s or lower" % lower] == 1, "a Drop number is never raised to Dial Fourth")
    r = one(ph("8655550623", "Daughter", lower, "Before Executor"), son)
    check(not r["move"] and r["kept"]["moved on an earlier run"] == 1, "a number moved on an earlier run is not moved again")
    r = one(ph("8655550624", "Daughter", "Dial First", "Before Executor"), son)
    check(not r["move"] and r["kept"]["raised again by a person after an earlier move"] == 1,
          "a number somebody raised again after a move stays where they put it")
    r = one(ph("8655550625", "Dial First"), son)
    check(not r["move"] and not r["unsure"] and not r["kept"], "the owner's own line (no relationship tag) is never looked at")
    r = one(ph("8655550626", "Relative", "Dial First"), son)
    check(not r["move"] and len(r["unsure"]) == 1, "a number tagged only Relative could be anybody's: it stays, for a person")
    r = one(ph("8655550627", "Brother", "Dial Third"), son)
    check(len(r["move"]) == 1, "a Dial Third relative's number moves down too")
    from .autopilot import gate
    only = {"owner": {"phones": [ph("8655550628", "Brother", "Dial First"), ph("8655550629", "Dial Fourth")]}}
    snapshot = json.dumps(only, sort_keys=True)
    none_left = pc.dial_left(only, [only["owner"]["phones"][0]], o, gate.GateConfig())
    one_left = pc.dial_left(only, [], o, gate.GateConfig())
    check(none_left == 0 and one_left == 1 and json.dumps(only, sort_keys=True) == snapshot,
          said(none_left == 0 and one_left == 1, "the numbers left to dial after a move are counted on a copy, never on the record",
               (none_left, one_left)))


def _settings(check, policy) -> None:
    from .autopilot import phonecleanup as pc
    print("  settings")
    try:
        p = policy.parse(playbook("plan"), EMPLOYEES)
        ok = pc.block_problems(p.stage(STAGE)) == []
    except policy.PolicyError as exc:
        ok = False
        print("    " + str(exc)[:300])
    check(ok, "the default block loads with no problem")
    bad = [({"lower_tier": "Dial First"}, "still dialed"), ({"lower_tier": "Dial Ninth"}, "is not a tier"),
           ({"why_tag": ""}, "why_tag must name"), ({"why_tag": "Executor"}, "is one of the executor_tags"),
           ({"why_tag": "Son"}, "reads as a relationship"), ({"why_tag": "Dial Fourth"}, "is a tier"),
           ({"keep_signers": "maybe"}, "keep_signers must be"), ({"max_reads": 0}, "max_reads must be"),
           ({"pr_fields": []}, "pr_fields must name"), ({"scope_lists": [], "scope_tags": []}, "at least one list or tag"),
           ({"relative_tags": []}, "relative_tags must be"), ({"relationship_fields": ["Decision Maker"]}, "relationship_fields must map")]
    for over, words in bad:
        try:
            policy.parse(playbook("plan", **over), EMPLOYEES)
            got = ""
        except policy.PolicyError as exc:
            got = str(exc)
        check(words in got, said(words in got, "a block with %s is refused when the playbook loads" % json.dumps(over), got[:200]))


# ====================================================================== the stage, end to end
def _fixture():
    """Every kind of record the stage meets. The comment on each says what it must do."""
    A = [ph("8655550301", "Son", "Dial First", "SmartSkip"),            # research: the executor's own
         ph("8655550302", "Daughter", "Dial First", "SmartSkip"),       # research: Kim's, moves
         ph("8655550303", "Brother", "Dial Second", "Tracerfy"),        # research: Ray's, moves
         ph("8655550304", "Daughter", "Dial First", "SmartSkip", status="CORRECT"),
         ph("8655550305", "Sister", "Dial Fourth", "SmartSkip"),
         ph("8655550306", "Dial First"),                                # the owner's own line
         ph("8655550307", "Son", "SmartSkip"),                          # no tier
         ph("8655550308", "Nephew", "Dial Third", "SmartSkip", status="WRONG"),
         ph("8655550309", "Cousin", "Dial First", "SmartSkip", "DNC")]
    recs = [
        rec("pc-a", "11 Example St", "Pat", "Example", tags=("Courthouse Data", "Deep Prospected"), phones=A, pr="Pat Example"),
        rec("pc-b", "12 Sample Rd", "Robin", "Sample", phones=[
            ph("8655550311", "Son", "Dial First", "SmartSkip"),          # could be Robin's own: stays, for a person
            ph("8655550312", "Daughter", "Dial Second", "SmartSkip"),    # moves
            ph("8655550313", "Grand Son", "Dial First", "SmartSkip"),    # a grandson: moves
            ph("8655550314", "Relative", "Dial First", "SmartSkip")]),   # anybody's: stays, for a person
        rec("pc-c", "13 Quill Ln", "Casey", "Quill", phones=[ph("8655550321", "Daughter", "Dial First", "SmartSkip")],
            pr="Casey Quill"),                                           # research could not confirm Casey living
        rec("pc-d", "14 Example Ct", "Dana", "Example", lists=("Obituary",), tags=("Deep Prospected",),
            phones=[ph("8655550331", "Son", "Dial First", "SmartSkip")]),     # nobody named yet
        rec("pc-e", "15 Sample Way", "Lou", "Sample", lists=("Obituary",),
            phones=[ph("8655550341", "Son", "Dial First", "SmartSkip")]),     # a low-confidence decision maker
        rec("pc-f", "16 Lead St", "Kerry", "Example", status="Warm Lead",
            phones=[ph("8655550351", "Daughter", "Dial First", "SmartSkip")], pr="Kerry Example"),
        rec("pc-g", "17 Market St", "Lee", "Market", tags=("Courthouse Data", "Do Not Market"),
            phones=[ph("8655550361", "Daughter", "Dial First", "SmartSkip")], pr="Lee Market"),
        rec("pc-h", "18 Holdings Ave", None, None, company="Example Holdings LLC",
            phones=[ph("8655550371", "Daughter", "Dial First", "SmartSkip")], pr="Quinn Example"),
        rec("pc-i", "19 Court St", "Rae", "Example", phones=[ph("8655550381", "Daughter", "Dial First", "SmartSkip")],
            pr="By The Chancery Court"),
        rec("pc-k", "21 Signer Pl", "Lou", "Sample", lists=("Obituary",), tags=("Deep Prospected", "Heir Identified"),
            deceased=True, phones=[ph("8655550401", "Son", "Dial First", "SmartSkip"),       # Jordan's, a signer: stays
                                   ph("8655550402", "Cousin", "Dial First", "SmartSkip")]),  # Lee's: moves
        rec("pc-l", "22 Phone Field Dr", "Taylor", "Example", phones=[
            ph("8655550411", "Son", "Dial First", "SmartSkip"),          # the PR phone field: the executor's own
            ph("8655550412", "Brother", "Dial First", "SmartSkip")]),    # moves
        rec("pc-m", "23 Decedent Ave", "Lou", "Example", phones=[ph("8655550421", "Daughter", "Dial First", "SmartSkip")],
            pr="Lou Example"),                                           # the decedent in the executor's place
        rec("pc-n", "24 Running Rd", "Nico", "Example", phones=[ph("8655550431", "Daughter", "Dial First", "SmartSkip")],
            pr="Nico Example"),                                          # research still running
    ]
    custom = {
        "pc-b": {"Personal Representative": "Robin Sample", "Decision Maker": "Robin Sample", "DM Relationship": "Son",
                 "DM Confidence": "high"},
        "pc-e": {"Decision Maker": "Avery Example", "DM Relationship": "Daughter", "DM Confidence": "low"},
        "pc-k": {"Decision Maker": "Morgan Sample", "DM Relationship": "Daughter", "DM Confidence": "high"},
        "pc-l": {"Personal Representative": "Taylor Example", "Personal Representative Phone": "865-555-0411",
                 "Decision Maker": "Taylor Example", "DM Relationship": "Daughter", "DM Confidence": "high"},
        "pc-m": {"Decedent Name": "Lou Example"},
    }
    return recs, custom


def _research(store, root: Path, u: str, result: dict, people=None, index=None) -> None:
    """A finished deep prospect on this box: the task with its result, and the run folder it names."""
    slug = "slug-" + u
    run_dir = root / "runs" / slug
    (run_dir / "analysis").mkdir(parents=True, exist_ok=True)
    full = dict(result, slug=slug, run_dir=str(run_dir))
    (run_dir / "agent_result.json").write_text(json.dumps(full), encoding="utf-8")
    (run_dir / "analysis" / (slug + ".json")).write_text(json.dumps({"slug": slug, "people": people or []}), encoding="utf-8")
    (run_dir / "people.json").write_text(json.dumps([{"slug": slug, "people": index or []}]), encoding="utf-8")
    tid, _ = store.enqueue("research", "deep_prospect", u, force=True)
    store.finish(tid, "done", full)


def _research_fixture(store, root: Path) -> None:
    _research(store, root, "pc-a",
              {"must_sign": [{"who": "Pat Example", "status": "Living", "why": "son", "short": "Pat"}],
               "call_order": [{"who": "Pat Example", "number": "(865) 555-0301", "why": "best"},
                              {"who": "Ray Example", "number": "8655550303", "why": "the uncle"}]},
              people=[{"name": "Pat Example", "relationship": "son", "tag": "Son", "living": "Living", "role": "decision maker",
                       "ss_key": "a1"},
                      {"name": "Kim Example", "relationship": "daughter", "tag": "Daughter", "living": "Living", "role": "channel",
                       "ss_key": "a2"}],
              index=[{"key": "a1", "name": "Pat Example", "phones": [{"number": "8655550301"}]},
                     {"key": "a2", "name": "Kim Example", "phones": [{"number": "8655550302"}]}])
    _research(store, root, "pc-c",
              {"must_sign": [{"who": "Casey Quill", "status": "UNVERIFIED: not shown living"}], "call_order": []},
              people=[{"name": "Casey Quill", "relationship": "son", "tag": "Son", "living": "Unknown", "role": "signer"}])
    _research(store, root, "pc-k",
              {"must_sign": [{"who": "Morgan Sample", "status": "Living"}, {"who": "Jordan Sample", "status": "Living"}],
               "verified": ["Morgan Sample", "Jordan Sample"],
               "call_order": [{"who": "Jordan Sample", "number": "8655550401"}, {"who": "Lee Other", "number": "8655550402"}]},
              people=[{"name": "Morgan Sample", "relationship": "daughter", "tag": "Daughter", "living": "Living", "role": "signer",
                       "ss_key": "k1"},
                      {"name": "Jordan Sample", "relationship": "son", "tag": "Son", "living": "Living", "role": "signer",
                       "ss_key": "k2"},
                      {"name": "Lee Other", "relationship": "cousin", "tag": "Cousin", "living": "Living", "role": "channel",
                       "ss_key": "k3"}],
              index=[{"key": "k2", "name": "Jordan Sample", "phones": [{"number": "8655550401"}]},
                     {"key": "k3", "name": "Lee Other", "phones": [{"number": "8655550402"}]}])
    store.enqueue("research", "deep_prospect", "pc-n", force=True)       # queued and never finished: still running


def _forget(store) -> None:
    """Start a scenario from no remembered verdict (the research tasks stay)."""
    with store._LOCK, store._db() as c:
        c.execute("DELETE FROM ap_facts")


def _texts(out) -> str:
    """What the stage says, with the box's own file path taken out (a temporary folder name is random)."""
    from .autopilot import phonecleanup as pc
    return " ".join([out.head] + list(out.lines) + [p.replace(str(pc.person_file()), "") for p in out.problems])


def _runs(check, store, policy) -> None:
    from .autopilot import activity, ledger, sweep, undo
    from .autopilot import phonecleanup as pc
    from .autopilot import researchqueue
    root = Path(config.DATA_DIR)
    recs, custom = _fixture()
    _research_fixture(store, root)

    def go(crm, mode="plan", **over):
        return sweep.run(STAGE, crm=crm, now=NOW, pol=policy.parse(playbook(mode, **over), EMPLOYEES), sources={},
                         scorer=NoScorer())

    config.MODE = "live"        # the box is live: the stage's own plan mode must still keep it from writing
    print("  a plan run")
    crm = FakeCRM(recs, custom)
    out = go(crm)
    check(not crm.writes, "a plan run writes nothing to DataSift")
    want = "Would move 6 relative's number(s) down to Dial Fourth on 4 record(s) where an executor or decision maker is named"
    check(out.head.startswith(want) and "3 record(s) wait on a person" in out.head,
          said(out.head.startswith(want), "the plan says how many numbers on how many records would move down", out.head))
    check(out.writes == 4, said(out.writes == 4, "every planned move still goes through the write guard (counted, not made)", out.writes))
    check(not {"pc-f", "pc-h", "pc-n"} & set(crm.reads),
          "a lead, a company-owned record and a record whose research is still running are never read")
    check(out.tally.get("a lead: the lead manager has it") == 1 and out.tally.get(pc.ENTITY_WHY) == 1
          and out.tally.get("research is still running on it: looked at on a later run") == 1,
          "each record passed over says why")
    check(out.tally.get("held back: tagged Do Not Market") == 1, "the gate holds a Do Not Market record")
    check(out.tally.get("no executor or decision maker named yet") == 2,
          said(out.tally.get("no executor or decision maker named yet") == 2,
               "a record naming nobody, or naming only a court, has no executor yet", dict(out.tally)))
    check(out.tally.get("the decision maker on the record is marked low confidence") == 1,
          "a low-confidence decision maker is not taken as named")
    check(not LOW_NUMBER.search(_texts(out)), said(not LOW_NUMBER.search(_texts(out)),
                                                   "no phone number appears in what the stage says", _texts(out)[:300]))
    ok = any(p.startswith("Some records need a person before their numbers can be sorted: 3 record(s)") for p in out.problems)
    check(ok, said(ok, "the records waiting on a person are one stable problem", out.problems))
    with pc.person_file().open(encoding="utf-8", newline="") as fh:
        listed = {r["record"]: r for r in csv.DictReader(fh)}
    check(set(listed) == {"pc-b", "pc-c", "pc-m"}, said(set(listed) == {"pc-b", "pc-c", "pc-m"},
                                                       "the unsure numbers, the unverified executor and the decedent clash are listed for a person",
                                                       sorted(listed)))
    check(listed.get("pc-b", {}).get("numbers") == "ending 0311, ending 0314"
          and not any(LOW_NUMBER.search(" ".join(r.values())) for r in listed.values()),
          "the list for a person carries the last four digits only")
    check(listed.get("pc-c", {}).get("why") == "research could not confirm the named executor is living"
          and listed.get("pc-m", {}).get("why") == "the executor field holds the name of the person who died",
          "each listed record says why it waits")

    print("  the next plan run")
    crm2 = FakeCRM(recs, custom)
    again = go(crm2)
    check(not crm2.reads and not crm2.writes, said(not crm2.reads, "a record whose verdict is remembered is not read again", crm2.reads))
    check(again.head.startswith(want), said(again.head.startswith(want), "the plan says the same work the next day without reading it again",
                                            again.head))

    print("  a live run")
    live_crm = FakeCRM(recs, custom)
    live = go(live_crm, "live")
    by_owner = {w[1]: w[2] for w in live_crm.writes}
    moved = {p["number"] for w in live_crm.writes for p in w[2]}
    check(set(by_owner) == {"o-pc-a", "o-pc-b", "o-pc-k", "o-pc-l"} and moved == {
        "8655550302", "8655550303", "8655550312", "8655550313", "8655550402", "8655550412"},
          said(moved == {"8655550302", "8655550303", "8655550312", "8655550313", "8655550402", "8655550412"},
               "a live run moves exactly the relatives' numbers a plan run found, one upsert per owner", sorted(moved)))
    check(live.head.startswith("Moved 6 relative's number(s) down to Dial Fourth on 4 record(s)"),
          said(live.head.startswith("Moved 6"), "the live run says what it moved", live.head))
    a2 = next((p for p in by_owner.get("o-pc-a", []) if p["number"] == "8655550302"), {})
    check(a2.get("tags") == ["Before Executor", "Daughter", "Dial Fourth", "SmartSkip"] and a2.get("status") == "UNKNOWN"
          and a2.get("type") == "MOBILE",
          said(a2.get("tags") == ["Before Executor", "Daughter", "Dial Fourth", "SmartSkip"],
               "a moved number keeps its relationship and source, takes the lower tier and says why", a2))
    stay = {"8655550301", "8655550304", "8655550305", "8655550306", "8655550307", "8655550308", "8655550309",
            "8655550311", "8655550314", "8655550401", "8655550411"}
    check(not moved & stay, "the executor's own, a signer's, a person-set status, the owner's line and the unsure numbers are not sent")
    check(all(w[0] == "upsert" for w in live_crm.writes), "the only write is the documented upsert-phones")
    check(all({p["number"] for p in live_crm.recs[r["uuid"]]["owner"]["phones"]} == {p["number"] for p in r["owner"]["phones"]}
              for r in recs), "nothing is deleted: every owner keeps every number")
    check(next(p for p in live_crm.recs["pc-a"]["owner"]["phones"] if p["number"] == "8655550301")["tags"]
          == ["Son", "Dial First", "SmartSkip"], "the executor's own number keeps its tier")
    check(live_crm.reads.count("pc-a") == 2 and "pc-c" not in live_crm.reads,
          "a plan run's remembered move is read again and done live, the first write is read back, and a held record is not re-read")
    ch = [c for c in ledger.changes() if c["what"] == "phones"]
    a_ch = next((c for c in ch if c["record_uuid"] == "pc-a"), None)
    check(len(ch) == 4 and a_ch is not None and {tuple(p["tags"]) for p in a_ch["before"]} == {
        ("Daughter", "Dial First", "SmartSkip"), ("Brother", "Dial Second", "Tracerfy")},
          "every live move records the tags the numbers carried before it")
    check(len(ledger.events(action=pc.KIND)) == 4, "every record changed is logged as this stage's action")
    facts = researchqueue.facts_by_key(pc.FACT)
    check(facts.get("pc-a", {}).get("verdict") == "moved" and facts.get("pc-a", {}).get("live") is True,
          "a live move is remembered as done")

    print("  after the move")
    crm3 = FakeCRM(list(live_crm.recs.values()), custom)
    go(crm3, "live")
    check(not crm3.writes, "a record already sorted is not written again")
    _forget(store)
    crm4 = FakeCRM(list(live_crm.recs.values()), custom)
    again4 = go(crm4, "live")
    check(not crm4.writes and "moved on an earlier run" in " ".join(again4.lines),
          said(not crm4.writes, "read afresh, the moved numbers are not moved a second time", again4.lines))

    print("  undo")
    run_id = a_ch["run_id"] if a_ch else ""
    crm_u = FakeCRM(list(live_crm.recs.values()), custom)
    res = undo.run(run_id, crm=crm_u, commit=True)
    a2_back = next(p for p in crm_u.recs["pc-a"]["owner"]["phones"] if p["number"] == "8655550302")
    check(res.get("undone") == 4 and sorted(a2_back["tags"]) == ["Daughter", "Dial First", "SmartSkip"]
          and not any("Before Executor" in p["tags"] for r in crm_u.recs.values() for p in r["owner"]["phones"]),
          said(res.get("undone") == 4, "undo puts every moved number back to the tags it carried", res))

    print("  a write that does not take")
    _forget(store)
    bad = FakeCRM(recs, custom, sticky=False)
    broke = go(bad, "live")
    check(len(bad.writes) == 1 and any(p.startswith("a phone change did not take on 11 Example St") for p in broke.problems),
          said(len(bad.writes) == 1, "a write that did not take is said, and nothing more is moved that run", broke.problems))
    check(broke.tally.get("not looked at: the run stopped after a phone change that did not take", 0) >= 1
          and researchqueue.facts_by_key(pc.FACT).get("pc-a", {}).get("verdict") != "moved",
          "the rest of the run waits, and the record is tried again next run")

    print("  the caps")
    _forget(store)
    capped = FakeCRM(recs, custom)
    one = go(capped, "live", max_records=1)
    check(len(capped.writes) == 1 and one.tally.get("left for the next run (change cap 1)", 0) >= 1,
          said(len(capped.writes) == 1, "no more records are changed in a run than max_records allows", dict(one.tally)))
    _forget(store)
    activity.reset(["state"])
    events = [joined(1, "pc-l", NOW - timedelta(days=1)), joined(2, "pc-a", NOW - timedelta(days=3))]
    first = FakeCRM(recs, custom, events=events)
    go(first, max_reads=1)
    check(first.reads == ["pc-l"], said(first.reads == ["pc-l"], "the record that most recently joined a list is read first", first.reads))
    _forget(store)
    two = FakeCRM(recs, custom, events=events)
    read2 = go(two, max_reads=2)
    check(two.reads == ["pc-l", "pc-a"] and read2.tally.get("left for the next run (read cap 2)", 0) >= 1,
          said(two.reads == ["pc-l", "pc-a"], "no more records are read in a run than max_reads allows, newest first", two.reads))

    print("  co-signers")
    _forget(store)
    never = go(FakeCRM(recs, custom), keep_signers="never")
    check(never.head.startswith("Would move 7 "), said(never.head.startswith("Would move 7 "),
                                                       "with keep_signers never, a verified co-signer's number moves too", never.head))
    bare = "1 of these record(s) would have no Dial First or Second number left, so the executor still needs a skip trace."
    check(bare in never.lines and not any("no Dial First or Second number left" in ln for ln in out.lines),
          said(bare in never.lines, "a record left with nothing to dial is said, so the executor gets traced", never.lines))
    _forget(store)
    keep = go(FakeCRM(recs, custom), keep_signers="always")
    check(keep.head.startswith("Would move 6 "), said(keep.head.startswith("Would move 6 "),
                                                      "keeping signers under a court executor keeps the same numbers here", keep.head))

    print("  the custom fields as they are now")
    _forget(store)
    ledger.set_fact("pc-b", "custom_fields", {})       # the gate's copy from before the probate upload named Robin
    go(FakeCRM(recs, custom))
    check(researchqueue.facts_by_key(pc.FACT).get("pc-b", {}).get("verdict") == "move",
          "a record is judged on its custom fields as they are now, not on the gate's week-old copy")
    _forget(store)
    unread = go(FakeCRM(recs, custom, fail_custom={"pc-b"}))
    check("pc-b" not in researchqueue.facts_by_key(pc.FACT)
          and any(p.startswith("could not read a record's custom fields") for p in unread.problems),
          "a record whose custom fields cannot be read is said, not remembered, and read again next run")

    print("  one number listed twice")
    _forget(store)
    twice = rec("pc-x", "30 Twice Ct", "Pat", "Twice", pr="Pat Twice",
                phones=[ph("8655550441", "Daughter", "Dial First", "SmartSkip"), ph("8655550441", "Daughter", "Dial First", "SmartSkip")])
    twice_crm = FakeCRM([twice], {"pc-x": {"Decision Maker": "Pat Twice", "DM Relationship": "Son", "DM Confidence": "high"}})
    twice_out = go(twice_crm, "live")
    sent = [p["number"] for w in twice_crm.writes for p in w[2]]
    check(sent == ["8655550441"], said(sent == ["8655550441"], "a number listed twice on one owner is sent once", sent))
    check(not any("did not take" in p for p in twice_out.problems),
          said(not twice_out.problems, "a number listed twice reads back as moved when one copy carries the new tags", twice_out.problems))

    print("  a dry box")
    _forget(store)
    changes_before = len(ledger.changes(include_undone=True))
    config.MODE = "dry"
    dry_crm = FakeCRM(recs, custom)
    dry = go(dry_crm, "live")
    check(not dry_crm.writes and dry.head.startswith("Would move"), "a live stage on a dry box writes nothing")
    check(len(ledger.changes(include_undone=True)) == changes_before, "a dry box records no change to undo")
    config.MODE = "live"
