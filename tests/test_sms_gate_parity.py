"""Parity: the texting app's copy of the suppression gate against the brain's own.

The SMS agent's image holds only src/sms_agent, so it cannot import the brain's gate
(src/agents/autopilot/gate.py) and carries its own copy of the rules for a text and a call
(src/sms_agent/gate_rules.py). Two copies of a rule drift, and a drifted suppression rule is a
homeowner who gets a text the business believed it had stopped. This file is what keeps them one
rule: it feeds both modules the same invented records, one rule at a time and then a few thousand
at random, and fails on the first different answer.

Zero network. Every name, street and number here is invented.

    python tests/test_sms_gate_parity.py
"""
from __future__ import annotations

import copy
import dataclasses
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

CHANNELS = ("text", "call")

MOBILE1 = {"number": "8655550101", "type": "MOBILE", "tags": ["Dial First"], "status": "UNKNOWN"}
MOBILE2 = {"number": "8655550102", "type": "MOBILE", "tags": [{"title": "Dial Second"}], "status": "UNKNOWN"}
LAND = {"number": "8655550103", "type": "LANDLINE", "tags": ["Dial First"], "status": "UNKNOWN"}
DROP = {"number": "8655550109", "type": "MOBILE", "tags": ["Drop"], "status": "UNKNOWN"}


def load():
    """(the brain's gate, the brain's lanes, this app's port). The brain's config works out its
    data folder at import, so it is pointed at a throwaway one first: this test must never open
    the brain's real database."""
    os.environ.setdefault("AGENTS_DATA_DIR", tempfile.mkdtemp(prefix="gate_parity_"))
    from agents.autopilot import gate as G
    from agents.autopilot import lanes as GL
    from sms_agent import gate_rules as P
    return G, GL, P


def base_record() -> dict:
    return {"uuid": "g0", "address": {"street": "10 Gate St", "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": "o-g0", "first_name": "Pat", "last_name": "Example",
                      "phones": [dict(MOBILE1), dict(LAND)]},
            "tags": ["FTM", "Courthouse Data", "Foreclosure"], "lists": ["Foreclosure"], "status": None, "mls": ""}


def _tags(r, *names):
    r["tags"] = list(r.get("tags") or []) + list(names)


def _lists(r, *names):
    r["lists"] = list(r.get("lists") or []) + list(names)


