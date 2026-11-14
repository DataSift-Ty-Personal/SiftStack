"""Preset patch: what each kind of preset gains, what is left alone, and that nothing is written
without both switches. Zero network: a small fake account with invented titles and no people."""
import copy
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.autopilot import preset_patch as pp  # noqa: E402

DAY = date(2026, 11, 13)
UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _id(kind, title):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, kind + ":" + title))


TAGS = ["Maple", "recently sold", "Mail Only", "Do Not Market", "Not Single Family", "Return Mail", "Do Not Mail",
        "Auction Soon", "Heirs Resolved", "Obituary", "FTM"]
LISTS = ["Low Equity", "DNC", "Obituary", "Probate", "Foreclosure", "Buyers"]
STATUSES = pp.LEAD_STATUSES + pp.FINISHED_STATUSES + pp.WORKING_STATUSES
# The published status listing, in the shape the account answers with: seven fixed headings, each
# naming the account's own statuses filed under it. Only the lead pipeline is in it.
PUBLISHED = [("dead_lead", ["Dead Lead"]), ("ghosting_lead", ["Ghosting Lead"]), ("lost_deal", []),
             ("new_lead", ["new_lead"]), ("not_interested", ["not_interested"]),
             ("qualified_lead", ["Cold Lead", "Warm Lead", "Hot Lead"]), ("unqualified_lead", ["No Contact New Lead"])]
# What the presets excluded before the patch: everything except the two lead statuses and the one
# finished status the plan is about.
OLD = [s for s in STATUSES if s not in ("No Contact New Lead", "follow_up", "Auction Date Passed", "prospecting")]
T = {t: _id("tag", t) for t in TAGS}
L = {t: _id("list", t) for t in LISTS}
DEAD4 = list(pp.LIVE_NUMBER)


def rec(n, owner, tags=("Maple",), lists=(), status="", attempts=0, mailed=0, deceased=False, phones=("UNKNOWN",),
        phone=True, situation="", assigned=""):
    return {"n": n, "owner": owner, "tags": list(tags), "lists": list(lists), "status": status, "attempts": attempts,
            "mailed": mailed, "deceased": deceased, "phones": list(phones), "phone": phone, "situation": situation,
            "assigned": assigned}


def records():
    return [
        rec(1, "o1"), rec(2, "o1"),                                            # one owner, two houses, never dialed
        rec(3, "o2", tags=("Maple", "Do Not Market"), attempts=1),
        rec(4, "o3", lists=("DNC",), attempts=1),
        rec(5, "o4", status="No Contact New Lead", attempts=1),
        rec(6, "o5", deceased=True, attempts=1),
        rec(7, "o6", attempts=1),
        rec(8, "o7", lists=("Obituary",), attempts=1),
        rec(9, "o8", tags=("Maple", "Not Single Family"), attempts=1),
        rec(10, "o9", phones=("WRONG", "DEAD"), attempts=1),                   # no live number
        rec(11, "o10", attempts=1), rec(12, "o10", attempts=1),                # one owner, two houses
        rec(13, "o11", status="Exhausted"),
        rec(14, "o12", status="Auction Date Passed", attempts=1),
        rec(15, "o13", attempts=5),
        rec(16, "o14", attempts=3, phones=("WRONG",)),
        rec(17, "o15", status="not_interested"),
        rec(18, "o16", tags=("Maple", "Do Not Market"), status="not_interested"),
        rec(19, "o17", lists=("Obituary", "Probate")),
        rec(20, "o18", status="prospecting", assigned="jordan", tags=("Maple", "Do Not Market")),
        rec(21, "o19", status="prospecting", assigned="jordan"),
        rec(22, "o20", status="Warm Lead", attempts=2),                        # a lead the presets already skip
    ]


def _not(tags=("recently sold", "Mail Only"), statuses=None):
    return {"any_tags": [T[t] for t in tags], "any_lists": [L["Low Equity"]], "any_neighborhood": ["Elm Hollow"],
            "any_property_status": list(OLD if statuses is None else statuses)}


def call_must(attempt, one_per_owner=False):
    m = {"phone": 1, "any_tags": [T["Maple"]], "must_not": _not(), "skiptraced": 1,
         "predictivecall_attempts": [attempt, attempt]}
    if one_per_owner:
        m["ownerPropertiesOwned"] = {"show_properties": "one_per_owner"}
    return m


def mail_must(n):
    m = {"any_tags": [T["Maple"]], "must_not": _not(("recently sold",)), "owner_vacant": 0,
         "directmail_attempts": [n, n]}
    if n:
        m["last_direct_mailed"] = ["36-months", "month"]
    return m


def obit_must(extra):
    return dict({"all_lists": [L["Obituary"]], "must_not": _not(("recently sold", "Mail Only", "Not Single Family")),
                 "ownerPropertiesOwned": {"show_properties": "one_per_owner"}}, **extra)


def account():
    """folder title -> [(preset title, saved filter)] in the order the account shows them."""
    return [
        ("01. MAPLE - CALL", [
            ("Maple - 00 Needs Skipped", {"phone": 0, "any_tags": [T["Maple"]], "must_not": _not(), "skiptraced": 0,
                                         "predictivecall_attempts": [0, 0]}),
            ("Maple - 02 Ready to Call", call_must(0, one_per_owner=True)),
            ("Maple - 03 Call Attempt 1", call_must(1)),
            ("Maple - 04 Call Attempt 2", call_must(2)),
            ("Maple - 05 Call Attempt 3", call_must(3)),
        ]),
        ("02. MAPLE - MAIL", [("Maple - Mail 01 Letter", mail_must(0)), ("Maple - Mail 02 Postcard", mail_must(1))]),
        ("09. BULK - CALL", [("Bulk - 0%d Call Attempt %d" % (n + 2, n), call_must(n)) for n in range(1, 6)]),
        ("11. DEEP PROSPECTING (ALL TIERS)", [
            ("01 No / Bad Phone", {"phone": 0, "skiptraced": 1, "must_not": _not(("recently sold",), OLD[:6])}),
            ("02 Obituary / Deceased", obit_must({"last_obituary_date": ["24-months", "quarter"]})),
            ("03 Exhausted Call -> DP", {"phone": 1, "skiptraced": 1, "predictivecall_attempts": [3, None],
                                         "ownerPropertiesOwned": {"show_properties": "one_per_owner"},
                                         "must_not": {"any_tags": [T["Mail Only"], T["recently sold"]],
                                                      "any_phone_status": list(DEAD4),
                                                      "any_property_status": ["sold"]}}),
            ("06 Obituary - Ready to Call", obit_must({"phone": 1, "predictivecall_attempts": [0, 0]})),
            ("07 Obituary - Call Attempt 1", obit_must({"phone": 1, "predictivecall_attempts": [1, 1]})),
            ("10 Obituary - Ready to Mail", obit_must({"directmail_attempts": [0, 0]})),
        ]),
        ("12. REACTIVATION", [
            ("Not Interested - Maple (30d)", {"any_tags": [T["Maple"]], "any_property_status": ["not_interested"],
                                             "last_updated_date": [{"field": "status", "options": ["36-months", "month"]}],
                                             "must_not": {"any_tags": [T["recently sold"]]}}),
            ("Rehash - Ready (never answered)", {"phone": 1, "predictivecall_attempts": [3, None],
                                                "must_not": {"any_property_status": ["sold", "not_interested"]}}),
        ]),
        ("13. HOT AUCTION SALE", [("Hot Auction + Buy Box", {"all_tags": [T["Maple"], T["Auction Soon"]],
                                                            "all_lists": [L["Foreclosure"]], "must_not": _not()})]),
        ("14. CLEAN UP", [("Recently Sold (suppress)", {"any_tags": [T["recently sold"]]})]),
        ("19. QC", [("Board - Leads Missing Tasks", {"tasks_incomplete": [0, 0],
                                                    "any_property_status": ["New Lead", "Cold Lead"]})]),
        ("20. CALLER QUEUES", [("Jordan - Actively Prospecting", {"assigned_to": "jordan",
                                                                 "any_property_status": ["prospecting"]})]),
        ("21. Dispo Sequential Marketing", [
            ("Dispo - 03 Ready to Call", {"phone": 1, "any_lists": [L["Buyers"]], "predictivecall_attempts": [0, 0],
                                         "must_not": {"any_property_status": ["not_interested"]}}),
            ("Dispo - 04 Call Attempt 1", {"phone": 1, "any_lists": [L["Buyers"]], "predictivecall_attempts": [1, 1],
                                          "must_not": {"any_property_status": ["not_interested"]}}),
        ]),
    ]


