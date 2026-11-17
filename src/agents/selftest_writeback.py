"""Research write-back selftest: what a deep prospect reads first and what it may write on a record.

One check per rule and per refusal: the tier rule (decision D12), who counts as verified, the
Heir Identified tag, the research level, the decision maker fields, the owner's deceased mark, the
owner change, the litigator rule, the skip trace counter, the report in the record's documents,
each heir's share, the free look, the order of the chain, and the write-back itself run twice
against a fake DataSift held in memory. Zero network. Every name, street and number is invented.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from . import config
from .research import writeback as wb

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SLUG, U, U2, OW = "12-test-ln", "rec-0001", "rec-0002", "own-0001"
OBIT = "https://obits.example.test/dorn-quillfeather"
COURT = "https://court.example.test/probate/2026-p-101"
OBIT_TEXT = ("Dorn Wesley Quillfeather, 81, of Knoxville, died March 4, 2026. He was preceded in death by his parents "
             "and a son, Garrick Quillfeather. He is survived by his wife of 52 years, Marla Quillfeather; a daughter, "
             "Ines Brackwater (Tomas); a son, Pell Quillfeather; and three grandchildren. A service will be held "
             "at the family church on Saturday. " * 2)
N_OWNER, N_MARLA, N_INES, N_PELL, N_DROP = "8655550100", "8655550111", "8655550112", "8655550113", "8655550119"


# ------------------------------------------------------------------ builders --

def reach(name, tag="", role="signer", owner_like=False):
    return {"name": name, "relationship": tag.lower(), "tag": tag, "role": role, "owner_like": owner_like}


def row(number, tier, reaches=(), *, on_crm=False, status=None, litigator=False, kind="MOBILE"):
    r = {"number": number, "tier": tier, "score": 90, "type": kind, "on_crm": on_crm, "crm_status": status,
         "sources": ["SmartSkip"], "litigator": litigator, "reaches": list(reaches), "note": "",
         "reaches_label": "; ".join(z["name"] for z in reaches)}
    r["do_not_dial"] = (status or "") in wb.BAD_STATUSES or litigator or tier == "Drop"
    return r


def item(number, tier):
    return {"number": number, "type": "MOBILE", "tags": [tier, "SmartSkip"], "status": "UNKNOWN",
            "is_connected": True, "verified": False, "_reaches": ""}


def who(name, rel, living="Living", role="signer", key="", **kw):
    return dict({"ss_key": key, "name": name, "relationship": rel, "tag": rel.split(" ")[0].strip("(,"), "living": living,
                 "role": role, "verified_by": "obituary"}, **kw)


def analysis(**over):
    an = {"slug": SLUG, "headline": "The owner died in 2026 and the family signs", "owner_status": "Deceased",
          "owner_status_note": "", "level": "Level 3: the owner on the title has died",
          "people": [who("Marla Quillfeather", "Wife", role="decision maker", key="k-marla"),
                     who("Ines Brackwater", "Daughter", key="k-ines"),
                     who("Pell Quillfeather", "Son", key="k-pell")],
          "must_sign": [{"who": "Marla Quillfeather", "status": "Required", "why": "surviving spouse", "short": "Marla (wife)"},
                        {"who": "Ines Brackwater", "status": "Required", "why": "child of the owner", "short": "Ines (daughter)"},
                        {"who": "Pell Quillfeather", "status": "Required", "why": "child of the owner", "short": "Pell (son)"}],
          "signing_flags": [], "open_items": [],
          "call_order": [{"who": "Marla Quillfeather", "number": N_MARLA, "why": "wife, Dial First mobile"}],
          "title": {"vesting": "Dorn W Quillfeather, sole owner", "chain": [], "flags": []},
          "death": {"decedent": "Dorn Wesley Quillfeather", "dod": "2026-03-04", "obituary_url": OBIT,
                    "owner_deceased": True, "confirmed": True},
          "crm_fields": {"Owner Deceased": "Yes", "Decedent Name": "Dorn Wesley Quillfeather",
                         "Decision Maker": "Marla Quillfeather", "DM Relationship": "Wife"},
          "outreach": {"opener": "Hello, is this Marla?", "avoid": []}, "corrections": []}
    an.update(over)
    return an


def research_file(**over):
    r = {"slug": SLUG, "owner_of_record": "Dorn W Quillfeather", "owner_status": "Deceased",
         "decedents": [{"name": "Dorn Wesley Quillfeather", "relationship_to_owner": "Owner of record", "dod": "2026-03-04",
                        "obituary_url": OBIT, "survivors_verbatim": "He is survived by his wife of 52 years, Marla "
                        "Quillfeather; a daughter, Ines Brackwater (Tomas); a son, Pell Quillfeather"},
                       {"name": "Garrick Quillfeather", "relationship_to_owner": "Son (died before the owner)",
                        "dod": "2019-06-01", "obituary_url": "", "survivors_verbatim": ""}],
         "people": [], "red_flags": [], "owner_status_evidence": [], "identity_proof": [], "title_and_sales": [],
         "life_stage_signals": [], "property_history": []}
    r.update(over)
    return r


def cache(**pages):
    return {"search": {}, "index": {}, "page": {u: {"ok": True, "text": t, "how": "direct"} for u, t in pages.items()}}


def name_check(*pairs):
    return {"%s|%s" % (n, nm): {"number": n, "name": nm, "match": ok, "grade": "A" if ok else "F", "activity": 90,
                                "linetype": "Mobile", "valid": True, "litigator": False} for n, nm, ok in pairs}


VERIFY = name_check((N_MARLA, "Marla Quillfeather", True), (N_INES, "Ines Brackwater", False),
                    (N_PELL, "Pell Quillfeather", True))
DIAL = [row(N_MARLA, "Dial First", [reach("Marla Quillfeather", "Wife", "decision maker")]),
        row(N_INES, "Dial Second", [reach("Ines Brackwater", "Daughter")]),
        row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")]),
        row(N_DROP, "Drop", [reach("Pell Quillfeather", "Son")]),
        row(N_OWNER, "Dial Fourth", [], on_crm=True)]
OWNER = {"uuid": OW, "first_name": "Dorn", "last_name": "Quillfeather", "company": None, "deceased": False,
         "skiptrace_attempts": 0, "address": {"street": "12 Test Ln", "city": "Knoxville", "state": "TN", "postal_code": "37900"},
         "phones": [{"number": N_OWNER, "type": "LANDLINE", "tags": ["Dial Fourth"], "status": "UNKNOWN"}]}


def record(**over):
    r = {"uuid": U, "id": 4101, "tags": ["Courthouse Data"], "lists": ["Obituary"], "notes": "", "status": None,
         "address": {"street": "12 Test Ln", "city": "Knoxville", "state": "TN", "postal_code": "37900", "county": "Knox"},
         "owner": copy.deepcopy(OWNER), "secondary_owners": []}
    r.update(over)
    return r


def people_index():
    def p(key, first, last, street, phones, traced=True, **kw):
        """`traced`: the person's own skip trace returned these numbers for them as the subject."""
        return dict({"key": key, "name": "%s %s" % (first, last), "first": first, "last": last, "street": street,
                     "city": "Knoxville", "state": "TN", "zip": "37900", "is_subject": False, "score_worthy": True,
                     "own_trace": traced,
                     "phones": [{"number": n, "ss_type": "mobile", "sources": ["SmartSkip"], "on_crm": False,
                                 "crm_status": None, "crm_tags": [], "own_trace": traced} for n in phones]}, **kw)
    return [p("k-dorn", "Dorn", "Quillfeather", "12 Test Ln", [], traced=False, is_subject=True),
            p("k-marla", "Marla", "Quillfeather", "12 Test Ln", [N_MARLA]),
            p("k-ines", "Ines", "Brackwater", "88 Sample Ct", [N_INES]),
            p("k-pell", "Pell", "Quillfeather", "41 Example Rd", [N_PELL, N_DROP])]


def assembled(an=None, rec=None, dial=None, research=None, scores=None):
    """One record the way dream100_dp.assemble hands it to the write-back."""
    rec = rec or record()
    people = people_index()
    return {"slug": SLUG, "an": an or analysis(), "research": research or research_file(),
            "dial": copy.deepcopy(dial or DIAL), "people": people, "pidx": {p["key"]: p for p in people},
            "scores": scores or {},
            "x": {"uuid": rec["uuid"], "owner_uuid": rec["owner"]["uuid"], "record": rec, "title_owner": "QUILLFEATHER DORN W",
                  "row": {"street": "12 Test Ln", "owner": wb.owner_label(rec["owner"]), "mail_street": "12 Test Ln"}}}


def cfg(**on):
    """The settings with every name cleared first, then the ones given."""
    return wb.settings({k: str(v) for k, v in on.items()})


@contextlib.contextmanager
def env(**values):
    """The write-back settings exactly as given. Every one is cleared first, so nothing this
    machine's own .env carries can change what a check sees."""
    saved = {k: os.environ.get(k) for k in wb.SETTING_NAMES}
    for k in wb.SETTING_NAMES:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k in wb.SETTING_NAMES:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


# ------------------------------------------------------------- a fake DataSift --