# (what the rule is, the change to the clean record, extra arguments, the code a text must report)
RULE_CASES = [
    ("a clean record passes", lambda r: None, {}, ""),
    ("Do Not Market stops a text and a call", lambda r: _tags(r, "Do Not Market"), {}, "do_not_market"),
    ("the Do Not Market tag matches in any case", lambda r: _tags(r, "do not market"), {}, "do_not_market"),
    ("nothing a caller may lift lifts Do Not Market", lambda r: _tags(r, "Do Not Market"),
     {"allow": ("lead", "not_interested", "mail_only", "exhausted", "in_research", "unknown_status", "listed",
                "deceased", "heir")}, "do_not_market"),
    ("a Litigator tag on the record", lambda r: _tags(r, "Litigator"), {}, "litigator"),
    ("a Litigator tag on one phone holds the whole record",
     lambda r: r["owner"]["phones"][0].update(tags=["Dial First", "Litigator"]), {}, "litigator"),
    ("a number already known as a litigator's", lambda r: None, {"litigators": {"8655550103"}}, "litigator"),
    ("the owner's opt-out flag", lambda r: r["owner"].update(opt_out=True), {}, "opted_out"),
    ("the owner's do-not-call flag", lambda r: r["owner"].update(dnc=True), {}, "opted_out"),
    ("the DNC list", lambda r: _lists(r, "DNC"), {}, "opted_out"),
    ("an opt-out status", lambda r: r.update(status="opt_out"), {}, "opted_out"),
    ("a dnc status", lambda r: r.update(status="dnc"), {}, "opted_out"),
    ("a sold tag", lambda r: _tags(r, "recently sold"), {}, "sold"),
    ("status sold", lambda r: r.update(status="sold"), {}, "sold"),
    ("the owner's deceased flag", lambda r: r["owner"].update(deceased=True), {}, "deceased"),
    ("a deceased tag", lambda r: _tags(r, "deceased"), {}, "deceased"),
    ("the Verify Deceased tag", lambda r: _tags(r, "Verify Deceased"), {}, "deceased"),
    ("the Obituary list", lambda r: _lists(r, "Obituary"), {}, "deceased"),
    ("the Owner Deceased field", lambda r: None, {"custom": {"Owner Deceased": "Yes"}}, "deceased"),
    ("an Owner Deceased field that says no holds nothing", lambda r: None, {"custom": {"Owner Deceased": "No"}}, ""),
    ("a probate record is not held by the Obituary list", lambda r: _lists(r, "Obituary", "Probate"), {}, ""),
    ("a deceased flag still holds a probate record",
     lambda r: (_lists(r, "Probate"), r["owner"].update(deceased=True)), {}, "deceased"),
    ("the heir lane without the heir tag is still held",
     lambda r: r["owner"].update(deceased=True), {"allow": ("heir",)}, "deceased"),
    ("the heir lane with a verified heir may work a dead owner's record",
     lambda r: (r["owner"].update(deceased=True), _tags(r, "Heir Identified")), {"allow": ("heir",)}, ""),
    ("an explicit deceased allowance", lambda r: r["owner"].update(deceased=True), {"allow": ("deceased",)}, ""),
    ("a lead is never touched by prospecting", lambda r: r.update(status="Hot Lead"), {}, "lead"),
    ("a new lead", lambda r: r.update(status="new_lead"), {}, "lead"),
    ("follow_up is a lead status", lambda r: r.update(status="follow_up"), {}, "lead"),
    ("a lead-management caller may act on a lead", lambda r: r.update(status="Warm Lead"), {"allow": ("lead",)}, ""),
    ("Not Interested is finished", lambda r: r.update(status="not_interested"), {}, "finished"),
    ("the second-chance lane lifts Not Interested", lambda r: r.update(status="not_interested"),
     {"allow": ("not_interested",)}, ""),
    ("and lifts nothing else", lambda r: r.update(status="Dead Lead"), {"allow": ("not_interested",)}, "finished"),
    ("a passed auction", lambda r: r.update(status="Auction Date Passed"), {}, "finished"),
    ("a buyer status", lambda r: r.update(status="buyer_found"), {}, "finished"),
    ("a status the gate has never seen", lambda r: r.update(status="Brand New Status"), {}, "unknown_status"),
    ("an install's own working status", lambda r: r.update(status="Brand New Status"),
     {"cfg": {"working_statuses": ["Brand New Status"]}}, ""),
    ("a record held for research", lambda r: r.update(status="Deep Prospecting"), {}, "in_research"),
    ("a status given as an object", lambda r: r.update(status={"title": "Hot Lead"}), {}, "lead"),
    ("a status object with a value", lambda r: r.update(status={"value": "sold", "title": "Sold"}), {}, "sold"),
    ("a listed house", lambda r: r.update(mls="Listed"), {}, "listed"),
    ("a pending sale", lambda r: r.update(mls="Pending"), {}, "listed"),
    ("status listed", lambda r: r.update(status="listed"), {}, "listed"),
    ("an MLS value of Sold alone holds nothing", lambda r: r.update(mls="Sold"), {}, ""),
    ("Not Single Family", lambda r: _tags(r, "Not Single Family"), {}, "not_single_family"),
    ("Mail Only", lambda r: _tags(r, "Mail Only"), {}, "mail_only"),
    ("Mail Only lifted by a caller that may", lambda r: _tags(r, "Mail Only"), {"allow": ("mail_only",)}, ""),
    ("do-not-mail does not stop a text", lambda r: r.update(do_not_mail_ever=True), {}, ""),
    ("returned mail does not stop a text", lambda r: r.update(status="Return Mail"), {}, ""),
    ("only a Drop number", lambda r: r["owner"].update(phones=[dict(DROP)]), {}, "no_number"),
    ("a number marked wrong", lambda r: r["owner"].update(phones=[dict(MOBILE1, status="WRONG")]), {}, "no_number"),
    ("a number marked do-not-call", lambda r: r["owner"].update(phones=[dict(MOBILE1, status="CORRECT_DNC")]), {},
     "no_number"),
    ("a number a person confirmed is usable whatever its tier",
     lambda r: r["owner"].update(phones=[dict(DROP, status="CORRECT")]), {}, ""),
    ("a phone tagged dnc", lambda r: r["owner"].update(phones=[dict(MOBILE1, tags=["Dial First", "DNC"])]), {},
     "no_number"),
    ("the number the registry flagged, where the search row shows it", lambda r: None,
     {"row": {"phone": {"number": "8655550101", "doNotCall": True}}}, "no_number"),
    ("a flag on a different number changes nothing", lambda r: None,
     {"row": {"phone": {"number": "8655550999", "doNotCall": True}}}, ""),
    ("an owner who is a lead on another record", lambda r: None,
     {"lane": "ftm", "owner": {"lane": "lead", "records": {"other": "lead"}}}, "owner_lane"),
    ("the record that puts the owner in the higher lane is not held by it", lambda r: None,
     {"lane": "ftm", "owner": {"lane": "hot_auction", "records": {"g0": "hot_auction"}}}, ""),
    ("an owner in a lower lane holds nothing", lambda r: None,
     {"lane": "ftm", "owner": {"lane": "bulk", "records": {"other": "bulk"}}}, ""),
    ("an install's own lane markers", lambda r: _tags(r, "My Hot Tag"),
     {"markers": {"hot_auction": {"tags": ["My Hot Tag"]}}, "owner": {"lane": "lead", "records": {"x": "lead"}}},
     "owner_lane"),
    ("several rules at once report the most serious first",
     lambda r: (_tags(r, "Do Not Market", "Mail Only"), r.update(status="sold"), r["owner"].update(deceased=True)), {},
     "do_not_market"),
    ("a record that cannot be read", lambda r: r.clear(), {}, "unreadable"),
]