class FakeCRM:
    """Just enough of the account: the listings the tool reads, a search that counts, and the two
    write routes. Like the real search it silently ignores a key it does not know."""

    def __init__(self, folders=None, recs=None, ignore=()):
        self.folders = []
        for title, presets in (folders if folders is not None else account()):
            fu = _id("folder", title)
            self.folders.append({"uuid": fu, "title": title, "presets": [
                {"uuid": _id("preset", pt), "title": pt, "folder": fu, "quick_filter": False, "type": "properties",
                 "filters": {"account": "acct", "must": copy.deepcopy(must)}} for pt, must in presets]})
        self.tags = {t: u for t, u in T.items()}
        self.published = list(PUBLISHED)
        self.status_listing_fails = False
        self.records = recs if recs is not None else records()
        self.ignore = set(ignore)
        self.calls = 0
        self.log = []
        self.corrupt = ""           # a preset title whose write is stored wrongly

    # ---- listings and writes
    def _presets(self):
        return [p for f in self.folders for p in f["presets"]]

    def preset(self, title):
        return next(p for p in self._presets() if p["title"] == title)

    def call(self, path, method="GET", body=None, headers=None):
        self.calls += 1
        self.log.append((method, path))
        if method == "GET" and path.startswith("/api/internal/filter-preset-folder/?"):
            rows = [{"uuid": f["uuid"], "title": f["title"]} for f in self.folders]
            return {"results": rows if "limit=999" in path else rows[:10]}
        m = re.match(r"^/api/internal/filter-preset-folder/([^/]+)/filter-preset/", path)
        if method == "GET" and m:
            rows = copy.deepcopy(next(f for f in self.folders if f["uuid"] == m.group(1))["presets"])
            return {"results": rows if "limit=999" in path else rows[:10]}
        if method == "GET" and path.startswith("/api/internal/tag/"):
            return {"results": [{"uuid": u, "title": t} for t, u in self.tags.items()]}
        if method == "GET" and path.startswith("/api/internal/list/"):
            return {"results": [{"uuid": u, "title": t} for t, u in L.items()]}
        if method == "GET" and path.startswith("/api/internal/global-status/"):
            if self.status_listing_fails:
                raise RuntimeError("HTTP 403 on GET /api/internal/global-status/")
            return {"next": None, "previous": None, "results": [
                {"uuid": _id("global", t), "title": t, "statuses": list(s), "hotness": None} for t, s in self.published]}
        m = re.match(r"^/api/internal/filter-preset/([^/]+)/$", path)
        if m and method == "GET":
            return copy.deepcopy(next(p for p in self._presets() if p["uuid"] == m.group(1)))
        if m and method == "PATCH":
            p = next(p for p in self._presets() if p["uuid"] == m.group(1))
            p["filters"] = copy.deepcopy(body["filters"])
            if p["title"] == self.corrupt:
                p["filters"]["must"]["must_not"]["any_property_status"].pop()
            return copy.deepcopy(p)
        if method == "POST" and path == "/api/internal/filter-preset/":
            f = next(f for f in self.folders if f["uuid"] == body["folder"])
            p = {"uuid": _id("preset", body["title"]), "title": body["title"], "folder": f["uuid"],
                 "quick_filter": body["quick_filter"], "type": "properties", "filters": copy.deepcopy(body["filters"])}
            f["presets"].append(p)
            return copy.deepcopy(p)
        raise AssertionError("the fake has no route for %s %s" % (method, path))

    # ---- the search
    def _has(self, r, key, v):
        tag_of, list_of = {u: t for t, u in self.tags.items()}, {u: t for t, u in L.items()}
        if key in ("any_tags", "all_tags"):
            hits = [tag_of.get(u) in r["tags"] for u in v]
            return any(hits) if key.startswith("any") else all(hits)
        if key in ("any_lists", "all_lists"):
            hits = [list_of.get(u) in r["lists"] for u in v]
            return any(hits) if key.startswith("any") else all(hits)
        if key == "any_property_status":
            return r["status"] in v
        if key == "any_phone_status":
            return any(p in v for p in r["phones"])
        if key == "any_mail_campaign_situation":
            return r["situation"] in v
        if key == "phone":
            return r["phone"] == bool(v)
        if key == "predictivecall_attempts":
            return v[0] <= r["attempts"] and (v[1] is None or r["attempts"] <= v[1])
        if key == "directmail_attempts":
            return v[0] <= r["mailed"] and (v[1] is None or r["mailed"] <= v[1])
        if key == "deceased":
            return r["deceased"] is bool(v)              # a blank mark is neither yes nor no
        if key == "do_not_mail_ever":
            return bool(v) is False                      # nobody carries the switch
        if key == "assigned_to":
            return r["assigned"] == v
        return None                                      # unknown: ignored, exactly like the real search

    def count(self, must):
        self.calls += 1
        self.log.append(("COUNT", ""))
        assert "ownerPropertiesOwned" not in must, "the search wants show_properties flat"
        for v in must.values():
            assert not (isinstance(v, list) and "36-months" in v), "a relative window reached the search"
        out = []
        for r in self.records:
            ok = True
            for k, v in must.items():
                if k in self.ignore or k == "show_properties":
                    continue
                if k == "must_not":
                    if any(self._has(r, k2, v2) for k2, v2 in v.items() if k2 not in self.ignore):
                        ok = False
                elif self._has(r, k, v) is False:
                    ok = False
            if ok:
                out.append(r)
        if must.get("show_properties") == "one_per_owner":
            return len({r["owner"] for r in out})
        return len(out)