class FakeDataSift:
    """One account in memory, answering exactly the routes the research write-back calls. Any other
    route is an error, so a write nobody planned cannot pass unseen."""
    FIELDS = ("Owner Deceased", "Date of Death", "Decedent Name", "Obituary URL", "Decision Maker", "DM Relationship")

    def __init__(self, records, fields=FIELDS, relink=False, selects=None):
        self.records = {r["uuid"]: r for r in records}
        self.fields = {label: "fld-%d" % i for i, label in enumerate(fields)}
        self.selects = selects or {}          # label -> the option labels of a select field
        self.values, self.messages, self.documents = {}, {}, {}
        self.calls, self.uploads, self.fail, self.relink, self._n = [], [], {}, relink, 0

    def _id(self, kind):
        self._n += 1
        return "%s-%d" % (kind, self._n)

    def owner(self, owner_uuid):
        return next(r["owner"] for r in self.records.values() if r["owner"]["uuid"] == owner_uuid)

    def writes(self, part=""):
        return [(m, p, b) for m, p, b, read in self.calls if not read and part in p]

    def post_file(self, url, fields, name, data):
        self.uploads.append((url, name, len(data)))
        return self.fail.get("upload", 204)

    def call(self, path, method="GET", body=None, headers=None, _retry=True):
        read = method == "GET" or (headers or {}).get("x-http-method-override") == "GET"
        self.calls.append((method, path, copy.deepcopy(body), read))
        bits = path.split("?")[0].strip("/").split("/")[2:]          # after api/internal
        if bits == ["custom-fields"]:
            return {"results": [{"label": label, "uuid": fid, "field_type": "select" if label in self.selects else "text",
                                 "options": [{"label": o, "uuid": "opt-" + o} for o in self.selects.get(label, [])]}
                                for label, fid in self.fields.items()]}
        if bits == ["property"] and read:
            owners = set(body["query"]["must"]["owners"])
            hit = [{"uuid": u} for u, r in self.records.items() if r["owner"]["uuid"] in owners]
            return {"count": len(hit), "results": hit[body["offset"]:body["offset"] + body["limit"]]}
        if bits[0] == "owner":
            ow = self.owner(bits[1])
            if len(bits) == 2 and method == "PATCH":
                ow.update(body)
            elif len(bits) == 2:
                return copy.deepcopy(ow)
            elif bits[2] == "upsert-phones":
                for p in body["phones"]:
                    ow["phones"] = [q for q in ow["phones"] if q["number"] != p["number"]] + [copy.deepcopy(p)]
            elif bits[2] == "skiptrace-attempts":
                assert isinstance(body["skiptrace_attempts"], int), "the counter route takes the total as an integer"
                ow["skiptrace_attempts"] = body["skiptrace_attempts"]
            else:
                raise AssertionError("unexpected route %s %s" % (method, path))
            return {}
        rec = self.records[bits[1]]
        rest = bits[2:]
        if not rest:
            if method == "GET":
                if self.fail.pop("first read", None):
                    raise OSError("connection reset")
                return copy.deepcopy(rec)
            if "tags" in body:
                rec["tags"] += [t for t in body["tags"] if t not in rec["tags"]]      # a PATCH of tags only adds
            if "owner" in body:
                if self.relink:        # the other way a server could answer: a new owner record is attached
                    rec["owner"] = dict(rec["owner"], uuid=self._id("own"), deceased=False, phones=[], skiptrace_attempts=0)
                rec["owner"].update({k: v for k, v in body["owner"].items() if k != "address"})
                rec["owner"]["address"] = dict(body["owner"].get("address") or rec["owner"]["address"])
            return copy.deepcopy(rec)
        if rest == ["add-tags"]:
            assert isinstance(body["tags"], str), "add-tags takes one title, as a string"
            if body["tags"] not in rec["tags"]:
                rec["tags"].append(body["tags"])
            return {"new_tags": [body["tags"]]}
        if rest == ["add-notes"]:
            rec["notes"] = (rec["notes"] + "\n" + body["notes"]).strip()
            return {}
        if rest[0] == "message":
            msgs = self.messages.setdefault(rec["uuid"], [])
            if len(rest) == 1 and method == "GET":
                return {"results": copy.deepcopy(msgs)}
            if len(rest) == 1:
                msgs.append({"uuid": self._id("msg"), "message": body["message"], "pinned": False})
                return copy.deepcopy(msgs[-1])
            m = next(x for x in msgs if x["uuid"] == rest[1])
            if rest[-1] == "pin":
                m["pinned"] = True
            else:
                m["message"] = body["message"]
            return copy.deepcopy(m)
        if rest[0] == "custom-field":
            vals = self.values.setdefault(rec["uuid"], {})
            if method == "GET":
                return [{"custom_field": {"label": k}, "value": v} for k, v in vals.items()]
            by_id = {fid: label for label, fid in self.fields.items()}
            vals.update({by_id[p["field_uuid"]]: p["value"] for p in body})
            return {}
        if rest[0] == "document":
            docs = self.documents.setdefault(rec["uuid"], [])
            if rest[1:] == ["presigned-url"]:
                return self.fail.get("target", {"presigned_post": {"url": "https://files.example.test/up", "fields": {"key": "k"}},
                                                "storage_key": self._id("store")})
            if len(rest) == 1 and method == "GET":
                return {"results": copy.deepcopy(docs)}
            if len(rest) == 1:
                docs.append(dict(body, uuid=self._id("doc")))
                return copy.deepcopy(docs[-1])
            if method == "DELETE":
                docs[:] = [x for x in docs if x["uuid"] != rest[1]]
                return ""
        raise AssertionError("unexpected route %s %s" % (method, path))


# --------------------------------------------------------------------- run --

def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.MODE, config.RUNS_DIR, config.RUN_CAP_USD, config.DAILY_CAP_USD, config.RESEARCH_DAILY_CAP_USD,
            config.FINAL_RESERVE_USD)
    store.init()
    try:
        with env():
            _rules(check)
            _shares(check)
            _free_look(check, tmp)
            _api_helpers(check, tmp)
            _chain(check, tmp)
            _order(check, tmp, store)
    finally:
        (config.MODE, config.RUNS_DIR, config.RUN_CAP_USD, config.DAILY_CAP_USD, config.RESEARCH_DAILY_CAP_USD,
         config.FINAL_RESERVE_USD) = keep