def _kwargs(G, GL, P, kw: dict) -> tuple[dict, dict]:
    """The same arguments in each module's own spelling."""
    kg, kp = dict(kw), dict(kw)
    if "cfg" in kw:
        kg["cfg"], kp["cfg"] = G.GateConfig(**kw["cfg"]), P.GateConfig(**kw["cfg"])
    if "markers" in kw:
        full = {k: dict(v) for k, v in GL.DEFAULT_MARKERS.items()}
        full.update(kw["markers"])
        kg.pop("markers")
        kg["lanes_cfg"] = GL.Lanes(markers=full)
        kp["markers"] = full
    return kg, kp


def _view(v) -> tuple:
    return (v.ok, list(v.reasons), list(v.numbers), v.lane, v.code, v.reason(), v.all_reasons())


def agree(G, GL, P, rec: dict, channel: str, kw: dict) -> tuple[bool, str]:
    kg, kp = _kwargs(G, GL, P, kw)
    a = _view(G.check(copy.deepcopy(rec), channel, **kg))
    b = _view(P.check(copy.deepcopy(rec), channel, **kp))
    return a == b, "" if a == b else "brain %r | texting %r" % (a[:3], b[:3])


# ---------------------------------------------------------------------- random records
STATUSES = [None, "", "prospecting", "sold", "new_lead", "Hot Lead", "Cold Lead", "follow_up", "No Contact New Lead",
            "not_interested", "Dead Lead", "dnc", "opt_out", "Deep Prospecting", "Exhausted", "Return Mail", "listed",
            "buyer", "Auction Date Passed", "closed", "Something Else", {"title": "Warm Lead"}, {"value": "sold"},
            {"slug": "sold"}, "  SOLD  "]