def build(fake, **kw):
    kw.setdefault("max_requests", 100000)
    reasons = kw.pop("reasons", True)
    return pp.build_plan(pp.Reader(fake, now=DAY, **kw), reasons=reasons, day="2026-11-13")


def entry(plan, title):
    return next(e for e in plan["presets"] if e["title"] == title)


def packs(e):
    return [c["pack"] for c in e["changes"]]


class allowed:
    """Both switches on, for the tests that exercise the write path against the fake."""

    def __init__(self, value="1"):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get(pp.ALLOW_ENV)
        if self.value is None:
            os.environ.pop(pp.ALLOW_ENV, None)
        else:
            os.environ[pp.ALLOW_ENV] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(pp.ALLOW_ENV, None)
        else:
            os.environ[pp.ALLOW_ENV] = self.old


# ------------------------------------------------------------------------------------------- roles
def test_a_role_comes_from_the_folder_and_the_title():
    cases = [
        ("01. MAPLE - CALL", "Maple - 00 Needs Skipped", "pre_call", None),
        ("01. MAPLE - CALL", "Maple - 01 Skipped No Numbers", "pre_call", None),
        ("01. MAPLE - CALL", "Maple - 02 Ready to Call", "call_step", 0),
        ("01. MAPLE - CALL", "Maple - 05 Call Attempt 3", "call_step", 3),
        ("02. MAPLE - MAIL", "Maple - Mail 04 Letter", "mail_step", None),
        ("11. DEEP PROSPECTING (ALL TIERS)", "01 No / Bad Phone", "dp_queue", None),
        ("11. DEEP PROSPECTING (ALL TIERS)", "03 Exhausted Call -> DP", "exhausted", None),
        ("11. DEEP PROSPECTING (ALL TIERS)", pp.NO_LIVE_NUMBER_TITLE, "no_live_number", None),
        ("11. DEEP PROSPECTING (ALL TIERS)", "07 Obituary - Call Attempt 1", "call_step", 1),
        ("11. DEEP PROSPECTING (ALL TIERS)", "10 Obituary - Ready to Mail", "mail_step", None),
        ("12. REACTIVATION", "Not Interested - Maple (30d)", "reactivation", None),
        ("13. HOT AUCTION SALE", "Hot Auction + Buy Box", "hot_auction", None),
        ("14. CLEAN UP", "Recently Sold (suppress)", "other", None),
        ("15. Lead Management", "2. Qualified Lead Tasks", "qc_board", None),
        ("19. QC", "Board - No Tasks", "qc_board", None),
        ("20. CALLER QUEUES", "Jordan - To Start", "caller_queue", None),
        ("21. Dispo Sequential Marketing", "Dispo - 04 Call Attempt 1", "dispo", None),
        ("default", "Anything", "other", None),
    ]
    for folder, title, role, attempt in cases:
        c = pp.classify(folder, title)
        assert (c["role"], c["attempt"]) == (role, attempt), (folder, title, c)
    assert pp.classify("11. DEEP PROSPECTING (ALL TIERS)", "06 Obituary - Ready to Call")["lane"] == "obituary"
    assert pp.classify("09. BULK - CALL", "Bulk - 02 Ready to Call")["lane"] == "bulk"
    # the obituary lane cannot skip the list it is built on, and a probate filing leaves it
    lane = pp.packs_for("call_step", "obituary")
    assert "obituary_list" not in lane and "probate_list" in lane
    assert "obituary_list" in pp.packs_for("call_step", "") and "probate_list" not in pp.packs_for("call_step", "")
    assert pp.packs_for("reactivation", "obituary") == []


def test_every_role_in_the_table_names_packs_that_exist():
    for role, cfg in pp.ROLES.items():
        assert cfg.get("label"), role
        assert bool(cfg.get("packs")) != bool(cfg.get("leave")), "%s is either patched or left alone" % role
        for code in (cfg.get("packs") or []) + (cfg.get("measure") or []):
            assert code in pp.PACKS, (role, code)
    for code, pack in pp.PACKS.items():
        kinds = [k for k in ("skip_tags", "skip_lists", "skip_statuses", "require", "skip", "stop_skipping_statuses")
                 if pack.get(k)]
        assert len(kinds) == 1 and pack.get("say") and pack.get("why"), code
    assert set(pp.LEAD_STATUSES).isdisjoint(pp.FINISHED_STATUSES)


# --------------------------------------------------------------------------------------- additions
def test_a_call_step_gains_what_it_lacks_and_keeps_what_it_has():
    fake = FakeCRM()
    before = copy.deepcopy(fake.preset("Maple - 03 Call Attempt 1")["filters"]["must"])
    plan = build(fake)
    e = entry(plan, "Maple - 03 Call Attempt 1")
    assert e["action"] == "change" and e["role"] == "call_step" and e["attempt"] == 1
    assert packs(e) == ["do_not_market", "dnc_list", "lead_statuses", "finished_statuses", "dead_owner",
                        "obituary_list", "not_single_family", "one_per_owner", "live_number"]
    after, mn = e["must_after"], e["must_after"]["must_not"]
    # what the owner had is still there, first and in the same order
    assert mn["any_tags"][:2] == before["must_not"]["any_tags"]
    assert mn["any_lists"][:1] == before["must_not"]["any_lists"]
    assert mn["any_property_status"][:len(OLD)] == OLD and mn["any_neighborhood"] == ["Elm Hollow"]
    assert after["any_tags"] == before["any_tags"] and after["predictivecall_attempts"] == [1, 1]
    # what was missing is appended once
    assert mn["any_tags"][2:] == [T["Do Not Market"], T["Not Single Family"]]
    assert mn["any_lists"][1:] == [L["DNC"], L["Obituary"]]
    assert mn["any_property_status"][len(OLD):] == ["No Contact New Lead", "follow_up", "Auction Date Passed"]
    assert after["deceased"] == 0 and after["any_phone_status"] == pp.LIVE_NUMBER
    assert after["ownerPropertiesOwned"] == {"show_properties": "one_per_owner"}
    for values in mn.values():
        assert len(values) == len(set(map(str, values)))
    # the two tags that do not exist yet wait; nothing is invented in their place
    assert {(w["kind"], w["title"]) for w in e["waiting"]} == {("tag", "Litigator"), ("tag", "Heir Identified")}
    assert [p["title"] for p in plan["prerequisites"]] == ["Heir Identified", "Litigator"]
    # the plan changed nothing in the account
    assert fake.preset("Maple - 03 Call Attempt 1")["filters"]["must"] == before