def _rules(check) -> None:
    print("  settings")
    s = wb.settings({})
    check(s["third_tier"] == "reserve" and s["verified_only"] is True and s["order"] == "trace_first",
          "with nothing set, the two rules that only remove a write are on and the order is today's")
    check(not any(s[k] for k in ("level", "heir_tag", "skiptrace_counter", "litigator", "document", "owner_swap")),
          "with nothing set, every write this chain has not proven on a real record is off")
    odd = wb.settings({"AGENTS_DP_THIRD_TIER": "everything", "AGENTS_DP_VERIFIED_ONLY": "maybe", "AGENTS_DP_ORDER": "sideways",
                       "AGENTS_DP_OWNER_SWAP": "maybe", "AGENTS_DP_LITIGATOR": "sure"})
    check(odd["third_tier"] == "reserve" and odd["verified_only"] and odd["order"] == "trace_first"
          and not odd["owner_swap"] and not odd["litigator"],
          "a value nobody can read keeps the default, and every default is the safe side")
    check(wb.settings({"AGENTS_DP_VERIFIED_ONLY": "0"})["verified_only"] is False
          and wb.settings({"AGENTS_DP_LEVEL": "on", "AGENTS_DP_LEVEL_FIELD": " DP Level "})["level_field"] == "DP Level",
          "a switch is read as written, and the level field can carry another name")

    print("  tier rule (decision D12)")
    items = [item(N_MARLA, "Dial First"), item(N_INES, "Dial Second"), item(N_PELL, "Dial Third")]
    kept, held = wb.tier_rule(DIAL, items)
    check([p["number"] for p in kept] == [N_MARLA, N_INES], "Dial First and Dial Second numbers go on the record")
    check([h["number"] for h in held] == [N_PELL] and "reserve" in held[0]["why"],
          "a Dial Third number is held in reserve while the record has a better one, and the reason is kept")
    only3 = [row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")]), row(N_OWNER, "Dial Fourth", [], on_crm=True)]
    kept, held = wb.tier_rule(only3, [item(N_PELL, "Dial Third")])
    check([p["number"] for p in kept] == [N_PELL] and not held,
          "a Dial Third number is written when the record has nothing better: no record is left with nothing to dial")
    on_crm_first = [row(N_OWNER, "Dial First", [], on_crm=True), row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")])]
    check(wb.tier_rule(on_crm_first, [item(N_PELL, "Dial Third")])[0] == [],
          "a First number already on the record counts as something better: the Third stays in reserve")
    dead_first = [row(N_OWNER, "Dial First", [], on_crm=True, status="WRONG"), row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")])]
    check(len(wb.tier_rule(dead_first, [item(N_PELL, "Dial Third")])[0]) == 1,
          "a First number a caller marked wrong is not something better")
    tagged = {N_OWNER: {"status": "UNKNOWN", "tags": ["Dial Second"]}}
    check(wb.tier_rule([row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")])], [item(N_PELL, "Dial Third")],
                       crm_phones=tagged)[0] == [],
          "a number on the record that carries a First or Second tag counts even when this run did not score it")
    low = [item(N_DROP, "Drop"), item("8655550118", "Dial Fourth"), {"number": "8655550117", "tags": ["SmartSkip"]}]
    for mode in wb.THIRD_MODES:
        kept, held = wb.tier_rule(DIAL + [row("8655550118", "Dial Fourth"), row("8655550117", "")], low, mode)
        check(kept == [] and len(held) == 3, "a Drop, a Dial Fourth and an unscored number are never written (%s)" % mode)
    check([p["number"] for p in wb.tier_rule(DIAL, items, "always")[0]] == [N_MARLA, N_INES, N_PELL],
          "always is the rule before: First to Third")
    check([p["number"] for p in wb.tier_rule(only3, [item(N_PELL, "Dial Third")], "never")[0]] == [],
          "never writes no Third number even when it is all the record has")
    two = DIAL[:2] + [row(N_PELL, "Dial Third", [reach("Pell Quillfeather", "Son")]),
                      row("8655550114", "Dial Third", [reach("Marla Quillfeather", "Wife")])]
    kept, held = wb.tier_rule(two, items + [item("8655550114", "Dial Third")], "person")
    check([p["number"] for p in kept] == [N_MARLA, N_INES, N_PELL] and [h["number"] for h in held] == ["8655550114"],
          "person keeps a Third number for someone with no First or Second of their own, and holds it for someone who has one")
    check([r["number"] for r in wb.dial_first(DIAL)] == [N_MARLA, N_INES] and [r["number"] for r in wb.dial_first(only3)] == [N_PELL],
          "a call order names First and Second numbers, and a Third only when there is nothing better")

    print("  who counts as verified")
    an, rs = analysis(), research_file()
    v = wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], cache(**{OBIT: OBIT_TEXT}))
    check(v["Marla Quillfeather"]["verified"] and v["Marla Quillfeather"]["kind"] == "obituary" and v["Marla Quillfeather"]["number"] == N_MARLA,
          "a signer the opened obituary names, with a number matched to her name, is verified")
    check(v["Ines Brackwater"]["documented"] and not v["Ines Brackwater"]["verified"] and "no number matches" in v["Ines Brackwater"]["why"],
          "named in the obituary but with no number in her name: a lead, not verified")
    searched = {"search": {}, "index": {}, "page": {}}
    v = wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], searched)
    check(not v["Marla Quillfeather"]["documented"] and not v["Marla Quillfeather"]["verified"],
          "an obituary that only turned up in a search, and was never opened, verifies nobody")
    failed = {"page": {OBIT: {"ok": False, "text": "", "how": "tried direct(403)"}}}
    check(not wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], failed)["Marla Quillfeather"]["documented"],
          "a page that could not be opened verifies nobody")
    stranger = cache(**{OBIT: "Corwin Aldermoss, 77, died Tuesday. Survived by his wife Marla Quillfeather and friends. " * 3})
    check(not wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], stranger)["Marla Quillfeather"]["documented"],
          "an opened page that is not the owner's obituary is not evidence, whatever names it carries")
    an_g = analysis(people=analysis()["people"] + [who("Garrick Quillfeather", "Son", key="")])
    v = wb.verified_people(an_g, rs, DIAL + [row("8655550115", "Dial First", [reach("Garrick Quillfeather", "Son")])],
                           dict(VERIFY, **name_check(("8655550115", "Garrick Quillfeather", True))), [OBIT], cache(**{OBIT: OBIT_TEXT}))
    check(not v["Garrick Quillfeather"]["documented"] and not v["Garrick Quillfeather"]["verified"],
          "a person the obituary lists only among those who died first is not named as a survivor")
    check(wb.named_among_living("She is survived by a son, Pell Quillfeather.", "Pell Quillfeather")
          and not wb.named_among_living("She is survived by a son, Pell.", "Pell")
          and not wb.named_among_living("Survived by her son Pell Quillfeather.", "Wren Quillfeather"),
          "a name is a first and a last name found in the text; one word is not a name")
    wrong = [dict(r, do_not_dial=True) if r["number"] == N_MARLA else r for r in DIAL]
    check(not wb.verified_people(an, rs, wrong, VERIFY, [OBIT], cache(**{OBIT: OBIT_TEXT}))["Marla Quillfeather"]["verified"],
          "a matched number that may not be dialed (wrong party, a caller's mark, a litigator) does not verify")
    other = name_check((N_MARLA, "Corwin Aldermoss", True))
    check(not wb.verified_people(an, rs, DIAL, other, [OBIT], cache(**{OBIT: OBIT_TEXT}))["Marla Quillfeather"]["verified"],
          "a number matched to somebody else's name does not verify her")
    for state in ("Unknown", "Deceased"):
        an_u = analysis(people=[who("Marla Quillfeather", "Wife", living=state, role="decision maker")])
        got = wb.verified_people(an_u, rs, DIAL, VERIFY, [OBIT], cache(**{OBIT: OBIT_TEXT}))["Marla Quillfeather"]
        check(not got["verified"] and "not shown living" in got["why"], "a signer whose status is %s is never verified" % state)
    v = wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], None)
    check(v["Marla Quillfeather"]["verified"] and "visited" in wb.evidence(rs, an, [OBIT], None)[0]["how"],
          "with no page cache (the Claude researcher), a visited obituary and its verbatim survivors paragraph stand in")
    check(not wb.verified_people(an, rs, DIAL, VERIFY, [], None)["Marla Quillfeather"]["documented"],
          "with no page cache, an obituary that is not in the run's visited list verifies nobody")
    rs_court = research_file(decedents=[], people=[{"name": "Pell Quillfeather", "source_url": COURT,
                                                   "quote": "Letters of administration issued to Pell Quillfeather"}])
    an_court = analysis(death=dict(analysis()["death"], obituary_url=""))
    v = wb.verified_people(an_court, rs_court, DIAL, VERIFY, [COURT],
                           cache(**{COURT: "Estate of Dorn Quillfeather. Letters of administration issued to Pell Quillfeather, administrator. " * 3}))
    check(v["Pell Quillfeather"]["verified"] and v["Pell Quillfeather"]["kind"] == "court",
          "a court record opened in the run that names the representative verifies him, with a number in his name")

    print("  the Heir Identified tag and the research level")
    good = wb.verified_people(an, rs, DIAL, VERIFY, [OBIT], cache(**{OBIT: OBIT_TEXT}))
    check(wb.heir_identified(an, good)[0], "a confirmed death and a verified signer earn the tag Heir Identified")
    check(wb.heir_identified(analysis(death=dict(an["death"], owner_deceased=False)), good) == (False, "the owner's death is not confirmed"),
          "no tag while the owner's death is not confirmed by the guard, whoever is verified")
    none = wb.verified_people(an, rs, DIAL, {}, [OBIT], cache(**{OBIT: OBIT_TEXT}))
    check(wb.heir_identified(an, none) == (False, "no signer is verified yet"), "no tag while no signer is verified")
    check(wb.research_level(an)[0] == 4, "a dead owner with more than one signer is level 4")
    one = analysis(people=analysis()["people"][:1], must_sign=analysis()["must_sign"][:1])
    check(wb.research_level(one)[0] == 3, "a dead owner with one signer is level 3")
    alive = analysis(death=dict(an["death"], owner_deceased=False), level="Level 3: deceased owner")
    check(wb.research_level(alive)[0] == 2, "the research's own level 3 does not stand without a confirmed death: level 2")
    check(wb.research_level(dict(alive, level="Level 4, an installment contract clouds the title"))[0] == 4,
          "a title the research calls level 4 is level 4 with or without a death")
    page = cache(**{OBIT: OBIT_TEXT})
    with_tag = wb.decide(assembled(), verify=VERIFY, urls=[OBIT], webcache=page, cfg=cfg(AGENTS_DP_HEIR_TAG=1, AGENTS_DP_LEVEL=1))
    check(with_tag["tags"] == [wb.HEIR_TAG] and with_tag["fields"][wb.LEVEL_FIELD] == "4",
          "switched on, the plan carries the heir tag and the level in its field")
    plain_run = wb.decide(assembled(), verify=VERIFY, urls=[OBIT], webcache=page, cfg=cfg())
    check(plain_run["tags"] == [] and wb.LEVEL_FIELD not in plain_run["fields"] and plain_run["level"][0] == 4,
          "with nothing set the level is worked out and reported, and neither it nor the heir tag is written")
    check(wb.decide(assembled(an=alive), verify=VERIFY, urls=[OBIT], webcache=page, cfg=cfg(AGENTS_DP_HEIR_TAG=1))["tags"] == []
          and wb.decide(assembled(), verify={}, urls=[OBIT], webcache=page, cfg=cfg(AGENTS_DP_HEIR_TAG=1))["tags"] == [],
          "switched on, the heir tag is still refused without a confirmed death, and without a verified signer")
    lines = an["must_sign"] + [{"who": "Successor trustee of the Quillfeather Trust"}, {"who": "Marla Quillfeather (wife, 12 Test Ln)"},
                               {"who": "OR the executor of the estate, if a probate is opened"}]
    check(wb.signer_names(dict(an, must_sign=lines)) == ["Marla Quillfeather", "Ines Brackwater", "Pell Quillfeather"],
          "signers are counted once each, and a line about a role or a what-if names nobody")
    check(wb.signer_names({"must_sign": [{"who": "Marla Quillfeather and Pell Quillfeather"}], "people": []}) ==
          ["Marla Quillfeather", "Pell Quillfeather"], "two people on one signer line are two signers")

    print("  the decision maker fields")
    owners = ["Dorn Quillfeather", "QUILLFEATHER DORN W", "Dorn W Quillfeather"]
    f, held = wb.filter_fields(an["crm_fields"], an, good, owners)
    check(f == an["crm_fields"] and not held, "a verified heir may be written as the decision maker")
    ines = dict(an["crm_fields"], **{"Decision Maker": "Ines Brackwater", "DM Relationship": "Daughter"})
    f, held = wb.filter_fields(ines, an, good, owners)
    check("Decision Maker" not in f and "DM Relationship" not in f and f["Owner Deceased"] == "Yes" and "not verified" in held[0],
          "an unverified signer is not written as the decision maker, and every other field still is")
    dorn = dict(an["crm_fields"], **{"Decision Maker": "Dorn Quillfeather"})
    check("has died" in wb.filter_fields(dorn, an, good, owners)[1][0], "the dead owner is never written as the decision maker")
    living = analysis(owner_status="Living", death={"decedent": "", "owner_deceased": False},
                      people=[who("Dorn Quillfeather", "Owner of record", role="owner")],
                      crm_fields={"Decision Maker": "Dorn Quillfeather", "DM Relationship": "Owner"})
    check(wb.filter_fields(living["crm_fields"], living, {}, owners) == (living["crm_fields"], []),
          "a living owner of record is the decision maker with nothing to verify")
    pr_owner = ["Pell Quillfeather", "QUILLFEATHER DORN W", "Dorn W Quillfeather"]
    probate = dict(an["crm_fields"], **{"Decision Maker": "Pell Quillfeather", "DM Relationship": "Personal representative"})
    check(wb.filter_fields(probate, an, none, pr_owner)[1] == [],
          "on a probate record the owner is the living representative the court named, and stays the decision maker")
    junior = analysis(people=[who("Dorn Quillfeather Jr", "Son")],
                      crm_fields={"Decision Maker": "Dorn Quillfeather Jr", "DM Relationship": "Son"})
    held = wb.filter_fields(junior["crm_fields"], junior, {}, owners)[1]
    check(held and "not verified" in held[0] and "has died" not in held[0],
          "a son named for his father is not his dead father, and is not the owner of record either: he needs verifying")
    jr_owner = analysis(owner_status="Living", death={"decedent": "", "owner_deceased": False},
                        people=[who("Corwin Aldermoss Jr", "Owner of record", role="decision maker")],
                        crm_fields={"Decision Maker": "Corwin Aldermoss Jr", "DM Relationship": "Owner"})
    check(wb.filter_fields(jr_owner["crm_fields"], jr_owner, {}, ["Corwin Aldermoss"])[1] == [],
          "a living owner whose Jr the CRM dropped is still the owner of record: with nobody dead there is no one to confuse him with")
    role_line = {"Decision Maker": "Successor trustee of the Quillfeather Family Trust"}
    check(wb.filter_fields(role_line, an, {}, owners) == (role_line, []), "a line about a role names nobody and passes")
    check(wb.filter_fields({"Decision Maker": "Corwin Aldermoss"}, an, good, owners) == ({"Decision Maker": "Corwin Aldermoss"}, []),
          "a name the research file does not hold is left as written: the rule removes only what it can show is wrong")
    pair = analysis(owner_status="Living", death={"decedent": "", "owner_deceased": False},
                    people=[who("Dorn Quillfeather", "Owner of record", role="owner"),
                            who("Marla Brackwater", "Owner of record", living="Unknown", role="owner"),
                            who("Marla Quillfeather", "Sister", living="Unknown", role="channel")],
                    crm_fields={"Decision Maker": "Dorn Quillfeather and Marla Brackwater", "DM Relationship": "Owners"})
    check(wb.filter_fields(pair["crm_fields"], pair, {}, ["Dorn Quillfeather"]) == (pair["crm_fields"], []),
          "two owners of record named together stay, one of them not yet confirmed living, and a third person whose "
          "first and last name merely appear among their words is not read into the field")
    check(wb.named_in("Dorn and Marla Quillfeather", "Dorn Quillfeather") and wb.named_in("Quillfeather, Marla J", "Marla Quillfeather")
          and wb.named_in("Marla J. Quillfeather (wife)", "Marla Quillfeather")
          and not wb.named_in("Dorn Quillfeather and Marla Brackwater", "Marla Quillfeather")
          and not wb.named_in("Dorn Quillfeather, Marla Brackwater", "Dorn Brackwater"),
          "a person is named where the first name sits beside the last, in either order or sharing one surname, not "
          "where the two words belong to two other people")
    estate = dict(an["crm_fields"], **{"Decision Maker": "Estate of Dorn Quillfeather, c/o Marla Quillfeather"})
    check(wb.filter_fields(estate, an, good, owners)[1] == [] and "Marla Quillfeather is not verified" in wb.filter_fields(estate, an, none, owners)[1][0],
          "the estate of the dead owner is a role, not the dead owner: the field stands or falls on the living person it names")
    dec = wb.decide(assembled(an=analysis(crm_fields=ines)), verify=VERIFY, urls=[OBIT], webcache=cache(**{OBIT: OBIT_TEXT}),
                    cfg=cfg(AGENTS_DP_VERIFIED_ONLY=0))
    check(dec["fields"]["Decision Maker"] == "Ines Brackwater", "with the rule switched off the fields pass as the analysis wrote them")

    print("  the owner's deceased mark")
    check(wb.deceased_flag(an, OWNER) == (True, ""), "the owner is marked deceased when the record's owner is the person who died")
    may, why = wb.deceased_flag(an, dict(OWNER, first_name="Pell"))
    check(not may and "is not the person who died" in why,
          "a probate record's owner is the living representative: the death on the title does not mark that person dead")
    check(wb.deceased_flag(alive, OWNER) == (False, ""), "no mark at all while the guard has not confirmed the death")
    sr = analysis(death=dict(an["death"], decedent="Dorn Quillfeather Sr."))
    check(not wb.deceased_flag(sr, dict(OWNER, last_name="Quillfeather Jr"))[0], "a Sr. who died does not mark his Jr. dead")
    check(wb.deceased_flag(an, dict(OWNER, first_name="Pell"), strict=False)[0], "switched off, the mark follows the death alone, as before")
    widower = analysis(people=[who("Dorn Quillfeather", "Husband, on the title", role="owner")],
                       death=dict(an["death"], decedent="Marla Quillfeather"))
    check(not wb.deceased_flag(widower, OWNER)[0],
          "where a husband and wife both hold title and she died, the husband on the record is not marked dead")
    check(wb.deceased_flag(an, dict(OWNER, first_name="D"))[0] and wb.deceased_flag(an, dict(OWNER, first_name="Corwin"))[0],
          "an owner stored by an initial, or one the research does not name, is still marked: only a person the research "
          "shows living is spared, so a dead owner is never left unmarked by this rule")
    namesake = analysis(people=[who("Dorn Quillfeather", "Son")], death=dict(an["death"], decedent="Dorn Quillfeather"))
    check(wb.deceased_flag(namesake, OWNER)[0],
          "a living son who carries the dead owner's exact name does not stop the mark: the name on the record is as much the father's")
    pr_fields = wb.decide(assembled(rec=record(owner=dict(copy.deepcopy(OWNER), first_name="Pell"))), cfg=cfg())
    check("Owner Deceased" not in pr_fields["fields"] and pr_fields["fields"]["Decedent Name"] == "Dorn Wesley Quillfeather"
          and any(h.startswith("Owner Deceased not written") for h in pr_fields["held_fields"]),
          "the Owner Deceased field follows the mark: it is not written about a living owner, and the decedent's own fields still are")
    pr_rec = assembled(rec=record(owner=dict(copy.deepcopy(OWNER), first_name="Pell")))
    check(wb.decide(pr_rec, cfg=cfg(AGENTS_DP_VERIFIED_ONLY=0))["deceased"][0] is True
          and wb.decide(pr_rec, cfg=cfg(AGENTS_DP_VERIFIED_ONLY=0, AGENTS_DP_OWNER_SWAP=1))["deceased"][0] is False,
          "with the owner change switched on the name check on the deceased mark cannot be switched off: the heir it "
          "puts on the record must never be marked dead by a later run")

    print("  the owner change")
    d = assembled()
    plan, refused = wb.swap_plan(d, good)
    check(plan and plan["person"] == "Marla Quillfeather" and plan["to"]["first_name"] == "Marla"
          and plan["to"]["address"] == {"street": "12 Test Ln", "city": "Knoxville", "state": "TN", "postal_code": "37900"},
          "the owner changes to the first verified person in the research's call order, at her own address")
    check(plan["before"]["first_name"] == "Dorn" and "Was: Dorn Quillfeather" in plan["note"] and "property address did not change" in plan["note"],
          "the plan keeps what the record held before, and the note says who was replaced")
    check(wb.swap_plan(d, none) == (None, ["no verified person to change the owner to"]), "nobody verified: the owner is not changed")
    check(wb.swap_plan(assembled(an=alive), good)[1] == ["the owner's death is not confirmed"], "no confirmed death: the owner is not changed")
    check("is not the person who died" in wb.swap_plan(d, good, owner=dict(OWNER, first_name="Marla"))[1][0],
          "an owner who is not the person who died is never replaced (a representative, or an heir an earlier run put there)")
    pell_first = assembled(an=analysis(call_order=[{"who": "Pell Quillfeather", "number": N_PELL, "why": "son"}]))
    check(wb.swap_plan(pell_first, good)[0]["person"] == "Pell Quillfeather" and wb.swap_plan(pell_first, good)[0]["to"]["address"]["street"] == "41 Example Rd",
          "the research's order decides who it is, and the address is that person's own, never the dead owner's")
    maiden = assembled(an=analysis(call_order=[{"who": "Ines Brackwater", "number": N_INES, "why": "daughter"}]))
    maiden["pidx"]["k-ines"].update(first="Ines", last="Quillfeather")       # the trace found her under her maiden name
    ines_ok = wb.verified_people(analysis(), research_file(), DIAL, name_check((N_INES, "Ines Brackwater", True)), [OBIT],
                                 cache(**{OBIT: OBIT_TEXT}))
    to = wb.swap_plan(maiden, ines_ok)[0]["to"]
    check((to["first_name"], to["last_name"], to["address"]["street"]) == ("Ines", "Brackwater", "88 Sample Ct"),
          "the name written is the one the research and the number check stand behind, not the maiden name her trace ran under")
    no_zip = assembled()
    no_zip["pidx"]["k-marla"]["zip"] = ""
    check("no complete mailing address" in wb.swap_plan(no_zip, good)[1][0], "an address missing its zip is not a mailing address: no change")
    untraced = assembled()
    for q in untraced["pidx"]["k-marla"]["phones"]:
        q["own_trace"] = False
    check("no address their own skip trace confirmed" in wb.swap_plan(untraced, good)[1][0],
          "an address that only rode in on the dead owner's relative list is not her own until her own trace confirms it: no change")
    mixed = assembled(an=analysis(people=[who("Marla Quillfeather", "Wife", role="decision maker", key="k-marla",
                                              trace_address={"street": "9 Elsewhere Way", "city": "", "state": "", "zip": ""})]
                                  + analysis()["people"][1:]))
    check("no complete mailing address" in wb.swap_plan(mixed, good)[1][0],
          "an address is taken whole from one source: a street from one place is never joined to a zip from another")
    flagged = assembled()
    flagged["pidx"]["k-marla"]["own_trace_deceased_flag"] = True
    check("flags Marla Quillfeather as deceased" in wb.swap_plan(flagged, good)[1][0], "a skip trace that flags the heir as deceased stops the change for a person to check")
    trust = assembled(rec=record(owner=dict(OWNER, first_name=None, last_name=None, company="Quillfeather Family Trust")))
    check(wb.swap_plan(trust, good)[1] == ["the owner is a company or a trust"], "a company or a trust on the record is not replaced by this rule")
    check(wb.decide(d, verify=VERIFY, urls=[OBIT], webcache=cache(**{OBIT: OBIT_TEXT}), cfg=cfg())["swap"] is None,
          "with nothing set the owner change is not even planned")

    print("  litigators")
    scores = {N_INES: {"tier": "DNC", "litigator": True}, "8655559999": {"tier": "DNC", "litigator": True}}
    check(wb.litigator_numbers(DIAL, people_index(), scores) == [N_INES],
          "a litigator flag counts for a number tied to this record, not for a number scored for another")
    check(wb.litigator_numbers(DIAL, [], {}, {"x": {"number": "(865) 555-0111", "name": "Marla Quillfeather", "litigator": True}}) == [N_MARLA],
          "the name check's litigator flag counts too")
    off_sheet = [dict(p, phones=p["phones"] + [{"number": "8655550120"}]) if p["key"] == "k-pell" else p for p in people_index()]
    check(wb.litigator_numbers(DIAL, off_sheet, {"8655550120": {"litigator": True}}) == ["8655550120"],
          "a flagged number on anyone in the record's people list counts, on the dial sheet or not")
    d_lit = assembled(scores=scores)
    check(wb.decide(d_lit, cfg=cfg())["suppress"] is False and wb.decide(d_lit, cfg=cfg())["litigators"] == [N_INES],
          "with nothing set a litigator is reported and nothing is tagged")
    on = wb.decide(d_lit, verify=VERIFY, urls=[OBIT], webcache=cache(**{OBIT: OBIT_TEXT}),
                   cfg=cfg(AGENTS_DP_LITIGATOR=1, AGENTS_DP_HEIR_TAG=1, AGENTS_DP_OWNER_SWAP=1))
    check(on["suppress"] and on["tags"] == [] and on["swap"] is None,
          "a record a litigator is tied to gets no heir tag and no owner change: it is leaving every lane")

    print("  the skip trace counter")
    led = {"smartskip": {"usd": 0.15, "when": "2026-11-13T10:00:00"}, "trestle": {"usd": 0.1, "when": "x"}}
    key = wb.trace_key(led)
    check(wb.counter_plan(0, True, None, key) == {"before": 0, "total": 1, "key": key}, "numbers landed: the counter goes up by one, as a total")
    check(wb.counter_plan(3, False, None, key) is None, "no numbers landed: the counter is left alone")
    check(wb.counter_plan(1, True, {"ok": True, "key": key}, key) is None, "the same trace is counted once, however often the write-back runs")
    check(wb.counter_plan(1, True, {"ok": False, "key": key}, key)["total"] == 2, "a counter write that did not stick is tried again")
    led2 = dict(led, smartskip_heirs_ab12cd={"usd": 0.3, "when": "2026-11-14T09:00:00"})
    check(wb.trace_key(led2) != key and wb.counter_plan(1, True, {"ok": True, "key": key}, wb.trace_key(led2))["total"] == 2,
          "a new trace order is a new trace, and counts once more")