TAGS = ["FTM", "Do Not Market", "do not market", "Litigator", "deceased", "Verify Deceased", "Heir Identified",
        "recently sold", "Sold", "Mail Only", "Not Single Family", "Priority 1", "Priority 2", "Auction Soon",
        "Obituary", "Probate", "Courthouse Data", {"title": "Mail Only"}, {"name": "Litigator"}]
LISTS = ["Foreclosure", "DNC", "dnc", "Obituary", "Probate", "Tax Delinquent", {"title": "Obituary"}]
PHONE_STATUS = ["UNKNOWN", "", None, "CORRECT", "WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC", "NO_ANSWER"]
PHONE_TYPE = ["MOBILE", "LANDLINE", "UNKNOWN", "", None, "VOIP", "mobile"]
PHONE_TAGS = [[], ["Dial First"], ["Dial Second"], ["Dial Third"], ["Drop"], [{"title": "Dial First"}],
              ["Dial First", "Litigator"], ["Dial Second", "DNC"], ["Relative"]]
NUMBERS = ["8655550101", "8655550102", "8655550103", "+1 (865) 555-0104", "18655550105", "555", ""]
ALLOW = ["lead", "not_interested", "mail_only", "exhausted", "in_research", "unknown_status", "listed", "deceased",
         "heir", "LEAD", " Heir "]
LANES = ["", "lead", "heirs", "hot_auction", "ftm", "obituary", "priority1", "strong", "bulk", "made_up"]


def random_case(rng: random.Random) -> tuple[dict, dict]:
    rec = {"uuid": rng.choice(["r1", "r2", "", "r3"]),
           "status": rng.choice(STATUSES),
           "tags": rng.sample(TAGS, rng.randint(0, 4)),
           "lists": rng.sample(LISTS, rng.randint(0, 3)),
           "mls": rng.choice(["", None, "Listed", "pending", "Sold", "Off Market"]),
           "owner": {"phones": [{"number": rng.choice(NUMBERS), "status": rng.choice(PHONE_STATUS),
                                 "type": rng.choice(PHONE_TYPE), "tags": copy.deepcopy(rng.choice(PHONE_TAGS))}
                                for _ in range(rng.randint(0, 4))]}}
    for flag in ("deceased", "opt_out", "dnc", "do_not_mail_ever"):
        if rng.random() < 0.12:
            rec["owner"][flag] = True
    if rng.random() < 0.08:
        rec["do_not_mail_ever"] = True
    if rng.random() < 0.05:
        rec.pop("owner")
    kw: dict = {}
    if rng.random() < 0.5:
        kw["allow"] = tuple(rng.sample(ALLOW, rng.randint(0, 4)))
    if rng.random() < 0.3:
        kw["row"] = {"phone": {"number": rng.choice(NUMBERS), "doNotCall": rng.random() < 0.6}}
    if rng.random() < 0.3:
        kw["custom"] = {rng.choice(["Owner Deceased", "owner deceased ", "Other Field"]):
                        rng.choice(["Yes", "no", "TRUE", "", "deceased", 1, 0])}
    if rng.random() < 0.3:
        kw["litigators"] = set(rng.sample(["8655550101", "8655550102", "8655550104"], rng.randint(0, 2)))
    if rng.random() < 0.4:
        kw["lane"] = rng.choice(LANES)
    if rng.random() < 0.4:
        lane = rng.choice(LANES[1:])
        kw["owner"] = {"lane": lane, "records": {rng.choice(["r1", "r9"]): rng.choice(LANES[1:])}}
    if rng.random() < 0.15:
        kw["cfg"] = {"working_statuses": ["Something Else"], "callable_tiers": ["Dial First"],
                     "deceased_tags": ["deceased"], "suppress_lists": ["DNC", "Tax Delinquent"]}
    if rng.random() < 0.1:
        kw["markers"] = {"ftm": {"tags": ["Courthouse Data"]}, "strong": {"lists": ["Tax Delinquent"]}}
    return rec, kw