def test_counts_now_after_and_by_rule():
    plan = build(FakeCRM())
    e = entry(plan, "Maple - 03 Call Attempt 1")
    # 11 records at one attempt; 8 leave for a reason each, and the 3 left belong to 2 owners
    assert (e["count_now"], e["count_after"]) == (11, 2)
    assert e["by_reason"] == {"do_not_market": 1, "dnc_list": 1, "lead_statuses": 1, "finished_statuses": 1,
                              "dead_owner": 1, "obituary_list": 1, "not_single_family": 1, "one_per_owner": 1,
                              "live_number": 1}
    assert (e["dead_owner_marked"], e["dead_owner_blank"]) == (1, 0) and not e["not_applied"]
    rule = {r["pack"]: r for r in plan["summary"]["by_reason"]}
    assert rule["dead_owner"]["leaving"] >= 2 and rule["mail_exhausted"]["returning"] >= 1
    assert plan["not_applied"] == []


def test_statuses_come_from_the_published_listing_and_the_table():
    fake = FakeCRM()
    plan = build(fake)
    paths = [p for m, p in fake.log if m == "GET"]
    assert [p for p in paths if p.startswith("/api/internal/global-status/")]
    assert not [p for p in paths if "/api/internal/status/" in p]         # the unlisted listing is never read
    # the listing names 8 statuses; the other 22 the tool knows come from its own table
    rows, notes = pp.known_statuses([{"title": t, "statuses": s} for t, s in PUBLISHED])
    assert len(rows) == len(STATUSES) == 30 and not notes
    assert [r["title"] for r in rows][:2] == ["Dead Lead", "Ghosting Lead"]
    after = entry(plan, "Maple - 03 Call Attempt 1")["must_after"]["must_not"]["any_property_status"]
    assert after[-3:] == ["No Contact New Lead", "follow_up", "Auction Date Passed"] and not plan["requests"]["notes"]
    # the spelling the account stores wins over the table's, whatever the case
    recs = records()
    recs[4]["status"] = "no contact new lead"                             # this account stores it in lower case
    lower = FakeCRM(recs=recs)
    lower.published = [("unqualified_lead", ["no contact new lead"])]
    after = entry(build(lower), "Maple - 03 Call Attempt 1")["must_after"]["must_not"]["any_property_status"]
    assert "no contact new lead" in after and "No Contact New Lead" not in after
    # a stored spelling that differs by more than case: the table's is not used, and the owner is told
    other = FakeCRM()
    other.published = [("unqualified_lead", ["No_Contact_New_Lead"]), ("qualified_lead", ["Lukewarm Lead"])]
    plan = build(other)
    e = entry(plan, "Maple - 03 Call Attempt 1")
    assert "No Contact New Lead" not in e["must_after"]["must_not"]["any_property_status"]
    assert ("status", "No Contact New Lead") in {(w["kind"], w["title"]) for w in e["waiting"]}
    notes = " ".join(plan["requests"]["notes"])
    assert "stores the status 'No_Contact_New_Lead'" in notes and "correct the table" in notes
    assert "'Lukewarm Lead' that the tool's table does not list" in notes
    assert "correct the table" in pp.render_md(plan)
    # the listing cannot be read: the table alone is used and the plan says so
    down = FakeCRM()
    down.status_listing_fails = True
    plan = build(down)
    assert "follow_up" in entry(plan, "Maple - 03 Call Attempt 1")["must_after"]["must_not"]["any_property_status"]
    assert any("could not be read" in n for n in plan["requests"]["notes"]) and plan["summary"]["changed"] == 20
    # a preset naming a status nobody knows gets the stored spelling as a hint
    flag = entry(build(FakeCRM()), "Board - Leads Missing Tasks")["flags"][0]
    assert "'New Lead'" in flag and "the stored spelling is 'new_lead'" in flag


def test_the_deceased_rule_waits_while_any_mark_is_blank():
    # 'deceased: no' is the only way the app can say it, and a blank mark fails that test too
    recs = records() + [rec(40, "o40", deceased=None, attempts=1)]
    plan = build(FakeCRM(recs=recs))
    e = entry(plan, "Maple - 03 Call Attempt 1")
    assert "dead_owner" not in packs(e) and "deceased" not in e["must_after"] and "dead_owner" not in e["held"]
    item = next(x for x in e["not_applied"] if x["pack"] == "dead_owner")
    assert (item["kind"], item["would_remove"], item["marked"], item["blank"]) == ("waiting", 2, 1, 1)
    # the living owner with the blank mark stays, and so does the dead one until the marks are fixed
    assert (e["count_now"], e["count_after"]) == (12, 4)
    total = next(r for r in plan["not_applied"] if r["pack"] == "dead_owner")
    assert total["kind"] == "waiting" and total["marked"] >= 1 and total["blank"] >= 1
    assert "blank mark" in total["because"] and not [h for h in plan["held_back"] if h["pack"] == "dead_owner"]
    assert "deceased" not in next(c for c in plan["creates"] if c["title"] == "Maple - 06 Call Attempt 4")["must_after"]
    md = pp.render_md(plan)
    for want in ("### Decide before you approve", "Skip records whose owner is marked deceased: waiting.",
                 "Waiting on the blank marks: skip records whose owner is marked deceased. Here it would remove 2 "
                 "(1 marked deceased, 1 with the mark blank)."):
        assert want in md, want
    # once every mark says yes or no, the rule is added like any other
    for r in recs:
        if r["deceased"] is None:
            r["deceased"] = False
    fixed = build(FakeCRM(recs=recs))
    assert "dead_owner" in packs(entry(fixed, "Maple - 03 Call Attempt 1")) and not fixed["not_applied"]
    # and the table can be told to add it anyway
    recs[-1]["deceased"] = None
    old = pp.PACKS["dead_owner"]["hold_if_blank"]
    pp.PACKS["dead_owner"]["hold_if_blank"] = False
    try:
        forced = entry(build(FakeCRM(recs=recs)), "Maple - 03 Call Attempt 1")
        assert forced["must_after"]["deceased"] == 0 and (forced["dead_owner_marked"], forced["dead_owner_blank"]) == (1, 1)
    finally:
        pp.PACKS["dead_owner"]["hold_if_blank"] = old