def _shares(check) -> None:
    print("  each heir's share")
    an, rs = analysis(), research_file()

    def split(a, r=None):
        got = wb.heir_shares(a, r or rs)
        return {h["name"]: h["fraction"] for h in got["heirs"]} if got and got["computed"] else (got or {}).get("reason")

    got = wb.heir_shares(an, rs)
    check(got["computed"] and split(an) == {"Marla Quillfeather": "1/3", "Ines Brackwater": "1/3", "Pell Quillfeather": "1/3"},
          "a wife and two children: the wife takes the greater of a third or a child's share, here a third each")
    check(abs(sum(h["share"] for h in got["heirs"]) - 1) < 1e-4 and got["decedent"] == "Dorn Wesley Quillfeather" and got["caution"],
          "the shares add up to the whole, and the result says it is the default a title company confirms")
    check(any("Garrick Quillfeather died before the owner" in n for n in got["notes"]),
          "a child who died first with no children named is said out loud, since children of his would change the split")
    one_child = analysis(people=an["people"][:2], must_sign=an["must_sign"][:2])
    check(split(one_child, research_file(decedents=rs["decedents"][:1])) == {"Marla Quillfeather": "1/2", "Ines Brackwater": "1/2"},
          "a wife and one child: half each")
    three = analysis(people=an["people"] + [who("Wren Quillfeather", "Daughter")])
    check(split(three, research_file(decedents=rs["decedents"][:1])) ==
          {"Marla Quillfeather": "1/3", "Ines Brackwater": "2/9", "Pell Quillfeather": "2/9", "Wren Quillfeather": "2/9"},
          "a wife and three children: a third to the wife, the rest in three")
    kids = analysis(people=an["people"][1:], must_sign=an["must_sign"][1:])
    check(split(kids, research_file(decedents=rs["decedents"][:1])) == {"Ines Brackwater": "1/2", "Pell Quillfeather": "1/2"},
          "no spouse: the children share equally")
    grand = analysis(people=an["people"] + [who("Tavi Quillfeather", "Grandson (son of the late Garrick)"),
                                            who("Wren Quillfeather", "Granddaughter (daughter of Garrick)")])
    check(split(grand) == {"Marla Quillfeather": "1/3", "Ines Brackwater": "2/9", "Pell Quillfeather": "2/9",
                           "Tavi Quillfeather": "1/9", "Wren Quillfeather": "1/9"},
          "a child who died first is stood in for by his own children, who split his one share")
    got = wb.heir_shares(grand, rs)
    check(next(h for h in got["heirs"] if h["name"] == "Tavi Quillfeather")["through"] == "Garrick Quillfeather",
          "a grandchild's row says whose share it is")
    junior = research_file(decedents=rs["decedents"][:1] + [{"name": "Dorn Quillfeather Jr", "relationship_to_owner": "Son", "dod": ""}])
    named_for = analysis(people=an["people"] + [who("Tavi Quillfeather", "Grandson (son of Dorn Jr)")])
    got = wb.heir_shares(named_for, junior)
    check({h["name"]: h["fraction"] for h in got["heirs"]}.get("Tavi Quillfeather") == "2/9",
          "a dead son who carried his father's name is still a son: his child takes his share, he is not the owner over again")
    check(any("takes Dorn Quillfeather Jr to have died before the owner" in n for n in got["notes"]),
          "with no dates to check, the result says the split assumes the child died before the owner")
    orphan = analysis(people=an["people"] + [who("Tavi Quillfeather", "Grandson")])
    got = wb.heir_shares(orphan, research_file(decedents=rs["decedents"][:1]))
    check(got["computed"] and "Tavi Quillfeather" not in {h["name"] for h in got["heirs"]}
          and any("Tavi Quillfeather takes no share" in n for n in got["notes"]),
          "a grandchild whose parent is living takes no share, and the result says so")
    two_dead = research_file(decedents=rs["decedents"] + [{"name": "Sorrel Quillfeather", "relationship_to_owner": "Daughter",
                                                          "dod": "2020-01-01"}])
    unclear = analysis(people=an["people"] + [who("Tavi Quillfeather", "Grandson")])
    check("does not say which deceased child" in split(unclear, two_dead),
          "two children died first and the file does not say whose child a grandchild is: no split is shown")
    all_dead = analysis(people=[who("Tavi Quillfeather", "Grandson"), who("Wren Quillfeather", "Granddaughter"),
                                who("Lark Quillfeather", "Granddaughter")],
                        must_sign=[{"who": "Tavi Quillfeather"}, {"who": "Wren Quillfeather"}, {"who": "Lark Quillfeather"}])
    check(split(all_dead, two_dead) == {"Tavi Quillfeather": "1/3", "Wren Quillfeather": "1/3", "Lark Quillfeather": "1/3"},
          "when no child outlived the owner, the grandchildren share equally")
    late = research_file(decedents=rs["decedents"][:1] + [{"name": "Pell Quillfeather", "relationship_to_owner": "Son",
                                                           "dod": "May 1, 2026"}])
    after = analysis(people=an["people"][:2] + [who("Pell Quillfeather", "Son", living="Deceased", role="none")])
    got = wb.heir_shares(after, late)
    check(got["computed"] and {h["name"]: h["fraction"] for h in got["heirs"]}.get("Estate of Pell Quillfeather") == "1/3"
          and any("died after the owner" in n for n in got["notes"]),
          "a child who died after the owner kept his share: it sits in his own estate, and that estate's heirs sign")
    step = analysis(people=an["people"][1:] + [who("Corwin Aldermoss", "Stepson")],
                    must_sign=an["must_sign"][1:] + [{"who": "Corwin Aldermoss"}])
    got = wb.heir_shares(step, research_file(decedents=rs["decedents"][:1]))
    check(got["computed"] and "Corwin Aldermoss" not in {h["name"] for h in got["heirs"]}
          and any("Corwin Aldermoss" in n and "no share by law" in n for n in got["notes"]),
          "a stepchild listed to sign takes no share without a will, and the result says so")
    check(wb.kin_class("Daughter-in-law") == "other" and wb.kin_class("Former wife") == "other"
          and wb.kin_class("Executor", "Executor (son of the owner)") == "child"
          and wb.kin_class("Executor", "Executor (attorney; the son declined)") == "fiduciary",
          "in-laws and former spouses do not inherit, and a representative is kin only where the tie is written beside the role")
    folks = analysis(people=[who("Hollis Quillfeather", "Father"), who("Odessa Quillfeather", "Mother")],
                     must_sign=[{"who": "Hollis Quillfeather"}, {"who": "Odessa Quillfeather"}])
    check(split(folks, research_file(decedents=rs["decedents"][:1])) == {"Hollis Quillfeather": "1/2", "Odessa Quillfeather": "1/2"},
          "no spouse and no descendants: the parents share")
    sibs = analysis(people=[who("Hollis Quillfeather", "Brother"), who("Odessa Brackwater", "Sister")],
                    must_sign=[{"who": "Hollis Quillfeather"}, {"who": "Odessa Brackwater"}])
    check(split(sibs, research_file(decedents=rs["decedents"][:1])) == {"Hollis Quillfeather": "1/2", "Odessa Brackwater": "1/2"},
          "no spouse, descendants or parents: the brothers and sisters share")
    unsure = analysis(people=[an["people"][1], who("Pell Quillfeather", "Son", living="Unknown")])
    got = wb.heir_shares(unsure, research_file(decedents=rs["decedents"][:1]))
    check(got["computed"] and any("Pell Quillfeather is not verified as living" in n for n in got["notes"]),
          "an heir nobody verified as living still holds a share, and the result flags that the split depends on it")
    v = {"Marla Quillfeather": {"verified": True}}
    got = wb.heir_shares(an, rs, verified=v)
    check([h["verified"] for h in got["heirs"]] == [True, False, False], "each row says whether that heir is verified")

    check(wb.heir_shares(analysis(people=an["people"][:1], must_sign=an["must_sign"][:1]), rs) is None,
          "one signer: there is nothing to split, and no shares are returned")
    alive = analysis(death=dict(an["death"], owner_deceased=False))
    check("is not confirmed" in split(alive), "the research says the owner died but the guard has not confirmed it: no estate is divided yet")
    couple = analysis(owner_status="Living", death={"decedent": "", "owner_deceased": False},
                      people=[who("Dorn Quillfeather", "Owner of record", role="owner"), who("Marla Quillfeather", "Owner of record", role="owner")],
                      must_sign=[{"who": "Dorn Quillfeather"}, {"who": "Marla Quillfeather"}])
    check(wb.heir_shares(couple, rs) is None, "two living owners on a title are two sellers, not heirs: no shares are returned at all")
    mortgage = analysis(title={"vesting": "Dorn W Quillfeather, sole owner. Deed of Trust to First Example Bank, Corwin Aldermoss, Trustee; "
                                          "a Substitute Trustee's sale is noticed.", "flags": []})
    check(isinstance(split(mortgage), dict) and wb.in_trust("The Quillfeather Family Trust dated 2011") and wb.in_trust("", "Quillfeather Living Trust")
          and not wb.in_trust("Deed of Trust recorded 2019; Substitute Trustee: Aldermoss Law"),
          "a deed of trust and its trustee are the mortgage, not a trust: the split is still worked out")
    co = analysis(people=an["people"] + [who("Odessa Quillfeather", "Co-owner of record", role="owner")])
    check("is on the title and living" in split(co), "a living co-owner on the title: the deed decides, so no split is shown")
    will = analysis(signing_flags=["The obituary mentions a last will filed with the court"])
    check("points to a will" in split(will), "a will is mentioned: shares follow the will, which was not read")
    execu = analysis(people=[an["people"][0], who("Pell Quillfeather", "Executor (son of the owner)")])
    check("is named as executor" in split(execu), "a named executor means a will: no intestate split is shown")
    both = analysis(signing_flags=["Died intestate per the family", "A last will may exist"])
    check("reads both ways on a will" in split(both), "a file that reads both ways on a will is left to a person")
    check("in a trust" in split(analysis(title={"vesting": "Dorn Quillfeather Revocable Trust", "flags": []})),
          "title in a trust: the trust sets the shares")
    rep = analysis(people=an["people"][:2] + [who("Corwin Aldermoss", "Administrator")])
    check("does not say how they are related" in split(rep, research_file(decedents=rs["decedents"][:1])),
          "a representative whose family tie is not stated could be an heir too: no split is shown")
    hypothetical = analysis(must_sign=an["must_sign"] + [{"who": "OR the executor of the estate, if a probate is opened",
                                                          "status": "Verify", "why": "only if a case is filed"}])
    check(isinstance(split(hypothetical), dict), "a signer line that only supposes an executor does not count as a will")
    great = analysis(people=an["people"] + [who("Lark Quillfeather", "Great-granddaughter")])
    check("three generations" in split(great), "a great-grandchild in the file sends the split to an attorney")