def run(check, rounds: int = 3000) -> None:
    """Every check goes through `check(condition, sentence)`, so the SMS agent's own selftest can
    run this file as one of its sections."""
    G, GL, P = load()

    # ---- the tables themselves
    check(P.RULES == G.RULES, "the rule table is the same in both files (channels blocked and the word that lifts each)")
    check(P.ORDER == G.ORDER, "the rules are ranked in the same order, so both report the same first reason")
    check(P.TEXT == G.TEXT, "both give a held record the same reason in words")
    check(P.BAD_PHONE == G.BAD_PHONE and P.TIER_NAMES == G.TIER_NAMES and P.YES == G.YES,
          "phone statuses, dial tiers and the words that mean yes are the same")
    check(P.FINISHED_STATUSES == G.FINISHED_STATUSES and P.WORKING_STATUSES == G.WORKING_STATUSES,
          "finished and working statuses are the same")
    check(dataclasses.asdict(P.GateConfig()) == dataclasses.asdict(G.GateConfig())
          and P.GateConfig.KEYS == G.GateConfig.KEYS, "the settings and their defaults are the same")
    check(P.LANE_ORDER == GL.ORDER and P.LEAD_STATUS_VALUES == GL.LEAD_STATUS_VALUES
          and P.LEAD_STATUSES == GL.LEAD_STATUSES and P.DEFAULT_MARKERS == GL.DEFAULT_MARKERS,
          "the lane order, the lead statuses and the lane markers are the same")
    check(set(P.CHANNELS) <= set(G.CHANNELS) and set(P.CHANNELS) == {"text", "call"},
          "the texting app answers for a text and a call, both channels the brain knows")
    check(P.PERSON_RULES <= set(G.RULES), "every person-level rule is a rule the brain has")

    # ---- one rule at a time
    for name, change, kw, want in RULE_CASES:
        rec = base_record()
        change(rec)
        for channel in CHANNELS:
            ok, diff = agree(G, GL, P, rec, channel, kw)
            check(ok, "%s: both gates agree on a %s%s" % (name, channel, (" (" + diff + ")") if diff else ""))
        kg, kp = _kwargs(G, GL, P, kw)
        got = P.check(copy.deepcopy(rec), "text", **kp).code
        check(got == want, "%s: a text reports %r (got %r)" % (name, want or "nothing", got or "nothing"))

    # a call differs from a text in two rules, and both files must say so the same way
    r = base_record()
    r.update(status="Exhausted")
    check(P.check(r, "call").code == "exhausted" and P.check(r, "text").ok
          and agree(G, GL, P, r, "call", {})[0] and agree(G, GL, P, r, "call", {"allow": ("exhausted",)})[0],
          "an exhausted record is not called again, and a text is not held by it")
    r = base_record()
    r["owner"]["phones"] = [dict(LAND)]
    check(P.check(r, "call").ok and P.check(r, "text").code == "no_number" and agree(G, GL, P, r, "text", {})[0],
          "a landline may be called and is never texted")

    # ---- the pieces, on their own
    for value in ("865-555-0101", "+18655550101", "18655550101", "5550101", None, 8655550101, "(865) 555 0101 x2"):
        check(P.digits(value) == G.digits(value), "a number is cleaned the same way: %r" % (value,))
    for st in STATUSES:
        rec = {"uuid": "s", "status": st, "tags": ["FTM"], "lists": []}
        check(P.status_of(rec) == GL.status_of(rec) and P.lane_of(rec) == GL.lane_of(rec),
              "status %r reads the same and puts the owner in the same lane" % (st,))
    for lane in LANES:
        check(P.rank(lane) == GL.rank(lane), "lane %r ranks the same" % lane)
    for tags, lists in ((["Auction Soon", "FTM"], []), (["Priority 1"], ["Obituary"]), ([], ["Obituary"]),
                        (["Heir Identified", "Priority 2"], []), (["Priority 2"], []), ([], [])):
        rec = {"uuid": "l", "status": None, "tags": tags, "lists": lists}
        check(P.lane_of(rec) == GL.lane_of(rec), "tags %s and lists %s give the same lane" % (tags, lists))
    for status in ("not_interested", "dnc", "opt_out", "Hot Lead", "sold", None, "Odd One", "Deep Prospecting"):
        row = {"uuid": "g9", "status": status}
        for channel in CHANNELS:
            for allow in ((), ("not_interested",), ("lead", "unknown_status", "in_research")):
                a, b = G.check_row(row, channel, allow=allow), P.check_row(row, channel, allow=allow)
                check((a.ok, a.reasons) == (b.ok, b.reasons),
                      "a search row with status %r answers the same for a %s, allowing %s" % (status, channel, allow or "nothing"))
    for row in ({"phone": {"number": "8655550101", "doNotCall": True}}, {"phone": {"number": "8655550101"}},
                {"phone": None}, {}, {"phone": {"number": "865", "doNotCall": True}}, None):
        check(P.registry_flagged(row) == G.registry_flagged(row), "the registry flag reads the same off %r" % (row,))
    for block in ({"working_statuses": ["A"]}, {"read_fields": True}, {"do_not_market_tags": []},
                  {"no_such_key": 1}, {"sold_tags": "not a list"}, None):
        pg, pp = [], []
        cg, cp = G.parse(block, pg), P.parse(block, pp)
        check(pg == pp and dataclasses.asdict(cg) == dataclasses.asdict(cp),
              "settings %r parse the same, with the same complaints" % (block,))
    try:
        P.check(base_record(), "mail")
        check(False, "the texting copy refuses a channel it does not answer for")
    except ValueError:
        check(True, "the texting copy refuses a channel it does not answer for")

    # ---- the one do-not-text rule can never be looser than the gate
    cfg = P.GateConfig()
    loose = []
    for st in PHONE_STATUS:
        for flag in (True, False):
            p = {"number": "8655550101", "type": "MOBILE", "tags": ["Dial First"], "status": st}
            row = {"phone": {"number": "8655550101", "doNotCall": flag}}
            usable = P.usable_numbers({"owner": {"phones": [p]}}, "text", cfg, row)
            refused = P.do_not_text(p, P.flagged_numbers([row]))
            if usable and refused:
                loose.append((st, flag, "the gate texts it, the rule refuses"))
            if not usable and not refused:
                loose.append((st, flag, "the rule texts it, the gate refuses"))
    check(not loose, "the do-not-text rule and the gate refuse exactly the same statuses and flags %s" % loose[:3])

    # ---- a few thousand records nobody thought of
    rng = random.Random(20261113)
    bad = []
    for i in range(rounds):
        rec, kw = random_case(rng)
        for channel in CHANNELS:
            ok, diff = agree(G, GL, P, rec, channel, kw)
            if not ok:
                bad.append("case %d %s: %s" % (i, channel, diff))
    check(not bad, "%d random records get the same verdict from both gates on both channels%s" % (
        rounds, (": " + "; ".join(bad[:3])) if bad else ""))
    usable_bad = 0
    rng = random.Random(7)
    for _ in range(rounds // 3):
        rec, kw = random_case(rng)
        for channel in CHANNELS:
            a = G.usable_numbers(rec if rec.get("owner") else dict(rec, owner={}), channel, G.GateConfig(), kw.get("row"),
                                 kw.get("litigators"))
            b = P.usable_numbers(rec if rec.get("owner") else dict(rec, owner={}), channel, P.GateConfig(), kw.get("row"),
                                 kw.get("litigators"))
            usable_bad += a != b
        sg = G.deceased_signals(dict(rec, owner=rec.get("owner") or {}), G.GateConfig(), kw.get("custom"))
        sp = P.deceased_signals(dict(rec, owner=rec.get("owner") or {}), P.GateConfig(), kw.get("custom"))
        usable_bad += sg != sp
    check(not usable_bad, "usable numbers and the signs of a death agree on every random record (%d differ)" % usable_bad)


def main() -> int:
    n = [0]

    def check(cond, msg):
        n[0] += 1
        if not cond:
            raise AssertionError(msg)

    run(check)
    print("test_sms_gate_parity: %d checks passed" % n[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