def test_the_first_to_market_lane_keeps_its_obituary_records():
    folders = [("05. FTM - CALL", [("FTM - 00 Needs Skipped", {"phone": 0, "any_tags": [T["Maple"]], "must_not": _not(),
                                                               "skiptraced": 0, "predictivecall_attempts": [0, 0]}),
                                   ("FTM - 02 Ready to Call", call_must(0, one_per_owner=True)),
                                   ("FTM - 03 Call Attempt 1", call_must(1))]),
               ("13. HOT AUCTION SALE", [("Hot Auction + Buy Box", {"all_tags": [T["Maple"], T["Auction Soon"]],
                                                                   "must_not": _not()})])]
    plan = build(FakeCRM(folders))
    e = entry(plan, "FTM - 03 Call Attempt 1")
    assert e["lane"] == "ftm" and "obituary_list" not in packs(e)
    assert L["Obituary"] not in e["must_after"]["must_not"]["any_lists"] and L["DNC"] in e["must_after"]["must_not"]["any_lists"]
    item = next(x for x in e["not_applied"] if x["pack"] == "obituary_list")
    assert (item["kind"], item["would_remove"]) == ("lane", 1) and "outranks the obituary lane" in item["because"]
    assert [x["pack"] for x in entry(plan, "FTM - 00 Needs Skipped")["not_applied"]] == ["obituary_list"]
    # the hot auction preset is not in that lane and keeps the rule
    assert "obituary_list" in packs(entry(plan, "Hot Auction + Buy Box"))
    total = next(r for r in plan["not_applied"] if r["pack"] == "obituary_list")
    assert (total["kind"], total["presets"], total["would_remove"]) == ("lane", 3, 2)
    md = pp.render_md(plan)
    assert "Skip records on the Obituary list: not applied in 3 presets." in md
    assert "Not applied in this lane: skip records on the Obituary list. It would have removed 1 here." in md
    assert pp.lane_skips("call_step", "ftm") and not pp.lane_skips("mail_step", "ftm")
    assert not pp.lane_skips("call_step", "obituary") and not pp.lane_skips("call_step", "")


def test_ready_to_call_already_shows_one_property_per_owner():
    e = entry(build(FakeCRM()), "Maple - 02 Ready to Call")
    assert "one_per_owner" not in packs(e) and "live_number" in packs(e)
    assert e["must_after"]["ownerPropertiesOwned"] == {"show_properties": "one_per_owner"}


def test_the_step_before_the_first_call_gets_no_dialing_rules():
    e = entry(build(FakeCRM()), "Maple - 00 Needs Skipped")
    assert e["role"] == "pre_call"
    assert "one_per_owner" not in packs(e) and "live_number" not in packs(e)
    assert {"do_not_market", "dnc_list", "dead_owner", "obituary_list"} <= set(packs(e))


def test_a_mail_step_gets_the_mail_rules_and_stops_skipping_exhausted():
    plan = build(FakeCRM())
    e = entry(plan, "Maple - Mail 01 Letter")
    assert packs(e) == ["do_not_market", "dnc_list", "lead_statuses", "finished_statuses", "dead_owner",
                        "not_single_family", "mail_tags", "do_not_mail_flag", "mail_exhausted"]
    mn = e["must_after"]["must_not"]
    assert "Exhausted" not in mn["any_property_status"] and "Return Mail" in mn["any_property_status"]
    assert {T["Return Mail"], T["Do Not Mail"], T["Do Not Market"]} <= set(mn["any_tags"])
    assert L["Obituary"] not in mn["any_lists"]                 # the list rule is for the call lanes
    assert e["must_after"]["do_not_mail_ever"] is False and "any_phone_status" not in e["must_after"]
    assert "ownerPropertiesOwned" not in e["must_after"]
    # 21 records, less the 3 whose status the preset already skips; then 7 leave and 1 comes back
    assert (e["count_now"], e["count_after"]) == (18, 12)
    assert e["by_reason"]["do_not_market"] == 2 and e["by_reason"]["dead_owner"] == 1
    assert e["by_reason"]["mail_exhausted"] == -1               # one exhausted record comes back
    # a relative window is resolved before it reaches the search (the fake refuses the raw form)
    assert entry(plan, "Maple - Mail 02 Postcard")["count_now"] == 0


def test_the_obituary_lane_keeps_its_list_and_drops_probate():
    plan = build(FakeCRM())
    e = entry(plan, "06 Obituary - Ready to Call")
    assert e["lane"] == "obituary" and "obituary_list" not in packs(e) and "probate_list" in packs(e)
    assert L["Obituary"] not in e["must_after"]["must_not"]["any_lists"]
    assert L["Probate"] in e["must_after"]["must_not"]["any_lists"] and e["must_after"]["all_lists"] == [L["Obituary"]]
    assert (e["count_now"], e["count_after"]) == (1, 0)          # the one record there has a probate filing
    m = entry(plan, "10 Obituary - Ready to Mail")
    assert "probate_list" in packs(m) and "mail_exhausted" in packs(m)
    assert any("Mail Only" in f for f in m["flags"])             # a mail preset that skips mail-only people
    assert T["Mail Only"] in m["must_after"]["must_not"]["any_tags"]      # flagged, never removed


def test_a_preset_is_never_told_to_skip_what_it_is_built_on():
    folders = [("01. MAPLE - CALL", [("Maple - 02 Ready to Call", dict(call_must(0), all_lists=[L["Obituary"]]))])]
    e = entry(build(FakeCRM(folders)), "Maple - 02 Ready to Call")
    assert "obituary_list" not in packs(e)
    assert L["Obituary"] not in e["must_after"]["must_not"]["any_lists"]
    assert any("built on the list Obituary" in c for c in e["conflicts"])
    # a filter the owner set differently is reported, not rewritten
    folders = [("01. MAPLE - CALL", [("Maple - 03 Call Attempt 1",
                                      dict(call_must(1), ownerPropertiesOwned={"show_properties": "all"}))])]
    e = entry(build(FakeCRM(folders)), "Maple - 03 Call Attempt 1")
    assert e["must_after"]["ownerPropertiesOwned"] == {"show_properties": "all"}
    assert any("already set" in c for c in e["conflicts"]) and "one_per_owner" not in packs(e)


def test_research_queues_and_caller_queues_get_only_their_own_rules():
    plan = build(FakeCRM())
    q = entry(plan, "01 No / Bad Phone")
    assert packs(q) == ["do_not_market", "dnc_list"]
    assert q["must_after"]["must_not"]["any_property_status"] == OLD[:6] and "deceased" not in q["must_after"]
    o = entry(plan, "02 Obituary / Deceased")
    assert packs(o) == ["do_not_market", "dnc_list", "probate_list"] and o["count_now"] is not None
    c = entry(plan, "Jordan - Actively Prospecting")
    assert packs(c) == ["do_not_market", "dnc_list", "dead_owner"]
    assert c["must_after"]["any_property_status"] == ["prospecting"]
    assert "any_property_status" not in c["must_after"]["must_not"]
    assert (c["count_now"], c["count_after"]) == (2, 1)
    h = entry(plan, "Hot Auction + Buy Box")
    assert "obituary_list" in packs(h) and "one_per_owner" not in packs(h) and "live_number" not in packs(h)