def _free_look(check, tmp: Path) -> None:
    print("  the free look")
    from .research import researcher
    rows = ("Quillfeather, Dorn Wesley | age 81 | B4 | obituary date 3/9/2026\n"
            "Quillfeather, Dorn Lee | age 44 | A2 | obituary date 7/1/1998\n"
            "Quillfeather, Dorn | age 52 | A7 | obituary date 2/2/2026\nCite this lookup as: http://index.example.test")
    asked = []

    def lookup(last, first):
        asked.append((last, first))
        return rows

    rec = record(notes="2026-10\nForeclosure\nAuction: 12/1/2026\nThe heirs of Dorn Quillfeather, deceased, are notified.",
                 owner=dict(OWNER, age=80))
    look = researcher.free_look({"record": rec}, {}, lookup=lookup, today=date(2026, 11, 13))
    check("Auction: 12/1/2026" in look["notice_text"] and look["notice_mentions"] == ["deceased", "heirs of"],
          "the free look carries the notice text on the record and the words in it that point to a death")
    hit = look["obituary_index"]["lookups"][0]
    check(asked == [("Quillfeather", "Dorn")] and [c["age"] for c in hit["possible"]] == ["81"],
          "the county obituary index is asked for the owner, and only a row of the right age and a recent date could be him")
    check(look["stop"] == "" and look["deed"] is None, "an index row stops nothing: it is a lead for the research, not a verdict")
    blount = record(address=dict(record()["address"], county="Blount"))
    asked.clear()
    look_b = researcher.free_look({"record": blount}, {}, lookup=lookup, today=date(2026, 11, 13))
    check(not asked and not look_b["obituary_index"]["covered"]
          and "says nothing" in researcher.free_look_brief(look_b)["county_obituary_index"],
          "the index is the Knoxville paper's: a Blount record is not looked up, and the brief says the gap proves nothing")
    down = researcher.free_look({"record": rec}, {}, lookup=lambda last, first: "index lookup failed: timed out", today=date(2026, 11, 13))
    check(down["obituary_index"]["lookups"][0]["failed"] and down["obituary_index"]["lookups"][0]["possible"] == [],
          "an index that does not answer is recorded as not answering, never as no death")
    company = record(owner=dict(OWNER, first_name=None, last_name=None, company="Quillfeather Holdings LLC"))
    asked.clear()
    researcher.free_look({"record": company}, {}, lookup=lookup)
    check(not asked, "a company is not looked up in an obituary index")
    couple = record(secondary_owners=[{"full_name": "Marla Jean Quillfeather"}])
    researcher.free_look({"record": couple}, {}, lookup=lookup)
    check(asked == [("Quillfeather", "Dorn"), ("Quillfeather", "Marla")], "a co-owner on the record is looked up too")
    owns = researcher.free_look({"record": rec}, {}, deed={"still_owns": True, "total_debt": 91000, "looked_up": "2026-11-12"})
    check(owns["stop"] == "" and owns["deed"]["total_debt"] == 91000, "a saved deed lookup that shows the owner still owns stops nothing and carries the debt")
    sold = researcher.free_look({"record": rec}, {}, deed={"still_owns": False, "last_deed": {"date": "2026-10-30", "grantee": "Aldermoss Homes LLC"}})
    check(sold["stop"] == "The deed records show the owner no longer owns this house (deeded to Aldermoss Homes LLC, on 2026-10-30).",
          "a saved deed lookup that shows the house was deeded away is the one thing that stops a run")
    check(researcher.free_look({"record": rec}, {}, deed={"still_owns": None})["stop"] == "", "a deed lookup that could not tell stops nothing")
    brief = researcher.free_look_brief(look)
    check(brief["how_to_use"].startswith("Free facts") and "lead, not proof" in brief["how_to_use"]
          and brief["county_obituary_index"][0]["could_be_this_owner"][0]["age"] == "81" and "notice_text_on_the_record" in brief,
          "the brief gives the free facts to the research with the warning that an index row is a lead, not proof")

    class Files:
        def __init__(self, files):
            self.files = files

        def load(self, name, default=None):
            return self.files.get(name, default)

    base = {"records.json": [{"slug": SLUG, "record": rec}]}
    check("read_first" not in researcher.record_brief(Files(dict(base)), {"street": "12 Test Ln"}),
          "in today's order there is no free look file, and the research brief is what it always was")
    check("read_first" in researcher.record_brief(Files(dict(base, **{"free_look.json": look})), {"street": "12 Test Ln"}),
          "in the free-first order the research brief carries the free look")
    folder = tmp / "wb_deeds"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (U + ".json")).write_text(json.dumps({"still_owns": True, "source": "a test"}), encoding="utf-8")
    (folder / (U2 + ".json")).write_text("{ not json", encoding="utf-8")
    check(researcher.saved_deed(Files({}), U, folder) == {"still_owns": True, "source": "a test"}
          and researcher.saved_deed(Files({}), "nobody", folder) is None,
          "a deed lookup saved for the record is read from the deed folder; no file means no lookup")
    check(researcher.saved_deed(Files({"deed_lookup.json": {"still_owns": False}}), U, folder) == {"still_owns": False},
          "a deed lookup in the run folder itself wins")
    check(researcher.saved_deed(Files({}), U2, folder) is None, "a deed file that does not parse is treated as no lookup, never as an answer")