def test_second_chance_quality_and_buyer_presets_are_left_alone():
    fake = FakeCRM()
    plan = build(fake)
    for title, word in (("Not Interested - Maple (30d)", "Not Interested"), ("Rehash - Ready (never answered)", ""),
                        ("Board - Leads Missing Tasks", "lead statuses"), ("Dispo - 03 Ready to Call", "buyers"),
                        ("Dispo - 04 Call Attempt 1", "buyers"), ("Recently Sold (suppress)", "not an outreach")):
        e = entry(plan, title)
        assert e["action"] == "left_alone" and not e["changes"] and "must_after" not in e, title
        assert word in e["left_alone_because"], title
    assert plan["summary"]["left_alone"] == 6
    # left alone, but the owner is told what sits in it
    assert entry(plan, "Not Interested - Maple (30d)")["would_catch"] == 1
    assert any("no upper end" in f for f in entry(plan, "Rehash - Ready (never answered)")["flags"])
    assert any("'New Lead'" in f for f in entry(plan, "Board - Leads Missing Tasks")["flags"])
    # the buyer program stops at two attempts and is not grown
    assert not [c for c in plan["creates"] if c["title"].startswith("Dispo")]


def test_merging_never_repeats_or_reorders():
    ix = pp.Index([{"uuid": u, "title": t} for t, u in T.items()], [{"uuid": u, "title": t} for t, u in L.items()],
                  [{"title": s} for s in STATUSES])
    must = {"phone": 1, "must_not": {"any_tags": [T["Mail Only"], T["Do Not Market"], T["recently sold"]],
                                    "any_property_status": ["sold", "follow_up"]}}
    keep = copy.deepcopy(must)
    once = pp.patch(must, pp.packs_for("call_step", ""), ix)
    assert must == keep                                           # the input is never edited
    assert once["must"]["must_not"]["any_tags"][:3] == keep["must_not"]["any_tags"]
    assert once["must"]["must_not"]["any_tags"].count(T["Do Not Market"]) == 1
    assert once["must"]["must_not"]["any_property_status"][:2] == ["sold", "follow_up"]
    assert once["must"]["must_not"]["any_property_status"].count("follow_up") == 1
    twice = pp.patch(once["must"], pp.packs_for("call_step", ""), ix)
    assert twice["must"] == once["must"] and not twice["by_pack"]
    # a status written in another case is the same status
    again = pp.patch({"phone": 1, "must_not": {"any_property_status": ["Follow_Up"]}}, ["lead_statuses"], ix)
    assert "follow_up" not in again["must"]["must_not"]["any_property_status"]


# ----------------------------------------------------------------------------- the round and exhausted
def test_each_call_lane_grows_to_the_end_of_the_round():
    plan = build(FakeCRM())
    made = {c["title"]: c for c in plan["creates"]}
    four = made["Maple - 06 Call Attempt 4"]
    assert four["clone_of"] == "Maple - 05 Call Attempt 3" and four["folder"] == "01. MAPLE - CALL"
    assert four["must_after"]["predictivecall_attempts"] == [4, 4] and four["must_after"]["deceased"] == 0
    assert four["must_after"]["ownerPropertiesOwned"] == {"show_properties": "one_per_owner"}
    assert four["count"] == 0
    # Bulk already runs past attempt 4: nothing is created, and the step past the round is flagged
    assert not [t for t in made if t.startswith("Bulk")]
    assert any("past the end of the round" in f for f in entry(plan, "Bulk - 07 Call Attempt 5")["flags"])
    assert not entry(plan, "Bulk - 06 Call Attempt 4")["flags"]
    # the obituary lane stops at attempt 1, so it grows by three, and a number already taken is said out loud
    assert {"08 Obituary - Call Attempt 2", "09 Obituary - Call Attempt 3", "10 Obituary - Call Attempt 4"} <= set(made)
    assert any("already used" in f for f in made["10 Obituary - Call Attempt 4"]["flags"])
    assert L["Probate"] in made["08 Obituary - Call Attempt 2"]["must_after"]["must_not"]["any_lists"]
    assert pp.next_title("Maple - 05 Call Attempt 3", 3, 4) == "Maple - 06 Call Attempt 4"
    assert pp.next_title("Plain call attempt 3", 3, 4) == "Plain call attempt 4"


def test_exhausted_moves_to_the_end_of_the_round_and_splits_in_two():
    plan = build(FakeCRM())
    e = entry(plan, "03 Exhausted Call -> DP")
    assert packs(e)[:2] == ["round", "split"] and {"lead_statuses", "finished_statuses"} <= set(packs(e))
    after = e["must_after"]
    assert after["predictivecall_attempts"] == [pp.ROUND_ENDS_AT, None]
    assert "any_phone_status" not in after["must_not"] and after["phone"] == 1
    assert (e["count_now"], e["count_rewritten"], e["count_after"]) == (1, 1, 1)
    half = next(c for c in plan["creates"] if c["title"] == pp.NO_LIVE_NUMBER_TITLE)
    assert half["clone_of"] == "03 Exhausted Call -> DP"
    assert half["must_after"]["predictivecall_attempts"] == [0, pp.ROUND_ENDS_AT - 1]
    assert half["must_after"]["must_not"]["any_phone_status"] == DEAD4
    assert half["count"] == 2                                     # the two records with no live number
    # a record at its last attempt is in one preset only: attempt 4 and exhausted do not share a counter
    four = next(c for c in plan["creates"] if c["title"] == "Maple - 06 Call Attempt 4")["must_after"]
    assert four["predictivecall_attempts"][1] < after["predictivecall_attempts"][0]
    assert four["any_phone_status"] == half["must_after"]["must_not"]["any_phone_status"]


# ------------------------------------------------------------------------------------------ proofs
def test_a_filter_the_search_does_not_read_is_held_back():
    plan = build(FakeCRM(ignore={"deceased"}))
    assert "dead_owner" in [h["pack"] for h in plan["held_back"]]
    e = entry(plan, "Maple - 03 Call Attempt 1")
    assert "dead_owner" not in packs(e) and "deceased" not in e["must_after"] and "dead_owner" in e["held"]
    proof = next(p for p in plan["proofs"] if p["form"] == "require deceased")
    assert proof["moved"] is False and proof["with"] == proof["without"]