def _api_helpers(check, tmp: Path) -> None:
    print("  the writes, one at a time, against a fake DataSift")
    api = FakeDataSift([record(), record(uuid=U2, tags=["Litigator"]), record(uuid="rec-0003", owner=dict(OWNER, uuid="own-0009"))])
    got = wb.suppress_litigator(api, U, OW)
    check(set(api.records[U]["tags"]) >= {"Litigator", "Do Not Market"} and set(api.records[U2]["tags"]) >= {"Litigator", "Do Not Market"},
          "a litigator tags the record and the same owner's other record Litigator and Do Not Market")
    check(api.records[U]["notes"] == wb.LITIGATOR_NOTE and api.records[U2]["notes"] == wb.LITIGATOR_NOTE and len(got["tagged"]) == 2,
          "each tagged record gets the one note saying why")
    check("Litigator" not in api.records["rec-0003"]["tags"], "another owner's record is not touched")
    check(got["tagged"][1]["added"] == ["Do Not Market"], "only the tag a record lacks is added")
    before = len(api.writes())
    again = wb.suppress_litigator(api, U, OW)
    check(len(api.writes()) == before and again["tagged"] == [], "run again, it adds no tag and no second note")
    lone = FakeDataSift([record()])
    real = lone.call

    def flaky(path, method="GET", body=None, headers=None, _retry=True):
        if method == "POST" and path == "/api/internal/property/":
            raise RuntimeError("search down")
        return real(path, method, body, headers)

    lone.call = flaky
    got = wb.suppress_litigator(lone, U, OW)
    check("Litigator" in lone.records[U]["tags"] and got["problems"] and "only this record was tagged" in got["problems"][0],
          "when the owner's other records cannot be listed, this record is still tagged and the gap is said")

    api = FakeDataSift([record()])
    wb.write_counter(api, OW, 1)
    check(api.owner(OW)["skiptrace_attempts"] == 1 and api.writes()[0][2] == {"skiptrace_attempts": 1},
          "the skip trace counter is written as the new total, an integer")

    pdf = tmp / "wb_report.pdf"
    pdf.write_bytes(b"%PDF-1.4 a first report")
    name = wb.document_name(SLUG)
    api = FakeDataSift([record()])
    got = wb.file_document(api, U, pdf, name, prop=4101, post=api.post_file)
    doc = api.documents[U][0]
    check(got["filed"] and doc["filename"] == name and doc["extension"] == "pdf" and doc["size"] == pdf.stat().st_size
          and doc["prop"] == 4101 and doc["storage_key"],
          "the report is sent to the upload target and registered on the record with its name, size and storage key")
    check(api.uploads == [("https://files.example.test/up", name, pdf.stat().st_size)], "the file itself goes to the address the presigned route gave")
    got = wb.file_document(api, U, pdf, name, post=api.post_file)
    check(not got["filed"] and len(api.documents[U]) == 1 and len(api.uploads) == 1, "the same report is not filed twice")
    pdf.write_bytes(b"%PDF-1.4 a second, longer report after new research")
    old = api.documents[U][0]["uuid"]
    got = wb.file_document(api, U, pdf, name, post=api.post_file)
    check(got["filed"] and got["replaced"] == [old] and [x["size"] for x in api.documents[U]] == [pdf.stat().st_size],
          "a new report replaces the older copy under our own name, so one current file stays on the record")
    api.documents[U].append({"uuid": "doc-hand", "filename": "survey.pdf", "size": 9})
    pdf.write_bytes(b"%PDF-1.4 a third report")
    wb.file_document(api, U, pdf, name, post=api.post_file)
    check(any(x["filename"] == "survey.pdf" for x in api.documents[U]), "a document somebody else put on the record is never removed")
    api.fail["upload"] = 403
    pdf.write_bytes(b"%PDF-1.4 a fourth report that the file store refuses")
    kept = copy.deepcopy(api.documents[U])
    try:
        wb.file_document(api, U, pdf, name, post=api.post_file)
        check(False, "a refused upload raises")
    except RuntimeError as exc:
        check("refused the upload" in str(exc) and api.documents[U] == kept,
              "when the file store refuses the upload nothing is registered and the older report stays")
    api.fail = {"target": {"presigned_post": {}, "storage_key": ""}}
    try:
        wb.file_document(api, U, pdf, name, post=api.post_file)
        check(False, "a missing upload target raises")
    except RuntimeError as exc:
        check("did not return an upload target" in str(exc) and api.documents[U] == kept, "no upload target: nothing is sent and nothing is registered")

    good = wb.verified_people(analysis(), research_file(), DIAL, VERIFY, [OBIT], cache(**{OBIT: OBIT_TEXT}))
    plan, _ = wb.swap_plan(assembled(), good)
    for relink in (False, True):
        api = FakeDataSift([record(owner=dict(copy.deepcopy(OWNER), deceased=True))], relink=relink)
        done = wb.swap_owner(api, U, plan)
        ow = api.records[U]["owner"]
        check(done["ok"] and ow["first_name"] == "Marla" and ow["deceased"] is False and done["same_owner"] is (not relink),
              "the owner change writes the name, and the person now named does not carry the dead owner's deceased mark (%s)"
              % ("a new owner attached" if relink else "the same owner renamed"))
        check(api.records[U]["notes"].count("Owner changed by research") == 1 and api.records[U]["address"]["street"] == "12 Test Ln",
              "one note says who was replaced, and the property address is untouched")
    stuck = FakeDataSift([record()])
    real = stuck.call
    stuck.call = lambda path, method="GET", body=None, headers=None, _retry=True: (
        copy.deepcopy(stuck.records[U]) if method == "PATCH" and path.endswith("/property/%s/" % U) else real(path, method, body, headers))
    done = wb.swap_owner(stuck, U, plan)
    check(not done["ok"] and stuck.records[U]["notes"] == "", "an owner change that did not take leaves no note claiming it did")


def _write_run(tmp: Path, name: str, *, an=None, rec=None, scores=None, verify=None, page=True, ledger=True):
    """A run folder on disk, the way the chain leaves it before the write-back."""
    import dream100_dp as dp
    run = dp.Run(str(tmp / name))
    rec = rec or record()
    run.save("records.json", [{"rank": 1, "slug": SLUG, "uuid": rec["uuid"], "owner_uuid": rec["owner"]["uuid"], "dataflik_id": None,
                               "row": {"street": "12 Test Ln", "zip": "37900", "owner": wb.owner_label(rec["owner"]),
                                       "mail_street": "12 Test Ln"},
                               "record": rec, "messages": [], "custom_fields": [], "timeline": [], "title_owner": "QUILLFEATHER DORN W"}])
    run.save("people.json", [{"slug": SLUG, "people": people_index(), "crm_only": []}])
    run.save("scores.json", scores or {N_MARLA: {"tier": "Dial First", "score": 95, "line_type": "mobile"},
                                       N_INES: {"tier": "Dial Second", "score": 70, "line_type": "mobile"},
                                       N_PELL: {"tier": "Dial Third", "score": 50, "line_type": "mobile"},
                                       N_DROP: {"tier": "Drop", "score": 5, "line_type": "mobile"},
                                       N_OWNER: {"tier": "Dial Fourth", "score": 30, "line_type": "landline"}})
    run.save("analysis/%s.json" % SLUG, an or analysis())
    run.save("research/%s.json" % SLUG, research_file())
    run.save("research/%s_urls.json" % SLUG, [OBIT])
    if page:
        run.save("research/%s_webcache.json" % SLUG, cache(**{OBIT: OBIT_TEXT}))
    run.save("verify.json", VERIFY if verify is None else verify)
    if ledger:
        run.save("ledger.json", {"smartskip": {"usd": 0.15, "note": "", "when": "2026-11-13T10:00:00"}})
    run.p("packs", SLUG + ".pdf").write_bytes(b"%PDF-1.4 the property file")
    return run


def _chain(check, tmp: Path) -> None:
    print("  the write-back, run against a fake DataSift")
    import datasift_api_upload as up
    import deep_prospect_record as dpr
    import dream100_dp as dp
    quiet = io.StringIO()

    def write_back(run, api, commit=True):
        with patched(up, Api=lambda *a, **k: api), patched(dp, READBACK_PAUSE_S=0), contextlib.redirect_stdout(quiet):
            dp.phase_writeback(SimpleNamespace(commit=commit, only="", top=1), run)
        return (run.load("writeback_audit.json") or {}).get(SLUG) or {}

    def no(*parts):
        raise AssertionError("the dry run built a DataSift client")

    run = _write_run(tmp, "wb_dry")
    with patched(up, Api=no), contextlib.redirect_stdout(quiet):
        dp.phase_writeback(SimpleNamespace(commit=False, only="", top=1), run)
    check(run.load("writeback_audit.json") is None, "a dry run builds no client, writes nothing and leaves no audit")
    check("not written: Dial Third held in reserve" in quiet.getvalue() and "research level 4" in quiet.getvalue(),
          "a dry run shows what would be held back and the level it worked out")

    api = FakeDataSift([record()])
    run = _write_run(tmp, "wb_default")
    entry = write_back(run, api)
    numbers = {p["number"]: p["tags"] for p in api.owner(OW)["phones"]}
    check(set(numbers) == {N_OWNER, N_MARLA, N_INES}, "live, with nothing set: the First and the Second number are added, the Third and the Drop are not")
    check(numbers[N_MARLA] == ["Wife", "Dial First", "SmartSkip"] and [h["number"] for h in entry["held"]["phones"]] == [N_PELL],
          "each number carries its relationship, tier and source, and the held number is in the audit with its reason")
    check(api.messages[U][0]["pinned"] and dp.MARKER in api.messages[U][0]["message"] and "Deep Prospected" in api.records[U]["tags"],
          "the research note is posted and pinned, and the record is tagged Deep Prospected")
    check(api.values[U] == {"Owner Deceased": "Yes", "Decedent Name": "Dorn Wesley Quillfeather",
                            "Decision Maker": "Marla Quillfeather", "DM Relationship": "Wife"},
          "the death fields and the verified decision maker are written")
    check(api.owner(OW)["deceased"] is True and "owner marked deceased" in entry["summary"],
          "the owner is marked deceased, because the owner on the record is the person who died")
    check(entry["rb"]["phones_ok"] and entry["rb"]["tag_ok"] and entry["rb"]["note_ok"] and not entry["errors"],
          "the read-back finds the phones, the tag and the note")
    new = ("skiptrace-attempts", "add-tags", "add-notes", "/document/")
    check(not any(api.writes(p) for p in new) and "Heir Identified" not in api.records[U]["tags"]
          and wb.LEVEL_FIELD not in api.values[U] and api.owner(OW)["first_name"] == "Dorn" and not api.uploads,
          "with nothing set, none of the unproven writes runs: no counter, no heir tag, no level, no document, no owner change")
    calls = len(api.writes())
    entry2 = write_back(run, api)
    check(len(api.messages[U]) == 1 and len(api.owner(OW)["phones"]) == 3 and api.records[U]["tags"].count("Deep Prospected") == 1,
          "run a second time, it leaves one note, the same three phones and one tag")
    check(entry2["rb"]["phones_ok"] and len(api.writes()) - calls <= calls, "the second run makes no write the first did not")

    api = FakeDataSift([record()])
    ines_dm = analysis(crm_fields=dict(analysis()["crm_fields"], **{"Decision Maker": "Ines Brackwater", "DM Relationship": "Daughter"}))
    entry = write_back(_write_run(tmp, "wb_unverified", an=ines_dm), api)
    check("Decision Maker" not in api.values[U] and api.values[U]["Owner Deceased"] == "Yes" and "not verified" in entry["held"]["fields"][0],
          "an unverified signer is not written as the decision maker, and the audit says why")

    pr = record(owner=dict(copy.deepcopy(OWNER), first_name="Pell"), lists=["Probate"])
    api = FakeDataSift([pr])
    probate = analysis(crm_fields=dict(analysis()["crm_fields"], **{"Decision Maker": "Pell Quillfeather", "DM Relationship": "Personal representative"}))
    entry = write_back(_write_run(tmp, "wb_probate", an=probate, rec=pr), api)
    marks = [w for w in api.writes("/owner/") if w[0] == "PATCH" and "deceased" in (w[2] or {})]
    check(api.owner(OW)["deceased"] is False and not marks and "is not the person who died" in entry["held"]["deceased"],
          "on a probate record the living representative is not marked deceased, and the audit says why")
    check(api.values[U]["Decision Maker"] == "Pell Quillfeather" and "owner marked deceased" not in entry["summary"],
          "the representative stays the decision maker, and the summary does not claim a mark that was not made")
    check("Owner Deceased" not in api.values[U] and api.values[U]["Decedent Name"] == "Dorn Wesley Quillfeather",
          "and the Owner Deceased field is not written about the living representative, while the decedent's name still is")

    moved = record(owner=dict(copy.deepcopy(OWNER), uuid="own-0777"))
    api = FakeDataSift([moved])
    entry = write_back(_write_run(tmp, "wb_moved"), api)
    check(api.owner("own-0777")["phones"] == OWNER["phones"] and api.owner("own-0777")["deceased"] is False
          and any("owner changed since it was read" in e for e in entry["errors"]),
          "when the record's owner is no longer the one the run read, no phone and no owner mark is written, and it is said")

    lit_scores = {N_MARLA: {"tier": "Dial First", "score": 95}, N_INES: {"tier": "DNC", "score": 60, "litigator": True},
                  N_PELL: {"tier": "Dial Third", "score": 50}, N_OWNER: {"tier": "Dial Fourth", "score": 30}}
    api = FakeDataSift([record(), record(uuid=U2)])
    entry = write_back(_write_run(tmp, "wb_lit_off", scores=lit_scores), api)
    check(N_MARLA in {p["number"] for p in api.owner(OW)["phones"]} and N_INES not in {p["number"] for p in api.owner(OW)["phones"]}
          and "Litigator" not in api.records[U]["tags"],
          "with nothing set a litigator's number is left off, as before, and the record is not tagged")
    with env(AGENTS_DP_LITIGATOR=1, AGENTS_DP_HEIR_TAG=1, AGENTS_DP_SKIPTRACE_COUNTER=1, AGENTS_DP_OWNER_SWAP=1):
        api = FakeDataSift([record(), record(uuid=U2)])
        entry = write_back(_write_run(tmp, "wb_lit_on", scores=lit_scores), api)
    check({"Litigator", "Do Not Market"} <= set(api.records[U]["tags"]) and {"Litigator", "Do Not Market"} <= set(api.records[U2]["tags"])
          and entry["rb"]["litigator_ok"] and entry["litigator"]["numbers"] == [N_INES[-4:]],
          "switched on, a litigator's number tags this record and the owner's other record, and it is read back")
    check(len(api.owner(OW)["phones"]) == 1 and "Heir Identified" not in api.records[U]["tags"] and api.owner(OW)["skiptrace_attempts"] == 0
          and api.owner(OW)["first_name"] == "Dorn"
          and any(h["number"] == N_MARLA and "a litigator's number is tied to this record" in h["why"] for h in entry["held"]["phones"]),
          "a record a litigator is tied to gets no new number, no heir tag, no counter and no owner change")

    everything = dict(AGENTS_DP_LEVEL=1, AGENTS_DP_HEIR_TAG=1, AGENTS_DP_SKIPTRACE_COUNTER=1, AGENTS_DP_DOCUMENT=1, AGENTS_DP_OWNER_SWAP=1)
    api = FakeDataSift([record()], fields=FakeDataSift.FIELDS + (wb.LEVEL_FIELD,))
    run = _write_run(tmp, "wb_all")
    with env(**everything), patched(wb, _post_file=api.post_file):
        entry = write_back(run, api)
    rb = entry["rb"]
    check(api.values[U][wb.LEVEL_FIELD] == "4" and rb["level_ok"], "switched on, the research level is written to its field and read back")
    check("Heir Identified" in api.records[U]["tags"] and rb["heir_tag_ok"], "switched on, a verified heir earns the tag Heir Identified")
    check(api.owner(OW)["skiptrace_attempts"] == 1 and rb["skiptrace_ok"] and entry["skiptrace"]["before"] == 0,
          "switched on, the owner's skip trace counter goes from 0 to 1 when numbers land, and the audit keeps the value before")
    check(len(api.documents[U]) == 1 and rb["document_ok"] and api.documents[U][0]["prop"] == 4101, "switched on, the report is filed in the record's documents")
    ow = api.owner(OW)
    check(ow["first_name"] == "Marla" and ow["last_name"] == "Quillfeather" and rb["owner_ok"] and ow["deceased"] is False
          and entry["owner_swap"]["before"]["first_name"] == "Dorn" and "Owner changed by research" in api.records[U]["notes"],
          "switched on, the owner changes to the verified wife, the audit keeps who was there before, and she is not marked deceased")
    check(all(s in entry["summary"] for s in ("tag Heir Identified", "skip trace counter 1", "report filed", "owner changed to Marla Quillfeather",
                                              "1 Dial Third number held in reserve")),
          "the summary names each thing that was done")
    with env(**everything), patched(wb, _post_file=api.post_file):
        entry2 = write_back(run, api)
    ow = api.owner(OW)
    check(ow["skiptrace_attempts"] == 1 and len(api.documents[U]) == 1 and len(api.uploads) == 1 and len(api.messages[U]) == 1
          and api.records[U]["notes"].count("Owner changed by research") == 1 and api.records[U]["tags"].count("Heir Identified") == 1,
          "run a second time with everything on: the counter, the document, the note and the tag are not written twice")
    check(ow["first_name"] == "Marla" and ow["deceased"] is False and "is not the person who died" in entry2["held"]["deceased"]
          and "is not the person who died" in entry2["held"]["owner_swap"][0],
          "and the heir an earlier run put on the record is neither marked deceased nor replaced again")
    check(sorted(p["number"] for p in ow["phones"]) == sorted([N_OWNER, N_MARLA, N_INES]), "and no phone is added twice")

    unconfirmed = analysis(death=dict(analysis()["death"], owner_deceased=False),
                           crm_fields={"Decedent Name": "Dorn Wesley Quillfeather", "Decision Maker": "Marla Quillfeather",
                                       "DM Relationship": "Wife"})
    api = FakeDataSift([record()], fields=FakeDataSift.FIELDS + (wb.LEVEL_FIELD,))
    with env(**everything), patched(wb, _post_file=api.post_file):
        entry = write_back(_write_run(tmp, "wb_unconfirmed", an=unconfirmed), api)
    marks = [w for w in api.writes("/owner/") if w[0] == "PATCH" and "deceased" in (w[2] or {})]
    check(not marks and api.owner(OW)["deceased"] is False and api.owner(OW)["first_name"] == "Dorn"
          and "Heir Identified" not in api.records[U]["tags"] and api.values[U][wb.LEVEL_FIELD] == "2"
          and entry["held"]["owner_swap"] == ["the owner's death is not confirmed"],
          "with every switch on and the death NOT confirmed by the guard: no deceased mark, no heir tag, no owner change, level 2")

    api = FakeDataSift([record()], fields=FakeDataSift.FIELDS + (wb.LEVEL_FIELD,), relink=True)
    with env(**everything), patched(wb, _post_file=api.post_file):
        entry = write_back(_write_run(tmp, "wb_relink"), api)
    new = api.records[U]["owner"]
    check(new["uuid"] != OW and new["first_name"] == "Marla" and not new["deceased"]
          and sorted(p["number"] for p in new["phones"]) == sorted([N_MARLA, N_INES]) and new["skiptrace_attempts"] == 1
          and entry["rb"]["phones_ok"] and entry["rb"]["skiptrace_ok"] and entry["skiptrace"]["carried"],
          "if the owner change attaches a new owner record instead of renaming the old one, the numbers and the counter follow the house")

    api = FakeDataSift([record()])
    with env(AGENTS_DP_LEVEL=1):
        entry = write_back(_write_run(tmp, "wb_no_field"), api)
    check(wb.LEVEL_FIELD not in api.values[U] and any("Research Level does not exist" in e for e in entry["errors"]),
          "a level field the account does not have is skipped and reported, and nothing else is lost")
    api = FakeDataSift([record()], fields=FakeDataSift.FIELDS + (wb.LEVEL_FIELD,), selects={wb.LEVEL_FIELD: ["1", "2", "3", "4"]})
    with env(AGENTS_DP_LEVEL=1):
        entry = write_back(_write_run(tmp, "wb_level_select"), api)
    check(api.values[U][wb.LEVEL_FIELD] == "opt-4" and entry["rb"]["level_ok"],
          "where the level field is a pick list, the option for the level is what gets written, and it reads back")
    run = _write_run(tmp, "wb_no_pdf")
    run.p("packs", SLUG + ".pdf").unlink()
    api = FakeDataSift([record()])
    with env(AGENTS_DP_DOCUMENT=1):
        entry = write_back(run, api)
    check(not api.documents.get(U) and any("the PDF has not been built" in e for e in entry["errors"]) and entry["rb"]["note_ok"],
          "switched on with no report built, nothing is filed, it is said, and the rest of the write-back still lands")
    api = FakeDataSift([record()])
    api.fail["first read"] = True
    entry = write_back(_write_run(tmp, "wb_read_fails"), api)
    check(any("record not read before writing" in e for e in entry["errors"]) and N_MARLA in {p["number"] for p in api.owner(OW)["phones"]}
          and api.owner(OW)["deceased"] is True,
          "when the record cannot be read first, the write-back goes on from the run's own copy of it, as it did before, and says so")

    api = FakeDataSift([record()])
    run = _write_run(tmp, "wb_counter_later")
    write_back(run, api)
    check(api.owner(OW)["skiptrace_attempts"] == 0, "numbers landed while the counter was switched off: it was not touched")
    fresh = run.load("records.json")            # the next run reads the record again: the numbers are on it now
    fresh[0]["record"]["owner"]["phones"] = copy.deepcopy(api.owner(OW)["phones"])
    run.save("records.json", fresh)
    with env(AGENTS_DP_SKIPTRACE_COUNTER=1):
        entry = write_back(run, api)
        check(not entry["phones_written"] and api.owner(OW)["skiptrace_attempts"] == 1 and entry["skiptrace"]["ok"],
              "switched on afterwards, the next write-back counts the trace whose numbers had already landed")
        write_back(run, api)
        check(api.owner(OW)["skiptrace_attempts"] == 1, "and counts it once")
    nothing = FakeDataSift([record()])
    with env(AGENTS_DP_SKIPTRACE_COUNTER=1):
        write_back(_write_run(tmp, "wb_counter_none", scores={N_MARLA: {"tier": "Drop"}, N_INES: {"tier": "Dial Fourth"}}), nothing)
    check(nothing.owner(OW)["skiptrace_attempts"] == 0 and not nothing.writes("skiptrace-attempts"),
          "a write-back that lands no number leaves the skip trace counter alone, switched on or not")

    print("  the single-record path")
    run = _write_run(tmp, "wb_single")
    row_ = run.load("records.json")[0]["row"]
    row_.update(uuid=U, owner_uuid=OW)
    saved = {k: getattr(dp, k) for k in ("top_rows", "_tax", "smartskip_rows", "assemble", "writeback_plan", "pack_md", "build_note")}
    try:
        dpr.patch(dict(row_, parcel_id="", tax_query=""))
        plan = dp.writeback_plan(dp.assemble(run)[0])
        check([p["number"] for p in plan["phones"]] == [N_MARLA, N_INES] and plan["held"][0]["number"] == N_PELL,
              "the single-record plan follows the same tier rule")
        with patched(dp, render_pdf=lambda md, pdf, photo=None: pdf), contextlib.redirect_stdout(quiet):
            dpr.phase_build(SimpleNamespace(commit=False), run, row_)
        shares = run.load("heir_shares.json")
        check(shares["computed"] and {h["name"]: h["fraction"] for h in shares["heirs"]}["Marla Quillfeather"] == "1/3"
              and shares["heirs"][0]["verified"] is True,
              "the build leaves each heir's share in the run folder, marked with who is verified")
        api = FakeDataSift([record(owner=dict(copy.deepcopy(OWNER), uuid="own-0777"))])
        retag = [{"number": N_OWNER, "type": "LANDLINE", "tags": ["Dial Fourth"], "status": "UNKNOWN", "is_connected": True, "verified": False}]
        with patched(up, Api=lambda *a, **k: api), patched(dp, READBACK_PAUSE_S=0), patched(dpr, existing_tier_writes=lambda r: retag), \
                contextlib.redirect_stdout(quiet):
            dpr.phase_writeback(SimpleNamespace(commit=True, only="", top=1), run, row_)
        check(not api.writes("upsert-phones"), "numbers already on the record are not re-tagged on an owner who is no longer attached to it")
    finally:
        for k, v in saved.items():
            setattr(dp, k, v)