def test_returned_mail_waits_until_a_count_can_move():
    plan = build(FakeCRM())
    assert [h["pack"] for h in plan["held_back"]] == ["returned_mail"]
    assert "any_mail_campaign_situation" not in entry(plan, "Maple - Mail 01 Letter")["must_after"]["must_not"]
    recs = records() + [rec(30, "o30", situation="returned")]
    plan = build(FakeCRM(recs=recs))
    assert not plan["held_back"]
    e = entry(plan, "Maple - Mail 01 Letter")
    assert e["must_after"]["must_not"]["any_mail_campaign_situation"] == ["returned", "undeliverable"]
    assert e["by_reason"]["returned_mail"] == 1


def test_a_switch_nobody_carries_is_proven_the_other_way_round():
    plan = build(FakeCRM())
    proof = next(p for p in plan["proofs"] if p["form"] == "require do_not_mail_ever")
    assert proof["with"] == proof["without"] and proof["with_opposite"] == 0 and proof["moved"] is True
    assert "do_not_mail_flag" in packs(entry(plan, "Maple - Mail 01 Letter"))
    for form in ("skip a tag", "skip a list", "skip a status", "require deceased", "require ownerPropertiesOwned",
                 "require any_phone_status"):
        p = next(p for p in plan["proofs"] if p["form"] == form)
        assert p["moved"] and p["with"] < p["without"], form


# ------------------------------------------------------------------------------- read-only and apply
def test_plan_reads_and_counts_and_does_nothing_else():
    fake = FakeCRM()
    keep = copy.deepcopy(fake.folders)
    plan = build(fake)
    assert {m for m, _ in fake.log} == {"GET", "COUNT"}
    assert fake.folders == keep
    assert plan["requests"]["made"] == fake.calls and not plan["requests"]["errors"]
    assert not hasattr(pp.Reader, "call") and not hasattr(pp.Reader, "write")
    # every listing asks for all rows: the folder listing defaults to ten
    assert all("limit=" in p for m, p in fake.log if m == "GET" and "global-status" not in p)
    assert plan["summary"]["presets_read"] == sum(len(f["presets"]) for f in fake.folders)


def test_apply_sends_nothing_without_both_switches():
    fake = FakeCRM()
    plan = build(fake)
    keep, calls = copy.deepcopy(fake.folders), fake.calls
    for commit, env in ((False, None), (True, None), (False, "1"), (True, "0"), (True, "yes")):
        with allowed(env):
            assert pp.may_write(commit) is False
            done = pp.apply_plan(fake, plan, commit=commit, out=lambda *_: None)
            assert done["allowed"] is False and not done["created"] and not done["patched"] and done["would"]
            try:
                pp._write(fake, pp.may_write(commit), "/api/internal/filter-preset/x/", "PATCH", {})
                raise AssertionError("a write went through")
            except pp.Refused:
                pass
    assert fake.calls == calls and fake.folders == keep           # a dry run does not even read
    with allowed("1"):
        assert pp.may_write(True) is True and pp.may_write(False) is False


def test_a_second_plan_over_a_patched_account_proposes_nothing():
    fake = FakeCRM()
    plan = build(fake)
    with allowed("1"):
        done = pp.apply_plan(fake, plan, commit=True, out=lambda *_: None)
    assert len(done["patched"]) == plan["summary"]["changed"] and len(done["created"]) == plan["summary"]["to_create"]
    assert fake.preset("Maple - 06 Call Attempt 4")["filters"]["must"]["predictivecall_attempts"] == [4, 4]
    assert fake.preset("Maple - Mail 01 Letter")["filters"]["account"] == "acct"      # the rest of the filter survives
    again = build(fake)
    assert again["summary"]["changed"] == 0 and again["summary"]["to_create"] == 0, \
        [(e["title"], packs(e)) for e in again["presets"] if e["action"] == "change"]
    assert entry(again, pp.NO_LIVE_NUMBER_TITLE)["role"] == "no_live_number"
    assert entry(again, "03 Exhausted Call -> DP")["action"] == "already_complete"
    # the two tags still wait; once they exist only their rules are left to add
    assert [p["title"] for p in again["prerequisites"]] == ["Heir Identified", "Litigator"]
    fake.tags["Litigator"], fake.tags["Heir Identified"] = _id("tag", "Litigator"), _id("tag", "Heir Identified")
    third = build(fake)
    assert third["summary"]["changed"] > 0 and not third["prerequisites"]
    assert {c["pack"] for e in third["presets"] for c in e["changes"]} == {"litigator", "heir_tag"}
    with allowed("1"):
        pp.apply_plan(fake, third, commit=True, out=lambda *_: None)
    assert build(fake)["summary"]["changed"] == 0


def test_a_run_that_stopped_part_way_is_finished_by_running_it_again():
    fake = FakeCRM()
    plan = build(fake)
    first_new = plan["creates"][0]["title"]
    first_change = next(e["title"] for e in plan["presets"] if e["action"] == "change")
    with allowed("1"):
        part = pp.apply_plan(fake, plan, commit=True, only=[first_new.lower(), first_change.lower()], out=lambda *_: None)
        assert part["created"] == [first_new] and part["patched"] == [first_change]
        fake.log.clear()
        rest = pp.apply_plan(fake, plan, commit=True, out=lambda *_: None)
    assert first_new in rest["already"] and first_change in rest["already"]
    assert len(rest["created"]) == plan["summary"]["to_create"] - 1 and len(rest["patched"]) == plan["summary"]["changed"] - 1
    sent_titles = [b.get("title") for m, b in fake.log if m == "POST" and isinstance(b, dict)]
    assert first_new not in sent_titles, "a preset that already reads as planned was created again"
    assert build(fake)["summary"]["changed"] == 0
    # a preset of the same name with a different filter is not ours: the run stops
    fake2 = FakeCRM()
    plan2 = build(fake2)
    with allowed("1"):
        pp.apply_plan(fake2, plan2, commit=True, only=[plan2["creates"][0]["title"].lower()], out=lambda *_: None)
        fake2.preset(plan2["creates"][0]["title"])["filters"]["must"]["predictivecall_attempts"] = [9, 9]
        try:
            pp.apply_plan(fake2, plan2, commit=True, out=lambda *_: None)
            raise AssertionError("a same-named preset with another filter was accepted")
        except pp.Refused as stop:
            assert "already exists with a different filter" in str(stop)


def test_apply_stops_on_the_first_read_back_that_differs():
    fake = FakeCRM()
    plan = build(fake)
    order = [e["title"] for e in plan["presets"] if e["action"] == "change"]
    fake.corrupt = order[2]
    fake.log.clear()
    with allowed("1"):
        try:
            pp.apply_plan(fake, plan, commit=True, out=lambda *_: None)
            raise AssertionError("the mismatch was not caught")
        except pp.Refused as stop:
            assert order[2] in str(stop) and "reads back differently" in str(stop)
    assert len([1 for m, _ in fake.log if m == "PATCH"]) == 3     # nothing after the bad one was touched
    assert pp.sha(fake.preset(order[3])["filters"]["must"]) == entry(plan, order[3])["before_sha"]


def test_apply_refuses_a_preset_that_changed_after_the_plan():
    fake = FakeCRM()
    plan = build(fake)
    plan["creates"] = []
    first = next(e["title"] for e in plan["presets"] if e["action"] == "change")
    fake.preset(first)["filters"]["must"]["must_not"]["any_neighborhood"].append("Birch Row")
    fake.log.clear()
    with allowed("1"):
        try:
            pp.apply_plan(fake, plan, commit=True, out=lambda *_: None)
            raise AssertionError("a stale plan was applied")
        except pp.Refused as stop:
            assert "changed since the plan" in str(stop)
    assert not [1 for m, _ in fake.log if m != "GET"]
    # --only narrows a run to the presets named
    fake2 = FakeCRM()
    plan2 = build(fake2)
    with allowed("1"):
        done = pp.apply_plan(fake2, plan2, commit=True, only=["maple - 03 call attempt 1"], out=lambda *_: None)
    assert done["patched"] == ["Maple - 03 Call Attempt 1"] and not done["created"]


# -------------------------------------------------------------------------- budget, cache and report
def test_a_spent_budget_leaves_counts_blank_and_the_plan_whole():
    fake = FakeCRM()
    plan = build(fake, max_requests=20)
    assert plan["requests"]["not_counted"] > 0 and fake.calls <= 20 + 1
    assert plan["summary"]["presets_read"] == 26 and plan["summary"]["changed"] > 0
    assert any(e["count_now"] is None for e in plan["presets"])
    assert "request budget" in pp.render_md(plan) and "not counted" in pp.render_md(plan)


def test_the_clients_own_hourly_budget_ends_the_counting_quietly():
    class BudgetExhausted(RuntimeError):
        pass

    fake = FakeCRM()
    real, state = fake.count, {"n": 0}

    def count(must):
        state["n"] += 1
        if state["n"] > 30:
            raise BudgetExhausted("the app has used its requests for this hour")
        return real(must)

    fake.count = count
    plan = build(fake)
    assert state["n"] == 31                                       # refused once, then never asked again
    assert "used its requests for this hour" in plan["requests"]["stopped"] and not plan["requests"]["errors"]
    assert plan["requests"]["not_counted"] > 50 and plan["summary"]["changed"] == 20
    assert "used its requests for this hour" in pp.render_md(plan)


def test_a_rule_that_could_not_be_checked_is_not_proposed():
    fake = FakeCRM()
    plan = build(fake, max_requests=0)
    assert {m for m, _ in fake.log} == {"GET"}                    # the listings are still read, nothing is counted
    # every pack is held except the two whose tag does not exist yet: those wait for the tag instead
    assert {h["pack"] for h in plan["held_back"]} == set(pp.PACKS) - {"litigator", "heir_tag"}
    assert all("could not be checked" in h["because"] for h in plan["held_back"])
    assert all("Not checked" in p["note"] for p in plan["proofs"])
    # the only changes left are the ones that need no filter proof: the exhausted preset's two edits
    assert [e["title"] for e in plan["presets"] if e["action"] == "change"] == ["03 Exhausted Call -> DP"]
    assert all(c["pack"] in ("round", "split") for e in plan["presets"] for c in e["changes"])


def test_a_cache_file_answers_the_second_run():
    with tempfile.TemporaryDirectory() as tmp:
        cache = str(Path(tmp) / "cache.json")
        fake = FakeCRM()
        first = build(fake, cache_path=cache)
        used = fake.calls
        second = build(fake, cache_path=cache)
        assert fake.calls == used and second["requests"]["made"] == 0 and second["requests"]["from_cache"] > 0
        assert [e["count_after"] for e in second["presets"]] == [e["count_after"] for e in first["presets"]]
        j, m = pp.write_plan(first, tmp)
        assert j.name == "plan_2026-11-13.json" and m.name == "plan_2026-11-13.md"
        assert json.loads(j.read_text(encoding="utf-8"))["summary"] == first["summary"]


def test_the_report_is_for_the_owner():
    plan = build(FakeCRM())
    md = pp.render_md(plan)
    assert md.startswith("# Preset exclusions") and "Nothing has been changed in the account" in md
    for jargon in ("must_not", "any_tags", "any_lists", "uuid", "ownerPropertiesOwned", "predictivecall"):
        assert jargon not in md, jargon
    assert not UUID_RX.search(md)
    assert chr(8212) not in md and chr(8211) not in md
    for want in ("Maple - 03 Call Attempt 1", "11 records now, 2 after", "Skip records tagged Do Not Market: 1 leaves",
                 "Skip records whose owner is marked deceased: 1 leaves",
                 "Skip every lead status (adds No Contact New Lead, Follow Up)", "Create these first",
                 "the tag Litigator", "Left alone", "New presets", "Maple - 06 Call Attempt 4",
                 "Stop skipping the Exhausted status: 1 comes back", "Across the 5 presets that change here",
                 "Held back", "### 12. REACTIVATION", "1 preset of the kind 'housekeeping view'",
                 "5 new presets would be created", "## When you approve",
                 "**10 Obituary - Call Attempt 4 (new preset).** The number 10 is already used"):
        assert want in md, want
    # a preset that gets the changes of the one above it is one line, plus only the rules that move records
    block = md[md.index("**Maple - 05 Call Attempt 3**"):md.index("### 02. MAPLE - MAIL")]
    assert "The same 9 changes as Maple - 03 Call Attempt 1." in block and "1 record now, 0 after" in block
    assert block.count("\n- ") == 2 and "wrong or dead: 1 leaves" in block
    # the plan file carries what apply needs, and says which packs each role gets
    assert plan["table"]["reactivation"] == [] and "do_not_market" in plan["table"]["mail_step"]
    assert all(e.get("after_sha") for e in plan["presets"] if e["action"] == "change")


def test_the_client_is_built_on_the_key_and_never_logs_in():
    import datasift_api_upload as up
    old_key, old_mint = os.environ.get("REISIFT_API_KEY"), up.Api._mint
    minted = []
    up.Api._mint = lambda self: minted.append(1)
    try:
        os.environ["REISIFT_API_KEY"] = "not-a-real-key"
        crm = pp.key_only_crm(gap=2.0)
        assert crm.api.api_key == "not-a-real-key" and not crm.api.token and not minted
        assert crm.min_gap >= 2.0 and crm.calls == 0
    finally:
        up.Api._mint = old_mint
        if old_key is None:
            os.environ.pop("REISIFT_API_KEY", None)
        else:
            os.environ["REISIFT_API_KEY"] = old_key


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/agents/autopilot/preset_patch.py", "tests/test_preset_patch.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text, name


def main() -> int:
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