def _order(check, tmp: Path, store) -> None:
    print("  the order of the chain")
    import deep_prospect_record as dpr
    import dream100_dp as dp
    from .research import deep_prospect, open_research, researcher
    config.RUNS_DIR, config.RUN_CAP_USD = tmp / "wb_runs", 5.0
    config.DAILY_CAP_USD = config.RESEARCH_DAILY_CAP_USD = 100000.0
    config.FINAL_RESERVE_USD = 1.0
    steps, briefs = [], []
    rec = record(notes="Foreclosure\nAuction: 12/1/2026")
    row_ = {"uuid": U, "owner_uuid": OW, "street": "12 Test Ln", "city": "Knoxville", "zip": "37900", "owner": "Dorn Quillfeather",
            "mail_street": "12 Test Ln", "lists": ["Obituary"]}

    def records(args, rn, row):
        steps.append("records")
        rn.save("records.json", [{"rank": 1, "slug": SLUG, "uuid": U, "owner_uuid": OW, "row": dict(row), "record": rec,
                                  "messages": [], "custom_fields": [], "timeline": [], "title_owner": "QUILLFEATHER DORN W"}])

    def smartskip(args, rn):
        steps.append("smartskip")
        rn.save("smartskip.json", [])

    def merge(args, rn, row):
        steps.append("merge")
        rn.save("people.json", [{"slug": SLUG, "people": people_index(), "crm_only": []}])

    def research(rn, row):
        steps.append("research")
        briefs.append(researcher.record_brief(rn, row))
        rn.save("research/%s.json" % SLUG, research_file())
        rn.save("research/%s_urls.json" % SLUG, [OBIT])
        return {}, 0.03

    def analyze(rn, row, final):
        steps.append("final analysis" if final else "analysis")
        rn.save("analysis/%s.json" % SLUG, analysis())
        return {}, 0.02

    def step(name):
        return lambda *a, **k: steps.append(name)

    def writeback(args, rn, row):
        steps.append("writeback")
        rn.save("writeback_audit.json", {SLUG: {"summary": "2 new numbers, pinned research note, tag Deep Prospected"}})

    engine = SimpleNamespace(name="fake", research=research, analyze=analyze, cap=0.5, label="research (fake)")
    index = "Quillfeather, Dorn Wesley | age 81 | B4 | obituary date 3/9/%d" % date.today().year

    def fakes(index_fn=None):
        """Every vendor, model and DataSift step of the chain replaced by a recorder. Fresh each run."""
        return [patched(dpr, row_for=lambda uuid, crm=None: dict(row_), patch=lambda row: None, phase_records=records,
                        phase_merge=merge, heir_rows=lambda rn, row: ([], []), phase_heirs=step("heirs"),
                        phase_gapfill=step("gapfill"), verify_pairs=lambda rn, row: [], phase_verify=step("verify"),
                        phase_build=step("build"), phase_writeback=writeback),
                patched(dp, load_env=lambda: {}, smartskip_rows=lambda recs: [{}], phase_smartskip=smartskip,
                        numbers_to_score=lambda rn: {}, phase_score=step("score")),
                patched(open_research, engine=lambda: engine,
                        lookup_obituary_index=index_fn or (lambda last, first="": index))]

    def go(mode, index_fn=None, **settings):
        steps.clear()
        briefs.clear()
        config.MODE = mode
        settings.setdefault("AGENTS_DEED_DIR", str(tmp / "wb_no_deeds"))
        task_id, _ = store.enqueue("research", "deep_prospect", U, subject="selftest", force=True)
        with contextlib.ExitStack() as stack:
            for f in fakes(index_fn):
                stack.enter_context(f)
            stack.enter_context(env(**settings))
            return deep_prospect.run({"id": task_id, "record_uuid": U}), task_id

    r, _ = go("live")
    check(steps.index("smartskip") < steps.index("research") < steps.index("analysis") and r["order"] == "trace_first",
          "today's order, the default: the paid trace is bought before the research runs")
    check("read_first" not in briefs[0], "and the research brief carries no free look")
    check(r["research_level"] == 4 and r["heir_shares"]["computed"] and len(r["heir_shares"]["heirs"]) == 3 and r["verified"] == [],
          "the result carries the level, each heir's share, and who is verified (nobody here: no page was opened and no name check ran)")
    text = deep_prospect.slack_text({"id": 1}, r)
    check("research level 4" in text and "Shares if there is no will: Marla Quillfeather 1/3" in text, "the Slack post says the level and the shares")
    check(r["writeback"].startswith("2 new numbers") and "Written: 2 new numbers" in text and N_MARLA not in text,
          "the Slack post says what was written, in the write-back's own words, with no whole phone number")

    r, _ = go("live", AGENTS_DP_ORDER="free_first")
    check(steps[:2] == ["records", "research"] and steps.index("research") < steps.index("smartskip") < steps.index("analysis"),
          "free first: the research reads the free facts before any trace is paid for, and the first analysis sees the trace")
    look = briefs[0].get("read_first") or {}
    check("Auction: 12/1/2026" in look.get("notice_text_on_the_record", "") and look["county_obituary_index"][0]["could_be_this_owner"],
          "free first: the research brief carries the notice text on the record and the obituary index rows for the owner")
    check(r["order"] == "free_first" and "writeback" in steps, "free first still ends with the same build and write-back")

    run_dir = config.RUNS_DIR / SLUG
    (run_dir / "deed_lookup.json").write_text(json.dumps({"still_owns": False, "last_deed": {"date": "2026-10-30"}}), encoding="utf-8")
    r, task_id = go("live", AGENTS_DP_ORDER="free_first")
    check(steps == ["records"] and r["stopped"].startswith("The deed records show the owner no longer owns this house")
          and not r["written_back"] and store.spent(task_id=task_id) == 0,
          "free first: a saved deed lookup showing the house was deeded away ends the run before a cent is spent or a word is written")
    check("Stopped before any paid step" in deep_prospect.slack_text({"id": 1}, r), "and the Slack post says why it stopped")
    r, _ = go("live")
    check("smartskip" in steps and "research" in steps and not r.get("stopped"),
          "today's order does not read a deed lookup at all: nothing about the default changed")
    (run_dir / "deed_lookup.json").unlink()
    check((json.loads((run_dir / "free_look.json").read_text(encoding="utf-8")) is None) and "read_first" not in briefs[0],
          "and a free look left by an earlier free-first run is not fed to today's order")

    r, _ = go("dry", AGENTS_DP_ORDER="free_first")
    check("smartskip" not in steps and "writeback" not in steps and "research" in steps and not r["written_back"],
          "dry stays dry in either order: no vendor is paid and nothing is written")
    config.RUN_CAP_USD = 0.1
    r, task_id = go("live", AGENTS_DP_ORDER="free_first")
    check("research" not in steps and "smartskip" not in steps and store.spent(task_id=task_id) == 0,
          "free first: when the research cannot run inside the cap, no trace is bought for the record either")

    def down(last, first=""):
        raise OSError("the library did not answer")

    config.RUN_CAP_USD, config.RUNS_DIR = 5.0, tmp / "wb_runs_index_down"
    r, _ = go("live", index_fn=down, AGENTS_DP_ORDER="free_first")
    check(steps.index("research") < steps.index("smartskip") and "read_first" not in briefs[0]
          and any("free look FAILED" in ln for ln in r["log"]),
          "free first: a free look that breaks stops nothing: the run goes on without it and the log says so")
